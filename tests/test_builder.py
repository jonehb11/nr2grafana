"""Tests for nr2grafana.grafana.builder.build_dashboards."""

import copy
import json
import os
import re
import unittest

from nr2grafana.config import load_config
from nr2grafana.grafana.builder import SCHEMA_VERSION, build_dashboards
from nr2grafana.grafana.validate import validate_dashboard
from nr2grafana.model import parse_nr_dashboard

FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures", "newrelic", "sample-service-dashboard.json")


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return parse_nr_dashboard(json.load(f))


def iter_panels(dash):
    for p in dash["panels"]:
        yield p
        for child in p.get("panels") or []:
            yield child


def panel_by_title(dash, prefix):
    for p in iter_panels(dash):
        title = p.get("title") or ""
        if title.startswith(prefix) or \
                title.startswith("[MANUAL] " + prefix):
            return p
    raise AssertionError("no panel with title starting %r" % prefix)


def build_one(widgets, cfg=None, name="Builder Test"):
    """Build a single-page dashboard from raw NR widget dicts."""
    data = {"name": name, "pages": [{"name": "Only", "widgets": widgets}]}
    return build_dashboards(parse_nr_dashboard(data), cfg or load_config())[0]


def widget(title, nrql, viz="viz.line", col=1, row=1):
    return {
        "title": title,
        "layout": {"column": col, "row": row, "width": 4, "height": 3},
        "visualization": {"id": viz},
        "rawConfiguration": {"nrqlQueries": [
            {"accountId": 1, "query": nrql}]},
    }


RDS_NRQL = ("SELECT average(aws.rds.CPUUtilization) FROM Metric WHERE "
            "aws.rds.DBClusterIdentifier = concat('acme-cluster-', {{env}}) "
            "FACET aws.rds.DBInstanceIdentifier TIMESERIES")
SQS_SUM_NRQL = ("SELECT sum(aws.sqs.NumberOfMessagesSent) FROM Metric WHERE "
                "aws.sqs.QueueName IN ('acme-queue-a', 'acme-queue-b') "
                "SINCE 1 day ago")
FINANCE_NRQL = ("SELECT sum(unblendedCost) FROM FinanceSample "
                "FACET serviceName TIMESERIES")


class RowsStrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nr = load_fixture()
        cls.outputs = build_dashboards(cls.nr, load_config())

    def test_single_output_with_shell_fields(self):
        self.assertEqual(len(self.outputs), 1)
        fname, dash, report = self.outputs[0]
        self.assertEqual(fname, "checkout-service-overview.json")
        self.assertEqual(dash["schemaVersion"], SCHEMA_VERSION)
        self.assertEqual(dash["schemaVersion"], 39)
        self.assertIsNone(dash["id"])
        self.assertEqual(dash["uid"], "nr-checkout-service-overview")
        self.assertEqual(dash["title"], "Checkout Service Overview")
        self.assertIn("newrelic-migration", dash["tags"])
        # most common SINCE (1 hour ago) becomes the dashboard range
        self.assertEqual(dash["time"], {"from": "now-1h", "to": "now"})

    def test_uid_charset_and_length(self):
        dash = self.outputs[0][1]
        self.assertRegex(dash["uid"], r"^[a-zA-Z0-9\-_]{1,40}$")

    def test_unique_panel_ids_including_row_children(self):
        dash = self.outputs[0][1]
        ids = [p["id"] for p in iter_panels(dash)]
        self.assertEqual(len(ids), len(set(ids)))
        # 18 widgets + 3 page rows
        self.assertEqual(len(ids), 21)

    def test_rows_per_page_and_collapse(self):
        dash = self.outputs[0][1]
        rows = [p for p in dash["panels"] if p["type"] == "row"]
        self.assertEqual([r["title"] for r in rows],
                         ["Golden Signals", "Logs", "Traces & Infra"])
        self.assertFalse(rows[0]["collapsed"])
        self.assertTrue(rows[1]["collapsed"])
        self.assertTrue(rows[2]["collapsed"])
        # first page's panels are flattened at top level; later pages'
        # panels live inside their (collapsed) row
        self.assertEqual(rows[0]["panels"], [])
        self.assertEqual(len(rows[1]["panels"]), 3)
        self.assertEqual(len(rows[2]["panels"]), 5)

    def test_collapsed_row_children_have_ids(self):
        dash = self.outputs[0][1]
        rows = [p for p in dash["panels"] if p["type"] == "row"]
        child_ids = [c["id"] for r in rows for c in r["panels"]]
        self.assertEqual(len(child_ids), 8)
        top_ids = {p["id"] for p in dash["panels"]}
        self.assertFalse(top_ids & set(child_ids))

    def test_datasource_variables_match_target_uids(self):
        dash = self.outputs[0][1]
        ds_vars = {v["name"]: v for v in dash["templating"]["list"]
                   if v["type"] == "datasource"}
        self.assertEqual(set(ds_vars),
                         {"datasource", "loki_datasource",
                          "tempo_datasource"})
        self.assertEqual(ds_vars["datasource"]["query"], "prometheus")
        self.assertEqual(ds_vars["loki_datasource"]["query"], "loki")
        self.assertEqual(ds_vars["tempo_datasource"]["query"], "tempo")
        # every ${var} datasource uid used in a target must have a variable
        for p in iter_panels(dash):
            for t in p.get("targets") or []:
                uid = (t.get("datasource") or {}).get("uid", "")
                m = re.fullmatch(r"\$\{(\w+)\}", uid)
                if m:
                    self.assertIn(m.group(1), ds_vars)

    def test_markdown_widget(self):
        dash = self.outputs[0][1]
        p = panel_by_title(dash, "Notes")
        self.assertEqual(p["type"], "text")
        self.assertTrue(p["transparent"])
        self.assertEqual(p["options"]["mode"], "markdown")
        self.assertIn("Checkout runbook", p["options"]["content"])
        # NR {{env}} placeholder rewritten to Grafana $env
        self.assertIn("$env", p["options"]["content"])
        self.assertNotIn("{{env}}", p["options"]["content"])
        self.assertNotIn("targets", p)

    def test_billboard_thresholds_to_stat_steps(self):
        dash = self.outputs[0][1]
        p = panel_by_title(dash, "Error rate")
        self.assertEqual(p["type"], "stat")
        self.assertEqual(
            p["fieldConfig"]["defaults"]["thresholds"],
            {"mode": "absolute",
             "steps": [{"color": "green", "value": None},
                       {"color": "yellow", "value": 1},
                       {"color": "red", "value": 5}]})
        self.assertEqual(p["fieldConfig"]["defaults"]["color"],
                         {"mode": "thresholds"})

    def test_nr_units_to_grafana_units(self):
        dash = self.outputs[0][1]
        p = panel_by_title(dash, "p95 / p99 Latency")
        # NR "SECONDS" -> Grafana "s"
        self.assertEqual(p["fieldConfig"]["defaults"]["unit"], "s")

    def test_panel_type_mapping(self):
        dash = self.outputs[0][1]
        self.assertEqual(panel_by_title(dash, "Throughput")["type"],
                         "timeseries")
        self.assertEqual(panel_by_title(dash, "Requests by endpoint")["type"],
                         "piechart")
        self.assertEqual(panel_by_title(dash, "Latency by endpoint")["type"],
                         "table")
        self.assertEqual(panel_by_title(dash, "Apdex")["type"], "gauge")
        # histogram() translation hints the panel to a heatmap
        self.assertEqual(panel_by_title(dash, "Duration distribution")["type"],
                         "heatmap")
        self.assertEqual(panel_by_title(dash, "Error logs")["type"], "logs")
        # trace search widget renders as a table of traces
        self.assertEqual(panel_by_title(dash, "Slow error traces")["type"],
                         "table")

    def test_needs_review_title_suffix(self):
        dash = self.outputs[0][1]
        p = panel_by_title(dash, "Apdex")
        self.assertTrue(p["title"].endswith("[REVIEW]"))

    def test_untranslatable_without_passthrough_is_text_panel(self):
        dash = self.outputs[0][1]
        p = panel_by_title(dash, "Checkout funnel")
        self.assertEqual(p["type"], "text")
        # 1.11: "[MANUAL] <title>" prefix (was a " [MANUAL]" suffix)
        self.assertEqual(p["title"], "[MANUAL] Checkout funnel")
        self.assertIn("funnel(", p["options"]["content"])
        self.assertNotIn("targets", p)

    def test_prom_targets_shape(self):
        dash = self.outputs[0][1]
        p = panel_by_title(dash, "Throughput")
        tgt = p["targets"][0]
        self.assertEqual(tgt["refId"], "A")
        self.assertTrue(tgt["range"])
        self.assertFalse(tgt["instant"])
        self.assertEqual(tgt["datasource"],
                         {"type": "prometheus", "uid": "${datasource}"})

    def test_compare_with_produces_second_target(self):
        dash = self.outputs[0][1]
        p = panel_by_title(dash, "Throughput this week vs last week")
        self.assertEqual(len(p["targets"]), 2)
        self.assertEqual([t["refId"] for t in p["targets"]], ["A", "B"])
        self.assertIn("offset 1w", p["targets"][1]["expr"])

    def test_logs_target_maxlines(self):
        dash = self.outputs[0][1]
        p = panel_by_title(dash, "Error logs")
        tgt = p["targets"][0]
        self.assertEqual(tgt["maxLines"], 100)
        self.assertEqual(tgt["datasource"]["type"], "loki")

    def test_tempo_target_limit(self):
        dash = self.outputs[0][1]
        p = panel_by_title(dash, "Slow error traces")
        tgt = p["targets"][0]
        self.assertEqual(tgt["queryType"], "traceql")
        self.assertEqual(tgt["limit"], 50)

    def test_instant_table_targets_use_table_format(self):
        dash = self.outputs[0][1]
        p = panel_by_title(dash, "Latency by endpoint")
        for tgt in p["targets"]:
            self.assertEqual(tgt["format"], "table")

    def test_variable_conversion(self):
        dash = self.outputs[0][1]
        tvars = {v["name"]: v for v in dash["templating"]["list"]}
        # NRQL uniques(appName) variable -> prometheus query variable
        app = tvars["app"]
        self.assertEqual(app["type"], "query")
        self.assertEqual(app["definition"], "label_values(service_name)")
        self.assertEqual(app["query"]["query"], "label_values(service_name)")
        self.assertTrue(app["multi"])
        self.assertTrue(app["includeAll"])
        # ENUM -> custom variable with default selected
        env = tvars["env"]
        self.assertEqual(env["type"], "custom")
        self.assertEqual(env["query"], "Production : prod, Staging : staging")
        self.assertEqual(env["current"],
                         {"selected": True, "text": "Production",
                          "value": "prod"})
        # STRING -> textbox
        self.assertEqual(tvars["filter"]["type"], "textbox")

    def test_report_entries(self):
        report = self.outputs[0][2]
        self.assertEqual(len(report), 18)
        first = report[0]
        for key in ("page", "widget", "visualization", "panel_id",
                    "panel_type", "confidence", "nrql", "queries", "notes"):
            self.assertIn(key, first)
        self.assertEqual(first["page"], "Golden Signals")
        self.assertEqual(first["widget"], "Throughput")
        funnel = [r for r in report if r["widget"] == "Checkout funnel"][0]
        self.assertEqual(funnel["confidence"], "untranslatable")
        self.assertEqual(funnel["fallback"], "text-placeholder")

    def test_output_passes_validator(self):
        self.assertEqual(validate_dashboard(self.outputs[0][1]), [])


class SplitStrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = load_config()
        cfg["page_strategy"] = "split"
        cls.outputs = build_dashboards(load_fixture(), cfg)

    def test_one_dashboard_per_page(self):
        self.assertEqual(
            [o[0] for o in self.outputs],
            ["checkout-service-overview--golden-signals.json",
             "checkout-service-overview--logs.json",
             "checkout-service-overview--traces-infra.json"])

    def test_uids_unique_valid_and_length_capped(self):
        uids = [o[1]["uid"] for o in self.outputs]
        self.assertEqual(len(uids), len(set(uids)))
        for uid in uids:
            self.assertRegex(uid, r"^[a-zA-Z0-9\-_]{1,40}$")

    def test_titles_and_cross_links(self):
        dash = self.outputs[0][1]
        self.assertEqual(dash["title"],
                         "Checkout Service Overview / Golden Signals")
        self.assertIn("nr-checkout-service-overview", dash["tags"])
        self.assertEqual(dash["links"][0]["tags"],
                         ["nr-checkout-service-overview"])

    def test_no_row_panels_in_split_mode(self):
        for _, dash, _ in self.outputs:
            self.assertFalse(any(p["type"] == "row"
                                 for p in dash["panels"]))

    def test_each_output_validates(self):
        for _, dash, _ in self.outputs:
            self.assertEqual(validate_dashboard(dash), [])


