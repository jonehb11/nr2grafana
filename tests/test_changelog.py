"""Tests for nr2grafana.changelog.ChangeLog."""

import copy
import unittest

from nr2grafana.changelog import (
    ChangeLog,
    _label_renames,
    _leading_metric,
)
from nr2grafana.config import DEFAULT_CONFIG, _merge


class FakeStore:
    """In-memory stand-in for nr2grafana.store.Store (change-log part)."""

    def __init__(self):
        self.rows = []
        self._id = 0

    def log_change(self, slug, change):
        self._id += 1
        row = dict(change)
        row["id"] = self._id
        row["slug"] = slug
        row["ts"] = "2026-01-01T00:00:%02dZ" % self._id
        self.rows.append(row)
        return self._id

    def list_changes(self, slug=""):
        return [dict(r) for r in self.rows
                if not slug or r["slug"] == slug]


def make_log():
    store = FakeStore()
    return ChangeLog(store), store


class RecordTests(unittest.TestCase):
    def test_record_returns_id_and_persists_fields(self):
        log, store = make_log()
        cid = log.record("svc-dash", "query-edit", "panel:1/A",
                         "up", "sum(up)", why="aggregate", source="ai")
        self.assertEqual(cid, 1)
        row = store.rows[0]
        self.assertEqual(row["slug"], "svc-dash")
        self.assertEqual(row["action"], "query-edit")
        self.assertEqual(row["target"], "panel:1/A")
        self.assertEqual(row["before"], "up")
        self.assertEqual(row["after"], "sum(up)")
        self.assertEqual(row["why"], "aggregate")
        self.assertEqual(row["source"], "ai")

    def test_record_defaults(self):
        log, store = make_log()
        log.record("d", "import", "dashboard", None, "folder/uid")
        self.assertEqual(store.rows[0]["source"], "user")
        self.assertEqual(store.rows[0]["why"], "")


class ReportTests(unittest.TestCase):
    def test_report_groups_by_dashboard(self):
        log, _ = make_log()
        log.record("a", "query-edit", "panel:1/A", "x", "y")
        log.record("b", "import", "dashboard", None, "uid1")
        log.record("a", "panel-edit", "panel:2", "t1", "t2")
        rep = log.report()
        self.assertEqual(rep["schema"], "nr2grafana/changes/v1")
        self.assertEqual(rep["total"], 3)
        slugs = [d["slug"] for d in rep["dashboards"]]
        self.assertEqual(slugs, ["a", "b"])
        self.assertEqual(rep["dashboards"][0]["count"], 2)

    def test_report_slug_filter(self):
        log, _ = make_log()
        log.record("a", "query-edit", "p", "x", "y")
        log.record("b", "query-edit", "p", "x", "y")
        rep = log.report("b")
        self.assertEqual(rep["total"], 1)
        self.assertEqual(rep["dashboards"][0]["slug"], "b")

    def test_report_chronological_order(self):
        log, store = make_log()
        log.record("a", "query-edit", "p1", "x", "y")
        log.record("a", "query-edit", "p2", "x", "y")
        store.rows.reverse()  # storage order should not matter
        rep = log.report("a")
        targets = [c["target"] for c in rep["dashboards"][0]["changes"]]
        self.assertEqual(targets, ["p1", "p2"])


class MarkdownTests(unittest.TestCase):
    def test_empty(self):
        log, _ = make_log()
        md = log.report_markdown()
        self.assertIn("No recorded changes.", md)

    def test_table_rendering(self):
        log, _ = make_log()
        log.record("svc-dash", "query-edit", "panel:1/A",
                   "up", "sum(up)", why="aggregate", source="user")
        md = log.report_markdown()
        self.assertIn("## svc-dash", md)
        self.assertIn("| When | Source | Action | Target | Change | Why |",
                      md)
        self.assertIn("`up` -> `sum(up)`", md)
        self.assertIn("| user | query-edit |", md)
        self.assertIn("aggregate", md)

    def test_long_values_truncated_and_pipes_escaped(self):
        log, _ = make_log()
        long_expr = "sum(rate(a_really_long_metric_name_total{x=\"1\"}" \
                    "[5m])) or vector(0) | something"
        log.record("d", "query-edit", "panel:9/B", long_expr, "up")
        md = log.report_markdown("d")
        self.assertIn("...", md)
        self.assertNotIn(long_expr, md)
        for line in md.splitlines():
            if "panel:9/B" in line:
                # escaped pipes must not add table columns
                self.assertEqual(line.count(" | "), 5)


class LabelRenameTests(unittest.TestCase):
    def test_matcher_rename_high_confidence(self):
        before = 'sum by (service) (rate(m_total{service="api"}[5m]))'
        after = ('sum by (service_name) '
                 '(rate(m_total{service_name="api"}[5m]))')
        renames = _label_renames(before, after)
        self.assertIn(("service", "service_name", "high"), renames)

    def test_group_only_rename_medium_confidence(self):
        before = "sum by (host) (rate(m_total[5m]))"
        after = "sum by (instance) (rate(m_total[5m]))"
        self.assertEqual(_label_renames(before, after),
                         [("host", "instance", "medium")])

    def test_no_rename_when_value_differs(self):
        before = 'm{app="a"}'
        after = 'm{service="b"}'
        self.assertEqual([r for r in _label_renames(before, after)
                          if r[2] == "high"], [])

    def test_logql_stream_selector_rename(self):
        before = '{appname="shop"} |= "error"'
        after = '{service_name="shop"} |= "error"'
        self.assertIn(("appname", "service_name", "high"),
                      _label_renames(before, after))

    def test_suggest_label_map_overlay(self):
        log, _ = make_log()
        log.record("d", "query-edit", "panel:1/A",
                   'rate(x_total{app="web"}[5m])',
                   'rate(x_total{service_name="web"}[5m])',
                   why="label does not exist")
        out = log.suggest_config("d")
        self.assertEqual(out["overlay"]["label_map"],
                         {"app": "service_name"})
        self.assertNotIn("metric_map", out["overlay"])
        rat = out["rationale"][0]
        self.assertEqual(rat["change_id"], 1)
        self.assertEqual(rat["confidence"], "high")
        self.assertIn("label rename", rat["inference"])


