"""Tests for nr2grafana.samples (raw sample pulls + human review).

All HTTP is stubbed: NerdGraph via a fake ``run_nrql``, Grafana via a
fake ``ds_query``, the Store via a small in-memory fake -- no network.
"""

import unittest

from nr2grafana.parity import readiness
from nr2grafana.samples import (
    VERDICTS, collect_samples, merge_samples, raw_sample_nrql,
    record_review, review_summary)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

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


def log_frame(ref_id, lines, start_ms=1700000000000):
    times = [start_ms + 1000 * i for i in range(len(lines))]
    return {"schema": {"refId": ref_id, "fields": [
                {"name": "Time", "type": "time"},
                {"name": "Line", "type": "string"}]},
            "data": {"values": [times, list(lines)]}}


def num_frame(ref_id, values, labels=None, start_ms=1700000000000):
    times = [start_ms + 60000 * i for i in range(len(values))]
    field = {"name": "Value", "type": "number"}
    if labels:
        field["labels"] = labels
    return {"schema": {"refId": ref_id, "fields": [
                {"name": "Time", "type": "time"}, field]},
            "data": {"values": [times, list(values)]}}


class FakeGrafana(object):
    """ds_query served from {expr-or-query: response-or-Exception}."""

    def __init__(self, responses, ds_map=None):
        self.responses = responses
        self.ds_map = ds_map if ds_map is not None else {
            "datasource": "prom-uid", "${datasource}": "prom-uid"}
        self.queries = []

    def resolve_ds_map(self, dash):
        return dict(self.ds_map)

    def ds_query(self, uid, ds_type, target, frm="now-1h", to="now"):
        self.queries.append((uid, ds_type, dict(target), frm, to))
        expr = target.get("expr") or target.get("query") or ""
        val = self.responses.get(expr)
        if isinstance(val, Exception):
            raise val
        ref = target.get("refId") or "A"
        if val is None:
            return {"results": {ref: {"status": 200, "frames": []}}}
        return {"results": {ref: {"status": 200, "frames": val}}}


class FakeStore(object):
    def __init__(self):
        self.artifacts = {}
        self.changes = []
        self.dashboards = {}

    def get_artifact(self, slug, kind):
        return self.artifacts.get((slug, kind))

    def save_artifact(self, slug, kind, data):
        self.artifacts[(slug, kind)] = data

    def log_change(self, slug, change):
        row = dict(change)
        row["slug"] = slug
        self.changes.append(row)
        return len(self.changes)

    def get_dashboard(self, slug):
        return self.dashboards.get(slug)


def panel(pid, expr, ds_type="prometheus", uid="${datasource}",
          refid="A", title=""):
    return {"id": pid, "type": "timeseries",
            "title": title or "P%d" % pid,
            "targets": [{"refId": refid, "expr": expr,
                         "datasource": {"type": ds_type,
                                        "uid": uid}}]}


def make_dash(panels):
    return {"uid": "d1", "title": "Dash",
            "templating": {"list": []}, "panels": panels}


# ---------------------------------------------------------------------------
# raw NRQL derivation
# ---------------------------------------------------------------------------

class RawNrqlTests(unittest.TestCase):
    def test_strips_aggregates_facet_timeseries(self):
        q = raw_sample_nrql(
            "SELECT count(*) FROM Log WHERE service = 'x' AND "
            "level = 'error' FACET level TIMESERIES SINCE 1 day ago",
            5)
        self.assertEqual(q, "SELECT * FROM Log WHERE service = 'x' "
                            "AND level = 'error' LIMIT 5")

    def test_no_where_clause(self):
        self.assertEqual(
            raw_sample_nrql("SELECT count(*) FROM Transaction "
                            "TIMESERIES", 3),
            "SELECT * FROM Transaction LIMIT 3")

    def test_no_from_returns_empty(self):
        self.assertEqual(raw_sample_nrql("SHOW EVENT TYPES", 5), "")
        self.assertEqual(raw_sample_nrql("", 5), "")


