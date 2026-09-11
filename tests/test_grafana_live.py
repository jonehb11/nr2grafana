"""Tests for nr2grafana.grafana.live.GrafanaLive.

GrafanaClient._req is stubbed with a fake instance state (datasources,
plugins, canned /api/ds/query responses) so no network is involved.
"""

import unittest

from nr2grafana.grafana.client import GrafanaError
from nr2grafana.grafana.live import (
    DS_TEMPLATES, GrafanaLive, _epoch_ms, _frame_points, _result_error,
    build_datasource_payload)


def data_frame(points=3):
    return {"schema": {"fields": [{"name": "Time"}, {"name": "Value"}]},
            "data": {"values": [list(range(points)),
                                [1.0] * points]}}


class FakeLive(GrafanaLive):
    """GrafanaLive with _req served from in-memory instance state."""

    def __init__(self, datasources=None, plugins=None, query_results=None,
                 plugins_error=False, routes=None):
        super().__init__("http://grafana.local:3000", token="t0k3n")
        self._datasources = datasources or []
        self._plugins = plugins or []
        # query_results: expr -> canned {"results": ...} dict, or a
        # GrafanaError instance to raise.
        self._query_results = query_results or {}
        self._plugins_error = plugins_error
        # routes: (method, path) -> canned response dict/list, or a
        # GrafanaError instance to raise. Checked before built-ins.
        self._routes = routes or {}
        self.requests = []

    def _req(self, method, path, body=None):
        self.requests.append((method, path, body))
        if (method, path) in self._routes:
            canned = self._routes[(method, path)]
            if isinstance(canned, GrafanaError):
                raise canned
            return canned
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
        if path.startswith("/api/datasources/proxy/"):
            # Unregistered proxy path: behave like a 404ing instance.
            raise GrafanaError("HTTP 404 on GET %s: not found" % path)
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
        # The fix must point at nr2grafana's own flows (web UI /
        # add-datasource CLI), never send the user into Grafana's UI.
        self.assertIn("add-datasource", by["datasource:loki"]["fix"])
        self.assertIn("Datasources", by["datasource:loki"]["fix"])

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


class DsTemplatesTests(unittest.TestCase):
    def test_all_seven_types_present(self):
        self.assertEqual(
            sorted(DS_TEMPLATES),
            sorted(["prometheus", "loki", "tempo", "cloudwatch",
                    "stackdriver", "grafana-azure-monitor-datasource",
                    "nrgrafanaplugin-newrelic-datasource"]))

    def test_template_and_field_shapes(self):
        for ds_type, tpl in DS_TEMPLATES.items():
            for key in ("label", "plugin_id", "core", "fields", "notes"):
                self.assertIn(key, tpl, "%s missing %s" % (ds_type, key))
            self.assertIsInstance(tpl["core"], bool)
            self.assertTrue(tpl["fields"])
            for f in tpl["fields"]:
                for key in ("name", "label", "required", "secret",
                            "placeholder", "help", "path"):
                    self.assertIn(key, f,
                                  "%s.%s missing %s"
                                  % (ds_type, f.get("name"), key))
                self.assertTrue(
                    f["path"] == "url"
                    or f["path"].startswith("jsonData.")
                    or f["path"].startswith("secureJsonData."),
                    "bad path %r" % f["path"])

    def test_cloudwatch_fields(self):
        paths = {f["name"]: f for f in DS_TEMPLATES["cloudwatch"]["fields"]}
        self.assertEqual(paths["authType"]["path"], "jsonData.authType")
        self.assertEqual(paths["defaultRegion"]["path"],
                         "jsonData.defaultRegion")
        self.assertEqual(paths["accessKey"]["path"],
                         "secureJsonData.accessKey")
        self.assertEqual(paths["secretKey"]["path"],
                         "secureJsonData.secretKey")
        self.assertTrue(paths["accessKey"]["secret"])
        self.assertTrue(paths["secretKey"]["secret"])

    def test_stackdriver_jwt_fields_and_notes(self):
        tpl = DS_TEMPLATES["stackdriver"]
        pk = [f for f in tpl["fields"] if f["name"] == "privateKey"][0]
        self.assertEqual(pk["path"], "secureJsonData.privateKey")
        self.assertTrue(pk["secret"])
        self.assertTrue(pk.get("multiline"))
        # The UI-only key-file upload limitation must be called out.
        self.assertIn("upload", tpl["notes"].lower())
        self.assertIn("private_key", tpl["notes"])

    def test_newrelic_fields(self):
        tpl = DS_TEMPLATES["nrgrafanaplugin-newrelic-datasource"]
        self.assertFalse(tpl["core"])
        by = {f["name"]: f for f in tpl["fields"]}
        self.assertEqual(by["apiKey"]["path"], "secureJsonData.apiKey")
        self.assertTrue(by["apiKey"]["secret"])
        self.assertEqual(by["accountId"]["path"], "jsonData.accountId")
        self.assertIn("grafana-cli plugins install", tpl["notes"])


