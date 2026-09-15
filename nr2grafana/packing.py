"""Kubernetes topology, bin-packing, right-sizing, durability and deep
Karpenter analysis for the LGTM observability node pool.

This is where cost meets availability. Every recommendation here trades a
dollar figure against durability / availability / performance, and the
engine defaults to the SAFE option: it right-sizes to the observed peak
(never below), keeps stateful ingesters on-demand, keeps zone-aware
replication, and refuses to cut redundancy for cost without a loud caveat.

Everything Kubernetes-dependent is OPTIONAL. The module only shells out to
``kubectl`` when it is on ``PATH`` and the caller enabled it; otherwise it
degrades to a clear "kubectl not available" note and the wider tool stays
fully usable without a cluster. Tests inject canned topology / nodepool
dicts, so nothing here requires a real cluster.

Domain grounding (see ARCHITECTURE-1.6 and the reference deepdive):

- **Bin-pack constraints**: pods of *different* Mimir zone groups never
  share a node (same-zone ingesters MAY -- that is the packing win, made
  safe by zone-aware replication); Loki / Tempo ingesters are one-per-node
  (RF=3). ``sum(mem limits) / node capacity > 1.0`` means a burst can OOM a
  node that carries two ingesters.
- **Right-size to peak**: suggested request = observed peak x1.5 (CPU) /
  x1.3 (mem), never below the observed peak. ``keeps_performance`` is only
  true when the suggestion stays above peak.
- **Durability**: never CPU-limit Mimir ingesters; ingesters need a
  priorityClass and ``memory request == limit``; a PDB that allows 0
  disruptions blocks drains AND consolidation; an AZ hosting two logical
  zones loses quorum on failure.
- **Karpenter**: memory-bound backends want ``r``-class; stateful
  ingesters stay on-demand (spot reclaim = ring churn / WAL loss);
  consolidation is the main cost lever but disruption budgets must respect
  PDBs; ``expireAfter: Never`` is patch drift; NodePool limits cap runaway
  scale.

Output schema: "nr2grafana/packing/v1".

All prices / instance shapes are GENERIC public AWS on-demand list values,
clearly overridable via ``cfg`` / the ``PRICES`` env var. No customer
value (account id, bucket, cluster or AZ) is baked into this module -- any
AZ that appears in a proposal is read live from the caller's own cluster.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

SCHEMA = "nr2grafana/packing/v1"
GENERATED_BY = "nr2grafana 1.6.0"

GIB = 2 ** 30
HOURS_MO = 730  # billed hours in an average month

# Right-sizing headroom over the observed 24h peak. Requests are set above
# peak so a suggestion never throttles the workload it right-sizes.
CPU_PEAK_HEADROOM = 1.5
MEM_PEAK_HEADROOM = 1.3

# Default namespaces treated as observability workloads and the default
# node-pool label selector. Both are overridable via cfg.
DEFAULT_NAMESPACES = [
    "mimir", "loki", "tempo", "kube-prometheus-stack",
    "otel-collector-gateway", "otel-collector-daemonset", "otel",
    "monitoring", "grafana",
]
DEFAULT_POOL_SELECTOR = "node-type=observability"

# --------------------------------------------------------------------------
# Generic price / shape tables (public AWS on-demand list prices, USD/hr and
# vCPU/GiB). These are ASSUMPTIONS, not a quote -- override via cfg["prices"]
# / the PRICES env var (JSON) or cfg["shapes"].
# --------------------------------------------------------------------------
PRICE_HR: Dict[str, float] = {
    "m6a.2xlarge": 0.3456, "m6a.4xlarge": 0.6912, "m6a.8xlarge": 1.3824,
    "r6a.2xlarge": 0.4536, "r6a.4xlarge": 0.9072, "r6a.8xlarge": 1.8144,
    "m7i.2xlarge": 0.4032, "m7i.4xlarge": 0.8064,
    "m6i.2xlarge": 0.3840, "r6i.2xlarge": 0.5040, "r6i.4xlarge": 1.0080,
    "r7g.2xlarge": 0.4284, "r7g.4xlarge": 0.8568, "m7g.2xlarge": 0.3264,
    "m7a.2xlarge": 0.4637, "r7a.2xlarge": 0.6088,
}
try:
    PRICE_HR.update(json.loads(os.environ.get("PRICES", "{}")))
except (ValueError, TypeError):  # bad JSON in env -> ignore, keep defaults
    pass

SHAPES: Dict[str, Tuple[int, int]] = {  # instance -> (vCPU, GiB)
    "m6a.2xlarge": (8, 32), "m6a.4xlarge": (16, 64), "m6a.8xlarge": (32, 128),
    "r6a.2xlarge": (8, 64), "r6a.4xlarge": (16, 128),
    "r6a.8xlarge": (32, 256),
    "m7i.2xlarge": (8, 32), "m7i.4xlarge": (16, 64),
    "m6i.2xlarge": (8, 32), "r6i.2xlarge": (8, 64), "r6i.4xlarge": (16, 128),
    "r7g.2xlarge": (8, 64), "r7g.4xlarge": (16, 128), "m7g.2xlarge": (8, 32),
    "m7a.2xlarge": (8, 32), "r7a.2xlarge": (8, 64),
}

# EKS ENI-based default max-pods per node size (no prefix delegation).
MAX_PODS = {"large": 29, "xlarge": 58, "2xlarge": 58, "4xlarge": 234,
            "8xlarge": 234, "12xlarge": 234, "16xlarge": 737}

# Instance size ladder used to pick a "one size larger" burst shape.
SIZE_LADDER = ["large", "xlarge", "2xlarge", "4xlarge", "8xlarge",
               "12xlarge", "16xlarge"]

# Thresholds that turn a number into a finding.
T = {
    "node_req_util_low": 0.45,   # node <45% requested = packing problem
    "mem_over_alloc_oom": 1.0,   # sum(mem limits)/capacity > this = OOM risk
    "cpu_util_low": 0.25,        # component peak <25% of CPU request
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
_SUF = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40,
        "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "m": 1e-3}


def qty(s: Any) -> float:
    """Parse a Kubernetes quantity string to a float.

    CPU is returned in cores, memory in bytes. ``None`` / ``""`` -> 0.0.
    """
    if s is None or s == "":
        return 0.0
    s = str(s)
    for suf, mult in _SUF.items():
        if s.endswith(suf):
            try:
                return float(s[: -len(suf)]) * mult
            except ValueError:
                return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def gib(b: float) -> float:
    """Bytes -> GiB."""
    return b / GIB


def pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Safe ``a / b`` -> None when b is falsy."""
    return None if not b else a / b


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _emit(log: Optional[Callable[[str], None]], msg: str) -> None:
    if log:
        log(msg)


def eks_allocatable(vcpu: int, mem_gib: int,
                    itype: Optional[str] = None) -> Tuple[float, float]:
    """Estimate ``(cpu_cores, mem_bytes)`` allocatable on an EKS node.

    Uses the documented kube-reserved formula: CPU tiers
    60m/10m/5m/5m/2.5m..., memory ``255Mi + 11Mi*maxPods + 100Mi`` eviction
    threshold, on a kubelet-reported capacity ~98.3% of nominal. Deliberately
    deterministic so the packer is reproducible in tests.
    """
    cores = [0.06, 0.01, 0.005, 0.005] + [0.0025] * max(0, vcpu - 4)
    cpu = vcpu - sum(cores[:vcpu])
    size = (itype or "").split(".")[-1]
    max_pods = MAX_PODS.get(size, 110)
    reserved_mib = 255 + 11 * max_pods + 100
    mem = mem_gib * 0.983 * GIB - reserved_mib * 2 ** 20
    return round(cpu, 2), mem


