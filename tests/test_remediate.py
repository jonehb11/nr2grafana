"""Tests for nr2grafana.remediate (apply_fix + auto_heal)."""

import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from nr2grafana.grafana.client import GrafanaError
from nr2grafana.remediate import apply_fix, auto_heal, overlay_path


# ---------------------------------------------------------------------------
# fixtures & stubs
# ---------------------------------------------------------------------------

def make_dash():
    """Dashboard with a prom panel, a tempo panel and a row-nested
    loki panel (exercises row recursion)."""
    return {
        "uid": "nr-pay",
        "title": "Payments",
        "panels": [
            {"id": 1, "type": "timeseries", "title": "Throughput",
             "targets": [{
                 "refId": "A",
                 "datasource": {"type": "prometheus", "uid": "${ds}"},
                 "expr": "sum(rate(http_req_count[5m]))",
             }]},
            {"id": 3, "type": "table", "title": "Slow spans",
             "targets": [{
                 "refId": "A",
                 "datasource": {"type": "tempo", "uid": "${tempo}"},
                 "queryType": "traceql",
                 "query": "{duration > 1s}",
             }]},
            {"id": 5, "type": "row", "title": "Logs", "collapsed": True,
             "panels": [
                 {"id": 6, "type": "logs", "title": "Errors",
                  "targets": [{
                      "refId": "B",
                      "datasource": {"type": "loki", "uid": "${loki}"},
                      "expr": '{service="payments"} |= "error"',
                  }]},
             ]},
        ],
    }


def make_datatest(dash):
    targets = []
    for panel in dash["panels"]:
        for sub in [panel] + (panel.get("panels") or []):
            for tgt in sub.get("targets") or []:
                targets.append({
                    "panel_id": sub["id"],
                    "panel_title": sub.get("title", ""),
                    "refId": tgt.get("refId", "A"),
                    "expr": tgt.get("expr") or tgt.get("query") or "",
                    "expect": "data",
                })
    return {"schema": "nr2grafana/datatest/v1",
            "dashboard": dash["title"], "uid": dash["uid"],
            "targets": targets}


class FakeChangeLog:
    """Records ChangeLog.record calls; no Store behind it."""

    def __init__(self):
        self.calls = []

    def record(self, slug, action, target, before, after,
               why="", source="user"):
        self.calls.append({"slug": slug, "action": action,
                           "target": target, "before": before,
                           "after": after, "why": why, "source": source})
        return len(self.calls)

    def by_action(self, action):
        return [c for c in self.calls if c["action"] == action]


class FakeGrafana:
    """Stub of the GrafanaLive surface remediate touches."""

    def __init__(self, test_rounds=None, health=None,
                 create_error="", push_error=""):
        self.created = []
        self.pushed = []
        self.health_checked = []
        self.test_calls = 0
        self._test_rounds = list(test_rounds or [])
        self._health = health or {"status": "ok", "message": "OK"}
        self._create_error = create_error
        self._push_error = push_error

    def create_datasource(self, payload):
        if self._create_error:
            raise GrafanaError(self._create_error)
        self.created.append(payload)
        return {"datasource": {"uid": "new-uid-1",
                               "type": payload.get("type", "")}}

    def datasource_health(self, uid):
        self.health_checked.append(uid)
        return dict(self._health)

    def update_dashboard(self, dash, folder_uid="", message=""):
        if self._push_error:
            raise GrafanaError(self._push_error)
        self.pushed.append({"dash": copy.deepcopy(dash),
                            "message": message})
        return {"status": "success"}

    def test_dashboard(self, dash, ds_map=None, log=None):
        self.test_calls += 1
        if self._test_rounds:
            round_result = self._test_rounds.pop(0)
            if isinstance(round_result, Exception):
                raise round_result
            return round_result
        return []


def edit_finding(panel_id=1, ref_id="A", new_expr="sum(rate(x[5m]))",
                 confidence="high", finding_id="f-1"):
    return {
        "id": finding_id,
        "severity": "warn",
        "area": "panel",
        "panel_id": panel_id,
        "problem": "metric not found",
        "evidence": "no series",
        "confidence": confidence,
        "fix": {
            "description": "did you mean x?",
            "kind": "edit-query",
            "action": {"panel_id": panel_id, "refId": ref_id,
                       "new_expr": new_expr},
        },
    }


