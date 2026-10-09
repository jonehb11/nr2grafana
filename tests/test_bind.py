"""Tests for nr2grafana.bind (SEAM-BIND): datasource binding and target
environment selection on converted dashboards."""

import copy
import json
import os
import unittest

from nr2grafana import bind
from nr2grafana.config import load_config
from nr2grafana.grafana.builder import build_dashboards
from nr2grafana.model import parse_nr_dashboard

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(PROJECT_ROOT, "fixtures", "newrelic",
                      "sample-service-dashboard.json")


def _ds(ds_type, uid):
    return {"type": ds_type, "uid": uid}


def _dash():
    """A hand-built portable dashboard covering every ref location:
    panel + target refs, a row with nested panels, a query variable, a
    legacy string ref, a CloudWatch target, a Mixed panel."""
    return {
        "title": "Checkout ($env)", "uid": "u1",
        "templating": {"list": [
            {"type": "datasource", "name": "datasource",
             "query": "prometheus", "current": {}, "options": []},
            {"type": "datasource", "name": "loki_datasource",
             "query": "loki", "current": {}, "options": []},
            {"type": "datasource", "name": "cloudwatch_datasource",
             "query": "cloudwatch", "current": {}, "options": []},
            {"type": "query", "name": "app",
             "datasource": _ds("prometheus", "${datasource}"),
             "query": {"query": "label_values(service_name)"},
             "current": {}, "options": []},
            {"type": "custom", "name": "env",
             "query": "Production : prod, Staging : staging",
             "options": [
                 {"selected": True, "text": "Production",
                  "value": "prod"},
                 {"selected": False, "text": "Staging",
                  "value": "staging"}],
             "current": {"selected": True, "text": "Production",
                         "value": "prod"}},
        ]},
        "annotations": {"list": [
            {"builtIn": 1, "name": "Annotations & Alerts",
             "datasource": _ds("grafana", "-- Grafana --")}]},
        "panels": [
            {"id": 1, "type": "timeseries", "title": "rps",
             "datasource": _ds("prometheus", "${datasource}"),
             "targets": [
                 {"refId": "A",
                  "datasource": _ds("prometheus", "${datasource}"),
                  "expr": "sum(rate(acme_backend_orders_total"
                          "{cluster=\"acme-cluster-$env\"}"
                          "[$__rate_interval]))"}]},
            {"id": 2, "type": "logs", "title": "errors",
             "datasource": "${loki_datasource}",
             "targets": [
                 {"refId": "A", "datasource": "${loki_datasource}",
                  "expr": "{app=\"acme-backend\", env=\"${env}\"} |= "
                          "\"error\""}]},
            {"id": 3, "type": "row", "title": "AWS", "collapsed": True,
             "panels": [
                 {"id": 4, "type": "timeseries", "title": "rds cpu",
                  "datasource": _ds("cloudwatch",
                                    "${cloudwatch_datasource}"),
                  "targets": [
                      {"refId": "A",
                       "datasource": _ds("cloudwatch",
                                         "${cloudwatch_datasource}"),
                       "namespace": "AWS/RDS",
                       "metricName": "CPUUtilization",
                       "dimensions": {"DBInstanceIdentifier":
                                      ["acme-db-[[env]]"]}}]}]},
            {"id": 5, "type": "timeseries", "title": "mixed",
             "datasource": _ds("datasource", "-- Mixed --"),
             "targets": [
                 {"refId": "A",
                  "datasource": _ds("prometheus", "${datasource}"),
                  "expr": "up"},
                 {"refId": "B",
                  "datasource": _ds("tempo", "${tempo_datasource}"),
                  "query": "{}"}]},
        ],
    }


# resolve_ds_map emits both the bare name and the ${name} form.
DS_MAP = {"datasource": "mimir-uid", "${datasource}": "mimir-uid",
          "loki_datasource": "loki-uid", "${loki_datasource}": "loki-uid",
          "cloudwatch_datasource": "cw-uid",
          "${cloudwatch_datasource}": "cw-uid"}


