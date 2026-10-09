"""NRQL parser.

Parses New Relic Query Language (NRQL) SELECT statements into a structured
AST that the translators (PromQL / LogQL / TraceQL) consume.

Design goals:
- Zero third-party dependencies (stdlib only, Python 3.9+).
- Forgiving: anything we cannot parse raises NrqlParseError with position
  info; callers degrade gracefully (panel is emitted with the original NRQL
  preserved and flagged needs-review) instead of aborting a batch run.
- Honest about what it drops: comments (``--``, ``//``, ``/* */``) are
  stripped anywhere; a second SELECT statement in the same string, and
  calendar-bucketing functions (dateOf/hourOf/weekOf...) that have no
  Grafana equivalent, land in ``NrqlQuery.extras`` so the router flags the
  panel for review instead of silently translating the wrong thing.

Predicate conventions the translators rely on:
- ``WHERE a AND flag`` (bare boolean attribute) and ``WHERE
  allColumnSearch('t', insensitive: true)`` (bare function call) both
  become ``Cmp(<left>, '=', Lit(True))`` -- the same shape as ``flag = true``
  and ``flag IS TRUE``, so one code path handles all spellings.
- Named function arguments (``t: 0.5``, ``insensitive: true``) are carried
  as ``Lit('name:value')`` with booleans lowercased (``insensitive:true``).
- ``r'...'`` raw strings (capture() regexes) keep their backslashes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple, Union


class NrqlParseError(Exception):
    def __init__(self, message: str, pos: int = -1, query: str = ""):
        self.pos = pos
        self.query = query
        ctx = ""
        if pos >= 0 and query:
            ctx = " near: ...%s" % query[max(0, pos - 5):pos + 25]
        super().__init__("%s%s" % (message, ctx))


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------

@dataclass
class Attr:
    """Attribute reference, e.g. duration or `k8s.pod.name`."""
    name: str


@dataclass
class Lit:
    """Literal: string, number, or boolean."""
    value: Any


@dataclass
class Star:
    pass


@dataclass
class Func:
    """Function call, e.g. average(duration) or percentile(duration, 95).

    ``where`` holds the embedded predicate for filter(...)/percentage(...)
    and the condition argument of if(...). ``cases`` collects every
    embedded ``WHERE <cond> [AS <alias>]`` for cases(...); for functions
    with a single embedded WHERE it holds one entry mirroring ``where``.
    """
    name: str  # lowercased
    args: List[Any] = field(default_factory=list)
    where: Optional["Cond"] = None
    cases: List[Tuple[Any, Optional[str]]] = field(default_factory=list)


@dataclass
class SelectItem:
    expr: Union[Attr, Lit, Star, Func]
    alias: Optional[str] = None
    # SELECT agg(x) * 1000 -- unit-conversion multiplier, very common in NR.
    multiplier: Optional[float] = None


# --- WHERE conditions ---

@dataclass
class Cmp:
    left: Any
    op: str  # '=', '!=', '<', '<=', '>', '>=', 'LIKE', 'NOT LIKE', 'RLIKE', 'NOT RLIKE'
    right: Any


@dataclass
class InList:
    left: Any
    values: List[Any]
    negated: bool = False


@dataclass
class NullCheck:
    left: Any
    negated: bool = False  # True => IS NOT NULL


@dataclass
class BoolOp:
    op: str  # 'and' | 'or'
    items: List[Any] = field(default_factory=list)


@dataclass
class NotOp:
    item: Any


Cond = Union[Cmp, InList, NullCheck, BoolOp, NotOp]


# --- FACET ---

@dataclass
class FacetItem:
    expr: Any  # Attr or Func (cases(), buckets(), string(), ...)
    alias: Optional[str] = None


@dataclass
class TimeseriesSpec:
    auto: bool = True
    max: bool = False
    interval_seconds: Optional[float] = None
    slide_by: Optional[str] = None


@dataclass
class OrderBy:
    expr: Any
    direction: str = "ASC"


@dataclass
class NrqlQuery:
    raw: str = ""
    select: List[SelectItem] = field(default_factory=list)
    from_: List[str] = field(default_factory=list)
    where: Optional[Cond] = None
    facet: List[FacetItem] = field(default_factory=list)
    facet_limit: Optional[Union[int, str]] = None
    timeseries: Optional[TimeseriesSpec] = None
    since: Optional[str] = None
    until: Optional[str] = None
    compare_with: Optional[str] = None
    limit: Optional[Union[int, str]] = None  # int or 'MAX'
    order_by: Optional[OrderBy] = None
    timezone: Optional[str] = None
    extrapolate: bool = False
    metric_format: Optional[str] = None
    # Anything at the tail we recognized but do not model, plus fragments
    # we deliberately dropped (a second SELECT statement, calendar-bucket
    # functions). The router surfaces every entry as needs-review.
    extras: List[str] = field(default_factory=list)
    # Human-readable parser remarks that do not by themselves make the
    # query needs-review (e.g. multi-event FROM: translators use the first
    # event type). Additive in 1.11; translators may ignore it.
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

# Comments: NRQL accepts "-- ...", "// ..." (to end of line) and
# "/* ... */" (multi-line). They may appear anywhere, including mid-query
# lines, and are discarded by the tokenizer. The string alternative comes
# first so a "--" inside a quoted value is never mistaken for a comment.
_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<string>[rR]?'(?:[^'\\]|\\.|'')*')
  | (?P<comment>--[^\n]*|//[^\n]*|/\*(?s:.*?)\*/)
  | (?P<qident>`[^`]*`)
  | (?P<var>\{\{\{?\s*[A-Za-z_][A-Za-z0-9_]*\s*\}?\}\})
  | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\w.]))
  | (?P<op><>|!=|<=|>=|=|<|>)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<semi>;)
  | (?P<star>\*)
  | (?P<slash>/)
  | (?P<plus>\+)
  | (?P<minus>-)
  | (?P<percent>%)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.\-/:$%{}\[\]]*)
    """,
    re.VERBOSE,
)