class BuildPayloadTests(unittest.TestCase):
    def test_prometheus_url_folds(self):
        p = build_datasource_payload(
            "prometheus", "Mimir", {"url": "http://mimir:9009/prometheus"})
        self.assertEqual(p["name"], "Mimir")
        self.assertEqual(p["type"], "prometheus")
        self.assertEqual(p["access"], "proxy")
        self.assertEqual(p["url"], "http://mimir:9009/prometheus")
        self.assertNotIn("jsonData", p)
        self.assertNotIn("secureJsonData", p)

    def test_cloudwatch_folds_json_and_secure(self):
        p = build_datasource_payload("cloudwatch", "AWS", {
            "authType": "keys", "defaultRegion": "us-east-1",
            "accessKey": "AKIAX", "secretKey": "sss"})
        self.assertEqual(p["jsonData"],
                         {"authType": "keys",
                          "defaultRegion": "us-east-1"})
        self.assertEqual(p["secureJsonData"],
                         {"accessKey": "AKIAX", "secretKey": "sss"})
        self.assertNotIn("url", p)

    def test_missing_required_raises_actionable(self):
        with self.assertRaises(GrafanaError) as cm:
            build_datasource_payload("prometheus", "P", {})
        self.assertIn("url", str(cm.exception))
        with self.assertRaises(GrafanaError) as cm:
            build_datasource_payload("cloudwatch", "AWS",
                                     {"authType": "keys"})
        self.assertIn("defaultRegion", str(cm.exception))

    def test_unknown_type_raises_with_known_list(self):
        with self.assertRaises(GrafanaError) as cm:
            build_datasource_payload("influxdb", "X", {})
        self.assertIn("influxdb", str(cm.exception))
        self.assertIn("prometheus", str(cm.exception))

    def test_raw_path_keys_accepted(self):
        p = build_datasource_payload("loki", "Loki", {
            "url": "http://loki:3100",
            "jsonData.httpHeaderName1": "X-Scope-OrgID",
            "secureJsonData.httpHeaderValue1": "tenant-1"})
        self.assertEqual(p["jsonData"]["httpHeaderName1"],
                         "X-Scope-OrgID")
        self.assertEqual(p["secureJsonData"]["httpHeaderValue1"],
                         "tenant-1")

    def test_empty_values_skipped(self):
        p = build_datasource_payload("tempo", "Tempo",
                                     {"url": "http://tempo:3200",
                                      "jsonData.extra": "  "})
        self.assertNotIn("jsonData", p)


class DatasourceCrudTests(unittest.TestCase):
    def test_update_datasource_puts_payload(self):
        gl = FakeLive(routes={
            ("PUT", "/api/datasources/uid/mimir"):
                {"datasource": {"uid": "mimir"}, "message": "updated"}})
        out = gl.update_datasource("mimir", {"name": "Mimir2"})
        self.assertEqual(out["message"], "updated")
        method, path, body = gl.requests[-1]
        self.assertEqual((method, path),
                         ("PUT", "/api/datasources/uid/mimir"))
        self.assertEqual(body, {"name": "Mimir2"})

    def test_delete_datasource_returns_none(self):
        gl = FakeLive(routes={
            ("DELETE", "/api/datasources/uid/old"):
                {"message": "deleted"}})
        self.assertIsNone(gl.delete_datasource("old"))
        self.assertEqual(gl.requests[-1][:2],
                         ("DELETE", "/api/datasources/uid/old"))


