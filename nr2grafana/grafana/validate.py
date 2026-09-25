"""Static validation of generated Grafana dashboard JSON.

Catches everything that would make an import fail or produce broken panels:
schema-level requirements, duplicate ids, malformed gridPos, empty or
unbalanced query expressions, bad datasource refs.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

_UID_RE = re.compile(r"^[a-zA-Z0-9\-_]{1,40}$")
_VAR_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")

_PAIRS = {"(": ")", "{": "}", "[": "]"}
_CLOSERS = {v: k for k, v in _PAIRS.items()}


def _balanced(expr: str) -> bool:
    stack: List[str] = []
    in_str = ""
    prev = ""
    for ch in expr:
        if in_str:
            if ch == in_str and prev != "\\":
                in_str = ""
            prev = "" if prev == "\\" else ch
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            prev = ""
            continue
        if ch in _PAIRS:
            stack.append(ch)
        elif ch in _CLOSERS:
            if not stack or stack.pop() != _CLOSERS[ch]:
                return False
        prev = ch
    return not stack and not in_str


def _iter_panels(dash: Dict[str, Any]):
    for p in dash.get("panels") or []:
        yield p
        if p.get("type") == "row":
            for child in p.get("panels") or []:
                yield child


# Panel types shipped with Grafana (plus the ones the converter emits).
KNOWN_PANEL_TYPES = {
    "timeseries", "stat", "gauge", "bargauge", "piechart", "table", "text",
    "heatmap", "histogram", "logs", "row", "nodeGraph", "geomap", "xychart",
    "state-timeline", "status-history", "candlestick", "trend", "traces",
    "alertlist", "dashlist", "news", "annolist", "canvas", "datagrid",
    "flamegraph", "graph", "singlestat", "barchart",
}

# Grafana built-in variables that need no templating entry.
_BUILTIN_VARS = {
    "__rate_interval", "__interval", "__interval_ms", "__range",
    "__range_s", "__range_ms", "__auto", "__all", "__from", "__to",
    "__dashboard", "__org", "__user", "__timeFilter", "__timezone",
    "__name", "__value", "__data", "__field", "__series", "__cell",
    "__url_time_range", "__auto_interval",
}

_VAR_USE_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[A-Za-z]+)?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
    r"|\[\[([A-Za-z_][A-Za-z0-9_]*)\]\]")
_TIME_OVERRIDE_RE = re.compile(r"^(now-)?\d+[smhdwMy](/[smhdwMy])?$|^now/[smhdwMy]$")
_NRQL_LEAK_RE = re.compile(
    r"\b(SINCE|FACET|TIMESERIES|NRQL|COMPARE WITH|UNTIL)\b")
_PLACEHOLDER_RE = re.compile(r"<(?:BY|SEL|SELBARE|W|AGG|AGGINV|HTTP)>")
_LOGQL_SELECTOR_RE = re.compile(r"\{([^}]*)\}")
_LOGQL_RANGE_FN_RE = re.compile(
    r"\b(avg_over_time|sum_over_time|max_over_time|min_over_time|"
    r"quantile_over_time|stddev_over_time|stdvar_over_time|first_over_time|"
    r"last_over_time|absent_over_time)\s*\(")


def _var_names(text: str):
    for m in _VAR_USE_RE.finditer(text or ""):
        name = m.group(1) or m.group(2) or m.group(3)
        if name:
            yield name


def _check_expr_language(ds_type: str, expr: str, where: str,
                         errs: List[str], warns: List[str]) -> None:
    """Sanity checks per query language that catch converter mistakes and
    hand edits: leaked NRQL, leftover template placeholders, empty Loki
    selectors, unwrap-less unwrap aggregations, malformed matcher lists."""
    if _PLACEHOLDER_RE.search(expr):
        errs.append("%s: expression still contains a converter placeholder "
                    "(%s)" % (where, _PLACEHOLDER_RE.search(expr).group(0)))
    if ds_type in ("prometheus", "loki") and _NRQL_LEAK_RE.search(expr):
        errs.append("%s: expression contains NRQL syntax (%s); it was not "
                    "translated" % (where,
                                    _NRQL_LEAK_RE.search(expr).group(1)))
    if ds_type in ("prometheus", "loki"):
        if "{," in expr or ",}" in expr or ",," in expr:
            errs.append("%s: malformed label matcher list (stray comma) in "
                        "%s" % (where, expr))
        if "{{" in expr and "}}" in expr:
            errs.append("%s: unresolved {{variable}} placeholder in the "
                        "query expression" % where)
    if ds_type == "prometheus":
        if re.search(r"\[\s*\]", expr):
            errs.append("%s: empty range selector [] in %s" % (where, expr))
        if re.search(r"\bby\s*\(\s*,", expr) or re.search(r",\s*\)", expr):
            errs.append("%s: malformed by()/argument list in %s"
                        % (where, expr))
    if ds_type == "loki":
        compact = expr.replace(" ", "")
        if "{}" in compact:
            errs.append("%s: Loki query has an empty stream selector {}"
                        % where)
        m = _LOGQL_SELECTOR_RE.search(expr)
        if m is None:
            errs.append("%s: LogQL query has no stream selector {...}: %s"
                        % (where, expr[:80]))
        elif not re.search(r'[A-Za-z_][A-Za-z0-9_]*\s*(=~|!~|!=|=)\s*"',
                           m.group(1)):
            errs.append("%s: Loki stream selector has no label matcher: %s"
                        % (where, expr[:80]))
        if _LOGQL_RANGE_FN_RE.search(expr) and "| unwrap " not in expr:
            errs.append("%s: LogQL %s needs an `| unwrap <field>` stage"
                        % (where, _LOGQL_RANGE_FN_RE.search(expr).group(1)))
    if ds_type == "tempo":
        stripped = expr.strip()
        if not stripped.startswith("{") and not re.match(
                r"^[0-9a-fA-F]{16,32}$", stripped):
            errs.append("%s: TraceQL query must start with { ... }: %s"
                        % (where, expr[:80]))


def validate_dashboard_full(dash: Dict[str, Any]) -> Dict[str, List[str]]:
    """Errors (import will fail or panels are broken) and warnings (the
    dashboard imports but something deserves a look)."""
    errs = list(validate_dashboard(dash))
    warns: List[str] = []
    if not isinstance(dash, dict) or not isinstance(dash.get("panels"), list):
        return {"errors": errs, "warnings": warns}
    var_names = {v.get("name") for v in
                 (dash.get("templating") or {}).get("list") or []}
    titles: Dict[str, int] = {}
    for p in _iter_panels(dash):
        where = "panel id=%s title=%r" % (p.get("id"), p.get("title", ""))
        ptype = p.get("type") or ""
        if ptype and ptype not in KNOWN_PANEL_TYPES:
            warns.append("%s: panel type %r is not a core Grafana panel; "
                         "the plugin must be installed" % (where, ptype))
        title = (p.get("title") or "").strip()
        if title and ptype != "row":
            titles[title] = titles.get(title, 0) + 1
        for key in ("timeFrom", "timeShift"):
            val = p.get(key)
            if val and not _TIME_OVERRIDE_RE.match(str(val)):
                errs.append("%s: %s %r is not a valid Grafana relative time "
                            "(e.g. 30m, 1h, 7d)" % (where, key, val))
        if ptype == "row":
            if p.get("collapsed") and not p.get("panels"):
                warns.append("%s: collapsed row has no panels" % where)
            if not p.get("collapsed") and p.get("panels"):
                errs.append("%s: an expanded row must not carry nested "
                            "panels (Grafana hides them)" % where)
            continue
        if ptype == "text":
            continue
        targets = p.get("targets") or []
        if not targets:
            warns.append("%s: panel has no targets (it will be empty)"
                         % where)
        for t in targets:
            rid = t.get("refId") or "?"
            tw = "%s target %s" % (where, rid)
            ds = t.get("datasource") or {}
            ds_type = ds.get("type", "") if isinstance(ds, dict) else ""
            expr = t.get("expr") or t.get("query") or t.get("queryText") or ""
            if isinstance(expr, str) and expr:
                _check_expr_language(ds_type, expr, tw, errs, warns)
                for name in _var_names(expr):
                    if name not in var_names and name not in _BUILTIN_VARS:
                        errs.append("%s: query references variable $%s "
                                    "which is not defined in templating"
                                    % (tw, name))
            if ds_type == "prometheus" and t.get("instant") and t.get("range"):
                warns.append("%s: both instant and range set; Grafana runs "
                             "both queries" % tw)
        for name in _var_names(p.get("title") or ""):
            if name not in var_names and name not in _BUILTIN_VARS:
                warns.append("%s: title references undefined variable $%s"
                             % (where, name))
    for title, n in titles.items():
        if n > 1:
            warns.append("%d panels share the title %r" % (n, title))
    for v in (dash.get("templating") or {}).get("list") or []:
        if v.get("type") == "query":
            q = v.get("query")
            if isinstance(q, dict):
                # Prometheus keeps a query string; Loki and Tempo variable
                # models carry the label to enumerate instead.
                qtext = str(q.get("query") or q.get("label") or "")
            else:
                qtext = str(q or "")
            if not qtext.strip():
                errs.append("template variable %r has an empty query"
                            % v.get("name"))
            for name in _var_names(qtext):
                if name not in var_names and name not in _BUILTIN_VARS:
                    errs.append("template variable %r references undefined "
                                "variable $%s" % (v.get("name"), name))
    if not dash.get("uid"):
        warns.append("dashboard has no uid; Grafana will assign a random "
                     "one and re-imports will not update in place")
    return {"errors": errs, "warnings": warns}


def validate_dashboard(dash: Dict[str, Any]) -> List[str]:
    """Returns a list of problems; empty list = valid."""
    errs: List[str] = []
    if not isinstance(dash, dict):
        return ["dashboard is not a JSON object"]
    if not (dash.get("title") or "").strip():
        errs.append("dashboard title is empty")
    uid = dash.get("uid")
    if uid is not None and not _UID_RE.match(str(uid)):
        errs.append("uid %r invalid (allowed: [a-zA-Z0-9_-], max 40 chars)"
                    % uid)
    if dash.get("id") not in (None,):
        errs.append("dashboard 'id' must be null for import")
    if not isinstance(dash.get("schemaVersion"), int):
        errs.append("schemaVersion missing")
    if not isinstance(dash.get("panels"), list):
        errs.append("panels must be a list")
        return errs

    try:
        json.dumps(dash)
    except (TypeError, ValueError) as e:
        errs.append("dashboard not JSON-serializable: %s" % e)

    seen_ids = set()
    for p in _iter_panels(dash):
        pid = p.get("id")
        where = "panel id=%s title=%r" % (pid, p.get("title", ""))
        if not isinstance(pid, int):
            errs.append("%s: id missing or not an int" % where)
        elif pid in seen_ids:
            errs.append("%s: duplicate panel id" % where)
        else:
            seen_ids.add(pid)
        if not p.get("type"):
            errs.append("%s: missing panel type" % where)
        gp = p.get("gridPos") or {}
        for k in ("h", "w", "x", "y"):
            if not isinstance(gp.get(k), int):
                errs.append("%s: gridPos.%s missing/not int" % (where, k))
        if isinstance(gp.get("x"), int) and isinstance(gp.get("w"), int):
            if gp["x"] < 0 or gp["w"] < 1 or gp["x"] + gp["w"] > 24:
                errs.append("%s: gridPos out of 24-column bounds (x=%s w=%s)"
                            % (where, gp["x"], gp["w"]))
        if isinstance(gp.get("h"), int) and gp["h"] < 1:
            errs.append("%s: gridPos.h < 1" % where)

        if p.get("type") in ("text", "row"):
            continue
        refids = set()
        for t in p.get("targets") or []:
            rid = t.get("refId")
            if not rid:
                errs.append("%s: target missing refId" % where)
            elif rid in refids:
                errs.append("%s: duplicate refId %s" % (where, rid))
            refids.add(rid)
            ds = t.get("datasource")
            if not (isinstance(ds, dict) and ds.get("type")
                    and ds.get("uid")):
                errs.append("%s: target %s datasource ref malformed"
                            % (where, rid))
            expr = t.get("expr") or t.get("query") or t.get("queryText")
            if not expr:
                errs.append("%s: target %s has no expr/query" % (where, rid))
            elif not _balanced(expr):
                errs.append("%s: target %s expression has unbalanced "
                            "brackets/quotes: %s" % (where, rid, expr))
            if isinstance(ds, dict) and ds.get("type") == "loki":
                e = t.get("expr", "")
                if "{}" in e.replace(" ", ""):
                    errs.append("%s: Loki query has an empty stream "
                                "selector {}" % where)

    names = set()
    for v in (dash.get("templating") or {}).get("list") or []:
        n = v.get("name", "")
        if not _VAR_NAME_RE.match(n or ""):
            errs.append("template variable name %r invalid" % n)
        if n in names:
            errs.append("duplicate template variable %r" % n)
        names.add(n)
        if not v.get("type"):
            errs.append("template variable %r missing type" % n)

    # Every ${var} datasource uid must have a matching datasource variable.
    ds_vars = {v.get("name") for v in
               (dash.get("templating") or {}).get("list") or []
               if v.get("type") == "datasource"}
    for p in _iter_panels(dash):
        for t in (p.get("targets") or []):
            ds = t.get("datasource") or {}
            uid = str(ds.get("uid", ""))
            m = re.fullmatch(r"\$\{([A-Za-z0-9_]+)\}", uid)
            if m and m.group(1) not in ds_vars:
                errs.append("panel id=%s references datasource variable "
                            "${%s} but no such datasource variable exists"
                            % (p.get("id"), m.group(1)))
    for v in (dash.get("templating") or {}).get("list") or []:
        ds = v.get("datasource") or {}
        if isinstance(ds, dict):
            uid = str(ds.get("uid", ""))
            m = re.fullmatch(r"\$\{([A-Za-z0-9_]+)\}", uid)
            if m and m.group(1) not in ds_vars:
                errs.append("template variable %r references datasource "
                            "variable ${%s} but no such datasource variable "
                            "exists" % (v.get("name"), m.group(1)))
    return errs
