"""The recommendation engine: safe, concrete ways to cut LGTM TCO.

``recommend`` cross-references what a datasource actually *ingests / stores*
(the traffic sample, schema ``nr2grafana/traffic/v1``) against what the
migrated dashboards actually *need* (the usage set, schema
``nr2grafana/usage/v1``) and turns the delta into ranked, paste-ready
recommendations (schema ``nr2grafana/optimize/v1``).

SAFETY is the whole point of the tool: it *knows* what is used, so it will
NEVER propose dropping a metric, label or log stream a dashboard depends
on. A dimension that is expensive **and** used never becomes an auto-drop
-- it becomes a ``needs-review`` recommendation (e.g. move a used but
high-cardinality Loki label to structured metadata, with a warning that
selectors must be rewritten). Only dimensions proven absent from the usage
set are marked ``keeps_intact: true`` and get a real drop snippet.

Domain grounding encoded here (see ARCHITECTURE-1.5.md):

- **Loki**: streams = unique stream-label combinations; cardinality is the
  #1 cost driver. Recommend a small low-cardinality stream-label set;
  move id-like / high-churn / unused labels to structured metadata or drop
  them at the agent; attack volume hotspots with per-stream retention or
  by dropping noisy debug lines before ingest.
- **Prometheus/Mimir**: active-series cardinality is the cost. Drop metrics
  no dashboard references (a keep-list built from the usage set is the
  safest form); ``labeldrop`` high-cardinality unused labels; flag
  histogram ``_bucket`` bloat.

Every recommendation carries estimated savings in native units (series /
streams / GB-per-day) AND estimated dollars via the pricing model, plus a
confidence. Dollar figures are ALWAYS estimates based on the user's pricing
inputs -- never a claim about an exact bill.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Set

from .usage import loki_label_keep_set, prometheus_keep_set

SCHEMA = "nr2grafana/optimize/v1"

# ---------------------------------------------------------------------------
# Pricing (assumptions only; costmodel owns the canonical defaults). Every
# lookup uses .get with these fallbacks, so the engine works whether or not
# costmodel is importable and whatever key names the caller supplies.
# ---------------------------------------------------------------------------
_FALLBACK_PRICING = {
    # Loki
    "loki_ingest_per_gb": 0.50,          # $/GB ingested (assumption)
    "loki_store_per_gb_month": 0.03,     # $/GB-month object storage
    "loki_retention_days": 30,           # current retention window
    "loki_per_1k_streams_month": 0.20,   # index cost, $/1k active streams
    # Mimir / Prometheus
    "mimir_per_1k_series_month": 0.60,   # $/1k active series/month
    "mimir_store_per_gb_month": 0.03,
    # Tempo
    "tempo_ingest_per_gb": 0.50,
    "tempo_retention_days": 15,
}

# Default heuristics; override via cfg.
_DEFAULTS = {
    "high_cardinality": 100,   # label value count above which it's "high"
    "top_metrics_drop_n": 5,   # explicit per-metric drop recs to emit
    "top_streams_n": 3,        # volume hotspots to inspect
    "min_bytes_share": 0.05,   # ignore streams below this share of volume
}

# Low-cardinality names that make good Loki stream labels.
_GOOD_STREAM_LABELS = {
    "cluster", "namespace", "app", "service_name", "service", "level",
    "job", "env", "environment", "region", "tenant", "team",
}

# id-like names must ALWAYS be structured metadata, never a stream label.
_ID_LIKE_RE = re.compile(
    r"(^|_)(id|uid|guid|uuid|traceid|spanid|requestid)$", re.I)
_ID_LIKE_EXACT = {
    "trace_id", "span_id", "request_id", "order_id", "user_id",
    "session_id", "correlation_id", "req_id", "transaction_id",
}

# High-churn / unbounded names that should not be stream labels.
_HIGH_CHURN = {
    "ip", "ipaddr", "ip_addr", "remote_addr", "client_ip", "pod",
    "pod_name", "podname", "container", "container_id", "instance",
    "url", "uri", "path", "timestamp", "ts", "host", "hostname",
    "node", "filename", "user", "session", "email", "device_id",
}


def _id_like(name: str) -> bool:
    low = (name or "").lower()
    return bool(_ID_LIKE_RE.search(low)) or low in _ID_LIKE_EXACT


def _high_churn(name: str) -> bool:
    return (name or "").lower() in _HIGH_CHURN


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _usd(x: float) -> float:
    return round(float(x), 2)


def _effective_pricing(pricing: Optional[Dict[str, Any]],
                       cost: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge fallback < costmodel defaults < cost.pricing < explicit."""
    eff = dict(_FALLBACK_PRICING)
    try:  # costmodel is a sibling; may not exist yet during the build
        from .costmodel import DEFAULT_PRICING
        if isinstance(DEFAULT_PRICING, dict):
            eff.update(DEFAULT_PRICING)
    except Exception:  # noqa: BLE001 - degrade to fallbacks
        pass
    if isinstance(cost, dict) and isinstance(cost.get("pricing"), dict):
        eff.update(cost["pricing"])
    if isinstance(pricing, dict):
        eff.update(pricing)
    return eff


