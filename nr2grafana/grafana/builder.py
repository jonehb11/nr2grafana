"""Grafana dashboard builder.

Turns an NRDashboard (+ per-query Translations) into Grafana dashboard JSON
(schemaVersion 39, importable into Grafana 10.3+ via UI or API).
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from ..model import NRDashboard, NRPage, NRVariable, NRWidget
from ..nrql.parser import Attr, Func, NrqlParseError, parse_nrql
from ..translate.common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW, UNTRANSLATABLE, Translation, map_attr,
    worst, _VAR_RE,
)
from ..translate.router import translate_query


def _nr_vars_to_grafana(text: str, cfg: Dict[str, Any]) -> str:
    """{{var}} / {{{var}}} -> $var (Grafana variable syntax)."""
    renames = cfg.get("var_renames") or {}
    return _VAR_RE.sub(
        lambda m: "$" + renames.get(m.group(1), m.group(1)), text or "")


SCHEMA_VERSION = 39

# NR unit -> Grafana unit id
NR_UNIT_MAP = {
    "COUNT": "short", "PERCENTAGE": "percent", "MS": "ms", "SECONDS": "s",
    "BYTES": "bytes", "BITS": "bits", "BYTES_PER_SECOND": "Bps",
    "BITS_PER_SECOND": "bps", "REQUESTS_PER_SECOND": "reqps",
    "PAGES_PER_SECOND": "ops", "OPERATIONS_PER_SECOND": "ops",
    "MESSAGES_PER_SECOND": "mps", "TIMESTAMP": "dateTimeAsIso",
    "CELSIUS": "celsius", "FAHRENHEIT": "fahrenheit", "HERTZ": "hertz",
    "APDEX": "short",
}

_SEVERITY_COLOR = {"WARNING": "yellow", "CRITICAL": "red",
                   "NOT_ALERTING": "green", "warning": "yellow",
                   "critical": "red", "success": "green",
                   "unavailable": "text"}


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")
    if not s:
        # Non-ASCII-only names (e.g. CJK) must not all collapse to the same
        # slug; derive a stable identifier from the original text.
        digest = hashlib.md5((text or "").encode("utf-8")).hexdigest()[:8]
        s = "dashboard-" + digest
    return s[:max_len]


# ---------------------------------------------------------------------------
# Datasource refs
# ---------------------------------------------------------------------------

_DS_VAR_NAMES = {"prometheus": "datasource", "loki": "loki_datasource",
                 "tempo": "tempo_datasource",
                 "newrelic": "newrelic_datasource"}


class _Build:
    """Mutable state for one output dashboard."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.panel_id = 0
        self.used_ds: List[str] = []
        self.report: List[Dict[str, Any]] = []
        self.timefroms: List[str] = []
        # (panel, range) pairs so differing panels get timeFrom overrides
        # once the dashboard-level range is decided.
        self.panel_ranges: List[Tuple[Dict[str, Any], str]] = []

    def next_id(self) -> int:
        self.panel_id += 1
        return self.panel_id

    def ds_ref(self, family: str) -> Dict[str, str]:
        if family not in self.used_ds:
            self.used_ds.append(family)
        ds = self.cfg["datasources"].get(family, {})
        return {"type": ds.get("type", family), "uid": ds.get("uid", "")}


# ---------------------------------------------------------------------------
# fieldConfig / options scaffolding
# ---------------------------------------------------------------------------

def _base_thresholds() -> Dict[str, Any]:
    return {"mode": "absolute",
            "steps": [{"color": "green", "value": None}]}


def _timeseries_custom(fill: int = 10, draw: str = "line",
                       stacking: str = "none") -> Dict[str, Any]:
    return {
        "drawStyle": draw, "lineInterpolation": "linear", "lineWidth": 1,
        "fillOpacity": fill, "gradientMode": "none", "spanNulls": False,
        "showPoints": "auto", "pointSize": 5, "barAlignment": 0,
        "stacking": {"mode": stacking, "group": "A"},
        "axisPlacement": "auto", "axisLabel": "", "axisColorMode": "text",
        "axisBorderShow": False, "axisCenteredZero": False,
        "scaleDistribution": {"type": "linear"},
        "hideFrom": {"tooltip": False, "viz": False, "legend": False},
        "insertNulls": False, "thresholdsStyle": {"mode": "off"},
    }


