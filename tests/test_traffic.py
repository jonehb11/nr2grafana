"""Tests for nr2grafana.traffic.sample_traffic.

A programmable stub stands in for GrafanaLive, serving canned tsdb status,
Loki volume and label cardinality inventories so no network is involved.
The assertions cover the report shape, the bytes-per-day derivation, the
histogram-series count and graceful per-datasource degradation.
"""

import unittest

from nr2grafana.traffic import (
    TRAFFIC_SCHEMA, _named_counts, _per_day, sample_traffic)


class StubGrafana(object):
    """Minimal GrafanaLive stand-in with programmable inventories.

    ``fail`` names of methods that should append an error and return an
    empty result, exercising graceful degradation.
    """

    def __init__(self, tsdb=None, health=None, loki_labels=None,
                 loki_volume=None, loki_card=None, fail=None):
        self._tsdb = tsdb or {}
        self._health = health or {"status": "ok", "message": "healthy"}
        self._loki_labels = loki_labels or []
        self._loki_volume = loki_volume or []
        self._loki_card = loki_card or []
        self._fail = set(fail or [])
        self.calls = []

    def datasource_health(self, uid):
        self.calls.append(("health", uid))
        return self._health

    def prom_tsdb_status(self, uid, errors=None):
        self.calls.append(("tsdb", uid))
        if "tsdb" in self._fail:
            if errors is not None:
                errors.append("HTTP 404 on GET /api/v1/status/tsdb")
            return {}
        return self._tsdb

    def loki_labels(self, uid, errors=None):
        self.calls.append(("labels", uid))
        if "labels" in self._fail:
            if errors is not None:
                errors.append("HTTP 403 on GET /loki/api/v1/labels")
            return []
        return list(self._loki_labels)

    def loki_volume(self, uid, matcher="{}", frm="now-24h", to="now",
                    errors=None):
        self.calls.append(("volume", uid, matcher))
        if "volume" in self._fail:
            if errors is not None:
                errors.append("HTTP 500 on GET /loki/api/v1/index/volume")
            return []
        return list(self._loki_volume)

    def loki_stream_cardinality(self, uid, labels=None, errors=None):
        self.calls.append(("card", uid, tuple(labels or ())))
        if "card" in self._fail:
            if errors is not None:
                errors.append("cardinality probe failed")
            return []
        return list(self._loki_card)


TSDB = {
    "headStats": {"numSeries": 12345},
    "seriesCountByMetricName": [
        {"name": "http_server_duration_seconds_bucket", "value": 4000},
        {"name": "request_latency_bucket", "value": 1500},
        {"name": "unused_expensive_metric", "value": 3000},
        {"name": "up", "value": 5}],
    "labelValueCountByLabelName": [
        {"name": "pod", "value": 4213},
        {"name": "job", "value": 7}]}

VOLUME = [
    {"stream": {"namespace": "chatty", "level": "debug"}, "bytes": 9000},
    {"stream": {"namespace": "quiet"}, "bytes": 1000}]

LOKI_CARD = [{"label": "pod", "values": 5000}, {"label": "job", "values": 3}]


class HelperTests(unittest.TestCase):
    def test_named_counts_sorted_and_filtered(self):
        rows = _named_counts(TSDB, "labelValueCountByLabelName",
                             "label", "values")
        self.assertEqual(rows, [{"label": "pod", "values": 4213},
                                {"label": "job", "values": 7}])

    def test_named_counts_missing_key(self):
        self.assertEqual(_named_counts({}, "nope", "a", "b"), [])

    def test_per_day_scales_window(self):
        # 12h window -> per-day is double the window bytes.
        self.assertAlmostEqual(_per_day(1000, "now-12h", "now"), 2000.0,
                               places=1)

    def test_per_day_full_day_identity(self):
        self.assertAlmostEqual(_per_day(5000, "now-24h", "now"), 5000.0,
                               places=1)

    def test_per_day_bad_spec_falls_back_to_day(self):
        self.assertAlmostEqual(_per_day(5000, "yesterday", "now"), 5000.0,
                               places=1)


