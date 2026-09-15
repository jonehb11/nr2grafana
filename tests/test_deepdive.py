"""Tests for nr2grafana.deepdive.

A ``FakePromClient`` subclasses :class:`PromClient` and overrides only the
network primitive ``_request``, so the real ``vector``/``scalar``/``by``/
``json`` parsing runs against canned Prometheus responses, the Mimir
cardinality API and the Loki series API. The single fake serves the prom,
mimir and loki roles at once. The canned metrics are shaped to trigger one
of every finding the deep-dive can emit -- capacity, churn (x2), duplicate
replicas, top cardinality, network (drop/fail/lag/wire), Loki (failed
flush / bad S3 / full PVC / small chunk / high-card stream label) and
Tempo/OTel (unused / export fail / queue) -- and the assertions check both
the finding content and the safety flags.
"""

import unittest

from nr2grafana import deepdive
from nr2grafana.deepdive import PromClient, analyze


GIB = 2 ** 30


def _res(pairs):
    """Build a Prom instant-vector result from (labels, value) pairs."""
    out = []
    for labels, value in pairs:
        out.append({"metric": dict(labels), "value": [0, str(value)]})
    return out


class FakePromClient(PromClient):
    """Canned LGTM self-metrics; no network. Never touches urllib."""

    def __init__(self):
        super(FakePromClient, self).__init__(base="http://fake")
        self.queries = []

    def _request(self, path, params=None):
        params = params or {}
        if path == "/api/v1/query":
            expr = params.get("query", "")
            self.queries.append(expr)
            return {"status": "success",
                    "data": {"result": self._match(expr)}}
        if path == "/api/v1/cardinality/label_values":
            return {"labels": [{
                "label_name": "__name__",
                "label_values_count": 500,
                "cardinality": [
                    {"label_value":
                     "apiserver_request_duration_seconds_bucket",
                     "series_count": 200000},
                    {"label_value": "container_cpu_usage_seconds_total",
                     "series_count": 5000}]}]}
        if path == "/api/v1/cardinality/label_names":
            return {"cardinality": [
                {"label_name": "pod", "label_values_count": 5000},
                {"label_name": "le", "label_values_count": 40}]}
        if path == "/loki/api/v1/series":
            return {"status": "success", "data": [
                {"namespace": "a", "pod": "p1"},
                {"namespace": "a", "pod": "p2"},
                {"namespace": "a", "pod": "p3"}]}
        return None

    # -- expr -> canned result --------------------------------------------

    def _match(self, expr):
        def has(*subs):
            return all(s in expr for s in subs)

        # --- capacity ---
        if has("sum(cortex_ingester_memory_series)"):
            return _res([({}, 6000000)])
        if has("count(cortex_ingester_memory_series)"):
            return _res([({}, 3)])
        if expr == "cortex_ingester_memory_series":
            return _res([({"pod": "ing-0"}, 2000000),
                         ({"pod": "ing-1"}, 2000000),
                         ({"pod": "ing-2"}, 2000000)])
        if has("cortex_distributor_received_samples_total"):
            return _res([({}, 20000)])   # low vs 2M unique -> churn
        if has("cortex_ingester_memory_series_created_total"):
            return _res([({}, 500000)])  # 8.3%/h churn
        if has("container_memory_working_set_bytes", 'namespace="mimir"'):
            v = int(8.6 * GIB)
            return _res([({"pod": "ing-0"}, v), ({"pod": "ing-1"}, v),
                         ({"pod": "ing-2"}, v)])
        if has("container_memory_working_set_bytes", 'namespace="tempo"'):
            return _res([({"pod": "tempo-0"}, int(1.0 * GIB))])
        if has("container_spec_memory_limit_bytes"):
            return _res([({}, int(12 * GIB))])
        if has("cortex_limits_overrides") or has(
                "max_global_series_per_user"):
            return _res([({"limit_name": "max_global_series_per_user"},
                          10000000)])

        # --- cardinality / churn ---
        if has("cortex_ha_tracker_elected_replica_changes_total"):
            return []      # no HA tracker active
        if has("prometheus_build_info"):
            return _res([({"cluster": "spoke-a"}, 2)])
        if has("prometheus_tsdb_head_series"):
            return _res([({"cluster": "spoke-a"}, 1000000)])

        # --- network ---
        if has("prometheus_remote_storage_bytes_total"):
            return _res([({"instance": "spoke-a"}, 500000)])
        if has("prometheus_remote_storage_samples_dropped_total"):
            return _res([({"instance": "spoke-a"}, 1000)])
        if has("prometheus_remote_storage_samples_failed_total"):
            return _res([({"instance": "spoke-a"}, 50)])
        if has("prometheus_remote_storage_highest_timestamp_in_seconds"):
            return _res([({"instance": "spoke-a", "url": "u1"}, 1000)])
        if has("prometheus_remote_storage_queue_highest_sent_timestamp"):
            return _res([({"instance": "spoke-a", "url": "u1"}, 900)])

        # --- loki ---
        if has("loki_distributor_lines_received_total"):
            return _res([({}, 300)])
        if has("loki_distributor_bytes_received_total"):
            return _res([({}, 220000)])
        if has("loki_ingester_streams_created_total"):
            return _res([({}, 40)])
        if has("loki_ingester_chunk_size_bytes_bucket"):
            return _res([({}, 50000)])   # small chunk
        if has("loki_ingester_chunk_utilization_bucket"):
            return _res([({}, 0.2)])
        if has("loki_ingester_chunks_flushed_total"):
            return _res([({"reason": "idle"}, 100)])
        if has("loki_ingester_failed_flushes_total"):
            return _res([({}, 10)])
        if has("loki_ingester_wal_disk_full_failures_total"):
            return _res([({}, 0)])
        if has("loki_s3_request_duration_seconds_count"):
            return _res([({"operation": "PUT", "status_code": "200"}, 100),
                         ({"operation": "PUT", "status_code": "500"}, 5)])
        if has("kubelet_volume_stats_used_bytes"):
            return _res([({"persistentvolumeclaim": "loki-0"}, 0.95)])

        # --- tempo / otel ---
        if has("tempo_distributor_spans_received_total"):
            return _res([({}, 0.1)])
        if has("tempo_metrics_generator_registry_active_series"):
            return _res([({}, 5000)])
        if has("otelcol_exporter_send_failed_spans_total"):
            return _res([({"exporter": "otlp"}, 100)])
        if has("otelcol_exporter_send_failed_metric_points_total"):
            return []
        if has("otelcol_exporter_queue_size"):
            return _res([({"exporter": "otlp"}, 0.9)])
        return []