def _finding(severity: str, area: str, title: str,
             evidence: Optional[Dict[str, Any]] = None, rationale: str = "",
             config: Optional[List[Dict[str, str]]] = None,
             est_savings: Optional[Dict[str, Any]] = None,
             keeps_performance: Optional[bool] = None,
             keeps_durability: Optional[bool] = None,
             keeps_availability: Optional[bool] = None,
             keeps_intact: Optional[bool] = None) -> Dict[str, Any]:
    """Build a finding dict with the same risk-flag shape as deepdive."""
    return {
        "severity": severity, "area": area, "title": title,
        "evidence": evidence or {}, "rationale": rationale,
        "config": config or [], "est_savings": est_savings or {},
        "keeps_performance": keeps_performance,
        "keeps_durability": keeps_durability,
        "keeps_availability": keeps_availability,
        "keeps_intact": keeps_intact,
    }


_SEV_ORDER = {"FAIL": 0, "WARN": 1, "INFO": 2}


def _rank(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(findings, key=lambda f: _SEV_ORDER.get(f["severity"], 9))


# --------------------------------------------------------------------------
# kubectl plumbing (all optional)
# --------------------------------------------------------------------------
def kubectl_available() -> bool:
    """True when ``kubectl`` is on PATH. Never raises."""
    try:
        return shutil.which("kubectl") is not None
    except Exception:
        return False


def _kget(args: List[str],
          log: Optional[Callable[[str], None]] = None) -> Optional[Dict]:
    """Run ``kubectl <args> -o json`` and return parsed JSON, or None.

    Never raises: a missing cluster / CRD / permission error becomes a
    logged note and a None return so callers degrade cleanly.
    """
    try:
        out = subprocess.run(
            ["kubectl"] + list(args) + ["-o", "json"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _emit(log, "kubectl %s failed: %s" % (" ".join(args), exc))
        return None
    if out.returncode != 0:
        _emit(log, "kubectl %s: %s" % (
            " ".join(args), (out.stderr or "").strip()[:200]))
        return None
    try:
        return json.loads(out.stdout)
    except ValueError as exc:
        _emit(log, "kubectl %s: bad JSON (%s)" % (" ".join(args), exc))
        return None


# --------------------------------------------------------------------------
# Topology
# --------------------------------------------------------------------------
def classify(pod: Dict[str, Any]) -> Tuple[str, str, Optional[str],
                                           Optional[str], bool]:
    """Classify a pod -> ``(stack, component, zone_group, anti, anti_hard)``.

    ``zone_group`` is set only for Mimir ingesters: pods of *different* zone
    groups must never share a node, same-zone pods may. ``anti`` is a soft
    HA spread key (replicas prefer separate nodes). ``anti_hard`` marks
    Loki / Tempo ingesters that must never share a node (RF=3).
    """
    ns = pod["metadata"]["namespace"]
    lab = pod["metadata"].get("labels", {}) or {}
    comp = (lab.get("app.kubernetes.io/component")
            or lab.get("component") or "")
    name = lab.get("app.kubernetes.io/name") or lab.get("app") or ns
    stack = name if name in (
        "mimir", "loki", "tempo", "grafana", "prometheus",
        "alertmanager") else ns
    zone_group = None
    anti = ("%s-%s" % (stack, comp)) if comp else None
    anti_hard = False
    if stack == "mimir" and comp == "ingester":
        zg = lab.get("name") or lab.get("rollout-group") or "ingester"
        zone_group = "mimir:%s" % zg
        anti = None  # same-zone ingesters may co-locate -- the packing win
    if stack in ("loki", "tempo") and comp == "ingester":
        anti_hard = True
    return stack, comp, zone_group, anti, anti_hard


def _pod_resources(pod: Dict[str, Any]) -> Tuple[float, float, float, float]:
    cpu_r = mem_r = cpu_l = mem_l = 0.0
    for c in pod["spec"].get("containers", []):
        res = c.get("resources", {}) or {}
        req = res.get("requests", {}) or {}
        lim = res.get("limits", {}) or {}
        cpu_r += qty(req.get("cpu"))
        mem_r += qty(req.get("memory"))
        cpu_l += qty(lim.get("cpu"))
        mem_l += qty(lim.get("memory"))
    return cpu_r, mem_r, cpu_l, mem_l


def collect_topology(namespaces: Optional[List[str]] = None,
                     pool_selector: str = DEFAULT_POOL_SELECTOR,
                     log: Optional[Callable[[str], None]] = None
                     ) -> Dict[str, Any]:
    """Read live cluster topology via kubectl (OPTIONAL).

    Returns a topology dict with ``nodes`` (alloc / requests / limits /
    instance type / zone / price), ``pods`` (requests, zone_group,
    anti-affinity, dnd, priority, restarts / OOM), ``pvcs`` (AZ pins), and a
    ``karpenter`` block (nodepools + nodeclaims). When kubectl is not
    available or the cluster cannot be read, returns
    ``{"available": False, "note": ...}`` -- the tool stays usable.
    """
    ns_list = namespaces or DEFAULT_NAMESPACES
    if not kubectl_available():
        return {"available": False,
                "note": "kubectl not available on PATH; Kubernetes topology "
                        "analysis skipped (the rest of nr2grafana is "
                        "unaffected)."}
    nodes_raw = _kget(["get", "nodes"], log)
    pods_raw = _kget(["get", "pods", "-A"], log)
    if nodes_raw is None or pods_raw is None:
        return {"available": False,
                "note": "kubectl is installed but the cluster could not be "
                        "read (no current context / unreachable / RBAC). "
                        "Topology analysis skipped."}

    key, _, val = pool_selector.partition("=")
    nodes = nodes_raw.get("items", [])
    pods = pods_raw.get("items", [])

    obs_nodes = set()
    for p in pods:
        if (p["metadata"]["namespace"] in ns_list
                and p["spec"].get("nodeName")):
            obs_nodes.add(p["spec"]["nodeName"])

    S: Dict[str, Any] = {
        "available": True, "pool_selector": pool_selector,
        "namespaces": ns_list, "nodes": {}, "pods": [], "pvcs": [],
    }
    for n in nodes:
        lab = n["metadata"].get("labels", {}) or {}
        nm = n["metadata"]["name"]
        in_pool = key in lab and lab.get(key) == val
        if not in_pool and nm not in obs_nodes:
            continue
        status = n.get("status", {})
        cap = status.get("capacity", {}) or {}
        alloc = status.get("allocatable", {}) or {}
        itype = lab.get("node.kubernetes.io/instance-type", "?")
        S["nodes"][nm] = {
            "in_pool": in_pool, "instance_type": itype,
            "zone": lab.get("topology.kubernetes.io/zone", "?"),
            "nodepool": (lab.get("karpenter.sh/nodepool")
                         or lab.get("eks.amazonaws.com/nodegroup")
                         or "managed/none"),
            "capacity_type": lab.get("karpenter.sh/capacity-type", "?"),
            "arch": lab.get("kubernetes.io/arch", "?"),
            "cpu_alloc": qty(alloc.get("cpu")),
            "mem_alloc": qty(alloc.get("memory")),
            "cpu_cap": qty(cap.get("cpu")), "mem_cap": qty(cap.get("memory")),
            "cpu_req": 0.0, "mem_req": 0.0, "cpu_lim": 0.0, "mem_lim": 0.0,
            "pods": [], "dnd_pods": [], "daemon_cpu_req": 0.0,
            "daemon_mem_req": 0.0, "price_hr": PRICE_HR.get(itype),
        }
    for p in pods:
        nn = p["spec"].get("nodeName")
        if (nn not in S["nodes"]
                or p["status"].get("phase") not in ("Running", "Pending")):
            continue
        cpu_r, mem_r, cpu_l, mem_l = _pod_resources(p)
        stack, comp, zg, anti, anti_hard = classify(p)
        owner = (p["metadata"].get("ownerReferences") or [{}])[0].get(
            "kind", "")
        ann = p["metadata"].get("annotations", {}) or {}
        dnd = str(ann.get("karpenter.sh/do-not-disrupt", "")).lower() == "true"
        statuses = p["status"].get("containerStatuses", []) or []
        rec = {
            "name": p["metadata"]["name"], "ns": p["metadata"]["namespace"],
            "node": nn, "stack": stack, "component": comp, "zone_group": zg,
            "anti": anti, "anti_hard": anti_hard, "owner": owner,
            "cpu_req": cpu_r, "mem_req": mem_r, "cpu_lim": cpu_l,
            "mem_lim": mem_l, "dnd": dnd,
            "priority_class": p["spec"].get("priorityClassName"),
            "node_selector": p["spec"].get("nodeSelector"),
            "restarts": sum(cs.get("restartCount", 0) for cs in statuses),
            "last_term": [
                cs.get("lastState", {}).get("terminated", {}).get("reason")
                for cs in statuses
                if cs.get("lastState", {}).get("terminated")],
        }
        S["pods"].append(rec)
        nd = S["nodes"][nn]
        nd["cpu_req"] += cpu_r
        nd["mem_req"] += mem_r
        nd["cpu_lim"] += cpu_l
        nd["mem_lim"] += mem_l
        if owner == "DaemonSet":
            nd["daemon_cpu_req"] += cpu_r
            nd["daemon_mem_req"] += mem_r
        else:
            nd["pods"].append("%s/%s(%.1fGi)" % (
                rec["ns"], rec["name"], gib(mem_r)))
        if dnd:
            nd["dnd_pods"].append("%s/%s" % (rec["ns"], rec["name"]))

    # PVC -> PV -> zone (which pods are physically pinned to an AZ).
    pv_raw = _kget(["get", "pv"], log) or {"items": []}
    pvs = {pv["metadata"]["name"]: pv for pv in pv_raw.get("items", [])}
    for ns in ns_list:
        pvc_raw = _kget(["get", "pvc", "-n", ns], log)
        if not pvc_raw:
            continue
        for pvc in pvc_raw.get("items", []):
            pv = pvs.get(pvc["spec"].get("volumeName", ""), {})
            zone = "?"
            aff = (pv.get("spec", {}).get("nodeAffinity", {})
                   .get("required", {}).get("nodeSelectorTerms", []))
            for term in aff:
                for me in term.get("matchExpressions", []):
                    if me.get("key", "").endswith("/zone"):
                        zone = ",".join(me.get("values", []))
            S["pvcs"].append({
                "ns": ns, "name": pvc["metadata"]["name"], "zone": zone,
                "size": qty(pvc.get("status", {}).get("capacity", {}).get(
                    "storage")),
                "storageclass": pvc["spec"].get("storageClassName")})

    _finish_topology(S)
    S["karpenter"] = _collect_karpenter(log)
    return S


def _collect_karpenter(log: Optional[Callable[[str], None]]) -> Dict[str, Any]:
    """Read Karpenter NodePools + NodeClaims (OPTIONAL, CRD may be absent)."""
    out: Dict[str, Any] = {"nodepools": {}, "nodeclaims": []}
    np_raw = _kget(["get", "nodepools.karpenter.sh"], log)
    if np_raw:
        for npool in np_raw.get("items", []):
            sp = npool.get("spec", {}) or {}
            tmpl = sp.get("template", {}).get("spec", {}) or {}
            out["nodepools"][npool["metadata"]["name"]] = {
                "limits": sp.get("limits"),
                "disruption": sp.get("disruption"),
                "requirements": tmpl.get("requirements"),
                "expireAfter": tmpl.get("expireAfter"),
                "node_labels": (npool.get("spec", {}).get("template", {})
                                .get("metadata", {}).get("labels")),
                "resources_in_use": npool.get("status", {}).get("resources"),
            }
    nc_raw = _kget(["get", "nodeclaims.karpenter.sh"], log)
    if nc_raw:
        for nc in nc_raw.get("items", []):
            lab = nc["metadata"].get("labels", {}) or {}
            out["nodeclaims"].append({
                "name": nc["metadata"]["name"],
                "nodepool": lab.get("karpenter.sh/nodepool"),
                "type": lab.get("node.kubernetes.io/instance-type"),
                "zone": lab.get("topology.kubernetes.io/zone"),
                "capacity_type": lab.get("karpenter.sh/capacity-type")})
    return out


def _finish_topology(S: Dict[str, Any]) -> None:
    """Fill node utilisation, pool cost, and the zone->AZ map."""
    total_cost = 0.0
    for nm, nd in S["nodes"].items():
        cost = (nd["price_hr"] or 0.0) * HOURS_MO
        nd["cost_mo"] = cost
        if nd["in_pool"]:
            total_cost += cost
        nd["util_cpu_req"] = pct(nd["cpu_req"], nd["cpu_alloc"])
        nd["util_mem_req"] = pct(nd["mem_req"], nd["mem_alloc"])
    S["pool_cost_mo"] = total_cost
    S["pool_node_count"] = sum(1 for n in S["nodes"].values() if n["in_pool"])
    zones: Dict[str, set] = defaultdict(set)
    for p in S["pods"]:
        if p["zone_group"]:
            zones[p["zone_group"]].add(S["nodes"][p["node"]]["zone"])
    S["mimir_zone_to_az"] = {k: sorted(v) for k, v in zones.items()}
    azs = set()
    for n in S["nodes"].values():
        if n["zone"] and n["zone"] != "?":
            azs.add(n["zone"])
    S["zones"] = sorted(azs)


# --------------------------------------------------------------------------
# Bin-packing
# --------------------------------------------------------------------------
def bin_pack(pods: List[Dict[str, Any]], shape: str, daemon_cpu: float,
             daemon_mem: float, prices: Optional[Dict[str, float]] = None,
             shapes: Optional[Dict[str, Tuple[int, int]]] = None
             ) -> Optional[List[Dict[str, Any]]]:
    """First-fit-decreasing (by memory) bin-pack with the real constraints.

    * Pods of *different* Mimir zone groups never share a node; same-zone
      ingesters MAY co-locate (the packing win, kept safe by zone-aware
      replication).
    * Loki / Tempo ingesters (``anti_hard``) never share a node with their
      own kind (RF=3).
    * Other replicated components prefer separate nodes (soft HA spread) but
      will co-locate rather than launch a new node.

    ``daemon_cpu`` / ``daemon_mem`` reserve per-node DaemonSet overhead.
    Returns the list of bins (each with ``mem_util`` / ``cpu_util`` and
    ``mem_limit_over_alloc`` = sum(mem limits)/node capacity, where >1.0
    means a burst can OOM the node), or ``None`` when a single pod cannot
    fit the shape at all. ``prices`` is accepted for API symmetry with
    :func:`packing_sim`; capacity comes from ``shapes``.
    """
    shapes = shapes or SHAPES
    if shape not in shapes:
        return None
    vcpu, mem = shapes[shape]
    cpu_alloc, mem_alloc = eks_allocatable(vcpu, mem, shape)
    cpu_alloc -= daemon_cpu
    mem_alloc -= daemon_mem
    if cpu_alloc <= 0 or mem_alloc <= 0:
        return None
    node_mem_bytes = mem * GIB
    bins: List[Dict[str, Any]] = []

    def fits(b: Dict[str, Any], p: Dict[str, Any], honor_soft: bool) -> bool:
        if (b["cpu"] + p["cpu_req"] > cpu_alloc
                or b["mem"] + p["mem_req"] > mem_alloc):
            return False
        if p["zone_group"] and b["zone_group"] not in (None, p["zone_group"]):
            return False
        anti = p.get("anti")
        if anti and anti in b["anti"] and (p.get("anti_hard") or honor_soft):
            return False
        return True

    ordered = sorted(pods, key=lambda x: (-x["mem_req"], -x["cpu_req"]))
    for p in ordered:
        if p["mem_req"] > mem_alloc or p["cpu_req"] > cpu_alloc:
            return None
        target = None
        for honor_soft in (True, False):
            for b in bins:
                if fits(b, p, honor_soft):
                    target = b
                    break
            if target:
                break
        if target is None:
            target = {"cpu": 0.0, "mem": 0.0, "mem_lim": 0.0, "pods": [],
                      "zone_group": None, "anti": set()}
            bins.append(target)
        target["cpu"] += p["cpu_req"]
        target["mem"] += p["mem_req"]
        target["mem_lim"] += (p.get("mem_lim") or p["mem_req"])
        target["pods"].append(p["name"])
        if p["zone_group"]:
            target["zone_group"] = p["zone_group"]
        if p.get("anti"):
            target["anti"].add(p["anti"])
    for b in bins:
        b["anti"] = sorted(b["anti"])
        b["mem_gib"] = round(gib(b["mem"]), 1)
        b["cpu_cores"] = round(b["cpu"], 2)
        b["mem_util"] = round(b["mem"] / mem_alloc, 2)
        b["cpu_util"] = round(b["cpu"] / cpu_alloc, 2)
        b["mem_limit_over_alloc"] = round(
            (b["mem_lim"] + daemon_mem) / node_mem_bytes, 2)
        del b["mem"]
        del b["mem_lim"]
    return bins


def _sim_pods(topo: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Non-daemon pods that belong on the observability pool (incl. Mimir
    ingesters that currently run off-pool)."""
    out = []
    nodes = topo.get("nodes", {})
    for p in topo.get("pods", []):
        if p["owner"] == "DaemonSet":
            continue
        node = nodes.get(p["node"], {})
        if node.get("in_pool") or (
                p["stack"] == "mimir" and p["component"] == "ingester"):
            # shallow copy so overrides never mutate the topology
            out.append(dict(p))
    return out


def packing_sim(topo: Dict[str, Any],
                prices: Optional[Dict[str, float]] = None,
                shapes: Optional[Dict[str, Tuple[int, int]]] = None,
                overrides: Optional[Dict[str, Dict[str, float]]] = None
                ) -> Dict[str, Any]:
    """Re-pack the pool's pods into every candidate shape, ranked by $/mo.

    ``overrides`` maps ``"stack:component"`` -> request overrides, e.g.
    ``{"mimir:ingester": {"cpu_req": 1.5}}`` -- used to model the effect of
    right-sizing before committing to a node shape. Returns the current pool
    cost, the ranked candidates, and the bin-pack floor (the cheapest shape
    that fits every pod under the real constraints).
    """
    prices = _merge(PRICE_HR, prices)
    shapes = shapes or SHAPES
    pods = _sim_pods(topo)
    if overrides:
        for p in pods:
            o = overrides.get("%s:%s" % (p["stack"], p["component"]))
            if o:
                p.update(o)
    pool_nodes = [n for n in topo.get("nodes", {}).values()
                  if n.get("in_pool")]
    d_cpu = max([n.get("daemon_cpu_req", 0.0) for n in pool_nodes] or [0.5])
    d_mem = max([n.get("daemon_mem_req", 0.0) for n in pool_nodes]
                or [1.0 * GIB])
    tot_cpu = sum(p["cpu_req"] for p in pods)
    tot_mem = sum(p["mem_req"] for p in pods)

    results = []
    for shape in shapes:
        if shape not in prices:
            continue
        bins = bin_pack(pods, shape, d_cpu, d_mem, prices, shapes)
        if bins is None:
            continue
        results.append({
            "shape": shape, "nodes": len(bins),
            "cost_mo": round(len(bins) * prices[shape] * HOURS_MO),
            "avg_mem_util": round(
                sum(b["mem_util"] for b in bins) / len(bins), 2),
            "max_mem_limit_over_alloc": max(
                b["mem_limit_over_alloc"] for b in bins),
            "instance_category": _family(shape)[0][:1],
            "bins": bins,
        })
    results.sort(key=lambda r: (r["cost_mo"], r["nodes"]))
    current = round(topo.get("pool_cost_mo", 0.0))

    S: Dict[str, Any] = {
        "pods_considered": len(pods),
        "total_cpu_req": round(tot_cpu, 1),
        "total_mem_req_gib": round(gib(tot_mem), 1),
        "daemon_overhead_per_node": {"cpu": round(d_cpu, 2),
                                     "mem_gib": round(gib(d_mem), 2)},
        "current_pool_cost_mo": current,
        "candidates": results[:8],
        "floor": results[0] if results else None,
        "findings": [],
    }
    if results:
        best = results[0]
        safe = _memory_safe_floor(results)
        note = ("Constraints honored: Mimir zone groups never mix on a node; "
                "Loki/Tempo ingesters one-per-node. Zero-request pods pack as "
                "size 0 -- fix requests before trusting this floor.")
        est = {"monthly_usd": max(0, current - best["cost_mo"]),
               "nodes": max(0, topo.get("pool_node_count", 0) - best["nodes"])}
        S["findings"].append(_finding(
            "INFO", "cost",
            "Bin-pack floor: %d x %s ~ $%s/mo vs $%s/mo on the pool today" % (
                best["nodes"], best["shape"], "{:,}".format(best["cost_mo"]),
                "{:,}".format(current)),
            evidence={"floor_shape": best["shape"], "floor_nodes":
                      best["nodes"], "floor_cost_mo": best["cost_mo"],
                      "current_cost_mo": current,
                      "max_mem_limit_over_alloc":
                      best["max_mem_limit_over_alloc"]},
            rationale=note, est_savings=est,
            keeps_performance=True, keeps_durability=True,
            keeps_availability=True))
        if (safe and safe["shape"] != best["shape"]):
            S["findings"].append(_finding(
                "WARN", "cost",
                "Cheapest shape %s has sum(mem limits)/capacity %.2f > 1.0 "
                "(burst-OOM risk); %s is the memory-safe floor" % (
                    best["shape"], best["max_mem_limit_over_alloc"],
                    safe["shape"]),
                evidence={"unsafe_shape": best["shape"],
                          "unsafe_over_alloc":
                          best["max_mem_limit_over_alloc"],
                          "safe_shape": safe["shape"],
                          "safe_cost_mo": safe["cost_mo"]},
                rationale="Packing two ingesters whose memory limits exceed "
                          "node capacity means one burst can OOM the node "
                          "holding both. Prefer the memory-optimized "
                          "(r-class) shape as the honest floor.",
                keeps_performance=True, keeps_durability=True,
                keeps_availability=True))
        S["recommended_floor"] = safe or best
    return S


def _memory_safe_floor(results: List[Dict[str, Any]]
                       ) -> Optional[Dict[str, Any]]:
    """Cheapest candidate whose sum(mem limits)/capacity <= 1.0, preferring
    r-class (memory-optimized) shapes when costs tie."""
    safe = [r for r in results
            if r["max_mem_limit_over_alloc"] <= T["mem_over_alloc_oom"]]
    if not safe:
        return None
    safe.sort(key=lambda r: (r["cost_mo"],
                             0 if r["instance_category"] == "r" else 1,
                             r["nodes"]))
    return safe[0]


# --------------------------------------------------------------------------
# Right-sizing
# --------------------------------------------------------------------------
def _core_gib_usd(prices: Optional[Dict[str, float]] = None
                  ) -> Tuple[float, float]:
    """Rough blended $/core-month and $/GiB-month from a representative
    r-class on-demand node, splitting its cost 50/50 across cpu and memory.
    An ESTIMATE used only to price right-sizing savings."""
    prices = prices or PRICE_HR
    hr = prices.get("r6a.2xlarge", 0.4536)
    vcpu, mem = SHAPES.get("r6a.2xlarge", (8, 64))
    core_mo = (hr * 0.5 / vcpu) * HOURS_MO
    gib_mo = (hr * 0.5 / mem) * HOURS_MO
    return core_mo, gib_mo


def rightsizing(usage_by_component: Dict[str, Dict[str, float]],
                requests_by_component: Dict[str, Dict[str, float]],
                prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Suggest per-pod requests from observed peak usage.

    ``usage_by_component`` maps a component key (e.g. ``"mimir/ingester"``)
    to ``{"cpu_peak": cores, "mem_peak": bytes, "pods": n}`` (24h peak sum
    across its pods). ``requests_by_component`` maps the same key to
    ``{"cpu_req": cores, "mem_req": bytes, "pods": n}``.

    Suggested request = peak x1.5 (CPU) / x1.3 (mem), NEVER below the
    observed peak, so ``keeps_performance`` is always true. Returns per
    component the suggestion, cores / GiB reclaimed and estimated $ saved,
    plus totals. Ingesters are right-sized on *requests* only -- never a CPU
    limit, and their memory request should equal the limit.
    """
    core_usd, gib_usd = _core_gib_usd(prices)
    comps: Dict[str, Any] = {}
    findings: List[Dict[str, Any]] = []
    tot_cpu_saved = tot_mem_saved = tot_usd = 0.0

    keys = set(usage_by_component) | set(requests_by_component)
    for k in sorted(keys):
        u = usage_by_component.get(k, {})
        r = requests_by_component.get(k, {})
        pods = int(r.get("pods") or u.get("pods") or 0)
        if pods <= 0:
            continue
        cpu_peak = float(u.get("cpu_peak") or 0.0)
        mem_peak = float(u.get("mem_peak") or 0.0)
        cpu_req = float(r.get("cpu_req") or 0.0)
        mem_req = float(r.get("mem_req") or 0.0)
        is_ing = k.endswith("/ingester") or "ingester" in k.split("/")[-1]

        cpu_peak_pp = cpu_peak / pods
        mem_peak_pp = mem_peak / pods
        # peak x headroom, and never below the observed per-pod peak.
        sug_cpu_pp = max(cpu_peak_pp * CPU_PEAK_HEADROOM, cpu_peak_pp, 0.1)
        sug_mem_pp = max(mem_peak_pp * MEM_PEAK_HEADROOM, mem_peak_pp)
        sug_cpu_total = sug_cpu_pp * pods
        sug_mem_total = sug_mem_pp * pods

        cpu_saved = max(0.0, cpu_req - sug_cpu_total)
        mem_saved = max(0.0, mem_req - sug_mem_total)
        keeps_perf = (sug_cpu_pp >= cpu_peak_pp
                      and sug_mem_pp >= mem_peak_pp)
        usd = cpu_saved * core_usd + gib(mem_saved) * gib_usd
        tot_cpu_saved += cpu_saved
        tot_mem_saved += mem_saved
        tot_usd += usd

        comps[k] = {
            "pods": pods,
            "cpu_req_cores": round(cpu_req, 2),
            "cpu_peak_cores": round(cpu_peak, 2),
            "cpu_util_peak": (round(pct(cpu_peak, cpu_req), 3)
                              if pct(cpu_peak, cpu_req) is not None else None),
            "suggested_cpu_req_per_pod": round(sug_cpu_pp, 2),
            "cpu_saved_cores": round(cpu_saved, 2),
            "mem_req_gib": round(gib(mem_req), 2),
            "mem_peak_gib": round(gib(mem_peak), 2),
            "mem_util_peak": (round(pct(mem_peak, mem_req), 3)
                              if pct(mem_peak, mem_req) is not None else None),
            "suggested_mem_req_per_pod_gib": round(gib(sug_mem_pp), 2),
            "mem_saved_gib": round(gib(mem_saved), 2),
            "est_usd_saved_mo": round(usd, 2),
            "keeps_performance": keeps_perf,
            "is_ingester": is_ing,
        }

        util = pct(cpu_peak, cpu_req)
        if cpu_req and util is not None and util < T["cpu_util_low"]:
            note = ("Suggest ~%.2f cores/pod (peak x%.1f). Set the request, "
                    "not a CPU limit." % (sug_cpu_pp, CPU_PEAK_HEADROOM))
            if is_ing:
                note += (" NEVER add a CPU limit to an ingester (WAL replay / "
                         "compaction throttling -> OOO + OOM).")
            findings.append(_finding(
                "INFO", "rightsizing",
                "%s: peak CPU is %.0f%% of request (%.1f cores over %d pods)"
                % (k, util * 100, cpu_req, pods),
                evidence={"cpu_req_cores": round(cpu_req, 2),
                          "cpu_peak_cores": round(cpu_peak, 2),
                          "suggested_cpu_req_per_pod": round(sug_cpu_pp, 2)},
                rationale=note,
                est_savings={"compute": "%.1f cores" % cpu_saved,
                             "monthly_usd": round(cpu_saved * core_usd, 2)},
                keeps_performance=keeps_perf, keeps_durability=True,
                keeps_availability=True))
        if is_ing and mem_req and mem_peak and (mem_peak / mem_req) > 0.9:
            findings.append(_finding(
                "WARN", "rightsizing",
                "%s peaks >90%% of its memory request" % k,
                evidence={"mem_req_gib": round(gib(mem_req), 2),
                          "mem_peak_gib": round(gib(mem_peak), 2)},
                rationale="Raise request AND limit together (request == limit "
                          "for ingesters) before it OOMs; do not CPU-limit.",
                keeps_performance=False, keeps_durability=False,
                keeps_availability=False))

    totals = {
        "cores_saved": round(tot_cpu_saved, 2),
        "gib_saved": round(gib(tot_mem_saved), 2),
        "est_usd_saved_mo": round(tot_usd, 2),
        "keeps_performance": True,
        "assumptions": "Suggested request = observed 24h peak x1.5 (cpu) "
                       "/ x1.3 (mem), never below peak. $ splits a "
                       "representative r-class node 50/50 cpu/mem (estimate).",
    }
    return {"components": comps, "totals": totals, "findings": findings}


# --------------------------------------------------------------------------
# Durability audit
# --------------------------------------------------------------------------
def durability_audit(topo: Dict[str, Any],
                     ring_health: Optional[Dict[str, Any]] = None
                     ) -> List[Dict[str, Any]]:
    """Audit the topology for durability / availability hazards.

    Flags: PDBs that allow 0 disruptions, ingesters without a priorityClass,
    CPU-limited ingesters (a Mimir ingester CPU limit is a FAIL), PVC zone
    pins that prevent co-location, an AZ hosting two Mimir logical zones,
    recent OOMs, unhealthy ring members, and zero-request pods on the pool.
    Returns a list of findings (durability findings default to the *current*
    state failing the relevant risk flag).
    """
    findings: List[Dict[str, Any]] = []
    nodes = topo.get("nodes", {})
    pods = topo.get("pods", [])

    # Zero-request pods make the packing math fiction -- flag first.
    for p in pods:
        node = nodes.get(p["node"], {})
        if (node.get("in_pool") and p["owner"] != "DaemonSet"
                and p["mem_req"] == 0):
            findings.append(_finding(
                "FAIL", "packing",
                "%s/%s has NO resource requests on the observability pool"
                % (p["ns"], p["name"]),
                rationale="Karpenter/scheduler treat it as size 0: packing "
                          "math is fiction until requests are set. Measure "
                          "RSS first (see right-sizing).",
                keeps_performance=False, keeps_durability=True,
                keeps_availability=True))

    # Ingesters without a priorityClass or with a CPU limit.
    for p in pods:
        if p["component"] != "ingester":
            continue
        if not p["priority_class"]:
            findings.append(_finding(
                "WARN", "durability",
                "%s/%s has no priorityClassName" % (p["ns"], p["name"]),
                rationale="Ingesters (Loki and Tempo too, not just Mimir) "
                          "must never lose a preemption fight to a querier.",
                keeps_availability=False, keeps_durability=False))
        if p["stack"] == "mimir" and p["cpu_lim"]:
            findings.append(_finding(
                "FAIL", "durability",
                "Mimir ingester %s/%s has a CPU limit (%.2f cores)"
                % (p["ns"], p["name"], p["cpu_lim"]),
                evidence={"cpu_limit_cores": round(p["cpu_lim"], 2)},
                rationale="Never CPU-limit a Mimir ingester: WAL replay / "
                          "compaction throttling causes out-of-order samples "
                          "and OOM. Remove the CPU limit.",
                keeps_performance=False, keeps_durability=False,
                keeps_availability=False))

    # Recent OOMKills.
    ooms = ["%s/%s" % (p["ns"], p["name"]) for p in pods
            if "OOMKilled" in (p.get("last_term") or [])]
    if ooms:
        findings.append(_finding(
            "WARN", "durability", "Recently OOMKilled: " + ", ".join(ooms),
            evidence={"pods": ooms},
            rationale="Memory limit too low or node memory pressure from "
                      "overcommit; raise request==limit, never CPU-limit.",
            keeps_durability=False, keeps_availability=False))

    # AZ hosting two Mimir logical zones -> quorum loss on AZ failure.
    az_load: Dict[str, set] = defaultdict(set)
    for zg, azs in (topo.get("mimir_zone_to_az") or {}).items():
        for az in azs:
            az_load[az].add(zg)
    for az, zgs in az_load.items():
        if len(zgs) >= 2:
            findings.append(_finding(
                "WARN", "durability",
                "AZ %s hosts %d Mimir logical zones: %s"
                % (az, len(zgs), sorted(zgs)),
                evidence={"az": az, "zone_groups": sorted(zgs)},
                rationale="Losing this AZ removes 2 of 3 replicas for those "
                          "series -> write/read quorum loss until it returns "
                          "(a 2-AZ cluster limitation; do not pretend a third "
                          "logical zone is a third AZ).",
                keeps_availability=False, keeps_durability=True))

    # Mimir zone PVCs spanning AZs can never co-locate (blocks packing).
    mz: Dict[str, set] = defaultdict(set)
    for pvc in topo.get("pvcs", []):
        m = re.search(r"ingester-zone-([a-z0-9]+)", pvc["name"])
        if pvc["ns"] == "mimir" and m:
            mz[m.group(1)].add(pvc["zone"])
    for z, azs in mz.items():
        azs = {a for a in azs if a and a != "?"}
        if len(azs) > 1:
            findings.append(_finding(
                "WARN", "packing",
                "Mimir zone-%s PVCs span AZs %s" % (z, sorted(azs)),
                evidence={"zone": z, "azs": sorted(azs)},
                rationale="These ingesters can never share a node; to pack "
                          "them, recreate ONE PVC in the target AZ (RF=3 "
                          "tolerates one replica's local data loss -- one "
                          "at a time, wait for ACTIVE).",
                keeps_durability=True, keeps_availability=True))

    # PDBs that currently allow 0 disruptions.
    for name, pdb in (topo.get("pdbs") or {}).items():
        if "ingester" in name and pdb.get("allowed") == 0:
            findings.append(_finding(
                "WARN", "durability",
                "PDB %s allows 0 disruptions right now" % name,
                evidence={"current": pdb.get("current"),
                          "desired": pdb.get("desired")},
                rationale="A node drain will hang AND Karpenter cannot "
                          "consolidate. Ensure maxUnavailable >= 1 with "
                          "enough healthy replicas.",
                keeps_availability=False, keeps_durability=True))

    # Ring health (injected -- the metric client lives in deepdive).
    for name, state in (ring_health or {}).items():
        findings.append(_finding(
            "FAIL", "durability",
            "Ring member(s) %s not ACTIVE: %s" % (name, state),
            evidence={"ring": name, "state": state},
            keeps_availability=False, keeps_durability=False))

    return findings


# --------------------------------------------------------------------------
# Deep Karpenter analysis
# --------------------------------------------------------------------------
def _family(shape: str) -> Tuple[str, str]:
    """``"r6a.2xlarge"`` -> ``("r6a", "2xlarge")``."""
    fam, _, size = shape.partition(".")
    return fam, size


def _burst_size(size: str) -> str:
    """One instance size larger, for burst headroom."""
    try:
        i = SIZE_LADDER.index(size)
        return SIZE_LADDER[min(i + 1, len(SIZE_LADDER) - 1)]
    except ValueError:
        return size


def _requirement_values(reqs: Optional[List[Dict[str, Any]]], key: str
                        ) -> List[str]:
    for r in reqs or []:
        if r.get("key") == key:
            return [str(v) for v in r.get("values", [])]
    return []


def karpenter_analyze(topo: Dict[str, Any], packing_sim_result: Dict[str, Any],
                      cfg: Optional[Dict[str, Any]] = None,
                      log: Optional[Callable[[str], None]] = None
                      ) -> Dict[str, Any]:
    """Deep Karpenter analysis + a proposed optimized NodePool.

    Reads the current NodePools / NodeClaims (from ``topo["karpenter"]``,
    which :func:`collect_topology` populates, or injected via
    ``cfg["karpenter"]``) and produces findings plus a GENERIC, paste-ready
    proposed NodePool YAML for the observability pool. Every cost lever in
    the proposal keeps availability: stateful ingesters stay on-demand,
    disruption budgets respect PDBs, ``expireAfter`` is finite-with-pacing,
    and NodePool limits cap runaway scale.
    """
    cfg = cfg or {}
    kdata = cfg.get("karpenter") or topo.get("karpenter") or {
        "nodepools": {}, "nodeclaims": []}
    nodepools = kdata.get("nodepools", {}) or {}
    findings: List[Dict[str, Any]] = []

    floor = (packing_sim_result.get("recommended_floor")
             or packing_sim_result.get("floor"))
    if floor:
        fam, size = _family(floor["shape"])
        category = fam[:1]
    else:
        fam, size, category = "r6a", "2xlarge", "r"
    burst = _burst_size(size)

    # --- findings over each current nodepool ---
    for name, np_ in nodepools.items():
        reqs = np_.get("requirements")
        cats = _requirement_values(reqs, "karpenter.k8s.aws/instance-category")
        cap_types = _requirement_values(reqs, "karpenter.sh/capacity-type")
        sizes = _requirement_values(reqs, "karpenter.k8s.aws/instance-size")

        # m-class on a memory-bound pool.
        if cats and "r" not in cats and "m" in cats:
            findings.append(_finding(
                "WARN", "karpenter",
                "NodePool %s is scoped to m-class on a memory-bound obs pool"
                % name,
                evidence={"instance_category": cats},
                rationale="Mimir/Loki ingesters are memory-bound; r-class "
                          "gives more GiB per dollar. Add 'r' to "
                          "instance-category (keep m only if the pool also "
                          "hosts cpu-bound stateless components).",
                keeps_performance=True, keeps_durability=True,
                keeps_availability=True))

        # Spot on a stateful ingester pool.
        pool_has_ingesters = any(
            p["component"] == "ingester"
            and topo.get("nodes", {}).get(p["node"], {}).get("nodepool")
            == name for p in topo.get("pods", []))
        if "spot" in cap_types and (pool_has_ingesters or "ingester" in name):
            findings.append(_finding(
                "FAIL", "karpenter",
                "NodePool %s allows spot but carries stateful ingesters"
                % name,
                evidence={"capacity_type": cap_types},
                rationale="A spot reclaim on an ingester = ring churn / WAL "
                          "loss risk. Pin the ingester pool to on-demand; use "
                          "spot-with-on-demand-fallback only for stateless "
                          "components (queriers, distributors, gateways).",
                keeps_availability=False, keeps_durability=False,
                keeps_performance=True))

        # Consolidation policy.
        disruption = np_.get("disruption") or {}
        policy = disruption.get("consolidationPolicy")
        if policy == "WhenEmpty":
            findings.append(_finding(
                "WARN", "karpenter",
                "NodePool %s consolidates WhenEmpty only" % name,
                evidence={"consolidationPolicy": policy},
                rationale="WhenEmpty leaves half-used nodes running. "
                          "WhenEmptyOrUnderutilized + consolidateAfter is the "
                          "main cost lever; pair it with disruption budgets "
                          "that respect PDBs.",
                keeps_performance=True, keeps_durability=True,
                keeps_availability=True))
        if not disruption.get("budgets"):
            findings.append(_finding(
                "WARN", "karpenter",
                "NodePool %s has no disruption budgets" % name,
                rationale="Without a budget, consolidation/drift can evict "
                          "more than one ingester at once. Add a budget of "
                          "nodes: \"1\" (and windowed budgets for ingester "
                          "pools) so consolidation never breaks quorum.",
                keeps_availability=False, keeps_durability=False,
                keeps_performance=True))

        # expireAfter Never.
        if str(np_.get("expireAfter")).lower() in ("never", "none", ""):
            findings.append(_finding(
                "WARN", "karpenter",
                "NodePool %s has expireAfter=Never" % name,
                evidence={"expireAfter": np_.get("expireAfter")},
                rationale="Nodes are never rotated -> AMI/patch drift "
                          "(security/compliance FAIL on regulated clusters). "
                          "Use a finite expireAfter PLUS a paced rotation "
                          "(drift + budget), never aggressive expiry that "
                          "churns ingesters.",
                keeps_durability=True, keeps_availability=True,
                keeps_performance=True))

        # Missing / exceeded limits.
        lim = np_.get("limits") or {}
        if not lim.get("cpu") and not lim.get("memory"):
            findings.append(_finding(
                "WARN", "karpenter",
                "NodePool %s has no cpu/memory limits" % name,
                rationale="A runaway (bad request, hot loop) can scale the "
                          "pool unbounded. Cap limits at the bin-pack floor "
                          "plus growth headroom.",
                keeps_performance=True, keeps_durability=True,
                keeps_availability=True))
        used = np_.get("resources_in_use") or {}
        if lim.get("cpu") and used.get("cpu") and (
                qty(used["cpu"]) > qty(lim["cpu"])):
            findings.append(_finding(
                "WARN", "karpenter",
                "NodePool %s is over its CPU limit (%s > %s)"
                % (name, used["cpu"], lim["cpu"]),
                rationale="Limits only block new launches; existing excess "
                          "stays until consolidated.",
                keeps_performance=True, keeps_durability=True,
                keeps_availability=True))

        # AZ coverage vs zone-aware ingesters.
        pool_zones = _requirement_values(reqs, "topology.kubernetes.io/zone")
        needed = set(topo.get("zones") or [])
        if pool_zones and needed and not needed.issubset(set(pool_zones)):
            findings.append(_finding(
                "WARN", "karpenter",
                "NodePool %s zone requirement %s misses AZs the zone-aware "
                "ingesters need (%s)" % (name, sorted(pool_zones),
                                         sorted(needed)),
                evidence={"pool_zones": sorted(pool_zones),
                          "needed_zones": sorted(needed)},
                rationale="A pool that cannot place a zone's PVC blocks that "
                          "zone's ingester from scheduling.",
                keeps_availability=False, keeps_durability=True))

    # do-not-disrupt pods that block consolidation.
    dnd = ["%s/%s" % (p["ns"], p["name"]) for p in topo.get("pods", [])
           if p.get("dnd")]
    if dnd:
        findings.append(_finding(
            "INFO", "karpenter",
            "%d pod(s) carry do-not-disrupt and block consolidation" % len(
                dnd),
            evidence={"pods": dnd[:20]},
            rationale="DND is fine for a live ingester DURING a rollout "
                      "window, but permanent DND stacks nodes and defeats "
                      "consolidation. Scope it to the window.",
            keeps_availability=True, keeps_durability=True,
            keeps_performance=True))

    # Weight / multi-pool advice when a general pool coexists.
    if len(nodepools) > 1:
        findings.append(_finding(
            "INFO", "karpenter",
            "Multiple NodePools present -- keep obs backends on the obs pool",
            rationale="Give the obs pool a higher weight and label its nodes; "
                      "add a nodeSelector/taint so memory-optimized nodes are "
                      "not squatted by general pods and obs backends do not "
                      "spill onto general nodes.",
            keeps_performance=True, keeps_durability=True,
            keeps_availability=True))

    zones = topo.get("zones") or []
    proposed_yaml = _proposed_nodepool_yaml(
        category, size, burst, floor, zones, packing_sim_result)
    ec2_note = (
        "EC2NodeClass (separate object, referenced by nodeClassRef above): "
        "use a maintained AMI family/alias (e.g. an EKS-optimized or "
        "Bottlerocket alias) so a finite expireAfter actually patches; select "
        "subnets/security-groups by TAG (never by hard-coded id); attach the "
        "node IAM role by name. For the Graviton (arm64) option, point the "
        "class at an arm64 AMI alias and confirm every chart image has an "
        "arm64 build first -- treat it as an opt-in, not automatic.")

    current = packing_sim_result.get("current_pool_cost_mo", 0)
    floor_cost = floor["cost_mo"] if floor else current
    floor_nodes = floor["nodes"] if floor else topo.get("pool_node_count", 0)
    est = {"monthly_usd": max(0, current - floor_cost),
           "nodes": max(0, topo.get("pool_node_count", 0) - floor_nodes),
           "keeps_availability": True, "keeps_durability": True,
           "keeps_performance": True}

    return {
        "nodepools": [dict(name=n, **v) for n, v in nodepools.items()],
        "nodeclaims": kdata.get("nodeclaims", []),
        "findings": _rank(findings),
        "recommended_shape": {"category": category, "primary_size": size,
                              "burst_size": burst,
                              "example_shape": floor["shape"] if floor
                              else "%s.%s" % (fam, size)},
        "proposed_nodepool_yaml": proposed_yaml,
        "proposed_ec2nodeclass_note": ec2_note,
        "est_savings": est,
    }


def _proposed_nodepool_yaml(category: str, size: str, burst: str,
                            floor: Optional[Dict[str, Any]],
                            zones: List[str],
                            sim: Dict[str, Any]) -> str:
    """Build a GENERIC, commented, paste-ready NodePool proposal.

    Placeholders (never customer values) stand in for names/roles/AMI. Any
    AZ listed is read live from the caller's own cluster (zones), falling
    back to placeholders when unknown. Two pools are emitted: an on-demand
    ingester pool and a spot-with-fallback stateless pool.
    """
    floor_nodes = floor["nodes"] if floor else 3
    vcpu, mem_gib = SHAPES.get(
        "%s6a.%s" % (category, size) if category else "r6a.2xlarge",
        SHAPES.get(floor["shape"], (8, 64)) if floor else (8, 64))
    # Limits = floor footprint + ~50% growth headroom, capped generously.
    lim_cpu = int(max(1, round(floor_nodes * vcpu * 1.5)))
    lim_mem = int(max(1, round(floor_nodes * mem_gib * 1.5)))
    if zones:
        zone_yaml = "\n".join("            - %s" % z for z in zones)
    else:
        zone_yaml = ("            - ${AZ_A}   # your zone-a AZ id\n"
                     "            - ${AZ_B}   # your zone-b AZ id")
    return _NODEPOOL_TEMPLATE.format(
        category=category or "r", size=size, burst=burst,
        lim_cpu=lim_cpu, lim_mem=lim_mem, zone_yaml=zone_yaml)


_NODEPOOL_TEMPLATE = """\
# ---------------------------------------------------------------------------
# PROPOSED Karpenter config for the observability backends. GENERIC template:
# replace every ${{PLACEHOLDER}} with your value. Every cost lever below keeps
# availability -- stateful ingesters stay on-demand and budgets respect PDBs.
# ---------------------------------------------------------------------------
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: ${{OBS_INGESTER_NODEPOOL}}      # stateful Mimir/Loki/Tempo ingesters
spec:
  weight: 50                            # win obs pods over any general pool
  limits:
    cpu: "{lim_cpu}"                    # bin-pack floor + ~50% growth headroom
    memory: {lim_mem}Gi                 # caps runaway scale; raise for growth
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized   # main cost lever
    consolidateAfter: 30m               # reclaim underused nodes, not instant
    budgets:
      - nodes: "1"                      # never evict >1 node (respects PDBs)
      - nodes: "0"                      # optional: freeze during peak windows
        schedule: "0 13 * * mon-fri"    # cron in the node's TZ (example)
        duration: 8h
  template:
    metadata:
      labels:
        workload-type: observability-backend   # nodeSelector target for obs
    spec:
      expireAfter: 720h                 # finite (NOT Never); pair with paced
                                        # drift rotation: patches + repacks
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: ${{OBS_EC2NODECLASS}}
      requirements:
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["{category}"]        # r-class: memory-bound backends
        - key: karpenter.k8s.aws/instance-size
          operator: In
          values: ["{size}", "{burst}"] # floor shape + one size for burst
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]         # ingesters: on-demand ONLY (no spot)
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]             # Graviton opt-in: add "arm64" only
                                        # once every chart image has an arm64
                                        # build (r7g/m7g ~5-10% cheaper)
        - key: topology.kubernetes.io/zone
          operator: In
          values:                       # cover every AZ a zone-aware PVC needs
{zone_yaml}
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: ${{OBS_STATELESS_NODEPOOL}}     # queriers/distributors/gateways only
spec:
  weight: 40
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 1m
    budgets:
      - nodes: "10%"
  template:
    metadata:
      labels:
        workload-type: observability-stateless
    spec:
      expireAfter: 720h
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: ${{OBS_EC2NODECLASS}}
      requirements:
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["{category}", "m"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"] # spot WITH on-demand fallback: safe
                                        # for stateless only
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
"""


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _merge(base: Dict[str, float],
           override: Optional[Dict[str, float]]) -> Dict[str, float]:
    out = dict(base)
    if override:
        out.update(override)
    return out


def analyze(cfg: Optional[Dict[str, Any]] = None,
            prices: Optional[Dict[str, float]] = None,
            log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Run the whole packing analysis and return the packing schema.

    ``cfg`` keys (all optional):
      * ``enabled`` -- attempt kubectl (default True).
      * ``topology`` -- inject a pre-collected topology dict (skips kubectl;
        used by tests and callers that already ran collect_topology).
      * ``namespaces`` / ``pool_selector`` -- collection scope.
      * ``usage`` / ``requests`` -- per-component peak usage / requests for
        right-sizing (usually derived from deepdive self-metrics).
      * ``overrides`` -- request overrides for the packing simulation.
      * ``karpenter`` -- inject nodepool/nodeclaim data.
      * ``ring_health`` -- injected unhealthy ring members.
      * ``prices`` / ``shapes`` -- override the price / shape tables.

    Never raises. When Kubernetes is unavailable it returns a valid schema
    with ``available: False`` and a clear note.
    """
    cfg = cfg or {}
    prices = _merge(PRICE_HR, cfg.get("prices") or prices)
    shapes = cfg.get("shapes") or SHAPES
    result: Dict[str, Any] = {
        "schema": SCHEMA, "generated_at": _utcnow(),
        "generated_by": GENERATED_BY,
        "kubectl_available": kubectl_available(),
        "available": False, "findings": [],
    }

    topo = cfg.get("topology")
    if topo is None:
        if cfg.get("enabled", True):
            topo = collect_topology(
                cfg.get("namespaces"),
                cfg.get("pool_selector", DEFAULT_POOL_SELECTOR), log)
        else:
            topo = {"available": False,
                    "note": "Kubernetes analysis disabled (enabled=False)."}

    if not topo.get("available"):
        result["note"] = topo.get(
            "note", "Kubernetes topology unavailable; packing analysis "
                    "skipped.")
        _emit(log, result["note"])
        return result

    result["available"] = True
    result["topology"] = _topology_summary(topo)

    sim = packing_sim(topo, prices, shapes, cfg.get("overrides"))
    result["packing_sim"] = {k: v for k, v in sim.items() if k != "findings"}

    rs = rightsizing(cfg.get("usage") or {}, cfg.get("requests") or {}, prices)
    result["rightsizing"] = {"components": rs["components"],
                             "totals": rs["totals"]}

    dur = durability_audit(topo, cfg.get("ring_health"))
    result["durability"] = dur

    kar = karpenter_analyze(topo, sim, cfg, log)
    result["karpenter"] = kar

    findings: List[Dict[str, Any]] = []
    findings += sim.get("findings", [])
    findings += rs.get("findings", [])
    findings += dur
    findings += kar.get("findings", [])
    result["findings"] = _rank(findings)

    # Headline: what is saveable without cutting durability/availability/perf.
    floor = sim.get("recommended_floor") or sim.get("floor") or {}
    node_saved = max(
        0, topo.get("pool_node_count", 0) - floor.get("nodes", 0)) \
        if floor else 0
    usd = max(0, sim.get("current_pool_cost_mo", 0)
              - floor.get("cost_mo", sim.get("current_pool_cost_mo", 0)))
    result["est_savings"] = {
        "monthly_usd": round(usd + rs["totals"]["est_usd_saved_mo"]),
        "packing_monthly_usd": usd,
        "rightsizing_monthly_usd": rs["totals"]["est_usd_saved_mo"],
        "cores": rs["totals"]["cores_saved"],
        "nodes": node_saved,
        "keeps_performance": True,
        "keeps_durability": True,
        "keeps_availability": True,
    }
    return result


def _topology_summary(topo: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, JSON-friendly topology summary for the artifact."""
    nodes = topo.get("nodes", {})
    return {
        "pool_selector": topo.get("pool_selector"),
        "namespaces": topo.get("namespaces"),
        "node_count": len(nodes),
        "pool_node_count": topo.get("pool_node_count", 0),
        "pool_cost_mo": round(topo.get("pool_cost_mo", 0.0)),
        "zones": topo.get("zones", []),
        "mimir_zone_to_az": topo.get("mimir_zone_to_az", {}),
        "pod_count": len(topo.get("pods", [])),
        "pvc_count": len(topo.get("pvcs", [])),
        "nodes": [
            {"name": nm, "instance_type": n["instance_type"],
             "zone": n["zone"], "in_pool": n["in_pool"],
             "capacity_type": n.get("capacity_type"),
             "cost_mo": round(n.get("cost_mo", 0.0)),
             "util_cpu_req": (round(n["util_cpu_req"], 2)
                              if n.get("util_cpu_req") is not None else None),
             "util_mem_req": (round(n["util_mem_req"], 2)
                              if n.get("util_mem_req") is not None else None)}
            for nm, n in sorted(nodes.items())],
    }
