"""NRQL -> PromQL translation (Mimir/Prometheus, OTel-fed).

Covers FROM Metric, APM events (Transaction, ...), Span aggregations (via
span metrics), and infrastructure sample events (via node_exporter /
kube-state-metrics / cAdvisor equivalents, see :mod:`nrmetrics`).

Semantics follow the migration spec in docs/translation-spec.md:
- TIMESERIES  -> range query, window $__rate_interval
- no TIMESERIES -> instant query, window $__range (NR aggregates the whole
  SINCE window, so instant PromQL must too)
- SINCE/UNTIL -> panel/dashboard time range, never PromQL
- units are never numerically rescaled; the panel unit is set instead
- an OR across different attributes becomes a PromQL ``or`` union of the
  per-branch range-vector functions (sound: series are deduplicated by
  their full label set before aggregation)
- numeric thresholds on a histogram's own value (``count(*) WHERE
  duration > 1``) become bucket arithmetic
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..nrql.parser import (
    Attr, BinOp, BoolOp, Cmp, Func, Lit, NotOp, NrqlQuery, NullCheck,
    SelectItem, Star,
)
from .common import (
    fn_name,
    APPROXIMATE, EXACT, NEEDS_REVIEW, UNTRANSLATABLE,
    Matcher, NumericPred, Translation, Untranslatable,
    cond_text, cond_to_branches, expr_text, facet_labels, hoist_rate_filter,
    legend_for, map_attr, merge_status_bands, offset_selectors,
    render_selector, sanitize_label, select_label, unwrap_attr, worst,
)
from . import nrmetrics
from .nrmetrics import Spec
from .common import grafana_var, is_nr_variable, sanitize_label  # noqa: E402


# ---------------------------------------------------------------------------
# Metric source resolution
# ---------------------------------------------------------------------------

@dataclass
class MetricSource:
    """A resolved Prometheus metric family for a query."""
    base: str                 # histogram base or full metric name
    mtype: str                # 'gauge' | 'counter' | 'histogram' | 'rate'
    unit: str = ""            # Grafana unit id ('s', 'ms', ...)
    confidence: str = EXACT
    note: str = ""
    extra_matchers: Optional[List[Matcher]] = None
    # The NR attribute whose numeric thresholds map onto this histogram's
    # buckets (Transaction.duration -> http_server_request_duration_*).
    value_attr: str = ""
    value_scale: float = 1.0   # bucket bound = NR threshold * value_scale
    # 'count' / 'sum' when the NR name was the *.count / *.sum series of a
    # histogram: sum(x.count) is the _count series, not the _sum.
    component: str = ""
    # A counter New Relic samples as a cumulative value (restartCount):
    # max/min/sum/average read the value itself, not its increase.
    cumulative: bool = False

    def name(self, suffix: str = "") -> str:
        return self.base + suffix


@dataclass
class DerivedSource:
    """A full PromQL template (see nrmetrics.Spec kind 'expr')."""
    expr: str
    unit: str = ""
    confidence: str = NEEDS_REVIEW
    note: str = ""


_COUNTER_SUFFIXES = ("_total", "_count")
_HISTO_HINTS = ("duration", "latency", "_time", "response_time", "elapsed")


def normalize_metric_name(name: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9_:]", "_", name)
    out = re.sub(r"_+", "_", out)
    if out and out[0].isdigit():
        out = "_" + out
    return out


def _spec_to_source(spec: Spec, cfg: Dict[str, Any], t: Translation) -> Any:
    """nrmetrics.Spec -> MetricSource / DerivedSource (or raise)."""
    if spec.kind == "none":
        raise Untranslatable(spec.reason)
    if spec.kind == "http":
        src = http_server_source(cfg)
        src.confidence = worst(src.confidence, spec.conf)
        if spec.note:
            src.note = spec.note
        return src
    if spec.kind == "http-errors":
        base = http_server_source(cfg)
        return MetricSource(
            base.name("_count"), "counter", unit="short",
            confidence=spec.conf, note=spec.note,
            extra_matchers=[Matcher(map_attr("httpResponseCode", cfg)[0],
                                    "=~", "5..")])
    if spec.kind == "expr":
        return DerivedSource(spec.expr, spec.unit, spec.conf, spec.note)
    if spec.kind == "count":
        sel = _with_fixed(spec.name, spec.matchers)
        if "{" not in sel:
            sel += "{<SELBARE>}"  # a bare name takes the WHERE matchers
        return DerivedSource("count <BY>(%s)" % sel, "short", spec.conf,
                             spec.note or "")
    matchers = [Matcher(l, o, v) for l, o, v in spec.matchers] or None
    return MetricSource(spec.name, spec.kind, unit=spec.unit,
                        confidence=spec.conf, note=spec.note,
                        extra_matchers=matchers,
                        cumulative=bool(getattr(spec, "cumulative", False)))


def _with_fixed(name: str, matchers: List[Tuple[str, str, str]]) -> str:
    """name{fixed<SEL>} rendering helper for count specs."""
    if not matchers:
        return name
    inner = ",".join(Matcher(l, o, v).render() for l, o, v in matchers)
    return "%s{%s<SEL>}" % (name, inner)


def resolve_metric(name: str, agg: str, cfg: Dict[str, Any],
                   t: Translation) -> Any:
    """Resolve an NR metric name (FROM Metric SELECT agg(name)) to a
    Prometheus metric family: config overrides, then built-in knowledge of
    New Relic's own metric names, then heuristics."""
    mm = cfg.get("metric_map", {})
    entry = mm.get(name)
    if entry is None:
        low = name.lower()
        for k, v in mm.items():
            if k.lower() == low:
                entry = v
                break
    if entry is not None:
        if isinstance(entry, str):
            # A bare name: infer the type from its suffix.
            entry = {"name": entry,
                     "type": "counter" if entry.endswith("_total")
                     else "gauge"}
        mtype = entry.get("type", "gauge")
        matchers = None
        if isinstance(entry.get("matchers"), dict):
            matchers = [Matcher(k, "=", str(v))
                        for k, v in entry["matchers"].items()]
        if entry.get("expr"):
            return DerivedSource(entry["expr"], entry.get("unit", ""),
                                 EXACT, "from metric_map")
        return MetricSource(
            base=entry.get("name", normalize_metric_name(name)),
            mtype=mtype, unit=entry.get("unit", ""),
            confidence=EXACT, extra_matchers=matchers)

    spec = nrmetrics.metric_spec(name, agg)
    if spec is not None:
        return _spec_to_source(spec, cfg, t)
    low = name.lower()
    for suffix in (".count", ".sum"):
        if low.endswith(suffix):
            # http.server.requests.count: the _count series of a known
            # histogram / timer.
            spec = nrmetrics.metric_spec(name[:-len(suffix)], agg)
            if spec is not None and spec.kind == "histogram":
                src = _spec_to_source(spec, cfg, t)
                if isinstance(src, MetricSource):
                    src.component = suffix[1:]
                return src

    base = normalize_metric_name(name)
    # Heuristics on name and aggregation shape.
    if base.endswith("_total"):
        return MetricSource(base, "counter", confidence=APPROXIMATE,
                            note="assumed counter from _total suffix")
    if base.endswith("_bucket"):
        return MetricSource(base[:-len("_bucket")], "histogram",
                            confidence=APPROXIMATE,
                            note="assumed histogram from _bucket suffix")
    if base.endswith("_sum") or base.endswith("_count"):
        stem = base.rsplit("_", 1)[0]
        if any(h in stem for h in _HISTO_HINTS):
            return MetricSource(stem, "histogram", confidence=NEEDS_REVIEW,
                                note="assumed %r is the _sum/_count of "
                                     "histogram %r" % (base, stem),
                                component=base.rsplit("_", 1)[1])
        # threads.count, custom.count: an OTel UpDownCounter/gauge keeps
        # its name; nothing says a histogram is behind it.
        return MetricSource(
            base, "gauge", confidence=NEEDS_REVIEW,
            note="assumed gauge %r (a *.count/*.sum metric name keeps its "
                 "name in Prometheus); if it is the _count/_sum series of a "
                 "histogram, add the histogram to metric_map" % base)
    looks_histo = any(h in base for h in _HISTO_HINTS)
    if agg in ("percentile", "median"):
        # percentile()/median() need per-event data. A real histogram gives
        # it (a *_bucket family); a plain gauge does not. Only assume a
        # histogram when the NAME looks like one (a _bucket suffix was
        # already handled above; here we accept duration/latency hints).
        # Otherwise assuming a histogram would emit histogram_quantile over
        # a nonexistent %s_bucket family and return NO DATA — so resolve as
        # a gauge and let _agg_expr take quantile_over_time of the raw
        # samples, a sound approximation.
        if looks_histo:
            return MetricSource(
                base, "histogram", confidence=NEEDS_REVIEW,
                note="name suggests a duration histogram (required by "
                     "%s()); verify %r is a histogram in Mimir or add it to "
                     "metric_map with type: histogram" % (agg, base))
        return MetricSource(
            base, "gauge", confidence=NEEDS_REVIEW,
            note="%s() of %r: no *_bucket histogram is known, so the "
                 "quantile is taken over the raw gauge samples via "
                 "quantile_over_time; if %r is actually a histogram add it "
                 "to metric_map with type: histogram" % (agg, base, base))
    if agg in ("histogram", "apdex"):
        return MetricSource(
            base, "histogram", confidence=NEEDS_REVIEW,
            note="assumed %r is a histogram (required by %s()); add it to "
                 "metric_map with the exact Prometheus name/type"
                 % (name, agg))
    if looks_histo and agg in ("average", "avg", "max", "min", "count",
                               "rate", "sum"):
        return MetricSource(
            base, "histogram", confidence=NEEDS_REVIEW,
            note="name suggests a duration histogram; verify %r type in "
                 "Mimir (if it is a plain gauge, add it to metric_map with "
                 "type: gauge)" % base)
    if agg in ("rate", "count", "sum"):
        base_c = base
        if cfg.get("metric_total_suffix", True) \
                and not base.endswith("_total"):
            base_c = base + "_total"
        return MetricSource(
            base_c, "counter", confidence=NEEDS_REVIEW,
            note="assumed counter %r (NR %s() is normally used on count-"
                 "type metrics); verify name/type in Mimir "
                 "(/api/v1/metadata) — if it is a gauge, add it to "
                 "metric_map with type: gauge" % (base_c, agg))
    return MetricSource(
        base, "gauge", confidence=NEEDS_REVIEW,
        note="assumed gauge %r; verify metric name/type in Mimir and add "
             "to metric_map if wrong" % base)


# APM events -> semconv HTTP server metrics.
def http_server_source(cfg: Dict[str, Any]) -> MetricSource:
    override = (cfg.get("http_metrics") or {}).get("duration_histogram")
    if override:
        unit = (cfg.get("http_metrics") or {}).get("unit") or "s"
        return MetricSource(override, "histogram", unit=unit,
                            confidence=APPROXIMATE,
                            note="HTTP server duration histogram from "
                                 "config (http_metrics)",
                            value_attr="duration",
                            value_scale=1000.0 if unit == "ms" else 1.0)
    if cfg.get("http_metrics_flavor", "semconv") == "legacy":
        return MetricSource("http_server_duration_milliseconds", "histogram",
                            unit="ms", confidence=APPROXIMATE,
                            note="legacy OTel semconv HTTP metric",
                            value_attr="duration", value_scale=1000.0)
    return MetricSource("http_server_request_duration_seconds", "histogram",
                        unit="s", confidence=APPROXIMATE,
                        note="OTel semconv HTTP server duration histogram",
                        value_attr="duration")


def db_client_source(cfg: Dict[str, Any]) -> MetricSource:
    return MetricSource("db_client_operation_duration_seconds", "histogram",
                        unit="s", confidence=NEEDS_REVIEW,
                        note="OTel semconv db.client.operation.duration "
                             "histogram", value_attr="databaseDuration")


def http_client_source(cfg: Dict[str, Any]) -> MetricSource:
    return MetricSource("http_client_request_duration_seconds", "histogram",
                        unit="s", confidence=NEEDS_REVIEW,
                        note="OTel semconv http.client.request.duration "
                             "histogram", value_attr="externalDuration")


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
                                 "exists in Mimir" % flavor,
                            value_attr="duration.ms",
                            value_scale=1.0 if unit == "ms" else 0.001)
    name = overrides.get("calls_total") or {
        "otel": "traces_span_metrics_calls_total",
        "otel-seconds": "traces_span_metrics_calls_total",
        "tempo": "traces_spanmetrics_calls_total",
        "legacy": "calls_total",
    }.get(flavor, "traces_span_metrics_calls_total")
    return MetricSource(name, "counter", confidence=NEEDS_REVIEW,
                        note="span-metrics naming is deployment-specific "
                             "(spanmetrics_flavor=%s)" % flavor)


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


def nr_duration_seconds(text: str) -> Optional[float]:
    m = _AGO_RE.match(text or "")
    if not m:
        return None
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]


_SPECIAL_RANGES = {
    "today": "now/d", "this week": "now/w", "this month": "now/M",
    "this year": "now/y", "yesterday": "now-1d/d", "last week": "now-1w/w",
    "last month": "now-1M/M", "last year": "now-1y/y",
    "monday": "now/w", "now": "now",
}


def nr_duration_to_grafana_range(text: str) -> Optional[str]:
    """'30 minutes ago' -> 'now-30m'."""
    key = re.sub(r"\s+", " ", (text or "").strip().lower())
    if key in _SPECIAL_RANGES:
        return _SPECIAL_RANGES[key]
    d = nr_duration_to_prom(text)
    return "now-%s" % d if d else None


# ---------------------------------------------------------------------------
# Expression building
# ---------------------------------------------------------------------------

_TXN_TYPE_ATTRS = ("transactiontype", "transactionsubtype",
                   "transaction_type", "transaction_sub_type")

# Transaction attributes every HTTP request carries (`x IS NOT NULL` is a
# no-op on the server-duration histogram) and the ones that only some
# transactions carry (a DB / external call happened) which the histogram
# cannot tell apart.
_HTTP_ALWAYS_PRESENT = ("duration", "totaltime", "webduration", "name",
                        "transactionname", "error", "http_route",
                        "span_name")
_HTTP_SOMETIMES_PRESENT = ("databaseduration", "externalduration",
                          "databasecallcount", "externalcallcount",
                          "queueduration")


def _drop_present_checks(cond: Any, t: Translation) -> Any:
    """Rewrite `duration IS NOT NULL` (always true on HTTP metrics) into a
    constant predicate before matcher building, so no label is looked up
    for it."""
    if isinstance(cond, BoolOp):
        return BoolOp(cond.op, [_drop_present_checks(c, t)
                                for c in cond.items])
    if isinstance(cond, NotOp):
        return NotOp(_drop_present_checks(cond.item, t))
    if isinstance(cond, NullCheck) and cond.negated:
        attr, _ci = unwrap_attr(cond.left)
        if attr is not None and attr.name.lower() in _HTTP_ALWAYS_PRESENT:
            t.notes.append("%s IS NOT NULL is always true on HTTP server "
                           "metrics (every request carries it); dropped"
                           % attr.name)
            return Cmp(Lit(True), "=", Lit(True))
    return cond


