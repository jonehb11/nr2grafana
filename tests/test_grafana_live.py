"""Tests for nr2grafana.grafana.live.GrafanaLive.

GrafanaClient._req is stubbed with a fake instance state (datasources,
plugins, canned /api/ds/query responses) so no network is involved.
"""

import unittest

from nr2grafana.grafana.client import GrafanaError
from nr2grafana.grafana.live import (
    GrafanaLive, _epoch_ms, _frame_points, _result_error)


def data_frame(points=3):
    return {"schema": {"fields": [{"name": "Time"}, {"name": "Value"}]},
            "data": {"values": [list(range(points)),
                                [1.0] * points]}}


class FakeLive(GrafanaLive):
    """GrafanaLive with _req served from in-memory instance state."""

    def __init__(self, datasources=None, plugins=None, query_results=None,
                 plugins_error=False):
        super().__init__("http://grafana.local:3000", token="t0k3n")
        self._datasources = datasources or []
        self._plugins = plugins or []
        # query_results: expr -> canned {"results": ...} dict, or a
        # GrafanaError instance to raise.
        self._query_results = query_results or {}
        self._plugins_error = plugins_error
        self.requests = []

    def _req(self, method, path, body=None):
        self.requests.append((method, path, body))
        if path == "/api/datasources":
            return self._datasources
        if path == "/api/plugins":
            if self._plugins_error:
                raise GrafanaError("HTTP 403 on GET /api/plugins: denied")
            return self._plugins
        if path.startswith("/api/datasources/uid/"):
            uid = path.rsplit("/", 1)[-1]
            for d in self._datasources:
                if d.get("uid") == uid:
                    return d
            raise GrafanaError("HTTP 404 on GET %s: not found" % path)
        if path == "/api/ds/query":
            expr = body["queries"][0].get("expr") \
                or body["queries"][0].get("query") or ""
            canned = self._query_results.get(expr)
            if isinstance(canned, GrafanaError):
                raise canned
            if canned is None:
                return {"results": {body["queries"][0]["refId"]:
                                    {"frames": [], "status": 200}}}
            return canned
        if path == "/api/dashboards/db":
            return {"status": "success", "uid": body["dashboard"]["uid"]}
        raise AssertionError("unexpected request %s %s" % (method, path))


MIMIR = {"uid": "mimir", "type": "prometheus", "name": "Mimir",
         "isDefault": False}
PROM_DEFAULT = {"uid": "prom-default", "type": "prometheus",
                "name": "Prometheus", "isDefault": True}
LOKI = {"uid": "loki-uid", "type": "loki", "name": "Loki",
        "isDefault": False}


def make_dash(panels=None, templating=None):
    return {"uid": "d1", "title": "Dash",
            "templating": {"list": templating or []},
            "panels": panels or []}


def ds_var(name, ds_type):
    return {"type": "datasource", "name": name, "query": ds_type}


def prom_panel(pid=1, expr="up", uid="${datasource}", refid="A",
               ds_type="prometheus"):
    return {"id": pid, "type": "timeseries", "title": "P%d" % pid,
            "targets": [{"refId": refid, "expr": expr,
                         "datasource": {"type": ds_type, "uid": uid}}]}


class EpochMsTests(unittest.TestCase):
    def test_now(self):
        self.assertEqual(_epoch_ms("now", now=1000.0), 1000000)

    def test_relative(self):
        self.assertEqual(_epoch_ms("now-1h", now=7200.0),
                         (7200 - 3600) * 1000)
        self.assertEqual(_epoch_ms("now-30m", now=3600.0),
                         (3600 - 1800) * 1000)
        self.assertEqual(_epoch_ms("now-2d", now=200000.0),
                         (200000 - 2 * 86400) * 1000)

    def test_epoch_passthrough(self):
        self.assertEqual(_epoch_ms("1700000000000"), 1700000000000)
        self.assertEqual(_epoch_ms(1700000000000), 1700000000000)

    def test_bad_spec_raises_actionable(self):
        with self.assertRaises(GrafanaError) as cm:
            _epoch_ms("yesterday")
        self.assertIn("yesterday", str(cm.exception))
        self.assertIn("now-1h", str(cm.exception))


