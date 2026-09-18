"""Reliability guardrail library for cost mitigations (stdlib only).

Every cost mitigation nr2grafana proposes is checked against this
library BEFORE it is offered. The whole point is a hard bias toward
safety: no cost cut may reduce availability, durability, performance,
or the ability to serve the CURRENT traffic rate without being flagged
loudly. The tool only PROPOSES changes -- it never executes AWS/K8s
mutations -- so this module never touches AWS; it reasons over the
mitigation the planner built and the read-only context discovery
collected.

The single public entry point is :func:`check`::

    check(mitigation, context) -> {
        "safe": bool,
        "violations": [ {"rule", "message", "impacts"} , ... ],
        "required_preconditions": [ str, ... ],
    }

``safe`` is ``True`` only when there are no violations. ``violations``
each name the rule, a human message, and which reliability dimensions
they harm (``availability`` / ``durability`` / ``performance``) so the
caller can flip the matching ``keeps_*`` flag to false and demote the
mitigation. ``required_preconditions`` are the assumptions that MUST
hold for the change to be safe -- the caller surfaces them verbatim as
the mitigation's reliability guardrails.

The rules encode the researched domain facts from ARCHITECTURE-1.9
(sections 0.2-0.6): RF/quorum math, zone-aware ring migration safety,
per-AZ NLB target health (never blind-disable cross-zone), the
PreferClose overload hazard, PodDisruptionBudgets / Karpenter
consolidation budgets, never CPU-limit ingesters, keep RF/retention,
and keep enough per-zone headroom to serve current traffic.

The function NEVER raises: a malformed mitigation degrades to an
unsafe result carrying an explanatory violation rather than a
traceback.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# --- Reliability dimensions a violation can harm. ---
AVAILABILITY = "availability"
DURABILITY = "durability"
PERFORMANCE = "performance"

# --- Canonical mitigation kinds this library understands. ---
KIND_MIMIR_ZONE_AWARE = "mimir_zone_aware"
KIND_LOKI_ZONE_AWARE = "loki_zone_aware"
KIND_PREFERCLOSE = "trafficdistribution_preferclose"
KIND_KARPENTER_SUBNETS = "karpenter_multiaz_subnets"
KIND_NLB_DISABLE_CROSS_ZONE = "nlb_disable_cross_zone"
KIND_UNKNOWN = "unknown"

KNOWN_KINDS = (
    KIND_MIMIR_ZONE_AWARE,
    KIND_LOKI_ZONE_AWARE,
    KIND_PREFERCLOSE,
    KIND_KARPENTER_SUBNETS,
    KIND_NLB_DISABLE_CROSS_ZONE,
)

# Default Mimir/Loki replication factor when none is supplied (0.2/0.3).
_DEFAULT_RF = 3


# --------------------------------------------------------------------------
# small numeric helpers (tolerant of strings / None / junk)
# --------------------------------------------------------------------------
def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Best-effort float; returns ``default`` on anything non-numeric."""
    if isinstance(value, bool):
        # bool is an int subclass; treat as not-a-number here.
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def quorum(rf: int) -> int:
    """Healthy zones needed to serve at replication factor ``rf``.

    ``floor(RF/2)+1`` -- RF=3 tolerates one zone down (needs 2). See
    ARCHITECTURE-1.9 0.2.
    """
    try:
        r = int(rf)
    except (TypeError, ValueError):
        return 0
    if r < 1:
        return 0
    return r // 2 + 1