def _http_fixups(matchers: List[Matcher], t: Translation,
                 cfg: Optional[Dict[str, Any]] = None) -> List[Matcher]:
    """OTel semconv HTTP server metric label conventions for FROM
    Transaction sources: the NR `error` flag and transaction `name` do not
    exist as labels there; transactionType = 'Web' is implicit."""
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
        elif m.label.lower() in _TXN_TYPE_ATTRS:
            val = m.value.lower()
            is_web = (val == "web") == (m.op in ("=", "=~"))
            if is_web:
                t.notes.append("transactionType = 'Web' is implicit for HTTP "
                               "server metrics; filter dropped")
            else:
                raise Untranslatable(
                    "non-web transactions (transactionType %s %r) have no "
                    "HTTP-server metric equivalent; rebuild this panel on "
                    "the messaging.* / rpc.* OTel metrics your background "
                    "workers emit" % (m.op, m.value))
        elif m.label.lower() in ("error_expected", "error.expected"):
            t.note("error.expected has no label on OTel HTTP metrics; every "
                   "5xx response counts (expected errors included)",
                   NEEDS_REVIEW)
            continue
        elif m.op == "!=" and m.value == "" \
                and m.label.lower() in _HTTP_ALWAYS_PRESENT:
            # `x IS NOT NULL` on a value every request carries: no filter.
            continue
        elif m.op in ("!=", "=") and m.value == "" \
                and m.label.lower() in _HTTP_SOMETIMES_PRESENT:
            t.note("%s IS %sNULL selects transactions by whether they made "
                   "a DB / external call; the HTTP server histogram cannot "
                   "tell them apart — filter dropped"
                   % (m.label, "NOT " if m.op == "!=" else ""),
                   NEEDS_REVIEW)
            continue
        else:
            out.append(m)
    return out


def _span_fixups(matchers: List[Matcher],
                 cfg: Optional[Dict[str, Any]] = None,
                 t: Optional[Translation] = None) -> List[Matcher]:
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
        elif m.label in ("otel_status_code", "status", "status_code") \
                and m.value.upper() in ("ERROR", "STATUS_CODE_ERROR"):
            out.append(Matcher("status_code", m.op if m.op in ("=", "!=")
                               else "=", "STATUS_CODE_ERROR"))
        elif m.label in ("otel_status_code", "status", "status_code") \
                and m.value.upper() in ("OK", "STATUS_CODE_OK", "UNSET",
                                        "STATUS_CODE_UNSET"):
            code = "STATUS_CODE_OK" if "OK" in m.value.upper() \
                else "STATUS_CODE_UNSET"
            out.append(Matcher("status_code", m.op if m.op in ("=", "!=")
                               else "=", code))
        elif m.label in ("span_kind", "kind") and _kind_values(m.value):
            # span.kind = 'server' -> span_kind="SPAN_KIND_SERVER" (the
            # OTel spanmetrics connector and Tempo both emit the enum name).
            out.append(Matcher("span_kind", m.op, _kind_values(m.value)))
        elif m.label.lower() in _SPAN_ROOT_LABELS and m.value in ("", "true",
                                                                "false"):
            # parentId IS NULL / nr.entryPoint IS TRUE: root spans. Span
            # metrics carry no parent label; entry spans are the server and
            # consumer kinds.
            wants_root = (m.value == "" and m.op == "=") or (
                m.value == "true" and m.op in ("=", "=~")) or (
                m.value == "false" and m.op in ("!=", "!~"))
            out.append(Matcher("span_kind", "=~" if wants_root else "!~",
                               "SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"))
            if t is not None:
                t.note("%s (root spans) approximated as server/consumer "
                       "span kinds on span metrics (no parent label); use "
                       "span_aggregations: traceql for the exact root-span "
                       "filter" % m.label, APPROXIMATE)
        else:
            out.append(m)
    return out


_SPAN_ROOT_LABELS = {"parentid", "parent_id", "parent.id", "parentspanid",
                     "parent_span_id", "nr_entrypoint", "nr.entrypoint",
                     "entrypoint"}


_SPAN_KINDS = ("server", "client", "producer", "consumer", "internal")


def _kind_values(value: str) -> str:
    """'server' / 'server|client' -> SPAN_KIND_SERVER[|SPAN_KIND_CLIENT]."""
    parts = [p.strip() for p in value.split("|")]
    if parts and all(p.lower() in _SPAN_KINDS for p in parts):
        return "|".join("SPAN_KIND_" + p.upper() for p in parts)
    return ""


def _legacy_aws_fixups(ctx: "_Ctx", matchers: List[Matcher]) -> List[Matcher]:
    """Legacy AWS sample events: `provider` picks the CloudWatch namespace
    (consumed, not a label); label.<Tag> attributes are YACE tag_<Tag>."""
    out: List[Matcher] = []
    for m in matchers:
        if m.label == "provider":
            value = m.value[4:] if m.value.startswith("(?i)") else m.value
            if m.op in ("=", "=~") and "|" not in value \
                    and not value.startswith("$"):
                ctx.aws_provider = value.replace("\\", "")
            else:
                ctx.t.note("WHERE provider %s %r does not name exactly one "
                           "AWS resource type; the CloudWatch namespace "
                           "could not be chosen — split the query per "
                           "resource type" % (m.op, m.value), NEEDS_REVIEW)
            continue
        if m.label.startswith("label_"):
            out.append(Matcher("tag_" + m.label[len("label_"):], m.op,
                               m.value))
            continue
        out.append(m)
    return out


_AGG_WORD = {"average": "avg", "avg": "avg", "latest": "avg", "sum": "sum",
             "max": "max", "min": "min", "count": "sum", "uniquecount": "avg",
             "median": "avg", "percentile": "avg", "rate": "sum",
             "earliest": "avg"}
_AGG_INV = {"avg": "avg", "sum": "sum", "max": "min", "min": "max"}


def _metric_names(q: NrqlQuery) -> List[str]:
    """Every FROM Metric name referenced by the SELECT items."""
    out: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Func):
            for a in node.args:
                walk(a)
        else:
            attr, _ = unwrap_attr(node)
            if attr is not None:
                out.append(attr.name)
            elif isinstance(node, Lit) and isinstance(node.value, str):
                out.append(node.value)
    for item in q.select:
        walk(item.expr)
    return out


def _uses_apm_http_metric(q: NrqlQuery) -> bool:
    if not q.from_ or q.from_[0].lower() != "metric":
        return False
    for name in _metric_names(q):
        spec = nrmetrics.METRICS.get(name.lower())
        if spec is not None and spec.kind in ("http", "http-errors"):
            return True
        if name.lower().startswith("newrelic.goldenmetrics.apm."):
            return True
    return False


class _Ctx:
    _STATIC = ("q", "cfg", "t", "initial")

    def snapshot(self) -> Dict[str, Any]:
        """Copy of the mutable translation state (matchers, facet labels,
        numeric predicates); translating an item consumes parts of it."""
        return {k: copy.deepcopy(v) for k, v in self.__dict__.items()
                if k not in self._STATIC}

    def restore(self, snap: Dict[str, Any]) -> None:
        for k, v in snap.items():
            setattr(self, k, copy.deepcopy(v))

    def __init__(self, q: NrqlQuery, cfg: Dict[str, Any], t: Translation):
        self.q = q
        self.t = t
        self.initial: Optional[Dict[str, Any]] = None
        self.is_range = q.timeseries is not None
        self.window = "$__rate_interval" if self.is_range else "$__range"
        self.event = (q.from_[0] if q.from_ else "Metric")
        etl = self.event.lower()
        # Event-specific attribute -> label conventions overlay the
        # generic label_map while translating this event type.
        overlay = dict(nrmetrics.EVENT_LABELS.get(etl) or {})
        if etl == "metric":
            overlay.setdefault("metricName", "__name__")
        if etl in nrmetrics.LEGACY_AWS_EVENTS:
            # label.<Tag> attributes of AWS samples are YACE tag_<Tag> labels.
            for tag in set(re.findall(r"\blabel\.([A-Za-z0-9_]+)", q.raw or "")):
                overlay["label." + tag] = "tag_" + tag
        if overlay:
            cfg = dict(cfg)
            merged = dict(cfg.get("label_map") or {})
            merged.update(overlay)
            cfg["label_map"] = merged
        self.cfg = cfg
        self.is_metric_event = etl == "metric"
        self.is_span = etl in ("span", "distributedtrace",
                               "distributedtracesummary")
        self.is_apm_http = etl in ("transaction", "transactionerror") \
            or _uses_apm_http_metric(q)
        self.is_legacy_aws = etl in nrmetrics.LEGACY_AWS_EVENTS
        self.aws_provider = ""  # WHERE provider = '...' on legacy AWS events
        where = q.where
        if self.is_apm_http and where is not None:
            where = _drop_present_checks(where, t)
        self.branches = cond_to_branches(where, cfg, t)
        # Numeric predicates captured while building the matchers belong
        # to the outer WHERE; each SELECT item may consume a copy.
        self.numeric: List[NumericPred] = list(t.numeric)
        del t.numeric[:]
        # Predicates a derived (template) source turned into PromQL filters.
        self.consumed_numeric: List[NumericPred] = []
        self.branches = [self._fixups(b) for b in self.branches]
        self.branches = [list({(m.label, m.op, m.value): m for m in b}.values())
                         for b in self.branches]
        self.branches = [merge_status_bands(b) for b in self.branches]
        if nrmetrics.is_infra_event(etl):
            for b in self.branches:
                for m in list(b):
                    spec = nrmetrics.infra_lookup(etl, m.label)
                    if spec is not None and spec.kind in (
                            "gauge", "counter", "rate", "histogram", "expr") \
                            and m.label.lower() not in ("status", "phase",
                                                        "state", "reason"):
                        b.remove(m)
                        if m.op in ("=", "!=") and re.fullmatch(
                                r"-?\d+(\.\d+)?", m.value):
                            # isReady = 0: a numeric comparison on the
                            # attribute's series (see _numeric_count_filters)
                            self.numeric.append(NumericPred(
                                m.label, m.label, m.op, float(m.value)))
                            continue
                        t.note("WHERE %s %s %r compares a metric-valued "
                               "attribute; the exporter carries it as a "
                               "metric (%s), not a label — express it as a "
                               "PromQL join manually; filter dropped"
                               % (m.label, m.op, m.value,
                                  spec.name or "a derived expression"),
                               NEEDS_REVIEW)
        if self.is_metric_event:
            for b in self.branches:
                for m in list(b):
                    if m.label in _NR_INTERNAL_ATTRS:
                        b.remove(m)
                        t.note("WHERE %s %s %r is New Relic ingest metadata "
                               "with no Prometheus label; dropped"
                               % (m.label, m.op, m.value), APPROXIMATE)
        if self.is_metric_event:
            for b in self.branches:
                for i, m in enumerate(b):
                    if m.label == "__name__" and m.op in ("=~", "!~") \
                            and not m.value.startswith("${"):
                        # metricName LIKE 'custom.%' -> custom_.* in Prom
                        b[i] = Matcher(m.label, m.op,
                                       m.value.replace("\\.", "_"))
        if len(self.branches) > 1:
            t.note("WHERE contains an OR across different attributes; "
                   "translated as a PromQL `or` union of %d filtered "
                   "selectors (series are deduplicated before aggregation)"
                   % len(self.branches), APPROXIMATE)
        self.by = facet_labels(q, cfg, t)
        if self.is_legacy_aws:
            self.by = ["tag_" + l[len("label_"):] if l.startswith("label_")
                       else l for l in self.by]
        if self.is_apm_http:
            if "span_name" in self.by or any(
                    src == "span_name" for _n, _r, src, _x in t.label_replace):
                self.by = ["http_route" if l == "span_name" else l
                           for l in self.by]
                t.label_replace = [(n, r, "http_route" if src == "span_name"
                                    else src, x)
                                   for n, r, src, x in t.label_replace]
                t.legend_template = t.legend_template.replace(
                    "{{span_name}}", "{{http_route}}")
                t.note("FACET on transaction name grouped by http_route on "
                       "semconv HTTP metrics", NEEDS_REVIEW)
            for l in list(self.by):
                if l.lower() in _TXN_TYPE_ATTRS:
                    self.by.remove(l)
                    t.note("FACET transactionType has no label on HTTP "
                           "server metrics (they are all web transactions); "
                           "grouping dropped", NEEDS_REVIEW)
        svc = cfg.get("span_service_label") or ""
        if self.is_span and svc and "service_name" in self.by:
            self.by = [svc if l == "service_name" else l for l in self.by]
        self.offset = ""  # ' offset 1w' when building COMPARE WITH targets

    @property
    def matchers(self) -> List[Matcher]:
        """First branch (the only one for AND-only WHERE clauses)."""
        return self.branches[0]

    def _fixups(self, matchers: List[Matcher]) -> List[Matcher]:
        if self.is_legacy_aws:
            return _legacy_aws_fixups(self, matchers)
        if self.is_span:
            return _span_fixups(matchers, self.cfg, self.t)
        if self.is_apm_http:
            return _http_fixups(matchers, self.t, self.cfg)
        return matchers

    def fixup_extra(self, matchers: List[Matcher]) -> List[Matcher]:
        return self._fixups(matchers)

    # -- rendering helpers ------------------------------------------------

    def rf(self, fn: str, name: str,
           extra: Optional[List[List[Matcher]]] = None,
           window: Optional[str] = None, offset: bool = True,
           prefix: str = "", suffix: str = "") -> str:
        """Render ``fn(prefix name{matchers}[window] suffix)`` (``fn``
        empty for a bare instant selector), unioned with PromQL ``or``
        across the WHERE branches and any embedded-filter branches."""
        parts: List[str] = []
        seen = set()
        for b in self.branches:
            for e in (extra or [[]]):
                sel = render_selector(name, b + e)
                if window:
                    sel += "[%s]" % window
                if offset and self.offset:
                    sel += self.offset
                rendered = "%s(%s%s%s)" % (fn, prefix, sel, suffix) if fn \
                    else sel + suffix
                if rendered not in seen:
                    seen.add(rendered)
                    parts.append(rendered)
        expr = parts[0] if len(parts) == 1 else "(" + " or ".join(parts) + ")"
        return self._label_replace(expr)

    def _label_replace(self, expr: str) -> str:
        for new, ref, src, rx in self.t.label_replace:
            expr = 'label_replace(%s, "%s", "%s", "%s", "%s")' % (
                expr, new, ref, src, rx.replace("\\", "\\\\")
                .replace('"', '\\"'))
        return expr

    def sel_tail(self, extra: Optional[List[Matcher]] = None) -> str:
        rendered = ",".join(m.render() for m in self.matchers + (extra or []))
        return ("," + rendered) if rendered else ""

    def sel_bare(self, extra: Optional[List[Matcher]] = None) -> str:
        return ",".join(m.render() for m in self.matchers + (extra or []))

    def by_clause(self, extra_labels: Optional[List[str]] = None) -> str:
        labels = list(dict.fromkeys((extra_labels or []) + self.by))
        return " by (%s)" % ", ".join(labels) if labels else ""

    def unit_hint(self, unit: str) -> None:
        if unit:
            self.t.notes.append("unit:%s" % unit)


# NR's TIMESERIES counts events per bucket. In a Grafana range query the
# per-bucket count is the per-second rate times the step width: rate() over
# $__rate_interval is robust to scrape gaps, while increase() over
# $__rate_interval over-counts by (rate_interval / interval).
_PER_STEP = " * $__interval_ms / 1000"


def _cancel_per_step(a: str, b: str) -> Tuple[str, str]:
    """Both operands of a ratio carry the per-step factor -> it cancels."""
    if a.endswith(_PER_STEP) and b.endswith(_PER_STEP):
        return a[:-len(_PER_STEP)], b[:-len(_PER_STEP)]
    return a, b


def _wrap_topk(ctx: _Ctx, expr: str) -> str:
    limit = ctx.q.limit
    is_var = isinstance(limit, str) and limit.startswith("$")
    if ctx.q.facet and (isinstance(limit, int) or is_var):
        fn = "topk"
        if ctx.q.order_by is not None \
                and str(ctx.q.order_by.direction).upper() == "ASC":
            fn = "bottomk"  # ORDER BY ... ASC keeps the smallest groups
        if ctx.is_range:
            ctx.t.note("FACET LIMIT %s became %s(%s, ...); on range queries "
                       "%s is evaluated per step so series may flicker"
                       % (limit, fn, limit, fn), APPROXIMATE)
        return "%s(%s, %s)" % (fn, limit, expr)
    return expr