_COMMENT_OR_STRING_RE = re.compile(
    r"'(?:[^'\\]|\\.|'')*'|--[^\n]*|//[^\n]*|/\*.*?\*/", re.S)


def strip_comments(text: str) -> str:
    """Return ``text`` without NRQL comments, whitespace collapsed.

    String literals are preserved verbatim (a ``--`` inside quotes is
    data, not a comment). Used for the human-readable fragments stored in
    ``NrqlQuery.extras``; the tokenizer drops comments on its own.
    """
    def _sub(m):
        return m.group() if m.group().startswith("'") else " "
    return " ".join(_COMMENT_OR_STRING_RE.sub(_sub, text).split())


@dataclass
class Tok:
    kind: str
    text: str
    pos: int

    def upper(self) -> str:
        return self.text.upper()


def tokenize(query: str) -> List[Tok]:
    toks: List[Tok] = []
    i = 0
    n = len(query)
    while i < n:
        m = _TOKEN_RE.match(query, i)
        if not m:
            raise NrqlParseError("unexpected character %r" % query[i], i, query)
        kind = m.lastgroup or ""
        if kind not in ("ws", "comment"):
            toks.append(Tok(kind, m.group(), i))
        i = m.end()
    return toks


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'",
             '"': '"', "0": "\0"}


def _unquote_string(text: str) -> str:
    if text[0] in "rR":
        # r'...' raw string (capture() regexes): backslashes are data.
        return text[2:-1].replace("''", "'")
    body = text[1:-1]
    body = body.replace("''", "'")
    # Standard escapes resolve; unknown escapes keep their backslash
    # ('C:\temp' must stay 'C:\temp', not become 'C:temp').
    body = re.sub(r"\\(.)",
                  lambda m: _ESCAPES.get(m.group(1), "\\" + m.group(1)),
                  body)
    return body


def _named_arg_value(val: Any) -> str:
    """Render the value of a named argument (t: 0.5, insensitive: true)
    for the ``Lit('name:value')`` carrier: booleans lowercase, attributes
    by name, literals verbatim."""
    if isinstance(val, Lit):
        if isinstance(val.value, bool):
            return "true" if val.value else "false"
        return "" if val.value is None else str(val.value)
    if isinstance(val, Attr):
        return val.name
    if isinstance(val, Func):
        return _func_text(val)
    return ""