def _num(d: Dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        v = d.get(key, default)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _savings(native: Dict[str, Any], monthly_usd: float,
             confidence: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in native.items():
        out[k] = v
    out["monthly_usd"] = _usd(monthly_usd)
    out["confidence"] = confidence
    return out


def _cfg_int(cfg: Dict[str, Any], key: str) -> int:
    try:
        return int(cfg.get(key, _DEFAULTS[key]))
    except (TypeError, ValueError):
        return int(_DEFAULTS[key])


def _cfg_float(cfg: Dict[str, Any], key: str) -> float:
    try:
        return float(cfg.get(key, _DEFAULTS[key]))
    except (TypeError, ValueError):
        return float(_DEFAULTS[key])


# ---------------------------------------------------------------------------
# Config snippet builders (valid, commented, paste-ready YAML)
# ---------------------------------------------------------------------------

def _prom_drop_metric_snippets(metric: str, series: int) -> List[Dict]:
    prom = (
        "# prometheus.yml scrape_config (or Alloy prometheus.scrape).\n"
        "# Metric `%s` is ingested (~%d active series) but referenced by\n"
        "# ZERO migrated dashboards -- safe to drop at the scrape/agent,\n"
        "# BEFORE it costs active-series in Mimir.\n"
        "metric_relabel_configs:\n"
        "  - source_labels: [__name__]\n"
        "    regex: '%s'\n"
        "    action: drop\n" % (metric, series, re.escape(metric)))
    otel = (
        "# OpenTelemetry Collector: drop the same unused metric.\n"
        "processors:\n"
        "  filter/drop_unused_metrics:\n"
        "    metrics:\n"
        "      metric:\n"
        "        - 'name == \"%s\"'\n"
        "service:\n"
        "  pipelines:\n"
        "    metrics:\n"
        "      processors: [filter/drop_unused_metrics]\n" % metric)
    return [
        {"target": "prometheus-relabel", "language": "yaml",
         "snippet": prom,
         "note": "apply at the scrape/agent to save before ingest"},
        {"target": "otel-collector", "language": "yaml",
         "snippet": otel,
         "note": "equivalent for an OTel Collector metrics pipeline"},
    ]


def _prom_keeplist_snippet(keep_metrics: List[str],
                           dropped: int) -> List[Dict]:
    regex = "|".join(re.escape(m) for m in keep_metrics) or "^$"
    prom = (
        "# SAFEST form: keep ONLY the metrics your migrated dashboards\n"
        "# use (histogram _bucket/_sum/_count siblings included); every\n"
        "# other ingested metric (~%d unused series) is dropped before\n"
        "# ingest. Review the keep-list before applying.\n"
        "metric_relabel_configs:\n"
        "  - source_labels: [__name__]\n"
        "    regex: '(%s)'\n"
        "    action: keep\n" % (dropped, regex))
    return [{"target": "prometheus-relabel", "language": "yaml",
             "snippet": prom,
             "note": "keep-list is safer than a drop-list: new noisy "
                     "metrics are excluded automatically"}]


def _prom_labeldrop_snippet(label: str, values: int) -> List[Dict]:
    prom = (
        "# Drop high-cardinality label `%s` (~%d distinct values) that NO\n"
        "# migrated dashboard groups by or filters on. Removing it before\n"
        "# ingest collapses active series.\n"
        "metric_relabel_configs:\n"
        "  - regex: '%s'\n"
        "    action: labeldrop\n" % (label, values, re.escape(label)))
    otel = (
        "# OTel Collector: delete the same attribute.\n"
        "processors:\n"
        "  attributes/drop_%s:\n"
        "    actions:\n"
        "      - key: %s\n"
        "        action: delete\n" % (label, label))
    return [
        {"target": "prometheus-relabel", "language": "yaml",
         "snippet": prom, "note": "labeldrop at the agent, before ingest"},
        {"target": "otel-collector", "language": "yaml",
         "snippet": otel, "note": "OTel attributes processor equivalent"},
    ]


def _prom_used_highcard_snippet(label: str, values: int) -> List[Dict]:
    note = (
        "# REVIEW -- do NOT blindly drop. Label `%s` has ~%d values AND is\n"
        "# used by a migrated dashboard, so dropping it would break those\n"
        "# panels. Options to cut its cost WITHOUT losing the dashboards:\n"
        "#  * add a recording rule that pre-aggregates away `%s` for the\n"
        "#    panels that don't need per-value detail, and point those\n"
        "#    panels at the recorded series;\n"
        "#  * or confirm each panel truly needs `%s` at full cardinality.\n"
        "# Example recording rule (Mimir/Prometheus rules file):\n"
        "groups:\n"
        "  - name: cardinality_reduction\n"
        "    rules:\n"
        "      - record: job:my_metric:sum\n"
        "        expr: sum without (%s) (my_metric)\n"
        % (label, values, label, label, label))
    return [{"target": "mimir-limits", "language": "yaml",
             "snippet": note,
             "note": "used dimension -- needs review, not an auto-drop"}]


def _prom_histogram_snippet(hist_series: int) -> List[Dict]:
    note = (
        "# Histogram bucket bloat: ~%d active series come from `_bucket`\n"
        "# metrics. Each histogram multiplies series by (number of `le`\n"
        "# buckets) x (other labels). Two safe levers:\n"
        "#  1. Native histograms (single series per histogram) -- enable\n"
        "#     in the SDK/agent and in Mimir:\n"
        "limits_config:\n"
        "  native_histograms_ingestion_enabled: true\n"
        "#  2. If classic buckets are required, drop buckets you never\n"
        "#     query with a keep-list on the `le` label at the agent:\n"
        "metric_relabel_configs:\n"
        "  - source_labels: [__name__, le]\n"
        "    regex: '.+_bucket;(0.1|0.5|1|5|\\+Inf)'\n"
        "    action: keep\n" % hist_series)
    return [{"target": "mimir-limits", "language": "yaml",
             "snippet": note,
             "note": "prefer native histograms; review bucket keep-list "
                     "against your used quantiles first"}]


def _loki_recommend_labels_snippet(recommended: List[str]) -> List[Dict]:
    labels = ", ".join("`%s`" % r for r in recommended) or "(none found)"
    lines = "\n".join("        %s: '{{ .%s }}'" % (r, r)
                      for r in recommended) or "        # (define here)"
    alloy = (
        "# Recommended Loki stream labels (low-cardinality, dashboard-\n"
        "# filtered): %s. Keep the stream-label set SMALL -- every extra\n"
        "# label multiplies stream count. Everything else should live in\n"
        "# the log line or structured metadata, not the index.\n"
        "# Grafana Alloy loki.process -- set ONLY these as labels:\n"
        "stage.labels:\n"
        "  values:\n"
        "%s\n" % (labels, lines))
    return [{"target": "alloy", "language": "yaml", "snippet": alloy,
             "note": "define the full stream-label set explicitly so no "
                     "stray high-cardinality label leaks into the index"}]


def _loki_structured_metadata_snippet(label: str, used: bool) -> List[Dict]:
    warn = ""
    if used:
        warn = (
            "# REVIEW: a migrated dashboard selects/groups on `%s`. In\n"
            "# Loki 3.x structured metadata is queryable, but stream\n"
            "# SELECTORS `{%s=...}` must be rewritten to a filter\n"
            "# expression `| %s=\"...\"`. Verify those queries first.\n"
            % (label, label, label))
    alloy = (
        "%s"
        "# Move `%s` OUT of the stream index into structured metadata\n"
        "# (Loki 3.x): still queryable, no per-value stream cost.\n"
        "# Grafana Alloy loki.process:\n"
        "stage.structured_metadata:\n"
        "  values:\n"
        "    %s:\n"
        "# ...and remove it from the stream labels (labeldrop):\n"
        "stage.label_drop:\n"
        "  values:\n"
        "    - %s\n" % (warn, label, label, label))
    limits = (
        "# Loki: structured metadata must be enabled (default in 3.x).\n"
        "limits_config:\n"
        "  allow_structured_metadata: true\n")
    return [
        {"target": "alloy", "language": "yaml", "snippet": alloy,
         "note": "structured metadata keeps the data queryable at no "
                 "stream-cardinality cost"},
        {"target": "loki-limits", "language": "yaml", "snippet": limits,
         "note": "enable structured metadata on the Loki side"},
    ]


def _loki_drop_label_snippet(label: str, values: int) -> List[Dict]:
    promtail = (
        "# promtail scrape_configs (or Alloy loki.relabel): drop stream\n"
        "# label `%s` (~%d values) BEFORE ingest so it never enters the\n"
        "# index. No migrated dashboard filters or groups on it.\n"
        "relabel_configs:\n"
        "  - action: labeldrop\n"
        "    regex: '%s'\n" % (label, values, re.escape(label)))
    otel = (
        "# OTel Collector: delete the same log attribute.\n"
        "processors:\n"
        "  attributes/drop_%s:\n"
        "    actions:\n"
        "      - key: %s\n"
        "        action: delete\n" % (label, label))
    return [
        {"target": "promtail", "language": "yaml", "snippet": promtail,
         "note": "cheapest place to fix -- drop at the agent before "
                 "ingest"},
        {"target": "otel-collector", "language": "yaml", "snippet": otel,
         "note": "OTel logs attributes processor equivalent"},
    ]


def _loki_dropline_snippet(selector: str, level: str) -> List[Dict]:
    promtail = (
        "# Drop noisy `%s` log lines at the agent BEFORE ingest (the\n"
        "# cheapest saving). This stream is a top bytes producer and no\n"
        "# migrated dashboard filters on level=%s.\n"
        "pipeline_stages:\n"
        "  - match:\n"
        "      selector: '%s |~ \"(?i)level[\\\"=: ]+%s\"'\n"
        "      action: drop\n"
        "      drop_counter_reason: %s_noise\n"
        % (level, level, selector or "{}", level, level))
    otel = (
        "# OTel Collector: drop the same lines with the filter processor.\n"
        "processors:\n"
        "  filter/drop_%s:\n"
        "    logs:\n"
        "      log_record:\n"
        "        - 'severity_text == \"%s\"'\n"
        % (level, level.upper()))
    return [
        {"target": "promtail", "language": "yaml", "snippet": promtail,
         "note": "drop before ingest to save ingest AND storage cost"},
        {"target": "otel-collector", "language": "yaml", "snippet": otel,
         "note": "OTel logs filter processor equivalent"},
    ]


def _loki_retention_snippet(selector: str, cur_days: int,
                            new_days: int) -> List[Dict]:
    limits = (
        "# Reduce retention for a high-volume stream to cut stored GB.\n"
        "# REVIEW: long-range queries beyond %dd on this stream will lose\n"
        "# data -- confirm no dashboard/alert needs the longer window.\n"
        "# Loki per-stream retention (compactor must run with\n"
        "# retention_enabled: true):\n"
        "limits_config:\n"
        "  retention_period: %dh   # global default\n"
        "  retention_stream:\n"
        "    - selector: '%s'\n"
        "      priority: 10\n"
        "      period: %dh   # keep this noisy stream only %dd\n"
        % (new_days, cur_days * 24, selector or "{}",
           new_days * 24, new_days))
    return [{"target": "loki-limits", "language": "yaml",
             "snippet": limits,
             "note": "retention trim -- review the window before applying"}]


# ---------------------------------------------------------------------------
# Per-family recommendation logic
# ---------------------------------------------------------------------------

def _prom_recs(ds: Dict[str, Any], usage: Dict[str, Any],
               pricing: Dict[str, Any], cfg: Dict[str, Any],
               recs: List[Dict[str, Any]]) -> None:
    prom = ds.get("prometheus") or {}
    uid = ds.get("uid", "") or ""
    suffix = ("-" + uid) if uid else ""
    keep_metrics = prometheus_keep_set(usage)
    keep_labels = set((usage.get("prometheus") or {}).get("labels") or [])
    high_card = _cfg_int(cfg, "high_cardinality")
    drop_n = _cfg_int(cfg, "top_metrics_drop_n")
    per1k = _num(pricing, "mimir_per_1k_series_month")

    top = [m for m in (prom.get("top_metrics") or [])
           if isinstance(m, dict) and m.get("metric")]
    unused = [m for m in top if m.get("metric") not in keep_metrics]
    unused.sort(key=lambda m: _num(m, "series"), reverse=True)

    # Explicit drops of the biggest unused offenders.
    for m in unused[:drop_n]:
        metric = m["metric"]
        series = int(_num(m, "series"))
        usd = series / 1000.0 * per1k
        sev = "high" if series >= high_card else "medium"
        recs.append({
            "id": "prom-drop-metric-%s" % re.sub(r"[^a-z0-9]+", "-",
                                                 metric.lower()),
            "family": "prometheus", "kind": "drop-metric",
            "severity": sev,
            "title": "Drop unused metric `%s` (~%d active series, used by "
                     "0 dashboards)" % (metric, series),
            "rationale": (
                "Active-series cardinality is the Mimir/Prometheus cost "
                "driver. `%s` is ingested but no migrated dashboard "
                "references it, so it is pure waste. Dropping it at the "
                "scrape/agent frees those active series immediately."
                % metric),
            "evidence": {"metric": metric, "series": series,
                         "used_by_dashboards": 0},
            "keeps_intact": True, "needs_review": False,
            "est_savings": _savings({"series": series}, usd, "high"),
            "config": _prom_drop_metric_snippets(metric, series),
        })

    # Keep-list (safest form) covering ALL unused metrics.
    if unused:
        dropped_series = int(sum(_num(m, "series") for m in unused))
        usd = dropped_series / 1000.0 * per1k
        recs.append({
            "id": "prom-keep-list%s" % suffix,
            "family": "prometheus", "kind": "drop-metric",
            "severity": "medium",
            "title": "Keep-list: ingest only the %d metric name(s) your "
                     "dashboards use" % len(keep_metrics),
            "rationale": (
                "The safest way to cut metric waste is a keep-list built "
                "from the usage set: everything your %d dashboard "
                "metric(s) need is kept (histogram siblings included) and "
                "every other ingested metric (~%d unused active series) is "
                "dropped before ingest. New noisy metrics are excluded "
                "automatically." % (len(keep_metrics), dropped_series)),
            "evidence": {"kept_metrics": len(keep_metrics),
                         "unused_metrics": len(unused),
                         "unused_series": dropped_series},
            "keeps_intact": True, "needs_review": False,
            "est_savings": _savings({"series": dropped_series}, usd,
                                    "high"),
            "config": _prom_keeplist_snippet(sorted(keep_metrics),
                                             dropped_series),
        })

    # High-cardinality labels.
    active = _num(prom, "active_series")
    for c in prom.get("label_cardinality") or []:
        if not isinstance(c, dict):
            continue
        label = c.get("label")
        values = int(_num(c, "values"))
        if not label or values <= high_card:
            continue
        if label in keep_labels:
            recs.append({
                "id": "prom-highcard-used-%s" % label,
                "family": "prometheus", "kind": "drop-label",
                "severity": "medium",
                "title": "Review high-cardinality label `%s` (~%d values, "
                         "used by dashboards)" % (label, values),
                "rationale": (
                    "Label `%s` has high cardinality and multiplies active "
                    "series, but a migrated dashboard uses it -- dropping "
                    "it would break those panels. Reduce cost with a "
                    "recording rule that pre-aggregates it away for panels "
                    "that don't need per-value detail." % label),
                "evidence": {"label": label, "cardinality": values,
                             "used_by_dashboards": 1},
                "keeps_intact": False, "needs_review": True,
                "est_savings": _savings({"series": 0}, 0.0, "low"),
                "config": _prom_used_highcard_snippet(label, values),
            })
        else:
            if active and values > 1:
                series_saved = int(active * (1.0 - 1.0 / values))
            else:
                series_saved = max(values - 1, 0)
            usd = series_saved / 1000.0 * per1k
            recs.append({
                "id": "prom-drop-label-%s" % label,
                "family": "prometheus", "kind": "drop-label",
                "severity": "high",
                "title": "Drop high-cardinality label `%s` (~%d values, "
                         "unused by dashboards)" % (label, values),
                "rationale": (
                    "Label `%s` has ~%d distinct values yet no migrated "
                    "dashboard groups by or filters on it. Each value "
                    "multiplies active series; `labeldrop` at the agent "
                    "collapses them safely." % (label, values)),
                "evidence": {"label": label, "cardinality": values,
                             "used_by_dashboards": 0},
                "keeps_intact": True, "needs_review": False,
                "est_savings": _savings({"series": series_saved}, usd,
                                        "low"),
                "config": _prom_labeldrop_snippet(label, values),
            })

    # Histogram bucket bloat (advisory; never drops used data).
    hist = int(_num(prom, "histogram_series"))
    if hist > 0 and (not active or hist >= 0.2 * active):
        series_saved = hist // 2
        usd = series_saved / 1000.0 * per1k
        recs.append({
            "id": "prom-histogram-buckets%s" % suffix,
            "family": "prometheus", "kind": "histogram",
            "severity": "low",
            "title": "Histogram bucket bloat (~%d `_bucket` series)" % hist,
            "rationale": (
                "Classic histograms multiply series by the number of `le` "
                "buckets times every other label. Native histograms use a "
                "single series each; alternatively drop buckets you never "
                "query. Review against the quantiles your dashboards use "
                "before trimming buckets."),
            "evidence": {"histogram_series": hist},
            "keeps_intact": True, "needs_review": True,
            "est_savings": _savings({"series": series_saved}, usd, "low"),
            "config": _prom_histogram_snippet(hist),
        })


def _stream_label(labels: Dict[str, Any], name: str) -> str:
    for k, v in (labels or {}).items():
        if k.lower() == name:
            return str(v)
    return ""


def _labels_to_selector(labels: Dict[str, Any]) -> str:
    if not labels:
        return "{}"
    parts = ", ".join('%s="%s"' % (k, v)
                      for k, v in sorted(labels.items()))
    return "{%s}" % parts


def _loki_recs(ds: Dict[str, Any], usage: Dict[str, Any],
               pricing: Dict[str, Any], cfg: Dict[str, Any],
               recs: List[Dict[str, Any]]) -> None:
    loki = ds.get("loki") or {}
    uid = ds.get("uid", "") or ""
    suffix = ("-" + uid) if uid else ""
    keep_labels = loki_label_keep_set(usage)
    usage_loki = usage.get("loki") or {}
    stream_labels_used = set(usage_loki.get("stream_labels") or [])
    filtered_values = usage_loki.get("filtered_values") or {}
    high_card = _cfg_int(cfg, "high_cardinality")
    top_n = _cfg_int(cfg, "top_streams_n")
    min_share = _cfg_float(cfg, "min_bytes_share")

    streams = _num(loki, "streams")
    bytes_window = _num(loki, "bytes_window")
    bytes_per_day = _num(loki, "bytes_per_day")
    per1k_streams = _num(pricing, "loki_per_1k_streams_month")
    ingest = _num(pricing, "loki_ingest_per_gb")
    store = _num(pricing, "loki_store_per_gb_month")
    retention = int(_num(pricing, "loki_retention_days")) or 30

    card = [c for c in (loki.get("label_cardinality") or [])
            if isinstance(c, dict) and c.get("label")]
    card_of = {c["label"]: int(_num(c, "values")) for c in card}

    # Recommended stream-label set (advisory).
    low_card_present = set(l for l, v in card_of.items()
                           if v <= high_card)
    recommended = sorted(
        (stream_labels_used & low_card_present) |
        (stream_labels_used & _GOOD_STREAM_LABELS))
    if not recommended:
        recommended = sorted(stream_labels_used & _GOOD_STREAM_LABELS)
    if recommended or card:
        recs.append({
            "id": "loki-recommend-stream-labels%s" % suffix,
            "family": "loki", "kind": "recommend-stream-labels",
            "severity": "low",
            "title": "Recommended stream-label set: %s"
                     % (", ".join("`%s`" % r for r in recommended)
                        or "(keep it minimal)"),
            "rationale": (
                "A Loki stream is one unique combination of stream-label "
                "values; cost scales with stream count. Keep the "
                "stream-label set to the few low-cardinality dimensions "
                "your dashboards actually filter on -- everything else "
                "belongs in structured metadata or the log line, not the "
                "index."),
            "evidence": {"recommended": recommended,
                         "current_label_count": len(card),
                         "streams": int(streams)},
            "keeps_intact": True, "needs_review": False,
            "est_savings": _savings({"streams": 0}, 0.0, "low"),
            "config": _loki_recommend_labels_snippet(recommended),
        })

    # Per-label decisions.
    for label, values in sorted(card_of.items()):
        used = label in keep_labels
        if streams and values > 1:
            streams_saved = int(streams * (1.0 - 1.0 / values))
        else:
            streams_saved = max(values - 1, 0)
        usd = streams_saved / 1000.0 * per1k_streams

        if used:
            if values > high_card:
                recs.append({
                    "id": "loki-structured-used-%s" % label,
                    "family": "loki", "kind": "to-structured-metadata",
                    "severity": "medium",
                    "title": "Move used high-cardinality label `%s` (~%d "
                             "values) to structured metadata"
                             % (label, values),
                    "rationale": (
                        "`%s` is high-cardinality (stream explosion) but a "
                        "migrated dashboard uses it, so it cannot simply be "
                        "dropped. In Loki 3.x it can live as structured "
                        "metadata -- queryable, no stream-cardinality cost "
                        "-- but selectors that match on it must be "
                        "rewritten. Review before applying." % label),
                    "evidence": {"label": label, "cardinality": values,
                                 "used_by_dashboards": 1},
                    "keeps_intact": False, "needs_review": True,
                    "est_savings": _savings({"streams": streams_saved},
                                            usd, "low"),
                    "config": _loki_structured_metadata_snippet(label,
                                                                True),
                })
            continue

        # Unused label: id-like / high-churn / high-card -> structured
        # metadata; otherwise a plain drop.
        if _id_like(label) or _high_churn(label) or values > high_card:
            recs.append({
                "id": "loki-structured-%s" % label,
                "family": "loki", "kind": "to-structured-metadata",
                "severity": "high" if values > high_card else "medium",
                "title": "Move stream label `%s` (~%d values, unused) to "
                         "structured metadata" % (label, values),
                "rationale": (
                    "`%s` is %s and unused by every migrated dashboard, so "
                    "it should never be a stream label. Move it to Loki 3.x "
                    "structured metadata: it stays queryable while removing "
                    "it from the index collapses streams."
                    % (label, "id-like/high-churn" if _id_like(label) or
                       _high_churn(label) else "high-cardinality")),
                "evidence": {"label": label, "cardinality": values,
                             "used_by_dashboards": 0},
                "keeps_intact": True, "needs_review": False,
                "est_savings": _savings({"streams": streams_saved}, usd,
                                        "medium"),
                "config": _loki_structured_metadata_snippet(label, False),
            })
        else:
            recs.append({
                "id": "loki-drop-label-%s" % label,
                "family": "loki", "kind": "drop-label",
                "severity": "medium",
                "title": "Drop unused stream label `%s` (~%d values)"
                         % (label, values),
                "rationale": (
                    "No migrated dashboard filters or groups on `%s`, so it "
                    "adds index/stream cost for nothing. Drop it at the "
                    "agent before ingest." % label),
                "evidence": {"label": label, "cardinality": values,
                             "used_by_dashboards": 0},
                "keeps_intact": True, "needs_review": False,
                "est_savings": _savings({"streams": streams_saved}, usd,
                                        "medium"),
                "config": _loki_drop_label_snippet(label, values),
            })

    # Volume hotspots.
    top_streams = [s for s in (loki.get("top_streams") or [])
                   if isinstance(s, dict)]
    top_streams.sort(key=lambda s: _num(s, "bytes"), reverse=True)
    for s in top_streams[:top_n]:
        sb = _num(s, "bytes")
        if sb <= 0:
            continue
        share = (sb / bytes_window) if bytes_window else 0.0
        if bytes_window and share < min_share:
            continue
        labels = s.get("labels") or {}
        selector = _labels_to_selector(labels)
        gb_day = (bytes_per_day * share) / 1e9 if bytes_per_day else \
            sb / 1e9
        level = _stream_label(labels, "level").lower()
        debug = level in ("debug", "trace")
        filt_levels = [v.lower() for v in filtered_values.get("level", [])]

        if debug and level not in filt_levels:
            usd = gb_day * 30.0 * ingest + \
                gb_day * retention * store
            recs.append({
                "id": "loki-drop-debug%s-%s" % (suffix, level),
                "family": "loki", "kind": "drop-line",
                "severity": "high",
                "title": "Drop noisy %s logs from %s (~%.2f GB/day, %.0f%% "
                         "of volume)" % (level, selector, gb_day,
                                         share * 100),
                "rationale": (
                    "This stream is a top bytes producer and consists of "
                    "level=%s lines that no migrated dashboard filters on. "
                    "Dropping them at the agent saves both ingest and "
                    "storage." % level),
                "evidence": {"selector": selector,
                             "bytes_share": round(share, 3),
                             "gb_per_day": round(gb_day, 3)},
                "keeps_intact": True, "needs_review": False,
                "est_savings": _savings({"gb_per_day": round(gb_day, 3)},
                                        usd, "medium"),
                "config": _loki_dropline_snippet(selector, level),
            })
        else:
            new_days = max(retention // 2, 1)
            saved_gb = gb_day * (retention - new_days)
            usd = saved_gb * store
            recs.append({
                "id": "loki-retention%s-%d"
                      % (suffix, len(recs)),
                "family": "loki", "kind": "retention",
                "severity": "medium",
                "title": "Trim retention for high-volume stream %s (~%.2f "
                         "GB/day, %.0f%% of volume)"
                         % (selector, gb_day, share * 100),
                "rationale": (
                    "This stream drives a large share of Loki volume. If "
                    "long-range history is not needed for it, a shorter "
                    "per-stream retention cuts stored GB. Review the window "
                    "against any dashboard/alert that queries it far back."),
                "evidence": {"selector": selector,
                             "bytes_share": round(share, 3),
                             "gb_per_day": round(gb_day, 3),
                             "current_retention_days": retention,
                             "proposed_retention_days": new_days},
                "keeps_intact": True, "needs_review": True,
                "est_savings": _savings({"gb_per_day": 0.0}, usd, "low"),
                "config": _loki_retention_snippet(selector, retention,
                                                  new_days),
            })


def _tempo_recs(ds: Dict[str, Any], usage: Dict[str, Any],
                pricing: Dict[str, Any], cfg: Dict[str, Any],
                recs: List[Dict[str, Any]]) -> None:
    tempo = ds.get("tempo") or {}
    uid = ds.get("uid", "") or ""
    suffix = ("-" + uid) if uid else ""
    # Best-effort only: suggest head sampling + attribute pruning.
    snippet = (
        "# Tempo cost tracks span volume and span-attribute cardinality.\n"
        "# Best-effort levers (review against your traces backend):\n"
        "#  1. Probabilistic head sampling at the OTel Collector:\n"
        "processors:\n"
        "  probabilistic_sampler:\n"
        "    sampling_percentage: 10\n"
        "#  2. Prune high-cardinality span attributes not used by the\n"
        "#     metrics-generator or dashboards:\n"
        "  attributes/prune:\n"
        "    actions:\n"
        "      - key: http.url\n"
        "        action: delete\n")
    recs.append({
        "id": "tempo-sampling%s" % suffix,
        "family": "tempo", "kind": "sampling",
        "severity": "low",
        "title": "Reduce trace volume with sampling + attribute pruning",
        "rationale": (
            "Tempo cost scales with span volume and span-attribute "
            "cardinality (metrics-generator). Head sampling and pruning "
            "unused high-cardinality span attributes cut both. Best-effort "
            "-- verify against your metrics-generator config first."),
        "evidence": {"note": tempo.get("note", "best-effort")},
        "keeps_intact": True, "needs_review": True,
        "est_savings": _savings({}, 0.0, "low"),
        "config": [{"target": "otel-collector", "language": "yaml",
                    "snippet": snippet,
                    "note": "sampling/pruning are policy choices -- "
                            "review before applying"}],
    })


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def recommend(traffic: Dict[str, Any], usage: Dict[str, Any],
              cost: Optional[Dict[str, Any]] = None,
              pricing: Optional[Dict[str, Any]] = None,
              cfg: Optional[Dict[str, Any]] = None,
              log=None) -> Dict[str, Any]:
    """Cross-reference traffic vs usage; emit safe cost recommendations.

    ``traffic`` is a ``nr2grafana/traffic/v1`` sample, ``usage`` a
    ``nr2grafana/usage/v1`` set (from :func:`usage.collect_usage`). ``cost``
    (schema ``nr2grafana/cost/v1``) and ``pricing`` supply the dollar model;
    either may be omitted -- documented assumptions are used and every
    dollar figure is an estimate, never a claim about an exact bill.

    Returns schema ``nr2grafana/optimize/v1``. Never raises per datasource.
    """
    emit = log or (lambda m: None)
    cfg = cfg or {}
    usage = usage or {}
    pricing_eff = _effective_pricing(pricing, cost)

    recs: List[Dict[str, Any]] = []
    for ds in (traffic or {}).get("datasources") or []:
        if not isinstance(ds, dict):
            continue
        family = ds.get("family") or ""
        try:
            if family == "prometheus":
                _prom_recs(ds, usage, pricing_eff, cfg, recs)
            elif family == "loki":
                _loki_recs(ds, usage, pricing_eff, cfg, recs)
            elif family == "tempo":
                _tempo_recs(ds, usage, pricing_eff, cfg, recs)
        except Exception as e:  # noqa: BLE001 - degrade, never raise
            emit("  warn: could not analyze %s %s: %s"
                 % (family, ds.get("uid", ""), e))

    # Rank: severity, then estimated monthly saving.
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: (
        sev_rank.get(r.get("severity"), 3),
        -_num(r.get("est_savings") or {}, "monthly_usd")))

    by_family: Dict[str, int] = {}
    total = 0.0
    safe = 0
    review = 0
    for r in recs:
        by_family[r["family"]] = by_family.get(r["family"], 0) + 1
        total += _num(r.get("est_savings") or {}, "monthly_usd")
        if r.get("keeps_intact"):
            safe += 1
        if r.get("needs_review"):
            review += 1
        emit("  %-22s %-9s %s" % (r["kind"], r["severity"], r["title"]))

    return {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime()),
        "pricing": pricing_eff,
        "recommendations": recs,
        "summary": {
            "by_family": by_family,
            "total_est_monthly_usd": _usd(total),
            "safe_count": safe,
            "needs_review_count": review,
            "count": len(recs),
        },
    }