def _fmt_q(p: float) -> str:
    v = p / 100.0
    s = ("%f" % v).rstrip("0").rstrip(".")
    return s or "0"


def _fmt_num(n: float) -> str:
    if n == int(n):
        return str(int(n))
    return repr(float(n))  # shortest exact text (PromQL accepts 1e-07)


def _scale_text(mult: float) -> str:
    """'* 1000', or '/ 60' when the factor is the reciprocal of an integer
    (keeps 1/60 exact instead of a rounded decimal)."""
    inv = 1.0 / mult if mult else 0.0
    if mult and abs(inv) >= 2 and abs(inv - round(inv)) < 1e-9 * abs(inv):
        return "/ %d" % int(round(inv))
    return "* %s" % _fmt_num(mult)


def _le_matcher(bound: float) -> Matcher:
    """Bucket-bound matcher robust to both le-label spellings.

    Prometheus text format renders integral bounds as le="2" while
    OpenMetrics renders le="2.0"; exact-match on one spelling silently
    returns no data on stacks using the other.
    """
    if bound == int(bound):
        return Matcher("le", "=~", "%d|%d\\.0" % (int(bound), int(bound)))
    return Matcher("le", "=", _fmt_num(bound))


def _take_thresholds(numeric: List[NumericPred], src: MetricSource) \
        -> Tuple[Optional[float], Optional[float], List[NumericPred]]:
    """Pop the numeric predicates on the source's own value attribute.
    -> (lower bound or None, upper bound or None, remaining)."""
    lo: Optional[float] = None
    hi: Optional[float] = None
    rest: List[NumericPred] = []
    want = src.value_attr.lower().replace("_", "").replace(".", "")
    for p in numeric:
        key = p.attr.lower().replace("_", "").replace(".", "")
        if want and key in (want, "duration", "durationms") \
                and src.mtype == "histogram":
            v = p.value * src.value_scale
            if p.op in (">", ">="):
                lo = v if lo is None else max(lo, v)
            else:
                hi = v if hi is None else min(hi, v)
        else:
            rest.append(p)
    return lo, hi, rest


def _agg_expr(ctx: _Ctx, fn: Func, src: MetricSource,
              extra: Optional[List[List[Matcher]]] = None,
              numeric: Optional[List[NumericPred]] = None) -> str:
    """Build the PromQL for one aggregation function against a source."""
    t = ctx.t
    W = ctx.window
    by = ctx.by_clause()
    name = fn.name
    ex: List[List[Matcher]] = [list(b) for b in (extra or [[]])]
    if src.extra_matchers:
        ex = [b + list(src.extra_matchers) for b in ex]
    numeric = list(numeric or [])

    def hsel(suffix: str, window: Optional[str] = W, fnname: str = "",
             more: Optional[List[Matcher]] = None, prefix: str = "",
             tail: str = "") -> str:
        e = ex if not more else [b + list(more) for b in ex]
        return ctx.rf(fnname, src.name(suffix), e, window, prefix=prefix,
                      suffix=tail)

    def leftover(preds: List[NumericPred]) -> None:
        for p in preds:
            t.note("numeric comparison %s %s %s cannot become a label "
                   "matcher for this metric; dropped — apply it manually"
                   % (p.attr, p.op, _fmt_num(p.value)), NEEDS_REVIEW)

    # Windowed counts: see _PER_STEP.
    inc = "rate" if ctx.is_range else "increase"

    def per_step(expr: str) -> str:
        return expr + _PER_STEP if ctx.is_range else expr

    if name in ("average", "avg"):
        leftover(numeric)
        if src.mtype == "counter" and src.cumulative:
            return "avg%s(%s)" % (by, hsel("", fnname="avg_over_time"))
        if src.mtype == "histogram":
            return ("sum%s(%s) / sum%s(%s)"
                    % (by, hsel("_sum", fnname="rate"),
                       by, hsel("_count", fnname="rate")))
        if src.mtype == "counter":
            t.note("average() of a counter is unusual; emitted sum(rate())",
                   NEEDS_REVIEW)
            return "sum%s(%s)" % (by, hsel("", fnname="rate"))
        if src.mtype == "rate":
            return "avg%s(%s)" % (by, hsel("", fnname="rate"))
        return "avg%s(%s)" % (by, hsel("", fnname="avg_over_time"))

    if name == "sum":
        leftover(numeric)
        if src.mtype == "counter" and src.cumulative:
            t.note("sum() of a cumulative sampled counter: the sum of the "
                   "current values across the group (New Relic summed every "
                   "sample, a multiple of this); rate() gives increases",
                   APPROXIMATE)
            return "sum%s(%s)" % (by, hsel(
                "", window="$__interval" if ctx.is_range else "$__range",
                fnname="last_over_time"))
        if src.mtype == "counter":
            return per_step("sum%s(%s)" % (by, hsel("", fnname=inc)))
        if src.mtype == "histogram":
            part = "_count" if src.component == "count" else "_sum"
            return per_step("sum%s(%s)" % (by, hsel(part, fnname=inc)))
        if src.mtype == "rate":
            return "sum%s(%s)" % (by, hsel("", fnname="rate"))
        if src.base.startswith("aws_") and (
                src.base.endswith("_sum") or src.base.endswith("_sample_count")):
            win = "$__interval" if ctx.is_range else "$__range"
            t.note("sum() of a CloudWatch Sum/SampleCount statistic adds the "
                   "per-period datapoints in each Grafana step (one datapoint "
                   "per CloudWatch period)", APPROXIMATE)
            return "sum%s(%s)" % (by, hsel("", window=win,
                                          fnname="sum_over_time"))
        t.note("sum() of a gauge: NR sums datapoints; emitted sum of "
               "per-series averages (current-total semantics)", APPROXIMATE)
        return "sum%s(%s)" % (by, hsel("", fnname="avg_over_time"))

    if name in ("max", "min"):
        leftover(numeric)
        if src.mtype == "histogram":
            if name == "max":
                t.note("max() from a histogram is the top bucket bound "
                       "(overestimate)", APPROXIMATE)
                return ("histogram_quantile(1, sum by (le%s)(%s))"
                        % ("".join(", " + l for l in ctx.by),
                           hsel("_bucket", fnname="rate")))
            t.note("min() cannot be derived from a histogram; emitted p0 "
                   "(lowest bucket bound)", NEEDS_REVIEW)
            return ("histogram_quantile(0, sum by (le%s)(%s))"
                    % ("".join(", " + l for l in ctx.by),
                       hsel("_bucket", fnname="rate")))
        if src.mtype == "rate":
            t.note("%s() of a per-second rate is approximated as the %s "
                   "across series of the windowed rate" % (name, name),
                   APPROXIMATE)
            return "%s%s(%s)" % (name, by, hsel("", fnname="rate"))
        if src.mtype == "counter" and not src.cumulative:
            t.note("%s() of a counter: emitted %s of the windowed increase"
                   % (name, name), APPROXIMATE)
            return per_step("%s%s(%s)" % (name, by, hsel("", fnname=inc)))
        fname = "max_over_time" if name == "max" else "min_over_time"
        return "%s%s(%s)" % (name, by, hsel("", fnname=fname))

    if name == "count":
        lo, hi, rest = _take_thresholds(numeric, src)
        leftover(rest)
        if src.mtype == "histogram":
            if lo is None and hi is None:
                return per_step("sum%s(%s)" % (by, hsel("_count", fnname=inc)))
            t.note("the duration threshold became histogram bucket "
                   "arithmetic; it is exact only if a bucket boundary "
                   "exists at %s — verify your histogram buckets"
                   % " and ".join(_fmt_num(v) for v in (lo, hi)
                                  if v is not None), NEEDS_REVIEW)
            if lo is None:
                return per_step("sum%s(%s)" % (by, hsel(
                    "_bucket", fnname=inc, more=[_le_matcher(hi)])))
            upper = ("sum%s(%s)" % (by, hsel("_bucket", fnname=inc,
                                             more=[_le_matcher(hi)]))
                     if hi is not None
                     else "sum%s(%s)" % (by, hsel("_count", fnname=inc)))
            diff = "%s - sum%s(%s)" % (
                upper, by, hsel("_bucket", fnname=inc,
                                more=[_le_matcher(lo)]))
            return per_step("(%s)" % diff) if ctx.is_range else diff
        if src.mtype == "counter":
            if ctx.is_metric_event:
                t.note("count() on a Metric counter emitted as the summed "
                       "increase (event count); NRQL count() strictly counts "
                       "datapoints — use sum() in NR to compare like for "
                       "like", APPROXIMATE)
            return per_step("sum%s(%s)" % (by, hsel("", fnname=inc)))
        if src.mtype == "rate":
            t.note("count() of a per-second rate source counts series, not "
                   "events", NEEDS_REVIEW)
            return "count%s(%s)" % (by, hsel("", window=None))
        if ctx.is_metric_event:
            win = "$__interval" if ctx.is_range else "$__range"
            t.note("count() of a gauge metric counts its datapoints per "
                   "step (count_over_time)", APPROXIMATE)
            return "sum%s(%s)" % (by, hsel("", window=win,
                                          fnname="count_over_time"))
        t.note("count(*) of a gauge-backed source counts series, not events",
               NEEDS_REVIEW)
        return "count%s(%s)" % (by, hsel("", window=None))

    if name in ("latest",):
        leftover(numeric)
        if src.mtype == "histogram":
            t.note("latest() on a histogram-backed source approximated as "
                   "the recent average", APPROXIMATE)
            return ("sum%s(%s) / sum%s(%s)"
                    % (by, hsel("_sum", window="$__rate_interval",
                                fnname="rate"),
                       by, hsel("_count", window="$__rate_interval",
                                fnname="rate")))
        if src.mtype == "rate":
            inner = hsel("", window="$__rate_interval", fnname="rate")
            return "avg%s(%s)" % (by, inner) if ctx.by else \
                ("sum(%s)" % inner if not ctx.is_range else inner)
        if src.mtype == "counter":
            t.note("latest() of a counter is its raw cumulative value",
                   APPROXIMATE)
        if ctx.is_range:
            inner = hsel("", window="$__interval", fnname="last_over_time")
            return inner if not ctx.by else "max%s(%s)" % (by, inner)
        base = hsel("", window=None)
        return base if not ctx.by else "max%s(%s)" % (by, base)

    if name == "bucketpercentile":
        name = "percentile"  # NR's histogram-bucket percentile
    if name == "getcdfvalue":
        leftover(numeric)
        thr = next((float(a.value) for a in fn.args[1:]
                    if isinstance(a, Lit) and isinstance(a.value,
                                                         (int, float))), None)
        if src.mtype != "histogram" or thr is None:
            raise Untranslatable(
                "getCdfValue() needs a histogram-backed attribute and a "
                "numeric threshold")
        t.note("getCdfValue(x, %s) rendered as the share of observations in "
               "buckets up to %s; exact only with a bucket boundary there"
               % (_fmt_num(thr), _fmt_num(thr)), NEEDS_REVIEW)
        t.notes.append("unit:percentunit")
        return "sum%s(%s) / sum%s(%s)" % (
            by, hsel("_bucket", fnname="rate", more=[_le_matcher(thr)]),
            by, hsel("_count", fnname="rate"))
    if name in ("percentile", "median"):
        leftover(numeric)
        # (quantile text for PromQL, legend suffix) per requested percentile
        pcts: List[Tuple[str, str]] = []
        if name == "median":
            pcts = [("0.5", "p50")]
        for a in ([] if name == "median" else fn.args[1:]):
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                pcts.append((_fmt_q(float(a.value)), "p%g" % float(a.value)))
                continue
            var = is_nr_variable(a)
            if var:
                gv = grafana_var(var, ctx.cfg)
                pcts.append(("$%s / 100" % gv, "p$%s" % gv))
                t.note("the percentile comes from dashboard variable {{%s}}; "
                       "its value must be a number from 0 to 100" % var,
                       APPROXIMATE)
        if not pcts:
            pcts = [("0.95", "p95")]
        if src.mtype in ("gauge", "rate"):
            # No buckets to interpolate: take the quantile of the raw
            # sampled values per series over the window instead of
            # emitting a query against a nonexistent _bucket family.
            t.note("percentile() of gauge %r mapped to per-series "
                   "quantile_over_time (averaged across series); NR "
                   "computes percentiles over all raw events — verify "
                   "against NR data" % src.base, NEEDS_REVIEW)
            exprs = []
            for qtext, _lbl in pcts:
                inner = hsel("", fnname="quantile_over_time",
                             prefix="%s, " % qtext)
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
            exprs = ["histogram_quantile(%s, sum by (%s)(%s))"
                     % (qtext, le_by, hsel("_bucket", fnname="rate"))
                     for qtext, _lbl in pcts]
        if len(exprs) > 1:
            for (_q, lbl), e in list(zip(pcts, exprs))[1:]:
                extra_t = Translation(
                    expr=_wrap_topk(ctx, e), datasource="prometheus",
                    query_type="range" if ctx.is_range else "instant",
                    legend=(legend_for(ctx.by, template=t.legend_template)
                            + " " + lbl).strip(),
                    confidence=t.confidence, group_by=list(ctx.by))
                t.extra.append(extra_t)
            t.legend = (legend_for(ctx.by, template=t.legend_template)
                        + " " + pcts[0][1]).strip()
        return exprs[0]

    if name == "rate":
        leftover(numeric)
        # rate(inner_agg(x), 1 unit)
        inner_fn = fn.args[0] if fn.args and isinstance(fn.args[0], Func) \
            else None
        if inner_fn is not None and inner_fn.name not in ("count", "sum",
                                                          "filter"):
            raise Untranslatable(
                "rate(%s(...)) has no metric equivalent: only rate(count(...))"
                " and rate(sum(...)) map to PromQL rate()"
                % fn_name(inner_fn.name))
        per_seconds = 60.0
        for a in fn.args:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                per_seconds = float(a.value)
        mult = "" if per_seconds == 1 else " * %s" % _fmt_num(per_seconds)
        inner = fn.args[0] if fn.args and isinstance(fn.args[0], Func) \
            else None
        target = ""
        if src.mtype == "histogram":
            # rate(count(x)) -> _count; rate(sum(x)) -> _sum (time spent);
            # rate(sum(x.count)) -> _count (the name says which series)
            target = "_sum" if inner is not None and inner.name == "sum" \
                else "_count"
            if src.component:
                target = "_" + src.component
        if src.mtype == "gauge":
            t.note("rate() of a gauge-backed source; emitted rate() anyway",
                   NEEDS_REVIEW)
        if src.mtype == "rate":
            t.note("rate() of an attribute that is already a per-second "
                   "rate; emitted the rate scaled to the requested unit",
                   APPROXIMATE)
        return "sum%s(%s)%s" % (by, hsel(target, fnname="rate"), mult)

    if name == "derivative":
        leftover(numeric)
        per_seconds = 60.0
        for a in fn.args[1:]:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                per_seconds = float(a.value)
        mult = "" if per_seconds == 1 else " * %s" % _fmt_num(per_seconds)
        if src.mtype == "histogram":
            raise Untranslatable(
                "derivative() on a histogram-backed source has no PromQL "
                "equivalent (only _bucket/_sum/_count series exist)")
        if src.mtype == "rate":
            raise Untranslatable(
                "derivative() of %s, which is already a per-second rate, "
                "would be a second derivative; PromQL has no sound form for "
                "it (deriv() over a subquery of rate() at best) — plot the "
                "rate itself" % src.base)
        if src.mtype == "counter":
            return "sum%s(%s)%s" % (by, hsel("", fnname="rate"), mult)
        t.note("derivative() mapped to deriv() (linear regression)",
               APPROXIMATE)
        inner = "%s%s" % (hsel("", fnname="deriv"), mult)
        return "avg%s(%s)" % (by, inner) if ctx.by else inner

    if name == "predictlinear":
        leftover(numeric)
        # predictLinear(attr, N units) — parser normalizes the duration
        # to seconds.
        horizon = 3600.0
        for a in fn.args[1:]:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                horizon = float(a.value)
        if src.mtype == "histogram":
            # Regress the derived average over the range with a subquery.
            inner_fn = fn.args[0] if fn.args and isinstance(fn.args[0], Func) \
                else Func("average", args=list(fn.args[:1]))
            if inner_fn.name in ("percentile", "median", "average", "avg",
                                 "max", "min", "count", "sum"):
                base = _agg_expr(ctx, inner_fn, src, extra, numeric)
            else:
                base = _agg_expr(ctx, Func("average", args=list(fn.args[:1])),
                                 src, extra, numeric)
            t.note("predictLinear() over a histogram-derived value is a "
                   "linear regression of that value across the dashboard "
                   "range (a PromQL subquery)", APPROXIMATE)
            return "predict_linear((%s)[$__range:], %s)" % (
                base, _fmt_num(horizon))
        if src.mtype == "counter":
            t.note("predictLinear() of a counter predicts the raw counter "
                   "value (resets skew the regression); NR predicts the "
                   "reported metric", NEEDS_REVIEW)
        t.note("predictLinear() mapped to predict_linear() (linear "
               "regression over the query window)", APPROXIMATE)
        inner = hsel("", window="$__range", fnname="predict_linear",
                     tail=", %s" % _fmt_num(horizon))
        return "avg%s(%s)" % (by, inner) if ctx.by else inner

    if name in ("uniquecount", "cardinality"):
        leftover(numeric)
        attr = fn.args[0] if fn.args else None
        attr, _ = unwrap_attr(attr)
        if attr is None:
            raise Untranslatable("uniqueCount needs an attribute argument")
        label, mapped = map_attr(attr.name, ctx.cfg)
        if ctx.is_apm_http and label == "span_name":
            label = "http_route"
        if not mapped:
            t.note("uniqueCount attribute %r not in label_map; used %r"
                   % (attr.name, label), NEEDS_REVIEW)
        t.note("uniqueCount() counts distinct label values on series — an "
               "approximation of event-level uniqueness", APPROXIMATE)
        inner_by = ", ".join(dict.fromkeys([label] + ctx.by))
        suffix = "_count" if src.mtype == "histogram" else ""
        if not ctx.is_range and not ctx.offset:
            metric_sel = hsel(suffix, window=W, fnname="last_over_time")
        else:
            metric_sel = hsel(suffix, window=None)
        return "count%s(count by (%s)(%s))" % (by, inner_by, metric_sel)

    if name == "stddev":
        leftover(numeric)
        if src.mtype in ("gauge", "rate"):
            # NRQL stddev is over datapoint values in the time window, i.e.
            # stddev_over_time per series — NOT PromQL's across-series
            # stddev(), which is 0 for a single series.
            t.note("stddev() mapped to per-series stddev_over_time; NR "
                   "computes it over all events in the window", APPROXIMATE)
            inner = hsel("", fnname="stddev_over_time")
            return "avg%s(%s)" % (by, inner) if ctx.by else inner
        raise Untranslatable(
            "stddev() cannot be derived from %s-backed metrics (needs raw "
            "values; histograms lack a sum-of-squares series)" % src.mtype)

    if name == "apdex":
        leftover(numeric)
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
        le1 = hsel("_bucket", fnname="rate", more=[_le_matcher(thr)])
        le4 = hsel("_bucket", fnname="rate", more=[_le_matcher(thr * 4)])
        cnt = hsel("_count", fnname="rate")
        return ("(sum%s(%s) + sum%s(%s)) / 2 / sum%s(%s)"
                % (by, le1, by, le4, by, cnt))

    if name == "histogram":
        leftover(numeric)
        if src.mtype != "histogram":
            raise Untranslatable("histogram() requires a histogram metric")
        t.note("histogram() rendered as Prometheus buckets over time "
               "(fixed bucket bounds, not NR's requested buckets)",
               APPROXIMATE)
        t.legend = "{{le}}"
        t.query_type = "range"  # heatmaps need range data
        t.notes.append("panel-hint:heatmap")
        return ("sum by (le)(%s)"
                % hsel("_bucket", window="$__interval", fnname="increase"))

    if name in ("funnel",):
        raise Untranslatable(
            "funnel() is event-sequence analysis with no metric equivalent")
    if name in ("earliest",):
        leftover(numeric)
        if ctx.is_range:
            raise Untranslatable(
                "earliest() with TIMESERIES has no PromQL equivalent (no "
                "first-in-bucket function); without TIMESERIES it is the "
                "value at the start of the time range")
        if src.mtype == "histogram":
            raise Untranslatable(
                "earliest() on a histogram-backed source has no PromQL "
                "equivalent (only _bucket/_sum/_count series exist)")
        t.note("earliest() rendered as the value at the start of the time "
               "range (the first sample in the window): a PromQL @ modifier "
               "on $__from", APPROXIMATE)
        inner = hsel("", window=None, tail=" @ ${__from:date:seconds}")
        return "avg%s(%s)" % (by, inner) if ctx.by else inner
    if name in ("eventtype", "keyset", "dimensions", "aggregationendtime",
                "bytecountestimate"):
        raise Untranslatable("%s() is NRDB introspection (attribute names "
                             "and event metadata); Grafana's label browser "
                             "is the equivalent" % fn_name(name))

    raise Untranslatable("aggregation %s() is not supported by the "
                         "translator" % name)


