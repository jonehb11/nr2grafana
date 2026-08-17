"""NRQL (FROM Log) -> LogQL translation.

Strategy per the migration spec:
- WHERE predicates split three ways: Loki stream-selector labels (per
  config loki_stream_labels), line filters (predicates on `message`), and
  pipeline label filters (everything else, behind an optional parser stage).
- SELECT */plain attributes -> log stream query for a Grafana logs panel.
- Aggregations -> LogQL metric queries (count_over_time, rate, unwrap ...).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..nrql.parser import Attr, Func, Lit, NrqlQuery, SelectItem, Star
from .common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW, Matcher, Translation, Untranslatable,
    cond_to_matchers, facet_labels, legend_for, map_attr, regex_escape, q,
    worst,
)


def _split_matchers(matchers: List[Matcher], cfg: Dict[str, Any],
                    t: Translation) -> Tuple[List[Matcher], List[str],
                                             List[Matcher], List[Matcher]]:
    """-> (stream_selector, line_filters, metadata_filters, parsed_filters)

    Metadata filters target Loki structured-metadata labels (config
    loki_metadata_labels) and are queryable without a parser stage; parsed
    filters need `| json`/`| logfmt` first.
    """
    stream_labels = set(cfg.get("loki_stream_labels") or [])
    meta_labels = set(cfg.get("loki_metadata_labels") or [])
    stream: List[Matcher] = []
    lines: List[str] = []
    meta: List[Matcher] = []
    parsed: List[Matcher] = []
    for m in matchers:
        if m.label == "message":
            lines.append(_line_filter(m, t))
        elif m.label in stream_labels:
            stream.append(m)
        elif m.label in meta_labels:
            meta.append(m)
        else:
            parsed.append(m)
    return stream, lines, meta, parsed


def _line_filter(m: Matcher, t: Translation) -> str:
    if m.op == "=":
        t.note("equality on message became a substring line filter "
               "(|= %s)" % q(m.value), APPROXIMATE)
        return "|= %s" % q(m.value)
    if m.op == "!=":
        t.note("inequality on message became a substring exclusion filter "
               "(!= %s)" % q(m.value), APPROXIMATE)
        return "!= %s" % q(m.value)
    # LIKE-derived matchers already carry (?i); RLIKE passes through as-is
    # (NRQL RLIKE is case-sensitive, like RE2).
    if m.op == "=~":
        return "|~ %s" % q(m.value)
    return "!~ %s" % q(m.value)


def _selector(stream: List[Matcher], t: Translation) -> str:
    if not stream:
        t.note("no stream-label filter found in WHERE; emitted "
               '{service_name=~".+"} which scans all streams — add a label '
               "filter", NEEDS_REVIEW)
        return '{service_name=~".+"}'
    return "{%s}" % ", ".join(m.render() for m in stream)


def _pipeline(meta: List[Matcher], parsed: List[Matcher],
              cfg: Dict[str, Any], t: Translation,
              need_parser: bool, error_guard: bool = True) -> str:
    parts: List[str] = []
    parser = cfg.get("loki_parser", "json")
    # Structured-metadata filters work without a parser stage; keep them
    # before it so they prune lines early.
    for m in meta:
        parts.append("| %s%s%s" % (m.label, m.op, q(m.value)))
    used_parser = False
    if (parsed or need_parser) and parser:
        parts.append("| %s" % parser)
        used_parser = True
    for m in parsed:
        parts.append("| %s%s%s" % (m.label, m.op, q(m.value)))
        t.note("filter on %r assumes it is a parsed %s field in Loki"
               % (m.label, parser or "json"), NEEDS_REVIEW)
    if used_parser and error_guard:
        parts.append('| __error__=""')
    return " ".join(parts)


def translate_to_logql(nq: NrqlQuery, cfg: Dict[str, Any]) -> Translation:
    t = Translation(datasource="loki")
    is_range = nq.timeseries is not None
    window = "$__auto" if is_range else "$__range"

    matchers = cond_to_matchers(nq.where, cfg, t)
    stream, lines, meta, parsed = _split_matchers(matchers, cfg, t)
    sel = _selector(stream, t)
    line_part = (" " + " ".join(lines)) if lines else ""

    items = [i for i in nq.select]
    aggs = [i for i in items if isinstance(i.expr, Func)]

    # --- plain log stream (logs panel) ---
    if not aggs:
        pipe = _pipeline(meta, parsed, cfg, t, need_parser=False)
        t.expr = (sel + line_part + ((" " + pipe) if pipe else "")).strip()
        t.query_type = "range"
        t.notes.append("panel-hint:logs")
        if isinstance(nq.limit, int):
            t.notes.append("maxlines:%d" % nq.limit)
        named = [i.expr.name for i in items if isinstance(i.expr, Attr)]
        if named:
            t.note("column projection (%s) is not supported by Loki; the "
                   "full log line is shown" % ", ".join(named), APPROXIMATE)
        return t

    # --- metric queries ---
    by = facet_labels(nq, cfg, t)
    stream_labels = set(cfg.get("loki_stream_labels") or [])
    facet_needs_parser = any(l not in stream_labels for l in by)
    if facet_needs_parser:
        t.note("FACET on non-stream-label attribute(s) requires the parser "
               "stage; verify field names after parsing", NEEDS_REVIEW)
    by_clause = " by (%s)" % ", ".join(by) if by else ""

    def base_stream(extra_pipe: str = "", need_parser: bool = False) -> str:
        pipe = _pipeline(meta, parsed, cfg, t,
                         need_parser=need_parser or facet_needs_parser)
        s = sel + line_part
        if pipe:
            s += " " + pipe
        if extra_pipe:
            s += " " + extra_pipe
        return s

    fn = aggs[0].expr
    assert isinstance(fn, Func)
    alias = aggs[0].alias
    name = fn.name

    def finish(expr: str, qtype: Optional[str] = None) -> Translation:
        if nq.facet and isinstance(nq.limit, int):
            expr = "topk(%d, %s)" % (nq.limit, expr)
        t.expr = expr
        t.query_type = qtype or ("range" if is_range else "instant")
        t.legend = legend_for(by, alias)
        t.group_by = by
        if len(aggs) > 1:
            t.note("multiple aggregations in one log query; only the first "
                   "was translated — split the others into separate panels",
                   NEEDS_REVIEW)
        if nq.compare_with:
            t.note("COMPARE WITH has no LogQL equivalent; comparison series "
                   "dropped", NEEDS_REVIEW)
        return t

    if name == "count":
        return finish("sum%s(count_over_time(%s [%s]))"
                      % (by_clause, base_stream(), window))

    if name == "rate":
        per_seconds = 60.0
        for a in fn.args:
            if isinstance(a, Lit) and isinstance(a.value, (int, float)):
                per_seconds = float(a.value)
        if per_seconds == 1:
            mult = ""
        elif per_seconds == int(per_seconds):
            mult = " * %d" % int(per_seconds)
        else:
            mult = " * %s" % per_seconds
        return finish("sum%s(rate(%s [%s]))%s"
                      % (by_clause, base_stream(), window, mult))

    def unwrap_attr() -> str:
        arg = fn.args[0] if fn.args else None
        if not isinstance(arg, Attr):
            raise Untranslatable("%s() on logs needs a numeric attribute"
                                 % name)
        field = arg.name.replace(".", "_")
        t.note("unwrap of %r assumes it is a numeric field after parsing"
               % field, APPROXIMATE)
        return field

    def unwrap_stream(field: str) -> str:
        pipe = _pipeline(meta, parsed, cfg, t, need_parser=True,
                         error_guard=False)
        s = sel + line_part
        if pipe:
            s += " " + pipe
        return '%s | unwrap %s | __error__=""' % (s, field)

    # Unwrapped range aggregations take by()-grouping directly, which
    # aggregates over all samples across streams in the group — matching
    # NR's event-level semantics. Without it, a bare avg_over_time returns
    # one series PER STREAM instead of NR's single series.
    group = " by (%s)" % ", ".join(by) if by else " by ()"

    if name in ("average", "sum", "max", "min"):
        over = {"average": "avg_over_time", "sum": "sum_over_time",
                "max": "max_over_time", "min": "min_over_time"}[name]
        field = unwrap_attr()
        return finish("%s(%s [%s])%s"
                      % (over, unwrap_stream(field), window, group))

    if name in ("percentile", "median"):
        pcts = [50.0] if name == "median" else [
            float(a.value) for a in fn.args[1:]
            if isinstance(a, Lit) and isinstance(a.value, (int, float))
        ] or [95.0]
        field = unwrap_attr()
        exprs = ["quantile_over_time(%s, %s [%s])%s"
                 % (_fq(p), unwrap_stream(field), window, group)
                 for p in pcts]
        for p, e in list(zip(pcts, exprs))[1:]:
            t.extra.append(Translation(
                expr=e, datasource="loki",
                query_type="range" if is_range else "instant",
                legend=(legend_for(by) + " p%g" % p).strip(),
                confidence=APPROXIMATE, group_by=by))
        out = finish(exprs[0])
        out.legend = (legend_for(by, alias) + " p%g" % pcts[0]).strip()
        return out

    if name in ("uniquecount",):
        arg = fn.args[0] if fn.args else None
        if not isinstance(arg, Attr):
            raise Untranslatable("uniqueCount() needs an attribute")
        label, _ = map_attr(arg.name, cfg)
        t.note("uniqueCount over logs can be expensive in Loki (series per "
               "value)", NEEDS_REVIEW)
        return finish(
            "count(sum by (%s)(count_over_time(%s [%s])))"
            % (label, base_stream(need_parser=True), window))

    if name == "latest":
        field = unwrap_attr()
        return finish("last_over_time(%s [%s])%s"
                      % (unwrap_stream(field), window, group))

    if name == "filter":
        inner = fn.args[0] if fn.args else None
        if isinstance(inner, Func) and inner.name == "count":
            extra = cond_to_matchers(fn.where, cfg, t)
            s2, l2, m2, p2 = _split_matchers(extra, cfg, t)
            sel2 = _selector(stream + s2, t)
            line2 = (" " + " ".join(lines + l2)) if (lines or l2) else ""
            pipe2 = _pipeline(meta + m2, parsed + p2, cfg, t,
                              facet_needs_parser)
            base = sel2 + line2 + ((" " + pipe2) if pipe2 else "")
            return finish("sum%s(count_over_time(%s [%s]))"
                          % (by_clause, base, window))
        raise Untranslatable("filter() on logs supports only count(*)")

    if name == "percentage":
        inner = fn.args[0] if fn.args else None
        if isinstance(inner, Func) and inner.name == "count":
            extra = cond_to_matchers(fn.where, cfg, t)
            s2, l2, m2, p2 = _split_matchers(extra, cfg, t)
            sel2 = _selector(stream + s2, t)
            line2 = (" " + " ".join(lines + l2)) if (lines or l2) else ""
            pipe2 = _pipeline(meta + m2, parsed + p2, cfg, t, False)
            num_base = sel2 + line2 + ((" " + pipe2) if pipe2 else "")
            t.notes.append("unit:percent")
            return finish(
                "100 * sum%s(count_over_time(%s [%s])) / "
                "sum%s(count_over_time(%s [%s]))"
                % (by_clause, num_base, window, by_clause, base_stream(),
                   window))
        raise Untranslatable("percentage() on logs supports only count(*)")

    raise Untranslatable("aggregation %s() is not supported for FROM Log"
                         % name)


def _fq(p: float) -> str:
    s = ("%f" % (p / 100.0)).rstrip("0").rstrip(".")
    return s or "0"
