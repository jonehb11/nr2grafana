"""Change tracking and codify-back-into-config suggestions.

Wraps the persistent :class:`~nr2grafana.store.Store` change log with a
small API for recording changes made to converted dashboards (query
edits, datasource assignments, imports, ...), reporting them as JSON or
Markdown, and -- the interesting part -- inferring a mergeable config
overlay from them via :meth:`ChangeLog.suggest_config`.

The idea: when a user (or the AI assistant) fixes a translated query by
renaming a label, swapping a metric name, or pointing panels at a
concrete datasource uid, that one-off fix can usually be codified as a
``label_map`` / ``metric_map`` / ``datasources.<family>.uid`` config
entry so every future conversion gets it right automatically.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-time only, avoids cycle
    from nr2grafana.store import Store

try:  # version string for generated reports
    import nr2grafana as _pkg
    _GENERATOR = "nr2grafana %s" % getattr(_pkg, "__version__", "1.1.0")
except Exception:  # pragma: no cover
    _GENERATOR = "nr2grafana 1.1.0"


ACTIONS = ("query-edit", "datasource-set", "panel-edit", "import",
           "datasource-created", "dashboard-updated")

# Grafana datasource plugin type -> config "datasources" family key.
_TYPE_TO_FAMILY = {
    "prometheus": "prometheus",
    "loki": "loki",
    "tempo": "tempo",
    "nrgrafanaplugin-newrelic-datasource": "newrelic",
}
_FAMILIES = ("prometheus", "loki", "tempo", "newrelic")

# Longest value rendered inline in the Markdown report.
_SHORT = 48

# label matchers: name =/!=/=~/!~ "value" (PromQL selectors, LogQL stream
# selectors and pipeline filters alike).
_MATCHER_RE = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')
_GROUP_RE = re.compile(r'\b(?:by|without|on|ignoring)\s*\(([^)]*)\)')
_IDENT_RE = re.compile(r'[A-Za-z_:][A-Za-z0-9_:]*')
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')
_BARE_METRIC_RE = re.compile(r'^[A-Za-z_:][A-Za-z0-9_:.]*$')

# Identifiers that can appear outside braces without being a metric name:
# PromQL keywords/aggregations and common LogQL stages.
_KEYWORDS = frozenset((
    "by", "without", "on", "ignoring", "group_left", "group_right",
    "offset", "bool", "and", "or", "unless",
    "sum", "avg", "min", "max", "count", "topk", "bottomk", "stddev",
    "stdvar", "quantile", "count_values", "group",
    "json", "logfmt", "pattern", "regexp", "unpack", "unwrap",
    "line_format", "label_format", "drop", "keep", "decolorize",
))


class ChangeLog:
    """Record, report and codify dashboard changes stored in a Store."""

    def __init__(self, store: "Store") -> None:
        self._store = store

    # -- recording ---------------------------------------------------------

    def record(self, slug: str, action: str, target: str,
               before: Any, after: Any, why: str = "",
               source: str = "user") -> int:
        """Record one change against a dashboard slug; returns change id.

        ``action`` is one of :data:`ACTIONS`; ``source`` is
        "user" | "ai" | "auto". The Store adds the timestamp and id.
        """
        change = {
            "action": action,
            "target": target,
            "before": before,
            "after": after,
            "why": why,
            "source": source,
        }
        return self._store.log_change(slug, change)

    # -- reporting ---------------------------------------------------------

    def report(self, slug: str = "") -> Dict[str, Any]:
        """JSON change report, grouped by dashboard, chronological."""
        changes = self._changes(slug)
        order: List[str] = []
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for ch in changes:
            key = ch.get("slug") or slug or ""
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(ch)
        return {
            "schema": "nr2grafana/changes/v1",
            "generated_by": _GENERATOR,
            "slug": slug,
            "total": len(changes),
            "dashboards": [
                {"slug": key, "count": len(groups[key]),
                 "changes": groups[key]}
                for key in order
            ],
        }

    def report_markdown(self, slug: str = "") -> str:
        """Markdown change report: one chronological table per dashboard."""
        rep = self.report(slug)
        lines = ["# Change log", ""]
        if not rep["dashboards"]:
            lines.append("No recorded changes.")
            lines.append("")
            return "\n".join(lines)
        for dash in rep["dashboards"]:
            title = dash["slug"] or "(no dashboard)"
            lines.append("## %s" % title)
            lines.append("")
            lines.append("| When | Source | Action | Target | Change |"
                         " Why |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for ch in dash["changes"]:
                lines.append("| %s | %s | %s | %s | %s | %s |" % (
                    _text(ch.get("ts", "")),
                    _text(ch.get("source", "")),
                    _text(ch.get("action", "")),
                    _cell(ch.get("target", "")),
                    _change_cell(ch.get("before"), ch.get("after")),
                    _text(ch.get("why", "")),
                ))
            lines.append("")
        return "\n".join(lines)

    # -- codify ------------------------------------------------------------

    def suggest_config(self, slug: str = "") -> Dict[str, Any]:
        """Infer a mergeable config overlay from recorded changes.

        Looks at "query-edit" changes (label renames -> ``label_map``,
        leading-metric swaps -> ``metric_map``) and "datasource-set" /
        "datasource-created" changes (-> ``datasources.<family>.uid``).

        Returns ``{"overlay": {...}, "rationale": [{"change_id",
        "inference", "confidence"}]}`` where overlay merges cleanly over
        the default config (dicts merge key-wise).
        """
        label_map: Dict[str, str] = {}
        metric_map: Dict[str, str] = {}
        ds_map: Dict[str, Dict[str, str]] = {}
        rationale: List[Dict[str, Any]] = []

        for ch in self._changes(slug):
            action = ch.get("action", "")
            if action == "query-edit":
                self._codify_query_edit(ch, label_map, metric_map,
                                        rationale)
            elif action in ("datasource-set", "datasource-created"):
                self._codify_datasource(ch, ds_map, rationale)

        overlay: Dict[str, Any] = {}
        if label_map:
            overlay["label_map"] = label_map
        if metric_map:
            overlay["metric_map"] = metric_map
        if ds_map:
            overlay["datasources"] = ds_map
        return {"overlay": overlay, "rationale": rationale}

    # -- internals ---------------------------------------------------------

    def _changes(self, slug: str = "") -> List[Dict[str, Any]]:
        rows = self._store.list_changes(slug)
        return sorted(rows, key=lambda r: (str(r.get("ts", "")),
                                           r.get("id", 0)))

    def _codify_query_edit(self, ch: Dict[str, Any],
                           label_map: Dict[str, str],
                           metric_map: Dict[str, str],
                           rationale: List[Dict[str, Any]]) -> None:
        before = ch.get("before")
        after = ch.get("after")
        if not isinstance(before, str) or not isinstance(after, str):
            return
        if before.strip() == after.strip():
            return

        renamed: Dict[str, str] = {}
        for old, new, conf in _label_renames(before, after):
            renamed[old] = new
            label_map[old] = new
            rationale.append({
                "change_id": ch.get("id"),
                "inference": "label rename %r -> %r in edited query"
                             % (old, new),
                "confidence": conf,
            })

        old_metric = _leading_metric(before)
        new_metric = _leading_metric(after)
        if (old_metric and new_metric and old_metric != new_metric
                and old_metric not in renamed
                and new_metric not in renamed.values()):
            key = _nrql_metric_key(ch) or old_metric
            metric_map[key] = new_metric
            conf = "high" if _only_metric_changed(
                before, after, old_metric, new_metric) else "medium"
            rationale.append({
                "change_id": ch.get("id"),
                "inference": "metric rename %r -> %r in edited query"
                             % (key, new_metric),
                "confidence": conf,
            })

    def _codify_datasource(self, ch: Dict[str, Any],
                           ds_map: Dict[str, Dict[str, str]],
                           rationale: List[Dict[str, Any]]) -> None:
        after = ch.get("after")
        uid = ""
        ds_type = ""
        if isinstance(after, dict):
            uid = str(after.get("uid", "") or "")
            ds_type = str(after.get("type", "") or "")
        elif isinstance(after, str):
            uid = after
        if not uid:
            return
        family, conf = _ds_family(ch, ds_type)
        if not family:
            return
        ds_map.setdefault(family, {})["uid"] = uid
        rationale.append({
            "change_id": ch.get("id"),
            "inference": "datasource uid for %r set to %r"
                         % (family, uid),
            "confidence": conf,
        })


# -- inference helpers -----------------------------------------------------

def _label_renames(before: str,
                   after: str) -> List[Tuple[str, str, str]]:
    """Detect label renames between two query expressions.

    High confidence: a matcher ``old<op>"value"`` disappears and
    ``new<op>"value"`` (same op, same value) appears, with ``old`` gone
    from and ``new`` new to the expression. Medium confidence: a single
    label swapped inside a ``by (...)`` / ``without (...)`` clause.
    """
    b = set(_MATCHER_RE.findall(before))
    a = set(_MATCHER_RE.findall(after))
    b_labels = set(m[0] for m in b)
    a_labels = set(m[0] for m in a)
    renames: List[Tuple[str, str, str]] = []
    seen = set()
    for old, op, val in sorted(b - a):
        for new, op2, val2 in sorted(a - b):
            if (op == op2 and val == val2 and old != new
                    and old not in a_labels and new not in b_labels
                    and old not in seen and new not in seen):
                renames.append((old, new, "high"))
                seen.add(old)
                seen.add(new)
                break
    bg = _group_labels(before)
    ag = _group_labels(after)
    removed = bg - ag
    added = ag - bg
    if len(removed) == 1 and len(added) == 1:
        old = removed.pop()
        new = added.pop()
        if old not in seen and new not in seen:
            renames.append((old, new, "medium"))
    return renames


def _group_labels(expr: str) -> set:
    labels = set()
    for grp in _GROUP_RE.findall(expr):
        for name in grp.split(","):
            name = name.strip()
            if name:
                labels.add(name)
    return labels


def _leading_metric(expr: str) -> str:
    """First identifier in expr that reads as a metric/series name.

    Skips string contents, label names inside ``{...}`` and ``by (...)``
    clauses, function calls (identifier followed by "("), PromQL
    keywords/aggregations and LogQL stages, and anything followed by a
    comparison operator (pipeline filters).
    """
    clean = _STRING_RE.sub(lambda m: " " * len(m.group(0)), expr)
    clean = _GROUP_RE.sub(lambda m: " " * len(m.group(0)), clean)
    for m in _IDENT_RE.finditer(clean):
        name = m.group(0)
        if name in _KEYWORDS:
            continue
        if clean.count("{", 0, m.start()) > clean.count("}", 0, m.start()):
            continue  # label name inside a selector
        rest = clean[m.end():].lstrip()
        if rest.startswith("("):
            continue  # function call
        if rest[:2] in ("=~", "!~", "!=", "==", ">=", "<="):
            continue  # comparison / filter
        if rest[:1] in ("=", "~"):
            continue
        return name
    return ""


def _only_metric_changed(before: str, after: str,
                         old: str, new: str) -> bool:
    swapped = re.sub(r"\b%s\b" % re.escape(old), new, before)
    return _squash(swapped) == _squash(after)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _nrql_metric_key(ch: Dict[str, Any]) -> str:
    """Original NRQL metric name carried on the change record, if any."""
    for key in ("nrql_metric", "nr_metric"):
        val = ch.get(key)
        if isinstance(val, str) and val:
            return val
    val = ch.get("nrql")
    if isinstance(val, str) and _BARE_METRIC_RE.match(val):
        return val
    return ""


def _ds_family(ch: Dict[str, Any], ds_type: str) -> Tuple[str, str]:
    """Resolve a change record to a config datasource family.

    Returns (family, confidence); family "" when undeterminable (no
    overlay entry is emitted then -- a wrong pin is worse than none).
    """
    fam = ch.get("family")
    if isinstance(fam, str) and fam in _FAMILIES:
        return fam, "high"
    if ds_type in _TYPE_TO_FAMILY:
        return _TYPE_TO_FAMILY[ds_type], "high"
    target = str(ch.get("target", "") or "")
    if target in _FAMILIES:
        return target, "high"
    if target in _TYPE_TO_FAMILY:
        return _TYPE_TO_FAMILY[target], "high"
    low = target.lower()
    for family in _FAMILIES:
        if family in low:
            return family, "medium"
    return "", ""


# -- markdown helpers ------------------------------------------------------

def _text(value: Any) -> str:
    s = value if isinstance(value, str) else (
        "" if value is None else json.dumps(value, sort_keys=True))
    s = s.replace("\n", " ").replace("|", "\\|")
    if len(s) > _SHORT:
        s = s[:_SHORT - 3] + "..."
    return s


def _cell(value: Any) -> str:
    s = _text(value)
    if not s:
        return ""
    return "`%s`" % s.replace("`", "'")


def _change_cell(before: Any, after: Any) -> str:
    b = _cell(before)
    a = _cell(after)
    if b and a:
        return "%s -> %s" % (b, a)
    if a:
        return "-> %s" % a
    if b:
        return "%s ->" % b
    return ""