def _derived_expr(ctx: _Ctx, fn: Func, src: DerivedSource,
                  extra: Optional[List[List[Matcher]]] = None) -> str:
    """Render a template source (nrmetrics kind 'expr' / 'count')."""
    t = ctx.t
    agg = _AGG_WORD.get(fn.name, "avg")
    if fn.name == "earliest" and ctx.is_range:
        raise Untranslatable(
            "earliest() with TIMESERIES has no PromQL equivalent (no "
            "first-in-bucket function); without TIMESERIES it is the value "
            "at the start of the time range")
    if fn.name in ("histogram", "apdex"):
        raise Untranslatable(
            "%s() cannot be applied to %s: the LGTM equivalent is a derived "
            "expression, not a raw histogram/gauge" % (fn_name(fn.name), ctx.event))
    extra_m = (extra or [[]])[0]
    if extra and len(extra) > 1:
        t.note("an OR inside filter()/percentage() on this derived infra "
               "expression could not be honored; only the first alternative "
               "was applied", NEEDS_REVIEW)
    if len(ctx.branches) > 1:
        t.note("the OR in WHERE could not be applied to this derived infra "
               "expression; only the first alternative was used",
               NEEDS_REVIEW)
    http = http_server_source(ctx.cfg).base
    window = ctx.window
    if fn.name == "latest" and not ctx.is_range:
        window = "$__rate_interval"
    by = ctx.by_clause().lstrip() if ctx.by else ""
    expr = (src.expr
            .replace("<W>", window)
            .replace("<STEP>", "$__interval" if ctx.is_range else "$__range")
            .replace("<BY>", by)
            .replace("<AGG>", agg)
            .replace("<AGGINV>", _AGG_INV.get(agg, agg))
            .replace("<SEL>", ctx.sel_tail(extra_m))
            .replace("<SELBARE>", ctx.sel_bare(extra_m))
            .replace("<HTTP>", http))
    expr = re.sub(r"\{,", "{", expr)   # tidy leading comma when no matchers
    if ctx.offset:
        # Offset every range/instant selector in the template (quoted
        # strings are skipped, so a "}" inside a regex is not a selector;
        # a bare name still has its "{}" here, so it is offset too).
        shifted = offset_selectors(expr, ctx.offset)
        if shifted == expr:
            t.note("COMPARE WITH could not be applied to this derived "
                   "expression (no selector to offset); the comparison "
                   "target repeats the current values", NEEDS_REVIEW)
        expr = shifted
    expr = expr.replace("{}", "")      # drop empty matcher braces
    expr = re.sub(r"(\b(?:avg|sum|max|min|count)) \(", r"\1(", expr)
    if fn.name in ("percentile", "median"):
        # The quantile of the derived value's samples per series over the
        # window (a subquery); NR ranks all raw events.
        qs: List[str] = []
        for a in fn.args[1:]:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                qs.append(_fmt_q(float(a.value)))
            elif is_nr_variable(a):
                gv = grafana_var(is_nr_variable(a), ctx.cfg)
                qs.append("$%s / 100" % gv)
                t.note("the percentile comes from dashboard variable "
                       "{{%s}}; its value must be a number from 0 to 100"
                       % is_nr_variable(a), APPROXIMATE)
        if not qs:
            qs = ["0.5" if fn.name == "median" else "0.95"]
        if len(qs) > 1:
            t.note("only the first percentile was translated for this "
                   "derived expression; add panels for the others",
                   NEEDS_REVIEW)
        t.note("%s() of a derived expression: quantile_over_time over a "
               "subquery of it (per series, over the window); NR ranks all "
               "raw events" % fn_name(fn.name), NEEDS_REVIEW)
        expr = "quantile_over_time(%s, (%s)[%s:])" % (qs[0], expr, window)
    if fn.name == "earliest":
        t.note("earliest() rendered as the value at the start of the time "
               "range (the first sample in the window): a PromQL @ modifier "
               "on $__from", APPROXIMATE)
        expr = ("last_over_time((%s)[$__rate_interval:] @ "
                "${__from:date:seconds})" % expr)
    if fn.name in ("derivative", "predictlinear", "stddev"):
        # Over-time functions of a derived expression: a PromQL subquery
        # turns the expression into the range vector they need.
        secs = 60.0 if fn.name == "derivative" else 3600.0
        for a in fn.args[1:]:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                secs = float(a.value)
        if fn.name == "derivative":
            mult = "" if secs == 1 else " * %s" % _fmt_num(secs)
            t.note("derivative() of a derived expression: deriv() (linear "
                   "regression) over a subquery of it", APPROXIMATE)
            expr = "deriv((%s)[%s:])%s" % (expr, window, mult)
        elif fn.name == "predictlinear":
            t.note("predictLinear() of a derived expression: predict_linear() "
                   "over a subquery of it across the query window",
                   APPROXIMATE)
            expr = "predict_linear((%s)[$__range:], %s)" % (
                expr, _fmt_num(secs))
        else:
            t.note("stddev() of a derived expression: stddev_over_time over "
                   "a subquery of it (per series, over the window); NR "
                   "computes it over all events in the window", APPROXIMATE)
            expr = "stddev_over_time((%s)[%s:])" % (expr, window)
    if fn.name == "rate":
        per = 60.0
        for a in fn.args[1:]:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                per = float(a.value)
        if "rate(" not in src.expr and "_total{" in src.expr:
            # A cumulative counter inside the template: rate() it in place.
            expr = re.sub(r"([A-Za-z_:][A-Za-z0-9_:]*_total(?:\{[^}]*\})?)"
                          r"((?: offset \S+)?)",
                          r"rate(\1[%s]\2)" % window, expr)
            t.note("rate() of a cumulative counter applied inside the derived "
                   "expression", APPROXIMATE)
        if per != 1:
            t.note("rate(..., %s seconds) of a per-second quantity scaled by "
                   "%s" % (_fmt_num(per), _fmt_num(per)), APPROXIMATE)
            expr = "(%s) * %s" % (expr, _fmt_num(per))
    if fn.name == "count" and not src.expr.startswith("count") and not any(
            "counts the exporter's series" in n for n in t.notes):
        t.note("count(*) on %s counts the exporter's series, not New Relic "
               "samples" % ctx.event, APPROXIMATE)
    return expr


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
    "pageviewtiming": _BROWSER_HINT,
    "browserinteraction": _BROWSER_HINT, "javascripterror": _BROWSER_HINT,
    "ajaxrequest": _BROWSER_HINT, "browsertiming": _BROWSER_HINT,
    "mobile": _MOBILE_HINT, "mobilecrash": _MOBILE_HINT,
    "mobilerequest": _MOBILE_HINT, "mobilerequesterror": _MOBILE_HINT,
    "mobilesession": _MOBILE_HINT, "mobilehandledexception": _MOBILE_HINT,
    "syntheticcheck": _SYNTH_HINT, "syntheticrequest": _SYNTH_HINT,
    "syntheticsprivatelocationstatus": _SYNTH_HINT,
    "nrconsumption": _NR_ONLY_HINT, "nrusage": _NR_ONLY_HINT,
    "nrauditevent": _NR_ONLY_HINT, "nrdailyusage": _NR_ONLY_HINT,
    "nrmtdconsumption": _NR_ONLY_HINT,
    "nrintegrationerror": ("New Relic ingest/integration error records; the "
                           "LGTM analogue is the collector's or agent's own "
                           "error logs in Loki"),
}


_NAME_LABELS = ("metricName", "metricname", "__name__")
_NR_INTERNAL_ATTRS = {"collector_name", "instrumentation_provider",
                      "instrumentation_name", "instrumentation_version",
                      "newrelic_source", "integrationName",
                      "integrationVersion", "integrationname",
                      "integrationversion", "nr_entityType"}

# NR dimensional K8s attributes that are entity identities / states of the
# sample events (FROM Metric SELECT uniqueCount(k8s.podName) ...).
_K8S_DIMENSIONS = {
    "k8s.podname": ("K8sPodSample", "podName"),
    "k8s.pod.status": ("K8sPodSample", "status"),
    "k8s.nodename": ("K8sNodeSample", "nodeName"),
    "k8s.namespacename": ("K8sNamespaceSample", "namespaceName"),
    "k8s.containername": ("K8sContainerSample", "containerName"),
    "k8s.container.status": ("K8sContainerSample", "status"),
    "k8s.container.reason": ("K8sContainerSample", "reason"),
    "k8s.deploymentname": ("K8sDeploymentSample", "deploymentName"),
    "k8s.clustername": ("K8sClusterSample", "clusterName"),
}


def _metric_name_from_where(ctx: _Ctx) -> Optional[str]:
    """FROM Metric ... WHERE metricName = 'x' selects the metric (a
    {{var}} value becomes the Grafana variable)."""
    for b in ctx.branches:
        for m in list(b):
            if m.label in _NAME_LABELS and m.op == "=":
                b.remove(m)
                return m.value
            if m.label in _NAME_LABELS and m.op == "=~" \
                    and m.value.startswith("${") \
                    and m.value.endswith(":regex}"):
                b.remove(m)
                return "$" + m.value[2:-len(":regex}")]
    return None


def _consume_name_matchers(ctx: _Ctx, name: str) -> None:
    """WHERE metricName = 'x' and WHERE x IS NOT NULL restate the metric
    the SELECT already names: drop them (a different metricName is noted
    — the SELECT wins)."""
    norm = name if name.startswith("$") else normalize_metric_name(name)
    for b in ctx.branches:
        for m in list(b):
            if m.label in _NAME_LABELS and m.op in ("=", "=~"):
                same = (m.value == name or m.value.startswith("${")
                        or (not name.startswith("$")
                            and normalize_metric_name(m.value) == norm))
                if not same:
                    ctx.t.note("WHERE metricName %s %r selects a different "
                               "metric than the SELECT (%s); the SELECT wins"
                               % (m.op, m.value, name), NEEDS_REVIEW)
                b.remove(m)
            elif m.op == "!=" and m.value == "" and m.label in (
                    norm, sanitize_label(name)):
                b.remove(m)  # x IS NOT NULL on the selected metric


