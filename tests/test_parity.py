"""Tests for nr2grafana.parity and NerdGraphClient.run_nrql.

All HTTP is stubbed: NerdGraph via a _post override, Grafana via fake
objects exposing resolve_ds_map/ds_query, so no network is involved.
"""

import unittest

from nr2grafana.nerdgraph import NerdGraphClient, NerdGraphError
from nr2grafana.parity import (
    _facet_attrs, _nrql_with_range, compare, normalize_grafana,
    normalize_nr, readiness, run_parity)


# ---------------------------------------------------------------------------
# Helpers: canned data
# ---------------------------------------------------------------------------

def nr_timeseries(values, begin=100, step=60, key="count"):
    rows = []
    for i, v in enumerate(values):
        rows.append({"beginTimeSeconds": begin + i * step,
                     "endTimeSeconds": begin + (i + 1) * step,
                     key: v})
    return rows


def gf_frame(times_s, values, labels=None, name="Value"):
    fields = [{"name": "Time", "type": "time"},
              {"name": name, "type": "number"}]
    if labels:
        fields[1]["labels"] = labels
    return {"schema": {"fields": fields},
            "data": {"values": [[t * 1000 for t in times_s],
                                list(values)]}}


def gf_response(frames, ref_id="A"):
    return {"results": {ref_id: {"frames": frames, "status": 200}}}


def series(points, labels=None):
    return {"labels": labels or {}, "points": points}


class FakeNR(object):
    """run_nrql served from {nrql-substring: results-or-Exception}."""

    def __init__(self, canned):
        self.canned = canned
        self.calls = []

    def run_nrql(self, account_id, nrql):
        self.calls.append((account_id, nrql))
        for key, val in self.canned.items():
            if key in nrql:
                if isinstance(val, Exception):
                    raise val
                return {"results": val, "metadata": {}}
        return {"results": [], "metadata": {}}


class FakeGrafana(object):
    """ds_query served from {expr: response-or-Exception}."""

    def __init__(self, responses, ds_map=None):
        self.responses = responses
        self.ds_map = ds_map if ds_map is not None else {
            "datasource": "prom-uid", "${datasource}": "prom-uid"}
        self.queries = []

    def resolve_ds_map(self, dash):
        return dict(self.ds_map)

    def ds_query(self, uid, ds_type, target, frm="now-1h", to="now"):
        self.queries.append((uid, ds_type, target, frm, to))
        expr = target.get("expr") or target.get("query") or ""
        val = self.responses.get(expr)
        if isinstance(val, Exception):
            raise val
        if val is None:
            return gf_response([], target.get("refId") or "A")
        return val


def prom_panel(pid, expr, refid="A", title=""):
    return {"id": pid, "type": "timeseries",
            "title": title or "P%d" % pid,
            "targets": [{"refId": refid, "expr": expr,
                         "datasource": {"type": "prometheus",
                                        "uid": "${datasource}"}}]}


# ---------------------------------------------------------------------------
# normalize_nr
# ---------------------------------------------------------------------------