class GridPosTests(unittest.TestCase):
    def _single_widget_dash(self, layout):
        data = {
            "name": "Grid Test",
            "pages": [{
                "name": "Only",
                "widgets": [{
                    "title": "W",
                    "layout": layout,
                    "visualization": {"id": "viz.line"},
                    "rawConfiguration": {"nrqlQueries": [{
                        "accountId": 1,
                        "query": "SELECT count(*) FROM Transaction TIMESERIES",
                    }]},
                }],
            }],
        }
        outputs = build_dashboards(parse_nr_dashboard(data), load_config())
        return outputs[0][1]["panels"][0]

    def test_column_row_width_height_scaling(self):
        # x=(col-1)*2, y=(row-1)*3, w=width*2, h=height*3
        p = self._single_widget_dash(
            {"column": 3, "row": 2, "width": 4, "height": 3})
        self.assertEqual(p["gridPos"], {"x": 4, "y": 3, "w": 8, "h": 9})

    def test_full_width(self):
        p = self._single_widget_dash(
            {"column": 1, "row": 1, "width": 12, "height": 3})
        self.assertEqual(p["gridPos"], {"x": 0, "y": 0, "w": 24, "h": 9})

    def test_width_clamped_to_24(self):
        p = self._single_widget_dash(
            {"column": 1, "row": 1, "width": 15, "height": 1})
        self.assertEqual(p["gridPos"]["w"], 24)
        self.assertEqual(p["gridPos"]["h"], 3)

    def test_defaults_when_layout_missing(self):
        p = self._single_widget_dash({})
        self.assertEqual(p["gridPos"], {"x": 0, "y": 0, "w": 8, "h": 9})


class PassthroughFallbackTests(unittest.TestCase):
    def test_passthrough_true_creates_newrelic_panel(self):
        cfg = load_config()
        cfg["passthrough_fallback"] = True
        outputs = build_dashboards(load_fixture(), cfg)
        dash = outputs[0][1]
        p = panel_by_title(dash, "Checkout funnel")
        self.assertEqual(p["type"], "table")
        self.assertTrue(p["title"].endswith("[NRQL PASSTHROUGH]"))
        tgt = p["targets"][0]
        self.assertEqual(tgt["datasource"]["type"],
                         "nrgrafanaplugin-newrelic-datasource")
        self.assertIn("funnel(", tgt["queryText"])
        self.assertTrue(tgt["useGrafanaTime"])
        # the newrelic datasource variable is auto-added
        names = [v["name"] for v in dash["templating"]["list"]
                 if v["type"] == "datasource"]
        self.assertIn("newrelic_datasource", names)
        # report marks the fallback
        report = outputs[0][2]
        funnel = [r for r in report if r["widget"] == "Checkout funnel"][0]
        self.assertEqual(funnel["fallback"], "nrql-passthrough")
        self.assertEqual(validate_dashboard(dash), [])

    def test_passthrough_false_creates_text_placeholder(self):
        cfg = load_config()
        cfg["passthrough_fallback"] = False
        dash = build_dashboards(load_fixture(), cfg)[0][1]
        p = panel_by_title(dash, "Checkout funnel")
        self.assertEqual(p["type"], "text")
        self.assertTrue(p["title"].startswith("[MANUAL] "))
        self.assertIn("Not automatically translatable",
                      p["options"]["content"])
        names = [v["name"] for v in dash["templating"]["list"]
                 if v["type"] == "datasource"]
        self.assertNotIn("newrelic_datasource", names)