class NormalizeDsMapTests(unittest.TestCase):
    def test_resolve_ds_map_shape(self):
        norm = bind.normalize_ds_map(DS_MAP, _dash())
        self.assertEqual(norm["datasource"],
                         {"type": "prometheus", "uid": "mimir-uid"})
        self.assertEqual(norm["loki_datasource"],
                         {"type": "loki", "uid": "loki-uid"})
        self.assertEqual(norm["cloudwatch_datasource"],
                         {"type": "cloudwatch", "uid": "cw-uid"})
        self.assertNotIn("${datasource}", norm)

    def test_explicit_dict_values_win_on_type(self):
        norm = bind.normalize_ds_map(
            {"datasource": {"type": "grafana-amazonprometheus-datasource",
                            "uid": "amp"}}, _dash())
        self.assertEqual(norm["datasource"]["type"],
                         "grafana-amazonprometheus-datasource")
        self.assertEqual(norm["datasource"]["uid"], "amp")

    def test_type_from_refs_when_no_variable(self):
        dash = _dash()
        dash["templating"]["list"] = []
        norm = bind.normalize_ds_map({"tempo_datasource": "tempo-uid"},
                                     dash)
        self.assertEqual(norm["tempo_datasource"]["type"], "tempo")

    def test_unresolved_entries_skipped(self):
        norm = bind.normalize_ds_map({"datasource": "${datasource}",
                                      "loki_datasource": "",
                                      "x": None}, _dash())
        self.assertEqual(norm, {})

    def test_default_family_without_dashboard(self):
        norm = bind.normalize_ds_map({"datasource": "p"})
        self.assertEqual(norm["datasource"]["type"], "prometheus")


class BindDatasourcesTests(unittest.TestCase):
    def setUp(self):
        self.src = _dash()
        self.before = copy.deepcopy(self.src)
        self.out = bind.bind_datasources(self.src, DS_MAP)

    def test_input_not_mutated(self):
        self.assertEqual(self.src, self.before)

    def test_target_and_panel_refs_bound(self):
        p1 = self.out["panels"][0]
        self.assertEqual(p1["datasource"], _ds("prometheus", "mimir-uid"))
        self.assertEqual(p1["targets"][0]["datasource"],
                         _ds("prometheus", "mimir-uid"))

    def test_legacy_string_refs_become_objects(self):
        p2 = self.out["panels"][1]
        self.assertEqual(p2["datasource"], _ds("loki", "loki-uid"))
        self.assertEqual(p2["targets"][0]["datasource"],
                         _ds("loki", "loki-uid"))

    def test_row_nested_cloudwatch_bound(self):
        inner = self.out["panels"][2]["panels"][0]
        self.assertEqual(inner["datasource"], _ds("cloudwatch", "cw-uid"))
        self.assertEqual(inner["targets"][0]["datasource"],
                         _ds("cloudwatch", "cw-uid"))
        # non-datasource target fields untouched
        self.assertEqual(inner["targets"][0]["namespace"], "AWS/RDS")

    def test_query_variable_datasource_bound(self):
        app = bind.find_variable(self.out, "app")
        self.assertEqual(app["datasource"], _ds("prometheus", "mimir-uid"))

    def test_builtin_refs_untouched(self):
        self.assertEqual(self.out["panels"][3]["datasource"],
                         _ds("datasource", "-- Mixed --"))
        self.assertEqual(self.out["annotations"]["list"][0]["datasource"],
                         _ds("grafana", "-- Grafana --"))

    def test_bound_datasource_variables_dropped(self):
        names = [v["name"] for v in self.out["templating"]["list"]]
        self.assertNotIn("datasource", names)
        self.assertNotIn("loki_datasource", names)
        self.assertNotIn("cloudwatch_datasource", names)
        # the non-datasource variables survive, order preserved
        self.assertEqual(names, ["app", "env"])

    def test_unresolved_variable_ref_kept_and_reported(self):
        # tempo was not in the map: its ref stays symbolic so the user
        # learns exactly which datasource to add.
        tgt = self.out["panels"][3]["targets"][1]
        self.assertEqual(tgt["datasource"]["uid"], "${tempo_datasource}")
        self.assertEqual(bind.unbound_refs(self.out), ["tempo_datasource"])
        self.assertEqual(bind.unbound_refs(self.src),
                         ["cloudwatch_datasource", "datasource",
                          "loki_datasource", "tempo_datasource"])

    def test_keep_vars_preselects_bound_uid(self):
        out = bind.bind_datasources(self.src, DS_MAP, keep_vars=True)
        var = bind.find_variable(out, "datasource")
        self.assertIsNotNone(var)
        self.assertEqual(var["current"]["value"], "mimir-uid")
        self.assertEqual(out["panels"][0]["targets"][0]["datasource"],
                         _ds("prometheus", "mimir-uid"))

    def test_dict_valued_map(self):
        out = bind.bind_datasources(
            self.src, {"datasource": {"type": "prometheus",
                                      "uid": "p1"}})
        self.assertEqual(out["panels"][0]["targets"][0]["datasource"],
                         _ds("prometheus", "p1"))
        self.assertIn("loki_datasource", bind.unbound_refs(out))

    def test_empty_map_is_identity(self):
        out = bind.bind_datasources(self.src, {})
        self.assertEqual(out, self.src)

    def test_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            bind.bind_datasources([], DS_MAP)

    def test_expressions_untouched(self):
        self.assertIn("$env", self.out["panels"][0]["targets"][0]["expr"])
        self.assertEqual(json.dumps(self.out["panels"][1]["targets"][0]
                                    ["expr"]),
                         json.dumps(self.src["panels"][1]["targets"][0]
                                    ["expr"]))

    def test_real_converted_dashboard_fully_bound(self):
        with open(SAMPLE, encoding="utf-8") as f:
            nr = parse_nr_dashboard(json.load(f))
        _fn, dash, _rep = build_dashboards(nr, load_config(""))[0]
        self.assertTrue(bind.unbound_refs(dash))
        names = [v["name"] for v in bind.datasource_variables(dash)]
        ds_map = {n: "%s-uid" % n for n in names}
        out = bind.bind_datasources(dash, ds_map)
        self.assertEqual(bind.unbound_refs(out), [])
        self.assertEqual(bind.datasource_variables(out), [])
        for v in out["templating"]["list"]:
            if v.get("type") == "query":
                self.assertEqual(v["datasource"]["uid"], "datasource-uid")


