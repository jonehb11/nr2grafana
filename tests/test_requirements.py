"""Tests for nr2grafana.requirements.analyze_dashboard / summarize."""

import json
import os
import unittest
from unittest import mock

from nr2grafana.config import load_config
from nr2grafana.grafana.builder import build_dashboards
from nr2grafana.model import parse_nr_dashboard
from nr2grafana.requirements import (
    SCHEMA, add_datasource_template, analyze_dashboard,
    missing_datasources, summarize, _logql_needs, _promql_needs,
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


def make_cw_panel(pid, uid="${cloudwatch_datasource}", **extra):
    target = dict({
        "refId": "A",
        "datasource": {"type": "cloudwatch", "uid": uid},
        "namespace": "AWS/RDS", "metricName": "CPUUtilization",
        "statistic": "Average", "region": "default",
        "dimensions": {"DBInstanceIdentifier": ["*"]},
        "queryMode": "Metrics", "metricEditorMode": 0,
    }, **extra)
    return {"id": pid, "type": "timeseries", "title": "RDS CPU %d" % pid,
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            "targets": [target]}


CW_NRQL = ("SELECT average(`aws.rds.CPUUtilization`) FROM Metric "
           "FACET aws.rds.DBInstanceIdentifier TIMESERIES")


class CloudWatchDetectionTests(unittest.TestCase):
    """SEAM-CW: the cloudwatch family is REQUIRED whenever a CW target
    exists or the widget report says a panel needs CloudWatch."""

    def setUp(self):
        self.cfg = load_config()
        self.cfg.setdefault("datasources", {})["cloudwatch"] = {
            "type": "cloudwatch", "uid": "${cloudwatch_datasource}"}

    def test_cw_target_makes_family_required(self):
        dash = make_dash([
            make_panel(1, "prometheus", "${datasource}", "up"),
            make_cw_panel(2),
        ])
        report = [report_entry(1, "SELECT count(*) FROM Transaction"),
                  report_entry(2, CW_NRQL, confidence="approximate",
                               cloudwatch=True)]
        req = analyze_dashboard(None, dash, report, self.cfg)
        ds = {d["family"]: d for d in req["datasources"]}
        self.assertIn("cloudwatch", ds)
        self.assertTrue(ds["cloudwatch"]["required"])
        self.assertTrue(ds["cloudwatch"]["core"])
        self.assertEqual(ds["cloudwatch"]["plugin_id"], "cloudwatch")
        self.assertEqual(ds["cloudwatch"]["uid_ref"],
                         "${cloudwatch_datasource}")
        self.assertEqual(ds["cloudwatch"]["panel_ids"], [2])
        self.assertIn("CloudWatch", ds["cloudwatch"]["purpose"])
        # core plugin: nothing to install
        self.assertEqual(req["plugins"], [])

    def test_report_flag_alone_makes_family_required(self):
        # [MANUAL] text placeholder: no target, but the report says the
        # closest equivalent is a CloudWatch target.
        dash = make_dash([
            {"id": 7, "type": "text", "title": "RDS CPU [MANUAL]",
             "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
             "options": {"mode": "markdown", "content": "why"}},
        ])
        report = [report_entry(
            7, CW_NRQL, confidence="untranslatable", cloudwatch=True,
            manual=True, missing_datasource="cloudwatch",
            notes=["aws.* metric has no data in Mimir"],
            closest_equivalent={
                "datasource": "cloudwatch",
                "cw_target": {"namespace": "AWS/RDS",
                              "metricName": "CPUUtilization",
                              "statistic": "Average",
                              "dimensions": {
                                  "DBInstanceIdentifier": ["*"]}},
                "note": "add a CloudWatch datasource"})]
        req = analyze_dashboard(None, dash, report, self.cfg)
        ds = {d["family"]: d for d in req["datasources"]}
        self.assertIn("cloudwatch", ds)
        self.assertEqual(ds["cloudwatch"]["panel_ids"], [7])
        self.assertEqual(ds["cloudwatch"]["uid_ref"],
                         "${cloudwatch_datasource}")

    def test_closest_equivalent_cw_without_flag(self):
        dash = make_dash([])
        report = [report_entry(
            3, CW_NRQL, confidence="needs-review",
            closest_equivalent={"datasource": "cloudwatch",
                                "note": "use CloudWatch"})]
        req = analyze_dashboard(None, dash, report, self.cfg)
        self.assertEqual([d["family"] for d in req["datasources"]],
                         ["cloudwatch"])

    def test_no_cloudwatch_without_evidence(self):
        dash = make_dash([make_panel(1, "prometheus", "${datasource}",
                                     "up")])
        report = [report_entry(1, "SELECT count(*) FROM Transaction")]
        req = analyze_dashboard(None, dash, report, self.cfg)
        self.assertEqual([d["family"] for d in req["datasources"]],
                         ["prometheus"])

    def test_cw_data_expectations(self):
        dash = make_dash([make_cw_panel(2)])
        req = analyze_dashboard(None, dash, [], self.cfg)
        exp = [e for e in req["data_expectations"]
               if e["panel_id"] == 2][0]
        self.assertEqual(exp["datasource"], "cloudwatch")
        self.assertEqual(exp["needs"]["namespace"], "AWS/RDS")
        self.assertEqual(exp["needs"]["metricName"], "CPUUtilization")
        self.assertEqual(exp["needs"]["statistic"], "Average")
        self.assertEqual(exp["needs"]["dimensions"],
                         ["DBInstanceIdentifier"])

    def test_cw_search_expression_expectation(self):
        dash = make_dash([make_cw_panel(
            4, expression="SEARCH('{AWS/SQS,QueueName} "
                          "MetricName=\"NumberOfMessagesSent\"', "
                          "'Sum', 300)",
            metricEditorMode=1)])
        req = analyze_dashboard(None, dash, [], self.cfg)
        exp = req["data_expectations"][0]
        self.assertIn("SEARCH(", exp["needs"]["expression"])


class MissingDatasourcesTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.dash = make_dash([
            make_panel(1, "prometheus", "${datasource}", "up"),
            make_panel(2, "loki", "loki-prod", '{job="x"}'),  # bound
            make_cw_panel(3),
        ])
        self.report = [
            report_entry(1, "SELECT count(*) FROM Transaction"),
            report_entry(2, "SELECT count(*) FROM Log"),
            report_entry(3, CW_NRQL, confidence="approximate",
                         cloudwatch=True),
        ]

    def test_unbound_vars_are_missing_without_check(self):
        req = analyze_dashboard(None, self.dash, self.report, self.cfg)
        missing = {m["family"]: m for m in req["missing_datasources"]}
        self.assertEqual(set(missing), {"prometheus", "cloudwatch"})
        self.assertEqual(missing["cloudwatch"]["reason"], "unbound")
        self.assertEqual(missing["cloudwatch"]["uid_ref"],
                         "${cloudwatch_datasource}")
        self.assertEqual(missing["cloudwatch"]["panel_ids"], [3])
        self.assertEqual(missing["cloudwatch"]["plugin_id"], "cloudwatch")
        self.assertIn("add-datasource --type cloudwatch",
                      missing["cloudwatch"]["fix"])

    def test_add_datasource_template_is_exact(self):
        req = analyze_dashboard(None, self.dash, self.report, self.cfg)
        cw = [m for m in req["missing_datasources"]
              if m["family"] == "cloudwatch"][0]
        tpl = cw["add_datasource"]
        self.assertEqual(
            tpl["cli"],
            "nr2grafana grafana add-datasource --type cloudwatch "
            "--name cloudwatch")
        self.assertIn("/api/datasources", tpl["api"])
        payload = tpl["payload"]
        self.assertEqual(payload["type"], "cloudwatch")
        self.assertEqual(payload["access"], "proxy")
        self.assertIn("authType", payload["jsonData"])
        self.assertIn("defaultRegion", payload["jsonData"])
        # secrets are placeholders, never pre-filled
        self.assertEqual(payload["secureJsonData"]["secretKey"],
                         "<secretKey>")
        self.assertIn("authType", tpl["required_fields"])
        self.assertIn("defaultRegion", tpl["required_fields"])
        prom = [m for m in req["missing_datasources"]
                if m["family"] == "prometheus"][0]
        self.assertEqual(prom["add_datasource"]["payload"]["url"],
                         "http://mimir:9009/prometheus")

    def test_check_rows_override_unbound(self):
        # Instance has prometheus (ok) but no cloudwatch (missing) and
        # a loki uid of the wrong type.
        rows = [
            {"item": "datasource:prometheus", "status": "ok",
             "detail": "1 datasource(s)", "fix": ""},
            {"item": "datasource:loki", "status": "wrong-type",
             "detail": "uid 'loki-prod' is a prometheus datasource",
             "fix": "Point the dashboard at a Loki datasource"},
            {"item": "datasource:cloudwatch", "status": "missing",
             "detail": "no datasource of type 'cloudwatch'",
             "fix": "Add a CloudWatch datasource"},
        ]
        req = analyze_dashboard(None, self.dash, self.report, self.cfg,
                                check_rows=rows)
        missing = {m["family"]: m for m in req["missing_datasources"]}
        self.assertEqual(set(missing), {"loki", "cloudwatch"})
        self.assertEqual(missing["cloudwatch"]["reason"], "absent")
        self.assertEqual(missing["cloudwatch"]["fix"],
                         "Add a CloudWatch datasource")
        self.assertEqual(missing["loki"]["reason"], "wrong-type")

    def test_empty_check_rows_means_nothing_missing_when_covered(self):
        rows = [{"item": "datasource:%s" % f, "status": "ok",
                 "detail": "", "fix": ""}
                for f in ("prometheus", "loki", "cloudwatch")]
        req = analyze_dashboard(None, self.dash, self.report, self.cfg,
                                check_rows=rows)
        self.assertEqual(req["missing_datasources"], [])

    def test_import_steps_and_summary_mention_missing(self):
        req = analyze_dashboard(None, self.dash, self.report, self.cfg)
        joined = " ".join(req["import"]["steps"])
        self.assertIn("Add missing datasource(s)", joined)
        self.assertIn("cloudwatch", joined)
        text = summarize(req)
        self.assertIn("missing datasources", text)
        self.assertIn("cloudwatch (unbound)", text)

    def test_missing_datasources_helper_recomputes(self):
        req = analyze_dashboard(None, self.dash, self.report, self.cfg)
        self.assertEqual(missing_datasources(req),
                         req["missing_datasources"])
        rows = [{"item": "datasource:%s" % f, "status": "ok",
                 "detail": "", "fix": ""}
                for f in ("prometheus", "loki", "cloudwatch")]
        self.assertEqual(missing_datasources(req, rows), [])

    def test_panel_missing_datasource_adds_panel_id(self):
        report = self.report + [report_entry(
            9, CW_NRQL, confidence="untranslatable", manual=True,
            missing_datasource="cloudwatch")]
        req = analyze_dashboard(None, self.dash, report, self.cfg)
        cw = [m for m in req["missing_datasources"]
              if m["family"] == "cloudwatch"][0]
        self.assertEqual(cw["panel_ids"], [3, 9])

    def test_json_serializable(self):
        req = analyze_dashboard(None, self.dash, self.report, self.cfg)
        json.dumps(req)


class ManualPanelsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.report = [
            report_entry(1, "SELECT count(*) FROM Transaction"),
            report_entry(
                2, "SELECT sum(cost) FROM FinanceSample",
                confidence="untranslatable", viz="viz.billboard",
                manual=True, notes=["FinanceSample has no LGTM source"],
                closest_equivalent={
                    "datasource": "prometheus",
                    "example_query": "sum(aws_cost_daily_usd)",
                    "note": "AWS Cost Explorer via nr2grafana tco"}),
            report_entry(
                3, CW_NRQL, confidence="needs-review", manual=True,
                missing_datasource="cloudwatch", cloudwatch=True,
                notes=["aws.* metric: no data in Mimir"],
                closest_equivalent={
                    "datasource": "cloudwatch",
                    "cw_target": {"namespace": "AWS/RDS",
                                  "metricName": "CPUUtilization",
                                  "statistic": "Average",
                                  "dimensions": {
                                      "DBInstanceIdentifier": ["*"]}},
                    "note": "CloudWatch target"}),
        ]
        self.req = analyze_dashboard(None, make_dash([]), self.report,
                                     self.cfg)

    def test_manual_panels_listed_with_why_and_equivalent(self):
        manual = {m["panel_id"]: m for m in self.req["manual_panels"]}
        self.assertEqual(set(manual), {2, 3})
        m2 = manual[2]
        self.assertEqual(m2["title"], "W2")
        self.assertEqual(m2["why"], "FinanceSample has no LGTM source")
        self.assertEqual(m2["closest_equivalent"]["example_query"],
                         "sum(aws_cost_daily_usd)")
        self.assertIn("sum(aws_cost_daily_usd)", m2["equivalent"])
        self.assertIn("AWS Cost Explorer", m2["equivalent"])
        self.assertIsNone(m2["missing_datasource"])
        self.assertIn("FinanceSample", m2["nrql"])
        m3 = manual[3]
        self.assertEqual(m3["missing_datasource"], "cloudwatch")
        self.assertIn("AWS/RDS CPUUtilization", m3["equivalent"])
        self.assertIn("DBInstanceIdentifier", m3["equivalent"])

    def test_nr_native_carries_closest_equivalent(self):
        native = {n["panel_id"]: n for n in self.req["nr_native"]}
        self.assertEqual(set(native), {2})
        self.assertEqual(native[2]["closest_equivalent"]["datasource"],
                         "prometheus")
        self.assertIn("sum(aws_cost_daily_usd)", native[2]["equivalent"])

    def test_summary_counts_extra_manual(self):
        text = summarize(self.req)
        self.assertIn("1 NR-native panel", text)
        self.assertIn("1 other [MANUAL] panel", text)


class AddDatasourceTemplateTests(unittest.TestCase):
    def test_unknown_family_still_yields_payload(self):
        tpl = add_datasource_template("stackdriver")
        self.assertEqual(tpl["payload"]["type"], "stackdriver")
        self.assertIn("add-datasource --type stackdriver", tpl["cli"])

    def test_fallback_when_live_templates_unavailable(self):
        import nr2grafana.requirements as reqmod
        with mock.patch.object(reqmod, "_ds_template_spec",
                               lambda pid: (reqmod._FALLBACK_FIELDS.get(
                                   pid, []), "")):
            tpl = reqmod.add_datasource_template("loki")
        self.assertEqual(tpl["payload"]["url"], "http://loki:3100")
        self.assertEqual(tpl["required_fields"], ["url"])


class SummarizeEdgeTests(unittest.TestCase):
    def test_empty_requirements(self):
        self.assertEqual(summarize({}),
                         "no datasource requirements detected")


if __name__ == "__main__":
    unittest.main()