def _validate_ignoring_cw_expr(dash):
    """validate_dashboard() predates CloudWatch targets and wants an
    expr/query on every target; builder-mode CloudWatch targets have
    namespace/metricName instead. Ignore only that message class."""
    return [e for e in validate_dashboard(dash)
            if "has no expr/query" not in e]


class CloudWatchBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fname, cls.dash, cls.report = build_one([
            widget("RDS CPU", RDS_NRQL),
            widget("Queue total", SQS_SUM_NRQL, viz="viz.billboard",
                   col=5),
            widget("Throughput", "SELECT count(*) FROM Transaction "
                   "WHERE appName = 'acme-backend' TIMESERIES", col=9),
        ])

    def test_builder_mode_target_shape(self):
        p = panel_by_title(self.dash, "RDS CPU")
        self.assertEqual(p["type"], "timeseries")
        self.assertEqual(len(p["targets"]), 1)
        tgt = p["targets"][0]
        self.assertEqual(tgt["refId"], "A")
        self.assertEqual(tgt["datasource"],
                         {"type": "cloudwatch",
                          "uid": "${cloudwatch_datasource}"})
        self.assertEqual(p["datasource"], tgt["datasource"])
        self.assertEqual(tgt["queryMode"], "Metrics")
        self.assertEqual(tgt["metricQueryType"], 0)
        self.assertEqual(tgt["metricEditorMode"], 0)
        self.assertEqual(tgt["namespace"], "AWS/RDS")
        self.assertEqual(tgt["metricName"], "CPUUtilization")
        self.assertEqual(tgt["statistic"], "Average")
        self.assertEqual(tgt["region"], "default")
        self.assertEqual(tgt["dimensions"],
                         {"DBClusterIdentifier": ["acme-cluster-$env"],
                          "DBInstanceIdentifier": ["*"]})
        self.assertEqual(tgt["dimension_keys"], ["DBInstanceIdentifier"])
        self.assertEqual(tgt["expression"], "")
        self.assertTrue(tgt["matchExact"])
        # FACET legend -> CloudWatch dynamic label
        self.assertEqual(tgt["label"], "${PROP('Dim.DBInstanceIdentifier')}")
        # no Python reprs anywhere in the target (F1)
        blob = json.dumps(tgt)
        for marker in ("Func(", "Lit(", "Attr("):
            self.assertNotIn(marker, blob)
        self.assertEqual(p["fieldConfig"]["defaults"]["unit"], "percent")

    def test_search_expression_target_and_stat_total(self):
        p = panel_by_title(self.dash, "Queue total")
        self.assertEqual(p["type"], "stat")
        tgt = p["targets"][0]
        self.assertEqual(tgt["metricEditorMode"], 1)
        self.assertEqual(tgt["namespace"], "AWS/SQS")
        self.assertEqual(tgt["statistic"], "Sum")
        self.assertEqual(
            tgt["expression"],
            "SUM(SEARCH('{AWS/SQS,QueueName} "
            "MetricName=\"NumberOfMessagesSent\" "
            "(QueueName=\"acme-queue-a\" OR QueueName=\"acme-queue-b\")', "
            "'Sum', 300))")
        # NR sum over the window -> stat panel adds the periods up
        self.assertEqual(p["options"]["reduceOptions"]["calcs"], ["sum"])
        self.assertEqual(p.get("timeFrom"), None)
        self.assertEqual(self.dash["time"], {"from": "now-1d", "to": "now"})

    def test_cloudwatch_datasource_variable_added_alongside_others(self):
        ds_vars = {v["name"]: v for v in self.dash["templating"]["list"]
                   if v["type"] == "datasource"}
        self.assertEqual(set(ds_vars), {"cloudwatch_datasource",
                                        "datasource"})
        self.assertEqual(ds_vars["cloudwatch_datasource"]["query"],
                         "cloudwatch")
        self.assertEqual(ds_vars["cloudwatch_datasource"]["label"],
                         "AWS (CloudWatch)")
        # prometheus panel untouched by the CloudWatch family
        p = panel_by_title(self.dash, "Throughput")
        self.assertEqual(p["targets"][0]["datasource"]["type"],
                         "prometheus")

    def test_concrete_cloudwatch_uid_skips_variable(self):
        cfg = load_config()
        cfg["datasources"]["cloudwatch"]["uid"] = "aws-prod"
        _, dash, report = build_one([widget("RDS CPU", RDS_NRQL)], cfg)
        tgt = dash["panels"][0]["targets"][0]
        self.assertEqual(tgt["datasource"],
                         {"type": "cloudwatch", "uid": "aws-prod"})
        names = [v["name"] for v in dash["templating"]["list"]]
        self.assertNotIn("cloudwatch_datasource", names)
        self.assertIsNone(report[0]["missing_datasource"])

    def test_validates_apart_from_legacy_expr_check(self):
        self.assertEqual(_validate_ignoring_cw_expr(self.dash), [])

    def test_report_fields_for_cloudwatch_widget(self):
        r = [r for r in self.report if r["widget"] == "RDS CPU"][0]
        self.assertEqual(r["confidence"], "approximate")
        self.assertTrue(r["cloudwatch"])
        self.assertFalse(r["manual"])
        self.assertFalse(r["k8s_mapped"])
        self.assertEqual(r["metric_kind"],
                         {"aws.rds.CPUUtilization": "cloudwatch"})
        self.assertEqual(r["missing_datasource"], "cloudwatch")
        self.assertEqual(r["render_vars"], ["env"])
        self.assertIsNone(r["closest_equivalent"])
        q = r["queries"][0]
        self.assertEqual(q["datasource"], "cloudwatch")
        self.assertEqual(q["cw"]["namespace"], "AWS/RDS")
        self.assertEqual(q["cw"]["metricName"], "CPUUtilization")

    def test_report_fields_for_prometheus_widget(self):
        r = [r for r in self.report if r["widget"] == "Throughput"][0]
        self.assertFalse(r["cloudwatch"])
        self.assertEqual(r["metric_kind"], {})
        self.assertEqual(r["missing_datasource"], "prometheus")
        self.assertEqual(r["render_vars"], [])
        self.assertFalse(r["manual"])


class ManualPlaceholderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fname, cls.dash, cls.report = build_one([
            widget("Spend", FINANCE_NRQL),
            widget("Deploys", "SELECT count(*) FROM Deployment "
                   "TIMESERIES", col=5),
            widget("Errors", "SELECT count(*) FROM Log WHERE "
                   "service_name = 'acme-backend' AND "
                   "level = 'ERROR' FACET funnel(x)", col=9),
            {"title": "Service map",
             "layout": {"column": 1, "row": 2, "width": 4, "height": 3},
             "visualization": {"id": "topology.service-map"},
             "rawConfiguration": {}},
        ])

    def test_title_prefix_and_body_sections(self):
        p = panel_by_title(self.dash, "Spend")
        self.assertEqual(p["type"], "text")
        self.assertEqual(p["title"], "[MANUAL] Spend")
        self.assertNotIn("targets", p)
        body = p["options"]["content"]
        self.assertIn("**Why:**", body)
        self.assertIn("FinanceSample", body)
        self.assertIn("**Closest equivalent (datasource: cloudwatch):**",
                      body)
        self.assertIn('"namespace": "AWS/Billing"', body)
        self.assertIn('"metricName": "EstimatedCharges"', body)
        self.assertIn("**Original NRQL:**", body)
        self.assertIn(FINANCE_NRQL, body)

    def test_finance_sample_closest_equivalent_is_cw_target(self):
        r = [r for r in self.report if r["widget"] == "Spend"][0]
        self.assertEqual(r["confidence"], "untranslatable")
        self.assertEqual(r["fallback"], "text-placeholder")
        self.assertTrue(r["manual"])
        ce = r["closest_equivalent"]
        self.assertEqual(ce["datasource"], "cloudwatch")
        self.assertEqual(ce["cw_target"]["namespace"], "AWS/Billing")
        self.assertEqual(ce["cw_target"]["metricName"], "EstimatedCharges")
        self.assertIn("Cost Explorer", ce["note"])
        self.assertEqual(r["missing_datasource"], "cloudwatch")
        self.assertFalse(r["cloudwatch"])

    def test_deployment_points_at_annotations(self):
        r = [r for r in self.report if r["widget"] == "Deploys"][0]
        ce = r["closest_equivalent"]
        self.assertEqual(ce["datasource"], "grafana")
        self.assertIn("annotation", ce["note"])
        body = panel_by_title(self.dash, "Deploys")["options"]["content"]
        self.assertIn("annotation", body)

    def test_service_map_without_queries(self):
        p = panel_by_title(self.dash, "Service map")
        self.assertEqual(p["title"], "[MANUAL] Service map")
        self.assertIn("no NRQL", p["options"]["content"])
        r = [r for r in self.report if r["widget"] == "Service map"][0]
        self.assertEqual(r["closest_equivalent"]["datasource"], "tempo")
        self.assertIn("service graph", r["closest_equivalent"]["note"])

    def test_every_entry_has_seam_report_keys(self):
        for r in self.report:
            for key in ("metric_kind", "missing_datasource",
                        "closest_equivalent", "manual", "render_vars",
                        "k8s_mapped", "cloudwatch"):
                self.assertIn(key, r, key)
        # a translated (non-manual) widget gets closest_equivalent only
        # when it needs review
        r = [r for r in self.report if r["widget"] == "Errors"][0]
        self.assertFalse(r["manual"])
        if r["confidence"] == "needs-review":
            self.assertEqual(r["closest_equivalent"]["datasource"],
                             "loki")
            self.assertTrue(r["closest_equivalent"]["example_query"])

    def test_output_validates(self):
        self.assertEqual(validate_dashboard(self.dash), [])


