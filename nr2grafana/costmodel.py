"""LGTM-stack cost model: pricing assumptions + TCO / savings math.

Pure functions, no network and no I/O. Given a traffic sample
("nr2grafana/traffic/v1", produced by :mod:`nr2grafana.traffic`) and a
set of user-editable pricing assumptions, estimate the current monthly
cost of each datasource component (Loki / Mimir / Tempo) and the whole
stack, plus rough resource footprints. :func:`apply_savings` then rolls
up a list of optimizer recommendations into a projected cost.

Everything here is an ESTIMATE based on the caller's pricing inputs --
it is never a real bill. The defaults in :data:`DEFAULT_PRICING` are
clearly-labeled placeholders; users are expected to override them with
their own numbers (their cloud egress, object-store rates, per-tenant
contract, etc.).

Output schema: "nr2grafana/cost/v1".
"""

from __future__ import annotations

import copy
import time
from typing import Any, Dict, List, Optional

SCHEMA = "nr2grafana/cost/v1"
GENERATED_BY = "nr2grafana 1.5.0"

# Decimal gigabyte (10**9 bytes). Cloud storage / egress is billed in
# decimal GB, so we use 1e9 rather than 2**30 throughout.
BYTES_PER_GB = 1_000_000_000.0

# Average days in a month. Monthly figures scale a per-day rate by this.
DAYS_PER_MONTH = 30.44


# ---------------------------------------------------------------------------
# Pricing assumptions
# ---------------------------------------------------------------------------
# IMPORTANT: every number below is an ASSUMPTION / placeholder, NOT a
# quoted price. Real cost depends entirely on your deployment (self-hosted
# vs Grafana Cloud, object-store rates, compression, replication factor,
# per-tenant contract). Override any subset via the ``pricing`` argument
# to :func:`estimate_costs`; unspecified keys fall back to these defaults.
#
# Units:
#   *_usd_per_gb            -- dollars per decimal GB (10**9 bytes)
#   *_usd_per_gb_month      -- dollars per GB kept in storage for one month
#   usd_per_1k_series_month -- dollars per 1000 active series per month
#   retention_days          -- how long ingested data is kept (drives the
#                              steady-state stored volume)
#   bytes_per_series_day    -- rough on-disk bytes a single active series
#                              accretes per day (samples * scrape rate,
#                              compressed) -- used only for the storage
#                              estimate, not for the series cost
#   ram_bytes_per_series    -- rough Mimir/Prometheus ingester RAM per
#                              active (in-memory) series
DEFAULT_PRICING: Dict[str, Any] = {
    # Loki: cost scales with bytes ingested and bytes retained.
    "loki": {
        "ingest_usd_per_gb": 0.50,        # ASSUMPTION: $/GB ingested
        "store_usd_per_gb_month": 0.03,   # ASSUMPTION: object-store $/GB-mo
        "retention_days": 30,             # ASSUMPTION
    },
    # Mimir/Prometheus: cost is dominated by ACTIVE SERIES (cardinality).
    "mimir": {
        # ASSUMPTION: ~$0.60 per 1000 active series per month. Clearly a
        # placeholder -- edit to your contract / infra cost.
        "usd_per_1k_series_month": 0.60,
        "store_usd_per_gb_month": 0.03,   # ASSUMPTION: long-term block store
        "bytes_per_series_day": 12_000.0,  # ASSUMPTION: ~12 KB/series/day
        "retention_days": 30,             # ASSUMPTION
    },
    # Tempo: lighter; trace bytes ingested + retained. Best-effort -- the
    # traffic sampler often can't measure trace volume, in which case Tempo
    # cost is reported as 0 with a note.
    "tempo": {
        "ingest_usd_per_gb": 0.50,        # ASSUMPTION: $/GB traces ingested
        "store_usd_per_gb_month": 0.03,   # ASSUMPTION
        "retention_days": 15,             # ASSUMPTION
    },
    # Rough resource heuristics (not billed; surfaced as capacity hints).
    "resources": {
        # ASSUMPTION: a few KB of ingester RAM per active series.
        "mimir_ram_bytes_per_series": 3_000.0,
    },
}


