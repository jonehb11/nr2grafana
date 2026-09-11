"""Human sample review: raw data from both sides, for sign-off.

Parity proves the aggregate numbers agree; this module pulls a handful
of ACTUAL raw samples from each source - New Relic events/rows via
NerdGraph (strictly read-only) and the migrated panel's data via
Grafana's ``/api/ds/query`` (Loki log lines, Prometheus datapoints) -
so a human can eyeball them side by side and confirm "yes, these are
my logs". The classic case: CloudWatch logs that used to be viewed
through New Relic vs the same logs now flowing into Loki.

Sample reports use schema ``nr2grafana/samples/v1`` and are persisted
as Store artifact kind ``"samples"``. Human verdicts (``confirmed`` /
``rejected`` / ``unsure``) are persisted per panel target in a
``"review"`` artifact and folded into
:func:`nr2grafana.parity.readiness`: a rejected panel blocks the
migration; a fully confirmed dashboard is graded "human-verified".

Payload sizes are capped: at most ``limit`` rows per side, strings
truncated to ~500 characters, at most a few series/frames per panel.
Nothing in here ever mutates New Relic or Grafana.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional

from .livecheck import iter_targets, substitute

SCHEMA = "nr2grafana/samples/v1"
REVIEW_SCHEMA = "nr2grafana/review/v1"
ARTIFACT_KIND = "samples"
REVIEW_KIND = "review"
VERDICTS = ("confirmed", "rejected", "unsure")

_MAX_STR = 500          # truncate any sampled string beyond this
_MAX_ROW_KEYS = 24      # cap attributes kept per NR event row
_MAX_SERIES = 3         # cap Prometheus series shown per target
_MAX_FRAMES = 3         # cap generic frames shown per target
_MAX_LIMIT = 25

_VAR_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# NRQL clause keywords that end a FROM/WHERE clause (mirrors the
# clause parsing in parity.py; duplicated so this module stays
# importable when parity is stubbed in tests).
_CLAUSE_END = (r"(?=\s+(?:SELECT|WHERE|FACET|SINCE|UNTIL|TIMESERIES|"
               r"LIMIT|ORDER|COMPARE|SLIDE|EXTRAPOLATE|WITH)\b|$)")
_FROM_RE = re.compile(
    r"\bFROM\s+([`\w:.]+(?:\s*,\s*[`\w:.]+)*)" + _CLAUSE_END,
    re.IGNORECASE)
_WHERE_RE = re.compile(r"\bWHERE\s+(.*?)" + _CLAUSE_END,
                       re.IGNORECASE | re.DOTALL)

# Relative "now-<n><unit>" range spec (mirrors parity.py).
_REL_TIME = re.compile(r"^now(?:-(\d+)([smhdw]))?$")
_UNIT_WORD = {"s": "second", "m": "minute", "h": "hour",
              "d": "day", "w": "week"}


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso(ts: Any) -> str:
    """Epoch seconds or milliseconds -> ISO-8601 UTC, '' when bad."""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return ""
    if t > 1e11:
        t = t / 1000.0
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    except (ValueError, OSError, OverflowError):
        return ""


def _trunc(text: str, cap: int = _MAX_STR) -> str:
    text = str(text)
    if len(text) <= cap:
        return text
    return text[:cap] + "...[truncated]"


def _clean_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Bound one NR result row: cap keys, truncate long strings."""
    out: Dict[str, Any] = {}
    for k in list(row)[:_MAX_ROW_KEYS]:
        v = row[k]
        if isinstance(v, str):
            out[k] = _trunc(v)
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            try:
                out[k] = _trunc(json.dumps(v, default=str))
            except (TypeError, ValueError):
                out[k] = _trunc(repr(v))
    return out


# ---------------------------------------------------------------------------
# NRQL derivation (mirrors helpers in parity.py)
# ---------------------------------------------------------------------------

def _nrql_with_range(nrql: str, frm: str, to: str) -> str:
    """Append SINCE/UNTIL matching the Grafana range, when absent."""
    if re.search(r"\b(SINCE|UNTIL)\b", nrql, re.IGNORECASE):
        return nrql

    def rel(spec, keyword):
        s = str(spec).strip()
        m = _REL_TIME.match(s)
        if m:
            if not m.group(1):
                return ""
            n = int(m.group(1))
            word = _UNIT_WORD[m.group(2)] + ("" if n == 1 else "s")
            return "%s %d %s AGO" % (keyword, n, word)
        if re.match(r"^\d+$", s):
            return "%s %s" % (keyword, s)
        return ""

    parts = [p for p in (rel(frm, "SINCE"), rel(to, "UNTIL")) if p]
    if not parts:
        return nrql
    return nrql.rstrip() + " " + " ".join(parts)