def ds_finding(needs_input=None):
    action = {"name": "Mimir", "type": "prometheus",
              "url": "http://mimir:9009", "access": "proxy"}
    if needs_input is not None:
        action["needs_input"] = needs_input
    return {
        "id": "f-ds", "severity": "blocker", "area": "datasource",
        "problem": "no prometheus datasource",
        "fix": {"description": "create a prometheus datasource",
                "kind": "add-datasource", "action": action},
    }


def overlay_finding(overlay=None):
    return {
        "id": "f-cfg", "severity": "info", "area": "config",
        "problem": "recurring label rename",
        "fix": {"description": "codify the rename",
                "kind": "config-overlay",
                "action": overlay or {"label_map": {"svc": "service"}}},
    }


class PackageDirMixin:
    """Temp <out>/<slug>/ package with dashboard.json + datatest.json."""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="nr2g-remediate-")
        self.addCleanup(shutil.rmtree, self.out, True)
        self.slug = "payments"
        self.pkg = os.path.join(self.out, self.slug)
        os.makedirs(self.pkg)
        self.dash = make_dash()
        self._write("dashboard.json", self.dash)
        self._write("datatest.json", make_datatest(self.dash))
        self.changelog = FakeChangeLog()

    def _write(self, name, data):
        with open(os.path.join(self.pkg, name), "w") as f:
            json.dump(data, f, indent=2)

    def _read(self, name):
        with open(os.path.join(self.pkg, name)) as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# apply_fix: edit-query
# ---------------------------------------------------------------------------

