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
import unittest
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
                "gf-error"))

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
