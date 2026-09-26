"""NRQL (FROM Span, search-shaped) -> TraceQL translation.

Aggregation-shaped Span queries are handled by the metrics translator via
span metrics; this module handles trace search / listing widgets
(SELECT * / plain attributes FROM Span WHERE ...).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..nrql.parser import (
    Attr, BoolOp, Cmp, Cond, Func, InList, Lit, NotOp, NrqlQuery, NullCheck,
)
from .common import (
    APPROXIMATE, NEEDS_REVIEW, Translation, Untranslatable, _VAR_RE,
    grafana_var, is_nr_variable, q, regex_escape,
)

# NR span attribute -> TraceQL field.
_TRACEQL_FIELDS = {
    "service.name": "resource.service.name",
    "appname": "resource.service.name",
    "entity.name": "resource.service.name",
    "serviceName": "resource.service.name",
    "name": "name",
    "span.kind": "kind",
    "spankind": "kind",
    "trace.id": "trace:id",
    "traceid": "trace:id",
    "http.statusCode": "span.http.response.status_code",
    "http.status_code": "span.http.response.status_code",
    "http.method": "span.http.request.method",
    "db.system": "span.db.system",
    "root.entity.name": "resource.service.name",
    "root.entityName": "resource.service.name",
}
_ROOT_ATTRS = {"parentid", "parent.id", "parentspanid", "parent.span.id",
               "nr.entrypoint", "entrypoint"}

_DURATION_ATTRS = {"duration", "duration.ms", "durationms", "duration_ms"}
_KIND_VALUES = {"server", "client", "producer", "consumer", "internal"}

# TraceQL is typed: an int attribute never matches a string literal (and
# regex operators are rejected on ints), so these compare as numbers.
_INT_FIELDS = {
    "span.http.response.status_code", "span.http.status_code",
    "span.http.request.body.size", "span.http.response.body.size",
    "span.net.peer.port", "span.net.host.port", "span.server.port",
    "span.client.port", "span.rpc.grpc.status_code", "span.thread.id",
    "resource.process.pid",
}
_INT_ATTR_SUFFIXES = ("statuscode", "status_code", ".port", "_port", ".pid",
                      ".size", "_size")


def _is_int_field(field: str, attr: str) -> bool:
    if field in _INT_FIELDS:
        return True
    return attr.lower().endswith(_INT_ATTR_SUFFIXES)


def _int_text(v: Any) -> Optional[str]:
    """Integer text for a literal that is (or spells) an integer."""
    raw = getattr(v, "value", v)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return str(int(raw)) if float(raw) == int(raw) else None
    if isinstance(raw, str) and re.fullmatch(r"-?\d+", raw.strip()):
        return str(int(raw.strip()))
    return None


def _field_for(attr: str, t: Translation) -> str:
    if attr in _TRACEQL_FIELDS:
        return _TRACEQL_FIELDS[attr]
    low = attr.lower()
    for k, v in _TRACEQL_FIELDS.items():
        if k.lower() == low:
            return v
    if low in _DURATION_ATTRS:
        return "duration"
    if low in ("error", "otel.status_code", "status"):
        return "status"
    # Unknown scope: use scope-agnostic attribute lookup.
    t.note("attribute %r mapped to scope-agnostic .%s in TraceQL; verify "
           "scope (span./resource.)" % (attr, attr), NEEDS_REVIEW)
    return "." + attr


def _value_text(v: Any, cfg: Optional[Dict[str, Any]] = None) -> str:
    if isinstance(v, Lit):
        if isinstance(v.value, bool):
            return "true" if v.value else "false"
        if isinstance(v.value, (int, float)):
            n = v.value
            return str(int(n)) if float(n) == int(n) else str(n)
        # 'prod-{{svc}}': keep the placeholder as a Grafana variable.
        return q(_VAR_RE.sub(lambda m: "$" + grafana_var(m.group(1), cfg or {}),
                             str(v.value)))
    if isinstance(v, Attr):
        return q(v.name)
    return q(str(v))


def _cond_to_traceql(cond: Optional[Cond], t: Translation,
                     cfg: Dict[str, Any]) -> str:
    if cond is None:
        return ""
    if isinstance(cond, BoolOp):
        joiner = " && " if cond.op == "and" else " || "
        parts = [_cond_to_traceql(c, t, cfg) for c in cond.items]
        parts = [p for p in parts if p]
        return "(" + joiner.join(parts) + ")" if len(parts) > 1 else \
            (parts[0] if parts else "")
    if isinstance(cond, NotOp):
        inner = _cond_to_traceql(cond.item, t, cfg)
        return "!(%s)" % inner if inner else ""
    if isinstance(cond, Cmp):
        return _cmp_to_traceql(cond, t, cfg)
    if isinstance(cond, InList):
        if not isinstance(cond.left, Attr):
            t.note("IN on a non-attribute dropped", NEEDS_REVIEW)
            return ""
        field = _field_for(cond.left.name, t)
        if _is_int_field(field, cond.left.name):
            ints = [_int_text(v) for v in cond.values]
            if ints and all(i is not None for i in ints):
                if cond.negated:
                    return "(%s)" % " && ".join(
                        "%s != %s" % (field, i) for i in ints)
                return "(%s)" % " || ".join(
                    "%s = %s" % (field, i) for i in ints)
        # TraceQL regex matchers are UNANCHORED (unlike PromQL) — anchor
        # explicitly or IN degrades to substring matching.
        alt = "|".join(regex_escape(str(getattr(v, "value", v)))
                       for v in cond.values)
        op = "!~" if cond.negated else "=~"
        return "%s %s %s" % (field, op, q("^(?:%s)$" % alt))
    if isinstance(cond, NullCheck):
        if isinstance(cond.left, Attr):
            if cond.left.name.lower() in _ROOT_ATTRS:
                # parentId IS NULL: a root span (Tempo's nested-set model).
                return "nestedSetParent %s 0" % (">=" if cond.negated
                                                 else "<")
            field = _field_for(cond.left.name, t)
            return "%s %s nil" % (field, "!=" if cond.negated else "=")
        return ""
    t.note("unsupported WHERE construct dropped in TraceQL", NEEDS_REVIEW)
    return ""


def _cmp_to_traceql(c: Cmp, t: Translation, cfg: Dict[str, Any]) -> str:
    if not isinstance(c.left, Attr):
        t.note("comparison on non-attribute dropped", NEEDS_REVIEW)
        return ""
    attr = c.left.name
    low = attr.lower()
    if low in _ROOT_ATTRS and c.op in ("=", "!="):
        # nr.entryPoint IS TRUE: the trace's root span.
        text = str(getattr(c.right, "value", c.right)).strip().lower()
        wants_root = (text in ("true", "1")) == (c.op == "=")
        return "nestedSetParent %s 0" % ("<" if wants_root else ">=")
    field = _field_for(attr, t)

    # error IS TRUE handled by parser as Cmp(error, '=', Lit(True));
    # otel.status_code = 'ERROR' / 'OK' / 'UNSET' name the status directly.
    if field == "status":
        raw = c.right.value if isinstance(c.right, Lit) else c.right
        text = str(raw).strip().lower()
        if low in ("otel.status_code", "status") and text in (
                "error", "ok", "unset", "status_code_error",
                "status_code_ok", "status_code_unset"):
            code = text.replace("status_code_", "")
            if c.op in ("=", "!="):
                return "status %s %s" % (c.op, code)
        truthy = text in ("true", "1")
        if c.op in ("=", "!="):
            eq = (c.op == "=") == bool(truthy)
            return "status = error" if eq else "status != error"
    if field == "kind":
        val = str(getattr(c.right, "value", c.right)).lower()
        if val in _KIND_VALUES:
            return "kind %s %s" % (c.op if c.op in ("=", "!=") else "=", val)

    if field == "duration":
        n = getattr(c.right, "value", None)
        if isinstance(n, (int, float)):
            unit = "ms" if "ms" in low or low == "duration.ms" else "s"
            num = int(n) if float(n) == int(n) else n
            op = c.op if c.op in ("<", "<=", ">", ">=", "=", "!=") else ">"
            return "duration %s %s%s" % (op, num, unit)

    var = is_nr_variable(c.right)
    if var:
        rhs = q("$%s" % grafana_var(var, cfg))
    else:
        rhs = _value_text(c.right, cfg)

    if not var and _is_int_field(field, attr):
        it = _int_text(c.right)
        if it is not None and c.op in ("=", "!=", "<", "<=", ">", ">="):
            return "%s %s %s" % (field, c.op, it)
        if c.op in ("LIKE", "NOT LIKE"):
            # http.statusCode LIKE '5%' -> the numeric band 500..599 (a
            # status code has three digits; the wildcard fills the rest).
            pat = str(getattr(c.right, "value", ""))
            m = re.fullmatch(r"(\d{1,3})%+", pat)
            if m and field.endswith("status_code"):
                width = 10 ** (3 - len(m.group(1)))
                lo = int(m.group(1)) * width
                hi = lo + width
                if c.op == "LIKE":
                    return "(%s >= %d && %s < %d)" % (field, lo, field, hi)
                return "(%s < %d || %s >= %d)" % (field, lo, field, hi)
            t.note("%s is an integer attribute in TraceQL; the LIKE pattern "
                   "%r cannot be applied to it (regex operators are rejected "
                   "on ints); dropped — express it as a numeric range"
                   % (field, pat), NEEDS_REVIEW)
            return ""
        if it is None and c.op in ("=", "!=") and isinstance(c.right, Lit):
            t.note("%s is an integer attribute in TraceQL but the NRQL "
                   "compares it with %r; verify the value" % (field,
                                                               c.right.value),
                   NEEDS_REVIEW)

    if c.op in ("=", "!=", "<", "<=", ">", ">="):
        return "%s %s %s" % (field, c.op, rhs)
    # TraceQL regex matchers are UNANCHORED (unlike PromQL/NRQL) — anchor
    # explicitly to preserve whole-value matching semantics.
    if c.op in ("LIKE", "NOT LIKE"):
        pattern = str(getattr(c.right, "value", ""))

        def like_body(seg: str) -> str:
            return "".join(".*" if ch == "%" else
                           ("." if ch == "_" else regex_escape(ch))
                           for ch in seg)
        parts: List[str] = []
        pos = 0
        for m in _VAR_RE.finditer(pattern):
            parts.append(like_body(pattern[pos:m.start()]))
            parts.append("${%s:regex}" % grafana_var(m.group(1), cfg))
            pos = m.end()
        parts.append(like_body(pattern[pos:]))
        body = "".join(parts)
        rx = "(?i)^%s$" % body
        return "%s %s %s" % (field, "=~" if c.op == "LIKE" else "!~", q(rx))
    if c.op in ("RLIKE", "NOT RLIKE"):
        raw_rx = str(getattr(c.right, "value", rhs))
        return "%s %s %s" % (field, "=~" if c.op == "RLIKE" else "!~",
                             q("^(?:%s)$" % raw_rx))
    t.note("operator %r unsupported in TraceQL; dropped" % c.op, NEEDS_REVIEW)
    return ""


def variable_field(attr: str) -> str:
    """Scoped tag name for a Tempo label-values dashboard variable."""
    low = attr.lower()
    for table in (_TRACEQL_BY_FIELDS, _TRACEQL_FIELDS):
        for k, v in table.items():
            if k.lower() == low:
                return v
    return "." + attr  # unknown scope: Tempo searches every scope


_TRACEQL_BY_FIELDS = {
    "service.name": "resource.service.name", "appname": "resource.service.name",
    "name": "name", "span.kind": "kind", "http.statuscode":
    "span.http.response.status_code", "http.status_code":
    "span.http.response.status_code", "http.method":
    "span.http.request.method", "db.system": "span.db.system",
}


def translate_span_metrics_traceql(nq: NrqlQuery, cfg: Dict[str, Any],
                                   root_only: bool = False) -> Translation:
    """Aggregation-shaped FROM Span queries as TraceQL metrics (Tempo
    2.4+): ``{ filters } | rate() by (field)``. Used when the config sets
    ``span_aggregations: "traceql"`` and always for uniqueCount(trace.id),
    which span metrics cannot express."""
    t = Translation(datasource="tempo", query_type="traceql-metrics",
                    confidence=APPROXIMATE)
    aggs = [i for i in nq.select if isinstance(i.expr, Func)]
    if not aggs:
        raise Untranslatable("TraceQL metrics need an aggregation")
    fn = aggs[0].expr
    assert isinstance(fn, Func)
    body = _cond_to_traceql(nq.where, t, cfg)
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    arg = fn.args[0] if fn.args else None
    attr = arg.name.lower() if isinstance(arg, Attr) else ""
    is_trace_count = fn.name in ("uniquecount", "cardinality") and attr in (
        "trace.id", "traceid", "trace_id")
    if root_only and not is_trace_count:
        # FROM DistributedTraceSummary: one row per trace = its root span.
        body = ("nestedSetParent < 0" + (" && " + body if body else ""))
        t.note("FROM DistributedTraceSummary aggregates one row per trace; "
               "translated over root spans (nestedSetParent < 0) with "
               "TraceQL metrics", NEEDS_REVIEW)
    if is_trace_count:
        body = ("nestedSetParent < 0" + (" && " + body if body else ""))
        agg = "count_over_time()"
        t.note("uniqueCount(trace.id) approximated as the number of root "
               "spans matching the filters (one root span per trace); "
               "filters on non-root spans would undercount", NEEDS_REVIEW)
        t.notes.append("unit:short")
    elif fn.name == "count":
        agg = "count_over_time()"
        t.notes.append("unit:short")
    elif fn.name == "rate":
        agg = "rate()"
        per = 60.0
        for a in fn.args:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                per = float(a.value)
        if per != 1:
            t.note("rate(count(*), %g seconds) rendered as a per-second "
                   "rate; scale the panel by %g" % (per, per), APPROXIMATE)
        t.notes.append("unit:reqps")
    elif fn.name in ("average", "avg", "max", "min", "percentile",
                     "median", "histogram", "sum"):
        if attr not in _DURATION_ATTRS:
            raise Untranslatable(
                "TraceQL metrics aggregate duration only; %s(%s) has no "
                "equivalent" % (fn.name, attr or "?"))
        if fn.name in ("average", "avg"):
            agg = "avg_over_time(duration)"
            t.note("avg_over_time() needs Tempo 2.6+", APPROXIMATE)
        elif fn.name == "max":
            agg = "max_over_time(duration)"
            t.note("max_over_time() needs Tempo 2.6+", APPROXIMATE)
        elif fn.name == "min":
            agg = "min_over_time(duration)"
            t.note("min_over_time() needs Tempo 2.6+", APPROXIMATE)
        elif fn.name == "sum":
            agg = "sum_over_time(duration)"
            t.note("sum_over_time() needs Tempo 2.7+", APPROXIMATE)
        elif fn.name == "histogram":
            agg = "histogram_over_time(duration)"
            t.notes.append("panel-hint:heatmap")
        else:
            pcts = [50.0] if fn.name == "median" else [
                float(a.value) for a in fn.args[1:]
                if isinstance(a, Lit) and isinstance(a.value, (int, float))
            ] or [95.0]
            agg = "quantile_over_time(duration, %s)" % ", ".join(
                ("%f" % (p / 100.0)).rstrip("0").rstrip(".") for p in pcts)
        t.notes.append("unit:s")
        t.note("TraceQL metrics compute over sampled spans in Tempo's "
               "metrics-generator window; values are seconds", APPROXIMATE)
    else:
        raise Untranslatable(
            "%s() has no TraceQL metrics equivalent" % fn.name)
    by = []
    for item in nq.facet:
        if isinstance(item.expr, Attr):
            by.append(_TRACEQL_BY_FIELDS.get(
                item.expr.name.lower(), "." + item.expr.name))
        else:
            t.note("FACET %s has no TraceQL by() equivalent; dropped"
                   % getattr(item.expr, "name", "?"), NEEDS_REVIEW)
    t.expr = "{ %s } | %s%s" % (body, agg,
                                (" by (%s)" % ", ".join(by)) if by else "")
    t.group_by = by
    if by:
        t.legend = " / ".join("{{%s}}" % b for b in by)
    if len(aggs) > 1:
        t.note("only the first aggregation was translated to TraceQL "
               "metrics; add the others as separate panels", NEEDS_REVIEW)
    if nq.compare_with:
        t.note("COMPARE WITH is not supported by TraceQL metrics; "
               "comparison dropped", NEEDS_REVIEW)
    t.notes.append("panel-hint:traceql-metrics")
    t.note("requires Tempo 2.4+ with the metrics-generator local-blocks "
           "processor enabled (TraceQL metrics)", NEEDS_REVIEW)
    return t


def translate_to_traceql(nq: NrqlQuery, cfg: Dict[str, Any]) -> Translation:
    t = Translation(datasource="tempo", query_type="traceql",
                    confidence=APPROXIMATE)
    body = _cond_to_traceql(nq.where, t, cfg)
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    t.expr = "{ %s }" % body if body else "{ }"
    if not body:
        t.note("no WHERE filters; this searches all traces", NEEDS_REVIEW)
    if isinstance(nq.limit, int):
        t.notes.append("limit:%d" % nq.limit)
    if nq.facet:
        t.note("FACET has no effect on a trace-search panel; grouping "
               "dropped", NEEDS_REVIEW)
    if nq.compare_with:
        t.note("COMPARE WITH is not applicable to trace search; "
               "comparison dropped", NEEDS_REVIEW)
    t.notes.append("panel-hint:traces")
    t.note("trace search results differ from NR raw span listings; "
           "Tempo returns matching traces/spans within the time range")
    return t
