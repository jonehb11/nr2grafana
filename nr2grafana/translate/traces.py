"""NRQL (FROM Span, search-shaped) -> TraceQL translation.

Aggregation-shaped Span queries are handled by the metrics translator via
span metrics; this module handles trace search / listing widgets
(SELECT * / plain attributes FROM Span WHERE ...).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..nrql.parser import (
    Attr, BoolOp, Cmp, Cond, Func, InList, Lit, NotOp, NrqlQuery, NullCheck,
)
from .common import (
    fn_name, expr_text, unwrap_attr,
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
    "root.span.name": "name",
    "root.spanName": "name",
}
# round(percentile(duration, 99), 1): cosmetic wrappers TraceQL lacks.
_TRACE_MATH_WRAPPERS = {"round", "abs", "floor", "ceil"}
# Trace-level counts of DistributedTraceSummary with no TraceQL field.
_TRACE_LEVEL_COUNTS = {"spancount", "span.count", "entitycount",
                       "entity.count", "servicecount", "service.count"}
_ROOT_ATTRS = {"parentid", "parent.id", "parentspanid", "parent.span.id",
               "nr.entrypoint", "entrypoint"}
# TraceQL fields typed string: a bare numeric literal against them is a
# type error ("binary operations must operate on the same type").
_STRING_FIELDS = {"trace:id", "span:id", "name", "span:name",
                  "resource.service.name", "span.http.request.method",
                  "span.db.system", "rootName", "rootServiceName"}

# Sentinel for a trace-level condition (errorCount > 0) that the entry
# points move out of the span filter: { spans } && { status = error }.
ANY_ERROR_SPAN = "__ANY_ERROR_SPAN__"
_ANY_ERROR_RE = re.compile(r"(\s*&&\s*)?__ANY_ERROR_SPAN__(\s*&&\s*)?")


def _top_level_or(body: str) -> bool:
    """True when the body has a || outside parentheses and quotes."""
    depth, quote = 0, ""
    for i, ch in enumerate(body):
        if quote:
            if ch == quote and body[i - 1] != "\\":
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0 and body[i:i + 2] == "||":
            return True
    return False


def strip_outer_parens(body: str, keep_or: bool = False) -> str:
    """'(a && b)' -> 'a && b' when one pair of parentheses wraps the whole
    body. With ``keep_or`` a body whose top level is an OR keeps them: a
    root-span filter is ANDed in front of it and && binds tighter than ||
    in TraceQL, so `nestedSetParent < 0 && a || b` would change meaning."""
    if not (body.startswith("(") and body.endswith(")")):
        return body
    depth, quote = 0, ""
    for i, ch in enumerate(body):
        if quote:
            if ch == quote and body[i - 1] != "\\":
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i != len(body) - 1:
                return body  # '(a) || (b)': not a single group
    inner = body[1:-1]
    if keep_or and _top_level_or(inner):
        return body
    return inner


def hoist_trace_level(body: str, t: Translation) -> Tuple[str, str]:
    """Remove the ANY_ERROR_SPAN sentinel from a span filter body; return
    (body, trace-level suffix). Inside an OR it cannot be hoisted and
    degrades to a span-level status filter."""
    if ANY_ERROR_SPAN not in body:
        return body, ""
    suffix = ""

    def sub(m: "re.Match[str]") -> str:
        return " && " if (m.group(1) and m.group(2)) else ""
    new = _ANY_ERROR_RE.sub(sub, body).strip()
    if ANY_ERROR_SPAN in new:  # pragma: no cover - defensive
        new = new.replace(ANY_ERROR_SPAN, "status = error")
    if "||" in body and ("(" in body):
        # an OR around the sentinel: approximate on the span itself
        new = _ANY_ERROR_RE.sub(lambda m: (m.group(1) or "") +
                                "status = error" + (m.group(2) or ""), body)
        t.note("errorCount > 0 inside an OR became a span-level status = "
               "error filter (TraceQL cannot OR a trace-level condition)",
               NEEDS_REVIEW)
        return new.strip(), ""
    suffix = " && { status = error }"
    t.note("errorCount > 0 became the trace-level condition "
           "`&& { status = error }` (the trace has at least one errored "
           "span)", APPROXIMATE)
    return strip_outer_parens(new, keep_or=True), suffix

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
            if not cond.negated:
                t.note("%s IS NULL became `%s = nil`; Tempo 2.7 rejects it "
                       "({.a = nil} not yet supported) and cannot select "
                       "spans that lack an attribute at all — drop the "
                       "predicate there" % (cond.left.name, field),
                       NEEDS_REVIEW)
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
    if low in _TRACE_LEVEL_COUNTS:
        t.note("WHERE %s %s %s: a trace's span/entity count has no TraceQL "
               "field; filter dropped" % (attr, c.op,
                                          expr_text(c.right)), NEEDS_REVIEW)
        return ""
    if low in _ROOT_ATTRS and c.op in ("=", "!="):
        # nr.entryPoint IS TRUE: the trace's root span.
        text = str(getattr(c.right, "value", c.right)).strip().lower()
        wants_root = (text in ("true", "1")) == (c.op == "=")
        return "nestedSetParent %s 0" % ("<" if wants_root else ">=")
    if low in ("errorcount", "error.count") and isinstance(c.right, Lit) \
            and isinstance(c.right.value, (int, float)):
        # DistributedTraceSummary.errorCount > 0: the trace has an errored
        # span — a trace-level condition ({ ... } && { status = error })
        # hoisted out of the span filter by the callers.
        n = float(c.right.value)
        any_error = (c.op == ">" and n <= 0) or (c.op == ">=" and n <= 1) \
            or (c.op == "!=" and n == 0)
        if any_error:
            return ANY_ERROR_SPAN
        t.note("errorCount %s %s cannot be expressed in TraceQL (a trace "
               "with no errored span / an exact error count); dropped"
               % (c.op, ("%g" % n)), NEEDS_REVIEW)
        return ""
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
        t.note("WHERE %s %s %r: %r is not a span kind (%s); the TraceQL "
               "kind field is an enum, so the filter was dropped"
               % (attr, c.op, val, val, ", ".join(sorted(_KIND_VALUES))),
               NEEDS_REVIEW)
        return ""

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
    elif field in _STRING_FIELDS and isinstance(c.right, Lit) \
            and isinstance(c.right.value, (int, float)) \
            and not isinstance(c.right.value, bool):
        n = c.right.value  # trace.id = 500: the id is a string in TraceQL
        rhs = q(str(int(n)) if float(n) == int(n) else str(n))
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
    while fn.name in _TRACE_MATH_WRAPPERS and fn.args \
            and isinstance(fn.args[0], Func):
        t.note("%s() dropped: TraceQL metrics have no math functions (use "
               "the panel's decimals / unit settings)" % fn_name(fn.name),
               APPROXIMATE)
        fn = fn.args[0]
    arg = fn.args[0] if fn.args else None
    attr_node, _cast = unwrap_attr(arg) if arg is not None else (None, None)
    attr = attr_node.name.lower() if isinstance(attr_node, Attr) else ""
    is_trace_count = fn.name in ("uniquecount", "cardinality") and attr in (
        "trace.id", "traceid", "trace_id")
    body = _cond_to_traceql(nq.where, t, cfg)
    body = strip_outer_parens(body, keep_or=root_only or is_trace_count)
    body, trace_level = hoist_trace_level(body, t)
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
                "equivalent" % (fn_name(fn.name), attr or "?"))
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
            t.note("sum_over_time() needs Tempo 2.8+ (2.7 rejects it)",
                   NEEDS_REVIEW)
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
            "arithmetic between aggregations has no TraceQL metrics "
            "equivalent (Tempo cannot combine metrics queries); split it "
            "into panels" if fn.name in ("_arith", "_ratio") else
            "%s() has no TraceQL metrics equivalent" % fn_name(fn.name))
    by = []
    for item in nq.facet:
        if isinstance(item.expr, Attr) and "{{" in item.expr.name:
            t.note("FACET %s: a dashboard variable cannot name a TraceQL "
                   "by() attribute; grouping dropped" % item.expr.name,
                   NEEDS_REVIEW)
        elif isinstance(item.expr, Attr):
            # The same field resolution as WHERE (root.entity.name ->
            # resource.service.name), so a facet never names a field the
            # filter spelled differently.
            by.append(_TRACEQL_BY_FIELDS.get(item.expr.name.lower())
                      or _field_for(item.expr.name, t))
        else:
            t.note("FACET %s has no TraceQL by() equivalent; dropped"
                   % getattr(item.expr, "name", "?"), NEEDS_REVIEW)
    t.expr = "{ %s }%s | %s%s" % (body, trace_level, agg,
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
    body = strip_outer_parens(body)
    body, trace_level = hoist_trace_level(body, t)
    t.expr = ("{ %s }" % body if body else "{ }") + trace_level
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
