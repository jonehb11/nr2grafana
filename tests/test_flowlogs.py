"""Tests for nr2grafana.flowlogs.

The module talks to CloudWatch Logs Insights only through an injectable
``query_fn(kind, query, ctx)``; these tests supply canned Logs Insights
results -- in BOTH the normalized list-of-dicts shape and the raw
``get-query-results`` list-of-{field,value} shape -- so the real
aggregation, AZ classification, port attribution and step-change logic run
without any AWS access.

The centerpiece is a reference cross-AZ scenario: an observability stack
whose gRPC replication/query fan-out on **port 9095** is ~91% of cross-AZ
bytes, confined to 2 of 3 AZs (Karpenter subnet gap), stepping up mid
window -- matching ARCHITECTURE-1.9 s0.1/s0.9 and the worked example.
"""

import unittest

from nr2grafana import flowlogs
from nr2grafana.flowlogs import analyze, detect_step_change, normalize_rows


GIB = 2 ** 30


def _bytes(gb):
    return int(gb * GIB)


# ---------------------------------------------------------------------------
# Canned Logs Insights fixtures (normalized list-of-dicts shape).
# ---------------------------------------------------------------------------

# Top talker flows: three cross-AZ (az1->az2) flows dominated by 9095, one
# same-AZ flow (excluded), one AZ-unresolvable flow (counted as unknown).
FLOWS_ROWS = [
    {"srcAddr": "10.0.1.5", "dstAddr": "10.0.2.9", "dstPort": "9095",
     "srcAz": "use1-az1", "dstAz": "use1-az2", "bytes": str(_bytes(500))},
    {"srcAddr": "10.0.1.6", "dstAddr": "10.0.2.10", "dstPort": "9095",
     "srcAz": "use1-az1", "dstAz": "use1-az2", "bytes": str(_bytes(400))},
    {"srcAddr": "10.0.1.7", "dstAddr": "10.0.2.11", "dstPort": "443",
     "srcAz": "use1-az1", "dstAz": "use1-az2", "bytes": str(_bytes(80))},
    {"srcAddr": "10.0.1.8", "dstAddr": "10.0.1.9", "dstPort": "9095",
     "srcAz": "use1-az1", "dstAz": "use1-az1", "bytes": str(_bytes(300))},
    {"srcAddr": "10.9.9.9", "dstAddr": "10.9.9.8", "dstPort": "9095",
     "srcAz": "", "dstAz": "", "bytes": str(_bytes(12))},
]

# Cross-AZ bytes by dst port (gb already divided): 9095=910, 443=80,
# 3100=10 -> 9095 is 91% of cross-AZ. Includes a same-AZ row to be excluded.
PORTS_ROWS = [
    {"dstPort": "9095", "srcAz": "use1-az1", "dstAz": "use1-az2",
     "gb": "910"},
    {"dstPort": "443", "srcAz": "use1-az1", "dstAz": "use1-az2", "gb": "80"},
    {"dstPort": "3100", "srcAz": "use1-az1", "dstAz": "use1-az2", "gb": "10"},
    {"dstPort": "9095", "srcAz": "use1-az1", "dstAz": "use1-az1",
     "gb": "500"},
]

# Daily cross-AZ GB, low for a week then stepping up -> step change on day 8.
DAILY_ROWS = (
    [{"day": "2026-08-2%d" % d, "srcAz": "use1-az1", "dstAz": "use1-az2",
      "gb": "10"} for d in range(4, 8)]           # 4 low days ~10
    + [{"day": "2026-08-2%d" % d, "srcAz": "use1-az1", "dstAz": "use1-az2",
        "gb": "120"} for d in range(8, 10)]        # step up
    + [{"day": "2026-08-3%d" % d, "srcAz": "use1-az1", "dstAz": "use1-az2",
        "gb": "120"} for d in range(0, 2)]
    # include a same-AZ row that must be excluded from the daily cross series
    + [{"day": "2026-08-24", "srcAz": "use1-az1", "dstAz": "use1-az1",
        "gb": "999"}]
)

ENI_MAP = {
    "10.0.1.5": {"az": "use1-az1", "workload": "mimir-ingester-zone-a-0"},
    "10.0.2.9": {"az": "use1-az2", "workload": "mimir-ingester-zone-b-1"},
    "10.0.1.6": {"az": "use1-az1", "workload": "mimir-querier-zone-a-3"},
    "10.0.2.10": {"az": "use1-az2", "workload": "mimir-ingester-zone-b-2"},
    "10.0.1.7": {"az": "use1-az1", "workload": "mimir-nlb-node-a"},
    "10.0.2.11": {"az": "use1-az2", "workload": "mimir-nlb-node-b"},
}