# ---------------------------------------------------------------------------
# collect_samples
# ---------------------------------------------------------------------------

class CollectSamplesTests(unittest.TestCase):
    def make_inputs(self):
        dash = make_dash([
            panel(1, "sum(rate(up[5m]))"),
            panel(2, '{service_name="web"} |= "err" | json',
                  ds_type="loki", uid="loki-uid", title="Logs"),
        ])
        report = [
            {"panel_id": 1, "account_ids": [123],
             "nrql": ["SELECT count(*) FROM Transaction Q1"],
             "queries": [{"expr": "sum(rate(up[5m]))"}]},
            {"panel_id": 2, "account_ids": [123],
             "nrql": ["SELECT count(*) FROM Log WHERE "
                      "service = 'web' Q2"],
             "queries": [{"expr": "x"}]},
        ]
        nr = FakeNR({
            "Q1": [{"count": 9}],
            "SELECT * FROM Log": [
                {"timestamp": 1700000000000,
                 "message": "boom happened", "level": "error"},
                {"timestamp": 1700000060000,
                 "message": "again", "level": "error"},
            ],
        })
        grafana = FakeGrafana({
            "sum(rate(up[5m]))": [num_frame("A", [1.0, 2.0, 3.0],
                                            {"job": "api"})],
            '{service_name="web"}': [log_frame("A", ["l1", "l2",
                                                     "l3"])],
        }, ds_map={"${datasource}": "prom-uid"})
        return nr, grafana, dash, report

    def test_both_sides_collected(self):
        nr, grafana, dash, report = self.make_inputs()
        logs = []
        out = collect_samples(nr, [123], grafana, dash, report,
                              limit=2, log=logs.append)
        self.assertEqual(out["schema"], "nr2grafana/samples/v1")
        self.assertEqual(out["limit"], 2)
        rows = {r["panel_id"]: r for r in out["panels"]}
        self.assertEqual(sorted(rows), [1, 2])
        # metric panel: NR aggregate rows, Grafana datapoints
        self.assertEqual(rows[1]["nr"]["kind"], "rows")
        self.assertEqual(rows[1]["nr"]["samples"], [{"count": 9}])
        self.assertEqual(rows[1]["grafana"]["kind"], "points")
        pts = rows[1]["grafana"]["samples"][0]
        self.assertEqual(pts["labels"], {"job": "api"})
        self.assertEqual(len(pts["points"]), 2)  # last `limit` points
        self.assertEqual(pts["points"][-1][1], 3.0)
        # log panel: derived SELECT * NRQL, Loki raw lines
        self.assertEqual(rows[2]["nr"]["kind"], "events")
        self.assertIn("SELECT * FROM Log WHERE service = 'web'",
                      rows[2]["nr"]["nrql"])
        self.assertIn("LIMIT 2", rows[2]["nr"]["nrql"])
        self.assertEqual(rows[2]["nr"]["samples"][0]["message"],
                         "boom happened")
        self.assertEqual(rows[2]["grafana"]["kind"], "logs")
        lines = [s["line"] for s in rows[2]["grafana"]["samples"]]
        self.assertEqual(lines, ["l2", "l3"])  # most recent 2
        self.assertTrue(
            rows[2]["grafana"]["samples"][0]["ts"].startswith("20"))
        self.assertTrue(logs)
        # the raw loki query was the bare stream selector
        loki_calls = [q for q in grafana.queries if q[1] == "loki"]
        self.assertEqual(loki_calls[0][2]["expr"],
                         '{service_name="web"}')
        self.assertEqual(loki_calls[0][2]["maxLines"], 2)

    def test_panel_filter(self):
        nr, grafana, dash, report = self.make_inputs()
        out = collect_samples(nr, [123], grafana, dash, report,
                              panel_id=2)
        self.assertEqual([r["panel_id"] for r in out["panels"]], [2])

    def test_errors_and_empty_never_raise(self):
        dash = make_dash([
            panel(1, "boom_expr"),
            panel(2, "empty_expr"),
        ])
        report = [
            {"panel_id": 1, "nrql": ["Q_ERR"],
             "queries": [{"expr": "boom_expr"}]},
            {"panel_id": 2, "nrql": ["Q_EMPTY"],
             "queries": [{"expr": "empty_expr"}]},
        ]
        nr = FakeNR({"Q_ERR": RuntimeError("nr exploded")})
        grafana = FakeGrafana(
            {"boom_expr": RuntimeError("HTTP 500 from ds/query")},
            ds_map={"${datasource}": "prom-uid"})
        out = collect_samples(nr, [1], grafana, dash, report)
        rows = {r["panel_id"]: r for r in out["panels"]}
        self.assertEqual(rows[1]["nr"]["kind"], "error")
        self.assertIn("nr exploded", rows[1]["nr"]["error"])
        self.assertEqual(rows[1]["grafana"]["kind"], "error")
        self.assertIn("HTTP 500", rows[1]["grafana"]["error"])
        self.assertEqual(rows[2]["nr"]["kind"], "empty")
        self.assertEqual(rows[2]["grafana"]["kind"], "empty")
        self.assertTrue(rows[2]["grafana"]["error"])

    def test_no_nr_client_and_no_accounts(self):
        dash = make_dash([panel(1, "up")])
        report = [{"panel_id": 1, "nrql": ["Q1"],
                   "queries": [{"expr": "up"}]}]
        grafana = FakeGrafana({}, ds_map={"${datasource}": "p"})
        out = collect_samples(None, [], grafana, dash, report)
        self.assertEqual(out["panels"][0]["nr"]["kind"], "error")
        self.assertIn("not configured",
                      out["panels"][0]["nr"]["error"])
        out = collect_samples(FakeNR({}), [], grafana, dash, report)
        self.assertIn("account id", out["panels"][0]["nr"]["error"])

    def test_unresolved_datasource(self):
        dash = make_dash([panel(1, "up")])
        report = [{"panel_id": 1, "nrql": ["Q1"],
                   "queries": [{"expr": "up"}]}]
        grafana = FakeGrafana({}, ds_map={})
        out = collect_samples(FakeNR({"Q1": [{"count": 1}]}), [1],
                              grafana, dash, report)
        gf = out["panels"][0]["grafana"]
        self.assertEqual(gf["kind"], "error")
        self.assertIn("unresolved datasource", gf["error"])

    def test_truncation_and_row_cap(self):
        long = "x" * 2000
        rows = [{"message": long, "level": "info"}
                for _ in range(20)]
        dash = make_dash([panel(1, '{a="b"}', ds_type="loki",
                                uid="loki-uid")])
        report = [{"panel_id": 1,
                   "nrql": ["SELECT count(*) FROM Log"],
                   "queries": [{"expr": "x"}]}]
        nr = FakeNR({"SELECT * FROM Log": rows})
        grafana = FakeGrafana({'{a="b"}': [log_frame("A",
                                                     [long] * 20)]})
        out = collect_samples(nr, [1], grafana, dash, report,
                              limit=4)
        nr_side = out["panels"][0]["nr"]
        self.assertEqual(len(nr_side["samples"]), 4)
        self.assertLess(len(nr_side["samples"][0]["message"]), 600)
        self.assertIn("truncated", nr_side["samples"][0]["message"])
        gf_side = out["panels"][0]["grafana"]
        self.assertEqual(len(gf_side["samples"]), 4)
        self.assertLess(len(gf_side["samples"][0]["line"]), 600)

    def test_passthrough_kind_rows_or_empty(self):
        dash = make_dash([panel(1, "trace-q", ds_type="tempo",
                                uid="tempo-uid")])
        report = [{"panel_id": 1, "nrql": [],
                   "queries": [{"expr": "trace-q"}]}]
        grafana = FakeGrafana({"trace-q": [{
            "schema": {"fields": [{"name": "traceID",
                                   "type": "string"},
                                  {"name": "duration",
                                   "type": "number"}]},
            "data": {"values": [["t1", "t2"], [10, 20]]}}]})
        out = collect_samples(None, [], grafana, dash, report)
        gf = out["panels"][0]["grafana"]
        self.assertEqual(gf["kind"], "rows")
        self.assertEqual(gf["samples"][0]["fields"],
                         ["traceID", "duration"])
        self.assertEqual(gf["samples"][0]["rows"][0], ["t1", 10])