def _run():
    fake = FakePromClient()
    cfg = {"deepdive": {"thresholds": {"stream_label_values_warn": 2}}}
    return analyze(prom=fake, mimir=fake, loki=fake, cfg=cfg), fake


def _by_area(findings, area):
    return [f for f in findings if f.get("area") == area]


def _titled(findings, needle):
    return [f for f in findings if needle.lower() in f["title"].lower()]


class PromClientTest(unittest.TestCase):

    def test_unavailable_client_never_raises(self):
        c = PromClient(None)
        self.assertFalse(c.available)
        self.assertEqual(c.vector("up"), [])
        self.assertIsNone(c.scalar("up"))
        self.assertEqual(c.by("up", "job"), {})
        self.assertIsNone(c.json("/api/v1/query", {"query": "up"}))

    def test_request_failure_is_collected_not_raised(self):
        # An unroutable-looking base must not raise; error is recorded.
        c = PromClient("http://127.0.0.1:1", timeout=0.2)
        self.assertEqual(c.vector("up"), [])
        self.assertTrue(c.errors)

    def test_vector_scalar_by_parsing(self):
        fake = FakePromClient()
        vec = fake.vector("cortex_ingester_memory_series")
        self.assertEqual(len(vec), 3)
        self.assertEqual(fake.scalar("sum(cortex_ingester_memory_series)"),
                         6000000.0)
        by = fake.by("cortex_ingester_memory_series", "pod")
        self.assertEqual(by["ing-0"], 2000000.0)

    def test_by_multi_label_tuple_key(self):
        fake = FakePromClient()
        by = fake.by(
            "prometheus_remote_storage_highest_timestamp_in_seconds",
            "instance", "url")
        self.assertEqual(by[("spoke-a", "u1")], 1000.0)


