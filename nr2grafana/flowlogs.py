"""VPC Flow Logs cross-AZ byte attribution (schema
"nr2grafana/flowlogs/v1").

This module answers ONE narrow question for the cost-anomaly RCA engine
(:mod:`nr2grafana.rca`): of the bytes crossing Availability-Zone
boundaries *inside one region*, which **destination port**, which **AZ
pair**, and which **workload** dominate, how many **GB/day** is that, and
**when did it step up**?  Cross-AZ (inter-AZ, same-region) transfer bills
at $0.01/GB in each direction, so a round-tripped GB effectively costs
$0.02/GB; a ``*DataTransfer-Regional-Bytes`` usage-type is this traffic,
NOT storage (see ARCHITECTURE-1.9 s0.1).  Port 9095 is the Mimir/Loki
gRPC inter-component port (ingest replication + query fan-out), the usual
culprit when a distributed hash-ring is confined to too few zones.

Everything is derived from CloudWatch Logs Insights queries over the VPC
Flow Logs log group.  Those queries only READ existing log data -- they
create no infrastructure and write nothing (s0.8) -- and are run through
:func:`nr2grafana.awscost.logs_insights_query`, which enforces the
read-only allow-list.  This module never runs ``aws`` itself.

:func:`analyze` NEVER raises.  When no flow-log group is configured, when
the query runner is unavailable, or when a query fails, it degrades to a
clearly-flagged report with ``available=False`` and an actionable
``note`` -- so the RCA engine can fall back to a LOW-confidence
hypothesis rather than fabricate a percentage.

AWS credentials are never read, logged, or embedded here; the allow-listed
``aws`` CLI resolves them from the local chain.  No customer values are
emitted; the report is measurements only.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

SCHEMA = "nr2grafana/flowlogs/v1"
GENERATED_BY = "nr2grafana 1.9.0"

# The Logs Insights query templates divide bytes by 1073741824 (2**30),
# so a "gb" figure from a query is GiB; we use the same divisor when a row
# reports raw ``bytes`` so the two paths agree. Stated as an assumption in
# the report. (Cross-AZ transfer is billed in decimal GB; the tiny GiB/GB
# difference is left to the cost layer -- this module reports measured
# volume, not dollars.)
BYTES_PER_GB = float(2 ** 30)

# Default query bounds so a runaway query can never scan unbounded data.
DEFAULT_FLOW_LIMIT = 50
DEFAULT_PORT_LIMIT = 20
DEFAULT_DAYS = 14
DEFAULT_EXPECTED_AZS = 3

# RFC1918 private ranges: the CIDR filter that keeps the analysis to
# intra-VPC private-IP flows (rules out NAT / internet egress and the
# same-AZ-over-public-IP edge case; see s0.1 / Step E).
PRIVATE_CIDRS = ("10.", "172.16.", "192.168.")

# Well-known LGTM / observability destination ports, for labeling the
# dominant driver. gRPC 9095 = Mimir/Loki inter-component (the classic
# cross-AZ ring-replication + query-fan-out driver).
PORT_LABELS: Dict[int, str] = {
    9095: "Mimir/Loki gRPC (inter-component: replication + query fan-out)",
    9096: "Mimir/Loki gRPC (alt)",
    9009: "Mimir HTTP (ingest/query)",
    3100: "Loki HTTP",
    3200: "Tempo",
    4317: "OTLP gRPC",
    4318: "OTLP HTTP",
    7946: "memberlist gossip (ring membership)",
    8080: "HTTP",
    443: "HTTPS",
    9042: "Cassandra",
    6379: "Redis",
    5432: "PostgreSQL",
    3306: "MySQL",
}


def port_label(port: Any) -> str:
    """Human label for a destination port (empty when unknown)."""
    p = _int(port)
    if p is None:
        return ""
    return PORT_LABELS.get(p, "")


# ---------------------------------------------------------------------------
# Small, tolerant coercion helpers. Logs Insights returns string values;
# field names vary by log format ("srcAddr" vs "srcaddr" vs "src_addr"),
# so every read goes through a case/format-insensitive getter.
# ---------------------------------------------------------------------------

def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _r(value: float, ndigits: int = 3) -> float:
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return 0.0


def _norm_key(key: Any) -> str:
    """Lowercase and strip separators/``@`` so field aliases collapse."""
    return "".join(
        c for c in str(key).lower() if c.isalnum())


# Canonical field -> the raw names that may carry it (any case/separator).
_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "src_addr": ("srcaddr", "pktsrcaddr", "sourceaddress", "source"),
    "dst_addr": ("dstaddr", "pktdstaddr", "destaddr", "destinationaddress",
                 "destination"),
    "dst_port": ("dstport", "destport", "destinationport", "port"),
    "src_az": ("srcaz", "srcazid", "sourceaz", "srcavailabilityzone"),
    "dst_az": ("dstaz", "dstazid", "destaz", "destavailabilityzone"),
    "az": ("azid", "az", "availabilityzone"),
    "bytes": ("bytes", "b", "sumbytes", "totalbytes"),
    "gb": ("gb", "gib", "gbytes"),
    "day": ("day", "bin", "timestamp", "time", "bin1d", "date"),
}


def _row_get(row: Dict[str, Any], canonical: str) -> Any:
    """Fetch ``canonical`` from a row, tolerating name/case variants."""
    if not isinstance(row, dict):
        return None
    # exact-ish first
    normed = {_norm_key(k): v for k, v in row.items()}
    for alias in _FIELD_ALIASES.get(canonical, (canonical,)):
        if alias in normed:
            return normed[alias]
    # canonical itself (already normalized form)
    can = _norm_key(canonical)
    if can in normed:
        return normed[can]
    return None


def _row_gb(row: Dict[str, Any]) -> float:
    """GB for a row: prefer a pre-divided ``gb``, else ``bytes``/2**30."""
    gb = _row_get(row, "gb")
    if gb is not None and gb != "":
        return _num(gb)
    return _num(_row_get(row, "bytes")) / BYTES_PER_GB


def normalize_rows(result: Any) -> List[Dict[str, Any]]:
    """Coerce a Logs Insights result into a list of ``{field: value}``.

    Accepts, in order of preference:
      * the raw CloudWatch ``get-query-results`` shape -- a dict with a
        ``results`` (or ``rows``) key whose value is a list of rows, each
        row a list of ``{"field","value"}`` pairs;
      * an already-normalized list of ``{field: value}`` dicts;
      * a bare list of raw rows (list-of-list-of-{field,value}).
    Anything unrecognized yields ``[]``. Never raises.
    """
    if result is None:
        return []
    if isinstance(result, dict):
        rows = result.get("results")
        if rows is None:
            rows = result.get("rows")
        if rows is None:
            return []
        result = rows
    if not isinstance(result, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in result:
        if isinstance(row, dict):
            # already {field: value}
            out.append(row)
        elif isinstance(row, list):
            d: Dict[str, Any] = {}
            for cell in row:
                if isinstance(cell, dict) and "field" in cell:
                    d[str(cell.get("field"))] = cell.get("value")
            if d:
                out.append(d)
    return out


# ---------------------------------------------------------------------------
# Logs Insights query builders (GENERIC placeholders; per ARCHITECTURE
# s0.9). The az fields are used when the custom log format carries az-id;
# otherwise AZ is resolved in Python from an ENI map. Callers inject
# <FLOW_LOG_GROUP>, <START_EPOCH>, <END_EPOCH> via the runner, not the
# string, so these stay templates.
# ---------------------------------------------------------------------------

def _cidr_filter(field: str) -> str:
    parts = ["%s like /^%s/" % (field, c.replace(".", "\\."))
             for c in PRIVATE_CIDRS]
    return "(" + " or ".join(parts) + ")"


def build_flows_query(limit: int = DEFAULT_FLOW_LIMIT) -> str:
    """Top talker flows by src/dst addr + dst port (+ az when present)."""
    return (
        "fields srcAddr, dstAddr, dstPort, srcAz, dstAz, bytes\n"
        "| filter action = 'ACCEPT'\n"
        "| filter %s and %s\n"
        "| stats sum(bytes) as bytes by srcAddr, dstAddr, dstPort, "
        "srcAz, dstAz\n"
        "| sort bytes desc\n"
        "| limit %d"
        % (_cidr_filter("srcAddr"), _cidr_filter("dstAddr"), int(limit)))


def build_ports_query(limit: int = DEFAULT_PORT_LIMIT) -> str:
    """Cross-AZ bytes by destination port (dominant-driver discovery)."""
    return (
        "fields dstPort, srcAz, dstAz, bytes\n"
        "| filter action = 'ACCEPT'\n"
        "| filter %s and %s\n"
        "| stats sum(bytes)/1073741824 as gb by dstPort, srcAz, dstAz\n"
        "| sort gb desc\n"
        "| limit %d"
        % (_cidr_filter("srcAddr"), _cidr_filter("dstAddr"), int(limit)))


def build_daily_query(limit: int = 400) -> str:
    """Daily cross-AZ bytes (bucketed by day) for step-change detection."""
    return (
        "fields srcAddr, dstAddr, srcAz, dstAz, bytes\n"
        "| filter action = 'ACCEPT'\n"
        "| filter %s and %s\n"
        "| stats sum(bytes)/1073741824 as gb by bin(1d) as day, "
        "srcAz, dstAz\n"
        "| sort day asc\n"
        "| limit %d"
        % (_cidr_filter("srcAddr"), _cidr_filter("dstAddr"), int(limit)))


# ---------------------------------------------------------------------------
# AZ / workload attribution.
# ---------------------------------------------------------------------------

def _lookup_ip(eni_map: Optional[Dict[str, Any]], addr: Any) -> Dict[str, Any]:
    if not isinstance(eni_map, dict) or addr is None:
        return {}
    ent = eni_map.get(str(addr))
    return ent if isinstance(ent, dict) else {}


def _az_of(row: Dict[str, Any], which: str,
           eni_map: Optional[Dict[str, Any]]) -> str:
    """AZ for the src/dst side of a flow: explicit az field, else ENI map."""
    az = _row_get(row, "src_az" if which == "src" else "dst_az")
    if az is None or az == "":
        # single-az column (rare) as a last resort for the src side
        az = _row_get(row, "az") if which == "src" else None
    if az is None or az == "":
        addr = _row_get(row, "src_addr" if which == "src" else "dst_addr")
        ent = _lookup_ip(eni_map, addr)
        az = ent.get("az") if ent else None
    return str(az) if az not in (None, "") else ""


def _workload_of(addr: Any, eni_map: Optional[Dict[str, Any]]) -> str:
    ent = _lookup_ip(eni_map, addr)
    if not ent:
        return ""
    for key in ("workload", "node", "pod", "description", "eni"):
        val = ent.get(key)
        if val:
            return str(val)
    return ""


# ---------------------------------------------------------------------------
# Step-change detection over a daily GB series.
# ---------------------------------------------------------------------------

def _mean(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def detect_step_change(daily: List[Tuple[str, float]],
                       min_ratio: float = 1.5) -> Dict[str, Any]:
    """Find the day cross-AZ GB/day steps up.

    ``daily`` is ``[(date, gb), ...]`` sorted ascending. Returns the split
    date maximizing the before/after jump among splits whose after-mean is
    at least ``min_ratio`` x the before-mean (a rise from ~0 always
    qualifies). ``detected`` is False when the series is too short or flat.
    """
    pts = [(d, _num(g)) for d, g in daily if d]
    n = len(pts)
    empty = {
        "detected": False,
        "date": "",
        "before_gb_per_day": 0.0,
        "after_gb_per_day": 0.0,
        "ratio": 0.0,
    }
    if n < 4:
        return empty
    gbs = [g for _, g in pts]
    best: Optional[Dict[str, Any]] = None
    for i in range(1, n):
        before = gbs[:i]
        after = gbs[i:]
        bm = _mean(before)
        am = _mean(after)
        if am <= bm:
            continue
        if bm <= 0.0:
            ratio = float("inf") if am > 0.0 else 0.0
        else:
            ratio = am / bm
        if ratio < min_ratio:
            continue
        jump = am - bm
        cand = {
            "detected": True,
            "date": pts[i][0],
            "before_gb_per_day": _r(bm),
            "after_gb_per_day": _r(am),
            "ratio": _r(ratio, 2) if ratio != float("inf") else None,
            "_jump": jump,
        }
        if best is None or jump > best["_jump"]:
            best = cand
    if best is None:
        return empty
    best.pop("_jump", None)
    return best


# ---------------------------------------------------------------------------
# Query runner plumbing.
# ---------------------------------------------------------------------------

def _awscost_runner(aws: Any, log_group: str, region: str, profile: str,
                    poll_timeout: int) -> Optional[Callable]:
    """Build a runner closure over ``aws.logs_insights_query``.

    Returns None when no usable ``logs_insights_query`` is reachable. The
    closure signature is ``runner(kind, query, ctx) -> raw_result`` where
    ``ctx`` carries ``log_group``/``start_epoch``/``end_epoch``/``limit``.
    Because ``logs_insights_query`` is delivered by the sibling awscost
    module, the call is made defensively (keyword then positional) so this
    module works regardless of the exact parameter names it settles on.
    """
    if aws is None:
        try:
            from nr2grafana import awscost as aws  # type: ignore
        except Exception:  # noqa: BLE001 - optional dependency
            return None
    fn = getattr(aws, "logs_insights_query", None)
    if not callable(fn):
        return None

    def runner(kind: str, query: str, ctx: Dict[str, Any]) -> Any:
        try:
            return fn(
                log_group=ctx.get("log_group", log_group),
                query=query,
                start_epoch=ctx.get("start_epoch"),
                end_epoch=ctx.get("end_epoch"),
                limit=ctx.get("limit"),
                region=region,
                profile=profile)
        except TypeError:
            # Fall back to a bare positional call if the parameter names
            # differ from awscost.logs_insights_query.
            return fn(
                ctx.get("log_group", log_group), query,
                ctx.get("start_epoch"), ctx.get("end_epoch"))

    return runner


def _degraded(note: str, log_group: str = "",
              query_errors: Optional[List[str]] = None) -> Dict[str, Any]:
    """A well-formed, empty report flagged unavailable (never raises)."""
    return {
        "schema": SCHEMA,
        "generated_by": GENERATED_BY,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "available": False,
        "note": note,
        "log_group": log_group,
        "window": {},
        "totals": {
            "cross_az_gb": 0.0, "same_az_gb": 0.0, "unknown_az_gb": 0.0,
            "total_gb": 0.0, "cross_az_gb_per_day": 0.0,
            "cross_az_pct_of_total": None, "days": 0.0},
        "dominant_port": None,
        "ports": [],
        "top_flows": [],
        "az_pairs": [],
        "az_imbalance": {},
        "step_change": {"detected": False, "date": ""},
        "daily": [],
        "queries": [],
        "query_errors": query_errors or [],
        "assumptions": _assumptions(),
        "summary": note,
    }


def _assumptions() -> List[str]:
    return [
        "GB here is GiB (bytes / 2**30), matching the Logs Insights query "
        "divisor; cross-AZ transfer bills in decimal GB, so the dollar "
        "layer applies its own conversion.",
        "'Cross-AZ' means source and destination ENIs are in DIFFERENT "
        "AZs of the SAME region (inter-AZ, not cross-region); a flow whose "
        "AZ cannot be resolved is counted as unknown, never as cross-AZ.",
        "Only private-IP (RFC1918) ACCEPT flows are counted, which rules "
        "out NAT / internet egress and the same-AZ-over-public-IP edge "
        "case (a Regional-Bytes charge over an EIP within one AZ).",
        "Byte totals are what VPC Flow Logs recorded over the window; "
        "sampling or dropped records (log-status SKIPDATA) undercount.",
    ]


# ---------------------------------------------------------------------------
# Row aggregation.
# ---------------------------------------------------------------------------

def _classify_flow(row: Dict[str, Any],
                   eni_map: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn one raw flow row into a normalized, AZ-classified record."""
    src_addr = _row_get(row, "src_addr")
    dst_addr = _row_get(row, "dst_addr")
    dst_port = _int(_row_get(row, "dst_port"))
    src_az = _az_of(row, "src", eni_map)
    dst_az = _az_of(row, "dst", eni_map)
    gb = _row_gb(row)
    if src_az and dst_az:
        cls = "cross" if src_az != dst_az else "same"
    else:
        cls = "unknown"
    return {
        "src_addr": str(src_addr) if src_addr not in (None, "") else "",
        "dst_addr": str(dst_addr) if dst_addr not in (None, "") else "",
        "dst_port": dst_port,
        "port_label": port_label(dst_port),
        "src_az": src_az,
        "dst_az": dst_az,
        "src_workload": _workload_of(src_addr, eni_map),
        "dst_workload": _workload_of(dst_addr, eni_map),
        "gb": gb,
        "class": cls,
    }


