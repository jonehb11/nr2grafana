"""Tests for nr2grafana.compare (side-by-side render model).

All I/O is stubbed: NerdGraph via a fake exposing run_nrql, Grafana via a
fake exposing ds_query / resolve_ds_map / datasource_health /
datasource_by_uid. No network is involved.
"""

import unittest

from nr2grafana.compare import (
    _downsample, _iter_layout, _render_mode, _viz_for, build_comparison,
    datasource_flow)


# ---------------------------------------------------------------------------
# canned data helpers
# ---------------------------------------------------------------------------

def nr_ts(values, begin=100, step=60, key="count"):
    rows = []
    for i, v in enumerate(values):
        rows.append({"beginTimeSeconds": begin + i * step,
                     "endTimeSeconds": begin + (i + 1) * step, key: v})
    return rows


def nr_faceted_ts(facet_values, points, begin=100, step=60):
    rows = []
    for fv in facet_values:
        for i, v in enumerate(points):
            rows.append({"facet": fv, "appName": fv,
                         "beginTimeSeconds": begin + i * step,
                         "count": v})
    return rows


def gf_frame(times_s, values, labels=None, name="Value"):
    fields = [{"name": "Time", "type": "time"},
              {"name": name, "type": "number"}]
    if labels:
        fields[1]["labels"] = labels
    return {"schema": {"fields": fields},
            "data": {"values": [[t * 1000 for t in times_s],
                                list(values)]}}


def gf_table_frame(names, values, types):
    fields = [{"name": n, "type": t} for n, t in zip(names, types)]
    return {"schema": {"fields": fields}, "data": {"values": values}}


def gf_log_frame(times_s, lines):
    fields = [{"name": "Time", "type": "time"},
              {"name": "Line", "type": "string"}]
    return {"schema": {"fields": fields},
            "data": {"values": [[t * 1000 for t in times_s], list(lines)]}}


def gf_response(frames, ref_id="A"):
    return {"results": {ref_id: {"frames": frames, "status": 200}}}


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

    def __init__(self, responses, ds_map=None, health=None,
                 by_uid=None):
        self.responses = responses
        self.ds_map = ds_map or {}
        self.health = health or {"status": "ok", "message": "probe ok"}
        self.by_uid = by_uid or {}
        self.queries = []

    def resolve_ds_map(self, dash):
        return dict(self.ds_map)

    def ds_query(self, uid, ds_type, target, frm="now-1h", to="now"):
        self.queries.append((uid, ds_type, dict(target), frm, to))
        expr = target.get("expr") or target.get("query") or ""
        val = self.responses.get(expr)
        if isinstance(val, Exception):
            raise val
        if val is None:
            return gf_response([], target.get("refId") or "A")
        return val

    def datasource_health(self, uid):
        return dict(self.health)

    def datasource_by_uid(self, uid):
        return self.by_uid.get(uid)


# ---------------------------------------------------------------------------
# panel / report builders
# ---------------------------------------------------------------------------

def panel(pid, ptype, expr, nrql, refid="A", ds_type="prometheus",
          uid="prom-uid", title="", unit="", gridy=0):
    p = {"id": pid, "type": ptype, "title": title or "P%d" % pid,
         "gridPos": {"x": 0, "y": gridy, "w": 12, "h": 8},
         "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
         "targets": [{"refId": refid, "expr": expr,
                      "datasource": {"type": ds_type, "uid": uid}}]}
    p["_nrql"] = nrql
    return p


def report_for(dash):
    """Build a widget_report from panels' stashed _nrql fields."""
    rep = []
    for p in dash["panels"]:
        if p.get("type") == "row":
            continue
        rep.append({"panel_id": p["id"], "nrql": [p.get("_nrql", "")],
                    "queries": [{"expr": p["targets"][0].get("expr")}]})
    return rep


def dash_of(panels, title="Dash", uid="d1"):
    return {"uid": uid, "title": title, "templating": {"list": []},
            "panels": panels}


# ---------------------------------------------------------------------------
# small unit helpers
# ---------------------------------------------------------------------------