def _legend(placement: str = "bottom", show: bool = True) -> Dict[str, Any]:
    return {"displayMode": "list", "placement": placement,
            "showLegend": show, "calcs": []}


# ---------------------------------------------------------------------------
# Widget conversion
# ---------------------------------------------------------------------------

def _grid_pos(widget: NRWidget, cfg: Dict[str, Any]) -> Dict[str, int]:
    lay = widget.layout or {}
    hm = int(cfg.get("row_height_units", 3))
    col = int(lay.get("column", 1) or 1)
    row = int(lay.get("row", 1) or 1)
    width = int(lay.get("width", 4) or 4)
    height = int(lay.get("height", 3) or 3)
    return {"x": (col - 1) * 2, "y": (row - 1) * hm,
            "w": max(1, min(24, width * 2)), "h": max(2, height * hm)}


def _panel_type_for(viz_id: str) -> str:
    return {
        "viz.line": "timeseries", "viz.area": "timeseries",
        "viz.stacked-bar": "timeseries", "viz.bar": "bargauge",
        "viz.billboard": "stat", "viz.bullet": "gauge",
        "viz.pie": "piechart", "viz.table": "table",
        "viz.markdown": "text", "viz.heatmap": "heatmap",
        "viz.histogram": "histogram", "viz.json": "table",
        "viz.event-feed": "table", "logger.log-table-widget": "logs",
    }.get(viz_id, "")


def _apply_notes_to_panel(panel: Dict[str, Any], trans: List[Translation],
                          widget: NRWidget, b: _Build) -> None:
    """unit:/timefrom: notes -> panel settings.

    Unit notes are matched to refIds (flat order mirrors _make_targets);
    when targets carry different units, the first becomes the panel default
    and the rest get byFrameRefID field overrides.
    """
    flat: List[Translation] = []
    for t in trans:
        flat.append(t)
        flat.extend(t.extra)
    default_unit = ""
    for idx, t in enumerate(flat):
        unit = next((n.split(":", 1)[1] for n in t.notes
                     if n.startswith("unit:")), "")
        if not unit:
            continue
        if not default_unit:
            default_unit = unit
            panel["fieldConfig"]["defaults"].setdefault("unit", unit)
        elif unit != default_unit:
            ref = chr(ord("A") + idx) if idx < 26 else "T%d" % idx
            panel["fieldConfig"]["overrides"].append({
                "matcher": {"id": "byFrameRefID", "options": ref},
                "properties": [{"id": "unit", "value": unit}],
            })
    for t in trans:
        for note in t.notes:
            if note.startswith("timefrom:"):
                rng = note.split(":", 1)[1]
                b.timefroms.append(rng)
                b.panel_ranges.append((panel, rng))


def _describe(widget: NRWidget, trans: List[Translation]) -> str:
    lines: List[str] = []
    conf = EXACT
    for t in trans:
        conf = worst(conf, t.confidence)
    lines.append("Migrated from New Relic (%s). Confidence: %s."
                 % (widget.viz_id or "widget", conf))
    for i, nq in enumerate(widget.nrql_queries):
        lines.append("NRQL[%d]: %s" % (i, nq.get("query", "")))
    seen = set()
    for t in trans:
        for note in t.notes:
            if ":" in note and note.split(":", 1)[0] in (
                    "unit", "timefrom", "maxlines", "limit", "panel-hint"):
                continue
            if note not in seen:
                seen.add(note)
                lines.append("- " + note)
    return "\n".join(lines)