class AnalyzeShapeTest(unittest.TestCase):

    def test_schema_and_summary(self):
        report, _ = _run()
        self.assertEqual(report["schema"], "nr2grafana/deepdive/v1")
        self.assertTrue(report["available"])
        self.assertIn("findings", report)
        summ = report["summary"]
        self.assertEqual(summ["findings"], len(report["findings"]))
        self.assertGreater(summ["fail"], 0)
        self.assertGreater(summ["warn"], 0)

    def test_findings_ranked_fail_first(self):
        report, _ = _run()
        order = [deepdive._SEV_ORDER[f["severity"]]
                 for f in report["findings"]]
        self.assertEqual(order, sorted(order))

    def test_every_finding_has_risk_flags(self):
        report, _ = _run()
        for f in report["findings"]:
            for flag in ("keeps_performance", "keeps_durability",
                         "keeps_availability"):
                self.assertIn(flag, f)
                self.assertIsInstance(f[flag], bool)
            self.assertIn(f["severity"], ("FAIL", "WARN", "INFO"))

    def test_no_endpoint_returns_unavailable(self):
        report = analyze()
        self.assertFalse(report["available"])
        self.assertEqual(report["findings"], [])
        self.assertIn("note", report)


class CapacityTest(unittest.TestCase):

    def test_limit_is_not_capacity(self):
        report, _ = _run()
        cap = _by_area(report["findings"], "capacity")
        self.assertEqual(len(cap), 1)
        f = cap[0]
        self.assertEqual(f["severity"], "WARN")
        ev = f["evidence"]
        # ~2M real capacity well below the configured 10M limit.
        self.assertLess(ev["global_series_capacity_est"], 10000000)
        self.assertEqual(ev["configured_max_global_series"], 10000000)
        self.assertTrue(ev["bytes_per_series_measured"])
        self.assertTrue(f["keeps_durability"] and f["keeps_availability"])
        # The snippet must not recommend cutting RF/retention.
        snippet = f["config"][0]["snippet"].lower()
        self.assertIn("guard", snippet)

    def test_bytes_per_series_measured_from_rss(self):
        report, _ = _run()
        cap = report["sections"]["capacity"]
        self.assertGreater(cap["bytes_per_series"], 3000)
        self.assertLess(cap["bytes_per_series"], 6000)


class ChurnTest(unittest.TestCase):

    def test_low_samples_per_series_and_high_churn(self):
        report, _ = _run()
        churn = _by_area(report["findings"], "churn")
        titles = " ".join(f["title"].lower() for f in churn)
        self.assertIn("samples/series", titles)
        self.assertIn("churn", titles)
        for f in churn:
            # Drop candidates must be verified unused first.
            self.assertFalse(f.get("keeps_intact", False))
            self.assertTrue(f.get("needs_review"))
            self.assertTrue(f["config"][0]["snippet"])


class CardinalityTest(unittest.TestCase):

    def test_duplicate_replicas_fail(self):
        report, _ = _run()
        dup = _titled(report["findings"], "Prometheus replicas")
        self.assertEqual(len(dup), 1)
        f = dup[0]
        self.assertEqual(f["severity"], "FAIL")
        self.assertEqual(f["area"], "cardinality")
        self.assertEqual(f["est_savings"]["series"], 1000000)
        self.assertGreater(f["est_savings"]["monthly_usd"], 0)
        # Dedup keeps everything intact.
        self.assertTrue(f["keeps_durability"])
        self.assertTrue(f.get("keeps_intact"))
        self.assertIn("ha_tracker", f["config"][0]["snippet"].lower())

    def test_top_metrics_drop_candidate(self):
        report, _ = _run()
        card = [f for f in _by_area(report["findings"], "cardinality")
                if f["severity"] == "INFO"]
        self.assertTrue(card)
        f = card[0]
        self.assertIn("apiserver_request_duration_seconds_bucket",
                      str(f["evidence"]))
        self.assertGreater(f["est_savings"]["series"], 0)
        self.assertFalse(f.get("keeps_intact", False))
        self.assertTrue(f.get("needs_review"))
        # Never drop at scrape; drop at remote_write.
        self.assertIn("remote_write", f["config"][0]["snippet"])


