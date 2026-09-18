"""AWS cost-anomaly ROOT-CAUSE engine (schema "nr2grafana/rca/v1").

Point this at an AWS **cost spike** -- a pasted human anomaly report, a
Cost Explorer usage-type jump, or an ``ce get-anomalies`` JSON payload --
and it performs a deterministic, evidence-first root-cause analysis by
converging multiple INDEPENDENT read-only evidence sources onto ONE
dominant driver with a measured ``% share``, plus ranked secondaries and
an explicit ``ruled_out`` list. It never asserts a cause from a single
source, and it never fabricates a share: when the quantitative source
(VPC Flow Logs) is missing it degrades to a clearly-flagged hypothesis at
LOW confidence.

The engine encodes the domain reasoning from ARCHITECTURE-1.9 (the binding
contract, sections 0.1-0.9 and the RCA METHODOLOGY steps A-F):

* ``*DataTransfer-Regional-Bytes`` (and ``*InterZone*``) is a cross-AZ
  network transfer -- "Regional" means cross-AZ INSIDE one region, NOT
  cross-region. The service tag (EBS, EC2, ...) is a CUR attribution
  artifact, so the dollars are network, not storage. Mnemonic:
  ``Regional-Bytes == cross-AZ network``.
* Cross-AZ bytes on gRPC port 9095 come from a NON-zone-aware Mimir/Loki
  hash-ring (RF=3 replication + query fan-out) confined to too few AZs.
* The AZ imbalance itself is a Karpenter discovery-subnet gap (subnets
  tagged in fewer AZs than the region offers -> the ring collapses into
  those AZs -> every RF write crosses a zone boundary).
* An NLB with cross-zone load balancing enabled is a common SECONDARY.
* Rule-out logic is mandatory and each ruled-out hypothesis carries its
  own disproving evidence (EBS storage flat, no RDS Multi-AZ replica,
  private-IP-only intra-VPC flows so not NAT/internet egress or EIP).

Nothing here touches AWS directly: it consumes report dicts produced by the
read-only :mod:`nr2grafana.flowlogs` / :mod:`nr2grafana.deepdive` /
:mod:`nr2grafana.packing` / :mod:`nr2grafana.tco` modules (and an optional
``aws`` evidence dict). It NEVER raises; every missing input degrades to a
note and lowers confidence. New Relic is never touched; no secrets are read,
logged, or embedded, and no customer id/AZ is baked into this module.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional

SCHEMA = "nr2grafana/rca/v1"
INCIDENT_SCHEMA = "nr2grafana/rca-incident/v1"
GENERATED_BY = "nr2grafana 1.9.0"

# Hypothesis classes a usage-type maps to (deterministic, per 0.1/Step A).
CROSS_AZ_NETWORK = "CROSS_AZ_NETWORK"
INTERNET_EGRESS = "INTERNET_EGRESS"
STORAGE_GROWTH = "STORAGE_GROWTH"
UNKNOWN = "UNKNOWN"

# gRPC inter-component port for Mimir/Loki (ingest replication + query
# fan-out). Documented so tests and evidence strings share one source.
GRPC_INTER_COMPONENT_PORT = 9095

# Default cost of one round-tripped GB across an AZ boundary: sender pays
# $0.01/GB AND receiver pays $0.01/GB (per 0.1), so a two-way GB is $0.02.
# Overridable via cfg["two_way_gb_cost"].
DEFAULT_TWO_WAY_GB_COST = 0.02

# Evidence-source labels for evidence_convergence (per Step F).
SRC_COST_EXPLORER = "cost-explorer"
SRC_CLOUDTRAIL = "cloudtrail"
SRC_FLOW_LOGS = "vpc-flow-logs"
SRC_EKS = "eks-control-plane"
SRC_LGTM = "lgtm-self-metrics"

_ALL_SOURCES = (SRC_COST_EXPLORER, SRC_CLOUDTRAIL, SRC_FLOW_LOGS,
                SRC_EKS, SRC_LGTM)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _num(val: Any) -> Optional[float]:
    """Coerce ``val`` to float; strip ``$``, commas, ``%``. None on fail."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("$", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _round(val: Optional[float], ndigits: int = 2) -> Optional[float]:
    if val is None:
        return None
    try:
        return round(float(val), ndigits)
    except (TypeError, ValueError):
        return None


def _as_fraction(val: Any) -> Optional[float]:
    """Normalize a share to a 0..1 fraction. Accepts 0.91 or 91 (pct)."""
    n = _num(val)
    if n is None:
        return None
    if n < 0:
        return None
    if n > 1.0:
        n = n / 100.0
    if n > 1.0:
        n = 1.0
    return n


def _pct(frac: Optional[float]) -> Optional[float]:
    if frac is None:
        return None
    return _round(frac * 100.0, 1)


def _is_dict(obj: Any) -> bool:
    return isinstance(obj, dict)


def _first_str(*vals: Any) -> str:
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


