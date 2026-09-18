"""Reliability-safe cost mitigation planner (schema
"nr2grafana/mitigation/v1").

Consumes an RCA result (:mod:`nr2grafana.rca`, schema
"nr2grafana/rca/v1") plus the optional deepdive / packing / capacity
context, and turns the identified cost drivers into a RANKED set of
mitigations. Every mitigation carries:

- the expected percent / $-per-day it saves,
- the concrete change and a GENERIC, paste-ready config (Mimir/Loki
  zone-aware values, a Kubernetes Service ``trafficDistribution:
  PreferClose``, a Karpenter 3-AZ ``NodePool``/``EC2NodeClass``, or the
  gated NLB cross-zone annotation) with NO customer values,
- reliability guardrails (the preconditions that MUST hold or the change
  breaks something), copied from :func:`nr2grafana.reliability.check`,
- the ``keeps_availability`` / ``keeps_durability`` / ``keeps_performance``
  and ``handles_current_traffic`` flags, and
- ``owner`` = GitOps/IaC -- this tool PROPOSES, it NEVER executes.

SAFETY (binding, ARCHITECTURE-1.9 cross-cutting rule 3): no mitigation is
ranked/marked safe if it would reduce availability, durability, performance
or the ability to serve the CURRENT traffic rate. Such a mitigation is
DEMOTED, its ``keeps_*`` flags set false, and a loud caveat attached.
Ranking is by expected $/day saved among the SAFE mitigations first.

The reliability rules live in :mod:`nr2grafana.reliability` (contract
section 5); this module calls :func:`reliability.check` and copies its
``required_preconditions`` verbatim. When that module is not importable a
self-contained fallback that encodes the SAME contract is used, so the
planner is correct standalone.

Zero external deps; stdlib only; nothing here executes AWS/K8s changes or
reads/logs secrets.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

SCHEMA = "nr2grafana/mitigation/v1"
GENERATED_BY = "nr2grafana 1.9.0"

# Reliability dimensions a violation can harm (mirror nr2grafana.reliability).
AVAILABILITY = "availability"
DURABILITY = "durability"
PERFORMANCE = "performance"

DEFAULT_RF = 3
# Two-way cross-AZ transfer price (send $0.01 + receive $0.01); ref 0.1.
USD_PER_TWO_WAY_GB = 0.02

# Default attribution of the dominant cross-AZ share across the primary
# levers. Mimir/Loki zone-aware kill the RF=3 replication writes; PreferClose
# trims query fan-out reads; the Karpenter 3-AZ subnet is a STRUCTURAL
# enabler (0 direct $, it unlocks the others). Overridable via
# cfg["mitigate"]["split"].
DEFAULT_SPLIT = {
    "mimir_zone_aware": 0.55,
    "loki_zone_aware": 0.30,
    "prefer_close": 0.15,
    "karpenter_3az": 0.0,
}

# ---------------------------------------------------------------------------
# GENERIC paste-ready config templates (placeholders in <ANGLE_BRACKETS>; NO
# customer values). Each cites its ARCHITECTURE-1.9 0.x source. Kept at or
# under 79 source columns so the templates copy cleanly.
# ---------------------------------------------------------------------------

_CFG_MIMIR = """\
# Mimir zone-aware replication (write + read path). RF must be <= #zones.
# Migrate the LIVE ring zone-by-zone via rollout-operator; ONE zone at a
# time. Source: ARCHITECTURE-1.9 section 0.2.
mimir:
  structuredConfig:
    ingester:
      ring:
        # -ingester.ring.zone-awareness-enabled
        zone_awareness_enabled: true
        replication_factor: <RF_DEFAULT_3>
    distributor:
      ring: {}
# Flags on the right components (write path = distributors + rulers,
# read path = queriers):
#   -distributor.zone-awareness-enabled=true
#   -ingester.ring.instance-availability-zone=<ZONE_A|ZONE_B|ZONE_C>
rollout_operator:
  enabled: true            # one zone at a time
ingester:
  zoneAwareReplication:
    enabled: true
  # zones -> your 3 AZs; DEPLOY ACROSS >= RF ZONES (RF=3 -> 3 AZs)
  # <ZONE_A>=<AZ_1>  <ZONE_B>=<AZ_2>  <ZONE_C>=<AZ_3>
"""

_CFG_LOKI = """\
# Loki zone-aware replication. RF = number of zones (3). Managed by the
# rollout-operator (rollout-group: ingester), one StatefulSet at a time.
# Source: ARCHITECTURE-1.9 section 0.3.
loki:
  config:
    distributor:
      zone_awareness_enabled: true    # write path
    querier:
      zone_awareness_enabled: true    # read path
    ingester:
      lifecycler:
        ring:
          replication_factor: <RF_3>
          zone_awareness_enabled: true
