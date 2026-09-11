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
    APPROXIMATE, EXACT, NEEDS_REVIEW, UNTRANSLATABLE,
    Matcher, Translation, Untranslatable, cond_text, cond_to_matchers,
    facet_labels, legend_for, map_attr, render_selector, sanitize_label,
    worst,
)


# ---------------------------------------------------------------------------
# Metric source resolution
# ---------------------------------------------------------------------------

@dataclass
class MetricSource:
    """A resolved Prometheus metric family for a query."""
    base: str                 # histogram base or full metric name
    mtype: str                # 'gauge' | 'counter' | 'histogram'
    unit: str = ""            # Grafana unit id ('s', 'ms', ...)
    confidence: str = EXACT
    note: str = ""
    extra_matchers: Optional[List[Matcher]] = None

    def name(self, suffix: str = "") -> str:
        return self.base + suffix


_COUNTER_SUFFIXES = ("_total", "_count")
_HISTO_HINTS = ("duration", "latency", "_time", "response_time")


def normalize_metric_name(name: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9_:]", "_", name)
    out = re.sub(r"_+", "_", out)
    if out and out[0].isdigit():
        out = "_" + out
    return out


def resolve_metric(name: str, agg: str, cfg: Dict[str, Any],
                   t: Translation) -> MetricSource:
    """Resolve an NR metric name (FROM Metric SELECT agg(name)) to a
    Prometheus metric family, using config overrides then heuristics."""
    mm = cfg.get("metric_map", {})
    if name in mm:
        entry = mm[name]
        if isinstance(entry, str):
            entry = {"name": entry}
        return MetricSource(
            base=entry.get("name", normalize_metric_name(name)),
            mtype=entry.get("type", "gauge"),
            unit=entry.get("unit", ""),
            confidence=EXACT)

    base = normalize_metric_name(name)
    # Heuristics on name and aggregation shape.
    if base.endswith("_total"):
        return MetricSource(base, "counter", confidence=APPROXIMATE,
                            note="assumed counter from _total suffix")
    if base.endswith("_bucket"):
        return MetricSource(base[:-len("_bucket")], "histogram",
                            confidence=APPROXIMATE,
                            note="assumed histogram from _bucket suffix")
    if agg in ("percentile", "median", "histogram", "apdex"):
        mtype = "histogram"
        note = ("assumed %r is a histogram (required by %s()); add it to "
                "metric_map with the exact Prometheus name/type" % (name, agg))
    elif agg in ("rate", "count"):
        mtype = "counter"
        base_c = base
        if cfg.get("metric_total_suffix", True) \
                and not base.endswith("_total"):
            base_c = base + "_total"
        return MetricSource(base_c, "counter", confidence=NEEDS_REVIEW,
                            note="assumed counter %r; verify name/type in "
                                 "Mimir (/api/v1/metadata)" % base_c)
    elif any(h in base for h in _HISTO_HINTS) and agg in ("average",):
        mtype = "histogram"
        note = ("name suggests a duration histogram; verify %r type in "
                "Mimir" % base)
    else:
        mtype = "gauge"
        note = ("assumed gauge %r; verify metric name/type in Mimir and add "
                "to metric_map if wrong" % base)
    return MetricSource(base, mtype, confidence=NEEDS_REVIEW, note=note)


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