# ---------------------------------------------------------------------------
# usage-type classification (Step A)
# ---------------------------------------------------------------------------
def classify_usage_type(usage_type: str) -> str:
    """Map a CUR/CE usage-type string to a hypothesis class.

    ``*DataTransfer-Regional-Bytes`` / ``*InterZone*`` -> CROSS_AZ_NETWORK
    (per 0.1: "Regional" == cross-AZ within one region, service tag is an
    attribution artifact). ``*DataTransfer-Out-Bytes`` -> INTERNET_EGRESS.
    Storage-shaped usage-types -> STORAGE_GROWTH. Else UNKNOWN.
    """
    ut = (usage_type or "").strip()
    low = ut.lower()
    if not low:
        return UNKNOWN
    # Cross-AZ within region. Match FIRST -- the most specific rule.
    if "regional-bytes" in low or "interzone" in low or "inter-az" in low:
        return CROSS_AZ_NETWORK
    if "cross-az" in low or "crossaz" in low:
        return CROSS_AZ_NETWORK
    # Internet egress (data transfer OUT to the public internet).
    if "datatransfer-out-bytes" in low or "-out-bytes" in low:
        return INTERNET_EGRESS
    if "dataxfer-out" in low or "bytesout" in low:
        return INTERNET_EGRESS
    # Storage growth.
    storage_markers = ("volumeusage", "snapshotusage", "storage",
                       "bytehrs", "byte-hrs", "timedstorage")
    for m in storage_markers:
        if m in low:
            return STORAGE_GROWTH
    # Generic data-transfer with no OUT marker -> treat as cross-AZ net.
    if "datatransfer" in low or "data-transfer" in low or "dataxfer" in low:
        return CROSS_AZ_NETWORK
    return UNKNOWN


# ---------------------------------------------------------------------------
# parse_anomaly_report
# ---------------------------------------------------------------------------
_RE_ACCOUNT = re.compile(r"\b(\d{12})\b")
_RE_REGION = re.compile(r"\b([a-z]{2}-[a-z]+-\d)\b")
_RE_USAGE = re.compile(
    r"\b([A-Za-z0-9-]*"
    r"(?:DataTransfer|InterZone|Regional-Bytes|VolumeUsage|SnapshotUsage)"
    r"[A-Za-z0-9-]*)\b", re.I)
_RE_DOLLARS_DAY = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(?:/|\bper\b)?\s*day", re.I)
_RE_DOLLARS = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_RE_GB_DAY = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*GB\s*(?:/|\bper\b)?\s*day", re.I)
_RE_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_RE_STEP_DATE = re.compile(
    r"(?:step[\s-]*change|stepped up|onset|began|started)[^0-9]*"
    r"(\d{4}-\d{2}-\d{2})", re.I)
_RE_SCORE = re.compile(r"score[^0-9]*([0-9]+(?:\.[0-9]+)?)", re.I)
_KNOWN_SERVICES = ("EBS", "EC2", "RDS", "S3", "EKS", "ELB", "VPC",
                   "CloudWatch", "Lambda", "ECS", "DynamoDB")


def _days_between(start: str, end: str) -> int:
    """Whole days spanned by an anomaly interval, inclusive; >=1."""
    fmt = "%Y-%m-%d"
    try:
        s = time.strptime((start or "")[:10], fmt)
        e = time.strptime((end or "")[:10], fmt)
    except (ValueError, TypeError):
        return 1
    d = (time.mktime(e) - time.mktime(s)) / 86400.0
    n = int(round(d)) + 1
    return n if n >= 1 else 1


