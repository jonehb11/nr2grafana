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
    """Raised when a query genuinely cannot be expressed in the target.

    ``closest_equivalent`` (optional) is a SEAM-REPORT dict
    {datasource, example_query, note} describing the nearest thing the
    LGTM stack offers, so an untranslatable widget can become an honest
    [MANUAL] placeholder instead of a dead end.
    """

    def __init__(self, message: str,
                 closest_equivalent: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.closest_equivalent = closest_equivalent


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
    # SEAM-REPORT fields (all optional; the builder copies them into the
    # widget report when present).
    metric_kind: str = ""              # counter|gauge|histogram|summary
    vars: List[str] = field(default_factory=list)  # Grafana vars used
    closest_equivalent: Optional[Dict[str, Any]] = None
    k8s_mapped: bool = False

    def note(self, msg: str, confidence: Optional[str] = None) -> None:
        if msg not in self.notes:
            self.notes.append(msg)
        if confidence:
            self.confidence = worst(self.confidence, confidence)

    def use_var(self, name: str) -> None:
        if name and name not in self.vars:
            self.vars.append(name)


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
# SEAM-RENDER: NRQL value node -> Grafana-ready text
# ---------------------------------------------------------------------------

# A rendered Grafana variable reference: $env, ${env}, ${env:regex}.
_GVAR_RE = re.compile(r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*(?::[a-z]+)?\}"
                      r"|[A-Za-z_][A-Za-z0-9_]*)")

# AST dataclass reprs. One of these inside an emitted query means a value
# node was stringified instead of rendered (failure class F1).
_REPR_RE = re.compile(
    r"\b(?:Func|Lit|Attr|Cmp|InList|BoolOp|NotOp|NullCheck|Star|SelectItem"
    r"|FacetItem)\(")


def _gvar(name: str, cfg: Dict[str, Any], tail: str = "") -> str:
    """$name, or ${name} when the following text would blend into the
    variable name (concat({{env}}, 'x') must not render as $envx)."""
    gname = grafana_var(name, cfg)
    if tail and (tail[0].isalnum() or tail[0] == "_"):
        return "${%s}" % gname
    return "$" + gname


def _render_text(text: str, cfg: Dict[str, Any]) -> Tuple[str, str]:
    names = _VAR_RE.findall(text)
    if not names:
        return text, "literal"
    if _VAR_RE.fullmatch(text.strip()):
        return "$" + grafana_var(names[0], cfg), "var"
    out: List[str] = []
    pos = 0
    for m in _VAR_RE.finditer(text):
        out.append(text[pos:m.start()])
        out.append(_gvar(m.group(1), cfg, text[m.end():m.end() + 1]))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), "mixed"


def render_value(node: Any, cfg: Optional[Dict[str, Any]] = None
                 ) -> Tuple[str, str]:
    """Render a NRQL value node to Grafana-ready text -> (text, kind).

    Lit -> its text ("literal"); {{var}} (as Attr or Lit) -> "$var"
    ("var"); concat(...) of those -> the joined text ("mixed" when a
    variable is involved, "literal" otherwise): concat('p-', {{env}}) ->
    "p-$env". lower()/upper() of a literal are applied. Anything else
    (an unknown function, SELECT *) returns a readable rendering with kind
    "unsupported" so the caller can drop it with a note. Never returns a
    Python repr.
    """
    cfg = cfg or {}
    if isinstance(node, Lit):
        if isinstance(node.value, str):
            return _render_text(node.value, cfg)
        return _lit_str(node), "literal"
    if isinstance(node, Attr):
        return _render_text(node.name, cfg)
    if isinstance(node, Func):
        if node.name == "concat":
            parts: List[str] = []
            kinds = set()
            args = list(node.args)
            for i, a in enumerate(args):
                txt, kind = render_value(a, cfg)
                if kind == "unsupported":
                    return txt, "unsupported"
                if kind == "var" and i + 1 < len(args):
                    nxt, _ = render_value(args[i + 1], cfg)
                    txt = _gvar(txt[1:], {}, nxt)
                parts.append(txt)
                kinds.add(kind)
            if not parts:
                return "", "literal"
            if kinds == {"literal"}:
                return "".join(parts), "literal"
            if kinds == {"var"} and len(parts) == 1:
                return parts[0], "var"
            return "".join(parts), "mixed"
        if node.name in ("lower", "tolower", "upper", "toupper") \
                and len(node.args) == 1:
            txt, kind = render_value(node.args[0], cfg)
            if kind == "literal":
                return (txt.lower() if "lower" in node.name
                        else txt.upper()), "literal"
            return "%s(%s)" % (node.name, txt), "unsupported"
        inner = ", ".join(render_value(a, cfg)[0] for a in node.args)
        return "%s(%s)" % (node.name, inner), "unsupported"
    if node is None:
        return "", "literal"
    if isinstance(node, (str, int, float, bool)):
        return _lit_str(Lit(node)), "literal"
    return getattr(node, "name", node.__class__.__name__), "unsupported"