def _nrql_for_target(entry: Optional[Dict[str, Any]], idx: int) -> str:
    """Best-effort original NRQL for the idx-th target of a panel."""
    if not entry:
        return ""
    nrqls = entry.get("nrql") or []
    if not nrqls:
        return ""
    queries = entry.get("queries") or []
    if len(nrqls) == len(queries) and idx < len(nrqls):
        return nrqls[idx]
    return nrqls[min(idx, len(nrqls) - 1)]


def raw_sample_nrql(nrql: str, limit: int) -> str:
    """Derive a raw-event sampling query from an aggregate NRQL.

    Strips aggregates/TIMESERIES/FACET and keeps the event type and
    WHERE clause: ``SELECT * FROM <event> WHERE <same where> LIMIT n``.
    Returns '' when no FROM clause can be found.
    """
    m = _FROM_RE.search(nrql or "")
    if not m:
        return ""
    event = re.sub(r"\s+", " ", m.group(1)).strip()
    where = ""
    wm = _WHERE_RE.search(nrql)
    if wm:
        where = re.sub(r"\s+", " ", wm.group(1)).strip()
    q = "SELECT * FROM %s" % event
    if where:
        q += " WHERE %s" % where
    return "%s LIMIT %d" % (q, limit)


def _loki_selector(expr: str) -> str:
    """First {...} stream selector in a LogQL expr, '' when absent."""
    m = re.search(r"\{[^}]*\}", expr or "")
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# Grafana frame extraction (bounded)
# ---------------------------------------------------------------------------

def _result_error(res: Dict[str, Any]) -> str:
    """Error message from one refId result, '' if none (mirrors
    grafana/live.py so this module has no import-time dependency on
    it)."""
    if res.get("error"):
        return str(res["error"])
    errs = res.get("errors") or []
    if errs:
        return "; ".join(str(e.get("message") or e)
                         if isinstance(e, dict) else str(e)
                         for e in errs)
    status = res.get("status")
    if isinstance(status, int) and status >= 400:
        return "query returned HTTP %d" % status
    return ""


def _split_fields(frame: Dict[str, Any]):
    """(fields, col_fn, time_idx, numeric_idxs, string_idxs)."""
    fields = ((frame.get("schema") or {}).get("fields")) or []
    cols = ((frame.get("data") or {}).get("values")) or []

    def col(i):
        return cols[i] if i < len(cols) else []

    time_idx = None
    num_idx: List[int] = []
    str_idx: List[int] = []
    for i, f in enumerate(fields):
        ftype = f.get("type") or ""
        name = f.get("name") or ""
        if time_idx is None and (ftype == "time"
                                 or name in ("Time", "time")):
            time_idx = i
            continue
        if ftype == "number":
            num_idx.append(i)
        elif ftype == "string":
            str_idx.append(i)
        else:
            sample = next((v for v in col(i) if v is not None), None)
            if isinstance(sample, bool):
                continue
            if isinstance(sample, (int, float)):
                num_idx.append(i)
            elif isinstance(sample, str):
                str_idx.append(i)
    return fields, col, time_idx, num_idx, str_idx


def _log_lines(frames: List[Any], limit: int) -> List[Dict[str, Any]]:
    """Up to ``limit`` most recent {"ts", "line"} entries from log
    frames (time + string columns)."""
    entries: List[Dict[str, Any]] = []
    for frame in frames or []:
        if not isinstance(frame, dict):
            continue
        _fields, col, time_idx, _num, str_idx = _split_fields(frame)
        if not str_idx:
            continue
        lines = col(str_idx[0])
        times = col(time_idx) if time_idx is not None else []
        for j, line in enumerate(lines):
            if line is None:
                continue
            ts = times[j] if j < len(times) else None
            entries.append({"ts": _iso(ts), "line": _trunc(str(line))})
    return entries[-limit:]


