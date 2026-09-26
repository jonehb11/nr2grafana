"""Tests for the four pipeline operations (import / convert / validate /
export) and the inspect/explain helpers, with Grafana faked in-process."""

import json
import os
import tempfile
import unittest
from unittest import mock

from nr2grafana import pipeline
from nr2grafana.grafana import live as live_mod
from nr2grafana.grafana.client import GrafanaError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures", "newrelic")
SAMPLE = os.path.join(FIXTURES, "sample-service-dashboard.json")
KITCHEN = os.path.join(FIXTURES, "edge-widget-kitchen-sink.json")


def _frame(points=2):
    return {"schema": {"fields": [{"name": "Time"}, {"name": "Value"}]},
            "data": {"values": [list(range(points)), [1.0] * points]}}


class FakeGrafana(live_mod.GrafanaLive):
    """GrafanaLive served from in-memory state; records every request."""

    datasources_state = [
        {"uid": "mimir", "type": "prometheus", "name": "Mimir",
         "isDefault": True},
        {"uid": "loki1", "type": "loki", "name": "Loki", "isDefault": False},
        {"uid": "tempo1", "type": "tempo", "name": "Tempo",
         "isDefault": False},
    ]
    fail_import = ""
    query_errors = {}
    tempo_traces = [{"traceID": "t1"}]

    def __init__(self, url, token="", insecure=False, timeout=30):
        super().__init__(url, token=token, insecure=insecure)
        self.stored = {}
        self.requests = []

    def _req(self, method, path, body=None):
        self.requests.append((method, path, body))
        if path == "/api/health":
            return {"version": "11.2.0"}
        if path == "/api/datasources":
            if not self.headers.get("Authorization"):
                raise GrafanaError("HTTP 401 on GET /api/datasources: "
                                   "Unauthorized")
            return list(self.datasources_state)
        if path == "/api/plugins":
            return [{"id": "prometheus"}, {"id": "loki"}, {"id": "tempo"}]
        if path == "/api/folders":
            return [{"uid": "f-existing", "title": "Existing"}]
        if path == "/api/folders" and method == "POST":
            return {"uid": "f-new"}
        if path == "/api/dashboards/db":
            if self.fail_import:
                raise GrafanaError(self.fail_import)
            d = body["dashboard"]
            self.stored[d["uid"]] = json.loads(json.dumps(d))
            return {"status": "success", "uid": d["uid"], "id": 42,
                    "url": "/d/%s/%s" % (d["uid"], "slug"), "version": 1}
        if path.startswith("/api/dashboards/uid/"):
            uid = path.rsplit("/", 1)[-1]
            if uid not in self.stored:
                raise GrafanaError("HTTP 404 on GET %s: not found" % path)
            return {"dashboard": dict(self.stored[uid], version=1, id=42),
                    "meta": {"url": "/d/%s/slug" % uid,
                             "folderTitle": body or "General"}}
        if path.startswith("/api/datasources/proxy/uid/tempo1/api/search?"):
            return {"traces": list(self.tempo_traces),
                    "metrics": {"completedJobs": 1}}
        if path == "/api/ds/query":
            q = body["queries"][0]
            expr = q.get("expr") or q.get("query") or ""
            if expr in self.query_errors:
                return {"results": {q["refId"]: {
                    "error": self.query_errors[expr], "status": 400}}}
            return {"results": {q["refId"]: {"frames": [_frame()]}}}
        raise AssertionError("unexpected request %s %s" % (method, path))


class _FakeMixin:
    def setUp(self):
        self._patch = mock.patch.object(pipeline, "GrafanaLive", FakeGrafana)
        self._patch.start()
        FakeGrafana.fail_import = ""
        FakeGrafana.query_errors = {}
        FakeGrafana.tempo_traces = [{"traceID": "t1"}]
        FakeGrafana.datasources_state = [
            {"uid": "mimir", "type": "prometheus", "name": "Mimir",
             "isDefault": True},
            {"uid": "loki1", "type": "loki", "name": "Loki",
             "isDefault": False},
            {"uid": "tempo1", "type": "tempo", "name": "Tempo",
             "isDefault": False}]
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "grafana")
        self.convert = pipeline.convert_dashboards([SAMPLE], self.out)

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()


