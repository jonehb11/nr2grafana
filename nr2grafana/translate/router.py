"""Query router: parse NRQL, pick a target datasource family, translate."""

from __future__ import annotations

from typing import Any, Dict

from ..nrql.parser import Func, NrqlParseError, Star, parse_nrql
from .common import (
    NEEDS_REVIEW, UNTRANSLATABLE, Translation, Untranslatable,
    route_event_type,
)
from .logs import translate_to_logql
from .metrics import (
    nr_duration_to_grafana_range, translate_to_promql,
)
from .traces import translate_to_traceql


def translate_query(nrql_text: str, cfg: Dict[str, Any]) -> Translation:
    """Translate one NRQL string. Never raises: untranslatable/broken
    queries come back as Translation(confidence='untranslatable')."""
    try:
        q = parse_nrql(nrql_text)
    except NrqlParseError as e:
        t = Translation(confidence=UNTRANSLATABLE)
        t.notes.append("NRQL could not be parsed: %s" % e)
        return t

    family = route_event_type(q.from_)
    try:
        if family == "logs":
            t = translate_to_logql(q, cfg)
        elif family == "traces":
            has_agg = any(isinstance(i.expr, Func) for i in q.select)
            if has_agg:
                t = translate_to_promql(q, cfg)  # span metrics in Mimir
            else:
                t = translate_to_traceql(q, cfg)
        else:
            t = translate_to_promql(q, cfg)
    except Untranslatable as e:
        t = Translation(confidence=UNTRANSLATABLE)
        t.notes.append(str(e))
        return t

    if q.extras:
        t.note("NRQL fragment(s) not understood and DROPPED from the "
               "translation: %s — verify the query semantics"
               % "; ".join(repr(x) for x in q.extras), NEEDS_REVIEW)
    if len(q.from_) > 1:
        t.note("query selects FROM multiple event types (%s); only %r was "
               "translated" % (", ".join(q.from_), q.from_[0]), NEEDS_REVIEW)

    # Time-range hints for the builder.
    if q.since:
        rng = nr_duration_to_grafana_range(q.since)
        if rng:
            t.notes.append("timefrom:%s" % rng)
        else:
            t.note("SINCE %r could not be mapped to a Grafana range; "
                   "dashboard default range applies" % q.since, NEEDS_REVIEW)
    if q.until:
        t.note("UNTIL %r cannot be expressed per-panel in Grafana; "
               "adjust the dashboard time range manually" % q.until,
               NEEDS_REVIEW)
    if q.extrapolate:
        t.notes.append("EXTRAPOLATE dropped (not applicable to metric data)")
    if q.timezone:
        t.notes.append("WITH TIMEZONE %s dropped; set the dashboard "
                       "timezone instead" % q.timezone)
    return t