class NormalizeNrTests(unittest.TestCase):
    def test_scalar_single_aggregate(self):
        out = normalize_nr([{"count": 5}],
                           "SELECT count(*) FROM Transaction")
        self.assertEqual(out, [{"labels": {}, "points": [[0.0, 5.0]]}])

    def test_scalar_dotted_key(self):
        out = normalize_nr([{"latest.duration": 1.5}],
                           "SELECT latest(duration) FROM Transaction")
        self.assertEqual(out, [{"labels": {},
                                "points": [[0.0, 1.5]]}])

    def test_multi_aggregate_row_splits_series(self):
        out = normalize_nr([{"count": 5, "average.duration": 1.2}],
                           "SELECT count(*), average(duration) "
                           "FROM Transaction")
        self.assertEqual(len(out), 2)
        labels = sorted(s["labels"]["aggregate"] for s in out)
        self.assertEqual(labels, ["average.duration", "count"])

    def test_timeseries_buckets(self):
        rows = nr_timeseries([1, 2, 3], begin=100, step=60)
        out = normalize_nr(rows, "SELECT count(*) FROM T TIMESERIES")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["points"],
                         [[100.0, 1.0], [160.0, 2.0], [220.0, 3.0]])

    def test_faceted_uses_facet_attr_name(self):
        rows = [{"facet": "web", "count": 5, "appName": "web"},
                {"facet": "api", "count": 7, "appName": "api"}]
        out = normalize_nr(rows, "SELECT count(*) FROM Transaction "
                                 "FACET appName")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["labels"], {"appName": "web"})
        self.assertEqual(out[1]["points"], [[0.0, 7.0]])

    def test_multi_facet_list(self):
        rows = [{"facet": ["web", "prod"], "count": 5}]
        out = normalize_nr(rows, "SELECT count(*) FROM T "
                                 "FACET appName, env LIMIT 10")
        self.assertEqual(out[0]["labels"],
                         {"appName": "web", "env": "prod"})

    def test_facet_without_known_attr(self):
        rows = [{"facet": "x", "count": 1}]
        out = normalize_nr(rows, "")
        self.assertEqual(out[0]["labels"], {"facet": "x"})

    def test_faceted_timeseries(self):
        rows = [{"facet": "web", "beginTimeSeconds": 10, "count": 1},
                {"facet": "web", "beginTimeSeconds": 70, "count": 2},
                {"facet": "api", "beginTimeSeconds": 10, "count": 9}]
        out = normalize_nr(rows, "SELECT count(*) FROM T FACET appName "
                                 "TIMESERIES")
        self.assertEqual(len(out), 2)
        by_label = {s["labels"]["appName"]: s["points"] for s in out}
        self.assertEqual(by_label["web"], [[10.0, 1.0], [70.0, 2.0]])
        self.assertEqual(by_label["api"], [[10.0, 9.0]])

    def test_facet_attrs_parsing(self):
        self.assertEqual(_facet_attrs("... FACET appName SINCE 1 day "
                                      "ago"), ["appName"])
        self.assertEqual(_facet_attrs("... FACET `host.name`, env "
                                      "LIMIT 5"), ["host.name", "env"])
        self.assertEqual(_facet_attrs("... FACET cases(WHERE x > 1) "
                                      "TIMESERIES"), ["facet"])
        self.assertEqual(_facet_attrs("SELECT count(*) FROM T"), [])


# ---------------------------------------------------------------------------
# normalize_grafana
# ---------------------------------------------------------------------------

class NormalizeGrafanaTests(unittest.TestCase):
    def test_timeseries_frame_ms_to_seconds(self):
        resp = gf_response([gf_frame([100, 160], [1.5, 2.5],
                                     labels={"job": "api"})])
        out = normalize_grafana(resp, "A")
        self.assertEqual(out, [{"labels": {"job": "api"},
                                "points": [[100.0, 1.5],
                                           [160.0, 2.5]]}])

    def test_multiple_value_fields_get_field_label(self):
        frame = {"schema": {"fields": [
                    {"name": "Time", "type": "time"},
                    {"name": "p50", "type": "number"},
                    {"name": "p99", "type": "number"}]},
                 "data": {"values": [[100000], [1.0], [9.0]]}}
        out = normalize_grafana(gf_response([frame]), "A")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["labels"], {"field": "p50"})
        self.assertEqual(out[1]["labels"], {"field": "p99"})

    def test_table_frame_rows_become_labeled_series(self):
        frame = {"schema": {"fields": [
                    {"name": "appName", "type": "string"},
                    {"name": "Value", "type": "number"}]},
                 "data": {"values": [["web", "api"], [5, 7]]}}
        out = normalize_grafana(gf_response([frame]), "A")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["labels"], {"appName": "web"})
        self.assertEqual(out[0]["points"], [[0.0, 5.0]])

    def test_null_values_skipped(self):
        resp = gf_response([gf_frame([100, 160, 220],
                                     [1.0, None, 3.0])])
        out = normalize_grafana(resp, "A")
        self.assertEqual(out[0]["points"], [[100.0, 1.0],
                                            [220.0, 3.0]])

    def test_missing_ref_id_is_empty(self):
        self.assertEqual(normalize_grafana(gf_response([]), "B"), [])
        self.assertEqual(normalize_grafana({}, "A"), [])


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

