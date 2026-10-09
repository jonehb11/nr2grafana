"""Tests for nr2grafana.artifacts (per-dashboard packages)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from nr2grafana.artifacts import (
    build_datatest, package_dashboard, render_readme, render_test_sh,
    write_index,
)


def make_dash():
    return {
        "id": None,
        "uid": "nr-payments",
        "title": "Payments Service",
        "schemaVersion": 39,
        "templating": {"list": []},
        "panels": [
            {"id": 1, "type": "timeseries", "title": "Throughput",
             "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
             "targets": [{
                 "refId": "A",
                 "datasource": {"type": "prometheus",
                                "uid": "${datasource}"},
                 "expr": 'sum(rate(http_server_request_duration_seconds'
                         '_count{service_name="$service"}'
                         '[$__rate_interval]))',
             }]},
            {"id": 2, "type": "logs", "title": "Errors",
             "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
             "targets": [{
                 "refId": "A",
                 "datasource": {"type": "loki",
                                "uid": "${loki_datasource}"},
                 "expr": '{service_name="payments"} |= "error"',
             }]},
            {"id": 3, "type": "table", "title": "Slow spans",
             "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
             "targets": [{
                 "refId": "A",
                 "datasource": {"type": "tempo",
                                "uid": "${tempo_datasource}"},
                 "queryType": "traceql",
                 "query": '{duration > 1s}',
             }]},
            {"id": 4, "type": "text", "title": "Notes",
             "gridPos": {"x": 12, "y": 8, "w": 12, "h": 8},
             "options": {"mode": "markdown", "content": "hi"}},
            {"id": 5, "type": "row", "title": "Page 2", "collapsed": True,
             "gridPos": {"x": 0, "y": 16, "w": 24, "h": 1},
             "panels": [
                 {"id": 6, "type": "stat", "title": "Usage",
                  "gridPos": {"x": 0, "y": 17, "w": 6, "h": 4},
                  "targets": [{
                      "refId": "A",
                      "datasource": {
                          "type": "nrgrafanaplugin-newrelic-datasource",
                          "uid": "${newrelic_datasource}"},
                      "queryText": "SELECT sum(usage) FROM NrConsumption",
                  }]},
             ]},
        ],
    }


def make_report():
    return [
        {"page": "Main", "widget": "Throughput", "panel_id": 1,
         "panel_type": "timeseries", "confidence": "exact",
         "nrql": ["SELECT rate(count(*), 1 second) FROM Transaction"],
         "queries": [{"datasource": "prometheus", "expr": "...",
                      "type": "range"}], "notes": []},
        {"page": "Main", "widget": "Errors", "panel_id": 2,
         "panel_type": "logs", "confidence": "approximate",
         "nrql": ["SELECT * FROM Log"], "queries": [], "notes": []},
        {"page": "Main", "widget": "Slow spans", "panel_id": 3,
         "panel_type": "table", "confidence": "needs-review",
         "nrql": ["SELECT * FROM Span"], "queries": [], "notes": []},
        {"page": "Page 2", "widget": "Usage", "panel_id": 6,
         "panel_type": "stat", "confidence": "untranslatable",
         "nrql": ["SELECT sum(usage) FROM NrConsumption"], "queries": [],
         "notes": [], "fallback": "nrql-passthrough"},
    ]


def make_requirements():
    return {
        "schema": "nr2grafana/requirements/v1",
        "dashboard": "Payments Service",
        "uid": "nr-payments",
        "generated_by": "nr2grafana 1.1.0",
        "datasources": [
            {"family": "prometheus", "plugin_id": "prometheus",
             "core": True, "uid_ref": "${datasource}",
             "purpose": "metrics (Mimir/Prometheus)", "panel_ids": [1],
             "required": True},
            {"family": "loki", "plugin_id": "loki", "core": True,
             "uid_ref": "${loki_datasource}", "purpose": "logs (Loki)",
             "panel_ids": [2], "required": True},
        ],
        "plugins": [
            {"id": "nrgrafanaplugin-newrelic-datasource",
             "reason": "passthrough panels",
             "grafana_cli": "grafana-cli plugins install "
                            "nrgrafanaplugin-newrelic-datasource"},
        ],
        "domains": [
            {"domain": "nr-consumption",
             "evidence": ["FROM NrConsumption"], "panel_ids": [6],
             "options": [{"kind": "datasource",
                          "plugin_id":
                              "nrgrafanaplugin-newrelic-datasource",
                          "note": "New Relic-only data"}]},
        ],
        "nr_native": [
            {"panel_id": 6, "widget": "viz.billboard",
             "why": "NrConsumption exists only in New Relic",
             "equivalent": "stat panel + NR datasource plugin"},
        ],
        "data_expectations": [
            {"panel_id": 1, "datasource": "prometheus",
             "needs": {"metrics":
                       ["http_server_request_duration_seconds_count"],
                       "labels": ["service_name"]}},
            {"panel_id": 2, "datasource": "loki",
             "needs": {"stream_selector": '{service_name="payments"}',
                       "labels": ["service_name"]}},
        ],
        "import": {"steps": [], "api_example": ""},
    }


class PackageDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="n2g-artifacts-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = package_dashboard(
            self.tmp, "payments-service", make_dash(), make_report(),
            make_requirements(), {})

    def read(self, name):
        with open(os.path.join(self.pkg, name), encoding="utf-8") as f:
            return f.read()

    def test_returns_package_dir(self):
        self.assertEqual(self.pkg,
                         os.path.join(self.tmp, "payments-service"))
        self.assertTrue(os.path.isdir(self.pkg))

    def test_all_files_exist(self):
        for name in ("dashboard.json", "requirements.json",
                     "widget-report.json", "README.md", "datatest.json",
                     "test.sh"):
            self.assertTrue(
                os.path.isfile(os.path.join(self.pkg, name)), name)

    def test_dashboard_json_format(self):
        text = self.read("dashboard.json")
        self.assertTrue(text.endswith("\n"))
        self.assertIn('\n  "uid": "nr-payments"', text)  # 2-space indent
        self.assertEqual(json.loads(text)["title"], "Payments Service")

    def test_json_files_roundtrip(self):
        self.assertEqual(json.loads(self.read("requirements.json")),
                         make_requirements())
        self.assertEqual(json.loads(self.read("widget-report.json")),
                         make_report())

    def test_test_sh_is_executable(self):
        mode = os.stat(os.path.join(self.pkg, "test.sh")).st_mode
        self.assertTrue(mode & 0o111, "test.sh not executable")

    def test_test_sh_syntax(self):
        proc = subprocess.run(
            ["sh", "-n", os.path.join(self.pkg, "test.sh")],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_test_sh_requires_env(self):
        proc = subprocess.run(
            ["env", "-i", "PATH=/usr/bin:/bin", "sh", "test.sh"],
            cwd=self.pkg, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("GRAFANA_URL", proc.stderr)
        self.assertIn("GRAFANA_TOKEN", proc.stderr)


class DatatestTests(unittest.TestCase):
    def setUp(self):
        self.dt = build_datatest(make_dash(), make_report())

    def target(self, pid):
        return [t for t in self.dt["targets"] if t["panel_id"] == pid][0]

    def test_schema_and_header(self):
        self.assertEqual(self.dt["schema"], "nr2grafana/datatest/v1")
        self.assertEqual(self.dt["dashboard"], "Payments Service")
        self.assertEqual(self.dt["uid"], "nr-payments")

    def test_targets_extracted_including_nested_rows(self):
        ids = sorted(t["panel_id"] for t in self.dt["targets"])
        self.assertEqual(ids, [1, 2, 3, 6])  # text panel 4 skipped

    def test_families_and_expr_keys(self):
        self.assertEqual(self.target(1)["datasource_family"], "prometheus")
        self.assertEqual(self.target(2)["datasource_family"], "loki")
        self.assertEqual(self.target(3)["datasource_family"], "tempo")
        self.assertEqual(self.target(3)["expr"], "{duration > 1s}")
        self.assertEqual(self.target(6)["datasource_family"], "newrelic")
        self.assertIn("NrConsumption", self.target(6)["expr"])

    def test_raw_expr_no_substitution(self):
        self.assertIn("$__rate_interval", self.target(1)["expr"])
        self.assertIn("$service", self.target(1)["expr"])

    def test_uid_ref_and_expect(self):
        self.assertEqual(self.target(1)["ds_uid_ref"], "${datasource}")
        self.assertEqual(self.target(1)["expect"], "data")     # exact
        self.assertEqual(self.target(2)["expect"], "data")     # approx
        self.assertEqual(self.target(3)["expect"], "any")      # review
        self.assertEqual(self.target(6)["expect"], "any")      # manual

    def test_refids(self):
        self.assertEqual(self.target(1)["refId"], "A")
        self.assertEqual(self.target(1)["panel_title"], "Throughput")


class ReadmeTests(unittest.TestCase):
    def setUp(self):
        self.text = render_readme("payments-service", make_dash(),
                                  make_report(), make_requirements(), {})

    def test_title_and_source(self):
        self.assertTrue(self.text.startswith("# Payments Service\n"))
        self.assertIn("nr2grafana 1.1.0", self.text)

    def test_confidence_table(self):
        self.assertIn("| exact | 1 |", self.text)
        self.assertIn("| approximate | 1 |", self.text)
        self.assertIn("| needs-review | 1 |", self.text)
        self.assertIn("| untranslatable | 1 |", self.text)

    def test_required_datasources(self):
        self.assertIn("Required datasources", self.text)
        self.assertIn("`prometheus`", self.text)
        self.assertIn("`loki`", self.text)
        self.assertIn("${datasource}", self.text)
        self.assertIn("Connections -> Data sources", self.text)

    def test_plugins_section(self):
        self.assertIn("grafana-cli plugins install "
                      "nrgrafanaplugin-newrelic-datasource", self.text)

    def test_nr_native_section(self):
        self.assertIn("New Relic-native widgets", self.text)
        self.assertIn("viz.billboard", self.text)
        self.assertIn("stat panel + NR datasource plugin", self.text)

    def test_import_steps(self):
        self.assertIn("Dashboards -> New -> Import", self.text)
        self.assertIn("$GRAFANA_URL/api/dashboards/db", self.text)
        self.assertIn("$GRAFANA_TOKEN", self.text)

    def test_troubleshooting(self):
        self.assertIn("Troubleshooting", self.text)
        self.assertIn("http_server_request_duration_seconds_count",
                      self.text)
        self.assertIn('{service_name="payments"}', self.text)
        self.assertIn("test.sh", self.text)


class TestShHelperTests(unittest.TestCase):
    """Exercise the python helper embedded in test.sh end to end
    (no network: fake datasources file + fake query responses)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="n2g-helper-")
        script = render_test_sh("Payments Service")
        start = script.index("<<'PYEOF'\n") + len("<<'PYEOF'\n")
        end = script.index("\nPYEOF")
        cls.helper = script[start:end]
        cls.manifest = os.path.join(cls.tmp, "datatest.json")
        with open(cls.manifest, "w") as f:
            json.dump(build_datatest(make_dash(), make_report()), f)
        cls.dsfile = os.path.join(cls.tmp, "ds.json")
        with open(cls.dsfile, "w") as f:
            json.dump([{"type": "prometheus", "uid": "mimir-uid"},
                       {"type": "loki", "uid": "loki-uid"}], f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def helper_run(self, *args):
        return subprocess.run([sys.executable, "-c", self.helper]
                              + list(args), capture_output=True, text=True)

    def test_count(self):
        proc = self.helper_run("count", self.manifest)
        self.assertEqual(proc.stdout.strip(), "4")

    def test_line(self):
        proc = self.helper_run("line", self.manifest, "0")
        self.assertEqual(proc.stdout.strip(),
                         "1|A|prometheus|Throughput")

    def test_body_substitutes_and_resolves(self):
        proc = self.helper_run("body", self.manifest, self.dsfile, "0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        q = body["queries"][0]
        self.assertEqual(q["datasource"]["uid"], "mimir-uid")
        self.assertIn("[5m]", q["expr"])                  # rate_interval
        self.assertIn('service_name=~".+"', q["expr"])    # $service
        self.assertNotIn("$", q["expr"])
        self.assertEqual(body["from"], "now-1h")

    def test_body_missing_datasource_fails_actionably(self):
        # target index 2 is tempo; dsfile has no tempo datasource
        proc = self.helper_run("body", self.manifest, self.dsfile, "2")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("no tempo datasource", proc.stderr)
        self.assertIn("Connections -> Data sources", proc.stderr)

    def check(self, payload, code="200"):
        resp = os.path.join(self.tmp, "resp.json")
        with open(resp, "w") as f:
            f.write(payload)
        return self.helper_run("check", resp, "A", code).stdout.strip()

    def test_check_pass(self):
        resp = {"results": {"A": {"frames": [
            {"data": {"values": [[1, 2], [3.5, 4.5]]}}]}}}
        self.assertEqual(self.check(json.dumps(resp)), "PASS")

    def test_check_no_data(self):
        resp = {"results": {"A": {"frames": [{"data": {"values": []}}]}}}
        self.assertEqual(self.check(json.dumps(resp)), "NO-DATA")

    def test_check_query_error(self):
        resp = {"results": {"A": {"error": "parse error: bad expr"}}}
        out = self.check(json.dumps(resp))
        self.assertTrue(out.startswith("FAIL"))
        self.assertIn("parse error", out)

    def test_check_http_error(self):
        out = self.check("not json at all", "502")
        self.assertTrue(out.startswith("FAIL"))
        self.assertIn("502", out)


class WriteIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="n2g-index-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_index_from_full_entries(self):
        entries = [{
            "slug": "payments-service", "title": "Payments Service",
            "dir": os.path.join(self.tmp, "payments-service"),
            "widget_report": make_report(),
            "requirements": make_requirements(),
        }]
        path = write_index(self.tmp, entries)
        self.assertEqual(path, os.path.join(self.tmp, "INDEX.md"))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Payments Service", text)
        self.assertIn("payments-service/", text)
        # panels=4, exact=1, approx=1, review=1, manual=1
        self.assertIn("| 4 | 1 | 1 | 1 | 1 |", text)
        self.assertIn("prometheus, loki", text)
        self.assertIn("nr-consumption", text)
        self.assertIn("test.sh", text)  # workflow header

    def test_index_from_minimal_entries(self):
        path = write_index(self.tmp, [{"slug": "x"}])
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("| x |", text)

    def test_index_empty(self):
        path = write_index(self.tmp, [])
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("no dashboards packaged", text)


CW_TARGET = {
    "refId": "A",
    "datasource": {"type": "cloudwatch", "uid": "${cloudwatch_datasource}"},
    "namespace": "AWS/RDS", "metricName": "CPUUtilization",
    "statistic": "Average", "region": "default",
    "dimensions": {"DBInstanceIdentifier": ["*"]},
    "queryMode": "Metrics", "metricEditorMode": 0,
}


def make_cw_dash():
    dash = make_dash()
    dash["panels"].append({
        "id": 7, "type": "timeseries", "title": "RDS CPU",
        "gridPos": {"x": 0, "y": 24, "w": 12, "h": 8},
        "targets": [dict(CW_TARGET)]})
    dash["panels"].append({
        "id": 8, "type": "text", "title": "Cost [MANUAL]",
        "gridPos": {"x": 12, "y": 24, "w": 12, "h": 8},
        "options": {"mode": "markdown", "content": "why"}})
    return dash


def make_cw_report():
    report = make_report()
    report.append({
        "page": "Main", "widget": "RDS CPU", "panel_id": 7,
        "panel_type": "timeseries", "confidence": "approximate",
        "nrql": ["SELECT average(`aws.rds.CPUUtilization`) FROM Metric "
                 "FACET aws.rds.DBInstanceIdentifier TIMESERIES"],
        "queries": [{"datasource": "cloudwatch"}], "notes": [],
        "cloudwatch": True, "missing_datasource": "cloudwatch"})
    report.append({
        "page": "Main", "widget": "Cost", "panel_id": 8,
        "panel_type": "text", "confidence": "untranslatable",
        "nrql": ["SELECT sum(cost) FROM FinanceSample"], "queries": [],
        "notes": ["FinanceSample exists only in New Relic"],
        "manual": True, "missing_datasource": None,
        "closest_equivalent": {
            "datasource": "prometheus",
            "example_query": "sum(aws_cost_daily_usd)",
            "note": "AWS Cost Explorer via `nr2grafana tco`"}})
    return report


def make_cw_requirements():
    req = make_requirements()
    req["datasources"].append({
        "family": "cloudwatch", "plugin_id": "cloudwatch", "core": True,
        "uid_ref": "${cloudwatch_datasource}",
        "purpose": "AWS metrics (CloudWatch)", "panel_ids": [7],
        "required": True})
    req["missing_datasources"] = [{
        "family": "cloudwatch", "plugin_id": "cloudwatch", "core": True,
        "uid_ref": "${cloudwatch_datasource}", "reason": "unbound",
        "detail": "no concrete datasource uid bound",
        "panel_ids": [7], "purpose": "AWS metrics (CloudWatch)",
        "fix": "Add a cloudwatch datasource",
        "add_datasource": {
            "cli": "nr2grafana grafana add-datasource --type cloudwatch "
                   "--name cloudwatch",
            "ui": "Connections -> Data sources -> Add new data source "
                  "-> cloudwatch",
            "api": "curl -sS -X POST \"$GRAFANA_URL/api/datasources\"",
            "payload": {"name": "cloudwatch", "type": "cloudwatch",
                        "access": "proxy",
                        "jsonData": {"authType": "keys",
                                     "defaultRegion": "us-east-1"},
                        "secureJsonData": {"accessKey": "<accessKey>",
                                           "secretKey": "<secretKey>"}},
            "required_fields": ["authType", "defaultRegion"],
            "notes": "needs cloudwatch:GetMetricData (read only)"}}]
    req["manual_panels"] = [{
        "panel_id": 8, "title": "Cost", "page": "Main",
        "visualization": "viz.billboard", "confidence": "untranslatable",
        "why": "FinanceSample exists only in New Relic",
        "nrql": "SELECT sum(cost) FROM FinanceSample",
        "missing_datasource": None,
        "closest_equivalent": {
            "datasource": "prometheus",
            "example_query": "sum(aws_cost_daily_usd)",
            "note": "AWS Cost Explorer via `nr2grafana tco`"},
        "equivalent": "prometheus: sum(aws_cost_daily_usd)"}, {
        "panel_id": 7, "title": "RDS CPU", "page": "Main",
        "visualization": "viz.line", "confidence": "needs-review",
        "why": "aws.* metric has no data in Mimir",
        "nrql": "SELECT average(`aws.rds.CPUUtilization`) FROM Metric",
        "missing_datasource": "cloudwatch",
        "closest_equivalent": {
            "datasource": "cloudwatch",
            "cw_target": {"namespace": "AWS/RDS",
                          "metricName": "CPUUtilization",
                          "statistic": "Average",
                          "dimensions": {"DBInstanceIdentifier": ["*"]}},
            "note": "bind ${cloudwatch_datasource}"},
        "equivalent": ""}]
    return req


class CloudWatchDatatestTests(unittest.TestCase):
    def setUp(self):
        self.dt = build_datatest(make_cw_dash(), make_cw_report())

    def target(self, pid):
        return [t for t in self.dt["targets"] if t["panel_id"] == pid][0]

    def test_cw_target_marked_and_skipped(self):
        ids = sorted(t["panel_id"] for t in self.dt["targets"])
        self.assertEqual(ids, [1, 2, 3, 6, 7])  # text panel 8 skipped
        cw = self.target(7)
        self.assertEqual(cw["datasource_family"], "cloudwatch")
        self.assertEqual(cw["ds_uid_ref"], "${cloudwatch_datasource}")
        self.assertEqual(cw["expect"], "skip")
        self.assertIn("AWS", cw["skip_reason"])
        self.assertEqual(cw["cw"]["namespace"], "AWS/RDS")
        self.assertEqual(cw["cw"]["metricName"], "CPUUtilization")
        self.assertEqual(cw["cw"]["dimensions"],
                         {"DBInstanceIdentifier": ["*"]})
        self.assertNotIn("refId", cw["cw"])
        self.assertIn("AWS/RDS CPUUtilization Average", cw["expr"])

    def test_prom_targets_unchanged(self):
        self.assertEqual(self.target(1)["expect"], "data")
        self.assertNotIn("cw", self.target(1))

    def test_json_serializable(self):
        json.dumps(self.dt)


class CloudWatchTestShTests(unittest.TestCase):
    """The embedded helper skips CloudWatch rows (exit 4) and the shell
    loop reports them as SKIP without failing the run."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="n2g-cw-")
        cls.pkg = package_dashboard(cls.tmp, "cw", make_cw_dash(),
                                    make_cw_report(),
                                    make_cw_requirements(), {})
        script = render_test_sh("CW")
        start = script.index("<<'PYEOF'\n") + len("<<'PYEOF'\n")
        end = script.index("\nPYEOF")
        cls.helper = script[start:end]
        cls.manifest = os.path.join(cls.pkg, "datatest.json")
        cls.dsfile = os.path.join(cls.tmp, "ds.json")
        with open(cls.dsfile, "w") as f:
            json.dump([{"type": "prometheus", "uid": "mimir-uid"},
                       {"type": "loki", "uid": "loki-uid"},
                       {"type": "tempo", "uid": "tempo-uid"},
                       {"type": "nrgrafanaplugin-newrelic-datasource",
                        "uid": "nr-uid"},
                       {"type": "cloudwatch", "uid": "cw-uid"}], f)
        # Fake curl: GET /api/datasources -> ds list; POST -> one frame
        # with data. Prints the HTTP code like `-w '%{http_code}'`.
        cls.bin = os.path.join(cls.tmp, "bin")
        os.makedirs(cls.bin)
        fake = os.path.join(cls.bin, "curl")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\n"
                    "out=''; post=0\n"
                    "while [ $# -gt 0 ]; do\n"
                    "  case \"$1\" in\n"
                    "    -o) out=$2; shift ;;\n"
                    "    -X) [ \"$2\" = POST ] && post=1; shift ;;\n"
                    "  esac\n"
                    "  shift\n"
                    "done\n"
                    "if [ $post -eq 1 ]; then\n"
                    "  cat >/dev/null\n"
                    "  printf '%s' '{\"results\":{\"A\":{\"frames\":"
                    "[{\"data\":{\"values\":[[1],[2]]}}]}}}' >\"$out\"\n"
                    "else\n"
                    "  cp \"@DS@\" \"$out\"\n"
                    "fi\n"
                    "printf 200\n".replace("@DS@", cls.dsfile))
        os.chmod(fake, 0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def helper_run(self, *args):
        return subprocess.run([sys.executable, "-c", self.helper]
                              + list(args), capture_output=True, text=True)

    def test_helper_skips_cloudwatch_row(self):
        proc = self.helper_run("count", self.manifest)
        n = int(proc.stdout.strip())
        self.assertEqual(n, 5)
        proc = self.helper_run("line", self.manifest, "4")
        self.assertEqual(proc.stdout.strip(), "7|A|cloudwatch|RDS CPU")
        proc = self.helper_run("body", self.manifest, self.dsfile, "4")
        self.assertEqual(proc.returncode, 4)
        self.assertIn("CloudWatch", proc.stderr)
        self.assertIn("verify in Grafana", proc.stderr)

    def test_helper_still_builds_prom_body(self):
        proc = self.helper_run("body", self.manifest, self.dsfile, "0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout)["queries"][0]["datasource"]["uid"],
            "mimir-uid")

    def test_test_sh_reports_skip_and_exits_zero(self):
        env = {"PATH": self.bin + os.pathsep + "/usr/bin:/bin",
               "GRAFANA_URL": "http://grafana.example.com",
               "GRAFANA_TOKEN": "glsa_test"}
        proc = subprocess.run(["sh", "test.sh"], cwd=self.pkg, env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SKIP     panel 7 [A] (cloudwatch) RDS CPU",
                      proc.stdout)
        self.assertIn("PASS     panel 1 [A] (prometheus) Throughput",
                      proc.stdout)
        self.assertIn("4 passed, 0 no-data, 0 failed, 1 skipped (of 5)",
                      proc.stdout)


class CloudWatchReadmeTests(unittest.TestCase):
    def setUp(self):
        self.text = render_readme("cw", make_cw_dash(), make_cw_report(),
                                  make_cw_requirements(), {})

    def test_before_you_import_lists_missing_with_template(self):
        before = self.text.index("## Before you import")
        missing = self.text.index("### Missing datasources")
        imp = self.text.index("## Import")
        self.assertTrue(before < missing < imp)
        section = self.text[missing:imp]
        self.assertIn("**cloudwatch** (`cloudwatch`, unbound) -- panels 7",
                      section)
        self.assertIn("nr2grafana grafana add-datasource --type cloudwatch",
                      section)
        self.assertIn("POST $GRAFANA_URL/api/datasources", section)
        self.assertIn('"type": "cloudwatch"', section)
        self.assertIn('"defaultRegion": "us-east-1"', section)
        self.assertIn('"secretKey": "<secretKey>"', section)
        self.assertIn("required: `authType`, `defaultRegion`", section)
        self.assertIn("GetMetricData", section)

    def test_required_table_includes_cloudwatch(self):
        self.assertIn("| Cloudwatch | `cloudwatch` | yes |", self.text)
        self.assertIn("${cloudwatch_datasource}", self.text)

    def test_manual_section_lists_why_and_equivalent(self):
        start = self.text.index("## [MANUAL] panels")
        end = self.text.index("## New Relic-native widgets")
        section = self.text[start:end]
        self.assertIn("Panel 8 'Cost'", section)
        self.assertIn("why: FinanceSample exists only in New Relic",
                      section)
        self.assertIn("closest equivalent (prometheus): "
                      "`sum(aws_cost_daily_usd)`", section)
        self.assertIn("how: AWS Cost Explorer", section)
        self.assertIn("original NRQL: `SELECT sum(cost) FROM "
                      "FinanceSample`", section)
        self.assertIn("Panel 7 'RDS CPU'", section)
        self.assertIn("missing datasource: `cloudwatch`", section)
        self.assertIn("closest equivalent (cloudwatch target):", section)
        self.assertIn("CPUUtilization", section)

    def test_manual_section_falls_back_to_report(self):
        req = make_cw_requirements()
        del req["manual_panels"]
        text = render_readme("cw", make_cw_dash(), make_cw_report(),
                             req, {})
        self.assertIn("## [MANUAL] panels", text)
        self.assertIn("Panel 8 'Cost'", text)
        self.assertIn("`sum(aws_cost_daily_usd)`", text)
        self.assertIn("Panel 6 'Usage'", text)  # untranslatable, no CE
        self.assertIn("none known", text)

    def test_troubleshooting_mentions_skip(self):
        self.assertIn("SKIP lines are CloudWatch targets", self.text)

    def test_no_missing_section_when_nothing_missing(self):
        req = make_cw_requirements()
        req["missing_datasources"] = []
        text = render_readme("cw", make_cw_dash(), make_cw_report(),
                             req, {})
        self.assertNotIn("### Missing datasources", text)


class CloudWatchIndexTests(unittest.TestCase):
    def test_index_missing_column(self):
        tmp = tempfile.mkdtemp(prefix="n2g-cwindex-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = write_index(tmp, [{
            "slug": "cw", "title": "CW", "dir": os.path.join(tmp, "cw"),
            "widget_report": make_cw_report(),
            "requirements": make_cw_requirements()}])
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("| Missing DS |", text)
        self.assertIn("prometheus, loki, cloudwatch | nr-consumption | "
                      "cloudwatch |", text)


class RobustnessTests(unittest.TestCase):
    def test_package_with_empty_requirements_and_report(self):
        tmp = tempfile.mkdtemp(prefix="n2g-min-")
        self.addCleanup(shutil.rmtree, tmp, True)
        dash = {"uid": "u", "title": "T", "panels": []}
        pkg = package_dashboard(tmp, "t", dash, [], {}, {})
        for name in ("dashboard.json", "requirements.json",
                     "widget-report.json", "README.md", "datatest.json",
                     "test.sh"):
            self.assertTrue(os.path.isfile(os.path.join(pkg, name)), name)
        with open(os.path.join(pkg, "README.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Troubleshooting", text)


if __name__ == "__main__":
    unittest.main()
