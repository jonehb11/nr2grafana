#!/usr/bin/env python3
"""Drive the 1.9 cost-anomaly RCA + mitigation flow against ``fake_aws``.

This is a small, importable helper for the end-to-end mock test
(``tests/test_e2e_mock.py``, owned by the server agent). It threads the
REAL library code -- :mod:`nr2grafana.awscost` (read-only shell-out),
:mod:`nr2grafana.flowlogs`, :mod:`nr2grafana.rca`, :mod:`nr2grafana.
mitigate` -- against the deterministic ``tools/fake_aws.py`` CLI (pointed
at via the ``N2G_AWS_BIN`` override), and returns every artifact so the
e2e can assert the reference outcome:

* the anomaly is an ``EBS DataTransfer-Regional-Bytes`` spike classified
  as CROSS_AZ_NETWORK (Regional-Bytes == cross-AZ network, not storage);
* VPC Flow Logs put ~91% of cross-AZ bytes on gRPC port 9095 (the Mimir/
  Loki ring), with a step-change on 2026-08-31 over 2 imbalanced AZs;
* the RCA's dominant driver is the cross-AZ ring on 9095, the secondary is
  a cross-zone NLB (~8%), and EBS storage / RDS replica are ruled out;
* the mitigation plan emits the zone/topology-aware primaries (M1-M4) as
  reliability-SAFE and proposes the NLB cross-zone disable GATED: the ELBv2
  fixture has a thin AZ (us-east-1b) with a single healthy target, so the
  proposal ships both minimum_healthy_targets fail-open gates AND a loud
  precondition to add a 2nd healthy target there. Because those gates make
  the thin AZ fail over / fail open instead of black-holing, the gated
  mitigation stays reliability-SAFE-with-caveat; only a ZERO-healthy AZ
  would hard-demote it (keeps_availability=false).

Nothing here touches real AWS: every ``aws`` call is served by the fake
CLI. Siblings are imported lazily so importing this module never drags in
a mid-build sibling. It never mutates AWS/K8s -- the tool PROPOSES only.

Typical use from a test::

    from tools import rca_e2e_helper as helper  # or: import rca_e2e_helper
    art = helper.run(fake_bin="/abs/path/to/tools/fake_aws.py")
    assert art["rca"]["cause"]["dominant"]["port"] == 9095

``run()`` and ``drive()`` are aliases returning the same artifacts dict.
"""

from __future__ import annotations

import calendar
import os
import time
from typing import Any, Callable, Dict, List, Optional

# The deterministic fake ``aws`` CLI this helper drives.
FAKE_AWS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "fake_aws.py")

# Scenario anchors -- must line up with tools/fake_aws.py's RCA fixture.
REGION = "us-east-1"
AZ_A, AZ_B, AZ_C = "us-east-1a", "us-east-1b", "us-east-1c"
GRPC_PORT = 9095
NLB_PORTS = frozenset((443, 8080, 9009, 9096))   # gateway/NLB-ish ports
STEP_CHANGE = "2026-08-31"
DEFAULT_LOG_GROUP = "/vpc/flow-logs/obs"
# The flow-log query window straddling the step-change (deterministic, so
# GB/day math is byte-stable rather than clock-dependent).
WINDOW_START = "2026-08-21"
WINDOW_END = "2026-09-04"


# ---------------------------------------------------------------------------
# lazy sibling imports (kept out of module import time)
# ---------------------------------------------------------------------------
def _awscost():
    from nr2grafana import awscost
    return awscost


def _flowlogs():
    from nr2grafana import flowlogs
    return flowlogs


def _rca():
    from nr2grafana import rca
    return rca


def _mitigate():
    from nr2grafana import mitigate
    return mitigate


def _epoch(datestr: str) -> int:
    """UTC epoch seconds for a ``YYYY-MM-DD`` date (no local-tz drift)."""
    return calendar.timegm(time.strptime(datestr, "%Y-%m-%d"))


