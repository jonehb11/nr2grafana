"""Tests for nr2grafana.tco: the TCO trend engine.

Drives the engine against a fake ``awscost``-shaped module returning
canned Cost Explorer JSON (a multi-month upward trend, a CE forecast, and
one anomaly). Asserts the trend math, observability attribution, change
correlation, forecast, anomaly normalization and snapshot diffing.
"""

import os
import tempfile
import unittest

from nr2grafana import tco
from nr2grafana.store import Store


# ---------------------------------------------------------------------------
# Canned CE data + a fake awscost module/client
# ---------------------------------------------------------------------------

_MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04"]
# Upward trends (constant month-over-month deltas -> clean linreg).
_EC2 = [100.0, 110.0, 120.0, 130.0]     # +10/mo
_S3 = [50.0, 55.0, 60.0, 65.0]          # +5/mo
_XFER = [10.0, 12.0, 14.0, 16.0]        # +2/mo
# Totals: 160, 177, 194, 211  (+17/mo)


def _metrics(amount):
    return {"UnblendedCost": {"Amount": "%f" % amount, "Unit": "USD"}}


def _results_by_time():
    out = []
    for i, m in enumerate(_MONTHS):
        start = m + "-01"
        out.append({
            "TimePeriod": {"Start": start, "End": start},
            "Total": {},
            "Estimated": i == len(_MONTHS) - 1,
            "Groups": [
                {"Keys": ["Amazon Elastic Compute Cloud - Compute"],
                 "Metrics": _metrics(_EC2[i])},
                {"Keys": ["Amazon Simple Storage Service"],
                 "Metrics": _metrics(_S3[i])},
                {"Keys": ["AWS Data Transfer"],
                 "Metrics": _metrics(_XFER[i])},
            ],
        })
    return out


class FakeAws(object):
    """Duck-typed stand-in for the nr2grafana.awscost module."""

    def __init__(self):
        self.calls = []

    def get_cost_and_usage(self, start, end, granularity="MONTHLY",
                           group_by=None, metrics=None, filt=None,
                           region="us-east-1", profile=""):
        self.calls.append(("cau", start, end, granularity, group_by))
        return {"ResultsByTime": _results_by_time()}

    def get_cost_forecast(self, start, end, metric="UNBLENDED_COST",
                          granularity="MONTHLY", region="us-east-1",
                          profile=""):
        self.calls.append(("fc", start, end, metric))
        return {
            "Total": {"Amount": "720.0", "Unit": "USD"},
            "ForecastResultsByTime": [
                {"TimePeriod": {"Start": "2026-05-01", "End": "2026-06-01"},
                 "MeanValue": "230.0",
                 "PredictionIntervalLowerBound": "210.0",
                 "PredictionIntervalUpperBound": "250.0"},
                {"TimePeriod": {"Start": "2026-06-01", "End": "2026-07-01"},
                 "MeanValue": "240.0",
                 "PredictionIntervalLowerBound": "220.0",
                 "PredictionIntervalUpperBound": "260.0"},
                {"TimePeriod": {"Start": "2026-07-01", "End": "2026-08-01"},
                 "MeanValue": "250.0",
                 "PredictionIntervalLowerBound": "230.0",
                 "PredictionIntervalUpperBound": "270.0"},
            ],
        }

    def get_anomalies(self, start, end, region="us-east-1", profile=""):
        self.calls.append(("an", start, end))
        return [{
            "AnomalyId": "a-1",
            "AnomalyStartDate": "2026-04-02",
            "AnomalyEndDate": "2026-04-05",
            "DimensionValue": "Amazon Simple Storage Service",
            "Impact": {"MaxImpact": "40.0", "TotalImpact": "95.0",
                       "TotalActualSpend": "160.0",
                       "TotalExpectedSpend": "65.0"},
            "AnomalyScore": {"MaxScore": "0.9", "CurrentScore": "0.7"},
            "RootCauses": [{"Service": "Amazon S3", "Region": "us-east-1",
                            "UsageType": "TimedStorage-ByteHrs"}],
        }]


def _packing():
    return {"schema": "nr2grafana/packing/v1", "available": True,
            "topology": {"pool_cost_mo": 80.0},
            "packing_sim": {"current_pool_cost_mo": 80.0},
            "est_savings": {"monthly_usd": 25.0}}