# Keywords that terminate the current clause.
_CLAUSE_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "FACET", "TIMESERIES", "SINCE", "UNTIL",
    "COMPARE", "LIMIT", "ORDER", "WITH", "EXTRAPOLATE", "SLIDE", "SHOW",
}

_DURATION_UNITS = {
    "millisecond": 0.001, "milliseconds": 0.001, "ms": 0.001,
    "second": 1, "seconds": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "m": 60,
    "hour": 3600, "hours": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
    "week": 604800, "weeks": 604800, "w": 604800,
    "month": 2592000, "months": 2592000,
}

# Calendar-bucketing functions (FACET dateOf(timestamp) and friends). They
# have no Grafana counterpart; the query is parsed as-is (the Func node is
# kept) and an extras note makes the router flag the panel for review.
_CALENDAR_FUNCS = {
    "dateof", "hourof", "weekof", "minuteof", "monthof", "quarterof",
    "yearof", "weekdayof", "dayofmonthof",
}

# Tokens that may legitimately follow a complete predicate; a bare
# attribute or function call in front of one of them is a truthy test.
_PREDICATE_END_KWS = {"AND", "OR", "AS"} | _CLAUSE_KEYWORDS


def _iter_funcs(node: Any):
    """Yield every Func node reachable from ``node`` (depth-first)."""
    if isinstance(node, Func):
        yield node
        for a in node.args:
            for f in _iter_funcs(a):
                yield f
        for f in _iter_funcs(node.where):
            yield f
        for cond, _alias in node.cases:
            for f in _iter_funcs(cond):
                yield f
    elif isinstance(node, (SelectItem, FacetItem, OrderBy)):
        for f in _iter_funcs(node.expr):
            yield f
    elif isinstance(node, Cmp):
        for f in _iter_funcs(node.left):
            yield f
        for f in _iter_funcs(node.right):
            yield f
    elif isinstance(node, InList):
        for f in _iter_funcs(node.left):
            yield f
        for v in node.values:
            for f in _iter_funcs(v):
                yield f
    elif isinstance(node, NullCheck):
        for f in _iter_funcs(node.left):
            yield f
    elif isinstance(node, BoolOp):
        for item in node.items:
            for f in _iter_funcs(item):
                yield f
    elif isinstance(node, NotOp):
        for f in _iter_funcs(node.item):
            yield f
    elif isinstance(node, list):
        for item in node:
            for f in _iter_funcs(item):
                yield f