class HelperTests(unittest.TestCase):
    def test_viz_mapping(self):
        self.assertEqual(_viz_for({"type": "timeseries"}), "timeseries")
        self.assertEqual(_viz_for({"type": "stat"}), "stat")
        self.assertEqual(_viz_for({"type": "bargauge"}), "bar")
        self.assertEqual(_viz_for({"type": "table"}), "table")
        self.assertEqual(_viz_for({"type": "logs"}), "logs")
        self.assertEqual(_viz_for({"type": "gauge"}), "gauge")
        self.assertEqual(_viz_for({"type": "piechart"}), "piechart")
        self.assertEqual(_viz_for({"type": "text"}), "text")
        self.assertEqual(_viz_for({"type": "nodeGraph"}), "unsupported")

    def test_render_mode(self):
        self.assertEqual(_render_mode("timeseries"), "series")
        self.assertEqual(_render_mode("bar"), "series")
        self.assertEqual(_render_mode("piechart"), "series")
        self.assertEqual(_render_mode("stat"), "scalar")
        self.assertEqual(_render_mode("gauge"), "scalar")
        self.assertEqual(_render_mode("table"), "rows")
        self.assertEqual(_render_mode("logs"), "lines")
        self.assertEqual(_render_mode("text"), "text")

    def test_downsample_even_stride(self):
        pts = [[float(i), float(i)] for i in range(250)]
        out = _downsample(pts, 100)
        self.assertLessEqual(len(out), 100)
        self.assertGreater(len(out), 1)
        # even stride: constant gap between kept timestamps
        gaps = {out[i + 1][0] - out[i][0] for i in range(len(out) - 1)}
        self.assertEqual(len(gaps), 1)

    def test_downsample_small_untouched(self):
        pts = [[0.0, 1.0], [1.0, 2.0]]
        self.assertEqual(_downsample(pts, 100), pts)

    def test_iter_layout_flat_and_nested_rows(self):
        dash = {"panels": [
            {"id": 10, "type": "row", "title": "Page A", "panels": []},
            {"id": 1, "type": "timeseries", "gridPos": {}, "targets": []},
            {"id": 20, "type": "row", "title": "Page B",
             "panels": [{"id": 2, "type": "stat", "targets": []}]},
        ]}
        got = [(p["id"], row) for p, row in _iter_layout(dash)]
        self.assertEqual(got, [(1, "Page A"), (2, "Page B")])


# ---------------------------------------------------------------------------
# build_comparison: viz kinds
# ---------------------------------------------------------------------------

