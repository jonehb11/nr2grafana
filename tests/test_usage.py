"""Tests for nr2grafana.usage (the KEEP set the dashboards need)."""

import unittest

from nr2grafana import usage


def _panel(pid, ds_type, uid, **extra):
    tgt = {"refId": "A", "datasource": {"type": ds_type, "uid": uid}}
    tgt.update(extra)
    return {"id": pid, "targets": [tgt]}


def _dash(*panels):
    return {"title": "D", "uid": "d1", "panels": list(panels)}


class CollectUsageTests(unittest.TestCase):
    def setUp(self):
        self.dash = _dash(
            _panel(1, "prometheus", "p",
                   expr='sum by (job, env) '
                        '(rate(http_requests_total{env="prod"}[5m]))'),
            _panel(2, "prometheus", "p",
                   expr='histogram_quantile(0.9, '
                        'sum by (le) (rate(latency_seconds_bucket[5m])))'),
            _panel(3, "loki", "l",
                   expr='sum by (level) (rate({app="api", '
                        'namespace=~"prod|stage"} |= "err" [5m]))'),
            _panel(4, "tempo", "t",
                   query='{ resource.service.name = "api" && '
                         'span.http.status_code = 500 }'),
        )
        self.u = usage.collect_usage(self.dash)

    def test_schema(self):
        self.assertEqual(self.u["schema"], "nr2grafana/usage/v1")
        self.assertEqual(self.u["dashboards"], 1)

    def test_prometheus_metrics_and_labels(self):
        prom = self.u["prometheus"]
        self.assertIn("http_requests_total", prom["metrics"])
        self.assertIn("latency_seconds_bucket", prom["metrics"])
        # labels from matcher and by()
        self.assertIn("job", prom["labels"])
        self.assertIn("env", prom["labels"])
        self.assertIn("le", prom["labels"])

    def test_loki_stream_labels_used_labels_values(self):
        loki = self.u["loki"]
        self.assertEqual(loki["stream_labels"], ["app", "namespace"])
        # by(level) grouping is a used label but not a stream selector
        self.assertIn("level", loki["used_labels"])
        self.assertIn("app", loki["used_labels"])
        self.assertEqual(loki["filtered_values"]["app"], ["api"])
        # regex alternation is split into concrete values
        self.assertEqual(loki["filtered_values"]["namespace"],
                         ["prod", "stage"])

    def test_tempo_queries_and_attrs(self):
        tempo = self.u["tempo"]
        self.assertEqual(len(tempo["traceql"]), 1)
        self.assertIn("service.name", " ".join(tempo["labels"]) + " "
                      + str(tempo["labels"]))

    def test_single_dict_or_list_accepted(self):
        as_list = usage.collect_usage([self.dash])
        self.assertEqual(as_list["prometheus"]["metrics"],
                         self.u["prometheus"]["metrics"])

    def test_empty_input(self):
        u = usage.collect_usage([])
        self.assertEqual(u["prometheus"]["metrics"], [])
        self.assertEqual(u["loki"]["stream_labels"], [])
        self.assertEqual(u["dashboards"], 0)

    def test_multiple_dashboards_union(self):
        d2 = _dash(_panel(9, "prometheus", "p", expr="other_metric_total"))
        u = usage.collect_usage([self.dash, d2])
        self.assertIn("http_requests_total", u["prometheus"]["metrics"])
        self.assertIn("other_metric_total", u["prometheus"]["metrics"])
        self.assertEqual(u["dashboards"], 2)


class KeepSetTests(unittest.TestCase):
    def test_prometheus_keep_set_includes_histogram_siblings(self):
        u = usage.collect_usage(_dash(
            _panel(1, "prometheus", "p",
                   expr="rate(latency_seconds_bucket[5m])")))
        keep = usage.prometheus_keep_set(u)
        # a dashboard using _bucket must keep the whole histogram family
        for name in ("latency_seconds", "latency_seconds_bucket",
                     "latency_seconds_sum", "latency_seconds_count"):
            self.assertIn(name, keep)

    def test_loki_label_keep_set_is_union(self):
        u = usage.collect_usage(_dash(
            _panel(1, "loki", "l",
                   expr='sum by (level) (rate({app="x"}[5m]))')))
        keep = usage.loki_label_keep_set(u)
        self.assertIn("app", keep)     # stream selector label
        self.assertIn("level", keep)   # by() grouping label


if __name__ == "__main__":
    unittest.main()