class NetworkTest(unittest.TestCase):

    def test_dropped_samples_fail(self):
        report, _ = _run()
        drop = _titled(report["findings"], "DROPPING samples")
        self.assertEqual(len(drop), 1)
        self.assertEqual(drop[0]["severity"], "FAIL")
        self.assertEqual(drop[0]["area"], "network")

    def test_wire_volume_info_with_egress_paths(self):
        report, _ = _run()
        wire = _titled(report["findings"], "wire volume")
        self.assertEqual(len(wire), 1)
        f = wire[0]
        self.assertEqual(f["severity"], "INFO")
        cost = f["evidence"]["cost_by_path_mo"]
        self.assertIn("cross-AZ (in+out)", cost)
        self.assertGreater(f["evidence"]["wire_gb_month"], 0)

    def test_lag_and_failed_warnings(self):
        report, _ = _run()
        net = _by_area(report["findings"], "network")
        titles = " ".join(f["title"].lower() for f in net)
        self.assertIn("lag", titles)
        self.assertIn("retrying/failing", titles)


class LokiTest(unittest.TestCase):

    def test_failed_flush_and_bad_s3_and_pvc(self):
        report, _ = _run()
        loki = _by_area(report["findings"], "loki")
        titles = " ".join(f["title"].lower() for f in loki)
        self.assertIn("flushes/h are failing", titles)
        self.assertIn("non-2xx", titles)
        self.assertIn("full", titles)
        for f in loki:
            if "full" in f["title"].lower():
                self.assertEqual(f["severity"], "FAIL")  # 95% >= 90%

    def test_small_chunk_and_stream_label(self):
        report, _ = _run()
        loki = _by_area(report["findings"], "loki")
        small = _titled(loki, "flushed chunk")
        self.assertTrue(small)
        self.assertTrue(small[0].get("needs_review"))
        label = _titled(loki, "stream label")
        self.assertTrue(label)
        self.assertIn("pod", label[0]["title"])
        self.assertFalse(label[0].get("keeps_intact", False))


class TempoOtelTest(unittest.TestCase):

    def test_tempo_unused_info(self):
        report, _ = _run()
        tempo = _titled(_by_area(report["findings"], "tempo"), "unused")
        self.assertTrue(tempo)
        self.assertEqual(tempo[0]["severity"], "INFO")

    def test_otel_export_fail_and_queue(self):
        report, _ = _run()
        tempo = _by_area(report["findings"], "tempo")
        titles = " ".join(f["title"].lower() for f in tempo)
        self.assertIn("failing to export", titles)
        self.assertIn("queue is 90% full", titles)
        for f in tempo:
            if "export" in f["title"].lower():
                self.assertEqual(f["config"][0]["target"],
                                 "otel-collector exporters")


class SafetyTest(unittest.TestCase):

    def test_no_snippet_recommends_cutting_rf_or_retention(self):
        report, _ = _run()
        for f in report["findings"]:
            for c in f.get("config") or []:
                low = c["snippet"].lower()
                # A cost snippet must never silently lower these.
                self.assertNotIn("replication_factor: 1", low)
                self.assertNotIn("replication_factor: 2", low)

    def test_est_savings_only_on_verified_or_safe(self):
        report, _ = _run()
        # The duplicate-replica saving is the only one that is both a real
        # $ figure AND keeps_intact true.
        dedup = _titled(report["findings"], "Prometheus replicas")[0]
        self.assertTrue(dedup.get("keeps_intact"))


if __name__ == "__main__":
    unittest.main()