class VizKindTests(unittest.TestCase):
    def run_one(self, p, nr_canned, gf_canned, **kw):
        dash = dash_of([p])
        nr = FakeNR(nr_canned)
        grafana = FakeGrafana(gf_canned)
        out = build_comparison(nr, [123], grafana, {}, dash,
                               report_for(dash), **kw)
        return out["panels"][0], nr, grafana

    def test_timeseries_series_both_sides(self):
        p = panel(1, "timeseries", "prom_ts",
                  "SELECT count(*) FROM T Q1", unit="ms")
        row, _nr, _g = self.run_one(
            p, {"Q1": nr_ts([1, 2, 3])},
            {"prom_ts": gf_response([gf_frame([100, 160, 220],
                                              [1, 2, 3])])})
        self.assertEqual(row["viz"], "timeseries")
        self.assertEqual(row["nr"]["kind"], "series")
        self.assertEqual(row["grafana"]["kind"], "series")
        self.assertEqual(row["nr"]["unit"], "ms")
        self.assertEqual(row["verdict"], "match")
        self.assertEqual(len(row["nr"]["series"][0]["points"]), 3)

    def test_stat_scalar(self):
        p = panel(1, "stat", "prom_scalar",
                  "SELECT count(*) FROM T Q2")
        row, _nr, _g = self.run_one(
            p, {"Q2": [{"count": 42}]},
            {"prom_scalar": gf_response([gf_frame([100], [42.0])])})
        self.assertEqual(row["viz"], "stat")
        self.assertEqual(row["nr"]["kind"], "scalar")
        self.assertEqual(row["nr"]["scalar"], 42.0)
        self.assertEqual(row["grafana"]["scalar"], 42.0)
        self.assertEqual(row["verdict"], "match")

    def test_gauge_scalar(self):
        p = panel(1, "gauge", "prom_g", "SELECT latest(x) FROM T Q3")
        row, _nr, _g = self.run_one(
            p, {"Q3": [{"latest.x": 7.0}]},
            {"prom_g": gf_response([gf_frame([100], [7.0])])})
        self.assertEqual(row["nr"]["kind"], "scalar")
        self.assertEqual(row["nr"]["scalar"], 7.0)

    def test_bar_series(self):
        p = panel(1, "bargauge", "prom_bar",
                  "SELECT count(*) FROM T Q4 FACET appName")
        row, _nr, _g = self.run_one(
            p, {"Q4": nr_faceted_ts(["web"], [5, 6])},
            {"prom_bar": gf_response([gf_frame([100, 160], [5, 6],
                                               labels={"app": "web"})])})
        self.assertEqual(row["viz"], "bar")
        self.assertEqual(row["nr"]["kind"], "series")
        self.assertEqual(row["grafana"]["kind"], "series")

    def test_piechart_series(self):
        p = panel(1, "piechart", "prom_pie",
                  "SELECT count(*) FROM T Q5")
        row, _nr, _g = self.run_one(
            p, {"Q5": nr_ts([3, 3, 3])},
            {"prom_pie": gf_response([gf_frame([100, 160, 220],
                                               [3, 3, 3])])})
        self.assertEqual(row["viz"], "piechart")
        self.assertEqual(row["nr"]["kind"], "series")

    def test_table_rows(self):
        p = panel(1, "table", "prom_tbl",
                  "SELECT count(*) FROM T Q6 FACET appName")
        nr_rows = [{"facet": "web", "appName": "web", "count": 5},
                   {"facet": "api", "appName": "api", "count": 7}]
        gf = gf_response([gf_table_frame(
            ["appName", "Value"], [["web", "api"], [5, 7]],
            ["string", "number"])])
        row, _nr, _g = self.run_one(p, {"Q6": nr_rows}, {"prom_tbl": gf})
        self.assertEqual(row["viz"], "table")
        self.assertEqual(row["nr"]["kind"], "table")
        self.assertEqual(row["grafana"]["kind"], "table")
        self.assertEqual(len(row["nr"]["rows"]), 2)
        self.assertEqual(row["nr"]["rows"][0]["appName"], "web")
        self.assertEqual(row["nr"]["rows"][0]["value"], 5.0)

    def test_logs_lines(self):
        p = panel(1, "logs", '{job="app"}',
                  "SELECT count(*) FROM Log WHERE x LIMIT 100",
                  ds_type="loki", uid="loki-uid")
        nr_rows = [{"timestamp": 1700000000000, "message": "hello"},
                   {"timestamp": 1700000001000, "message": "world"}]
        gf = gf_response([gf_log_frame([1700000000, 1700000001],
                                       ["hello", "world"])])
        row, nr, _g = self.run_one(
            p, {"FROM Log": nr_rows}, {'{job="app"}': gf})
        self.assertEqual(row["viz"], "logs")
        self.assertEqual(row["nr"]["kind"], "logs")
        self.assertEqual(row["grafana"]["kind"], "logs")
        self.assertEqual(len(row["nr"]["lines"]), 2)
        self.assertEqual(row["nr"]["lines"][0]["line"], "hello")
        # both have 2 lines -> counts agree
        self.assertEqual(row["verdict"], "match")
        # raw SELECT * derived for the NR side
        self.assertTrue(any("SELECT * FROM Log" in q
                            for _a, q in nr.calls))

    def test_text_panel_no_query(self):
        p = {"id": 1, "type": "text", "title": "Notes",
             "gridPos": {"x": 0, "y": 0, "w": 24, "h": 3},
             "options": {}, "targets": []}
        dash = dash_of([p])
        out = build_comparison(FakeNR({}), [123], FakeGrafana({}), {},
                               dash, [])
        row = out["panels"][0]
        self.assertEqual(row["viz"], "text")
        self.assertEqual(row["verdict"], "both-empty")
        # text panels are not scored
        self.assertEqual(out["summary"], {})


# ---------------------------------------------------------------------------
# build_comparison: verdicts
# ---------------------------------------------------------------------------