def _agg_expr(ctx: _Ctx, fn: Func, src: MetricSource,
              extra: Optional[List[Matcher]] = None) -> str:
    """Build the PromQL for one aggregation function against a source."""
    t = ctx.t
    W = ctx.window
    by = ctx.by_clause()
    name = fn.name
    ex = list(extra or [])
    if src.extra_matchers:
        ex.extend(src.extra_matchers)

    def hsel(suffix: str, window: Optional[str] = W) -> str:
        return ctx.selector(src.name(suffix), ex, window)

    if name in ("average", "avg"):
        if src.mtype == "histogram":
            return ("sum%s(rate(%s)) / sum%s(rate(%s))"
                    % (by, hsel("_sum"), by, hsel("_count")))
        if src.mtype == "counter":
            t.note("average() of a counter is unusual; emitted sum(rate())",
                   NEEDS_REVIEW)
            return "sum%s(rate(%s))" % (by, hsel(""))
        return "avg%s(avg_over_time(%s))" % (by, hsel(""))

    if name == "sum":
        if src.mtype == "counter":
            return "sum%s(increase(%s))" % (by, hsel(""))
        if src.mtype == "histogram":
            return "sum%s(increase(%s))" % (by, hsel("_sum"))
        t.note("sum() of a gauge: NR sums datapoints; emitted sum of "
               "per-series averages (current-total semantics)", APPROXIMATE)
        return "sum%s(avg_over_time(%s))" % (by, hsel(""))

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
        if src.mtype in ("histogram",):
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
        if src.mtype == "histogram":
            t.note("latest() on a histogram-backed source approximated as "
                   "the recent average", APPROXIMATE)
            return ("sum%s(rate(%s)) / sum%s(rate(%s))"
                    % (by, hsel("_sum", window="$__rate_interval"),
                       by, hsel("_count", window="$__rate_interval")))
        if ctx.is_range:
            return "last_over_time(%s)" % hsel("", window="$__interval") \
                if not ctx.by else \
                "max%s(last_over_time(%s))" % (by, hsel("", window="$__interval"))
        base = hsel("", window=None)
        return base if not ctx.by else "max%s(%s)" % (by, base)

    if name in ("percentile", "median"):
        pcts = [50.0] if name == "median" else [
            float(a.value) for a in fn.args[1:]
            if isinstance(a, Lit) and isinstance(a.value, (int, float))
        ] or [95.0]
        if src.mtype == "gauge":
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
        else:
            if src.mtype != "histogram":
                t.note("percentile() requires a histogram; %r resolved as %s"
                       % (src.base, src.mtype), NEEDS_REVIEW)
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
        target = "_count" if src.mtype == "histogram" else ""
        if src.mtype == "gauge":
            t.note("rate() of a gauge-backed source; emitted rate() anyway",
                   NEEDS_REVIEW)
        return "sum%s(rate(%s))%s" % (by, hsel(target), mult)

    if name == "derivative":
        per_seconds = 60.0
        for a in fn.args[1:]:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                per_seconds = float(a.value)
        mult = "" if per_seconds == 1 else " * %s" % _fmt_num(per_seconds)
        if src.mtype == "histogram":
            raise Untranslatable(
                "derivative() on a histogram-backed source has no PromQL "
                "equivalent (only _bucket/_sum/_count series exist)")
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
        if src.mtype == "histogram":
            raise Untranslatable(
                "predictLinear() on a histogram-backed source has no "
                "PromQL equivalent")
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
                   % (attr.name, label), NEEDS_REVIEW)
        t.note("uniqueCount() counts distinct label values on series — an "
               "approximation of event-level uniqueness", APPROXIMATE)
        inner_by = ", ".join([label] + ctx.by)
        metric_sel = hsel("_count" if src.mtype == "histogram" else "",
                          window=None)
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
            "values; histograms lack a sum-of-squares series)" % src.mtype)

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
            raise Untranslatable("apdex() requires a histogram metric")
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
            raise Untranslatable("histogram() requires a histogram metric")
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
_EVENT_EQUIVALENTS = {
    "pageview": _BROWSER_HINT, "pageaction": _BROWSER_HINT,
    "browserinteraction": _BROWSER_HINT, "javascripterror": _BROWSER_HINT,
    "ajaxrequest": _BROWSER_HINT,
    "mobile": _MOBILE_HINT, "mobilecrash": _MOBILE_HINT,
    "mobilerequest": _MOBILE_HINT, "mobilerequesterror": _MOBILE_HINT,
    "syntheticcheck": _SYNTH_HINT, "syntheticrequest": _SYNTH_HINT,
    "nrconsumption": _NR_ONLY_HINT, "nrusage": _NR_ONLY_HINT,
    "nrauditevent": _NR_ONLY_HINT,
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

    msg = "no metric mapping for FROM %s" % et
    hint = _EVENT_EQUIVALENTS.get(etl)
    if hint:
        msg += " (%s)" % hint
    raise Untranslatable(msg)


def _infra_lookup(ctx: _Ctx, item: SelectItem) -> Optional[Translation]:
    q = ctx.q
    if not q.from_:
        return None
    etl = q.from_[0].lower()
    fn = item.expr if isinstance(item.expr, Func) else None
    if fn is None or not fn.args or not isinstance(fn.args[0], Attr):
        # count(*) FROM K8sPodSample WHERE status='Running' style
        if fn and fn.name == "count" and etl == "k8spodsample":
            phase = _extract_eq(ctx, "status") or _extract_eq(ctx, "phase")
            if phase:
                expr = "sum%s(kube_pod_status_phase{phase=%s%s})" % (
                    ctx.by_clause(), '"%s"' % phase, _sel_tail(ctx))
                return _finish_infra(ctx, item, expr, EXACT, "short", "")
        return None
    attr = fn.args[0].name.lower().replace("_", "").replace(".", "")
    key = (etl, attr)
    hit = INFRA_MAP.get(key)
    if hit is None:
        return None
    template, conf, unit, note = hit
    if fn.name not in ("average", "latest"):
        ctx.t.note("the %s() aggregation was replaced by the canonical "
                   "exporter expression for %s.%s — verify the semantics "
                   "match the original widget" % (fn.name, etl,
                                                  fn.args[0].name),
                   NEEDS_REVIEW)
    expr = (template
            .replace("<W>", ctx.window)
            .replace("<BY>", ctx.by_clause().lstrip() + "" if ctx.by else "")
            .replace("<SEL>", _sel_tail(ctx))
            .replace("<SELBARE>", ",".join(m.render() for m in ctx.matchers)))
    expr = re.sub(r"\{,", "{", expr)  # tidy leading comma when no matchers
    expr = expr.replace("{}", "")     # drop empty matcher braces
    return _finish_infra(ctx, item, expr, conf, unit, note)


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
    if fn.name == "filter":
        inner = fn.args[0] if fn.args else None
        if not isinstance(inner, Func):
            raise Untranslatable("filter() needs an inner aggregation")
        extra = extra + _embedded_matchers(ctx, fn.where)
        inner, if_extra = _rewrite_if(ctx, inner)
        if if_extra:
            extra = extra + if_extra
        src = _source_for(ctx, SelectItem(expr=inner))
        t.confidence = worst(t.confidence, src.confidence)
        if src.note:
            t.note(src.note)
        _unit_note(t, inner.name, src)
        return _agg_expr(ctx, inner, src, extra=extra)

    if fn.name == "percentage":
        inner = fn.args[0] if fn.args else None
        if not isinstance(inner, Func):
            raise Untranslatable("percentage() needs an inner aggregation")
        num_extra = extra + _embedded_matchers(ctx, fn.where)
        src = _source_for(ctx, SelectItem(expr=inner))
        t.confidence = worst(t.confidence, src.confidence)
        if src.note:
            t.note(src.note)
        num = _agg_expr(ctx, inner, src, extra=num_extra)
        den = _agg_expr(ctx, inner, src, extra=extra)
        t.notes.append("unit:percent")
        return "100 * (%s) / (%s)" % (num, den)

    fn, if_extra = _rewrite_if(ctx, fn)
    if if_extra:
        extra = extra + if_extra
    src = _source_for(ctx, SelectItem(expr=fn))
    t.confidence = worst(t.confidence, src.confidence)
    if src.note:
        t.note(src.note)
    _unit_note(t, fn.name, src)
    return _agg_expr(ctx, fn, src, extra=extra)


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