def _point_series(frames: List[Any], limit: int) \
        -> List[Dict[str, Any]]:
    """Up to _MAX_SERIES series of the last ``limit`` [ts, value]
    points each, from Prometheus-style frames."""
    out: List[Dict[str, Any]] = []
    for frame in frames or []:
        if len(out) >= _MAX_SERIES:
            break
        if not isinstance(frame, dict):
            continue
        fields, col, time_idx, num_idx, _strs = _split_fields(frame)
        tcol = col(time_idx) if time_idx is not None else []
        for i in num_idx:
            if len(out) >= _MAX_SERIES:
                break
            f = fields[i]
            labels = dict(f.get("labels") or {})
            if not labels and len(num_idx) > 1:
                labels = {"field": f.get("name") or "value%d" % i}
            points: List[List[float]] = []
            for j, v in enumerate(col(i)):
                if v is None or isinstance(v, bool):
                    continue
                if time_idx is not None:
                    if j >= len(tcol) or tcol[j] is None:
                        continue
                    t = float(tcol[j])
                    if t > 1e11:
                        t = t / 1000.0
                else:
                    t = 0.0
                points.append([t, float(v)])
            if points:
                out.append({"labels": labels,
                            "points": points[-limit:]})
    return out


def _frame_rows(frames: List[Any], limit: int) -> List[Dict[str, Any]]:
    """Generic bounded table view of frames (tempo/passthrough)."""
    out: List[Dict[str, Any]] = []
    for frame in frames or []:
        if not isinstance(frame, dict) or len(out) >= _MAX_FRAMES:
            continue
        fields = ((frame.get("schema") or {}).get("fields")) or []
        cols = ((frame.get("data") or {}).get("values")) or []
        names = [f.get("name") or "col%d" % i
                 for i, f in enumerate(fields)]
        nrows = max((len(c) for c in cols if isinstance(c, list)),
                    default=0)
        rows: List[List[Any]] = []
        for r in range(min(nrows, limit)):
            row: List[Any] = []
            for c in cols:
                v = c[r] if isinstance(c, list) and r < len(c) else None
                if isinstance(v, str):
                    v = _trunc(v)
                elif not isinstance(v, (int, float, bool)) \
                        and v is not None:
                    v = _trunc(str(v))
                row.append(v)
            rows.append(row)
        if rows:
            out.append({"fields": names, "rows": rows})
    return out


# ---------------------------------------------------------------------------
# per-side collectors
# ---------------------------------------------------------------------------

def _nr_side(nr, aids: List[int], nrql: str, ds_type: str,
             frm: str, to: str, limit: int) -> Dict[str, Any]:
    """One panel target's New Relic samples (read-only)."""
    out: Dict[str, Any] = {"kind": "empty", "samples": [], "error": ""}
    if nr is None:
        out["kind"] = "error"
        out["error"] = ("New Relic client not configured -- add the "
                        "API key to pull NR samples")
        return out
    if not nrql:
        out["error"] = "no original NRQL recorded for this target"
        return out
    if not aids:
        out["kind"] = "error"
        out["error"] = ("no New Relic account id available -- pass "
                        "account ids or re-convert so the widget "
                        "report records them")
        return out
    raw = ds_type == "loki"
    query = raw_sample_nrql(nrql, limit) if raw else ""
    kind = "events" if query else "rows"
    query = _nrql_with_range(query or nrql, frm, to)
    out["nrql"] = query
    last_err = ""
    empty_seen = False
    for aid in aids:
        try:
            got = nr.run_nrql(int(aid), query)
        except Exception as e:  # noqa: BLE001 - degrade per side
            last_err = str(e)
            continue
        results = (got or {}).get("results") or []
        if results:
            out["kind"] = kind
            out["samples"] = [_clean_row(r) for r in results[:limit]
                              if isinstance(r, dict)]
            return out
        empty_seen = True
    if empty_seen:
        out["error"] = "New Relic returned no rows for this range"
        return out
    out["kind"] = "error"
    out["error"] = last_err or "no account produced a result"
    return out


