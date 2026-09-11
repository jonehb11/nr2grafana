"""Side-by-side render model: real data from both sides, ready to chart.

This is the flagship 1.4 view. :func:`build_comparison` fetches REAL data
for every panel of a converted dashboard from BOTH sources -- the original
NRQL through NerdGraph (strictly read-only) and the translated query
through Grafana's ``/api/ds/query`` -- and returns everything the web UI
needs to draw the New Relic dashboard next to the Grafana dashboard, panel
by panel, with per-panel agreement verdicts.

It builds on :mod:`nr2grafana.parity` (reusing ``compare`` for the verdict
and ``normalize_nr`` / ``normalize_grafana`` for the common series shape)
and :mod:`nr2grafana.samples` (raw log-line derivation). Nothing here ever
mutates New Relic or Grafana, and no per-panel failure is allowed to raise:
a broken side becomes an ``nr-error`` / ``gf-error`` verdict carrying the
message.

The comparison report uses schema ``nr2grafana/comparison/v1``.

:func:`datasource_flow` powers the "add a datasource and watch data flow"
loop: it groups the dashboard's panels by the datasource family they need,
probes each through ``ds_query``, counts data / no-data / error and captures
one real sample series proving flow. The caller snapshots it before and
after creating a datasource to show which panels newly light up.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .livecheck import iter_targets, substitute
from .parity import (
    _WEIGHTS, _nrql_for_target, _nrql_with_range, compare, normalize_grafana,
    normalize_nr)
from .samples import (
    _iso, _log_lines, _loki_selector, _result_error, _trunc, raw_sample_nrql)

SCHEMA = "nr2grafana/comparison/v1"
ARTIFACT_KIND = "comparison"
DSFLOW_SCHEMA = "nr2grafana/dsflow/v1"

_MAX_SERIES = 8         # cap series charted per panel side
_ROW_CAP = 200          # cap table rows / log lines / raw sample rows

_VAR_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# Grafana panel ``type`` -> the viz kind the UI draws on BOTH sides.
_VIZ_BY_TYPE = {
    "timeseries": "timeseries", "graph": "timeseries",
    "state-timeline": "timeseries", "status-history": "timeseries",
    "stat": "stat", "gauge": "gauge",
    "bargauge": "bar", "barchart": "bar",
    "table": "table", "table-old": "table",
    "logs": "logs", "piechart": "piechart",
    "text": "text",
}


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _family(ds_type: str) -> str:
    """Map a datasource plugin type to a datasource family."""
    t = (ds_type or "").lower()
    if "newrelic" in t:
        return "newrelic"
    if t in ("prometheus", "loki", "tempo"):
        return t
    return t


def _viz_for(panel: Dict[str, Any]) -> str:
    """viz kind for a Grafana panel; ``unsupported`` when unknown."""
    return _VIZ_BY_TYPE.get(panel.get("type") or "", "unsupported")


def _render_mode(viz: str) -> str:
    """How a viz kind wants its data shaped: series/scalar/rows/lines."""
    if viz == "logs":
        return "lines"
    if viz == "table":
        return "rows"
    if viz in ("stat", "gauge"):
        return "scalar"
    if viz == "text":
        return "text"
    return "series"  # timeseries, bar, piechart, unsupported


# ---------------------------------------------------------------------------
# layout helpers
# ---------------------------------------------------------------------------

def _iter_layout(dash: Dict[str, Any]):
    """Yield ``(panel, row_title)`` for every data panel in grid order.

    Handles both layouts the builder emits: the first page's row is a flat
    marker followed by sibling panels, later pages' rows nest their panels
    in ``row["panels"]``. Row and (data-less) marker panels are not
    yielded themselves; their title flows down to the panels under them.
    """
    current_row = ""
    for panel in dash.get("panels") or []:
        if not isinstance(panel, dict):
            continue
        if panel.get("type") == "row":
            current_row = panel.get("title") or ""
            for child in panel.get("panels") or []:
                if isinstance(child, dict):
                    yield child, current_row
            continue
        yield panel, current_row


def _grid_of(panel: Dict[str, Any]) -> Dict[str, int]:
    """gridPos as ``{x, y, w, h}`` ints, with sane defaults."""
    g = panel.get("gridPos") or {}

    def num(key, default):
        try:
            return int(g.get(key, default))
        except (TypeError, ValueError):
            return default

    return {"x": num("x", 0), "y": num("y", 0),
            "w": num("w", 12), "h": num("h", 8)}


def _panel_unit(panel: Dict[str, Any]) -> str:
    defaults = (panel.get("fieldConfig") or {}).get("defaults") or {}
    return str(defaults.get("unit") or "")


# ---------------------------------------------------------------------------
# series shaping
# ---------------------------------------------------------------------------

def _series_name(labels: Dict[str, Any]) -> str:
    if not labels:
        return ""
    return ", ".join(str(v) for v in labels.values())


def _downsample(points: List[List[float]], limit: int) -> List[List[float]]:
    """Even-stride downsample to at most ``limit`` points."""
    n = len(points)
    if limit <= 0 or n <= limit:
        return [[float(p[0]), float(p[1])] for p in points]
    stride = -(-n // limit)  # ceil division -> <= limit kept
    return [[float(points[i][0]), float(points[i][1])]
            for i in range(0, n, stride)]


def _to_series(norm: List[Dict[str, Any]], limit: int) \
        -> List[Dict[str, Any]]:
    """Common-shape series -> chartable ``{name, points}`` (capped)."""
    out: List[Dict[str, Any]] = []
    for s in (norm or [])[:_MAX_SERIES]:
        out.append({"name": _series_name(s.get("labels") or {}),
                    "points": _downsample(s.get("points") or [], limit)})
    return out


def _last_value(norm: List[Dict[str, Any]]) -> Optional[float]:
    """Last value of the longest series (for stat/gauge scalars)."""
    best: List[Any] = []
    for s in norm or []:
        pts = s.get("points") or []
        if len(pts) >= len(best):
            best = pts
    if best:
        return float(best[-1][1])
    return None


def _row_of(s: Dict[str, Any]) -> Dict[str, Any]:
    """A table row: the series labels plus its last value."""
    row: Dict[str, Any] = dict(s.get("labels") or {})
    pts = s.get("points") or []
    row["value"] = float(pts[-1][1]) if pts else None
    return row


def _empty_side(unit: str = "") -> Dict[str, Any]:
    return {"kind": "empty", "series": [], "scalar": None, "rows": None,
            "lines": None, "unit": unit, "error": ""}


def _assemble_side(mode: str, norm: List[Dict[str, Any]],
                   lines: List[Dict[str, Any]], limit: int) \
        -> Dict[str, Any]:
    """Shape collected data into the schema's per-side dict."""
    side = _empty_side()
    if mode == "lines":
        if lines:
            side["kind"] = "logs"
            side["lines"] = lines[:_ROW_CAP]
        return side
    norm = norm or []
    if not norm:
        return side
    if mode == "scalar":
        side["kind"] = "scalar"
        side["scalar"] = _last_value(norm)
        side["series"] = _to_series(norm, limit)
    elif mode == "rows":
        side["kind"] = "table"
        side["rows"] = [_row_of(s) for s in norm[:_ROW_CAP]]
    else:
        side["kind"] = "series"
        side["series"] = _to_series(norm, limit)
    return side


