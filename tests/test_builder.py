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
        if (p.get("title") or "").startswith(prefix):
            return p
    raise AssertionError("no panel with title starting %r" % prefix)


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
        self.assertTrue(p["title"].endswith("[MANUAL]"))
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
        # ...scoped to the metric family FROM Transaction maps to.
        self.assertEqual(
            app["definition"],
            "label_values(http_server_request_duration_seconds_count, "
            "service_name)")
        self.assertEqual(app["query"]["query"], app["definition"])
        self.assertEqual(app["datasource"]["type"], "prometheus")
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
        self.assertIn("Not automatically translatable",
                      p["options"]["content"])
        names = [v["name"] for v in dash["templating"]["list"]
                 if v["type"] == "datasource"]
        self.assertNotIn("newrelic_datasource", names)


if __name__ == "__main__":
    unittest.main()


class BuilderAdditionsTests(unittest.TestCase):
    def _convert(self, widgets, variables=None, guid=""):
        from nr2grafana.config import load_config
        from nr2grafana.grafana.builder import build_dashboards
        from nr2grafana.model import parse_nr_dashboard
        data = {"name": "T", "guid": guid, "pages": [{"name": "P",
                                                     "widgets": widgets}],
                "variables": variables or []}
        return build_dashboards(parse_nr_dashboard(data), load_config(),
                                source_file="t.json")

    def _widget(self, viz, nrql, title="w"):
        return {"title": title, "visualization": {"id": viz},
                "layout": {"column": 1, "row": 1, "width": 4, "height": 3},
                "rawConfiguration": {"nrqlQueries": [
                    {"accountId": 1, "query": nrql}]}}

    def test_timeshift_hint_sets_panel_override(self):
        fn, dash, rep = self._convert([self._widget(
            "viz.line", "SELECT count(*) FROM Transaction SINCE 1 day ago "
            "UNTIL 1 hour ago TIMESERIES")])[0]
        p = dash["panels"][0]
        self.assertEqual(p["timeShift"], "1h")
        self.assertEqual(dash["time"]["from"], "now-23h")

    def test_provenance_and_source_link(self):
        fn, dash, rep = self._convert(
            [self._widget("viz.line", "SELECT count(*) FROM Transaction")],
            guid="ABC123")[0]
        self.assertEqual(dash["nr2grafana"]["source"]["name"], "T")
        self.assertEqual(dash["nr2grafana"]["source"]["file"], "t.json")
        self.assertIn("(guid ABC123)", dash["description"])
        self.assertEqual(dash["links"][0]["url"],
                         "https://one.newrelic.com/redirect/entity/ABC123")

    def test_service_map_reported_with_reason_and_equivalent(self):
        fn, dash, rep = self._convert([{
            "title": "map", "visualization": {"id": "topology.service-map"},
            "layout": {"column": 1, "row": 1, "width": 4, "height": 3},
            "rawConfiguration": {}}])[0]
        entry = rep[0]
        self.assertEqual(entry["confidence"], "untranslatable")
        self.assertIn("service maps", entry["reason"])
        self.assertIn("node graph", entry["equivalent"])
        self.assertEqual(dash["panels"][0]["type"], "text")

    def test_new_viz_ids(self):
        outs = self._convert([
            self._widget("viz.scatter", "SELECT average(duration) FROM "
                                        "Transaction TIMESERIES", "s"),
            self._widget("viz.sparkline", "SELECT count(*) FROM Transaction",
                         "sp"),
            self._widget("viz.traffic-light", "SELECT count(*) FROM "
                                              "Transaction", "tl")])
        dash = outs[0][1]
        by = {p["title"].split(" [")[0]: p for p in dash["panels"]}
        self.assertEqual(by["s"]["type"], "timeseries")
        self.assertEqual(by["s"]["fieldConfig"]["defaults"]["custom"]
                         ["drawStyle"], "points")
        self.assertEqual(by["sp"]["type"], "stat")
        self.assertEqual(by["sp"]["options"]["graphMode"], "area")
        self.assertEqual(by["tl"]["options"]["colorMode"], "background")

    def test_traceql_metrics_target(self):
        fn, dash, rep = self._convert([self._widget(
            "viz.billboard", "SELECT uniqueCount(trace.id) FROM Span WHERE "
                             "service.name = 'c'")])[0]
        p = dash["panels"][0]
        tgt = p["targets"][0]
        self.assertEqual(tgt["datasource"]["type"], "tempo")
        self.assertEqual(tgt["metricsQueryType"], "range")
        self.assertTrue(tgt["query"].endswith("| count_over_time()"))
        self.assertEqual(p["type"], "stat")

    def test_report_entry_has_panel_title(self):
        fn, dash, rep = self._convert([self._widget(
            "viz.line", "SELECT count(*) FROM Transaction FACET name "
                        "TIMESERIES", "byname")])[0]
        self.assertEqual(rep[0]["panel_title"], "byname [REVIEW]")


