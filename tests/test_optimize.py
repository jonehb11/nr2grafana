"""Tests for nr2grafana.optimize (the recommendation engine).

The central assertion is the SAFETY guarantee: the engine must never emit
an auto-safe drop for a metric/label the migrated dashboards use. Used but
costly dimensions may only appear as needs-review recommendations.
"""

import unittest

from nr2grafana import optimize, usage


# A converted dashboard, so usage is computed by the real collector.
_DASH = {
    "title": "D", "uid": "d1", "panels": [
        {"id": 1, "targets": [{
            "refId": "A",
            "datasource": {"type": "prometheus", "uid": "p"},
            "expr": 'sum by (job, pod) '
                    '(rate(http_requests_total{env="prod"}[5m]))'}]},
        {"id": 2, "targets": [{
            "refId": "A",
            "datasource": {"type": "loki", "uid": "l"},
            "expr": 'sum by (level) (rate({app="api", '
                    'namespace="prod"} |= "err" [5m]))'}]},
    ],
}

# Traffic: several deliberately-unused, high-series / high-cardinality
# dimensions plus the ones the dashboard actually uses.
_TRAFFIC = {
    "schema": "nr2grafana/traffic/v1",
    "datasources": [
        {"family": "prometheus", "uid": "p", "prometheus": {
            "active_series": 50000,
            "top_metrics": [
                {"metric": "http_requests_total", "series": 8000},
                {"metric": "go_gc_duration_seconds", "series": 20000},
                {"metric": "unused_debug_metric", "series": 15000},
            ],
            "label_cardinality": [
                {"label": "job", "values": 12},
                {"label": "pod", "values": 4213},        # USED + high-card
                {"label": "container_id", "values": 6000},  # unused
            ],
            "histogram_series": 30000,
        }},
        {"family": "loki", "uid": "l", "loki": {
            "streams": 8000,
            "bytes_window": 100000000000.0,
            "bytes_per_day": 100000000000.0,
            "top_streams": [
                {"labels": {"app": "noisy", "level": "debug"},
                 "bytes": 60000000000.0},
                {"labels": {"app": "api", "namespace": "prod"},
                 "bytes": 20000000000.0},
            ],
            "label_cardinality": [
                {"label": "app", "values": 20},
                {"label": "namespace", "values": 8},
                {"label": "trace_id", "values": 900000},   # id-like unused
                {"label": "container", "values": 30},       # unused churn
            ],
        }},
        {"family": "tempo", "uid": "t", "tempo": {"note": "best-effort"}},
    ],
}