class LeadingMetricTests(unittest.TestCase):
    def test_bare_metric(self):
        self.assertEqual(_leading_metric("up"), "up")

    def test_nested_functions_and_grouping(self):
        expr = ("histogram_quantile(0.95, sum by (le) "
                "(rate(foo_bucket{job=\"x\"}[5m])))")
        self.assertEqual(_leading_metric(expr), "foo_bucket")

    def test_logql_pipeline_not_a_metric(self):
        expr = '{service_name="x"} | json | level = "error"'
        self.assertEqual(_leading_metric(expr), "")


class MetricRenameTests(unittest.TestCase):
    def test_suggest_metric_map_overlay(self):
        log, _ = make_log()
        log.record("d", "query-edit", "panel:2/A",
                   "rate(http_server_duration_milliseconds_bucket[5m])",
                   "rate(http_server_request_duration_seconds_bucket"
                   "[5m])")
        out = log.suggest_config("d")
        self.assertEqual(
            out["overlay"]["metric_map"],
            {"http_server_duration_milliseconds_bucket":
             "http_server_request_duration_seconds_bucket"})
        self.assertEqual(out["rationale"][0]["confidence"], "high")

    def test_nrql_metric_key_wins(self):
        log, store = make_log()
        cid = log.record("d", "query-edit", "panel:2/A",
                         "rate(apm_duration_bucket[5m])",
                         "rate(traces_span_metrics_duration_"
                         "milliseconds_bucket[5m])")
        # simulate a producer that attached the original NRQL metric
        store.rows[0]["nrql_metric"] = "apm.service.transaction.duration"
        out = log.suggest_config("d")
        self.assertEqual(
            out["overlay"]["metric_map"],
            {"apm.service.transaction.duration":
             "traces_span_metrics_duration_milliseconds_bucket"})
        self.assertEqual(out["rationale"][0]["change_id"], cid)

    def test_medium_confidence_when_more_than_metric_changed(self):
        log, _ = make_log()
        log.record("d", "query-edit", "p",
                   "rate(old_total[5m])",
                   "sum(rate(new_total[5m]))")
        out = log.suggest_config("d")
        self.assertEqual(out["overlay"]["metric_map"],
                         {"old_total": "new_total"})
        self.assertEqual(out["rationale"][0]["confidence"], "medium")

    def test_label_rename_not_mistaken_for_metric(self):
        log, _ = make_log()
        log.record("d", "query-edit", "p",
                   'sum by (host) (rate(m_total{host="a"}[5m]))',
                   'sum by (instance) (rate(m_total{instance="a"}[5m]))')
        out = log.suggest_config("d")
        self.assertNotIn("metric_map", out["overlay"])
        self.assertEqual(out["overlay"]["label_map"],
                         {"host": "instance"})


class DatasourceSuggestTests(unittest.TestCase):
    def test_uid_from_family_target(self):
        log, _ = make_log()
        log.record("d", "datasource-set", "prometheus",
                   "${datasource}", "mimir")
        out = log.suggest_config("d")
        self.assertEqual(out["overlay"]["datasources"],
                         {"prometheus": {"uid": "mimir"}})
        self.assertEqual(out["rationale"][0]["confidence"], "high")

    def test_uid_from_after_dict_type(self):
        log, _ = make_log()
        log.record("d", "datasource-set", "panel:3",
                   {"type": "loki", "uid": "${loki_datasource}"},
                   {"type": "loki", "uid": "loki-main"})
        out = log.suggest_config("d")
        self.assertEqual(out["overlay"]["datasources"],
                         {"loki": {"uid": "loki-main"}})

    def test_unknown_family_skipped(self):
        log, _ = make_log()
        log.record("d", "datasource-set", "panel:3", "x", "some-uid")
        out = log.suggest_config("d")
        self.assertEqual(out["overlay"], {})
        self.assertEqual(out["rationale"], [])

    def test_last_change_wins(self):
        log, _ = make_log()
        log.record("d", "datasource-set", "tempo", "", "tempo-old")
        log.record("d", "datasource-set", "tempo", "tempo-old",
                   "tempo-new")
        out = log.suggest_config("d")
        self.assertEqual(out["overlay"]["datasources"]["tempo"]["uid"],
                         "tempo-new")


class OverlayMergeTests(unittest.TestCase):
    def test_overlay_merges_over_default_config(self):
        log, _ = make_log()
        log.record("d", "query-edit", "p",
                   'rate(x_total{app="w"}[5m])',
                   'rate(x_total{service_name="w"}[5m])')
        log.record("d", "datasource-set", "prometheus", "", "mimir")
        overlay = log.suggest_config("d")["overlay"]
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        _merge(cfg, overlay)
        self.assertEqual(cfg["label_map"]["app"], "service_name")
        self.assertEqual(cfg["datasources"]["prometheus"]["uid"], "mimir")
        # untouched families keep their defaults
        self.assertEqual(cfg["datasources"]["loki"]["uid"],
                         "${loki_datasource}")

    def test_empty_when_no_changes(self):
        log, _ = make_log()
        out = log.suggest_config()
        self.assertEqual(out, {"overlay": {}, "rationale": []})


if __name__ == "__main__":
    unittest.main()