class DatasourceHealthTests(unittest.TestCase):
    def test_health_endpoint_ok_normalized(self):
        gl = FakeLive(routes={
            ("GET", "/api/datasources/uid/mimir/health"):
                {"status": "OK", "message": "data source is working"}})
        out = gl.datasource_health("mimir")
        self.assertEqual(out, {"status": "ok",
                               "message": "data source is working"})

    def test_health_endpoint_error(self):
        gl = FakeLive(routes={
            ("GET", "/api/datasources/uid/mimir/health"):
                {"status": "ERROR", "message": "connection refused"}})
        out = gl.datasource_health("mimir")
        self.assertEqual(out["status"], "error")
        self.assertIn("connection refused", out["message"])

    def test_404_falls_back_to_probe_ok(self):
        # No health route registered -> FakeLive raises 404 -> probe.
        gl = FakeLive(datasources=[MIMIR],
                      query_results={"vector(1)": {"results": {"A": {
                          "frames": [data_frame(1)], "status": 200}}}})
        out = gl.datasource_health("mimir")
        self.assertEqual(out["status"], "ok")
        self.assertIn("probe", out["message"])
        exprs = [b["queries"][0].get("expr") for _m, p, b in gl.requests
                 if p == "/api/ds/query"]
        self.assertEqual(exprs, ["vector(1)"])

    def test_probe_error_reported(self):
        gl = FakeLive(datasources=[MIMIR],
                      query_results={"vector(1)": GrafanaError(
                          "HTTP 502 on POST /api/ds/query: bad gateway")})
        out = gl.datasource_health("mimir")
        self.assertEqual(out["status"], "error")
        self.assertIn("HTTP 502", out["message"])

    def test_probe_result_error_reported(self):
        gl = FakeLive(datasources=[MIMIR],
                      query_results={"vector(1)": {"results": {"A": {
                          "error": "connection refused",
                          "status": 500}}}})
        out = gl.datasource_health("mimir")
        self.assertEqual(out["status"], "error")
        self.assertIn("connection refused", out["message"])

    def test_unknown_uid_is_error(self):
        gl = FakeLive(datasources=[])
        out = gl.datasource_health("ghost")
        self.assertEqual(out["status"], "error")
        self.assertIn("ghost", out["message"])

    def test_unprobeable_type_is_unknown(self):
        cw = {"uid": "cw", "type": "cloudwatch", "name": "AWS"}
        gl = FakeLive(datasources=[cw])
        out = gl.datasource_health("cw")
        self.assertEqual(out["status"], "unknown")
        self.assertIn("cloudwatch", out["message"])

    def test_never_raises(self):
        gl = FakeLive(routes={
            ("GET", "/api/datasources/uid/x/health"):
                GrafanaError("cannot reach http://grafana.local:3000"),
            ("GET", "/api/datasources/uid/x"):
                GrafanaError("cannot reach http://grafana.local:3000")})
        out = gl.datasource_health("x")
        self.assertEqual(out["status"], "error")
        self.assertIn("cannot reach", out["message"])


PROXY = "/api/datasources/proxy/uid/mimir"
LOKI_PROXY = "/api/datasources/proxy/uid/loki-uid"


class ProxyIntrospectionTests(unittest.TestCase):
    def test_prom_metric_names(self):
        gl = FakeLive(routes={
            ("GET", PROXY + "/api/v1/label/__name__/values"):
                {"status": "success", "data": ["up", "node_load1"]}})
        self.assertEqual(gl.prom_metric_names("mimir"),
                         ["up", "node_load1"])

    def test_prom_metric_names_failure_returns_empty_and_stashes(self):
        errors = []
        gl = FakeLive(routes={
            ("GET", PROXY + "/api/v1/label/__name__/values"):
                GrafanaError("HTTP 404 on GET .../values: proxy off")})
        self.assertEqual(gl.prom_metric_names("mimir", errors=errors),
                         [])
        self.assertEqual(len(errors), 1)
        self.assertIn("HTTP 404", errors[0])

    def test_prom_metric_names_bad_shape(self):
        errors = []
        gl = FakeLive(routes={
            ("GET", PROXY + "/api/v1/label/__name__/values"):
                {"unexpected": True}})
        self.assertEqual(gl.prom_metric_names("mimir", errors=errors),
                         [])
        self.assertTrue(errors)

    def test_prom_label_values_with_match(self):
        path = (PROXY + "/api/v1/label/job/values"
                "?match%5B%5D=up%7Binstance%3D%22a%22%7D")
        gl = FakeLive(routes={
            ("GET", path): {"status": "success", "data": ["api", "web"]}})
        vals = gl.prom_label_values("mimir", "job",
                                    match='up{instance="a"}')
        self.assertEqual(vals, ["api", "web"])

    def test_prom_label_values_no_match_param(self):
        gl = FakeLive(routes={
            ("GET", PROXY + "/api/v1/label/job/values"):
                {"status": "success", "data": ["api"]}})
        self.assertEqual(gl.prom_label_values("mimir", "job"), ["api"])

    def test_prom_series(self):
        gl = FakeLive(routes={})
        # Path contains computed timestamps; intercept via requests log.
        series = gl.prom_series("mimir", 'up{job="api"}')
        self.assertEqual(series, [])  # 404 from fake -> []
        _m, path, _b = gl.requests[-1]
        self.assertTrue(path.startswith(PROXY + "/api/v1/series?"))
        self.assertIn("match%5B%5D=up%7Bjob%3D%22api%22%7D", path)
        self.assertIn("start=", path)
        self.assertIn("end=", path)

    def test_prom_series_filters_non_dicts(self):
        gl = FakeLive()
        real_proxy = gl._proxy_get
        gl._proxy_get = lambda uid, path, errors=None: {
            "status": "success",
            "data": [{"__name__": "up", "job": "api"}, "junk"]}
        try:
            series = gl.prom_series("mimir", "up")
        finally:
            gl._proxy_get = real_proxy
        self.assertEqual(series, [{"__name__": "up", "job": "api"}])

    def test_loki_labels_and_values(self):
        gl = FakeLive(routes={
            ("GET", LOKI_PROXY + "/loki/api/v1/labels"):
                {"status": "success", "data": ["job", "namespace"]},
            ("GET", LOKI_PROXY + "/loki/api/v1/label/job/values"):
                {"status": "success", "data": ["nginx"]}})
        self.assertEqual(gl.loki_labels("loki-uid"),
                         ["job", "namespace"])
        self.assertEqual(gl.loki_label_values("loki-uid", "job"),
                         ["nginx"])

    def test_loki_failure_returns_empty(self):
        errors = []
        gl = FakeLive(routes={
            ("GET", LOKI_PROXY + "/loki/api/v1/labels"):
                GrafanaError("HTTP 403 on GET: denied")})
        self.assertEqual(gl.loki_labels("loki-uid", errors=errors), [])
        self.assertIn("HTTP 403", errors[0])