# ---------------------------------------------------------------------------
# merge_samples
# ---------------------------------------------------------------------------

class MergeSamplesTests(unittest.TestCase):
    def test_merge_replaces_and_appends(self):
        old = {"schema": "s", "panels": [
            {"panel_id": 1, "refId": "A", "v": "old1"},
            {"panel_id": 2, "refId": "A", "v": "old2"}]}
        fresh = {"schema": "s", "generated_at": "now", "panels": [
            {"panel_id": 2, "refId": "A", "v": "new2"},
            {"panel_id": 3, "refId": "A", "v": "new3"}]}
        out = merge_samples(old, fresh)
        by = {(r["panel_id"], r["refId"]): r["v"]
              for r in out["panels"]}
        self.assertEqual(by, {(1, "A"): "old1", (2, "A"): "new2",
                              (3, "A"): "new3"})
        self.assertEqual(out["generated_at"], "now")

    def test_merge_into_nothing(self):
        fresh = {"panels": [{"panel_id": 1, "refId": "A"}]}
        self.assertEqual(merge_samples(None, fresh), fresh)
        self.assertEqual(merge_samples({}, fresh), fresh)


# ---------------------------------------------------------------------------
# review bookkeeping
# ---------------------------------------------------------------------------

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.store.dashboards["d"] = {
            "slug": "d", "data": make_dash([
                panel(1, "up"), panel(2, "down")])}

    def test_record_and_merge(self):
        e1 = record_review(self.store, "d", 1, "A", "confirmed",
                           note="looks right")
        self.assertEqual(e1["verdict"], "confirmed")
        self.assertTrue(e1["ts"])
        art = self.store.get_artifact("d", "review")
        self.assertEqual(art["schema"], "nr2grafana/review/v1")
        self.assertEqual(art["reviews"]["1:A"]["note"],
                         "looks right")
        record_review(self.store, "d", 2, "A", "rejected",
                      note="wrong data")
        record_review(self.store, "d", 1, "A", "unsure")
        art = self.store.get_artifact("d", "review")
        self.assertEqual(len(art["reviews"]), 2)
        self.assertEqual(art["reviews"]["1:A"]["verdict"], "unsure")
        # change log entries with before/after
        acts = [c for c in self.store.changes
                if c["action"] == "panel-review"]
        self.assertEqual(len(acts), 3)
        self.assertEqual(acts[2]["before"], "confirmed")
        self.assertEqual(acts[2]["after"], "unsure")

    def test_empty_ref_defaults_to_a(self):
        record_review(self.store, "d", 1, "", "confirmed")
        art = self.store.get_artifact("d", "review")
        self.assertIn("1:A", art["reviews"])

    def test_invalid_verdict_raises(self):
        for bad in ("", "yes", "CONFIRM", None):
            with self.assertRaises(ValueError):
                record_review(self.store, "d", 1, "A", bad)
        # case-insensitive acceptance of the real verdicts
        e = record_review(self.store, "d", 1, "A", "Confirmed")
        self.assertEqual(e["verdict"], "confirmed")
        self.assertEqual(sorted(VERDICTS),
                         ["confirmed", "rejected", "unsure"])

    def test_summary_counts_and_unreviewed(self):
        s = review_summary(self.store, "d")
        self.assertEqual(s["confirmed"], 0)
        self.assertEqual(s["unreviewed"], 2)
        record_review(self.store, "d", 1, "A", "confirmed")
        record_review(self.store, "d", 2, "A", "rejected")
        s = review_summary(self.store, "d")
        self.assertEqual((s["confirmed"], s["rejected"],
                          s["unsure"], s["unreviewed"]),
                         (1, 1, 0, 0))

    def test_summary_unknown_dashboard(self):
        s = review_summary(self.store, "nope")
        self.assertEqual(s["confirmed"], 0)
        self.assertIsNone(s["unreviewed"])


