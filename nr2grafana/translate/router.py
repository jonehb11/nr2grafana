"""Query router: parse NRQL, pick a target datasource family, translate."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import copy

from ..nrql.parser import (
    Attr, BoolOp, Cmp, Func, Lit, NrqlParseError, NrqlQuery, Star,
    TimeseriesSpec, parse_nrql,
)
from .common import (
    fn_name,
    APPROXIMATE, EXACT, NEEDS_REVIEW, UNTRANSLATABLE, Translation,
    Untranslatable, _VAR_RE, route_event_type, worst,
)
from .logs import translate_to_logql
from .metrics import (
    nr_duration_seconds, nr_duration_to_grafana_range, translate_to_promql,
)
from .traces import translate_span_metrics_traceql, translate_to_traceql


def _interval_text(seconds: float) -> str:
    n = int(seconds) if seconds == int(seconds) else seconds
    if isinstance(n, int):
        for div, unit in ((86400, "d"), (3600, "h"), (60, "m")):
            if n % div == 0 and n >= div:
                return "%d%s" % (n // div, unit)
        return "%ds" % n
    return "%gs" % n


def _timing_notes(q, t) -> None:
    """SLIDE BY / fixed TIMESERIES buckets / ORDER BY / unlimited FACET —
    query-shape aspects Grafana handles differently; note them honestly."""
    ts = q.timeseries
    if ts is not None and ts.slide_by:
        t.note("SLIDE BY %s (sliding aggregation windows) has no "
               "equivalent; Grafana steps by the query interval, so the "
               "series will look more stepped than in NR" % ts.slide_by,
               APPROXIMATE)
    if ts is not None and ts.interval_var:
        t.notes.append("interval:$%s" % ts.interval_var)
        t.note("TIMESERIES {{%s}}: the panel's min interval is the variable; "
               "its value must be a Grafana interval such as 5m"
               % ts.interval_var, NEEDS_REVIEW)
    if ts is not None and ts.interval_seconds:
        t.notes.append("interval:%s" % _interval_text(ts.interval_seconds))
        t.notes.append(
            "TIMESERIES %s: the panel's min interval is set to %s so "
            "Grafana buckets the way New Relic did (a wider dashboard "
            "range still widens the step)"
            % (_interval_text(ts.interval_seconds),
               _interval_text(ts.interval_seconds)))
    if q.facet and t.group_by and not isinstance(q.limit, int):
        t.notes.append(
            "FACET without LIMIT: NR returns the top 10 groups by "
            "default, the translated query returns ALL groups — wrap in "
            "topk(10, ...) if the cardinality is high")
    if q.order_by is not None and t.group_by:
        t.notes.append(
            "ORDER BY is not preserved in the query; topk() sorts by "
            "value — use panel sorting for other orderings")


def _translate_span_aggregation(q, cfg: Dict[str, Any]) -> Translation:
    """Aggregated FROM Span: span metrics in Mimir by default; TraceQL
    metrics when configured (span_aggregations: "traceql") or when span
    metrics cannot express the aggregation (uniqueCount(trace.id))."""
    mode = str(cfg.get("span_aggregations") or "spanmetrics").lower()
    if q.from_ and q.from_[0].lower() == "distributedtracesummary":
        # One row per trace: only TraceQL metrics can restrict to root spans.
        return translate_span_metrics_traceql(q, cfg, root_only=True)
    aggs = [i.expr for i in q.select if isinstance(i.expr, Func)]
    first = aggs[0] if aggs else None
    trace_count = (first is not None
                   and first.name in ("uniquecount", "cardinality")
                   and first.args and isinstance(first.args[0], Attr)
                   and first.args[0].name.lower() in ("trace.id", "traceid",
                                                      "trace_id"))
    if mode == "traceql" or trace_count:
        try:
            return translate_span_metrics_traceql(q, cfg)
        except Untranslatable as e:
            if trace_count:
                raise
            t = translate_to_promql(q, cfg)  # fall back to span metrics
            t.note("TraceQL metrics cannot express this query (%s); "
                   "translated with span metrics in Mimir instead — the "
                   "panel needs span metrics to exist" % e, NEEDS_REVIEW)
            return t
    return translate_to_promql(q, cfg)  # span metrics in Mimir


def _rel_seconds(text: str) -> Optional[float]:
    key = (text or "").strip().lower()
    if key == "now":
        return 0.0
    return nr_duration_seconds(text)


def _short_duration(seconds: float) -> str:
    n = int(seconds) if seconds == int(seconds) else seconds
    if isinstance(n, int):
        for div, unit in ((86400, "d"), (3600, "h"), (60, "m")):
            if n % div == 0 and n >= div:
                return "%d%s" % (n // div, unit)
        return "%ds" % n
    return "%gs" % n


# SINCE <previous unit> UNTIL <this unit>: the whole previous calendar
# unit. Grafana: timeFrom pins the panel to "this unit so far", timeShift
# "1u/u" moves both ends back one unit and rounds them to its bounds.
_WHOLE_UNITS = {
    ("yesterday", "today"): ("now/d", "1d/d", "day"),
    ("last week", "this week"): ("now/w", "1w/w", "week"),
    ("last month", "this month"): ("now/M", "1M/M", "month"),
    ("last year", "this year"): ("now/y", "1y/y", "year"),
}
_VAR_AGO_RE = re.compile(
    r"^\{\{\{?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}?\}\}\s+"
    r"(second|minute|hour|day|week|month|year)s?\s+ago$", re.I)
_GRAFANA_UNIT = {"second": "s", "minute": "m", "hour": "h", "day": "d",
                 "week": "w", "month": "M", "year": "y"}


def _time_key(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _time_hints(q, t: Translation) -> None:
    """SINCE/UNTIL -> timefrom:/timeshift: hints for the builder.

    SINCE a UNTIL b (both relative) is exactly a Grafana panel with
    timeFrom = a - b and timeShift = b."""
    var = _VAR_RE.fullmatch((q.since or "").strip()) if q.since else None
    if var is not None:
        t.notes.append("timefrom:$%s" % var.group(1))
        t.note("SINCE {{%s}}: the panel's relative time comes from the "
               "variable; its value must be a Grafana span such as 1h or 7d "
               "(New Relic wrote e.g. '1 hour ago')" % var.group(1),
               NEEDS_REVIEW)
        return
    var_ago = _VAR_AGO_RE.match((q.since or "").strip()) if q.since else None
    if var_ago is not None and not q.until:
        unit = _GRAFANA_UNIT[var_ago.group(2).lower()]
        t.notes.append("timefrom:now-${%s}%s" % (var_ago.group(1), unit))
        t.note("SINCE %s: the panel's relative time is now-${%s}%s; the "
               "variable's value must be a whole number"
               % (q.since.strip(), var_ago.group(1), unit), NEEDS_REVIEW)
        return
    whole = _WHOLE_UNITS.get((_time_key(q.since), _time_key(q.until))) \
        if q.since and q.until else None
    if whole is not None:
        rng, shift, unit = whole
        t.notes.append("timefrom:%s" % rng)
        t.notes.append("timeshift:%s" % shift)
        t.note("SINCE %s UNTIL %s became a panel time override (timeFrom %s "
               "with timeShift %s: the whole previous %s)"
               % (q.since, q.until, rng, shift, unit))
        return
    since_s = _rel_seconds(q.since) if q.since else None
    until_s = _rel_seconds(q.until) if q.until else None
    if q.until and until_s is not None and until_s > 0:
        if since_s is not None and since_s > until_s:
            t.notes.append("timefrom:now-%s" % _short_duration(
                since_s - until_s))
            t.notes.append("timeshift:%s" % _short_duration(until_s))
            t.note("SINCE %s UNTIL %s became a panel time override "
                   "(timeFrom %s, timeShift %s)"
                   % (q.since, q.until, _short_duration(since_s - until_s),
                      _short_duration(until_s)))
            return
        t.note("UNTIL %r cannot be expressed per-panel in Grafana without "
               "a relative SINCE; adjust the dashboard time range manually"
               % q.until, NEEDS_REVIEW)
    elif q.until and until_s is None:
        t.note("UNTIL %r (absolute or non-relative) cannot be expressed "
               "per-panel in Grafana; adjust the dashboard time range "
               "manually" % q.until, NEEDS_REVIEW)
    if q.since:
        rng = nr_duration_to_grafana_range(q.since)
        if rng:
            t.notes.append("timefrom:%s" % rng)
        else:
            t.note("SINCE %r could not be mapped to a Grafana range; "
                   "dashboard default range applies" % q.since, NEEDS_REVIEW)


# Nested queries: SELECT <outer agg>(alias) FROM (SELECT <inner agg> AS
# alias FROM X ... FACET a). The inner query is one PromQL/LogQL vector per
# facet value; the outer aggregation folds it.
_NESTED_AGGS = {"average": "avg", "avg": "avg", "sum": "sum", "max": "max",
                "min": "min", "count": "count", "stddev": "stddev",
                "median": "quantile", "percentile": "quantile"}


def _nested_where_filters(cond: Any, aliases: List[str], t: Translation) \
        -> List[str]:
    """Outer WHERE on the inner aliases (WHERE c > 100) -> PromQL
    comparison filters on the inner vector."""
    if cond is None:
        return []
    if isinstance(cond, BoolOp) and cond.op.lower() == "and":
        out: List[str] = []
        for item in cond.items:
            out.extend(_nested_where_filters(item, aliases, t))
        return out
    if isinstance(cond, Cmp) and isinstance(cond.left, Attr) \
            and cond.left.name in aliases and isinstance(cond.right, Lit) \
            and isinstance(cond.right.value, (int, float)) \
            and not isinstance(cond.right.value, bool) \
            and cond.op in ("<", "<=", ">", ">=", "=", "!="):
        return ["%s %s" % (cond.op, ("%g" % cond.right.value))]
    t.note("the outer WHERE of the nested query (%s) is neither a numeric "
           "comparison on an inner alias nor an AND of them; dropped"
           % cond_text_safe(cond), NEEDS_REVIEW)
    return []


def cond_text_safe(cond: Any) -> str:
    from .common import cond_text
    try:
        return cond_text(cond)
    except Exception:  # pragma: no cover - defensive
        return repr(cond)


def _translate_nested(q: NrqlQuery, cfg: Dict[str, Any],
                      widget_viz: str) -> Translation:
    inner = q.subquery
    assert inner is not None
    fail = Translation(confidence=UNTRANSLATABLE)
    if inner.subquery is not None:
        fail.notes.append("a nested query inside a nested query has no "
                          "PromQL/LogQL equivalent")
        return fail
    if not any(isinstance(i.expr, Func) for i in inner.select):
        fail.notes.append("the inner query of a nested query must aggregate "
                          "(SELECT agg(...) AS alias ... FACET attr)")
        return fail
    if not inner.facet:
        fail.notes.append("a nested query only makes sense over a FACET "
                          "in the inner query (one value per group to "
                          "aggregate again); this inner query has no FACET")
        return fail
    outer_items = [i for i in q.select if isinstance(i.expr, Func)]
    if not outer_items or len(outer_items) != len(q.select):
        fail.notes.append("the outer SELECT of a nested query must be "
                          "aggregations over the inner aliases")
        return fail
    implied = _imply_timeseries(q, widget_viz)
    # The outer time clauses win; the inner query runs on the outer's
    # TIMESERIES (New Relic requires them to agree).
    inner = copy.deepcopy(inner)
    inner.timeseries = copy.deepcopy(q.timeseries)
    inner.since = q.since or inner.since
    inner.until = q.until or inner.until
    inner.compare_with = None
    inner.limit = None
    aliases = [i.alias for i in inner.select if i.alias]
    inner_attrs = [f.expr.name if isinstance(f.expr, Attr) else None
                   for f in inner.facet]
    outer_attrs = [f.expr.name if isinstance(f.expr, Attr) else None
                   for f in q.facet]
    for a in outer_attrs:
        if a is None or a not in inner_attrs:
            fail.notes.append("the outer FACET of a nested query must be "
                              "one of the inner FACET attributes (%s)"
                              % ", ".join(str(x) for x in inner_attrs))
            return fail
    probe = Translation()
    filters = _nested_where_filters(q.where, aliases, probe)
    from .common import legend_for, select_label

    class _Fail(Exception):
        pass

    def fold(fn: Func, t: Translation) -> Tuple[str, List[str], str]:
        """PromQL/LogQL for one outer aggregation over the inner vector:
        (expr, group labels, unit note or '')."""
        if fn.name in ("_ratio", "_arith") and len(fn.args) >= 2:
            # sum(errors) / sum(total) over the inner per-host counts.
            operands = fn.args[1:] if fn.name == "_arith" else fn.args
            op = str(getattr(fn.args[0], "value", "/")) \
                if fn.name == "_arith" else "/"
            sides = []
            for side in operands:
                if not isinstance(side, Func):
                    raise _Fail("arithmetic over a nested query must "
                                "combine aggregations of the inner aliases")
                sides.append(fold(side, t))
            expr = "(%s) %s (%s)" % (sides[0][0], op, sides[1][0])
            unit = ""
            if op == "/" and all(s[2] in ("unit:short", "") for s in sides):
                unit = "unit:percentunit"
            return expr, sides[0][1], unit
        word = _NESTED_AGGS.get(fn.name)
        if word is None:
            raise _Fail("%s() over a nested query has no PromQL "
                        "aggregation; only average/sum/max/min/count/"
                        "stddev/percentile/median fold the inner series"
                        % fn_name(fn.name))
        arg = fn.args[0] if fn.args else None
        if fn.name == "count" and (arg is None or isinstance(arg, Star)):
            target = inner.select[0]
        elif isinstance(arg, Attr) and arg.name in aliases:
            target = next(i for i in inner.select if i.alias == arg.name)
        elif isinstance(arg, Attr) and len(inner.select) == 1 \
                and inner.select[0].alias is None:
            target = inner.select[0]
        else:
            raise _Fail("%s refers to %r, which is not an alias of the "
                        "inner SELECT (%s)"
                        % (fn_name(fn.name), getattr(arg, "name", arg),
                           ", ".join(aliases) or "no aliases"))
        sub = copy.deepcopy(inner)
        sub.select = [copy.deepcopy(target)]
        t_in = translate_parsed(sub, cfg, "")
        if t_in.confidence == UNTRANSLATABLE:
            raise _Fail("; ".join(t_in.notes) or "inner query untranslatable")
        if t_in.datasource == "tempo":
            raise _Fail("a nested query over span searches has no TraceQL "
                        "equivalent; aggregate spans with span metrics "
                        "instead")
        if word == "quantile":
            if t_in.datasource == "loki":
                raise _Fail("percentile() over a nested LogQL query: LogQL "
                            "has no quantile vector aggregation")
            pct = 50.0
            if fn.name == "percentile":
                nums = [a.value for a in fn.args[1:]
                        if isinstance(a, Lit)
                        and isinstance(a.value, (int, float))]
                pct = float(nums[0]) if nums else 95.0
            head = "quantile(%s, " % ("%g" % (pct / 100.0))
        else:
            head = word + "("
        by_labels = [t_in.group_by[inner_attrs.index(a)]
                     for a in outer_attrs
                     if inner_attrs.index(a) < len(t_in.group_by)]
        if by_labels:
            head = head.replace("(", " by (%s)(" % ", ".join(by_labels), 1)
        body = t_in.expr
        for f in filters:
            body = "(%s) %s" % (body, f)
        t.datasource = t_in.datasource
        t.query_type = t_in.query_type
        t.confidence = worst(t.confidence, t_in.confidence)
        unit = ""
        for n in t_in.notes:
            if n.startswith("unit:"):
                unit = n
            elif not n.startswith(("timefrom:", "timeshift:", "interval:")):
                t.note(n)
        if word == "count":
            unit = "unit:short"
        t.note("nested query: %s over the inner per-%s %s → %s of the inner "
               "vector%s" % (select_label(fn), "/".join(
                   str(a) for a in inner_attrs), select_label(target.expr),
                   word, (" filtered by %s" % ", ".join(filters)) if filters
                   else ""), APPROXIMATE)
        return "%s%s)" % (head, body), by_labels, unit

    primary: Optional[Translation] = None
    for item in outer_items:
        fn = item.expr
        assert isinstance(fn, Func)
        t = Translation(confidence=APPROXIMATE)
        try:
            expr, by_labels, unit = fold(fn, t)
        except _Fail as e:
            fail.notes.append(str(e))
            return fail
        if q.facet and isinstance(q.limit, int):
            expr = "topk(%d, %s)" % (q.limit, expr)
        tag = item.alias or select_label(fn)
        legend = legend_for(by_labels)
        t.expr = expr
        t.legend = (legend + " " + tag).strip() if by_labels else tag
        t.group_by = by_labels
        if unit:
            t.notes.append(unit)
        for n in probe.notes:
            t.note(n, NEEDS_REVIEW)
        if primary is None:
            primary = t
        else:
            primary.extra.append(t)
    assert primary is not None
    if implied:
        primary.note("the NRQL has no TIMESERIES clause but a %s widget "
                     "plots over time; translated as a range query (one "
                     "point per Grafana interval) — add TIMESERIES in New "
                     "Relic to make this exact" % widget_viz, APPROXIMATE)
    _timing_notes(q, primary)
    _time_hints(q, primary)
    if q.compare_with:
        primary.note("COMPARE WITH on a nested query is not supported; the "
                     "comparison series was dropped", NEEDS_REVIEW)
    return primary


# Widget kinds that plot over time: New Relic renders them only with a
# TIMESERIES query, so a query lacking the clause still means "over time".
_TIME_CHART_VIZ = {"viz.line", "viz.area", "viz.stacked-bar", "viz.sparkline",
                   "viz.scatter"}


# Widgets that show one value per series: a TIMESERIES query behind them
# would put every bucket into the pie / table / bar.
_SNAPSHOT_VIZ = {"viz.table", "viz.pie", "viz.bar", "viz.bullet"}


def _imply_timeseries(q, widget_viz: str) -> bool:
    if widget_viz in _SNAPSHOT_VIZ and q.timeseries is not None:
        q.timeseries = None
        return False
    if widget_viz not in _TIME_CHART_VIZ or q.timeseries is not None:
        return False
    if not any(isinstance(i.expr, Func) for i in q.select):
        return False
    q.timeseries = TimeseriesSpec()
    return True


def translate_query(nrql_text: str, cfg: Dict[str, Any],
                    widget_viz: str = "") -> Translation:
    """Translate one NRQL string. Never raises: untranslatable/broken
    queries come back as Translation(confidence='untranslatable').

    ``widget_viz`` (the NR visualization id) lets a time chart imply
    TIMESERIES when the query omits it."""
    try:
        q = parse_nrql(nrql_text)
    except NrqlParseError as e:
        t = Translation(confidence=UNTRANSLATABLE)
        t.notes.append("NRQL could not be parsed: %s" % e)
        return t
    if q.subquery is not None:
        return _translate_nested(q, cfg, widget_viz)
    return translate_parsed(q, cfg, widget_viz)


def translate_parsed(q: NrqlQuery, cfg: Dict[str, Any],
                     widget_viz: str = "") -> Translation:
    """translate_query() for an already parsed query."""
    had_timeseries = q.timeseries is not None
    implied = _imply_timeseries(q, widget_viz)
    snapshot = had_timeseries and q.timeseries is None

    family = route_event_type(q.from_, cfg)
    try:
        if family == "logs":
            t = translate_to_logql(q, cfg)
        elif family == "traces":
            has_agg = any(isinstance(i.expr, Func) for i in q.select)
            if has_agg:
                t = _translate_span_aggregation(q, cfg)
            else:
                t = translate_to_traceql(q, cfg)
        else:
            t = translate_to_promql(q, cfg)
    except Untranslatable as e:
        t = Translation(confidence=UNTRANSLATABLE)
        t.notes.append(str(e))
        return t

    # Numeric WHERE predicates no translator could express.
    for p in t.numeric:
        t.note("numeric comparison %s %s %s cannot be expressed for this "
               "target; dropped — apply it manually"
               % (p.attr, p.op, ("%g" % p.value)), NEEDS_REVIEW)
    del t.numeric[:]

    if q.extras:
        t.note("NRQL fragment(s) not understood and DROPPED from the "
               "translation: %s — verify the query semantics"
               % "; ".join(repr(x) for x in q.extras), NEEDS_REVIEW)
    if t.confidence != UNTRANSLATABLE:
        _timing_notes(q, t)
        if implied:
            t.note("the NRQL has no TIMESERIES clause but a %s widget plots "
                   "over time; translated as a range query (one point per "
                   "Grafana interval) — add TIMESERIES in New Relic to make "
                   "this exact" % widget_viz, APPROXIMATE)
        if snapshot:
            t.note("TIMESERIES dropped: a %s widget shows one value per "
                   "series, so the query runs as an instant query over the "
                   "range" % widget_viz, APPROXIMATE)
    if len(q.from_) > 1:
        t.note("query selects FROM multiple event types (%s); only %r was "
               "translated" % (", ".join(q.from_), q.from_[0]), NEEDS_REVIEW)

    # Time-range hints for the builder.
    _time_hints(q, t)
    if q.extrapolate:
        t.notes.append("EXTRAPOLATE dropped (not applicable to metric data)")
    if q.timezone:
        t.notes.append("WITH TIMEZONE %s dropped; set the dashboard "
                       "timezone instead" % q.timezone)
    return t
