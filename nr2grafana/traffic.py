"""Sample real datasource traffic from the LGTM stack.

This is the measurement layer for the 1.5 cost-optimization feature: for
each datasource it asks Grafana (through the datasource proxy) what is
actually ingested/stored right now - Mimir active series and label
cardinality, Loki per-stream ingested bytes over a window and stream-label
cardinality, and a best-effort note for Tempo.

The output is the ``nr2grafana/traffic/v1`` report consumed by
``usage``/``costmodel``/``optimize``. Nothing here mutates anything; New
Relic is not touched. No datasource sampling error ever escapes: each
datasource collects its own ``errors`` list and the report is always
returned. Secrets never appear in the report.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from .grafana.client import GrafanaError
from .grafana.live import _epoch_ms

TRAFFIC_SCHEMA = "nr2grafana/traffic/v1"

_TEMPO_NOTE = (
    "Tempo traffic sampling is best-effort: trace/span volume and span-"
    "attribute cardinality are not exposed through a simple proxy "
    "endpoint. For cost analysis, enable the Tempo metrics-generator and "
    "sample the generated span metrics from Mimir, and review the "
    "sampling rate and span-attribute retention.")


def _utcnow() -> str:
    """UTC timestamp, seconds precision, e.g. ``2026-01-02T03:04:05Z``."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _named_counts(status: Dict[str, Any], key: str, name_key: str,
                  value_key: str) -> List[Dict[str, Any]]:
    """Turn a tsdb ``[{"name","value"}, ...]`` stat block into rows.

    Non-dict entries and unparseable values are skipped; the result is
    sorted by count descending.
    """
    out: List[Dict[str, Any]] = []
    for row in status.get(key) or []:
        if not isinstance(row, dict) or row.get("name") is None:
            continue
        try:
            count = int(row.get("value") or 0)
        except (TypeError, ValueError):
            continue
        out.append({name_key: str(row["name"]), value_key: count})
    out.sort(key=lambda d: d[value_key], reverse=True)
    return out


def _per_day(bytes_window: int, frm: str, to: str) -> float:
    """Scale bytes seen over ``[frm, to]`` to a per-24h rate.

    Falls back to a 24h window when the time spec cannot be parsed or the
    window is non-positive, so a bad range never divides by zero.
    """
    now = time.time()
    try:
        window_ms = _epoch_ms(to, now) - _epoch_ms(frm, now)
    except GrafanaError:
        window_ms = 86400 * 1000
    window_s = window_ms / 1000.0
    if window_s <= 0:
        window_s = 86400.0
    return float(bytes_window) * 86400.0 / window_s


def _prom_active_series(status: Dict[str, Any],
                        top_metrics: List[Dict[str, Any]]) -> int:
    """Active-series count: authoritative headStats, else sum of metrics.

    ``headStats.numSeries`` is the real head active-series count; when it
    is absent (some Mimir builds) fall back to summing the per-metric
    series counts (an undercount when the API returns only top metrics,
    but the best available signal).
    """
    head = status.get("headStats")
    if isinstance(head, dict) and "numSeries" in head:
        try:
            return int(head.get("numSeries") or 0)
        except (TypeError, ValueError):
            pass
    return sum(int(m.get("series") or 0) for m in top_metrics)


def _sample_prometheus(grafana: Any, uid: str,
                       errors: List[str]) -> Dict[str, Any]:
    """Prometheus/Mimir block: active series, top metrics, cardinality."""
    status = grafana.prom_tsdb_status(uid, errors)
    metrics = _named_counts(status, "seriesCountByMetricName",
                            "metric", "series")
    label_card = _named_counts(status, "labelValueCountByLabelName",
                               "label", "values")
    histogram = sum(m["series"] for m in metrics
                    if m["metric"].endswith("_bucket"))
    return {"active_series": _prom_active_series(status, metrics),
            "top_metrics": metrics[:50],
            "label_cardinality": label_card,
            "histogram_series": histogram}


def _sample_loki(grafana: Any, uid: str, frm: str, to: str,
                 errors: List[str]) -> Dict[str, Any]:
    """Loki block: stream count, ingested bytes/day, top streams, labels."""
    labels = grafana.loki_labels(uid, errors)
    # Loki's /index/volume needs a real selector - a bare {} is rejected.
    # Match every stream carrying a known label as a broad approximation.
    matcher = ('{%s=~".+"}' % labels[0]) if labels else "{}"
    volume = grafana.loki_volume(uid, matcher, frm, to, errors)
    bytes_window = sum(int(v.get("bytes") or 0) for v in volume)
    top_streams = [{"labels": v.get("stream") or {},
                    "bytes": int(v.get("bytes") or 0)}
                   for v in volume[:20]]
    label_card = grafana.loki_stream_cardinality(uid, labels, errors)
    return {"streams": len(volume),
            "bytes_window": bytes_window,
            "bytes_per_day": _per_day(bytes_window, frm, to),
            "top_streams": top_streams,
            "label_cardinality": label_card}


def _sample_tempo(grafana: Any, uid: str,
                  errors: List[str]) -> Dict[str, Any]:
    """Tempo block: a best-effort note (no cheap volume endpoint)."""
    return {"note": _TEMPO_NOTE}


def sample_traffic(grafana: Any, ds_list: List[Dict[str, Any]],
                   frm: str = "now-24h", to: str = "now",
                   log: Optional[Callable[[str], None]] = None) \
        -> Dict[str, Any]:
    """Sample real traffic/cardinality/volume for each datasource.

    ``ds_list`` is ``[{"family", "uid", "type"}]`` (``family`` preferred,
    ``type`` used as a fallback). For each entry this probes datasource
    health and the family-specific traffic signals, degrading to whatever
    is reachable and collecting per-datasource ``errors`` - it never raises
    for a single datasource, so one broken proxy cannot fail the report.

    Returns the ``nr2grafana/traffic/v1`` report described in the 1.5
    contract.
    """
    emit = log or (lambda m: None)
    datasources: List[Dict[str, Any]] = []
    for ds in ds_list or []:
        family = (ds.get("family") or ds.get("type") or "").lower()
        uid = ds.get("uid") or ""
        errors: List[str] = []
        entry: Dict[str, Any] = {"family": family, "uid": uid}
        try:
            entry["health"] = grafana.datasource_health(uid)
        except Exception as e:  # noqa: BLE001 - health must never break us
            entry["health"] = {"status": "error", "message": str(e)}
        emit("sampling %s datasource %r" % (family or "unknown", uid))
        try:
            if family == "prometheus":
                entry["prometheus"] = _sample_prometheus(grafana, uid,
                                                         errors)
            elif family == "loki":
                entry["loki"] = _sample_loki(grafana, uid, frm, to, errors)
            elif family == "tempo":
                entry["tempo"] = _sample_tempo(grafana, uid, errors)
            else:
                errors.append("unsupported datasource family %r; only "
                              "prometheus, loki and tempo are sampled"
                              % family)
        except Exception as e:  # noqa: BLE001 - one ds must not sink report
            errors.append("unexpected error sampling datasource %r: %s"
                          % (uid, e))
        entry["errors"] = errors
        for err in errors:
            emit("  note: %s" % err)
        datasources.append(entry)
    return {"schema": TRAFFIC_SCHEMA, "generated_at": _utcnow(),
            "range": {"from": frm, "to": to},
            "datasources": datasources}