def _make_targets(trans: List[Translation], b: _Build) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    flat: List[Translation] = []
    for t in trans:
        flat.append(t)
        flat.extend(t.extra)
    for idx, t in enumerate(flat):
        ref = chr(ord("A") + idx) if idx < 26 else "T%d" % idx
        if t.datasource == "tempo":
            tgt: Dict[str, Any] = {
                "refId": ref, "datasource": b.ds_ref("tempo"),
                "queryType": "traceql", "query": t.expr,
                "tableType": "traces", "filters": [], "limit": 20,
            }
            for note in t.notes:
                if note.startswith("limit:"):
                    tgt["limit"] = int(note.split(":")[1])
            targets.append(tgt)
            continue
        if t.datasource == "loki":
            tgt = {"refId": ref, "datasource": b.ds_ref("loki"),
                   "expr": t.expr, "queryType": t.query_type,
                   "legendFormat": t.legend or "", "editorMode": "code"}
            for note in t.notes:
                if note.startswith("maxlines:"):
                    tgt["maxLines"] = int(note.split(":")[1])
            targets.append(tgt)
            continue
        if t.datasource == "newrelic":
            targets.append({"refId": ref, "datasource": b.ds_ref("newrelic"),
                            "queryText": t.expr, "useGrafanaTime": True})
            continue
        tgt = {"refId": ref, "datasource": b.ds_ref("prometheus"),
               "expr": t.expr, "legendFormat": t.legend or "__auto",
               "editorMode": "code", "range": t.query_type == "range",
               "instant": t.query_type == "instant",
               "format": "time_series"}
        targets.append(tgt)
    return targets


