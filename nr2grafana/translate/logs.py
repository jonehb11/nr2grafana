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

from ..nrql.parser import Attr, BoolOp, Func, Lit, NrqlQuery, SelectItem, Star
from .common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW, Matcher, Translation, Untranslatable,
    cond_to_matchers, facet_labels, legend_for, map_attr, regex_escape, q,
    worst,
)
from .metrics import nr_duration_to_prom


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


def _logql_filter(nq: NrqlQuery, cfg: Dict[str, Any],
                  item: SelectItem, n_aggs: int) -> Translation:
    """filter(agg, WHERE cond): merge the embedded WHERE into the query's
    filters and re-translate. Merging BEFORE the selector is built lets
    the embedded predicate contribute stream-selector labels."""
    fn = item.expr
    assert isinstance(fn, Func)
    inner = fn.args[0] if fn.args else None
    if not isinstance(inner, Func) or inner.name in ("filter", "percentage"):
        raise Untranslatable(
            "filter() on logs needs a plain inner aggregation")
    if fn.where is None:
        combined = nq.where
    elif nq.where is None:
        combined = fn.where
    else:
        combined = BoolOp("and", [nq.where, fn.where])
    sub = NrqlQuery(
        raw=nq.raw, select=[SelectItem(expr=inner, alias=item.alias)],
        from_=nq.from_, where=combined, facet=nq.facet,
        facet_limit=nq.facet_limit, timeseries=nq.timeseries,
        since=nq.since, until=nq.until, compare_with=nq.compare_with,
        limit=nq.limit)
    out = translate_to_logql(sub, cfg)
    out.note("filter(...) merged its embedded WHERE into the log query "
             "filters")
    if n_aggs > 1:
        out.note("multiple aggregations in one log query; only the first "
                 "was translated — split the others into separate panels",
                 NEEDS_REVIEW)
    return out


def _rewrite_if_agg(item: SelectItem) -> Optional[SelectItem]:
    """agg(if(cond, x[, else])) -> filter(agg', WHERE cond) when the if()
    is trivially a filter (NRQL aggregations skip NULL); raise
    Untranslatable with a precise reason otherwise."""
    fn = item.expr
    if not (isinstance(fn, Func) and fn.args
            and isinstance(fn.args[0], Func) and fn.args[0].name == "if"):
        return None
    branch = fn.args[0]
    if branch.where is None:
        raise Untranslatable(
            "the if() condition could not be parsed as a predicate; "
            "rewrite the query as filter(%s(...), WHERE ...)" % fn.name)
    vals = branch.args  # then [, else] — the condition is branch.where
    then = vals[0] if vals else None
    els = vals[1] if len(vals) > 1 else None

    def is_num(v, *nums):
        return isinstance(v, Lit) and isinstance(v.value, (int, float)) \
            and not isinstance(v.value, bool) and float(v.value) in nums

    zero_else = els is None or is_num(els, 0)
    if fn.name == "count" and els is None:
        new = Func("count", args=[Star()])
    elif fn.name == "sum" and is_num(then, 1) and zero_else:
        new = Func("count", args=[Star()])
    elif zero_else and then is not None and (els is None or fn.name == "sum"):
        new = Func(fn.name, args=[then] + list(fn.args[1:]))
    else:
        raise Untranslatable(
            "%s(if(cond, x, y)): the ELSE value enters the aggregation "
            "for every non-matching row, which has no LogQL equivalent — "
            "split into separate filtered queries" % fn.name)
    return SelectItem(expr=Func("filter", args=[new], where=branch.where),
                      alias=item.alias)


def translate_to_logql(nq: NrqlQuery, cfg: Dict[str, Any]) -> Translation:
    t = Translation(datasource="loki")
    is_range = nq.timeseries is not None
    window = "$__auto" if is_range else "$__range"

    first_agg = next((i for i in nq.select if isinstance(i.expr, Func)),
                     None)
    if first_agg is not None:
        n_aggs = sum(1 for i in nq.select if isinstance(i.expr, Func))
        rewritten = _rewrite_if_agg(first_agg)
        if rewritten is not None:
            out = _logql_filter(nq, cfg, rewritten, n_aggs)
            out.note("if(...) translated as a filtered aggregation (the "
                     "condition became log filters)", APPROXIMATE)
            return out
        if first_agg.expr.name == "filter":
            return _logql_filter(nq, cfg, first_agg, n_aggs)

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
        if nq.compare_with:
            t.note("COMPARE WITH has no equivalent for a logs panel; "
                   "comparison dropped", NEEDS_REVIEW)
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
            off = nr_duration_to_prom(nq.compare_with)
            token = "[%s]" % window
            if off and token in expr:
                if "month" in nq.compare_with.lower():
                    t.note("COMPARE WITH month approximated as 30 days",
                           APPROXIMATE)
                shifted = expr.replace(token,
                                       "[%s] offset %s" % (window, off))
                t.extra.append(Translation(
                    expr=shifted, datasource="loki",
                    query_type=t.query_type,
                    legend=((t.legend + " ") if t.legend else "")
                    + "(%s earlier)" % off,
                    confidence=t.confidence, group_by=list(by)))
                t.note("COMPARE WITH %r became an 'offset %s' comparison "
                       "target (LogQL range offsets need Loki 2.3+); verify"
                       % (nq.compare_with, off), NEEDS_REVIEW)
            else:
                t.note("COMPARE WITH %r could not become a LogQL offset; "
                       "comparison series dropped" % nq.compare_with,
                       NEEDS_REVIEW)
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

    if name in ("latest", "earliest"):
        over = "last_over_time" if name == "latest" else "first_over_time"
        field = unwrap_attr()
        return finish("%s(%s [%s])%s"
                      % (over, unwrap_stream(field), window, group))

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