class VerdictTests(unittest.TestCase):
    def verdict(self, nr_canned, gf_canned, nrql="SELECT c FROM T Q",
                expr="e"):
        p = panel(1, "timeseries", expr, nrql)
        dash = dash_of([p])
        out = build_comparison(FakeNR(nr_canned), [123],
                               FakeGrafana(gf_canned), {}, dash,
                               report_for(dash))
        return out["panels"][0]

    def test_match(self):
        r = self.verdict({"Q": nr_ts([1, 2, 3])},
                         {"e": gf_response([gf_frame([100, 160, 220],
                                                     [1, 2, 3])])})
        self.assertEqual(r["verdict"], "match")

    def test_close_ratio(self):
        r = self.verdict({"Q": nr_ts([1, 2, 3])},
                         {"e": gf_response([gf_frame([100, 160, 220],
                                                     [1010, 1990, 3020])])})
        self.assertEqual(r["verdict"], "close")
        self.assertIsNotNone(r["ratio"])

    def test_value_mismatch(self):
        r = self.verdict({"Q": nr_ts([1, 2, 3])},
                         {"e": gf_response([gf_frame([100, 160, 220],
                                                     [10, 1, 5])])})
        self.assertEqual(r["verdict"], "value-mismatch")

    def test_shape_mismatch(self):
        nr_rows = nr_faceted_ts(["a", "b", "c"], [1, 2])
        gf = gf_response([gf_frame([100, 160], [9, 9],
                                   labels={"job": "zzz"})])
        r = self.verdict({"Q": nr_rows}, {"e": gf},
                         nrql="SELECT c FROM T Q FACET appName")
        self.assertEqual(r["verdict"], "shape-mismatch")

    def test_nr_empty(self):
        r = self.verdict({}, {"e": gf_response([gf_frame([100], [1.0])])})
        self.assertEqual(r["verdict"], "nr-empty")

    def test_gf_empty(self):
        r = self.verdict({"Q": nr_ts([1, 2, 3])}, {})
        self.assertEqual(r["verdict"], "gf-empty")

    def test_both_empty(self):
        r = self.verdict({}, {})
        self.assertEqual(r["verdict"], "both-empty")

    def test_nr_error(self):
        r = self.verdict({"Q": RuntimeError("boom")},
                         {"e": gf_response([gf_frame([100], [1.0])])})
        self.assertEqual(r["verdict"], "nr-error")
        self.assertIn("boom", r["detail"])

    def test_gf_error(self):
        r = self.verdict({"Q": nr_ts([1, 2, 3])},
                         {"e": ValueError("bad ds")})
        self.assertEqual(r["verdict"], "gf-error")
        self.assertIn("bad ds", r["detail"])

    def test_both_error_detail_mentions_both(self):
        r = self.verdict({"Q": RuntimeError("nrbad")},
                         {"e": ValueError("gfbad")})
        self.assertEqual(r["verdict"], "gf-error")
        self.assertIn("gfbad", r["detail"])
        self.assertIn("nrbad", r["detail"])

    def test_nr_client_missing_is_nr_error(self):
        p = panel(1, "timeseries", "e", "SELECT c FROM T Q")
        dash = dash_of([p])
        out = build_comparison(
            None, [], FakeGrafana({"e": gf_response(
                [gf_frame([100], [1.0])])}), {}, dash, report_for(dash))
        row = out["panels"][0]
        self.assertEqual(row["verdict"], "nr-error")
        self.assertIn("not configured", row["nr"]["error"])

    def test_unresolved_datasource_is_gf_error(self):
        p = panel(1, "timeseries", "e", "SELECT c FROM T Q",
                  uid="${ds}")
        dash = dash_of([p])
        out = build_comparison(
            FakeNR({"Q": nr_ts([1, 2, 3])}), [123],
            FakeGrafana({}, ds_map={}), {}, dash, report_for(dash))
        row = out["panels"][0]
        self.assertEqual(row["verdict"], "gf-error")
        self.assertIn("unresolved datasource", row["grafana"]["error"])


# ---------------------------------------------------------------------------
# TIMESERIES augmentation, downsampling, caps, layout, summary/score
# ---------------------------------------------------------------------------

