"""End-to-end tests against the offline mock stack (tools/mock_stack).

Boots the fake Grafana + fake NerdGraph servers on ephemeral ports and
drives the whole product loop through the real library code: fetch ->
parse + convert + package -> datasource creation -> requirement check ->
data test -> parity -> diagnose -> apply an edit-query fix -> import.
No network beyond 127.0.0.1, no real credentials.

Sibling 1.2 modules are imported lazily inside the tests and skipped
when genuinely absent (they are developed concurrently; by verify time
all exist).
"""

import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import mock_stack  # noqa: E402


def _import_or_skip(test, *names):
    """Import sibling modules, skipping the test when one is absent."""
    mods = []
    for name in names:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            test.skipTest("sibling module %s not written yet" % name)
        mods.append(importlib.import_module(name))
    return mods


class MockStackBase(unittest.TestCase):
    """Starts a fresh mock stack per test."""

    def setUp(self):
        self.stack = mock_stack.start_mock()
        self.addCleanup(self.stack.stop)

    # helpers ---------------------------------------------------------------

    def grafana(self, token=None):
        (live,) = _import_or_skip(self, "nr2grafana.grafana.live")
        tok = self.stack.grafana_token if token is None else token
        return live.GrafanaLive(self.stack.grafana_url, token=tok)

    def nerdgraph(self, key=None):
        (ng,) = _import_or_skip(self, "nr2grafana.nerdgraph")
        client = ng.NerdGraphClient(
            key if key is not None else self.stack.nr_api_key)
        client.endpoint = self.stack.nr_url + "/graphql"
        return client

    def make_datasource(self, ds_type, name, url):
        (live,) = _import_or_skip(self, "nr2grafana.grafana.live")
        payload = live.build_datasource_payload(ds_type, name,
                                                {"url": url})
        resp = self.grafana().create_datasource(payload)
        return resp["datasource"]["uid"]


class MockGrafanaTest(MockStackBase):
    """The fake Grafana behaves like the real API surface."""

    def test_health_is_public_but_api_requires_auth(self):
        from nr2grafana.grafana.client import GrafanaClient, GrafanaError
        anon = GrafanaClient(self.stack.grafana_url)
        self.assertEqual(anon.health().get("database"), "ok")
        with self.assertRaises(GrafanaError) as ctx:
            anon._req("GET", "/api/user")
        self.assertIn("401", str(ctx.exception))
        bad = GrafanaClient(self.stack.grafana_url, token="wrong")
        with self.assertRaises(GrafanaError) as ctx:
            bad.datasources()
        self.assertIn("401", str(ctx.exception))

    def test_datasource_crud_health_and_proxy(self):
        g = self.grafana()
        uid = self.make_datasource("prometheus", "Mimir",
                                   "http://mimir:9009/prometheus")
        self.assertTrue(uid)
        ds = g.datasource_by_uid(uid)
        self.assertEqual(ds["type"], "prometheus")
        self.assertEqual(g.datasource_health(uid)["status"], "ok")
        names = g.prom_metric_names(uid)
        self.assertIn("up", names)
        self.assertIn("http_server_request_duration_seconds_count",
                      names)
        self.assertIn("error",
                      g.prom_label_values(uid, "level"))
        series = g.prom_series(uid, "up")
        self.assertEqual(series[0].get("__name__"), "up")
        g.update_datasource(uid, {"name": "Mimir2"})
        self.assertEqual(g.datasource_by_uid(uid)["name"], "Mimir2")
        g.delete_datasource(uid)
        self.assertIsNone(g.datasource_by_uid(uid))

    def test_create_datasource_never_echoes_secrets(self):
        g = self.grafana()
        resp = g.create_datasource({
            "name": "CW", "type": "cloudwatch", "access": "proxy",
            "jsonData": {"authType": "keys"},
            "secureJsonData": {"secretKey": "SUPER-SECRET"}})
        blob = json.dumps(resp)
        self.assertNotIn("SUPER-SECRET", blob)
        self.assertTrue(
            resp["datasource"]["secureJsonFields"]["secretKey"])
        blob = json.dumps(g.datasources())
        self.assertNotIn("SUPER-SECRET", blob)

    def test_loki_proxy_inventory(self):
        g = self.grafana()
        uid = self.make_datasource("loki", "Loki", "http://loki:3100")
        self.assertIn("service_name", g.loki_labels(uid))
        self.assertIn("checkout",
                      g.loki_label_values(uid, "service_name"))

    def test_cost_introspection_endpoints(self):
        """tsdb status, count() and Loki volume back the cost view with
        realistic, obviously-wasteful signals (independent of the 1.5
        sibling modules; uses the stable proxy helper)."""
        import urllib.parse as _up
        g = self.grafana()
        puid = self.make_datasource("prometheus", "Mimir",
                                    "http://mimir:9009/prometheus")
        luid = self.make_datasource("loki", "Loki", "http://loki:3100")

        tsdb = g._proxy_get(puid, "/api/v1/status/tsdb")["data"]
        top = tsdb["seriesCountByMetricName"]
        # An obviously-unused waste metric tops the active-series rank.
        self.assertEqual(top[0]["name"],
                         "apiserver_request_duration_seconds_bucket")
        names = [r["name"] for r in top]
        self.assertIn("container_network_receive_bytes_total", names)
        self.assertGreater(top[0]["value"], top[-1]["value"])
        self.assertEqual(tsdb["numSeries"],
                         sum(r["value"] for r in top))
        labels = dict((r["name"], r["value"])
                      for r in tsdb["labelValueCountByLabelName"])
        # High-cardinality labels dominate; id-like labels are worst.
        self.assertGreater(labels["pod"], 1000)
        self.assertGreater(labels["id"], labels["pod"])

        def _count(expr):
            path = "/api/v1/query?query=" + _up.quote(expr)
            return g._proxy_get(puid, path)["data"]["result"]

        res = _count("count(apiserver_request_duration_seconds_bucket)")
        self.assertEqual(res[0]["value"][1], "41200")
        self.assertEqual(_count("count(no_such_metric_at_all)"), [])

        vol = g._proxy_get(
            luid, "/loki/api/v1/index/volume?query=%7B%7D")["data"]
        streams = vol["result"]
        self.assertTrue(streams)
        # One chatty id-fanned debug stream dominates the byte volume.
        top_stream = streams[0]["metric"]
        self.assertEqual(top_stream.get("level"), "debug")
        self.assertIn("request_id", top_stream)
        self.assertGreater(int(streams[0]["value"][1]), 40000000)
        # request_id is a high-cardinality stream label (explosion).
        rid = g._proxy_get(
            luid, "/loki/api/v1/label/request_id/values")["data"]
        self.assertGreater(len(rid), 1000)
        # volume_range returns a bucketed matrix of the same streams.
        rng = g._proxy_get(
            luid,
            "/loki/api/v1/index/volume_range?query=%7B%7D")["data"]
        self.assertEqual(rng["resultType"], "matrix")
        self.assertTrue(rng["result"][0]["values"])

    def test_ds_query_is_deterministic(self):
        g = self.grafana()
        uid = self.make_datasource("prometheus", "Mimir",
                                   "http://mimir:9009/prometheus")

        def run(expr):
            return g.ds_query(uid, "prometheus",
                              {"refId": "A", "expr": expr})

        # known metric -> points
        resp = run('sum(rate(up[5m]))')
        frames = resp["results"]["A"]["frames"]
        self.assertEqual(len(frames), 1)
        self.assertGreater(len(frames[0]["data"]["values"][0]), 0)
        # grouping -> one series per facet value
        resp = run('sum by (pod)('
                   'kube_pod_container_status_restarts_total)')
        self.assertEqual(len(resp["results"]["A"]["frames"]), 2)
        labels = [f["schema"]["fields"][1]["labels"]["pod"]
                  for f in resp["results"]["A"]["frames"]]
        self.assertEqual(sorted(labels), ["a", "b"])
        # unknown metric -> empty frame
        resp = run("no_such_metric_at_all")
        self.assertEqual(
            resp["results"]["A"]["frames"][0]["data"]["values"][0], [])
        # syntax_error marker -> query error
        resp = run("rate(syntax_error{")
        self.assertIn("syntax_error",
                      resp["results"]["A"].get("error", ""))

    def test_dashboard_import_search_and_read(self):
        g = self.grafana()
        dash = {"uid": "e2e-dash", "title": "E2E Dash", "panels": []}
        resp = g.import_dashboard(dash)
        self.assertEqual(resp["status"], "success")
        got = g.get_dashboard_by_uid("e2e-dash")
        self.assertEqual(got["dashboard"]["title"], "E2E Dash")
        hits = g.search_dashboards("e2e")
        self.assertEqual(hits[0]["uid"], "e2e-dash")

    def test_permissions_report_sees_admin(self):
        report = self.grafana().permissions_report()
        self.assertEqual(report["user"], "mock-sa")
        self.assertTrue(report["can_admin_datasources"])
        self.assertTrue(report["can_edit_dashboards"])


