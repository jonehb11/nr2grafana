"""Deep, structured understanding of a New Relic dashboard.

``inspect_dashboard`` turns an NR dashboard export into a complete
semantic model an operator or an AI agent can reason about without
opening the JSON: every page and widget, each NRQL query parsed into its
clauses (aggregations, event types, WHERE tree, FACET, TIMESERIES,
SINCE/UNTIL, COMPARE WITH, LIMIT), the attributes and dashboard
variables it touches, thresholds, units and layout, and — for each
query — the translation plan: target datasource family, the emitted
PromQL/LogQL/TraceQL, confidence and every assumption the converter
made. The dashboard-level summary names the datasource types Grafana
will need and lists exactly which widgets cannot be migrated and why.

``explain_nrql`` does the same for a single NRQL string.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .grafana.builder import _PANEL_TYPES, widget_kind_reason
from .model import NRDashboard, NRWidget, parse_nr_dashboard
from .nrql.parser import (
    Attr, BinOp, BoolOp, Cmp, Func, InList, Lit, NotOp, NrqlParseError,
    NullCheck, Star, parse_nrql,
)
from .translate.common import (
    UNTRANSLATABLE, cond_text, expr_text, nr_var_names, route_event_type,
)
from .translate.router import translate_query

_HINT_PREFIXES = ("unit:", "timefrom:", "timeshift:", "maxlines:", "limit:",
                  "panel-hint:")


def _node(expr: Any) -> Dict[str, Any]:
    """JSON-able rendering of an AST expression."""
    if isinstance(expr, Func):
        out: Dict[str, Any] = {"function": expr.name,
                               "args": [_node(a) for a in expr.args]}
        if expr.where is not None:
            out["where"] = cond_text(expr.where)
        if expr.cases and expr.name == "cases":
            out["cases"] = [{"where": cond_text(c), "alias": a}
                            for c, a in expr.cases]
        return out
    if isinstance(expr, Attr):
        return {"attribute": expr.name}
    if isinstance(expr, Lit):
        return {"literal": expr.value}
    if isinstance(expr, Star):
        return {"star": True}
    if isinstance(expr, BinOp):
        return {"op": expr.op, "left": _node(expr.left),
                "right": _node(expr.right)}
    return {"raw": str(expr)}


def _attrs_in(expr: Any, out: List[str]) -> None:
    if isinstance(expr, Attr):
        if expr.name not in out and not expr.name.startswith("{{"):
            out.append(expr.name)
    elif isinstance(expr, Func):
        for a in expr.args:
            _attrs_in(a, out)
        if expr.where is not None:
            _attrs_in(expr.where, out)
        for c, _ in expr.cases:
            _attrs_in(c, out)
    elif isinstance(expr, BinOp):
        _attrs_in(expr.left, out)
        _attrs_in(expr.right, out)
    elif isinstance(expr, Cmp):
        _attrs_in(expr.left, out)
        if isinstance(expr.right, Attr) and not expr.right.name.startswith(
                "{{"):
            out.append(expr.right.name)
    elif isinstance(expr, InList):
        _attrs_in(expr.left, out)
    elif isinstance(expr, NullCheck):
        _attrs_in(expr.left, out)
    elif isinstance(expr, BoolOp):
        for i in expr.items:
            _attrs_in(i, out)
    elif isinstance(expr, NotOp):
        _attrs_in(expr.item, out)


def _predicates(cond: Any, out: List[Dict[str, Any]]) -> None:
    """Flat list of the comparison predicates in a WHERE tree."""
    if cond is None:
        return
    if isinstance(cond, BoolOp):
        for i in cond.items:
            _predicates(i, out)
    elif isinstance(cond, NotOp):
        before = len(out)
        _predicates(cond.item, out)
        for p in out[before:]:
            p["negated"] = not p.get("negated", False)
    elif isinstance(cond, Cmp):
        out.append({"attribute": expr_text(cond.left), "op": cond.op,
                    "value": expr_text(cond.right)})
    elif isinstance(cond, InList):
        out.append({"attribute": expr_text(cond.left),
                    "op": "NOT IN" if cond.negated else "IN",
                    "value": [expr_text(v) for v in cond.values]})
    elif isinstance(cond, NullCheck):
        out.append({"attribute": expr_text(cond.left),
                    "op": "IS NOT NULL" if cond.negated else "IS NULL",
                    "value": None})


def explain_nrql(nrql: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Parse + translate one NRQL query into a structured explanation."""
    out: Dict[str, Any] = {"nrql": nrql}
    try:
        q = parse_nrql(nrql)
    except NrqlParseError as e:
        out["parse_error"] = str(e)
        out["translation"] = {"confidence": UNTRANSLATABLE, "expr": "",
                              "datasource": "", "notes": [str(e)]}
        return out
    attrs: List[str] = []
    for item in q.select:
        _attrs_in(item.expr, attrs)
    if q.where is not None:
        _attrs_in(q.where, attrs)
    for f in q.facet:
        _attrs_in(f.expr, attrs)
    preds: List[Dict[str, Any]] = []
    _predicates(q.where, preds)
    out["parsed"] = {
        "select": [{"expr": expr_text(i.expr), "tree": _node(i.expr),
                    "alias": i.alias, "multiplier": i.multiplier}
                   for i in q.select],
        "from": list(q.from_),
        "family": route_event_type(q.from_, cfg),
        "where": cond_text(q.where) if q.where is not None else "",
        "predicates": preds,
        "facet": [{"expr": expr_text(f.expr), "alias": f.alias}
                  for f in q.facet],
        "timeseries": (None if q.timeseries is None else {
            "auto": q.timeseries.auto, "max": q.timeseries.max,
            "interval_seconds": q.timeseries.interval_seconds,
            "interval_var": q.timeseries.interval_var,
            "slide_by": q.timeseries.slide_by}),
        "since": q.since, "until": q.until, "compare_with": q.compare_with,
        "limit": q.limit,
        "order_by": ({"expr": expr_text(q.order_by.expr),
                      "direction": q.order_by.direction}
                     if q.order_by is not None else None),
        "timezone": q.timezone, "extrapolate": q.extrapolate,
        "with": {k: expr_text(v) for k, v in q.with_.items()},
        "attributes": attrs,
        "variables": nr_var_names(nrql),
        "unparsed_fragments": list(q.extras),
    }
    t = translate_query(nrql, cfg)
    targets = [{"datasource": x.datasource, "type": x.query_type,
                "expr": x.expr, "legend": x.legend}
               for x in [t] + list(t.extra)]
    hints = {}
    for n in t.notes:
        for pre in _HINT_PREFIXES:
            if n.startswith(pre):
                hints[pre[:-1]] = n[len(pre):]
    out["translation"] = {
        "confidence": t.confidence,
        "datasource": t.datasource,
        "query_type": t.query_type,
        "expr": t.expr,
        "targets": targets,
        "group_by": list(t.group_by),
        "panel_hints": hints,
        "notes": [n for n in t.notes
                  if not n.startswith(_HINT_PREFIXES)],
    }
    return out