def _timeslice_name(ctx: _Ctx) -> Optional[str]:
    for b in ctx.branches:
        for m in b:
            if m.label == "metricTimesliceName" and m.op == "=":
                return m.value
    return None


def _source_for(ctx: _Ctx, item: SelectItem) -> Any:
    q = ctx.q
    t = ctx.t
    cfg = ctx.cfg
    et = ctx.event
    etl = et.lower()
    fn = item.expr if isinstance(item.expr, Func) else None
    agg = fn.name if fn else ""
    arg = fn.args[0] if fn and fn.args else None

    if etl == "metric":
        # rate(sum(m), 1 minute) style nesting: descend to the metric name.
        while isinstance(arg, Func) and arg.args \
                and arg.name not in ("_ratio", "_arith"):
            arg = arg.args[0]
        inner_attr, _ = unwrap_attr(arg)
        name = None
        low0 = inner_attr.name.lower() if inner_attr is not None else ""
        k8s = _K8S_DIMENSIONS.get(low0)
        if k8s is not None:
            # k8s.podName / k8s.pod.status are the sample event's identity
            # and state, not metrics: translate as the sample would.
            event, plain = k8s
            saved_event = ctx.event
            ctx.event = event
            try:
                pf = Func(agg or "latest", args=[Attr(plain)])
                return _infra_source(ctx, SelectItem(expr=pf), pf,
                                     agg or "latest", Attr(plain))
            finally:
                ctx.event = saved_event
        if agg in ("uniquecount", "cardinality") and low0.startswith("aws.") \
                and len(low0.split(".")) >= 3:
            # uniqueCount(aws.ec2.InstanceId): a dimension, counted on the
            # YACE resource-info series.
            ns = low0.split(".")[1]
            label = nrmetrics.aws_attr_label(inner_attr.name) \
                or sanitize_label(inner_attr.name)
            return DerivedSource(
                "count <BY>(count by (%s%s)(aws_%s_info{<SELBARE>}))"
                % (label, "".join(", " + l for l in ctx.by), ns), "short",
                NEEDS_REVIEW, "distinct %s across YACE's aws_%s_info resource "
                "series" % (label, ns))
        if inner_attr is not None and inner_attr.name.lower() == "metricname":
            if agg in ("uniquecount", "cardinality"):
                # uniqueCount(metricName) WHERE metricName LIKE 'x%'
                return DerivedSource(
                    "count <BY>(count by (__name__)({<SELBARE>}))", "short",
                    NEEDS_REVIEW, "distinct metric names matching the WHERE "
                    "(__name__ matcher; the selector needs at least one "
                    "matcher)")
            raise Untranslatable(
                "%s(metricName) FROM Metric: metricName is the metric's "
                "identity, not a value" % (agg or "?"))
        if inner_attr is not None:
            name = inner_attr.name
        elif isinstance(arg, Lit) and isinstance(arg.value, str):
            name = arg.value
        elif isinstance(arg, Star) or arg is None:
            name = _metric_name_from_where(ctx)
            if name is None:
                raise Untranslatable(
                    "FROM Metric needs a metric name argument in %s() (or "
                    "a WHERE metricName = '...' filter)" % (agg or "?"))
        if name is None:
            raise Untranslatable(
                "FROM Metric needs a metric name argument in %s()"
                % (agg or "?"))
        var = name[1:] if name.startswith("$") \
            else is_nr_variable(Attr(name))
        if var:
            gv = "$" + grafana_var(var, cfg)
            mtype = "counter" if agg in ("count", "sum", "rate") else "gauge"
            _consume_name_matchers(ctx, gv)
            t.note("the metric name comes from dashboard variable {{%s}}: "
                   "its values must be Prometheus metric names (a "
                   "label_values(__name__) variable) and the metric is "
                   "assumed to be a %s" % (var, mtype), NEEDS_REVIEW)
            return MetricSource(gv, mtype, confidence=NEEDS_REVIEW)
        _consume_name_matchers(ctx, name)
        if name.lower() == "newrelic.timeslice.value":
            ts = _timeslice_name(ctx)
            if ts:
                mm = cfg.get("metric_map", {})
                if ts in mm:
                    for b in ctx.branches:
                        b[:] = [m for m in b
                                if m.label != "metricTimesliceName"]
                    return resolve_metric(ts, agg, cfg, t)
                raise Untranslatable(
                    "legacy timeslice metric %r (newrelic.timeslice.value) "
                    "has no automatic equivalent; add it to metric_map, "
                    "e.g. \"%s\": {\"name\": \"<prometheus metric>\", "
                    "\"type\": \"counter|gauge|histogram\"}" % (ts, ts))
        src = resolve_metric(name, agg, cfg, t)
        if isinstance(src, MetricSource) and src.base.startswith("aws_"):
            # YACE exposes resource tags as tag_<Key> labels.
            for b in ctx.branches:
                for i, m in enumerate(b):
                    if m.label.startswith("tags_"):
                        b[i] = Matcher("tag_" + m.label[5:], m.op, m.value)
            ctx.by = ["tag_" + l[5:] if l.startswith("tags_") else l
                      for l in ctx.by]
        return src

    if etl in ("transaction", "transactionerror"):
        attr, _ = unwrap_attr(arg)
        attr_name = attr.name if attr is not None else ""
        key = attr_name.lower()
        kind, note = nrmetrics.TRANSACTION_ATTRS.get(key, ("", ""))
        if attr is None or kind == "http" or agg in ("count", "uniquecount",
                                                     "cardinality", "rate"):
            if attr is not None and key not in nrmetrics.TRANSACTION_ATTRS \
                    and agg not in ("uniquecount", "cardinality"):
                if agg in ("count", "rate"):
                    t.note("%s(%s) counts requests; NR would count events "
                           "where %s is not NULL" % (agg, attr_name,
                                                     attr_name), APPROXIMATE)
                else:
                    raise Untranslatable(
                        "%s(%s) FROM %s: the Transaction attribute %r has no "
                        "OTel HTTP metric equivalent (only duration / "
                        "totalTime / databaseDuration / externalDuration "
                        "map); if %r is a custom attribute, record it as a "
                        "metric and add it to metric_map"
                        % (agg, attr_name, et, attr_name, attr_name))
            src = http_server_source(cfg)
            t.note("FROM %s mapped to OTel HTTP server metrics (%s); "
                   "requires the service to be OTel-instrumented"
                   % (et, src.base), APPROXIMATE)
            if note:
                t.note(note, APPROXIMATE)
            if etl == "transactionerror":
                src.extra_matchers = [Matcher(
                    map_attr("httpResponseCode", cfg)[0], "=~", "5..")]
                t.note("FROM TransactionError approximated as 5xx "
                       "responses; adjust if your NR error config counted "
                       "4xx too", NEEDS_REVIEW)
            return src
        if kind == "timestamp":
            src = http_server_source(cfg)
            src.value_attr = "timestamp"
            t.note("FROM %s mapped to OTel HTTP server metrics (%s)"
                   % (et, src.base), APPROXIMATE)
            return src
        if kind == "db":
            t.note(note, NEEDS_REVIEW)
            return db_client_source(cfg)
        if kind == "db-count":
            t.note(note, NEEDS_REVIEW)
            src = db_client_source(cfg)
            if agg in ("average", "avg"):
                t.note("average(databaseCallCount) is DB calls per request: "
                       "DB client operations divided by HTTP server "
                       "requests", APPROXIMATE)
                return DerivedSource(
                    "sum <BY>(rate(%s{<SELBARE>}[<W>])) / sum <BY>(rate("
                    "<HTTP>_count{<SELBARE>}[<W>]))" % src.name("_count"),
                    "short", NEEDS_REVIEW, src.note)
            return MetricSource(src.name("_count"), "counter", "short",
                                NEEDS_REVIEW, src.note)
        if kind == "ext":
            t.note(note, NEEDS_REVIEW)
            return http_client_source(cfg)
        if kind == "ext-count":
            t.note(note, NEEDS_REVIEW)
            src = http_client_source(cfg)
            if agg in ("average", "avg"):
                t.note("average(externalCallCount) is external calls per "
                       "request: HTTP client requests divided by HTTP server "
                       "requests", APPROXIMATE)
                return DerivedSource(
                    "sum <BY>(rate(%s{<SELBARE>}[<W>])) / sum <BY>(rate("
                    "<HTTP>_count{<SELBARE>}[<W>]))" % src.name("_count"),
                    "short", NEEDS_REVIEW, src.note)
            return MetricSource(src.name("_count"), "counter", "short",
                                NEEDS_REVIEW, src.note)
        raise Untranslatable(
            "%s(%s) FROM %s: %s" % (agg, attr_name, et, note or (
                "the Transaction attribute %r has no OTel HTTP metric "
                "equivalent (only duration / totalTime / databaseDuration / "
                "externalDuration map); string attributes such as error "
                "messages live in Loki (FROM Log) or as span events in "
                "Tempo, not in metrics" % attr_name)))

    if etl in ("span", "distributedtrace", "distributedtracesummary"):
        attr, _ = unwrap_attr(arg)
        wants_duration = attr is not None and "duration" in attr.name.lower()
        if agg in ("percentile", "median", "average", "max", "min",
                   "histogram", "apdex") or wants_duration:
            if attr is not None and not wants_duration \
                    and agg in ("average", "max", "min", "percentile",
                                "median"):
                raise Untranslatable(
                    "%s(%s) FROM Span: only duration is carried by span "
                    "metrics; %r has no span-metric equivalent"
                    % (agg, attr.name, attr.name))
            return spanmetrics_source(cfg, "duration")
        return spanmetrics_source(cfg, "calls")

    if ctx.is_legacy_aws:
        return _legacy_aws_source(ctx, agg, arg)

    # Infrastructure sample events and other built-in event knowledge.
    if nrmetrics.is_infra_event(etl):
        return _infra_source(ctx, item, fn, agg, arg)

    entry = _event_map(ctx)
    if entry is not None and entry.get("family", "metrics") == "metrics":
        metric = entry.get("metric")
        if metric:
            return resolve_metric(str(metric), agg, cfg, t)

    msg = "no metric mapping for FROM %s" % et
    hint = _EVENT_EQUIVALENTS.get(etl)
    if hint:
        msg += " (%s)" % hint
    else:
        msg += (" (custom or unknown event type; if these events are "
                "shipped to Loki as logs add an event_map entry "
                "{\"%s\": {\"family\": \"logs\", \"labels\": {...}}} to "
                "the config, or map it to a metric with {\"family\": "
                "\"metrics\", \"metric\": \"...\"})" % et)
    raise Untranslatable(msg)


def _event_map(ctx: _Ctx) -> Optional[Dict[str, Any]]:
    from .common import event_map_entry
    return event_map_entry(ctx.event, ctx.cfg)


def _legacy_aws_source(ctx: _Ctx, agg: str, arg: Any) -> Any:
    """Legacy AWS polling-integration sample events -> YACE metrics."""
    t = ctx.t
    cfg = ctx.cfg
    attr, _ = unwrap_attr(arg)
    attr_name = attr.name if attr is not None else ""
    mm = cfg.get("metric_map", {})
    if attr_name:
        for key in ("%s.%s" % (ctx.event, attr_name), attr_name):
            if key in mm:
                return resolve_metric(key, agg, cfg, t)
    provider = ctx.aws_provider
    ns = nrmetrics.legacy_aws_namespace(ctx.event, provider)
    known = ", ".join(nrmetrics.legacy_aws_providers(ctx.event))
    if ns is None:
        if provider:
            raise Untranslatable(
                "FROM %s WHERE provider = %r: unknown AWS resource type "
                "(known: %s); map the attribute in metric_map (key "
                "\"%s.%s\") instead" % (ctx.event, provider, known,
                                          ctx.event, attr_name))
        raise Untranslatable(
            "FROM %s needs WHERE provider = '<resource type>' to choose the "
            "CloudWatch namespace (one of: %s)" % (ctx.event, known))
    is_star = attr is None or isinstance(arg, Star)
    if agg == "count" and is_star:
        t.note("count(*) FROM %s counts YACE's discovered %s resources "
               "(aws_%s_info series), not New Relic samples"
               % (ctx.event, provider, ns), APPROXIMATE)
        return DerivedSource("count <BY>(aws_%s_info{<SELBARE>})" % ns,
                             "short", NEEDS_REVIEW, "")
    if agg in ("uniquecount", "cardinality") and attr is not None:
        label, _mapped = map_attr(attr_name, cfg)
        return DerivedSource(
            "count <BY>(count by (%s%s)(aws_%s_info{<SELBARE>}))"
            % (label, "".join(", " + l for l in ctx.by), ns), "short",
            NEEDS_REVIEW, "distinct %s across YACE's aws_%s_info resource "
            "series" % (label, ns))
    if attr is None:
        raise Untranslatable("%s() FROM %s needs a provider.* attribute"
                             % (agg or "?", ctx.event))
    spec = nrmetrics.legacy_aws_spec(ctx.event, provider, attr_name, agg)
    if spec is None:
        raise Untranslatable(
            "%s(%s) FROM %s: only provider.<Metric>.<Statistic> attributes "
            "map to CloudWatch metrics; add %r to metric_map (key "
            "\"%s.%s\")" % (agg, attr_name, ctx.event, attr_name,
                              ctx.event, attr_name))
    src = _spec_to_source(spec, cfg, t)
    if spec.note:
        t.note(spec.note)
    return src


_POPULATION_RE = re.compile(r"\(([A-Za-z_:][A-Za-z0-9_:]*)\{[^{}]*\}\)$")


def _numeric_count_filters(ctx: _Ctx, etl: str, src: Any) -> Any:
    """count(*) / uniqueCount(entity) WHERE <metric-valued attr> > n: the
    population is the attribute's own series filtered by the comparison
    (count((kube_deployment_spec_replicas - ...) > 0)) — PromQL's way of
    saying "entities whose value satisfies the predicate"."""
    if not isinstance(src, DerivedSource) or not ctx.numeric:
        return src
    if not (src.expr.startswith("count ") or src.expr.startswith("sum ")
            or src.expr.startswith("<AGG> <BY>(")):
        return src  # not a population count: the leftover note applies
    filters: List[str] = []
    names: List[str] = []
    for p in list(ctx.numeric):
        spec = nrmetrics.infra_lookup(etl, p.attr)
        if spec is None:
            continue
        entity = _ENTITY_LABELS.get(etl)
        if spec.kind == "gauge" or (spec.kind == "counter"
                                    and getattr(spec, "cumulative", False)):
            sel = _with_fixed(spec.name, spec.matchers)
            body = sel if "{" in sel else sel + "{<SELBARE>}"
        elif spec.kind == "expr" and ("<AGG> <BY>" in spec.expr
                                      or "<AGGINV> <BY>" in spec.expr):
            # One value per entity: the template's aggregation folds the
            # entity's sub-series (CPU cores, filesystems) on its labels.
            fold = ("avg by (%s)" % entity) if entity else ""
            body = "(" + spec.expr.replace("<AGGINV> <BY>", fold, 1).replace(
                "<AGG> <BY>", fold, 1) + ")"
        else:
            continue
        op = "==" if p.op == "=" else p.op
        filters.append("(%s %s %s)" % (body, op, _fmt_num(p.value)))
        names.append("%s %s %s" % (p.attr, p.op, _fmt_num(p.value)))
        ctx.numeric.remove(p)
        ctx.consumed_numeric.append(p)
    if not filters:
        return src
    joined = filters[0] if len(filters) == 1 else " and ".join(filters)
    m = _POPULATION_RE.search(src.expr)
    if not m:
        return src
    head = src.expr[:m.start()]
    population = m.group(0)[1:-1]
    if head.startswith("sum "):
        # A phase/state-selected population (sum of value-1 series): the
        # filtered series must still be counted, and intersected with the
        # state series on the entity labels.
        entity = _ENTITY_LABELS.get(etl)
        head = "count" + head[len("sum"):]
        if entity:
            joined = "%s and on (%s) (%s == 1)" % (joined, entity, population)
        else:
            ctx.t.note("the status filter was dropped next to the numeric "
                       "comparison (no entity labels known to intersect on)",
                       NEEDS_REVIEW)
    elif head.startswith("<AGG> <BY>"):
        # sum of per-entity gauges (process counts): the entities whose
        # value satisfies the predicate are counted instead.
        head = "count <BY>" + head[len("<AGG> <BY>"):]
    src.expr = head + "(%s)" % joined
    shown = joined.replace("<SELBARE>", "...").replace("<SEL>", "")
    ctx.t.note("WHERE %s: the entities are selected by the attribute's own "
               "series (%s), a PromQL filter instead of a label matcher"
               % (", ".join(names), shown), APPROXIMATE)
    return src