def _gf_side(grafana, uid: str, ds_type: str, tgt: Dict[str, Any],
             expr: str, frm: str, to: str, limit: int,
             raw_uid: str) -> Dict[str, Any]:
    """One panel target's Grafana samples."""
    out: Dict[str, Any] = {"kind": "empty", "samples": [], "error": ""}
    if grafana is None:
        out["kind"] = "error"
        out["error"] = "Grafana client not configured"
        return out
    if not uid or _VAR_REF.match(uid):
        out["kind"] = "error"
        out["error"] = ("unresolved datasource ref %r (no %s "
                        "datasource on instance?)"
                        % (raw_uid, ds_type or "matching"))
        return out
    ref = tgt.get("refId") or "A"
    if ds_type == "loki":
        selector = _loki_selector(expr)
        if not selector:
            out["error"] = ("no stream selector found in the LogQL "
                            "expression")
            return out
        target: Dict[str, Any] = {"refId": ref, "expr": selector,
                                  "maxLines": limit}
        out["query"] = selector
    elif ds_type == "prometheus":
        target = {"refId": ref, "expr": expr}
        out["query"] = expr
    else:
        target = dict(tgt)
        for k in ("expr", "query"):
            if isinstance(target.get(k), str):
                target[k] = substitute(target[k])
    try:
        resp = grafana.ds_query(uid, ds_type, target, frm, to)
    except Exception as e:  # noqa: BLE001 - degrade per side
        out["kind"] = "error"
        out["error"] = str(e)
        return out
    res = ((resp or {}).get("results") or {}).get(ref) or {}
    err = _result_error(res)
    if err:
        out["kind"] = "error"
        out["error"] = err
        return out
    frames = res.get("frames") or []
    if ds_type == "loki":
        out["samples"] = _log_lines(frames, limit)
        out["kind"] = "logs" if out["samples"] else "empty"
        if not out["samples"]:
            out["error"] = ("no log lines in Loki for this stream "
                            "selector and range")
    elif ds_type == "prometheus":
        out["samples"] = _point_series(frames, limit)
        out["kind"] = "points" if out["samples"] else "empty"
        if not out["samples"]:
            out["error"] = "the query returned no datapoints"
    else:
        out["samples"] = _frame_rows(frames, limit)
        out["kind"] = "rows" if out["samples"] else "empty"
        if not out["samples"]:
            out["error"] = ("no rows returned (%s datasource; raw "
                            "sampling is best-effort for this type)"
                            % (ds_type or "unknown"))
    return out


# ---------------------------------------------------------------------------
# full dashboard collection
# ---------------------------------------------------------------------------

def collect_samples(nr, account_ids, grafana, dash, widget_report,
                    ds_map=None, frm="now-1h", to="now", limit=5,
                    panel_id=None,
                    log: Optional[Callable[[str], None]] = None) \
        -> Dict[str, Any]:
    """Pull raw samples from both sides for every panel target.

    ``nr`` is a NerdGraphClient (or None), ``grafana`` a GrafanaLive
    (or None), ``dash`` the converted dashboard JSON and
    ``widget_report`` the builder's migration report (source of each
    panel's original NRQL). When ``panel_id`` is given only that
    panel's targets are sampled. Per-side failures become
    ``kind: "error"`` entries; this never raises per panel.
    """
    emit = log or (lambda m: None)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, _MAX_LIMIT))
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
    target_idx: Dict[Any, int] = {}
    for panel, tgt in iter_targets(dash):
        if panel.get("type") in ("row", "text"):
            continue
        pid = panel.get("id")
        idx = target_idx.get(pid, 0)
        target_idx[pid] = idx + 1
        if panel_id is not None and pid != panel_id:
            continue
        ds = tgt.get("datasource") or {}
        raw_uid = ds.get("uid") or ""
        uid = raw_uid
        ds_type = ds.get("type") or ""
        if uid in ds_map:
            uid = ds_map[uid]
        elif _VAR_REF.match(uid):
            uid = ds_map.get(uid[2:-1], uid)
        expr = tgt.get("expr") or tgt.get("query") \
            or tgt.get("queryText") or ""
        if isinstance(expr, str):
            expr = substitute(expr)
        nrql = _nrql_for_target(by_panel.get(pid), idx)
        row: Dict[str, Any] = {
            "panel_id": pid,
            "panel_title": panel.get("title") or "",
            "refId": tgt.get("refId") or "",
            "datasource": uid,
            "ds_type": ds_type,
            "expr": expr,
            "nrql": nrql,
        }
        row["nr"] = _nr_side(nr, aids, nrql, ds_type, frm, to, limit)
        row["grafana"] = _gf_side(grafana, uid, ds_type, tgt, expr,
                                  frm, to, limit, raw_uid)
        panels.append(row)
        emit("  nr:%-7s gf:%-7s %s [%s]"
             % (row["nr"]["kind"], row["grafana"]["kind"],
                row["panel_title"], row["refId"]))
    return {
        "schema": SCHEMA,
        "dashboard": dash.get("title") or dash.get("uid") or "",
        "generated_at": _utcnow(),
        "range": {"from": frm, "to": to},
        "limit": limit,
        "panels": panels,
    }


