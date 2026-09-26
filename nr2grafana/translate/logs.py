"""NRQL (FROM Log) -> LogQL translation.

Strategy per the migration spec:
- WHERE predicates split three ways: Loki stream-selector labels (per
  config loki_stream_labels), line filters (predicates on `message`), and
  pipeline label filters (everything else, behind an optional parser stage).
- SELECT */plain attributes -> log stream query for a Grafana logs panel.
- Aggregations -> LogQL metric queries (count_over_time, rate, unwrap ...).
- Numeric comparisons (duration_ms > 500) become numeric label filters
  after the parser stage.
- An OR across different attributes becomes a pipeline ``or`` expression
  behind the stream labels the branches share.
- Several aggregations in one SELECT become one target each; agg/agg and
  other arithmetic between aggregations is preserved as LogQL arithmetic.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from dataclasses import replace as _dc_replace

from ..nrql.parser import Attr, BoolOp, Func, Lit, NrqlQuery, SelectItem, Star
from .common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW, Matcher, NumericPred, Translation,
    Untranslatable, cond_text, cond_to_branches, event_map_entry, expr_text,
    facet_labels, hoist_rate_filter, legend_for, map_attr, regex_escape, q,
    unwrap_attr, worst,
)
from .metrics import (
    _extract_facet_cases, _scaled_unit, note_cases_other, nr_duration_to_prom,
)

_NUMERIC_OPS = ("<", "<=", ">", ">=")


def _fmt_num(n: float) -> str:
    return str(int(n)) if n == int(n) else repr(float(n))


def _scale_text(mult: float) -> str:
    inv = 1.0 / mult if mult else 0.0
    if mult and abs(inv) >= 2 and abs(inv - round(inv)) < 1e-9 * abs(inv):
        return "/ %d" % int(round(inv))
    return "* %s" % _fmt_num(mult)


_LOG_MATH = {"round", "abs", "ceil", "floor", "sqrt", "exp", "ln", "log",
             "log10", "log2", "clamp_max", "clamp_min", "pow", "mod"}
_LEVEL_LABELS = {"level", "severity", "severity_text", "severitytext",
                 "detected_level", "log_level", "loglevel"}


def _ci_levels(matchers: List[Matcher], t: Translation) -> List[Matcher]:
    """Log-level comparisons match case-insensitively: New Relic stores
    the level as the agent sent it (ERROR / Error / error) while Loki's
    level / detected_level labels are normally lowercase."""
    out: List[Matcher] = []
    for m in matchers:
        if m.label.lower() in _LEVEL_LABELS and m.value \
                and not m.value.startswith("(?i)"):
            if m.op in ("=", "!="):
                out.append(Matcher(m.label, "=~" if m.op == "=" else "!~",
                                   "(?i)" + regex_escape(m.value)))
            elif m.op in ("=~", "!~"):
                out.append(Matcher(m.label, m.op, "(?i)" + m.value))
            else:
                out.append(m)
                continue
            t.notes.append("%s compared case-insensitively (Loki levels are "
                           "usually lowercase; New Relic had %r)"
                           % (m.label, m.value))
        else:
            out.append(m)
    return out


def _render_pipe(m: Matcher) -> str:
    if m.op in _NUMERIC_OPS:
        return "%s %s %s" % (m.label, m.op, m.value)
    return "%s%s%s" % (m.label, m.op, q(m.value))


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
        if m.label == "timestamp":
            t.note("WHERE on timestamp dropped; the dashboard time range "
                   "selects the period", APPROXIMATE)
            continue
        if m.op in _NUMERIC_OPS:
            parsed.append(m)
        elif m.label == "message":
            lines.append(_line_filter(m, t))
        elif m.label in stream_labels:
            stream.append(m)
        elif m.label in meta_labels:
            meta.append(m)
        else:
            parsed.append(m)
    return stream, lines, meta, parsed


def _line_filter(m: Matcher, t: Translation) -> str:
    if m.value == "" and m.op in ("=", "!="):
        # message != '' / IS NOT NULL: every line has a message. (A `!= ""`
        # line filter would exclude EVERY line, since all contain "".)
        t.note("message %s '' is always %s for log lines; filter dropped"
               % (m.op, "false" if m.op == "=" else "true"), APPROXIMATE)
        return ""
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
              need_parser: bool, error_guard: bool = True,
              or_groups: Optional[List[List[Matcher]]] = None) -> str:
    parts: List[str] = []
    parser = cfg.get("loki_parser", "json")
    # Structured-metadata filters work without a parser stage; keep them
    # before it so they prune lines early.
    for m in meta:
        parts.append("| %s" % _render_pipe(m))
    used_parser = False
    if (parsed or need_parser or or_groups) and parser:
        parts.append("| %s" % parser)
        used_parser = True
    for m in parsed:
        parts.append("| %s" % _render_pipe(m))
        if m.op in _NUMERIC_OPS:
            t.note("numeric filter on %r assumes it is a parsed numeric %s "
                   "field in Loki" % (m.label, parser or "json"),
                   NEEDS_REVIEW)
        else:
            t.note("filter on %r assumes it is a parsed %s field in Loki"
                   % (m.label, parser or "json"), NEEDS_REVIEW)
    if or_groups:
        alts = []
        for g in or_groups:
            inner = " and ".join(_render_pipe(m) for m in g)
            alts.append("(%s)" % inner if len(g) > 1 else inner)
        parts.append("| %s" % " or ".join(alts))
    if used_parser and error_guard:
        parts.append('| __error__=""')
    return " ".join(parts)


class _Split:
    """WHERE analysis shared by every aggregation of a query."""

    def __init__(self, nq: NrqlQuery, cfg: Dict[str, Any], t: Translation,
                 extra_stream: Optional[List[Matcher]] = None):
        branches = [_ci_levels(b, t)
                    for b in cond_to_branches(nq.where, cfg, t)]
        numeric = [Matcher(p.label, p.op, _fmt_num(p.value))
                   for p in t.numeric]
        del t.numeric[:]
        self.or_groups: List[List[Matcher]] = []
        if len(branches) == 1:
            matchers = branches[0] + numeric
        else:
            matchers, self.or_groups = _merge_branches(branches, cfg, t)
            matchers = matchers + numeric
        matchers = list(extra_stream or []) + matchers
        self.stream, self.lines, self.meta, self.parsed = \
            _split_matchers(matchers, cfg, t)
        self.sel = _selector(self.stream, t)
        self.lines = [l for l in self.lines if l]
        self.line_part = (" " + " ".join(self.lines)) if self.lines else ""


def _merge_branches(branches: List[List[Matcher]], cfg: Dict[str, Any],
                    t: Translation) -> Tuple[List[Matcher],
                                             List[List[Matcher]]]:
    """OR across attributes: keep the stream matchers every branch shares
    as the selector and express the rest as a pipeline ``or``."""
    stream_labels = set(cfg.get("loki_stream_labels") or [])
    keys = [set((m.label, m.op, m.value) for m in b) for b in branches]
    common = set.intersection(*keys) if keys else set()
    shared = [m for m in branches[0]
              if (m.label, m.op, m.value) in common]
    if any(m.label == "message" and (m.label, m.op, m.value) not in common
           for b in branches for m in b):
        t.note("an OR mixing message predicates with attribute filters "
               "cannot be expressed in one LogQL query; that OR clause was "
               "DROPPED (the filters every alternative shares were kept) — "
               "verify filter logic", NEEDS_REVIEW)
        return shared, []
    groups: List[List[Matcher]] = []
    for b in branches:
        rest = [m for m in b if (m.label, m.op, m.value) not in common]
        if rest:
            groups.append(rest)
    if not any(m.label in stream_labels for m in shared):
        t.note("the OR in WHERE spans different stream labels, so the "
               "query scans all streams and filters in the pipeline "
               "(stream labels are matched as parsed labels)", NEEDS_REVIEW)
    else:
        t.note("the OR in WHERE became a pipeline `or` filter behind the "
               "shared stream selector", APPROXIMATE)
    return shared, groups


def _logql_filter(nq: NrqlQuery, cfg: Dict[str, Any],
                  item: SelectItem, extra_stream: List[Matcher]) \
        -> Translation:
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
    sub = _sub_query(nq, SelectItem(expr=inner, alias=item.alias), combined)
    out = _translate_one(sub, cfg, extra_stream)
    out.note("filter(...) merged its embedded WHERE into the log query "
             "filters")
    return out


def _sub_query(nq: NrqlQuery, item: SelectItem, where: Any) -> NrqlQuery:
    return NrqlQuery(
        raw=nq.raw, select=[item], from_=nq.from_, where=where,
        facet=list(nq.facet), facet_limit=nq.facet_limit,
        timeseries=nq.timeseries, since=nq.since, until=nq.until,
        compare_with=nq.compare_with, limit=nq.limit)


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


def _event_stream_labels(nq: NrqlQuery, cfg: Dict[str, Any],
                         t: Translation) -> List[Matcher]:
    """Custom event types routed to Loki via config event_map carry their
    own stream labels."""
    if not nq.from_:
        return []
    entry = event_map_entry(nq.from_[0], cfg)
    if not entry or entry.get("family") != "logs":
        return []
    labels = entry.get("labels") or {}
    out = [Matcher(str(k), "=", str(v)) for k, v in labels.items()]
    t.note("FROM %s routed to Loki per config event_map (stream labels %s)"
           % (nq.from_[0], ", ".join(m.render() for m in out) or "none"),
           NEEDS_REVIEW)
    return out


def variable_scope(nq: NrqlQuery, cfg: Dict[str, Any]) -> str:
    """Stream selector scoping a Loki label_values() variable derived from
    a dashboard-variable NRQL, or '' when the WHERE names no stream label."""
    t = Translation(datasource="loki")
    try:
        extra_stream = _event_stream_labels(nq, cfg, t)
        sp = _Split(nq, cfg, t, extra_stream)
    except Untranslatable:
        return ""
    return sp.sel if sp.stream else ""


def translate_to_logql(nq: NrqlQuery, cfg: Dict[str, Any]) -> Translation:
    t0 = Translation(datasource="loki")
    extra_stream = _event_stream_labels(nq, cfg, t0)
    items = list(nq.select)
    aggs = [i for i in items if isinstance(i.expr, Func)]
    if not aggs:
        out = _translate_one(nq, cfg, extra_stream)
        out.notes[:0] = t0.notes
        out.confidence = worst(out.confidence, t0.confidence)
        return out
    case_specs, case_other = _extract_facet_cases(nq)
    if case_specs:
        note_cases_other(t0, case_other)
        return _translate_log_cases(nq, cfg, extra_stream, t0, aggs,
                                    case_specs)
    # One target per aggregation; the first is the primary.
    primary: Optional[Translation] = None
    failures: List[str] = []
    for item in aggs:
        try:
            sub = _translate_one(_sub_query(nq, item, nq.where), cfg,
                                 extra_stream)
        except Untranslatable as e:
            failures.append("%s: %s" % (expr_text(item.expr), e))
            continue
        if primary is None:
            primary = sub
        else:
            unit_notes = [n for n in sub.notes if n.startswith("unit:")]
            primary.confidence = worst(primary.confidence, sub.confidence)
            for n in sub.notes:
                if not n.startswith("unit:") and n not in primary.notes:
                    primary.notes.append(n)
            sub.notes = unit_notes
            extra_targets = sub.extra
            sub.extra = []
            primary.extra.append(sub)
            primary.extra.extend(extra_targets)
    if primary is None:
        raise Untranslatable("; ".join(failures))
    for f in failures:
        primary.note("SELECT item dropped (no LogQL equivalent) — %s" % f,
                     NEEDS_REVIEW)
    primary.notes[:0] = t0.notes
    primary.confidence = worst(primary.confidence, t0.confidence)
    return primary


def _translate_log_cases(nq: NrqlQuery, cfg: Dict[str, Any],
                         extra_stream: Optional[List[Matcher]],
                         t0: Translation, aggs: List[SelectItem],
                         case_specs: List[Tuple[Any, Optional[str]]]
                         ) -> Translation:
    """FACET cases(WHERE c1 AS a, ...) / if(...) on logs: one filtered
    query per case (each case is a WHERE)."""
    item = aggs[0]
    primary: Optional[Translation] = None
    for cond, alias in case_specs:
        where = cond if nq.where is None else BoolOp("and", [nq.where, cond])
        sub_q = _sub_query(nq, item, where)
        sub_q.facet = list(nq.facet)
        try:
            sub = _translate_one(sub_q, cfg, extra_stream)
        except Untranslatable as e:
            if primary is not None:
                primary.note("case %r could not be translated: %s"
                             % (alias or cond_text(cond), e), NEEDS_REVIEW)
                continue
            raise
        label = alias or cond_text(cond)
        sub.legend = (label + " " + sub.legend).strip() if sub.legend \
            else label
        if primary is None:
            primary = sub
        else:
            unit_notes = [n for n in sub.notes if n.startswith("unit:")]
            primary.confidence = worst(primary.confidence, sub.confidence)
            for n in sub.notes:
                if not n.startswith("unit:") and n not in primary.notes:
                    primary.notes.append(n)
            sub.notes = unit_notes
            sub.extra = []
            primary.extra.append(sub)
    assert primary is not None
    primary.note("FACET cases(...) became one filtered query per case; NR's "
                 "implicit 'Other' bucket is not emitted", APPROXIMATE)
    if len(aggs) > 1:
        primary.note("only the first SELECT item was translated with FACET "
                     "cases(...); add the others as separate panels",
                     NEEDS_REVIEW)
    primary.notes[:0] = t0.notes
    primary.confidence = worst(primary.confidence, t0.confidence)
    return primary


def _apply_multiplier(t: Translation, item: SelectItem) -> None:
    if not item.multiplier:
        return
    t.expr = "(%s) %s" % (t.expr, _scale_text(item.multiplier))
    unit = next((n.split(":", 1)[1] for n in t.notes
                 if n.startswith("unit:")), "")
    t.notes[:] = [n for n in t.notes if not n.startswith("unit:")]
    scaled = _scaled_unit(unit, item.multiplier) if unit else None
    if scaled:
        t.notes.append("unit:%s" % scaled)
    else:
        t.note("SELECT arithmetic '%s' preserved; set the panel unit "
               "manually" % _scale_text(item.multiplier), APPROXIMATE)
    for e in t.extra:
        e.expr = "(%s) %s" % (e.expr, _scale_text(item.multiplier))


def _translate_one(nq: NrqlQuery, cfg: Dict[str, Any],
                   extra_stream: Optional[List[Matcher]] = None) \
        -> Translation:
    """Translate a query with at most one aggregation."""
    t = Translation(datasource="loki")
    is_range = nq.timeseries is not None
    window = "$__auto" if is_range else "$__range"
    extra_stream = list(extra_stream or [])

    first_agg = next((i for i in nq.select if isinstance(i.expr, Func)),
                     None)
    if first_agg is not None:
        fn0 = first_agg.expr
        assert isinstance(fn0, Func)
        hoisted = hoist_rate_filter(fn0)
        if hoisted is not fn0:
            fn0 = hoisted
            first_agg = _dc_replace(first_agg, expr=fn0)
        if fn0.name in ("_ratio", "_arith"):
            out = _arith(nq, cfg, first_agg, extra_stream)
            _apply_multiplier(out, first_agg)
            return out
        rewritten = _rewrite_if_agg(first_agg)
        if rewritten is not None:
            out = _logql_filter(nq, cfg, rewritten, extra_stream)
            out.note("if(...) translated as a filtered aggregation (the "
                     "condition became log filters)", APPROXIMATE)
            _apply_multiplier(out, first_agg)
            return out
        if fn0.name == "filter":
            out = _logql_filter(nq, cfg, first_agg, extra_stream)
            _apply_multiplier(out, first_agg)
            return out

    split = _Split(nq, cfg, t, extra_stream)
    sel, line_part = split.sel, split.line_part
    meta, parsed, or_groups = split.meta, split.parsed, split.or_groups

    items = [i for i in nq.select]
    aggs = [i for i in items if isinstance(i.expr, Func)]

    # --- plain log stream (logs panel) ---
    if not aggs:
        pipe = _pipeline(meta, parsed, cfg, t, need_parser=False,
                         or_groups=or_groups)
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
    # FACET capture(message, r'(?P<name>...)') is a regexp parser stage
    # (PromQL would use label_replace; Loki extracts the label directly).
    regexp_labels = {new for new, _ref, src, _rx in t.label_replace
                     if src == "message"}
    regexp_stage = " ".join(
        "| regexp %s" % q(rx if "(?P<" in rx
                          else rx.replace("(", "(?P<%s>" % new, 1))
        for new, _ref, src, rx in t.label_replace if src == "message")
    if regexp_stage:
        t.notes[:] = [n for n in t.notes if "became label_replace(" not in n]
        t.note("FACET capture(message, ...) became a `| regexp` parser stage "
               "extracting %s from the log line; verify the pattern"
               % ", ".join(sorted(regexp_labels)), APPROXIMATE)
        t.label_replace = [x for x in t.label_replace if x[2] != "message"]
    facet_needs_parser = any(l not in stream_labels and l not in regexp_labels
                             for l in by)
    if facet_needs_parser:
        t.note("FACET on non-stream-label attribute(s) requires the parser "
               "stage; verify field names after parsing", NEEDS_REVIEW)
    by_clause = " by (%s)" % ", ".join(by) if by else ""

    def base_stream(extra_pipe: str = "", need_parser: bool = False) -> str:
        pipe = _pipeline(meta, parsed, cfg, t,
                         need_parser=need_parser or facet_needs_parser,
                         or_groups=or_groups)
        s = sel + line_part
        if regexp_stage:
            s += " " + regexp_stage
        if pipe:
            s += " " + pipe
        if extra_pipe:
            s += " " + extra_pipe
        return s

    item = aggs[0]
    fn = item.expr
    assert isinstance(fn, Func)
    alias = item.alias
    name = fn.name

    def finish(expr: str, qtype: Optional[str] = None) -> Translation:
        if nq.facet and isinstance(nq.limit, int):
            expr = "topk(%d, %s)" % (nq.limit, expr)
        t.expr = expr
        t.query_type = qtype or ("range" if is_range else "instant")
        t.legend = legend_for(by, alias, t.legend_template)
        t.group_by = by
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
        _apply_multiplier(t, item)
        return t

    if name == "count":
        t.notes.append("unit:short")
        return finish("sum%s(count_over_time(%s [%s]))"
                      % (by_clause, base_stream(), window))

    def unwrap_attr_name() -> str:
        arg = fn.args[0] if fn.args else None
        attr, _ = unwrap_attr(arg)
        if attr is None:
            raise Untranslatable("%s() on logs needs a numeric attribute"
                                 % name)
        field = attr.name.replace(".", "_")
        t.note("unwrap of %r assumes it is a numeric field after parsing"
               % field, APPROXIMATE)
        return field

    def unwrap_stream(field: str) -> str:
        pipe = _pipeline(meta, parsed, cfg, t, need_parser=True,
                         error_guard=False, or_groups=or_groups)
        s = sel + line_part
        if pipe:
            s += " " + pipe
        return '%s | unwrap %s | __error__=""' % (s, field)

    # Unwrapped range aggregations take by()-grouping directly, which
    # aggregates over all samples across streams in the group — matching
    # NR's event-level semantics. Without it, a bare avg_over_time returns
    # one series PER STREAM instead of NR's single series.
    group = " by (%s)" % ", ".join(by) if by else " by ()"

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
        inner = fn.args[0] if fn.args and isinstance(fn.args[0], Func) \
            else None
        if inner is not None and inner.name == "sum":
            # rate(sum(bytes), 1 second): LogQL rate() over an unwrapped
            # value is the per-second sum of the values.
            arg = inner.args[0] if inner.args else None
            attr, _ = unwrap_attr(arg)
            if attr is None:
                raise Untranslatable("rate(sum(x)) on logs needs a numeric "
                                     "attribute")
            field = attr.name.replace(".", "_")
            t.note("unwrap of %r assumes it is a numeric field after parsing"
                   % field, APPROXIMATE)
            t.notes.append("unit:%s" % (
                "Bps" if per_seconds == 1 and "byte" in field.lower()
                else "short"))
            return finish("sum%s(rate(%s [%s]))%s"
                          % (by_clause, unwrap_stream(field), window, mult))
        if inner is not None and inner.name not in ("count",):
            raise Untranslatable(
                "rate(%s(...)) on logs has no LogQL equivalent: only "
                "rate(count(*)) (lines per unit of time) and rate(sum(x)) "
                "(sum of a field per unit of time) translate" % inner.name)
        t.notes.append("unit:%s" % ("cps" if per_seconds == 1 else
                                    "cpm" if per_seconds == 60 else "short"))
        return finish("sum%s(rate(%s [%s]))%s"
                      % (by_clause, base_stream(), window, mult))

    if name in ("average", "avg", "sum", "max", "min"):
        over = {"average": "avg_over_time", "avg": "avg_over_time",
                "sum": "sum_over_time", "max": "max_over_time",
                "min": "min_over_time"}[name]
        field = unwrap_attr_name()
        if name == "sum":
            # LogQL rejects by()/without() on sum_over_time; sum outside.
            return finish("sum%s(sum_over_time(%s [%s]))"
                          % (by_clause, unwrap_stream(field), window))
        return finish("%s(%s [%s])%s"
                      % (over, unwrap_stream(field), window, group))

    if name in ("percentile", "median"):
        pcts = [50.0] if name == "median" else [
            float(a.value) for a in fn.args[1:]
            if isinstance(a, Lit) and isinstance(a.value, (int, float))
        ] or [95.0]
        field = unwrap_attr_name()
        exprs = ["quantile_over_time(%s, %s [%s])%s"
                 % (_fq(p), unwrap_stream(field), window, group)
                 for p in pcts]
        for p, e in list(zip(pcts, exprs))[1:]:
            t.extra.append(Translation(
                expr=e, datasource="loki",
                query_type="range" if is_range else "instant",
                legend=(legend_for(by, template=t.legend_template)
                        + " p%g" % p).strip(),
                confidence=APPROXIMATE, group_by=by))
        out = finish(exprs[0])
        out.legend = (legend_for(by, alias, t.legend_template)
                      + " p%g" % pcts[0]).strip()
        return out

    if name in ("uniquecount", "cardinality"):
        arg = fn.args[0] if fn.args else None
        attr, _ = unwrap_attr(arg)
        if attr is None:
            raise Untranslatable("uniqueCount() needs an attribute")
        label, _ = map_attr(attr.name, cfg)
        t.note("uniqueCount over logs can be expensive in Loki (series per "
               "value)", NEEDS_REVIEW)
        t.notes.append("unit:short")
        return finish(
            "count(sum by (%s)(count_over_time(%s [%s])))"
            % (label, base_stream(need_parser=True), window))

    if name in ("latest", "earliest"):
        arg = fn.args[0] if fn.args else None
        attr, _ = unwrap_attr(arg)
        if attr is not None and attr.name.lower() in ("message", "timestamp"):
            # latest(message): the most recent log line — a logs panel
            # limited to one line is the faithful rendering.
            t.expr = base_stream().strip()
            t.query_type = "range"
            t.notes.append("panel-hint:logs")
            t.notes.append("maxlines:1")
            t.note("%s(%s) rendered as a logs panel showing the %s "
                   "matching line" % (name, attr.name,
                                      "newest" if name == "latest"
                                      else "oldest"), APPROXIMATE)
            return t
        over = "last_over_time" if name == "latest" else "first_over_time"
        field = unwrap_attr_name()
        return finish("%s(%s [%s])%s"
                      % (over, unwrap_stream(field), window, group))

    if name == "percentage":
        inner = fn.args[0] if fn.args else None
        if isinstance(inner, Func) and inner.name == "count":
            probe = Translation()
            branches = [_ci_levels(b, probe)
                        for b in cond_to_branches(fn.where, cfg, probe)]
            for n in probe.notes:
                t.note(n)
            numeric = [Matcher(p.label, p.op, _fmt_num(p.value))
                       for p in probe.numeric]
            if len(branches) > 1:
                t.note("an OR inside percentage(...) could not be honored "
                       "on logs; only the first alternative was applied",
                       NEEDS_REVIEW)
            extra = branches[0] + numeric
            s2, l2, m2, p2 = _split_matchers(extra, cfg, t)
            sel2 = _selector(split.stream + s2, t)
            line2 = (" " + " ".join(split.lines + l2)) \
                if (split.lines or l2) else ""
            pipe2 = _pipeline(meta + m2, parsed + p2, cfg, t, False,
                              or_groups=or_groups)
            num_base = sel2 + line2 + ((" " + pipe2) if pipe2 else "")
            t.notes.append("unit:percent")
            return finish(
                "100 * sum%s(count_over_time(%s [%s])) / "
                "sum%s(count_over_time(%s [%s]))"
                % (by_clause, num_base, window, by_clause, base_stream(),
                   window))
        raise Untranslatable("percentage() on logs supports only count(*)")

    if name in ("funnel",):
        raise Untranslatable(
            "funnel() is event-sequence analysis with no LogQL equivalent")
    if name in ("eventtype", "keyset"):
        raise Untranslatable("%s() is NRDB introspection" % name)

    if name in _LOG_MATH:
        inner = fn.args[0] if fn.args else None
        if name == "round" and isinstance(inner, Func):
            out = _translate_one(_sub_query(nq, SelectItem(expr=inner,
                                                           alias=alias),
                                            nq.where), cfg, extra_stream)
            out.note("round() dropped: LogQL has no rounding function; set "
                     "the panel's decimals instead", APPROXIMATE)
            return out
        raise Untranslatable(
            "LogQL has no %s() function; apply it with a Grafana "
            "transformation (Add field from calculation) on the aggregated "
            "series" % name)
    raise Untranslatable("aggregation %s() is not supported for FROM Log"
                         % name)


def _arith(nq: NrqlQuery, cfg: Dict[str, Any], item: SelectItem,
           extra_stream: List[Matcher]) -> Translation:
    """agg(x) / agg(y) and other arithmetic between log aggregations."""
    fn = item.expr
    assert isinstance(fn, Func)
    if fn.name == "_ratio":
        op, left, right = "/", fn.args[0], fn.args[1]
    else:
        op = str(getattr(fn.args[0], "value", "+"))
        left, right = fn.args[1], fn.args[2]
    out = Translation(datasource="loki")

    def side(node: Any) -> str:
        if isinstance(node, Func):
            sub = _translate_one(_sub_query(nq, SelectItem(expr=node),
                                            nq.where), cfg, extra_stream)
            out.confidence = worst(out.confidence, sub.confidence)
            for n in sub.notes:
                if not n.startswith(("unit:", "panel-hint:", "maxlines:")) \
                        and n not in out.notes:
                    out.notes.append(n)
            out.group_by = sub.group_by
            out.legend = sub.legend
            out.query_type = sub.query_type
            if sub.extra:
                out.note("only the first target of %s entered the "
                         "arithmetic" % expr_text(node), NEEDS_REVIEW)
            return "(%s)" % sub.expr
        if isinstance(node, Lit) and isinstance(node.value, (int, float)):
            return _fmt_num(float(node.value))
        raise Untranslatable("arithmetic operand %s has no LogQL "
                             "equivalent" % expr_text(node))

    l_expr = side(left)
    r_expr = side(right)
    out.expr = "%s %s %s" % (l_expr, op, r_expr)
    out.legend = item.alias or out.legend
    out.note("arithmetic between log aggregations preserved as LogQL "
             "arithmetic (both sides share the stream selector and "
             "grouping)", APPROXIMATE)
    if fn.name == "_ratio":
        def base_name(f: Any) -> str:
            while isinstance(f, Func) and f.name == "filter" and f.args:
                f = f.args[0]
            return f.name if isinstance(f, Func) else ""
        if base_name(left) in ("count", "rate", "uniquecount") \
                and base_name(right) in ("count", "rate", "uniquecount"):
            out.notes.append("unit:percentunit")
    return out


def _fq(p: float) -> str:
    s = ("%f" % (p / 100.0)).rstrip("0").rstrip(".")
    return s or "0"