def _widget_model(widget: NRWidget, page: str, cfg: Dict[str, Any]) \
        -> Dict[str, Any]:
    rc = widget.raw_configuration or {}
    kind_reason, equivalent = widget_kind_reason(widget.viz_id)
    queries = [explain_nrql(nq.get("query", ""), cfg)
               for nq in widget.nrql_queries if nq.get("query")]
    confidences = [q["translation"]["confidence"] for q in queries]
    if kind_reason:
        status = "cannot-migrate"
        reason = kind_reason
    elif widget.viz_id == "viz.markdown":
        status, reason = "exact", ""
    elif not queries:
        status = "cannot-migrate"
        reason = ("no NRQL query (legacy metric chart or entity-bound "
                  "widget)")
    elif all(c == UNTRANSLATABLE for c in confidences):
        status = "cannot-migrate"
        reason = "; ".join(n for q in queries
                           for n in q["translation"]["notes"])
    else:
        order = ["exact", "approximate", "needs-review", "untranslatable"]
        status = max(confidences, key=order.index)
        reason = ""
    thresholds = rc.get("thresholds")
    model: Dict[str, Any] = {
        "page": page,
        "title": widget.title or "(untitled)",
        "visualization": widget.viz_id,
        "grafana_panel": _PANEL_TYPES.get(widget.viz_id, "")
        or ("text" if kind_reason else "timeseries"),
        "layout": dict(widget.layout or {}),
        "units": (rc.get("units") or {}).get("unit"),
        "thresholds": thresholds if isinstance(thresholds, (list, dict))
        else None,
        "legend": (rc.get("legend") or {}).get("enabled"),
        "y_axis": rc.get("yAxisLeft"),
        "sorting": rc.get("initialSorting"),
        "markdown": rc.get("text") if widget.viz_id == "viz.markdown"
        else None,
        "account_ids": sorted({int(v) for nq in widget.nrql_queries
                               for v in ((nq.get("accountIds") or [])
                                         or ([nq.get("accountId")]
                                             if nq.get("accountId")
                                             else []))
                               if str(v).isdigit()}),
        "queries": queries,
        "status": status,
        "reason": reason,
        "equivalent": equivalent,
    }
    return model


