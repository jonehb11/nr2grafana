"""Tests for nr2grafana.requirements.analyze_dashboard / summarize."""

import json
import os
import unittest

from nr2grafana.config import load_config
from nr2grafana.grafana.builder import build_dashboards
from nr2grafana.model import parse_nr_dashboard
from nr2grafana.requirements import (
    SCHEMA, analyze_dashboard, summarize, _logql_needs, _promql_needs,
)

FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures", "newrelic", "sample-service-dashboard.json")


def make_panel(pid, ds_type, uid, expr, **extra):
    target = dict({"refId": "A",
                   "datasource": {"type": ds_type, "uid": uid}}, **extra)
    if expr is not None:
        target["expr"] = expr
    return {"id": pid, "type": "timeseries", "title": "P%d" % pid,
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            "targets": [target]}


def make_dash(panels, title="Test Dashboard", uid="nr-test"):
    return {"id": None, "uid": uid, "title": title,
            "schemaVersion": 39, "panels": panels,
            "templating": {"list": []}}


def report_entry(pid, nrql, confidence="exact", viz="viz.line", **extra):
    return dict({"page": "Main", "widget": "W%d" % pid,
                 "visualization": viz, "panel_id": pid,
                 "panel_type": "timeseries", "confidence": confidence,
                 "nrql": [nrql], "queries": [], "notes": []}, **extra)


class BasicShapeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        prom_expr = ('sum by (service_name) (rate('
                     'http_server_request_duration_seconds_count'
                     '{service_name="checkout"}[$__rate_interval]))')
        loki_expr = ('sum(count_over_time({service_name="checkout", '
                     'namespace="prod"} | json | level="error" [5m]))')
        self.dash = make_dash([
            make_panel(1, "prometheus", "${datasource}", prom_expr),
            make_panel(2, "loki", "${loki_datasource}", loki_expr),
        ])
        self.report = [
            report_entry(1, "SELECT count(*) FROM Transaction "
                            "WHERE appName='checkout' TIMESERIES"),
            report_entry(2, "SELECT count(*) FROM Log WHERE "
                            "level='error' TIMESERIES"),
        ]
        self.req = analyze_dashboard(None, self.dash, self.report,
                                     self.cfg)

    def test_schema_and_header(self):
        self.assertEqual(self.req["schema"], SCHEMA)
        self.assertEqual(self.req["schema"],
                         "nr2grafana/requirements/v1")
        self.assertEqual(self.req["dashboard"], "Test Dashboard")
        self.assertEqual(self.req["uid"], "nr-test")
        self.assertTrue(self.req["generated_by"].startswith("nr2grafana"))

    def test_datasources(self):
        ds = {d["family"]: d for d in self.req["datasources"]}
        self.assertEqual(set(ds), {"prometheus", "loki"})
        self.assertEqual(ds["prometheus"]["plugin_id"], "prometheus")
        self.assertTrue(ds["prometheus"]["core"])
        self.assertTrue(ds["prometheus"]["required"])
        self.assertEqual(ds["prometheus"]["uid_ref"], "${datasource}")
        self.assertEqual(ds["prometheus"]["panel_ids"], [1])
        self.assertEqual(ds["loki"]["panel_ids"], [2])
        self.assertEqual(ds["loki"]["uid_ref"], "${loki_datasource}")

    def test_no_plugins_for_core_datasources(self):
        self.assertEqual(self.req["plugins"], [])

    def test_domains(self):
        doms = {d["domain"]: d for d in self.req["domains"]}
        self.assertIn("apm", doms)
        self.assertIn("logs", doms)
        self.assertIn("FROM Transaction", doms["apm"]["evidence"])
        self.assertEqual(doms["apm"]["panel_ids"], [1])
        self.assertEqual(doms["logs"]["panel_ids"], [2])
        kinds = [o["kind"] for o in doms["logs"]["options"]]
        self.assertIn("datasource", kinds)
        self.assertIn("pipeline", kinds)
        loki_opt = [o for o in doms["logs"]["options"]
                    if o["kind"] == "datasource"][0]
        self.assertEqual(loki_opt["plugin_id"], "loki")

    def test_data_expectations_prometheus(self):
        exp = [e for e in self.req["data_expectations"]
               if e["panel_id"] == 1][0]
        self.assertEqual(exp["datasource"], "prometheus")
        self.assertEqual(
            exp["needs"]["metrics"],
            ["http_server_request_duration_seconds_count"])
        self.assertIn("service_name", exp["needs"]["labels"])

    def test_data_expectations_loki(self):
        exp = [e for e in self.req["data_expectations"]
               if e["panel_id"] == 2][0]
        self.assertEqual(exp["datasource"], "loki")
        self.assertEqual(exp["needs"]["stream_selector"],
                         '{service_name="checkout", namespace="prod"}')
        self.assertEqual(exp["needs"]["labels"],
                         ["namespace", "service_name"])

    def test_import_section(self):
        imp = self.req["import"]
        self.assertTrue(imp["steps"])
        joined = " ".join(imp["steps"])
        self.assertIn("prometheus", joined)
        self.assertIn("dashboard.json", joined)
        self.assertIn("GRAFANA_TOKEN", imp["api_example"])
        self.assertIn("/api/dashboards/db", imp["api_example"])

    def test_no_nr_native(self):
        self.assertEqual(self.req["nr_native"], [])

    def test_summarize(self):
        text = summarize(self.req)
        self.assertIn("2 datasources", text)
        self.assertIn("prometheus", text)
        self.assertIn("apm", text)


class AwsLambdaTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        dash = make_dash([
            make_panel(1, "prometheus", "${datasource}",
                       "avg(aws_lambda_duration_average)"),
        ])
        report = [
            report_entry(1, "SELECT average(`aws.lambda.Duration`) "
                            "FROM Metric WHERE aws.accountId = '1' "
                            "TIMESERIES"),
            report_entry(2, "SELECT count(*) FROM AwsLambdaInvocation "
                            "FACET provider.functionName TIMESERIES"),
        ]
        self.req = analyze_dashboard(None, dash, report, self.cfg)

    def test_lambda_domain_detected_with_evidence(self):
        doms = {d["domain"]: d for d in self.req["domains"]}
        self.assertIn("aws-lambda", doms)
        ev = doms["aws-lambda"]["evidence"]
        self.assertIn("FROM AwsLambdaInvocation", ev)
        self.assertIn("metric aws.lambda.Duration", ev)
        self.assertEqual(sorted(doms["aws-lambda"]["panel_ids"]), [1, 2])

    def test_lambda_options_cover_datasource_and_pipeline(self):
        doms = {d["domain"]: d for d in self.req["domains"]}
        opts = doms["aws-lambda"]["options"]
        ds_opts = [o for o in opts if o["kind"] == "datasource"]
        self.assertEqual(ds_opts[0]["plugin_id"], "cloudwatch")
        self.assertTrue(ds_opts[0]["core"])
        pipeline_notes = " ".join(o["note"] for o in opts
                                  if o["kind"] == "pipeline")
        self.assertIn("Mimir", pipeline_notes)
        self.assertIn("aws_lambda_", pipeline_notes)
        self.assertIn("lambda-promtail", pipeline_notes)

    def test_lambda_beats_generic_aws_for_lambda_evidence(self):
        doms = {d["domain"]: d for d in self.req["domains"]}
        # generic aws still fires for aws.accountId / provider.*
        self.assertIn("aws", doms)
        self.assertNotIn("FROM AwsLambdaInvocation",
                         doms["aws"]["evidence"])
        self.assertNotIn("metric aws.lambda.Duration",
                         doms["aws"]["evidence"])


class NrNativeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        dash = make_dash([
            {"id": 5, "type": "table", "title": "Usage [NRQL PASSTHROUGH]",
             "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
             "targets": [{
                 "refId": "A",
                 "datasource": {
                     "type": "nrgrafanaplugin-newrelic-datasource",
                     "uid": "${newrelic_datasource}"},
                 "queryText": "SELECT sum(consumption) FROM NrConsumption",
                 "useGrafanaTime": True}]},
        ])
        report = [report_entry(
            5, "SELECT sum(consumption) FROM NrConsumption",
            confidence="untranslatable", viz="viz.billboard",
            fallback="nrql-passthrough",
            notes=["NR consumption data has no LGTM equivalent"])]
        self.req = analyze_dashboard(None, dash, report, self.cfg)

    def test_plugin_required_for_passthrough(self):
        plugins = {p["id"]: p for p in self.req["plugins"]}
        self.assertIn("nrgrafanaplugin-newrelic-datasource", plugins)
        p = plugins["nrgrafanaplugin-newrelic-datasource"]
        self.assertEqual(
            p["grafana_cli"],
            "grafana-cli plugins install "
            "nrgrafanaplugin-newrelic-datasource")
        ds = {d["family"]: d for d in self.req["datasources"]}
        self.assertFalse(ds["newrelic"]["core"])

    def test_nr_native_entry(self):
        self.assertEqual(len(self.req["nr_native"]), 1)
        native = self.req["nr_native"][0]
        self.assertEqual(native["panel_id"], 5)
        self.assertEqual(native["widget"], "viz.billboard")
        self.assertIn("no LGTM equivalent", native["why"])
        self.assertIn("stat panel", native["equivalent"])
        self.assertIn("passthrough", native["equivalent"])

    def test_nr_account_domain(self):
        doms = {d["domain"]: d for d in self.req["domains"]}
        self.assertIn("nr-account", doms)
        opt = doms["nr-account"]["options"][0]
        self.assertEqual(opt["plugin_id"],
                         "nrgrafanaplugin-newrelic-datasource")
        self.assertFalse(opt["core"])

    def test_summarize_mentions_native_and_plugin(self):
        text = summarize(self.req)
        self.assertIn("nrgrafanaplugin-newrelic-datasource", text)
        self.assertIn("1 NR-native panel", text)

    def test_import_steps_mention_plugin_install(self):
        joined = " ".join(self.req["import"]["steps"])
        self.assertIn("grafana-cli plugins install", joined)


class DomainMatchingTests(unittest.TestCase):
    def _domains(self, nrql, cfg=None):
        dash = make_dash([])
        report = [report_entry(1, nrql)]
        req = analyze_dashboard(None, dash, report,
                                cfg or load_config())
        return {d["domain"]: d for d in req["domains"]}

    def test_infra_samples(self):
        doms = self._domains("SELECT average(cpuPercent) FROM "
                             "SystemSample FACET hostname TIMESERIES")
        self.assertIn("infra-host", doms)
        notes = " ".join(o["note"] for o in
                         doms["infra-host"]["options"])
        self.assertIn("node_exporter", notes)

    def test_k8s(self):
        doms = self._domains("SELECT latest(podsRunning) FROM "
                             "K8sPodSample FACET clusterName")
        self.assertIn("k8s", doms)
        self.assertNotIn("infra-host", doms)

    def test_gcp_and_azure(self):
        doms = self._domains("SELECT average(`gcp.run.container."
                             "cpu.utilizations`) FROM Metric")
        self.assertIn("gcp", doms)
        opt = [o for o in doms["gcp"]["options"]
               if o["kind"] == "datasource"][0]
        self.assertEqual(opt["plugin_id"], "stackdriver")
        doms = self._domains("SELECT average(`azure.vm.percentageCpu`) "
                             "FROM Metric")
        self.assertIn("azure", doms)
        opt = [o for o in doms["azure"]["options"]
               if o["kind"] == "datasource"][0]
        self.assertEqual(opt["plugin_id"],
                         "grafana-azure-monitor-datasource")

    def test_traces_synthetics_browser_mobile(self):
        self.assertIn("traces", self._domains(
            "SELECT count(*) FROM Span WHERE service.name='x'"))
        self.assertIn("synthetics", self._domains(
            "SELECT percentage(count(*), WHERE result='SUCCESS') "
            "FROM SyntheticCheck"))
        self.assertIn("browser-rum", self._domains(
            "SELECT count(*) FROM PageView FACET appName"))
        self.assertIn("mobile", self._domains(
            "SELECT count(*) FROM MobileCrash"))

    def test_custom_domain_map_wins(self):
        cfg = load_config()
        cfg["domain_map"] = [
            {"domain": "acme-custom",
             "event_types": ["Transaction"],
             "options": [{"kind": "pipeline",
                          "note": "acme internal exporter"}]},
        ]
        doms = self._domains(
            "SELECT count(*) FROM Transaction TIMESERIES", cfg)
        self.assertIn("acme-custom", doms)
        self.assertNotIn("apm", doms)

    def test_unparseable_nrql_falls_back_to_regex(self):
        doms = self._domains("SELECT !!bogus!! FROM AwsLambdaInvocation "
                             "WHERE ((")
        self.assertIn("aws-lambda", doms)