def make_query_fn(flows=None, ports=None, daily=None, raw=False, calls=None):
    """Build a query_fn dispatching canned rows per query kind.

    ``raw=True`` returns the AWS get-query-results shape to exercise
    :func:`normalize_rows`. ``calls`` (a list) records each invocation.
    """
    flows = FLOWS_ROWS if flows is None else flows
    ports = PORTS_ROWS if ports is None else ports
    daily = DAILY_ROWS if daily is None else daily
    table = {"flows": flows, "ports": ports, "daily": daily}

    def _wrap(rows):
        if not raw:
            return list(rows)
        return {"status": "Complete", "results": [
            [{"field": k, "value": str(v)} for k, v in r.items()]
            for r in rows]}

    def query_fn(kind, query, ctx):
        if calls is not None:
            calls.append((kind, query, ctx))
        return _wrap(table.get(kind, []))

    return query_fn


class ReferenceScenarioTests(unittest.TestCase):
    """The port-9095 cross-AZ reference scenario end to end."""

    def setUp(self):
        self.rep = analyze(
            log_group="/vpc/flow-logs", eni_map=ENI_MAP, days=8,
            query_fn=make_query_fn(), expected_azs=3)

    def test_available_and_schema(self):
        self.assertTrue(self.rep["available"])
        self.assertEqual(self.rep["schema"], "nr2grafana/flowlogs/v1")
        self.assertEqual(self.rep["log_group"], "/vpc/flow-logs")

    def test_dominant_port_is_9095_at_91pct(self):
        dom = self.rep["dominant_port"]
        self.assertEqual(dom["port"], 9095)
        self.assertAlmostEqual(dom["pct_of_cross_az"], 91.0, places=1)
        self.assertIn("gRPC", dom["label"])

    def test_port_breakdown_ranked_and_summed(self):
        ports = self.rep["ports"]
        self.assertEqual([p["port"] for p in ports], [9095, 443, 3100])
        total_pct = sum(p["pct_of_cross_az"] for p in ports)
        self.assertAlmostEqual(total_pct, 100.0, places=1)

    def test_same_az_excluded_from_cross_totals(self):
        # 500+400+80 GB cross; the 300 GB same-AZ 9095 flow is NOT counted.
        self.assertAlmostEqual(
            self.rep["totals"]["cross_az_gb"], 980.0, places=1)
        self.assertAlmostEqual(
            self.rep["totals"]["same_az_gb"], 300.0, places=1)

    def test_unknown_az_flow_not_counted_as_cross(self):
        self.assertAlmostEqual(
            self.rep["totals"]["unknown_az_gb"], 12.0, places=1)
        self.assertEqual(self.rep["counts"]["unknown_az_flows"], 1)

    def test_gb_per_day(self):
        # 980 GB over an 8-day window.
        self.assertAlmostEqual(
            self.rep["totals"]["cross_az_gb_per_day"], 122.5, places=1)

    def test_top_flows_sorted_with_workloads(self):
        top = self.rep["top_flows"]
        self.assertEqual(top[0]["gb"], 500.0)
        self.assertEqual(top[0]["dst_port"], 9095)
        self.assertEqual(top[0]["src_workload"], "mimir-ingester-zone-a-0")
        self.assertEqual(top[0]["dst_workload"], "mimir-ingester-zone-b-1")
        # every listed flow is genuinely cross-AZ
        for f in top:
            self.assertNotEqual(f["src_az"], f["dst_az"])

    def test_az_imbalance_detected(self):
        imb = self.rep["az_imbalance"]
        self.assertTrue(imb["imbalanced"])
        self.assertEqual(imb["az_count"], 2)
        self.assertEqual(imb["expected_az_count"], 3)
        self.assertEqual(imb["azs"], ["use1-az1", "use1-az2"])
        self.assertIn("Karpenter", imb["note"])

    def test_az_pairs(self):
        pairs = self.rep["az_pairs"]
        self.assertEqual(pairs[0]["src_az"], "use1-az1")
        self.assertEqual(pairs[0]["dst_az"], "use1-az2")

    def test_step_change_detected(self):
        step = self.rep["step_change"]
        self.assertTrue(step["detected"])
        self.assertEqual(step["date"], "2026-08-28")
        self.assertGreater(step["after_gb_per_day"],
                           step["before_gb_per_day"])

    def test_daily_series_excludes_same_az(self):
        # the 999 GB same-AZ day must not appear in the cross-AZ series
        for _, gb in self.rep["daily"]:
            self.assertLess(gb, 999.0)

    def test_queries_and_assumptions_present(self):
        kinds = [q["kind"] for q in self.rep["queries"]]
        self.assertEqual(sorted(kinds), ["daily", "flows", "ports"])
        self.assertTrue(self.rep["assumptions"])
        self.assertEqual(self.rep["query_errors"], [])


