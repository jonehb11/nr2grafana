"""Tests for nr2grafana.grafana.validate.validate_dashboard."""

import copy
import json
import os
import unittest

from nr2grafana.config import load_config
from nr2grafana.grafana.builder import build_dashboards
from nr2grafana.grafana.validate import validate_dashboard, _balanced
from nr2grafana.model import parse_nr_dashboard

FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures", "newrelic", "sample-service-dashboard.json")


def make_panel(pid=1, ptype="timeseries", expr="up", ds=None, refid="A"):
    return {
        "id": pid, "type": ptype, "title": "P%d" % pid,
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        "targets": [{
            "refId": refid,
            "datasource": ds or {"type": "prometheus", "uid": "mimir"},
            "expr": expr,
        }],
    }


def make_dash(**overrides):
    dash = {
        "id": None,
        "uid": "test-dash",
        "title": "Test Dashboard",
        "schemaVersion": 39,
        "panels": [make_panel()],
        "templating": {"list": []},
    }
    dash.update(overrides)
    return dash


class ValidDashboardTests(unittest.TestCase):
    def test_minimal_valid_dashboard(self):
        self.assertEqual(validate_dashboard(make_dash()), [])

    def test_converter_output_passes(self):
        with open(FIXTURE, encoding="utf-8") as f:
            nr = parse_nr_dashboard(json.load(f))
        for strategy in ("rows", "split"):
            cfg = load_config()
            cfg["page_strategy"] = strategy
            for _, dash, _ in build_dashboards(nr, cfg):
                self.assertEqual(validate_dashboard(dash), [],
                                 "strategy=%s" % strategy)

    def test_not_a_dict(self):
        self.assertEqual(validate_dashboard([]),
                         ["dashboard is not a JSON object"])


class SchemaLevelTests(unittest.TestCase):
    def test_empty_title(self):
        errs = validate_dashboard(make_dash(title="   "))
        self.assertIn("dashboard title is empty", errs)

    def test_bad_uid(self):
        errs = validate_dashboard(make_dash(uid="has spaces!"))
        self.assertTrue(any("uid" in e and "invalid" in e for e in errs))

    def test_uid_too_long(self):
        errs = validate_dashboard(make_dash(uid="x" * 41))
        self.assertTrue(any("invalid" in e for e in errs))

    def test_none_uid_allowed(self):
        self.assertEqual(validate_dashboard(make_dash(uid=None)), [])

    def test_nonnull_id_rejected(self):
        errs = validate_dashboard(make_dash(id=42))
        self.assertTrue(any("'id' must be null" in e for e in errs))

    def test_missing_schema_version(self):
        d = make_dash()
        del d["schemaVersion"]
        errs = validate_dashboard(d)
        self.assertIn("schemaVersion missing", errs)

    def test_panels_not_a_list(self):
        errs = validate_dashboard(make_dash(panels={}))
        self.assertIn("panels must be a list", errs)