def _deepdive():
    return {"schema": "nr2grafana/deepdive/v1", "available": True,
            "sections": {"network": {
                "wire_gb_month": 100.0,
                "wire_cost_by_path_mo": {"cross_az": 2.0, "tgw": 4.0,
                                         "nat": 6.0}}},
            "summary": {"total_est_monthly_usd": 40.0}}


# ---------------------------------------------------------------------------
# cost_series
# ---------------------------------------------------------------------------

class TestCostSeries(unittest.TestCase):

    def test_parses_groups_and_totals(self):
        aws = FakeAws()
        s = tco.cost_series(aws, months=4)
        self.assertEqual(s["months"], _MONTHS)
        self.assertEqual(s["currency"], "USD")
        self.assertEqual(s["group_by"], "SERVICE")
        self.assertEqual(
            s["groups"]["Amazon Elastic Compute Cloud - Compute"], _EC2)
        self.assertEqual(s["total"], [160.0, 177.0, 194.0, 211.0])
        self.assertEqual(s["estimated"][-1], True)
        # group_by forwarded as a CE GroupDefinition list.
        self.assertEqual(aws.calls[0][4],
                         [{"Type": "DIMENSION", "Key": "SERVICE"}])

    def test_total_only_when_no_groups(self):
        class NoGroups(object):
            def get_cost_and_usage(self, start, end, **kw):
                return {"ResultsByTime": [
                    {"TimePeriod": {"Start": "2026-03-01"},
                     "Total": {"UnblendedCost":
                               {"Amount": "42.5", "Unit": "USD"}}}]}
        s = tco.cost_series(NoGroups(), months=1)
        self.assertEqual(s["total"], [42.5])
        self.assertEqual(s["groups"], {})

    def test_raises_when_no_aws_method(self):
        with self.assertRaises(tco.TcoError):
            tco.cost_series(object(), months=3)

    def test_raises_actionable_on_call_failure(self):
        class Boom(object):
            def get_cost_and_usage(self, *a, **k):
                raise RuntimeError("access denied")
        with self.assertRaises(tco.TcoError) as ctx:
            tco.cost_series(Boom())
        self.assertIn("access denied", str(ctx.exception))


# ---------------------------------------------------------------------------
# trends
# ---------------------------------------------------------------------------

class TestTrends(unittest.TestCase):

    def setUp(self):
        self.series = tco.cost_series(FakeAws(), months=4)
        self.tr = tco.trends(self.series)

    def test_total_trend_math(self):
        t = self.tr["total"]
        self.assertEqual(t["first"], 160.0)
        self.assertEqual(t["last"], 211.0)
        self.assertEqual(t["delta_total"], 51.0)
        self.assertEqual(t["mom_deltas"], [17.0, 17.0, 17.0])
        self.assertEqual(t["direction"], "up")
        self.assertEqual(t["run_rate_monthly"], 211.0)
        self.assertEqual(t["run_rate_annual"], 2532.0)
        self.assertAlmostEqual(t["projection"]["slope_per_month"], 17.0)
        self.assertAlmostEqual(t["projection"]["next_month"], 228.0)
        self.assertAlmostEqual(t["projection"]["r2"], 1.0)

    def test_ec2_group_growth_and_cagr(self):
        g = self.tr["by_group"]["Amazon Elastic Compute Cloud - Compute"]
        self.assertEqual(g["mom_deltas"], [10.0, 10.0, 10.0])
        # pct growth: 10, 9.091, 8.333
        self.assertAlmostEqual(g["pct_growth"][0], 10.0)
        self.assertAlmostEqual(g["pct_growth"][1], 9.091, places=2)
        # monthly CAGR: (130/100)^(1/3) - 1 ~= 9.139%
        self.assertAlmostEqual(g["cagr_monthly_pct"], 9.139, places=2)

    def test_flat_direction(self):
        t = tco._trend_obj([100.0, 101.0, 100.5, 100.0])
        self.assertEqual(t["direction"], "flat")

    def test_down_direction(self):
        t = tco._trend_obj([200.0, 150.0, 120.0, 100.0])
        self.assertEqual(t["direction"], "down")

    def test_pct_growth_none_on_zero_prev(self):
        t = tco._trend_obj([0.0, 10.0])
        self.assertIsNone(t["pct_growth"][0])
        self.assertIsNone(t["cagr_monthly_pct"])


# ---------------------------------------------------------------------------
# attribute_observability
# ---------------------------------------------------------------------------

