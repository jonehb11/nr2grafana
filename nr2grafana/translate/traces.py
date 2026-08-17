"""NRQL (FROM Span, search-shaped) -> TraceQL translation.

Aggregation-shaped Span queries are handled by the metrics translator via
span metrics; this module handles trace search / listing widgets
(SELECT * / plain attributes FROM Span WHERE ...).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..nrql.parser import (
    Attr, BoolOp, Cmp, Cond, Func, InList, Lit, NotOp, NrqlQuery, NullCheck,
)
from .common import (
    APPROXIMATE, NEEDS_REVIEW, Translation, Untranslatable, grafana_var,
    is_nr_variable, q, regex_escape,
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
}

_DURATION_ATTRS = {"duration", "duration.ms", "durationms", "duration_ms"}
_KIND_VALUES = {"server", "client", "producer", "consumer", "internal"}


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


def _value_text(v: Any) -> str:
    if isinstance(v, Lit):
        if isinstance(v.value, bool):
            return "true" if v.value else "false"
        if isinstance(v.value, (int, float)):
            n = v.value
            return str(int(n)) if float(n) == int(n) else str(n)
        return q(str(v.value))
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
        # TraceQL regex matchers are UNANCHORED (unlike PromQL) — anchor
        # explicitly or IN degrades to substring matching.
        alt = "|".join(regex_escape(str(getattr(v, "value", v)))
                       for v in cond.values)
        op = "!~" if cond.negated else "=~"
        return "%s %s %s" % (field, op, q("^(?:%s)$" % alt))
    if isinstance(cond, NullCheck):
        if isinstance(cond.left, Attr):
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
    field = _field_for(attr, t)

    # error IS TRUE handled by parser as Cmp(error, '=', Lit(True))
    if field == "status":
        truthy = isinstance(c.right, Lit) and c.right.value in (True, "true")
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
        rhs = _value_text(c.right)

    if c.op in ("=", "!=", "<", "<=", ">", ">="):
        return "%s %s %s" % (field, c.op, rhs)
    # TraceQL regex matchers are UNANCHORED (unlike PromQL/NRQL) — anchor
    # explicitly to preserve whole-value matching semantics.
    if c.op in ("LIKE", "NOT LIKE"):
        pattern = str(getattr(c.right, "value", ""))
        body = "".join(
            ".*" if ch == "%" else ("." if ch == "_" else regex_escape(ch))
            for ch in pattern)
        rx = "(?i)^%s$" % body
        return "%s %s %s" % (field, "=~" if c.op == "LIKE" else "!~", q(rx))
    if c.op in ("RLIKE", "NOT RLIKE"):
        raw_rx = str(getattr(c.right, "value", rhs))
        return "%s %s %s" % (field, "=~" if c.op == "RLIKE" else "!~",
                             q("^(?:%s)$" % raw_rx))
    t.note("operator %r unsupported in TraceQL; dropped" % c.op, NEEDS_REVIEW)
    return ""


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
    t.notes.append("panel-hint:traces")
    t.note("trace search results differ from NR raw span listings; "
           "Tempo returns matching traces/spans within the time range")
    return t