def _line_count_series(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Single scalar series of the log-line count (for verdict compare)."""
    if not lines:
        return []
    return [{"labels": {}, "points": [[0.0, float(len(lines))]]}]


# ---------------------------------------------------------------------------
# target resolution
# ---------------------------------------------------------------------------

def _resolve_target(tgt: Dict[str, Any], ds_map: Dict[str, str]) \
        -> Tuple[str, str, str]:
    """(resolved uid, datasource type, substituted expression)."""
    ds = tgt.get("datasource") or {}
    uid = ds.get("uid") or ""
    ds_type = ds.get("type") or ""
    if uid in ds_map:
        uid = ds_map[uid]
    elif _VAR_REF.match(uid):
        uid = ds_map.get(uid[2:-1], uid)
    expr = tgt.get("expr") or tgt.get("query") or tgt.get("queryText") or ""
    if isinstance(expr, str):
        expr = substitute(expr)
    return uid, ds_type, expr


# ---------------------------------------------------------------------------
# New Relic side
# ---------------------------------------------------------------------------

def _run_nr(nr, aids: List[int], query: str):
    """(results, error) for one NRQL, trying each account id in order."""
    empty_seen = False
    last_err = "no account produced a result"
    for aid in aids:
        try:
            got = nr.run_nrql(int(aid), query)
        except Exception as e:  # noqa: BLE001 - degrade, never raise
            last_err = str(e)
            continue
        results = (got or {}).get("results") or []
        if results:
            return results, ""
        empty_seen = True
    if empty_seen:
        return [], ""
    return None, last_err


def _nr_lines(results: List[Any]) -> List[Dict[str, Any]]:
    """Raw NR event rows -> ``{ts, line}`` log entries."""
    out: List[Dict[str, Any]] = []
    for row in results[:_ROW_CAP]:
        if not isinstance(row, dict):
            continue
        ts = _iso(row.get("timestamp"))
        msg = row.get("message")
        if msg is None:
            msg = row.get("log")
        if msg is None:
            msg = row.get("body")
        if msg is None:
            try:
                msg = json.dumps(row, default=str, sort_keys=True)
            except (TypeError, ValueError):
                msg = str(row)
        out.append({"ts": ts, "line": _trunc(str(msg))})
    return out


def _nr_panel(nr, aids: List[int], nrqls: List[str], mode: str,
              frm: str, to: str, limit: int):
    """One panel's New Relic side: (side_dict, cmp_series, built_query).

    ``cmp_series`` is the common-shape series list handed to
    :func:`parity.compare` (a log-line count series for ``lines`` mode),
    or ``None`` when the side errored.
    """
    if mode == "text":
        return _empty_side(), [], ""
    if nr is None:
        s = _empty_side()
        s["kind"] = "error"
        s["error"] = "New Relic client not configured"
        return s, None, ""
    if not any(nrqls):
        s = _empty_side()
        s["kind"] = "error"
        s["error"] = "no original NRQL recorded for this panel"
        return s, None, ""
    if not aids:
        s = _empty_side()
        s["kind"] = "error"
        s["error"] = "no New Relic account id available"
        return s, None, ""

    norm_all: List[Dict[str, Any]] = []
    lines_all: List[Dict[str, Any]] = []
    errors: List[str] = []
    got = False
    empty_any = False
    first_query = ""
    for nrql in nrqls:
        if not nrql:
            continue
        if mode == "lines":
            raw = raw_sample_nrql(nrql, min(limit, _ROW_CAP))
            if not raw:
                errors.append("could not derive a raw log query from "
                              "the panel's NRQL")
                continue
            query = _nrql_with_range(raw, frm, to)
        else:
            query = _nrql_with_range(nrql, frm, to)
            # Graph panels need a series: append TIMESERIES when the
            # stored NRQL lacks it (built locally; never mutated).
            if mode == "series" and "timeseries" not in query.lower():
                query = query.rstrip() + " TIMESERIES"
        if not first_query:
            first_query = query
        results, err = _run_nr(nr, aids, query)
        if results is None:
            errors.append(err)
            continue
        if not results:
            empty_any = True
            continue
        got = True
        if mode == "lines":
            lines_all.extend(_nr_lines(results))
        else:
            norm_all.extend(normalize_nr(results, query))

    if not got:
        if empty_any:
            return _assemble_side(mode, [], [], limit), [], first_query
        s = _empty_side()
        s["kind"] = "error"
        s["error"] = "; ".join(errors) or "no data"
        return s, None, first_query

    side = _assemble_side(mode, norm_all, lines_all, limit)
    cmp = _line_count_series(lines_all) if mode == "lines" else norm_all
    return side, cmp, first_query


# ---------------------------------------------------------------------------
# Grafana side
# ---------------------------------------------------------------------------

def _gf_panel(grafana, targets: List[Dict[str, Any]],
              ds_map: Dict[str, str], mode: str, frm: str, to: str,
              limit: int):
    """One panel's Grafana side: (side_dict, cmp_series)."""
    if mode == "text":
        return _empty_side(), []
    if grafana is None:
        s = _empty_side()
        s["kind"] = "error"
        s["error"] = "Grafana client not configured"
        return s, None
    if not targets:
        return _empty_side(), []

    norm_all: List[Dict[str, Any]] = []
    lines_all: List[Dict[str, Any]] = []
    errors: List[str] = []
    got = False
    empty_any = False
    for tgt in targets:
        uid, ds_type, expr = _resolve_target(tgt, ds_map)
        ref = tgt.get("refId") or "A"
        if not uid or _VAR_REF.match(uid):
            errors.append("unresolved datasource ref %r (no matching "
                          "datasource on the instance?)"
                          % ((tgt.get("datasource") or {}).get("uid")))
            continue
        if mode == "lines":
            selector = _loki_selector(expr) or expr
            target = {"refId": ref, "expr": selector,
                      "maxLines": min(limit, _ROW_CAP)}
        else:
            target = dict(tgt)
            for k in ("expr", "query"):
                if isinstance(tgt.get(k), str):
                    target[k] = substitute(tgt[k])
        try:
            resp = grafana.ds_query(uid, ds_type, target, frm, to)
        except Exception as e:  # noqa: BLE001 - degrade, never raise
            errors.append(str(e))
            continue
        res = ((resp or {}).get("results") or {}).get(ref) or {}
        err = _result_error(res)
        if err:
            errors.append(err)
            continue
        frames = res.get("frames") or []
        if mode == "lines":
            ls = _log_lines(frames, min(limit, _ROW_CAP))
            if ls:
                lines_all.extend(ls)
                got = True
            else:
                empty_any = True
        else:
            norm = normalize_grafana(resp, ref)
            if norm:
                norm_all.extend(norm)
                got = True
            else:
                empty_any = True

    if not got:
        if empty_any:
            return _assemble_side(mode, [], [], limit), []
        s = _empty_side()
        s["kind"] = "error"
        s["error"] = "; ".join(errors) or "no data"
        return s, None

    side = _assemble_side(mode, norm_all, lines_all, limit)
    cmp = _line_count_series(lines_all) if mode == "lines" else norm_all
    return side, cmp


# ---------------------------------------------------------------------------
# NR raw dashboard layout (for the New Relic column)
# ---------------------------------------------------------------------------

def _nr_layout(nr_dashboard_raw: Optional[Dict[str, Any]]) \
        -> Dict[str, Any]:
    """Best-effort page/widget layout of the original NR dashboard."""
    pages: List[Dict[str, Any]] = []
    for pg in (nr_dashboard_raw or {}).get("pages") or []:
        if not isinstance(pg, dict):
            continue
        widgets: List[Dict[str, Any]] = []
        for w in pg.get("widgets") or []:
            if not isinstance(w, dict):
                continue
            lay = w.get("layout") or {}
            viz = w.get("visualization")
            if isinstance(viz, dict):
                viz = viz.get("id") or ""
            elif not isinstance(viz, str):
                viz = w.get("visualizationId") or ""
            widgets.append({
                "title": w.get("title") or "",
                "viz": viz,
                "layout": {"column": lay.get("column"),
                           "row": lay.get("row"),
                           "width": lay.get("width"),
                           "height": lay.get("height")},
            })
        pages.append({"name": pg.get("name") or "", "widgets": widgets})
    return {"nr_pages": pages}


# ---------------------------------------------------------------------------
# public: build_comparison
# ---------------------------------------------------------------------------

def _weight(verdict: str, gf_has_data: bool) -> float:
    """Score weight for a verdict (NR-error with GF data still counts)."""
    if verdict == "nr-error" and gf_has_data:
        return 0.6
    return _WEIGHTS.get(verdict, 0.0)


def build_comparison(nr, account_ids, grafana, nr_dashboard_raw, dash,
                     widget_report, ds_map=None, frm="now-1h", to="now",
                     limit_points=100, log=None) -> Dict[str, Any]:
    """Render model for the side-by-side comparison view.

    Fetches real data for every panel of ``dash`` from both New Relic
    (original NRQL via ``nr``) and Grafana (translated query via
    ``grafana.ds_query``), shaped for charting per the panel's viz kind,
    with a per-panel agreement verdict from :func:`parity.compare`. Never
    raises per panel: a failed side yields an ``*-error`` verdict carrying
    the message. Returns schema ``nr2grafana/comparison/v1``.
    """
    emit = log or (lambda m: None)
    try:
        limit_points = int(limit_points)
    except (TypeError, ValueError):
        limit_points = 100
    limit_points = max(2, min(limit_points, 2000))
    if isinstance(account_ids, (int, str)):
        account_ids = [account_ids]
    aids: List[int] = []
    for a in account_ids or []:
        try:
            aids.append(int(a))
        except (TypeError, ValueError):
            pass

    if ds_map is None and grafana is not None:
        try:
            ds_map = grafana.resolve_ds_map(dash)
        except Exception as e:  # noqa: BLE001 - degrade, don't raise
            emit("  warn: could not resolve datasources: %s" % e)
            ds_map = {}
    ds_map = ds_map or {}

    by_panel: Dict[Any, Dict[str, Any]] = {}
    for entry in widget_report or []:
        by_panel[entry.get("panel_id")] = entry

    panels: List[Dict[str, Any]] = []
    summary: Dict[str, int] = {}
    weight_sum = 0.0
    counted = 0
    for panel, row_title in _iter_layout(dash):
        viz = _viz_for(panel)
        mode = _render_mode(viz)
        pid = panel.get("id")
        unit = _panel_unit(panel)
        targets = [t for t in (panel.get("targets") or [])
                   if isinstance(t, dict)]
        entry = by_panel.get(pid)
        nrqls = [_nrql_for_target(entry, i) for i in range(len(targets))]

        nr_side, nr_cmp, nr_query = _nr_panel(
            nr, aids, nrqls, mode, frm, to, limit_points)
        gf_side, gf_cmp = _gf_panel(
            grafana, targets, ds_map, mode, frm, to, limit_points)
        nr_side["unit"] = unit
        gf_side["unit"] = unit

        ds_type = ""
        expr = ""
        if targets:
            _uid, ds_type, expr = _resolve_target(targets[0], ds_map)

        is_data_panel = mode != "text" and bool(targets)
        ratio = None
        if not is_data_panel:
            verdict = "both-empty"
            detail = ("informational text panel (no query)"
                      if mode == "text" else "panel has no query targets")
        elif gf_cmp is None:
            verdict = "gf-error"
            detail = gf_side["error"]
            if nr_cmp is None and nr_side["error"]:
                detail += "; New Relic side also failed: %s" \
                    % nr_side["error"]
        elif nr_cmp is None:
            verdict = "nr-error"
            detail = nr_side["error"]
        else:
            result = compare(nr_cmp, gf_cmp)
            verdict = result["verdict"]
            detail = result["detail"]
            ratio = result["ratio"]

        row = {
            "panel_id": pid,
            "title": panel.get("title") or "",
            "row": row_title,
            "grid": _grid_of(panel),
            "viz": viz,
            "nr": nr_side,
            "grafana": gf_side,
            "verdict": verdict,
            "detail": detail,
            "ratio": ratio,
            "nrql": nr_query,
            "expr": expr,
            "datasource": _family(ds_type),
        }
        panels.append(row)

        if is_data_panel:
            summary[verdict] = summary.get(verdict, 0) + 1
            gf_has = gf_side["kind"] in ("series", "scalar", "table",
                                         "logs")
            weight_sum += _weight(verdict, gf_has)
            counted += 1
            emit("  %-15s %s [%s] %s"
                 % (verdict, row["title"], viz, detail))

    score = int(round(100.0 * weight_sum / counted)) if counted else 0
    return {
        "schema": SCHEMA,
        "dashboard": dash.get("title") or dash.get("uid") or "",
        "uid": dash.get("uid") or "",
        "generated_at": _utcnow(),
        "range": {"from": frm, "to": to},
        "panels": panels,
        "summary": summary,
        "score": score,
        "layout": _nr_layout(nr_dashboard_raw),
    }


# ---------------------------------------------------------------------------
# public: datasource_flow
# ---------------------------------------------------------------------------

def _required_families(requirements: Optional[Dict[str, Any]],
                       dash: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(family, plugin_id) pairs the dashboard needs.

    From the requirements artifact when present, else derived from the
    dashboard's own target datasource types.
    """
    out: List[Tuple[str, str]] = []
    seen = set()
    for req in (requirements or {}).get("datasources") or []:
        plugin = req.get("plugin_id") or req.get("family") or ""
        fam = req.get("family") or _family(plugin)
        if fam and fam not in seen:
            seen.add(fam)
            out.append((fam, plugin))
    if out:
        return out
    for _panel, tgt in iter_targets(dash):
        ds_type = (tgt.get("datasource") or {}).get("type") or ""
        fam = _family(ds_type)
        if fam and fam not in seen:
            seen.add(fam)
            out.append((fam, ds_type))
    return out


def _family_flow(grafana, dash: Dict[str, Any], ds_map: Dict[str, str],
                 fam: str, plugin: str, focus_uid: str,
                 focus_fam: str) -> Dict[str, Any]:
    """Probe every panel of one datasource family; count and sample."""
    total = with_data = no_data = errored = 0
    flowing: List[Any] = []
    sample: List[Dict[str, Any]] = []
    forced_uid = focus_uid if (focus_uid and focus_fam == fam) else ""
    used_uid = forced_uid

    for panel, tgt in iter_targets(dash):
        if panel.get("type") in ("row", "text"):
            continue
        ds_type = (tgt.get("datasource") or {}).get("type") or ""
        if _family(ds_type) != fam:
            continue
        total += 1
        uid, _t, expr = _resolve_target(tgt, ds_map)
        if forced_uid:
            uid = forced_uid  # probe against the datasource under test
        if not uid or _VAR_REF.match(uid):
            errored += 1
            continue
        if not used_uid:
            used_uid = uid
        ref = tgt.get("refId") or "A"
        target = dict(tgt)
        for k in ("expr", "query"):
            if isinstance(tgt.get(k), str):
                target[k] = substitute(tgt[k])
        try:
            resp = grafana.ds_query(uid, ds_type, target)
        except Exception:  # noqa: BLE001 - degrade, never raise
            errored += 1
            continue
        res = ((resp or {}).get("results") or {}).get(ref) or {}
        if _result_error(res):
            errored += 1
            continue
        frames = res.get("frames") or []
        norm = normalize_grafana(resp, ref)
        pts = sum(len(s.get("points") or []) for s in norm)
        lines = _log_lines(frames, 50) if not pts else []
        if pts or lines:
            with_data += 1
            flowing.append(panel.get("id"))
            if not sample:
                if norm:
                    sample = _to_series(norm, 100)
                elif lines:
                    sample = [{"name": "log lines",
                               "points": [[0.0, float(len(lines))]]}]
        else:
            no_data += 1

    if used_uid:
        try:
            health = grafana.datasource_health(used_uid)
        except Exception as e:  # noqa: BLE001
            health = {"status": "unknown",
                      "message": "health check failed: %s" % e}
    else:
        health = {"status": "unknown",
                  "message": "no datasource resolved for this family"}

    return {
        "family": fam,
        "uid": used_uid,
        "health": health,
        "panels_total": total,
        "panels_with_data": with_data,
        "panels_no_data": no_data,
        "panels_error": errored,
        "sample_series": sample,
        "newly_flowing": flowing,
    }


def datasource_flow(grafana, requirements, dash, widget_report,
                    ds_uid=None, ds_map=None, log=None) -> Dict[str, Any]:
    """Probe how data flows through each datasource the dashboard needs.

    Groups the dashboard's panels by required datasource family, runs each
    panel's query through ``grafana.ds_query`` and reports, per family,
    ``panels_total`` / ``panels_with_data`` / ``panels_no_data`` /
    ``panels_error``, a health check, and one real ``sample_series``
    proving flow. ``newly_flowing`` lists the panel ids currently
    returning data through the family -- the caller snapshots this before
    and after creating a datasource to show which panels newly light up.

    When ``ds_uid`` is given, focus on that datasource's family and probe
    its panels against that exact uid (the "add datasource and watch it
    flow" loop). Never raises: a broken probe counts as ``panels_error``.
    """
    emit = log or (lambda m: None)
    if grafana is None:
        return {"schema": DSFLOW_SCHEMA, "generated_at": _utcnow(),
                "families": [], "error": "Grafana client not configured"}

    if ds_map is None:
        try:
            ds_map = grafana.resolve_ds_map(dash)
        except Exception as e:  # noqa: BLE001 - degrade, don't raise
            emit("  warn: could not resolve datasources: %s" % e)
            ds_map = {}
    ds_map = ds_map or {}

    focus_fam = ""
    if ds_uid:
        try:
            ds = grafana.datasource_by_uid(ds_uid)
        except Exception:  # noqa: BLE001
            ds = None
        if ds:
            focus_fam = _family(ds.get("type") or "")

    fams = _required_families(requirements, dash)
    if ds_uid and focus_fam:
        fams = [(f, p) for (f, p) in fams if f == focus_fam] \
            or [(focus_fam, "")]

    families: List[Dict[str, Any]] = []
    for fam, plugin in fams:
        entry = _family_flow(grafana, dash, ds_map, fam, plugin,
                             ds_uid or "", focus_fam)
        families.append(entry)
        emit("  %-11s %d/%d panels flowing (%d no-data, %d error)"
             % (fam, entry["panels_with_data"], entry["panels_total"],
                entry["panels_no_data"], entry["panels_error"]))

    return {"schema": DSFLOW_SCHEMA, "generated_at": _utcnow(),
            "families": families}