def _panel_options(ptype: str, widget: NRWidget,
                   trans: List[Translation]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Returns (options, fieldConfig) for the panel type."""
    rc = widget.raw_configuration or {}
    defaults: Dict[str, Any] = {
        "color": {"mode": "palette-classic"},
        "thresholds": _base_thresholds(),
        "mappings": [],
    }
    options: Dict[str, Any] = {}

    # NR units override
    unit = ((rc.get("units") or {}).get("unit") or "").upper()
    if unit in NR_UNIT_MAP:
        defaults["unit"] = NR_UNIT_MAP[unit]

    # y-axis min/max
    y = rc.get("yAxisLeft") or {}
    if isinstance(y.get("min"), (int, float)):
        defaults["min"] = y["min"]
    if isinstance(y.get("max"), (int, float)):
        defaults["max"] = y["max"]

    legend_enabled = (rc.get("legend") or {}).get("enabled", True)

    if ptype == "timeseries":
        fill = 10
        draw, stacking = "line", "none"
        if widget.viz_id == "viz.area":
            fill = 30
        if widget.viz_id == "viz.stacked-bar":
            draw, stacking, fill = "bars", "normal", 80
        defaults["custom"] = _timeseries_custom(fill, draw, stacking)
        # NR line thresholds -> threshold area lines
        thr = rc.get("thresholds")
        if isinstance(thr, dict) and thr.get("thresholds"):
            steps = [{"color": "green", "value": None}]
            for item in sorted(
                    [x for x in thr["thresholds"]
                     if isinstance(x.get("from"), (int, float))],
                    key=lambda x: x["from"]):
                steps.append({"color": _SEVERITY_COLOR.get(
                    item.get("severity", ""), "red"),
                    "value": item["from"]})
            if len(steps) > 1:
                defaults["thresholds"] = {"mode": "absolute", "steps": steps}
                defaults["custom"]["thresholdsStyle"] = {"mode": "line"}
        options = {"legend": _legend(show=bool(legend_enabled)),
                   "tooltip": {"mode": "multi", "sort": "desc"}}

    elif ptype == "stat":
        defaults["color"] = {"mode": "thresholds"}
        thr = rc.get("thresholds")
        if isinstance(thr, list) and thr:
            steps = [{"color": "green", "value": None}]
            for item in sorted(
                    [x for x in thr
                     if isinstance(x.get("value"), (int, float))],
                    key=lambda x: x["value"]):
                steps.append({"color": _SEVERITY_COLOR.get(
                    item.get("alertSeverity", ""), "red"),
                    "value": item["value"]})
            defaults["thresholds"] = {"mode": "absolute", "steps": steps}
        options = {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"],
                              "fields": ""},
            "orientation": "auto", "textMode": "auto", "wideLayout": True,
            "colorMode": "value", "graphMode": "none", "justifyMode": "auto",
            "showPercentChange": False,
            "percentChangeColorMode": "standard",
        }

    elif ptype == "gauge":
        defaults["color"] = {"mode": "thresholds"}
        limit = rc.get("limit")
        if isinstance(limit, (int, float)):
            defaults["max"] = limit
        elif trans:
            trans[0].note("viz.bullet without a limit: gauge max left "
                          "unset (auto-scales); set fieldConfig max to "
                          "restore the target line")
        options = {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"],
                              "fields": ""},
            "orientation": "auto", "showThresholdLabels": False,
            "showThresholdMarkers": True, "sizing": "auto",
        }

    elif ptype == "bargauge":
        defaults["color"] = {"mode": "thresholds"}
        options = {
            "reduceOptions": {"values": True, "calcs": [], "fields": ""},
            "orientation": "horizontal", "displayMode": "gradient",
            "valueMode": "color", "namePlacement": "auto",
            "showUnfilled": True, "sizing": "auto",
        }

    elif ptype == "piechart":
        defaults["custom"] = {"hideFrom": {"tooltip": False, "viz": False,
                                           "legend": False}}
        options = {
            "reduceOptions": {"values": True, "calcs": [], "fields": ""},
            "pieType": "pie", "displayLabels": [],
            "tooltip": {"mode": "single", "sort": "none"},
            "legend": {"displayMode": "list", "placement": "right",
                       "showLegend": bool(legend_enabled), "values": []},
        }

    elif ptype == "table":
        defaults["custom"] = {"align": "auto", "cellOptions": {"type": "auto"},
                              "inspect": False, "filterable": True}
        options = {"showHeader": True, "cellHeight": "sm",
                   "footer": {"show": False, "reducer": ["sum"],
                              "countRows": False, "fields": ""},
                   "sortBy": []}
        srt = rc.get("initialSorting") or {}
        if srt.get("name"):
            options["sortBy"] = [{"displayName": srt["name"],
                                  "desc": srt.get("direction") == "desc"}]

    elif ptype == "heatmap":
        options = {
            "calculate": False,
            "color": {"mode": "scheme", "scheme": "Spectral", "steps": 64,
                      "fill": "dark-orange", "reverse": False,
                      "exponent": 0.5},
            "cellGap": 1, "filterValues": {"le": 1e-9},
            "yAxis": {"axisPlacement": "left", "reverse": False},
            "rowsFrame": {"layout": "auto"},
            "tooltip": {"mode": "single", "showColorScale": False,
                        "yHistogram": False},
            "legend": {"show": True}, "showValue": "never",
        }
        defaults["custom"] = {"hideFrom": {"tooltip": False, "viz": False,
                                           "legend": False},
                              "scaleDistribution": {"type": "linear"}}

    elif ptype == "histogram":
        defaults["custom"] = {"lineWidth": 1, "fillOpacity": 80,
                              "gradientMode": "none",
                              "hideFrom": {"tooltip": False, "viz": False,
                                           "legend": False}}
        options = {"bucketCount": 30, "bucketOffset": 0, "combine": False,
                   "legend": _legend(show=bool(legend_enabled)),
                   "tooltip": {"mode": "single", "sort": "none"}}

    elif ptype == "logs":
        options = {"showTime": True, "showLabels": False,
                   "showCommonLabels": False, "wrapLogMessage": True,
                   "prettifyLogMessage": False, "enableLogDetails": True,
                   "dedupStrategy": "none", "sortOrder": "Descending"}

    return options, {"defaults": defaults, "overrides": []}


def _convert_widget(widget: NRWidget, b: _Build,
                    page_name: str) -> Dict[str, Any]:
    cfg = b.cfg
    panel: Dict[str, Any] = {
        "id": b.next_id(),
        "title": _nr_vars_to_grafana(widget.title or "", cfg),
        "gridPos": _grid_pos(widget, cfg),
        "transparent": False,
        "links": [],
        "transformations": [],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {},
        "targets": [],
    }

    # Markdown widgets: no queries.
    if widget.viz_id == "viz.markdown":
        panel["type"] = "text"
        panel["transparent"] = True
        panel["options"] = {
            "mode": "markdown",
            "content": _nr_vars_to_grafana(
                (widget.raw_configuration or {}).get("text", ""), cfg),
        }
        del panel["targets"]
        _report(b, page_name, widget, panel, EXACT, [])
        return panel

    queries = [nq.get("query", "") for nq in widget.nrql_queries
               if nq.get("query")]

    # Non-NRQL widgets (service maps, inventory, custom viz, legacy metric
    # charts) cannot be converted.
    if not queries:
        reason = ("widget type %r has no NRQL queries (service map / "
                  "inventory / legacy metric chart / custom visualization); "
                  "recreate manually" % (widget.viz_id or "unknown"))
        return _fallback_panel(panel, widget, b, page_name, [reason], [])

    trans = [translate_query(qtext, cfg) for qtext in queries]

    conf = EXACT
    for t in trans:
        conf = worst(conf, t.confidence)

    if conf == UNTRANSLATABLE:
        reasons = [n for t in trans for n in t.notes]
        return _fallback_panel(panel, widget, b, page_name, reasons, queries)

    # Panel type: NR viz mapping, overridden by translation hints. Known
    # no-panel viz ids (funnel etc.) never reach here — their queries are
    # untranslatable — so an unmapped id at this point is a custom viz.
    mapped_ptype = _panel_type_for(widget.viz_id)
    if not mapped_ptype and trans and widget.viz_id not in (
            "viz.funnel", "topology.service-map", "infra.inventory"):
        trans[0].note("unrecognized NR visualization id %r; rendered as a "
                      "timeseries panel — adjust manually if needed"
                      % (widget.viz_id or "?"), NEEDS_REVIEW)
        conf = worst(conf, NEEDS_REVIEW)
    ptype = mapped_ptype or "timeseries"
    hints = {n.split(":", 1)[1] for t in trans for n in t.notes
             if n.startswith("panel-hint:")}
    if "heatmap" in hints:
        ptype = "heatmap"
    if "logs" in hints:
        ptype = "logs"
    if "traces" in hints:
        ptype = "table"

    # Instant table/pie/bar targets from prometheus should come back as table
    # frames for correct rendering.
    panel["type"] = ptype
    options, field_config = _panel_options(ptype, widget, trans)
    panel["options"] = options
    panel["fieldConfig"] = field_config
    panel["targets"] = _make_targets(trans, b)

    if ptype == "heatmap":
        for tgt in panel["targets"]:
            if tgt.get("expr") and "datasource" in tgt \
                    and tgt["datasource"].get("type") == "prometheus":
                tgt["format"] = "heatmap"
                tgt["legendFormat"] = "{{le}}"

    # Table panels want table frames from instant queries; piechart and
    # bargauge keep time_series format so series keep their label names.
    if ptype == "table":
        for tgt in panel["targets"]:
            if tgt.get("instant"):
                tgt["format"] = "table"
        panel["transformations"] = [
            {"id": "merge", "options": {}},
            {"id": "organize",
             "options": {"excludeByName": {"Time": True},
                         "renameByName": {}}}]

    # Panel-level datasource: first target's datasource (mixed if several).
    ds_set = {(t["datasource"]["type"], t["datasource"]["uid"])
              for t in panel["targets"] if "datasource" in t}
    if len(ds_set) == 1:
        panel["datasource"] = panel["targets"][0]["datasource"]
    elif len(ds_set) > 1:
        panel["datasource"] = {"type": "datasource", "uid": "-- Mixed --"}

    _apply_notes_to_panel(panel, trans, widget, b)
    panel["description"] = _describe(widget, trans)
    if conf == NEEDS_REVIEW:
        panel["title"] = (panel["title"] + " [REVIEW]").strip()
    _report(b, page_name, widget, panel, conf, trans)
    return panel


def _fallback_panel(panel: Dict[str, Any], widget: NRWidget, b: _Build,
                    page_name: str, reasons: List[str],
                    queries: List[str]) -> Dict[str, Any]:
    cfg = b.cfg
    if cfg.get("passthrough_fallback") and queries:
        panel["type"] = "table"
        panel["datasource"] = b.ds_ref("newrelic")
        panel["targets"] = [
            {"refId": chr(ord("A") + i), "datasource": b.ds_ref("newrelic"),
             "queryText": qt, "useGrafanaTime": True}
            for i, qt in enumerate(queries)]
        panel["options"] = {"showHeader": True, "cellHeight": "sm",
                            "footer": {"show": False, "reducer": ["sum"],
                                       "countRows": False, "fields": ""}}
        panel["fieldConfig"] = {"defaults": {}, "overrides": []}
        panel["title"] = (panel["title"] + " [NRQL PASSTHROUGH]").strip()
        panel["description"] = (
            "Untranslatable query kept on the New Relic Grafana datasource "
            "plugin (install nrgrafanaplugin-newrelic-datasource).\n"
            + "\n".join("- " + r for r in reasons))
        _report(b, page_name, widget, panel, UNTRANSLATABLE, [],
                fallback="nrql-passthrough", extra_notes=reasons)
        return panel

    body = ["### Not automatically translatable", ""]
    for qt in queries:
        body.append("```\n%s\n```" % qt)
    body.append("")
    body.extend("- " + r for r in reasons)
    body.append("")
    body.append("_Recreate this widget manually or enable "
                "`passthrough_fallback` in the converter config._")
    panel["type"] = "text"
    panel["options"] = {"mode": "markdown", "content": "\n".join(body)}
    panel["fieldConfig"] = {"defaults": {}, "overrides": []}
    panel.pop("targets", None)
    panel["title"] = (panel["title"] + " [MANUAL]").strip()
    _report(b, page_name, widget, panel, UNTRANSLATABLE, [],
            fallback="text-placeholder", extra_notes=reasons)
    return panel


def _report(b: _Build, page: str, widget: NRWidget, panel: Dict[str, Any],
            conf: str, trans: List[Translation], fallback: str = "",
            extra_notes: Optional[List[str]] = None) -> None:
    entry = {
        "page": page,
        "widget": widget.title or "(untitled)",
        "visualization": widget.viz_id,
        "panel_id": panel["id"],
        "panel_type": panel.get("type"),
        "confidence": conf,
        "nrql": [nq.get("query", "") for nq in widget.nrql_queries],
        "queries": [],
        "notes": [],
    }
    for t in trans:
        for x in [t] + t.extra:
            entry["queries"].append(
                {"datasource": x.datasource, "expr": x.expr,
                 "type": x.query_type})
        entry["notes"].extend(t.notes)
    if extra_notes:
        entry["notes"].extend(extra_notes)
    if fallback:
        entry["fallback"] = fallback
    b.report.append(entry)


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

def _convert_variable(v: NRVariable, b: _Build,
                      cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = (cfg.get("var_renames") or {}).get(v.name, v.name)
    common = {"name": name, "label": v.title or v.name, "hide": 0,
              "skipUrlSync": False}
    if v.type == "ENUM":
        options = [{"selected": False, "text": it.get("title") or
                    str(it.get("value")), "value": str(it.get("value"))}
                   for it in v.items]
        current: Dict[str, Any] = {}
        if v.default_values:
            default = v.default_values[0]
            text = next((o["text"] for o in options
                         if o["value"] == default), default)
            current = {"selected": True, "text": text, "value": default}
            for o in options:
                o["selected"] = o["value"] == default
        elif options:
            current = {"selected": True, "text": options[0]["text"],
                       "value": options[0]["value"]}
        out = dict(common, type="custom",
                   query=", ".join("%s : %s" % (o["text"], o["value"])
                                   for o in options),
                   multi=v.is_multi, includeAll=v.is_multi,
                   options=options, current=current)
        return out
    if v.type == "STRING":
        val = v.default_values[0] if v.default_values else ""
        return dict(common, type="textbox", query=val,
                    current={"selected": False, "text": val, "value": val},
                    options=[])
    if v.type == "NRQL":
        # Try to derive label_values() from a uniques()-style NRQL variable.
        nrql = (v.nrql_query or {}).get("query", "")
        label = None
        try:
            pq = parse_nrql(nrql)
            for item in pq.select:
                if isinstance(item.expr, Func) and item.expr.name in (
                        "uniques", "uniquecount", "keyset") and item.expr.args:
                    arg = item.expr.args[0]
                    if isinstance(arg, Attr):
                        label, _ = map_attr(arg.name, cfg)
                        break
        except NrqlParseError:
            pass
        if label:
            query = "label_values(%s)" % label
            return dict(
                common, type="query",
                datasource=b.ds_ref("prometheus"),
                query={"query": query,
                       "refId": "PrometheusVariableQueryEditor-VariableQuery"},
                definition=query, refresh=2, regex="", sort=1,
                multi=v.is_multi, includeAll=v.is_multi, allValue=".*",
                current={"selected": False, "text": ["All"],
                         "value": ["$__all"]} if v.is_multi else {},
                options=[])
        # Unmappable NRQL variable -> textbox with a warning.
        return dict(common, type="textbox", query="",
                    label=(v.title or v.name) + " (was NRQL variable)",
                    current={"selected": False, "text": "", "value": ""},
                    options=[])
    return None


def _datasource_variables(b: _Build) -> List[Dict[str, Any]]:
    out = []
    for family in b.used_ds:
        ds = b.cfg["datasources"].get(family, {})
        uid = ds.get("uid", "")
        var_name = _DS_VAR_NAMES.get(family, family + "_datasource")
        if uid == "${%s}" % var_name:
            out.append({
                "type": "datasource", "name": var_name,
                "label": {"prometheus": "Metrics (Mimir)",
                          "loki": "Logs (Loki)",
                          "tempo": "Traces (Tempo)",
                          "newrelic": "New Relic"}.get(family, family),
                "query": ds.get("type", family),
                "regex": "", "refresh": 1, "multi": False,
                "includeAll": False, "current": {}, "options": [],
                "hide": 0, "skipUrlSync": False,
            })
    return out


# ---------------------------------------------------------------------------
# Dashboard assembly
# ---------------------------------------------------------------------------

def _dashboard_shell(title: str, uid: str, description: str,
                     cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": None,
        "uid": uid,
        "title": title,
        "description": description,
        "tags": list(cfg.get("tags") or []),
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {},
        "refresh": "1m",
        "schemaVersion": SCHEMA_VERSION,
        "version": 0,
        "fiscalYearStartMonth": 0,
        "liveNow": False,
        "weekStart": "",
        "templating": {"list": []},
        "annotations": {"list": [{
            "builtIn": 1,
            "datasource": {"type": "grafana", "uid": "-- Grafana --"},
            "enable": True, "hide": True,
            "iconColor": "rgba(0, 211, 255, 1)",
            "name": "Annotations & Alerts", "type": "dashboard",
        }]},
        "links": [],
        "panels": [],
    }


def _page_panels(page: NRPage, b: _Build) -> List[Dict[str, Any]]:
    widgets = sorted(page.widgets,
                     key=lambda w: (w.layout.get("row", 1),
                                    w.layout.get("column", 1)))
    return [_convert_widget(w, b, page.name) for w in widgets]


def _finish_dashboard(dash: Dict[str, Any], b: _Build,
                      nr_vars: List[NRVariable]) -> None:
    # Convert NR variables FIRST: query variables may register additional
    # datasource families (b.ds_ref), which must get datasource variables.
    converted: List[Dict[str, Any]] = []
    for v in nr_vars:
        gv = _convert_variable(v, b, b.cfg)
        if gv:
            converted.append(gv)
    tvars: List[Dict[str, Any]] = []
    tvars.extend(_datasource_variables(b))
    tvars.extend(converted)
    tvars.extend(copy.deepcopy(b.cfg.get("extra_variables") or []))
    dash["templating"]["list"] = tvars
    # Most common SINCE across widgets becomes the dashboard range; panels
    # whose SINCE differs get a relative timeFrom override where possible
    # (Grafana timeFrom accepts "30m"/"1h" style values, not now/d).
    if b.timefroms:
        best = max(set(b.timefroms), key=b.timefroms.count)
        dash["time"] = {"from": best, "to": "now"}
        for panel, rng in b.panel_ranges:
            if rng != best and rng.startswith("now-") and "/" not in rng:
                panel["timeFrom"] = rng[len("now-"):]
                panel["hideTimeOverride"] = False


def build_dashboards(nr: NRDashboard, cfg: Dict[str, Any]) \
        -> List[Tuple[str, Dict[str, Any], List[Dict[str, Any]]]]:
    """Convert one NR dashboard. Returns [(suggested_filename, dashboard
    JSON dict, report entries)]. page_strategy 'rows' emits one dashboard;
    'split' emits one per page."""
    # NR variables whose names collide with the generated datasource
    # variables get renamed everywhere (queries, markdown, titles).
    reserved = set(_DS_VAR_NAMES.values())
    renames = {v.name: v.name + "_var" for v in nr.variables
               if v.name in reserved}
    if renames:
        cfg = dict(cfg, var_renames=renames)

    strategy = cfg.get("page_strategy", "rows")
    base_slug = slugify(nr.name, 30)
    results: List[Tuple[str, Dict[str, Any], List[Dict[str, Any]]]] = []

    if strategy == "split" and len(nr.pages) > 1:
        tag = "nr-" + base_slug
        for page in nr.pages:
            b = _Build(cfg)
            title = "%s / %s" % (nr.name, page.name)
            uid = slugify("nr-%s-%s" % (base_slug, page.name))
            dash = _dashboard_shell(title, uid, nr.description, cfg)
            dash["tags"].append(tag)
            dash["links"] = [{"title": "Pages", "type": "dashboards",
                              "tags": [tag], "asDropdown": True,
                              "includeVars": True, "keepTime": True,
                              "icon": "external link", "targetBlank": False,
                              "url": ""}]
            dash["panels"] = _page_panels(page, b)
            _finish_dashboard(dash, b, nr.variables)
            results.append(("%s--%s.json" % (base_slug, slugify(page.name, 30)),
                            dash, b.report))
        return results

    b = _Build(cfg)
    uid = slugify("nr-" + base_slug)
    dash = _dashboard_shell(nr.name, uid, nr.description, cfg)
    if len(nr.pages) <= 1:
        if nr.pages:
            dash["panels"] = _page_panels(nr.pages[0], b)
    else:
        panels: List[Dict[str, Any]] = []
        y = 0
        for idx, page in enumerate(nr.pages):
            row = {"id": b.next_id(), "type": "row",
                   "title": page.name, "collapsed": idx > 0,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
                   "panels": []}
            children = _page_panels(page, b)
            height = max((p["gridPos"]["y"] + p["gridPos"]["h"]
                          for p in children), default=0)
            if idx == 0:
                for p in children:
                    p["gridPos"]["y"] += y + 1
                panels.append(row)
                panels.extend(children)
                y += height + 1
            else:
                for p in children:
                    p["gridPos"]["y"] += y + 1
                row["panels"] = children
                panels.append(row)
                y += 1
        dash["panels"] = panels
    _finish_dashboard(dash, b, nr.variables)
    results.append((base_slug + ".json", dash, b.report))
    return results