# Entity identity labels per sample event (for intersecting series).
_ENTITY_LABELS = {
    "k8spodsample": "namespace, pod",
    "k8scontainersample": "namespace, pod, container",
    "k8snodesample": "node",
    "k8sdeploymentsample": "namespace, deployment",
    "k8sdaemonsetsample": "namespace, daemonset",
    "k8sstatefulsetsample": "namespace, statefulset",
    "systemsample": "instance",
}


def _infra_source(ctx: _Ctx, item: SelectItem, fn: Optional[Func],
                  agg: str, arg: Any) -> Any:
    """Infra sample events via nrmetrics knowledge."""
    t = ctx.t
    etl = ctx.event.lower()
    whole = nrmetrics.INFRA.get((etl, "*"))
    if whole is not None and whole.kind == "none" \
            and not nrmetrics.has_attr_specs(etl):
        raise Untranslatable(whole.reason)
    while isinstance(arg, Func) and arg.args \
            and arg.name not in ("_ratio", "_arith"):
        arg = arg.args[0]  # rate(sum(x), 1 second): descend to x
    attr, _ = unwrap_attr(arg)
    attr_name = attr.name if attr is not None else ""
    count_spec = nrmetrics.infra_count_spec(etl)
    phase = nrmetrics.PHASE_ATTRS.get(etl)

    # count(*) / uniqueCount(<entity attr>) -> entity population.
    is_count = fn is not None and fn.name == "count" and (
        attr is None or isinstance(arg, Star)
        or (count_spec is not None and attr_name.lower() != "timestamp"
            and nrmetrics.infra_lookup(etl, attr_name) is None))
    if is_count and attr is not None and not isinstance(arg, Star):
        t.note("count(%s) counts the samples that carry the attribute, "
               "i.e. the entity population" % attr_name, APPROXIMATE)
    is_unique = fn is not None and fn.name in ("uniquecount", "cardinality") \
        and attr is not None
    if etl == "k8spodsample" and (is_count or is_unique) and any(
            r.label == "reason" for b in ctx.branches for r in b):
        # WHERE reason = 'Evicted': kube_pod_info has no reason label; the
        # kube_pod_status_reason series is 1 for the pods in that state.
        for b in ctx.branches:
            for m in list(b):
                if m.label.lower() in ("status", "phase"):
                    b.remove(m)
                    t.notes.append("the %s filter is implied by the reason "
                                   "filter (kube_pod_status_reason)" % m.label)
        _drop_implicit(ctx)
        return DerivedSource(
            "count <BY>(kube_pod_status_reason{<SELBARE>} == 1)", "short",
            NEEDS_REVIEW, nrmetrics.KSM_NOTE + "; pods whose status reason "
            "is set (kube_pod_status_reason: Evicted, NodeAffinity, "
            "NodeLost, Shutdown, UnexpectedAdmissionError)")
    if count_spec is not None and (is_count or (
            is_unique and attr_name.lower() in
            [a.lower() for a in count_spec.entity_attrs])):
        src = _spec_to_source(count_spec, ctx.cfg, t)
        if count_spec.note:
            t.note(count_spec.note)
        if phase:
            metric, label, names = phase
            src = _apply_phase(ctx, src, metric, label, names) or src
        if is_count:
            t.note("count(*) FROM %s counts the exporter's series (one per "
                   "%s) rather than New Relic samples" % (
                       ctx.event, ctx.event.replace("Sample", "").
                       replace("K8s", "").lower() or "entity"), APPROXIMATE)
        src = _numeric_count_filters(ctx, etl, src)
        if is_unique and isinstance(src, DerivedSource) \
                and count_spec.kind == "expr":
            # An aggregated population (namedprocess_namegroup_num_procs
            # summed over process groups) is not one series per entity:
            # count the distinct label values among the series instead.
            label, mapped = map_attr(attr_name, ctx.cfg)
            group = "count by (%s%s)(" % (
                label, "".join(", " + l for l in ctx.by))
            if mapped and src.expr.startswith("count <BY>("):
                # numeric WHERE filters already turned it into a count
                src.expr = src.expr.replace(
                    "count <BY>(", "count <BY>(" + group, 1) + ")"
            elif mapped and src.expr.startswith("<AGG> <BY>("):
                src.expr = "count <BY>(%s%s)" % (
                    group, src.expr[len("<AGG> <BY>("):])
            elif src.expr.startswith("<AGG> <BY>("):
                # No label for the attribute (pid): every member of the
                # aggregated series is one entity, so the population itself.
                src.expr = "sum" + src.expr[len("<AGG>"):]
        _drop_implicit(ctx)
        return src
    if is_unique and count_spec is not None:
        # uniqueCount of a non-entity attribute: distinct label values on
        # the population metric.
        src = _spec_to_source(count_spec, ctx.cfg, t)
        label, mapped = map_attr(attr_name, ctx.cfg)
        if not mapped:
            t.note("uniqueCount attribute %r not in label_map; used %r"
                   % (attr_name, label), NEEDS_REVIEW)
        src = _numeric_count_filters(ctx, etl, src)
        if isinstance(src, DerivedSource) and "count <BY>(" in src.expr:
            src.expr = src.expr.replace(
                "count <BY>(", "count <BY>(count by (%s%s)(" % (
                    label, "".join(", " + l for l in ctx.by)), 1) + ")"
        elif isinstance(src, DerivedSource) and \
                src.expr.startswith("<AGG> <BY>("):
            # An aggregated population (namedprocess_namegroup_num_procs
            # summed over groups): distinct label values among the series.
            src.expr = "count <BY>(count by (%s%s)(%s)" % (
                label, "".join(", " + l for l in ctx.by),
                src.expr[len("<AGG> <BY>("):])
        elif isinstance(src, DerivedSource):
            t.note("uniqueCount(%s) on %s: the population expression cannot "
                   "be grouped by %r; emitted the population itself"
                   % (attr_name, ctx.event, label), NEEDS_REVIEW)
        _drop_implicit(ctx)
        return src

    if attr_name.lower() == "timestamp" and fn is not None \
            and count_spec is not None and count_spec.kind == "count":
        # latest(timestamp): when the entity last reported.
        if fn.name not in ("latest", "max", "min", "earliest"):
            raise Untranslatable(
                "%s(timestamp) FROM %s has no metric equivalent (only "
                "latest/max/min(timestamp) — the time of the last or first "
                "sample — translate)" % (fn_name(fn.name), ctx.event))
        sel = _with_fixed(count_spec.name, count_spec.matchers)
        if "{" not in sel:
            sel += "{<SELBARE>}"
        _drop_implicit(ctx)
        if fn.name in ("latest", "max"):
            t.note("latest(timestamp) rendered as the timestamp of the most "
                   "recent sample (epoch milliseconds)", APPROXIMATE)
            return DerivedSource("max <BY>(timestamp(%s)) * 1000" % sel,
                                 "dateTimeAsIso", APPROXIMATE,
                                 count_spec.note or "")
        t.note("%s(timestamp) rendered as the timestamp of the first sample "
               "in the range (epoch milliseconds)" % fn_name(fn.name),
               APPROXIMATE)
        return DerivedSource(
            "min <BY>(min_over_time(timestamp(%s)[$__range:])) * 1000" % sel,
            "dateTimeAsIso", APPROXIMATE, count_spec.note or "")
    spec = nrmetrics.infra_lookup(etl, attr_name) if attr is not None else None
    if attr_name.lower() == "reason" and etl in ("k8scontainersample",
                                                 "k8spodsample"):
        ctx.by = list(dict.fromkeys(ctx.by + ["reason"]))
    if spec is None and attr is None:
        if fn is not None and fn.name == "rate":
            raise Untranslatable(
                "rate(count(*)) FROM %s measures New Relic's sampling rate "
                "(samples per interval), not a metric; the exporter scrapes "
                "on a fixed interval, so there is nothing to translate"
                % ctx.event)
        raise Untranslatable(
            "%s() FROM %s needs an attribute argument" % (agg or "?",
                                                          ctx.event))
    if spec is None:
        if phase and attr_name.lower() in phase[2]:
            # latest(status) FACET podName
            metric, label, _names = phase
            if "%s" in metric:
                raise Untranslatable(
                    "container status is three boolean series in "
                    "kube-state-metrics (kube_pod_container_status_running / "
                    "_waiting / _terminated); build one stat panel per state")
            t.note("%s(%s) rendered as the active %s label of %s (one "
                   "series per entity with value 1); Grafana shows the "
                   "%s in the legend" % (agg, attr_name, label, metric,
                                         label), NEEDS_REVIEW)
            ctx.by = list(dict.fromkeys(ctx.by + [label]))
            return DerivedSource(
                "max <BY>(%s{<SELBARE>} == 1)" % metric, "short",
                NEEDS_REVIEW, nrmetrics.KSM_NOTE)
        label, mapped = map_attr(attr_name, ctx.cfg)
        if mapped or (count_spec is not None and attr_name.lower() in
                      [a.lower() for a in count_spec.entity_attrs]):
            raise Untranslatable(
                "%s(%s) FROM %s: %s is a label (%s) on the exporter's "
                "metrics, not a value; FACET %s or uniques(%s) list its "
                "values" % (agg, attr_name, ctx.event, attr_name, label,
                            attr_name, attr_name))
        raise Untranslatable(
            "%s(%s) FROM %s: no known exporter metric for attribute %r; "
            "add it to metric_map (keyed \"%s.%s\") with the Prometheus "
            "metric that carries it"
            % (agg, attr_name, ctx.event, attr_name, ctx.event, attr_name))
    src = _spec_to_source(spec, ctx.cfg, t)
    if spec.note:
        t.note(spec.note)
    if phase:
        metric, label, names = phase
        _apply_phase(ctx, None, metric, label, names)
    _drop_implicit(ctx)
    t.note("infra-event mapping assumes the exporter metrics exist in "
           "Mimir (node_exporter / kube-state-metrics / cAdvisor / "
           "process-exporter)", NEEDS_REVIEW)
    return src


# kube_pod_container_status_<state>: one boolean series per state.
_CONTAINER_STATES = ("running", "waiting", "terminated")


def _apply_phase(ctx: _Ctx, src: Any, metric: str, label: str,
                 names: List[str]) -> Any:
    """WHERE status = 'Running' on pod-like events: the population metric
    becomes the phase metric filtered on its phase label."""
    found: List[Matcher] = []
    for b in ctx.branches:
        for m in list(b):
            if m.label.lower() in [n.lower() for n in names] or \
                    m.label.lower() in ("status", "phase", "state"):
                b.remove(m)
                found.append(m)
    if label:
        ctx.by = [label if l.lower() in [n.lower() for n in names] else l
                  for l in ctx.by]
    if not found and label and label in ctx.by and src is not None \
            and "%s" not in metric:
        # FACET status: group the phase metric (only the active phase is 1).
        expr = "sum <BY>(%s{<SELBARE>} == 1)" % metric
        if isinstance(src, DerivedSource):
            src.expr = expr
            src.confidence = EXACT
            return src
        return DerivedSource(expr, "short", EXACT, nrmetrics.KSM_NOTE)
    if not found:
        return None
    m = found[0]
    if src is None:
        if "%s" in metric and m.op not in ("=", "=~"):
            ctx.t.note("status %s %r on container samples has no single "
                       "series (one boolean series per state); filter "
                       "dropped" % (m.op, m.value), NEEDS_REVIEW)
            return None
        ctx.t.note("the %s filter was dropped: cAdvisor/kube-state-metrics "
                   "resource series carry no phase label; add a "
                   "kube_pod_status_phase join manually if required"
                   % "/".join(n for n in names), NEEDS_REVIEW)
        return None
    if "%s" in metric:
        if m.op not in ("=", "=~"):
            ctx.t.note("status %s %r on container samples has no single "
                       "series (one boolean series per state); filter "
                       "dropped" % (m.op, m.value), NEEDS_REVIEW)
            return None
        if m.op == "=~":
            # status IN ('Waiting', 'Terminated') / LIKE 'Wait%': one
            # boolean series per state, so the regex selects among the
            # known state names (a metric-name regex must not catch
            # kube_pod_container_status_waiting_reason and friends).
            try:
                states = [s for s in _CONTAINER_STATES
                          if re.fullmatch(m.value, s, re.IGNORECASE)]
            except re.error:
                states = []
            if not states:
                ctx.t.note("status =~ %r matches none of the container "
                           "states (%s); filter dropped"
                           % (m.value, ", ".join(_CONTAINER_STATES)),
                           NEEDS_REVIEW)
                return None
            if len(states) > 1:
                return DerivedSource(
                    'sum <BY>({__name__=~"%s(%s)"<SEL>})' % (
                        metric.replace("%s", ""), "|".join(states)),
                    "short", EXACT, nrmetrics.KSM_NOTE)
            m = Matcher(m.label, "=", states[0])
        state = m.value.lower()
        has_reason = any(r.label == "reason" for b in ctx.branches for r in b)
        if has_reason and state in ("waiting", "terminated"):
            # kube_pod_container_status_waiting_reason{reason="..."}
            return DerivedSource("sum <BY>(%s_reason{<SELBARE>})"
                                 % (metric % state), "short", EXACT,
                                 nrmetrics.KSM_NOTE)
        return DerivedSource("sum <BY>(%s{<SELBARE>})" % (metric % state),
                             "short", EXACT, nrmetrics.KSM_NOTE)
    phase_m = Matcher(label, m.op, m.value)
    expr = "sum <BY>(%s{%s<SEL>})" % (metric, phase_m.render())
    if isinstance(src, DerivedSource):
        src.expr = expr
        src.confidence = EXACT
        return src
    return DerivedSource(expr, "short", EXACT, nrmetrics.KSM_NOTE)


def _drop_implicit(ctx: _Ctx) -> None:
    etl = ctx.event.lower()
    for b in ctx.branches:
        for m in list(b):
            reason = nrmetrics.IMPLICIT_FILTERS.get((etl, m.label.lower()))
            if reason:
                b.remove(m)
                ctx.t.notes.append("filter on %s dropped: %s" % (m.label,
                                                                 reason))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _if_parts(fn: Func) -> Tuple[Any, List[Any]]:
    """if(cond, then[, else]) -> (cond, [then, else]); a bare attribute
    condition (if(error, ...)) is the boolean test `attr = true`."""
    if fn.where is not None:
        return fn.where, list(fn.args)
    if fn.args and isinstance(fn.args[0], Attr):
        return Cmp(fn.args[0], "=", Lit(True)), list(fn.args[1:])
    return None, list(fn.args)