class TestEditQuery(PackageDirMixin, unittest.TestCase):

    def test_prometheus_expr_edit_rewrites_package(self):
        res = apply_fix(edit_finding(new_expr="sum(rate(y[5m]))"),
                        dash=self.dash, package_dir=self.pkg,
                        changelog=self.changelog, slug=self.slug)
        self.assertTrue(res["applied"])
        self.assertEqual(res["kind"], "edit-query")
        # in-memory dash patched under "expr"
        tgt = self.dash["panels"][0]["targets"][0]
        self.assertEqual(tgt["expr"], "sum(rate(y[5m]))")
        # package dashboard.json rewritten
        disk = self._read("dashboard.json")
        self.assertEqual(disk["panels"][0]["targets"][0]["expr"],
                         "sum(rate(y[5m]))")
        # datatest.json kept consistent
        manifest = self._read("datatest.json")
        row = [t for t in manifest["targets"]
               if t["panel_id"] == 1 and t["refId"] == "A"][0]
        self.assertEqual(row["expr"], "sum(rate(y[5m]))")

    def test_changelog_record_query_edit(self):
        apply_fix(edit_finding(new_expr="new_metric_total"),
                  dash=self.dash, package_dir=self.pkg,
                  changelog=self.changelog, slug=self.slug)
        recs = self.changelog.by_action("query-edit")
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["slug"], self.slug)
        self.assertEqual(rec["before"], "sum(rate(http_req_count[5m]))")
        self.assertEqual(rec["after"], "new_metric_total")
        self.assertEqual(rec["why"], "metric not found")  # finding problem
        self.assertEqual(rec["source"], "user")

    def test_tempo_uses_query_key(self):
        res = apply_fix(edit_finding(panel_id=3,
                                     new_expr="{duration > 2s}"),
                        dash=self.dash, package_dir=self.pkg,
                        changelog=self.changelog, slug=self.slug)
        self.assertTrue(res["applied"])
        tgt = self.dash["panels"][1]["targets"][0]
        self.assertEqual(tgt["query"], "{duration > 2s}")
        self.assertNotIn("expr", tgt)

    def test_row_nested_panel_found(self):
        res = apply_fix(edit_finding(panel_id=6, ref_id="B",
                                     new_expr='{app="payments"}'),
                        dash=self.dash, package_dir=self.pkg,
                        changelog=self.changelog, slug=self.slug)
        self.assertTrue(res["applied"])
        nested = self.dash["panels"][2]["panels"][0]["targets"][0]
        self.assertEqual(nested["expr"], '{app="payments"}')
        manifest = self._read("datatest.json")
        row = [t for t in manifest["targets"] if t["panel_id"] == 6][0]
        self.assertEqual(row["expr"], '{app="payments"}')

    def test_loads_dashboard_from_package_when_dash_absent(self):
        res = apply_fix(edit_finding(new_expr="up"), dash=None,
                        package_dir=self.pkg,
                        changelog=self.changelog, slug=self.slug)
        self.assertTrue(res["applied"])
        disk = self._read("dashboard.json")
        self.assertEqual(disk["panels"][0]["targets"][0]["expr"], "up")

    def test_missing_panel_refused(self):
        res = apply_fix(edit_finding(panel_id=99), dash=self.dash,
                        package_dir=self.pkg, changelog=self.changelog)
        self.assertFalse(res["applied"])
        self.assertIn("99", res["detail"])
        self.assertEqual(self.changelog.calls, [])

    def test_missing_refid_refused(self):
        res = apply_fix(edit_finding(ref_id="Z"), dash=self.dash,
                        package_dir=self.pkg, changelog=self.changelog)
        self.assertFalse(res["applied"])
        self.assertIn("Z", res["detail"])

    def test_no_dash_no_package_refused(self):
        res = apply_fix(edit_finding())
        self.assertFalse(res["applied"])
        self.assertIn("dashboard", res["detail"])

    def test_unchanged_expr_refused(self):
        res = apply_fix(
            edit_finding(new_expr="sum(rate(http_req_count[5m]))"),
            dash=self.dash, package_dir=self.pkg,
            changelog=self.changelog)
        self.assertFalse(res["applied"])
        self.assertEqual(self.changelog.calls, [])

    def test_action_without_new_expr_refused(self):
        finding = edit_finding()
        finding["fix"]["action"] = {"panel_id": 1, "refId": "A"}
        res = apply_fix(finding, dash=self.dash, package_dir=self.pkg)
        self.assertFalse(res["applied"])
        self.assertIn("new_expr", res["detail"])

    def test_push_calls_update_dashboard(self):
        gf = FakeGrafana()
        res = apply_fix(edit_finding(new_expr="up"), grafana=gf,
                        dash=self.dash, package_dir=self.pkg,
                        changelog=self.changelog, slug=self.slug,
                        push=True)
        self.assertTrue(res["applied"])
        self.assertEqual(res["verify"], {"pushed": True})
        self.assertEqual(len(gf.pushed), 1)
        pushed_expr = gf.pushed[0]["dash"]["panels"][0]["targets"][0]
        self.assertEqual(pushed_expr["expr"], "up")
        self.assertEqual(
            len(self.changelog.by_action("dashboard-updated")), 1)

    def test_push_failure_keeps_local_edit(self):
        gf = FakeGrafana(push_error="HTTP 403: forbidden")
        res = apply_fix(edit_finding(new_expr="up"), grafana=gf,
                        dash=self.dash, package_dir=self.pkg,
                        changelog=self.changelog, slug=self.slug,
                        push=True)
        self.assertTrue(res["applied"])  # local edit stands
        self.assertFalse(res["verify"]["pushed"])
        self.assertIn("403", res["verify"]["error"])
        disk = self._read("dashboard.json")
        self.assertEqual(disk["panels"][0]["targets"][0]["expr"], "up")

    def test_no_push_by_default(self):
        gf = FakeGrafana()
        apply_fix(edit_finding(new_expr="up"), grafana=gf,
                  dash=self.dash, package_dir=self.pkg)
        self.assertEqual(gf.pushed, [])


# ---------------------------------------------------------------------------
# apply_fix: add-datasource
# ---------------------------------------------------------------------------