# Deploy 3 per-zone ingester StatefulSets (zone-a/zone-b/zone-c); each pod
# labeled availability-zone=<ZONE>. Migration safety: set the existing
# ingester PDB maxUnavailable:0, DOUBLE max-series/stream limits first,
# enable the write path, WAIT query_ingesters_within (~3h), THEN enable
# the read path.
"""

_CFG_PREFER_CLOSE = """\
# Kubernetes topology-aware routing: prefer same-zone endpoints so query
# fan-out / traffic stays in-AZ. Needs k8s >= 1.31 (beta) / >= 1.33 (GA).
# Source: ARCHITECTURE-1.9 section 0.4.
apiVersion: v1
kind: Service
metadata:
  name: <SERVICE_NAME>
spec:
  trafficDistribution: PreferClose    # >= 1.33 alias: PreferSameZone
  selector:
    <APP_SELECTOR>
  ports:
    - port: <PORT>
      targetPort: <TARGET_PORT>
# GUARDRAIL: only when endpoints are spread across ALL serving zones with
# >= replicas/zone to carry the current per-zone traffic. PreferClose has
# NO overload safeguard; imbalance -> hotspot the thin zone.
"""

_CFG_KARPENTER = """\
# Karpenter 3-AZ discovery -- structural fix for the AZ imbalance that
# forces the ring into 2 zones. Source: ARCHITECTURE-1.9 section 0.5.
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: <NODECLASS_NAME>
spec:
  role: "<KARPENTER_NODE_ROLE>"
  amiSelectorTerms:
    - alias: <AMI_ALIAS_e.g._al2023@latest>
  subnetSelectorTerms:      # MUST resolve a subnet in EACH of the 3 AZs
    - tags:
        karpenter.sh/discovery: "<CLUSTER_NAME>"
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "<CLUSTER_NAME>"
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: <NODEPOOL_NAME>
spec:
  template:
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: <NODECLASS_NAME>
      requirements:
        - key: topology.kubernetes.io/zone
          operator: In
          values: ["<AZ_1>", "<AZ_2>", "<AZ_3>"]   # all 3 AZs
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]      # or ["spot", "on-demand"]
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: <e.g._1m>
    budgets:
      - nodes: "10%"                 # rate-limit voluntary disruption
  limits:
    cpu: "<CPU_LIMIT>"
# ACTION for the imbalance root cause: tag a subnet in the MISSING AZ
# (e.g. the 3rd AZ) with karpenter.sh/discovery=<CLUSTER_NAME> so the ring
# can balance across 3 AZs. A discovery-subnet gap -> no nodes there.
"""

_CFG_NLB = """\
# NLB cross-zone off (secondary saving). GATED -- never blind-disable: a
# thin AZ with too few healthy targets black-holes. Confirm target health
# first. Source: ARCHITECTURE-1.9 section 0.6.
# NOTE: annotation keys are indented 3 spaces so the (unbreakable) AWS
# target-group-attribute key fits; YAML only requires siblings to align.
apiVersion: v1
kind: Service
metadata:
  name: <NLB_SERVICE_NAME>
  annotations:
   service.beta.kubernetes.io/aws-load-balancer-type: "external"
   service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: "ip"
   # Turn OFF cross-zone (removes the cross-AZ LB charge) ...
   service.beta.kubernetes.io/aws-load-balancer-attributes: >-
    load_balancing.cross_zone.enabled=false
   # ... ONLY WITH these health gates so a thin AZ fails safe:
   service.beta.kubernetes.io/aws-load-balancer-target-group-attributes: >-
    target_group_health.dns_failover.minimum_healthy_targets.count=1,
    target_group_health.unhealthy_state_routing.minimum_healthy_targets.count=1
   # Lower-risk alternative (keep cross-zone ON, same-AZ affinity):
   #   dns_record.client_routing_policy=availability_zone_affinity
spec:
  type: LoadBalancer
  selector:
    <APP_SELECTOR>
  ports:
    - port: <PORT>
      targetPort: <TARGET_PORT>