def _if_cases(fn: Func, negated: List[Any]) \
        -> Optional[List[Tuple[Any, Optional[str]]]]:
    """if(c1, 'a', if(c2, 'b', 'c')) -> [(c1, a), (!c1 & c2, b), (!c1 & !c2, c)]."""
    cond, vals = _if_parts(fn)
    if cond is None or not vals:
        return None

    def with_prior(c: Any) -> Any:
        return c if not negated else BoolOp("and", list(negated) + [c])
    then = vals[0]
    els = vals[1] if len(vals) > 1 else None
    specs: List[Tuple[Any, Optional[str]]] = [
        (with_prior(cond), str(getattr(then, "value", "true")))]
    if isinstance(els, Func) and els.name == "if":
        nested = _if_cases(els, negated + [NotOp(cond)])
        if nested is None:
            return None
        specs.extend(nested)
    elif els is not None:
        specs.append((with_prior(NotOp(cond)),
                      str(getattr(els, "value", "false"))))
    return specs


def _extract_facet_cases(q: NrqlQuery) \
        -> Tuple[Optional[List[Tuple[Any, Optional[str]]]], Optional[str]]:
    """Pop the FACET cases(...) / if(...) item (other FACET attributes stay
    as the grouping); return its (cond, alias) list and the name of the
    catch-all bucket (`cases(...) OR 'other'`), if any."""
    idx = next((i for i, f in enumerate(q.facet)
                if isinstance(f.expr, Func) and f.expr.name in ("cases", "if")),
               None)
    if idx is None:
        return None, None
    fn = q.facet[idx].expr
    specs: Optional[List[Tuple[Any, Optional[str]]]] = None
    other: Optional[str] = None
    if fn.name == "cases" and fn.cases:
        specs = list(fn.cases)
        for a in fn.args:
            if isinstance(a, Lit) and isinstance(a.value, str) \
                    and a.value.startswith("OR:"):
                other = a.value[3:]
    elif fn.name == "if":
        specs = _if_cases(fn, [])
    if specs is None:
        return None, None
    q.facet = q.facet[:idx] + q.facet[idx + 1:]
    return specs, other


def note_cases_other(t: Translation, other: Optional[str]) -> None:
    if other:
        t.note("FACET cases(...) OR %r: the catch-all bucket (rows matching "
               "no case) is not emitted; add a target with the negated case "
               "conditions if you need it" % other, APPROXIMATE)


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
    base = fn
    while base.name == "filter" and base.args \
            and isinstance(base.args[0], Func):
        base = base.args[0]
    for cond, _alias in case_specs:
        probe = Translation()
        branches = cond_to_branches(cond, ctx.cfg, probe)
        usable = [p for p in probe.numeric if _numeric_consumable(ctx, p)]
        if not any(branches) and not usable:
            return None
        if not any(branches) and usable and base.name != "count":
            # average(duration) FACET cases(WHERE duration < 1 ...): only
            # count(*) can be split at a histogram bucket boundary; every
            # case would otherwise be the same unfiltered query.
            t.note("FACET cases(...) compares %s, which only count(*) can "
                   "split at a histogram bucket boundary — %s() cannot be "
                   "computed for a sub-range of the buckets; grouping "
                   "dropped, the panel shows the overall value"
                   % (usable[0].attr, base.name), NEEDS_REVIEW)
            return None
    t.note("FACET cases(...) became one filtered query per case; NR's "
           "implicit 'Other' bucket is not emitted", APPROXIMATE)

    def case_legend(label: str) -> str:
        rest = legend_for(ctx.by, None, t.legend_template) if ctx.by else ""
        return (label + " " + rest).strip() if rest else label

    for idx, (cond, alias) in enumerate(case_specs):
        label = case_legend(alias or cond_text(cond))
        if idx == 0:
            extra, numeric = _embedded_numeric(ctx, cond)
            t.expr = _translate_item(ctx, fn, extra=extra,
                                     numeric=ctx.numeric + numeric)
            t.legend = label
            continue
        sub = Translation(datasource="prometheus", query_type=t.query_type)
        saved = ctx.t
        ctx.t = sub
        try:
            extra, numeric = _embedded_numeric(ctx, cond)
            sub.expr = _translate_item(ctx, fn, extra=extra,
                                       numeric=ctx.numeric + numeric)
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
    t.group_by = list(ctx.by)
    return t


# (unit, multiplier) -> the unit the panel should show after scaling.
_UNIT_SCALE = {
    ("s", 1000.0): "ms", ("ms", 0.001): "s", ("s", 1000000.0): "µs",
    ("ms", 1000.0): "µs", ("s", 1 / 60.0): "m", ("s", 1 / 3600.0): "h",
    ("ms", 1 / 60000.0): "m", ("percentunit", 100.0): "percent",
    ("percent", 0.01): "percentunit", ("bytes", 1 / 1024.0): "kbytes",
    ("bytes", 1 / 1048576.0): "mbytes", ("bytes", 1 / 1073741824.0): "gbytes",
    ("bytes", 0.001): "decbytes", ("bytes", 8.0): "bits",
    ("Bps", 8.0): "bps", ("bps", 0.125): "Bps", ("Bps", 1 / 1024.0): "KBs",
    ("Bps", 1 / 1048576.0): "MBs", ("short", 1.0): "short",
}


def _scaled_unit(unit: str, mult: float) -> Optional[str]:
    for (u, m), out in _UNIT_SCALE.items():
        if u == unit and abs(m - mult) <= abs(m) * 1e-6:
            return out
    if unit in ("short", "none", ""):
        return "short"
    return None


def translate_to_promql(q: NrqlQuery, cfg: Dict[str, Any]) -> Translation:
    t = Translation(datasource="prometheus")
    case_specs, case_other = _extract_facet_cases(q)
    ctx = _Ctx(q, cfg, t)
    ctx.initial = ctx.snapshot()  # COMPARE WITH re-translates from here
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
            "equivalent — an event listing belongs in Loki (FROM Log) or "
            "Tempo (FROM Span)")

    if case_specs:
        note_cases_other(t, case_other)
        out = _translate_facet_cases(ctx, items, case_specs)
        if out is not None:
            return out
        t.note("FACET cases(...) conditions could not become label "
               "matchers; grouping dropped — split the cases into "
               "separate filtered panels manually", NEEDS_REVIEW)

    primary_expr: Optional[str] = None
    primary_legend = ""
    failures: List[str] = []
    # Several aggregations in one SELECT: every target needs a legend that
    # names its item (the alias, else the NRQL expression) next to the
    # FACET labels, or the series are indistinguishable in Grafana.
    multi = sum(1 for i in items if isinstance(i.expr, Func)) > 1
    for item in items:
        fn = item.expr
        if not isinstance(fn, Func):
            t.note("non-aggregated SELECT item %r dropped"
                   % expr_text(fn), NEEDS_REVIEW)
            continue
        # Isolate this item's legend and unit notes so multi-aggregation
        # SELECTs don't cross-pollinate target attribution.
        t.legend = ""
        extras_before = len(t.extra)
        notes_before = len(t.notes)
        conf_before = t.confidence
        try:
            expr = _wrap_topk(ctx, _translate_item(
                ctx, fn, numeric=list(ctx.numeric)))
        except Untranslatable as e:
            failures.append("%s: %s" % (select_label(fn), e))
            t.notes[notes_before:] = []
            t.confidence = conf_before
            del t.extra[extras_before:]
            continue
        if item.multiplier:
            expr = "(%s) %s" % (expr, _scale_text(item.multiplier))
            for e in t.extra[extras_before:]:
                e.expr = "(%s) %s" % (e.expr, _scale_text(item.multiplier))
            new = t.notes[notes_before:]
            unit = next((n.split(":", 1)[1] for n in new
                         if n.startswith("unit:")), "")
            t.notes[notes_before:] = [n for n in new
                                      if not n.startswith("unit:")]
            scaled = _scaled_unit(unit, item.multiplier) if unit else None
            if not unit and isinstance(fn, Func) and fn.name == "_ratio" \
                    and abs(item.multiplier - 100.0) < 1e-9:
                scaled = "percent"
            if scaled:
                t.notes.append("unit:%s" % scaled)
                if scaled != unit:
                    t.notes.append("SELECT arithmetic '%s' preserved; panel "
                                   "unit set to %s%s"
                                   % (_scale_text(item.multiplier), scaled,
                                      (" (was %s)" % unit) if unit else ""))
            else:
                t.note("SELECT arithmetic '%s' preserved; the derived "
                       "panel unit no longer applies — set it manually"
                       % _scale_text(item.multiplier), APPROXIMATE)
        item_legend = t.legend or legend_for(ctx.by, item.alias,
                                             t.legend_template)
        if multi and not t.legend:
            tag = item.alias or expr_text(fn)
            item_legend = ((legend_for(ctx.by, None, t.legend_template)
                            + " " + tag).strip() if ctx.by else tag)
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
        reasons = [f.split(": ", 1)[1] if ": " in f else f for f in failures]
        if len(failures) > 1 and len(set(reasons)) == 1:
            raise Untranslatable("%s: %s" % (
                ", ".join(f.split(": ", 1)[0] for f in failures), reasons[0]))
        raise Untranslatable("; ".join(failures) if failures
                             else "no translatable SELECT items")
    for f in failures:
        t.note("SELECT item dropped (no PromQL equivalent) — %s" % f,
               NEEDS_REVIEW)
    t.expr = primary_expr
    t.legend = primary_legend
    t.group_by = list(ctx.by)
    if not t.legend and not ctx.by and not multi:
        # One un-aliased aggregation without FACET: name the series the
        # way New Relic did (Grafana would otherwise show the PromQL).
        first = next(i for i in items if isinstance(i.expr, Func))
        t.legend = first.alias or select_label(first.expr)
    _apply_compare_with(ctx, t)
    return t


_UNIT_PRESERVING_AGGS = {"average", "avg", "percentile", "median", "max",
                         "min", "latest", "histogram", "stddev"}
_COUNT_AGGS = {"count", "uniquecount", "cardinality", "rate", "derivative"}


_REQUEST_METRIC_HINTS = ("http_server_request", "http_client_request",
                         "rpc_server", "rpc_client", "spanmetrics_calls",
                         "traces_span_metrics_calls")


def _rate_unit(fn: Func, unit: str, base: str) -> str:
    """Grafana unit for rate(agg, N unit) / derivative(x, N unit): counts
    (or requests) per second / minute; bytes per second; otherwise
    dimensionless."""
    per = 60.0
    args = fn.args if fn.name == "rate" else fn.args[1:]
    for a in args:
        if isinstance(a, Lit) and isinstance(a.value, (int, float)) \
                and not isinstance(a.value, bool):
            per = float(a.value)
    inner = fn.args[0] if fn.args and isinstance(fn.args[0], Func) else None
    while inner is not None and inner.name == "filter" and inner.args \
            and isinstance(inner.args[0], Func):
        inner = inner.args[0]
    if fn.name == "rate" and inner is not None \
            and inner.name in ("count", "uniquecount", "cardinality"):
        unit = "short"  # rate(count(*)) counts events whatever they measure
    if unit == "bytes":
        return "Bps" if per == 1 else "short"
    if unit and unit not in ("short", "none"):
        return "short"
    requests = any(h in base for h in _REQUEST_METRIC_HINTS)
    if per == 1:
        return "reqps" if requests else "cps"
    if per == 60:
        return "reqpm" if requests else "cpm"
    return "short"


def _unit_note(t: Translation, agg: str, src: Any,
               fn: Optional[Func] = None) -> None:
    """The panel unit follows the aggregation: count-shaped aggregations
    yield counts regardless of what the underlying metric measures."""
    if isinstance(src, DerivedSource):
        if agg == "rate" and fn is not None:
            t.notes.append("unit:%s" % _rate_unit(fn, src.unit, src.expr))
        elif src.unit and agg not in ("count", "uniquecount", "cardinality"):
            t.notes.append("unit:%s" % src.unit)
        elif agg in ("count", "uniquecount", "cardinality"):
            t.notes.append("unit:short")
        return
    if getattr(src, "component", "") == "count" and agg in ("sum", "rate",
                                                             "derivative"):
        src_unit = "short"
    else:
        src_unit = src.unit
    if agg in ("rate", "derivative") and fn is not None:
        t.notes.append("unit:%s" % _rate_unit(fn, src_unit, src.base))
    elif agg in _COUNT_AGGS:
        t.notes.append("unit:short")
    elif agg == "sum" and src_unit != src.unit:
        t.notes.append("unit:short")
    elif agg == "sum" and src.mtype == "counter":
        # the increase of a counter is a count (or the counter's own unit)
        t.notes.append("unit:%s" % (src.unit or "short"))
    elif src.unit and (agg in _UNIT_PRESERVING_AGGS or agg == "sum"):
        t.notes.append("unit:%s" % src.unit)


def _embedded(ctx: _Ctx, cond: Any) -> List[List[Matcher]]:
    """Convert an embedded WHERE (filter()/percentage()/if()) into extra
    matcher branches, applying the same event-type fixups as the outer
    WHERE. Numeric predicates land in ctx.t.numeric for the caller."""
    branches = cond_to_branches(cond, ctx.cfg, ctx.t)
    return [ctx.fixup_extra(b) for b in branches]


def _embedded_numeric(ctx: _Ctx, cond: Any) \
        -> Tuple[List[List[Matcher]], List[NumericPred]]:
    """Like _embedded but also returns (and clears) the numeric predicates
    the embedded condition produced."""
    nb = len(ctx.t.numeric)
    branches = _embedded(ctx, cond)
    numeric = list(ctx.t.numeric[nb:])
    del ctx.t.numeric[nb:]
    return branches, numeric


def _is_lit(v: Any, *values: float) -> bool:
    return isinstance(v, Lit) and isinstance(v.value, (int, float)) \
        and not isinstance(v.value, bool) and float(v.value) in values


def _rewrite_if(ctx: _Ctx, fn: Func) -> Tuple[Func, Optional[List[List[Matcher]]]]:
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
    cond, vals = _if_parts(branch)
    if cond is None:
        raise Untranslatable(
            "the if() condition could not be parsed as a predicate; "
            "rewrite the query as filter(%s(...), WHERE ...)" % fn_name(fn.name))
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
            "split into separate filtered queries" % fn_name(fn.name))
    ctx.t.note("if(%s, ...) translated as a filtered aggregation "
               "(the condition became label matchers)"
               % cond_text(cond), APPROXIMATE)
    return new, _embedded(ctx, cond)


def _numeric_consumable(ctx: _Ctx, pred: NumericPred) -> bool:
    """Can a numeric predicate become histogram bucket arithmetic here?"""
    key = pred.attr.lower().replace("_", "").replace(".", "")
    if ctx.is_apm_http:
        return key in ("duration", "totaltime", "webduration")
    if ctx.is_span:
        return key in ("duration", "durationms")
    return False


def _product(a: List[List[Matcher]], b: List[List[Matcher]]) \
        -> List[List[Matcher]]:
    return [x + y for x in a for y in b]


_GETFIELD_AGG = {"count": "count", "sum": "sum", "max": "max", "min": "min",
                 "average": "average", "avg": "average", "latest": "latest",
                 "total": "sum"}


# NRQL math functions -> PromQL (log() in NRQL is the natural logarithm).
_MATH_FUNCS = {"abs": "abs", "ceil": "ceil", "floor": "floor", "sqrt": "sqrt",
               "exp": "exp", "ln": "ln", "log": "ln", "log10": "log10",
               "log2": "log2", "round": "round", "clamp_max": "clamp_max",
               "clamp_min": "clamp_min", "pow": "^", "mod": "%"}
_UNITLESS_MATH = {"sqrt", "exp", "ln", "log", "log10", "log2", "pow"}