def _as_list(value: Any) -> List[Any]:
    """Coerce to a list; a bare string becomes a single-item list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


# --------------------------------------------------------------------------
# result accumulator
# --------------------------------------------------------------------------
class _Result(object):
    """Collects violations and preconditions, de-duping preconditions."""

    def __init__(self):
        self.violations = []  # type: List[Dict[str, Any]]
        self._pre = []        # type: List[str]
        self._seen = set()    # type: set

    def violate(self, rule, message, impacts):
        # type: (str, str, List[str]) -> None
        self.violations.append({
            "rule": rule,
            "message": message,
            "impacts": list(impacts),
        })

    def require(self, text):
        # type: (str) -> None
        if text and text not in self._seen:
            self._seen.add(text)
            self._pre.append(text)

    def finish(self, kind):
        # type: (str) -> Dict[str, Any]
        return {
            "kind": kind,
            "safe": not self.violations,
            "violations": self.violations,
            "required_preconditions": self._pre,
        }


# --------------------------------------------------------------------------
# kind normalization (tolerant of naming drift across modules)
# --------------------------------------------------------------------------
def normalize_kind(mitigation):
    # type: (Any) -> str
    """Map a mitigation to one of :data:`KNOWN_KINDS` or ``unknown``.

    Uses the explicit ``kind`` field when present, else falls back to the
    ``id``/``title``/``type`` text, matched by substring so a slightly
    different label from the planner still routes to the right rules.
    """
    if not isinstance(mitigation, dict):
        return KIND_UNKNOWN
    raw = ""
    for key in ("kind", "id", "type", "title", "name"):
        val = mitigation.get(key)
        if isinstance(val, str) and val.strip():
            raw += " " + val.lower()
    if not raw:
        return KIND_UNKNOWN
    # order matters: check the most specific tokens first.
    if "mimir" in raw:
        return KIND_MIMIR_ZONE_AWARE
    if "loki" in raw:
        return KIND_LOKI_ZONE_AWARE
    if "nlb" in raw or "cross_zone" in raw or "cross-zone" in raw \
            or "crosszone" in raw:
        return KIND_NLB_DISABLE_CROSS_ZONE
    if "prefer" in raw or "topology" in raw \
            or "trafficdistribution" in raw or "traffic_distribution" in raw:
        return KIND_PREFERCLOSE
    if "karpenter" in raw or "subnet" in raw or "nodepool" in raw \
            or "nodeclass" in raw:
        return KIND_KARPENTER_SUBNETS
    return KIND_UNKNOWN


# --------------------------------------------------------------------------
# individual rules -- each mutates the shared _Result
# --------------------------------------------------------------------------
def _rule_rf_retention(m, ctx, res):
    # type: (Dict, Dict, _Result) -> None
    """QUORUM/RF math and keep-RF / keep-retention (0.2/0.3).

    zones after the change must be >= RF; healthy zones must be >=
    ``floor(RF/2)+1``; the mitigation must not reduce RF or retention.
    """
    rf_before = _num(ctx.get("replication_factor"))
    rf_after = _num(m.get("replication_factor"))
    rf = rf_after if rf_after is not None else rf_before
    if rf is None:
        rf = float(_DEFAULT_RF)
    rf_i = int(rf)

    zones = _as_list(m.get("zones")) or _as_list(ctx.get("available_zones"))
    nzones = len([z for z in zones if z])

    res.require(
        "deploy across >= RF zones (RF=%d needs >= %d zones) and keep "
        ">= floor(RF/2)+1 = %d zones healthy" % (rf_i, rf_i, quorum(rf_i)))
    res.require(
        "do not change the replication factor (keep RF=%d) or the "
        "retention window" % rf_i)

    if nzones and nzones < rf_i:
        res.violate(
            "rf_quorum",
            "deploying to fewer zones (%d) than the replication factor "
            "(%d) can miss or fail writes -- data-loss risk"
            % (nzones, rf_i),
            [DURABILITY, AVAILABILITY])

    healthy = _num(ctx.get("healthy_zones"))
    if healthy is None and nzones:
        healthy = float(nzones)
    if healthy is not None and int(healthy) < quorum(rf_i):
        res.violate(
            "rf_quorum",
            "only %d healthy zone(s); RF=%d needs a quorum of %d healthy "
            "zones to serve" % (int(healthy), rf_i, quorum(rf_i)),
            [AVAILABILITY])

    if rf_before is not None and rf_after is not None \
            and rf_after < rf_before:
        res.violate(
            "keep_rf",
            "mitigation reduces the replication factor from %d to %d"
            % (int(rf_before), int(rf_after)),
            [DURABILITY])

    ret_before = _num(ctx.get("retention_days"))
    ret_after = _num(m.get("retention_days"))
    if ret_before is not None and ret_after is not None \
            and ret_after < ret_before:
        res.violate(
            "keep_retention",
            "mitigation reduces retention from %d to %d days"
            % (int(ret_before), int(ret_after)),
            [DURABILITY])


def _rule_zone_migration(m, ctx, res):
    # type: (Dict, Dict, _Result) -> None
    """Live zone-aware ring migration safety (0.2/0.3).

    The rollout must touch ONE zone at a time; the existing ingester PDB
    must be ``maxUnavailable:0`` during the reshuffle; series/stream
    limits must be doubled first; the write path must be enabled and the
    ``query_ingesters_within`` / ``query_store_after`` wait observed
    BEFORE the read path (and before decommissioning old ingesters).
    """
    res.require(
        "migrate the live ring one zone at a time via the "
        "rollout-operator (never roll more than one zone concurrently)")
    res.require(
        "set the existing ingester PodDisruptionBudget maxUnavailable:0 "
        "during the reshuffle")
    res.require(
        "double the max-series / max-stream limits BEFORE adding zones "
        "(series briefly double-count across zones)")
    res.require(
        "enable the write path first, wait query_ingesters_within "
        "(~3h) / query_store_after (~12h), THEN enable the read path and "
        "decommission old ingesters")

    mig = m.get("migration")
    if not isinstance(mig, dict):
        # Nothing asserted; the preconditions above are the must-hold set.
        return
    if mig.get("one_zone_at_a_time") is False:
        res.violate(
            "zone_migration",
            "rolling more than one zone at a time can drop the quorum "
            "mid-migration",
            [AVAILABILITY, DURABILITY])
    if "existing_ingester_pdb_max_unavailable" in mig:
        val = _num(mig.get("existing_ingester_pdb_max_unavailable"))
        if val is not None and int(val) != 0:
            res.violate(
                "zone_migration",
                "existing ingester PDB maxUnavailable must be 0 during "
                "the reshuffle (got %d)" % int(val),
                [AVAILABILITY, DURABILITY])
    if mig.get("limits_doubled") is False:
        res.violate(
            "zone_migration",
            "max-series/stream limits were not doubled first; zone "
            "distribution multiplies streams and will hit the limit",
            [AVAILABILITY])
    if mig.get("write_path_before_read_path") is False:
        res.violate(
            "zone_migration",
            "read path enabled before the write path backfilled -- "
            "queries can miss recent data",
            [DURABILITY])
    if mig.get("wait_observed") is False:
        res.violate(
            "zone_migration",
            "old ingesters touched before the query_ingesters_within / "
            "query_store_after wait -- risks losing recent samples",
            [DURABILITY])


def _rule_no_cpu_limit_ingesters(m, ctx, res):
    # type: (Dict, Dict, _Result) -> None
    """Never set CPU limits on ingesters -- throttling breaks ingest."""
    res.require(
        "keep ingester CPU as requests only (no CPU limit); CPU "
        "throttling stalls ingestion and replication")
    if m.get("cpu_limit_ingesters") is True or m.get("cpu_limit") is True:
        res.violate(
            "no_cpu_limit_ingesters",
            "mitigation sets a CPU limit on ingesters; throttling breaks "
            "ingest and replication",
            [PERFORMANCE, DURABILITY])


def _rule_pdb(m, ctx, res):
    # type: (Dict, Dict, _Result) -> None
    """A PodDisruptionBudget must protect any StatefulSet being rolled."""
    res.require(
        "a PodDisruptionBudget must guard every rolled StatefulSet so "
        "voluntary disruption cannot break quorum")
    if m.get("pdb_present") is False:
        res.violate(
            "pdb",
            "no PodDisruptionBudget for the StatefulSet being rolled; a "
            "rollout can take ingesters below quorum",
            [AVAILABILITY, DURABILITY])


def _rule_preferclose(m, ctx, res):
    # type: (Dict, Dict, _Result) -> None
    """PreferClose has NO overload safeguard (0.4).

    Only safe when endpoints are spread across ALL serving zones and each
    zone has enough replicas to carry its current per-zone traffic; an
    imbalance hotspots the zone with few replicas.
    """
    res.require(
        "PreferClose has no overload safeguard: only enable it when "
        "endpoints are spread across ALL serving zones with >= the "
        "replicas per zone needed to carry current per-zone traffic")

    serving = [z for z in (
        _as_list(ctx.get("serving_zones"))
        or list((ctx.get("per_zone_current_rate") or {}).keys())
        or _as_list(ctx.get("available_zones"))) if z]
    replicas = ctx.get("per_zone_replicas")
    endpoint_zones = [z for z in (
        _as_list(ctx.get("endpoint_zones"))
        or (list(replicas.keys()) if isinstance(replicas, dict) else []))
        if z]

    if serving and endpoint_zones:
        missing = [z for z in serving if z not in endpoint_zones]
        for z in missing:
            res.violate(
                "preferclose_overload",
                "zone %s serves traffic but has no local endpoints; "
                "PreferClose will hotspot / drop same-zone traffic" % z,
                [AVAILABILITY, PERFORMANCE])

    required = ctx.get("per_zone_required_replicas")
    if isinstance(replicas, dict) and isinstance(required, dict):
        for z, need in required.items():
            have = _num(replicas.get(z), 0.0) or 0.0
            need_n = _num(need, 0.0) or 0.0
            if have < need_n:
                res.violate(
                    "preferclose_overload",
                    "zone %s has %d replica(s) but needs %d to carry its "
                    "current same-zone traffic; PreferClose would hotspot "
                    "it" % (z, int(have), int(need_n)),
                    [PERFORMANCE, AVAILABILITY])


def _rule_nlb_health(m, ctx, res):
    # type: (Dict, Dict, _Result) -> None
    """Never blind-disable NLB cross-zone (0.6).

    With cross-zone OFF each NLB node serves only same-AZ targets, so an
    AZ with zero or a single healthy target black-holes clients that
    resolve to its zonal node. Disabling is safe only after confirming
    healthy targets in EVERY enabled AZ and setting both
    ``minimum_healthy_targets.count`` gates (DNS failover +
    unhealthy-state fail-open).
    """
    res.require(
        "before disabling cross-zone, confirm >= 1 (ideally >= 2) "
        "HEALTHY target in EVERY enabled AZ (elbv2 "
        "describe-target-health)")
    res.require(
        "set target_group_health.dns_failover.minimum_healthy_targets."
        "count>=1 and target_group_health.unhealthy_state_routing."
        "minimum_healthy_targets.count>=1 so a thin AZ fails over / "
        "fails open instead of black-holing")
    res.require(
        "lower-risk alternative: keep cross-zone ON and set "
        "dns_record.client_routing_policy=availability_zone_affinity")

    gates = _gates_set(m)
    health = ctx.get("nlb_target_health")
    enabled = [z for z in _as_list(ctx.get("nlb_enabled_azs")) if z]
    if isinstance(health, dict) and not enabled:
        enabled = [z for z in health.keys() if z]

    thin = False
    for az in enabled:
        info = health.get(az) if isinstance(health, dict) else None
        healthy = None
        if isinstance(info, dict):
            healthy = _num(info.get("healthy"))
        elif info is not None:
            healthy = _num(info)
        if healthy is None:
            # Unknown health for an enabled AZ -> must verify first.
            res.require(
                "verify healthy target count for enabled AZ %s before "
                "disabling cross-zone" % az)
            thin = True
            continue
        if healthy < 1:
            res.violate(
                "nlb_target_health",
                "AZ %s has no healthy target; disabling cross-zone "
                "black-holes clients resolving to its zonal node" % az,
                [AVAILABILITY])
        elif healthy < 2:
            thin = True
            res.require(
                "AZ %s has a single healthy target; add a second (or "
                "keep cross-zone) so one failure cannot black-hole it"
                % az)

    if not gates:
        res.violate(
            "nlb_target_health",
            "disabling cross-zone without the dns_failover / "
            "unhealthy_state_routing minimum_healthy_targets gates "
            "black-holes any thin AZ",
            [AVAILABILITY])
    elif thin:
        # Gates set but an AZ is thin: fail-open covers it, so this is
        # not a hard violation -- the precondition above records it.
        pass


def _gates_set(m):
    # type: (Dict) -> bool
    """True when both NLB minimum_healthy_targets gates are configured."""
    if m.get("health_gates_set") is True:
        return True
    dns = _num(m.get("dns_failover_min_healthy"))
    unh = _num(m.get("unhealthy_state_routing_min_healthy"))
    return dns is not None and dns >= 1 and unh is not None and unh >= 1


def _rule_karpenter(m, ctx, res):
    # type: (Dict, Dict, _Result) -> None
    """Karpenter multi-AZ discovery + rate-limited disruption (0.5).

    Discovery subnets must resolve in >= RF AZs or the ring collapses
    into fewer zones (the imbalance root cause); voluntary disruption
    must be rate-limited by consolidation budgets and stateful workloads
    protected by PDBs.
    """
    rf = _num(ctx.get("replication_factor"))
    rf_i = int(rf) if rf is not None else _DEFAULT_RF

    res.require(
        "tag a discovery subnet in EACH of >= RF (%d) AZs so Karpenter "
        "can balance the ring across zones (a subnet gap forces the ring "
        "into fewer AZs)" % rf_i)
    res.require(
        "set Karpenter disruption budgets (e.g. nodes: \"10%%\") to "
        "rate-limit voluntary node churn")
    res.require(
        "keep PodDisruptionBudgets on stateful workloads so "
        "consolidation cannot drop quorum")

    azs = [z for z in (
        _as_list(m.get("discovery_subnet_azs"))
        or _as_list(m.get("zones"))) if z]
    if azs and len(azs) < rf_i:
        res.violate(
            "karpenter_subnets",
            "discovery subnets resolve in only %d AZ(s) but RF=%d; the "
            "ring collapses into fewer zones and every replica crosses a "
            "zone boundary" % (len(azs), rf_i),
            [DURABILITY, AVAILABILITY])

    if m.get("consolidation_budgets") is False:
        res.violate(
            "karpenter_budgets",
            "no Karpenter disruption budgets; unbounded consolidation "
            "can churn too many nodes at once and drop quorum",
            [AVAILABILITY])
    if m.get("pdb_present") is False:
        res.violate(
            "pdb",
            "no PodDisruptionBudget on stateful workloads; consolidation "
            "can take ingesters below quorum",
            [AVAILABILITY, DURABILITY])


def _rule_current_traffic(m, ctx, res):
    # type: (Dict, Dict, _Result) -> None
    """Post-change per-zone capacity must still serve current traffic."""
    res.require(
        "verify post-change per-zone capacity >= the current per-zone "
        "request / ingest rate so the change still serves current "
        "traffic")

    rate = ctx.get("per_zone_current_rate")
    cap = ctx.get("per_zone_capacity_after")
    if isinstance(rate, dict) and isinstance(cap, dict):
        for z, r in rate.items():
            need = _num(r)
            have = _num(cap.get(z))
            if need is None:
                continue
            if have is not None and have < need:
                res.violate(
                    "handles_current_traffic",
                    "zone %s post-change capacity (%g) is below its "
                    "current traffic rate (%g)" % (z, have, need),
                    [PERFORMANCE])


def _rule_unknown(m, ctx, res):
    # type: (Dict, Dict, _Result) -> None
    """Fail closed on a mitigation we have no rule for."""
    res.violate(
        "unknown_mitigation",
        "no reliability rule exists for this mitigation kind; it cannot "
        "be certified safe automatically -- review it manually",
        [AVAILABILITY, DURABILITY, PERFORMANCE])
    res.require(
        "add a reliability rule for this mitigation kind, or review its "
        "availability / durability / performance impact by hand")


# Which rules run for each kind. Order controls precondition ordering.
_RULES_BY_KIND = {
    KIND_MIMIR_ZONE_AWARE: (
        _rule_rf_retention, _rule_zone_migration,
        _rule_no_cpu_limit_ingesters, _rule_pdb, _rule_current_traffic),
    KIND_LOKI_ZONE_AWARE: (
        _rule_rf_retention, _rule_zone_migration,
        _rule_no_cpu_limit_ingesters, _rule_pdb, _rule_current_traffic),
    KIND_PREFERCLOSE: (
        _rule_preferclose, _rule_current_traffic),
    KIND_KARPENTER_SUBNETS: (
        _rule_karpenter, _rule_current_traffic),
    KIND_NLB_DISABLE_CROSS_ZONE: (
        _rule_nlb_health, _rule_current_traffic),
}


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def check(mitigation, context=None):
    # type: (Any, Optional[Dict[str, Any]]) -> Dict[str, Any]
    """Check ``mitigation`` against the reliability guardrails.

    Returns ``{"kind", "safe", "violations", "required_preconditions"}``.
    ``safe`` is ``True`` only when no violation fired. Never raises: a
    malformed mitigation yields an unsafe result with an explanatory
    violation.
    """
    res = _Result()
    try:
        ctx = context if isinstance(context, dict) else {}
        if not isinstance(mitigation, dict):
            res.violate(
                "invalid_mitigation",
                "mitigation is not a mapping; cannot verify reliability",
                [AVAILABILITY, DURABILITY, PERFORMANCE])
            return res.finish(KIND_UNKNOWN)
        kind = normalize_kind(mitigation)
        rules = _RULES_BY_KIND.get(kind)
        if rules is None:
            _rule_unknown(mitigation, ctx, res)
            _rule_current_traffic(mitigation, ctx, res)
        else:
            for rule in rules:
                rule(mitigation, ctx, res)
        return res.finish(kind)
    except Exception as exc:  # never leak a traceback to the caller
        res.violate(
            "internal_error",
            "reliability check failed internally (%s); treat as unsafe "
            "and review manually" % exc,
            [AVAILABILITY, DURABILITY, PERFORMANCE])
        return res.finish(KIND_UNKNOWN)


def check_all(mitigations, context=None):
    # type: (Any, Optional[Dict[str, Any]]) -> List[Dict[str, Any]]
    """Convenience: run :func:`check` over an iterable of mitigations."""
    out = []  # type: List[Dict[str, Any]]
    for m in _as_list(mitigations):
        out.append(check(m, context))
    return out