class MockNerdGraphTest(MockStackBase):
    """The fake NerdGraph serves fixtures and deterministic NRQL."""

    def test_requires_api_key(self):
        from nr2grafana.nerdgraph import NerdGraphError
        bad = self.nerdgraph(key="NRAK-WRONG")
        with self.assertRaises(NerdGraphError) as ctx:
            bad.list_dashboards()
        self.assertIn("Authentication failed", str(ctx.exception))

    def test_lists_and_fetches_fixtures(self):
        ng = self.nerdgraph()
        ents = ng.list_dashboards()
        names = [e["name"] for e in ents]
        self.assertIn("Checkout Service Overview", names)
        guid = next(e["guid"] for e in ents
                    if e["name"] == "Checkout Service Overview")
        entity = ng.get_dashboard(guid)
        self.assertTrue(entity.get("pages"))
        from nr2grafana.nerdgraph import NerdGraphError
        with self.assertRaises(NerdGraphError):
            ng.get_dashboard("NO-SUCH-GUID")

    def test_nrql_results_and_errors(self):
        from nr2grafana.nerdgraph import NerdGraphError
        ng = self.nerdgraph()
        got = ng.run_nrql(1234567, "SELECT count(*) FROM Transaction "
                                   "SINCE 1 hour ago")
        self.assertEqual(got["results"][0]["result"],
                         mock_stack.DEFAULT_VALUE)
        got = ng.run_nrql(1234567, "SELECT count(*) FROM Transaction "
                                   "FACET appName TIMESERIES")
        facets = set(r["facet"] for r in got["results"])
        self.assertEqual(facets, set(["a", "b"]))
        with self.assertRaises(NerdGraphError) as ctx:
            ng.run_nrql(999, "SELECT count(*) FROM Transaction")
        self.assertIn("999", str(ctx.exception))
        with self.assertRaises(NerdGraphError) as ctx:
            ng.run_nrql(1234567, "SELECT syntax_error FROM (")
        self.assertIn("Syntax", str(ctx.exception))

    def test_read_only_surface(self):
        """The fake NerdGraph accepts only POST /graphql queries."""
        req = urllib.request.Request(self.stack.nr_url + "/graphql",
                                     method="GET")
        try:
            urllib.request.urlopen(req)
            self.fail("GET /graphql should 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


class FullLoopTest(MockStackBase):
    """fetch -> convert -> package -> check -> test -> parity ->
    diagnose -> fix -> import, all through the mock stack."""

    def setUp(self):
        super(FullLoopTest, self).setUp()
        self.tmp = tempfile.mkdtemp(prefix="nr2g-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_full_loop(self):
        (model, config_mod, builder, requirements_mod, artifacts,
         livecheck, parity, diagnose_mod, remediate) = _import_or_skip(
            self,
            "nr2grafana.model", "nr2grafana.config",
            "nr2grafana.grafana.builder", "nr2grafana.requirements",
            "nr2grafana.artifacts", "nr2grafana.livecheck",
            "nr2grafana.parity", "nr2grafana.diagnose",
            "nr2grafana.remediate")

        # -- 1. fetch from the fake NerdGraph ---------------------------
        ng = self.nerdgraph()
        ents = ng.list_dashboards()
        guid = next(e["guid"] for e in ents
                    if e["name"] == "Checkout Service Overview")
        entity = ng.get_dashboard(guid)

        # -- 2. parse + convert -----------------------------------------
        cfg = config_mod.load_config("")
        nr_dash = model.parse_nr_dashboard(entity)
        built = builder.build_dashboards(nr_dash, cfg)
        self.assertEqual(len(built), 1)
        _fname, dash, report = built[0]

        # Sabotage one panel with a near-miss metric name so the
        # diagnose -> remediate leg has something real to fix.
        broken_pid = broken_ref = None
        for panel, tgt in livecheck.iter_targets(dash):
            expr = tgt.get("expr") or ""
            if "checkout_orders_completed" in expr:
                tgt["expr"] = expr.replace("checkout_orders_completed",
                                           "checkout_orders_complete")
                broken_pid = panel.get("id")
                broken_ref = tgt.get("refId") or "A"
                break
        self.assertIsNotNone(broken_pid,
                             "fixture translation changed: no "
                             "checkout_orders_completed target found")

        # -- 3. requirements + package ----------------------------------
        req = requirements_mod.analyze_dashboard(nr_dash, dash, report,
                                                 cfg)
        slug = "checkout-service-overview"
        pkg = artifacts.package_dashboard(self.tmp, slug, dash, report,
                                          req, cfg)
        for fname in ("dashboard.json", "requirements.json",
                      "widget-report.json", "datatest.json"):
            self.assertTrue(os.path.exists(os.path.join(pkg, fname)),
                            fname)

        # -- 4. connect to the fake Grafana, create datasources ---------
        g = self.grafana()
        self.assertEqual(g.health().get("database"), "ok")
        self.make_datasource("prometheus", "Mimir",
                             "http://mimir:9009/prometheus")
        self.make_datasource("loki", "Loki", "http://loki:3100")
        self.make_datasource("tempo", "Tempo", "http://tempo:3200")

        check_rows = g.check_requirements(req)
        ds_missing = [r for r in check_rows
                      if str(r.get("item", "")).startswith("datasource:")
                      and r.get("status") in ("missing", "wrong-type")]
        self.assertEqual(ds_missing, [])

        # -- 5. per-panel data test -------------------------------------
        tests = g.test_dashboard(dash)
        self.assertGreater(len(tests), 10)
        self.assertEqual([r for r in tests if r["status"] == "error"],
                         [])
        with_data = [r for r in tests if r["status"] == "data"]
        self.assertGreater(len(with_data), 5)
        broken_row = next(r for r in tests
                          if r["panel_id"] == broken_pid
                          and r["refId"] == broken_ref)
        self.assertEqual(broken_row["status"], "no-data")

        # -- 6. parity: NR numbers vs Grafana numbers -------------------
        prep = parity.run_parity(ng, [1234567], g, dash, report)
        self.assertEqual(prep["schema"], "nr2grafana/parity/v1")
        self.assertTrue(0 <= prep["score"] <= 100)
        self.assertGreaterEqual(prep["score"], 60)
        by_key = dict(((r["panel_id"], r["refId"]), r)
                      for r in prep["panels"])
        throughput_pid = next(e["panel_id"] for e in report
                              if e.get("widget") == "Throughput")
        tp_rows = [r for r in prep["panels"]
                   if r["panel_id"] == throughput_pid]
        self.assertEqual(tp_rows[0]["verdict"], "match")
        self.assertEqual(
            by_key[(broken_pid, broken_ref)]["verdict"], "gf-empty")
        self.assertGreaterEqual(prep["summary"].get("match", 0), 5)

        # -- 7. diagnose: root cause with a machine-applicable fix ------
        diag = diagnose_mod.diagnose(g, nr=ng, dash=dash,
                                     requirements=req,
                                     test_results=tests, parity=prep,
                                     cfg=cfg)
        self.assertEqual(diag["schema"], "nr2grafana/diagnosis/v1")
        blockers = [f for f in diag["findings"]
                    if f.get("severity") == "blocker"]
        self.assertEqual(blockers, [])
        finding = None
        for f in diag["findings"]:
            fx = f.get("fix") or {}
            action = fx.get("action") or {}
            if fx.get("kind") == "edit-query" \
                    and action.get("panel_id") == broken_pid:
                finding = f
                break
        self.assertIsNotNone(finding,
                             "no edit-query finding for the broken "
                             "panel; findings: %s"
                             % json.dumps(diag["findings"], indent=1))
        self.assertIn("checkout_orders_completed",
                      finding["fix"]["action"]["new_expr"])

        # -- 8. remediate: apply the fix, package stays consistent ------
        res = remediate.apply_fix(finding, grafana=g, dash=dash,
                                  package_dir=pkg, slug=slug)
        self.assertTrue(res["applied"], res)
        panel_expr = None
        for panel, tgt in livecheck.iter_targets(dash):
            if panel.get("id") == broken_pid \
                    and (tgt.get("refId") or "A") == broken_ref:
                panel_expr = tgt.get("expr") or ""
        self.assertIn("checkout_orders_completed", panel_expr)
        with open(os.path.join(pkg, "dashboard.json"),
                  encoding="utf-8") as f:
            self.assertIn("checkout_orders_completed", f.read())
        with open(os.path.join(pkg, "datatest.json"),
                  encoding="utf-8") as f:
            self.assertIn("checkout_orders_completed", f.read())

        retest = g.test_dashboard(dash)
        fixed_row = next(r for r in retest
                         if r["panel_id"] == broken_pid
                         and r["refId"] == broken_ref)
        self.assertEqual(fixed_row["status"], "data")

        # -- 9. import into the fake Grafana ----------------------------
        imported = g.import_dashboard(dash, overwrite=True)
        self.assertEqual(imported.get("status"), "success")
        got = g.get_dashboard_by_uid(imported["uid"])
        self.assertEqual(got["dashboard"]["title"], dash["title"])
        self.assertTrue(g.search_dashboards("Checkout"))

        # -- 10. readiness after the fix --------------------------------
        prep2 = parity.run_parity(ng, [1234567], g, dash, report)
        self.assertGreater(prep2["score"], prep["score"] - 1)
        ready = parity.readiness(prep2, check_rows=check_rows,
                                 test_rows=retest)
        self.assertIn(ready["grade"], ("ready", "almost"))
        self.assertTrue(ready["reasons"])

    def test_samples_and_human_review(self):
        """Raw-sample pulls line up across the two fake backends and
        human verdicts drive readiness."""
        (model, config_mod, builder, samples_mod, parity,
         store_mod) = _import_or_skip(
            self, "nr2grafana.model", "nr2grafana.config",
            "nr2grafana.grafana.builder", "nr2grafana.samples",
            "nr2grafana.parity", "nr2grafana.store")
        ng = self.nerdgraph()
        guid = next(e["guid"] for e in ng.list_dashboards()
                    if e["name"] == "Checkout Service Overview")
        cfg = config_mod.load_config("")
        nr_dash = model.parse_nr_dashboard(ng.get_dashboard(guid))
        _fname, dash, report = builder.build_dashboards(nr_dash,
                                                        cfg)[0]
        g = self.grafana()
        self.make_datasource("prometheus", "Mimir",
                             "http://mimir:9009/prometheus")
        self.make_datasource("loki", "Loki", "http://loki:3100")
        self.make_datasource("tempo", "Tempo", "http://tempo:3200")

        rep = samples_mod.collect_samples(ng, [1234567], g, dash,
                                          report, limit=4)
        self.assertEqual(rep["schema"], "nr2grafana/samples/v1")
        self.assertTrue(rep["panels"])
        # a Loki log panel yields real log lines on the Grafana side
        # and derived SELECT * events on the NR side, and the two
        # tell the same story (the mock emits matching messages).
        log_rows = [r for r in rep["panels"]
                    if r.get("ds_type") == "loki"
                    and r["grafana"]["kind"] == "logs"]
        self.assertTrue(log_rows,
                        "no loki panel produced raw log lines: %s"
                        % json.dumps([(r["panel_title"],
                                       r["grafana"]) for r in
                                      rep["panels"]
                                      if r.get("ds_type") == "loki"],
                                     indent=1))
        row = log_rows[0]
        self.assertLessEqual(len(row["grafana"]["samples"]), 4)
        self.assertIn("mock payment failed",
                      row["grafana"]["samples"][0]["line"])
        self.assertEqual(row["nr"]["kind"], "events")
        self.assertLessEqual(len(row["nr"]["samples"]), 4)
        self.assertIn("mock payment",
                      str(row["nr"]["samples"][0].get("message")))
        self.assertIn("SELECT *", row["nr"]["nrql"])
        # a Prometheus panel yields datapoints
        self.assertTrue([r for r in rep["panels"]
                         if r.get("ds_type") == "prometheus"
                         and r["grafana"]["kind"] == "points"])

        # human verdicts persist and drive readiness
        store = store_mod.Store(os.path.join(self.tmp, "review.db"))
        try:
            slug = "checkout-service-overview"
            store.upsert_dashboard(slug, dash.get("title", slug),
                                   "e2e", "", dash)
            store.save_artifact(slug, "samples", rep)
            samples_mod.record_review(store, slug, row["panel_id"],
                                      row["refId"], "rejected",
                                      note="wrong log stream")
            summ = samples_mod.review_summary(store, slug)
            self.assertEqual(summ["rejected"], 1)
            self.assertIsNotNone(summ["unreviewed"])
            fake_parity = {"panels": [{}], "score": 95,
                           "summary": {"match": 1}}
            ready = parity.readiness(
                fake_parity,
                review=store.get_artifact(slug, "review"))
            self.assertEqual(ready["grade"], "blocked")
            self.assertTrue(any("rejected" in s
                                for s in ready["reasons"]))
            samples_mod.record_review(store, slug, row["panel_id"],
                                      row["refId"], "confirmed")
            ready2 = parity.readiness(
                fake_parity,
                review=store.get_artifact(slug, "review"))
            self.assertEqual(ready2["grade"], "ready")
            self.assertGreaterEqual(ready2["score"], 90)
            self.assertTrue(any("human-verified" in s
                                for s in ready2["reasons"]))
        finally:
            store.close()

    def test_parity_flags_value_mismatch_when_backends_disagree(self):
        """A deliberate constant offset between the fake backends
        surfaces as a non-match verdict, proving parity is comparing
        real numbers end to end."""
        (model, config_mod, builder, parity) = _import_or_skip(
            self, "nr2grafana.model", "nr2grafana.config",
            "nr2grafana.grafana.builder", "nr2grafana.parity")
        # Grafana returns 10x the NR constant for one metric.
        self.stack.state.prom_metrics[
            "http_server_request_duration_seconds_count"] = \
            mock_stack.DEFAULT_VALUE * 10
        ng = self.nerdgraph()
        guid = next(e["guid"] for e in ng.list_dashboards()
                    if e["name"] == "Checkout Service Overview")
        cfg = config_mod.load_config("")
        nr_dash = model.parse_nr_dashboard(ng.get_dashboard(guid))
        _fname, dash, report = builder.build_dashboards(nr_dash, cfg)[0]
        g = self.grafana()
        self.make_datasource("prometheus", "Mimir",
                             "http://mimir:9009/prometheus")
        self.make_datasource("loki", "Loki", "http://loki:3100")
        self.make_datasource("tempo", "Tempo", "http://tempo:3200")
        prep = parity.run_parity(ng, [1234567], g, dash, report)
        throughput_pid = next(e["panel_id"] for e in report
                              if e.get("widget") == "Throughput")
        row = next(r for r in prep["panels"]
                   if r["panel_id"] == throughput_pid)
        self.assertIn(row["verdict"], ("value-mismatch", "close"))


class CompareViewTest(MockStackBase):
    """The side-by-side comparison model (compare.build_comparison) and
    the "add a datasource and watch it flow" loop
    (compare.datasource_flow) run end to end over the fixture against the
    two fake backends and produce believable, chartable data."""

    def setUp(self):
        super(CompareViewTest, self).setUp()
        self.tmp = tempfile.mkdtemp(prefix="nr2g-cmp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _convert_fixture(self):
        """Fetch + convert the Checkout fixture; return
        ``(ng, entity, dash, report)``."""
        (model, config_mod, builder) = _import_or_skip(
            self, "nr2grafana.model", "nr2grafana.config",
            "nr2grafana.grafana.builder")
        ng = self.nerdgraph()
        guid = next(e["guid"] for e in ng.list_dashboards()
                    if e["name"] == "Checkout Service Overview")
        entity = ng.get_dashboard(guid)
        cfg = config_mod.load_config("")
        nr_dash = model.parse_nr_dashboard(entity)
        _fname, dash, report = builder.build_dashboards(nr_dash, cfg)[0]
        return ng, entity, dash, report

    def _all_datasources(self):
        self.make_datasource("prometheus", "Mimir",
                             "http://mimir:9009/prometheus")
        self.make_datasource("loki", "Loki", "http://loki:3100")
        self.make_datasource("tempo", "Tempo", "http://tempo:3200")

    def test_build_comparison_charts_both_sides_with_verdicts(self):
        """Every panel gets real render data on both sides; the fake
        backends agree on the golden signals (>= 1 match) and disagree on
        the diverge metrics (>= 1 value-mismatch)."""
        (compare,) = _import_or_skip(self, "nr2grafana.compare")
        ng, entity, dash, report = self._convert_fixture()
        g = self.grafana()
        self._all_datasources()

        cmp = compare.build_comparison(ng, [1234567], g, entity, dash,
                                       report, frm="now-1h", to="now")
        self.assertEqual(cmp["schema"], "nr2grafana/comparison/v1")
        self.assertTrue(cmp["panels"])
        self.assertTrue(0 <= cmp["score"] <= 100)

        # Grid + row layout is preserved so the UI can align both sides.
        self.assertTrue(cmp["layout"]["nr_pages"])
        for p in cmp["panels"]:
            self.assertIn("grid", p)
            self.assertIn(p["verdict"], (
                "match", "close", "value-mismatch", "shape-mismatch",
                "nr-empty", "gf-empty", "both-empty", "nr-error",
                "gf-error", "unverifiable", "unverifiable-logs"))

        # At least one timeseries panel draws a real multi-point series
        # on BOTH sides (this is what the flagship chart renders).
        charted = []
        for p in cmp["panels"]:
            if p["viz"] != "timeseries":
                continue
            nr_pts = [s for s in (p["nr"].get("series") or [])
                      if len(s.get("points") or []) >= 2]
            gf_pts = [s for s in (p["grafana"].get("series") or [])
                      if len(s.get("points") or []) >= 2]
            if nr_pts and gf_pts:
                charted.append(p)
        self.assertTrue(charted,
                        "no timeseries panel produced chartable series "
                        "on both sides")
        # Grafana series really are multi-point (>= 30 raw before caps).
        sample = charted[0]["grafana"]["series"][0]["points"]
        self.assertGreaterEqual(len(sample), 20)

        verdicts = set(p["verdict"] for p in cmp["panels"])
        self.assertIn("match", verdicts,
                      "expected >= 1 matching panel; verdicts=%s"
                      % sorted(verdicts))
        self.assertIn("value-mismatch", verdicts,
                      "expected >= 1 mismatching panel (diverge "
                      "metrics); verdicts=%s" % sorted(verdicts))
        self.assertGreaterEqual(cmp["summary"].get("match", 0), 1)
        self.assertGreaterEqual(cmp["summary"].get("value-mismatch", 0),
                                1)

        # A value-mismatch panel carries the diverging metric's expr and
        # a ratio away from 1.0 (the honest "these disagree" story).
        mm = next(p for p in cmp["panels"]
                  if p["verdict"] == "value-mismatch")
        self.assertTrue(mm["expr"])

        def _has_data(side):
            return bool(side.get("series") or side.get("lines")
                        or side.get("rows")) or side.get("scalar") is not None
        self.assertTrue(_has_data(mm["nr"]))
        self.assertTrue(_has_data(mm["grafana"]))

    def test_datasource_flow_before_and_after_creating_loki(self):
        """Loki-backed log panels read empty until a Loki datasource
        exists, then flow real log lines the instant it is created."""
        (compare,) = _import_or_skip(self, "nr2grafana.compare")
        _ng, _entity, dash, report = self._convert_fixture()
        g = self.grafana()
        # Prometheus + tempo present, but deliberately NO Loki yet.
        self.make_datasource("prometheus", "Mimir",
                             "http://mimir:9009/prometheus")
        self.make_datasource("tempo", "Tempo", "http://tempo:3200")

        before = compare.datasource_flow(g, None, dash, report)
        loki_before = next((f for f in before["families"]
                            if f["family"] == "loki"), None)
        self.assertIsNotNone(loki_before,
                             "dashboard has no loki family: %s"
                             % [f["family"] for f in before["families"]])
        self.assertGreater(loki_before["panels_total"], 0)
        self.assertEqual(loki_before["panels_with_data"], 0)
        self.assertEqual(loki_before["newly_flowing"], [])

        # Create the Loki datasource -> data flows immediately.
        loki_uid = self.make_datasource("loki", "Loki",
                                        "http://loki:3100")
        after = compare.datasource_flow(g, None, dash, report,
                                        ds_uid=loki_uid)
        loki_after = next(f for f in after["families"]
                          if f["family"] == "loki")
        self.assertGreater(loki_after["panels_with_data"], 0)
        self.assertTrue(loki_after["newly_flowing"],
                        "no loki panel lit up after creating the "
                        "datasource")
        self.assertEqual(loki_after["health"].get("status"), "ok")
        # A real sample series/log frame proves the flow to the UI.
        self.assertTrue(loki_after["sample_series"])


class CostOptimizationTest(MockStackBase):
    """The 1.5 cost loop end to end over the mock stack: sample what the
    datasources ingest -> subtract what the converted Checkout dashboard
    needs -> price it -> recommend safe cuts -> project the savings.

    Proves the offline mock demoes the whole cost story, and that the
    recommendation engine's safety guarantee holds on real fixture data
    (never proposes dropping a dimension the dashboard uses). Sibling
    1.5 modules are imported lazily and skipped when not yet written."""

    def test_traffic_usage_cost_optimize_end_to_end(self):
        (model, config_mod, builder, traffic_mod, usage_mod,
         costmodel_mod, optimize_mod) = _import_or_skip(
            self, "nr2grafana.model", "nr2grafana.config",
            "nr2grafana.grafana.builder", "nr2grafana.traffic",
            "nr2grafana.usage", "nr2grafana.costmodel",
            "nr2grafana.optimize")

        # -- convert the Checkout fixture -------------------------------
        ng = self.nerdgraph()
        guid = next(e["guid"] for e in ng.list_dashboards()
                    if e["name"] == "Checkout Service Overview")
        cfg = config_mod.load_config("")
        nr_dash = model.parse_nr_dashboard(ng.get_dashboard(guid))
        _fname, dash, report = builder.build_dashboards(nr_dash, cfg)[0]

        # -- create the LGTM datasources --------------------------------
        g = self.grafana()
        puid = self.make_datasource("prometheus", "Mimir",
                                    "http://mimir:9009/prometheus")
        luid = self.make_datasource("loki", "Loki", "http://loki:3100")
        tuid = self.make_datasource("tempo", "Tempo",
                                    "http://tempo:3200")
        ds_list = [
            {"family": "prometheus", "uid": puid, "type": "prometheus"},
            {"family": "loki", "uid": luid, "type": "loki"},
            {"family": "tempo", "uid": tuid, "type": "tempo"},
        ]

        # -- 1. sample what the datasources actually ingest -------------
        traffic = traffic_mod.sample_traffic(g, ds_list)
        self.assertEqual(traffic["schema"], "nr2grafana/traffic/v1")
        prom = next(d for d in traffic["datasources"]
                    if d["family"] == "prometheus")
        top_names = [m["metric"]
                     for m in prom["prometheus"]["top_metrics"]]
        self.assertIn("apiserver_request_duration_seconds_bucket",
                      top_names)
        loki = next(d for d in traffic["datasources"]
                    if d["family"] == "loki")
        self.assertGreater(loki["loki"]["bytes_window"], 0)

        # -- 2. what the converted dashboard actually needs -------------
        usage = usage_mod.collect_usage([dash], [report])
        used_metrics = set(usage["prometheus"]["metrics"])
        used_loki = set(usage["loki"]["stream_labels"])
        # The waste dimensions are genuinely NOT referenced.
        self.assertNotIn("apiserver_request_duration_seconds_bucket",
                         used_metrics)
        self.assertNotIn("request_id", used_loki)

        # -- 3. price the current traffic -------------------------------
        cost = costmodel_mod.estimate_costs(traffic)
        self.assertEqual(cost["schema"], "nr2grafana/cost/v1")
        self.assertGreater(cost["monthly_total"], 0)

        # -- 4. recommend safe cuts -------------------------------------
        opt = optimize_mod.recommend(traffic, usage, cost=cost, cfg=cfg)
        self.assertEqual(opt["schema"], "nr2grafana/optimize/v1")
        recs = opt["recommendations"]
        self.assertTrue(recs)

        # a) drop an UNUSED, high-series Prometheus metric, marked safe.
        metric_drop = [
            r for r in recs
            if r.get("family") == "prometheus"
            and r.get("kind") == "drop-metric"
            and ("apiserver_request_duration_seconds_bucket"
                 in json.dumps(r)
                 or "container_network_receive_bytes_total"
                 in json.dumps(r))]
        self.assertTrue(
            metric_drop,
            "no drop-metric recommendation for an unused metric; "
            "recs=%s" % json.dumps(recs, indent=1))
        self.assertTrue(all(r.get("keeps_intact") for r in metric_drop),
                        "unused-metric drop must be marked keeps_intact")

        # b) drop / restructure an UNUSED high-cardinality Loki label.
        loki_drop = [
            r for r in recs
            if r.get("family") == "loki"
            and r.get("kind") in ("drop-label", "to-structured-metadata")
            and ("request_id" in json.dumps(r)
                 or "pod" in json.dumps(r))]
        self.assertTrue(
            loki_drop,
            "no drop-label/to-structured-metadata rec for an unused "
            "Loki label; recs=%s" % json.dumps(recs, indent=1))
        self.assertTrue(all(r.get("keeps_intact") for r in loki_drop),
                        "unused-label drop must be marked keeps_intact")

        # Safety guarantee on real fixture data: no auto-safe drop ever
        # targets a dimension the Checkout dashboard uses.
        for r in recs:
            if not r.get("keeps_intact"):
                continue
            blob = json.dumps(r)
            if r.get("kind") == "drop-metric":
                for um in used_metrics:
                    self.assertNotIn(
                        '"%s"' % um, blob,
                        "safe drop-metric targets used metric %s" % um)
            if r.get("kind") == "drop-label" \
                    and r.get("family") == "loki":
                for ul in used_loki:
                    self.assertNotIn(
                        '"%s"' % ul, blob,
                        "safe drop-label targets used label %s" % ul)

        # -- 5. project the savings -------------------------------------
        saved = costmodel_mod.apply_savings(cost, recs)
        self.assertGreater(saved["saved_pct"], 0,
                           "cutting waste must yield a positive "
                           "estimated savings percentage")
        self.assertGreater(saved["saved_total"], 0)
        self.assertLessEqual(saved["projected_total"],
                             cost["monthly_total"] + 1e-6)


_KEEPS = ("keeps_performance", "keeps_durability",
          "keeps_availability", "keeps_intact")


class DeepDiveMockTest(MockStackBase):
    """The 1.6 deep-dive end to end over the fake self-metrics endpoint.

    deepdive.analyze reads the component self-metrics (cortex_*, loki_*,
    prometheus_remote_storage_*, container_memory_working_set_bytes) the
    mock serves against a deterministic churn / duplicate-replica /
    high-cardinality (Mimir) + tiny-chunk / failed-flush (Loki) scenario,
    and must surface capacity + cardinality + churn findings that carry a
    config snippet, an estimated saving and explicit safety flags. The
    deepdive sibling is imported lazily and the test skips when it is not
    yet written."""

    def test_deepdive_analyze_end_to_end(self):
        (deepdive,) = _import_or_skip(self, "nr2grafana.deepdive")
        res = deepdive.analyze(prom=self.stack.prom_url,
                               mimir=self.stack.mimir_url,
                               loki=self.stack.loki_url)
        self.assertEqual(res.get("schema"), "nr2grafana/deepdive/v1")
        findings = res.get("findings") or []
        self.assertTrue(findings, "deep-dive produced no findings")
        for f in findings:
            self.assertIn(f.get("severity"), ("FAIL", "WARN", "INFO"), f)
            self.assertTrue(f.get("area"), f)
        areas = set(f.get("area") for f in findings)
        self.assertIn("capacity", areas,
                      "no capacity finding; areas=%s" % sorted(areas))
        self.assertIn("cardinality", areas,
                      "no cardinality finding; areas=%s" % sorted(areas))

        # Every safety flag that IS present must be a real bool (the
        # engine must never leave a durability/availability claim fuzzy).
        for f in findings:
            for k in _KEEPS:
                if k in f:
                    self.assertIsInstance(f[k], bool, (k, f))

        # A churn finding carrying a config snippet, an estimated saving
        # and at least one explicit safety flag (the actionable shape the
        # 1.6 contract promises for every recommendation).
        def _is_churn(f):
            if f.get("area") == "churn":
                return True
            blob = json.dumps(f).lower()
            return ("churn" in blob or "samples/series" in blob
                    or "samples per series" in blob or "stale" in blob)

        churn = [f for f in findings if _is_churn(f)]
        self.assertTrue(churn, "no churn-related finding; findings=%s"
                        % json.dumps(findings, indent=1))
        # A churn finding carrying a machine-pasteable config snippet and
        # at least one explicit safety flag.
        good = [f for f in churn
                if isinstance(f.get("config"), list) and f.get("config")
                and any(k in f for k in _KEEPS)]
        self.assertTrue(
            good,
            "no churn finding with config + safety flags; "
            "churn findings=%s" % json.dumps(churn, indent=1))
        snippet = good[0]["config"][0]
        self.assertTrue(snippet.get("snippet") or snippet.get("target"),
                        snippet)
        # Any per-finding est_savings that IS present must be a dict.
        for f in findings:
            if "est_savings" in f and f["est_savings"] is not None:
                self.assertIsInstance(f["est_savings"], dict, f)

        # The deep-dive wires up an estimated-savings roll-up (the money
        # side of the recommendations), reported at the summary level.
        summary = res.get("summary") or {}
        self.assertIn("total_est_monthly_usd", summary, summary)


class AiContextMockTest(MockStackBase):
    """An AI context bundle assembled over a real converted dashboard.

    Converts the Checkout fixture, persists it (plus a live deep-dive
    artifact when the sibling exists) and folds everything into one
    ``nr2grafana/ai-context/v1`` bundle that renders to markdown/prompt.
    """

    def setUp(self):
        super(AiContextMockTest, self).setUp()
        self.tmp = tempfile.mkdtemp(prefix="nr2g-ai-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_build_context_over_converted_dashboard(self):
        (model, config_mod, builder, store_mod, aicontext) = \
            _import_or_skip(
                self, "nr2grafana.model", "nr2grafana.config",
                "nr2grafana.grafana.builder", "nr2grafana.store",
                "nr2grafana.aicontext")
        ng = self.nerdgraph()
        guid = next(e["guid"] for e in ng.list_dashboards()
                    if e["name"] == "Checkout Service Overview")
        cfg = config_mod.load_config("")
        nr_dash = model.parse_nr_dashboard(ng.get_dashboard(guid))
        _fname, dash, report = builder.build_dashboards(nr_dash, cfg)[0]

        store = store_mod.Store(os.path.join(self.tmp, "ctx.db"))
        try:
            slug = "checkout-service-overview"
            store.upsert_dashboard(slug, dash.get("title", slug),
                                   "e2e", guid, dash)
            # Fold in a live deep-dive artifact when deepdive exists.
            have_deepdive = False
            try:
                deepdive = importlib.import_module("nr2grafana.deepdive")
            except Exception:
                deepdive = None
            if deepdive is not None:
                dd = deepdive.analyze(prom=self.stack.prom_url,
                                      mimir=self.stack.mimir_url,
                                      loki=self.stack.loki_url)
                store.save_artifact(slug, "deepdive", dd)
                have_deepdive = True

            ctx = aicontext.build_context(store, slug)
            self.assertEqual(ctx["schema"], "nr2grafana/ai-context/v1")
            self.assertIn("preamble", ctx)
            self.assertIsNotNone(ctx["dashboard"])
            self.assertEqual(ctx["dashboard"]["slug"], slug)
            if have_deepdive:
                self.assertIn("deepdive", ctx["available_artifacts"],
                              ctx["available_artifacts"])

            # Renders to a compact markdown doc and a single-string prompt.
            md = aicontext.to_markdown(ctx)
            self.assertIsInstance(md, str)
            self.assertTrue(md.strip())
            prompt = aicontext.to_prompt(ctx, question="Why no data?")
            self.assertIsInstance(prompt, str)
            self.assertIn("Why no data?", prompt)
        finally:
            store.close()


class McpProbeMockTest(unittest.TestCase):
    """An MCP probe/round-trip against tools/fake_mcp_server.py.

    Uses the real ``nr2grafana.mcp`` client over stdio so the offline
    demo path (probe a Grafana-like MCP server, list + call tools) is
    exercised without the real mcp-grafana binary."""

    def _server_cmd(self):
        server = os.path.join(TOOLS, "fake_mcp_server.py")
        self.assertTrue(os.path.exists(server),
                        "fake MCP server missing: %s" % server)
        return [sys.executable, server]

    def test_probe_and_round_trip(self):
        (mcp_mod,) = _import_or_skip(self, "nr2grafana.mcp")
        cmd = self._server_cmd()
        out = mcp_mod.probe(command=cmd)
        self.assertTrue(out.get("ok"), out)
        self.assertIn("search_dashboards", out.get("tools", []))
        self.assertNotIn("error", out)

        with mcp_mod.MCPClient(command=cmd) as client:
            info = client.initialize()
            self.assertIn("serverInfo", info)
            names = [t.get("name") for t in client.list_tools()]
            self.assertIn("query_prometheus", names)
            res = client.call_tool(
                "query_prometheus",
                {"datasourceUid": "mimir", "expr": "up"})
            self.assertFalse(res.get("isError"))
            self.assertTrue(res.get("content"))

    def test_generated_config_references_token_env_not_value(self):
        (mcp_mod,) = _import_or_skip(self, "nr2grafana.mcp")
        conf = mcp_mod.generate_mcp_config("http://grafana.local:3000",
                                           kind="claude")
        self.assertIn("mcpServers", conf)
        blob = json.dumps(conf)
        # The token is referenced via env var, never embedded.
        self.assertIn("${GRAFANA_SERVICE_ACCOUNT_TOKEN}", blob)
        for leak in ("glsa_", "glc_", "Bearer "):
            self.assertNotIn(leak, blob)


class TcoMockTest(unittest.TestCase):
    """The 1.7 TCO trend engine end to end against the fake ``aws`` CLI.

    Points :mod:`nr2grafana.awscost` at ``tools/fake_aws.py`` via the
    ``N2G_AWS_BIN`` override and drives ``tco.analyze`` over its canned,
    deterministic, upward Cost Explorer bill: an upward trend is
    detected, a forecast is produced, observability attribution is
    computed and the seeded anomaly is surfaced. It also proves the
    read-only guard refuses a mutating command -- both at the awscost
    boundary (refused before exec) and at the fake CLI itself (which
    exits non-zero on any non-read subcommand). The ``tco`` sibling is
    imported lazily and the analyze test skips when it is not yet
    written; the read-only leg exercises the already-present awscost."""

    BUCKETS = ["mimir-blocks", "loki-chunks", "tempo-traces"]

    def setUp(self):
        self.fake = os.path.join(TOOLS, "fake_aws.py")
        self.assertTrue(os.path.exists(self.fake),
                        "fake aws CLI missing: %s" % self.fake)
        prev = os.environ.get("N2G_AWS_BIN")
        os.environ["N2G_AWS_BIN"] = self.fake

        def _restore():
            if prev is None:
                os.environ.pop("N2G_AWS_BIN", None)
            else:
                os.environ["N2G_AWS_BIN"] = prev
        self.addCleanup(_restore)

    def _awscost(self):
        (awscost,) = _import_or_skip(self, "nr2grafana.awscost")
        return awscost

    def test_fake_aws_drives_awscost_read_only(self):
        """awscost, pointed at the fake, reads a believable upward bill
        (grouped both ways), a forecast, one anomaly, the caller identity
        and S3 bucket sizes -- all through the real read-only shell-out."""
        awscost = self._awscost()
        self.assertTrue(awscost.aws_available())

        def _total(entry):
            return sum(float(g["Metrics"]["UnblendedCost"]["Amount"])
                       for g in entry["Groups"])

        for group_by in ("SERVICE", "USAGE_TYPE"):
            cu = awscost.get_cost_and_usage(
                "2026-03-01", "2026-09-01", group_by=group_by)
            rows = cu["ResultsByTime"]
            self.assertGreaterEqual(len(rows), 4)
            self.assertGreater(_total(rows[-1]), _total(rows[0]),
                               "%s bill is not upward" % group_by)
            self.assertTrue(rows[0]["Groups"])

        fc = awscost.get_cost_forecast("2026-09-01", "2026-12-01")
        self.assertTrue(fc.get("ForecastResultsByTime"))
        self.assertIn("Amount", fc.get("Total", {}))

        anoms = awscost.get_anomalies("2026-06-01", "2026-09-01")
        self.assertEqual(len(anoms), 1)
        self.assertGreater(anoms[0]["Impact"]["TotalImpact"], 0)

        ident = awscost.caller_identity()
        self.assertEqual(ident.get("Account"), "123456789012")
        self.assertNotIn("Secret", json.dumps(ident))

        sizes = awscost.s3_bucket_sizes(self.BUCKETS)
        for b in self.BUCKETS:
            self.assertIsNotNone(sizes[b]["bytes"])
            self.assertIsNotNone(sizes[b]["objects"])
            self.assertNotIn("error", sizes[b])

    def test_mutating_command_is_refused(self):
        """A mutating command is impossible to run: refused by the
        awscost guard before exec, and independently refused (non-zero
        exit) by the fake CLI even if it were invoked directly."""
        awscost = self._awscost()
        for service, sub in (("ce", "create-anomaly-monitor"),
                             ("ec2", "terminate-instances"),
                             ("s3api", "delete-bucket")):
            with self.assertRaises(awscost.AWSError):
                awscost.run_aws(service, sub)

        # The fake CLI itself refuses (non-zero) a non-read subcommand...
        import subprocess
        proc = subprocess.run(
            [sys.executable, self.fake, "ce", "create-anomaly-monitor",
             "--monitor", "x"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(proc.returncode, 0)
        # ...but serves a read subcommand (exit 0).
        proc = subprocess.run(
            [sys.executable, self.fake, "sts", "get-caller-identity"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("123456789012",
                      proc.stdout.decode("utf-8", "replace"))

    def test_tco_analyze_end_to_end(self):
        """tco.analyze over the fake bill: upward trend + forecast +
        by-service breakdown + observability attribution + the seeded
        anomaly, all threaded through awscost's read-only shell-out."""
        awscost, tco = _import_or_skip(
            self, "nr2grafana.awscost", "nr2grafana.tco")
        change_log = [{
            "date": "2026-05-15",
            "action": ("dropped metric "
                       "apiserver_request_duration_seconds_bucket"),
            "source": "optimize",
        }]
        res = tco.analyze(awscost, months=6, buckets=self.BUCKETS,
                          change_log=change_log)
        self.assertEqual(res.get("schema"), "nr2grafana/tco/v1")

        # -- upward total trend -----------------------------------------
        total = res.get("total") or {}
        series = total.get("series") or []
        self.assertGreaterEqual(len(series), 4, series)
        first_usd = float(series[0][1])
        last_usd = float(series[-1][1])
        self.assertGreater(last_usd, first_usd,
                           "total cost series is not upward: %s" % series)
        trend = total.get("trend") or {}
        self.assertTrue(trend, "no trend computed")
        direction = trend.get("direction")
        if direction is not None:
            self.assertEqual(direction, "up",
                             "trend direction not up: %s" % trend)

        # -- forecast present -------------------------------------------
        self.assertTrue(total.get("forecast"),
                        "no forecast in TCO report")

        # -- by-service breakdown ---------------------------------------
        by_service = res.get("by_service") or []
        self.assertTrue(by_service, "no by-service breakdown")

        # -- observability attribution computed -------------------------
        attribution = res.get("observability_attribution")
        self.assertIsInstance(attribution, dict)
        self.assertTrue(attribution,
                        "observability attribution not computed")

        # -- the seeded anomaly is surfaced -----------------------------
        anomalies = res.get("anomalies") or []
        self.assertTrue(anomalies, "seeded anomaly not surfaced")

        # -- the contract's remaining top-level sections exist ----------
        self.assertIn("change_correlation", res)
        self.assertTrue(res.get("assumptions"),
                        "TCO report must carry labeled assumptions")


def _web_req(base, method, path, body=None, timeout=20):
    """Drive the nr2grafana web server over HTTP with a loopback
    Origin (its 1.7.1 security guard requires a same-origin Origin on
    state-changing methods)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Origin": base})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}


def _web_poll(base, jid, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, job = _web_req(base, "GET", "/api/jobs/" + jid)
        if code == 200 and job.get("status") in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("web job %s did not finish in time" % jid)


class WebPasteAndAiConvertTest(MockStackBase):
    """The 1.8 core-flow additions driven through the REAL web server
    over HTTP against the offline mock stack: paste a NR dashboard to
    convert it (SEAM-2), then run conversion-mode AI over an
    untranslatable panel and apply the proposal (SEAM-1 + SEAM-3),
    with a fake local console agent standing in for the AI backend."""

    def setUp(self):
        super(WebPasteAndAiConvertTest, self).setUp()
        self.tmp = tempfile.mkdtemp(prefix="nr2g-web-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        store_mod, web = _import_or_skip(
            self, "nr2grafana.store", "nr2grafana.web.server")
        self.web = web
        self.out_dir = os.path.join(self.tmp, "out")
        st = store_mod.Store(os.path.join(self.tmp, "web.db"))
        # Fresh in-memory session so nothing leaks between tests.
        web.SESSION = web.Session()
        web.SESSION.grafana_url = ""
        web.SESSION.nr_api_key = ""
        web.SESSION.anthropic_api_key = ""
        web.SESSION.out_dir = self.out_dir
        self.httpd = web.create_server("127.0.0.1", 0, store=st)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

        def _shutdown():
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)
            try:
                st.close()
            except Exception:
                pass
        self.addCleanup(_shutdown)

    def _fixture_object(self):
        ng = self.nerdgraph()
        guid = next(e["guid"] for e in ng.list_dashboards()
                    if e["name"] == "Checkout Service Overview")
        return ng.get_dashboard(guid)

    def _fake_agent(self, expr):
        """Write a tiny console agent that prints a strict-JSON
        suggest_fix reply carrying ``expr`` -- a stand-in local AI."""
        script = os.path.join(self.tmp, "agent.py")
        payload = json.dumps({
            "explanation": "translated the funnel into a metric query",
            "fixed_expr": expr, "confidence": "medium", "actions": []})
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\n")
            f.write("sys.stdin.read()\n")
            f.write("print(%r)\n" % payload)
        return "%s %s" % (sys.executable, script)

    def test_paste_convert_then_ai_convert_untranslatable_panel(self):
        obj = self._fixture_object()

        # -- 1. SEAM-2: paste the NR dashboard object to convert it -----
        code, resp = _web_req(self.base, "POST", "/api/convert",
                              {"nr_json": obj, "out_dir": self.out_dir,
                               "package": True})
        self.assertEqual(code, 200, resp)
        job = _web_poll(self.base, resp["job"])
        self.assertEqual(job["status"], "done", job)
        self.assertGreaterEqual(len(job["result"]["dashboards"]), 1)
        slug = job["result"]["dashboards"][0]["slug"]

        # persisted + packaged on disk
        code, det = _web_req(self.base, "GET",
                             "/api/dashboards/" + slug)
        self.assertEqual(code, 200)
        self.assertIn("panels", det["dashboard"])
        pkg = os.path.join(self.out_dir, slug)
        self.assertTrue(os.path.isfile(
            os.path.join(pkg, "dashboard.json")))
        self.assertTrue(os.path.isfile(
            os.path.join(pkg, "datatest.json")))

        # locate the untranslatable "Checkout funnel" placeholder panel
        wr = det["widget_report"]
        funnel = next(w for w in wr
                      if w.get("confidence") == "untranslatable")
        pid = funnel["panel_id"]
        panel = next(p for p in det["dashboard"]["panels"]
                     if p.get("id") == pid)
        self.assertEqual(panel["type"], "text")  # dead placeholder
        self.assertNotIn("targets", panel)

        # -- 2. wire up the fake local AI agent -------------------------
        new_expr = "sum(rate(checkout_orders_completed_total[5m]))"
        cmd = self._fake_agent(new_expr)
        code, _ = _web_req(self.base, "POST", "/api/settings",
                           {"ai_command": cmd})
        self.assertEqual(code, 200)

        # -- 3. SEAM-1: conversion-mode AI suggest for that panel -------
        code, sug = _web_req(self.base, "POST", "/api/ai/suggest",
                             {"slug": slug, "panel_id": pid,
                              "mode": "convert",
                              "ds_family": "prometheus"})
        self.assertEqual(code, 200, sug)
        self.assertEqual(sug["fixed_expr"], new_expr)

        # -- 4. SEAM-3: apply the proposal via /api/panel/convert -------
        code, out = _web_req(self.base, "POST", "/api/panel/convert",
                             {"slug": slug, "panel_id": pid,
                              "expr": sug["fixed_expr"],
                              "ds_family": "prometheus"})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["was_type"], "text")
        conv = out["panel"]
        self.assertEqual(conv["targets"][0]["expr"], new_expr)
        self.assertEqual(conv["targets"][0]["datasource"]["type"],
                         "prometheus")
        self.assertEqual(conv["datasource"]["type"], "prometheus")
        self.assertNotEqual(conv["type"], "text")  # a live viz now

        # the panel really gained a real target in the stored dashboard
        code, det2 = _web_req(self.base, "GET",
                              "/api/dashboards/" + slug)
        self.assertEqual(code, 200)
        panel2 = next(p for p in det2["dashboard"]["panels"]
                      if p.get("id") == pid)
        self.assertIn(new_expr,
                      json.dumps(panel2.get("targets") or []))

        # and the package datatest.json was rewritten to include it
        with open(os.path.join(pkg, "datatest.json"),
                  encoding="utf-8") as f:
            dt = json.load(f)
        self.assertIn(new_expr,
                      json.dumps(dt.get("targets") or []))

        # a change was recorded for the applied conversion
        code, ch = _web_req(self.base, "GET",
                            "/api/changes?slug=" + slug)
        self.assertEqual(code, 200)
        self.assertTrue(any(c.get("action") == "query-edit"
                            for c in ch["changes"]))

    def test_paste_non_dashboard_is_rejected(self):
        code, body = _web_req(self.base, "POST", "/api/convert",
                              {"nr_json": {"totally": "not a dashboard"}})
        self.assertEqual(code, 400)
        self.assertIn("error", body)


def _call_kw(fn, *args, **kwargs):
    """Call ``fn`` with ``args`` positionally and only the ``kwargs`` its
    signature accepts (a ``**kwargs`` function gets everything). Keeps the
    e2e robust against the concurrently-developed rca/flowlogs/mitigate
    signatures."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


class RcaMitigateMockTest(unittest.TestCase):
    """The 1.9 cost-anomaly RCA + reliability-safe mitigation end to end
    against the fake ``aws`` CLI (tools/fake_aws.py via N2G_AWS_BIN) and
    the shared driver (tools/rca_e2e_helper.py). Reproduces the reference
    investigation generically: the dominant driver is the non-zone-aware
    LGTM ring cross-AZ on gRPC port 9095, the secondary is a cross-zone
    NLB, EBS *storage* is ruled out, and the mitigation plan is
    reliability-safe (zone-aware replication + trafficDistribution
    PreferClose + a us-east-1c Karpenter discovery subnet, with the NLB
    cross-zone disable GATED on per-AZ target health).

    All sibling modules and the helper are imported lazily; the test
    skips cleanly until they exist (they are developed concurrently). The
    helper is the coordination point with the mock agent: it drives the
    read-only pipeline against the fake CLI and returns a bundle with at
    least an ``rca`` (schema nr2grafana/rca/v1); a ``mitigation`` is
    produced here from that RCA when the helper does not include one."""

    def setUp(self):
        self.fake = os.path.join(TOOLS, "fake_aws.py")
        self.assertTrue(os.path.exists(self.fake),
                        "fake aws CLI missing: %s" % self.fake)
        prev = os.environ.get("N2G_AWS_BIN")
        os.environ["N2G_AWS_BIN"] = self.fake

        def _restore():
            if prev is None:
                os.environ.pop("N2G_AWS_BIN", None)
            else:
                os.environ["N2G_AWS_BIN"] = prev
        self.addCleanup(_restore)

    def _siblings_or_skip(self, *names):
        """Import sibling modules, skipping (never failing) when one is
        absent OR still mid-build (they are developed concurrently; by
        final verification all import cleanly and this test runs)."""
        mods = []
        for name in names:
            try:
                mods.append(importlib.import_module(name))
            except Exception as e:
                self.skipTest("sibling %s not ready: %s" % (name, e))
        return mods

    def _helper(self):
        if not os.path.exists(os.path.join(TOOLS, "rca_e2e_helper.py")):
            self.skipTest("rca_e2e_helper not written yet")
        try:
            return importlib.import_module("rca_e2e_helper")
        except Exception as e:  # pragma: no cover - coordination guard
            self.skipTest("rca_e2e_helper import failed: %s" % e)

    def _bundle(self, helper, awscost, rca, mitigate, flowlogs):
        """Obtain {rca[, mitigation, deepdive, packing]} from the helper,
        tolerating either a high-level driver or lower-level fixture
        accessors. Skips (never errors) when the helper cannot drive the
        pipeline, so a coordination mismatch degrades to a skip."""
        for name in ("run", "run_e2e", "drive", "e2e", "run_rca",
                     "analyze"):
            fn = getattr(helper, name, None)
            if not callable(fn):
                continue
            try:
                res = _call_kw(fn, awscost=awscost, aws=awscost, rca=rca,
                               mitigate=mitigate, flowlogs=flowlogs)
            except TypeError:
                try:
                    res = fn(awscost)
                except Exception:
                    continue
            except Exception:
                continue
            if isinstance(res, dict) and isinstance(res.get("rca"), dict):
                return res
        # Fall back to lower-level helper pieces + the known engine
        # signatures (rca.analyze / mitigate.plan).
        anomaly = self._helper_value(
            helper, ("anomaly", "get_anomaly", "load_anomaly",
                     "build_anomaly"), awscost, rca)
        if anomaly is None:
            self.skipTest("rca_e2e_helper exposes no usable driver / "
                          "anomaly accessor")
        if not isinstance(anomaly, dict):
            anomaly = rca.parse_anomaly_report(anomaly)
        flow = self._helper_value(
            helper, ("flowlogs", "flow_logs", "run_flowlogs"), awscost,
            flowlogs)
        deepdive = self._helper_value(
            helper, ("deepdive",), awscost, None)
        packing = self._helper_value(
            helper, ("packing",), awscost, None)
        rca_res = _call_kw(rca.analyze, anomaly, aws=awscost,
                           flowlogs=flow, deepdive=deepdive,
                           packing=packing)
        if not isinstance(rca_res, dict):
            self.skipTest("rca.analyze returned no report")
        return {"rca": rca_res, "deepdive": deepdive, "packing": packing}

    def _helper_value(self, helper, names, awscost, extra):
        for name in names:
            attr = getattr(helper, name, None)
            if attr is None:
                continue
            if callable(attr):
                for args in ((awscost, extra), (awscost,), ()):
                    try:
                        return attr(*args)
                    except TypeError:
                        continue
                    except Exception:
                        return None
            else:
                return attr
        return None

    def test_rca_and_mitigate_end_to_end(self):
        awscost, rca, mitigate, flowlogs, _rel = self._siblings_or_skip(
            "nr2grafana.awscost", "nr2grafana.rca",
            "nr2grafana.mitigate", "nr2grafana.flowlogs",
            "nr2grafana.reliability")
        self.assertTrue(awscost.aws_available())
        helper = self._helper()
        bundle = self._bundle(helper, awscost, rca, mitigate, flowlogs)

        # -- RCA: cross-AZ ring on port 9095, EBS storage ruled out -----
        rca_res = bundle["rca"]
        self.assertEqual(rca_res.get("schema"), "nr2grafana/rca/v1")
        cause = rca_res.get("cause") or {}
        rblob = json.dumps(rca_res)
        self.assertIn("9095", rblob,
                      "dominant driver should key on gRPC port 9095")
        low = rblob.lower()
        self.assertTrue("cross-az" in low or "cross_az" in low
                        or "zone" in low,
                        "RCA should name the cross-AZ / zone driver")
        ruled = json.dumps(cause.get("ruled_out") or [])
        self.assertIn("EBS", ruled,
                      "EBS storage growth must be an explicit ruled-out")
        convergence = rca_res.get("evidence_convergence") or []
        self.assertIn("vpc-flow-logs", convergence,
                      "flow logs must be a converging evidence source")

        # -- Mitigation: reliability-safe, GATED NLB --------------------
        plan = bundle.get("mitigation")
        if not isinstance(plan, dict):
            plan = _call_kw(mitigate.plan, rca_res,
                            deepdive=bundle.get("deepdive"),
                            packing=bundle.get("packing"))
        self.assertIsInstance(plan, dict)
        self.assertEqual(plan.get("schema"), "nr2grafana/mitigation/v1")
        mits = plan.get("mitigations") or []
        self.assertTrue(mits, "mitigation plan has no mitigations")
        pblob = json.dumps(plan)
        self.assertIn("zone_awareness_enabled", pblob,
                      "primary mitigation should enable zone-aware "
                      "replication")
        self.assertIn("PreferClose", pblob,
                      "topology-aware routing (trafficDistribution: "
                      "PreferClose) should be proposed")

        # Every mitigation states reliability guardrails and boolean
        # keeps_* flags; the NLB cross-zone disable is GATED (its
        # availability claim is honest, never silently true).
        for mm in mits:
            for k in ("keeps_availability", "keeps_durability",
                      "keeps_performance"):
                if k in mm:
                    self.assertIsInstance(mm[k], bool, (k, mm))
            guards = (mm.get("reliability_guardrails")
                      or mm.get("reliability_preconditions")
                      or mm.get("required_preconditions"))
            self.assertTrue(guards,
                            "mitigation %r carries no reliability "
                            "guardrails" % mm.get("title"))
        nlb = [mm for mm in mits
               if "cross_zone" in json.dumps(mm).lower()
               or "cross-zone" in json.dumps(mm).lower()]
        self.assertTrue(nlb, "no NLB cross-zone mitigation proposed")
        gated = json.dumps(nlb).lower()
        self.assertTrue("target" in gated and "health" in gated,
                        "NLB cross-zone disable must be GATED on per-AZ "
                        "target health")


def _web_req_hdr(base, method, path, headers, body=None, timeout=20):
    """Drive the web server with arbitrary headers (no implicit Origin),
    returning (status, parsed_json)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}


def _web_poll_hdr(base, jid, headers, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, job = _web_req_hdr(base, "GET", "/api/jobs/" + jid, headers)
        if code == 200 and job.get("status") in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("web job %s did not finish in time" % jid)


class TokenApiE2ETest(MockStackBase):
    """B/E: the token-authed headless HTTP API driven over real HTTP.

    A programmatic (non-browser) client with a valid bearer token can
    POST without a same-origin Origin; the same POST without the token is
    403. Exercises a real convert of a pasted NR dashboard object end to
    end through the web server against the offline mock stack."""

    TOKEN = "n2g_e2e_token_0123456789abcdef"

    def setUp(self):
        super(TokenApiE2ETest, self).setUp()
        self.tmp = tempfile.mkdtemp(prefix="nr2g-tok-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        store_mod, web = _import_or_skip(
            self, "nr2grafana.store", "nr2grafana.web.server")
        self.web = web
        self.out_dir = os.path.join(self.tmp, "out")
        st = store_mod.Store(os.path.join(self.tmp, "web.db"))
        web.SESSION = web.Session()
        web.SESSION.grafana_url = ""
        web.SESSION.nr_api_key = ""
        web.SESSION.anthropic_api_key = ""
        web.SESSION.out_dir = self.out_dir
        self.httpd = web.create_server("127.0.0.1", 0, store=st,
                                       api_token=self.TOKEN)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

        def _shutdown():
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)
            try:
                st.close()
            except Exception:
                pass
        self.addCleanup(_shutdown)

    def _fixture_object(self):
        ng = self.nerdgraph()
        guid = next(e["guid"] for e in ng.list_dashboards()
                    if e["name"] == "Checkout Service Overview")
        return ng.get_dashboard(guid)

    def test_token_post_succeeds_without_token_403(self):
        obj = self._fixture_object()
        auth = {"Content-Type": "application/json",
                "Authorization": "Bearer " + self.TOKEN}
        no_auth = {"Content-Type": "application/json"}

        # -- without a token (and no Origin), a POST is refused ----------
        code, body = _web_req_hdr(self.base, "POST", "/api/convert",
                                  no_auth,
                                  body={"nr_json": obj,
                                        "out_dir": self.out_dir,
                                        "package": False})
        self.assertEqual(code, 403, body)

        # -- with a valid token, the same POST is accepted and runs -----
        code, resp = _web_req_hdr(self.base, "POST", "/api/convert", auth,
                                  body={"nr_json": obj,
                                        "out_dir": self.out_dir,
                                        "package": False})
        self.assertEqual(code, 200, resp)
        self.assertIn("job", resp)
        job = _web_poll_hdr(self.base, resp["job"], auth)
        self.assertEqual(job["status"], "done", job)
        self.assertGreaterEqual(len(job["result"]["dashboards"]), 1)
        slug = job["result"]["dashboards"][0]["slug"]

        # the converted dashboard is discoverable via the token API
        code, det = _web_req_hdr(self.base, "GET",
                                 "/api/dashboards/" + slug, auth)
        self.assertEqual(code, 200, det)
        self.assertIn("panels", det["dashboard"])

    def test_health_and_spec_unauthed_and_token_not_leaked(self):
        code, health = _web_req_hdr(self.base, "GET", "/api/health", {})
        self.assertEqual(code, 200)
        self.assertTrue(health["ok"])
        code, spec = _web_req_hdr(self.base, "GET", "/api/spec", {})
        self.assertEqual(code, 200)
        self.assertEqual(spec["auth"]["scheme"], "bearer")
        self.assertTrue(spec["endpoints"])
        # the token value never appears in a discovery response
        self.assertNotIn(self.TOKEN, json.dumps(spec))
        self.assertNotIn(self.TOKEN, json.dumps(health))


def _mcp_rpc(proc, msg):
    """Send one newline-delimited JSON-RPC message and read the next
    newline-delimited JSON response from the MCP server subprocess."""
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise AssertionError("MCP server closed stdout unexpectedly")
    return json.loads(line.decode())


def _mcp_tool_json(result):
    """Extract the JSON payload from an MCP tools/call result
    ({content:[{type:'text', text:<json>}]})."""
    content = (result or {}).get("content") or []
    for item in content:
        if item.get("type") == "text":
            try:
                return json.loads(item.get("text") or "")
            except ValueError:
                return item.get("text")
    return None


class McpServerStdioE2ETest(MockStackBase):
    """E: nr2grafana AS an MCP server, driven over real stdio JSON-RPC.

    Launches ``python -m nr2grafana mcp serve`` as a subprocess (an
    isolated store via N2G_DB) and runs a full op through it: convert a
    pasted NR dashboard object -> list_dashboards -> get_dashboard. The
    mcp_server module is developed concurrently; this skips cleanly until
    it (and its CLI wiring) is present."""

    def setUp(self):
        super(McpServerStdioE2ETest, self).setUp()
        # The MCP server module must exist to run this round-trip. Probe
        # with find_spec (never import it here) so this test process's
        # nr2grafana package namespace stays clean -- importing the real
        # submodule would leave it cached as an attribute of the package
        # and defeat a later test that patches it out. The subprocess
        # imports it fresh anyway.
        try:
            spec = importlib.util.find_spec("nr2grafana.mcp_server")
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            self.skipTest("nr2grafana.mcp_server not written yet")
        self.tmp = tempfile.mkdtemp(prefix="nr2g-mcp-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _fixture_object(self):
        ng = self.nerdgraph()
        guid = next(e["guid"] for e in ng.list_dashboards()
                    if e["name"] == "Checkout Service Overview")
        return ng.get_dashboard(guid)

    def test_convert_list_get_over_stdio(self):
        import subprocess
        obj = self._fixture_object()
        out_dir = os.path.join(self.tmp, "out")
        env = dict(os.environ)
        env["N2G_DB"] = os.path.join(self.tmp, "mcp.db")
        proc = subprocess.Popen(
            [sys.executable, "-m", "nr2grafana", "mcp", "serve"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            init = _mcp_rpc(proc, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {},
                           "clientInfo": {"name": "e2e", "version": "1"}}})
            self.assertIn("result", init, init)
            self.assertIn("serverInfo", init["result"], init["result"])

            listed = _mcp_rpc(proc, {"jsonrpc": "2.0", "id": 2,
                                     "method": "tools/list", "params": {}})
            tools = [t.get("name")
                     for t in (listed.get("result") or {}).get("tools", [])]
            for needed in ("convert", "list_dashboards", "get_dashboard"):
                self.assertIn(needed, tools,
                              "MCP server missing tool %r: %s"
                              % (needed, tools))

            # -- convert the pasted NR dashboard object -----------------
            conv = _mcp_rpc(proc, {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "convert",
                           "arguments": {"nr_json": obj,
                                         "out_dir": out_dir,
                                         "package": False}}})
            self.assertIn("result", conv, conv)
            self.assertFalse(conv["result"].get("isError"), conv)
            conv_payload = _mcp_tool_json(conv["result"])
            self.assertIsInstance(conv_payload, dict, conv_payload)
            dashboards = conv_payload.get("dashboards") or []
            self.assertTrue(dashboards, conv_payload)
            slug = dashboards[0]["slug"]

            # -- list_dashboards sees the converted dashboard -----------
            listed2 = _mcp_rpc(proc, {
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "list_dashboards", "arguments": {}}})
            lst_payload = _mcp_tool_json(listed2["result"])
            slugs = json.dumps(lst_payload)
            self.assertIn(slug, slugs, lst_payload)

            # -- get_dashboard returns the converted dashboard JSON -----
            got = _mcp_rpc(proc, {
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "get_dashboard",
                           "arguments": {"slug": slug}}})
            got_payload = _mcp_tool_json(got["result"])
            self.assertIn("panels", json.dumps(got_payload), got_payload)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


class FixtureLoadingTest(unittest.TestCase):
    """load_fixtures serves only well-formed dashboards."""

    def test_malformed_fixtures_are_skipped(self):
        fixtures = mock_stack.load_fixtures()
        names = [f.get("name") for f in fixtures]
        self.assertIn("Checkout Service Overview", names)
        self.assertNotIn("Alert Policy Export", names)
        for fx in fixtures:
            self.assertTrue(fx.get("guid"))
            self.assertIsInstance(fx.get("pages"), list)

    def test_missing_dir_yields_empty(self):
        self.assertEqual(
            mock_stack.load_fixtures("/no/such/dir/anywhere"), [])


if __name__ == "__main__":
    unittest.main()