def _math_operand(ctx: "_Ctx", node: Any,
                  extra: Optional[List[List[Matcher]]],
                  numeric: Optional[List[NumericPred]]) -> str:
    """Operand of a math function: an aggregation, arithmetic over
    aggregations, or a number."""
    if isinstance(node, Func):
        return _translate_item(ctx, node, extra, list(numeric or []))
    if isinstance(node, BinOp):
        left = _math_operand(ctx, node.left, extra, numeric)
        right = _math_operand(ctx, node.right, extra, numeric)
        return "(%s %s %s)" % (left, node.op, right)
    if isinstance(node, Lit) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return _fmt_num(float(node.value))
    raise Untranslatable("math function operand %s has no metric equivalent"
                         % expr_text(node))


def _translate_item(ctx: _Ctx, fn: Func,
                    extra: Optional[List[List[Matcher]]] = None,
                    numeric: Optional[List[NumericPred]] = None) -> str:
    t = ctx.t
    extra = [list(b) for b in (extra or [[]])]
    numeric = list(numeric or [])
    fn = hoist_rate_filter(fn)
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
    if fn.name == "getfield":
        # getField(summaryMetric, count|sum|max|min|average) -> agg(m)
        if len(fn.args) < 2:
            raise Untranslatable("getField() needs (metric, field)")
        fld = fn.args[1]
        fname = (fld.name if isinstance(fld, Attr)
                 else str(getattr(fld, "value", ""))).lower()
        agg = _GETFIELD_AGG.get(fname)
        if not agg:
            raise Untranslatable(
                "getField(..., %s) has no metric equivalent" % fname)
        return _translate_item(ctx, Func(agg, args=[fn.args[0]]), extra,
                               numeric)
    if fn.name == "latest" and ctx.is_apm_http and fn.args:
        lattr, _ = unwrap_attr(fn.args[0])
        lkey = lattr.name.lower() if lattr is not None else ""
        if lattr is not None and lkey not in nrmetrics.TRANSACTION_ATTRS \
                and lkey != "timestamp" and map_attr(lattr.name, ctx.cfg)[1]:
            t.note("latest(%s) has no metric value; rendered as the %r label "
                   "values seen in the range (every value, not only the "
                   "latest)" % (lattr.name, map_attr(lattr.name, ctx.cfg)[0]),
                   APPROXIMATE)
            fn = Func("uniques", args=[lattr])
    if fn.name == "uniques":
        arg0 = fn.args[0] if fn.args else None
        uattr, _ = unwrap_attr(arg0)
        if uattr is None:
            raise Untranslatable("uniques() needs an attribute argument")
        label, mapped = map_attr(uattr.name, ctx.cfg)
        if ctx.is_apm_http and label == "span_name":
            label = "http_route"
        if not mapped:
            t.note("uniques attribute %r not in label_map; used %r"
                   % (uattr.name, label), NEEDS_REVIEW)
        ex = [list(b) for b in (extra or [[]])]
        cspec = nrmetrics.infra_count_spec(ctx.event.lower()) \
            if nrmetrics.is_infra_event(ctx.event.lower()) else None
        if cspec is not None and cspec.kind == "count":
            # the exporter's entity-population series carries the labels
            fixed = [Matcher(l, o, v) for l, o, v in cspec.matchers]
            ex = [b + fixed for b in ex]
            sel = ctx.rf("", cspec.name, ex, None)
            t.confidence = worst(t.confidence, cspec.conf)
            if cspec.note:
                t.note(cspec.note)
        else:
            src = _source_for(ctx, SelectItem(expr=Func("count",
                                                        args=[Star()])))
            t.confidence = worst(t.confidence, src.confidence)
            if src.note:
                t.note(src.note)
            if isinstance(src, DerivedSource):
                raise Untranslatable(
                    "uniques(%s) FROM %s: the entity population is a "
                    "derived expression; list the values with a dashboard "
                    "variable (label_values) instead"
                    % (uattr.name, ctx.event))
            if src.extra_matchers:
                ex = [b + list(src.extra_matchers) for b in ex]
            sel = ctx.rf("", src.name("_count" if src.mtype == "histogram"
                                      else ""), ex, None)
        labels = list(dict.fromkeys([label] + ctx.by))
        t.note("uniques(%s) listed as the distinct %r label values present "
               "in the range (an instant table; the value column is 1)"
               % (uattr.name, label), APPROXIMATE)
        t.notes.append("panel-hint:table")
        t.query_type = "instant"
        t.legend = "{{%s}}" % label
        return "group by (%s)(%s)" % (", ".join(labels), sel)

    if fn.name in _MATH_FUNCS and fn.args:
        pf = _MATH_FUNCS[fn.name]
        notes_before = len(t.notes)
        inner = _math_operand(ctx, fn.args[0], extra, numeric)
        second = fn.args[1] if len(fn.args) > 1 else None
        if fn.name == "round":
            places = 0.0
            if isinstance(second, Lit) and isinstance(second.value,
                                                      (int, float)):
                places = float(second.value)
            out = ("round(%s, %s)" % (inner, _fmt_num(10.0 ** -places))
                   if places else "round(%s)" % inner)
        elif fn.name in ("clamp_max", "clamp_min"):
            if second is None:
                raise Untranslatable("%s() needs a bound" % fn_name(fn.name))
            out = "%s(%s, %s)" % (pf, inner, _math_operand(ctx, second,
                                                           extra, numeric))
        elif fn.name in ("pow", "mod"):
            if second is None:
                raise Untranslatable("%s() needs a second operand" % fn_name(fn.name))
            out = "(%s) %s (%s)" % (inner, pf, _math_operand(ctx, second,
                                                             extra, numeric))
        else:
            out = "%s(%s)" % (pf, inner)
        if fn.name in _UNITLESS_MATH:
            t.notes[notes_before:] = [n for n in t.notes[notes_before:]
                                      if not n.startswith("unit:")]
        t.notes.append("%s() applied as PromQL %s" % (fn_name(fn.name), pf))
        return out

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
        left_expr = _translate_item(ctx, left, extra, list(numeric))
        left_units = [n for n in t.notes[notes_before:]
                      if n.startswith("unit:")]
        mid = len(t.notes)
        right_expr = _translate_item(ctx, right, extra, list(numeric))
        right_units = [n for n in t.notes[mid:] if n.startswith("unit:")]
        left_expr, right_expr = _cancel_per_step(left_expr, right_expr)
        # A ratio is dimensionless; the operands' own unit hints do not
        # carry over to the quotient.
        new = t.notes[notes_before:]
        t.notes[notes_before:] = [n for n in new if not n.startswith("unit:")]
        t.note("agg(x) / agg(y) ratio: PromQL divides the two results "
               "matching on the shared FACET grouping (a series present in "
               "only one operand drops out; the denominator must be "
               "non-zero)", APPROXIMATE)
        count_like = {"count", "uniquecount", "cardinality", "rate",
                      "filter", "percentage"}

        def base_name(f: Func) -> str:
            while f.name == "filter" and f.args and isinstance(f.args[0], Func):
                f = f.args[0]
            return f.name

        def filtered(f: Func) -> bool:
            # filter(count(*), WHERE c) / count(if(c, 1)) / sum(if(c, 1, 0))
            return f.name == "filter" or f.where is not None or bool(
                f.args and isinstance(f.args[0], Func)
                and f.args[0].name == "if")
        # sum() of a counter is a count too; an average/latest of a plain
        # number (load / cores) is not.
        counts_left = base_name(left) in count_like \
            or (base_name(left) == "sum" and "unit:short" in left_units)
        counts_right = base_name(right) in count_like \
            or (base_name(right) == "sum" and "unit:short" in right_units)
        per_entity = base_name(right) in ("uniquecount", "cardinality")
        if counts_left and counts_right and (filtered(left)
                                             or not per_entity):
            # count/count (errors over requests, a filtered count over the
            # count) is a proportion in [0, 1]; count(*) / uniqueCount(host)
            # is a per-entity number.
            t.notes.append("unit:percentunit")
        return "(%s) / (%s)" % (left_expr, right_expr)

    if fn.name == "_arith":
        op = str(getattr(fn.args[0], "value", "+"))
        left, right = fn.args[1], fn.args[2]

        def side(node: Any) -> str:
            if isinstance(node, Func):
                return "(%s)" % _translate_item(ctx, node, extra,
                                                list(numeric))
            if isinstance(node, Lit) and isinstance(node.value, (int, float)):
                return _fmt_num(float(node.value))
            raise Untranslatable(
                "arithmetic operand %s has no metric equivalent"
                % expr_text(node))
        notes_before = len(t.notes)
        l_expr = side(left)
        r_expr = side(right)
        new = t.notes[notes_before:]
        units = [n for n in new if n.startswith("unit:")]
        t.notes[notes_before:] = [n for n in new if not n.startswith("unit:")]
        if op in ("+", "-") and units:
            t.notes.append(units[0])
        t.note("SELECT arithmetic (%s) between aggregations preserved as "
               "PromQL arithmetic; both sides match on the FACET grouping"
               % op, APPROXIMATE)
        return "%s %s %s" % (l_expr, op, r_expr)

    if fn.name == "filter":
        inner = fn.args[0] if fn.args else None
        if not isinstance(inner, Func):
            raise Untranslatable("filter() needs an inner aggregation")
        nb = len(t.numeric)
        extra = _product(extra, _embedded(ctx, fn.where))
        numeric = numeric + t.numeric[nb:]
        del t.numeric[nb:]
        inner, if_extra = _rewrite_if(ctx, inner)
        if if_extra:
            numeric = numeric + t.numeric
            del t.numeric[:]
            extra = _product(extra, if_extra)
        src = _source_for(ctx, SelectItem(expr=inner))
        t.confidence = worst(t.confidence, src.confidence)
        if src.note:
            t.note(src.note)
        _unit_note(t, inner.name, src, inner)
        if isinstance(src, DerivedSource):
            _note_leftover_numeric(ctx, numeric)
            return _derived_expr(ctx, inner, src, extra)
        return _agg_expr(ctx, inner, src, extra=extra, numeric=numeric)

    if fn.name == "percentage":
        inner = fn.args[0] if fn.args else None
        if not isinstance(inner, Func):
            raise Untranslatable("percentage() needs an inner aggregation")
        nb = len(t.numeric)
        num_extra = _product(extra, _embedded(ctx, fn.where))
        num_numeric = numeric + t.numeric[nb:]
        del t.numeric[nb:]
        src = _source_for(ctx, SelectItem(expr=inner))
        t.confidence = worst(t.confidence, src.confidence)
        if src.note:
            t.note(src.note)
        if isinstance(src, DerivedSource):
            num = _derived_expr(ctx, inner, src, num_extra)
            den = _derived_expr(ctx, inner, src, extra)
        else:
            num = _agg_expr(ctx, inner, src, extra=num_extra,
                            numeric=num_numeric)
            den = _agg_expr(ctx, inner, src, extra=extra, numeric=numeric)
            num, den = _cancel_per_step(num, den)
        t.notes.append("unit:percent")
        return "100 * (%s) / (%s)" % (num, den)

    fn, if_extra = _rewrite_if(ctx, fn)
    if if_extra:
        numeric = numeric + t.numeric
        del t.numeric[:]
        extra = _product(extra, if_extra)
    src = _source_for(ctx, SelectItem(expr=fn))
    t.confidence = worst(t.confidence, src.confidence)
    if src.note:
        t.note(src.note)
    _unit_note(t, fn.name, src, fn)
    if isinstance(src, DerivedSource):
        _note_leftover_numeric(ctx, numeric)
        return _derived_expr(ctx, fn, src, extra)
    if src.value_attr == "timestamp":
        return _latest_timestamp(ctx, fn, src, extra)
    return _agg_expr(ctx, fn, src, extra=extra, numeric=numeric)


def _note_leftover_numeric(ctx: _Ctx, numeric: List[NumericPred]) -> None:
    """A derived (template) expression cannot take numeric predicates the
    count path did not turn into filters: say so instead of dropping them
    silently."""
    for p in numeric:
        if p in ctx.consumed_numeric:
            continue
        ctx.t.note("numeric comparison %s %s %s cannot become a label "
                   "matcher for this derived expression; dropped — apply "
                   "it manually" % (p.attr, p.op, _fmt_num(p.value)),
                   NEEDS_REVIEW)


def _latest_timestamp(ctx: _Ctx, fn: Func, src: MetricSource,
                      extra: List[List[Matcher]]) -> str:
    """latest(timestamp) -> time of the last sample (ms epoch)."""
    t = ctx.t
    if fn.name not in ("latest", "max"):
        raise Untranslatable(
            "%s(timestamp) has no metric equivalent (only latest(timestamp) "
            "— time of the last sample — translates)" % fn_name(fn.name))
    t.notes[:] = [n for n in t.notes if not n.startswith("unit:")]
    t.notes.append("unit:dateTimeAsIso")
    t.note("latest(timestamp) rendered as the timestamp of the most recent "
           "sample (epoch milliseconds)", APPROXIMATE)
    suffix = "_count" if src.mtype == "histogram" else ""
    sel = ctx.rf("timestamp", src.name(suffix), extra, None)
    return "max%s(%s) * 1000" % (ctx.by_clause(), sel)


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
    # The first pass consumed matchers (phase filters, numeric predicates)
    # and facet labels; the comparison series must start from the same
    # WHERE, not from what was left over.
    saved = ctx.snapshot()
    if ctx.initial is not None:
        ctx.restore(ctx.initial)
    ctx.offset = " offset %s" % off
    try:
        shifted = translate_to_promql_with_offset(ctx)
    finally:
        ctx.restore(saved)
        ctx.offset = ""
    if shifted:
        shifted.legend = ((t.legend + " " if t.legend else "")
                          + "(%s earlier)" % off)
        t.extra.append(shifted)


def variable_scope(q: NrqlQuery, cfg: Dict[str, Any]) -> str:
    """Selector scoping a Grafana label_values() variable derived from a
    dashboard-variable NRQL (SELECT uniques(attr) FROM X WHERE ...): the
    metric family FROM maps to, with the WHERE clause as matchers. '' when
    no metric can be named (the variable is then unscoped)."""
    t = Translation(datasource="prometheus")
    try:
        ctx = _Ctx(q, cfg, t)
        etl = ctx.event.lower()
        name = ""
        fixed: List[Matcher] = []
        if etl in ("transaction", "transactionerror"):
            name = http_server_source(cfg).name("_count")
        elif ctx.is_span:
            name = spanmetrics_source(cfg, "calls").name()
        elif ctx.is_metric_event:
            mn = _metric_name_from_where(ctx)
            if mn:
                src = resolve_metric(mn, "latest", cfg, t)
                if isinstance(src, MetricSource):
                    name = src.name("_count" if src.mtype == "histogram"
                                    else "")
                    fixed = list(src.extra_matchers or [])
        elif ctx.is_legacy_aws:
            ns = nrmetrics.legacy_aws_namespace(ctx.event, ctx.aws_provider)
            if ns:
                name = "aws_%s_info" % ns
        elif nrmetrics.is_infra_event(etl):
            spec = nrmetrics.infra_count_spec(etl)
            if spec is not None and spec.kind == "count":
                name = spec.name
                fixed = [Matcher(l, o, v) for l, o, v in spec.matchers]
        if not name:
            return ""
        return render_selector(name, fixed + ctx.matchers)
    except Untranslatable:
        return ""


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
        expr = _translate_item(ctx, items[0].expr, numeric=list(ctx.numeric))
        if items[0].multiplier:
            expr = "(%s) %s" % (expr, _scale_text(items[0].multiplier))
        sub.expr = _wrap_topk(ctx, expr)
        sub.group_by = list(ctx.by)
        return sub
    except Untranslatable:
        return None
    finally:
        ctx.t = saved_t