class FrameHelperTests(unittest.TestCase):
    def test_frame_points(self):
        self.assertEqual(_frame_points(data_frame(5)), 5)
        self.assertEqual(_frame_points({"data": {"values": []}}), 0)
        self.assertEqual(_frame_points({}), 0)

    def test_result_error_variants(self):
        self.assertEqual(_result_error({}), "")
        self.assertEqual(_result_error({"error": "boom"}), "boom")
        self.assertEqual(
            _result_error({"errors": [{"message": "parse error"}]}),
            "parse error")
        self.assertEqual(_result_error({"status": 500}),
                         "query returned HTTP 500")
        self.assertEqual(_result_error({"status": 200}), "")


class ResolveDsMapTests(unittest.TestCase):
    def test_preferred_wins(self):
        gl = FakeLive(datasources=[PROM_DEFAULT, MIMIR])
        dash = make_dash(templating=[ds_var("datasource", "prometheus")])
        m = gl.resolve_ds_map(dash, preferred={"datasource": "mimir"})
        self.assertEqual(m["datasource"], "mimir")
        self.assertEqual(m["${datasource}"], "mimir")

    def test_default_beats_first(self):
        gl = FakeLive(datasources=[MIMIR, PROM_DEFAULT])
        dash = make_dash(templating=[ds_var("datasource", "prometheus")])
        m = gl.resolve_ds_map(dash)
        self.assertEqual(m["datasource"], "prom-default")

    def test_first_of_type_when_no_default(self):
        gl = FakeLive(datasources=[MIMIR, LOKI])
        dash = make_dash(templating=[
            ds_var("datasource", "prometheus"),
            ds_var("loki_datasource", "loki")])
        m = gl.resolve_ds_map(dash)
        self.assertEqual(m["datasource"], "mimir")
        self.assertEqual(m["loki_datasource"], "loki-uid")

    def test_no_matching_type_omitted(self):
        gl = FakeLive(datasources=[MIMIR])
        dash = make_dash(templating=[ds_var("tempo_datasource", "tempo")])
        m = gl.resolve_ds_map(dash)
        self.assertNotIn("tempo_datasource", m)

    def test_raw_placeholder_in_target(self):
        gl = FakeLive(datasources=[MIMIR])
        dash = make_dash(panels=[prom_panel(uid="${orphan_ds}")])
        m = gl.resolve_ds_map(dash)
        self.assertEqual(m["${orphan_ds}"], "mimir")
        self.assertEqual(m["orphan_ds"], "mimir")