def merge_samples(existing: Optional[Dict[str, Any]],
                  fresh: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a partial (per-panel) sample run into a stored report.

    Rows are keyed by (panel_id, refId): re-pulled rows replace the
    stored ones in place, new rows append. Top-level metadata comes
    from the fresh run.
    """
    old_panels = (existing or {}).get("panels") or []
    if not old_panels:
        return fresh
    fresh_by_key: Dict[Any, Dict[str, Any]] = {}
    for r in fresh.get("panels") or []:
        fresh_by_key[(r.get("panel_id"), r.get("refId"))] = r
    merged: List[Dict[str, Any]] = []
    used = set()
    for r in old_panels:
        key = (r.get("panel_id"), r.get("refId"))
        if key in fresh_by_key:
            merged.append(fresh_by_key[key])
            used.add(key)
        else:
            merged.append(r)
    for r in fresh.get("panels") or []:
        key = (r.get("panel_id"), r.get("refId"))
        if key not in used:
            merged.append(r)
    out = dict(fresh)
    out["panels"] = merged
    return out


# ---------------------------------------------------------------------------
# review bookkeeping
# ---------------------------------------------------------------------------

def record_review(store, slug: str, panel_id, ref_id: str,
                  verdict: str, note: str = "",
                  source: str = "user") -> Dict[str, Any]:
    """Record a human verdict for one panel target.

    Persists into the per-slug ``"review"`` artifact (read-modify-
    write merge keyed by ``"<panel_id>:<refId>"``) and appends a
    ``panel-review`` change-log entry. Raises :class:`ValueError` for
    an unknown verdict. Returns the stored entry.
    """
    verdict = str(verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError("verdict must be one of %s, got %r"
                         % ("/".join(VERDICTS), verdict))
    ref = ref_id or "A"
    art = None
    try:
        art = store.get_artifact(slug, REVIEW_KIND)
    except Exception:  # noqa: BLE001 - treat as no reviews yet
        art = None
    reviews = dict((art or {}).get("reviews") or {})
    key = "%s:%s" % (panel_id, ref)
    before = (reviews.get(key) or {}).get("verdict") or ""
    entry = {
        "panel_id": panel_id,
        "refId": ref,
        "verdict": verdict,
        "note": note or "",
        "source": source or "user",
        "ts": _utcnow(),
    }
    reviews[key] = entry
    store.save_artifact(slug, REVIEW_KIND,
                        {"schema": REVIEW_SCHEMA, "reviews": reviews})
    try:
        store.log_change(slug, {
            "action": "panel-review",
            "target": "panel %s [%s]" % (panel_id, ref),
            "before": before,
            "after": verdict,
            "why": note or "",
            "source": source if source in ("user", "ai", "auto")
            else "user",
        })
    except Exception:  # noqa: BLE001 - the review itself is saved
        pass
    return entry


def _target_count(store, slug: str) -> Optional[int]:
    """Reviewable target count from the stored dashboard, or None."""
    try:
        row = store.get_dashboard(slug)
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    data = row.get("data")
    dash = None
    if isinstance(data, dict):
        if isinstance(data.get("dashboard"), dict):
            dash = data["dashboard"]
        elif "panels" in data:
            dash = data
    if dash is None:
        return None
    n = 0
    for panel, _tgt in iter_targets(dash):
        if panel.get("type") in ("row", "text"):
            continue
        n += 1
    return n


def review_summary(store, slug: str) -> Dict[str, Any]:
    """Verdict counts for a dashboard's review artifact.

    ``unreviewed`` is computed against the stored dashboard's target
    count when available, else None.
    """
    art = None
    try:
        art = store.get_artifact(slug, REVIEW_KIND)
    except Exception:  # noqa: BLE001
        art = None
    counts = {"confirmed": 0, "rejected": 0, "unsure": 0}
    reviews = (art or {}).get("reviews") or {}
    for entry in reviews.values():
        v = (entry or {}).get("verdict")
        if v in counts:
            counts[v] += 1
    total = _target_count(store, slug)
    reviewed = sum(counts.values())
    out: Dict[str, Any] = dict(counts)
    out["total"] = total
    out["unreviewed"] = max(0, total - reviewed) \
        if total is not None else None
    return out
