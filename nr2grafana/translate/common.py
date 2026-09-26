"""Shared translation machinery: routing, matcher rendering, confidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..nrql.parser import (
    Attr, BinOp, BoolOp, Cmp, Cond, Func, InList, Lit, NotOp, NrqlQuery,
    NullCheck,
)

# Confidence taxonomy for every translated query.
EXACT = "exact"                # semantically equivalent
APPROXIMATE = "approximate"    # close; minor semantic drift (e.g. avg of rates)
NEEDS_REVIEW = "needs-review"  # translated but must be human-verified
UNTRANSLATABLE = "untranslatable"

_CONF_ORDER = {EXACT: 0, APPROXIMATE: 1, NEEDS_REVIEW: 2, UNTRANSLATABLE: 3}


def worst(*levels: str) -> str:
    return max(levels, key=lambda l: _CONF_ORDER[l])


class Untranslatable(Exception):
    """Raised when a query genuinely cannot be expressed in the target."""


@dataclass
class NumericPred:
    """A numeric comparison from WHERE that is not a label matcher
    (``duration > 1``). Translators that can express it (histogram bucket
    arithmetic, LogQL ``| field > n``) consume it; whatever is left is
    reported as dropped."""
    label: str      # mapped label name
    attr: str       # original NR attribute name
    op: str         # '<', '<=', '>', '>='
    value: float


@dataclass
class Translation:
    """Result of translating one NRQL query."""
    expr: str = ""
    datasource: str = "prometheus"     # config datasources key
    query_type: str = "range"          # 'range' | 'instant'
    legend: str = ""                   # Grafana legendFormat
    confidence: str = EXACT
    notes: List[str] = field(default_factory=list)
    group_by: List[str] = field(default_factory=list)  # mapped facet labels
    # Extra sibling targets (e.g. multi-select NRQL -> several exprs).
    extra: List["Translation"] = field(default_factory=list)
    # Numeric WHERE predicates awaiting a translator that can express them.
    numeric: List[NumericPred] = field(default_factory=list)
    # FACET capture(...) -> label_replace(expr, new, "$N", src, regex)
    label_replace: List[Tuple[str, str, str, str]] = field(default_factory=list)
    # FACET concat(...) -> a legend template mixing {{label}} and literals.
    legend_template: str = ""

    def note(self, msg: str, confidence: Optional[str] = None) -> None:
        if msg not in self.notes:
            self.notes.append(msg)
        if confidence:
            self.confidence = worst(self.confidence, confidence)


# ---------------------------------------------------------------------------
# Event-type routing
# ---------------------------------------------------------------------------

LOG_EVENT_TYPES = {"log", "logextendedrecord"}
SPAN_EVENT_TYPES = {"span", "distributedtrace", "distributedtracesummary"}
METRIC_EVENT_TYPES = {"metric"}
# APM/browser/mobile/infra events all translate (approximately) to metrics.
APM_EVENT_TYPES = {
    "transaction", "transactionerror", "pageview", "pageaction",
    "browserinteraction", "javascripterror", "mobile", "mobilecrash",
    "mobilerequest", "mobilerequesterror", "ajaxrequest",
    "syntheticcheck", "syntheticrequest",
}
INFRA_EVENT_TYPES = {
    "systemsample", "processsample", "storagesample", "networksample",
    "containersample",
}
K8S_EVENT_TYPES = {
    "k8sclustersample", "k8snodesample", "k8spodsample", "k8scontainersample",
    "k8sdeploymentsample", "k8snamespacesample", "k8sdaemonsetsample",
    "k8sstatefulsetsample", "k8sreplicasetsample", "k8shpasample",
    "k8sservicesample", "k8svolumesample",
    "k8seventssample", "k8sevent",
}


def event_map_entry(event_type: str, cfg: Dict[str, Any]) \
        -> Optional[Dict[str, Any]]:
    """Site-specific routing for custom event types (config ``event_map``):
    ``{"Purchase": {"family": "logs", "labels": {"job": "purchases"}}}``."""
    table = cfg.get("event_map") or {}
    if not isinstance(table, dict):
        return None
    for key, entry in table.items():
        if isinstance(entry, dict) and key.lower() == event_type.lower():
            return entry
    return None


def route_event_type(event_types: List[str],
                     cfg: Optional[Dict[str, Any]] = None) -> str:
    """Decide the target family for a FROM clause: metrics|logs|traces."""
    if not event_types:
        return "metrics"
    et = event_types[0].lower()
    if et in LOG_EVENT_TYPES:
        return "logs"
    if et in SPAN_EVENT_TYPES:
        return "traces"
    if cfg:
        entry = event_map_entry(et, cfg)
        if entry and entry.get("family") in ("logs", "traces", "metrics"):
            return str(entry["family"])
    return "metrics"


# ---------------------------------------------------------------------------
# NR dashboard-variable placeholders: {{var}} / {{{var}}}
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\{\{\{?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}?\}\}")


def nr_var_names(text: str) -> List[str]:
    return _VAR_RE.findall(text or "")


def is_nr_variable(value: Any) -> Optional[str]:
    """If a literal/attr is exactly one {{var}} placeholder, return its name."""
    if isinstance(value, Lit) and isinstance(value.value, str):
        m = _VAR_RE.fullmatch(value.value.strip())
        return m.group(1) if m else None
    if isinstance(value, Attr):
        m = _VAR_RE.fullmatch(value.name.strip())
        return m.group(1) if m else None
    return None


def grafana_var(name: str, cfg: Dict[str, Any]) -> str:
    """Apply variable renames (NR variables clashing with reserved
    datasource-variable names get renamed by the builder)."""
    return (cfg.get("var_renames") or {}).get(name, name)


# ---------------------------------------------------------------------------
# Label / attribute mapping
# ---------------------------------------------------------------------------

_LABEL_SAFE_RE = re.compile(r"[^a-zA-Z0-9_]")


def sanitize_label(name: str) -> str:
    out = _LABEL_SAFE_RE.sub("_", name)
    if out and out[0].isdigit():
        out = "_" + out
    return out


def map_attr(name: str, cfg: Dict[str, Any]) -> Tuple[str, bool]:
    """Map an NR attribute to a target label.

    Returns (label, was_mapped). was_mapped is True only for attributes
    present in the label_map — an attribute merely being label-safe is not
    evidence it exists as a label in the target stack, so callers flag
    unmapped attributes for review.
    """
    label_map = cfg.get("label_map", {})
    if name in label_map:
        return label_map[name], True
    var = _VAR_RE.fullmatch(name.strip())
    if var:
        # FACET {{attr}} / WHERE {{attr}} = ...: Grafana interpolates the
        # variable into the label position.
        return "$" + grafana_var(var.group(1), cfg), True
    # Strip common NR prefixes then retry.
    for prefix in ("tags.", "attributes.", "resource.", "label.", "labels."):
        if name.startswith(prefix) and name[len(prefix):] in label_map:
            return label_map[name[len(prefix):]], True
    # Case-insensitive fallback: NR attribute casing varies between
    # agents (podName / podname).
    low = name.lower()
    for k, v in label_map.items():
        if k.lower() == low:
            return v, True
    # CloudWatch dimensions under the YACE exporter convention.
    if low.startswith("aws."):
        from .nrmetrics import aws_attr_label
        aws = aws_attr_label(name)
        if aws:
            return aws, True
    return sanitize_label(name), False


_PROM_ESCAPE = {"\\": "\\\\", '"': '\\"', "\n": "\\n"}


def q(value: Any) -> str:
    """Quote a value for a PromQL/LogQL label matcher."""
    s = "" if value is None else str(value)
    if isinstance(value, bool):
        s = "true" if value else "false"
    out = []
    for ch in s:
        out.append(_PROM_ESCAPE.get(ch, ch))
    return '"%s"' % "".join(out)


_REGEX_META = re.compile(r"([\\.^$|?*+()\[\]{}])")


def regex_escape(text: str) -> str:
    return _REGEX_META.sub(r"\\\1", text)


def like_to_regex(pattern: str) -> str:
    """NRQL LIKE pattern (%, _) -> RE2 regex (unanchored NR semantics ->
    fully anchored regex, since Prom regex matchers are anchored)."""
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern) and pattern[i + 1] in "%_\\":
            out.append(regex_escape(pattern[i + 1]))  # escaped wildcard
            i += 2
            continue
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(regex_escape(ch))
        i += 1
    return "".join(out)


def has_embedded_variable(value: Any) -> bool:
    """True for a string literal that contains a {{var}} placeholder as
    part of a longer value ('%{{host}}%', 'prod-{{svc}}')."""
    return isinstance(value, Lit) and isinstance(value.value, str) \
        and _VAR_RE.search(value.value) is not None


def var_aware_regex(raw: str, cfg: Dict[str, Any], like: bool = False) -> str:
    """A literal embedding {{var}} placeholders -> regex: literal segments
    are escaped (LIKE wildcards translated when ``like``) and each variable
    becomes ${var:regex}, so Grafana escapes the selected value and a
    multi-value selection still matches."""
    out: List[str] = []
    pos = 0
    for m in _VAR_RE.finditer(raw):
        seg = raw[pos:m.start()]
        out.append(like_to_regex(seg) if like else regex_escape(seg))
        out.append("${%s:regex}" % grafana_var(m.group(1), cfg))
        pos = m.end()
    seg = raw[pos:]
    out.append(like_to_regex(seg) if like else regex_escape(seg))
    return "".join(out)


@dataclass
class Matcher:
    label: str
    op: str    # '=', '!=', '=~', '!~'
    value: str  # already regex/plain, NOT quoted

    def render(self) -> str:
        return "%s%s%s" % (self.label, self.op, q(self.value))


def _lit_str(v: Any) -> str:
    if isinstance(v, Lit):
        if isinstance(v.value, bool):
            return "true" if v.value else "false"
        if isinstance(v.value, float) and v.value == int(v.value):
            return str(int(v.value))
        return str(v.value)
    if isinstance(v, Attr):
        return v.name
    return str(v)


# Functions in WHERE / FACET that merely reshape an attribute; the label
# underneath is what matters. Value in the table: whether the function
# lowercases/uppercases (=> case-insensitive matching is required).
_UNWRAP_FUNCS = {
    "numeric": False, "string": False, "cast": False, "tostring": False,
    "tonumeric": False, "todatetime": False, "getfield": False,
    "tolower": True, "lower": True, "toupper": True, "upper": True,
}

MAX_OR_BRANCHES = 8


def unwrap_attr(expr: Any) -> Tuple[Optional[Attr], bool]:
    """(Attr, case_insensitive) for an Attr or a reshaping function of one;
    (None, False) when the expression is something else."""
    if isinstance(expr, Attr):
        return expr, False
    if isinstance(expr, Func) and expr.name in _UNWRAP_FUNCS and expr.args:
        inner, ci = unwrap_attr(expr.args[0])
        return inner, ci or _UNWRAP_FUNCS[expr.name]
    return None, False


def cond_to_matchers(cond: Optional[Cond], cfg: Dict[str, Any],
                     t: Translation) -> List[Matcher]:
    """Flatten a WHERE condition into ANDed label matchers.

    A top-level OR across different attributes cannot be one selector; it
    is reported and dropped here (translators that support unions call
    :func:`cond_to_branches` instead).
    """
    branches = cond_to_branches(cond, cfg, t)
    if len(branches) == 1:
        return branches[0]
    t.note("an OR across different attributes cannot be expressed as one "
           "label selector for this target; that OR clause was DROPPED — "
           "verify filter logic", NEEDS_REVIEW)
    return []


def cond_to_branches(cond: Optional[Cond], cfg: Dict[str, Any],
                     t: Translation) -> List[List[Matcher]]:
    """WHERE -> disjunctive normal form: a list of alternative matcher
    conjunctions (usually one). ``(a AND b) OR c`` -> ``[[a, b], [c]]``.
    Same-attribute ORs merge into one regex matcher first, so the common
    ``x = 'a' OR x = 'b'`` stays a single branch."""
    if cond is None:
        return [[]]
    dnf = _dnf(cond, cfg, False)
    if dnf is None:
        t.note("WHERE expands to more than %d OR alternatives; the OR "
               "clause was DROPPED — simplify the filter" % MAX_OR_BRANCHES,
               NEEDS_REVIEW)
        return [[]]
    multi = len(dnf) > 1
    out: List[List[Matcher]] = []
    for branch in dnf:
        matchers: List[Matcher] = []
        for leaf, negate in branch:
            if isinstance(leaf, Matcher):
                matchers.append(leaf)
                continue
            matchers.extend(_leaf_matchers(leaf, negate, cfg, t,
                                           allow_numeric=not multi))
        key = [(m.label, m.op, m.value) for m in matchers]
        if key not in [[(m.label, m.op, m.value) for m in b] for b in out]:
            out.append(matchers)
    return out or [[]]


def _dnf(cond: Cond, cfg: Dict[str, Any], negate: bool) \
        -> Optional[List[List[Tuple[Any, bool]]]]:
    if isinstance(cond, NotOp):
        return _dnf(cond.item, cfg, not negate)
    if isinstance(cond, BoolOp):
        op = cond.op if not negate else ("or" if cond.op == "and" else "and")
        if op == "or":
            merged = _try_merge_or(cond, cfg, negate)
            if merged is not None:
                return [[(merged, False)]]
        subs = [_dnf(i, cfg, negate) for i in cond.items]
        if any(s is None for s in subs):
            return None
        if op == "or":
            out = [b for s in subs for b in s]  # type: ignore[union-attr]
        else:
            out = [[]]
            for s in subs:
                out = [a + b for a in out for b in s]  # type: ignore[union-attr]
                if len(out) > MAX_OR_BRANCHES:
                    return None
        if len(out) > MAX_OR_BRANCHES:
            return None
        return out
    return [[(cond, negate)]]


def _try_merge_or(cond: BoolOp, cfg: Dict[str, Any],
                  negate: bool) -> Optional[Matcher]:
    """OR of simple predicates on the SAME attribute -> one regex matcher.
    (appName='a' OR appName='b') -> job=~"a|b". Returns None if the OR
    spans different attributes or non-mergeable predicate shapes."""
    label: Optional[str] = None
    parts: List[str] = []
    for item in cond.items:
        if isinstance(item, Cmp) and item.op in ("=", "LIKE", "RLIKE"):
            attr, ci = unwrap_attr(item.left)
            if attr is None:
                return None
            l, _ = map_attr(attr.name, cfg)
            val = item.right
            if is_nr_variable(val) or has_embedded_variable(val) \
                    or isinstance(val, (Attr, Func, BinOp)):
                return None
            raw = _lit_str(val)
            if item.op == "=":
                parts.append(("(?i:%s)" % regex_escape(raw)) if ci
                             else regex_escape(raw))
            elif item.op == "LIKE":
                # NRQL LIKE is case-insensitive; scope the flag to this
                # alternative so it doesn't leak across the union.
                parts.append("(?i:%s)" % like_to_regex(raw))
            else:
                parts.append("(?:%s)" % raw)
        elif isinstance(item, InList) and not item.negated:
            attr, ci = unwrap_attr(item.left)
            if attr is None:
                return None
            l, _ = map_attr(attr.name, cfg)
            if any(is_nr_variable(v) or has_embedded_variable(v)
                   for v in item.values):
                return None
            parts.extend(regex_escape(_lit_str(v)) for v in item.values)
        else:
            return None
        if label is None:
            label = l
        elif label != l:
            return None
    if label is None or not parts:
        return None
    return Matcher(label, "!~" if negate else "=~", "|".join(parts))


def _leaf_matchers(cond: Any, negate: bool, cfg: Dict[str, Any],
                   t: Translation, allow_numeric: bool = True) \
        -> List[Matcher]:
    if isinstance(cond, Cmp) and isinstance(cond.left, Lit) \
            and isinstance(cond.right, Lit):
        # WHERE true / 1 = 1: a constant predicate.
        same = cond.left.value == cond.right.value
        truth = same if cond.op == "=" else (not same if cond.op == "!="
                                              else None)
        if truth is not None and negate:
            truth = not truth
        if truth is False:
            t.note("a WHERE condition is always false (%s); no data can match"
                   % cond_text(cond), NEEDS_REVIEW)
        return []
    if isinstance(cond, Cmp):
        if cond.op in ("<", "<=", ">", ">=") and isinstance(cond.right, Lit) \
                and isinstance(cond.right.value, str) \
                and re.fullmatch(r"-?\d+(\.\d+)?", cond.right.value.strip()):
            # status >= '500': a numeric comparison spelled as a string
            cond = Cmp(cond.left, cond.op, Lit(float(cond.right.value)))
        return _cmp_to_matcher(cond, cfg, t, negate, allow_numeric)
    if isinstance(cond, InList):
        neg = cond.negated != negate
        label, mapped, ci = _left_label(cond.left, cfg, t)
        if label is None:
            return []
        var = None
        if len(cond.values) == 1:
            var = is_nr_variable(cond.values[0])
        if var:
            # IN ({{var}}) -> multi-value Grafana variable regex match
            return [Matcher(label, "!~" if neg else "=~",
                            "${%s:regex}" % grafana_var(var, cfg))]
        alt = "|".join(
            var_aware_regex(_lit_str(v), cfg) if has_embedded_variable(v)
            else regex_escape(_lit_str(v)) for v in cond.values)
        if ci:
            alt = "(?i)" + alt
        return [Matcher(label, "!~" if neg else "=~", alt)]
    if isinstance(cond, NullCheck):
        label, _, _ = _left_label(cond.left, cfg, t)
        if label is None:
            return []
        # IS NULL -> label absent; IS NOT NULL -> label present
        present = cond.negated != negate
        return [Matcher(label, "!=" if present else "=", "")]
    t.note("unsupported WHERE construct dropped: %r" % (cond,), NEEDS_REVIEW)
    return []


def _left_label(left: Any, cfg: Dict[str, Any], t: Translation,
                quiet: bool = False) -> Tuple[Optional[str], bool, bool]:
    """-> (label or None when it cannot be a matcher, mapped, case_insensitive)."""
    attr, ci = unwrap_attr(left)
    if attr is not None:
        label, mapped = map_attr(attr.name, cfg)
        if not mapped and not quiet:
            t.note("attribute %r not in label_map; used %r — verify the label "
                   "exists in your stack" % (attr.name, label), NEEDS_REVIEW)
        return label, mapped, ci
    if isinstance(left, Func):
        t.note("function %s(...) in WHERE cannot become a label matcher; "
               "dropped" % left.name, NEEDS_REVIEW)
        return None, False, False
    if isinstance(left, BinOp):
        t.note("arithmetic in WHERE (%s) cannot become a label matcher; "
               "dropped" % expr_text(left), NEEDS_REVIEW)
        return None, False, False
    t.note("WHERE predicate on %r cannot become a label matcher; dropped"
           % _lit_str(left), NEEDS_REVIEW)
    return None, False, False


def _fold_arith(left: Any, op: str, value: float) \
        -> Optional[Tuple[Any, str, float]]:
    """(duration * 1000) > 500 -> (duration, '>', 0.5)."""
    if not isinstance(left, BinOp):
        return left, op, value
    if isinstance(left.right, Lit) and isinstance(left.right.value, (int, float)) \
            and not isinstance(left.right.value, bool):
        k = float(left.right.value)
        if left.op == "*" and k != 0:
            return _fold_arith(left.left, op, value / k)
        if left.op == "/" and k != 0:
            return _fold_arith(left.left, op, value * k)
        if left.op == "+":
            return _fold_arith(left.left, op, value - k)
        if left.op == "-":
            return _fold_arith(left.left, op, value + k)
    if isinstance(left.left, Lit) and isinstance(left.left.value, (int, float)) \
            and not isinstance(left.left.value, bool) and left.op == "*":
        k = float(left.left.value)
        if k != 0:
            return _fold_arith(left.right, op, value / k)
    return None


def _cmp_to_matcher(cmp_: Cmp, cfg: Dict[str, Any], t: Translation,
                    negate: bool, allow_numeric: bool = True) -> List[Matcher]:
    op = cmp_.op
    val = cmp_.right
    left = cmp_.left

    def flip(o: str) -> str:
        return {"=": "!=", "!=": "=", "=~": "!~", "!~": "=~"}[o]

    if op in ("<", "<=", ">", ">=") and isinstance(val, Lit) \
            and isinstance(val.value, (int, float)) \
            and not isinstance(val.value, bool):
        folded = _fold_arith(left, op, float(val.value))
        if folded is None:
            t.note("arithmetic in WHERE (%s) could not be folded into a "
                   "simple comparison; dropped" % expr_text(left),
                   NEEDS_REVIEW)
            return []
        left, op, num = folded
        # Numeric predicates are not label matchers; whoever consumes (or
        # drops) them explains, so no label_map note here.
        label, _, _ = _left_label(left, cfg, t, quiet=True)
        if label is None:
            return []
        if negate:
            op = {"<": ">=", "<=": ">", ">": "<=", ">=": "<"}[op]
        # Numeric comparisons on labels are strings in Prom; special-case
        # http status classes, else hand over to the translator.
        m = _status_class_matcher(label, op, num)
        if m:
            return [m]
        attr, _ = unwrap_attr(left)
        if allow_numeric:
            t.numeric.append(NumericPred(label, attr.name if attr else label,
                                         op, num))
        else:
            t.note("numeric comparison %s %s %s inside an OR cannot become "
                   "a label matcher; dropped — apply it manually"
                   % (label, op, _fmt(num)), NEEDS_REVIEW)
        return []

    if op in ("<", "<=", ">", ">=") and is_nr_variable(val):
        attr0, _ = unwrap_attr(left)
        t.note("comparison %s %s {{%s}} takes its threshold from a dashboard "
               "variable; a metric bucket or threshold cannot be chosen at "
               "conversion time — hard-code the value (at a histogram bucket "
               "boundary) or use a Grafana threshold line instead; dropped"
               % (attr0.name if attr0 is not None else expr_text(left), op,
                  is_nr_variable(val)), NEEDS_REVIEW)
        return []
    label, _, ci = _left_label(left, cfg, t)
    if label is None:
        return []
    var = is_nr_variable(val)
    if var:
        var = grafana_var(var, cfg)
    if not var and isinstance(val, Attr):
        t.note("comparison of two attributes (%s %s %s) cannot become a "
               "label matcher; dropped — verify the intent"
               % (label, op, val.name), NEEDS_REVIEW)
        return []
    if not var and isinstance(val, (Func, BinOp)):
        t.note("comparison against an expression (%s %s %s) cannot become "
               "a label matcher; dropped" % (label, op, expr_text(val)),
               NEEDS_REVIEW)
        return []
    raw = "$%s" % var if var else _lit_str(val)
    embedded = not var and has_embedded_variable(val)

    if op in ("=", "!="):
        m_op = "=" if op == "=" else "!="
        if negate:
            m_op = flip(m_op)
        if var:
            # Grafana multi-value vars need regex matching.
            return [Matcher(label, "=~" if m_op == "=" else "!~",
                            "${%s:regex}" % var)]
        if embedded:
            # 'prod-{{svc}}': the variable part must stay a variable.
            return [Matcher(label, "=~" if m_op == "=" else "!~",
                            ("(?i)" if ci else "") + var_aware_regex(raw, cfg))]
        if ci:
            return [Matcher(label, "=~" if m_op == "=" else "!~",
                            "(?i)" + regex_escape(raw))]
        return [Matcher(label, m_op, raw)]
    if op in ("LIKE", "NOT LIKE"):
        m_op = "=~" if op == "LIKE" else "!~"
        if negate:
            m_op = flip(m_op)
        # NRQL LIKE is case-insensitive; RE2 needs an explicit flag.
        if var:
            pattern = "${%s:regex}" % var
        elif embedded:
            # '%{{host}}%' -> .*${host:regex}.* (the braces are not text).
            pattern = "(?i)" + var_aware_regex(raw, cfg, like=True)
        else:
            pattern = "(?i)" + like_to_regex(raw)
        return [Matcher(label, m_op, pattern)]
    if op in ("RLIKE", "NOT RLIKE"):
        m_op = "=~" if op == "RLIKE" else "!~"
        if negate:
            m_op = flip(m_op)
        if embedded:
            raw = _VAR_RE.sub(
                lambda m: "${%s:regex}" % grafana_var(m.group(1), cfg), raw)
        return [Matcher(label, m_op, ("(?i)" + raw) if ci else raw)]
    if op in ("<", "<=", ">", ">="):
        t.note("comparison %s %s %s is not numeric; dropped"
               % (label, op, raw), NEEDS_REVIEW)
        return []
    t.note("operator %r unsupported in WHERE; dropped" % op, NEEDS_REVIEW)
    return []


def _fmt(n: float) -> str:
    return str(int(n)) if n == int(n) else ("%f" % n).rstrip("0").rstrip(".")


def _status_class_matcher(label: str, op: str, n: float) -> Optional[Matcher]:
    if label not in ("http_response_status_code", "http_status_code",
                     "status_code", "http_status"):
        return None
    if n != int(n):
        return None
    n = int(n)
    ranges = {(">=", 400): "4..|5..", (">=", 500): "5..",
              (">", 399): "4..|5..", (">", 499): "5..",
              ("<", 400): "[123]..", ("<", 500): "[1234]..",
              ("<=", 399): "[123]..", ("<=", 499): "[1234]..",
              ("<", 300): "[12]..", ("<=", 299): "[12]..",
              (">=", 300): "[345]..", (">", 299): "[345]..",
              (">=", 200): "[2345]..", ("<", 200): "1..",
              (">=", 100): ".*"}
    pattern = ranges.get((op, n))
    if not pattern:
        return None
    return Matcher(label, "=~", pattern)


def render_selector(metric: str, matchers: List[Matcher]) -> str:
    inner = ",".join(m.render() for m in matchers)
    if metric:
        return "%s{%s}" % (metric, inner) if inner else metric
    return "{%s}" % inner


# Time-bucketing FACET functions and what to do instead in Grafana.
_TIME_FACETS = {
    "hourof": "1h", "minuteof": "1m", "dateof": "1d", "dayof": "1d",
    "weekof": "1w", "weekdayof": "1d", "monthof": "30d", "yearof": "365d",
}

_NAMED_GROUP_RE = re.compile(r"\(\?P?<([A-Za-z_][A-Za-z0-9_]*)>")


def _capture_group(regex: str) -> Tuple[str, str]:
    """(label name, $N reference) for the named group in a capture() regex."""
    name = "capture"
    m = _NAMED_GROUP_RE.search(regex)
    if m:
        name = m.group(1)
        # Index of this group among capturing groups.
        idx = 0
        i = 0
        while i < m.start():
            if regex[i] == "\\":
                i += 2
                continue
            if regex[i] == "(":
                if regex.startswith("(?", i) and not (
                        regex.startswith("(?P<", i) or regex.startswith("(?<", i)):
                    pass  # non-capturing / flags group
                else:
                    idx += 1
            i += 1
        return name, "$%d" % (idx + 1)
    return name, "$1"


def facet_labels(query: NrqlQuery, cfg: Dict[str, Any],
                 t: Translation) -> List[str]:
    labels: List[str] = []
    legend_parts: List[str] = []
    use_template = False
    for item in query.facet:
        attr, _ci = unwrap_attr(item.expr)
        if attr is not None:
            label, mapped = map_attr(attr.name, cfg)
            if not mapped:
                t.note("FACET attribute %r not in label_map; used %r"
                       % (attr.name, label), NEEDS_REVIEW)
            labels.append(label)
            legend_parts.append("{{%s}}" % label)
            continue
        fn = item.expr if isinstance(item.expr, Func) else None
        if fn is None:
            t.note("unsupported FACET expression dropped", NEEDS_REVIEW)
            continue
        if fn.name == "concat":
            use_template = True
            for a in fn.args:
                sub, _ = unwrap_attr(a)
                if sub is not None:
                    label, mapped = map_attr(sub.name, cfg)
                    if not mapped:
                        t.note("FACET attribute %r not in label_map; used %r"
                               % (sub.name, label), NEEDS_REVIEW)
                    labels.append(label)
                    legend_parts.append("{{%s}}" % label)
                elif isinstance(a, Lit):
                    legend_parts.append(str(a.value))
            t.note("FACET concat(...) grouped by each attribute; the legend "
                   "joins them the way concat did", APPROXIMATE)
            continue
        if fn.name in ("capture", "aparse") and len(fn.args) >= 2 \
                and isinstance(fn.args[1], Lit):
            sub, _ = unwrap_attr(fn.args[0])
            if sub is None:
                t.note("FACET %s(...) on a non-attribute dropped" % fn.name,
                       NEEDS_REVIEW)
                continue
            src, mapped = map_attr(sub.name, cfg)
            if not mapped:
                t.note("FACET attribute %r not in label_map; used %r"
                       % (sub.name, src), NEEDS_REVIEW)
            pattern = str(fn.args[1].value)
            if fn.name == "aparse":
                # NR anchor-parse: * captures, % matches without capturing.
                rx = "".join("(.*)" if ch == "*" else (".*" if ch == "%"
                                                       else regex_escape(ch))
                             for ch in pattern)
                new, ref = "aparse", "$1"
            else:
                rx = pattern
                new, ref = _capture_group(rx)
            t.label_replace.append((new, ref, src, rx))
            labels.append(new)
            legend_parts.append("{{%s}}" % new)
            t.note("FACET %s(%s, ...) became label_replace(...) deriving "
                   "label %r from %r with the regex; verify the pattern "
                   "matches the whole label value (PromQL anchors it)"
                   % (fn.name, sub.name, new, src), APPROXIMATE)
            continue
        if fn.name in _TIME_FACETS:
            t.note("FACET %s(...) buckets by time; Grafana does this with "
                   "TIMESERIES-style range queries — use a %s interval / "
                   "Min interval on the panel instead of a grouping"
                   % (fn.name, _TIME_FACETS[fn.name]), NEEDS_REVIEW)
            continue
        if fn.name == "buckets":
            t.note("FACET buckets(...) (numeric bucketing) has no label "
                   "equivalent; grouping dropped — a Grafana histogram "
                   "panel over the same metric is the closest match",
                   NEEDS_REVIEW)
            continue
        if fn.name in ("cases", "if"):
            t.note("FACET %s(...) needs one filtered query per case, which "
                   "this target does not support; grouping dropped"
                   % fn.name, NEEDS_REVIEW)
            continue
        t.note("FACET %s(...) has no label equivalent; grouping dropped"
               % fn.name, NEEDS_REVIEW)
    if use_template and legend_parts:
        t.legend_template = "".join(legend_parts)
    return labels


def expr_text(node: Any) -> str:
    """Human-readable rendering of a SELECT/WHERE expression."""
    if isinstance(node, BinOp):
        return "%s %s %s" % (expr_text(node.left), node.op,
                             expr_text(node.right))
    if isinstance(node, Func):
        if node.name == "_ratio" and len(node.args) == 2:
            return "%s / %s" % (expr_text(node.args[0]),
                                expr_text(node.args[1]))
        if node.name == "_arith" and len(node.args) == 3:
            return "(%s %s %s)" % (expr_text(node.args[1]),
                                   _lit_str(node.args[0]),
                                   expr_text(node.args[2]))
        inner = ", ".join(expr_text(a) for a in node.args)
        if node.where is not None:
            inner += (", " if inner else "") + "WHERE " + cond_text(node.where)
        return "%s(%s)" % (node.name, inner)
    if isinstance(node, Lit):
        return _lit_str(node)
    if isinstance(node, Attr):
        return node.name
    return "*" if node.__class__.__name__ == "Star" else str(node)


def cond_text(cond: Optional[Cond]) -> str:
    """Human-readable rendering of a WHERE condition (legends, notes)."""
    if cond is None:
        return ""
    if isinstance(cond, BoolOp):
        joiner = " %s " % cond.op.upper()
        return "(" + joiner.join(cond_text(c) for c in cond.items) + ")"
    if isinstance(cond, NotOp):
        return "NOT %s" % cond_text(cond.item)
    if isinstance(cond, Cmp):
        return "%s %s %s" % (expr_text(cond.left), cond.op,
                             expr_text(cond.right))
    if isinstance(cond, InList):
        return "%s %sIN (%s)" % (expr_text(cond.left),
                                 "NOT " if cond.negated else "",
                                 ", ".join(expr_text(v) for v in cond.values))
    if isinstance(cond, NullCheck):
        return "%s IS %sNULL" % (expr_text(cond.left),
                                 "NOT " if cond.negated else "")
    return repr(cond)


def legend_for(labels: List[str], alias: Optional[str] = None,
               template: str = "") -> str:
    if template:
        return template
    if labels:
        return " / ".join("{{%s}}" % l for l in labels)
    return alias or ""