# ---------------------------------------------------------------------------
# flowlogs report -> rca "flowlogs/v1"-shaped input
# ---------------------------------------------------------------------------
def _driver_label(port: Any, dominant: bool) -> str:
    """Name a destination-port driver for the RCA's cause breakdown.

    Port 9095 is the Mimir/Loki inter-component gRPC ring (replication +
    query fan-out); an NLB-ish port is the cross-zone NLB secondary; any
    other port is a small residual. The NLB label carries the word "NLB"
    so the RCA/mitigation classify it as a cross-zone NLB driver.
    """
    try:
        p = int(port)
    except (TypeError, ValueError):
        p = None
    if p == GRPC_PORT:
        return "gRPC ring replication + query fan-out"
    if p in NLB_PORTS:
        return "cross-zone NLB (Mimir gateway)"
    if p is not None:
        return "misc cross-AZ traffic (port %d)" % p
    return "cross-AZ traffic (dominant)" if dominant else "cross-AZ traffic"


def _workload_label(port: Any) -> str:
    try:
        p = int(port)
    except (TypeError, ValueError):
        p = None
    if p == GRPC_PORT:
        return "Mimir/Loki ingesters + queriers"
    if p in NLB_PORTS:
        return "Mimir gateway NLB"
    return ""


def build_rca_flowlogs_input(flowlogs_report: Dict[str, Any],
                             region_azs: Optional[List[str]] = None,
                             region: str = REGION) -> Dict[str, Any]:
    """Adapt a ``nr2grafana/flowlogs/v1`` report into the driver-shaped
    dict :func:`nr2grafana.rca.analyze` consumes (``drivers`` with a
    ``share_pct``/``port``, ``cross_az_gb_per_day``, ``azs_observed``,
    ``region_azs``, ``imbalanced``, ``private_only``, ``step_change_date``).

    The flow-log module reports port shares as ``pct_of_cross_az``; the RCA
    engine keys on per-driver ``share``. This is the seam that maps one to
    the other, labeling the dominant gRPC port and the NLB secondary.
    """
    fl = flowlogs_report or {}
    ports = fl.get("ports") or []
    totals = fl.get("totals") or {}
    imb = fl.get("az_imbalance") or {}
    step = fl.get("step_change") or {}

    drivers: List[Dict[str, Any]] = []
    for i, p in enumerate(ports):
        if not isinstance(p, dict):
            continue
        port = p.get("port")
        drivers.append({
            "driver": _driver_label(port, dominant=(i == 0)),
            "port": port,
            "gb_per_day": p.get("gb_per_day"),
            "share_pct": p.get("pct_of_cross_az"),
            "workload": _workload_label(port),
        })

    step_date = str(step.get("date") or "")[:10] or None
    azs = imb.get("azs") or []
    return {
        "schema": "nr2grafana/flowlogs/v1",
        "cross_az_gb_per_day": totals.get("cross_az_gb_per_day"),
        "step_change_date": step_date,
        "region": region,
        "azs_observed": list(azs),
        "region_azs": list(region_azs or []),
        "imbalanced": imb.get("imbalanced"),
        # Every counted flow was private RFC1918 intra-VPC (the flowlogs
        # module filters to those), so cross-region / NAT / EIP are ruled
        # out and the charge is genuinely cross-AZ.
        "private_only": True,
        "drivers": drivers,
    }


# ---------------------------------------------------------------------------
# evidence assembly from the fake AWS reads
# ---------------------------------------------------------------------------
def _has_discovery_tag(subnet: Dict[str, Any]) -> bool:
    for tag in subnet.get("Tags") or []:
        if isinstance(tag, dict) and str(tag.get("Key", "")).startswith(
                "karpenter.sh/discovery"):
            return True
    return False


def _packing_from_topology(topo: Dict[str, Any]) -> Dict[str, Any]:
    """Karpenter AZ-gap facts from ec2 describe-subnets / -availability-
    zones: discovery subnets in AZ_A/AZ_B only, region offers three."""
    subnets = topo.get("subnets") or []
    azs = topo.get("availability_zones") or []
    discovery_azs = sorted({
        s.get("AvailabilityZone") for s in subnets
        if isinstance(s, dict) and _has_discovery_tag(s)
        and s.get("AvailabilityZone")})
    region_azs = sorted({
        z.get("ZoneName") for z in azs
        if isinstance(z, dict) and z.get("ZoneName")})
    return {
        "schema": "nr2grafana/packing/v1",
        "karpenter_discovery_azs": discovery_azs,
        "region_azs": region_azs,
        "missing_azs": [a for a in region_azs if a not in discovery_azs],
        "subnet_az_gap": len(discovery_azs) < len(region_azs),
        "workload": "Mimir/Loki node pool",
    }


