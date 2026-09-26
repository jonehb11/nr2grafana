"""Grafana dashboard builder.

Turns an NRDashboard (+ per-query Translations) into Grafana dashboard JSON
(schemaVersion 39, importable into Grafana 10.3+ via UI or API).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..model import NRDashboard, NRPage, NRVariable, NRWidget
from ..nrql.parser import Attr, Func, NrqlParseError, parse_nrql
from ..translate.common import select_label
from ..translate.common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW, UNTRANSLATABLE, Translation, map_attr,
    route_event_type, worst, _VAR_RE,
)
from ..translate.logs import variable_scope as loki_variable_scope
from ..translate.metrics import (
    nr_duration_to_grafana_range, nr_duration_to_prom,
    variable_scope as prom_variable_scope,
)
from ..translate.router import translate_query
from ..translate.traces import variable_field as tempo_variable_field


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
        self.refresh_ms: List[int] = []  # widget refreshInterval values
        # (panel, range) pairs so differing panels get timeFrom overrides
        # once the dashboard-level range is decided.
        self.panel_ranges: List[Tuple[Dict[str, Any], str]] = []
        # Variables whose values reach Grafana as a query interval or a
        # relative time (TIMESERIES {{v}}, SINCE {{v}}): New Relic wrote
        # their values as '5 minutes' / '1 hour ago'; Grafana needs 5m /
        # now-1h.
        self.interval_vars: set = set()
        self.range_vars: set = set()

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
    # New Relic's grid is 12 columns; a layout outside it (column 13,
    # column 5 + width 12) is clamped so Grafana's 24-column grid accepts
    # the panel (see _layout_notes).
    x = max(0, min(22, (col - 1) * 2))
    return {"x": x, "y": max(0, (row - 1) * hm),
            "w": max(1, min(24 - x, width * 2)), "h": max(2, height * hm)}


def _layout_notes(widget: NRWidget) -> List[str]:
    lay = widget.layout or {}
    col = int(lay.get("column", 1) or 1)
    width = int(lay.get("width", 4) or 4)
    if col < 1 or width < 1 or col + width - 1 > 12:
        return ["layout column %d width %d is outside New Relic's 12-column "
                "grid; the panel was clamped to fit (check its position)"
                % (col, width)]
    return []


_PANEL_TYPES = {
    "viz.line": "timeseries", "viz.area": "timeseries",
    "viz.stacked-bar": "timeseries", "viz.bar": "bargauge",
    "viz.billboard": "stat", "viz.billboard-comparison": "stat",
    "viz.bullet": "gauge", "viz.pie": "piechart", "viz.table": "table",
    "viz.markdown": "text", "viz.heatmap": "heatmap",
    "viz.histogram": "histogram", "viz.json": "table",
    "viz.event-feed": "table", "logger.log-table-widget": "logs",
    "viz.log-table": "logs", "viz.scatter": "timeseries",
    "viz.sparkline": "stat", "viz.traffic-light": "stat",
    "viz.timeslice": "timeseries", "viz.event-table": "table",
}

# NR widget kinds that have no Grafana panel at all (no NRQL to
# translate, or a visualization Grafana does not have). Value: the reason
# and the closest Grafana equivalent to rebuild by hand.
NO_PANEL_WIDGETS = {
    "viz.funnel": ("funnel charts are per-user step conversion over "
                   "event sequences", "no Grafana panel; keep the widget "
                   "in New Relic or rebuild from Faro/frontend events"),
    "topology.service-map": ("service maps are built from New Relic's "
                             "entity relationships",
                             "Grafana node graph panel fed by Tempo's "
                             "service graph (metrics-generator "
                             "service-graphs processor)"),
    "viz.service-map": ("service maps are built from New Relic's entity "
                        "relationships",
                        "Grafana node graph panel fed by Tempo's service "
                        "graph"),
    "infra.inventory": ("inventory widgets read New Relic's infrastructure "
                        "inventory, not NRDB",
                        "table panel over kube_*_info / node_uname_info "
                        "series"),
    "viz.inventory": ("inventory widgets read New Relic's infrastructure "
                      "inventory, not NRDB",
                      "table panel over kube_*_info / node_uname_info "
                      "series"),
    "viz.geo-map": ("geo maps need location attributes New Relic derives "
                    "from IP", "Grafana geomap panel over a metric with "
                    "location labels"),
    "viz.thresholds": ("alert-condition thresholds are New Relic alerting "
                       "objects", "Grafana Alerting"),
}


def _panel_type_for(viz_id: str) -> str:
    return _PANEL_TYPES.get(viz_id, "")


def widget_kind_reason(viz_id: str) -> Tuple[str, str]:
    """(reason, equivalent) for widget kinds Grafana cannot render."""
    if viz_id in NO_PANEL_WIDGETS:
        return NO_PANEL_WIDGETS[viz_id]
    if viz_id and viz_id not in _PANEL_TYPES and "." in viz_id \
            and not viz_id.startswith("viz."):
        return ("custom visualization %r (a New Relic nerdpack) has no "
                "Grafana equivalent" % viz_id,
                "rebuild with a Grafana panel or plugin of the same shape")
    return ("", "")


_LEGEND_LABEL_RE = re.compile(r"\{\{[^}]*\}\}")


def _select_item_labels(queries: List[str]) -> List[str]:
    """Alias-or-expression label of every aggregation in the widget's NRQL,
    in SELECT order."""
    out: List[str] = []
    for qtext in queries:
        try:
            q = parse_nrql(qtext)
        except NrqlParseError:
            continue
        for item in q.select:
            if isinstance(item.expr, Func):
                out.append(item.alias or select_label(item.expr))
    return out


def _facet_aliases(queries: List[str],
                   trans: List[Translation]) -> Dict[str, str]:
    """FACET name AS 'Endpoint' -> {'http_route': 'Endpoint'}: the group
    label column takes the facet alias."""
    out: Dict[str, str] = {}
    for qtext, t in zip(queries, trans):
        try:
            q = parse_nrql(qtext)
        except NrqlParseError:
            continue
        aliases = [f.alias for f in q.facet]
        if len(aliases) == len(t.group_by):
            for label, alias in zip(t.group_by, aliases):
                if alias and label:
                    out[label] = alias
    return out


def _table_column_names(targets: List[Dict[str, Any]],
                        queries: List[str],
                        trans: Optional[List[Translation]] = None
                        ) -> Dict[str, str]:
    """Names for the value columns of a table built from instant queries.
    Grafana calls them 'Value' / 'Value #A'; New Relic showed the alias or
    the aggregation, which the legend (minus its {{label}} parts) or the
    SELECT item provides. Facet aliases rename the group columns."""
    labels = _select_item_labels(queries)
    rename: Dict[str, str] = dict(_facet_aliases(queries, trans or []))
    for i, tgt in enumerate(targets):
        legend = _LEGEND_LABEL_RE.sub("", tgt.get("legendFormat") or "")
        legend = legend.strip(" /:-")
        if legend in ("", "__auto") and len(labels) == len(targets):
            legend = labels[i]
        if not legend or legend == "__auto":
            continue
        rename["Value #%s" % (tgt.get("refId") or "")] = legend
        if len(targets) == 1:
            rename["Value"] = legend
    return rename


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
            matcher = {"id": "byFrameRefID", "options": ref}
            if panel.get("type") == "table":
                # The merge transformation drops the frames' refIds; the
                # renamed value column is what survives.
                for tr in panel.get("transformations") or []:
                    names = (tr.get("options") or {}).get("renameByName") \
                        if tr.get("id") == "organize" else None
                    col = (names or {}).get("Value #%s" % ref)
                    if col:
                        matcher = {"id": "byName", "options": col}
            panel["fieldConfig"]["overrides"].append({
                "matcher": matcher,
                "properties": [{"id": "unit", "value": unit}],
            })
    rc = widget.raw_configuration or {}
    for guid in rc.get("linkedEntityGuids") or []:
        if isinstance(guid, str) and guid:
            panel.setdefault("links", []).append({
                "title": "New Relic entity", "targetBlank": True,
                "url": "https://one.newrelic.com/redirect/entity/%s" % guid})
    refresh = rc.get("refreshInterval")
    if isinstance(refresh, (int, float)) and refresh > 0:
        b.refresh_ms.append(int(refresh))
    ignore_picker = bool((rc.get("platformOptions") or {}).get(
        "ignoreTimeRange"))
    for t in trans:
        # SINCE yesterday UNTIL today: timeFrom now/d + timeShift 1d/d only
        # mean "the whole previous day" together, so the panel keeps its
        # own timeFrom whatever the dashboard range is.
        whole_unit = any(n.startswith("timeshift:") and "/" in n
                         for n in t.notes)
        for note in t.notes:
            if note.startswith("timefrom:") and ignore_picker:
                # NR "ignore time picker": the query's SINCE always applies.
                rng = note.split(":", 1)[1]
                panel["timeFrom"] = rng[len("now-"):] if (
                    rng.startswith("now-") and "/" not in rng) else rng
                panel["hideTimeOverride"] = False
                continue
            if note.startswith("timefrom:"):
                rng = note.split(":", 1)[1]
                if "$" in rng or whole_unit:
                    # SINCE {{var}} / {{n}} minutes ago / a whole calendar
                    # unit: a per-panel override, never the dashboard
                    # default.
                    panel["timeFrom"] = rng
                    panel["hideTimeOverride"] = False
                    b.range_vars.update(_VAR_NAME_RE.findall(rng))
                    continue
                b.timefroms.append(rng)
                b.panel_ranges.append((panel, rng))
            elif note.startswith("timeshift:"):
                panel["timeShift"] = note.split(":", 1)[1]
                panel["hideTimeOverride"] = False
            elif note.startswith("interval:"):
                # TIMESERIES <n unit> -> the panel's min interval.
                panel["interval"] = note.split(":", 1)[1]
                b.interval_vars.update(_VAR_NAME_RE.findall(panel["interval"]))


_VAR_NAME_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _grafana_span(value: Any, kind: str) -> str:
    """A New Relic variable value used as a Grafana interval or relative
    time: '5 minutes' -> '5m'; '1 hour ago' / 'today' -> 'now-1h' /
    'now/d'. Values that are not durations (a plain number in
    now-${n}m, free text) are left alone."""
    text = str(value).strip()
    if not text:
        return text
    if kind == "interval":
        span = nr_duration_to_prom(text)
        return span or text
    rng = nr_duration_to_grafana_range(text)
    if not rng:
        span = nr_duration_to_prom(text)
        rng = ("now-" + span) if span else ""
    return rng or text


def _span_variable_values(tvars: List[Dict[str, Any]], b: _Build) -> None:
    """Rewrite the values of variables that reach Grafana as intervals or
    relative times (see _Build.interval_vars / range_vars)."""
    for var in tvars:
        name = var.get("name") or ""
        kind = "interval" if name in b.interval_vars else (
            "range" if name in b.range_vars else "")
        if not kind:
            continue
        if var.get("type") == "custom":
            for opt in var.get("options") or []:
                opt["value"] = _grafana_span(opt.get("value"), kind)
            var["query"] = ", ".join(
                "%s : %s" % (o.get("text"), o.get("value"))
                for o in var.get("options") or [])
            cur = var.get("current") or {}
            if isinstance(cur.get("value"), str):
                cur["value"] = _grafana_span(cur["value"], kind)
        elif var.get("type") == "textbox":
            val = _grafana_span(var.get("query"), kind)
            var["query"] = val
            var["current"] = {"selected": False, "text": val, "value": val}


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
                    "unit", "timefrom", "timeshift", "maxlines", "limit",
                    "panel-hint"):
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
            if t.query_type == "traceql-metrics":
                targets.append({
                    "refId": ref, "datasource": b.ds_ref("tempo"),
                    "queryType": "traceql", "query": t.expr,
                    "metricsQueryType": "range", "filters": [],
                    "legendFormat": t.legend or "",
                })
                continue
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


# Grafana unit id -> (family, size of one unit in the family's base unit);
# thresholds and axis limits convert between units of one family.
_UNIT_BASE = {
    "ns": ("time", 1e-9), "µs": ("time", 1e-6), "ms": ("time", 1e-3),
    "s": ("time", 1.0), "m": ("time", 60.0), "h": ("time", 3600.0),
    "d": ("time", 86400.0),
    "percent": ("ratio", 0.01), "percentunit": ("ratio", 1.0),
    "bits": ("bytes", 0.125), "bytes": ("bytes", 1.0),
    "decbytes": ("bytes", 1.0), "kbytes": ("bytes", 1024.0),
    "mbytes": ("bytes", 1024.0 ** 2), "gbytes": ("bytes", 1024.0 ** 3),
    "deckbytes": ("bytes", 1e3), "decmbytes": ("bytes", 1e6),
    "decgbytes": ("bytes", 1e9),
    "bps": ("datarate", 1.0), "Bps": ("datarate", 8.0),
    "KBs": ("datarate", 8.0 * 1024), "MBs": ("datarate", 8.0 * 1024 ** 2),
    "Kbits": ("datarate", 1e3), "Mbits": ("datarate", 1e6),
    "reqps": ("rate", 1.0), "reqpm": ("rate", 1 / 60.0),
    "cps": ("rate", 1.0), "cpm": ("rate", 1 / 60.0), "ops": ("rate", 1.0),
    "mps": ("rate", 1.0), "rps": ("rate", 1.0),
}


def _unit_factor(from_unit: str, to_unit: str) -> Optional[float]:
    """Multiply a number in from_unit by this to express it in to_unit;
    None when the units are not of one family."""
    a, b = _UNIT_BASE.get(from_unit), _UNIT_BASE.get(to_unit)
    if not a or not b or a[0] != b[0]:
        return None
    return a[1] / b[1]


def _panel_options(ptype: str, widget: NRWidget, trans: List[Translation]) \
        -> Tuple[Dict[str, Any], Dict[str, Any], List[Tuple[str, str]]]:
    """Returns (options, fieldConfig, notes) for the panel type."""
    rc = widget.raw_configuration or {}
    defaults: Dict[str, Any] = {
        "color": {"mode": "palette-classic"},
        "thresholds": _base_thresholds(),
        "mappings": [],
    }
    options: Dict[str, Any] = {}
    overrides: List[Dict[str, Any]] = []
    unit_notes: List[Tuple[str, str]] = []

    # Units: the query decides what the values are (seconds for an OTel
    # duration histogram even when the NR widget said MS); the NR widget
    # unit applies only when the translation has none. NR thresholds and
    # axis limits were written in the NR unit: convert them when the two
    # units are of one family, else say so.
    unit = ((rc.get("units") or {}).get("unit") or "").upper()
    nr_unit = NR_UNIT_MAP.get(unit, "")
    trans_unit = next((n.split(":", 1)[1] for t in trans for x in [t] + t.extra
                       for n in x.notes if n.startswith("unit:")), "")
    factor = _unit_factor(nr_unit, trans_unit) if nr_unit and trans_unit \
        else None
    if trans_unit:
        defaults["unit"] = trans_unit
        if nr_unit and nr_unit != trans_unit:
            if factor is None:
                unit_notes.append((
                    "widget unit %s replaced by the query's unit %s; "
                    "thresholds and axis limits were left as written — "
                    "check them" % (unit, trans_unit), NEEDS_REVIEW))
            elif factor != 1:
                unit_notes.append((
                    "widget unit %s: the query returns %s, so thresholds "
                    "and axis limits were converted (x%g)"
                    % (unit, trans_unit, factor), APPROXIMATE))
    elif nr_unit:
        defaults["unit"] = nr_unit

    def conv(v: Any) -> Any:
        return v * factor if factor not in (None, 1) else v

    # y-axis min/max
    y = rc.get("yAxisLeft") or {}
    if isinstance(y.get("min"), (int, float)):
        defaults["min"] = conv(y["min"])
    if isinstance(y.get("max"), (int, float)):
        defaults["max"] = conv(y["max"])
    if y.get("zero") is True and "min" not in defaults:
        defaults["min"] = 0  # NR "start y-axis at zero"

    legend_enabled = (rc.get("legend") or {}).get("enabled", True)

    if ptype == "timeseries":
        fill = 10
        draw, stacking = "line", "none"
        if widget.viz_id == "viz.area":
            fill = 30
        if widget.viz_id == "viz.stacked-bar":
            draw, stacking, fill = "bars", "normal", 80
        defaults["custom"] = _timeseries_custom(fill, draw, stacking)
        if widget.viz_id == "viz.scatter":
            defaults["custom"]["drawStyle"] = "points"
            defaults["custom"]["showPoints"] = "always"
            defaults["custom"]["fillOpacity"] = 0
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
                    "value": conv(item["from"])})
            if len(steps) > 1:
                defaults["thresholds"] = {"mode": "absolute", "steps": steps}
                defaults["custom"]["thresholdsStyle"] = {"mode": "line"}
        options = {"legend": _legend(show=bool(legend_enabled)),
                   "tooltip": {"mode": "multi", "sort": "desc"}}
        # NR "null values" handling: "preserve" connects gaps; "zero" has
        # no panel option (PromQL returns no sample rather than 0).
        null_mode = str((rc.get("nullValues") or {}).get("nullValue") or "")
        if null_mode == "preserve":
            defaults["custom"]["spanNulls"] = True
        for so in (rc.get("colors") or {}).get("seriesOverrides") or []:
            if isinstance(so, dict) and so.get("seriesName") and so.get("color"):
                overrides.append({
                    "matcher": {"id": "byName", "options": so["seriesName"]},
                    "properties": [{"id": "color", "value": {
                        "mode": "fixed", "fixedColor": so["color"]}}]})
        for name in (rc.get("yAxisRight") or {}).get("series") or []:
            if isinstance(name, str) and name:
                overrides.append({
                    "matcher": {"id": "byName", "options": name},
                    "properties": [{"id": "custom.axisPlacement",
                                    "value": "right"}]})

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
                    "value": conv(item["value"])})
            defaults["thresholds"] = {"mode": "absolute", "steps": steps}
        options = {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"],
                              "fields": ""},
            "orientation": "auto", "textMode": "auto", "wideLayout": True,
            "colorMode": "value", "graphMode": "none", "justifyMode": "auto",
            "showPercentChange": False,
            "percentChangeColorMode": "standard",
        }
        if widget.viz_id == "viz.sparkline":
            options["graphMode"] = "area"
        elif widget.viz_id == "viz.billboard-comparison":
            options["showPercentChange"] = True
        elif widget.viz_id == "viz.traffic-light":
            options["colorMode"] = "background"
            options["textMode"] = "none"
        if widget.viz_id != "viz.traffic-light" and any(
                t.query_type == "range" for t in trans):
            # A TIMESERIES query behind a billboard: show the trend too.
            options["graphMode"] = "area"

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

    return options, {"defaults": defaults, "overrides": overrides}, unit_notes


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

    # Widget kinds Grafana has no panel for (service maps, funnels,
    # inventory, custom nerdpack visualizations) are reported precisely.
    kind_reason, equivalent = widget_kind_reason(widget.viz_id)
    if kind_reason:
        reasons = ["widget kind %s cannot be migrated: %s"
                   % (widget.viz_id, kind_reason),
                   "closest Grafana equivalent: %s" % equivalent]
        return _fallback_panel(panel, widget, b, page_name, reasons,
                               queries if widget.viz_id != "viz.funnel"
                               else queries, equivalent=equivalent)

    # Non-NRQL widgets (legacy metric charts, entity-bound widgets) cannot
    # be converted.
    if not queries:
        reason = ("widget %r carries no NRQL query (a legacy metric chart "
                  "or entity-bound widget); recreate it by hand"
                  % (widget.viz_id or "unknown"))
        return _fallback_panel(panel, widget, b, page_name, [reason], [],
                               equivalent=_PANEL_TYPES.get(widget.viz_id,
                                                           "timeseries"))

    trans = [translate_query(qtext, cfg, widget.viz_id or "")
             for qtext in queries]
    _widget_config_notes(widget, trans)

    conf = EXACT
    for t in trans:
        conf = worst(conf, t.confidence)

    if conf == UNTRANSLATABLE:
        reasons = [n for t in trans for n in t.notes
                   if t.confidence == UNTRANSLATABLE]
        reasons += [n for t in trans if t.confidence != UNTRANSLATABLE
                    for n in t.notes if not _is_hint(n)]
        return _fallback_panel(panel, widget, b, page_name, reasons, queries,
                               equivalent=_PANEL_TYPES.get(widget.viz_id,
                                                           "timeseries"))

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
    if "traceql-metrics" in hints and ptype in ("table", "logs"):
        ptype = "timeseries"
    if "table" in hints and ptype in ("timeseries", "stat", "bargauge",
                                      "piechart", "gauge"):
        ptype = "table"  # uniques(): a list of values

    # Instant table/pie/bar targets from prometheus should come back as table
    # frames for correct rendering.
    panel["type"] = ptype
    options, field_config, unit_notes = _panel_options(ptype, widget, trans)
    for msg, level in unit_notes:
        if trans:
            trans[0].note(msg, level)
        conf = worst(conf, level)
    panel["options"] = options
    panel["fieldConfig"] = field_config
    panel["targets"] = _make_targets(trans, b)

    if ptype == "heatmap" and "heatmap" in hints:
        # histogram() -> Prometheus le buckets; a FACET heatmap keeps its
        # series as rows (Grafana's "time series buckets" layout).
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
                         "renameByName": _table_column_names(
                             panel["targets"], queries, trans)}}]

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


def _widget_config_notes(widget: NRWidget, trans: List[Translation]) -> None:
    """Widget configuration that has no exact Grafana counterpart."""
    ok = [t for t in trans if t.confidence != UNTRANSLATABLE]
    if not ok:
        return
    rc = widget.raw_configuration or {}
    if (rc.get("facet") or {}).get("showOtherSeries"):
        ok[0].note("New Relic grouped the facets beyond the limit into an "
                   "'Other' series; topk() has no remainder bucket, so the "
                   "panel shows the top groups only", APPROXIMATE)
    null_mode = str((rc.get("nullValues") or {}).get("nullValue") or "")
    if null_mode == "zero":
        ok[0].note("NR showed missing values as zero; PromQL/LogQL return no "
                   "sample instead — append `or vector(0)` to the query or "
                   "use the panel's 'Connect null values' option", APPROXIMATE)
    if widget.viz_id == "viz.billboard-comparison":
        ok[0].note("billboard comparison: the stat panel shows the current "
                   "value and the COMPARE WITH target as a second value; "
                   "its percent-change badge is computed within the query "
                   "range, not against the comparison period", APPROXIMATE)


def _is_hint(note: str) -> bool:
    return ":" in note and note.split(":", 1)[0] in (
        "unit", "timefrom", "timeshift", "maxlines", "limit", "panel-hint",
        "interval")


def _fallback_panel(panel: Dict[str, Any], widget: NRWidget, b: _Build,
                    page_name: str, reasons: List[str],
                    queries: List[str], equivalent: str = "") \
        -> Dict[str, Any]:
    cfg = b.cfg
    reasons = [r for r in reasons if not _is_hint(r)]
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
                fallback="nrql-passthrough", extra_notes=reasons,
                equivalent=equivalent)
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
            fallback="text-placeholder", extra_notes=reasons,
            equivalent=equivalent)
    return panel


def _report(b: _Build, page: str, widget: NRWidget, panel: Dict[str, Any],
            conf: str, trans: List[Translation], fallback: str = "",
            extra_notes: Optional[List[str]] = None,
            equivalent: str = "") -> None:
    entry = {
        "page": page,
        "widget": widget.title or "(untitled)",
        "visualization": widget.viz_id,
        "panel_id": panel["id"],
        "panel_title": panel.get("title", ""),
        "panel_type": panel.get("type"),
        "confidence": conf,
        "nrql": [nq.get("query", "") for nq in widget.nrql_queries],
        "queries": [],
        "notes": [],
    }
    if conf == UNTRANSLATABLE:
        entry["reason"] = "; ".join(extra_notes or []) or "untranslatable"
        entry["equivalent"] = equivalent
    account_ids: List[int] = []
    for nq in widget.nrql_queries:
        raw = nq.get("accountIds")
        if not raw and nq.get("accountId") is not None:
            raw = [nq.get("accountId")]
        for v in raw or []:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv not in account_ids:
                account_ids.append(iv)
    if account_ids:
        entry["account_ids"] = account_ids
    for t in trans:
        for x in [t] + t.extra:
            entry["queries"].append(
                {"datasource": x.datasource, "expr": x.expr,
                 "type": x.query_type})
        entry["notes"].extend(t.notes)
    if extra_notes:
        entry["notes"].extend(extra_notes)
    entry["notes"].extend(_layout_notes(widget))
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
        # Derive a label-values query from a uniques()/FACET NRQL variable,
        # on the datasource family its FROM clause maps to, scoped by its
        # WHERE clause.
        nrql = (v.nrql_query or {}).get("query", "")
        plan = _variable_plan(nrql, cfg)
        if plan:
            family, query, definition = plan
            return dict(
                common, type="query", datasource=b.ds_ref(family),
                query=query, definition=definition, refresh=2, regex="",
                sort=1, multi=v.is_multi, includeAll=v.is_multi,
                allValue=".*",
                current={"selected": False, "text": ["All"],
                         "value": ["$__all"]} if v.is_multi else {},
                options=[])
        # Unmappable NRQL variable -> textbox with a warning.
        return dict(common, type="textbox", query="",
                    label=(v.title or v.name) + " (was NRQL variable)",
                    current={"selected": False, "text": "", "value": ""},
                    options=[])
    return None


def _variable_attr(pq: Any) -> Optional[Attr]:
    """The attribute a variable query enumerates: uniques(attr) / keyset
    or a single-attribute FACET (SELECT count(*) ... FACET attr)."""
    for item in pq.select:
        fn = item.expr
        if isinstance(fn, Func) and fn.name in ("uniques", "keyset") \
                and fn.args and isinstance(fn.args[0], Attr):
            return fn.args[0]
    if pq.facet and isinstance(pq.facet[0].expr, Attr):
        return pq.facet[0].expr
    return None


def _variable_plan(nrql: str, cfg: Dict[str, Any]) \
        -> Optional[Tuple[str, Dict[str, Any], str]]:
    """-> (datasource family, variable query model, definition text)."""
    try:
        pq = parse_nrql(nrql)
    except NrqlParseError:
        return None
    attr = _variable_attr(pq)
    if attr is None:
        return None
    family = route_event_type(pq.from_, cfg)
    if family == "traces":
        field = tempo_variable_field(attr.name)
        return ("tempo",
                {"type": 1, "label": field,
                 "refId": "TempoDatasourceVariableQueryEditor-VariableQuery"},
                "label_values(%s)" % field)
    label, _mapped = map_attr(attr.name, cfg)
    if family == "logs":
        stream = loki_variable_scope(pq, cfg)
        definition = ("label_values(%s, %s)" % (stream, label) if stream
                      else "label_values(%s)" % label)
        return ("loki",
                {"type": 1, "label": label, "stream": stream,
                 "refId": "LokiVariableQueryEditor-VariableQuery"},
                definition)
    scope = prom_variable_scope(pq, cfg)
    query = ("label_values(%s, %s)" % (scope, label) if scope
             else "label_values(%s)" % label)
    return ("prometheus",
            {"query": query, "qryType": 1,
             "refId": "PrometheusVariableQueryEditor-VariableQuery"},
            query)


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

def _source_description(nr: NRDashboard, description: str) -> str:
    src = "Migrated from New Relic dashboard %r" % nr.name
    if nr.guid:
        src += " (guid %s)" % nr.guid
    if nr.account_id:
        src += " account %s" % nr.account_id
    src += " by nr2grafana."
    return (description.strip() + "\n\n" + src) if description.strip() \
        else src


def _source_links(nr: NRDashboard) -> List[Dict[str, Any]]:
    if not nr.guid:
        return []
    return [{"title": "Original New Relic dashboard", "type": "link",
             "url": "https://one.newrelic.com/redirect/entity/%s" % nr.guid,
             "targetBlank": True, "icon": "external link", "tags": [],
             "asDropdown": False, "includeVars": False, "keepTime": False,
             "tooltip": "Open the source dashboard in New Relic"}]


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
        if gv and any(c.get("name") == gv.get("name") for c in converted):
            # Two New Relic variables with one name (an invalid export):
            # Grafana refuses duplicate names, so the first one wins.
            first = next(c for c in converted if c.get("name") == gv["name"])
            first["description"] = ("another New Relic variable named %r "
                                    "was dropped (duplicate name)"
                                    % gv["name"])
            continue
        if gv:
            converted.append(gv)
    tvars: List[Dict[str, Any]] = []
    tvars.extend(_datasource_variables(b))
    tvars.extend(converted)
    tvars.extend(copy.deepcopy(b.cfg.get("extra_variables") or []))
    _span_variable_values(tvars, b)
    # {{var}} references without a variable definition (a page exported
    # without its dashboard variables): a textbox keeps the dashboard
    # importable and its label says what to set.
    defined = {v.get("name") for v in tvars}
    for name in sorted(set(_VAR_NAME_RE.findall(
            json.dumps(dash.get("panels") or [], ensure_ascii=False)))):
        if name in defined or name.startswith("__"):
            continue
        tvars.append({
            "type": "textbox", "name": name,
            "label": "%s (undefined in the New Relic dashboard)" % name,
            "description": "referenced as {{%s}} by a widget but not "
                           "defined among the dashboard's variables; set "
                           "its value" % name,
            "query": "", "hide": 0, "options": [],
            "current": {"selected": False, "text": "", "value": ""}})
        defined.add(name)
    dash["templating"]["list"] = tvars
    if b.refresh_ms:
        secs = max(5, min(b.refresh_ms) // 1000)
        dash["refresh"] = ("%dm" % (secs // 60) if secs % 60 == 0
                           else "%ds" % secs)
    # Most common SINCE across widgets becomes the dashboard range; panels
    # whose SINCE differs get a relative timeFrom override (Grafana accepts
    # "30m"/"1h" as well as its own now/d, now-1d/d, now/w spellings).
    if b.timefroms:
        best = max(set(b.timefroms), key=b.timefroms.count)
        dash["time"] = {"from": best, "to": "now"}
        for panel, rng in b.panel_ranges:
            if rng == best or not rng.startswith("now"):
                continue
            if "/" in rng:
                panel["timeFrom"] = rng
            elif rng.startswith("now-"):
                panel["timeFrom"] = rng[len("now-"):]
            else:
                continue
            panel["hideTimeOverride"] = False


def _source_meta(nr: NRDashboard, source_file: str = "") -> Dict[str, Any]:
    """Provenance block stored in the dashboard JSON (Grafana keeps unknown
    top-level keys on API import; the UI drops them on save)."""
    from .. import __version__
    return {"version": __version__,
            "source": {"name": nr.name, "guid": nr.guid,
                       "account_id": nr.account_id, "file": source_file,
                       "pages": len(nr.pages), "widgets": nr.widget_count()}}


def build_dashboards(nr: NRDashboard, cfg: Dict[str, Any],
                     source_file: str = "") \
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
            dash = _dashboard_shell(title, uid,
                                    _source_description(nr, nr.description),
                                    cfg)
            dash["tags"].append(tag)
            dash["links"] = [{"title": "Pages", "type": "dashboards",
                              "tags": [tag], "asDropdown": True,
                              "includeVars": True, "keepTime": True,
                              "icon": "external link", "targetBlank": False,
                              "url": ""}] + _source_links(nr)
            dash["panels"] = _page_panels(page, b)
            _finish_dashboard(dash, b, nr.variables)
            dash["nr2grafana"] = _source_meta(nr, source_file)
            results.append(("%s--%s.json" % (base_slug, slugify(page.name, 30)),
                            dash, b.report))
        return results

    b = _Build(cfg)
    uid = slugify("nr-" + base_slug)
    dash = _dashboard_shell(nr.name, uid,
                            _source_description(nr, nr.description), cfg)
    dash["links"] = _source_links(nr)
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
    dash["nr2grafana"] = _source_meta(nr, source_file)
    results.append((base_slug + ".json", dash, b.report))
    return results