def effective_pricing(pricing: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
    """Return DEFAULT_PRICING deep-merged with the caller's overrides.

    Dicts merge key-wise; any leaf the caller supplies wins. The result
    is a fresh copy -- neither the defaults nor the input is mutated.
    """
    merged = copy.deepcopy(DEFAULT_PRICING)
    if pricing:
        _merge(merged, pricing)
    return merged


def _merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> None:
    """Deep-merge ``overlay`` into ``base`` in place (dicts recurse,
    everything else replaces)."""
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _merge(base[key], val)
        else:
            base[key] = val


# ---------------------------------------------------------------------------
# Small defensive helpers
# ---------------------------------------------------------------------------

def _num(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to a non-negative float, tolerating None / junk."""
    try:
        if value is None:
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:            # NaN guard
        return default
    return out if out > 0.0 else 0.0


def _r(value: float, places: int = 4) -> float:
    """Round for stable, human-friendly output."""
    return round(float(value), places)


# ---------------------------------------------------------------------------
# Per-component cost
# ---------------------------------------------------------------------------

def _loki_component(ds: Dict[str, Any],
                    price: Dict[str, Any]) -> Dict[str, Any]:
    """Estimate monthly cost for one Loki datasource."""
    loki = ds.get("loki") or {}
    bytes_per_day = _num(loki.get("bytes_per_day"))
    streams = _num(loki.get("streams"))

    ingest_per_gb = _num(price.get("ingest_usd_per_gb"))
    store_per_gb_mo = _num(price.get("store_usd_per_gb_month"))
    retention_days = _num(price.get("retention_days"))

    gb_per_day = bytes_per_day / BYTES_PER_GB
    ingest_gb_month = gb_per_day * DAYS_PER_MONTH
    ingest_usd = ingest_gb_month * ingest_per_gb
    # Steady state: ``retention_days`` worth of daily ingest sits in store.
    stored_gb = gb_per_day * retention_days
    storage_usd = stored_gb * store_per_gb_mo
    monthly = ingest_usd + storage_usd

    return {
        "family": "loki",
        "uid": ds.get("uid", ""),
        "drivers": {
            "bytes_per_day": _r(bytes_per_day, 2),
            "gb_per_day": _r(gb_per_day),
            "streams": int(streams),
        },
        "monthly_cost": _r(monthly),
        "breakdown": {
            "ingest_usd": _r(ingest_usd),
            "storage_usd": _r(storage_usd),
            "ingest_gb_month": _r(ingest_gb_month),
            "stored_gb": _r(stored_gb),
        },
        "resources": {"storage_gb_month": _r(stored_gb)},
    }


def _mimir_component(ds: Dict[str, Any],
                     price: Dict[str, Any],
                     res_price: Dict[str, Any]) -> Dict[str, Any]:
    """Estimate monthly cost for one Mimir/Prometheus datasource."""
    prom = ds.get("prometheus") or {}
    active_series = _num(prom.get("active_series"))
    histogram_series = _num(prom.get("histogram_series"))

    per_1k = _num(price.get("usd_per_1k_series_month"))
    store_per_gb_mo = _num(price.get("store_usd_per_gb_month"))
    bytes_per_series_day = _num(price.get("bytes_per_series_day"))
    retention_days = _num(price.get("retention_days"))
    ram_per_series = _num(res_price.get("mimir_ram_bytes_per_series"))

    series_usd = (active_series / 1000.0) * per_1k
    stored_gb = (active_series * bytes_per_series_day
                 * retention_days) / BYTES_PER_GB
    storage_usd = stored_gb * store_per_gb_mo
    monthly = series_usd + storage_usd
    ram_gb = (active_series * ram_per_series) / BYTES_PER_GB

    return {
        "family": "prometheus",
        "uid": ds.get("uid", ""),
        "drivers": {
            "active_series": int(active_series),
            "histogram_series": int(histogram_series),
        },
        "monthly_cost": _r(monthly),
        "breakdown": {
            "series_usd": _r(series_usd),
            "storage_usd": _r(storage_usd),
            "stored_gb": _r(stored_gb),
        },
        "resources": {
            "mimir_ram_gb": _r(ram_gb),
            "storage_gb_month": _r(stored_gb),
        },
    }


def _tempo_component(ds: Dict[str, Any],
                     price: Dict[str, Any]) -> Dict[str, Any]:
    """Estimate monthly cost for one Tempo datasource (best-effort).

    The traffic sampler usually cannot measure trace bytes, so this is 0
    unless the sample carries a ``bytes_per_day``.
    """
    tempo = ds.get("tempo") or {}
    bytes_per_day = _num(tempo.get("bytes_per_day"))

    ingest_per_gb = _num(price.get("ingest_usd_per_gb"))
    store_per_gb_mo = _num(price.get("store_usd_per_gb_month"))
    retention_days = _num(price.get("retention_days"))

    gb_per_day = bytes_per_day / BYTES_PER_GB
    ingest_gb_month = gb_per_day * DAYS_PER_MONTH
    ingest_usd = ingest_gb_month * ingest_per_gb
    stored_gb = gb_per_day * retention_days
    storage_usd = stored_gb * store_per_gb_mo
    monthly = ingest_usd + storage_usd

    note = "estimated from sampled trace bytes"
    if bytes_per_day <= 0.0:
        note = ("Tempo volume not measured by the traffic sampler; cost "
                "shown as 0 (best-effort)")

    return {
        "family": "tempo",
        "uid": ds.get("uid", ""),
        "drivers": {
            "bytes_per_day": _r(bytes_per_day, 2),
            "gb_per_day": _r(gb_per_day),
        },
        "monthly_cost": _r(monthly),
        "breakdown": {
            "ingest_usd": _r(ingest_usd),
            "storage_usd": _r(storage_usd),
            "ingest_gb_month": _r(ingest_gb_month),
            "stored_gb": _r(stored_gb),
        },
        "resources": {"storage_gb_month": _r(stored_gb)},
        "note": note,
    }


# Map a traffic datasource ``family`` to the pricing-block key it uses.
_PRICE_KEY = {"loki": "loki", "prometheus": "mimir", "tempo": "tempo"}


def estimate_costs(traffic: Optional[Dict[str, Any]] = None,
                   pricing: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    """Estimate current monthly cost of the sampled LGTM stack.

    ``traffic`` is a "nr2grafana/traffic/v1" document (or anything with a
    ``datasources`` list of the same shape). ``pricing`` overrides any
    subset of :data:`DEFAULT_PRICING`. Returns schema "nr2grafana/cost/v1"
    with a per-component breakdown, the stack ``monthly_total``, the
    effective pricing used, and rough ``resources`` estimates.

    Defensive against missing / partial traffic fields; never raises for a
    malformed component. Every figure is an ESTIMATE based on the supplied
    pricing, never an exact bill.
    """
    price = effective_pricing(pricing)
    res_price = price.get("resources") or {}

    datasources = []
    if isinstance(traffic, dict):
        raw = traffic.get("datasources")
        if isinstance(raw, list):
            datasources = raw

    components: List[Dict[str, Any]] = []
    for ds in datasources:
        if not isinstance(ds, dict):
            continue
        family = ds.get("family")
        block_key = _PRICE_KEY.get(family)
        block = price.get(block_key) or {}
        if family == "loki":
            components.append(_loki_component(ds, block))
        elif family == "prometheus":
            components.append(_mimir_component(ds, block, res_price))
        elif family == "tempo":
            components.append(_tempo_component(ds, block))
        # Unknown families are skipped rather than guessed at.

    monthly_total = sum(c["monthly_cost"] for c in components)
    ram_gb = sum(c.get("resources", {}).get("mimir_ram_gb", 0.0)
                 for c in components)
    storage_gb = sum(c.get("resources", {}).get("storage_gb_month", 0.0)
                     for c in components)

    return {
        "schema": SCHEMA,
        "generated_by": GENERATED_BY,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pricing": price,
        "components": components,
        "monthly_total": _r(monthly_total),
        "resources": {
            "mimir_ram_gb_est": _r(ram_gb),
            "storage_gb_month_est": _r(storage_gb),
        },
        "disclaimer": ("Estimated, based on your pricing inputs -- not an "
                       "exact bill."),
    }


# ---------------------------------------------------------------------------
# Savings roll-up
# ---------------------------------------------------------------------------

def _rec_list(recommendations: Any) -> List[Dict[str, Any]]:
    """Accept either a raw list of recs or an optimize document
    ({"recommendations": [...]}) and return the list."""
    if isinstance(recommendations, dict):
        recs = recommendations.get("recommendations")
        if isinstance(recs, list):
            return [r for r in recs if isinstance(r, dict)]
        return []
    if isinstance(recommendations, list):
        return [r for r in recommendations if isinstance(r, dict)]
    return []


def apply_savings(cost: Optional[Dict[str, Any]],
                  recommendations: Any) -> Dict[str, Any]:
    """Roll up recommendation savings against an ``estimate_costs`` result.

    Reads each recommendation's ``est_savings.monthly_usd`` (dollars) and
    the native-unit fields (``series``, ``streams``, ``gb_per_day``),
    grouping by ``family``. Returns projected totals, dollars saved, the
    saved percentage, and a per-component (per-family) breakdown. Savings
    for a family are capped at that family's current estimated cost so a
    component can never project below zero.

    Purely additive and defensive: unknown families, missing est_savings,
    and a None ``cost`` all degrade gracefully.
    """
    cost = cost if isinstance(cost, dict) else {}
    components = cost.get("components")
    if not isinstance(components, list):
        components = []

    # Current cost per family (multiple datasources of a family sum).
    current_by_family: Dict[str, float] = {}
    order: List[str] = []
    for comp in components:
        if not isinstance(comp, dict):
            continue
        fam = comp.get("family", "unknown")
        if fam not in current_by_family:
            current_by_family[fam] = 0.0
            order.append(fam)
        current_by_family[fam] += _num(comp.get("monthly_cost"))

    # Requested savings per family, in dollars and native units.
    saved_usd_by_family: Dict[str, float] = {}
    native_by_family: Dict[str, Dict[str, float]] = {}
    for rec in _rec_list(recommendations):
        fam = rec.get("family", "unknown")
        est = rec.get("est_savings") or {}
        saved_usd_by_family[fam] = (saved_usd_by_family.get(fam, 0.0)
                                    + _num(est.get("monthly_usd")))
        nat = native_by_family.setdefault(
            fam, {"series": 0.0, "streams": 0.0, "gb_per_day": 0.0})
        nat["series"] += _num(est.get("series"))
        nat["streams"] += _num(est.get("streams"))
        nat["gb_per_day"] += _num(est.get("gb_per_day"))
        if fam not in current_by_family:
            # A rec for a family with no measured cost still shows up.
            current_by_family.setdefault(fam, 0.0)
            if fam not in order:
                order.append(fam)

    per_component: List[Dict[str, Any]] = []
    saved_total = 0.0
    for fam in order:
        current = current_by_family.get(fam, 0.0)
        requested = saved_usd_by_family.get(fam, 0.0)
        # Never project a component below zero.
        saved = requested if requested < current else current
        projected = current - saved
        saved_total += saved
        nat = native_by_family.get(fam, {})
        per_component.append({
            "family": fam,
            "current": _r(current),
            "saved": _r(saved),
            "saved_requested": _r(requested),
            "projected": _r(projected),
            "saved_pct": _r((saved / current * 100.0) if current > 0
                            else 0.0, 2),
            "native_savings": {
                "series": int(_num(nat.get("series"))),
                "streams": int(_num(nat.get("streams"))),
                "gb_per_day": _r(_num(nat.get("gb_per_day"))),
            },
        })

    current_total = sum(current_by_family.values())
    projected_total = current_total - saved_total
    saved_pct = (saved_total / current_total * 100.0) if current_total > 0 \
        else 0.0

    native_total = {"series": 0, "streams": 0, "gb_per_day": 0.0}
    for nat in native_by_family.values():
        native_total["series"] += int(_num(nat.get("series")))
        native_total["streams"] += int(_num(nat.get("streams")))
        native_total["gb_per_day"] += _num(nat.get("gb_per_day"))
    native_total["gb_per_day"] = _r(native_total["gb_per_day"])

    return {
        "current_total": _r(current_total),
        "projected_total": _r(projected_total),
        "saved_total": _r(saved_total),
        "saved_pct": _r(saved_pct, 2),
        "per_component": per_component,
        "native_savings": native_total,
        "disclaimer": ("Estimated savings, based on your pricing inputs -- "
                       "not an exact bill."),
    }
