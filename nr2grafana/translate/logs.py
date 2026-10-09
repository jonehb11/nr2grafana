"""NRQL (FROM Log) -> LogQL translation.

Strategy per the migration spec:
- WHERE predicates split three ways: Loki stream-selector labels (per
  config loki_stream_labels), line filters (predicates on `message` and
  allColumnSearch()), and pipeline label filters (everything else, behind
  an optional parser stage).
- SELECT */plain attributes -> log stream query for a Grafana logs panel;
  projected columns render through `| json | line_format "{{.f1}} ..."`.
- Aggregations -> LogQL metric queries (count_over_time, rate, unwrap ...).
- FACET aparse()/capture() -> `| regexp "(?P<name>...)"` + `by (name)`.
- Dashboard variables and concat() inside label values render as `$var`
  text (SEAM-RENDER); an AST repr in an emitted query is a bug.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from ..nrql.parser import (
    Attr, BoolOp, Cmp, Cond, Func, InList, Lit, NotOp, NrqlQuery, SelectItem,
    Star,
)
from .common import (
    APPROXIMATE, NEEDS_REVIEW, Matcher, Translation, Untranslatable,
    cond_to_matchers, grafana_var, is_nr_variable, legend_for, like_to_regex,
    map_attr, q, regex_escape, sanitize_label, worst,
)
from .metrics import nr_duration_to_prom

try:  # SEAM-RENDER: shared with the metrics translator
    from .common import render_value as _render_value
except ImportError:  # pragma: no cover - sibling module not landed yet
    _render_value = None


# ---------------------------------------------------------------------------
# Value rendering (SEAM-RENDER) - Lit/Attr/{{var}}/concat -> "p-$env"
# ---------------------------------------------------------------------------

def _lit_text(v: Lit) -> str:
    val = v.value
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return "" if val is None else str(val)


def _fallback_render_value(node: Any,
                           cfg: Dict[str, Any]) -> Tuple[str, str]:
    """Minimal local stand-in for common.render_value: renders Lit, Attr,
    {{var}} and concat(...) of those to Grafana-ready text. Returns
    (text, kind) with kind in literal|var|mixed. Never a Python repr."""
    if isinstance(node, Lit):
        return _lit_text(node), "literal"
    if isinstance(node, Attr):
        var = is_nr_variable(node)
        if var:
            return "$%s" % grafana_var(var, cfg), "var"
        return node.name, "literal"
    if isinstance(node, Func) and node.name == "concat":
        texts: List[str] = []
        kinds = set()
        for arg in node.args:
            text, kind = _fallback_render_value(arg, cfg)
            texts.append(text)
            kinds.add(kind)
        if kinds == {"literal"}:
            kind = "literal"
        elif kinds == {"var"}:
            kind = "var"
        else:
            kind = "mixed"
        return "".join(texts), kind
    raise Untranslatable(
        "cannot render %s(...) as a label value; replace it with a literal "
        "or a {{variable}}" % getattr(node, "name", type(node).__name__))


def render_value(node: Any, cfg: Dict[str, Any]) -> Tuple[str, str]:
    if _render_value is not None:
        return _render_value(node, cfg)
    return _fallback_render_value(node, cfg)


def _is_concat(node: Any) -> bool:
    return isinstance(node, Func) and node.name == "concat"


def _value_regex(node: Any, cfg: Dict[str, Any], like: bool = False) -> str:
    """Render a value node for a regex matcher: literal parts are escaped
    (LIKE wildcards converted when ``like``), $var parts pass through."""
    if _is_concat(node):
        return "".join(_value_regex(a, cfg, like) for a in node.args)
    text, kind = render_value(node, cfg)
    if kind == "var":
        return text
    esc = like_to_regex(text) if like else regex_escape(text)
    if kind == "literal":
        return esc
    # mixed text from an upstream renderer: keep its $vars unescaped
    return re.sub(r"\\\$(?=[A-Za-z_{])", "$", esc)


def _prerender(cond: Optional[Cond], cfg: Dict[str, Any],
               t: Translation) -> Optional[Cond]:
    """Rewrite concat()/{{var}} value nodes into rendered literals so the
    shared matcher builder never sees (and never reprs) an AST node."""
    if cond is None:
        return None
    if isinstance(cond, BoolOp):
        return BoolOp(cond.op, [_prerender(c, cfg, t) for c in cond.items])
    if isinstance(cond, NotOp):
        return NotOp(_prerender(cond.item, cfg, t))
    try:
        if isinstance(cond, Cmp) and _is_concat(cond.right):
            if cond.op in ("=", "!=", "RLIKE", "NOT RLIKE"):
                text, kind = render_value(cond.right, cfg)
                if kind == "unsupported":
                    t.note("value expression %r in WHERE has no exact "
                           "LogQL rendering; verify the label value"
                           % text, NEEDS_REVIEW)
                return Cmp(cond.left, cond.op, Lit(text))
            if cond.op in ("LIKE", "NOT LIKE"):
                pat = "(?i)" + _value_regex(cond.right, cfg, like=True)
                op = "RLIKE" if cond.op == "LIKE" else "NOT RLIKE"
                return Cmp(cond.left, op, Lit(pat))
            return cond
        if isinstance(cond, InList) and (
                any(_is_concat(v) for v in cond.values)
                or (len(cond.values) > 1
                    and any(is_nr_variable(v) for v in cond.values))):
            alt = "|".join(_value_regex(v, cfg) for v in cond.values)
            return Cmp(cond.left, "NOT RLIKE" if cond.negated else "RLIKE",
                       Lit(alt))
    except Untranslatable as e:
        t.note(str(e), NEEDS_REVIEW)
    return cond


# ---------------------------------------------------------------------------
# allColumnSearch() -> line filters
# ---------------------------------------------------------------------------

_SEARCH_FN = "allcolumnsearch"
_INSENSITIVE_RE = re.compile(r"(?i)^insensitive\s*[:=]\s*(\w+)$")


def _search_terms(node: Any) -> Optional[Tuple[str, bool, bool]]:
    """-> (text, insensitive, positive) when ``node`` is an allColumnSearch
    predicate, in either the bare-function or the `fn = true` shape."""
    positive = True
    if isinstance(node, Cmp) and isinstance(node.left, Func) \
            and node.left.name == _SEARCH_FN and node.op in ("=", "!=") \
            and isinstance(node.right, Lit) \
            and isinstance(node.right.value, bool):
        positive = (node.right.value is True) == (node.op == "=")
        node = node.left
    if not (isinstance(node, Func) and node.name == _SEARCH_FN):
        return None
    text: Optional[str] = None
    insensitive = False
    for a in node.args:
        if isinstance(a, Lit) and isinstance(a.value, bool):
            insensitive = insensitive or a.value
        elif isinstance(a, Lit) and isinstance(a.value, str):
            m = _INSENSITIVE_RE.match(a.value)
            if m:
                insensitive = insensitive or m.group(1).lower() == "true"
            elif text is None:
                text = a.value
        elif isinstance(a, Attr) and text is None:
            text = a.name
    return text or "", insensitive, positive


def _contains_search(node: Any) -> bool:
    if _search_terms(node) is not None:
        return True
    if isinstance(node, BoolOp):
        return any(_contains_search(i) for i in node.items)
    if isinstance(node, NotOp):
        return _contains_search(node.item)
    return False


def _extract_searches(cond: Optional[Cond], out: List[str], t: Translation,
                      negate: bool = False) -> Optional[Cond]:
    """Pull allColumnSearch() predicates out of the AND-tree into Loki
    line filters (``out``); return the remaining condition (or None)."""
    if cond is None:
        return None
    found = _search_terms(cond)
    if found is not None:
        text, insensitive, positive = found
        positive = positive != negate
        if not text:
            t.note("allColumnSearch() without a search string was dropped",
                   NEEDS_REVIEW)
            return None
        if insensitive:
            out.append("%s %s" % ("|~" if positive else "!~",
                                  q("(?i)" + regex_escape(text))))
        else:
            out.append("%s %s" % ("|=" if positive else "!=", q(text)))
        t.note("allColumnSearch(%r) became a whole-line filter (Loki has "
               "no per-attribute search; stream labels and structured "
               "metadata are not part of the line)" % text, APPROXIMATE)
        return None
    if isinstance(cond, NotOp):
        inner = _extract_searches(cond.item, out, t, not negate)
        return None if inner is None else NotOp(inner)
    if isinstance(cond, BoolOp) and (cond.op == "and") != negate:
        # a conjunction (or a negated disjunction, which is one too)
        kept: List[Cond] = []
        for item in cond.items:
            rest = _extract_searches(item, out, t, negate)
            if rest is not None:
                kept.append(rest)
        if not kept:
            return None
        if len(kept) == 1:
            return kept[0]
        return BoolOp(cond.op, kept)
    if isinstance(cond, BoolOp) and _contains_search(cond):
        t.note("allColumnSearch() inside an OR clause cannot become a "
               "line filter; that clause was DROPPED - split it into "
               "separate queries", NEEDS_REVIEW)
        return None
    return cond


# ---------------------------------------------------------------------------
# WHERE -> matchers
# ---------------------------------------------------------------------------

_LEVEL_LABELS = frozenset([
    "level", "log_level", "loglevel", "severity", "severity_text",
    "severitytext", "detected_level", "log_severity",
])


def _relax_levels(matchers: List[Matcher], cfg: Dict[str, Any],
                  t: Translation) -> List[Matcher]:
    """level='ERROR' -> level=~"(?i)ERROR": Loki level labels vary in case
    between shippers (ERROR/error/Error); opt out with
    loki_case_insensitive_levels: false."""
    if not cfg.get("loki_case_insensitive_levels", True):
        return matchers
    out: List[Matcher] = []
    changed = False
    for m in matchers:
        if m.label.lower() in _LEVEL_LABELS and m.value \
                and not m.value.startswith("$"):
            if m.op in ("=", "!="):
                m = Matcher(m.label, "=~" if m.op == "=" else "!~",
                            "(?i)" + regex_escape(m.value))
                changed = True
            elif m.op in ("=~", "!~") and not m.value.startswith("(?i)"):
                m = Matcher(m.label, m.op, "(?i)" + m.value)
                changed = True
        out.append(m)
    if changed:
        t.note("level matcher made case-insensitive ((?i)) because Loki "
               "level labels vary in case between shippers; set "
               "loki_case_insensitive_levels: false for exact matching")
    return out


def _prepare(cond: Optional[Cond], cfg: Dict[str, Any],
             t: Translation) -> Tuple[List[Matcher], List[str]]:
    """WHERE -> (label matchers, extra line filters from allColumnSearch)."""
    searches: List[str] = []
    rest = _extract_searches(cond, searches, t)
    rest = _prerender(rest, cfg, t)
    matchers = _relax_levels(cond_to_matchers(rest, cfg, t), cfg, t)
    return matchers, searches


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
               % (m.label, parser or "json"), APPROXIMATE)
    if used_parser and error_guard:
        parts.append('| __error__=""')
    return " ".join(parts)


# ---------------------------------------------------------------------------
# FACET aparse()/capture() -> regexp parser stages
# ---------------------------------------------------------------------------

_GROUP_RE = re.compile(r"\(\?P?<([A-Za-z_][A-Za-z0-9_]*)>")


def _rq(regex: str) -> str:
    """Quote a regex for a LogQL parser stage: a backtick raw string keeps
    the human-readable form (no doubled backslashes) when it can."""
    if "\\" in regex and "`" not in regex:
        return "`%s`" % regex
    return q(regex)


def _aparse_regex(pattern: str, base: str) -> Tuple[str, List[str]]:
    """NR aparse pattern -> RE2: `%` matches without capturing, `*`
    captures (named base, base_2, ...)."""
    names: List[str] = []
    out: List[str] = []
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "*":
            name = base if not names else "%s_%d" % (base, len(names) + 1)
            names.append(name)
            out.append("(?P<%s>.*)" % name)
        else:
            out.append(regex_escape(ch))
    regex = "".join(out)
    if not names:
        names = [base]
        regex = "(?P<%s>%s)" % (base, regex)
    return regex, names


def _capture_regex(pattern: str, alias: Optional[str], base: str,
                   t: Translation) -> Tuple[str, List[str]]:
    # RE2 wants (?P<name>...); accept the (?<name>...) spelling too.
    pattern = re.sub(r"\(\?<(?=[A-Za-z_])", "(?P<", pattern)
    groups = _GROUP_RE.findall(pattern)
    if not groups:
        return "(?P<%s>%s)" % (base, pattern), [base]
    if alias and len(groups) == 1 and groups[0] != base:
        pattern = _GROUP_RE.sub("(?P<%s>" % base, pattern, count=1)
        groups = [base]
    elif alias and len(groups) > 1:
        t.note("capture() with several named groups ignores AS %r; "
               "grouped by %s" % (alias, ", ".join(groups)))
    return pattern, groups


def _facet_regex(fn: Func, alias: Optional[str],
                 t: Translation) -> Tuple[str, str, List[str]]:
    """-> (source field, regex, extracted label names)."""
    args = list(fn.args)
    if len(args) >= 3 and isinstance(args[1], Attr) and args[1].name == "r":
        del args[1]  # r'...' raw-string prefix tokenized separately
    field = "message"
    if args and isinstance(args[0], Attr):
        field = args[0].name
    elif args and not isinstance(args[0], Lit):
        raise Untranslatable(
            "%s() needs an attribute as its first argument" % fn.name)
    pats = [a for a in args[1:]
            if isinstance(a, Lit) and isinstance(a.value, str)]
    if not pats:
        raise Untranslatable(
            "%s() needs a string pattern as its second argument" % fn.name)
    base = sanitize_label(alias) if alias else fn.name
    if fn.name == "aparse":
        regex, names = _aparse_regex(pats[-1].value, base)
    else:
        regex, names = _capture_regex(pats[-1].value, alias, base, t)
    return field, regex, names


def _facet_plan(nq: NrqlQuery, cfg: Dict[str, Any],
                t: Translation) -> Tuple[List[str], List[str], bool]:
    """-> (group-by labels, extra parser stages, needs_parser)."""
    stream_labels = set(cfg.get("loki_stream_labels") or [])
    by: List[str] = []
    stages: List[str] = []
    needs_parser = False
    for item in nq.facet:
        expr = item.expr
        if isinstance(expr, Attr):
            label, mapped = map_attr(expr.name, cfg)
            if not mapped:
                t.note("FACET attribute %r not in label_map; used %r"
                       % (expr.name, label), APPROXIMATE)
            by.append(label)
            if label not in stream_labels:
                needs_parser = True
        elif isinstance(expr, Func) and expr.name in ("aparse", "capture"):
            field, regex, names = _facet_regex(expr, item.alias, t)
            if field != "message":
                needs_parser = True
                stages.append("| line_format %s"
                              % q("{{.%s}}" % sanitize_label(field)))
                t.note("FACET %s(%s, ...) re-parses the %r field via "
                       "line_format; verify the field name after `| %s`"
                       % (expr.name, field, field,
                          cfg.get("loki_parser", "json") or "json"),
                       NEEDS_REVIEW)
            stages.append("| regexp %s" % _rq(regex))
            by.extend(names)
            t.note("FACET %s(...) became a regexp parser stage extracting "
                   "%s" % (expr.name, ", ".join(names)),
                   APPROXIMATE if expr.name == "aparse" else None)
        elif isinstance(expr, Func):
            t.note("FACET %s(...) has no label equivalent; grouping dropped"
                   % expr.name, NEEDS_REVIEW)
        else:
            t.note("unsupported FACET expression dropped", NEEDS_REVIEW)
    if needs_parser:
        t.note("FACET on non-stream-label attribute(s) requires the parser "
               "stage; verify field names after parsing", APPROXIMATE)
    return by, stages, needs_parser


# ---------------------------------------------------------------------------
# filter()/if() rewrites
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _logql_ratio(nq: NrqlQuery, cfg: Dict[str, Any],
                 item: SelectItem) -> Translation:
    """filter(count(*), WHERE ...) * 100 / count(*) and friends: both
    operands translated against the same FROM/WHERE/FACET and divided."""
    fn = item.expr
    if len(fn.args) != 2 or not all(isinstance(a, Func) for a in fn.args):
        raise Untranslatable(
            "a '/' ratio on logs must divide two aggregations "
            "(filter(count(*), WHERE ...) / count(*))")
    parts: List[Translation] = []
    for operand in fn.args:
        sub = copy.copy(nq)
        sub.select = [SelectItem(expr=operand, alias=None)]
        sub.compare_with = None
        parts.append(translate_to_logql(sub, cfg))
    left, right = parts
    t = Translation(datasource="loki", query_type=left.query_type,
                    legend=legend_for(left.group_by, item.alias),
                    group_by=list(left.group_by))
    for part in parts:
        for n in part.notes:
            if not n.startswith("unit:"):
                t.note(n)
        t.confidence = worst(t.confidence, part.confidence)
        for v in part.vars:
            t.use_var(v)
    expr = "(%s) / (%s)" % (left.expr, right.expr)
    mult = item.multiplier
    if mult and mult != 1:
        expr = "%g * %s" % (mult, expr)
        if mult == 100:
            t.notes.append("unit:percent")
    else:
        t.notes.append("unit:percentunit")
    t.expr = expr
    t.note("agg(x) / agg(y) ratio: LogQL divides the two results matching "
           "on the shared FACET grouping (a series present in only one "
           "operand drops out; the denominator must be non-zero)",
           APPROXIMATE)
    if nq.compare_with:
        t.note("COMPARE WITH on a ratio query has no LogQL equivalent; "
               "comparison dropped", NEEDS_REVIEW)
    return t


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
        if first_agg.expr.name == "_ratio":
            return _logql_ratio(nq, cfg, first_agg)

    if len(nq.from_) > 1:
        t.note("FROM %s selects several event types; Loki keeps one log "
               "store, so the query was translated against %r — pick the "
               "other sources with stream labels if they are separate "
               "streams" % (", ".join(nq.from_), nq.from_[0]))

    matchers, searches = _prepare(nq.where, cfg, t)
    stream, lines, meta, parsed = _split_matchers(matchers, cfg, t)
    lines = lines + searches
    sel = _selector(stream, t)
    line_part = (" " + " ".join(lines)) if lines else ""

    items = [i for i in nq.select]
    aggs = [i for i in items if isinstance(i.expr, Func)]

    # --- plain log stream (logs panel) ---
    if not aggs:
        return _stream_query(nq, cfg, t, items, sel, line_part, meta,
                             parsed)

    # --- metric queries ---
    by, facet_stages, facet_needs_parser = _facet_plan(nq, cfg, t)
    stream_labels = set(cfg.get("loki_stream_labels") or [])
    by_clause = " by (%s)" % ", ".join(by) if by else ""

    def base_stream(need_parser: bool = False,
                    error_guard: bool = True) -> str:
        pipe = _pipeline(meta, parsed, cfg, t,
                         need_parser=need_parser or facet_needs_parser,
                         error_guard=error_guard)
        s = sel + line_part
        if pipe:
            s += " " + pipe
        for stage in facet_stages:
            s += " " + stage
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

    if name == "bytecountestimate":
        t.notes.append("unit:bytes")
        return finish("sum%s(bytes_over_time(%s [%s]))"
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
        return '%s | unwrap %s | __error__=""' % (
            base_stream(need_parser=True, error_guard=False), field)

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
        inner_by = by + ([label] if label not in by else [])
        t.note("uniqueCount over logs can be expensive in Loki (series per "
               "value)", NEEDS_REVIEW)
        return finish(
            "count%s(count by (%s)(count_over_time(%s [%s])))"
            % (by_clause, ", ".join(inner_by),
               base_stream(need_parser=label not in stream_labels), window))

    if name in ("latest", "earliest"):
        over = "last_over_time" if name == "latest" else "first_over_time"
        field = unwrap_attr()
        return finish("%s(%s [%s])%s"
                      % (over, unwrap_stream(field), window, group))

    if name == "percentage":
        inner = fn.args[0] if fn.args else None
        if isinstance(inner, Func) and inner.name == "count":
            extra, s2lines = _prepare(fn.where, cfg, t)
            s2, l2, m2, p2 = _split_matchers(extra, cfg, t)
            l2 = l2 + s2lines
            sel2 = _selector(stream + s2, t)
            line2 = (" " + " ".join(lines + l2)) if (lines or l2) else ""
            pipe2 = _pipeline(meta + m2, parsed + p2, cfg, t,
                              facet_needs_parser)
            num_base = sel2 + line2 + ((" " + pipe2) if pipe2 else "")
            for stage in facet_stages:
                num_base += " " + stage
            t.notes.append("unit:percent")
            return finish(
                "100 * sum%s(count_over_time(%s [%s])) / "
                "sum%s(count_over_time(%s [%s]))"
                % (by_clause, num_base, window, by_clause, base_stream(),
                   window))
        raise Untranslatable("percentage() on logs supports only count(*)")

    raise Untranslatable("aggregation %s() is not supported for FROM Log"
                         % name)


def _stream_query(nq: NrqlQuery, cfg: Dict[str, Any], t: Translation,
                  items: List[SelectItem], sel: str, line_part: str,
                  meta: List[Matcher], parsed: List[Matcher]) -> Translation:
    """SELECT * / SELECT f1, f2 FROM Log -> a logs-panel stream query."""
    parser = cfg.get("loki_parser", "json")
    named = [i.expr.name for i in items
             if isinstance(i.expr, Attr) and not is_nr_variable(i.expr)]
    shown = [sanitize_label(n) for n in named if n != "timestamp"]
    fmt = ""
    if shown and parser:
        fmt = "| line_format %s" % q(" ".join("{{.%s}}" % f for f in shown))
    pipe = _pipeline(meta, parsed, cfg, t, need_parser=bool(fmt),
                     error_guard=bool(parsed))
    expr = sel + line_part
    if pipe:
        expr += " " + pipe
    if fmt:
        expr += " " + fmt
    t.expr = expr.strip()
    t.query_type = "range"
    t.notes.append("panel-hint:logs")
    if isinstance(nq.limit, int):
        t.notes.append("maxlines:%d" % nq.limit)
    if fmt:
        t.note("column projection (%s) rendered with line_format after "
               "`| %s`; lines that are not %s or lack those fields show "
               "empty" % (", ".join(named), parser, parser), APPROXIMATE)
        if "timestamp" in named:
            t.note("the timestamp column is omitted from line_format; the "
                   "logs panel shows each entry's time")
    elif named:
        t.note("column projection (%s) is not supported by Loki; the "
               "full log line is shown" % ", ".join(named), APPROXIMATE)
    if nq.compare_with:
        t.note("COMPARE WITH has no equivalent for a logs panel; "
               "comparison dropped", NEEDS_REVIEW)
    return t


def _fq(p: float) -> str:
    s = ("%f" % (p / 100.0)).rstrip("0").rstrip(".")
    return s or "0"