class TestAttribution(unittest.TestCase):

    def setUp(self):
        self.series = tco.cost_series(FakeAws(), months=4)

    def test_full_attribution(self):
        buckets = {"mimir-blocks": {"monthly_usd": 20.0},
                   "loki-chunks": {"monthly_usd": 10.0},
                   "tempo-traces": {"monthly_usd": 5.0}}
        a = tco.attribute_observability(
            self.series, deepdive=_deepdive(), packing=_packing(),
            buckets=buckets)

        # EC2: pool 80 vs EC2 line 130 -> 61.54%.
        self.assertEqual(a["ec2"]["obs_monthly_usd"], 80.0)
        self.assertEqual(a["ec2"]["service_total_usd"], 130.0)
        self.assertAlmostEqual(a["ec2"]["share_pct"], 61.54, places=1)

        # S3: 35 vs 65 -> 53.85%.
        self.assertEqual(a["s3"]["obs_monthly_usd"], 35.0)
        self.assertEqual(a["s3"]["service_total_usd"], 65.0)
        self.assertAlmostEqual(a["s3"]["share_pct"], 53.85, places=1)
        comps = sorted(r["component"] for r in a["s3"]["buckets"])
        self.assertEqual(comps, ["loki", "mimir", "tempo"])

        # Data transfer: median(2,4,6)=4 vs 16 -> 25%.
        dt = a["data_transfer"]
        self.assertEqual(dt["wire_gb_month"], 100.0)
        self.assertEqual(dt["point_usd"], 4.0)
        self.assertEqual(dt["low_usd"], 2.0)
        self.assertEqual(dt["high_usd"], 6.0)
        self.assertEqual(dt["service_total_usd"], 16.0)
        self.assertAlmostEqual(dt["share_pct"], 25.0)

        # Totals: 80 + 35 + 4 = 119 of 211.
        self.assertEqual(a["total_obs_monthly_usd"], 119.0)
        self.assertEqual(a["total_spend_latest_usd"], 211.0)
        self.assertAlmostEqual(a["obs_share_pct"], 56.4, places=1)
        self.assertTrue(a["assumptions"])

    def test_bucket_bytes_priced_with_assumption(self):
        buckets = {"mimir-blocks": {"bytes": tco.BYTES_PER_GB * 100}}
        a = tco.attribute_observability(self.series, buckets=buckets)
        # 100 GB * 0.023 = 2.3
        self.assertAlmostEqual(a["s3"]["obs_monthly_usd"], 2.3, places=3)
        self.assertTrue(any("S3 Standard assumption" in x
                            for x in a["assumptions"]))

    def test_degrades_without_inputs(self):
        a = tco.attribute_observability(self.series)
        self.assertIsNone(a["ec2"]["obs_monthly_usd"])
        self.assertIsNone(a["s3"]["obs_monthly_usd"])
        self.assertIn("note", a["ec2"])
        # empty series -> no crash
        a2 = tco.attribute_observability({})
        self.assertIsNone(a2["obs_share_pct"])


# ---------------------------------------------------------------------------
# correlate_changes
# ---------------------------------------------------------------------------