class CheckRequirementsTests(unittest.TestCase):
    def reqs(self, datasources=None, plugins=None):
        return {"schema": "nr2grafana/requirements/v1",
                "datasources": datasources or [], "plugins": plugins or []}

    def test_datasource_ok_and_missing(self):
        gl = FakeLive(datasources=[MIMIR])
        rows = gl.check_requirements(self.reqs(datasources=[
            {"family": "prometheus", "plugin_id": "prometheus",
             "core": True, "uid_ref": "${datasource}", "required": True},
            {"family": "loki", "plugin_id": "loki", "core": True,
             "uid_ref": "${loki_datasource}", "required": True}]))
        by = {r["item"]: r for r in rows}
        self.assertEqual(by["datasource:prometheus"]["status"], "ok")
        self.assertEqual(by["datasource:loki"]["status"], "missing")
        self.assertIn("Add a Loki datasource", by["datasource:loki"]["fix"])
        self.assertIn("Data sources", by["datasource:loki"]["fix"])

    def test_concrete_uid_wrong_type(self):
        gl = FakeLive(datasources=[LOKI])
        rows = gl.check_requirements(self.reqs(datasources=[
            {"family": "prometheus", "plugin_id": "prometheus",
             "core": True, "uid_ref": "loki-uid", "required": True}]))
        self.assertEqual(rows[0]["status"], "wrong-type")
        self.assertIn("loki", rows[0]["detail"])

    def test_concrete_uid_found(self):
        gl = FakeLive(datasources=[MIMIR])
        rows = gl.check_requirements(self.reqs(datasources=[
            {"family": "prometheus", "plugin_id": "prometheus",
             "core": True, "uid_ref": "mimir", "required": True}]))
        self.assertEqual(rows[0]["status"], "ok")

    def test_missing_plugin(self):
        gl = FakeLive(plugins=[{"id": "grafana-piechart-panel"}])
        rows = gl.check_requirements(self.reqs(plugins=[
            {"id": "nrgrafanaplugin-newrelic-datasource",
             "reason": "passthrough panels",
             "grafana_cli": "grafana-cli plugins install "
                            "nrgrafanaplugin-newrelic-datasource"}]))
        row = [r for r in rows if r["item"].startswith("plugin:")][0]
        self.assertEqual(row["status"], "missing")
        self.assertIn("grafana-cli plugins install", row["fix"])

    def test_installed_plugin_ok(self):
        gl = FakeLive(plugins=[{"id": "some-plugin"}])
        rows = gl.check_requirements(
            self.reqs(plugins=[{"id": "some-plugin"}]))
        self.assertEqual(rows[0]["status"], "ok")

    def test_plugins_endpoint_denied_degrades(self):
        gl = FakeLive(plugins_error=True)
        rows = gl.check_requirements(
            self.reqs(plugins=[{"id": "some-plugin"}]))
        statuses = [r["status"] for r in rows]
        self.assertIn("missing", statuses)  # degraded, no traceback

    def test_noncore_missing_fix_mentions_install(self):
        gl = FakeLive(datasources=[])
        rows = gl.check_requirements(self.reqs(datasources=[
            {"family": "newrelic",
             "plugin_id": "nrgrafanaplugin-newrelic-datasource",
             "core": False, "uid_ref": "${newrelic_datasource}",
             "required": False}]))
        self.assertEqual(rows[0]["status"], "missing")
        self.assertIn("grafana-cli plugins install", rows[0]["fix"])


class DsQueryTests(unittest.TestCase):
    def test_prometheus_body(self):
        gl = FakeLive(query_results={"up": {"results": {"A": {
            "frames": [data_frame(2)], "status": 200}}}})
        gl.ds_query("mimir", "prometheus", {"refId": "A", "expr": "up"})
        method, path, body = gl.requests[-1]
        self.assertEqual((method, path), ("POST", "/api/ds/query"))
        q = body["queries"][0]
        self.assertEqual(q["datasource"], {"uid": "mimir",
                                           "type": "prometheus"})
        self.assertTrue(q["range"])
        self.assertEqual(q["intervalMs"], 60000)
        self.assertEqual(q["maxDataPoints"], 300)
        self.assertTrue(body["from"].isdigit())
        self.assertTrue(body["to"].isdigit())
        self.assertLess(int(body["from"]), int(body["to"]))

    def test_loki_body(self):
        gl = FakeLive()
        gl.ds_query("loki-uid", "loki",
                    {"refId": "B", "expr": "{job=\"x\"}"})
        q = gl.requests[-1][2]["queries"][0]
        self.assertEqual(q["queryType"], "range")
        self.assertNotIn("range", q)

    def test_tempo_passthrough(self):
        gl = FakeLive()
        gl.ds_query("tempo-uid", "tempo",
                    {"refId": "A", "query": "{}", "queryType": "traceql"})
        q = gl.requests[-1][2]["queries"][0]
        self.assertEqual(q["query"], "{}")
        self.assertEqual(q["queryType"], "traceql")