def _func_text(fn: Func) -> str:
    """Compact NRQL-ish rendering of a Func for notes (no Python reprs)."""
    parts: List[str] = []
    for a in fn.args:
        if isinstance(a, Func):
            parts.append(_func_text(a))
        elif isinstance(a, Attr):
            parts.append(a.name)
        elif isinstance(a, Star):
            parts.append("*")
        elif isinstance(a, Lit):
            parts.append(repr(a.value) if isinstance(a.value, str)
                         else str(a.value))
    return "%s(%s)" % (fn.name, ", ".join(parts))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, query: str):
        self.query = query
        self.toks = tokenize(query)
        self.i = 0

    # -- token helpers --

    def peek(self, offset: int = 0) -> Optional[Tok]:
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else None

    def next(self) -> Tok:
        tok = self.peek()
        if tok is None:
            raise NrqlParseError("unexpected end of query", len(self.query), self.query)
        self.i += 1
        return tok

    def at_kw(self, *kws: str) -> bool:
        tok = self.peek()
        return tok is not None and tok.kind == "ident" and tok.upper() in kws

    def eat_kw(self, *kws: str) -> bool:
        if self.at_kw(*kws):
            self.i += 1
            return True
        return False

    def expect_kw(self, kw: str) -> None:
        if not self.eat_kw(kw):
            tok = self.peek()
            raise NrqlParseError(
                "expected %s, got %r" % (kw, tok.text if tok else "<eof>"),
                tok.pos if tok else len(self.query), self.query)

    def expect(self, kind: str) -> Tok:
        tok = self.peek()
        if tok is None or tok.kind != kind:
            raise NrqlParseError(
                "expected %s, got %r" % (kind, tok.text if tok else "<eof>"),
                tok.pos if tok else len(self.query), self.query)
        return self.next()

    def _at_clause_boundary(self) -> bool:
        tok = self.peek()
        if tok is None or tok.kind == "semi":
            return True
        return tok.kind == "ident" and tok.upper() in _CLAUSE_KEYWORDS

    def _at_predicate_end(self) -> bool:
        """True when the next token cannot continue a comparison, i.e.
        the expression just parsed stands alone as a truthy predicate."""
        tok = self.peek()
        if tok is None or tok.kind in ("rparen", "comma", "semi"):
            return True
        return tok.kind == "ident" and tok.upper() in _PREDICATE_END_KWS

    # -- entry --

    def parse(self) -> NrqlQuery:
        q = NrqlQuery(raw=self.query.strip())
        # NR allows FROM-first form: FROM Txn SELECT ...
        if self.at_kw("FROM"):
            self.next()
            q.from_ = self.parse_from_list()
            self.expect_kw("SELECT")
            q.select = self.parse_select_list()
        else:
            self.expect_kw("SELECT")
            q.select = self.parse_select_list()
            if self.eat_kw("FROM"):
                q.from_ = self.parse_from_list()
        while self.peek() is not None:
            tok = self.peek()
            assert tok is not None
            u = tok.upper() if tok.kind == "ident" else ""
            if u == "WHERE":
                self.next()
                cond = self.parse_condition()
                # A second WHERE clause (NRQL allows "WHERE a FACET x
                # WHERE b") narrows the first: AND them together.
                q.where = cond if q.where is None else \
                    BoolOp("and", [q.where, cond])
            elif u == "FACET":
                self.next()
                q.facet = self.parse_facet_list()
            elif u == "TIMESERIES":
                self.next()
                q.timeseries = self.parse_timeseries()
            elif u == "SINCE":
                self.next()
                q.since = self.consume_clause_text()
            elif u == "UNTIL":
                self.next()
                q.until = self.consume_clause_text()
            elif u == "COMPARE":
                self.next()
                self.expect_kw("WITH")
                q.compare_with = self.consume_clause_text()
            elif u == "LIMIT":
                self.next()
                q.limit = self.parse_limit()
            elif u == "ORDER":
                self.next()
                self.expect_kw("BY")
                q.order_by = self.parse_order_by()
            elif u == "SLIDE":
                self.next()
                self.expect_kw("BY")
                text = self.consume_clause_text()
                if q.timeseries is not None:
                    q.timeseries.slide_by = text
                else:
                    q.extras.append("SLIDE BY " + text)
            elif u == "WITH":
                self.next()
                if self.eat_kw("TIMEZONE"):
                    q.timezone = self.consume_clause_text().strip("'\" ")
                elif self.eat_kw("METRIC_FORMAT"):
                    tok = self.peek()
                    if tok is not None and tok.kind == "string":
                        self.next()
                        q.metric_format = _unquote_string(tok.text)
                    else:
                        q.metric_format = self.consume_clause_text()
                else:
                    q.extras.append("WITH " + self.consume_clause_text())
            elif u == "EXTRAPOLATE":
                self.next()
                q.extrapolate = True
            elif u == "FROM" and not q.from_:
                # FROM appearing late (e.g. after an extras capture) must
                # still be honored, or the query gets misrouted.
                self.next()
                q.from_ = self.parse_from_list()
            elif tok.kind == "semi" or u == "SELECT" or u == "FROM":
                # A second statement in the same string ("SELECT ... ;
                # SELECT ..." or a FROM-first restart). Parse only the
                # first; the remainder is kept verbatim in extras so the
                # panel is flagged instead of the second WHERE/FACET
                # silently overwriting the first one's.
                if tok.kind == "semi":
                    self.next()
                    if self.peek() is None:
                        break
                    tok = self.peek()
                    assert tok is not None
                rest = strip_comments(self.query[tok.pos:]).rstrip("; ")
                if rest:
                    q.extras.append(rest)
                    q.notes.append(
                        "NRQL string contains more than one statement; "
                        "only the first SELECT was translated, the rest "
                        "(%s) was dropped -- split it into its own "
                        "panel/target" % rest)
                break
            else:
                # Unknown tail clause; capture and stop being clever.
                q.extras.append(self.consume_clause_text(include_first=True))
        self._post_parse(q)
        return q

    def _post_parse(self, q: NrqlQuery) -> None:
        """Attach notes/extras for shapes we parse but cannot translate."""
        if len(q.from_) > 1:
            q.notes.append(
                "FROM lists multiple event types (%s); translators use "
                "the first (%s) -- add a second target for the others if "
                "they live in a different stream/metric"
                % (", ".join(q.from_), q.from_[0]))
        seen = set()
        for fn in _iter_funcs([q.select, q.facet, q.where, q.order_by]):
            if fn.name in _CALENDAR_FUNCS and fn.name not in seen:
                seen.add(fn.name)
                q.extras.append(
                    "%s (calendar bucketing has no Grafana equivalent; "
                    "use TIMESERIES with a matching interval or a time-"
                    "bucket transformation)" % _func_text(fn))

    # -- clause parsers --

    def parse_from_list(self) -> List[str]:
        names = [self.parse_name()]
        while self.peek() is not None and self.peek().kind == "comma":  # type: ignore[union-attr]
            self.next()
            names.append(self.parse_name())
        return names

    def parse_name(self) -> str:
        tok = self.next()
        if tok.kind == "qident":
            return tok.text[1:-1]
        if tok.kind == "ident":
            return tok.text
        if tok.kind == "string":
            return _unquote_string(tok.text)
        raise NrqlParseError("expected name, got %r" % tok.text, tok.pos, self.query)

    def parse_select_list(self) -> List[SelectItem]:
        items = [self.parse_select_item()]
        while self.peek() is not None and self.peek().kind == "comma":  # type: ignore[union-attr]
            self.next()
            items.append(self.parse_select_item())
        return items

    def parse_select_item(self) -> SelectItem:
        expr = self.parse_expr()
        multiplier: Optional[float] = None
        # 1000 * agg(x) -- leading unit-conversion factor (the mirror image
        # of the far more common agg(x) * 1000 form).
        while isinstance(expr, Lit) and isinstance(expr.value, (int, float)) \
                and not isinstance(expr.value, bool):
            tok = self.peek()
            if tok is None or tok.kind != "star":
                break
            self.next()
            multiplier = (multiplier or 1.0) * float(expr.value)
            expr = self.parse_expr()
        # agg(x) * 1000 / agg(x) / 60 style unit-conversion arithmetic.
        while True:
            tok = self.peek()
            nxt = self.peek(1)
            if tok is not None and tok.kind == "star" and nxt is not None \
                    and nxt.kind == "number":
                self.next()
                factor = float(self.next().text)
                multiplier = (multiplier or 1.0) * factor
                continue
            if tok is not None and tok.kind == "slash" and nxt is not None \
                    and nxt.kind == "number":
                self.next()
                factor = float(self.next().text)
                if factor != 0:
                    multiplier = (multiplier or 1.0) / factor
                continue
            if tok is not None and tok.kind == "slash" and nxt is not None \
                    and nxt.kind == "ident" and isinstance(expr, Func) \
                    and self.peek(2) is not None \
                    and self.peek(2).kind == "lparen":
                # agg(x) / agg(y) -- a ratio of two aggregations (the
                # classic error-rate shape); modeled as the pseudo-
                # function _ratio for the translators.
                self.next()
                right = self.parse_func()
                expr = Func("_ratio", args=[expr, right])
                continue
            if tok is not None and tok.kind in ("plus", "minus") \
                    and isinstance(expr, Func):
                # agg(x) + agg(y) / agg(x) - agg(y): the sum/difference of
                # two aggregations (several queues added up); modeled as
                # the pseudo-function _arith(left, right, Lit('+'|'-')).
                self.next()
                right = self.parse_expr()
                expr = Func("_arith", args=[expr, right, Lit(tok.text)])
                continue
            break
        alias = None
        if self.eat_kw("AS"):
            tok = self.next()
            if tok.kind == "string":
                alias = _unquote_string(tok.text)
            elif tok.kind in ("ident", "qident"):
                alias = tok.text.strip("`")
            else:
                raise NrqlParseError("bad alias %r" % tok.text, tok.pos, self.query)
        return SelectItem(expr=expr, alias=alias, multiplier=multiplier)

    def parse_arith(self, left: Any) -> Any:
        """Absorb ``+ - * /`` operators after a value expression inside
        a function argument (position(x, '-', 3) - position(x, '-', 2))
        into _arith(left, right, Lit(op)) nodes; a '* <number>' after an
        aggregation stays a plain unit factor for the SELECT-item code."""
        ops = {"plus": "+", "minus": "-", "star": "*", "slash": "/"}
        while True:
            tok = self.peek()
            if tok is None or tok.kind not in ops:
                return left
            nxt = self.peek(1)
            if nxt is None or nxt.kind in ("rparen", "comma") or (
                    nxt.kind == "ident" and nxt.upper() in _CLAUSE_KEYWORDS):
                return left
            self.next()
            right = self.parse_expr()
            left = Func("_arith", args=[left, right, Lit(ops[tok.kind])])

    def parse_expr(self) -> Any:
        tok = self.peek()
        if tok is None:
            raise NrqlParseError("unexpected end of query", len(self.query), self.query)
        if tok.kind == "star":
            self.next()
            return Star()
        if tok.kind == "string":
            self.next()
            return Lit(_unquote_string(tok.text))
        if tok.kind == "number":
            self.next()
            if "." in tok.text or "e" in tok.text.lower():
                return Lit(float(tok.text))
            return Lit(int(tok.text))
        if tok.kind == "lparen":
            # Parenthesized SELECT expression: (agg(x) + agg(y)) AS 'z'.
            self.next()
            inner = self.parse_arith(self.parse_expr())
            self.expect("rparen")
            return inner
        if tok.kind == "qident":
            self.next()
            return Attr(tok.text[1:-1])
        if tok.kind == "var":
            # NR dashboard-variable placeholder {{name}} used as a value or
            # identifier; carried through as an Attr for the translators.
            self.next()
            return Attr(tok.text)
        if tok.kind == "ident":
            nxt = self.peek(1)
            if nxt is not None and nxt.kind == "lparen":
                return self.parse_func()
            self.next()
            up = tok.text.upper()
            if up == "TRUE":
                return Lit(True)
            if up == "FALSE":
                return Lit(False)
            if up == "NULL":
                return Lit(None)
            return Attr(tok.text)
        raise NrqlParseError("unexpected token %r" % tok.text, tok.pos, self.query)

    def parse_func(self) -> Func:
        name_tok = self.expect("ident")
        fn = Func(name=name_tok.text.lower())
        self.expect("lparen")
        # Empty arg list.
        if self.peek() is not None and self.peek().kind == "rparen":  # type: ignore[union-attr]
            self.next()
            return fn
        # if(condition, then[, else]) -- the first argument is a predicate,
        # not a value expression; try it as a condition with backtracking
        # (if(error, ...) with a bare truthy attribute falls through).
        if fn.name == "if":
            save = self.i
            try:
                fn.where = self.parse_condition()
                fn.cases.append((fn.where, None))
            except NrqlParseError:
                self.i = save
                fn.where = None
                del fn.cases[:]
        while True:
            tok = self.peek()
            if tok is None:
                raise NrqlParseError("unterminated function call",
                                     name_tok.pos, self.query)
            if tok.kind == "rparen":
                self.next()
                return fn
            if tok.kind == "comma":
                self.next()
                continue
            if self.at_kw("WHERE"):
                self.next()
                cond = self.parse_condition()
                alias: Optional[str] = None
                if self.eat_kw("AS"):
                    a_tok = self.next()
                    alias = _unquote_string(a_tok.text) \
                        if a_tok.kind == "string" else a_tok.text.strip("`")
                if fn.where is None:
                    fn.where = cond
                fn.cases.append((cond, alias))
                continue
            arg = self.parse_func_arg(fn)
            if arg is not None:
                fn.args.append(arg)
            # Absorb trailing modifiers attached to this argument:
            # "1 minute" durations and "AS 'label'" aliases.
            while True:
                tok = self.peek()
                if tok is None:
                    break
                if tok.kind == "ident" and fn.args \
                        and isinstance(fn.args[-1], Lit) \
                        and isinstance(fn.args[-1].value, (int, float)) \
                        and tok.text.lower() in _DURATION_UNITS:
                    unit = self.next().text.lower()
                    val = fn.args[-1].value
                    # normalized to seconds
                    fn.args[-1] = Lit(float(val) * _DURATION_UNITS[unit])
                    continue
                if tok.kind == "ident" and tok.upper() == "AS":
                    self.next()
                    alias_tok = self.next()
                    fn.args.append(Lit("AS:" + (
                        _unquote_string(alias_tok.text)
                        if alias_tok.kind == "string" else alias_tok.text)))
                    continue
                break

    def parse_func_arg(self, fn: Func) -> Any:
        # apdex(duration, t: 0.5) -- named threshold arg.
        tok = self.peek()
        nxt = self.peek(1)
        if tok is not None and tok.kind == "ident" and tok.text.endswith(":"):
            self.next()
            val = self.parse_expr()
            return Lit("%s%s" % (tok.text, _named_arg_value(val)))
        if (tok is not None and nxt is not None and tok.kind == "ident"
                and nxt.kind == "op" and nxt.text == "="):
            # e.g. buckets(x, width = 10)? Rare; keep raw.
            name = self.next().text
            self.next()
            val = self.parse_expr()
            return Lit("%s=%s" % (name, _named_arg_value(val)))
        return self.parse_arith(self.parse_expr())

    # -- WHERE --

    def parse_condition(self) -> Cond:
        return self.parse_or()

    def parse_or(self) -> Cond:
        left = self.parse_and()
        items = [left]
        while self.eat_kw("OR"):
            items.append(self.parse_and())
        return items[0] if len(items) == 1 else BoolOp("or", items)

    def parse_and(self) -> Cond:
        left = self.parse_not()
        items = [left]
        while self.eat_kw("AND"):
            items.append(self.parse_not())
        return items[0] if len(items) == 1 else BoolOp("and", items)

    def parse_not(self) -> Cond:
        if self.at_kw("NOT") and not (
                self.peek(1) is not None and self.peek(1).kind == "ident"  # type: ignore[union-attr]
                and self.peek(1).upper() in ("IN", "LIKE", "RLIKE")):  # type: ignore[union-attr]
            self.next()
            return NotOp(self.parse_not())
        return self.parse_predicate()

    def parse_predicate(self) -> Cond:
        tok = self.peek()
        if tok is not None and tok.kind == "lparen":
            # Could be a parenthesized condition.
            self.next()
            cond = self.parse_condition()
            self.expect("rparen")
            return cond
        left = self.parse_expr()
        if isinstance(left, (Attr, Func)) and self._at_predicate_end():
            # Bare boolean attribute ("... AND should_publish", "WHERE
            # `flag`") or predicate function (allColumnSearch(...)):
            # normalized to the same shape as "x = true" / "x IS TRUE".
            return Cmp(left, "=", Lit(True))
        tok = self.peek()
        if tok is None:
            raise NrqlParseError("dangling predicate", len(self.query), self.query)
        if tok.kind == "op":
            op = self.next().text
            if op == "<>":
                op = "!="
            right = self.parse_expr()
            return Cmp(left, op, right)
        if tok.kind == "ident":
            u = tok.upper()
            negated = False
            if u == "NOT":
                self.next()
                negated = True
                tok = self.peek()
                if tok is None:
                    raise NrqlParseError("dangling NOT", len(self.query), self.query)
                u = tok.upper()
            if u == "LIKE":
                self.next()
                right = self.parse_expr()
                return Cmp(left, "NOT LIKE" if negated else "LIKE", right)
            if u == "RLIKE":
                self.next()
                right = self.parse_expr()
                return Cmp(left, "NOT RLIKE" if negated else "RLIKE", right)
            if u == "IN":
                self.next()
                self.expect("lparen")
                values: List[Any] = []
                while True:
                    values.append(self.parse_expr())
                    t = self.next()
                    if t.kind == "rparen":
                        break
                    if t.kind != "comma":
                        raise NrqlParseError("bad IN list", t.pos, self.query)
                return InList(left, values, negated=negated)
            if u == "IS":
                self.next()
                neg = self.eat_kw("NOT")
                if self.eat_kw("NULL"):
                    return NullCheck(left, negated=neg)
                if self.eat_kw("TRUE"):
                    return Cmp(left, "!=" if neg else "=", Lit(True))
                if self.eat_kw("FALSE"):
                    return Cmp(left, "!=" if neg else "=", Lit(False))
                tok = self.peek()
                raise NrqlParseError(
                    "expected NULL/TRUE/FALSE after IS",
                    tok.pos if tok else len(self.query), self.query)
        raise NrqlParseError("expected comparison operator, got %r" % tok.text,
                             tok.pos, self.query)

    # -- FACET --

    def parse_facet_list(self) -> List[FacetItem]:
        items = [self.parse_facet_item()]
        while self.peek() is not None and self.peek().kind == "comma":  # type: ignore[union-attr]
            self.next()
            items.append(self.parse_facet_item())
        return items

    def parse_facet_item(self) -> FacetItem:
        expr = self.parse_expr()
        alias = None
        if self.eat_kw("AS"):
            tok = self.next()
            alias = _unquote_string(tok.text) if tok.kind == "string" else tok.text.strip("`")
        return FacetItem(expr=expr, alias=alias)

    # -- TIMESERIES --

    def parse_timeseries(self) -> TimeseriesSpec:
        spec = TimeseriesSpec()
        tok = self.peek()
        if tok is None:
            return spec
        if tok.kind == "ident" and tok.upper() == "AUTO":
            self.next()
            return spec
        if tok.kind == "ident" and tok.upper() == "MAX":
            self.next()
            spec.auto = False
            spec.max = True
            return spec
        if tok.kind == "number":
            self.next()
            value = float(tok.text)
            unit_tok = self.peek()
            if unit_tok is not None and unit_tok.kind == "ident" \
                    and unit_tok.text.lower() in _DURATION_UNITS:
                self.next()
                value *= _DURATION_UNITS[unit_tok.text.lower()]
            spec.auto = False
            spec.interval_seconds = value
            return spec
        return spec

    # -- misc --

    def parse_limit(self) -> Union[int, str]:
        tok = self.next()
        if tok.kind == "number":
            return int(float(tok.text))
        if tok.kind == "ident" and tok.upper() == "MAX":
            return "MAX"
        raise NrqlParseError("bad LIMIT %r" % tok.text, tok.pos, self.query)

    def parse_order_by(self) -> OrderBy:
        expr = self.parse_expr()
        direction = "ASC"
        if self.eat_kw("DESC"):
            direction = "DESC"
        elif self.eat_kw("ASC"):
            direction = "ASC"
        return OrderBy(expr, direction)

    def consume_clause_text(self, include_first: bool = False) -> str:
        """Consume raw tokens until the next clause keyword; return text."""
        parts: List[str] = []
        if include_first:
            parts.append(self.next().text)
        depth = 0
        while True:
            tok = self.peek()
            if tok is None:
                break
            if depth == 0 and tok.kind == "semi":
                break
            if depth == 0 and tok.kind == "ident" and tok.upper() in _CLAUSE_KEYWORDS:
                break
            if tok.kind == "lparen":
                depth += 1
            elif tok.kind == "rparen":
                depth -= 1
            parts.append(self.next().text)
        return " ".join(parts)


def parse_nrql(query: str) -> NrqlQuery:
    """Parse an NRQL query string into an NrqlQuery AST."""
    return _Parser(query).parse()