def _parse_ce_anomaly(anom: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a single GetAnomalies ``Anomalies[]`` element (0.7 fields)."""
    impact = anom.get("Impact") or {}
    total_impact = _num(impact.get("TotalImpact"))
    actual = _num(impact.get("TotalActualSpend"))
    expected = _num(impact.get("TotalExpectedSpend"))
    if total_impact is None and actual is not None and expected is not None:
        total_impact = actual - expected
    start = _first_str(anom.get("AnomalyStartDate"))
    end = _first_str(anom.get("AnomalyEndDate")) or start
    days = _days_between(start, end)

    # RootCauses -> pick the largest contributor for the headline driver.
    roots = anom.get("RootCauses") or []
    best = None
    best_contrib = -1.0
    for rc in roots:
        if not _is_dict(rc):
            continue
        c = _num((rc.get("Impact") or {}).get("Contribution"))
        c = c if c is not None else 0.0
        if c > best_contrib:
            best_contrib = c
            best = rc
    best = best or (roots[0] if roots and _is_dict(roots[0]) else {})

    usage_type = _first_str(best.get("UsageType"))
    service = _first_str(best.get("Service"), anom.get("DimensionValue"))
    region = _first_str(best.get("Region"))
    account = _first_str(best.get("LinkedAccount"),
                         best.get("LinkedAccountName"))
    score = _num((anom.get("AnomalyScore") or {}).get("MaxScore"))
    if score is None:
        score = _num((anom.get("AnomalyScore") or {}).get("CurrentScore"))

    dollars_per_day = None
    if total_impact is not None and days:
        dollars_per_day = total_impact / days

    return {
        "usage_type": usage_type,
        "service": service,
        "region": region,
        "account": account,
        "total_impact": _round(total_impact),
        "days": days,
        "dollars_per_day": dollars_per_day,
        "gb_per_day": None,
        "onset": start or None,
        "step_change": start or None,
        "score": score,
        "anomaly_id": _first_str(anom.get("AnomalyId")),
        "monitor_arn": _first_str(anom.get("MonitorArn")),
        "source": "ce-anomaly",
    }


def _parse_text_report(text: str) -> Dict[str, Any]:
    """Best-effort parse of a pasted human anomaly report."""
    usage_type = ""
    m = _RE_USAGE.search(text)
    if m:
        usage_type = m.group(1)

    service = ""
    if usage_type:
        # Service word immediately preceding the usage-type token, if known.
        idx = text.find(usage_type)
        prefix = text[max(0, idx - 40):idx]
        words = re.findall(r"[A-Za-z]+", prefix)
        if words:
            for cand in reversed(words):
                for known in _KNOWN_SERVICES:
                    if cand.lower() == known.lower():
                        service = known
                        break
                if service:
                    break
    if not service:
        for known in _KNOWN_SERVICES:
            if re.search(r"\b" + re.escape(known) + r"\b", text):
                service = known
                break

    dollars_per_day = None
    m = _RE_DOLLARS_DAY.search(text)
    if m:
        dollars_per_day = _num(m.group(1))
    gb_per_day = None
    m = _RE_GB_DAY.search(text)
    if m:
        gb_per_day = _num(m.group(1))

    step_change = None
    m = _RE_STEP_DATE.search(text)
    if m:
        step_change = m.group(1)
    else:
        m = _RE_DATE.search(text)
        if m:
            step_change = m.group(1)

    account = ""
    m = _RE_ACCOUNT.search(text)
    if m:
        account = m.group(1)
    region = ""
    m = _RE_REGION.search(text)
    if m:
        region = m.group(1)
    score = None
    m = _RE_SCORE.search(text)
    if m:
        score = _num(m.group(1))

    return {
        "usage_type": usage_type,
        "service": service,
        "region": region,
        "account": account,
        "total_impact": None,
        "days": None,
        "dollars_per_day": dollars_per_day,
        "gb_per_day": gb_per_day,
        "onset": step_change,
        "step_change": step_change,
        "score": score,
        "anomaly_id": "",
        "monitor_arn": "",
        "source": "pasted-report",
    }


def parse_anomaly_report(text_or_json: Any) -> Dict[str, Any]:
    """Parse a pasted report OR a Cost Explorer anomaly into an incident.

    Accepts, in priority order:

    * a ``dict`` shaped like a GetAnomalies response (``{"Anomalies": [..]}``)
      or a single ``Anomalies[]`` element (0.7 field names, exactly);
    * a JSON string of either of the above;
    * a plain-text human report like the reference worked example.

    Returns a normalized incident dict (schema
    "nr2grafana/rca-incident/v1") with ``usage_type``, ``service``,
    ``account``, ``region``, ``dollars_per_day``, ``gb_per_day``, ``onset``,
    ``step_change``, ``score``, the derived ``hypothesis_class`` and the
    ``assumptions`` behind any $->GB conversion. Never raises.
    """
    parsed: Optional[Dict[str, Any]] = None

    obj: Any = text_or_json
    if isinstance(text_or_json, str):
        stripped = text_or_json.strip()
        if stripped[:1] in ("{", "["):
            try:
                obj = json.loads(stripped)
            except (ValueError, TypeError):
                obj = text_or_json  # fall through to text parsing

    if _is_dict(obj):
        anom = obj
        anoms = obj.get("Anomalies")
        if isinstance(anoms, list) and anoms:
            first = None
            for a in anoms:
                if _is_dict(a):
                    first = a
                    break
            anom = first if first is not None else {}
        parsed = _parse_ce_anomaly(anom)
    elif isinstance(obj, list):
        first = None
        for a in obj:
            if _is_dict(a):
                first = a
                break
        parsed = _parse_ce_anomaly(first or {})
    elif isinstance(text_or_json, str):
        parsed = _parse_text_report(text_or_json)
    else:
        parsed = _parse_text_report(str(text_or_json or ""))

    parsed["schema"] = INCIDENT_SCHEMA
    parsed["hypothesis_class"] = classify_usage_type(parsed.get("usage_type"))
    parsed["assumptions"] = []
    return parsed


# ---------------------------------------------------------------------------
# defensive views over the (sibling-owned) input reports
# ---------------------------------------------------------------------------
def _flowlogs_view(flowlogs: Any) -> Dict[str, Any]:
    """Extract the quantitative facts from a flowlogs/v1 report.

    Reads several tolerant key spellings so a small canned dict or the real
    :mod:`nr2grafana.flowlogs` output both work. Returns a normalized view;
    ``present`` is False when no usable flow-log data was supplied.
    """
    view = {
        "present": False,
        "drivers": [],  # [{driver, port, gb_per_day, share(frac), workload}]
        "dominant": None,
        "secondaries": [],
        "cross_az_gb_per_day": None,
        "step_change_date": None,
        "azs": [],
        "imbalanced": None,
        "private_only": None,
        "region": None,
    }
    if not _is_dict(flowlogs):
        return view

    raw_drivers = (flowlogs.get("drivers") or flowlogs.get("by_port")
                   or flowlogs.get("top_ports") or [])
    drivers: List[Dict[str, Any]] = []
    for d in raw_drivers:
        if not _is_dict(d):
            continue
        # Honor the KEY: a *_pct key is always a percentage (so 1.0 means
        # 1%, not 100%); a bare "share" is a fraction unless it is >1.
        if d.get("share_pct") is not None:
            n = _num(d.get("share_pct"))
            share = None if n is None else max(0.0, min(1.0, n / 100.0))
        elif d.get("pct") is not None:
            n = _num(d.get("pct"))
            share = None if n is None else max(0.0, min(1.0, n / 100.0))
        else:
            share = _as_fraction(d.get("share"))
        drivers.append({
            "driver": _first_str(d.get("driver"), d.get("name"),
                                 d.get("label")),
            "port": d.get("port") if d.get("port") is not None
            else d.get("dstPort"),
            "gb_per_day": _num(d.get("gb_per_day") if d.get("gb_per_day")
                               is not None else d.get("gb")),
            "share": share,
            "workload": _first_str(d.get("workload"), d.get("component"),
                                   d.get("app")),
        })
    # Rank by share then bytes; the top one is dominant.
    drivers.sort(
        key=lambda x: (x["share"] if x["share"] is not None else -1.0,
                       x["gb_per_day"] if x["gb_per_day"] is not None
                       else -1.0),
        reverse=True)
    view["drivers"] = drivers
    if drivers:
        view["dominant"] = drivers[0]
        view["secondaries"] = drivers[1:]
        view["present"] = True

    view["cross_az_gb_per_day"] = _num(
        flowlogs.get("cross_az_gb_per_day")
        if flowlogs.get("cross_az_gb_per_day") is not None
        else flowlogs.get("total_cross_az_gb")
        if flowlogs.get("total_cross_az_gb") is not None
        else flowlogs.get("gb_per_day"))
    view["step_change_date"] = _first_str(
        flowlogs.get("step_change_date"), flowlogs.get("step_change"),
        flowlogs.get("onset")) or None
    azs = (flowlogs.get("azs_observed") or flowlogs.get("azs")
           or flowlogs.get("az_pair") or [])
    if isinstance(azs, (list, tuple)):
        view["azs"] = [str(a) for a in azs if a]
    imb = flowlogs.get("imbalanced")
    if imb is None and view["azs"]:
        region_azs = flowlogs.get("region_azs") or []
        if isinstance(region_azs, (list, tuple)) and region_azs:
            imb = len(view["azs"]) < len(region_azs)
    view["imbalanced"] = imb
    view["private_only"] = flowlogs.get("private_only")
    view["region"] = _first_str(flowlogs.get("region")) or None
    if view["cross_az_gb_per_day"] is not None or view["step_change_date"]:
        view["present"] = True
    return view


def _deepdive_view(deepdive: Any) -> Dict[str, Any]:
    """Extract ring/zone-awareness facts from a deepdive/v1 report.

    Prefers explicit hint keys; falls back to scanning ``findings`` for a
    non-zone-aware ring / cross-AZ network finding.
    """
    view = {
        "present": False,
        "zone_aware": None,
        "replication_factor": None,
        "ring_zones": [],
        "workload": "",
        "confirms_cross_az": False,
    }
    if not _is_dict(deepdive):
        return view
    view["present"] = True

    if "zone_aware" in deepdive:
        view["zone_aware"] = deepdive.get("zone_aware")
    view["replication_factor"] = _num(
        deepdive.get("replication_factor")
        if deepdive.get("replication_factor") is not None
        else deepdive.get("rf"))
    rz = deepdive.get("ring_zones") or deepdive.get("zones") or []
    if isinstance(rz, (list, tuple)):
        view["ring_zones"] = [str(z) for z in rz if z]
    view["workload"] = _first_str(deepdive.get("workload"),
                                  deepdive.get("component"))

    findings = deepdive.get("findings") or []
    for f in findings:
        if not _is_dict(f):
            continue
        blob = " ".join(str(f.get(k, "")) for k in
                        ("title", "rationale", "area")).lower()
        ev = f.get("evidence") or {}
        evblob = json.dumps(ev).lower() if _is_dict(ev) else str(ev).lower()
        blob = blob + " " + evblob
        if ("zone-aware" in blob or "zone aware" in blob
                or "zone_aware" in blob):
            if ("non-zone" in blob or "not zone" in blob
                    or "false" in blob or "disabled" in blob):
                if view["zone_aware"] is None:
                    view["zone_aware"] = False
            view["confirms_cross_az"] = True
        if "cross-az" in blob or "cross az" in blob or "9095" in blob:
            view["confirms_cross_az"] = True
        if view["replication_factor"] is None and _is_dict(ev):
            for k in ("rf", "replication_factor", "replication"):
                if k in ev:
                    view["replication_factor"] = _num(ev.get(k))
                    break
        if not view["workload"]:
            for k in ("workload", "component", "app"):
                if _is_dict(ev) and ev.get(k):
                    view["workload"] = str(ev.get(k))
                    break
    return view


def _packing_view(packing: Any, k8s: Any) -> Dict[str, Any]:
    """Extract the Karpenter subnet/AZ-gap facts (per 0.5)."""
    view = {
        "present": False,
        "discovery_azs": [],
        "region_azs": [],
        "missing_azs": [],
        "subnet_az_gap": None,
        "workload": "",
    }
    for src in (packing, k8s):
        if not _is_dict(src):
            continue
        view["present"] = True
        da = (src.get("karpenter_discovery_azs")
              or src.get("discovery_azs") or src.get("node_azs") or [])
        if isinstance(da, (list, tuple)) and da and not view["discovery_azs"]:
            view["discovery_azs"] = [str(a) for a in da if a]
        ra = (src.get("region_azs") or src.get("available_azs") or [])
        if isinstance(ra, (list, tuple)) and ra and not view["region_azs"]:
            view["region_azs"] = [str(a) for a in ra if a]
        ma = src.get("missing_azs") or []
        if isinstance(ma, (list, tuple)) and ma and not view["missing_azs"]:
            view["missing_azs"] = [str(a) for a in ma if a]
        if view["subnet_az_gap"] is None and "subnet_az_gap" in src:
            view["subnet_az_gap"] = src.get("subnet_az_gap")
        if not view["workload"]:
            view["workload"] = _first_str(src.get("workload"),
                                          src.get("component"))
        # Scan findings for a subnet-discovery gap finding.
        for f in (src.get("findings") or []):
            if not _is_dict(f):
                continue
            blob = " ".join(str(f.get(k, "")) for k in
                            ("title", "rationale")).lower()
            if ("discovery subnet" in blob or "subnet discovery" in blob
                    or "karpenter" in blob and "az" in blob):
                if ("gap" in blob or "missing" in blob or "only" in blob
                        or "2 of 3" in blob):
                    if view["subnet_az_gap"] is None:
                        view["subnet_az_gap"] = True

    if (view["subnet_az_gap"] is None and view["discovery_azs"]
            and view["region_azs"]):
        view["subnet_az_gap"] = (
            len(view["discovery_azs"]) < len(view["region_azs"]))
    if (not view["missing_azs"] and view["discovery_azs"]
            and view["region_azs"]):
        have = set(view["discovery_azs"])
        view["missing_azs"] = [a for a in view["region_azs"]
                               if a not in have]
    return view


def _tco_view(tco: Any) -> Dict[str, Any]:
    """Extract the storage-flatness rule-out evidence from a tco/v1 report."""
    view = {"present": False, "storage_flat": None}
    if not _is_dict(tco):
        return view
    view["present"] = True
    if "storage_flat" in tco:
        view["storage_flat"] = tco.get("storage_flat")
    return view


def _aws_view(aws: Any) -> Dict[str, Any]:
    """Extract corroborating evidence from an ``aws`` evidence dict.

    ``aws`` is a plain dict of PRE-FETCHED read-only evidence (the caller
    ran the read-only awscost helpers and handed us the results): e.g.
    ``{"cloudtrail_events": [...], "volumes_flat": True,
    "snapshots_flat": True, "rds_multi_az": False,
    "rds_enis_in_flows": False}``. Anything else is ignored (we never call
    AWS from here; the tool is proposal/analysis only).
    """
    view = {
        "present": False,
        "cloudtrail_events": [],
        "volumes_flat": None,
        "snapshots_flat": None,
        "rds_multi_az": None,
        "rds_enis_in_flows": None,
    }
    if not _is_dict(aws):
        return view
    view["present"] = True
    evts = aws.get("cloudtrail_events") or aws.get("events") or []
    if isinstance(evts, (list, tuple)):
        view["cloudtrail_events"] = list(evts)
    for k in ("volumes_flat", "snapshots_flat", "rds_multi_az",
              "rds_enis_in_flows"):
        if k in aws:
            view[k] = aws.get(k)
    return view


# ---------------------------------------------------------------------------
# rule-out logic (Step E)
# ---------------------------------------------------------------------------
def _build_ruled_out(incident: Dict[str, Any], fv: Dict[str, Any],
                     tv: Dict[str, Any], av: Dict[str, Any],
                     notes: List[str]) -> List[Dict[str, Any]]:
    """Return ruled_out[]; each entry carries its DISPROVING evidence.

    Only hypotheses with actual disproving evidence are ruled out. When the
    evidence is absent, a note records that we could not rule it out (rather
    than silently claiming we did).
    """
    ruled: List[Dict[str, Any]] = []
    service = (incident.get("service") or "").upper()

    # 1) Storage growth (EBS volume/snapshot). Disproved by flat storage.
    storage_flat = tv.get("storage_flat")
    vols_flat = av.get("volumes_flat")
    snaps_flat = av.get("snapshots_flat")
    if storage_flat is True or (vols_flat is True and snaps_flat is True):
        parts = []
        if storage_flat is True:
            parts.append("Cost Explorer storage usage-type is flat across "
                         "the step-change")
        if vols_flat is True and snaps_flat is True:
            parts.append("ec2 describe-volumes/snapshots show count and "
                         "size flat across the step-change")
        ruled.append({
            "hypothesis": "%s storage growth (volume/snapshot)"
                          % (service or "EBS"),
            "evidence": "; ".join(parts) + " -- the "
            "DataTransfer-Regional-Bytes dollars are cross-AZ network, not "
            "storage (the service tag is a CUR attribution artifact).",
        })
    elif incident.get("hypothesis_class") == CROSS_AZ_NETWORK:
        notes.append("Could not positively rule out storage growth: no flat "
                     "storage evidence (pass tco.storage_flat or aws "
                     "volumes_flat/snapshots_flat).")

    # 2) Cross-region transfer. Disproved when both endpoints same region.
    if fv.get("present") and fv.get("private_only") is True:
        region = _first_str(fv.get("region"), incident.get("region"))
        ruled.append({
            "hypothesis": "Cross-region data transfer",
            "evidence": "VPC Flow Logs show both endpoints in the same "
            "region%s (private intra-VPC IPs) -- 'Regional-Bytes' is "
            "cross-AZ within one region, not cross-region."
            % (" (%s)" % region if region else ""),
        })

    # 3) NAT / internet egress. Disproved by private-IP-only intra-VPC flows.
    if fv.get("present") and fv.get("private_only") is True:
        ruled.append({
            "hypothesis": "NAT gateway / internet egress",
            "evidence": "Dominant flows are private-IP to private-IP "
            "intra-VPC (not via a NAT gateway, not public) -- not egress.",
        })
        # 4) Same-AZ over public/EIP address (the 0.1 edge case).
        ruled.append({
            "hypothesis": "Same-AZ traffic billed via public/EIP address",
            "evidence": "Flows use private RFC1918 IPs, so the charge is "
            "genuinely cross-AZ, not same-AZ traffic over a public/EIP "
            "address.",
        })

    # 5) RDS cross-AZ (Multi-AZ) replica. Disproved by no Multi-AZ / no ENIs.
    rds_multi = av.get("rds_multi_az")
    rds_enis = av.get("rds_enis_in_flows")
    if rds_multi is False or rds_enis is False:
        parts = []
        if rds_multi is False:
            parts.append("no RDS Multi-AZ replica is configured")
        if rds_enis is False:
            parts.append("no RDS ENIs appear in the dominant cross-AZ flows")
        ruled.append({
            "hypothesis": "RDS cross-AZ (Multi-AZ) replication",
            "evidence": ("; ".join(parts) + ".").capitalize(),
        })

    return ruled


# ---------------------------------------------------------------------------
# convergence + confidence (Step F)
# ---------------------------------------------------------------------------
def _collect_convergence(incident: Dict[str, Any], fv: Dict[str, Any],
                         dv: Dict[str, Any], pv: Dict[str, Any],
                         av: Dict[str, Any]) -> List[str]:
    """Which independent evidence sources AGREE on the dominant driver."""
    conv: List[str] = []
    # Cost Explorer always frames the incident.
    if incident.get("usage_type") or incident.get("dollars_per_day"):
        conv.append(SRC_COST_EXPLORER)
    if fv.get("present") and fv.get("dominant"):
        conv.append(SRC_FLOW_LOGS)
    if dv.get("present") and (dv.get("zone_aware") is False
                             or dv.get("confirms_cross_az")
                             or dv.get("replication_factor")):
        conv.append(SRC_LGTM)
    if pv.get("present") and (pv.get("subnet_az_gap") is True
                             or pv.get("missing_azs")
                             or pv.get("discovery_azs")):
        conv.append(SRC_EKS)
    if av.get("present") and av.get("cloudtrail_events"):
        conv.append(SRC_CLOUDTRAIL)
    # Preserve canonical ordering, dedupe.
    return [s for s in _ALL_SOURCES if s in conv]


def _score_confidence(convergence: List[str], have_share: bool) -> str:
    """Confidence rises with independent agreeing sources; no share -> LOW."""
    if not have_share:
        return "LOW"
    n = len(convergence)
    if n <= 1:
        return "LOW"
    if n >= 4:
        return "HIGH"
    return "MEDIUM"


# ---------------------------------------------------------------------------
# dominant / secondary cause construction
# ---------------------------------------------------------------------------
def _fmt_gb(gb: Optional[float]) -> str:
    if gb is None:
        return "an unknown volume of"
    return "~%s GB/day" % ("{:,.0f}".format(gb) if gb >= 100
                           else "{:,.1f}".format(gb))


def _cross_az_dominant(incident, fv, dv, pv, av):
    """Build the dominant cross-AZ cause with converged evidence."""
    dom = fv.get("dominant") or {}
    share = dom.get("share")
    port = dom.get("port")
    gb = dom.get("gb_per_day")
    rf = dv.get("replication_factor")
    rf_txt = "RF=%d" % int(rf) if rf else "RF=3 (default)"
    workload = _first_str(dom.get("workload"), dv.get("workload"),
                          pv.get("workload"),
                          "Mimir/Loki ingesters, distributors and queriers")
    n_azs = len(fv.get("azs") or [])
    az_txt = ("%d imbalanced AZs" % n_azs) if n_azs else "too few AZs"

    is_grpc = str(port) == str(GRPC_INTER_COMPONENT_PORT)
    port_txt = ("gRPC port %s" % port) if port is not None else "the gRPC port"
    summary = (
        "Non-zone-aware distributed hash-ring in the Grafana LGTM stack "
        "(%s) -- %s replication plus query fan-out over %s -- confined to "
        "%s, so every replicated write and query fan-out crosses an AZ "
        "boundary and bills as cross-AZ transfer." % (
            workload, rf_txt, port_txt, az_txt))

    evidence: List[str] = []
    ut = incident.get("usage_type") or ""
    evidence.append(
        "Cost Explorer: usage-type '%s' classifies as cross-AZ network "
        "transfer (Regional-Bytes == cross-AZ within one region); the "
        "service tag is a CUR attribution artifact, not storage." % ut)
    if fv.get("present") and dom:
        evidence.append(
            "VPC Flow Logs: %s carries %s%% of cross-AZ bytes (%s)%s%s." % (
                port_txt,
                "{:.0f}".format(_pct(share)) if share is not None else "?",
                _fmt_gb(gb),
                (", the dominant driver") if share and share >= 0.5 else "",
                (", stepping up on %s" % fv.get("step_change_date"))
                if fv.get("step_change_date") else ""))
    if dv.get("present"):
        za = dv.get("zone_aware")
        za_txt = ("the ring is NON-zone-aware" if za is False
                  else "the ring topology")
        grpc_note = (" gRPC %s is Mimir/Loki inter-component replication + "
                     "query fan-out." % port) if is_grpc else ""
        evidence.append(
            "LGTM self-metrics: %s, %s; replicas of a series/stream land in "
            "arbitrary zones.%s" % (za_txt, rf_txt, grpc_note))
    az_gap = pv.get("subnet_az_gap") or pv.get("missing_azs")
    if pv.get("present") and az_gap:
        miss = pv.get("missing_azs") or []
        disc = pv.get("discovery_azs") or []
        reg = pv.get("region_azs") or []
        evidence.append(
            "EKS/EC2 topology: Karpenter discovery subnets exist in only "
            "%s of %s AZs%s, so nodes cannot launch in the missing AZ and "
            "the ring collapses into fewer AZs -- concentrating cross-AZ "
            "replication traffic." % (
                len(disc) if disc else "fewer",
                len(reg) if reg else "the region's",
                (" (missing %s)" % ", ".join(miss)) if miss else ""))
    if av.get("present") and av.get("cloudtrail_events"):
        ev0 = av["cloudtrail_events"][0]
        name = (ev0.get("EventName") or ev0.get("event_name")
                or "a scale-up/config change") if _is_dict(ev0) else \
            "a scale-up/config change"
        evidence.append(
            "CloudTrail: %s near the step-change date correlates a Karpenter "
            "scale-up / config change with the cost step-change." % name)

    return {
        "driver": _first_str(dom.get("driver"),
                             "cross-AZ ring replication + query fan-out"),
        "share": _round(share, 4) if share is not None else None,
        "share_pct": _pct(share),
        "port": port,
        "summary": summary,
        "evidence": evidence,
    }


def _generic_dominant(incident, fv):
    """Dominant cause for non-cross-AZ classes or when flow logs are thin."""
    dom = fv.get("dominant") or {}
    share = dom.get("share")
    hc = incident.get("hypothesis_class")
    if hc == INTERNET_EGRESS:
        summary = ("Internet egress (DataTransfer-Out) -- traffic leaving "
                   "the VPC to the public internet, likely via a NAT gateway "
                   "or public endpoint. Confirm the top talkers and move "
                   "chatty egress behind a VPC endpoint / cache.")
    elif hc == STORAGE_GROWTH:
        summary = ("Storage growth -- the usage-type is storage-shaped "
                   "(volume/snapshot byte-hours). Confirm volume/snapshot "
                   "count and size growth across the step-change.")
    else:
        summary = ("Unclassified cost driver. Insufficient signal to name a "
                   "single dominant cause; gather VPC Flow Logs / usage-type "
                   "detail to converge on a driver.")
    evidence = []
    ut = incident.get("usage_type") or ""
    if ut:
        evidence.append("Cost Explorer: usage-type '%s' (class %s)."
                        % (ut, hc))
    if dom:
        evidence.append(
            "VPC Flow Logs: top driver '%s'%s." % (
                _first_str(dom.get("driver"), "port %s" % dom.get("port")),
                (" carries %s%% of bytes" % "{:.0f}".format(_pct(share)))
                if share is not None else ""))
    return {
        "driver": _first_str(dom.get("driver"), "unresolved"),
        "share": _round(share, 4) if share is not None else None,
        "share_pct": _pct(share),
        "port": dom.get("port"),
        "summary": summary,
        "evidence": evidence,
    }


def _build_secondaries(fv: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rank secondary drivers from the flow-log data (e.g. cross-zone NLB)."""
    out: List[Dict[str, Any]] = []
    for d in fv.get("secondaries") or []:
        driver = _first_str(d.get("driver"),
                            "port %s" % d.get("port") if d.get("port")
                            is not None else "secondary flow")
        low = driver.lower()
        port = d.get("port")
        if "nlb" in low or "load balancer" in low or "cross-zone" in low:
            summary = ("An NLB with cross-zone load balancing ENABLED routes "
                       "each zonal node's traffic to targets in ALL AZs, "
                       "incurring cross-AZ charges (NLB cross-zone is a "
                       "secondary contributor).")
        else:
            summary = ("Secondary cross-AZ flow on %s." % (
                "port %s" % port if port is not None else "this path"))
        out.append({
            "driver": driver,
            "share": _round(d.get("share"), 4)
            if d.get("share") is not None else None,
            "share_pct": _pct(d.get("share")),
            "port": port,
            "summary": summary,
            "evidence": ["VPC Flow Logs: %s%s." % (
                driver,
                " carries %s%% of cross-AZ bytes"
                % "{:.0f}".format(_pct(d.get("share")))
                if d.get("share") is not None else "")],
        })
    return out


# ---------------------------------------------------------------------------
# analyze (Steps A-F)
# ---------------------------------------------------------------------------
def analyze(anomaly: Any, aws: Any = None, flowlogs: Any = None,
            deepdive: Any = None, packing: Any = None, tco: Any = None,
            k8s: Any = None, cfg: Optional[Dict[str, Any]] = None,
            log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Root-cause a cost anomaly -> schema "nr2grafana/rca/v1".

    ``anomaly`` is a pasted report, a CE anomaly (dict/JSON), or an already
    parsed incident dict (from :func:`parse_anomaly_report`). The remaining
    arguments are OPTIONAL pre-computed read-only report dicts:

    * ``flowlogs`` -- the quantitative source; supplies driver byte-shares
      (schema nr2grafana/flowlogs/v1).
    * ``deepdive`` -- LGTM self-metrics (ring zone-awareness, RF).
    * ``packing`` / ``k8s`` -- Karpenter/EC2 topology (AZ-gap).
    * ``tco`` -- storage-flatness rule-out.
    * ``aws`` -- a dict of PRE-FETCHED read-only corroborating evidence
      (cloudtrail_events, volumes_flat, snapshots_flat, rds_*). This module
      never calls AWS itself.

    Never raises: every missing input degrades to a note and lowers
    confidence. The dominant/secondary ``share`` comes ONLY from measured
    flow-log bytes; with no flow logs the result is a clearly-flagged
    hypothesis at LOW confidence (no fabricated %).
    """
    emit = log or (lambda m: None)
    cfg = cfg or {}
    notes: List[str] = []

    # --- Step A: frame the incident -------------------------------------
    if _is_dict(anomaly) and anomaly.get("schema") == INCIDENT_SCHEMA:
        incident_raw = dict(anomaly)
    else:
        incident_raw = parse_anomaly_report(anomaly)
    if not incident_raw.get("hypothesis_class"):
        incident_raw["hypothesis_class"] = classify_usage_type(
            incident_raw.get("usage_type"))

    two_way = _num(cfg.get("two_way_gb_cost")) or DEFAULT_TWO_WAY_GB_COST
    assumptions = list(incident_raw.get("assumptions") or [])
    dollars = _num(incident_raw.get("dollars_per_day"))
    gb = _num(incident_raw.get("gb_per_day"))
    if (gb is None and dollars is not None and two_way > 0
            and incident_raw.get("hypothesis_class") == CROSS_AZ_NETWORK):
        gb = dollars / two_way
        assumptions.append(
            "Converted $%.2f/day to %s GB/day at $%.2f per round-tripped "
            "(two-way) GB -- sender AND receiver each pay $%.2f/GB for "
            "cross-AZ transfer (per AWS data-transfer pricing)." % (
                dollars, "{:,.0f}".format(gb), two_way, two_way / 2.0))
    if incident_raw.get("hypothesis_class") == CROSS_AZ_NETWORK:
        assumptions.append(
            "'%s' treated as cross-AZ NETWORK transfer, not storage: "
            "Regional-Bytes == cross-AZ within one region; the service tag "
            "is a CUR classification artifact." % (
                incident_raw.get("usage_type") or "the usage-type"))

    incident = {
        "usage_type": incident_raw.get("usage_type") or "",
        "service": incident_raw.get("service") or "",
        "account": incident_raw.get("account") or "",
        "region": incident_raw.get("region") or "",
        "dollars_per_day": _round(dollars),
        "gb_per_day": _round(gb, 1),
        "onset": incident_raw.get("onset"),
        "step_change": incident_raw.get("step_change"),
        "score": _round(incident_raw.get("score"), 3),
        "hypothesis_class": incident_raw.get("hypothesis_class"),
        "total_impact": incident_raw.get("total_impact"),
        "days": incident_raw.get("days"),
        "source": incident_raw.get("source"),
        "anomaly_id": incident_raw.get("anomaly_id", ""),
        "assumptions": assumptions,
    }

    # --- Steps B-D: read the evidence views -----------------------------
    fv = _flowlogs_view(flowlogs)
    dv = _deepdive_view(deepdive)
    pv = _packing_view(packing, k8s)
    tv = _tco_view(tco)
    av = _aws_view(aws)

    if not fv.get("present"):
        notes.append("No VPC Flow Logs supplied: cannot measure a driver "
                     "byte-share. Result is a hypothesis only (confidence "
                     "capped at LOW). Provide a flowlogs report to converge.")
    # Reconcile step-change/GB between incident and flow logs.
    if fv.get("step_change_date") and not incident["step_change"]:
        incident["step_change"] = fv["step_change_date"]
    if fv.get("cross_az_gb_per_day") is not None and incident["gb_per_day"] \
            is None:
        incident["gb_per_day"] = _round(fv["cross_az_gb_per_day"], 1)

    # --- build the cause ------------------------------------------------
    hc = incident["hypothesis_class"]
    if hc == CROSS_AZ_NETWORK and fv.get("present"):
        dominant = _cross_az_dominant(incident, fv, dv, pv, av)
    else:
        dominant = _generic_dominant(incident, fv)
    secondaries = _build_secondaries(fv)
    ruled_out = _build_ruled_out(incident, fv, tv, av, notes)

    # --- Step F: converge & score ---------------------------------------
    convergence = _collect_convergence(incident, fv, dv, pv, av)
    have_share = dominant.get("share") is not None
    confidence = _score_confidence(convergence, have_share)

    if len(convergence) <= 1 and have_share:
        notes.append("Single-source claim: confidence capped at LOW until a "
                     "second independent source agrees.")

    emit("rca: %s driver=%s share=%s confidence=%s sources=%d" % (
        hc, dominant.get("driver"),
        dominant.get("share_pct"), confidence, len(convergence)))

    return {
        "schema": SCHEMA,
        "generated_by": GENERATED_BY,
        "generated_at": _now(),
        "incident": incident,
        "cause": {
            "dominant": dominant,
            "secondary": secondaries,
            "ruled_out": ruled_out,
        },
        "evidence_convergence": convergence,
        "confidence": confidence,
        "notes": notes,
    }