class Iteration2BuilderTests(unittest.TestCase):
    """Widget/variable-level fidelity: implied TIMESERIES, heatmap layouts,
    time overrides, scoped variables."""

    def _build(self, widgets, variables=None, cfg_updates=None):
        from nr2grafana.config import load_config
        from nr2grafana.grafana.builder import build_dashboards
        from nr2grafana.model import parse_nr_dashboard
        cfg = load_config()
        cfg["label_map"].update(cfg_updates or {})
        data = {"name": "Q", "pages": [{"name": "P", "widgets": widgets}],
                "variables": variables or []}
        return build_dashboards(parse_nr_dashboard(data), cfg)[0][1]

    def _widget(self, title, viz, nrql, extra=None, col=1):
        rc = {"nrqlQueries": [{"accountId": 1, "query": nrql}]}
        rc.update(extra or {})
        return {"title": title, "visualization": {"id": viz},
                "layout": {"column": col, "row": 1, "width": 4, "height": 3},
                "rawConfiguration": rc}

    def _panel(self, dash, title):
        return panel_by_title(dash, title)

    def test_line_widget_without_timeseries_gets_a_range_query(self):
        dash = self._build([self._widget(
            "Line", "viz.line",
            "SELECT count(*) FROM Transaction WHERE appName = 'c' "
            "SINCE 1 hour ago")])
        tgt = self._panel(dash, "Line")["targets"][0]
        self.assertTrue(tgt["range"])
        self.assertFalse(tgt["instant"])
        self.assertIn("$__rate_interval", tgt["expr"])
        self.assertIn("no TIMESERIES clause",
                      self._panel(dash, "Line")["description"])

    def test_billboard_with_timeseries_shows_a_sparkline(self):
        dash = self._build([self._widget(
            "Stat", "viz.billboard",
            "SELECT count(*) FROM Transaction WHERE appName = 'c' TIMESERIES")])
        self.assertEqual(self._panel(dash, "Stat")["options"]["graphMode"],
                         "area")

    def test_billboard_without_timeseries_has_no_sparkline(self):
        dash = self._build([self._widget(
            "Stat", "viz.billboard",
            "SELECT count(*) FROM Transaction WHERE appName = 'c'")])
        self.assertEqual(self._panel(dash, "Stat")["options"]["graphMode"],
                         "none")

    def test_facet_heatmap_keeps_series_as_rows(self):
        dash = self._build([self._widget(
            "Facet heat", "viz.heatmap",
            "SELECT count(*) FROM Transaction WHERE appName = 'c' "
            "FACET name TIMESERIES")])
        p = self._panel(dash, "Facet heat")
        tgt = p["targets"][0]
        self.assertEqual(p["type"], "heatmap")
        self.assertEqual(tgt["format"], "time_series")
        self.assertEqual(tgt["legendFormat"], "{{http_route}}")
        self.assertFalse(p["options"]["calculate"])

    def test_histogram_heatmap_uses_le_buckets(self):
        dash = self._build([self._widget(
            "Hist heat", "viz.heatmap",
            "SELECT histogram(duration, 10, 20) FROM Transaction "
            "WHERE appName = 'c'")])
        tgt = self._panel(dash, "Hist heat")["targets"][0]
        self.assertEqual(tgt["format"], "heatmap")
        self.assertEqual(tgt["legendFormat"], "{{le}}")

    def test_y_axis_zero_and_other_series_note(self):
        dash = self._build([self._widget(
            "Zero", "viz.line",
            "SELECT count(*) FROM Transaction WHERE appName = 'c' "
            "FACET name LIMIT 5 TIMESERIES",
            {"yAxisLeft": {"zero": True},
             "facet": {"showOtherSeries": True}})])
        p = self._panel(dash, "Zero")
        self.assertEqual(p["fieldConfig"]["defaults"]["min"], 0)
        self.assertIn("'Other' series", p["description"])

    def test_billboard_comparison_note(self):
        dash = self._build([self._widget(
            "Cmp", "viz.billboard-comparison",
            "SELECT count(*) FROM Transaction WHERE appName = 'c' "
            "COMPARE WITH 1 day ago")])
        p = self._panel(dash, "Cmp")
        self.assertTrue(p["options"]["showPercentChange"])
        self.assertIn("COMPARE WITH target", p["description"])

    def test_calendar_relative_ranges_become_time_overrides(self):
        dash = self._build([
            self._widget("A", "viz.line",
                         "SELECT count(*) FROM Transaction SINCE 1 hour ago "
                         "TIMESERIES"),
            self._widget("B", "viz.line",
                         "SELECT count(*) FROM Transaction SINCE 1 hour ago "
                         "TIMESERIES", col=5),
            self._widget("Today", "viz.billboard",
                         "SELECT count(*) FROM Transaction SINCE today",
                         col=9),
            self._widget("Yesterday", "viz.line",
                         "SELECT count(*) FROM Transaction SINCE yesterday "
                         "TIMESERIES 5 minutes", col=13),
        ])
        self.assertEqual(dash["time"], {"from": "now-1h", "to": "now"})
        self.assertEqual(self._panel(dash, "Today")["timeFrom"], "now/d")
        yesterday = self._panel(dash, "Yesterday")
        self.assertEqual(yesterday["timeFrom"], "now-1d/d")
        self.assertEqual(yesterday["interval"], "5m")
        self.assertNotIn("timeFrom", self._panel(dash, "A"))

    def test_nrql_variables_are_scoped_per_datasource_family(self):
        variables = [
            {"name": "app", "title": "App", "type": "NRQL",
             "isMultiSelection": True,
             "nrqlQuery": {"accountIds": [1], "query":
                           "SELECT uniques(appName) FROM Transaction "
                           "WHERE environment = 'prod'"}},
            {"name": "host", "title": "Host", "type": "NRQL",
             "nrqlQuery": {"accountIds": [1], "query":
                           "SELECT count(*) FROM SystemSample FACET hostname"}},
            {"name": "svc", "title": "Svc", "type": "NRQL",
             "nrqlQuery": {"accountIds": [1], "query":
                           "SELECT uniques(service.name) FROM Log "
                           "WHERE level = 'error'"}},
            {"name": "span_svc", "title": "Span svc", "type": "NRQL",
             "nrqlQuery": {"accountIds": [1], "query":
                           "SELECT uniques(service.name) FROM Span"}},
            {"name": "mhost", "title": "Metric host", "type": "NRQL",
             "nrqlQuery": {"accountIds": [1], "query":
                           "SELECT uniques(host) FROM Metric WHERE metricName"
                           " = 'apm.service.transaction.duration' AND "
                           "appName = 'c'"}},
            {"name": "raw", "title": "Raw", "type": "NRQL",
             "nrqlQuery": {"accountIds": [1], "query":
                           "SELECT count(*) FROM Transaction"}},
        ]
        dash = self._build(
            [self._widget("W", "viz.line",
                          "SELECT count(*) FROM Transaction TIMESERIES")],
            variables, {"environment": "deployment_environment",
                        "hostname": "instance"})
        tvars = {v["name"]: v for v in dash["templating"]["list"]}
        app = tvars["app"]
        self.assertEqual(app["datasource"]["type"], "prometheus")
        self.assertEqual(
            app["definition"],
            'label_values(http_server_request_duration_seconds_count{'
            'deployment_environment="prod"}, service_name)')
        self.assertEqual(app["query"]["query"], app["definition"])
        self.assertTrue(app["multi"])
        self.assertEqual(tvars["host"]["definition"],
                         "label_values(node_uname_info, instance)")
        svc = tvars["svc"]
        self.assertEqual(svc["datasource"]["type"], "loki")
        self.assertEqual(svc["query"], {
            "type": 1, "label": "service_name",
            "stream": '{level=~"(?i)error"}',
            "refId": "LokiVariableQueryEditor-VariableQuery"})
        span = tvars["span_svc"]
        self.assertEqual(span["datasource"]["type"], "tempo")
        self.assertEqual(span["query"]["label"], "resource.service.name")
        self.assertEqual(
            tvars["mhost"]["definition"],
            'label_values(http_server_request_duration_seconds_count{'
            'service_name="c"}, instance)')
        # No enumerable attribute -> textbox with a warning label.
        self.assertEqual(tvars["raw"]["type"], "textbox")
        from nr2grafana.grafana.validate import validate_dashboard_full
        res = validate_dashboard_full(dash)
        self.assertEqual([e for e in res["errors"] if "variable" in e], [])