class TestAddDatasource(unittest.TestCase):

    def setUp(self):
        self.changelog = FakeChangeLog()

    def test_creates_health_checks_and_logs(self):
        gf = FakeGrafana()
        res = apply_fix(ds_finding(), grafana=gf,
                        changelog=self.changelog, slug="payments")
        self.assertTrue(res["applied"])
        self.assertEqual(res["kind"], "add-datasource")
        self.assertEqual(len(gf.created), 1)
        self.assertEqual(gf.created[0]["type"], "prometheus")
        self.assertEqual(gf.health_checked, ["new-uid-1"])
        self.assertEqual(res["verify"]["status"], "ok")
        recs = self.changelog.by_action("datasource-created")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["after"]["uid"], "new-uid-1")

    def test_meta_keys_stripped_from_payload(self):
        gf = FakeGrafana()
        finding = ds_finding()
        finding["fix"]["action"]["confidence"] = "high"
        finding["fix"]["action"]["note"] = "hint"
        apply_fix(finding, grafana=gf, changelog=self.changelog)
        payload = gf.created[0]
        self.assertNotIn("confidence", payload)
        self.assertNotIn("note", payload)
        self.assertNotIn("needs_input", payload)

    def test_unfilled_needs_input_refused_with_instructions(self):
        gf = FakeGrafana()
        res = apply_fix(ds_finding(needs_input=["url", "basicAuthUser"]),
                        grafana=gf, changelog=self.changelog)
        self.assertFalse(res["applied"])
        self.assertIn("url", res["detail"])
        self.assertIn("basicAuthUser", res["detail"])
        self.assertEqual(gf.created, [])          # nothing created
        self.assertEqual(self.changelog.calls, [])  # nothing logged

    def test_no_grafana_refused(self):
        res = apply_fix(ds_finding())
        self.assertFalse(res["applied"])
        self.assertIn("token", res["detail"])

    def test_no_action_refused(self):
        res = apply_fix({"fix": {"kind": "add-datasource",
                                 "description": "add prometheus"}},
                        grafana=FakeGrafana())
        self.assertFalse(res["applied"])
        self.assertIn("add prometheus", res["detail"])

    def test_create_error_is_actionable(self):
        gf = FakeGrafana(create_error="HTTP 403: permission denied")
        res = apply_fix(ds_finding(), grafana=gf,
                        changelog=self.changelog)
        self.assertFalse(res["applied"])
        self.assertIn("403", res["detail"])
        self.assertIn("Admin", res["detail"])
        self.assertEqual(self.changelog.calls, [])

    def test_failing_health_reported_but_applied(self):
        gf = FakeGrafana(health={"status": "error",
                                 "message": "connection refused"})
        res = apply_fix(ds_finding(), grafana=gf,
                        changelog=self.changelog)
        self.assertTrue(res["applied"])
        self.assertEqual(res["verify"]["status"], "error")
        self.assertIn("connection refused", res["detail"])


# ---------------------------------------------------------------------------
# apply_fix: config-overlay
# ---------------------------------------------------------------------------

class TestConfigOverlay(PackageDirMixin, unittest.TestCase):

    def test_creates_overlay_next_to_package_dir(self):
        res = apply_fix(overlay_finding(), package_dir=self.pkg,
                        changelog=self.changelog, slug=self.slug)
        self.assertTrue(res["applied"])
        path = overlay_path(self.pkg)
        self.assertEqual(os.path.dirname(path),
                         os.path.abspath(self.out))
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data, {"label_map": {"svc": "service"}})
        recs = self.changelog.by_action("config-overlay")
        self.assertEqual(len(recs), 1)

    def test_deep_merges_into_existing_overlay(self):
        path = overlay_path(self.pkg)
        with open(path, "w") as f:
            json.dump({"label_map": {"host": "instance"},
                       "metric_map": {"a": "b"}}, f)
        apply_fix(overlay_finding({"label_map": {"svc": "service"}}),
                  package_dir=self.pkg, changelog=self.changelog)
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["label_map"],
                         {"host": "instance", "svc": "service"})
        self.assertEqual(data["metric_map"], {"a": "b"})

    def test_overlay_wrapper_key_accepted(self):
        finding = overlay_finding()
        finding["fix"]["action"] = {
            "overlay": {"metric_map": {"old": "new"}}}
        res = apply_fix(finding, package_dir=self.pkg)
        self.assertTrue(res["applied"])
        with open(overlay_path(self.pkg)) as f:
            self.assertEqual(json.load(f),
                             {"metric_map": {"old": "new"}})

    def test_noop_when_already_present(self):
        apply_fix(overlay_finding(), package_dir=self.pkg)
        res = apply_fix(overlay_finding(), package_dir=self.pkg,
                        changelog=self.changelog)
        self.assertFalse(res["applied"])
        self.assertEqual(self.changelog.calls, [])

    def test_no_package_dir_refused_with_instructions(self):
        res = apply_fix(overlay_finding())
        self.assertFalse(res["applied"])
        self.assertIn("label_map", res["detail"])

    def test_empty_overlay_refused(self):
        finding = overlay_finding()
        finding["fix"]["action"] = {}
        res = apply_fix(finding, package_dir=self.pkg)
        self.assertFalse(res["applied"])