class TestDashboardTests(unittest.TestCase):
    def test_status_classification(self):
        gl = FakeLive(
            datasources=[MIMIR, LOKI],
            query_results={
                "up": {"results": {"A": {"frames": [data_frame(4)],
                                         "status": 200}}},
                "absent_metric": {"results": {"A": {"frames": [],
                                                    "status": 200}}},
                "bad(": {"results": {"A": {"error": "parse error at 4",
                                           "status": 400}}},
                "boom": GrafanaError("HTTP 500 on POST /api/ds/query: x"),
            })
        dash = make_dash(
            templating=[ds_var("datasource", "prometheus")],
            panels=[prom_panel(1, "up"),
                    prom_panel(2, "absent_metric"),
                    prom_panel(3, "bad("),
                    prom_panel(4, "boom")])
        rows = gl.test_dashboard(dash)
        by = {r["panel_id"]: r for r in rows}
        self.assertEqual(by[1]["status"], "data")
        self.assertEqual(by[1]["frames"], 1)
        self.assertEqual(by[1]["points"], 4)
        self.assertEqual(by[2]["status"], "no-data")
        self.assertEqual(by[2]["points"], 0)
        self.assertEqual(by[3]["status"], "error")
        self.assertIn("parse error", by[3]["error"])
        self.assertEqual(by[4]["status"], "error")
        self.assertIn("HTTP 500", by[4]["error"])

    def test_never_raises_and_unresolved_ds(self):
        gl = FakeLive(datasources=[])  # nothing to resolve against
        dash = make_dash(
            templating=[ds_var("datasource", "prometheus")],
            panels=[prom_panel(1, "up")])
        rows = gl.test_dashboard(dash)
        self.assertEqual(rows[0]["status"], "error")
        self.assertIn("unresolved datasource", rows[0]["error"])

    def test_substitutes_template_vars(self):
        gl = FakeLive(
            datasources=[MIMIR],
            query_results={
                'rate(http_requests_total{service_name="x"}[5m])':
                    {"results": {"A": {"frames": [data_frame(1)],
                                       "status": 200}}}})
        dash = make_dash(
            templating=[ds_var("datasource", "prometheus")],
            panels=[prom_panel(
                1, 'rate(http_requests_total{service_name="$service"}'
                   '[$__rate_interval])')])
        rows = gl.test_dashboard(dash)
        self.assertEqual(rows[0]["status"], "data")
        self.assertNotIn("$", rows[0]["expr"])

    def test_collapsed_row_nested_panels(self):
        row = {"id": 10, "type": "row", "title": "Section",
               "collapsed": True,
               "panels": [prom_panel(11, "up")]}
        gl = FakeLive(
            datasources=[MIMIR],
            query_results={"up": {"results": {"A": {
                "frames": [data_frame(2)], "status": 200}}}})
        dash = make_dash(
            templating=[ds_var("datasource", "prometheus")],
            panels=[row])
        rows = gl.test_dashboard(dash)
        self.assertEqual([r["panel_id"] for r in rows], [11])
        self.assertEqual(rows[0]["status"], "data")

    def test_explicit_ds_map_and_log(self):
        logged = []
        gl = FakeLive(query_results={"up": {"results": {"A": {
            "frames": [data_frame(1)], "status": 200}}}})
        dash = make_dash(panels=[prom_panel(1, "up")])
        rows = gl.test_dashboard(dash, ds_map={"datasource": "mimir"},
                                 log=logged.append)
        self.assertEqual(rows[0]["status"], "data")
        self.assertEqual(rows[0]["datasource"], "mimir")
        self.assertTrue(logged)
        # explicit ds_map means no /api/datasources call was needed
        self.assertNotIn("/api/datasources",
                         [p for _m, p, _b in gl.requests])


class InventoryAndPushTests(unittest.TestCase):
    def test_datasource_by_uid_none_on_404(self):
        gl = FakeLive(datasources=[MIMIR])
        self.assertEqual(gl.datasource_by_uid("mimir"), MIMIR)
        self.assertIsNone(gl.datasource_by_uid("nope"))

    def test_search_dashboards_quotes_query(self):
        gl = FakeLive()
        try:
            gl.search_dashboards("a b")
        except AssertionError:
            pass  # FakeLive has no /api/search route; inspect the request
        _m, path, _b = gl.requests[-1]
        self.assertIn("type=dash-db", path)
        self.assertIn("query=a%20b", path)

    def test_update_dashboard_overwrites(self):
        gl = FakeLive()
        out = gl.update_dashboard({"uid": "d1", "title": "T"},
                                  folder_uid="f1")
        self.assertEqual(out["uid"], "d1")
        _m, path, body = gl.requests[-1]
        self.assertEqual(path, "/api/dashboards/db")
        self.assertTrue(body["overwrite"])
        self.assertEqual(body["folderUid"], "f1")
        self.assertIn("nr2grafana", body["message"])


if __name__ == "__main__":
    unittest.main()
