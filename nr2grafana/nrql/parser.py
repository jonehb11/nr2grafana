"""NRQL parser.

Parses New Relic Query Language (NRQL) SELECT statements into a structured
AST that the translators (PromQL / LogQL / TraceQL) consume.

Design goals:
- Zero third-party dependencies (stdlib only, Python 3.9+).
- Forgiving: anything we cannot parse raises NrqlParseError with position
  info; callers degrade gracefully (panel is emitted with the original NRQL
  preserved and flagged needs-review) instead of aborting a batch run.
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

    ``where`` holds the embedded predicate for filter(...)/percentage(...).
    """
    name: str  # lowercased
    args: List[Any] = field(default_factory=list)
    where: Optional["Cond"] = None


@dataclass
class SelectItem:
    expr: Union[Attr, Lit, Star, Func]
    alias: Optional[str] = None
    # SELECT agg(x) * 1000 — unit-conversion multiplier, very common in NR.
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
    # Anything at the tail we recognized but do not model.
    extras: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<string>'(?:[^'\\]|\\.|'')*')
  | (?P<qident>`[^`]*`)
  | (?P<var>\{\{\{?\s*[A-Za-z_][A-Za-z0-9_]*\s*\}?\}\})
  | (?P<number>-?\d+(?:\.\d+)?(?![\w.]))
  | (?P<op><>|!=|<=|>=|=|<|>)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<star>\*)
  | (?P<percent>%)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.\-/:$%{}\[\]]*)
    """,
    re.VERBOSE,
)


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
        if kind != "ws":
            toks.append(Tok(kind, m.group(), i))
        i = m.end()
    return toks


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'",
             '"': '"', "0": "\0"}


def _unquote_string(text: str) -> str:
    body = text[1:-1]
    body = body.replace("''", "'")
    # Standard escapes resolve; unknown escapes keep their backslash
    # ('C:\temp' must stay 'C:\temp', not become 'C:temp').
    body = re.sub(r"\\(.)",
                  lambda m: _ESCAPES.get(m.group(1), "\\" + m.group(1)),
                  body)
    return body


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
        if tok is None:
            return True
        return tok.kind == "ident" and tok.upper() in _CLAUSE_KEYWORDS

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
                q.where = self.parse_condition()
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
            else:
                # Unknown tail clause; capture and stop being clever.
                q.extras.append(self.consume_clause_text(include_first=True))
        return q

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
        # agg(x) * 1000 style unit-conversion arithmetic.
        while True:
            tok = self.peek()
            nxt = self.peek(1)
            if tok is not None and tok.kind == "star" and nxt is not None \
                    and nxt.kind == "number":
                self.next()
                factor = float(self.next().text)
                multiplier = (multiplier or 1.0) * factor
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
            return Lit(float(tok.text) if "." in tok.text else int(tok.text))
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
                fn.where = self.parse_condition()
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
        # apdex(duration, t: 0.5) — named threshold arg.
        tok = self.peek()
        nxt = self.peek(1)
        if tok is not None and tok.kind == "ident" and tok.text.endswith(":"):
            self.next()
            val = self.parse_expr()
            return Lit("%s%s" % (tok.text, getattr(val, "value", "")))
        if (tok is not None and nxt is not None and tok.kind == "ident"
                and nxt.kind == "op" and nxt.text == "="):
            # e.g. buckets(x, width = 10)? Rare; keep raw.
            name = self.next().text
            self.next()
            val = self.parse_expr()
            return Lit("%s=%s" % (name, getattr(val, "value", "")))
        return self.parse_expr()

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