class TestCorrelateChanges(unittest.TestCase):

    def _dip_series(self):
        return {"months": ["2026-01", "2026-02", "2026-03"],
                "total": [100.0, 90.0, 95.0], "groups": {},
                "group_order": [], "currency": "USD"}

    def test_alignment_on_dip(self):
        changes = [
            {"ts": "2026-01-10T00:00:00Z", "action": "drop-metric",
             "target": "unused_metric", "source": "ai"},
            {"ts": "2026-02-10T00:00:00Z", "action": "drop-label",
             "target": "pod", "source": "user"},
        ]
        c = tco.correlate_changes(self._dip_series(), changes)
        ev = c["events"]
        self.assertEqual(ev[0]["observed"], "decrease")
        self.assertTrue(ev[0]["aligned"])
        self.assertEqual(ev[0]["delta_usd"], -10.0)
        self.assertEqual(ev[1]["observed"], "increase")
        self.assertFalse(ev[1]["aligned"])
        self.assertEqual(c["summary"]["measured"], 2)
        self.assertEqual(c["summary"]["aligned"], 1)
        self.assertIn("causation", c["note"].lower())

    def test_pending_and_out_of_range(self):
        s = self._dip_series()
        changes = [
            {"ts": "2026-03-10T00:00:00Z", "action": "x"},   # last month
            {"ts": "2025-12-10T00:00:00Z", "action": "y"},   # before series
        ]
        c = tco.correlate_changes(s, changes)
        obs = {e["action"]: e["observed"] for e in c["events"]}
        self.assertEqual(obs["x"], "pending")
        self.assertEqual(obs["y"], "unknown")
        self.assertEqual(c["summary"]["measured"], 0)

    def test_accepts_changelog_report_shape(self):
        report = {"dashboards": [
            {"changes": [{"ts": "2026-01-05T00:00:00Z", "action": "a",
                          "target": "t", "why": "w", "source": "auto"}]}]}
        c = tco.correlate_changes(self._dip_series(), report)
        self.assertEqual(len(c["events"]), 1)
        self.assertEqual(c["events"][0]["action"], "a")

    def test_accepts_changelog_object(self):
        class FakeCL(object):
            def report(self, slug=""):
                return {"dashboards": [{"changes": [
                    {"ts": "2026-02-01T00:00:00Z", "action": "b"}]}]}
        c = tco.correlate_changes(self._dip_series(), FakeCL())
        self.assertEqual(c["events"][0]["action"], "b")

    def test_event_savings_extracted(self):
        changes = [{"ts": "2026-01-10T00:00:00Z", "action": "drop",
                    "est_savings": {"monthly_usd": 12.5}}]
        c = tco.correlate_changes(self._dip_series(), changes)
        self.assertEqual(c["events"][0]["est_savings_usd"], 12.5)


# ---------------------------------------------------------------------------
# forecast
# ---------------------------------------------------------------------------

class TestForecast(unittest.TestCase):

    def test_linear_and_ce(self):
        aws = FakeAws()
        series = tco.cost_series(aws, months=4)
        fc = tco.forecast(aws, months=3, series=series)

        lin = fc["linear"]
        self.assertEqual([p["value"] for p in lin["by_month"]],
                         [228.0, 245.0, 262.0])
        self.assertEqual([p["month"] for p in lin["by_month"]],
                         ["2026-05", "2026-06", "2026-07"])
        self.assertEqual(lin["total_usd"], 735.0)
        self.assertAlmostEqual(lin["slope_per_month"], 17.0)

        ce = fc["ce_forecast"]
        self.assertTrue(ce["available"])
        self.assertEqual(ce["total_usd"], 720.0)
        self.assertEqual(len(ce["by_month"]), 3)
        self.assertEqual(ce["by_month"][0]["mean"], 230.0)
        self.assertEqual(ce["by_month"][0]["low"], 210.0)
        self.assertEqual(ce["by_month"][0]["high"], 250.0)

    def test_ce_failure_degrades(self):
        class NoFc(object):
            def get_cost_and_usage(self, *a, **k):
                return {"ResultsByTime": _results_by_time()}
        fc = tco.forecast(NoFc(), months=3)
        self.assertFalse(fc["ce_forecast"]["available"])
        self.assertIn("error", fc["ce_forecast"])
        # linear still computed from the fetched series.
        self.assertIsNotNone(fc["linear"])

    def test_never_raises_on_bad_aws(self):
        fc = tco.forecast(object(), months=2)
        self.assertFalse(fc["ce_forecast"]["available"])
        self.assertIsNone(fc["linear"])


# ---------------------------------------------------------------------------
# anomalies
# ---------------------------------------------------------------------------

class TestAnomalies(unittest.TestCase):

    def test_normalized(self):
        an = tco._normalize_anomalies(FakeAws().get_anomalies("a", "b"))
        self.assertEqual(len(an), 1)
        a = an[0]
        self.assertEqual(a["id"], "a-1")
        self.assertEqual(a["total_impact_usd"], 95.0)
        self.assertEqual(a["max_impact_usd"], 40.0)
        self.assertEqual(a["actual_spend_usd"], 160.0)
        self.assertEqual(a["expected_spend_usd"], 65.0)
        self.assertEqual(a["dimension"], "Amazon Simple Storage Service")
        self.assertEqual(a["root_causes"][0]["service"], "Amazon S3")

    def test_wrapped_dict_shape(self):
        raw = {"Anomalies": FakeAws().get_anomalies("a", "b")}
        self.assertEqual(len(tco._normalize_anomalies(raw)), 1)


# ---------------------------------------------------------------------------
# snapshots + trend_over_snapshots
# ---------------------------------------------------------------------------