class ReportFlagTests(unittest.TestCase):
    def test_k8s_mapped_and_render_vars(self):
        cfg = load_config()
        cfg["metric_map"]["k8s.container.cpuRequestedCores"] = {
            "name": "kube_pod_container_resource_requests",
            "type": "gauge"}
        _, dash, report = build_one([widget(
            "K8s CPU",
            "SELECT latest(k8s.container.cpuRequestedCores) FROM Metric "
            "WHERE k8s.clusterName = concat('acme-cluster-', {{env}}) "
            "FACET k8s.deploymentName TIMESERIES")], cfg)
        r = report[0]
        self.assertTrue(r["k8s_mapped"])
        self.assertFalse(r["cloudwatch"])
        self.assertEqual(r["render_vars"], ["env"])
        self.assertEqual(r["metric_kind"],
                         {"k8s.container.cpuRequestedCores": "gauge"})
        expr = dash["panels"][0]["targets"][0]["expr"]
        self.assertIn("kube_pod_container_resource_requests", expr)
        for marker in ("Func(", "Lit(", "Attr("):
            self.assertNotIn(marker, expr)

    def test_needs_review_gets_closest_equivalent(self):
        _, dash, report = build_one([widget(
            "Unknown gauge",
            "SELECT percentile(acme_backend.queue.depth, 95) FROM Metric "
            "WHERE host = 'web.example.com' TIMESERIES")])
        r = report[0]
        self.assertEqual(r["confidence"], "needs-review")
        ce = r["closest_equivalent"]
        self.assertEqual(ce["datasource"], "prometheus")
        self.assertEqual(ce["example_query"],
                         dash["panels"][0]["targets"][0]["expr"])
        self.assertTrue(ce["note"])
        self.assertIn("acme_backend.queue.depth", r["metric_kind"])

    def test_passthrough_entry_is_not_manual(self):
        cfg = load_config()
        cfg["passthrough_fallback"] = True
        _, dash, report = build_one([widget("Spend", FINANCE_NRQL)], cfg)
        r = report[0]
        self.assertEqual(r["fallback"], "nrql-passthrough")
        self.assertFalse(r["manual"])
        self.assertEqual(r["closest_equivalent"]["datasource"],
                         "cloudwatch")
        self.assertTrue(dash["panels"][0]["title"].endswith(
            "[NRQL PASSTHROUGH]"))


if __name__ == "__main__":
    unittest.main()