class RecommendTests(unittest.TestCase):
    def setUp(self):
        self.usage = usage.collect_usage(_DASH)
        self.pricing = {
            "mimir_per_1k_series_month": 0.60,
            "loki_ingest_per_gb": 0.50,
            "loki_store_per_gb_month": 0.03,
            "loki_retention_days": 30,
            "loki_per_1k_streams_month": 0.20,
        }
        self.out = optimize.recommend(_TRAFFIC, self.usage,
                                      pricing=self.pricing)
        self.recs = self.out["recommendations"]

    def _by_kind(self, kind):
        return [r for r in self.recs if r["kind"] == kind]

    def test_schema_and_summary(self):
        self.assertEqual(self.out["schema"], "nr2grafana/optimize/v1")
        s = self.out["summary"]
        self.assertEqual(s["count"], len(self.recs))
        self.assertGreater(s["total_est_monthly_usd"], 0)
        self.assertIn("prometheus", s["by_family"])
        self.assertIn("loki", s["by_family"])

    def test_all_kinds_present(self):
        kinds = {r["kind"] for r in self.recs}
        for kind in ("drop-metric", "drop-label", "to-structured-metadata",
                     "recommend-stream-labels", "retention", "drop-line",
                     "histogram", "sampling"):
            self.assertIn(kind, kinds, "missing kind %s" % kind)

    # ---- THE safety guarantee -------------------------------------

    def test_never_auto_drops_a_used_metric_or_label(self):
        prom_keep = usage.prometheus_keep_set(self.usage)
        loki_keep = usage.loki_label_keep_set(self.usage)
        prom_labels = set(self.usage["prometheus"]["labels"])
        for r in self.recs:
            ev = r.get("evidence") or {}
            is_auto_drop = (r["kind"] in ("drop-metric", "drop-label",
                                          "drop-line") and r["keeps_intact"])
            if is_auto_drop:
                if "metric" in ev:
                    self.assertNotIn(ev["metric"], prom_keep)
                if "label" in ev and r["family"] == "loki":
                    self.assertNotIn(ev["label"], loki_keep)
                if "label" in ev and r["family"] == "prometheus":
                    self.assertNotIn(ev["label"], prom_labels)

    def test_used_dimensions_are_needs_review_not_safe(self):
        # `pod` is used by the dashboard AND high-cardinality: it must
        # surface only as a needs-review rec, never an auto-safe drop.
        prom_labels = set(self.usage["prometheus"]["labels"])
        self.assertIn("pod", prom_labels)
        pod_recs = [r for r in self.recs
                    if (r.get("evidence") or {}).get("label") == "pod"
                    and r["family"] == "prometheus"]
        self.assertTrue(pod_recs)
        for r in pod_recs:
            self.assertFalse(r["keeps_intact"])
            self.assertTrue(r["needs_review"])
            # and its config must NOT contain a real labeldrop/drop action
            blob = " ".join(c["snippet"] for c in r["config"])
            self.assertNotIn("labeldrop", blob)
            self.assertNotIn("action: drop", blob)

    def test_unused_metrics_are_dropped_and_safe(self):
        dropped = {r["evidence"]["metric"]
                   for r in self._by_kind("drop-metric")
                   if "metric" in (r.get("evidence") or {})}
        self.assertIn("go_gc_duration_seconds", dropped)
        self.assertIn("unused_debug_metric", dropped)
        self.assertNotIn("http_requests_total", dropped)  # used
        for r in self._by_kind("drop-metric"):
            self.assertTrue(r["keeps_intact"])

    def test_keep_list_present_and_covers_used(self):
        keeplist = [r for r in self._by_kind("drop-metric")
                    if r["id"].startswith("prom-keep-list")]
        self.assertEqual(len(keeplist), 1)
        blob = " ".join(c["snippet"] for c in keeplist[0]["config"])
        self.assertIn("action: keep", blob)
        # keep-list keeps the used metric
        self.assertIn("http_requests_total", blob)

    # ---- id-like / structured metadata -----------------------------

    def test_id_like_label_goes_to_structured_metadata(self):
        sm = self._by_kind("to-structured-metadata")
        labels = {r["evidence"].get("label") for r in sm}
        self.assertIn("trace_id", labels)
        trace = [r for r in sm if r["evidence"].get("label") == "trace_id"]
        self.assertTrue(trace[0]["keeps_intact"])  # unused id -> safe move
        blob = " ".join(c["snippet"] for c in trace[0]["config"])
        self.assertIn("structured_metadata", blob)

    # ---- Loki volume hotspots --------------------------------------

    def test_debug_stream_dropped_at_agent(self):
        drop_lines = self._by_kind("drop-line")
        self.assertTrue(drop_lines)
        r = drop_lines[0]
        self.assertTrue(r["keeps_intact"])
        blob = " ".join(c["snippet"] for c in r["config"])
        self.assertIn("pipeline_stages", blob)
        self.assertIn("action: drop", blob)
        self.assertGreater(r["est_savings"]["gb_per_day"], 0)

    def test_retention_rec_for_used_stream_is_needs_review(self):
        ret = self._by_kind("retention")
        self.assertTrue(ret)
        for r in ret:
            self.assertTrue(r["needs_review"])
            blob = " ".join(c["snippet"] for c in r["config"])
            self.assertIn("retention_stream", blob)

    def test_recommended_stream_label_set(self):
        rec = self._by_kind("recommend-stream-labels")[0]
        # only low-cardinality, dashboard-used labels
        self.assertEqual(sorted(rec["evidence"]["recommended"]),
                         ["app", "namespace"])

    # ---- savings math wiring ---------------------------------------

    def test_drop_metric_savings_math(self):
        # series / 1000 * $/1k-series-month
        r = [x for x in self._by_kind("drop-metric")
             if x.get("evidence", {}).get("metric")
             == "go_gc_duration_seconds"][0]
        self.assertEqual(r["est_savings"]["series"], 20000)
        self.assertAlmostEqual(r["est_savings"]["monthly_usd"],
                               20000 / 1000.0 * 0.60, places=2)

    def test_dropline_savings_uses_ingest_and_storage(self):
        r = self._by_kind("drop-line")[0]
        gb_day = r["est_savings"]["gb_per_day"]
        expected = gb_day * 30 * 0.50 + gb_day * 30 * 0.03
        self.assertAlmostEqual(r["est_savings"]["monthly_usd"], expected,
                               places=1)

    def test_pricing_scales_savings(self):
        cheap = optimize.recommend(_TRAFFIC, self.usage,
                                   pricing={"mimir_per_1k_series_month":
                                            0.60})
        dear = optimize.recommend(_TRAFFIC, self.usage,
                                  pricing={"mimir_per_1k_series_month":
                                           6.00})

        def metric_usd(out):
            r = [x for x in out["recommendations"]
                 if x.get("evidence", {}).get("metric")
                 == "go_gc_duration_seconds"][0]
            return r["est_savings"]["monthly_usd"]
        self.assertAlmostEqual(metric_usd(dear), metric_usd(cheap) * 10,
                               places=1)

    def test_every_rec_has_savings_and_config(self):
        for r in self.recs:
            self.assertIn("monthly_usd", r["est_savings"])
            self.assertIn("confidence", r["est_savings"])
            self.assertTrue(r["config"])
            for c in r["config"]:
                self.assertIn("target", c)
                self.assertIn("snippet", c)
                self.assertTrue(c["snippet"].strip())

    def test_cost_pricing_used_when_no_explicit_pricing(self):
        cost = {"schema": "nr2grafana/cost/v1",
                "pricing": {"mimir_per_1k_series_month": 3.00}}
        out = optimize.recommend(_TRAFFIC, self.usage, cost=cost)
        r = [x for x in out["recommendations"]
             if x.get("evidence", {}).get("metric")
             == "go_gc_duration_seconds"][0]
        self.assertAlmostEqual(r["est_savings"]["monthly_usd"],
                               20000 / 1000.0 * 3.00, places=2)