class RawShapeTests(unittest.TestCase):
    """The raw get-query-results shape normalizes identically."""

    def test_raw_shape_matches_normalized(self):
        rep = analyze(log_group="/vpc/flow-logs", eni_map=ENI_MAP, days=8,
                      query_fn=make_query_fn(raw=True), expected_azs=3)
        self.assertTrue(rep["available"])
        self.assertEqual(rep["dominant_port"]["port"], 9095)
        self.assertAlmostEqual(
            rep["dominant_port"]["pct_of_cross_az"], 91.0, places=1)


class NormalizeRowsTests(unittest.TestCase):

    def test_dict_with_results(self):
        raw = {"results": [[{"field": "a", "value": "1"},
                            {"field": "b", "value": "2"}]]}
        self.assertEqual(normalize_rows(raw), [{"a": "1", "b": "2"}])

    def test_dict_with_rows(self):
        self.assertEqual(
            normalize_rows({"rows": [{"a": "1"}]}), [{"a": "1"}])

    def test_list_of_dicts_passthrough(self):
        self.assertEqual(normalize_rows([{"a": "1"}]), [{"a": "1"}])

    def test_list_of_raw_rows(self):
        raw = [[{"field": "a", "value": "1"}]]
        self.assertEqual(normalize_rows(raw), [{"a": "1"}])

    def test_garbage_returns_empty(self):
        for junk in (None, 5, "x", {"nope": 1}, [5, "y"]):
            self.assertEqual(normalize_rows(junk), [])


class AzFallbackTests(unittest.TestCase):
    """When the flow-log format lacks az-id, resolve AZ via the ENI map."""

    def test_eni_map_resolves_az(self):
        flows = [
            {"srcAddr": "10.0.1.5", "dstAddr": "10.0.2.9", "dstPort": "9095",
             "bytes": str(_bytes(100))},
            {"srcAddr": "10.0.1.6", "dstAddr": "10.0.2.10", "dstPort": "9095",
             "bytes": str(_bytes(50))},
        ]
        ports = [{"dstPort": "9095", "gb": "150"}]  # no az -> excluded
        rep = analyze(log_group="/g", eni_map=ENI_MAP, days=5,
                      query_fn=make_query_fn(flows=flows, ports=ports,
                                             daily=[]), expected_azs=3)
        self.assertTrue(rep["available"])
        # AZ came entirely from the ENI map
        self.assertAlmostEqual(rep["totals"]["cross_az_gb"], 150.0, places=1)
        self.assertEqual(rep["az_imbalance"]["az_count"], 2)
        # ports query had no az fields -> falls back to flow-derived ports
        self.assertEqual(rep["dominant_port"]["port"], 9095)


class AwsRunnerTests(unittest.TestCase):
    """The default runner drives awscost.logs_insights_query correctly."""

    def test_calls_logs_insights_query_by_keyword(self):
        seen = {}

        class FakeAws(object):
            # Signature mirrors awscost.logs_insights_query exactly.
            def logs_insights_query(self, log_group, query, start_epoch,
                                    end_epoch, limit=1000, poll_interval=1.0,
                                    max_polls=60, region="us-east-1",
                                    profile=""):
                seen["log_group"] = log_group
                seen["start_epoch"] = start_epoch
                seen["end_epoch"] = end_epoch
                seen["region"] = region
                # raw Logs Insights results shape
                return {"status": "Complete", "results": [
                    [{"field": k, "value": str(v)} for k, v in r.items()]
                    for r in PORTS_ROWS]}

        rep = analyze(aws=FakeAws(), log_group="/vpc/fl", days=8,
                      region="us-west-2", eni_map=ENI_MAP, expected_azs=3)
        self.assertTrue(rep["available"])
        self.assertEqual(seen["log_group"], "/vpc/fl")
        self.assertEqual(seen["region"], "us-west-2")
        self.assertEqual(seen["end_epoch"] - seen["start_epoch"], 8 * 86400)
        self.assertEqual(rep["dominant_port"]["port"], 9095)

    def test_runner_from_module_import(self):
        # aws=None imports nr2grafana.awscost lazily; a runner is built even
        # though no query will succeed here (no query_fn / real AWS).
        from nr2grafana import awscost
        runner = flowlogs._awscost_runner(awscost, "/g", "us-east-1", "", 30)
        self.assertTrue(callable(runner))