# ---------------------------------------------------------------------------
# readiness folding
# ---------------------------------------------------------------------------

class ReadinessReviewTests(unittest.TestCase):
    def parity(self, score=92, n_panels=2):
        return {"schema": "nr2grafana/parity/v1",
                "panels": [{} for _ in range(n_panels)],
                "score": score, "summary": {"match": n_panels}}

    def review(self, verdicts):
        reviews = {}
        for i, v in enumerate(verdicts):
            reviews["%d:A" % (i + 1)] = {"panel_id": i + 1,
                                         "refId": "A", "verdict": v}
        return {"schema": "nr2grafana/review/v1", "reviews": reviews}

    def test_rejected_blocks(self):
        r = readiness(self.parity(score=100),
                      review=self.review(["confirmed", "rejected"]))
        self.assertEqual(r["grade"], "blocked")
        self.assertTrue(any("rejected in human sample review" in s
                            for s in r["reasons"]))
        self.assertTrue(any("panel 2 [A]" in s for s in r["reasons"]))

    def test_all_confirmed_floors_score(self):
        r = readiness(self.parity(score=70),
                      review=self.review(["confirmed", "confirmed"]))
        self.assertEqual(r["grade"], "ready")
        self.assertGreaterEqual(r["score"], 90)
        self.assertTrue(any("human-verified" in s
                            for s in r["reasons"]))

    def test_partial_confirmation_does_not_floor(self):
        r = readiness(self.parity(score=70),
                      review=self.review(["confirmed"]))
        self.assertEqual(r["grade"], "almost")
        self.assertEqual(r["score"], 70)
        self.assertFalse(any("human-verified" in s
                             for s in r["reasons"]))

    def test_confirmed_without_denominator_does_not_floor(self):
        # No parity and no test rows: coverage cannot be proven, so
        # confirmations must not fake full human verification.
        r = readiness(None, review=self.review(["confirmed"]))
        self.assertFalse(any("human-verified" in s
                             for s in r["reasons"]))
        # Data-test rows work as the denominator when parity is
        # absent.
        rows = [{"status": "data"}, {"status": "no-data"}]
        r = readiness(None, test_rows=rows,
                      review=self.review(["confirmed", "confirmed"]))
        self.assertGreaterEqual(r["score"], 90)
        self.assertTrue(any("human-verified" in s
                            for s in r["reasons"]))

    def test_unsure_does_not_floor(self):
        r = readiness(self.parity(score=70),
                      review=self.review(["confirmed", "unsure"]))
        self.assertEqual(r["score"], 70)
        self.assertFalse(any("human-verified" in s
                             for s in r["reasons"]))

    def test_missing_datasource_still_blocks_confirmed(self):
        checks = [{"item": "datasource:loki", "status": "missing",
                   "detail": "", "fix": "Add Loki"}]
        r = readiness(self.parity(score=92), check_rows=checks,
                      review=self.review(["confirmed", "confirmed"]))
        self.assertEqual(r["grade"], "blocked")
        self.assertEqual(r["score"], 92 - 15)  # no floor when blocked
        self.assertFalse(any("human-verified" in s
                             for s in r["reasons"]))

    def test_no_review_is_unchanged(self):
        r = readiness(self.parity(score=92))
        self.assertEqual(r["grade"], "ready")
        self.assertEqual(r["score"], 92)
        r = readiness(self.parity(score=92), review={})
        self.assertEqual(r["score"], 92)


if __name__ == "__main__":
    unittest.main()