class ExpectationExtractionTests(unittest.TestCase):
    def test_promql_histogram_quantile(self):
        metrics, labels = _promql_needs(
            'histogram_quantile(0.95, sum by (le, service_name) '
            '(rate(http_server_request_duration_seconds_bucket'
            '{service_name=~"${env:regex}", http_route!=""}'
            '[$__rate_interval])))')
        self.assertEqual(
            metrics, ["http_server_request_duration_seconds_bucket"])
        self.assertEqual(labels, ["http_route", "le", "service_name"])

    def test_promql_bare_metric_and_arithmetic(self):
        metrics, labels = _promql_needs(
            "sum(rate(foo_total[5m])) / sum(rate(bar_total[5m])) * 100")
        self.assertEqual(metrics, ["foo_total", "bar_total"])
        self.assertEqual(labels, [])

    def test_promql_ignores_functions_and_keywords(self):
        metrics, _ = _promql_needs(
            'clamp_max(avg without (instance) '
            '(node_load1 offset 5m), 100) and on (job) up')
        self.assertEqual(metrics, ["node_load1", "up"])

    def test_logql_selector_and_group(self):
        selector, labels = _logql_needs(
            'sum by (level) (count_over_time({service_name="checkout"} '
            '| json | level=~"error|warn" [5m]))')
        self.assertEqual(selector, '{service_name="checkout"}')
        self.assertEqual(labels, ["level", "service_name"])


class EndToEndFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as f:
            cls.nr = parse_nr_dashboard(json.load(f))
        cls.cfg = load_config()
        fname, dash, report = build_dashboards(cls.nr, cls.cfg)[0]
        cls.req = analyze_dashboard(cls.nr, dash, report, cls.cfg)

    def test_schema_and_families(self):
        self.assertEqual(self.req["schema"], SCHEMA)
        families = {d["family"] for d in self.req["datasources"]}
        self.assertIn("prometheus", families)
        self.assertIn("loki", families)

    def test_every_datasource_panel_exists(self):
        for ds in self.req["datasources"]:
            self.assertTrue(ds["panel_ids"])
            self.assertTrue(ds["uid_ref"].startswith("${"))

    def test_expectations_reference_real_panels(self):
        self.assertTrue(self.req["data_expectations"])
        for exp in self.req["data_expectations"]:
            self.assertIsInstance(exp["panel_id"], int)
            self.assertIn(exp["datasource"],
                          ("prometheus", "loki", "tempo"))

    def test_json_serializable(self):
        json.dumps(self.req)

    def test_summary_is_short_single_line(self):
        text = summarize(self.req)
        self.assertNotIn("\n", text)
        self.assertLess(len(text), 400)


class SummarizeEdgeTests(unittest.TestCase):
    def test_empty_requirements(self):
        self.assertEqual(summarize({}),
                         "no datasource requirements detected")


if __name__ == "__main__":
    unittest.main()