class SampleTrafficShapeTests(unittest.TestCase):
    def test_report_envelope(self):
        g = StubGrafana(tsdb=TSDB)
        rep = sample_traffic(g, [{"family": "prometheus", "uid": "mimir"}],
                             frm="now-24h", to="now")
        self.assertEqual(rep["schema"], TRAFFIC_SCHEMA)
        self.assertEqual(rep["range"], {"from": "now-24h", "to": "now"})
        self.assertTrue(rep["generated_at"].endswith("Z"))
        self.assertEqual(len(rep["datasources"]), 1)

    def test_prometheus_block(self):
        g = StubGrafana(tsdb=TSDB)
        rep = sample_traffic(g, [{"family": "prometheus", "uid": "mimir"}])
        ds = rep["datasources"][0]
        self.assertEqual(ds["family"], "prometheus")
        self.assertEqual(ds["health"]["status"], "ok")
        prom = ds["prometheus"]
        self.assertEqual(prom["active_series"], 12345)
        # top_metrics sorted by series desc.
        self.assertEqual(prom["top_metrics"][0]["metric"],
                         "http_server_duration_seconds_bucket")
        self.assertEqual(prom["top_metrics"][0]["series"], 4000)
        self.assertEqual(prom["label_cardinality"][0],
                         {"label": "pod", "values": 4213})
        # histogram_series = sum of the two *_bucket metrics.
        self.assertEqual(prom["histogram_series"], 4000 + 1500)
        self.assertEqual(ds["errors"], [])

    def test_active_series_falls_back_to_metric_sum(self):
        tsdb = {"seriesCountByMetricName": [
            {"name": "a", "value": 10}, {"name": "b", "value": 20}]}
        g = StubGrafana(tsdb=tsdb)
        rep = sample_traffic(g, [{"family": "prometheus", "uid": "m"}])
        self.assertEqual(rep["datasources"][0]["prometheus"]
                         ["active_series"], 30)

    def test_loki_block_and_bytes_per_day(self):
        g = StubGrafana(loki_labels=["namespace", "pod"],
                        loki_volume=VOLUME, loki_card=LOKI_CARD)
        rep = sample_traffic(g, [{"family": "loki", "uid": "loki"}],
                             frm="now-12h", to="now")
        loki = rep["datasources"][0]["loki"]
        self.assertEqual(loki["streams"], 2)
        self.assertEqual(loki["bytes_window"], 10000)
        # 12h window doubles to a per-day figure.
        self.assertAlmostEqual(loki["bytes_per_day"], 20000.0, places=1)
        self.assertEqual(loki["top_streams"][0]["labels"]["namespace"],
                         "chatty")
        self.assertEqual(loki["label_cardinality"][0]["label"], "pod")
        # A real selector (not bare {}) is built from a known label.
        vol_call = [c for c in g.calls if c[0] == "volume"][0]
        self.assertIn("namespace", vol_call[2])
        # The discovered labels are reused for cardinality (no re-fetch).
        card_call = [c for c in g.calls if c[0] == "card"][0]
        self.assertEqual(card_call[2], ("namespace", "pod"))

    def test_tempo_block_is_best_effort_note(self):
        g = StubGrafana()
        rep = sample_traffic(g, [{"family": "tempo", "uid": "tempo"}])
        self.assertIn("best-effort",
                      rep["datasources"][0]["tempo"]["note"])
        self.assertEqual(rep["datasources"][0]["errors"], [])

    def test_family_falls_back_to_type(self):
        g = StubGrafana(tsdb=TSDB)
        rep = sample_traffic(g, [{"type": "prometheus", "uid": "m"}])
        self.assertEqual(rep["datasources"][0]["family"], "prometheus")
        self.assertIn("prometheus", rep["datasources"][0])


class SampleTrafficDegradationTests(unittest.TestCase):
    def test_unsupported_family_records_error_never_raises(self):
        g = StubGrafana()
        rep = sample_traffic(g, [{"family": "cloudwatch", "uid": "cw"}])
        ds = rep["datasources"][0]
        self.assertTrue(ds["errors"])
        self.assertIn("unsupported", ds["errors"][0])

    def test_prom_tsdb_failure_degrades(self):
        g = StubGrafana(tsdb=TSDB, fail={"tsdb"})
        rep = sample_traffic(g, [{"family": "prometheus", "uid": "m"}])
        ds = rep["datasources"][0]
        self.assertTrue(ds["errors"])
        # Still emits a well-formed (empty) prometheus block.
        self.assertEqual(ds["prometheus"]["active_series"], 0)
        self.assertEqual(ds["prometheus"]["top_metrics"], [])
        self.assertEqual(ds["prometheus"]["histogram_series"], 0)

    def test_loki_volume_failure_degrades(self):
        g = StubGrafana(loki_labels=["ns"], fail={"volume"})
        rep = sample_traffic(g, [{"family": "loki", "uid": "loki"}])
        ds = rep["datasources"][0]
        self.assertTrue(ds["errors"])
        self.assertEqual(ds["loki"]["streams"], 0)
        self.assertEqual(ds["loki"]["bytes_window"], 0)
        self.assertEqual(ds["loki"]["bytes_per_day"], 0.0)

    def test_multiple_datasources_isolated(self):
        g = StubGrafana(tsdb=TSDB, loki_labels=["ns"], loki_volume=VOLUME,
                        loki_card=LOKI_CARD, fail={"tsdb"})
        rep = sample_traffic(g, [
            {"family": "prometheus", "uid": "m"},
            {"family": "loki", "uid": "loki"}])
        self.assertEqual(len(rep["datasources"]), 2)
        # Prometheus degraded, Loki still sampled fine.
        self.assertTrue(rep["datasources"][0]["errors"])
        self.assertEqual(rep["datasources"][1]["errors"], [])
        self.assertEqual(rep["datasources"][1]["loki"]["bytes_window"],
                         10000)

    def test_health_exception_becomes_error_status(self):
        class Boom(StubGrafana):
            def datasource_health(self, uid):
                raise RuntimeError("network down")

        g = Boom(tsdb=TSDB)
        rep = sample_traffic(g, [{"family": "prometheus", "uid": "m"}])
        self.assertEqual(rep["datasources"][0]["health"]["status"],
                         "error")
        self.assertIn("network down",
                      rep["datasources"][0]["health"]["message"])

    def test_log_callback_receives_messages(self):
        logged = []
        g = StubGrafana(tsdb=TSDB)
        sample_traffic(g, [{"family": "prometheus", "uid": "m"}],
                       log=logged.append)
        self.assertTrue(any("sampling" in m for m in logged))

    def test_empty_ds_list_returns_empty_report(self):
        g = StubGrafana()
        rep = sample_traffic(g, [])
        self.assertEqual(rep["datasources"], [])
        self.assertEqual(rep["schema"], TRAFFIC_SCHEMA)


if __name__ == "__main__":
    unittest.main()