def _variable_model(v) -> Dict[str, Any]:
    nrql = (v.nrql_query or {}).get("query", "") if v.nrql_query else ""
    out: Dict[str, Any] = {
        "name": v.name, "title": v.title, "type": v.type,
        "multi": v.is_multi, "defaults": list(v.default_values),
        "replacement_strategy": v.replacement_strategy,
        "grafana": {"NRQL": "query (label_values)", "ENUM": "custom",
                    "STRING": "textbox"}.get(v.type, "textbox"),
    }
    if nrql:
        out["nrql"] = nrql
        try:
            q = parse_nrql(nrql)
            fn = next((i.expr for i in q.select if isinstance(i.expr, Func)),
                      None)
            attr = None
            if fn is not None and fn.args and isinstance(fn.args[0], Attr):
                attr = fn.args[0].name
            out["attribute"] = attr
            out["from"] = list(q.from_)
        except NrqlParseError as e:
            out["parse_error"] = str(e)
    if v.type == "ENUM":
        out["items"] = [{"title": it.get("title"), "value": it.get("value")}
                        for it in v.items]
    return out


def inspect_dashboard(data: Dict[str, Any], cfg: Dict[str, Any],
                      source: str = "") -> Dict[str, Any]:
    """Full semantic model of one NR dashboard export (see module doc)."""
    nr: NRDashboard = parse_nr_dashboard(data)
    pages = []
    widgets: List[Dict[str, Any]] = []
    for page in nr.pages:
        models = [_widget_model(w, page.name, cfg) for w in page.widgets]
        widgets.extend(models)
        pages.append({"name": page.name, "description": page.description,
                      "widgets": len(models)})
    families: Dict[str, List[str]] = {}
    event_types: Dict[str, int] = {}
    attributes: Dict[str, int] = {}
    variables_used: Dict[str, int] = {}
    for w in widgets:
        for qm in w["queries"]:
            tr = qm["translation"]
            parsed = qm.get("parsed") or {}
            for et in parsed.get("from") or []:
                event_types[et] = event_types.get(et, 0) + 1
            for a in parsed.get("attributes") or []:
                attributes[a] = attributes.get(a, 0) + 1
            for v in parsed.get("variables") or []:
                variables_used[v] = variables_used.get(v, 0) + 1
            if tr["confidence"] != UNTRANSLATABLE and tr.get("datasource"):
                families.setdefault(tr["datasource"], [])
                if w["title"] not in families[tr["datasource"]]:
                    families[tr["datasource"]].append(w["title"])
    ds_types = {"prometheus": "prometheus", "loki": "loki", "tempo": "tempo",
                "newrelic": "nrgrafanaplugin-newrelic-datasource"}
    datasources = [{"family": fam,
                    "type": ds_types.get(fam, fam),
                    "purpose": {"prometheus": "metrics (Mimir/Prometheus)",
                                "loki": "logs (Loki)",
                                "tempo": "traces (Tempo)",
                                "newrelic": "NRQL passthrough"}.get(fam, fam),
                    "widgets": names}
                   for fam, names in sorted(families.items())]
    status_counts: Dict[str, int] = {}
    for w in widgets:
        status_counts[w["status"]] = status_counts.get(w["status"], 0) + 1
    cannot = [{"page": w["page"], "widget": w["title"],
               "visualization": w["visualization"],
               "nrql": [q["nrql"] for q in w["queries"]],
               "reason": w["reason"], "equivalent": w["equivalent"]}
              for w in widgets if w["status"] == "cannot-migrate"]
    review = [{"page": w["page"], "widget": w["title"],
               "notes": [n for q in w["queries"]
                         for n in q["translation"]["notes"]]}
              for w in widgets if w["status"] == "needs-review"]
    return {
        "source": source,
        "dashboard": {
            "name": nr.name, "guid": nr.guid, "account_id": nr.account_id,
            "description": nr.description, "permissions": nr.permissions,
            "pages": pages, "widgets": len(widgets),
        },
        "variables": [_variable_model(v) for v in nr.variables],
        "variables_used": variables_used,
        "event_types": event_types,
        "attributes": attributes,
        "datasources_needed": datasources,
        "summary": status_counts,
        "cannot_migrate": cannot,
        "needs_review": review,
        "widgets": widgets,
    }