class CompareTests(unittest.TestCase):
    def ts(self, values, labels=None):
        return series([[100.0 + 60 * i, float(v)]
                       for i, v in enumerate(values)], labels)

    def test_match(self):
        r = compare([self.ts([1, 2, 3])], [self.ts([1, 2, 3])])
        self.assertEqual(r["verdict"], "match")
        self.assertEqual(r["nr_summary"]["points"], 3)
        self.assertEqual(r["gf_summary"]["mean"], 2.0)

    def test_scalar_match(self):
        r = compare([series([[0.0, 5.0]])], [series([[0.0, 5.0]])])
        self.assertEqual(r["verdict"], "match")

    def test_close_within_tolerance(self):
        r = compare([self.ts([10, 10, 10])], [self.ts([11, 11, 11])])
        self.assertEqual(r["verdict"], "close")
        self.assertIn("tolerance", r["detail"])

    def test_close_ratio_1000_ms_vs_s_with_noise(self):
        r = compare([self.ts([1, 2, 3])],
                    [self.ts([1010, 1990, 3020])])
        self.assertEqual(r["verdict"], "close")
        self.assertAlmostEqual(r["ratio"], 1000, delta=30)
        self.assertIn("ms vs s", r["detail"])

    def test_close_ratio_60_per_minute(self):
        r = compare([self.ts([1, 1, 1])], [self.ts([60, 61, 59])])
        self.assertEqual(r["verdict"], "close")
        self.assertIn("per-min", r["detail"])

    def test_close_ratio_inverse_1000(self):
        r = compare([self.ts([1000, 2000, 3000])],
                    [self.ts([1, 2, 3])])
        self.assertEqual(r["verdict"], "close")
        self.assertIn("s vs ms", r["detail"])

    def test_close_generic_constant_ratio(self):
        r = compare([self.ts([1, 2, 3])], [self.ts([7, 14, 21])])
        self.assertEqual(r["verdict"], "close")
        self.assertAlmostEqual(r["ratio"], 7.0, places=3)
        self.assertIn("constant ratio", r["detail"])

    def test_value_mismatch_scattered(self):
        r = compare([self.ts([1, 2, 3])], [self.ts([10, 1, 5])])
        self.assertEqual(r["verdict"], "value-mismatch")
        self.assertIsNone(r["ratio"])

    def test_shape_mismatch(self):
        nr = [self.ts([1], {"appName": "a"}),
              self.ts([2], {"appName": "b"}),
              self.ts([3], {"appName": "c"})]
        gf = [self.ts([9], {"job": "zzz"})]
        r = compare(nr, gf)
        self.assertEqual(r["verdict"], "shape-mismatch")

    def test_faceted_pairing_by_label_value(self):
        nr = [self.ts([1, 2], {"appName": "web"}),
              self.ts([5, 6], {"appName": "api"})]
        gf = [self.ts([5, 6], {"service": "API"}),
              self.ts([1, 2], {"service": "web"})]
        r = compare(nr, gf)
        self.assertEqual(r["verdict"], "match")

    def test_empties(self):
        empty = [series([])]
        data = [self.ts([1, 2])]
        self.assertEqual(compare([], [])["verdict"], "both-empty")
        self.assertEqual(compare(empty, data)["verdict"], "nr-empty")
        self.assertEqual(compare(data, [])["verdict"], "gf-empty")


# ---------------------------------------------------------------------------
# run_parity
# ---------------------------------------------------------------------------