class Iteration3BuilderTests(Iteration2BuilderTests):
    def test_uniques_widget_becomes_a_table(self):
        dash = self._build([self._widget(
            "Hosts", "viz.billboard",
            "SELECT uniques(host) FROM Transaction WHERE appName = 'c'")])
        p = self._panel(dash, "Hosts")
        self.assertEqual(p["type"], "table")
        self.assertTrue(p["targets"][0]["instant"])

    def test_variable_since_and_interval(self):
        dash = self._build([
            self._widget("A", "viz.line",
                         "SELECT count(*) FROM Transaction SINCE {{since}} "
                         "TIMESERIES {{interval}}"),
            self._widget("B", "viz.line",
                         "SELECT count(*) FROM Transaction SINCE 1 hour ago "
                         "TIMESERIES", col=5),
        ], [{"name": "since", "type": "STRING",
             "defaultValues": [{"value": {"string": "1h"}}]},
            {"name": "interval", "type": "STRING",
             "defaultValues": [{"value": {"string": "5m"}}]}])
        a = self._panel(dash, "A")
        self.assertEqual(a["timeFrom"], "$since")
        self.assertEqual(a["interval"], "$interval")
        self.assertEqual(dash["time"], {"from": "now-1h", "to": "now"})
        from nr2grafana.grafana.validate import validate_dashboard_full
        self.assertEqual(validate_dashboard_full(dash)["errors"], [])