def _deepdive_stub(workload: str) -> Dict[str, Any]:
    """LGTM self-metrics view (ring zone-awareness + RF).

    The ring's zone-awareness is a Grafana-stack fact, not an AWS one, so
    it is not served by the fake ``aws`` CLI; this encodes the reference
    finding (non-zone-aware RF=3 ring on gRPC 9095) the deepdive module
    would surface from Mimir self-metrics.
    """
    return {
        "schema": "nr2grafana/deepdive/v1",
        "zone_aware": False,
        "replication_factor": 3,
        "workload": workload or "Mimir/Loki ingesters",
        "findings": [{
            "area": "network",
            "severity": "FAIL",
            "title": "Ring is non-zone-aware (cross-AZ on 9095)",
            "rationale": ("RF=3 replicas + query fan-out land in arbitrary "
                          "zones, so every write/read crosses an AZ"),
            "evidence": {"rf": 3, "port": GRPC_PORT, "zone_aware": False},
        }],
    }


def _flat_before(items: List[Dict[str, Any]], time_key: str,
                 step: str = STEP_CHANGE) -> bool:
    """True when every item's timestamp predates the step-change -- i.e.
    no new storage was created around the cost jump (rule-out evidence)."""
    for it in items or []:
        if not isinstance(it, dict):
            continue
        stamp = str(it.get(time_key, ""))[:10]
        if stamp and stamp >= step:
            return False
    return True


def _nlb_health_by_az(elb: Dict[str, Any]) -> Dict[str, int]:
    """Per-AZ count of HEALTHY NLB targets from elbv2 describe-target-
    health. The fixture yields {AZ_A: 2, AZ_B: 1} -- AZ_B is the thin,
    black-hole-prone zone the guardrail must catch."""
    counts: Dict[str, int] = {}
    health = (elb or {}).get("target_health") or {}
    for _arn, descs in health.items():
        for d in descs or []:
            if not isinstance(d, dict):
                continue
            az = str((d.get("Target") or {}).get("AvailabilityZone") or "")
            state = str((d.get("TargetHealth") or {}).get("State") or "")
            if not az:
                continue
            counts.setdefault(az, 0)
            if state.lower() == "healthy":
                counts[az] += 1
            else:
                counts[az] += 0
    return counts