class RunParityTests(unittest.TestCase):
    def make_inputs(self):
        dash = {"uid": "d1", "title": "Dash",
                "templating": {"list": []},
                "panels": [prom_panel(1, "expr_ok"),
                           prom_panel(2, "expr_gf_err"),
                           prom_panel(3, "expr_nr_err")]}
        report = [
            {"panel_id": 1, "nrql": ["SELECT count(*) FROM T Q1 "
                                     "TIMESERIES"],
             "queries": [{"datasource": "prometheus",
                          "expr": "expr_ok", "type": "range"}]},
            {"panel_id": 2, "nrql": ["SELECT count(*) FROM T Q2"],
             "queries": [{"datasource": "prometheus",
                          "expr": "expr_gf_err", "type": "range"}]},
            {"panel_id": 3, "nrql": ["SELECT count(*) FROM T Q3"],
             "queries": [{"datasource": "prometheus",
                          "expr": "expr_nr_err", "type": "range"}]},
        ]
        nr = FakeNR({
            "Q1": nr_timeseries([1, 2, 3]),
            "Q2": [{"count": 9}],
            "Q3": NerdGraphError("NRQL query failed: syntax"),
        })
        grafana = FakeGrafana({
            "expr_ok": gf_response([gf_frame([100, 160, 220],
                                             [1, 2, 3])]),
            "expr_gf_err": RuntimeError("HTTP 500 from /api/ds/query"),
            "expr_nr_err": gf_response([gf_frame([100], [4.0])]),
        })
        return nr, grafana, dash, report

    def test_full_run(self):
        nr, grafana, dash, report = self.make_inputs()
        logs = []
        out = run_parity(nr, [123], grafana, dash, report,
                         log=logs.append)
        self.assertEqual(out["schema"], "nr2grafana/parity/v1")
        self.assertEqual(out["dashboard"], "Dash")
        self.assertEqual(out["range"], {"from": "now-1h", "to": "now"})
        verdicts = {r["panel_id"]: r["verdict"] for r in out["panels"]}
        self.assertEqual(verdicts, {1: "match", 2: "gf-error",
                                    3: "nr-error"})
        rows = {r["panel_id"]: r for r in out["panels"]}
        self.assertIn("HTTP 500", rows[2]["detail"])
        self.assertIn("syntax", rows[3]["detail"])
        # nr-error with Grafana data earns the 0.6 weight:
        # (1.0 + 0.0 + 0.6) / 3 -> 53
        self.assertEqual(out["score"], 53)
        self.assertEqual(out["summary"],
                         {"match": 1, "gf-error": 1, "nr-error": 1})
        self.assertEqual(rows[1]["nrql"],
                         "SELECT count(*) FROM T Q1 TIMESERIES")
        self.assertEqual(rows[1]["datasource"], "prom-uid")
        self.assertTrue(logs)

    def test_range_appended_to_nrql(self):
        nr, grafana, dash, report = self.make_inputs()
        run_parity(nr, [123], grafana, dash, report, frm="now-30m")
        q1 = next(q for a, q in nr.calls if "Q1" in q)
        self.assertTrue(q1.endswith("SINCE 30 minutes AGO"), q1)
        self.assertEqual(nr.calls[0][0], 123)

    def test_no_nr_client_gf_data_scores_060(self):
        _, grafana, dash, report = self.make_inputs()
        dash["panels"] = [prom_panel(3, "expr_nr_err")]
        out = run_parity(None, [], grafana, dash, report)
        row = out["panels"][0]
        self.assertEqual(row["verdict"], "nr-error")
        self.assertIn("not configured", row["detail"])
        self.assertEqual(row["gf_summary"]["points"], 1)
        self.assertEqual(out["score"], 60)

    def test_unresolved_datasource_is_gf_error(self):
        nr, _, dash, report = self.make_inputs()
        grafana = FakeGrafana({}, ds_map={})
        dash["panels"] = [prom_panel(1, "expr_ok")]
        out = run_parity(nr, [123], grafana, dash, report)
        row = out["panels"][0]
        self.assertEqual(row["verdict"], "gf-error")
        self.assertIn("unresolved datasource", row["detail"])

    def test_second_account_tried_after_error(self):
        _, grafana, dash, report = self.make_inputs()
        dash["panels"] = [prom_panel(1, "expr_ok")]

        class FlakyNR(FakeNR):
            def run_nrql(self, account_id, nrql):
                if account_id == 1:
                    raise NerdGraphError("account 1 not accessible")
                return FakeNR.run_nrql(self, account_id, nrql)

        nr = FlakyNR({"Q1": nr_timeseries([1, 2, 3])})
        out = run_parity(nr, [1, 2], grafana, dash, report)
        self.assertEqual(out["panels"][0]["verdict"], "match")

    def test_never_raises_per_panel(self):
        nr = FakeNR({"Q": RuntimeError("boom")})
        grafana = FakeGrafana({"e": ValueError("bad")})
        dash = {"uid": "d", "title": "D",
                "panels": [prom_panel(1, "e")]}
        report = [{"panel_id": 1, "nrql": ["SELECT Q"],
                   "queries": [{"expr": "e"}]}]
        out = run_parity(nr, [1], grafana, dash, report)
        self.assertEqual(out["panels"][0]["verdict"], "gf-error")
        self.assertIn("bad", out["panels"][0]["detail"])
        self.assertIn("boom", out["panels"][0]["detail"])


class NrqlRangeTests(unittest.TestCase):
    def test_existing_since_kept(self):
        q = "SELECT count(*) FROM T SINCE 3 days ago"
        self.assertEqual(_nrql_with_range(q, "now-1h", "now"), q)

    def test_relative_appended(self):
        self.assertEqual(
            _nrql_with_range("SELECT c FROM T", "now-1h", "now"),
            "SELECT c FROM T SINCE 1 hour AGO")
        self.assertEqual(
            _nrql_with_range("SELECT c FROM T", "now-30m", "now-5m"),
            "SELECT c FROM T SINCE 30 minutes AGO UNTIL 5 minutes AGO")

    def test_epoch_ms_appended(self):
        self.assertEqual(
            _nrql_with_range("SELECT c FROM T", "1700000000000",
                             "1700003600000"),
            "SELECT c FROM T SINCE 1700000000000 UNTIL 1700003600000")


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------