class TestSnapshots(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()

    def _report(self, gen_at, run_rate):
        return {"schema": tco.SCHEMA, "generated_at": gen_at,
                "currency": "USD", "months": 4,
                "total": {"trend": {"last": run_rate,
                                    "run_rate_monthly": run_rate,
                                    "run_rate_annual": run_rate * 12,
                                    "direction": "up"}},
                "by_service": [{"service": "EC2", "latest": run_rate}],
                "observability_attribution": {"obs_share_pct": 50.0}}

    def test_snapshot_and_diff(self):
        tco.snapshot(self.store, self._report("2026-01-01T00:00:00Z", 100.0))
        tco.snapshot(self.store, self._report("2026-02-01T00:00:00Z", 120.0))
        tco.snapshot(self.store, self._report("2026-03-01T00:00:00Z", 110.0))

        trend = tco.trend_over_snapshots(self.store)
        self.assertEqual(len(trend["snapshots"]), 3)
        self.assertEqual(len(trend["diffs"]), 2)
        self.assertEqual(trend["diffs"][0]["run_rate_delta_usd"], 20.0)
        self.assertAlmostEqual(trend["diffs"][0]["pct"], 20.0)
        self.assertEqual(trend["diffs"][1]["run_rate_delta_usd"], -10.0)
        self.assertEqual(trend["overall"]["run_rate_delta_usd"], 10.0)

    def test_same_day_snapshot_replaces(self):
        tco.snapshot(self.store, self._report("2026-01-01T00:00:00Z", 100.0))
        tco.snapshot(self.store, self._report("2026-01-01T09:00:00Z", 150.0))
        trend = tco.trend_over_snapshots(self.store)
        self.assertEqual(len(trend["snapshots"]), 1)
        self.assertEqual(trend["snapshots"][0]["run_rate_monthly"], 150.0)

    def test_empty_store(self):
        trend = tco.trend_over_snapshots(self.store)
        self.assertEqual(trend["snapshots"], [])
        self.assertEqual(trend["diffs"], [])


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

class TestAnalyze(unittest.TestCase):

    def test_full_report(self):
        aws = FakeAws()
        buckets = {"mimir-blocks": {"monthly_usd": 20.0}}
        report = tco.analyze(
            aws, deepdive=_deepdive(), packing=_packing(),
            change_log=[{"ts": "2026-02-10T00:00:00Z", "action": "drop"}],
            months=4, buckets=buckets)

        self.assertEqual(report["schema"], "nr2grafana/tco/v1")
        self.assertTrue(report["available"])
        self.assertEqual(report["currency"], "USD")
        self.assertEqual(report["total"]["trend"]["direction"], "up")
        self.assertEqual(len(report["total"]["series"]), 4)
        self.assertEqual(report["total"]["series"][0], ["2026-01", 160.0])

        # by_service ranked by latest spend.
        self.assertEqual(report["by_service"][0]["service"],
                         "Amazon Elastic Compute Cloud - Compute")
        self.assertEqual(report["by_service"][0]["latest"], 130.0)

        self.assertTrue(report["anomalies"])
        self.assertEqual(report["anomalies"][0]["id"], "a-1")
        self.assertTrue(report["total"]["forecast"]["ce_forecast"]
                        ["available"])
        self.assertIn("recommendations", report)
        self.assertTrue(report["recommendations"])
        self.assertTrue(report["assumptions"])

    def test_analyze_persists_snapshot(self):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "a.db"))
        try:
            tco.analyze(FakeAws(), store=store, months=4)
            snap = store.get_artifact(tco.SNAP_SLUG, tco.SNAP_KIND)
            self.assertIsNotNone(snap)
            self.assertEqual(len(snap["snapshots"]), 1)
        finally:
            store.close()

    def test_analyze_degrades_without_aws(self):
        report = tco.analyze(object(), months=4)
        self.assertFalse(report["available"])
        self.assertIn("note", report)
        self.assertEqual(report["total"]["series"], [])
        # recommendations still present (insufficient-data path).
        self.assertTrue(report["recommendations"])

    def test_recommendation_flags_upward_trend(self):
        report = tco.analyze(FakeAws(), deepdive=_deepdive(),
                             packing=_packing(), months=4)
        ids = [r["id"] for r in report["recommendations"]]
        self.assertIn("trend-up", ids)
        self.assertIn("act-on-savings", ids)


if __name__ == "__main__":
    unittest.main()