# GUARDRAIL: confirm >= 2 HEALTHY targets in EVERY enabled AZ (elbv2
# describe-target-health) BEFORE disabling cross-zone. keeps_availability
# stays false until every enabled AZ passes.
"""

# ---------------------------------------------------------------------------
# reliability.check integration (contract section 5). Prefer the real module;
# fall back to a self-contained implementation of the SAME contract so the
# planner is correct when reliability.py is not yet present.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - trivial import guard
    from nr2grafana import reliability as _reliability  # type: ignore
except Exception:  # noqa: BLE001
    _reliability = None  # type: ignore


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _num(value: Any, default: float = 0.0) -> float:
    """Best-effort float; never raises."""
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _quorum_min(rf: int) -> int:
    """Healthy zones needed to serve: floor(RF/2)+1."""
    return int(math.floor(rf / 2.0)) + 1


def _check(mitigation: Dict[str, Any],
           context: Dict[str, Any]) -> Dict[str, Any]:
    """Call reliability.check, falling back to the local contract impl.

    Returns ``{safe, violations, required_preconditions}``.
    """
    if _reliability is not None and hasattr(_reliability, "check"):
        try:
            res = _reliability.check(mitigation, context)
            if isinstance(res, dict) and "safe" in res:
                res.setdefault("violations", [])
                res.setdefault("required_preconditions", [])
                return res
        except Exception:  # noqa: BLE001 - never trust a sibling to not raise
            pass
    return _fallback_check(mitigation, context)


def _v(rule: str, message: str, impacts: List[str]) -> Dict[str, Any]:
    return {"rule": rule, "message": message, "impacts": list(impacts)}


def _violation_text(v: Any) -> str:
    """A human string for a violation (dict from reliability, or a str)."""
    if isinstance(v, dict):
        return str(v.get("message") or v.get("rule") or v)
    return str(v)


def _fallback_check(mitigation: Dict[str, Any],
                    context: Dict[str, Any]) -> Dict[str, Any]:
    """Self-contained reliability guardrails (contract section 5).

    Used ONLY when :mod:`nr2grafana.reliability` is not importable. Mirrors
    that module's result contract: violations are dicts ``{rule, message,
    impacts}`` where ``impacts`` lists the harmed dimensions
    (availability / durability / performance), so :func:`_keeps_flags`
    reasons over ``impacts`` uniformly for both implementations.
    """
    kind = mitigation.get("kind", "")
    rf = int(_num(context.get("replication_factor"), DEFAULT_RF)) or DEFAULT_RF
    need = _quorum_min(rf)
    violations: List[Dict[str, Any]] = []
    preconds: List[str] = []

    def _current_traffic() -> None:
        preconds.append(
            "verify post-change per-zone capacity >= the current per-zone "
            "rate so the change still serves current traffic")
        rate = context.get("per_zone_current_rate")
        cap = context.get("per_zone_capacity_after")
        if isinstance(rate, dict) and isinstance(cap, dict):
            for z, r in rate.items():
                nd = _num(r)
                hv = _num(cap.get(z))
                if hv < nd:
                    violations.append(_v(
                        "handles_current_traffic",
                        "zone %s post-change capacity (%g) < current rate "
                        "(%g)" % (z, hv, nd), [PERFORMANCE]))

    if kind in ("mimir_zone_aware", "loki_zone_aware"):
        zones = mitigation.get("zones") or context.get("available_zones")
        nzones = len([z for z in (zones or []) if z])
        healthy = int(_num(context.get("healthy_zones"), nzones or rf))
        preconds.extend([
            "deploy across >= RF zones (RF=%d needs >= %d zones) and keep "
            ">= floor(RF/2)+1 = %d zones healthy" % (rf, rf, need),
            "do not change the replication factor (keep RF=%d) or the "
            "retention window" % rf,
            "migrate the live ring one zone at a time via the "
            "rollout-operator (never roll more than one zone concurrently)",
            "set the existing ingester PDB maxUnavailable:0 during the "
            "reshuffle",
            "double the max-series / max-stream limits BEFORE adding zones",
            "enable the write path first, wait query_ingesters_within / "
            "query_store_after, THEN enable the read path",
            "keep ingester CPU as requests only (no CPU limit)",
        ])
        if nzones and nzones < rf:
            violations.append(_v(
                "rf_quorum",
                "deploying to fewer zones (%d) than RF (%d) risks data "
                "loss" % (nzones, rf), [DURABILITY, AVAILABILITY]))
        if healthy < need:
            violations.append(_v(
                "rf_quorum",
                "only %d healthy zone(s); RF=%d needs a quorum of %d"
                % (healthy, rf, need), [AVAILABILITY]))
        if mitigation.get("cpu_limit_ingesters") is True:
            violations.append(_v(
                "no_cpu_limit_ingesters",
                "CPU limit on ingesters throttles ingest",
                [PERFORMANCE, DURABILITY]))
        if mitigation.get("pdb_present") is False:
            violations.append(_v(
                "pdb", "no PodDisruptionBudget for the rolled StatefulSet",
                [AVAILABILITY, DURABILITY]))
        _current_traffic()

    elif kind == "prefer_close":
        preconds.append(
            "PreferClose has no overload safeguard: only enable it when "
            "endpoints are spread across ALL serving zones with >= the "
            "replicas per zone needed to carry current per-zone traffic")
        replicas = context.get("per_zone_replicas")
        required = context.get("per_zone_required_replicas")
        if isinstance(replicas, dict) and isinstance(required, dict):
            for z, nd in required.items():
                have = _num(replicas.get(z))
                if have < _num(nd):
                    violations.append(_v(
                        "preferclose_overload",
                        "zone %s has %d replica(s) but needs %d -- "
                        "PreferClose would hotspot it"
                        % (z, int(have), int(_num(nd))),
                        [PERFORMANCE, AVAILABILITY]))
        _current_traffic()

    elif kind == "karpenter_3az":
        preconds.extend([
            "tag a discovery subnet in EACH of >= RF (%d) AZs so Karpenter "
            "can balance the ring across zones" % rf,
            "set Karpenter disruption budgets to rate-limit node churn",
            "keep PodDisruptionBudgets on stateful workloads",
        ])
        azs = mitigation.get("discovery_subnet_azs") or mitigation.get(
            "zones") or []
        azs = [z for z in azs if z]
        if azs and len(azs) < rf:
            violations.append(_v(
                "karpenter_subnets",
                "discovery subnets resolve in only %d AZ(s) but RF=%d"
                % (len(azs), rf), [DURABILITY, AVAILABILITY]))
        if mitigation.get("consolidation_budgets") is False:
            violations.append(_v(
                "karpenter_budgets",
                "no Karpenter disruption budgets", [AVAILABILITY]))
        if mitigation.get("pdb_present") is False:
            violations.append(_v(
                "pdb", "no PodDisruptionBudget on stateful workloads",
                [AVAILABILITY, DURABILITY]))
        _current_traffic()

    elif kind == "nlb_cross_zone":
        preconds.extend([
            "before disabling cross-zone, confirm >= 1 (ideally >= 2) "
            "HEALTHY target in EVERY enabled AZ (describe-target-health)",
            "set both minimum_healthy_targets.count gates so a thin AZ "
            "fails over / fails open instead of black-holing",
            "lower-risk alternative: keep cross-zone ON and set "
            "dns_record.client_routing_policy=availability_zone_affinity",
        ])
        gates = _fallback_gates(mitigation)
        health = context.get("nlb_target_health")
        enabled = [z for z in (context.get("nlb_enabled_azs") or []) if z]
        if isinstance(health, dict) and not enabled:
            enabled = [z for z in health if z]
        for az in enabled:
            info = health.get(az) if isinstance(health, dict) else None
            hc = None
            if isinstance(info, dict):
                hc = _num(info.get("healthy"), -1.0)
            elif info is not None:
                hc = _num(info, -1.0)
            if hc is None or hc < 0:
                preconds.append(
                    "verify healthy target count for enabled AZ %s" % az)
                continue
            if hc < 1:
                violations.append(_v(
                    "nlb_target_health",
                    "AZ %s has no healthy target; disabling cross-zone "
                    "black-holes it" % az, [AVAILABILITY]))
            elif hc < 2:
                preconds.append(
                    "AZ %s has a single healthy target; add a second (or "
                    "keep cross-zone)" % az)
        if not gates:
            violations.append(_v(
                "nlb_target_health",
                "disabling cross-zone without the minimum_healthy_targets "
                "gates black-holes a thin AZ", [AVAILABILITY]))
        _current_traffic()

    else:
        violations.append(_v(
            "unknown_mitigation",
            "no reliability rule for this mitigation kind",
            [AVAILABILITY, DURABILITY, PERFORMANCE]))

    return {
        "kind": kind,
        "safe": not violations,
        "violations": violations,
        "required_preconditions": preconds,
    }


def _fallback_gates(mitigation: Dict[str, Any]) -> bool:
    if mitigation.get("health_gates_set") is True:
        return True
    dns = _num(mitigation.get("dns_failover_min_healthy"), -1.0)
    unh = _num(mitigation.get("unhealthy_state_routing_min_healthy"), -1.0)
    return dns >= 1 and unh >= 1


# ---------------------------------------------------------------------------
# keeps_* mapping. reliability.check tags each violation with `impacts`
# (availability / durability / performance); we honour those, and fall back
# to a keyword scan of the message when a violation carries no impacts.
# handles_current_traffic is false whenever availability OR performance is
# harmed (a black-hole or a hotspot both stop serving current traffic).
# ---------------------------------------------------------------------------

_DIM_KEYWORDS = (
    ("data loss", DURABILITY),
    ("durab", DURABILITY),
    ("retention", DURABILITY),
    ("replication factor", DURABILITY),
    ("black-hole", AVAILABILITY),
    ("quorum", AVAILABILITY),
    ("availab", AVAILABILITY),
    ("hotspot", PERFORMANCE),
    ("overload", PERFORMANCE),
    ("throttl", PERFORMANCE),
    ("capacity", PERFORMANCE),
    ("performance", PERFORMANCE),
)


def _dims_broken(violations: List[Any]) -> Tuple[set, bool]:
    """Return (set of harmed dimensions, any_unclassified)."""
    dims = set()  # type: set
    unclassified = False
    for v in violations:
        impacts = None
        message = ""
        if isinstance(v, dict):
            impacts = v.get("impacts")
            message = str(v.get("message", ""))
        else:
            message = str(v)
        if isinstance(impacts, (list, tuple)) and impacts:
            for d in impacts:
                dims.add(str(d))
            continue
        low = message.lower()
        hit = False
        for kw, dim in _DIM_KEYWORDS:
            if kw in low:
                dims.add(dim)
                hit = True
        if not hit:
            unclassified = True
    return dims, unclassified


def _keeps_flags(chk: Dict[str, Any]) -> Dict[str, bool]:
    """Derive keeps_* / handles_current_traffic from a reliability result.

    A safe result keeps everything. An unsafe result drops the specific
    dimensions its violations harm; an unclassifiable violation drops ALL
    flags (conservative -- never certify a change we cannot reason about).
    """
    if chk.get("safe"):
        return {
            "keeps_availability": True,
            "keeps_durability": True,
            "keeps_performance": True,
            "handles_current_traffic": True,
        }
    dims, unclassified = _dims_broken(chk.get("violations") or [])
    if unclassified:
        return {
            "keeps_availability": False,
            "keeps_durability": False,
            "keeps_performance": False,
            "handles_current_traffic": False,
        }
    keeps_availability = AVAILABILITY not in dims
    keeps_performance = PERFORMANCE not in dims
    return {
        "keeps_availability": keeps_availability,
        "keeps_durability": DURABILITY not in dims,
        "keeps_performance": keeps_performance,
        "handles_current_traffic": keeps_availability and keeps_performance,
    }


# ---------------------------------------------------------------------------
# RCA input parsing (schema "nr2grafana/rca/v1"), defensive on key names.
# ---------------------------------------------------------------------------

def _incident(rca: Dict[str, Any]) -> Dict[str, Any]:
    inc = (rca or {}).get("incident") or {}
    if not isinstance(inc, dict):
        inc = {}
    usd_day = _num(inc.get("usd_per_day"))
    if not usd_day:
        usd_day = _num(inc.get("$/day"))
    if not usd_day:
        usd_day = _num(inc.get("dollars_per_day"))
    gb_day = _num(inc.get("gb_per_day"))
    if not gb_day:
        gb_day = _num(inc.get("GB/day"))
    return {
        "usage_type": inc.get("usage_type"),
        "service": inc.get("service"),
        "account": inc.get("account"),
        "region": inc.get("region"),
        "usd_per_day": round(usd_day, 2),
        "gb_per_day": round(gb_day, 2),
        "onset": inc.get("onset"),
        "step_change": inc.get("step_change") or inc.get("step-change"),
    }


def _cause(rca: Dict[str, Any]) -> Dict[str, Any]:
    cause = (rca or {}).get("cause") or {}
    if not isinstance(cause, dict):
        cause = {}
    dom = cause.get("dominant") or {}
    if not isinstance(dom, dict):
        dom = {}
    sec = cause.get("secondary") or []
    if not isinstance(sec, list):
        sec = []
    return {"dominant": dom, "secondary": sec}


def _share(driver: Dict[str, Any]) -> float:
    """Fraction 0..1 from a driver's `share` (accepts 0.91 or 91)."""
    s = _num(driver.get("share"))
    if s > 1.0:
        s = s / 100.0
    return max(0.0, min(1.0, s))