def render_inspection_text(model: Dict[str, Any]) -> str:
    """Compact human rendering of an inspection."""
    d = model["dashboard"]
    lines = ["New Relic dashboard: %s" % d["name"]]
    if d.get("guid"):
        lines.append("  guid: %s%s" % (d["guid"], (" (account %s)"
                                                   % d["account_id"])
                                       if d.get("account_id") else ""))
    lines.append("  pages: %d, widgets: %d" % (len(d["pages"]), d["widgets"]))
    if model["variables"]:
        lines.append("  variables: " + ", ".join(
            "%s (%s -> %s)" % (v["name"], v["type"], v["grafana"])
            for v in model["variables"]))
    lines.append("  event types: " + ", ".join(
        "%s x%d" % (k, n) for k, n in sorted(model["event_types"].items()))
        if model["event_types"] else "  event types: none")
    lines.append("  Grafana datasources needed: " + (", ".join(
        "%s (%s; %d widgets)" % (ds["type"], ds["purpose"],
                                 len(ds["widgets"]))
        for ds in model["datasources_needed"]) or "none"))
    lines.append("  migration: " + ", ".join(
        "%d %s" % (n, k) for k, n in sorted(model["summary"].items())))
    lines.append("")
    for page in d["pages"]:
        lines.append("Page: %s" % page["name"])
        for w in model["widgets"]:
            if w["page"] != page["name"]:
                continue
            lines.append("  [%s] %s  (%s -> %s)" % (
                w["status"], w["title"], w["visualization"],
                w["grafana_panel"]))
            for qm in w["queries"]:
                lines.append("      NRQL: %s" % qm["nrql"])
                tr = qm["translation"]
                for tgt in tr["targets"]:
                    lines.append("      -> %s [%s]: %s" % (
                        tgt["datasource"], tgt["type"], tgt["expr"]))
                for n in tr["notes"]:
                    lines.append("         . %s" % n)
            if w["status"] == "cannot-migrate":
                lines.append("      cannot migrate: %s" % w["reason"])
                if w["equivalent"]:
                    lines.append("      closest Grafana equivalent: %s"
                                 % w["equivalent"])
        lines.append("")
    if model["cannot_migrate"]:
        lines.append("Widgets that cannot be migrated (%d):"
                     % len(model["cannot_migrate"]))
        for c in model["cannot_migrate"]:
            lines.append("  - %s / %s (%s): %s" % (
                c["page"], c["widget"], c["visualization"], c["reason"]))
    return "\n".join(lines)


def render_explanation_text(model: Dict[str, Any]) -> str:
    lines = ["NRQL: %s" % model["nrql"]]
    if model.get("parse_error"):
        lines.append("  parse error: %s" % model["parse_error"])
        return "\n".join(lines)
    p = model["parsed"]
    lines.append("  FROM %s  (%s)" % (", ".join(p["from"]) or "?",
                                      p["family"]))
    for item in p["select"]:
        extra = ""
        if item["alias"]:
            extra += " AS %r" % item["alias"]
        if item["multiplier"]:
            extra += " x%g" % item["multiplier"]
        lines.append("  SELECT %s%s" % (item["expr"], extra))
    if p["where"]:
        lines.append("  WHERE %s" % p["where"])
    if p["facet"]:
        lines.append("  FACET %s" % ", ".join(
            f["expr"] + (" AS %r" % f["alias"] if f["alias"] else "")
            for f in p["facet"]))
    if p["timeseries"] is not None:
        ts = p["timeseries"]
        lines.append("  TIMESERIES %s" % (
            "MAX" if ts["max"] else ("AUTO" if ts["auto"] else
                                     "%gs" % ts["interval_seconds"])))
    for key in ("since", "until", "compare_with", "limit"):
        if p.get(key) is not None:
            lines.append("  %s %s" % (key.upper().replace("_", " "),
                                      p[key]))
    if p["variables"]:
        lines.append("  variables: %s" % ", ".join(p["variables"]))
    if p["attributes"]:
        lines.append("  attributes: %s" % ", ".join(p["attributes"]))
    tr = model["translation"]
    lines.append("Translation: %s -> %s (%s)" % (
        tr["confidence"], tr["datasource"], tr["query_type"]))
    for tgt in tr["targets"]:
        lines.append("  %s" % tgt["expr"] + (
            "   legend: %s" % tgt["legend"] if tgt["legend"] else ""))
    for k, v in tr["panel_hints"].items():
        lines.append("  panel %s: %s" % (k, v))
    for n in tr["notes"]:
        lines.append("  . %s" % n)
    return "\n".join(lines)