class AugmentationTests(unittest.TestCase):
    def test_timeseries_appended_for_graph_panel(self):
        p = panel(1, "timeseries", "e", "SELECT count(*) FROM T Q1")
        dash = dash_of([p])
        nr = FakeNR({"Q1": nr_ts([1, 2, 3])})
        out = build_comparison(nr, [123], FakeGrafana({}), {}, dash,
                               report_for(dash))
        q1 = next(q for _a, q in nr.calls if "Q1" in q)
        self.assertTrue(q1.rstrip().endswith("TIMESERIES"), q1)
        self.assertEqual(out["panels"][0]["nrql"], q1)

    def test_timeseries_not_appended_for_stat(self):
        p = panel(1, "stat", "e", "SELECT count(*) FROM T Q2")
        dash = dash_of([p])
        nr = FakeNR({"Q2": [{"count": 1}]})
        build_comparison(nr, [123], FakeGrafana({}), {}, dash,
                         report_for(dash))
        q2 = next(q for _a, q in nr.calls if "Q2" in q)
        self.assertNotIn("TIMESERIES", q2)

    def test_timeseries_not_duplicated(self):
        p = panel(1, "timeseries", "e",
                  "SELECT count(*) FROM T Q1 TIMESERIES")
        dash = dash_of([p])
        nr = FakeNR({"Q1": nr_ts([1, 2, 3])})
        build_comparison(nr, [123], FakeGrafana({}), {}, dash,
                         report_for(dash))
        q1 = next(q for _a, q in nr.calls if "Q1" in q)
        self.assertEqual(q1.lower().count("timeseries"), 1)

    def test_downsample_and_series_cap(self):
        # 12 facet series of 250 points each; capped 8 series, <=50 pts.
        nr_rows = nr_faceted_ts(["s%d" % i for i in range(12)],
                                list(range(250)))
        p = panel(1, "timeseries", "e",
                  "SELECT count(*) FROM T Qbig FACET appName")
        dash = dash_of([p])
        out = build_comparison(FakeNR({"Qbig": nr_rows}), [123],
                               FakeGrafana({}), {}, dash,
                               report_for(dash), limit_points=50)
        series = out["panels"][0]["nr"]["series"]
        self.assertEqual(len(series), 8)
        for s in series:
            self.assertLessEqual(len(s["points"]), 50)

    def test_summary_and_score(self):
        panels = [
            panel(1, "timeseries", "ok", "SELECT c FROM T Q1", gridy=0),
            panel(2, "timeseries", "bad", "SELECT c FROM T Q2", gridy=8),
        ]
        dash = dash_of(panels)
        nr = FakeNR({"Q1": nr_ts([1, 2, 3]), "Q2": nr_ts([1, 2, 3])})
        grafana = FakeGrafana({
            "ok": gf_response([gf_frame([100, 160, 220], [1, 2, 3])]),
            "bad": gf_response([gf_frame([100, 160, 220], [50, 1, 90])]),
        })
        out = build_comparison(nr, [123], grafana, {}, dash,
                               report_for(dash))
        self.assertEqual(out["summary"].get("match"), 1)
        self.assertEqual(out["summary"].get("value-mismatch"), 1)
        # (1.0 + 0.25) / 2 = 0.625 -> 62 (banker's rounding of 62.5)
        self.assertEqual(out["score"], 62)
        self.assertEqual(out["schema"], "nr2grafana/comparison/v1")
        self.assertEqual(out["uid"], "d1")

    def test_grid_and_row_preserved(self):
        p = panel(2, "timeseries", "e", "SELECT c FROM T Q", gridy=8)
        dash = {"uid": "d", "title": "D", "panels": [
            {"id": 1, "type": "row", "title": "Golden", "panels": []}, p]}
        out = build_comparison(FakeNR({}), [123], FakeGrafana({}), {},
                               dash, report_for(dash))
        row = out["panels"][0]
        self.assertEqual(row["row"], "Golden")
        self.assertEqual(row["grid"], {"x": 0, "y": 8, "w": 12, "h": 8})

    def test_nr_layout_from_raw(self):
        raw = {"pages": [{"name": "Overview", "widgets": [
            {"title": "Throughput", "visualization": {"id": "viz.line"},
             "layout": {"column": 1, "row": 1, "width": 4,
                        "height": 3}}]}]}
        dash = dash_of([panel(1, "timeseries", "e", "SELECT c FROM T Q")])
        out = build_comparison(FakeNR({}), [123], FakeGrafana({}), raw,
                               dash, report_for(dash))
        pages = out["layout"]["nr_pages"]
        self.assertEqual(pages[0]["name"], "Overview")
        self.assertEqual(pages[0]["widgets"][0]["title"], "Throughput")
        self.assertEqual(pages[0]["widgets"][0]["viz"], "viz.line")

    def test_never_raises_per_panel(self):
        p = panel(1, "timeseries", "e", "SELECT c FROM T Q")
        dash = dash_of([p])
        out = build_comparison(
            FakeNR({"Q": RuntimeError("x")}), [123],
            FakeGrafana({"e": ValueError("y")}), {}, dash,
            report_for(dash))
        self.assertEqual(out["panels"][0]["verdict"], "gf-error")