class QueryBuilderTests(unittest.TestCase):

    def test_queries_bound_and_private_only(self):
        for q in (flowlogs.build_flows_query(50),
                  flowlogs.build_ports_query(20),
                  flowlogs.build_daily_query()):
            self.assertIn("action = 'ACCEPT'", q)
            self.assertIn("limit", q)
            self.assertIn("10\\.", q)         # RFC1918 private filter
            self.assertIn("172\\.16", q)

    def test_flow_limit_injected(self):
        self.assertIn("limit 7", flowlogs.build_flows_query(7))

    def test_no_customer_values(self):
        q = flowlogs.build_ports_query()
        for bad in ("348342704569", "arn:", "us-east-1c"):
            self.assertNotIn(bad, q)

    def test_port_label(self):
        self.assertIn("gRPC", flowlogs.port_label(9095))
        self.assertEqual(flowlogs.port_label(99999), "")
        self.assertEqual(flowlogs.port_label("nope"), "")


class StepChangeTests(unittest.TestCase):

    def test_detects_step(self):
        daily = [("d1", 10), ("d2", 10), ("d3", 10), ("d4", 100),
                 ("d5", 100), ("d6", 100)]
        step = detect_step_change(daily)
        self.assertTrue(step["detected"])
        self.assertEqual(step["date"], "d4")

    def test_flat_series_no_step(self):
        daily = [("d1", 10), ("d2", 10), ("d3", 10), ("d4", 10)]
        self.assertFalse(detect_step_change(daily)["detected"])

    def test_short_series_no_step(self):
        short = [("d1", 1), ("d2", 9)]
        self.assertFalse(detect_step_change(short)["detected"])

    def test_rise_from_zero(self):
        daily = [("d1", 0), ("d2", 0), ("d3", 50), ("d4", 50)]
        step = detect_step_change(daily)
        self.assertTrue(step["detected"])
        self.assertEqual(step["date"], "d3")


class DegradeTests(unittest.TestCase):
    """Never raise; degrade to a clear, actionable note."""

    def test_no_log_group(self):
        rep = analyze()
        self.assertFalse(rep["available"])
        self.assertIn("no VPC Flow Logs log group", rep["note"])
        self.assertEqual(rep["schema"], "nr2grafana/flowlogs/v1")
        self.assertEqual(rep["totals"]["cross_az_gb"], 0.0)

    def test_log_group_from_cfg(self):
        cfg = {"flowlogs": {"log_group": "/from/cfg", "expected_azs": 3}}
        rep = analyze(cfg=cfg, eni_map=ENI_MAP, days=8,
                      query_fn=make_query_fn())
        self.assertTrue(rep["available"])
        self.assertEqual(rep["log_group"], "/from/cfg")

    def test_runner_unavailable(self):
        class NoLogs(object):
            pass
        rep = analyze(aws=NoLogs(), log_group="/g")
        self.assertFalse(rep["available"])
        self.assertIn("logs_insights_query", rep["note"])

    def test_all_queries_fail(self):
        def boom(kind, query, ctx):
            raise RuntimeError("StartQuery denied")
        rep = analyze(log_group="/g", query_fn=boom)
        self.assertFalse(rep["available"])
        self.assertTrue(rep["query_errors"])
        self.assertIn("all flow-log queries failed", rep["note"])

    def test_partial_query_failure_still_reports(self):
        def flaky(kind, query, ctx):
            if kind == "daily":
                raise RuntimeError("timeout")
            return (FLOWS_ROWS if kind == "flows" else PORTS_ROWS)
        rep = analyze(log_group="/g", eni_map=ENI_MAP, days=8,
                      query_fn=flaky, expected_azs=3)
        self.assertTrue(rep["available"])
        self.assertEqual(rep["dominant_port"]["port"], 9095)
        self.assertTrue(any("daily" in e for e in rep["query_errors"]))
        self.assertFalse(rep["step_change"]["detected"])

    def test_no_cross_az_flows(self):
        same_only = [
            {"srcAddr": "10.0.1.1", "dstAddr": "10.0.1.2", "dstPort": "9095",
             "srcAz": "use1-az1", "dstAz": "use1-az1",
             "bytes": str(_bytes(50))}]
        rep = analyze(log_group="/g", days=5,
                      query_fn=make_query_fn(flows=same_only, ports=[],
                                             daily=[]))
        self.assertFalse(rep["available"])
        self.assertIn("no cross-AZ", rep["note"])
        # window/daily still populated for context
        self.assertIn("start_epoch", rep["window"])

    def test_never_raises_on_garbage(self):
        def junk(kind, query, ctx):
            return {"totally": "unexpected"}
        rep = analyze(log_group="/g", query_fn=junk)
        # unrecognized shape -> zero rows -> no-cross-az degrade, no crash
        self.assertFalse(rep["available"])

    def test_window_defaults_and_epochs(self):
        calls = []
        analyze(log_group="/g", days=8, query_fn=make_query_fn(calls=calls),
                eni_map=ENI_MAP)
        _, _, ctx = calls[0]
        self.assertEqual(ctx["end_epoch"] - ctx["start_epoch"], 8 * 86400)


if __name__ == "__main__":
    unittest.main()
