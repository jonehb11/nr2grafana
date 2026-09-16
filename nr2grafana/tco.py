"""TCO trend engine: cost trends, forecast, anomalies and attribution.

Given the user's own AWS Cost Explorer data (read through the read-only
:mod:`nr2grafana.awscost` module), this module builds a monthly cost
series, computes trends (month-over-month deltas, growth, CAGR, run-rate,
direction, a naive linear projection), forecasts the next few months (the
AWS CE forecast alongside our own linear extrapolation), lists CE cost
anomalies, estimates the **observability share** of the bill, and
correlates the tool's own recorded optimization actions against the cost
movements that followed.

Everything here is an ESTIMATE built on the caller's Cost Explorer data
with clearly labeled assumptions. Cost correlation is presented as
correlation, never proof of causation -- a bill moves for many reasons.

The AWS side is duck-typed: pass the :mod:`nr2grafana.awscost` module (or
any object exposing ``get_cost_and_usage`` / ``get_cost_forecast`` /
``get_anomalies``). AWS access is OPTIONAL; missing or broken calls yield
actionable notes and a partial report -- never a traceback.

Report schema: "nr2grafana/tco/v1".
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

SCHEMA = "nr2grafana/tco/v1"
SERIES_SCHEMA = "nr2grafana/tco-series/v1"
SNAPSHOTS_SCHEMA = "nr2grafana/tco-snapshots/v1"
TREND_SCHEMA = "nr2grafana/tco-trend/v1"
GENERATED_BY = "nr2grafana 1.7.0"

# CE cost metrics, most-preferred first. UnblendedCost is the everyday
# "what you pay" figure; the rest are fallbacks if it is absent.
METRIC_PREFERENCE = ("UnblendedCost", "NetUnblendedCost", "AmortizedCost",
                     "NetAmortizedCost", "BlendedCost")

# A month's total must move more than this fraction of the prior month to
# count as "up"/"down" rather than "flat".
FLAT_FRACTION = 0.05

# Snapshot history is stored as a single artifact under this slug/kind.
SNAP_SLUG = "_tco"
SNAP_KIND = "tco-snapshot"

# ASSUMPTION: generic S3 Standard storage rate, used only when bucket sizes
# (bytes) are supplied without an explicit dollar figure.
S3_USD_PER_GB_MONTH = 0.023
BYTES_PER_GB = 1_000_000_000.0

# Substring keys (case-insensitive) used to pick CE SERVICE lines.
_EC2_KEYS = ("elastic compute cloud",)
_S3_KEYS = ("simple storage service", "amazon s3")
_XFER_KEYS = ("data transfer", "ec2 - other")


class TcoError(Exception):
    """Raised for a hard TCO failure (e.g. no usable AWS client)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _num(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` (incl. CE string amounts) to a float."""
    try:
        if value is None:
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:            # NaN guard
        return default
    return out


def _r(value: float, places: int = 2) -> float:
    return round(float(value), places)


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < low:
        return low
    if n > high:
        return high
    return n


def _utcnow() -> str:
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_months(d: date, n: int) -> date:
    """First-of-month ``d`` shifted by ``n`` months (n may be negative)."""
    total = (d.year * 12 + (d.month - 1)) + n
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


def _month_range(months: int) -> Any:
    """(start, end) ISO dates spanning ``months`` up to and incl. today.

    ``start`` is the first day of the month ``months-1`` months back;
    ``end`` is exclusive (tomorrow) so the current partial month is
    included. CE marks the current month ``Estimated``.
    """
    today = _today()
    cur = _month_start(today)
    start = _add_months(cur, -(months - 1))
    end = today + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _aws_fn(aws: Any, name: str) -> Optional[Callable]:
    """Return a callable ``aws.<name>`` (module or object), else None."""
    if aws is None:
        return None
    fn = getattr(aws, name, None)
    return fn if callable(fn) else None


def _aws_hint(action: str, exc: Exception) -> str:
    return ("could not %s via AWS Cost Explorer: %s. Check that the aws CLI "
            "is installed and configured (read-only), that Cost Explorer is "
            "enabled for the account, and that your credentials allow ce:* "
            "read calls." % (action, str(exc)[:200]))


def _median(values: List[float]) -> float:
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _linreg(values: List[float]) -> Any:
    """Least-squares fit over x = 0..n-1. Returns (slope, intercept, r2)."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    if n == 1:
        return 0.0, float(values[0]), 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n))
    if sxx == 0:
        return 0.0, mean_y, 0.0
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    syy = sum((values[i] - mean_y) ** 2 for i in range(n))
    if syy == 0:
        r2 = 1.0
    else:
        ss_res = sum(
            (values[i] - (slope * xs[i] + intercept)) ** 2
            for i in range(n))
        r2 = 1.0 - ss_res / syy
    return slope, intercept, r2


# ---------------------------------------------------------------------------
# CE parsing
# ---------------------------------------------------------------------------

def _metric_amount(metrics: Any) -> float:
    """Best cost amount from a CE Metrics dict (per preference order)."""
    if not isinstance(metrics, dict):
        return 0.0
    for key in METRIC_PREFERENCE:
        blk = metrics.get(key)
        if isinstance(blk, dict) and "Amount" in blk:
            return _num(blk.get("Amount"))
    for blk in metrics.values():
        if isinstance(blk, dict) and "Amount" in blk:
            return _num(blk.get("Amount"))
    return 0.0


def _currency(rbt: List[Dict[str, Any]]) -> str:
    for r in rbt:
        tot = r.get("Total") or {}
        for blk in tot.values():
            if isinstance(blk, dict) and blk.get("Unit"):
                return str(blk["Unit"])
        for g in (r.get("Groups") or []):
            for blk in (g.get("Metrics") or {}).values():
                if isinstance(blk, dict) and blk.get("Unit"):
                    return str(blk["Unit"])
    return "USD"


def _empty_series(group_by: str) -> Dict[str, Any]:
    return {
        "schema": SERIES_SCHEMA,
        "currency": "USD",
        "granularity": "MONTHLY",
        "group_by": group_by,
        "months": [],
        "groups": {},
        "group_order": [],
        "total": [],
        "estimated": [],
        "range": {},
    }


def _parse_series(raw: Any, group_by: str, start: str,
                  end: str) -> Dict[str, Any]:
    rbt = raw.get("ResultsByTime") if isinstance(raw, dict) else None
    rbt = rbt if isinstance(rbt, list) else []

    months: List[str] = []
    totals: List[float] = []
    estimated: List[bool] = []
    by_month: Dict[str, Dict[str, float]] = {}
    order: List[str] = []

    for i, r in enumerate(rbt):
        tp = r.get("TimePeriod", {}) or {}
        mstart = str(tp.get("Start", "") or "")
        label = mstart[:7] if len(mstart) >= 7 else ("m%d" % i)
        months.append(label)
        estimated.append(bool(r.get("Estimated", False)))

        groups = r.get("Groups") or []
        if groups:
            mtotal = 0.0
            for g in groups:
                keys = g.get("Keys") or ["(unknown)"]
                name = str(keys[0]) if keys else "(unknown)"
                amt = _metric_amount(g.get("Metrics") or {})
                by_month.setdefault(name, {})[label] = amt
                if name not in order:
                    order.append(name)
                mtotal += amt
            totals.append(_r(mtotal, 6))
        else:
            totals.append(_r(_metric_amount(r.get("Total") or {}), 6))

    group_series: Dict[str, List[float]] = {}
    for name in order:
        vals = by_month[name]
        group_series[name] = [_r(vals.get(lbl, 0.0), 6) for lbl in months]

    return {
        "schema": SERIES_SCHEMA,
        "currency": _currency(rbt),
        "granularity": "MONTHLY",
        "group_by": group_by,
        "months": months,
        "groups": group_series,
        "group_order": order,
        "total": totals,
        "estimated": estimated,
        "range": {"start": start, "end": end},
    }


# ---------------------------------------------------------------------------
# cost_series
# ---------------------------------------------------------------------------

def cost_series(aws_mod_or_client: Any, months: int = 6,
                group_by: str = "SERVICE", profile: str = "",
                region: str = "us-east-1",
                log: Optional[Callable[[str], None]] = None
                ) -> Dict[str, Any]:
    """Monthly cost per group over ``months`` via CE get-cost-and-usage.

    ``aws_mod_or_client`` is the :mod:`nr2grafana.awscost` module or any
    object exposing ``get_cost_and_usage``. Returns a series document
    (schema "nr2grafana/tco-series/v1"). Raises :class:`TcoError` with an
    actionable message if AWS is unavailable or the call fails.
    """
    emit = log or (lambda m: None)
    fn = _aws_fn(aws_mod_or_client, "get_cost_and_usage")
    if fn is None:
        raise TcoError(
            "this AWS client exposes no get_cost_and_usage(); pass the "
            "nr2grafana.awscost module or a compatible read-only client.")

    months = _clamp_int(months, 1, 24, 6)
    start, end = _month_range(months)
    group_defs = None
    if group_by:
        group_defs = [{"Type": "DIMENSION", "Key": str(group_by)}]

    emit("tco: fetching %d month(s) of cost grouped by %s ..."
         % (months, group_by or "TOTAL"))
    try:
        raw = fn(start, end, granularity="MONTHLY", group_by=group_defs,
                 metrics=["UnblendedCost"], region=region, profile=profile)
    except Exception as exc:  # noqa: BLE001 - surfaced as actionable error
        raise TcoError(_aws_hint("get cost and usage", exc))
    return _parse_series(raw, group_by, start, end)


# ---------------------------------------------------------------------------
# trends
# ---------------------------------------------------------------------------

def _trend_obj(values: List[float]) -> Dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"points": 0, "first": 0.0, "last": 0.0, "delta_total": 0.0,
                "mom_deltas": [], "pct_growth": [], "avg_mom_pct": None,
                "cagr_monthly_pct": None, "run_rate_monthly": 0.0,
                "run_rate_annual": 0.0, "direction": "flat",
                "projection": {"slope_per_month": 0.0, "intercept": 0.0,
                               "next_month": 0.0, "r2": 0.0}}

    first = values[0]
    last = values[-1]
    mom = [_r(values[i] - values[i - 1], 4) for i in range(1, n)]
    pct: List[Optional[float]] = []
    for i in range(1, n):
        prev = values[i - 1]
        if prev > 0:
            pct.append(_r((values[i] - prev) / prev * 100.0, 3))
        else:
            pct.append(None)

    real_pct = [p for p in pct if p is not None]
    avg_pct = _r(sum(real_pct) / len(real_pct), 3) if real_pct else None

    cagr = None
    if first > 0 and n > 1:
        cagr = _r(((last / first) ** (1.0 / (n - 1)) - 1.0) * 100.0, 3)

    slope, intercept, r2 = _linreg(values)
    next_month = max(0.0, slope * n + intercept)

    change = last - first
    base = max(abs(first), 1.0)
    if abs(change) / base < FLAT_FRACTION:
        direction = "flat"
    elif change > 0:
        direction = "up"
    else:
        direction = "down"

    return {
        "points": n,
        "first": _r(first),
        "last": _r(last),
        "delta_total": _r(change),
        "mom_deltas": mom,
        "pct_growth": pct,
        "avg_mom_pct": avg_pct,
        "cagr_monthly_pct": cagr,
        "run_rate_monthly": _r(last),
        "run_rate_annual": _r(last * 12.0),
        "direction": direction,
        "projection": {
            "slope_per_month": _r(slope, 4),
            "intercept": _r(intercept, 4),
            "next_month": _r(next_month),
            "r2": _r(r2, 4),
        },
    }


def trends(series: Dict[str, Any]) -> Dict[str, Any]:
    """Per-group and total trend metrics for a cost ``series``.

    Computes month-over-month deltas, % growth, average growth, monthly
    CAGR, run-rate (monthly + annualized), a coarse direction, and a
    least-squares linear projection of the next month.
    """
    series = series if isinstance(series, dict) else {}
    totals = series.get("total") or []
    groups = series.get("groups") or {}
    order = series.get("group_order") or list(groups.keys())

    by_group: Dict[str, Any] = {}
    for name in order:
        by_group[name] = _trend_obj(groups.get(name) or [])

    return {
        "window_months": len(totals),
        "total": _trend_obj(totals),
        "by_group": by_group,
    }


# ---------------------------------------------------------------------------
# observability attribution
# ---------------------------------------------------------------------------

def _latest_total_by_keys(series: Dict[str, Any],
                          keys: Any) -> Optional[float]:
    """Sum the latest-month cost of groups whose name matches a key."""
    groups = series.get("groups") or {}
    months = series.get("months") or []
    if not groups or not months:
        return None
    lower_keys = [k.lower() for k in keys]
    total = 0.0
    matched = False
    for name, vals in groups.items():
        low = name.lower()
        if any(k in low for k in lower_keys) and vals:
            total += _num(vals[-1])
            matched = True
    return _r(total) if matched else None


def _share_pct(part: Optional[float],
               whole: Optional[float]) -> Optional[float]:
    if part is None or whole is None or whole <= 0:
        return None
    return _r(part / whole * 100.0, 2)


def _packing_pool_cost(packing: Any) -> Optional[float]:
    if not isinstance(packing, dict):
        return None
    topo = packing.get("topology") or {}
    cost = topo.get("pool_cost_mo")
    if cost is None:
        sim = packing.get("packing_sim") or {}
        cost = sim.get("current_pool_cost_mo")
    return _r(_num(cost)) if cost is not None else None


def _classify_bucket(name: str) -> str:
    low = name.lower()
    if "mimir" in low or "cortex" in low:
        return "mimir"
    if "loki" in low:
        return "loki"
    if "tempo" in low:
        return "tempo"
    return "other-obs"


def _bucket_cost(buckets: Any, assumptions: List[str]) -> Any:
    """(obs_s3_usd_or_None, rows) from a buckets spec.

    ``buckets`` may be a dict ``{name: {"monthly_usd"|"cost_mo"|"bytes"|
    "size_bytes": ...}}`` or a plain list of bucket names. Dollar figures
    are used directly; byte sizes are priced with :data:`S3_USD_PER_GB_MONTH`
    (a labeled assumption). A bare name list yields component labels with an
    unknown cost.
    """
    rows: List[Dict[str, Any]] = []
    if not buckets:
        return None, rows

    used_byte_rate = False
    total = 0.0
    have_cost = False

    items: List[Any]
    if isinstance(buckets, dict):
        items = list(buckets.items())
    elif isinstance(buckets, (list, tuple)):
        items = [(b, None) for b in buckets]
    else:
        return None, rows

    for name, info in items:
        name = str(name)
        comp = _classify_bucket(name)
        cost: Optional[float] = None
        if isinstance(info, dict):
            if info.get("monthly_usd") is not None:
                cost = _r(_num(info.get("monthly_usd")))
            elif info.get("cost_mo") is not None:
                cost = _r(_num(info.get("cost_mo")))
            else:
                b = info.get("bytes")
                if b is None:
                    b = info.get("size_bytes")
                if b is not None:
                    cost = _r(_num(b) / BYTES_PER_GB * S3_USD_PER_GB_MONTH)
                    used_byte_rate = True
        elif isinstance(info, (int, float)):
            cost = _r(_num(info))
        rows.append({"bucket": name, "component": comp,
                     "monthly_usd": cost})
        if cost is not None:
            total += cost
            have_cost = True

    if used_byte_rate:
        assumptions.append(
            "S3 obs-bucket cost from bytes priced at $%.3f/GB-month "
            "(S3 Standard assumption); real cost varies by storage class "
            "and request/transfer charges." % S3_USD_PER_GB_MONTH)
    return (_r(total) if have_cost else None), rows


def _deepdive_network(deepdive: Any) -> Dict[str, Any]:
    if not isinstance(deepdive, dict):
        return {}
    sec = ((deepdive.get("sections") or {}).get("network") or {})
    return sec if isinstance(sec, dict) else {}


def attribute_observability(series: Dict[str, Any], deepdive: Any = None,
                            traffic: Any = None, packing: Any = None,
                            buckets: Any = None) -> Dict[str, Any]:
    """Estimate the observability share of the AWS bill.

    Attributes three cost families to the LGTM stack, each a clearly
    labeled ESTIMATE:

    * **EC2** -- the observability node pool cost from ``packing``
      (``topology.pool_cost_mo``), compared to the CE EC2 line.
    * **S3** -- Mimir/Loki/Tempo object-store buckets, from ``buckets``.
    * **Data transfer / TGW / NAT** -- from the deep-dive's measured
      remote_write wire volume and its per-path egress cost estimates.

    Returns per-family dollars + share of the matching CE service line,
    plus a total observability estimate and its share of overall spend.
    """
    series = series if isinstance(series, dict) else {}
    assumptions = [
        "Observability attribution is an ESTIMATE: it maps the tool's own "
        "topology/traffic measurements onto your CE service lines, it is "
        "not a tagged cost allocation.",
    ]

    totals = series.get("total") or []
    latest_total = _r(_num(totals[-1])) if totals else None

    # EC2 -----------------------------------------------------------------
    ec2_service = _latest_total_by_keys(series, _EC2_KEYS)
    obs_ec2 = _packing_pool_cost(packing)
    ec2 = {
        "obs_monthly_usd": obs_ec2,
        "service_total_usd": ec2_service,
        "share_pct": _share_pct(obs_ec2, ec2_service),
        "source": "packing topology.pool_cost_mo (observability node pool)",
    }
    if obs_ec2 is None:
        ec2["note"] = ("no packing pool cost available; run the packing "
                       "analysis (needs kubectl) to attribute EC2.")

    # S3 ------------------------------------------------------------------
    s3_service = _latest_total_by_keys(series, _S3_KEYS)
    obs_s3, bucket_rows = _bucket_cost(buckets, assumptions)
    s3 = {
        "obs_monthly_usd": obs_s3,
        "service_total_usd": s3_service,
        "share_pct": _share_pct(obs_s3, s3_service),
        "buckets": bucket_rows,
        "source": "mimir/loki/tempo object-store buckets",
    }
    if obs_s3 is None:
        s3["note"] = ("no bucket cost/size supplied; pass buckets= "
                      "(names, sizes, or per-bucket $) to attribute S3.")

    # Data transfer / TGW / NAT ------------------------------------------
    net = _deepdive_network(deepdive)
    wire_gb = _r(_num(net.get("wire_gb_month")))
    by_path = net.get("wire_cost_by_path_mo") or {}
    path_vals = [_num(v) for v in by_path.values()]
    point = _r(_median(path_vals)) if path_vals else None
    low = _r(min(path_vals)) if path_vals else None
    high = _r(max(path_vals)) if path_vals else None
    xfer_service = _latest_total_by_keys(series, _XFER_KEYS)
    data_transfer = {
        "wire_gb_month": wire_gb,
        "cost_by_path_mo": {k: _r(_num(v)) for k, v in by_path.items()},
        "point_usd": point,
        "low_usd": low,
        "high_usd": high,
        "service_total_usd": xfer_service,
        "share_pct": _share_pct(point, xfer_service),
        "source": "deep-dive remote_write wire GB/month x per-path egress",
    }
    if point is not None:
        assumptions.append(
            "Data-transfer obs cost uses the deep-dive's measured "
            "remote_write wire volume priced across egress paths "
            "(cross-AZ/TGW/NAT); the point figure is the median of those "
            "path estimates -- the true cost depends on your actual "
            "topology.")
    else:
        data_transfer["note"] = ("no deep-dive network section; run the "
                                  "deep-dive to attribute data transfer.")

    # Totals --------------------------------------------------------------
    parts = [p for p in (obs_ec2, obs_s3, point) if p is not None]
    total_obs = _r(sum(parts)) if parts else None

    return {
        "ec2": ec2,
        "s3": s3,
        "data_transfer": data_transfer,
        "total_obs_monthly_usd": total_obs,
        "total_spend_latest_usd": latest_total,
        "obs_share_pct": _share_pct(total_obs, latest_total),
        "currency": series.get("currency", "USD"),
        "assumptions": assumptions,
        "note": ("Estimated observability share of spend -- not a tagged "
                 "cost allocation."),
    }


# ---------------------------------------------------------------------------
# change correlation
# ---------------------------------------------------------------------------

def _event_ts(item: Dict[str, Any]) -> str:
    for key in ("ts", "date", "generated_at", "started_at", "when"):
        val = item.get(key)
        if isinstance(val, str) and len(val) >= 7:
            return val
    return ""


def _event_savings(item: Dict[str, Any]) -> Optional[float]:
    est = item.get("est_savings")
    if isinstance(est, dict) and est.get("monthly_usd") is not None:
        return _r(_num(est.get("monthly_usd")))
    summ = item.get("summary")
    if isinstance(summ, dict):
        for key in ("total_est_monthly_usd", "total_est_savings_usd"):
            if summ.get(key) is not None:
                return _r(_num(summ.get(key)))
    if item.get("est_monthly_usd") is not None:
        return _r(_num(item.get("est_monthly_usd")))
    return None


def _normalize_changes(change_log: Any) -> List[Dict[str, Any]]:
    """Flatten assorted change/artifact inputs into dated events."""
    events: List[Dict[str, Any]] = []
    if change_log is None:
        return events

    # ChangeLog instance -> its JSON report.
    if hasattr(change_log, "report") and callable(
            getattr(change_log, "report")):
        try:
            change_log = change_log.report()
        except Exception:  # noqa: BLE001
            change_log = None
        if change_log is None:
            return events

    raw_items: List[Any] = []
    if isinstance(change_log, dict):
        if isinstance(change_log.get("dashboards"), list):
            for dash in change_log["dashboards"]:
                if isinstance(dash, dict):
                    raw_items.extend(dash.get("changes") or [])
        elif isinstance(change_log.get("changes"), list):
            raw_items.extend(change_log["changes"])
        else:
            raw_items.append(change_log)
    elif isinstance(change_log, (list, tuple)):
        raw_items.extend(change_log)

    for it in raw_items:
        if not isinstance(it, dict):
            continue
        ts = _event_ts(it)
        if not ts:
            continue
        events.append({
            "ts": ts,
            "action": str(it.get("action", it.get("schema", "")) or ""),
            "target": str(it.get("target", "") or ""),
            "why": str(it.get("why", it.get("title", "")) or ""),
            "source": str(it.get("source", "") or ""),
            "est_savings_usd": _event_savings(it),
        })
    events.sort(key=lambda e: e["ts"])
    return events


def correlate_changes(series: Dict[str, Any], change_log: Any,
                      snapshots: Any = None) -> Dict[str, Any]:
    """Align dated optimization actions to the cost movement that followed.

    For each recorded change/optimize/deep-dive event, compares the total
    cost of the change's month to the following month and reports whether
    the bill moved in the expected (down) direction. This is CORRELATION,
    not causation -- a bill moves for many reasons at once.
    """
    series = series if isinstance(series, dict) else {}
    months = series.get("months") or []
    totals = series.get("total") or []
    idx = {m: i for i, m in enumerate(months)}

    events = _normalize_changes(change_log)
    out: List[Dict[str, Any]] = []
    aligned = 0
    counted = 0

    for e in events:
        month = e["ts"][:7]
        rec: Dict[str, Any] = {
            "ts": e["ts"], "month": month, "action": e["action"],
            "target": e["target"], "why": e["why"], "source": e["source"],
            "est_savings_usd": e["est_savings_usd"],
            "expected": "decrease",
        }
        if month not in idx:
            rec["observed"] = "unknown"
            rec["note"] = "no cost data for this month in the series."
            out.append(rec)
            continue
        i = idx[month]
        before = _r(_num(totals[i]))
        rec["cost_this_month"] = before
        if i + 1 < len(totals):
            after = _r(_num(totals[i + 1]))
            delta = _r(after - before)
            eps = max(before * 0.01, 1.0)
            if delta < -eps:
                observed = "decrease"
            elif delta > eps:
                observed = "increase"
            else:
                observed = "flat"
            rec["cost_next_month"] = after
            rec["delta_usd"] = delta
            rec["pct"] = (_r(delta / before * 100.0, 2)
                          if before > 0 else None)
            rec["observed"] = observed
            rec["aligned"] = observed == "decrease"
            counted += 1
            if observed == "decrease":
                aligned += 1
        else:
            rec["observed"] = "pending"
            rec["note"] = "no following month yet to measure the effect."
        out.append(rec)

    return {
        "events": out,
        "summary": {
            "events": len(out),
            "measured": counted,
            "aligned": aligned,
            "aligned_pct": (_r(aligned / counted * 100.0, 1)
                            if counted else None),
        },
        "note": ("Correlation, NOT causation: a change appearing before a "
                 "cost drop does not prove it caused the drop -- many "
                 "factors move a bill in the same window."),
        "assumptions": [
            "Optimization actions are expected to lower cost; alignment "
            "means the following month's total fell.",
            "Effects are measured on the total bill at monthly granularity; "
            "component-level effects may lag or be masked by other spend.",
        ],
    }


# ---------------------------------------------------------------------------
# forecast
# ---------------------------------------------------------------------------

def _linear_forecast(series: Dict[str, Any],
                     months: int) -> Optional[Dict[str, Any]]:
    if not isinstance(series, dict):
        return None
    totals = series.get("total") or []
    labels = series.get("months") or []
    if not totals:
        return None
    slope, intercept, r2 = _linreg(totals)
    n = len(totals)

    last_label = labels[-1] if labels else ""
    try:
        base = datetime.strptime(last_label + "-01", "%Y-%m-%d").date()
        base = _month_start(base)
    except (ValueError, TypeError):
        base = _month_start(_today())

    out = []
    total = 0.0
    for k in range(1, months + 1):
        val = max(0.0, slope * (n - 1 + k) + intercept)
        lbl = _add_months(base, k).isoformat()[:7]
        out.append({"month": lbl, "value": _r(val)})
        total += val
    return {
        "by_month": out,
        "total_usd": _r(total),
        "slope_per_month": _r(slope, 4),
        "r2": _r(r2, 4),
        "basis_months": n,
    }


def _ce_forecast(aws_mod_or_client: Any, months: int, metric: str,
                 profile: str, region: str) -> Dict[str, Any]:
    fn = _aws_fn(aws_mod_or_client, "get_cost_forecast")
    if fn is None:
        return {"available": False,
                "error": "this AWS client exposes no get_cost_forecast()."}
    today = _today()
    start = today.isoformat()
    end = _add_months(_month_start(today), months).isoformat()
    try:
        raw = fn(start, end, metric=metric, granularity="MONTHLY",
                 region=region, profile=profile)
    except Exception as exc:  # noqa: BLE001
        return {"available": False,
                "error": _aws_hint("get cost forecast", exc)}

    if not isinstance(raw, dict):
        return {"available": False,
                "error": "unexpected CE forecast response shape."}

    frbt = raw.get("ForecastResultsByTime") or []
    by_month = []
    for r in frbt:
        if not isinstance(r, dict):
            continue
        tp = r.get("TimePeriod", {}) or {}
        by_month.append({
            "period": str(tp.get("Start", "") or "")[:7],
            "mean": _r(_num(r.get("MeanValue"))),
            "low": _r(_num(r.get("PredictionIntervalLowerBound"))),
            "high": _r(_num(r.get("PredictionIntervalUpperBound"))),
        })
    total = _num((raw.get("Total") or {}).get("Amount"))
    return {
        "available": True,
        "total_usd": _r(total),
        "by_month": by_month,
        "metric": metric,
    }


def forecast(aws_mod_or_client: Any, months: int = 3,
             series: Optional[Dict[str, Any]] = None,
             metric: str = "UNBLENDED_COST", profile: str = "",
             region: str = "us-east-1",
             log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Forecast the next ``months``: the AWS CE forecast + our own linear.

    The CE forecast is AWS's model (with prediction intervals); the linear
    piece is a naive least-squares extrapolation of the observed monthly
    total. Never raises -- a failed CE call degrades to
    ``{"available": False, "error": ...}``.
    """
    emit = log or (lambda m: None)
    months = _clamp_int(months, 1, 12, 3)

    if series is None:
        try:
            series = cost_series(aws_mod_or_client, months=6,
                                 profile=profile, region=region, log=log)
        except TcoError as exc:
            emit("tco: linear forecast basis unavailable: %s" % exc)
            series = None

    linear = _linear_forecast(series, months) if series else None
    emit("tco: requesting CE forecast for next %d month(s) ..." % months)
    ce = _ce_forecast(aws_mod_or_client, months, metric, profile, region)

    return {
        "months": months,
        "ce_forecast": ce,
        "linear": linear,
        "currency": (series or {}).get("currency", "USD"),
        "note": ("CE forecast is AWS's own model; the linear projection is "
                 "our naive least-squares extrapolation of the observed "
                 "monthly total. Both are ESTIMATES."),
        "assumptions": [
            "Linear projection assumes the recent monthly trend continues "
            "unchanged; it ignores seasonality and one-off charges.",
        ],
    }


# ---------------------------------------------------------------------------
# anomalies
# ---------------------------------------------------------------------------

def _normalize_anomalies(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = raw.get("Anomalies") or []
    if not isinstance(raw, (list, tuple)):
        return out
    for a in raw:
        if not isinstance(a, dict):
            continue
        impact = a.get("Impact") or {}
        score = a.get("AnomalyScore") or {}
        causes = []
        for rc in (a.get("RootCauses") or []):
            if isinstance(rc, dict):
                causes.append({
                    "service": rc.get("Service", ""),
                    "region": rc.get("Region", ""),
                    "usage_type": rc.get("UsageType", ""),
                })
        out.append({
            "id": a.get("AnomalyId", ""),
            "start": a.get("AnomalyStartDate", ""),
            "end": a.get("AnomalyEndDate", ""),
            "dimension": a.get("DimensionValue", ""),
            "total_impact_usd": _r(_num(impact.get("TotalImpact"))),
            "max_impact_usd": _r(_num(impact.get("MaxImpact"))),
            "actual_spend_usd": _r(_num(impact.get("TotalActualSpend"))),
            "expected_spend_usd": _r(_num(impact.get("TotalExpectedSpend"))),
            "max_score": _r(_num(score.get("MaxScore")), 4),
            "root_causes": causes,
        })
    out.sort(key=lambda x: x.get("total_impact_usd", 0.0), reverse=True)
    return out


def _fetch_anomalies(aws_mod_or_client: Any, months: int, profile: str,
                     region: str,
                     log: Callable[[str], None]) -> List[Dict[str, Any]]:
    fn = _aws_fn(aws_mod_or_client, "get_anomalies")
    if fn is None:
        return []
    start, end = _month_range(months)
    try:
        raw = fn(start, end, region=region, profile=profile)
    except Exception as exc:  # noqa: BLE001
        log("tco: could not fetch anomalies: %s" % str(exc)[:120])
        return []
    return _normalize_anomalies(raw)


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------

def _compact_snapshot(report: Dict[str, Any]) -> Dict[str, Any]:
    total = (report.get("total") or {})
    tr = total.get("trend") or {}
    services = report.get("by_service") or []
    top = [{"service": s.get("service"), "latest": s.get("latest")}
           for s in services[:5]]
    attr = report.get("observability_attribution") or {}
    return {
        "date": (report.get("generated_at") or _utcnow())[:10],
        "generated_at": report.get("generated_at") or _utcnow(),
        "currency": report.get("currency", "USD"),
        "months": report.get("months"),
        "total_latest_usd": tr.get("last"),
        "run_rate_monthly": tr.get("run_rate_monthly"),
        "run_rate_annual": tr.get("run_rate_annual"),
        "direction": tr.get("direction"),
        "obs_share_pct": attr.get("obs_share_pct"),
        "top_services": top,
    }


def snapshot(store: Any, report: Dict[str, Any]) -> None:
    """Persist a compact, dated TCO snapshot into ``store``.

    Snapshots accumulate in one artifact (schema
    "nr2grafana/tco-snapshots/v1"); a same-day snapshot replaces the prior
    one for that day so the history stays one-per-day.
    """
    if store is None or not isinstance(report, dict):
        return
    snap = _compact_snapshot(report)
    try:
        existing = store.get_artifact(SNAP_SLUG, SNAP_KIND)
    except Exception:  # noqa: BLE001
        existing = None
    history: List[Dict[str, Any]] = []
    if isinstance(existing, dict) and isinstance(
            existing.get("snapshots"), list):
        history = [s for s in existing["snapshots"]
                   if isinstance(s, dict) and s.get("date") != snap["date"]]
    history.append(snap)
    history.sort(key=lambda s: s.get("date", ""))
    doc = {"schema": SNAPSHOTS_SCHEMA, "generated_at": _utcnow(),
           "snapshots": history}
    try:
        store.save_artifact(SNAP_SLUG, SNAP_KIND, doc)
    except Exception:  # noqa: BLE001 - persistence is best-effort
        pass


def trend_over_snapshots(store: Any) -> Dict[str, Any]:
    """Diff stored dated TCO snapshots to show the trend over time."""
    empty = {"schema": TREND_SCHEMA, "snapshots": [], "diffs": [],
             "note": "No TCO snapshots recorded yet."}
    if store is None:
        return empty
    try:
        existing = store.get_artifact(SNAP_SLUG, SNAP_KIND)
    except Exception:  # noqa: BLE001
        existing = None
    if not isinstance(existing, dict):
        return empty
    snaps = existing.get("snapshots")
    if not isinstance(snaps, list) or not snaps:
        return empty
    snaps = sorted((s for s in snaps if isinstance(s, dict)),
                   key=lambda s: s.get("date", ""))

    diffs = []
    for i in range(1, len(snaps)):
        prev = snaps[i - 1]
        cur = snaps[i]
        pr = _num(prev.get("run_rate_monthly"))
        cr = _num(cur.get("run_rate_monthly"))
        diffs.append({
            "from": prev.get("date"),
            "to": cur.get("date"),
            "run_rate_delta_usd": _r(cr - pr),
            "pct": (_r((cr - pr) / pr * 100.0, 2) if pr > 0 else None),
        })

    first = snaps[0]
    last = snaps[-1]
    fr = _num(first.get("run_rate_monthly"))
    lr = _num(last.get("run_rate_monthly"))
    return {
        "schema": TREND_SCHEMA,
        "generated_at": _utcnow(),
        "snapshots": snaps,
        "diffs": diffs,
        "overall": {
            "from": first.get("date"),
            "to": last.get("date"),
            "run_rate_delta_usd": _r(lr - fr),
            "pct": (_r((lr - fr) / fr * 100.0, 2) if fr > 0 else None),
        },
        "note": ("Run-rate diffs across dated snapshots -- an ESTIMATE from "
                 "your CE data at each snapshot time."),
    }


# ---------------------------------------------------------------------------
# recommendations + analyze
# ---------------------------------------------------------------------------

def _recommendations(total_trend: Dict[str, Any], attr: Dict[str, Any],
                     deepdive: Any, packing: Any,
                     corr: Dict[str, Any]) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    direction = total_trend.get("direction", "flat")
    last = _num(total_trend.get("last"))
    proj = (total_trend.get("projection") or {}).get("next_month")

    dd_save = 0.0
    if isinstance(deepdive, dict):
        dd_save = _num((deepdive.get("summary") or {}).get(
            "total_est_monthly_usd"))
    pk_save = 0.0
    if isinstance(packing, dict):
        pk_save = _num((packing.get("est_savings") or {}).get("monthly_usd"))
    total_save = _r(dd_save + pk_save)

    if direction == "up":
        title = ("Total spend is trending UP (latest $%s/mo, projected "
                 "$%s next month)." % (_r(last), _r(_num(proj))))
        detail = ("Costs are rising. ")
        if total_save > 0:
            detail += ("The deep-dive/packing analyses estimate about "
                       "$%s/mo is safely saveable -- acting on those "
                       "recommendations could offset the trend." % total_save)
        else:
            detail += ("Run the deep-dive and packing analyses to find "
                       "safe savings.")
        recs.append({"id": "trend-up", "severity": "high", "title": title,
                     "detail": detail, "est_monthly_usd": total_save})
    elif direction == "down":
        recs.append({
            "id": "trend-down", "severity": "info",
            "title": "Total spend is trending DOWN.",
            "detail": ("Costs are falling; keep verifying it is not from "
                       "dropped/failed ingestion (check the deep-dive "
                       "network findings)."),
        })

    obs_share = attr.get("obs_share_pct")
    if obs_share is not None and _num(obs_share) >= 20.0:
        recs.append({
            "id": "obs-share-high", "severity": "medium",
            "title": ("Observability is ~%.0f%% of estimated spend."
                      % _num(obs_share)),
            "detail": ("The LGTM stack is a material share of the bill; "
                       "prioritize the series-elimination and packing "
                       "levers, which scale down EC2, S3 and transfer "
                       "together. All figures are estimates."),
        })

    if total_save > 0:
        recs.append({
            "id": "act-on-savings", "severity": "medium",
            "title": ("~$%s/mo estimated saveable without cutting "
                      "durability/availability/performance." % total_save),
            "detail": ("From the deep-dive ($%s) and packing ($%s) "
                       "analyses. These are the safe levers; verify each "
                       "against your dashboards before applying."
                       % (_r(dd_save), _r(pk_save))),
            "est_monthly_usd": total_save,
        })

    summ = corr.get("summary") or {}
    if summ.get("measured") and summ.get("aligned"):
        recs.append({
            "id": "changes-aligned", "severity": "info",
            "title": ("%d of %d recorded changes were followed by a cost "
                      "decrease." % (summ.get("aligned"),
                                     summ.get("measured"))),
            "detail": ("Correlation only -- but the optimization actions "
                       "line up with lower bills. Keep recording changes "
                       "to strengthen the signal."),
        })

    if not recs:
        recs.append({
            "id": "insufficient-data", "severity": "info",
            "title": "Not enough cost history for strong recommendations.",
            "detail": ("Collect more months of CE data and run the "
                       "deep-dive/packing analyses to unlock savings "
                       "recommendations."),
        })
    return recs


def analyze(aws_mod_or_client: Any, store: Any = None, deepdive: Any = None,
            traffic: Any = None, packing: Any = None, change_log: Any = None,
            months: int = 6, buckets: Any = None, group_by: str = "SERVICE",
            profile: str = "", region: str = "us-east-1",
            log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Full TCO analysis -> schema "nr2grafana/tco/v1".

    Builds the cost series, trends, observability attribution, forecast,
    anomalies, change correlation and recommendations from the user's own
    Cost Explorer data. Never raises: a missing/broken AWS client yields a
    partial report with actionable notes. When ``store`` is given, a dated
    snapshot is persisted.
    """
    emit = log or (lambda m: None)
    months = _clamp_int(months, 1, 24, 6)
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    series: Optional[Dict[str, Any]] = None
    series_error = ""
    try:
        series = cost_series(aws_mod_or_client, months=months,
                             group_by=group_by, profile=profile,
                             region=region, log=log)
    except TcoError as exc:
        series_error = str(exc)
        emit("tco: %s" % series_error)

    eff_series = series if series is not None else _empty_series(group_by)
    tr = trends(eff_series)

    # by-service breakdown (top by latest month).
    by_service: List[Dict[str, Any]] = []
    groups = eff_series.get("groups") or {}
    order = eff_series.get("group_order") or list(groups.keys())
    latest_by = {}
    for name in order:
        vals = groups.get(name) or []
        latest_by[name] = _num(vals[-1]) if vals else 0.0
    ranked = sorted(order, key=lambda n: latest_by[n], reverse=True)
    grand_latest = sum(latest_by.values())
    for name in ranked[:15]:
        vals = groups.get(name) or []
        latest = _r(latest_by[name])
        by_service.append({
            "service": name,
            "series": [[eff_series["months"][i], _r(_num(vals[i]))]
                       for i in range(len(vals))],
            "latest": latest,
            "share_pct": (_r(latest / grand_latest * 100.0, 2)
                          if grand_latest > 0 else None),
            "trend": tr["by_group"].get(name, {}),
        })

    attr = attribute_observability(eff_series, deepdive=deepdive,
                                   traffic=traffic, packing=packing,
                                   buckets=buckets)
    corr = correlate_changes(eff_series, change_log)
    fc = forecast(aws_mod_or_client, months=3, series=series,
                  profile=profile, region=region, log=log)
    anomalies = _fetch_anomalies(aws_mod_or_client, months, profile,
                                 region, emit)
    recs = _recommendations(tr["total"], attr, deepdive, packing, corr)

    total_series = [[eff_series["months"][i], _r(_num(eff_series["total"][i]))]
                    for i in range(len(eff_series.get("total") or []))]

    assumptions = [
        "All figures are ESTIMATES derived from your own AWS Cost Explorer "
        "data; they are not a bill.",
        "Monthly granularity; the current month is partial and CE flags it "
        "as estimated.",
    ]
    assumptions.extend(attr.get("assumptions") or [])

    report = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "generated_by": GENERATED_BY,
        "currency": eff_series.get("currency", "USD"),
        "months": months,
        "available": series is not None,
        "range": eff_series.get("range", {}),
        "total": {
            "series": total_series,
            "trend": tr["total"],
            "forecast": fc,
        },
        "by_service": by_service,
        "observability_attribution": attr,
        "anomalies": anomalies,
        "change_correlation": corr,
        "recommendations": recs,
        "assumptions": assumptions,
        "disclaimer": ("Estimated from your Cost Explorer data. Cost "
                       "correlation is correlation, not proof of "
                       "causation."),
    }
    if series_error:
        report["note"] = series_error

    if store is not None:
        snapshot(store, report)

    emit("tco: analysis complete (%d service line(s), %d anomaly(ies))"
         % (len(by_service), len(anomalies)))
    return report