# ---------------------------------------------------------------------------
# datasource_flow
# ---------------------------------------------------------------------------

class DatasourceFlowTests(unittest.TestCase):
    def make(self, gf_responses, **kw):
        panels = [
            panel(1, "timeseries", "prom_a", "SELECT c FROM T Q1",
                  ds_type="prometheus", uid="prom-uid"),
            panel(2, "timeseries", "prom_b", "SELECT c FROM T Q2",
                  ds_type="prometheus", uid="prom-uid"),
            panel(3, "logs", '{job="app"}', "SELECT c FROM Log LIMIT 10",
                  ds_type="loki", uid="loki-uid"),
        ]
        dash = dash_of(panels)
        reqs = {"datasources": [
            {"family": "prometheus", "plugin_id": "prometheus"},
            {"family": "loki", "plugin_id": "loki"}]}
        grafana = FakeGrafana(gf_responses, **kw)
        return grafana, reqs, dash

    def test_counts_and_sample(self):
        grafana, reqs, dash = self.make({
            "prom_a": gf_response([gf_frame([100, 160], [1, 2])]),
            "prom_b": gf_response([]),  # no data
            '{job="app"}': gf_response([]),  # loki empty (no ds yet)
        })
        out = datasource_flow(grafana, reqs, dash, [])
        fams = {f["family"]: f for f in out["families"]}
        self.assertEqual(out["schema"], "nr2grafana/dsflow/v1")
        prom = fams["prometheus"]
        self.assertEqual(prom["panels_total"], 2)
        self.assertEqual(prom["panels_with_data"], 1)
        self.assertEqual(prom["panels_no_data"], 1)
        self.assertEqual(prom["newly_flowing"], [1])
        self.assertTrue(prom["sample_series"])
        self.assertEqual(prom["health"]["status"], "ok")
        loki = fams["loki"]
        self.assertEqual(loki["panels_with_data"], 0)

    def test_error_counted(self):
        grafana, reqs, dash = self.make({
            "prom_a": ValueError("500 boom"),
            "prom_b": gf_response([gf_frame([100], [1])]),
            '{job="app"}': gf_response([]),
        })
        out = datasource_flow(grafana, reqs, dash, [])
        prom = {f["family"]: f for f in out["families"]}["prometheus"]
        self.assertEqual(prom["panels_error"], 1)
        self.assertEqual(prom["panels_with_data"], 1)

    def test_focus_uid_filters_to_family(self):
        # loki now returns lines: newly flowing after "creating" it.
        grafana, reqs, dash = self.make(
            {'{job="app"}': gf_response([gf_log_frame([1700000000],
                                                      ["boot"])])},
            by_uid={"loki-uid": {"type": "loki", "uid": "loki-uid"}})
        out = datasource_flow(grafana, reqs, dash, [], ds_uid="loki-uid")
        self.assertEqual(len(out["families"]), 1)
        loki = out["families"][0]
        self.assertEqual(loki["family"], "loki")
        self.assertEqual(loki["uid"], "loki-uid")
        self.assertEqual(loki["panels_with_data"], 1)
        self.assertEqual(loki["newly_flowing"], [3])
        self.assertTrue(loki["sample_series"])

    def test_no_grafana_client(self):
        _g, reqs, dash = self.make({})
        out = datasource_flow(None, reqs, dash, [])
        self.assertEqual(out["families"], [])
        self.assertIn("not configured", out["error"])

    def test_families_derived_when_no_requirements(self):
        grafana, _reqs, dash = self.make({
            "prom_a": gf_response([gf_frame([100], [1])])})
        out = datasource_flow(grafana, {}, dash, [])
        fams = sorted(f["family"] for f in out["families"])
        self.assertEqual(fams, ["loki", "prometheus"])


if __name__ == "__main__":
    unittest.main()