def _aggregate(flow_rows: List[Dict[str, Any]],
               port_rows: List[Dict[str, Any]],
               eni_map: Optional[Dict[str, Any]], days: float,
               expected_azs: int) -> Dict[str, Any]:
    """Build totals, port breakdown, top flows, AZ pairs and imbalance."""
    flows = [_classify_flow(r, eni_map) for r in flow_rows]

    cross = [f for f in flows if f["class"] == "cross"]
    same = [f for f in flows if f["class"] == "same"]
    unknown = [f for f in flows if f["class"] == "unknown"]

    cross_gb = sum(f["gb"] for f in cross)
    same_gb = sum(f["gb"] for f in same)
    unknown_gb = sum(f["gb"] for f in unknown)
    total_gb = cross_gb + same_gb + unknown_gb

    def pct_cross(gb: float) -> Optional[float]:
        return _r(gb / cross_gb * 100.0, 2) if cross_gb > 0 else None

    # --- port breakdown over cross-AZ bytes ---------------------------------
    # Prefer the dedicated ports query (it carries az fields), else derive
    # from the classified flows.
    port_gb: Dict[Optional[int], float] = {}
    used_port_rows = False
    for r in port_rows or []:
        s_az = _az_of(r, "src", eni_map)
        d_az = _az_of(r, "dst", eni_map)
        if not (s_az and d_az) or s_az == d_az:
            continue
        port = _int(_row_get(r, "dst_port"))
        port_gb[port] = port_gb.get(port, 0.0) + _row_gb(r)
        used_port_rows = True
    if not used_port_rows:
        for f in cross:
            port_gb[f["dst_port"]] = port_gb.get(f["dst_port"], 0.0) + f["gb"]

    # If the ports query gave its own cross-AZ total, prefer it for the
    # percentage denominator so the port shares are internally consistent.
    port_cross_total = sum(port_gb.values())
    denom = port_cross_total if used_port_rows and port_cross_total > 0 \
        else cross_gb

    def pct_of(gb: float) -> Optional[float]:
        return _r(gb / denom * 100.0, 2) if denom > 0 else None

    ports = []
    for port, gb in sorted(port_gb.items(),
                           key=lambda kv: kv[1], reverse=True):
        ports.append({
            "port": port,
            "label": port_label(port),
            "gb": _r(gb),
            "gb_per_day": _r(gb / days) if days > 0 else _r(gb),
            "pct_of_cross_az": pct_of(gb),
        })
    dominant_port = ports[0] if ports else None

    # --- top cross-AZ flows -------------------------------------------------
    top_flows = []
    for f in sorted(cross, key=lambda x: x["gb"], reverse=True)[:20]:
        top_flows.append({
            "src_addr": f["src_addr"],
            "dst_addr": f["dst_addr"],
            "src_az": f["src_az"],
            "dst_az": f["dst_az"],
            "dst_port": f["dst_port"],
            "port_label": f["port_label"],
            "src_workload": f["src_workload"],
            "dst_workload": f["dst_workload"],
            "gb": _r(f["gb"]),
            "gb_per_day": _r(f["gb"] / days) if days > 0 else _r(f["gb"]),
            "pct_of_cross_az": pct_cross(f["gb"]),
        })

    # --- AZ pairs + imbalance ----------------------------------------------
    pair_gb: Dict[Tuple[str, str], float] = {}
    azs_seen = set()
    for f in cross:
        key = (f["src_az"], f["dst_az"])
        pair_gb[key] = pair_gb.get(key, 0.0) + f["gb"]
        azs_seen.add(f["src_az"])
        azs_seen.add(f["dst_az"])
    az_pairs = []
    for (s_az, d_az), gb in sorted(pair_gb.items(),
                                   key=lambda kv: kv[1], reverse=True):
        az_pairs.append({
            "src_az": s_az, "dst_az": d_az,
            "gb": _r(gb),
            "pct_of_cross_az": pct_cross(gb),
        })

    az_count = len(azs_seen)
    imbalanced = 0 < az_count < int(expected_azs)
    imbalance = {
        "azs": sorted(a for a in azs_seen if a),
        "az_count": az_count,
        "expected_az_count": int(expected_azs),
        "imbalanced": imbalanced,
        "note": "",
    }
    if imbalanced:
        imbalance["note"] = (
            "cross-AZ traffic is confined to %d of %d expected AZs -- the "
            "hash-ring/pods are not spread across all zones. A common root "
            "cause is a Karpenter discovery-subnet gap (no tagged subnet "
            "in the missing AZ), which forces every RF>1 replica write "
            "across a zone boundary." % (az_count, int(expected_azs)))
    elif az_count == 0:
        imbalance["note"] = (
            "no cross-AZ flows could be attributed to AZ pairs (AZ not in "
            "the flow-log format and no ENI map provided).")

    totals = {
        "cross_az_gb": _r(cross_gb),
        "same_az_gb": _r(same_gb),
        "unknown_az_gb": _r(unknown_gb),
        "total_gb": _r(total_gb),
        "cross_az_gb_per_day": _r(cross_gb / days) if days > 0
        else _r(cross_gb),
        "cross_az_pct_of_total": (_r(cross_gb / total_gb * 100.0, 2)
                                  if total_gb > 0 else None),
        "days": _r(days, 2),
    }
    return {
        "totals": totals,
        "ports": ports,
        "dominant_port": dominant_port,
        "top_flows": top_flows,
        "az_pairs": az_pairs,
        "az_imbalance": imbalance,
        "flow_count": len(flows),
        "cross_flow_count": len(cross),
        "unknown_flow_count": len(unknown),
    }