class SafetyEdgeCaseTests(unittest.TestCase):
    def test_used_debug_level_prevents_dropline(self):
        # If a dashboard filters on level="debug", the debug stream must
        # NOT be auto-dropped -- it becomes a needs-review retention rec.
        dash = {"title": "D", "panels": [{"id": 1, "targets": [{
            "datasource": {"type": "loki", "uid": "l"},
            "expr": '{app="noisy", level="debug"} | json'}]}]}
        u = usage.collect_usage(dash)
        out = optimize.recommend(_TRAFFIC, u)
        for r in out["recommendations"]:
            if r["kind"] == "drop-line":
                sel = r["evidence"].get("selector", "")
                self.assertNotIn("noisy", sel)

    def test_no_datasources_yields_empty(self):
        out = optimize.recommend({"datasources": []}, {})
        self.assertEqual(out["recommendations"], [])
        self.assertEqual(out["summary"]["count"], 0)

    def test_never_raises_on_garbage(self):
        out = optimize.recommend(
            {"datasources": [{"family": "prometheus", "uid": "p",
                              "prometheus": None}, "notadict"]}, {})
        self.assertEqual(out["schema"], "nr2grafana/optimize/v1")

    def test_no_pricing_still_produces_dollars(self):
        out = optimize.recommend(_TRAFFIC, usage.collect_usage(_DASH))
        self.assertGreater(out["summary"]["total_est_monthly_usd"], 0)


if __name__ == "__main__":
    unittest.main()