def _driver_class(driver: Dict[str, Any]) -> str:
    for key in ("class", "hypothesis_class", "hypothesis", "type"):
        val = driver.get(key)
        if isinstance(val, str) and val:
            return val.upper()
    # Infer from usage_type / summary text as a last resort.
    text = " ".join(str(driver.get(k, "")) for k in
                    ("usage_type", "summary", "title")).lower()
    if "nlb" in text or "cross-zone" in text or "load balanc" in text:
        return "NLB_CROSS_ZONE"
    if "regional-bytes" in text or "cross-az" in text or "interzone" in text:
        return "CROSS_AZ_NETWORK"
    return ""


# ---------------------------------------------------------------------------
# Context assembly for reliability.check.
# ---------------------------------------------------------------------------

def _first(cap: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in cap and cap[k] is not None:
            return cap[k]
    return None


def _build_context(rca: Dict[str, Any], deepdive: Optional[Dict[str, Any]],
                   packing: Optional[Dict[str, Any]],
                   capacity: Optional[Dict[str, Any]],
                   cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the reliability.check ``context`` from the read-only inputs.

    Key names follow :mod:`nr2grafana.reliability`'s contract so its rules
    read them directly; common aliases from ``capacity`` are accepted.
    """
    mcfg = (cfg.get("mitigate") or {}) if isinstance(cfg, dict) else {}
    rf = int(_num(mcfg.get("rf"), DEFAULT_RF)) or DEFAULT_RF
    cap = capacity if isinstance(capacity, dict) else {}

    available = _first(cap, "available_zones", "zones", "azs")
    if isinstance(available, int):
        available = ["zone-%d" % i for i in range(available)]
    if not isinstance(available, (list, tuple)):
        available = None
    n_avail = len([z for z in available if z]) if available else 0
    # The primary/structural fix targets a layout with >= RF zones.
    target_n = max(rf, n_avail)
    target_zones = ["<AZ_%d>" % (i + 1) for i in range(target_n)]

    ctx: Dict[str, Any] = {
        "rca": rca,
        "deepdive": deepdive,
        "packing": packing,
        "capacity": cap,
        "cfg": cfg,
        # RF / quorum (0.2/0.3):
        "replication_factor": rf,
        "available_zones": list(available) if available else None,
        "healthy_zones": _first(cap, "healthy_zones"),
        "retention_days": _first(cap, "retention_days"),
        # PreferClose (0.4):
        "serving_zones": _first(cap, "serving_zones"),
        "endpoint_zones": _first(cap, "endpoint_zones"),
        "per_zone_replicas": _first(cap, "per_zone_replicas"),
        "per_zone_required_replicas": _first(
            cap, "per_zone_required_replicas"),
        # Handles-current-traffic (rule _rule_current_traffic):
        "per_zone_current_rate": _first(
            cap, "per_zone_current_rate", "per_zone_traffic"),
        "per_zone_capacity_after": _first(
            cap, "per_zone_capacity_after", "per_zone_capacity"),
        # NLB (0.6):
        "nlb_target_health": _first(cap, "nlb_target_health"),
        "nlb_enabled_azs": _first(cap, "nlb_enabled_azs"),
        # Internal: the AZ layout our proposals target (>= RF zones).
        "_target_zones": target_zones,
    }
    return ctx


# ---------------------------------------------------------------------------
# Mitigation builders.
# ---------------------------------------------------------------------------

def _split_weights(cfg: Dict[str, Any]) -> Dict[str, float]:
    weights = dict(DEFAULT_SPLIT)
    mcfg = (cfg.get("mitigate") or {}) if isinstance(cfg, dict) else {}
    override = mcfg.get("split")
    if isinstance(override, dict):
        for k, v in override.items():
            if k in weights and isinstance(v, (int, float)):
                weights[k] = float(v)
    return weights


def _expected(usd_per_day: float, total_usd: float,
              gb_per_day: float) -> Dict[str, Any]:
    pct = (usd_per_day / total_usd * 100.0) if total_usd > 0 else 0.0
    return {
        "usd_per_day": round(usd_per_day, 2),
        "usd_per_month": round(usd_per_day * 30.0, 2),
        "percent_of_anomaly": round(pct, 1),
        "gb_per_day": round(gb_per_day, 1),
    }


def _safety_fields(kind: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """The safety measures our PROPOSAL always includes, expressed as the
    mitigation fields reliability.check inspects (so a correctly-built
    proposal is not flagged for a measure it already carries)."""
    target = context.get("_target_zones") or []
    if kind in ("mimir_zone_aware", "loki_zone_aware"):
        return {
            "zones": list(target),
            "pdb_present": True,
            "cpu_limit_ingesters": False,
            "migration": {
                "one_zone_at_a_time": True,
                "existing_ingester_pdb_max_unavailable": 0,
                "limits_doubled": True,
                "write_path_before_read_path": True,
                "wait_observed": True,
            },
        }
    if kind == "karpenter_3az":
        return {
            "zones": list(target),
            "discovery_subnet_azs": list(target),
            "consolidation_budgets": True,
            "pdb_present": True,
        }
    if kind == "nlb_cross_zone":
        return {
            "health_gates_set": True,
            "dns_failover_min_healthy": 1,
            "unhealthy_state_routing_min_healthy": 1,
        }
    return {}


def _mk(kind: str, mid: str, role: str, addresses: str, title: str,
        change: str, config_name: str, config_body: str, source: str,
        expected: Dict[str, Any], context: Dict[str, Any],
        extra_caveats: Optional[List[str]] = None) -> Dict[str, Any]:
    mit: Dict[str, Any] = {
        "id": mid,
        "kind": kind,
        "role": role,
        "addresses": addresses,
        "title": title,
        "expected": expected,
        "change": change,
        "owner": "GitOps/IaC -- proposal only, never executed",
        "config": {
            "format": "yaml",
            "filename": config_name,
            "content": config_body,
        },
        "source": source,
    }
    mit.update(_safety_fields(kind, context))
    chk = _check(mit, context)
    mit["reliability_guardrails"] = list(
        chk.get("required_preconditions") or [])
    mit["reliability"] = {
        "safe": bool(chk.get("safe")),
        "violations": list(chk.get("violations") or []),
    }
    flags = _keeps_flags(chk)
    mit.update(flags)
    caveats: List[str] = list(extra_caveats or [])
    if not chk.get("safe"):
        caveats.append(
            "NOT reliability-safe as-is: DEMOTED. Do not apply until the "
            "guardrails below pass. Broken: "
            + "; ".join(_violation_text(v)
                        for v in (chk.get("violations") or [])))
    mit["caveats"] = caveats
    mit["safe"] = bool(chk.get("safe"))
    return mit


def _cross_az_mitigations(dominant: Dict[str, Any], total_usd: float,
                          total_gb: float, context: Dict[str, Any],
                          cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """M1-M4: the primary set that makes the stack zone/topology aware."""
    share = _share(dominant)
    dom_usd = total_usd * share
    dom_gb = total_gb * share
    weights = _split_weights(cfg)

    def portion(kind: str) -> Tuple[float, float]:
        w = weights.get(kind, 0.0)
        return dom_usd * w, dom_gb * w

    out: List[Dict[str, Any]] = []

    u, g = portion("mimir_zone_aware")
    out.append(_mk(
        "mimir_zone_aware", "M1", "primary", "dominant",
        "Make Mimir zone-aware (kill cross-AZ RF=3 replication writes)",
        "Enable Mimir zone-aware replication on the write and read path "
        "and label each ingester with its AZ, so the RF=3 quorum keeps "
        "replicas in-zone instead of crossing AZ boundaries. Migrate the "
        "live ring one zone at a time with the rollout-operator.",
        "mimir-zone-aware.values.yaml", _CFG_MIMIR, "0.2",
        _expected(u, total_usd, g), context))

    u, g = portion("loki_zone_aware")
    out.append(_mk(
        "loki_zone_aware", "M2", "primary", "dominant",
        "Make Loki zone-aware (in-zone stream replication)",
        "Enable Loki zone-aware replication and deploy 3 per-zone ingester "
        "StatefulSets so RF=3 stream replicas stay in-zone. Double the "
        "stream limits first and enable write path before read path.",
        "loki-zone-aware.values.yaml", _CFG_LOKI, "0.3",
        _expected(u, total_usd, g), context))

    u, g = portion("prefer_close")
    out.append(_mk(
        "prefer_close", "M3", "primary", "dominant",
        "Prefer same-zone routing for intra-stack Services",
        "Set trafficDistribution: PreferClose on the distributor / querier "
        "/ gateway Services so query fan-out stays in-zone. Only safe when "
        "every serving zone has enough replicas to carry its own traffic.",
        "service-prefer-close.yaml", _CFG_PREFER_CLOSE, "0.4",
        _expected(u, total_usd, g), context))

    u, g = portion("karpenter_3az")
    out.append(_mk(
        "karpenter_3az", "M4", "structural", "dominant",
        "Add a 3rd-AZ Karpenter discovery subnet (unblock zone balance)",
        "Tag a discovery subnet in the missing AZ so Karpenter can launch "
        "nodes in all 3 AZs. This is the STRUCTURAL enabler: without a "
        "subnet in the 3rd AZ the zone-aware ring cannot spread and the "
        "cross-AZ traffic persists. No direct $ but unlocks M1-M3.",
        "karpenter-3az.yaml", _CFG_KARPENTER, "0.5",
        _expected(u, total_usd, g), context,
        extra_caveats=[
            "Structural enabler: realises M1-M3's savings by letting the "
            "ring balance across 3 AZs; on its own it adds an AZ, it does "
            "not by itself cut the bill."]))
    return out


def _nlb_mitigation(secondary: Dict[str, Any], total_usd: float,
                    total_gb: float, context: Dict[str, Any],
                    mid: str) -> Dict[str, Any]:
    share = _share(secondary)
    u = total_usd * share
    g = total_gb * share
    return _mk(
        "nlb_cross_zone", mid, "secondary", "secondary",
        "Disable NLB cross-zone (GATED on per-AZ target health)",
        "Turn off cross-zone load balancing on the stack's NLB to drop the "
        "cross-AZ LB charge -- but ONLY with the minimum_healthy_targets "
        "gates set AND after confirming redundant healthy targets in every "
        "enabled AZ, or a thin AZ black-holes. Prefer availability_zone_"
        "affinity if any AZ is thin.",
        "nlb-cross-zone.yaml", _CFG_NLB, "0.6",
        _expected(u, total_usd, g), context)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def plan(rca: Optional[Dict[str, Any]] = None,
         deepdive: Optional[Dict[str, Any]] = None,
         packing: Optional[Dict[str, Any]] = None,
         capacity: Optional[Dict[str, Any]] = None,
         cfg: Optional[Dict[str, Any]] = None,
         log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Build the reliability-safe mitigation plan for an RCA result.

    ``rca`` is a "nr2grafana/rca/v1" dict (from :func:`nr2grafana.rca.
    analyze`). ``deepdive`` / ``packing`` / ``capacity`` are optional
    context. ``capacity`` may carry the per-zone facts the guardrails need:
    ``zones``, ``healthy_zones``, ``per_zone_replicas``,
    ``per_zone_required_replicas``, ``endpoints_spread_all_zones``,
    ``nlb_target_health`` (``{az: healthy_count}``), ``consolidation_
    budgets``, ``pdb_present``, ``cpu_limit_on_ingesters``.

    Returns schema "nr2grafana/mitigation/v1". Never raises.
    """
    emit = log or (lambda m: None)
    cfg = cfg or {}
    rca = rca or {}

    incident = _incident(rca)
    total_usd = incident["usd_per_day"]
    total_gb = incident["gb_per_day"]
    cause = _cause(rca)
    context = _build_context(rca, deepdive, packing, capacity, cfg)

    mitigations: List[Dict[str, Any]] = []
    notes: List[str] = []

    dom = cause["dominant"]
    dom_class = _driver_class(dom)
    if dom_class == "CROSS_AZ_NETWORK":
        emit("mitigate: dominant driver is cross-AZ network; emitting "
             "zone/topology-aware primary set (M1-M4).")
        mitigations.extend(_cross_az_mitigations(
            dom, total_usd, total_gb, context, cfg))
    elif dom:
        notes.append(
            "Dominant driver class '%s' has no reliability-safe config "
            "template in this release; only cross-AZ network drivers are "
            "auto-planned. Review manually." % (dom_class or "unknown"))

    # Secondary drivers: NLB cross-zone gets M5 (gated).
    sec_idx = len(mitigations) + 1
    for sec in cause["secondary"]:
        if not isinstance(sec, dict):
            continue
        if _driver_class(sec) == "NLB_CROSS_ZONE":
            mitigations.append(_nlb_mitigation(
                sec, total_usd, total_gb, context, "M%d" % sec_idx))
            sec_idx += 1

    if not mitigations:
        notes.append(
            "No auto-plannable driver found in the RCA (need a cross-AZ "
            "network dominant driver and/or an NLB cross-zone secondary).")

    # Rank: SAFE first, then by expected $/day saved desc, then id.
    def rank_key(m: Dict[str, Any]) -> Tuple[int, float, str]:
        return (0 if m.get("safe") else 1,
                -_num(m.get("expected", {}).get("usd_per_day")),
                m.get("id", ""))
    mitigations.sort(key=rank_key)

    safe_usd = sum(_num(m["expected"]["usd_per_day"])
                   for m in mitigations if m.get("safe"))
    all_usd = sum(_num(m["expected"]["usd_per_day"]) for m in mitigations)
    safe_count = sum(1 for m in mitigations if m.get("safe"))
    unsafe_count = len(mitigations) - safe_count

    headline = (
        "~$%.0f/day saved without reducing availability, durability, "
        "performance, or current-traffic capacity" % safe_usd)
    if unsafe_count:
        headline += (
            " (a further ~$%.0f/day is available but %d mitigation(s) are "
            "DEMOTED until their reliability guardrails pass)"
            % (max(0.0, all_usd - safe_usd), unsafe_count))

    emit("mitigate: %d mitigation(s), %d safe / %d demoted; safe savings "
         "~$%.2f/day." % (len(mitigations), safe_count, unsafe_count,
                          safe_usd))

    return {
        "schema": SCHEMA,
        "generated_by": GENERATED_BY,
        "generated_at": _utcnow(),
        "incident_ref": incident,
        "mitigations": mitigations,
        "summary": {
            "count": len(mitigations),
            "safe_count": safe_count,
            "unsafe_count": unsafe_count,
            "total_est_usd_per_day_saved": round(safe_usd, 2),
            "total_est_usd_per_month_saved": round(safe_usd * 30.0, 2),
            "total_est_usd_per_day_saved_if_all": round(all_usd, 2),
            "headline": headline,
        },
        "notes": notes,
    }