class PermissionsReportTests(unittest.TestCase):
    def test_admin_token(self):
        gl = FakeLive(
            datasources=[MIMIR],
            routes={
                ("GET", "/api/user"): {"login": "sa-nr2grafana"},
                ("GET", "/api/org"): {"name": "Main Org."},
                ("GET", "/api/access-control/user/permissions"): {
                    "datasources:create": ["datasources:*"],
                    "dashboards:write": ["dashboards:*"]}})
        out = gl.permissions_report()
        self.assertEqual(out["user"], "sa-nr2grafana")
        self.assertEqual(out["role"], "Admin")
        self.assertTrue(out["can_admin_datasources"])
        self.assertTrue(out["can_edit_dashboards"])
        self.assertIn("Main Org.", out["detail"])

    def test_editor_token(self):
        gl = FakeLive(
            datasources=[MIMIR],
            routes={
                ("GET", "/api/user"): {"login": "sa-editor"},
                ("GET", "/api/org"): {"name": "Main Org."},
                ("GET", "/api/access-control/user/permissions"): {
                    "dashboards:create": ["folders:*"]}})
        out = gl.permissions_report()
        self.assertEqual(out["role"], "Editor")
        self.assertFalse(out["can_admin_datasources"])
        self.assertTrue(out["can_edit_dashboards"])
        self.assertIn("Admin", out["detail"])  # says what role is needed

    def test_bad_token_never_raises(self):
        err = GrafanaError("HTTP 401 on GET /api/user: invalid token")
        gl = FakeLive(routes={
            ("GET", "/api/user"): err,
            ("GET", "/api/org"): err,
            ("GET", "/api/access-control/user/permissions"): err})
        # /api/datasources also fails for a bad token.
        gl._routes[("GET", "/api/datasources")] = err
        out = gl.permissions_report()
        self.assertEqual(out["user"], "")
        self.assertFalse(out["can_admin_datasources"])
        self.assertFalse(out["can_edit_dashboards"])
        self.assertIn("401", out["detail"])
        self.assertIn("token", out["detail"])

    def test_no_access_control_api_degrades(self):
        gl = FakeLive(
            datasources=[MIMIR],
            routes={
                ("GET", "/api/user"): {"login": "sa-old"},
                ("GET", "/api/org"): {"name": "Org"},
                ("GET", "/api/access-control/user/permissions"):
                    GrafanaError("HTTP 404 on GET: not found")})
        out = gl.permissions_report()
        self.assertEqual(out["user"], "sa-old")
        self.assertFalse(out["can_admin_datasources"])
        self.assertTrue(out["can_edit_dashboards"])
        self.assertIn("access-control", out["detail"])

    def test_only_gets_are_issued(self):
        gl = FakeLive(
            datasources=[MIMIR],
            routes={
                ("GET", "/api/user"): {"login": "u"},
                ("GET", "/api/org"): {"name": "O"},
                ("GET", "/api/access-control/user/permissions"): {}})
        gl.permissions_report()
        self.assertTrue(all(m == "GET" for m, _p, _b in gl.requests))


if __name__ == "__main__":
    unittest.main()
