"""NR vs Grafana data parity: prove migrated panels show the same data.

For every translated panel target this module runs the original NRQL
through NerdGraph (read-only) and the translated expression through
Grafana's ``/api/ds/query``, normalizes both responses into a common
series shape and compares them.

Common series shape::

    {"labels": {"appName": "web"}, "points": [[epoch_seconds, value], ...]}

The parity report uses schema ``nr2grafana/parity/v1`` and is persisted
by callers as Store artifact kind ``"parity"``.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .livecheck import iter_targets, substitute

SCHEMA = "nr2grafana/parity/v1"
ARTIFACT_KIND = "parity"

# Relative "now-<n><unit>" range spec (mirrors grafana/live.py).
_REL_TIME = re.compile(r"^now(?:-(\d+)([smhdw]))?$")
_UNIT_WORD = {"s": "second", "m": "minute", "h": "hour",
              "d": "day", "w": "week"}

# FACET clause: attribute list up to the next NRQL keyword.
_FACET_RE = re.compile(
    r"\bFACET\s+(.*?)(?=\s+(?:SINCE|UNTIL|TIMESERIES|LIMIT|ORDER|"
    r"COMPARE|WHERE|SLIDE|EXTRAPOLATE|WITH)\b|$)",
    re.IGNORECASE | re.DOTALL)
_FACET_ALIAS = re.compile(r"\bAS\s+[`'\"]?([\w .-]+?)[`'\"]?\s*$",
                          re.IGNORECASE)
_IDENT = re.compile(r"^`?([A-Za-z_][\w.]*)`?$")
_VAR_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# NR result keys that are structure, not values.
_NR_META_KEYS = ("facet", "beginTimeSeconds", "endTimeSeconds",
                 "timestamp", "beginTimeMillis", "endTimeMillis")

# Weight per verdict for the 0-100 score. nr-error rows earn the
# "Grafana has data but no NR comparison" weight when the Grafana side
# did return points (see _row_weight).
_WEIGHTS = {"match": 1.0, "close": 0.8, "nr-empty": 0.6,
            "both-empty": 0.5, "value-mismatch": 0.25,
            "shape-mismatch": 0.25, "gf-empty": 0.25,
            "nr-error": 0.0, "gf-error": 0.0}

# Known constant ratios (grafana/nr) that hint at unit mismatches.
_UNIT_HINTS = [
    (1000.0, "Grafana values ~1000x New Relic: likely ms vs s"),
    (0.001, "Grafana values ~1/1000 of New Relic: likely s vs ms"),
    (60.0, "Grafana values ~60x New Relic: likely per-min vs per-s"),
    (1.0 / 60.0,
     "Grafana values ~1/60 of New Relic: likely per-s vs per-min"),
]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _facet_attrs(nrql: str) -> List[str]:
    """Names of the FACET attributes in an NRQL query, best effort."""
    m = _FACET_RE.search(nrql or "")
    if not m:
        return []
    out: List[str] = []
    for part in m.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        alias = _FACET_ALIAS.search(part)
        if alias:
            out.append(alias.group(1).strip())
            continue
        ident = _IDENT.match(part)
        out.append(ident.group(1) if ident else "facet")
    return out


def normalize_nr(results: List[Dict], nrql: str) -> List[Dict]:
    """Normalize raw NerdGraph NRQL results into the common series shape.

    Handles un-faceted single aggregates (``[{"count": 5}]``), multiple
    aggregates per row (one series per aggregate, labelled), TIMESERIES
    buckets (``beginTimeSeconds``/``endTimeSeconds``) and faceted rows
    (``facet`` as a string or list; labelled with the FACET attribute
    name parsed from the NRQL when known).
    """
    facet_keys = _facet_attrs(nrql)
    series: Dict[Tuple, Dict[str, Any]] = {}
    order: List[Tuple] = []
    for row in results or []:
        if not isinstance(row, dict):
            continue
        labels: Dict[str, str] = {}
        if "facet" in row:
            fvals = row["facet"]
            if not isinstance(fvals, list):
                fvals = [fvals]
            for i, fv in enumerate(fvals):
                if i < len(facet_keys):
                    key = facet_keys[i]
                elif len(fvals) == 1:
                    key = "facet"
                else:
                    key = "facet%d" % i
                labels[key] = "" if fv is None else str(fv)
        ts = None
        if row.get("beginTimeSeconds") is not None:
            ts = float(row["beginTimeSeconds"])
        elif row.get("endTimeSeconds") is not None:
            ts = float(row["endTimeSeconds"])
        elif row.get("timestamp") is not None:
            t = float(row["timestamp"])
            ts = t / 1000.0 if t > 1e11 else t
        skip = set(_NR_META_KEYS) | set(facet_keys)
        values: Dict[str, float] = {}
        for k, v in row.items():
            if k in skip or isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                values[k] = float(v)
        multi = len(values) > 1
        for k in sorted(values):
            lb = dict(labels)
            if multi:
                lb["aggregate"] = k
            key = tuple(sorted(lb.items()))
            if key not in series:
                series[key] = {"labels": lb, "points": []}
                order.append(key)
            series[key]["points"].append(
                [ts if ts is not None else 0.0, values[k]])
    out = [series[k] for k in order]
    for s in out:
        s["points"].sort(key=lambda p: p[0])
    return out


def _to_seconds(t: float) -> float:
    """Grafana frame times are epoch ms; normalize to epoch seconds."""
    return t / 1000.0 if t > 1e11 else t


def normalize_grafana(ds_query_response: Dict, ref_id: str) -> List[Dict]:
    """Normalize one refId of a ``/api/ds/query`` response into series.

    Time-field frames yield one series per numeric field (labels from
    ``schema.fields[].labels``); table frames (no time field) yield one
    one-point series per row, labelled by the frame's string columns.
    """
    res = ((ds_query_response or {}).get("results") or {}).get(ref_id) \
        or {}
    out: List[Dict[str, Any]] = []
    for frame in res.get("frames") or []:
        if not isinstance(frame, dict):
            continue
        fields = ((frame.get("schema") or {}).get("fields")) or []
        cols = ((frame.get("data") or {}).get("values")) or []

        def col(i):
            return cols[i] if i < len(cols) else []

        time_idx = None
        time_is_ms = False
        num_idx: List[int] = []
        str_idx: List[int] = []
        for i, f in enumerate(fields):
            ftype = f.get("type") or ""
            name = f.get("name") or ""
            if time_idx is None and (ftype == "time"
                                     or name in ("Time", "time")):
                time_idx = i
                # Grafana dataframe time fields are epoch milliseconds.
                time_is_ms = ftype == "time"
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

        if time_idx is not None:
            tcol = col(time_idx)
            for i in num_idx:
                f = fields[i]
                labels = dict(f.get("labels") or {})
                if not labels and len(num_idx) > 1:
                    labels = {"field": f.get("name") or "value%d" % i}
                points = []
                for j, v in enumerate(col(i)):
                    if v is None or isinstance(v, bool):
                        continue
                    if j >= len(tcol) or tcol[j] is None:
                        continue
                    t = float(tcol[j])
                    t = t / 1000.0 if time_is_ms else _to_seconds(t)
                    points.append([t, float(v)])
                if points:
                    points.sort(key=lambda p: p[0])
                    out.append({"labels": labels, "points": points})
            continue

        # Table / scalar frame: rows become one-point series.
        nrows = max((len(col(i)) for i in num_idx + str_idx), default=0)
        for r in range(nrows):
            row_labels: Dict[str, str] = {}
            for i in str_idx:
                c = col(i)
                if r < len(c) and c[r] is not None:
                    row_labels[fields[i].get("name") or "col%d" % i] = \
                        str(c[r])
            for i in num_idx:
                c = col(i)
                v = c[r] if r < len(c) else None
                if v is None or isinstance(v, bool):
                    continue
                lb = dict(row_labels)
                if len(num_idx) > 1:
                    lb["field"] = fields[i].get("name") or "value%d" % i
                out.append({"labels": lb,
                            "points": [[0.0, float(v)]]})
    return out


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

_SAMPLE_CAP = 40


def _sample_points(series: List[Dict],
                   cap: int = _SAMPLE_CAP) -> List[List[float]]:
    """Evenly-downsampled ``[t, v]`` points from the largest series.

    Powers the web UI's side-by-side mini sparklines; capped so parity
    artifacts stay small even for dense range queries.
    """
    best: List[Any] = []
    for s in series or []:
        pts = s.get("points") or []
        if len(pts) > len(best):
            best = pts
    if not best:
        return []
    if len(best) <= cap:
        idx = range(len(best))
    else:
        step = (len(best) - 1) / float(cap - 1)
        idx = sorted(set(int(round(i * step)) for i in range(cap)))
    return [[float(best[i][0]), float(best[i][1])] for i in idx]


def _summary(series: List[Dict]) -> Dict[str, Any]:
    values = [p[1] for s in series or [] for p in s.get("points") or []]
    out: Dict[str, Any] = {"series": len(series or []),
                           "points": len(values)}
    if values:
        out["mean"] = sum(values) / len(values)
        out["min"] = min(values)
        out["max"] = max(values)
        out["last"] = values[-1]
        out["sample"] = _sample_points(series)
    else:
        out["mean"] = None
    return out


def _median(values: List[float]) -> float:
    vs = sorted(values)
    n = len(vs)
    mid = n // 2
    if n % 2:
        return vs[mid]
    return (vs[mid - 1] + vs[mid]) / 2.0


def _label_values(s: Dict) -> frozenset:
    return frozenset(str(v).lower()
                     for v in (s.get("labels") or {}).values())


def _pair_series(nr: List[Dict], gf: List[Dict]) \
        -> Tuple[List[Tuple[Dict, Dict]], int]:
    """Pair NR and Grafana series; returns (pairs, unpaired_count).

    Single series pair directly; otherwise pairing is by shared label
    values (facet values survive translation even when label names do
    not). Equal-length leftovers pair by sorted label order.
    """
    if len(nr) == 1 and len(gf) == 1:
        return [(nr[0], gf[0])], 0
    pairs: List[Tuple[Dict, Dict]] = []
    used = set()
    unpaired_nr: List[Dict] = []
    for sa in nr:
        va = _label_values(sa)
        best = None
        best_score = 0
        for j, sb in enumerate(gf):
            if j in used:
                continue
            score = len(va & _label_values(sb))
            if score > best_score:
                best, best_score = j, score
        if best is None:
            unpaired_nr.append(sa)
        else:
            used.add(best)
            pairs.append((sa, gf[best]))
    left_gf = [s for j, s in enumerate(gf) if j not in used]
    if not pairs and len(unpaired_nr) == len(left_gf):
        key = lambda s: sorted((s.get("labels") or {}).items())  # noqa: E731
        pairs = list(zip(sorted(unpaired_nr, key=key),
                         sorted(left_gf, key=key)))
        return pairs, 0
    return pairs, len(unpaired_nr) + len(left_gf)


def _median_interval(points: List[List[float]]) -> float:
    if len(points) < 2:
        return 0.0
    gaps = [points[i + 1][0] - points[i][0]
            for i in range(len(points) - 1)]
    return _median(gaps)


def _align(pa: List[List[float]], pb: List[List[float]]) \
        -> List[Tuple[float, float]]:
    """Pair points by nearest timestamp; returns [(nr_val, gf_val)]."""
    if not pa or not pb:
        return []
    window = max(_median_interval(pa), _median_interval(pb))
    if window <= 0:
        # Scalars / single points: timestamps carry no information.
        if len(pa) == 1 and len(pb) == 1:
            return [(pa[0][1], pb[0][1])]
        window = 60.0
    out: List[Tuple[float, float]] = []
    j = 0
    for ta, va in pa:
        while j + 1 < len(pb) and abs(pb[j + 1][0] - ta) \
                <= abs(pb[j][0] - ta):
            j += 1
        if abs(pb[j][0] - ta) <= window * 0.75:
            out.append((va, pb[j][1]))
    return out


def _unit_hint(ratio: float) -> str:
    for const, text in _UNIT_HINTS:
        if abs(ratio - const) <= 0.1 * const:
            return text
    return ""


def compare(nr_series: List[Dict], gf_series: List[Dict],
            tolerance: float = 0.15) -> Dict[str, Any]:
    """Compare normalized NR and Grafana series lists.

    Returns ``{"verdict", "detail", "ratio", "nr_summary",
    "gf_summary"}``. Verdicts: match | close | value-mismatch |
    shape-mismatch | nr-empty | gf-empty | both-empty. ``close`` means
    within tolerance or a consistent constant ratio (noise-tolerant:
    median ratio with a MAD check); known constants add a unit hint.
    """
    nr_sum = _summary(nr_series)
    gf_sum = _summary(gf_series)
    result = {"verdict": "", "detail": "", "ratio": None,
              "nr_summary": nr_sum, "gf_summary": gf_sum}
    nr_has = nr_sum["points"] > 0
    gf_has = gf_sum["points"] > 0
    if not nr_has and not gf_has:
        result["verdict"] = "both-empty"
        result["detail"] = "no data on either side for this range"
        return result
    if not nr_has:
        result["verdict"] = "nr-empty"
        result["detail"] = ("New Relic returned no data; Grafana has %d "
                            "point(s) (cannot verify values)"
                            % gf_sum["points"])
        return result
    if not gf_has:
        result["verdict"] = "gf-empty"
        result["detail"] = ("New Relic has %d point(s) but the Grafana "
                            "query returned none" % nr_sum["points"])
        return result

    pairs, unpaired = _pair_series(nr_series, gf_series)
    if not pairs or unpaired > len(pairs):
        result["verdict"] = "shape-mismatch"
        result["detail"] = ("series do not line up: New Relic %d "
                            "series/%d points vs Grafana %d series/%d "
                            "points" % (nr_sum["series"],
                                        nr_sum["points"],
                                        gf_sum["series"],
                                        gf_sum["points"]))
        return result

    eps = 1e-12
    rel_errs: List[float] = []
    point_rels: List[float] = []
    ratios: List[float] = []
    for sa, sb in pairs:
        va = [p[1] for p in sa["points"]]
        vb = [p[1] for p in sb["points"]]
        ma = sum(va) / len(va)
        mb = sum(vb) / len(vb)
        rel_errs.append(abs(mb - ma) / max(abs(ma), abs(mb), eps))
        for a, b in _align(sa["points"], sb["points"]):
            point_rels.append(abs(b - a) / max(abs(a), abs(b), eps))
            if abs(a) > eps:
                ratios.append(b / a)
    worst = max(rel_errs)
    point_med = _median(point_rels) if point_rels else None
    mean_note = ("NR mean %.4g vs Grafana mean %.4g"
                 % (nr_sum["mean"], gf_sum["mean"]))
    extra = "; %d unmatched series" % unpaired if unpaired else ""

    if worst <= 0.02 and (point_med is None or point_med <= 0.05):
        result["verdict"] = "match"
        result["detail"] = "values agree (%s)%s" % (mean_note, extra)
        return result
    if worst <= tolerance and (point_med is None
                               or point_med <= tolerance):
        result["verdict"] = "close"
        result["detail"] = ("within %.0f%% tolerance (%s, %.0f%% off)%s"
                            % (tolerance * 100, mean_note,
                               worst * 100, extra))
        return result

    if ratios:
        med_r = _median(ratios)
        mad = _median([abs(r - med_r) for r in ratios])
        consistent = abs(med_r) > eps \
            and mad <= max(0.1 * abs(med_r), eps)
        hint = _unit_hint(med_r) if consistent else ""
        if consistent and (hint or len(ratios) >= 3):
            result["verdict"] = "close"
            result["ratio"] = med_r
            detail = ("consistent constant ratio ~%.4g "
                      "(Grafana/New Relic)" % med_r)
            if hint:
                detail += " - " + hint
            result["detail"] = detail + extra
            return result

    result["verdict"] = "value-mismatch"
    result["detail"] = ("values differ: %s (%.0f%% off)%s"
                        % (mean_note, worst * 100, extra))
    return result


# ---------------------------------------------------------------------------
# Full dashboard run
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


def _row_weight(row: Dict[str, Any]) -> float:
    verdict = row.get("verdict", "")
    if verdict == "nr-error" \
            and (row.get("gf_summary") or {}).get("points"):
        return 0.6  # Grafana has data; only the NR comparison failed.
    return _WEIGHTS.get(verdict, 0.0)


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


def run_parity(nr, account_ids, grafana, dash, widget_report,
               ds_map=None, frm="now-1h", to="now", log=None) \
        -> Dict[str, Any]:
    """Run every translated panel target on both sides and compare.

    ``nr`` is a NerdGraphClient (or None), ``account_ids`` the account
    id(s) to run NRQL against, ``grafana`` a GrafanaLive (or None),
    ``dash`` the converted dashboard JSON and ``widget_report`` the
    builder's migration report (source of each panel's original NRQL).
    Per-side failures become verdict ``nr-error`` / ``gf-error`` rows;
    this never raises per panel.
    """
    emit = log or (lambda m: None)
    if isinstance(account_ids, (int, str)):
        account_ids = [account_ids]
    aids = [int(a) for a in (account_ids or [])]

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

    nr_cache: Dict[Tuple[int, str], Any] = {}

    def nr_side(nrql):
        """Returns (series, error). Tries each account id in order."""
        if nr is None:
            return None, "New Relic client not configured"
        if not nrql:
            return None, "no original NRQL recorded for this target"
        if not aids:
            return None, "no New Relic account id available"
        q = _nrql_with_range(nrql, frm, to)
        empty_seen = False
        last_err = "no account produced a result"
        for aid in aids:
            key = (aid, q)
            if key not in nr_cache:
                try:
                    nr_cache[key] = nr.run_nrql(aid, q)
                except Exception as e:  # noqa: BLE001
                    nr_cache[key] = e
            got = nr_cache[key]
            if isinstance(got, Exception):
                last_err = str(got)
                continue
            results = got.get("results") or []
            if results:
                return normalize_nr(results, q), ""
            empty_seen = True
        if empty_seen:
            return [], ""
        return None, last_err

    panels: List[Dict[str, Any]] = []
    target_idx: Dict[Any, int] = {}
    for panel, tgt in iter_targets(dash):
        if panel.get("type") in ("row", "text"):
            continue
        pid = panel.get("id")
        idx = target_idx.get(pid, 0)
        target_idx[pid] = idx + 1
        ds = tgt.get("datasource") or {}
        uid = ds.get("uid") or ""
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
            "nrql": nrql, "expr": expr, "datasource": uid,
            "verdict": "", "detail": "", "ratio": None,
            "nr_summary": {}, "gf_summary": {},
        }
        panels.append(row)

        nr_series, nr_err = nr_side(nrql)

        gf_series = None
        gf_err = ""
        if grafana is None:
            gf_err = "Grafana client not configured"
        elif not uid or _VAR_REF.match(uid):
            gf_err = ("unresolved datasource ref %r (no %s datasource "
                      "on instance?)"
                      % (ds.get("uid"), ds_type or "matching"))
        else:
            target = dict(tgt)
            for k in ("expr", "query"):
                if isinstance(tgt.get(k), str):
                    target[k] = substitute(tgt[k])
            try:
                resp = grafana.ds_query(uid, ds_type, target, frm, to)
                gf_series = normalize_grafana(resp, row["refId"] or "A")
            except Exception as e:  # noqa: BLE001
                gf_err = str(e)

        if gf_series is not None:
            row["gf_summary"] = _summary(gf_series)
        if nr_series is not None:
            row["nr_summary"] = _summary(nr_series)

        if gf_err:
            row["verdict"] = "gf-error"
            row["detail"] = gf_err
            if nr_err:
                row["detail"] += "; NR side also failed: %s" % nr_err
        elif nr_err:
            row["verdict"] = "nr-error"
            row["detail"] = nr_err
        else:
            cmp_result = compare(nr_series, gf_series)
            row["verdict"] = cmp_result["verdict"]
            row["detail"] = cmp_result["detail"]
            row["ratio"] = cmp_result["ratio"]
            row["nr_summary"] = cmp_result["nr_summary"]
            row["gf_summary"] = cmp_result["gf_summary"]
        emit("  %-15s %s [%s] %s" % (row["verdict"],
                                     row["panel_title"], row["refId"],
                                     row["detail"]))

    summary: Dict[str, int] = {}
    for row in panels:
        summary[row["verdict"]] = summary.get(row["verdict"], 0) + 1
    score = 0
    if panels:
        score = int(round(100.0 * sum(_row_weight(r) for r in panels)
                          / len(panels)))
    return {
        "schema": SCHEMA,
        "dashboard": dash.get("title") or dash.get("uid") or "",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime()),
        "range": {"from": frm, "to": to},
        "panels": panels,
        "score": score,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def _review_rows(review) -> List[Dict[str, Any]]:
    """Verdict entries from a review artifact (or raw mapping)."""
    if not isinstance(review, dict):
        return []
    raw = review.get("reviews")
    if not isinstance(raw, dict):
        raw = review
    return [v for v in raw.values()
            if isinstance(v, dict) and v.get("verdict")]


def readiness(parity, check_rows=None, test_rows=None,
              review=None) -> Dict[str, Any]:
    """Overall migration readiness from parity + check + test + human
    review artifacts.

    Returns ``{"score": 0-100, "grade": "ready|almost|blocked",
    "reasons": [...]}``. Missing required datasources/plugins always
    block; otherwise the grade follows the (penalized) parity score.
    ``review`` is the "review" artifact (human sample sign-off): any
    rejected panel forces "blocked"; every panel confirmed adds a
    "human-verified" reason and floors the score at 90 when nothing
    else blocks.
    """
    reasons: List[str] = []
    summary: Dict[str, int] = {}
    score = 0
    if isinstance(parity, dict) and parity.get("panels") is not None:
        score = int(parity.get("score") or 0)
        summary = parity.get("summary") or {}
    elif test_rows:
        ok = sum(1 for r in test_rows if r.get("status") == "data")
        score = int(round(100.0 * ok / len(test_rows)))
        reasons.append("no parity report yet: score is data-test "
                       "coverage only")

    missing = [r for r in (check_rows or [])
               if r.get("status") in ("missing", "wrong-type")]
    for r in missing:
        reasons.append("%s: %s" % (r.get("item") or "requirement",
                                   r.get("fix") or r.get("detail")
                                   or r.get("status")))

    def count(*verdicts):
        return sum(int(summary.get(v) or 0) for v in verdicts)

    n = count("gf-empty")
    if n:
        reasons.append("%d panel(s) return no data in Grafana while "
                       "New Relic has data" % n)
    n = count("value-mismatch", "shape-mismatch")
    if n:
        reasons.append("%d panel(s) show data that does not match "
                       "New Relic" % n)
    n = count("gf-error", "nr-error")
    if n:
        reasons.append("%d panel(s) hit errors during the parity run"
                       % n)
    n = count("nr-empty", "both-empty")
    if n:
        reasons.append("%d panel(s) had no New Relic data to compare "
                       "against" % n)
    if test_rows and summary:
        n = sum(1 for r in test_rows if r.get("status") == "error")
        if n:
            reasons.append("%d panel data-test error(s)" % n)

    rows = _review_rows(review)
    rejected = [r for r in rows if r.get("verdict") == "rejected"]
    if rejected:
        names = ", ".join("panel %s [%s]" % (r.get("panel_id"),
                                             r.get("refId") or "A")
                          for r in rejected[:6])
        if len(rejected) > 6:
            names += ", ..."
        reasons.append("%d panel(s) rejected in human sample review: "
                       "%s" % (len(rejected), names))
    all_confirmed = bool(rows) and all(
        r.get("verdict") == "confirmed" for r in rows)
    if all_confirmed:
        # "All panels" needs a known denominator: the parity run's
        # target rows, else the data-test rows. Without one, a
        # partial review must not count as full human verification.
        n_targets = None
        if isinstance(parity, dict) \
                and parity.get("panels") is not None:
            n_targets = len(parity.get("panels") or [])
        elif test_rows:
            n_targets = len(test_rows)
        if n_targets is None or len(rows) < n_targets:
            all_confirmed = False
    if all_confirmed and not missing:
        score = max(score, 90)
        reasons.append("human-verified: every reviewed panel's live "
                       "samples were confirmed by a person")

    score = max(0, score - 15 * len(missing))
    if missing or rejected:
        grade = "blocked"
    elif score >= 85:
        grade = "ready"
    elif score >= 60:
        grade = "almost"
    else:
        grade = "blocked"
    if grade == "ready" and not reasons:
        reasons.append("all compared panels agree with New Relic")
    return {"score": score, "grade": grade, "reasons": reasons}