def value_vars(node: Any, cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """Grafana variable names referenced by a value node (renamed)."""
    cfg = cfg or {}
    text = ""
    if isinstance(node, Lit) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, Attr):
        text = node.name
    elif isinstance(node, Func):
        out: List[str] = []
        for a in node.args:
            for v in value_vars(a, cfg):
                if v not in out:
                    out.append(v)
        return out
    return [grafana_var(n, cfg) for n in nr_var_names(text)]


def regex_escape_keep_vars(text: str) -> str:
    """regex_escape() that leaves rendered $var references intact so
    "p-$env" becomes "p-$env" (not "p-\\$env") inside an alternation."""
    out: List[str] = []
    pos = 0
    for m in _GVAR_RE.finditer(text):
        out.append(regex_escape(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(regex_escape(text[pos:]))
    return "".join(out)


def like_to_regex_keep_vars(pattern: str) -> str:
    out: List[str] = []
    pos = 0
    for m in _GVAR_RE.finditer(pattern):
        out.append(like_to_regex(pattern[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(like_to_regex(pattern[pos:]))
    return "".join(out)


def has_ast_repr(text: str) -> bool:
    """True when a Python AST repr (Func(/Lit(/Attr(...) leaked into text."""
    return bool(_REPR_RE.search(text or ""))


def assert_no_repr(*texts: str) -> None:
    """Hard guard against failure class F1: raise if any emitted query
    contains a Python AST repr instead of a rendered value."""
    for text in texts:
        if has_ast_repr(text):
            raise Untranslatable(
                "internal translator bug: a Python AST repr leaked into the "
                "emitted query (%s); the WHERE value was not rendered via "
                "render_value() - report this query"
                % _REPR_RE.search(text).group(0).rstrip("("))


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
    k8s = k8s_attr_label(name, label_map)
    if k8s:
        return k8s, True
    return sanitize_label(name), False


_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# NR Kubernetes-integration attribute -> kube-state-metrics/cAdvisor label
# when the generic rule would produce something else.
_K8S_ATTR_SPECIAL = {
    "cluster_name": "cluster", "namespace_name": "namespace",
    "deployment_name": "deployment", "pod_name": "pod",
    "container_name": "container", "node_name": "node",
    "daemonset_name": "daemonset", "statefulset_name": "statefulset",
    "replicaset_name": "replicaset", "cronjob_name": "cronjob",
    "job_name": "job_name", "service_name": "service",
    "host_name": "node", "image_name": "image",
}


def k8s_attr_label(name: str, label_map: Dict[str, str]) -> str:
    """SEAM-K8S attribute rule for `k8s.*` NR attributes: strip the `k8s.`
    prefix, camelCase -> snake_case, apply label_map, then `<x>Name` ->
    `<x>`. k8s.clusterName -> cluster, k8s.namespaceName -> namespace,
    k8s.deploymentName -> deployment, k8s.podName -> pod,
    k8s.containerName -> container, k8s.nodeName -> node. Returns "" when
    the name is not a k8s attribute."""
    if not name.startswith("k8s."):
        return ""
    rest = name[len("k8s."):]
    if not rest:
        return ""
    if rest in label_map:
        return label_map[rest]
    snake = _CAMEL_RE.sub("_", rest.replace(".", "_")).lower()
    snake = re.sub(r"_+", "_", snake)
    if snake in label_map:
        return label_map[snake]
    if snake in _K8S_ATTR_SPECIAL:
        return _K8S_ATTR_SPECIAL[snake]
    if snake.endswith("_name"):
        base = snake[:-len("_name")]
        return label_map.get(base, base)
    return sanitize_label(snake)


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
    # Bookkeeping filled by the OR-merge path (vars used, notes to copy).
    vars: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

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
    if isinstance(v, Func):
        # Never str() an AST node into a query (failure class F1).
        return render_value(v)[0]
    if v is None:
        return ""
    return str(v)


# Attributes carrying the application identity; NR often suffixes their
# values with the environment ("svc (prod)"), which the target label never
# carries (failure class F10).
_APP_ATTRS = {"appname", "entity.name", "entityname", "service.name",
              "servicename", "app", "application", "app.name"}
_APP_ENV_SUFFIX_RE = re.compile(r"^(.*\S)\s+\(([A-Za-z0-9_.-]+)\)$")
# 'svc ($env)' / 'svc (${env:regex})' after SEAM-RENDER of concat().
_APP_VAR_SUFFIX_RE = re.compile(
    r"^(.*\S)\s*\((\$(?:\{[A-Za-z_][A-Za-z0-9_]*(?::[a-z]+)?\}"
    r"|[A-Za-z_][A-Za-z0-9_]*))\)$")


def split_app_env(value: str) -> Tuple[str, str]:
    """'svc (prod)' -> ('svc', 'prod'); 'svc' -> ('svc', '')."""
    m = _APP_ENV_SUFFIX_RE.match(value or "")
    if not m:
        return value, ""
    return m.group(1), m.group(2)


def _is_app_attr(left: Any) -> bool:
    return isinstance(left, Attr) and left.name.lower() in _APP_ATTRS


def _app_value(left: Any, raw: str, kind: str, cfg: Dict[str, Any],
               t: Translation) -> str:
    """Strip the ' (env)' suffix from an application-identity value and
    explain where the environment went."""
    if not _is_app_attr(left):
        return raw
    env_var = cfg.get("env_var") or "env"
    if kind == "mixed":
        # concat('svc (', {{env}}, ')') rendered as 'svc ($env)': the
        # suffix is the environment the dashboard variable selects.
        m = _APP_VAR_SUFFIX_RE.match(raw)
        if not m:
            return raw
        t.note("%s = %r: the ' (%s)' suffix is a New Relic naming "
               "convention; the label value is %r and the environment is "
               "selected by the %s dashboard variable (bind it with --env "
               "or let the user pick it)"
               % (left.name, raw, m.group(2), m.group(1), m.group(2)),
               APPROXIMATE)
        t.use_var(m.group(2).strip("${}").split(":")[0])
        return m.group(1)
    if kind != "literal":
        return raw
    name, env = split_app_env(raw)
    if not env:
        return raw
    target = cfg.get("target_env") or ""
    mapped = (cfg.get("env_map") or {}).get(env) \
        if isinstance(cfg.get("env_map"), dict) else None
    hint = ("the export is pinned to env %r" % target if target
            else "pin it with --env %s (or config target_env) or let the "
                 "$%s dashboard variable select it" % (env, env_var))
    t.note("%s = %r: the ' (%s)' suffix is a New Relic naming convention; "
           "the label value is %r and the environment %r%s is not part of "
           "the label - %s"
           % (left.name, raw, env, name, env,
              " (env_map -> %r)" % mapped if mapped else "", hint),
           APPROXIMATE)
    return name


_ENTITY_GUID_ATTRS = {"entity.guid", "entityguid", "entity_guid", "guid"}


def _resolve_entity_guid(left: Any, raw: str, cfg: Dict[str, Any],
                         t: Translation) -> Optional[Matcher]:
    """entity.guid = '<GUID>' (failure class F9): GUIDs are NR-internal,
    so match on the entity NAME when live hints resolved it; otherwise
    drop the predicate and say exactly what to do."""
    hints = cfg.get("live_hints") or {}
    entities = hints.get("entities") if isinstance(hints, dict) else None
    entity = (entities or {}).get(raw) if isinstance(entities, dict) \
        else None
    default_label = cfg.get("entity_label") or "service_name"
    if isinstance(entity, dict) and entity.get("name"):
        label = entity.get("service_label") or default_label
        name = str(entity["name"])
        svc, env = split_app_env(name)
        t.note("entity.guid = %r resolved via live New Relic entity lookup "
               "to %r (%s); matched on %s=%r"
               % (raw, name, entity.get("type") or "entity", label, svc),
               APPROXIMATE)
        if env:
            t.note("entity %r carries the environment suffix ' (%s)'; it is "
                   "not part of the %s label" % (name, env, label))
        return Matcher(label, "=", svc)
    t.closest_equivalent = {
        "datasource": "prometheus",
        "example_query": '{%s="<entity name>"}' % default_label,
        "note": ("entity.guid %r is a New Relic-internal id with no label "
                 "in Mimir; filter on the entity's NAME via %s=\"<name>\". "
                 "Run `convert --live` (NerdGraph entity lookup, read-only) "
                 "to resolve it automatically." % (raw, default_label)),
    }
    t.note("entity.guid = %r cannot be matched in Mimir (GUIDs are NR-"
           "internal) and was DROPPED; closest equivalent: %s=\"<entity "
           "name>\" - run `convert --live` to resolve the GUID to its entity "
           "name, or set the label manually" % (raw, default_label),
           NEEDS_REVIEW)
    return None


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
    used_vars: List[str] = []
    t = Translation()
    for item in cond.items:
        if isinstance(item, Cmp) and isinstance(item.left, Attr) \
                and item.op in ("=", "LIKE", "RLIKE"):
            l, _ = map_attr(item.left.name, cfg)
            if item.left.name.lower() in _ENTITY_GUID_ATTRS:
                return None
            raw, kind = render_value(item.right, cfg)
            if kind == "unsupported":
                return None
            if kind == "var":
                # One multi-value variable among alternatives: use its
                # regex form so every selected value matches.
                raw = "${%s:regex}" % raw[1:]
            raw = _app_value(item.left, raw, kind, cfg, t)
            used_vars.extend(value_vars(item.right, cfg))
            if item.op == "=":
                parts.append(regex_escape_keep_vars(raw))
            elif item.op == "LIKE":
                # NRQL LIKE is case-insensitive; scope the flag to this
                # alternative so it doesn't leak across the union.
                parts.append("(?i:%s)" % like_to_regex_keep_vars(raw))
            else:
                parts.append("(?:%s)" % raw)
        elif isinstance(item, InList) and isinstance(item.left, Attr) \
                and not item.negated:
            l, _ = map_attr(item.left.name, cfg)
            for v in item.values:
                raw, kind = render_value(v, cfg)
                if kind == "unsupported":
                    return None
                if kind == "var":
                    raw = "${%s:regex}" % raw[1:]
                raw = _app_value(item.left, raw, kind, cfg, t)
                used_vars.extend(value_vars(v, cfg))
                parts.append(regex_escape_keep_vars(raw))
        else:
            return None
        if label is None:
            label = l
        elif label != l:
            return None
    if label is None or not parts:
        return None
    m = Matcher(label, "!~" if negate else "=~", "|".join(parts))
    m.vars = used_vars
    m.notes = list(t.notes)
    return m


def _walk_cond(cond: Cond, cfg: Dict[str, Any], t: Translation,
               out: List[Matcher], negate: bool) -> None:
    if isinstance(cond, BoolOp):
        if cond.op == "or" and not negate:
            merged = _try_merge_or(cond, cfg, negate=False)
            if merged is not None:
                for v in merged.vars:
                    t.use_var(v)
                for n in merged.notes:
                    t.note(n, APPROXIMATE)
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
            t.use_var(grafana_var(var, cfg))
            out.append(Matcher(label, "!~" if neg else "=~",
                               "${%s:regex}" % grafana_var(var, cfg)))
            return
        parts: List[str] = []
        for v in cond.values:
            raw, kind = render_value(v, cfg)
            if kind == "unsupported":
                t.note("IN-list value %s is not a literal, {{var}} or "
                       "concat() of those; that value was DROPPED from the "
                       "%s matcher" % (raw, label), NEEDS_REVIEW)
                continue
            if kind == "var":
                raw = "${%s:regex}" % raw[1:]
            for name in value_vars(v, cfg):
                t.use_var(name)
            raw = _app_value(cond.left, raw, kind, cfg, t)
            parts.append(regex_escape_keep_vars(raw))
        if not parts:
            return
        out.append(Matcher(label, "!~" if neg else "=~", "|".join(parts)))
        return
    if isinstance(cond, NullCheck):
        label, _ = _left_label(cond.left, cfg, t)
        # IS NULL -> label absent; IS NOT NULL -> label present
        present = cond.negated != negate
        out.append(Matcher(label, "!=" if present else "=", ""))
        return
    t.note("unsupported WHERE construct dropped: %s" % cond_text(cond),
           NEEDS_REVIEW)


def _left_label(left: Any, cfg: Dict[str, Any], t: Translation) -> Tuple[str, bool]:
    if isinstance(left, Attr):
        label, mapped = map_attr(left.name, cfg)
        if not mapped:
            # Custom metric dimensions keep their name through OTel/Prom
            # (dots -> underscores); the corpus humans kept them verbatim,
            # so this is approximate, not a review item.
            t.note("attribute %r not in label_map; used %r — verify the label "
                   "exists in your stack" % (left.name, label), APPROXIMATE)
        return label, mapped
    if isinstance(left, Func):
        t.note("function %s(...) in WHERE cannot become a label matcher; "
               "dropped" % left.name, NEEDS_REVIEW)
        return sanitize_label(left.name), False
    return sanitize_label(_lit_str(left)), False


def _cmp_to_matcher(cmp_: Cmp, cfg: Dict[str, Any], t: Translation,
                    negate: bool) -> List[Matcher]:
    op = cmp_.op
    val = cmp_.right
    if isinstance(cmp_.left, Attr) \
            and cmp_.left.name.lower() in _ENTITY_GUID_ATTRS \
            and op == "=" and not negate:
        text, kind = render_value(val, cfg)
        if kind == "literal":
            m = _resolve_entity_guid(cmp_.left, text, cfg, t)
            return [m] if m else []
    label, _ = _left_label(cmp_.left, cfg, t)
    var = is_nr_variable(val)
    if var:
        var = grafana_var(var, cfg)
        t.use_var(var)
        raw, kind = "$%s" % var, "var"
    else:
        raw, kind = render_value(val, cfg)
        if kind == "unsupported":
            t.note("WHERE %s %s %s: the value is not a literal, {{var}} or "
                   "concat() of those and cannot become a label matcher; "
                   "predicate DROPPED - apply it manually"
                   % (_lit_str(cmp_.left), op, raw), NEEDS_REVIEW)
            return []
        for name in value_vars(val, cfg):
            t.use_var(name)
        raw = _app_value(cmp_.left, raw, kind, cfg, t)

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
        if var:
            pattern = "${%s:regex}" % var
        elif kind == "mixed":
            pattern = "(?i)" + like_to_regex_keep_vars(raw)
        else:
            pattern = "(?i)" + like_to_regex(raw)
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
              ("<=", 399): "[123]..", ("<=", 499): "[1234]..",
              ("<", 300): "[12]..", ("<=", 299): "[12]..",
              (">=", 300): "[345]..", (">", 299): "[345]..", }
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
                       % (item.expr.name, label), APPROXIMATE)
            labels.append(label)
        elif isinstance(item.expr, Func):
            t.note("FACET %s(...) has no label equivalent; grouping dropped"
                   % item.expr.name, NEEDS_REVIEW)
        else:
            t.note("unsupported FACET expression dropped", NEEDS_REVIEW)
    return labels


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
        return "%s %s %s" % (_lit_str(cond.left), cond.op,
                             _lit_str(cond.right))
    if isinstance(cond, InList):
        return "%s %sIN (%s)" % (_lit_str(cond.left),
                                 "NOT " if cond.negated else "",
                                 ", ".join(_lit_str(v) for v in cond.values))
    if isinstance(cond, NullCheck):
        return "%s IS %sNULL" % (_lit_str(cond.left),
                                 "NOT " if cond.negated else "")
    return render_value(cond)[0]


def legend_for(labels: List[str], alias: Optional[str] = None) -> str:
    if labels:
        return " / ".join("{{%s}}" % l for l in labels)
    return alias or ""