class PanelTests(unittest.TestCase):
    def test_duplicate_panel_ids(self):
        d = make_dash(panels=[make_panel(1), make_panel(1)])
        errs = validate_dashboard(d)
        self.assertTrue(any("duplicate panel id" in e for e in errs))

    def test_duplicate_id_inside_collapsed_row(self):
        row = {"id": 1, "type": "row", "title": "R",
               "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1},
               "panels": [make_panel(1)]}
        errs = validate_dashboard(make_dash(panels=[row]))
        self.assertTrue(any("duplicate panel id" in e for e in errs))

    def test_missing_panel_id(self):
        p = make_panel()
        del p["id"]
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("id missing or not an int" in e for e in errs))

    def test_missing_panel_type(self):
        p = make_panel()
        p["type"] = ""
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("missing panel type" in e for e in errs))

    def test_gridpos_out_of_bounds(self):
        p = make_panel()
        p["gridPos"] = {"x": 20, "y": 0, "w": 8, "h": 8}
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("out of 24-column bounds" in e for e in errs))

    def test_gridpos_missing_key(self):
        p = make_panel()
        p["gridPos"] = {"x": 0, "y": 0, "w": 8}
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("gridPos.h missing" in e for e in errs))

    def test_gridpos_zero_height(self):
        p = make_panel()
        p["gridPos"] = {"x": 0, "y": 0, "w": 8, "h": 0}
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("gridPos.h < 1" in e for e in errs))

    def test_missing_refid(self):
        p = make_panel()
        del p["targets"][0]["refId"]
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("target missing refId" in e for e in errs))

    def test_duplicate_refid(self):
        p = make_panel()
        p["targets"].append(dict(p["targets"][0]))
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("duplicate refId A" in e for e in errs))

    def test_malformed_datasource_ref(self):
        p = make_panel(ds={"type": "prometheus"})  # uid missing
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("datasource ref malformed" in e for e in errs))

    def test_missing_expr(self):
        p = make_panel()
        del p["targets"][0]["expr"]
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("has no expr/query" in e for e in errs))

    def test_unbalanced_brackets_in_expr(self):
        p = make_panel(expr="sum(rate(up[5m])")
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("unbalanced" in e for e in errs))

    def test_unbalanced_quote_in_expr(self):
        p = make_panel(expr='up{job="x}')
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("unbalanced" in e for e in errs))

    def test_empty_loki_stream_selector(self):
        p = make_panel(expr='{} |= "boom"',
                       ds={"type": "loki", "uid": "loki"})
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("empty stream selector" in e for e in errs))

    def test_loki_selector_with_labels_ok(self):
        p = make_panel(expr='{service_name="x"} |= "boom"',
                       ds={"type": "loki", "uid": "loki"})
        self.assertEqual(validate_dashboard(make_dash(panels=[p])), [])

    def test_text_and_row_panels_skip_target_checks(self):
        text = {"id": 1, "type": "text", "title": "T",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 4}}
        row = {"id": 2, "type": "row", "title": "R",
               "gridPos": {"x": 0, "y": 4, "w": 24, "h": 1}}
        self.assertEqual(validate_dashboard(make_dash(panels=[text, row])),
                         [])


class TemplatingTests(unittest.TestCase):
    def test_invalid_variable_name(self):
        d = make_dash(templating={"list": [
            {"name": "bad name", "type": "custom"}]})
        errs = validate_dashboard(d)
        self.assertTrue(any("template variable name" in e for e in errs))

    def test_duplicate_variable(self):
        d = make_dash(templating={"list": [
            {"name": "v", "type": "custom"},
            {"name": "v", "type": "custom"}]})
        errs = validate_dashboard(d)
        self.assertTrue(any("duplicate template variable" in e for e in errs))

    def test_variable_missing_type(self):
        d = make_dash(templating={"list": [{"name": "v"}]})
        errs = validate_dashboard(d)
        self.assertTrue(any("missing type" in e for e in errs))

    def test_missing_datasource_variable_for_uid_ref(self):
        p = make_panel(ds={"type": "prometheus", "uid": "${datasource}"})
        errs = validate_dashboard(make_dash(panels=[p]))
        self.assertTrue(any("no such datasource variable" in e for e in errs))

    def test_datasource_variable_satisfies_uid_ref(self):
        p = make_panel(ds={"type": "prometheus", "uid": "${datasource}"})
        d = make_dash(panels=[p], templating={"list": [
            {"name": "datasource", "type": "datasource",
             "query": "prometheus"}]})
        self.assertEqual(validate_dashboard(d), [])


class BalancedHelperTests(unittest.TestCase):
    def test_balanced_expressions(self):
        self.assertTrue(_balanced('sum(rate(up{job="x"}[5m]))'))
        self.assertTrue(_balanced('{a="}"}'))  # brace inside string
        self.assertTrue(_balanced(""))

    def test_unbalanced_expressions(self):
        self.assertFalse(_balanced("sum(up"))
        self.assertFalse(_balanced("sum)up("))
        self.assertFalse(_balanced("a[1)"))
        self.assertFalse(_balanced('"unterminated'))


if __name__ == "__main__":
    unittest.main()