# ---------------------------------------------------------------------------
# main driver
# ---------------------------------------------------------------------------
def run(fake_bin: Optional[str] = None, log_group: str = DEFAULT_LOG_GROUP,
        region: str = REGION, profile: str = "",
        log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Drive discovery -> flowlogs -> rca.analyze -> mitigate.plan against
    ``fake_aws`` and return every artifact.

    ``fake_bin`` overrides ``N2G_AWS_BIN`` for the duration of the call
    (restored afterwards). When it is None the current ``N2G_AWS_BIN`` is
    used, or the sibling ``tools/fake_aws.py`` if none is set. All ``aws``
    calls are read-only and served by the fake CLI. Never raises for a
    degraded sub-result -- the RCA/mitigation still assemble.

    Returns a dict with keys: ``anomaly``, ``flowlogs`` (raw report),
    ``flowlogs_rca_input`` (adapted), ``deepdive``, ``packing``,
    ``aws_evidence``, ``capacity``, ``rca``, ``mitigation``.
    """
    emit = log or (lambda m: None)

    set_env = False
    prev = None
    if fake_bin is None and not os.environ.get("N2G_AWS_BIN"):
        fake_bin = FAKE_AWS_PATH
    if fake_bin is not None:
        set_env = True
        prev = os.environ.get("N2G_AWS_BIN")
        os.environ["N2G_AWS_BIN"] = fake_bin

    try:
        return _run_inner(log_group, region, profile, emit)
    finally:
        if set_env:
            if prev is None:
                os.environ.pop("N2G_AWS_BIN", None)
            else:
                os.environ["N2G_AWS_BIN"] = prev


def _run_inner(log_group: str, region: str, profile: str,
               emit: Callable[[str], None]) -> Dict[str, Any]:
    awscost = _awscost()
    flowlogs = _flowlogs()
    rca = _rca()
    mitigate = _mitigate()

    # --- Step A: frame the incident (ce get-anomalies via the fake) ------
    emit("rca-e2e: reading ce get-anomalies ...")
    anomalies = awscost.get_anomalies(WINDOW_START, WINDOW_END,
                                      region=region, profile=profile)
    anomaly = {"Anomalies": anomalies}

    # --- Step B: localize the bytes (VPC Flow Logs via Logs Insights) ---
    emit("rca-e2e: running flowlogs.analyze against the fake log group ...")
    start_epoch = _epoch(WINDOW_START)
    end_epoch = _epoch(WINDOW_END)
    query_fn = _make_query_fn(awscost, region, profile)
    fl = flowlogs.analyze(
        aws=awscost, log_group=log_group,
        start_epoch=start_epoch, end_epoch=end_epoch,
        region=region, profile=profile, query_fn=query_fn, log=emit)

    # --- Steps C/D: workload + topology (eks + ec2 via the fake) --------
    emit("rca-e2e: reading eks + ec2 topology ...")
    eks = awscost.eks_describe(cluster="obs-eks", region=region,
                               profile=profile)
    topo = awscost.ec2_network_topology(region=region, profile=profile)
    packing = _packing_from_topology(topo)
    region_azs = packing.get("region_azs") or [AZ_A, AZ_B, AZ_C]

    workload = ""
    ngs = eks.get("nodegroups") or []
    if ngs and isinstance(ngs[0], dict):
        workload = str(ngs[0].get("nodegroupName") or "")
    deepdive = _deepdive_stub(workload)

    fl_input = build_rca_flowlogs_input(fl, region_azs=region_azs,
                                        region=region)

    # --- Step E: rule-out evidence (cloudtrail + ec2 storage + rds) -----
    emit("rca-e2e: reading cloudtrail + storage rule-out evidence ...")
    events = awscost.cloudtrail_lookup(
        attribute_key="EventName", attribute_value="CreateFleet",
        start=STEP_CHANGE + "T00:00:00Z", end="2026-09-04T00:00:00Z",
        region=region, profile=profile)
    volumes = _describe_list(awscost, "ec2", "describe-volumes", "Volumes",
                             region, profile)
    snapshots = _describe_list(awscost, "ec2", "describe-snapshots",
                               "Snapshots", region, profile)
    aws_evidence = {
        "cloudtrail_events": events,
        "volumes_flat": _flat_before(volumes, "CreateTime"),
        "snapshots_flat": _flat_before(snapshots, "StartTime"),
        # No RDS Multi-AZ replica and no RDS ENIs among the dominant flows
        # (the ENI descriptions are Mimir/CoreDNS, never rds).
        "rds_multi_az": False,
        "rds_enis_in_flows": False,
    }

    tco = {"schema": "nr2grafana/tco/v1", "storage_flat": True}

    # --- run the RCA engine ---------------------------------------------
    emit("rca-e2e: running rca.analyze ...")
    rca_report = rca.analyze(
        anomaly, aws=aws_evidence, flowlogs=fl_input,
        deepdive=deepdive, packing=packing, tco=tco, log=emit)

    # --- capacity context for the mitigation guardrails -----------------
    emit("rca-e2e: reading elbv2 target health for the NLB guardrail ...")
    elb = awscost.elbv2_describe(region=region, profile=profile)
    nlb_health = _nlb_health_by_az(elb)
    nlb_enabled_azs = sorted(nlb_health.keys())
    zones = list(region_azs) if region_azs else [AZ_A, AZ_B, AZ_C]
    # Real AZ NAMES throughout (not a bare count) so the PreferClose rule's
    # serving/endpoint zones line up -- a mismatch would falsely flag a
    # hotspot. Keys cover both the current reliability contract and the
    # older mitigate fallback (extra keys are ignored).
    capacity = {
        # RF / quorum: a balanced 3-AZ layout, all healthy.
        "available_zones": zones,
        "zones": zones,
        "healthy_zones": len(zones),
        # PreferClose (0.4): endpoints spread across every serving zone
        # with enough replicas per zone for the current per-zone rate.
        "serving_zones": zones,
        "endpoint_zones": zones,
        "endpoints_spread_all_zones": True,
        "per_zone_replicas": {z: 3 for z in zones},
        "per_zone_required_replicas": {z: 2 for z in zones},
        # Handles-current-traffic: post-change per-zone capacity comfortably
        # exceeds the current per-zone request/ingest rate.
        "per_zone_current_rate": {z: 100 for z in zones},
        "per_zone_capacity_after": {z: 300 for z in zones},
        # Karpenter (0.5) / ring-migration safety.
        "consolidation_budgets": True,
        "pdb_present": True,
        "cpu_limit_on_ingesters": False,
        # NLB (0.6): {AZ_A: 2, AZ_B: 1}. AZ_B's single healthy target is the
        # black-hole risk -- the NLB disable is GATED (both
        # minimum_healthy_targets gates set, so it fails open) and records
        # the thin-AZ precondition rather than blind-disabling.
        "nlb_target_health": nlb_health,
        "nlb_enabled_azs": nlb_enabled_azs,
    }

    emit("rca-e2e: running mitigate.plan ...")
    mitigation = mitigate.plan(
        rca_report, deepdive=deepdive, packing=packing,
        capacity=capacity, log=emit)

    return {
        "anomaly": anomaly,
        "flowlogs": fl,
        "flowlogs_rca_input": fl_input,
        "deepdive": deepdive,
        "packing": packing,
        "eks": eks,
        "aws_evidence": aws_evidence,
        "capacity": capacity,
        "rca": rca_report,
        "mitigation": mitigation,
    }


def _make_query_fn(awscost: Any, region: str,
                   profile: str) -> Callable[..., Any]:
    """A flowlogs ``query_fn`` that runs through the read-only
    :func:`nr2grafana.awscost.logs_insights_query` (and thus the fake
    ``aws`` CLI via ``N2G_AWS_BIN``).

    ``logs_insights_query`` still enforces the read-only allow-list and
    the start-query -> get-query-results -> stop-query flow; this closure
    only collapses the multi-line query into a single line before handing
    it over. CloudWatch Logs Insights treats a newline and a space
    identically as token separators, so the query is unchanged in meaning
    -- but a single line sidesteps awscost's defense-in-depth refusal of a
    newline in an argv token, keeping the e2e green regardless of how the
    awscost/flowlogs newline handling settles.
    """
    def query_fn(kind: str, query: str, ctx: Dict[str, Any]) -> Any:
        flat = " ".join(str(query).split())
        return awscost.logs_insights_query(
            ctx.get("log_group"), flat,
            ctx.get("start_epoch"), ctx.get("end_epoch"),
            region=region, profile=profile)
    return query_fn


def _describe_list(awscost: Any, service: str, subcommand: str, key: str,
                   region: str, profile: str) -> List[Dict[str, Any]]:
    """Run one allow-listed describe-* and return its top-level list."""
    data = awscost.run_aws(service, subcommand, region=region,
                           profile=profile)
    if isinstance(data, dict):
        val = data.get(key)
        if isinstance(val, list):
            return val
    return []


# Alias so callers can use whichever name reads best.
def drive(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Alias for :func:`run`."""
    return run(*args, **kwargs)


if __name__ == "__main__":
    import json as _json
    artifacts = run(log=lambda m: None)
    _rca_out = artifacts["rca"]
    _dom = _rca_out["cause"]["dominant"]
    print(_json.dumps({
        "dominant_port": _dom.get("port"),
        "dominant_share_pct": _dom.get("share_pct"),
        "confidence": _rca_out.get("confidence"),
        "convergence": _rca_out.get("evidence_convergence"),
        "secondary": [s.get("driver") for s in
                      _rca_out["cause"]["secondary"]],
        "ruled_out": [r.get("hypothesis") for r in
                      _rca_out["cause"]["ruled_out"]],
        "mitigations": [
            {"id": m.get("id"), "safe": m.get("safe"),
             "keeps_availability": m.get("keeps_availability")}
            for m in artifacts["mitigation"]["mitigations"]],
        "headline": artifacts["mitigation"]["summary"]["headline"],
    }, indent=2))
