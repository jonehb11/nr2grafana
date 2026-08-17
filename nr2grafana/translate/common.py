"""Shared translation machinery: routing, matcher rendering, confidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..nrql.parser import (
    Attr, BoolOp, Cmp, Cond, Func, InList, Lit, NotOp, NrqlQuery, NullCheck,
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


def route_event_type(event_types: List[str]) -> str:
    """Decide the target family for a FROM clause: metrics|logs|traces."""
    if not event_types:
        return "metrics"
    et = event_types[0].lower()
    if et in LOG_EVENT_TYPES:
        return "logs"
    if et in SPAN_EVENT_TYPES:
        return "traces"
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
    # Strip common NR prefixes then retry.
    for prefix in ("tags.", "attributes.", "resource."):
        if name.startswith(prefix) and name[len(prefix):] in label_map:
            return label_map[name[len(prefix):]], True
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
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(regex_escape(ch))
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


def cond_to_matchers(cond: Optional[Cond], cfg: Dict[str, Any],
                     t: Translation) -> List[Matcher]:
    """Flatten a WHERE condition into ANDed label matchers.

    OR at top level between different labels, arithmetic, and other
    non-conjunctive shapes cannot be label matchers; they degrade the
    confidence and are noted (the widget still converts).
    """
    if cond is None:
        return []
    matchers: List[Matcher] = []
    _walk_cond(cond, cfg, t, matchers, negate=False)
    return matchers


def _try_merge_or(cond: BoolOp, cfg: Dict[str, Any],
                  negate: bool) -> Optional[Matcher]:
    """OR of simple predicates on the SAME attribute -> one regex matcher.
    (appName='a' OR appName='b') -> job=~"a|b". Returns None if the OR
    spans different attributes or non-mergeable predicate shapes."""
    label: Optional[str] = None
    parts: List[str] = []
    for item in cond.items:
        if isinstance(item, Cmp) and isinstance(item.left, Attr) \
                and item.op in ("=", "LIKE", "RLIKE"):
            l, _ = map_attr(item.left.name, cfg)
            val = item.right
            if is_nr_variable(val):
                return None
            raw = _lit_str(val)
            if item.op == "=":
                parts.append(regex_escape(raw))
            elif item.op == "LIKE":
                # NRQL LIKE is case-insensitive; scope the flag to this
                # alternative so it doesn't leak across the union.
                parts.append("(?i:%s)" % like_to_regex(raw))
            else:
                parts.append("(?:%s)" % raw)
        elif isinstance(item, InList) and isinstance(item.left, Attr) \
                and not item.negated:
            l, _ = map_attr(item.left.name, cfg)
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


def _walk_cond(cond: Cond, cfg: Dict[str, Any], t: Translation,
               out: List[Matcher], negate: bool) -> None:
    if isinstance(cond, BoolOp):
        if cond.op == "or" and not negate:
            merged = _try_merge_or(cond, cfg, negate=False)
            if merged is not None:
                out.append(merged)
                return
            t.note("an OR clause in WHERE could not be merged into a single "
                   "label matcher (different attributes, variables, or "
                   "mixed predicate shapes); that clause was DROPPED — "
                   "verify filter logic", NEEDS_REVIEW)
            return
        if cond.op == "and" and negate:
            # NOT (a AND b) = NOT a OR NOT b — an OR in disguise.
            t.note("negated AND in WHERE cannot become label matchers; "
                   "clause dropped — verify filter logic", NEEDS_REVIEW)
            return
        for item in cond.items:
            _walk_cond(item, cfg, t, out, negate)
        return
    if isinstance(cond, NotOp):
        _walk_cond(cond.item, cfg, t, out, not negate)
        return
    if isinstance(cond, Cmp):
        out.extend(_cmp_to_matcher(cond, cfg, t, negate))
        return
    if isinstance(cond, InList):
        neg = cond.negated != negate
        label, mapped = _left_label(cond.left, cfg, t)
        var = None
        if len(cond.values) == 1:
            var = is_nr_variable(cond.values[0])
        if var:
            # IN ({{var}}) -> multi-value Grafana variable regex match
            out.append(Matcher(label, "!~" if neg else "=~",
                               "${%s:regex}" % grafana_var(var, cfg)))
            return
        alt = "|".join(regex_escape(_lit_str(v)) for v in cond.values)
        out.append(Matcher(label, "!~" if neg else "=~", alt))
        return
    if isinstance(cond, NullCheck):
        label, _ = _left_label(cond.left, cfg, t)
        # IS NULL -> label absent; IS NOT NULL -> label present
        present = cond.negated != negate
        out.append(Matcher(label, "!=" if present else "=", ""))
        return
    t.note("unsupported WHERE construct dropped: %r" % (cond,), NEEDS_REVIEW)


def _left_label(left: Any, cfg: Dict[str, Any], t: Translation) -> Tuple[str, bool]:
    if isinstance(left, Attr):
        label, mapped = map_attr(left.name, cfg)
        if not mapped:
            t.note("attribute %r not in label_map; used %r — verify the label "
                   "exists in your stack" % (left.name, label), NEEDS_REVIEW)
        return label, mapped
    if isinstance(left, Func):
        t.note("function %s(...) in WHERE cannot become a label matcher; "
               "dropped" % left.name, NEEDS_REVIEW)
        return sanitize_label(left.name), False
    return sanitize_label(_lit_str(left)), False


def _cmp_to_matcher(cmp_: Cmp, cfg: Dict[str, Any], t: Translation,
                    negate: bool) -> List[Matcher]:
    label, _ = _left_label(cmp_.left, cfg, t)
    op = cmp_.op
    val = cmp_.right
    var = is_nr_variable(val)
    if var:
        var = grafana_var(var, cfg)
    raw = "$%s" % var if var else _lit_str(val)

    def flip(o: str) -> str:
        return {"=": "!=", "!=": "=", "=~": "!~", "!~": "=~"}[o]

    if op in ("=", "!="):
        m_op = "=" if op == "=" else "!="
        if negate:
            m_op = flip(m_op)
        if var:
            # Grafana multi-value vars need regex matching.
            return [Matcher(label, "=~" if m_op == "=" else "!~",
                            "${%s:regex}" % var)]
        return [Matcher(label, m_op, raw)]
    if op in ("LIKE", "NOT LIKE"):
        m_op = "=~" if op == "LIKE" else "!~"
        if negate:
            m_op = flip(m_op)
        # NRQL LIKE is case-insensitive; RE2 needs an explicit flag.
        pattern = ("(?i)" + like_to_regex(raw)) if not var \
            else "${%s:regex}" % var
        return [Matcher(label, m_op, pattern)]
    if op in ("RLIKE", "NOT RLIKE"):
        m_op = "=~" if op == "RLIKE" else "!~"
        if negate:
            m_op = flip(m_op)
        return [Matcher(label, m_op, raw)]
    if op in ("<", "<=", ">", ">="):
        # Numeric comparisons on labels are strings in Prom; special-case
        # http status classes, else flag.
        m = _status_class_matcher(label, op, val, negate)
        if m:
            return [m]
        t.note("numeric comparison %s %s %s cannot become a label matcher; "
               "dropped — apply it manually" % (label, op, raw), NEEDS_REVIEW)
        return []
    t.note("operator %r unsupported in WHERE; dropped" % op, NEEDS_REVIEW)
    return []


def _status_class_matcher(label: str, op: str, val: Any,
                          negate: bool) -> Optional[Matcher]:
    if label not in ("http_response_status_code", "http_status_code") \
            or not isinstance(val, Lit):
        return None
    try:
        n = int(val.value)
    except (TypeError, ValueError):
        return None
    ranges = {(">=", 400): "4..|5..", (">=", 500): "5..",
              (">", 399): "4..|5..", (">", 499): "5..",
              ("<", 400): "[123]..", ("<", 500): "[1234]..",
              ("<=", 399): "[123]..", ("<=", 499): "[1234]..", }
    pattern = ranges.get((op, n))
    if not pattern:
        return None
    return Matcher(label, "!~" if negate else "=~", pattern)


def render_selector(metric: str, matchers: List[Matcher]) -> str:
    inner = ",".join(m.render() for m in matchers)
    if metric:
        return "%s{%s}" % (metric, inner) if inner else metric
    return "{%s}" % inner


def facet_labels(query: NrqlQuery, cfg: Dict[str, Any],
                 t: Translation) -> List[str]:
    labels: List[str] = []
    for item in query.facet:
        if isinstance(item.expr, Attr):
            label, mapped = map_attr(item.expr.name, cfg)
            if not mapped:
                t.note("FACET attribute %r not in label_map; used %r"
                       % (item.expr.name, label), NEEDS_REVIEW)
            labels.append(label)
        elif isinstance(item.expr, Func):
            t.note("FACET %s(...) has no label equivalent; grouping dropped"
                   % item.expr.name, NEEDS_REVIEW)
        else:
            t.note("unsupported FACET expression dropped", NEEDS_REVIEW)
    return labels


def legend_for(labels: List[str], alias: Optional[str] = None) -> str:
    if labels:
        return " / ".join("{{%s}}" % l for l in labels)
    return alias or ""