class ReadinessTests(unittest.TestCase):
    def test_ready(self):
        parity = {"schema": "nr2grafana/parity/v1", "panels": [{}],
                  "score": 92, "summary": {"match": 3}}
        r = readiness(parity)
        self.assertEqual(r["grade"], "ready")
        self.assertEqual(r["score"], 92)
        self.assertTrue(r["reasons"])

    def test_almost(self):
        parity = {"panels": [{}], "score": 70,
                  "summary": {"match": 2, "gf-empty": 1}}
        r = readiness(parity)
        self.assertEqual(r["grade"], "almost")
        self.assertTrue(any("no data in Grafana" in s
                            for s in r["reasons"]))

    def test_missing_datasource_blocks(self):
        parity = {"panels": [{}], "score": 92,
                  "summary": {"match": 3}}
        checks = [{"item": "datasource:loki", "status": "missing",
                   "detail": "no datasource of type 'loki'",
                   "fix": "Add a Loki datasource"}]
        r = readiness(parity, check_rows=checks)
        self.assertEqual(r["grade"], "blocked")
        self.assertEqual(r["score"], 92 - 15)
        self.assertTrue(any("Loki" in s for s in r["reasons"]))

    def test_low_score_blocks(self):
        parity = {"panels": [{}], "score": 30,
                  "summary": {"value-mismatch": 2}}
        r = readiness(parity)
        self.assertEqual(r["grade"], "blocked")

    def test_no_parity_uses_test_rows(self):
        rows = [{"status": "data"}, {"status": "data"},
                {"status": "no-data"}, {"status": "error"}]
        r = readiness(None, test_rows=rows)
        self.assertEqual(r["score"], 50)
        self.assertEqual(r["grade"], "blocked")
        self.assertTrue(any("data-test coverage" in s
                            for s in r["reasons"]))


# ---------------------------------------------------------------------------
# NerdGraphClient.run_nrql
# ---------------------------------------------------------------------------

class StubNerd(NerdGraphClient):
    def __init__(self, response=None, error=None):
        NerdGraphClient.__init__(self, "NRAK-test")
        self.response = response
        self.error = error
        self.posts = []

    def _post(self, query, variables=None, retries=3):
        self.posts.append((query, variables))
        if self.error is not None:
            raise self.error
        return self.response


class RunNrqlTests(unittest.TestCase):
    def test_success(self):
        client = StubNerd({"actor": {"account": {"nrql": {
            "results": [{"count": 5}],
            "metadata": {"facets": None,
                         "timeWindow": {"begin": 1, "end": 2}}}}}})
        out = client.run_nrql(123, "SELECT count(*) FROM Transaction")
        self.assertEqual(out["results"], [{"count": 5}])
        self.assertEqual(out["metadata"]["timeWindow"]["end"], 2)
        query, variables = client.posts[0]
        self.assertIn("nrql(query: $q, timeout: 30)", query)
        self.assertEqual(variables,
                         {"id": 123,
                          "q": "SELECT count(*) FROM Transaction"})

    def test_null_account_is_actionable(self):
        client = StubNerd({"actor": {"account": None}})
        with self.assertRaises(NerdGraphError) as cm:
            client.run_nrql(999, "SELECT 1")
        self.assertIn("999", str(cm.exception))
        self.assertIn("access", str(cm.exception))

    def test_syntax_error_hint(self):
        client = StubNerd(
            error=NerdGraphError("NerdGraph errors: NRQL Syntax Error"))
        with self.assertRaises(NerdGraphError) as cm:
            client.run_nrql(123, "SELEKT nope")
        msg = str(cm.exception)
        self.assertIn("account 123", msg)
        self.assertIn("SELEKT nope", msg)

    def test_access_error_hint(self):
        client = StubNerd(
            error=NerdGraphError("NerdGraph errors: access denied"))
        with self.assertRaises(NerdGraphError) as cm:
            client.run_nrql(42, "SELECT 1 FROM T")
        self.assertIn("account 42", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
