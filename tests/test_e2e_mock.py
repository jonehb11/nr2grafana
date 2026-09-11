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