class ImportTests(unittest.TestCase):
    def test_import_local_files_normalises_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as out:
            r = pipeline.import_dashboards(out, files=[SAMPLE, KITCHEN])
            self.assertTrue(r["ok"])
            self.assertEqual(len(r["dashboards"]), 2)
            names = sorted(os.listdir(out))
            self.assertIn("checkout-service-overview.json", names)
            self.assertIn("import-manifest.json", names)
            manifest = json.load(open(os.path.join(out,
                                                   "import-manifest.json")))
            first = manifest["dashboards"][0]
            for key in ("name", "guid", "account_id", "pages", "widgets",
                        "variables", "file", "origin"):
                self.assertIn(key, first)

    def test_import_rejects_non_dashboard_file_per_entry(self):
        with tempfile.TemporaryDirectory() as out:
            bad = os.path.join(FIXTURES, "edge-malformed-nopages.json")
            r = pipeline.import_dashboards(out, files=[bad, SAMPLE])
            self.assertFalse(r["ok"])
            self.assertEqual(len(r["failed"]), 1)
            self.assertIn("pages", r["failed"][0]["error"])
            self.assertEqual(len(r["dashboards"]), 1)

    def test_import_without_key_or_files_is_usage_error(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(pipeline.PipelineError) as ctx:
                pipeline.import_dashboards(out, api_key="")
            self.assertEqual(ctx.exception.code, pipeline.EXIT_USAGE)
            self.assertIn("NEW_RELIC_API_KEY", str(ctx.exception))

    def test_import_from_newrelic_uses_nerdgraph_and_filters_by_name(self):
        calls = {}

        class FakeNG:
            def __init__(self, key, region="US"):
                calls["key"] = key
                calls["region"] = region

            def list_dashboards(self):
                return [{"guid": "g1", "name": "Checkout"},
                        {"guid": "g2", "name": "Billing"}]

            def get_dashboard(self, guid):
                calls.setdefault("fetched", []).append(guid)
                data = json.load(open(SAMPLE))
                data["guid"] = guid
                return data

        import nr2grafana.nerdgraph as ng
        with mock.patch.object(ng, "NerdGraphClient", FakeNG), \
                tempfile.TemporaryDirectory() as out:
            r = pipeline.import_dashboards(out, api_key="NRAK-x",
                                           name_filter="check")
        self.assertEqual(calls["fetched"], ["g1"])
        self.assertEqual(r["dashboards"][0]["guid"], "g1")
        self.assertEqual(r["dashboards"][0]["origin"], "newrelic:g1")


class ConvertTests(unittest.TestCase):
    def test_convert_reports_cannot_migrate_with_reason_and_equivalent(self):
        with tempfile.TemporaryDirectory() as out:
            r = pipeline.convert_dashboards([KITCHEN], out)
            d = r["dashboards"][0]
            self.assertEqual(d["source_dashboard"]["name"],
                             "Widget Kitchen Sink")
            kinds = {c["visualization"]: c for c in d["cannot_migrate"]}
            custom = [k for k in kinds if k.endswith(".custom-viz")]
            self.assertEqual(len(custom), 1)
            self.assertIn("nerdpack", kinds[custom[0]]["reason"])
            self.assertIn("viz.event-feed", kinds)
            self.assertTrue(kinds["viz.event-feed"]["nrql"])
            self.assertTrue(all(c["reason"] for c in d["cannot_migrate"]))
            self.assertEqual(d["validation"]["errors"], [])
            self.assertEqual(r["totals"]["cannot_migrate"],
                             len(d["cannot_migrate"]))

    def test_convert_embeds_provenance_and_datasources(self):
        with tempfile.TemporaryDirectory() as out:
            r = pipeline.convert_dashboards([SAMPLE], out)
            d = r["dashboards"][0]
            dash = json.load(open(d["output"]))
            self.assertEqual(dash["nr2grafana"]["source"]["name"],
                             "Checkout Service Overview")
            self.assertIn("Migrated from New Relic dashboard",
                          dash["description"])
            types = {x["type"] for x in d["datasources"]}
            self.assertEqual(types, {"prometheus", "loki", "tempo"})
            report = json.load(open(r["report"]))
            self.assertEqual(set(report), {"reports", "failed_inputs"})

    def test_convert_bad_file_does_not_kill_batch(self):
        with tempfile.TemporaryDirectory() as out:
            bad = os.path.join(FIXTURES, "edge-malformed-syntax.json")
            r = pipeline.convert_dashboards([bad, SAMPLE], out)
            self.assertFalse(r["ok"])
            self.assertEqual(len(r["failed_inputs"]), 1)
            self.assertEqual(len(r["dashboards"]), 1)

    def test_convert_missing_input_is_usage_error(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(pipeline.PipelineError) as ctx:
                pipeline.convert_dashboards(["/nope/x.json"], out)
            self.assertEqual(ctx.exception.code, pipeline.EXIT_USAGE)


class ValidateTests(_FakeMixin, unittest.TestCase):
    def test_static_validation_passes_converter_output(self):
        r = pipeline.validate_dashboards([self.out], write_results=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["totals"]["errors"], 0)

    def test_static_validation_flags_broken_json_per_file(self):
        junk = os.path.join(self.out, "junk.json")
        with open(junk, "w") as f:
            f.write("{nope")
        r = pipeline.validate_dashboards([junk], write_results=False)
        self.assertFalse(r["ok"])
        self.assertIn("INVALID JSON", r["dashboards"][0]["errors"][0])

    def test_live_validation_binds_datasources_and_tests_panels(self):
        r = pipeline.validate_dashboards([self.out], grafana_url="http://g",
                                         grafana_token="t", test=True)
        d = r["dashboards"][0]
        self.assertTrue(r["ok"], d["errors"])
        by_type = {n["type"]: n for n in d["datasources"]}
        self.assertEqual(by_type["prometheus"]["chosen"]["uid"], "mimir")
        self.assertEqual(by_type["loki"]["chosen"]["uid"], "loki1")
        self.assertEqual(by_type["tempo"]["status"], "ok")
        self.assertGreater(d["data_test"]["summary"].get("data", 0), 10)
        self.assertTrue(os.path.exists(os.path.join(
            self.out, "checkout-service-overview.validate-results.json")))

    def test_live_validation_names_missing_datasource_type_and_panels(self):
        FakeGrafana.datasources_state = [
            {"uid": "mimir", "type": "prometheus", "name": "Mimir",
             "isDefault": True}]
        r = pipeline.validate_dashboards([self.out], grafana_url="http://g",
                                         grafana_token="t",
                                         write_results=False)
        d = r["dashboards"][0]
        self.assertFalse(r["ok"])
        missing = [e for e in d["errors"] if "no loki datasource" in e]
        self.assertEqual(len(missing), 1)
        self.assertIn("needed by 3 panel(s)", missing[0])
        self.assertIn("add a loki datasource", missing[0])
        self.assertTrue(any("no tempo datasource" in e for e in d["errors"]))

    def test_live_validation_reports_query_errors_and_no_data(self):
        # test_dashboard substitutes $__rate_interval before querying
        FakeGrafana.query_errors = {
            'sum(rate(http_server_request_duration_seconds_count{service_name="checkout"}[5m])) * 60':
            "parse error: bad selector"}
        r = pipeline.validate_dashboards([self.out], grafana_url="http://g",
                                         grafana_token="t", test=True,
                                         write_results=False)
        d = r["dashboards"][0]
        self.assertFalse(r["ok"])
        self.assertTrue(any("query error: parse error" in e
                            for e in d["errors"]))

    def test_datasource_override_picks_named_instance(self):
        FakeGrafana.datasources_state.append(
            {"uid": "prom2", "type": "prometheus", "name": "Prom2",
             "isDefault": False})
        r = pipeline.validate_dashboards(
            [self.out], grafana_url="http://g", grafana_token="t",
            datasource_overrides=["prometheus=prom2"], write_results=False)
        by_type = {n["type"]: n for n in r["dashboards"][0]["datasources"]}
        self.assertEqual(by_type["prometheus"]["chosen"]["uid"], "prom2")

    def test_unknown_override_is_an_error_with_candidates(self):
        r = pipeline.validate_dashboards(
            [self.out], grafana_url="http://g", grafana_token="t",
            datasource_overrides=["prometheus=nope"], write_results=False)
        self.assertFalse(r["ok"])
        err = [e for e in r["dashboards"][0]["errors"] if "nope" in e][0]
        self.assertIn("mimir", err)

    def test_bad_token_is_connect_error(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.validate_dashboards([self.out], grafana_url="http://g",
                                         grafana_token="")
        self.assertEqual(ctx.exception.code, pipeline.EXIT_CONNECT)
        self.assertIn("rejected the token", str(ctx.exception))

    def test_bad_override_syntax_is_usage_error(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.validate_dashboards([self.out], grafana_url="http://g",
                                         grafana_token="t",
                                         datasource_overrides=["mimir"])
        self.assertEqual(ctx.exception.code, pipeline.EXIT_USAGE)


class ExportTests(_FakeMixin, unittest.TestCase):
    def test_export_creates_verifies_and_names_source(self):
        r = pipeline.export_dashboards([self.out], grafana_url="http://g",
                                       grafana_token="t", folder="Existing",
                                       test=True)
        self.assertTrue(r["ok"], r["dashboards"][0].get("problems"))
        d = r["dashboards"][0]
        self.assertEqual(d["uid"], "nr-checkout-service-overview")
        self.assertEqual(d["url"], "http://g/d/nr-checkout-service-overview/slug")
        self.assertEqual(d["source"]["name"], "Checkout Service Overview")
        self.assertTrue(d["verified"]["ok"])
        self.assertEqual(d["verified"]["panels"], 18)
        self.assertEqual(d["data_test"]["summary"], {"data": 19})
        self.assertEqual(r["totals"], {"dashboards": 1, "created": 1,
                                       "failed": 0})
        self.assertTrue(os.path.exists(os.path.join(
            self.out, "checkout-service-overview.export-results.json")))

    def test_export_binds_datasource_variables_to_instance(self):
        pipeline.export_dashboards([self.out], grafana_url="http://g",
                                   grafana_token="t")
        # the fake stored what was POSTed; inspect through a fresh client
        live = pipeline.GrafanaLive("http://g", token="t")
        posted = [b for m, p, b in live.requests if p == "/api/dashboards/db"]
        self.assertEqual(posted, [])  # fresh instance has no requests
        # so re-run and capture on the instance used by export:
        with mock.patch.object(pipeline, "GrafanaLive") as factory:
            inst = FakeGrafana("http://g", token="t")
            factory.return_value = inst
            pipeline.export_dashboards([self.out], grafana_url="http://g",
                                       grafana_token="t")
            body = [b for m, p, b in inst.requests
                    if p == "/api/dashboards/db"][0]
        variables = {v["name"]: v for v in
                     body["dashboard"]["templating"]["list"]
                     if v["type"] == "datasource"}
        self.assertEqual(variables["datasource"]["current"]["value"], "mimir")
        self.assertEqual(variables["loki_datasource"]["current"]["value"],
                         "loki1")
        self.assertNotIn("nr2grafana", body["dashboard"])
        self.assertIsNone(body["dashboard"]["id"])
        self.assertIn("Checkout Service Overview", body["message"])

    def test_export_refuses_when_datasource_missing(self):
        FakeGrafana.datasources_state = [
            {"uid": "mimir", "type": "prometheus", "name": "Mimir",
             "isDefault": True}]
        r = pipeline.export_dashboards([self.out], grafana_url="http://g",
                                       grafana_token="t")
        self.assertFalse(r["ok"])
        d = r["dashboards"][0]
        self.assertNotIn("url", d)
        self.assertTrue(any("no loki datasource" in p for p in d["problems"]))
        r2 = pipeline.export_dashboards([self.out], grafana_url="http://g",
                                        grafana_token="t", allow_missing=True)
        self.assertTrue(r2["dashboards"][0].get("url"))

    def test_export_refuses_dashboard_with_validation_errors(self):
        path = os.path.join(self.out, "checkout-service-overview.json")
        dash = json.load(open(path))
        panel = next(p for p in dash["panels"] if p.get("targets"))
        panel["targets"][0]["expr"] = "sum(rate(x[5m])"
        json.dump(dash, open(path, "w"))
        r = pipeline.export_dashboards([self.out], grafana_url="http://g",
                                       grafana_token="t")
        self.assertFalse(r["ok"])
        self.assertTrue(any(p.startswith("validation:")
                            for p in r["dashboards"][0]["problems"]))

    def test_export_import_failure_has_hint(self):
        FakeGrafana.fail_import = ("HTTP 412 on POST /api/dashboards/db: "
                                   '{"status":"name-exists"}')
        r = pipeline.export_dashboards([self.out], grafana_url="http://g",
                                       grafana_token="t")
        self.assertFalse(r["ok"])
        self.assertIn("--overwrite", r["dashboards"][0]["problems"][0])

    def test_export_needs_url(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.export_dashboards([self.out], grafana_url="")
        self.assertEqual(ctx.exception.code, pipeline.EXIT_USAGE)


class InspectExplainTests(unittest.TestCase):
    def test_inspect_model_shape(self):
        m = pipeline.inspect_inputs([KITCHEN])[0]
        self.assertEqual(m["dashboard"]["name"], "Widget Kitchen Sink")
        self.assertEqual(len(m["widgets"]), 20)
        self.assertIn("Transaction", m["event_types"])
        self.assertTrue(any(v["name"] == "app" and v["grafana"].startswith(
            "query") for v in m["variables"]))
        ds = {d["type"] for d in m["datasources_needed"]}
        self.assertIn("prometheus", ds)
        cannot = {c["visualization"] for c in m["cannot_migrate"]}
        self.assertIn("viz.event-feed", cannot)
        w = next(w for w in m["widgets"] if w["title"] == "FACET cases")
        q = w["queries"][0]
        self.assertEqual(q["parsed"]["from"], ["Transaction"])
        self.assertEqual(q["parsed"]["facet"][0]["expr"].split("(")[0],
                         "cases")
        self.assertIn("count(*)", q["parsed"]["select"][0]["expr"])
        self.assertEqual(q["translation"]["datasource"], "prometheus")
        self.assertEqual(len(q["translation"]["targets"]), 2)

    def test_inspect_text_rendering_lists_cannot_migrate(self):
        from nr2grafana.inspect import render_inspection_text
        text = render_inspection_text(pipeline.inspect_inputs([KITCHEN])[0])
        self.assertIn("Widgets that cannot be migrated", text)
        self.assertIn("Custom viz widget", text)
        self.assertIn("Grafana datasources needed", text)

    def test_explain_structure(self):
        m = pipeline.explain("SELECT average(duration) * 1000 AS 'ms' FROM "
                             "Transaction WHERE appName = 'x' AND "
                             "(host = 'a' OR name = 'b') FACET name "
                             "TIMESERIES SINCE 1 day ago UNTIL 1 hour ago")
        p = m["parsed"]
        self.assertEqual(p["select"][0]["multiplier"], 1000.0)
        self.assertEqual(p["select"][0]["alias"], "ms")
        self.assertEqual(p["from"], ["Transaction"])
        self.assertEqual({x["attribute"] for x in p["predicates"]},
                         {"appName", "host", "name"})
        self.assertEqual(p["facet"][0]["expr"], "name")
        self.assertEqual(p["until"], "1 hour ago")
        t = m["translation"]
        self.assertEqual(t["panel_hints"]["unit"], "ms")
        self.assertEqual(t["panel_hints"]["timeshift"], "1h")
        self.assertEqual(t["panel_hints"]["timefrom"], "now-23h")
        self.assertIn(" or ", t["expr"])

    def test_explain_parse_error(self):
        m = pipeline.explain("SELECT count(*) FROM Transaction WHERE name IN "
                             "(SELECT name FROM Transaction)")
        self.assertIn("subquery", m["parse_error"])
        self.assertEqual(m["translation"]["confidence"], "untranslatable")
        m = pipeline.explain("SELECT count(*) FROM (SELECT 1)")
        self.assertEqual(m["translation"]["confidence"], "untranslatable")
        self.assertTrue(any("must aggregate" in n
                            for n in m["translation"]["notes"]))


if __name__ == "__main__":
    unittest.main()


class TraceqlSearchDataTestTests(_FakeMixin, unittest.TestCase):
    def test_traceql_searches_go_through_tempo_search_api(self):
        from nr2grafana.grafana.live import is_traceql_search
        self.assertTrue(is_traceql_search(
            {"queryType": "traceql", "query": '{ status = error }'}))
        self.assertTrue(is_traceql_search({"query": "{}"}))
        self.assertFalse(is_traceql_search(
            {"queryType": "traceql", "query": "{} | rate()",
             "metricsQueryType": "range"}))
        self.assertFalse(is_traceql_search(
            {"queryType": "traceql",
             "query": '{ span.http.route = "/x" } | quantile_over_time('
                      'duration, 0.95)'}))
        self.assertFalse(is_traceql_search(
            {"queryType": "traceId", "query": "abc"}))
        FakeGrafana.tempo_traces = []
        inst = FakeGrafana("http://g", token="t")
        dash = {"panels": [{"id": 1, "type": "table", "title": "Traces",
                            "targets": [{"refId": "A",
                                         "datasource": {"type": "tempo",
                                                        "uid": "tempo1"},
                                         "queryType": "traceql",
                                         "query": '{ status = error }',
                                         "limit": 7}]}],
                "templating": {"list": []}}
        rows = inst.test_dashboard(dash, ds_map={})
        self.assertEqual(rows[0]["status"], "no-data")
        self.assertEqual(rows[0]["error"], "")
        paths = [p for m, p, _b in inst.requests if "/api/search" in p]
        self.assertEqual(len(paths), 1)
        self.assertIn("limit=7", paths[0])
        self.assertIn("q=%7B+status+%3D+error+%7D", paths[0])
        self.assertFalse(any(p == "/api/ds/query" for _m, p, _b
                             in inst.requests))
        FakeGrafana.tempo_traces = [{"traceID": "abc"}]
        rows = FakeGrafana("http://g", token="t").test_dashboard(
            dash, ds_map={})
        self.assertEqual(rows[0]["status"], "data")
        self.assertEqual(rows[0]["points"], 1)