def _daily_series(daily_rows: List[Dict[str, Any]],
                  eni_map: Optional[Dict[str, Any]]) -> List[List[Any]]:
    """Cross-AZ GB per day, ascending by date."""
    per_day: Dict[str, float] = {}
    for r in daily_rows or []:
        s_az = _az_of(r, "src", eni_map)
        d_az = _az_of(r, "dst", eni_map)
        if not (s_az and d_az) or s_az == d_az:
            continue
        day = _row_get(r, "day")
        if day in (None, ""):
            continue
        key = str(day)
        per_day[key] = per_day.get(key, 0.0) + _row_gb(r)
    return [[d, _r(per_day[d])] for d in sorted(per_day)]


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def analyze(aws: Any = None, log_group: str = "",
            start_epoch: Optional[int] = None,
            end_epoch: Optional[int] = None,
            days: int = DEFAULT_DAYS, onset: str = "",
            eni_map: Optional[Dict[str, Any]] = None,
            cfg: Optional[Dict[str, Any]] = None,
            expected_azs: int = DEFAULT_EXPECTED_AZS,
            region: str = "us-east-1", profile: str = "",
            query_fn: Optional[Callable] = None,
            flow_limit: int = DEFAULT_FLOW_LIMIT,
            port_limit: int = DEFAULT_PORT_LIMIT,
            poll_timeout: int = 120,
            log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Attribute cross-AZ bytes from VPC Flow Logs -> schema
    "nr2grafana/flowlogs/v1".

    ``aws`` is the :mod:`nr2grafana.awscost` module (or any object exposing
    ``logs_insights_query``); when None it is imported lazily. ``log_group``
    is the VPC Flow Logs CloudWatch log group. ``eni_map`` maps a private
    IP string to ``{"az","workload","node","eni",...}`` so flows resolve to
    AZ/workload when the log format lacks ``az-id``.

    ``query_fn`` (mainly for tests) overrides the AWS runner: it is called
    as ``query_fn(kind, query, ctx)`` -- ``kind`` in
    {"flows","ports","daily"} -- and must return a Logs Insights result in
    any shape :func:`normalize_rows` accepts.

    NEVER raises. Missing configuration, an unavailable runner, or a failed
    query all degrade to ``available=False`` with an actionable ``note``.
    """
    emit = log or (lambda m: None)
    cfg = cfg or {}
    fcfg = cfg.get("flowlogs") if isinstance(cfg, dict) else {}
    fcfg = fcfg if isinstance(fcfg, dict) else {}
    if not log_group:
        log_group = str(fcfg.get("log_group", "") or "")
    if eni_map is None and isinstance(fcfg.get("eni_map"), dict):
        eni_map = fcfg["eni_map"]
    expected_azs = int(fcfg.get("expected_azs", expected_azs) or expected_azs)

    days = int(days) if days and int(days) > 0 else DEFAULT_DAYS
    now = int(time.time())
    if end_epoch is None:
        end_epoch = now
    if start_epoch is None:
        start_epoch = int(end_epoch) - days * 86400
    # Effective window length in days for GB/day math.
    span = max(1.0, (int(end_epoch) - int(start_epoch)) / 86400.0)

    runner = query_fn
    if runner is None:
        if not log_group:
            note = ("no VPC Flow Logs log group configured. Set "
                    "cfg['flowlogs']['log_group'] (or pass "
                    "--flow-logs-group) to a CloudWatch log group receiving "
                    "VPC Flow Logs, and ensure the flow-log format includes "
                    "srcaddr/dstaddr/dstport/bytes (ideally az-id). Without "
                    "flow logs the cross-AZ driver cannot be measured, so "
                    "the RCA falls back to a low-confidence hypothesis.")
            emit("flowlogs: " + note)
            return _degraded(note, log_group)
        runner = _awscost_runner(aws, log_group, region, profile,
                                 poll_timeout)
    if runner is None:
        note = ("cannot run CloudWatch Logs Insights queries: the aws CLI "
                "is unavailable or awscost.logs_insights_query is missing. "
                "Install/configure the AWS CLI (read-only) or pass a "
                "query_fn. Flow-log analysis is optional; the rest of the "
                "RCA still runs.")
        emit("flowlogs: " + note)
        return _degraded(note, log_group)

    ctx = {
        "log_group": log_group,
        "start_epoch": int(start_epoch),
        "end_epoch": int(end_epoch),
    }
    queries = [
        ("flows", build_flows_query(flow_limit), flow_limit),
        ("ports", build_ports_query(port_limit), port_limit),
        ("daily", build_daily_query(), 400),
    ]
    results: Dict[str, List[Dict[str, Any]]] = {}
    query_errors: List[str] = []
    query_meta: List[Dict[str, Any]] = []
    any_ok = False
    for kind, query, limit in queries:
        query_meta.append({"kind": kind, "query": query})
        qctx = dict(ctx)
        qctx["limit"] = limit
        try:
            emit("flowlogs: running %s query ..." % kind)
            raw = runner(kind, query, qctx)
            rows = normalize_rows(raw)
            results[kind] = rows
            any_ok = True
        except Exception as exc:  # noqa: BLE001 - one query must not sink all
            msg = "%s query failed: %s" % (kind, str(exc)[:200])
            emit("flowlogs: " + msg)
            query_errors.append(msg)
            results[kind] = []

    if not any_ok:
        note = ("all flow-log queries failed against log group %r -- verify "
                "the log group exists, receives VPC Flow Logs, and the "
                "credentials allow logs:StartQuery/GetQueryResults "
                "(read-only). See query_errors for detail." % log_group)
        emit("flowlogs: " + note)
        return _degraded(note, log_group, query_errors)

    agg = _aggregate(results.get("flows", []), results.get("ports", []),
                     eni_map, span, expected_azs)
    daily = _daily_series(results.get("daily", []), eni_map)
    step = detect_step_change(daily)

    totals = agg["totals"]
    if totals["cross_az_gb"] <= 0.0 and not query_errors:
        note = ("no cross-AZ private-IP flows found in %r over the window. "
                "Either traffic really is same-AZ, or the flow-log format "
                "lacks az-id and no ENI map was provided to resolve AZ. "
                "This is not proof of no cross-AZ cost -- provide an ENI map "
                "or a custom flow-log format with az-id to measure it."
                % log_group)
        emit("flowlogs: " + note)
        report = _degraded(note, log_group, query_errors)
        report["window"] = {
            "start_epoch": int(start_epoch), "end_epoch": int(end_epoch),
            "days": _r(span, 2)}
        report["daily"] = daily
        report["queries"] = query_meta
        return report

    dom = agg["dominant_port"]
    if dom and dom.get("port") is not None:
        summary = (
            "cross-AZ transfer ~%.1f GB/day; dominant destination port %s"
            "%s carries %s%% of cross-AZ bytes across %d AZ(s)%s."
            % (totals["cross_az_gb_per_day"], dom["port"],
               (" (" + dom["label"] + ")") if dom.get("label") else "",
               dom.get("pct_of_cross_az"), agg["az_imbalance"]["az_count"],
               "; step-change " + step["date"] if step.get("detected")
               else ""))
    else:
        summary = ("cross-AZ transfer ~%.1f GB/day (dominant port "
                   "unresolved)." % totals["cross_az_gb_per_day"])
    emit("flowlogs: " + summary)

    return {
        "schema": SCHEMA,
        "generated_by": GENERATED_BY,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "available": True,
        "note": "",
        "log_group": log_group,
        "window": {
            "start_epoch": int(start_epoch),
            "end_epoch": int(end_epoch),
            "days": _r(span, 2)},
        "onset": onset,
        "totals": totals,
        "dominant_port": dom,
        "ports": agg["ports"],
        "top_flows": agg["top_flows"],
        "az_pairs": agg["az_pairs"],
        "az_imbalance": agg["az_imbalance"],
        "step_change": step,
        "daily": daily,
        "counts": {
            "flows": agg["flow_count"],
            "cross_az_flows": agg["cross_flow_count"],
            "unknown_az_flows": agg["unknown_flow_count"]},
        "queries": query_meta,
        "query_errors": query_errors,
        "assumptions": _assumptions(),
        "summary": summary,
    }