# ---------------------------------------------------------------------------
# apply_fix: advice-only + unknown kinds
# ---------------------------------------------------------------------------

class TestAdviceKinds(unittest.TestCase):

    def test_advice_kinds_are_noops_with_instructions(self):
        for kind in ("install-plugin", "credentials", "pipeline", "none"):
            res = apply_fix({"fix": {"kind": kind,
                                     "description": "do X by hand"}})
            self.assertFalse(res["applied"], kind)
            self.assertEqual(res["kind"], kind)
            self.assertIn("do X by hand", res["detail"])

    def test_unknown_kind_refused(self):
        res = apply_fix({"kind": "reboot-universe"})
        self.assertFalse(res["applied"])
        self.assertIn("reboot-universe", res["detail"])

    def test_flat_fix_dict_accepted(self):
        res = apply_fix({"kind": "pipeline",
                         "description": "deploy cloudwatch-exporter"})
        self.assertFalse(res["applied"])
        self.assertIn("cloudwatch-exporter", res["detail"])


# ---------------------------------------------------------------------------
# auto_heal
# ---------------------------------------------------------------------------

class TestAutoHeal(PackageDirMixin, unittest.TestCase):

    def _run(self, gf, diagnose_rounds, **kw):
        """auto_heal with a stubbed diagnose returning one dict per
        call (last one repeats)."""
        rounds = list(diagnose_rounds)

        def fake_diagnose(*args, **kwargs):
            if len(rounds) > 1:
                return rounds.pop(0)
            return rounds[0]

        with mock.patch("nr2grafana.remediate._load_diagnose",
                        return_value=fake_diagnose):
            return auto_heal(gf, None, self.dash, [], {}, self.slug,
                             self.pkg, changelog=self.changelog, **kw)

    def test_heals_then_converges(self):
        no_data = [{"panel_id": 1, "refId": "A", "status": "no-data"}]
        ok = [{"panel_id": 1, "refId": "A", "status": "data"}]
        gf = FakeGrafana(test_rounds=[no_data, ok, ok])
        result = self._run(gf, [
            {"findings": [edit_finding(new_expr="up")]},
            {"findings": []},
        ])
        self.assertEqual(result["fixed"], 1)
        self.assertTrue(result["converged"])
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(result["rounds"][0]["fixed"], 1)
        self.assertEqual(result["rounds"][1]["fixed"], 0)
        self.assertEqual(result["remaining_findings"], [])
        # re-tested between rounds
        self.assertEqual(gf.test_calls, 2)
        # the fix landed in memory and on disk
        self.assertEqual(
            self.dash["panels"][0]["targets"][0]["expr"], "up")
        disk = self._read("dashboard.json")
        self.assertEqual(disk["panels"][0]["targets"][0]["expr"], "up")

    def test_changes_recorded_as_source_auto(self):
        gf = FakeGrafana(test_rounds=[[], [], []])
        self._run(gf, [{"findings": [edit_finding(new_expr="up")]},
                       {"findings": []}])
        recs = self.changelog.by_action("query-edit")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["source"], "auto")

    def test_never_creates_datasources(self):
        gf = FakeGrafana(test_rounds=[[], [], []])
        # complete, immediately-creatable payload -- still not safe
        result = self._run(gf, [{"findings": [ds_finding()]}])
        self.assertEqual(gf.created, [])
        self.assertEqual(result["fixed"], 0)
        self.assertTrue(result["converged"])
        self.assertEqual(len(result["remaining_findings"]), 1)
        self.assertEqual(result["remaining_findings"][0]["id"], "f-ds")

    def test_low_confidence_edit_not_applied(self):
        gf = FakeGrafana(test_rounds=[[], [], []])
        result = self._run(
            gf, [{"findings": [edit_finding(new_expr="up",
                                            confidence="medium")]}])
        self.assertEqual(result["fixed"], 0)
        self.assertEqual(
            self.dash["panels"][0]["targets"][0]["expr"],
            "sum(rate(http_req_count[5m]))")
        self.assertEqual(len(result["remaining_findings"]), 1)

    def test_config_overlay_is_safe(self):
        gf = FakeGrafana(test_rounds=[[], [], []])
        result = self._run(gf, [{"findings": [overlay_finding()]},
                                {"findings": []}])
        self.assertEqual(result["fixed"], 1)
        self.assertTrue(os.path.exists(overlay_path(self.pkg)))

    def test_repeated_finding_applied_once(self):
        # diagnose keeps returning the same finding: round 2 must see
        # no NEW safe fixes and converge instead of looping.
        gf = FakeGrafana(test_rounds=[[], [], [], []])
        result = self._run(
            gf, [{"findings": [edit_finding(new_expr="up")]}],
            max_rounds=5)
        self.assertEqual(result["fixed"], 1)
        self.assertTrue(result["converged"])
        self.assertEqual(len(result["rounds"]), 2)
        recs = self.changelog.by_action("query-edit")
        self.assertEqual(len(recs), 1)

    def test_max_rounds_respected(self):
        gf = FakeGrafana(test_rounds=[[]] * 10)
        rounds = [{"findings": [edit_finding(
            new_expr="up_%d" % i, finding_id="f-%d" % i)]}
            for i in range(10)]
        result = self._run(gf, rounds, max_rounds=3)
        self.assertEqual(len(result["rounds"]), 3)
        self.assertFalse(result["converged"])
        self.assertEqual(result["fixed"], 3)

    def test_no_push_by_default(self):
        gf = FakeGrafana(test_rounds=[[], [], []])
        self._run(gf, [{"findings": [edit_finding(new_expr="up")]},
                       {"findings": []}])
        self.assertEqual(gf.pushed, [])

    def test_push_true_pushes_edits(self):
        gf = FakeGrafana(test_rounds=[[], [], []])
        self._run(gf, [{"findings": [edit_finding(new_expr="up")]},
                       {"findings": []}], push=True)
        self.assertEqual(len(gf.pushed), 1)

    def test_test_dashboard_failure_is_actionable(self):
        gf = FakeGrafana(
            test_rounds=[GrafanaError("HTTP 401: unauthorized")])
        result = self._run(gf, [{"findings": []}])
        self.assertIn("error", result)
        self.assertIn("401", result["error"])
        self.assertIn("token", result["error"])
        self.assertEqual(result["fixed"], 0)
        self.assertEqual(result["rounds"], [])

    def test_round_log_structure(self):
        no_data = [{"panel_id": 1, "refId": "A", "status": "no-data"},
                   {"panel_id": 3, "refId": "A", "status": "data"}]
        gf = FakeGrafana(test_rounds=[no_data, no_data])
        result = self._run(gf, [{"findings": [edit_finding(
            new_expr="up")]}, {"findings": []}])
        first = result["rounds"][0]
        self.assertEqual(first["round"], 1)
        self.assertEqual(first["tests"], {"no-data": 1, "data": 1})
        self.assertEqual(first["findings"], 1)
        self.assertEqual(len(first["applied"]), 1)
        self.assertTrue(first["applied"][0]["applied"])
        self.assertEqual(first["applied"][0]["finding_id"], "f-1")

    def test_log_callback_receives_rounds(self):
        lines = []
        gf = FakeGrafana(test_rounds=[[], [], []])
        rounds = [{"findings": [edit_finding(new_expr="up")]},
                  {"findings": []}]
        it = iter(rounds + [rounds[-1]])

        def fake_diagnose(*args, **kwargs):
            return next(it)

        with mock.patch("nr2grafana.remediate._load_diagnose",
                        return_value=fake_diagnose):
            auto_heal(gf, None, self.dash, [], {}, self.slug, self.pkg,
                      changelog=self.changelog, log=lines.append)
        joined = "\n".join(lines)
        self.assertIn("round 1", joined)
        self.assertIn("converged", joined)


if __name__ == "__main__":
    unittest.main()
