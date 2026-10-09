"""NRQL -> PromQL translation (Mimir/Prometheus, OTel-fed).

Covers FROM Metric, APM events (Transaction, ...), Span aggregations (via
span metrics), and infrastructure sample events (via hostmetrics /
kube-state-metrics / cAdvisor equivalents).

Semantics follow the migration spec in docs/translation-spec.md:
- TIMESERIES  -> range query, window $__rate_interval
- no TIMESERIES -> instant query, window $__range (NR aggregates the whole
  SINCE window, so instant PromQL must too)
- SINCE/UNTIL -> panel/dashboard time range, never PromQL
- units are never numerically rescaled; the panel unit is set instead
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..nrql.parser import Attr, Func, Lit, NrqlQuery, SelectItem, Star
from .common import (
    APPROXIMATE, EXACT, INFRA_EVENT_TYPES, K8S_EVENT_TYPES, NEEDS_REVIEW,
    UNTRANSLATABLE, Matcher, Translation, Untranslatable, assert_no_repr,
    cond_text, cond_to_matchers, facet_labels, legend_for, map_attr,
    render_selector, sanitize_label, worst,
)


# ---------------------------------------------------------------------------
# Metric source resolution
# ---------------------------------------------------------------------------

@dataclass
class MetricSource:
    """A resolved Prometheus metric family for a query.

    ``mtype`` is one of counter|gauge|histogram|summary, or "expr" for a
    pre-built exporter expression (``expr_template``: INFRA_MAP /
    K8S_METRIC_MAP formulas). ``wrap_agg`` says whether the NRQL
    aggregation must still be applied around that expression (per-series
    formulas such as rate(container_cpu...)) or the template is already a
    complete aggregate (node_exporter CPU % formula).
    """
    base: str                 # histogram/summary base or full metric name
    mtype: str
    unit: str = ""            # Grafana unit id ('s', 'ms', ...)
    confidence: str = EXACT
    note: str = ""
    extra_matchers: Optional[List[Matcher]] = None
    expr_template: str = ""
    wrap_agg: bool = True
    infra: bool = False       # exporter-backed mapping (INFRA/K8S maps)
    k8s: bool = False         # came from K8S_METRIC_MAP
    kind_source: str = ""     # metric_map|metric_kinds|live_hints|k8s_map|
    #                           suffix|name_rule|agg_rule|default

    def name(self, suffix: str = "") -> str:
        return self.base + suffix


_COUNTER_SUFFIXES = ("_total", "_count")
_HISTO_HINTS = ("duration", "latency", "_time", "response_time")

# SEAM-KIND NAME-RULES (contract section 1). Applied to the normalized
# Prometheus name (event/summary/histogram words) or its last dotted
# segment (gauge words).
_GAUGE_WORDS_RE = re.compile(
    r"(?i)\b(percent|percentage|utilization|usage|ratio|bytes|cores|count$|"
    r"size|"
    r"free|used|available|desired|missing|requested|limit|gauge|"
    r"temperature|age|lag|depth|visible|inflight|queued|connections)\b")
_WORD_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[_\-.]+")


def _past_tense_event(nr_name: str, last: str) -> bool:
    """True when an APP metric's last word is a past-tense verb that is
    not a state adjective: 'refresh.requested' -> event, 'pods.desired'
    -> level. Infra/k8s/host families never take this path."""
    head = nr_name.split(".", 1)[0].lower()
    if head in ("k8s", "host", "system", "container", "process", "net",
                "aws", "apm", "newrelic"):
        return False
    words = _segment_words(last).split()
    if not words:
        return False
    w = words[-1].lower()
    return len(w) >= 5 and w.endswith("ed") and w not in _STATE_WORDS


def _segment_words(segment: str) -> str:
    """'memoryUsedPercent' -> 'memory Used Percent', 'queue_size_by_x'
    -> 'queue size by x': the gauge NAME-RULE matches whole words, so
    'titration' does not read as 'ratio' nor 'storage' as 'age'."""
    return " ".join(w for w in _WORD_SPLIT_RE.split(segment) if w)
_EVENT_WORDS_RE = re.compile(
    r"(?i)(created|dispensed|executed|processed|failed|succeeded|received|"
    r"sent|completed|started|triggered|updated|deleted|signals?|events?|"
    r"requests?|errors?|hits?|calls?|messages?|tasks?|retries|timeouts|"
    r"status_\d{3})$")
# sum()/count() of an app metric whose last word is a past-tense verb
# ("order.price.refresh.requested") counts events -> counter; state
# adjectives that merely look past-tense stay levels (gauge words).
_STATE_WORDS = frozenset([
    "used", "unused", "desired", "queued", "reserved", "allocated",
    "committed", "cached", "buffered", "enabled", "disabled", "shared",
    "mapped", "loaded", "connected", "assigned", "limited",
])
_SUMMARY_WORDS_RE = re.compile(
    r"(?i)(mean|median|upper|lower|percentiles|summary|p\d\d|stddev)$")
_HISTO_WORDS_RE = re.compile(
    r"(?i)(bucket|duration|latency|seconds|milliseconds)$")
_QUANTILE_AGGS = ("percentile", "median", "histogram", "apdex")
_COUNTER_AGGS = ("sum", "count", "rate")
_GAUGE_AGGS = ("latest", "average", "avg", "max", "min", "derivative",
               "predictlinear", "stddev")
_KINDS = ("counter", "gauge", "histogram", "summary")


def normalize_metric_name(name: str) -> str:
    """Deterministic NR -> Prometheus rename: dots and dashes (and any
    other non-identifier character) -> underscores, camelCase preserved."""
    out = re.sub(r"[^a-zA-Z0-9_:]", "_", name)
    out = re.sub(r"_+", "_", out)
    if out and out[0].isdigit():
        out = "_" + out
    return out


def _normalize_kind(kind: Any) -> str:
    k = str(kind or "").strip().lower()
    if k in ("histogram", "summary", "counter", "gauge"):
        return k
    if k in ("count", "cumulative", "monotonic"):
        return "counter"
    if k in ("distribution", "timer"):
        return "summary"
    return ""


def _source_for_kind(name: str, kind: str, confidence: str, note: str,
                     kind_source: str, unit: str = "") -> MetricSource:
    """Build a MetricSource for a known kind, normalizing the family name
    (strip the _bucket/_sum/_count series suffix off histogram/summary
    families)."""
    base = name
    if kind in ("histogram", "summary"):
        for suf in ("_bucket", "_sum", "_count"):
            if base.endswith(suf):
                base = base[:-len(suf)]
                break
    return MetricSource(base, kind, unit=unit, confidence=confidence,
                        note=note, kind_source=kind_source)


def _hint_source(base: str, hints: Dict[str, Any], cfg: Dict[str, Any]
                 ) -> Tuple[Optional[MetricSource], Optional[str]]:
    """SEAM-HINTS: Mimir metadata decides both the kind and the exact
    family name (``base`` vs ``base_total``). Returns (source, existing
    name) - the second is set when the name exists in Mimir but its type
    is unknown so the rules decide the kind without renaming it."""
    types = hints.get("metric_types") if isinstance(hints, dict) else None
    if not isinstance(types, dict):
        types = {}
    names = set(types)
    extra = hints.get("metric_names") if isinstance(hints, dict) else None
    if isinstance(extra, (list, tuple, set)):
        names.update(str(n) for n in extra)
    cands = [base, base + "_total", base + "_bucket", base + "_count",
             base + "_sum"]
    for cand in cands:
        kind = _normalize_kind(types.get(cand))
        if kind:
            src = _source_for_kind(
                cand, kind, EXACT,
                "metric %r verified in Mimir metadata as a %s (live hints)"
                % (cand, kind), "live_hints")
            return src, None
    for cand in cands:
        if cand in names:
            if cand.endswith("_total"):
                return _source_for_kind(
                    cand, "counter", APPROXIMATE,
                    "metric %r exists in Mimir (live hints); _total suffix "
                    "implies a counter" % cand, "live_hints"), None
            if cand.endswith("_bucket"):
                return _source_for_kind(
                    cand, "histogram", APPROXIMATE,
                    "series %r exists in Mimir (live hints); treated as a "
                    "histogram" % cand, "live_hints"), None
            return None, cand
    return None, None


def infer_metric_kind(nr_name: str, agg: str, cfg: Dict[str, Any],
                      hints: Optional[Dict[str, Any]] = None
                      ) -> MetricSource:
    """SEAM-KIND: resolve an NR metric name + aggregation to a Prometheus
    family (name + kind). Resolution order: cfg["metric_map"] >
    K8S_METRIC_MAP (+ cfg["k8s_metric_map"]) > cfg["metric_kinds"] >
    live hints (cfg["live_hints"].metric_types / metric_names) >
    suffix (_total/_bucket) > NAME-RULES > AGG-RULES > default gauge.
    Counters get the _total suffix (cfg metric_total_suffix, default True)
    unless hints show the bare name exists."""
    cfg = cfg or {}
    agg = (agg or "").lower()
    if hints is None:
        hints = cfg.get("live_hints") or {}

    mm = cfg.get("metric_map") or {}
    if nr_name in mm:
        entry = mm[nr_name]
        if isinstance(entry, str):
            entry = {"name": entry}
        kind = _normalize_kind(entry.get("type")) or "gauge"
        return MetricSource(
            base=entry.get("name", normalize_metric_name(nr_name)),
            mtype=kind, unit=entry.get("unit", ""), confidence=EXACT,
            note="metric %r mapped by config metric_map to %r (%s)"
                 % (nr_name, entry.get("name"), kind),
            kind_source="metric_map")

    k8s = k8s_metric_entry(nr_name, cfg)
    if k8s is not None:
        return k8s_source(nr_name, k8s)

    base = normalize_metric_name(nr_name)
    if nr_name.startswith("k8s."):
        return MetricSource(
            base, "gauge", confidence=NEEDS_REVIEW,
            note="Kubernetes-integration metric %r has no K8S_METRIC_MAP "
                 "entry; %r is the literal rename and almost certainly "
                 "does not exist in Mimir - the kube-state-metrics/cAdvisor "
                 "family differs by name; add it to config k8s_metric_map "
                 "(e.g. {\"metric\": \"kube_...\"}) or metric_map"
                 % (nr_name, base),
            kind_source="default")
    kinds = cfg.get("metric_kinds") or {}
    kind = _normalize_kind(kinds.get(nr_name) or kinds.get(base)) \
        if isinstance(kinds, dict) else ""
    if kind:
        name = base
        if kind == "counter" and cfg.get("metric_total_suffix", True) \
                and not base.endswith("_total"):
            name = base + "_total"
        return _source_for_kind(
            name, kind, EXACT, "metric %r is a %s per config metric_kinds"
            % (nr_name, kind), "metric_kinds")

    src, existing = _hint_source(base, hints, cfg)
    if src is not None:
        return src

    if base.endswith("_total"):
        return MetricSource(base, "counter", confidence=APPROXIMATE,
                            note="assumed counter from _total suffix",
                            kind_source="suffix")
    if base.endswith("_bucket"):
        return MetricSource(base[:-len("_bucket")], "histogram",
                            confidence=APPROXIMATE,
                            note="assumed histogram from _bucket suffix",
                            kind_source="suffix")

    last = nr_name.rsplit(".", 1)[-1]
    verify = ("verify the name/type in Mimir (/api/v1/metadata) or run "
              "`convert --live`; override via config metric_map or "
              "metric_kinds")
    exists = " (name verified in Mimir by live hints)" if existing else ""

    def counter(reason: str, source: str) -> MetricSource:
        name = existing or base
        if not existing and cfg.get("metric_total_suffix", True) \
                and not base.endswith("_total"):
            name = base + "_total"
        return MetricSource(
            name, "counter", confidence=APPROXIMATE,
            note="metric %r -> counter %r%s (%s); %s"
                 % (nr_name, name, exists, reason, verify),
            kind_source=source)

    def gauge(reason: str, source: str) -> MetricSource:
        return MetricSource(
            existing or base, "gauge", confidence=APPROXIMATE,
            note="metric %r -> gauge %r%s (%s); %s"
                 % (nr_name, existing or base, exists, reason, verify),
            kind_source=source)

    # NAME-RULES
    if agg in ("sum", "count") and (_EVENT_WORDS_RE.search(base)
                                    or _past_tense_event(nr_name, last)):
        return counter("name rule: %s() of an event-word metric; NR sum()"
                       " of an event count is a Prometheus counter" % agg,
                       "name_rule")
    if _SUMMARY_WORDS_RE.search(base):
        return MetricSource(
            existing or base, "summary", confidence=APPROXIMATE,
            note="metric %r -> summary %r%s (name rule: NR summary metric "
                 "-> Prometheus summary with %s_sum/%s_count series); %s"
                 % (nr_name, existing or base, exists, base, base, verify),
            kind_source="name_rule")
    if _HISTO_WORDS_RE.search(base) and agg in _QUANTILE_AGGS:
        return MetricSource(
            existing or base, "histogram", confidence=NEEDS_REVIEW,
            note="metric %r -> histogram %r%s (name rule: duration/latency"
                 " name with %s()); requires a %s_bucket series; %s"
                 % (nr_name, existing or base, exists, agg, base, verify),
            kind_source="name_rule")
    if _GAUGE_WORDS_RE.search(_segment_words(last)):
        return gauge("name rule: %r is a level, not an event count"
                     % last, "name_rule")

    # AGG-RULES
    if agg in ("percentile", "median"):
        if any(h in base for h in _HISTO_HINTS):
            return MetricSource(
                existing or base, "histogram", confidence=NEEDS_REVIEW,
                note="name suggests a duration histogram (required by "
                     "%s()); verify %r is a histogram in Mimir or add it to "
                     "metric_map with type: histogram" % (agg, base),
                kind_source="agg_rule")
        return MetricSource(
            existing or base, "gauge", confidence=NEEDS_REVIEW,
            note="%s() of %r: no *_bucket histogram is known, so the "
                 "quantile is taken over the raw gauge samples via "
                 "quantile_over_time; if %r is actually a histogram add it "
                 "to metric_map with type: histogram" % (agg, base, base),
            kind_source="agg_rule")
    if agg in ("histogram", "apdex"):
        return MetricSource(
            existing or base, "histogram", confidence=NEEDS_REVIEW,
            note="assumed %r is a histogram (required by %s()); add it to "
                 "metric_map with the exact Prometheus name/type"
                 % (nr_name, agg),
            kind_source="agg_rule")
    if agg in _COUNTER_AGGS:
        return counter("aggregation rule: %s() of an unknown metric is "
                       "read as an event counter" % agg, "agg_rule")
    if agg in _GAUGE_AGGS:
        return gauge("aggregation rule: %s() of an unknown metric is read "
                     "as a level" % agg, "agg_rule")
    src = gauge("default: no rule matched", "default")
    src.confidence = NEEDS_REVIEW
    return src


def resolve_metric(name: str, agg: str, cfg: Dict[str, Any],
                   t: Translation) -> MetricSource:
    """Resolve an NR metric name (FROM Metric SELECT agg(name)) to a
    Prometheus metric family (see infer_metric_kind)."""
    return infer_metric_kind(name, agg, cfg)


# ---------------------------------------------------------------------------
# SEAM-K8S: NR Kubernetes-integration metrics -> kube-state-metrics/cAdvisor
# ---------------------------------------------------------------------------

# Entry keys: "metric" (+ "matchers") for a plain family the NRQL
# aggregation applies to, or "expr" for a per-series formula template
# (placeholders <W> window, <SEL> ",matchers", <SELBARE> "matchers",
# <BYK> "by (namespace, pod, container[, facet labels])", <KEYS> the same
# label list); "kind"; "unit"; "note". Extensible via cfg["k8s_metric_map"]
# (same shape, or a plain "prom_name" string).
_C = 'container!=""'
K8S_METRIC_MAP: Dict[str, Dict[str, Any]] = {
    "k8s.container.cpuRequestedCores": {
        "metric": "kube_pod_container_resource_requests",
        "matchers": [("resource", "=", "cpu")], "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.container.cpuLimitCores": {
        "metric": "kube_pod_container_resource_limits",
        "matchers": [("resource", "=", "cpu")], "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.container.memoryRequestedBytes": {
        "metric": "kube_pod_container_resource_requests",
        "matchers": [("resource", "=", "memory")], "kind": "gauge",
        "unit": "bytes", "note": "kube-state-metrics"},
    "k8s.container.memoryLimitBytes": {
        "metric": "kube_pod_container_resource_limits",
        "matchers": [("resource", "=", "memory")], "kind": "gauge",
        "unit": "bytes", "note": "kube-state-metrics"},
    "k8s.container.memoryWorkingSetBytes": {
        "metric": "container_memory_working_set_bytes",
        "matchers": [("container", "!=", "")], "kind": "gauge",
        "unit": "bytes", "note": "cAdvisor"},
    "k8s.container.memoryUsedBytes": {
        "metric": "container_memory_usage_bytes",
        "matchers": [("container", "!=", "")], "kind": "gauge",
        "unit": "bytes", "note": "cAdvisor"},
    "k8s.container.cpuUsedCores": {
        "expr": "rate(container_cpu_usage_seconds_total{%s<SEL>}[<W>])" % _C,
        "kind": "gauge", "unit": "short",
        "note": "cAdvisor; CPU seconds per second == cores"},
    "k8s.container.cpuCoresUtilization": {
        "expr": ("100 * sum <BYK>(rate(container_cpu_usage_seconds_total{"
                 "%s<SEL>}[<W>])) / on(<KEYS>) max <BYK>("
                 "kube_pod_container_resource_limits{resource=\"cpu\"<SEL>})"
                 % _C),
        "kind": "gauge", "unit": "percent",
        "note": "cAdvisor usage over the kube-state-metrics CPU limit"},
    "k8s.container.cpuRequestedCoresUtilization": {
        "expr": ("100 * sum <BYK>(rate(container_cpu_usage_seconds_total{"
                 "%s<SEL>}[<W>])) / on(<KEYS>) max <BYK>("
                 "kube_pod_container_resource_requests{resource=\"cpu\""
                 "<SEL>})" % _C),
        "kind": "gauge", "unit": "percent",
        "note": "cAdvisor usage over the kube-state-metrics CPU request"},
    "k8s.container.memoryUtilization": {
        "expr": ("100 * sum <BYK>(container_memory_working_set_bytes{"
                 "%s<SEL>}) / on(<KEYS>) max <BYK>("
                 "kube_pod_container_resource_limits{resource=\"memory\""
                 "<SEL>})" % _C),
        "kind": "gauge", "unit": "percent",
        "note": "cAdvisor working set over the kube-state-metrics memory "
                "limit"},
    "k8s.container.memoryRequestedUtilization": {
        "expr": ("100 * sum <BYK>(container_memory_working_set_bytes{"
                 "%s<SEL>}) / on(<KEYS>) max <BYK>("
                 "kube_pod_container_resource_requests{resource=\"memory\""
                 "<SEL>})" % _C),
        "kind": "gauge", "unit": "percent",
        "note": "cAdvisor working set over the kube-state-metrics memory "
                "request"},
    "k8s.container.cpuCfsThrottledPeriodsDelta": {
        "expr": ("rate(container_cpu_cfs_throttled_periods_total{%s<SEL>}"
                 "[<W>])" % _C),
        "kind": "gauge", "unit": "short", "note": "cAdvisor CFS throttling"},
    "k8s.container.cpuCfsPeriodsDelta": {
        "expr": "rate(container_cpu_cfs_periods_total{%s<SEL>}[<W>])" % _C,
        "kind": "gauge", "unit": "short", "note": "cAdvisor CFS periods"},
    "k8s.container.cpuCfsThrottledSecondsDelta": {
        "expr": ("rate(container_cpu_cfs_throttled_seconds_total{%s<SEL>}"
                 "[<W>])" % _C),
        "kind": "gauge", "unit": "s", "note": "cAdvisor CFS throttling"},
    "k8s.container.restartCount": {
        "metric": "kube_pod_container_status_restarts_total",
        "kind": "gauge", "unit": "short",
        "note": "kube-state-metrics; NR restartCount is the absolute count"},
    "k8s.container.isReady": {
        "metric": "kube_pod_container_status_ready", "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.pod.isReady": {
        "metric": "kube_pod_status_ready",
        "matchers": [("condition", "=", "true")], "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.pod.isScheduled": {
        "metric": "kube_pod_status_scheduled",
        "matchers": [("condition", "=", "true")], "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.pod.restartCount": {
        "metric": "kube_pod_container_status_restarts_total",
        "kind": "gauge", "unit": "short",
        "note": "kube-state-metrics (per container; sum for the pod)"},
    "k8s.pod.status": {
        "metric": "kube_pod_status_phase", "kind": "gauge", "unit": "short",
        "note": "kube-state-metrics; one 0/1 series per phase label (NR "
                "status is a string) - filter on phase=\"Running\" etc."},
    "k8s.deployment.podsAvailable": {
        "metric": "kube_deployment_status_replicas_available",
        "kind": "gauge", "unit": "short", "note": "kube-state-metrics"},
    "k8s.deployment.podsDesired": {
        "metric": "kube_deployment_spec_replicas", "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.deployment.podsUnavailable": {
        "metric": "kube_deployment_status_replicas_unavailable",
        "kind": "gauge", "unit": "short", "note": "kube-state-metrics"},
    "k8s.deployment.podsTotal": {
        "metric": "kube_deployment_status_replicas", "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.deployment.podsUpdated": {
        "metric": "kube_deployment_status_replicas_updated", "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.deployment.podsMissing": {
        "expr": ("kube_deployment_spec_replicas{<SELBARE>} - "
                 "kube_deployment_status_replicas_available{<SELBARE>}"),
        "kind": "gauge", "unit": "short",
        "note": "kube-state-metrics desired minus available"},
    "k8s.node.allocatableCpuCores": {
        "metric": "kube_node_status_allocatable",
        "matchers": [("resource", "=", "cpu")], "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.node.allocatableMemoryBytes": {
        "metric": "kube_node_status_allocatable",
        "matchers": [("resource", "=", "memory")], "kind": "gauge",
        "unit": "bytes", "note": "kube-state-metrics"},
    "k8s.node.capacityCpuCores": {
        "metric": "kube_node_status_capacity",
        "matchers": [("resource", "=", "cpu")], "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.node.capacityMemoryBytes": {
        "metric": "kube_node_status_capacity",
        "matchers": [("resource", "=", "memory")], "kind": "gauge",
        "unit": "bytes", "note": "kube-state-metrics"},
    "k8s.node.unschedulable": {
        "metric": "kube_node_spec_unschedulable", "kind": "gauge",
        "unit": "short", "note": "kube-state-metrics"},
    "k8s.node.cpuUsedCores": {
        "expr": "rate(node_cpu_seconds_total{mode!=\"idle\"<SEL>}[<W>])",
        "kind": "gauge", "unit": "short",
        "note": "node_exporter (per cpu/mode series; sum them)"},
    "k8s.node.memoryUsedBytes": {
        "expr": ("node_memory_MemTotal_bytes{<SELBARE>} - "
                 "node_memory_MemAvailable_bytes{<SELBARE>}"),
        "kind": "gauge", "unit": "bytes", "note": "node_exporter"},
    "k8s.node.memoryWorkingSetBytes": {
        "expr": ("node_memory_MemTotal_bytes{<SELBARE>} - "
                 "node_memory_MemAvailable_bytes{<SELBARE>}"),
        "kind": "gauge", "unit": "bytes", "note": "node_exporter"},
    "k8s.node.fsUsedBytes": {
        "expr": ("node_filesystem_size_bytes{fstype!~\"tmpfs|overlay\"<SEL>}"
                 " - node_filesystem_avail_bytes{fstype!~\"tmpfs|overlay\""
                 "<SEL>}"),
        "kind": "gauge", "unit": "bytes", "note": "node_exporter"},
}

# NR Kubernetes sample event -> K8S_METRIC_MAP name prefix, so
# `FROM K8sContainerSample SELECT latest(cpuRequestedCores)` reuses the map.
_K8S_SAMPLE_PREFIX = {
    "k8scontainersample": "k8s.container.",
    "k8spodsample": "k8s.pod.",
    "k8sdeploymentsample": "k8s.deployment.",
    "k8snodesample": "k8s.node.",
}
_K8S_IDENTITY = ["namespace", "pod", "container"]


def _k8s_map(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = dict(K8S_METRIC_MAP)
    user = cfg.get("k8s_metric_map") if cfg else None
    if isinstance(user, dict):
        for k, v in user.items():
            if isinstance(v, str):
                v = {"metric": v, "kind": "gauge"}
            if isinstance(v, dict):
                out[k] = v
    return out


def k8s_metric_entry(nr_name: str, cfg: Dict[str, Any]
                     ) -> Optional[Dict[str, Any]]:
    """K8S_METRIC_MAP lookup, case-insensitive; `k8s.container.X` and the
    K8sContainerSample attribute spelling `containerX` both resolve."""
    table = _k8s_map(cfg)
    if nr_name in table:
        return table[nr_name]
    low = nr_name.lower()
    for k, v in table.items():
        if k.lower() == low:
            return v
    return None


def k8s_sample_entry(event_lower: str, attr: str, cfg: Dict[str, Any]
                     ) -> Optional[Tuple[str, Dict[str, Any]]]:
    prefix = _K8S_SAMPLE_PREFIX.get(event_lower)
    if not prefix:
        return None
    cands = [attr]
    kind_word = prefix.split(".")[1]  # container/pod/deployment/node
    if attr.lower().startswith(kind_word) and len(attr) > len(kind_word):
        stripped = attr[len(kind_word):]
        cands.append(stripped[0].lower() + stripped[1:])
    for cand in cands:
        entry = k8s_metric_entry(prefix + cand, cfg)
        if entry is not None:
            return prefix + cand, entry
    return None


def k8s_source(nr_name: str, entry: Dict[str, Any]) -> MetricSource:
    kind = _normalize_kind(entry.get("kind")) or "gauge"
    unit = entry.get("unit", "")
    note = "K8S_METRIC_MAP: %s -> %s (%s)" % (
        nr_name, entry.get("metric") or entry.get("expr"),
        entry.get("note") or entry.get("notes")
        or "kube-state-metrics/cAdvisor")
    expr = entry.get("expr") or ""
    if expr and not entry.get("metric") and re.fullmatch(
            r"[A-Za-z_:][A-Za-z0-9_:]*", expr):
        # cfg k8s_metric_map {"expr": "<plain family name>"}
        entry = dict(entry, metric=expr)
        expr = ""
    if expr:
        return MetricSource("", "expr", unit=unit, confidence=APPROXIMATE,
                            note=note, expr_template=expr,
                            wrap_agg=True, infra=True, k8s=True,
                            kind_source="k8s_map")
    matchers = [Matcher(l, o, v) for (l, o, v) in entry.get("matchers", [])]
    labels = entry.get("labels")
    if isinstance(labels, dict):
        matchers.extend(Matcher(str(l), "=", str(v))
                        for l, v in labels.items())
    return MetricSource(entry["metric"], kind, unit=unit,
                        confidence=APPROXIMATE, note=note,
                        extra_matchers=matchers or None, infra=True,
                        k8s=True, kind_source="k8s_map")


# APM events -> semconv HTTP server metrics.
def http_server_source(cfg: Dict[str, Any]) -> MetricSource:
    override = (cfg.get("http_metrics") or {}).get("duration_histogram")
    if override:
        return MetricSource(override, "histogram",
                            unit=(cfg.get("http_metrics") or {}).get("unit")
                            or "s",
                            confidence=APPROXIMATE,
                            note="HTTP server duration histogram from "
                                 "config (http_metrics)")
    if cfg.get("http_metrics_flavor", "semconv") == "legacy":
        return MetricSource("http_server_duration_milliseconds", "histogram",
                            unit="ms", confidence=APPROXIMATE,
                            note="legacy OTel semconv HTTP metric")
    return MetricSource("http_server_request_duration_seconds", "histogram",
                        unit="s", confidence=APPROXIMATE,
                        note="OTel semconv HTTP server duration histogram")


def spanmetrics_source(cfg: Dict[str, Any], want: str) -> MetricSource:
    """want: 'duration' or 'calls'."""
    flavor = cfg.get("spanmetrics_flavor", "otel")
    overrides = cfg.get("span_metrics", {})
    if want == "duration":
        name = overrides.get("duration_histogram") or {
            "otel": "traces_span_metrics_duration_milliseconds",
            "otel-seconds": "traces_span_metrics_duration_seconds",
            "tempo": "traces_spanmetrics_latency",
            "legacy": "duration_milliseconds",
        }.get(flavor, "traces_span_metrics_duration_milliseconds")
        unit = overrides.get("unit") or (
            "s" if (name.endswith("_seconds") or flavor == "tempo")
            else "ms")
        return MetricSource(name, "histogram", unit=unit,
                            confidence=NEEDS_REVIEW,
                            note="span-metrics naming is deployment-specific "
                                 "(spanmetrics_flavor=%s); verify metric "
                                 "exists in Mimir" % flavor)
    name = overrides.get("calls_total") or {
        "otel": "traces_span_metrics_calls_total",
        "otel-seconds": "traces_span_metrics_calls_total",
        "tempo": "traces_spanmetrics_calls_total",
        "legacy": "calls_total",
    }.get(flavor, "traces_span_metrics_calls_total")
    return MetricSource(name, "counter", confidence=NEEDS_REVIEW,
                        note="span-metrics naming is deployment-specific "
                             "(spanmetrics_flavor=%s)" % flavor)


# Infra sample events: (event_lower, attr) -> (template, confidence, unit, note)
# Templates may use {W} (window), {sel} (extra matchers incl. braces content),
# {by} rendered via _apply_by().
INFRA_MAP: Dict[Tuple[str, str], Tuple[str, str, str, str]] = {
    ("systemsample", "cpupercent"): (
        "100 * (1 - avg <BY>(rate(node_cpu_seconds_total{mode=\"idle\"<SEL>}[<W>])))",
        APPROXIMATE, "percent", "node_exporter CPU busy %"),
    ("systemsample", "cpuuserpercent"): (
        "100 * avg <BY>(rate(node_cpu_seconds_total{mode=\"user\"<SEL>}[<W>]))",
        APPROXIMATE, "percent", ""),
    ("systemsample", "cpusystempercent"): (
        "100 * avg <BY>(rate(node_cpu_seconds_total{mode=\"system\"<SEL>}[<W>]))",
        APPROXIMATE, "percent", ""),
    ("systemsample", "cpuiowaitpercent"): (
        "100 * avg <BY>(rate(node_cpu_seconds_total{mode=\"iowait\"<SEL>}[<W>]))",
        APPROXIMATE, "percent", ""),
    ("systemsample", "memoryusedpercent"): (
        "100 * (1 - node_memory_MemAvailable_bytes{<SELBARE>} / node_memory_MemTotal_bytes{<SELBARE>})",
        APPROXIMATE, "percent", "used = total - available"),
    ("systemsample", "memoryfreebytes"): (
        "node_memory_MemAvailable_bytes{<SELBARE>}", APPROXIMATE, "bytes", ""),
    ("systemsample", "memoryusedbytes"): (
        "node_memory_MemTotal_bytes{<SELBARE>} - node_memory_MemAvailable_bytes{<SELBARE>}",
        APPROXIMATE, "bytes", ""),
    ("systemsample", "diskusedpercent"): (
        "100 * (1 - node_filesystem_avail_bytes{fstype!~\"tmpfs|overlay|squashfs\"<SEL>} "
        "/ node_filesystem_size_bytes{fstype!~\"tmpfs|overlay|squashfs\"<SEL>})",
        APPROXIMATE, "percent", "per-filesystem; NR reports aggregate"),
    ("systemsample", "loadaverageoneminute"): (
        "node_load1{<SELBARE>}", EXACT, "short", ""),
    ("systemsample", "loadaveragefiveminute"): (
        "node_load5{<SELBARE>}", EXACT, "short", ""),
    ("systemsample", "loadaveragefifteenminute"): (
        "node_load15{<SELBARE>}", EXACT, "short", ""),
    ("networksample", "receivebytespersecond"): (
        "rate(node_network_receive_bytes_total{device!=\"lo\"<SEL>}[<W>])",
        EXACT, "Bps", ""),
    ("networksample", "transmitbytespersecond"): (
        "rate(node_network_transmit_bytes_total{device!=\"lo\"<SEL>}[<W>])",
        EXACT, "Bps", ""),
    ("storagesample", "readbytespersecond"): (
        "rate(node_disk_read_bytes_total{<SELBARE>}[<W>])", EXACT, "Bps", ""),
    ("storagesample", "writebytespersecond"): (
        "rate(node_disk_written_bytes_total{<SELBARE>}[<W>])", EXACT, "Bps", ""),
    ("storagesample", "totalutilizationpercent"): (
        "100 * rate(node_disk_io_time_seconds_total{<SELBARE>}[<W>])",
        APPROXIMATE, "percent", ""),
    ("k8scontainersample", "restartcount"): (
        "sum <BY>(kube_pod_container_status_restarts_total{<SELBARE>})",
        EXACT, "short", "kube-state-metrics"),
    ("k8scontainersample", "cpuusedcores"): (
        "sum <BY>(rate(container_cpu_usage_seconds_total{container!=\"\"<SEL>}[<W>]))",
        EXACT, "short", "cAdvisor"),
    ("k8scontainersample", "memoryworkingsetbytes"): (
        "sum <BY>(container_memory_working_set_bytes{container!=\"\"<SEL>})",
        EXACT, "bytes", "cAdvisor"),
    ("k8scontainersample", "cpulimitcores"): (
        "sum <BY>(kube_pod_container_resource_limits{resource=\"cpu\"<SEL>})",
        EXACT, "short", ""),
    ("k8scontainersample", "memorylimitbytes"): (
        "sum <BY>(kube_pod_container_resource_limits{resource=\"memory\"<SEL>})",
        EXACT, "bytes", ""),
    ("k8spodsample", "isready"): (
        "sum <BY>(kube_pod_status_ready{condition=\"true\"<SEL>})",
        EXACT, "short", ""),
    ("k8snodesample", "allocatablecpucores"): (
        "sum <BY>(kube_node_status_allocatable{resource=\"cpu\"<SEL>})",
        EXACT, "short", ""),
    ("k8snodesample", "allocatablememorybytes"): (
        "sum <BY>(kube_node_status_allocatable{resource=\"memory\"<SEL>})",
        EXACT, "bytes", ""),
    ("k8sdeploymentsample", "podsdesired"): (
        "sum <BY>(kube_deployment_spec_replicas{<SELBARE>})", EXACT, "short", ""),
    ("k8sdeploymentsample", "podsavailable"): (
        "sum <BY>(kube_deployment_status_replicas_available{<SELBARE>})",
        EXACT, "short", ""),
    ("k8sdeploymentsample", "podsunavailable"): (
        "sum <BY>(kube_deployment_status_replicas_unavailable{<SELBARE>})",
        EXACT, "short", ""),
}


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

_AGO_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*"
    r"(millisecond|second|minute|hour|day|week|month)s?\s*(ago)?\s*$",
    re.IGNORECASE)

_UNIT_TO_PROM = {"millisecond": "ms", "second": "s", "minute": "m",
                 "hour": "h", "day": "d", "week": "w", "month": "d"}
_UNIT_SECONDS = {"millisecond": 0.001, "second": 1, "minute": 60,
                 "hour": 3600, "day": 86400, "week": 604800, "month": 2592000}


def nr_duration_to_prom(text: str) -> Optional[str]:
    """'1 week ago' -> '1w'; '1 month' -> '30d' (approximated)."""
    m = _AGO_RE.match(text or "")
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "month":
        n = n * 30
        unit = "day"
    if n == int(n):
        n = int(n)
    return "%s%s" % (n, _UNIT_TO_PROM[unit])


def nr_duration_to_grafana_range(text: str) -> Optional[str]:
    """'30 minutes ago' -> 'now-30m'."""
    special = {"today": "now/d", "this week": "now/w", "this month": "now/M",
               "yesterday": "now-1d/d"}
    key = (text or "").strip().lower()
    if key in special:
        return special[key]
    d = nr_duration_to_prom(text)
    return "now-%s" % d if d else None


# ---------------------------------------------------------------------------
# Expression building
# ---------------------------------------------------------------------------

def _http_fixups(matchers: List[Matcher], t: Translation,
                 cfg: Optional[Dict[str, Any]] = None) -> List[Matcher]:
    """OTel semconv HTTP server metric label conventions for FROM
    Transaction sources: the NR `error` flag and transaction `name` do not
    exist as labels there."""
    status_label = map_attr("httpResponseCode", cfg or {})[0] \
        if cfg else "http_response_status_code"
    out: List[Matcher] = []
    for m in matchers:
        if m.label == "error" and m.value in ("true", "false"):
            wanted_error = (m.value == "true") == (m.op in ("=", "=~"))
            out.append(Matcher(status_label,
                               "=~" if wanted_error else "!~", "5.."))
            t.note("`error` approximated as HTTP 5xx responses on semconv "
                   "metrics; adjust if your error definition differs",
                   NEEDS_REVIEW)
        elif m.label == "span_name":
            out.append(Matcher("http_route", m.op, m.value))
            t.note("NR transaction `name` mapped to the http_route label; "
                   "NR names (WebTransaction/...) differ from route "
                   "patterns — verify the matcher value", NEEDS_REVIEW)
        else:
            out.append(m)
    return out


def _span_fixups(matchers: List[Matcher],
                 cfg: Optional[Dict[str, Any]] = None) -> List[Matcher]:
    """Span-metrics label conventions: error flag -> status_code label;
    service identity label per span_service_label (Tempo emits `service`,
    the OTel spanmetrics connector emits `service_name`)."""
    svc = (cfg or {}).get("span_service_label") or ""
    out: List[Matcher] = []
    for m in matchers:
        if svc and m.label == "service_name":
            out.append(Matcher(svc, m.op, m.value))
        elif m.label == "error" and m.value in ("true", "false"):
            wanted_error = (m.value == "true") == (m.op in ("=", "=~"))
            out.append(Matcher("status_code", "=" if wanted_error else "!=",
                               "STATUS_CODE_ERROR"))
        elif m.label in ("otel_status_code", "status") \
                and m.value.upper() in ("ERROR", "STATUS_CODE_ERROR"):
            out.append(Matcher("status_code", m.op if m.op in ("=", "!=")
                               else "=", "STATUS_CODE_ERROR"))
        else:
            out.append(m)
    return out


class _Ctx:
    def __init__(self, q: NrqlQuery, cfg: Dict[str, Any], t: Translation):
        self.q = q
        self.cfg = cfg
        self.t = t
        self.is_range = q.timeseries is not None
        self.window = "$__rate_interval" if self.is_range else "$__range"
        self.matchers = cond_to_matchers(q.where, cfg, t)
        self.is_metric_event = bool(q.from_) and q.from_[0].lower() == "metric"
        self.is_span = bool(q.from_) and q.from_[0].lower() in (
            "span", "distributedtrace", "distributedtracesummary")
        self.is_apm_http = bool(q.from_) and q.from_[0].lower() in (
            "transaction", "transactionerror")
        if self.is_span:
            self.matchers = _span_fixups(self.matchers, cfg)
        elif self.is_apm_http:
            self.matchers = _http_fixups(self.matchers, t, cfg)
        self.by = facet_labels(q, cfg, t)
        if self.is_apm_http and "span_name" in self.by:
            self.by = ["http_route" if l == "span_name" else l
                       for l in self.by]
            t.note("FACET on transaction name grouped by http_route on "
                   "semconv HTTP metrics", NEEDS_REVIEW)
        svc = cfg.get("span_service_label") or ""
        if self.is_span and svc and "service_name" in self.by:
            self.by = [svc if l == "service_name" else l for l in self.by]
        self.offset = ""  # ' offset 1w' when building COMPARE WITH targets

    def sel(self, extra: Optional[List[Matcher]] = None) -> List[Matcher]:
        return self.matchers + (extra or [])

    def selector(self, name: str, extra: Optional[List[Matcher]] = None,
                 window: Optional[str] = None) -> str:
        s = render_selector(name, self.sel(extra))
        if window:
            s += "[%s]" % window
        if self.offset:
            s += self.offset
        return s

    def by_clause(self, extra_labels: Optional[List[str]] = None) -> str:
        labels = list(dict.fromkeys((extra_labels or []) + self.by))
        return " by (%s)" % ", ".join(labels) if labels else ""


def _wrap_topk(ctx: _Ctx, expr: str) -> str:
    limit = ctx.q.limit
    if ctx.q.facet and isinstance(limit, int):
        if ctx.is_range:
            ctx.t.note("FACET LIMIT %d became topk(%d, ...); on range queries "
                       "topk is evaluated per step so series may flicker"
                       % (limit, limit), APPROXIMATE)
        return "topk(%d, %s)" % (limit, expr)
    return expr


def _render_template(ctx: _Ctx, template: str, matchers: List[Matcher],
                     window: Optional[str] = None) -> str:
    """Fill an INFRA_MAP / K8S_METRIC_MAP template with the query's
    window, matchers and grouping."""
    W = window or ctx.window
    if any(m.label == "container" and m.op == "=" for m in matchers):
        # An explicit container filter makes the cAdvisor pause-container
        # guard redundant.
        template = template.replace('container!=""<SEL>', "<SEL>")
    bare = ",".join(m.render() for m in matchers)
    keys = list(dict.fromkeys(_K8S_IDENTITY + ctx.by))
    by = ctx.by_clause()
    expr = (template
            .replace("<W>", W)
            .replace(" <BY>(", by + "(")
            .replace("<BY>", by.lstrip())
            .replace("<BYK>", "by (%s)" % ", ".join(keys))
            .replace("<KEYS>", ", ".join(keys))
            .replace("<SEL>", ("," + bare) if bare else "")
            .replace("<SELBARE>", bare))
    expr = re.sub(r"\{,", "{", expr)  # tidy leading comma when no matchers
    expr = expr.replace("{}", "")     # drop empty matcher braces
    if ctx.offset:
        expr = re.sub(r"\[([^\]]+)\]", r"[\1]" + ctx.offset, expr)
    return expr


_WRAP_AGG = {"average": "avg", "avg": "avg", "sum": "sum", "max": "max",
             "min": "min", "latest": "max", "count": "count"}


def _expr_source_agg(ctx: _Ctx, fn: Func, src: MetricSource,
                     matchers: List[Matcher]) -> str:
    """Apply the NRQL aggregation to a pre-built exporter expression."""
    t = ctx.t
    name = fn.name
    expr = _render_template(ctx, src.expr_template, matchers)
    if not src.wrap_agg:
        # A complete canonical formula (node_exporter CPU %): the NRQL
        # aggregation is replaced, not applied.
        if name not in ("average", "latest"):
            t.note("the %s() aggregation was replaced by the canonical "
                   "exporter expression - verify the semantics match the "
                   "original widget" % name, NEEDS_REVIEW)
        return expr
    if name in _WRAP_AGG:
        outer = _WRAP_AGG[name]
        if name == "latest" and not ctx.by:
            return expr
        if name == "latest":
            t.note("latest() of a derived expression grouped with max by "
                   "(...) (last_over_time cannot wrap an expression)")
        return "%s%s(%s)" % (outer, ctx.by_clause(), expr)
    if name == "rate":
        t.note("rate() of an exporter formula that is already a rate; "
               "emitted the formula itself", NEEDS_REVIEW)
        return "sum%s(%s)" % (ctx.by_clause(), expr)
    if name in ("percentile", "median"):
        pcts = [50.0] if name == "median" else [
            float(a.value) for a in fn.args[1:]
            if isinstance(a, Lit) and isinstance(a.value, (int, float))
        ] or [95.0]
        t.note("%s() of a derived exporter expression computed as the "
               "quantile across series at each step (no raw samples)"
               % name, NEEDS_REVIEW)
        return "quantile%s(%s, %s)" % (ctx.by_clause(), _fmt_q(pcts[0]),
                                       expr)
    raise Untranslatable(
        "%s() cannot be applied to the exporter expression for this "
        "Kubernetes/infra attribute (%s); use average/sum/max/min/latest"
        % (name, src.expr_template.split("{")[0]))


def _agg_expr(ctx: _Ctx, fn: Func, src: MetricSource,
              extra: Optional[List[Matcher]] = None) -> str:
    """Build the PromQL for one aggregation function against a source."""
    t = ctx.t
    W = ctx.window
    by = ctx.by_clause()
    name = fn.name
    ex = list(extra or [])
    if src.extra_matchers:
        explicit = {m.label for m in ctx.sel(ex) if m.op == "="}
        ex.extend(m for m in src.extra_matchers
                  if not (m.op == "!=" and m.value == ""
                          and m.label in explicit))

    if src.expr_template:
        return _expr_source_agg(ctx, fn, src, ctx.sel(ex))

    def hsel(suffix: str, window: Optional[str] = W) -> str:
        return ctx.selector(src.name(suffix), ex, window)

    if name in ("average", "avg"):
        if src.mtype in ("histogram", "summary"):
            if src.mtype == "summary":
                t.note("average() of NR summary metric %r = sum/count of "
                       "the Prometheus summary (%s_sum / %s_count)"
                       % (src.base, src.base, src.base), APPROXIMATE)
            return ("sum%s(rate(%s)) / sum%s(rate(%s))"
                    % (by, hsel("_sum"), by, hsel("_count")))
        if src.mtype == "counter":
            t.note("average() of a counter is unusual; emitted sum(rate())",
                   NEEDS_REVIEW)
            return "sum%s(rate(%s))" % (by, hsel(""))
        return "avg%s(avg_over_time(%s))" % (by, hsel(""))

    if name == "sum":
        if src.mtype == "counter":
            if ctx.is_range:
                # Count per bucket (NR TIMESERIES sum); rate()*$__interval
                # is equivalent.
                t.note("sum() of counter %r on TIMESERIES = increase per "
                       "bucket ($__interval); NR per-bucket sum" % src.base)
                return "sum%s(increase(%s))" % (by, hsel("", "$__interval"))
            t.note("sum() of counter %r = increase over the panel range "
                   "(total in the window, NR sum semantics)" % src.base)
            return "sum%s(increase(%s))" % (by, hsel(""))
        if src.mtype in ("histogram", "summary"):
            return "sum%s(increase(%s))" % (by, hsel("_sum"))
        t.note("sum() of a gauge: NR sums datapoints; emitted sum of "
               "per-series averages (current-total semantics)", APPROXIMATE)
        return "sum%s(avg_over_time(%s))" % (by, hsel(""))

    if name in ("max", "min") and src.mtype == "summary":
        if name == "min":
            raise Untranslatable(
                "min() of NR summary metric %r: a Prometheus summary "
                "exposes no minimum (only quantiles, _sum and _count); "
                "closest equivalent is the average "
                "sum(rate(%s_sum[..]))/sum(rate(%s_count[..]))"
                % (src.base, src.base, src.base),
                closest_equivalent={
                    "datasource": "prometheus",
                    "example_query": "sum(rate(%s_sum[$__range])) / "
                                     "sum(rate(%s_count[$__range]))"
                                     % (src.base, src.base),
                    "note": "summary average instead of min"})
        t.note("max() of NR summary metric %r approximated by its "
               "quantile=\"0.99\" series (a Prometheus summary has no max); "
               "adjust the quantile label to one your exporter emits"
               % src.base, NEEDS_REVIEW)
        qsel = ctx.selector(src.base, ex + [Matcher("quantile", "=",
                                                     "0.99")], W)
        return "max%s(max_over_time(%s))" % (by, qsel)

    if name in ("max", "min"):
        if src.mtype == "histogram":
            if name == "max":
                t.note("max() from a histogram is the top bucket bound "
                       "(overestimate)", APPROXIMATE)
                return ("histogram_quantile(1, sum by (le%s)(rate(%s)))"
                        % ("".join(", " + l for l in ctx.by), hsel("_bucket")))
            t.note("min() cannot be derived from a histogram; emitted p0 "
                   "(lowest bucket bound)", NEEDS_REVIEW)
            return ("histogram_quantile(0, sum by (le%s)(rate(%s)))"
                    % ("".join(", " + l for l in ctx.by), hsel("_bucket")))
        fname = "max_over_time" if name == "max" else "min_over_time"
        return "%s%s(%s(%s))" % (name, by, fname, hsel(""))

    if name == "count":
        # count(*) / count(attr)
        if src.mtype in ("histogram", "summary"):
            return "sum%s(increase(%s))" % (by, hsel("_count"))
        if src.mtype == "counter":
            if ctx.is_metric_event:
                t.note("count() on a Metric counter emitted as the summed "
                       "increase (event count); NRQL count() strictly counts "
                       "datapoints — use sum() in NR to compare like for "
                       "like", APPROXIMATE)
            return "sum%s(increase(%s))" % (by, hsel(""))
        t.note("count(*) of a gauge-backed source counts series, not events",
               NEEDS_REVIEW)
        return "count%s(%s)" % (by, hsel("", window=None))

    if name in ("latest",):
        if src.mtype in ("histogram", "summary"):
            t.note("latest() on a %s-backed source approximated as "
                   "the recent average" % src.mtype, APPROXIMATE)
            return ("sum%s(rate(%s)) / sum%s(rate(%s))"
                    % (by, hsel("_sum", window="$__rate_interval"),
                       by, hsel("_count", window="$__rate_interval")))
        if src.mtype == "counter":
            t.note("latest() of counter %r is the raw cumulative counter "
                   "value (NR latest() of a counter metric is the last "
                   "reported delta); consider sum() instead" % src.base,
                   NEEDS_REVIEW)
        # last_over_time: per step on TIMESERIES, over the panel range on
        # instant queries (NR latest() = last value in the SINCE window;
        # a bare selector would miss series older than the 5m lookback).
        win = "$__interval" if ctx.is_range else "$__range"
        inner = "last_over_time(%s)" % hsel("", window=win)
        return inner if not ctx.by else "max%s(%s)" % (by, inner)

    if name in ("percentile", "median"):
        pcts = [50.0] if name == "median" else [
            float(a.value) for a in fn.args[1:]
            if isinstance(a, Lit) and isinstance(a.value, (int, float))
        ] or [95.0]
        if src.mtype == "summary":
            t.note("percentile() of NR summary metric %r mapped to the "
                   "Prometheus summary's pre-computed quantile series "
                   "(%s{quantile=\"0.NN\"}); only quantiles the exporter "
                   "emits exist - never histogram_quantile (no _bucket "
                   "series)" % (src.base, src.base), NEEDS_REVIEW)
            exprs = []
            for p in pcts:
                qsel = ctx.selector(src.base, ex + [Matcher(
                    "quantile", "=", _fmt_q(p))], None)
                exprs.append("avg%s(%s)" % (by, qsel))
        elif src.mtype == "gauge":
            # No buckets to interpolate: take the quantile of the raw
            # sampled values per series over the window instead of
            # emitting a query against a nonexistent _bucket family.
            t.note("percentile() of gauge %r mapped to per-series "
                   "quantile_over_time (averaged across series); NR "
                   "computes percentiles over all raw events — verify "
                   "against NR data" % src.base, NEEDS_REVIEW)
            exprs = []
            for p in pcts:
                inner = "quantile_over_time(%s, %s)" % (_fmt_q(p), hsel(""))
                exprs.append("avg%s(%s)" % (by, inner) if ctx.by else inner)
        elif src.mtype != "histogram":
            # Neither a histogram (no _bucket series) nor a gauge (no raw
            # samples to rank): emitting histogram_quantile over a
            # nonexistent %s_bucket family would silently return no data.
            raise Untranslatable(
                "%s() of a %s-backed metric %r has no sound PromQL "
                "equivalent (histogram_quantile needs a *_bucket series and "
                "quantile_over_time needs raw gauge samples); map %r to a "
                "histogram in metric_map"
                % (name, src.mtype, src.base, src.base))
        else:
            t.note("histogram_quantile interpolates within buckets; NR "
                   "percentiles are computed from event data", APPROXIMATE)
            le_by = "le" + "".join(", " + l for l in ctx.by)
            exprs = ["histogram_quantile(%s, sum by (%s)(rate(%s)))"
                     % (_fmt_q(p), le_by, hsel("_bucket")) for p in pcts]
        if len(exprs) > 1:
            for p, e in list(zip(pcts, exprs))[1:]:
                extra_t = Translation(
                    expr=_wrap_topk(ctx, e), datasource="prometheus",
                    query_type="range" if ctx.is_range else "instant",
                    legend=(legend_for(ctx.by) + " p%g" % p).strip(),
                    confidence=t.confidence, group_by=list(ctx.by))
                t.extra.append(extra_t)
            t.legend = (legend_for(ctx.by) + " p%g" % pcts[0]).strip()
        return exprs[0]

    if name == "rate":
        # rate(inner_agg(x), 1 unit)
        per_seconds = 60.0
        for a in fn.args:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                per_seconds = float(a.value)
        mult = "" if per_seconds == 1 else " * %s" % _fmt_num(per_seconds)
        target = "_count" if src.mtype in ("histogram", "summary") else ""
        if src.mtype == "gauge":
            t.note("rate() of a gauge-backed source; emitted rate() anyway",
                   NEEDS_REVIEW)
        elif src.mtype == "counter":
            t.note("rate(sum(%s), 1 %s) = per-second rate of the counter%s"
                   % (src.base, "second" if per_seconds == 1 else
                      "%s seconds" % _fmt_num(per_seconds),
                      " scaled by %s" % _fmt_num(per_seconds)
                      if per_seconds != 1 else ""))
        return "sum%s(rate(%s))%s" % (by, hsel(target), mult)

    if name == "derivative":
        per_seconds = 60.0
        for a in fn.args[1:]:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                per_seconds = float(a.value)
        mult = "" if per_seconds == 1 else " * %s" % _fmt_num(per_seconds)
        if src.mtype in ("histogram", "summary"):
            raise Untranslatable(
                "derivative() on a %s-backed source has no PromQL "
                "equivalent (only _bucket/_sum/_count series exist)"
                % src.mtype)
        if src.mtype == "counter":
            return "sum%s(rate(%s))%s" % (by, hsel(""), mult)
        t.note("derivative() mapped to deriv() (linear regression)",
               APPROXIMATE)
        inner = "deriv(%s)%s" % (hsel(""), mult)
        return "avg%s(%s)" % (by, inner) if ctx.by else inner

    if name == "predictlinear":
        # predictLinear(attr, N units) — parser normalizes the duration
        # to seconds.
        horizon = 3600.0
        for a in fn.args[1:]:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                horizon = float(a.value)
        if src.mtype in ("histogram", "summary"):
            raise Untranslatable(
                "predictLinear() on a %s-backed source has no "
                "PromQL equivalent" % src.mtype)
        if src.mtype == "counter":
            t.note("predictLinear() of a counter predicts the raw counter "
                   "value (resets skew the regression); NR predicts the "
                   "reported metric", NEEDS_REVIEW)
        t.note("predictLinear() mapped to predict_linear() (linear "
               "regression over the query window)", APPROXIMATE)
        inner = "predict_linear(%s, %s)" % (hsel(""), _fmt_num(horizon))
        return "avg%s(%s)" % (by, inner) if ctx.by else inner

    if name in ("uniquecount", "cardinality"):
        attr = fn.args[0] if fn.args else None
        if not isinstance(attr, Attr):
            raise Untranslatable("uniqueCount needs an attribute argument")
        label, mapped = map_attr(attr.name, ctx.cfg)
        if not mapped:
            t.note("uniqueCount attribute %r not in label_map; used %r"
                   % (attr.name, label), APPROXIMATE)
        t.note("uniqueCount() counts distinct label values on series — an "
               "approximation of event-level uniqueness", APPROXIMATE)
        inner_by = ", ".join([label] + ctx.by)
        metric_sel = hsel("_count" if src.mtype in ("histogram", "summary")
                          else "", window=None)
        if not ctx.is_range:
            metric_sel = "last_over_time(%s[%s])" % (metric_sel, W) \
                if not ctx.offset else metric_sel
        return "count%s(count by (%s)(%s))" % (by, inner_by, metric_sel)

    if name == "stddev":
        if src.mtype == "gauge":
            # NRQL stddev is over datapoint values in the time window, i.e.
            # stddev_over_time per series — NOT PromQL's across-series
            # stddev(), which is 0 for a single series.
            t.note("stddev() mapped to per-series stddev_over_time; NR "
                   "computes it over all events in the window", APPROXIMATE)
            inner = "stddev_over_time(%s)" % hsel("")
            return "avg%s(%s)" % (by, inner) if ctx.by else inner
        raise Untranslatable(
            "stddev() cannot be derived from %s-backed metrics (needs raw "
            "values; histograms and summaries lack a sum-of-squares "
            "series)" % src.mtype)

    if name == "apdex":
        thr = 0.5
        for a in fn.args[1:]:
            if isinstance(a, Lit) and isinstance(a.value, str) \
                    and a.value.startswith("t:"):
                try:
                    thr = float(a.value[2:])
                except ValueError:
                    pass
            elif isinstance(a, Lit) and isinstance(a.value, (int, float)):
                thr = float(a.value)
        if src.mtype != "histogram":
            raise Untranslatable(
                "apdex() requires a histogram metric (%r resolved as a %s; "
                "map it to a histogram in metric_map if it has _bucket "
                "series)" % (src.base, src.mtype))
        # NRQL apdex thresholds are seconds; a millisecond-unit histogram
        # needs the bucket bounds scaled or the formula is off by 1000x.
        if src.unit == "ms":
            t.note("apdex threshold t=%s s scaled to %s to match the "
                   "millisecond histogram %r"
                   % (_fmt_num(thr), _fmt_num(thr * 1000), src.base))
            thr = thr * 1000.0
        t.note("apdex formula requires histogram bucket bounds at exactly "
               "t=%g and 4t=%g; verify your buckets" % (thr, thr * 4),
               NEEDS_REVIEW)
        le1 = ctx.selector(src.name("_bucket"),
                           ex + [_le_matcher(thr)], W)
        le4 = ctx.selector(src.name("_bucket"),
                           ex + [_le_matcher(thr * 4)], W)
        cnt = hsel("_count")
        return ("(sum%s(rate(%s)) + sum%s(rate(%s))) / 2 / sum%s(rate(%s))"
                % (by, le1, by, le4, by, cnt))

    if name == "histogram":
        if src.mtype != "histogram":
            raise Untranslatable(
                "histogram() requires a histogram metric (%r resolved as a "
                "%s; map it to a histogram in metric_map if it has _bucket "
                "series)" % (src.base, src.mtype))
        t.note("histogram() rendered as Prometheus buckets over time "
               "(fixed bucket bounds, not NR's requested buckets)",
               APPROXIMATE)
        t.legend = "{{le}}"
        t.query_type = "range"  # heatmaps need range data
        t.notes.append("panel-hint:heatmap")
        return ("sum by (le)(increase(%s))"
                % ctx.selector(src.name("_bucket"), ex, "$__interval"))

    if name in ("funnel",):
        raise Untranslatable(
            "funnel() is event-sequence analysis with no metric equivalent")
    if name in ("earliest",):
        raise Untranslatable(
            "earliest() has no PromQL equivalent (PromQL lacks a "
            "first_over_time function)")
    if name in ("eventtype", "keyset", "aggregationendtime"):
        raise Untranslatable("%s() is NRDB introspection" % name)

    raise Untranslatable("aggregation %s() is not supported by the "
                         "translator" % name)


def _fmt_q(p: float) -> str:
    v = p / 100.0
    s = ("%f" % v).rstrip("0").rstrip(".")
    return s or "0"


def _fmt_num(n: float) -> str:
    if n == int(n):
        return str(int(n))
    return ("%f" % n).rstrip("0").rstrip(".")


def _le_matcher(bound: float) -> Matcher:
    """Bucket-bound matcher robust to both le-label spellings.

    Prometheus text format renders integral bounds as le="2" while
    OpenMetrics renders le="2.0"; exact-match on one spelling silently
    returns no data on stacks using the other.
    """
    if bound == int(bound):
        return Matcher("le", "=~", "%d|%d\\.0" % (int(bound), int(bound)))
    return Matcher("le", "=", _fmt_num(bound))


# ---------------------------------------------------------------------------
# Source resolution per event type
# ---------------------------------------------------------------------------

# Unmapped NR event families -> where their data lives in an LGTM stack;
# used to make 'no metric mapping' failures actionable.
_BROWSER_HINT = ("browser RUM data; Grafana Faro (frontend observability) "
                 "is the LGTM equivalent")
_MOBILE_HINT = "mobile RUM data; Grafana Faro is the LGTM equivalent"
_SYNTH_HINT = ("synthetic monitoring; blackbox_exporter or Grafana "
               "Synthetic Monitoring is the LGTM equivalent")
_NR_ONLY_HINT = ("New Relic account/consumption data; only available via "
                 "the New Relic datasource plugin")
_FINANCE_HINT = ("AWS billing data (FinanceSample); closest equivalent is "
                 "AWS Cost Explorer via the CloudWatch datasource or the "
                 "nr2grafana TCO/awscost feature")
_DEPLOY_HINT = ("deployment markers; closest equivalent is a Grafana "
                "annotation query (e.g. from Loki deploy logs or a "
                "deployment-event metric)")
_EVENT_EQUIVALENTS = {
    "pageview": _BROWSER_HINT, "pageaction": _BROWSER_HINT,
    "browserinteraction": _BROWSER_HINT, "javascripterror": _BROWSER_HINT,
    "ajaxrequest": _BROWSER_HINT,
    "mobile": _MOBILE_HINT, "mobilecrash": _MOBILE_HINT,
    "mobilerequest": _MOBILE_HINT, "mobilerequesterror": _MOBILE_HINT,
    "syntheticcheck": _SYNTH_HINT, "syntheticrequest": _SYNTH_HINT,
    "nrconsumption": _NR_ONLY_HINT, "nrusage": _NR_ONLY_HINT,
    "nrauditevent": _NR_ONLY_HINT,
    "financesample": _FINANCE_HINT, "deployment": _DEPLOY_HINT,
}
_EVENT_CLOSEST = {
    "financesample": {"datasource": "cloudwatch",
                      "example_query": "AWS/Billing EstimatedCharges "
                                       "(Maximum, Currency=USD)",
                      "note": _FINANCE_HINT},
    "deployment": {"datasource": "loki",
                   "example_query": '{job="deployer"} |= "deployed"',
                   "note": _DEPLOY_HINT},
    "nrconsumption": {"datasource": "newrelic", "example_query": "",
                      "note": _NR_ONLY_HINT},
    "nrusage": {"datasource": "newrelic", "example_query": "",
                "note": _NR_ONLY_HINT},
    "pageview": {"datasource": "loki", "example_query":
                 'sum(count_over_time({app="faro"} | json '
                 '| kind="measurement" [$__interval]))',
                 "note": _BROWSER_HINT},
    "syntheticcheck": {"datasource": "prometheus", "example_query":
                       "probe_success{job=\"blackbox\"}",
                       "note": _SYNTH_HINT},
}


def _source_for(ctx: _Ctx, item: SelectItem) -> MetricSource:
    q = ctx.q
    t = ctx.t
    et = (q.from_[0] if q.from_ else "Metric")
    etl = et.lower()
    fn = item.expr if isinstance(item.expr, Func) else None
    agg = fn.name if fn else ""
    arg = fn.args[0] if fn and fn.args else None

    if etl == "metric":
        # rate(sum(m), 1 minute) style nesting: descend to the metric name.
        while isinstance(arg, Func) and arg.args:
            arg = arg.args[0]
        if isinstance(arg, (Attr,)):
            return resolve_metric(arg.name, agg, ctx.cfg, t)
        if isinstance(arg, Lit) and isinstance(arg.value, str):
            return resolve_metric(arg.value, agg, ctx.cfg, t)
        raise Untranslatable(
            "FROM Metric needs a metric name argument in %s()" % (agg or "?"))

    if etl in ("transaction",):
        src = http_server_source(ctx.cfg)
        t.note("FROM Transaction mapped to OTel HTTP server metrics (%s); "
               "requires the service to be OTel-instrumented"
               % src.base, APPROXIMATE)
        return src
    if etl == "transactionerror":
        src = http_server_source(ctx.cfg)
        src.extra_matchers = [Matcher("http_response_status_code", "=~", "5..")]
        t.note("FROM TransactionError approximated as 5xx responses; adjust "
               "if your NR error config counted 4xx too", NEEDS_REVIEW)
        return src

    if etl in ("span", "distributedtrace", "distributedtracesummary"):
        wants_duration = False
        if fn and fn.args and isinstance(fn.args[0], Attr):
            wants_duration = "duration" in fn.args[0].name.lower()
        if agg in ("percentile", "median", "average", "max", "min",
                   "histogram", "apdex") or wants_duration:
            return spanmetrics_source(ctx.cfg, "duration")
        return spanmetrics_source(ctx.cfg, "calls")

    infra = _infra_source(ctx, fn) if fn else None
    if infra is not None:
        return infra
    if etl in INFRA_EVENT_TYPES or etl in K8S_EVENT_TYPES:
        attr = fn.args[0].name if fn and fn.args \
            and isinstance(fn.args[0], Attr) else "*"
        raise Untranslatable(
            "no exporter mapping for %s.%s; closest equivalent: the "
            "node_exporter / kube-state-metrics / cAdvisor family for that "
            "attribute (extend config k8s_metric_map, e.g. "
            "\"k8s.container.<attr>\": {\"metric\": \"<prom name>\"})"
            % (et, attr),
            closest_equivalent={
                "datasource": "prometheus",
                "example_query": "kube_pod_container_resource_requests"
                                 "{resource=\"cpu\"}",
                "note": "kube-state-metrics / cAdvisor / node_exporter "
                        "expose the equivalent data; add the family to "
                        "config k8s_metric_map"})

    msg = "no metric mapping for FROM %s" % et
    hint = _EVENT_EQUIVALENTS.get(etl)
    if hint:
        msg += " (%s)" % hint
    raise Untranslatable(msg, closest_equivalent=_EVENT_CLOSEST.get(etl))


def _infra_source(ctx: _Ctx, fn: Func) -> Optional[MetricSource]:
    """INFRA_MAP / K8S_METRIC_MAP source for an infrastructure sample
    event aggregation (`FROM K8sContainerSample SELECT latest(x)`)."""
    q = ctx.q
    if not q.from_ or not fn.args or not isinstance(fn.args[0], Attr):
        return None
    etl = q.from_[0].lower()
    raw_attr = fn.args[0].name
    attr = raw_attr.lower().replace("_", "").replace(".", "")
    hit = INFRA_MAP.get((etl, attr))
    if hit is not None:
        template, conf, unit, note = hit
        return MetricSource(
            "", "expr", unit=unit, confidence=conf,
            note="infra-event mapping assumes node_exporter / "
                 "kube-state-metrics / cAdvisor metrics exist in Mimir%s"
                 % (" (%s)" % note if note else ""),
            expr_template=template, wrap_agg=False, infra=True,
            kind_source="infra_map")
    k8s = k8s_sample_entry(etl, raw_attr, ctx.cfg)
    if k8s is not None:
        name, entry = k8s
        return k8s_source(name, entry)
    return None


def _infra_lookup(ctx: _Ctx, item: SelectItem) -> Optional[Translation]:
    """Whole-query infra special cases that are not per-attribute
    sources: count(*) FROM K8sPodSample WHERE status = 'Running'."""
    q = ctx.q
    if not q.from_:
        return None
    etl = q.from_[0].lower()
    fn = item.expr if isinstance(item.expr, Func) else None
    if fn is None or not fn.args or not isinstance(fn.args[0], Attr):
        if fn and fn.name == "count" and etl == "k8spodsample":
            phase = _extract_eq(ctx, "status") or _extract_eq(ctx, "phase")
            if phase:
                expr = "sum%s(kube_pod_status_phase{phase=%s%s})" % (
                    ctx.by_clause(), '"%s"' % phase, _sel_tail(ctx))
                ctx.t.k8s_mapped = True
                return _finish_infra(ctx, item, expr, EXACT, "short", "")
    return None


def _sel_tail(ctx: _Ctx) -> str:
    rendered = ",".join(m.render() for m in ctx.matchers)
    return ("," + rendered) if rendered else ""


def _extract_eq(ctx: _Ctx, label: str) -> Optional[str]:
    for m in ctx.matchers:
        if m.label == label and m.op == "=":
            ctx.matchers.remove(m)
            return m.value
    return None


def _finish_infra(ctx: _Ctx, item: SelectItem, expr: str, conf: str,
                  unit: str, note: str) -> Translation:
    t = ctx.t
    t.confidence = worst(t.confidence, conf,
                         NEEDS_REVIEW)  # infra mappings depend on exporters
    t.note("infra-event mapping assumes node_exporter / kube-state-metrics / "
           "cAdvisor metrics exist in Mimir%s" % (" (%s)" % note if note else ""))
    t.expr = _wrap_topk(ctx, expr)
    t.query_type = "range" if ctx.is_range else "instant"
    t.legend = legend_for(ctx.by, item.alias)
    t.group_by = list(ctx.by)
    if unit:
        t.notes.append("unit:%s" % unit)
    return t


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _extract_facet_cases(q: NrqlQuery) -> Optional[List[Tuple[Any, Optional[str]]]]:
    """Pop a sole FACET cases(...) item; return its (cond, alias) list."""
    if len(q.facet) == 1 and isinstance(q.facet[0].expr, Func) \
            and q.facet[0].expr.name == "cases" and q.facet[0].expr.cases:
        specs = list(q.facet[0].expr.cases)
        q.facet = []
        return specs
    return None


def _translate_facet_cases(ctx: _Ctx,
                           items: List[SelectItem],
                           case_specs: List[Tuple[Any, Optional[str]]]
                           ) -> Optional[Translation]:
    """FACET cases(WHERE c1 AS a, WHERE c2 ...) -> one filtered query per
    case (each case IS trivially a filter). Returns None when any case
    condition cannot become label matchers; the caller then degrades to
    the dropped-grouping note."""
    t = ctx.t
    funcs = [i for i in items if isinstance(i.expr, Func)]
    if not funcs:
        return None
    fn = funcs[0].expr
    assert isinstance(fn, Func)
    for cond, _alias in case_specs:
        if not cond_to_matchers(cond, ctx.cfg, Translation()):
            return None
    t.note("FACET cases(...) became one filtered query per case; NR's "
           "implicit 'Other' bucket is not emitted", APPROXIMATE)
    for idx, (cond, alias) in enumerate(case_specs):
        label = alias or cond_text(cond)
        if idx == 0:
            t.expr = _translate_item(ctx, fn, extra=_embedded_matchers(
                ctx, cond))
            t.legend = label
            continue
        sub = Translation(datasource="prometheus", query_type=t.query_type)
        saved = ctx.t
        ctx.t = sub
        try:
            sub.expr = _translate_item(ctx, fn, extra=_embedded_matchers(
                ctx, cond))
        except Untranslatable as e:
            t.note("case %r could not be translated: %s" % (label, e),
                   NEEDS_REVIEW)
            continue
        finally:
            ctx.t = saved
        sub.legend = label
        t.extra.append(sub)
    if len(items) > 1:
        t.note("only the first SELECT item was translated with FACET "
               "cases(...); add the others as separate panels", NEEDS_REVIEW)
    if ctx.q.compare_with:
        t.note("COMPARE WITH combined with FACET cases(...) is not "
               "supported; the comparison series was dropped", NEEDS_REVIEW)
    t.group_by = []
    return t


def translate_to_promql(q: NrqlQuery, cfg: Dict[str, Any]) -> Translation:
    t = Translation(datasource="prometheus")
    case_specs = _extract_facet_cases(q)
    ctx = _Ctx(q, cfg, t)
    t.query_type = "range" if ctx.is_range else "instant"

    items = [i for i in q.select if not isinstance(i.expr, Star)]
    if not items:
        raise Untranslatable(
            "SELECT * (raw event listing) has no metrics equivalent — route "
            "this widget to Loki/Tempo or keep it in New Relic")
    plain = [i for i in items if not isinstance(i.expr, Func)]
    if plain and len(plain) == len(items):
        raise Untranslatable(
            "SELECT of raw attributes (no aggregation) has no metrics "
            "equivalent")

    infra = _infra_lookup(ctx, items[0])
    if infra is not None:
        if case_specs:
            infra.note("FACET cases(...) has no label equivalent on "
                       "infra-event mappings; grouping dropped", NEEDS_REVIEW)
        if len(items) > 1:
            infra.note("only the first SELECT item of this infra query was "
                       "translated; add the others as separate panels",
                       NEEDS_REVIEW)
        _apply_compare_with(ctx, infra)
        assert_no_repr(infra.expr, *[x.expr for x in infra.extra])
        return infra

    if case_specs:
        out = _translate_facet_cases(ctx, items, case_specs)
        if out is not None:
            return out
        t.note("FACET cases(...) conditions could not become label "
               "matchers; grouping dropped — split the cases into "
               "separate filtered panels manually", NEEDS_REVIEW)

    primary_expr: Optional[str] = None
    primary_legend = ""
    for item in items:
        fn = item.expr
        if not isinstance(fn, Func):
            t.note("non-aggregated SELECT item %r dropped" % item, NEEDS_REVIEW)
            continue
        # Isolate this item's legend and unit notes so multi-aggregation
        # SELECTs don't cross-pollinate target attribution.
        t.legend = ""
        extras_before = len(t.extra)
        notes_before = len(t.notes)
        expr = _wrap_topk(ctx, _translate_item(ctx, fn))
        if item.multiplier:
            expr = "(%s) * %s" % (expr, _fmt_num(item.multiplier))
            new = t.notes[notes_before:]
            t.notes[notes_before:] = [n for n in new
                                      if not n.startswith("unit:")]
            t.note("SELECT arithmetic '* %s' preserved; the derived panel "
                   "unit no longer applies — set it manually"
                   % _fmt_num(item.multiplier), APPROXIMATE)
        item_legend = t.legend or legend_for(ctx.by, item.alias)
        if primary_expr is None:
            primary_expr = expr
            primary_legend = item_legend
        else:
            new = t.notes[notes_before:]
            unit_notes = [n for n in new if n.startswith("unit:")]
            t.notes[notes_before:] = [n for n in new
                                      if not n.startswith("unit:")]
            t.extra.insert(extras_before, Translation(
                expr=expr, datasource="prometheus", query_type=t.query_type,
                legend=item_legend, confidence=t.confidence,
                group_by=list(ctx.by), notes=unit_notes))
    if primary_expr is None:
        raise Untranslatable("no translatable SELECT items")
    t.expr = primary_expr
    t.legend = primary_legend
    t.group_by = list(ctx.by)
    _apply_compare_with(ctx, t)
    # Failure class F1 guard: no AST repr may ever reach a query.
    assert_no_repr(t.expr, *[x.expr for x in t.extra])
    return t


_UNIT_PRESERVING_AGGS = {"average", "avg", "percentile", "median", "max",
                         "min", "latest", "histogram", "stddev"}
_COUNT_AGGS = {"count", "uniquecount", "cardinality", "rate", "derivative"}


def _unit_note(t: Translation, agg: str, src: MetricSource) -> None:
    """The panel unit follows the aggregation: count-shaped aggregations
    yield counts regardless of what the underlying metric measures."""
    if agg in _COUNT_AGGS:
        t.notes.append("unit:short")
    elif src.unit and (agg in _UNIT_PRESERVING_AGGS
                       or (agg == "sum" and src.mtype != "counter")):
        t.notes.append("unit:%s" % src.unit)


def _embedded_matchers(ctx: _Ctx, cond: Any) -> List[Matcher]:
    """Convert an embedded WHERE (filter()/percentage()/if()) into extra
    matchers, applying the same event-type fixups as the outer WHERE."""
    extra = cond_to_matchers(cond, ctx.cfg, ctx.t)
    if ctx.is_span:
        extra = _span_fixups(extra, ctx.cfg)
    elif ctx.is_apm_http:
        extra = _http_fixups(extra, ctx.t, ctx.cfg)
    return extra


def _is_lit(v: Any, *values: float) -> bool:
    return isinstance(v, Lit) and isinstance(v.value, (int, float)) \
        and not isinstance(v.value, bool) and float(v.value) in values


def _rewrite_if(ctx: _Ctx, fn: Func) -> Tuple[Func, Optional[List[Matcher]]]:
    """agg(if(cond, x[, else])) -> filtered aggregation when the if() is
    trivially a filter; raise Untranslatable (with a precise reason)
    otherwise.

    Sound because NRQL aggregations skip NULL: agg(if(cond, x)) aggregates
    x only over rows matching cond == filter(agg(x), WHERE cond). A
    non-trivial ELSE value changes every row's contribution and has no
    selector equivalent.
    """
    if not (fn.args and isinstance(fn.args[0], Func)
            and fn.args[0].name == "if"):
        return fn, None
    branch = fn.args[0]
    if branch.where is None:
        raise Untranslatable(
            "the if() condition could not be parsed as a predicate; "
            "rewrite the query as filter(%s(...), WHERE ...)" % fn.name)
    vals = branch.args  # then [, else] — condition lives in branch.where
    then = vals[0] if vals else None
    els = vals[1] if len(vals) > 1 else None
    zero_else = els is None or _is_lit(els, 0)
    if fn.name == "count" and els is None:
        new = Func("count", args=[Star()])
    elif fn.name == "sum" and _is_lit(then, 1) and zero_else:
        # sum(if(cond, 1, 0)) is a row count over cond.
        new = Func("count", args=[Star()])
    elif zero_else and then is not None and (els is None or fn.name == "sum"):
        # ELSE 0 is only neutral for sum(); for other aggregations the
        # zeros would enter the population.
        new = Func(fn.name, args=[then] + list(fn.args[1:]))
    else:
        raise Untranslatable(
            "%s(if(cond, x, y)): the ELSE value enters the aggregation for "
            "every non-matching row, which has no PromQL equivalent — "
            "split into separate filtered queries" % fn.name)
    ctx.t.note("if(%s, ...) translated as a filtered aggregation "
               "(the condition became label matchers)"
               % cond_text(branch.where), APPROXIMATE)
    return new, _embedded_matchers(ctx, branch.where)


def _translate_item(ctx: _Ctx, fn: Func,
                    extra: Optional[List[Matcher]] = None) -> str:
    t = ctx.t
    extra = list(extra or [])
    if fn.name == "if":
        raise Untranslatable(
            "bare if() in SELECT has no metric equivalent; wrap it in an "
            "aggregation or split into filtered queries")
    if fn.name == "funnel":
        # Raise before source resolution so the explanation is the same
        # for every event type (PageView etc. have no metric mapping).
        raise Untranslatable(
            "funnel() is event-sequence analysis (per-user step "
            "conversion) with no metric equivalent; keep this widget in "
            "New Relic or rebuild it from Faro/frontend events")
    if fn.name == "_ratio":
        # agg(x) / agg(y) — a ratio of two aggregations (error-rate shape).
        # Translate each operand against the SAME context (so they share
        # the FACET grouping and outer WHERE) and divide; PromQL matches
        # the two vectors on the shared group labels.
        if len(fn.args) != 2 or not (isinstance(fn.args[0], Func)
                                     and isinstance(fn.args[1], Func)):
            raise Untranslatable(
                "a '/' ratio must divide two aggregations (agg(x) / agg(y))")
        left, right = fn.args[0], fn.args[1]
        notes_before = len(t.notes)
        left_expr = _translate_item(ctx, left, extra)
        right_expr = _translate_item(ctx, right, extra)
        # A ratio is dimensionless; the operands' own unit hints do not
        # carry over to the quotient.
        new = t.notes[notes_before:]
        t.notes[notes_before:] = [n for n in new if not n.startswith("unit:")]
        t.note("agg(x) / agg(y) ratio: PromQL divides the two results "
               "matching on the shared FACET grouping (a series present in "
               "only one operand drops out; the denominator must be "
               "non-zero)", APPROXIMATE)
        count_like = {"count", "uniquecount", "cardinality", "rate"}
        if left.name in count_like and right.name in count_like:
            # count/count (etc.) is a proportion in [0, 1].
            t.notes.append("unit:percentunit")
        return "(%s) / (%s)" % (left_expr, right_expr)

    if fn.name == "_arith":
        # agg(x) + agg(y) / agg(x) - agg(y): both operands against the
        # same context; PromQL adds/subtracts matching on the shared
        # group labels.
        if len(fn.args) != 3 or not (isinstance(fn.args[0], Func)
                                     and isinstance(fn.args[1], Func)):
            raise Untranslatable(
                "a '+'/'-' between SELECT terms must combine two "
                "aggregations (agg(x) + agg(y))")
        left, right, op = fn.args[0], fn.args[1], fn.args[2]
        op_text = str(getattr(op, "value", "+") or "+")
        left_expr = _translate_item(ctx, left, extra)
        right_expr = _translate_item(ctx, right, extra)
        t.note("agg(x) %s agg(y): PromQL combines the two results matching "
               "on the shared FACET grouping (a series present in only one "
               "operand drops out; use 'or vector(0)' to keep it)"
               % op_text, APPROXIMATE)
        return "(%s) %s (%s)" % (left_expr, op_text, right_expr)

    if fn.name == "filter":
        inner = fn.args[0] if fn.args else None
        if not isinstance(inner, Func):
            raise Untranslatable("filter() needs an inner aggregation")
        extra = extra + _embedded_matchers(ctx, fn.where)
        inner, if_extra = _rewrite_if(ctx, inner)
        if if_extra:
            extra = extra + if_extra
        src = _source_for(ctx, SelectItem(expr=inner))
        _record_source(t, src)
        _unit_note(t, inner.name, src)
        return _agg_expr(ctx, inner, src, extra=extra)

    if fn.name == "percentage":
        inner = fn.args[0] if fn.args else None
        if not isinstance(inner, Func):
            raise Untranslatable("percentage() needs an inner aggregation")
        num_extra = extra + _embedded_matchers(ctx, fn.where)
        src = _source_for(ctx, SelectItem(expr=inner))
        _record_source(t, src)
        num = _agg_expr(ctx, inner, src, extra=num_extra)
        den = _agg_expr(ctx, inner, src, extra=extra)
        t.notes.append("unit:percent")
        return "100 * (%s) / (%s)" % (num, den)

    if fn.name in ("sum", "latest", "max", "min", "average", "avg",
                   "count") and len(fn.args) == 1 \
            and isinstance(fn.args[0], Lit) \
            and isinstance(fn.args[0].value, (int, float)) \
            and not isinstance(fn.args[0].value, bool):
        # SELECT sum(0) AS Placeholder: a constant, not a metric.
        t.notes.append("%s(%g) is a constant; emitted vector(%g)"
                       % (fn.name, fn.args[0].value, fn.args[0].value))
        return "vector(%g)" % fn.args[0].value

    fn, if_extra = _rewrite_if(ctx, fn)
    if if_extra:
        extra = extra + if_extra
    src = _source_for(ctx, SelectItem(expr=fn))
    _record_source(t, src)
    _unit_note(t, fn.name, src)
    return _agg_expr(ctx, fn, src, extra=extra)


def _record_source(t: Translation, src: MetricSource) -> None:
    """Fold a resolved source into the translation: confidence floor,
    explanatory note, SEAM-REPORT metric_kind / k8s_mapped."""
    t.confidence = worst(t.confidence, src.confidence)
    if src.note:
        t.note(src.note)
    if src.infra and not src.k8s:
        # Exporter-backed sample mappings depend on which exporters run.
        t.confidence = worst(t.confidence, NEEDS_REVIEW)
    if src.k8s:
        t.k8s_mapped = True
    if src.mtype in _KINDS:
        t.metric_kind = src.mtype
    elif src.mtype == "expr" and not t.metric_kind:
        t.metric_kind = "gauge"


def _apply_compare_with(ctx: _Ctx, t: Translation) -> None:
    if not ctx.q.compare_with:
        return
    off = nr_duration_to_prom(ctx.q.compare_with)
    if not off:
        t.note("COMPARE WITH %r could not be parsed; comparison series "
               "dropped" % ctx.q.compare_with, NEEDS_REVIEW)
        return
    if "month" in ctx.q.compare_with.lower():
        t.note("COMPARE WITH month approximated as 30 days", APPROXIMATE)
    ctx.offset = " offset %s" % off
    try:
        shifted = translate_to_promql_with_offset(ctx)
    finally:
        ctx.offset = ""
    if shifted:
        shifted.legend = ((t.legend + " " if t.legend else "")
                          + "(%s earlier)" % off)
        t.extra.append(shifted)


def translate_to_promql_with_offset(ctx: _Ctx) -> Optional[Translation]:
    """Re-translate the primary select item with ctx.offset set."""
    sub = Translation(datasource="prometheus",
                      query_type="range" if ctx.is_range else "instant")
    saved_t = ctx.t
    ctx.t = sub
    try:
        items = [i for i in ctx.q.select if isinstance(i.expr, Func)]
        if not items:
            return None
        expr = _translate_item(ctx, items[0].expr)
        sub.expr = _wrap_topk(ctx, expr)
        sub.group_by = list(ctx.by)
        return sub
    except Untranslatable:
        return None
    finally:
        ctx.t = saved_t
