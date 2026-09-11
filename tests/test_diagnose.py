"""Tests for nr2grafana.diagnose (root-cause diagnostics engine).

No network: a FakeLive stub with programmable metric/label inventories
answers every Grafana call, and canned test/parity artifacts drive each
finding type (did-you-mean, _total suffix flip, label-value mismatch by
matcher elimination, Loki parser mismatch, missing pipeline, auth
failures, datasource findings).
"""

import re
import unittest

from nr2grafana.diagnose import (
    MAX_PROBES_PER_PANEL, SCHEMA, _prom_metrics, _scan_selectors,
    diagnose)
from nr2grafana.grafana.client import GrafanaError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _frame(points):
    return {"data": {"values": [[1000 * i for i in range(points)],
                                [1.0] * points]}}


def _resp(points, ref_id="A"):
    frames = [_frame(points)] if points else []
    return {"results": {ref_id: {"status": 200, "frames": frames}}}


_SEL_RE = re.compile(r"\{[^}]*\}")
_MATCH_RE = re.compile(
    r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')


class FakeLive(object):
    """Programmable stand-in for GrafanaLive.

    ``series`` maps uid -> metric -> list of label dicts; probe
    queries (``count(...)`` / ``sum(count_over_time(...))``) are
    evaluated against them. ``loki_streams`` maps uid -> list of
    stream label dicts.
    """

    def __init__(self, metrics=None, series=None, loki_streams=None,
                 check_rows=None, dss=None, health=None,
                 user_error=None, org_error=None, perms=None):
        self.metrics = metrics or {}
        self.series = series or {}
        self.loki_streams = loki_streams or {}
        self.check_rows = check_rows
        self.dss = dss or []
        self.health = health or {}
        self.user_error = user_error
        self.org_error = org_error
        self.perms = perms
        self.queries = []
        self.req_paths = []

    # -- auth --------------------------------------------------------------

    def _req(self, method, path, body=None):
        self.req_paths.append(path)
        if path == "/api/user":
            if self.user_error:
                raise GrafanaError(self.user_error)
            return {"login": "svc"}
        if path == "/api/org":
            if self.org_error:
                raise GrafanaError(self.org_error)
            return {"name": "Main Org."}
        return {}

    def permissions_report(self):
        return self.perms or {"user": "svc", "role": "Admin",
                              "can_admin_datasources": True,
                              "can_edit_dashboards": True,
                              "detail": ""}

    # -- inventory ---------------------------------------------------------

    def datasources(self):
        return list(self.dss)

    def check_requirements(self, requirements):
        return list(self.check_rows or [])

    def datasource_health(self, uid):
        return self.health.get(uid, {"status": "ok", "message": ""})

    def prom_metric_names(self, uid):
        return list(self.metrics.get(uid, []))

    def prom_label_values(self, uid, label, match=""):
        values = []
        metrics = self.series.get(uid, {})
        pools = [metrics.get(match, [])] if match else \
            list(metrics.values())
        for pool in pools:
            for s in pool:
                if label in s and s[label] not in values:
                    values.append(s[label])
        return values

    def prom_series(self, uid, match, frm="now-1h"):
        return [dict(s) for s in
                self.series.get(uid, {}).get(match, [])]

    def loki_labels(self, uid):
        names = []
        for s in self.loki_streams.get(uid, []):
            for k in s:
                if k not in names:
                    names.append(k)
        return names

    def loki_label_values(self, uid, label):
        values = []
        for s in self.loki_streams.get(uid, []):
            if label in s and s[label] not in values:
                values.append(s[label])
        return values

    # -- probe evaluation --------------------------------------------------

    def ds_query(self, uid, ds_type, target, frm="now-1h", to="now"):
        expr = target.get("expr") or ""
        self.queries.append((uid, ds_type, expr))
        if ds_type == "loki":
            streams = self.loki_streams.get(uid, [])
            sel = _SEL_RE.search(expr)
            matchers = _MATCH_RE.findall(sel.group(0)) if sel else []
            n = sum(1 for s in streams
                    if self._match(s, matchers))
            return _resp(n)
        # prometheus: count(metric{...}) or count(metric)
        m = re.match(r"^count\((.*)\)$", expr)
        inner = m.group(1) if m else expr
        sel = _SEL_RE.search(inner)
        metric = inner.split("{", 1)[0].strip()
        matchers = _MATCH_RE.findall(sel.group(0)) if sel else []
        pool = self.series.get(uid, {}).get(metric, [])
        n = sum(1 for s in pool if self._match(s, matchers))
        return _resp(n)

    @staticmethod
    def _match(labels, matchers):
        for label, op, value in matchers:
            got = labels.get(label)
            if op == "=" and got != value:
                return False
            if op == "!=" and got == value:
                return False
            if op == "=~" and (got is None
                               or not re.fullmatch(value, got)):
                return False
            if op == "!~" and got is not None \
                    and re.fullmatch(value, got):
                return False
        return True


class FakeNR(object):
    def __init__(self, error=None, email="me@example.com"):
        self.error = error
        self.email = email

    def _post(self, query, variables=None, retries=3):
        if self.error:
            raise self.error
        return {"actor": {"user": {"email": self.email}}}


# ---------------------------------------------------------------------------
# Canned artifacts
# ---------------------------------------------------------------------------

def prom_panel(pid, expr, refid="A"):
    return {"id": pid, "type": "timeseries", "title": "P%d" % pid,
            "targets": [{"refId": refid, "expr": expr,
                         "datasource": {"type": "prometheus",
                                        "uid": "prom-uid"}}]}


def loki_panel(pid, expr, refid="A"):
    return {"id": pid, "type": "logs", "title": "L%d" % pid,
            "targets": [{"refId": refid, "expr": expr,
                         "datasource": {"type": "loki",
                                        "uid": "loki-uid"}}]}


def test_row(pid, expr, uid="prom-uid", status="no-data", error="",
             refid="A"):
    return {"panel_id": pid, "panel_title": "P%s" % pid,
            "refId": refid, "datasource": uid, "expr": expr,
            "status": status, "error": error, "frames": 0, "points": 0}


def by_id(report):
    return {f["id"]: f for f in report["findings"]}


class DiagnoseBase(unittest.TestCase):

    def assertFinding(self, report, fid):
        found = by_id(report)
        self.assertIn(fid, found, "have: %s" % sorted(found))
        return found[fid]


# ---------------------------------------------------------------------------
# Layer 3: prometheus panels
# ---------------------------------------------------------------------------

class TestDidYouMean(DiagnoseBase):

    def test_close_metric_suggested(self):
        gf = FakeLive(metrics={"prom-uid": [
            "http_server_request_duration_seconds_bucket", "up"]})
        expr = ("sum(rate("
                "http_server_request_duration_sec_bucket[5m]))")
        dash = {"panels": [prom_panel(1, expr)]}
        report = diagnose(gf, dash=dash,
                          test_results=[test_row(1, expr)])
        self.assertEqual(report["schema"], SCHEMA)
        f = self.assertFinding(report, "panel-1-metric-missing")
        self.assertEqual(f["severity"], "warn")
        self.assertEqual(f["area"], "panel")
        self.assertEqual(f["panel_id"], 1)
        self.assertIn("did you mean", f["problem"])
        self.assertEqual(f["fix"]["kind"], "edit-query")
        action = f["fix"]["action"]
        self.assertEqual(action["panel_id"], 1)
        self.assertEqual(action["refId"], "A")
        self.assertIn("http_server_request_duration_seconds_bucket",
                      action["new_expr"])
        self.assertNotIn("_sec_bucket", action["new_expr"])
        self.assertEqual(f["confidence"], "high")

    def test_rename_lands_in_config_overlay(self):
        gf = FakeLive(metrics={"prom-uid": [
            "http_server_request_duration_seconds_bucket"]})
        expr = "rate(http_server_request_duration_sec_bucket[5m])"
        report = diagnose(gf, dash={"panels": [prom_panel(1, expr)]},
                          test_results=[test_row(1, expr)])
        f = self.assertFinding(report, "config-renames")
        self.assertEqual(f["fix"]["kind"], "config-overlay")
        overlay = f["fix"]["action"]
        self.assertEqual(
            overlay["metric_map"]
            ["http_server_request_duration_sec_bucket"],
            "http_server_request_duration_seconds_bucket")

    def test_absent_metric_flagged_as_pipeline(self):
        gf = FakeLive(metrics={"prom-uid": ["up"]})
        expr = "sum(rate(aws_lambda_duration_seconds[5m]))"
        report = diagnose(gf, dash={"panels": [prom_panel(3, expr)]},
                          test_results=[test_row(3, expr)])
        f = self.assertFinding(report, "panel-3-metric-missing")
        self.assertEqual(f["fix"]["kind"], "pipeline")
        self.assertNotIn("did you mean", f["problem"])


class TestTotalSuffixFlip(DiagnoseBase):

    def test_missing_total_suffix(self):
        gf = FakeLive(metrics={"prom-uid": ["app_requests_total"]})
        expr = "rate(app_requests[5m])"
        report = diagnose(gf, dash={"panels": [prom_panel(1, expr)]},
                          test_results=[test_row(1, expr)])
        f = self.assertFinding(report, "panel-1-metric-total-suffix")
        self.assertEqual(f["fix"]["kind"], "edit-query")
        self.assertEqual(f["fix"]["action"]["new_expr"],
                         "rate(app_requests_total[5m])")
        self.assertEqual(f["confidence"], "high")
        overlay = self.assertFinding(report,
                                     "config-renames")["fix"]["action"]
        self.assertIs(overlay["metric_total_suffix"], True)

    def test_extra_total_suffix(self):
        gf = FakeLive(metrics={"prom-uid": ["app_requests"]})
        expr = "rate(app_requests_total[5m])"
        report = diagnose(gf, dash={"panels": [prom_panel(1, expr)]},
                          test_results=[test_row(1, expr)])
        f = self.assertFinding(report, "panel-1-metric-total-suffix")
        self.assertEqual(f["fix"]["action"]["new_expr"],
                         "rate(app_requests[5m])")
        overlay = self.assertFinding(report,
                                     "config-renames")["fix"]["action"]
        self.assertIs(overlay["metric_total_suffix"], False)


class TestMatcherElimination(DiagnoseBase):

    def _gf(self):
        return FakeLive(
            metrics={"prom-uid": ["svc_requests_total"]},
            series={"prom-uid": {"svc_requests_total": [
                {"service_name": "checkout", "env": "prod"},
                {"service_name": "payments", "env": "prod"},
            ]}})

    def test_label_value_mismatch(self):
        gf = self._gf()
        expr = ('sum(rate(svc_requests_total'
                '{service_name="check-out",env="prod"}[5m]))')
        report = diagnose(gf, dash={"panels": [prom_panel(1, expr)]},
                          test_results=[test_row(1, expr)])
        f = self.assertFinding(report, "panel-1-label-value")
        self.assertIn("checkout", f["problem"])
        self.assertEqual(f["fix"]["kind"], "edit-query")
        self.assertIn('service_name="checkout"',
                      f["fix"]["action"]["new_expr"])
        self.assertIn('env="prod"', f["fix"]["action"]["new_expr"])
        # evidence lists the actual values of the offending label
        self.assertIn("checkout", f["evidence"])
        self.assertIn("payments", f["evidence"])

    def test_label_name_mismatch(self):
        gf = FakeLive(
            metrics={"prom-uid": ["svc_requests_total"]},
            series={"prom-uid": {"svc_requests_total": [
                {"service_name": "web"}]}})
        expr = 'sum(svc_requests_total{servicename="web"})'
        report = diagnose(gf, dash={"panels": [prom_panel(2, expr)]},
                          test_results=[test_row(2, expr)])
        f = self.assertFinding(report, "panel-2-label-missing")
        self.assertIn("service_name", f["problem"])
        self.assertIn('service_name="web"',
                      f["fix"]["action"]["new_expr"])
        overlay = self.assertFinding(report,
                                     "config-renames")["fix"]["action"]
        self.assertEqual(overlay["label_map"]["servicename"],
                         "service_name")

    def test_selector_ok_points_at_outer_expression(self):
        gf = self._gf()
        expr = ('sum(rate(svc_requests_total'
                '{service_name="checkout"}[5m]))')
        report = diagnose(gf, dash={"panels": [prom_panel(1, expr)]},
                          test_results=[test_row(1, expr)])
        f = self.assertFinding(report, "panel-1-selector-ok")
        self.assertEqual(f["severity"], "info")
        self.assertIn("$__rate_interval", f["fix"]["description"])

    def test_probe_budget_capped_per_panel(self):
        labels = {"l%d" % i: "v%d" % i for i in range(12)}
        gf = FakeLive(metrics={"prom-uid": ["m"]},
                      series={"prom-uid": {"m": []}})
        sel = ",".join('%s="%s"' % kv for kv in sorted(labels.items()))
        expr = "sum(m{%s})" % sel
        diagnose(gf, dash={"panels": [prom_panel(1, expr)]},
                 test_results=[test_row(1, expr)])
        self.assertLessEqual(len(gf.queries), MAX_PROBES_PER_PANEL)


# ---------------------------------------------------------------------------
# Layer 3: loki panels
# ---------------------------------------------------------------------------

class TestLoki(DiagnoseBase):

    def test_parser_mismatch_json_vs_logfmt(self):
        gf = FakeLive(loki_streams={"loki-uid": [
            {"service_name": "web", "level": "info"}]})
        expr = ('sum(count_over_time({service_name="web"} '
                '| json [5m]))')
        report = diagnose(gf, dash={"panels": [loki_panel(4, expr)]},
                          test_results=[
                              test_row(4, expr, uid="loki-uid")])
        f = self.assertFinding(report, "panel-4-loki-parser")
        self.assertIn("logfmt", f["problem"])
        self.assertEqual(f["fix"]["kind"], "edit-query")
        self.assertIn("| logfmt", f["fix"]["action"]["new_expr"])
        self.assertNotIn("| json", f["fix"]["action"]["new_expr"])
        overlay = self.assertFinding(report,
                                     "config-renames")["fix"]["action"]
        self.assertEqual(overlay["loki_parser"], "logfmt")

    def test_missing_stream_label(self):
        gf = FakeLive(loki_streams={"loki-uid": [
            {"service_name": "web"}]})
        expr = '{servicename="web"} |= "error"'
        report = diagnose(gf, dash={"panels": [loki_panel(5, expr)]},
                          test_results=[
                              test_row(5, expr, uid="loki-uid")])
        f = self.assertFinding(report, "panel-5-loki-label-missing")
        self.assertIn("service_name", f["problem"])
        self.assertIn('service_name="web"',
                      f["fix"]["action"]["new_expr"])

    def test_stream_label_value_mismatch(self):
        gf = FakeLive(loki_streams={"loki-uid": [
            {"service_name": "webshop"}]})
        expr = '{service_name="web-shop"} |= "error"'
        report = diagnose(gf, dash={"panels": [loki_panel(6, expr)]},
                          test_results=[
                              test_row(6, expr, uid="loki-uid")])
        f = self.assertFinding(report, "panel-6-loki-label-value")
        self.assertIn('service_name="webshop"',
                      f["fix"]["action"]["new_expr"])


# ---------------------------------------------------------------------------
# Layer 4: missing pipeline
# ---------------------------------------------------------------------------

class TestPipeline(DiagnoseBase):

    def test_nr_has_data_grafana_missing_metric(self):
        gf = FakeLive(metrics={"prom-uid": ["up"]})
        expr = "sum(rate(aws_lambda_duration_seconds[5m]))"
        reqs = {"datasources": [], "domains": [
            {"domain": "aws-lambda",
             "evidence": ["FROM AwsLambdaInvocation"],
             "panel_ids": [3],
             "options": [
                 {"kind": "datasource", "plugin_id": "cloudwatch",
                  "core": True, "note": "CloudWatch datasource"},
                 {"kind": "pipeline",
                  "note": "YACE -> Mimir; aws_lambda_*"}]}]}
        parity = {"panels": [
            {"panel_id": 3, "refId": "A", "expr": expr,
             "datasource": "prom-uid", "verdict": "gf-empty",
             "detail": "", "ratio": None,
             "nr_summary": {"series": 1, "points": 5, "mean": 2.0},
             "gf_summary": {"series": 0, "points": 0, "mean": None}}]}
        report = diagnose(gf, dash={"panels": [prom_panel(3, expr)]},
                          requirements=reqs,
                          test_results=[test_row(3, expr)],
                          parity=parity)
        f = self.assertFinding(report, "pipeline-aws-lambda")
        self.assertEqual(f["severity"], "blocker")
        self.assertIn("New Relic still has this data", f["problem"])
        self.assertEqual(f["fix"]["kind"], "add-datasource")
        action = f["fix"]["action"]
        self.assertEqual(action["type"], "cloudwatch")
        self.assertIn("accessKey", action["needs_input"])
        self.assertIn("secretKey", action["needs_input"])

    def test_pipeline_without_datasource_option(self):
        gf = FakeLive(metrics={"prom-uid": ["up"]})
        expr = "sum(node_cpu_seconds_totalx)"
        reqs = {"domains": [
            {"domain": "infra-host", "evidence": ["FROM SystemSample"],
             "panel_ids": [7],
             "options": [{"kind": "pipeline",
                          "note": "node_exporter -> Mimir"}]}]}
        report = diagnose(gf, dash={"panels": [prom_panel(7, expr)]},
                          requirements=reqs,
                          test_results=[test_row(7, expr)])
        f = self.assertFinding(report, "pipeline-infra-host")
        self.assertEqual(f["fix"]["kind"], "pipeline")
        self.assertIn("node_exporter", f["fix"]["description"])
        self.assertEqual(f["severity"], "warn")  # no parity proof


# ---------------------------------------------------------------------------
# Layer 1: auth
# ---------------------------------------------------------------------------

class TestAuth(DiagnoseBase):

    def test_grafana_bad_token(self):
        gf = FakeLive(user_error="HTTP 401 from /api/user: "
                                 "invalid token")
        report = diagnose(gf)
        f = self.assertFinding(report, "grafana-auth")
        self.assertEqual(f["severity"], "blocker")
        self.assertEqual(f["area"], "auth")
        self.assertEqual(f["fix"]["kind"], "credentials")
        self.assertIn("service-account token",
                      f["fix"]["description"])

    def test_grafana_insufficient_role(self):
        gf = FakeLive(user_error="HTTP 403 forbidden",
                      org_error="HTTP 403 forbidden")
        report = diagnose(gf)
        f = self.assertFinding(report, "grafana-role")
        self.assertIn("Editor", f["fix"]["description"])
        self.assertIn("Admin", f["fix"]["description"])

    def test_grafana_editor_token_gets_admin_note_only(self):
        gf = FakeLive(perms={"user": "svc", "role": "Editor",
                             "can_admin_datasources": False,
                             "can_edit_dashboards": True,
                             "detail": "no datasources:create"})
        report = diagnose(gf)
        f = self.assertFinding(report, "grafana-role-admin")
        self.assertEqual(f["severity"], "info")

    def test_nr_bad_key(self):
        gf = FakeLive()
        nr = FakeNR(error=Exception(
            "Authentication failed (HTTP 401). Check that your key "
            "is a USER key"))
        report = diagnose(gf, nr=nr)
        f = self.assertFinding(report, "nr-auth")
        self.assertEqual(f["severity"], "blocker")
        self.assertIn("USER API key", f["fix"]["description"])

    def test_panel_level_auth_error(self):
        gf = FakeLive(metrics={"prom-uid": ["up"]})
        row = test_row(9, "up", status="error",
                       error="HTTP 401 Unauthorized from upstream")
        report = diagnose(gf, dash={"panels": [prom_panel(9, "up")]},
                          test_results=[row])
        f = self.assertFinding(report, "panel-9-auth")
        self.assertEqual(f["area"], "auth")
        self.assertIn("Unauthorized", f["evidence"])


# ---------------------------------------------------------------------------
# Layer 2: datasources
# ---------------------------------------------------------------------------

class TestDatasources(DiagnoseBase):

    def _reqs(self):
        return {"datasources": [
            {"family": "prometheus", "plugin_id": "prometheus",
             "core": True, "uid_ref": "${datasource}",
             "panel_ids": [1], "required": True}]}

    def test_missing_datasource_gets_payload_template(self):
        gf = FakeLive(check_rows=[
            {"item": "datasource:prometheus", "status": "missing",
             "detail": "no datasource of type 'prometheus'",
             "fix": "Add a Prometheus datasource"}])
        report = diagnose(gf, requirements=self._reqs())
        f = self.assertFinding(report, "ds-prometheus-missing")
        self.assertEqual(f["severity"], "blocker")
        self.assertEqual(f["fix"]["kind"], "add-datasource")
        action = f["fix"]["action"]
        self.assertEqual(action["type"], "prometheus")
        self.assertEqual(action["access"], "proxy")
        self.assertIn("url", action["needs_input"])
        self.assertIn("url", f["fix"]["description"].lower())

    def test_failing_health_becomes_blocker(self):
        gf = FakeLive(
            check_rows=[{"item": "datasource:prometheus",
                         "status": "ok", "detail": "", "fix": ""}],
            dss=[{"uid": "p1", "type": "prometheus", "name": "Mimir",
                  "isDefault": True}],
            health={"p1": {"status": "error",
                           "message": "connection refused"}})
        report = diagnose(gf, requirements=self._reqs())
        f = self.assertFinding(report, "ds-prometheus-health")
        self.assertEqual(f["severity"], "blocker")
        self.assertIn("connection refused", f["evidence"])
        self.assertIn("FROM THE GRAFANA SERVER",
                      f["fix"]["description"])

    def test_missing_plugin(self):
        gf = FakeLive(check_rows=[
            {"item": "plugin:nrgrafanaplugin-newrelic-datasource",
             "status": "missing", "detail": "not installed",
             "fix": "grafana-cli plugins install "
                    "nrgrafanaplugin-newrelic-datasource"}])
        report = diagnose(gf, requirements={"datasources": []})
        f = self.assertFinding(
            report,
            "plugin-nrgrafanaplugin-newrelic-datasource-missing")
        self.assertEqual(f["fix"]["kind"], "install-plugin")
        self.assertIn("grafana-cli", f["fix"]["description"])


# ---------------------------------------------------------------------------
# Degradation, report shape, helpers
# ---------------------------------------------------------------------------

class TestGracefulDegradation(DiagnoseBase):

    def test_all_none(self):
        report = diagnose(None)
        self.assertEqual(report["schema"], SCHEMA)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["summary"]["findings"], 0)
        self.assertTrue(report["generated_at"])

    def test_no_dash_uses_expectations_for_type(self):
        gf = FakeLive(loki_streams={"loki-uid": [
            {"service_name": "web"}]})
        expr = '{servicename="web"}'
        reqs = {"data_expectations": [
            {"panel_id": 5, "datasource": "loki",
             "needs": {"stream_selector": expr,
                       "labels": ["servicename"]}}]}
        report = diagnose(gf, requirements=reqs,
                          test_results=[
                              test_row(5, expr, uid="loki-uid")])
        self.assertFinding(report, "panel-5-loki-label-missing")

    def test_inventory_unreadable_is_reported_not_raised(self):
        gf = FakeLive()  # no metrics for the uid -> empty inventory
        expr = "up"
        report = diagnose(gf, dash={"panels": [prom_panel(1, expr)]},
                          test_results=[test_row(1, expr)])
        self.assertFinding(report, "metrics-unavailable-prom-uid")

    def test_generic_query_error_verbatim(self):
        gf = FakeLive(metrics={"prom-uid": ["up"]})
        row = test_row(2, "up{", status="error",
                       error='parse error: unexpected "{"')
        report = diagnose(gf, dash={"panels": [prom_panel(2, "up{")]},
                          test_results=[row])
        f = self.assertFinding(report, "panel-2-query-error")
        self.assertIn('parse error: unexpected "{"', f["evidence"])

    def test_parity_gf_empty_rows_used_without_test_results(self):
        gf = FakeLive(metrics={"prom-uid": ["app_requests_total"]})
        expr = "rate(app_requests[5m])"
        parity = {"panels": [
            {"panel_id": 1, "refId": "A", "expr": expr,
             "datasource": "prom-uid", "verdict": "gf-empty",
             "detail": "", "nr_summary": {"points": 3},
             "gf_summary": {"points": 0}}]}
        report = diagnose(gf, dash={"panels": [prom_panel(1, expr)]},
                          parity=parity)
        self.assertFinding(report, "panel-1-metric-total-suffix")

    def test_ids_unique_and_blockers_first(self):
        gf = FakeLive(user_error="HTTP 401 bad",
                      metrics={"prom-uid": ["up"]})
        rows = [test_row(1, "down", refid="A"),
                test_row(1, "downn", refid="B")]
        dash = {"panels": [{
            "id": 1, "type": "timeseries", "title": "P1",
            "targets": [
                {"refId": "A", "expr": "down",
                 "datasource": {"type": "prometheus",
                                "uid": "prom-uid"}},
                {"refId": "B", "expr": "downn",
                 "datasource": {"type": "prometheus",
                                "uid": "prom-uid"}}]}]}
        report = diagnose(gf, dash=dash, test_results=rows)
        ids = [f["id"] for f in report["findings"]]
        self.assertEqual(len(ids), len(set(ids)))
        sevs = [f["severity"] for f in report["findings"]]
        self.assertEqual(sevs, sorted(
            sevs, key=lambda s: {"blocker": 0, "warn": 1,
                                 "info": 2}[s]))
        self.assertEqual(report["summary"]["blocker"],
                         sevs.count("blocker"))


class TestScanners(unittest.TestCase):

    def test_scan_selectors_quote_aware(self):
        expr = ('sum(rate(m{a="x{y}",b=~"p|q"}[5m])) '
                '+ other{c!="z"}')
        sels = _scan_selectors(expr)
        self.assertEqual(len(sels), 2)
        self.assertEqual(sels[0]["metric"], "m")
        self.assertEqual(
            [(m["label"], m["op"], m["value"])
             for m in sels[0]["matchers"]],
            [("a", "=", "x{y}"), ("b", "=~", "p|q")])
        self.assertEqual(sels[1]["metric"], "other")
        self.assertEqual(sels[1]["matchers"][0]["op"], "!=")

    def test_prom_metrics_bare_and_selector(self):
        expr = ('sum by (job) (rate(reqs_total{job="x"}[5m])) '
                '/ ignoring(job) group_left up')
        self.assertEqual(_prom_metrics(expr), ["reqs_total", "up"])

    def test_name_matcher_metric(self):
        expr = 'count({__name__="foo_total",job="x"})'
        self.assertEqual(_prom_metrics(expr), ["foo_total"])


if __name__ == "__main__":
    unittest.main()
