"""Query router: parse NRQL, pick a target datasource family, translate."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..nrql.parser import (
    Attr, Func, NrqlParseError, Star, TimeseriesSpec, parse_nrql,
)
from .common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW, UNTRANSLATABLE, Translation,
    Untranslatable, _VAR_RE, route_event_type,
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
        return translate_span_metrics_traceql(q, cfg)
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


# Widget kinds that plot over time: New Relic renders them only with a
# TIMESERIES query, so a query lacking the clause still means "over time".
_TIME_CHART_VIZ = {"viz.line", "viz.area", "viz.stacked-bar", "viz.sparkline",
                   "viz.scatter"}


def _imply_timeseries(q, widget_viz: str) -> bool:
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
    implied = _imply_timeseries(q, widget_viz)

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