class SetTargetEnvTests(unittest.TestCase):
    def test_custom_variable_existing_option_selected(self):
        out = bind.set_target_env(_dash(), "staging")
        var = bind.find_variable(out, "env")
        self.assertEqual(var["current"],
                         {"selected": True, "text": "Staging",
                          "value": "staging"})
        selected = [o["value"] for o in var["options"] if o["selected"]]
        self.assertEqual(selected, ["staging"])
        # not pinned: the interpolations are kept, variable kept
        self.assertIn("$env", out["panels"][0]["targets"][0]["expr"])

    def test_custom_variable_missing_option_added(self):
        out = bind.set_target_env(_dash(), "qa")
        var = bind.find_variable(out, "env")
        self.assertEqual(var["current"]["value"], "qa")
        self.assertIn({"selected": True, "text": "qa", "value": "qa"},
                      var["options"])
        self.assertIn("qa : qa", var["query"])
        self.assertFalse([o for o in var["options"]
                          if o["value"] == "prod"][0]["selected"])

    def test_env_map_maps_name_to_concrete_value(self):
        out = bind.set_target_env(_dash(), "prod",
                                  env_map={"prod": "production"})
        var = bind.find_variable(out, "env")
        self.assertEqual(var["current"]["value"], "production")
        self.assertEqual(var["current"]["text"], "prod")

    def test_env_map_dict_entry(self):
        value, text = bind.resolve_env(
            "prod", {"prod": {"value": "p", "text": "Prod"}})
        self.assertEqual((value, text), ("p", "Prod"))
        self.assertEqual(bind.resolve_env("x", None), ("x", "x"))

    def test_textbox_variable(self):
        dash = _dash()
        dash["templating"]["list"][-1] = {
            "type": "textbox", "name": "env", "query": "",
            "current": {"selected": False, "text": "", "value": ""},
            "options": []}
        out = bind.set_target_env(dash, "prod")
        var = bind.find_variable(out, "env")
        self.assertEqual(var["query"], "prod")
        self.assertEqual(var["current"]["value"], "prod")

    def test_query_variable_preselected(self):
        dash = _dash()
        dash["templating"]["list"][-1] = {
            "type": "query", "name": "env", "multi": False,
            "query": {"query": "label_values(env)"}, "current": {},
            "options": []}
        out = bind.set_target_env(dash, "prod")
        self.assertEqual(bind.find_variable(out, "env")["current"],
                         {"selected": True, "text": "prod",
                          "value": "prod"})

    def test_missing_variable_created_when_referenced(self):
        dash = _dash()
        dash["templating"]["list"] = [
            v for v in dash["templating"]["list"] if v["name"] != "env"]
        out = bind.set_target_env(dash, "prod")
        var = bind.find_variable(out, "env")
        self.assertIsNotNone(var)
        self.assertEqual(var["type"], "custom")
        self.assertEqual(var["current"]["value"], "prod")

    def test_custom_var_name(self):
        dash = _dash()
        dash["templating"]["list"][-1]["name"] = "environment"
        out = bind.set_target_env(dash, "staging", var_name="environment")
        self.assertEqual(bind.find_variable(out, "environment")
                         ["current"]["value"], "staging")
        self.assertIsNone(bind.find_variable(out, "env"))

    def test_pin_rewrites_every_interpolation_and_drops_var(self):
        src = _dash()
        self.assertEqual(bind.env_refs(src), 4)
        out = bind.set_target_env(src, "prod", pin=True)
        self.assertIsNone(bind.find_variable(out, "env"))
        self.assertEqual(bind.env_refs(out), 0)
        self.assertIn('cluster="acme-cluster-prod"',
                      out["panels"][0]["targets"][0]["expr"])
        self.assertIn('env="prod"', out["panels"][1]["targets"][0]["expr"])
        inner = out["panels"][2]["panels"][0]["targets"][0]
        self.assertEqual(inner["dimensions"]["DBInstanceIdentifier"],
                         ["acme-db-prod"])
        self.assertEqual(out["title"], "Checkout (prod)")
        # other variables untouched
        self.assertIsNotNone(bind.find_variable(out, "app"))

    def test_pin_with_env_map_uses_concrete_value(self):
        out = bind.set_target_env(_dash(), "prod",
                                  env_map={"prod": "production"},
                                  pin=True)
        self.assertIn("acme-cluster-production",
                      out["panels"][0]["targets"][0]["expr"])

    def test_pin_does_not_touch_similar_names(self):
        dash = _dash()
        dash["panels"][0]["targets"][0]["expr"] = \
            "up{a=\"$env\", b=\"$envoy\", c=\"${env_name}\"}"
        out = bind.set_target_env(dash, "prod", pin=True)
        self.assertEqual(out["panels"][0]["targets"][0]["expr"],
                         "up{a=\"prod\", b=\"$envoy\", c=\"${env_name}\"}")

    def test_pin_without_variable_or_refs_is_quiet(self):
        dash = {"title": "x", "panels": []}
        out = bind.set_target_env(dash, "prod", pin=True)
        self.assertEqual(out["templating"]["list"], [])

    def test_input_not_mutated(self):
        src = _dash()
        before = copy.deepcopy(src)
        bind.set_target_env(src, "staging", pin=True)
        self.assertEqual(src, before)

    def test_blank_env_rejected(self):
        with self.assertRaises(ValueError):
            bind.set_target_env(_dash(), "  ")

    def test_bind_then_pin_on_real_dashboard(self):
        with open(SAMPLE, encoding="utf-8") as f:
            nr = parse_nr_dashboard(json.load(f))
        _fn, dash, _rep = build_dashboards(nr, load_config(""))[0]
        names = [v["name"] for v in bind.datasource_variables(dash)]
        out = bind.bind_datasources(dash, {n: "u-" + n for n in names})
        out = bind.set_target_env(out, "staging", pin=True)
        self.assertEqual(bind.unbound_refs(out), [])
        self.assertIsNone(bind.find_variable(out, "env"))
        text = json.dumps(out)
        self.assertNotIn("$env", text)
        self.assertNotIn("${datasource}", text)


if __name__ == "__main__":
    unittest.main()
