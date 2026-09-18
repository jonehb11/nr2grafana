#!/usr/bin/env python3
"""A fake ``aws`` CLI for offline TCO demos and tests (stdlib only).

It emulates just the read-only subcommands that
:mod:`nr2grafana.awscost` invokes, emitting canned Cost Explorer /
CloudWatch / STS JSON on stdout so ``nr2grafana.tco`` can be driven end to
end without real AWS credentials or network. Point ``awscost`` at it via
the ``N2G_AWS_BIN`` environment variable::

    N2G_AWS_BIN=$PWD/tools/fake_aws.py python3 -m nr2grafana tco analyze

Everything it emits is **deterministic**: values are derived from the
requested time window and from a fixed per-key table, never from a random
number generator, so repeated runs and repeated months are byte-stable.
The synthetic bill shows a believable multi-month **upward** trend broken
down by SERVICE or USAGE_TYPE, a forecast that continues the climb, and
one cost anomaly.

**Read-only refusal.** This tool is the honest mirror of the awscost
guard: it recognises only read verbs
(``get-``/``list-``/``describe-``/``head-``/``lookup-``/``search-``/
``batch-get-``) plus the two hand-audited read-of-data exceptions
(``logs start-query`` / ``logs stop-query``, which only initiate/cancel a
CloudWatch Logs Insights query over EXISTING log data -- see
ARCHITECTURE-1.9 section 0.8). Any other subcommand -- anything that would
*change* AWS state -- makes it print an error to stderr and exit non-zero,
so a test (or the guard) can prove that a mutating command is refused even
at the CLI boundary.

**RCA (1.9) cross-AZ scenario.** Beyond the TCO bill, this fake also
serves a deterministic reproduction of the ARCHITECTURE-1.9 reference
worked example: a ``*-DataTransfer-Regional-Bytes`` cost anomaly whose
real cause is a NON-zone-aware Mimir/Loki hash-ring (RF=3 replication +
query fan-out on gRPC port 9095) confined to 2 imbalanced AZs, with a
cross-zone NLB secondary and EBS storage ruled out. It answers
``ce get-anomalies``, ``cloudtrail lookup-events``, ``logs start-query /
get-query-results / stop-query`` (VPC Flow Logs), ``eks describe-*`` /
``list-*``, ``ec2 describe-subnets/network-interfaces/availability-zones/
volumes/snapshots``, and ``elbv2 describe-*`` (one AZ with a single
healthy target, so the NLB black-hole guardrail is exercised). Every
number is index-seeded and byte-stable: 91% of cross-AZ bytes on port
9095, a step-change on 2026-08-31, ~16,470 GiB/day two-way (which at
$0.02 per round-tripped GB reproduces the ~$164/day headline).

Invoked the way the real CLI is::

    aws [--output json] [--region R] [--profile P] SERVICE SUBCOMMAND [args]

Global options are tolerated and ignored; SERVICE is located as the first
token matching a known AWS service name and SUBCOMMAND is the token after
it.
"""

import json
import sys

# Read-only verb prefixes -- mirror of awscost.READONLY_VERBS. A
# subcommand that does not start with one of these is a mutation and is
# refused (non-zero exit) before any output is produced.
READONLY_VERBS = ("get-", "list-", "describe-", "head-", "lookup-",
                  "search-", "batch-get-")

# Justified read-only EXCEPTIONS to the verb-prefix rule -- the honest
# mirror of awscost.READONLY_EXCEPTIONS. ``logs start-query`` only INITIATES
# a CloudWatch Logs Insights query over log DATA that already exists (VPC
# Flow Logs); ``logs stop-query`` only CANCELS one. Neither creates a log
# group/stream nor writes anything. See ARCHITECTURE-1.9 section 0.8. This
# set is intentionally tiny; nothing else bypasses the prefix gate.
READONLY_EXCEPTIONS = frozenset([
    ("logs", "start-query"),
    ("logs", "stop-query"),
])

# Known AWS service names we might be asked about. Used only to locate the
# SERVICE positional past any leading global flags/values.
KNOWN_SERVICES = ("ce", "sts", "cloudwatch", "s3api", "ec2", "pricing",
                  "organizations", "iam", "logs", "resourcegroupstaggingapi",
                  "cloudtrail", "eks", "elbv2")

# Per-SERVICE monthly baseline (month index 0) and month-over-month growth
# factor. Every growth factor is > 1 so the total bill climbs no matter
# how the caller groups it. The observability-relevant lines (EC2 compute,
# S3 storage, data transfer, EC2-Other/NAT) are present and sizeable so
# tco.attribute_observability has something to attribute.
SERVICE_TABLE = (
    ("Amazon Elastic Compute Cloud - Compute", 4000.0, 1.080),
    ("Amazon Simple Storage Service", 1500.0, 1.060),
    ("EC2 - Other", 900.0, 1.100),
    ("AWS Data Transfer", 600.0, 1.120),
    ("Amazon Relational Database Service", 1200.0, 1.030),
    ("AmazonCloudWatch", 400.0, 1.050),
    ("Amazon Managed Grafana", 150.0, 1.040),
    ("AWS Key Management Service", 30.0, 1.000),
)

# Per-USAGE_TYPE baseline / growth, used when grouped by USAGE_TYPE.
USAGE_TYPE_TABLE = (
    ("USE1-BoxUsage:m5.2xlarge", 3000.0, 1.090),
    ("USE1-BoxUsage:r5.xlarge", 1500.0, 1.070),
    ("USE1-TimedStorage-ByteHrs", 1400.0, 1.060),
    ("USE1-DataTransfer-Out-Bytes", 700.0, 1.120),
    ("USE1-NatGateway-Bytes", 500.0, 1.110),
    ("USE1-EBS:VolumeUsage.gp3", 600.0, 1.040),
    ("USE1-CW:MetricMonitorUsage", 350.0, 1.050),
)

# Forecast continues the climb from roughly the latest run-rate.
FORECAST_BASE = 13200.0
FORECAST_GROWTH = 1.060

DAY = "-01"

# --- RCA (1.9) cross-AZ scenario ------------------------------------------
# Deterministic reproduction of the ARCHITECTURE-1.9 reference worked
# example (sections 0.1, 0.5-0.9). Every value is a fixed constant so the
# whole RCA -> mitigation flow is byte-stable across runs.
RCA_REGION = "us-east-1"
AZ_A, AZ_B, AZ_C = "us-east-1a", "us-east-1b", "us-east-1c"
RCA_CLUSTER = "obs-eks"
RCA_STEP_CHANGE = "2026-08-31"     # true cost step-change date
RCA_ANOMALY_END = "2026-09-03"     # 4-day interval -> $164/day headline

GRPC_PORT = 9095    # Mimir/Loki inter-component gRPC: ring + query fan-out
NLB_PORT = 443      # cross-zone Mimir gateway NLB (secondary driver)
MISC_PORT = 53      # small residual (DNS), the "everything else" bucket
GIB = 1073741824    # 2 ** 30, the Logs Insights query byte divisor

# Per-day cross-AZ GiB by destination port -- a 91% / 8% / 1% split. Summed
# over RCA_WINDOW_DAYS this is what the flows/ports queries report; the daily
# query straddles the step-change. 14,988 + 1,318 + 164 == 16,470 GiB/day.
RCA_WINDOW_DAYS = 14
DAILY_CROSS_GIB = ((GRPC_PORT, 14988), (NLB_PORT, 1318), (MISC_PORT, 164))

# ELBv2 fixture ARNs (account 123456789012, matching get-caller-identity).
_ELB_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
            "loadbalancer/net/mimir-nlb/0a1b2c3d4e5f6789")
_TG_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
           "targetgroup/mimir-tg/1122334455667788")


def _err(msg):
    sys.stderr.write("fake-aws: " + msg + "\n")


def _print_json(obj):
    sys.stdout.write(json.dumps(obj, indent=2) + "\n")


def _parse_flags(tokens):
    """Group ``--flag [values...]`` into ``{flag: [values]}``.

    Handles both ``--flag value`` (one or more values until the next
    ``--flag``) and ``--flag=value``. Bare flags map to an empty list.
    """
    flags = {}
    cur = None
    for tok in tokens:
        if tok.startswith("--"):
            body = tok[2:]
            if "=" in body:
                key, val = body.split("=", 1)
                flags.setdefault(key, []).append(val)
                cur = None
            else:
                cur = body
                flags.setdefault(cur, [])
        elif cur is not None:
            flags[cur].append(tok)
    return flags


def _first(flags, name, default=None):
    vals = flags.get(name)
    if vals:
        return vals[0]
    return default


def _kv(spec):
    """Parse ``Key=Val,Key2=Val2`` shorthand into a dict."""
    out = {}
    for part in (spec or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _month_index(start):
    """Absolute month ordinal for a ``YYYY-MM-...`` date (year*12+month)."""
    y, m = int(start[0:4]), int(start[5:7])
    return y * 12 + (m - 1)


def _month_range(start, end):
    """List ``(year, month)`` first-of-month tuples in ``[start, end)``.

    ``start``/``end`` are ``YYYY-MM-DD`` with ``end`` exclusive (CE
    convention). At least one month is always returned.
    """
    sy, sm = int(start[0:4]), int(start[5:7])
    ey, em = int(end[0:4]), int(end[5:7])
    out = []
    y, m = sy, sm
    while (y, m) < (ey, em):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    if not out:
        out.append((sy, sm))
    return out


def _fmt_month(y, m):
    return "%04d-%02d%s" % (y, m, DAY)


def _next_month(y, m):
    m += 1
    if m > 12:
        m = 1
        y += 1
    return y, m


def _amount(base, growth, idx):
    """Deterministic monthly amount: geometric climb, no RNG."""
    return round(base * (growth ** idx), 4)


def _metric_block(metrics, amount):
    block = {}
    for name in metrics:
        block[name] = {"Amount": "%.10f" % amount, "Unit": "USD"}
    return block


def _cmd_get_cost_and_usage(flags):
    tp = _kv(_first(flags, "time-period", ""))
    start = tp.get("Start", "2026-03-01")
    end = tp.get("End", "2026-09-01")
    metrics = flags.get("metrics") or ["UnblendedCost"]
    group_specs = flags.get("group-by") or []
    # Only the first grouping dimension drives the synthetic keys; that is
    # all the tco default (single SERVICE / USAGE_TYPE grouping) needs.
    group_key = ""
    group_type = "DIMENSION"
    if group_specs:
        gk = _kv(group_specs[0])
        group_key = gk.get("Key", "")
        group_type = gk.get("Type", "DIMENSION")

    if group_key == "USAGE_TYPE":
        table = USAGE_TYPE_TABLE
    else:
        table = SERVICE_TABLE

    months = _month_range(start, end)
    base_ord = _month_index(start)
    results = []
    for (y, m) in months:
        idx = (y * 12 + (m - 1)) - base_ord
        ny, nm = _next_month(y, m)
        entry = {
            "TimePeriod": {"Start": _fmt_month(y, m),
                           "End": _fmt_month(ny, nm)},
            "Estimated": False,
        }
        if group_key:
            groups = []
            for (name, base, growth) in table:
                amt = _amount(base, growth, idx)
                groups.append({
                    "Keys": [name],
                    "Metrics": _metric_block(metrics, amt),
                })
            entry["Total"] = {}
            entry["Groups"] = groups
        else:
            total = sum(_amount(base, growth, idx)
                        for (_n, base, growth) in table)
            entry["Total"] = _metric_block(metrics, round(total, 4))
            entry["Groups"] = []
        results.append(entry)

    out = {
        "ResultsByTime": results,
        "DimensionValueAttributes": [],
    }
    if group_key:
        out["GroupDefinitions"] = [{"Type": group_type, "Key": group_key}]
    return out


def _cmd_get_cost_forecast(flags):
    tp = _kv(_first(flags, "time-period", ""))
    start = tp.get("Start", "2026-09-01")
    end = tp.get("End", "2026-12-01")
    months = _month_range(start, end)
    results = []
    total = 0.0
    for k, (y, m) in enumerate(months):
        mean = round(FORECAST_BASE * (FORECAST_GROWTH ** k), 4)
        total += mean
        ny, nm = _next_month(y, m)
        results.append({
            "TimePeriod": {"Start": _fmt_month(y, m),
                           "End": _fmt_month(ny, nm)},
            "MeanValue": "%.10f" % mean,
            "PredictionIntervalLowerBound": "%.10f" % round(mean * 0.93, 4),
            "PredictionIntervalUpperBound": "%.10f" % round(mean * 1.07, 4),
        })
    return {
        "Total": {"Amount": "%.10f" % round(total, 4), "Unit": "USD"},
        "ForecastResultsByTime": results,
    }


def _cmd_get_anomalies(_flags):
    """One EBS ``DataTransfer-Regional-Bytes`` anomaly (0.7 field shape).

    Anchored to the reference step-change with a 4-day interval so
    ``TotalImpact / days == $164/day`` -- self-consistent with 0.1: at
    $0.02 per round-tripped GB that is ~8,200 GB/day one-way, i.e. the
    ~16,470 GiB/day two-way cross-AZ transfer the flow logs measure. The
    ``RootCauses[].UsageType`` uses the literal ``<Region>-DataTransfer-
    Regional-Bytes`` usage-type code; the EBS service tag is a CUR
    classification artifact (the dollars are cross-AZ network, not
    storage).
    """
    anomaly = {
        "AnomalyId": "fake-anomaly-xaz-0001",
        "AnomalyStartDate": RCA_STEP_CHANGE,
        "AnomalyEndDate": RCA_ANOMALY_END,
        "DimensionValue": "EBS",
        "AnomalyScore": {"MaxScore": 0.92, "CurrentScore": 0.88},
        "Impact": {
            "MaxImpact": 180.0,
            "TotalImpact": 656.0,          # 656 / 4 days == $164/day
            "TotalActualSpend": 800.0,
            "TotalExpectedSpend": 144.0,
            "TotalImpactPercentage": 455.6,
        },
        "MonitorArn": ("arn:aws:ce::123456789012:anomalymonitor/"
                       "fake-monitor-xaz"),
        "Feedback": "",
        "RootCauses": [{
            "Service": "EBS",
            "Region": RCA_REGION,
            "LinkedAccount": "123456789012",
            "LinkedAccountName": "prod",
            "UsageType": "USE1-DataTransfer-Regional-Bytes",
            "Impact": {"Contribution": 150.0},
        }, {
            "Service": "EC2",
            "Region": RCA_REGION,
            "LinkedAccount": "123456789012",
            "UsageType": "USE1-DataTransfer-Regional-Bytes",
            "Impact": {"Contribution": 14.0},
        }],
    }
    return {"Anomalies": [anomaly], "NextPageToken": None}


def _cmd_get_caller_identity(_flags):
    return {
        "UserId": "AROAEXAMPLEID:nr2grafana-e2e",
        "Account": "123456789012",
        "Arn": ("arn:aws:sts::123456789012:assumed-role/"
                "ReadOnlyDemo/nr2grafana-e2e"),
    }


def _seed(text):
    """Stable positional char-weighted checksum (no builtin hash salt)."""
    total = 0
    for i, ch in enumerate(text):
        total += (i + 1) * ord(ch)
    return total


def _cmd_get_metric_statistics(flags):
    dims = flags.get("dimensions") or []
    bucket = ""
    for spec in dims:
        kv = _kv(spec)
        if kv.get("Name") == "BucketName":
            bucket = kv.get("Value", "")
            break
    metric_name = _first(flags, "metric-name", "BucketSizeBytes")
    end = _first(flags, "end-time", "2026-09-15T00:00:00Z")
    seed = _seed(bucket or metric_name)
    if metric_name == "NumberOfObjects":
        value = float(100000 + (seed * 7) % 900000)
        unit = "Count"
    else:
        # BucketSizeBytes: 200..1000 GiB, deterministic per bucket name.
        size_gib = 200 + (seed % 800)
        value = float(size_gib) * (1024 ** 3)
        unit = "Bytes"
    return {
        "Label": metric_name,
        "Datapoints": [{
            "Timestamp": end,
            "Average": value,
            "Unit": unit,
        }],
    }


# --------------------------------------------------------------------------
# RCA (1.9) cross-AZ scenario handlers.
# --------------------------------------------------------------------------

def _row(pairs):
    """A CloudWatch Logs Insights result row: a list of field/value cells."""
    return [{"field": k, "value": v} for k, v in pairs]


def _query_kind(query):
    """Classify a Logs Insights query string into flows/ports/daily.

    Mirrors the templates :mod:`nr2grafana.flowlogs` builds: the daily
    query buckets ``by bin(1d)``, the ports query groups ``by dstPort``,
    and the flows query groups ``by srcAddr``. Deterministic substring
    match -- no state, no RNG.
    """
    q = (query or "").lower()
    if "bin(1d)" in q:
        return "daily"
    if "by dstport" in q:
        return "ports"
    return "flows"


def _flows_rows():
    """Top cross-AZ talker flows (private-IP 10.x, AZ_A<->AZ_B) + one
    same-AZ flow, one row per destination port. Byte totals are the daily
    GiB share times the window, so cross-AZ GB/day rebuilds to 16,470."""
    rows = []
    endpoints = {
        GRPC_PORT: ("10.0.1.10", "10.0.2.20"),
        NLB_PORT: ("10.0.1.30", "10.0.2.40"),
        MISC_PORT: ("10.0.1.50", "10.0.2.60"),
    }
    for port, gib_day in DAILY_CROSS_GIB:
        s_addr, d_addr = endpoints[port]
        total_bytes = gib_day * RCA_WINDOW_DAYS * GIB
        rows.append(_row([
            ("srcAddr", s_addr), ("dstAddr", d_addr),
            ("dstPort", str(port)),
            ("srcAz", AZ_A), ("dstAz", AZ_B),
            ("bytes", str(total_bytes))]))
    # A same-AZ private-IP flow (AZ_A<->AZ_A): keeps cross_az_pct_of_total
    # < 100 and anchors the private-IP-only rule-out (not NAT/egress/EIP).
    rows.append(_row([
        ("srcAddr", "10.0.1.10"), ("dstAddr", "10.0.1.11"),
        ("dstPort", str(GRPC_PORT)),
        ("srcAz", AZ_A), ("dstAz", AZ_A),
        ("bytes", str(200 * GIB))]))
    return rows


def _ports_rows():
    """Cross-AZ bytes by destination port (pre-divided GiB). Shares come
    out 91% / 8% / 1%; port 9095 dominates."""
    rows = []
    for port, gib_day in DAILY_CROSS_GIB:
        gib = gib_day * RCA_WINDOW_DAYS
        rows.append(_row([
            ("dstPort", str(port)),
            ("srcAz", AZ_A), ("dstAz", AZ_B),
            ("gb", str(gib))]))
    return rows


def _daily_rows():
    """Daily cross-AZ GiB straddling the step-change: low before, high on
    and after 2026-08-31 (so step-change detection lands on that date)."""
    total_high = sum(g for _p, g in DAILY_CROSS_GIB)   # 16,470
    series = [
        ("2026-08-25", 500), ("2026-08-26", 480), ("2026-08-27", 520),
        ("2026-08-28", 495), ("2026-08-29", 510), ("2026-08-30", 505),
        (RCA_STEP_CHANGE, total_high), ("2026-09-01", total_high),
        ("2026-09-02", total_high), ("2026-09-03", total_high),
    ]
    rows = []
    for day, gib in series:
        rows.append(_row([
            ("day", day), ("srcAz", AZ_A), ("dstAz", AZ_B),
            ("gb", str(gib))]))
    return rows


def _cmd_lookup_events(_flags):
    """CloudTrail: a Karpenter scale-up correlated to the step-change."""
    return {"Events": [{
        "EventId": "fake-ct-0001",
        "EventName": "CreateFleet",
        "EventTime": RCA_STEP_CHANGE + "T02:14:00Z",
        "Username": "karpenter",
        "EventSource": "ec2.amazonaws.com",
        "ReadOnly": "false",
        "Resources": [{
            "ResourceType": "AWS::EC2::Instance",
            "ResourceName": "i-0fakescaleup01"}],
    }], "NextToken": None}


def _cmd_start_query(flags):
    """Logs Insights start-query: return a queryId encoding the query
    kind so the paired get-query-results serves matching rows."""
    query = _first(flags, "query-string", "") or ""
    return {"queryId": "fake-%s" % _query_kind(query)}


def _cmd_get_query_results(flags):
    """Logs Insights get-query-results: rows keyed off the fake queryId."""
    qid = _first(flags, "query-id", "") or ""
    if qid.endswith("ports"):
        rows = _ports_rows()
    elif qid.endswith("daily"):
        rows = _daily_rows()
    else:
        rows = _flows_rows()
    return {
        "status": "Complete",
        "results": rows,
        "statistics": {
            "recordsMatched": float(len(rows)),
            "recordsScanned": float(len(rows) * 10),
            "bytesScanned": float(len(rows) * 4096)},
    }


def _cmd_stop_query(_flags):
    return {}


def _cmd_list_clusters(_flags):
    return {"clusters": [RCA_CLUSTER]}


def _cmd_describe_cluster(flags):
    return {"cluster": {
        "name": _first(flags, "name", RCA_CLUSTER),
        "status": "ACTIVE",
        "version": "1.31",
        "arn": ("arn:aws:eks:us-east-1:123456789012:cluster/%s"
                % RCA_CLUSTER),
        "resourcesVpcConfig": {
            # Cluster subnets live in AZ_A/AZ_B only (no AZ_C).
            "subnetIds": ["subnet-a1", "subnet-b1"]},
    }}


def _cmd_list_nodegroups(_flags):
    return {"nodegroups": ["ng-observability"]}


def _cmd_describe_nodegroup(flags):
    return {"nodegroup": {
        "nodegroupName": _first(flags, "nodegroup-name",
                                "ng-observability"),
        "clusterName": _first(flags, "cluster-name", RCA_CLUSTER),
        "status": "ACTIVE",
        "capacityType": "ON_DEMAND",
        # Nodes land only in AZ_A/AZ_B -- the imbalance the ring inherits.
        "subnets": ["subnet-a1", "subnet-b1"],
    }}


def _discovery_tag():
    return [{"Key": "karpenter.sh/discovery", "Value": RCA_CLUSTER}]


def _cmd_describe_subnets(_flags):
    """Karpenter discovery subnets tagged in AZ_A and AZ_B ONLY (none in
    AZ_C) -- the discovery-subnet gap that forces the ring into 2 AZs
    (ARCHITECTURE-1.9 section 0.5)."""
    return {"Subnets": [
        {"SubnetId": "subnet-a1", "AvailabilityZone": AZ_A,
         "CidrBlock": "10.0.1.0/24", "Tags": _discovery_tag()},
        {"SubnetId": "subnet-b1", "AvailabilityZone": AZ_B,
         "CidrBlock": "10.0.2.0/24", "Tags": _discovery_tag()},
    ]}


def _cmd_describe_availability_zones(_flags):
    """The region offers three AZs -- more than the discovery subnets
    cover, which is what makes the layout imbalanced."""
    return {"AvailabilityZones": [
        {"ZoneName": AZ_A, "ZoneId": "use1-az1", "State": "available",
         "RegionName": RCA_REGION},
        {"ZoneName": AZ_B, "ZoneId": "use1-az2", "State": "available",
         "RegionName": RCA_REGION},
        {"ZoneName": AZ_C, "ZoneId": "use1-az4", "State": "available",
         "RegionName": RCA_REGION},
    ]}


def _cmd_describe_network_interfaces(_flags):
    """ENI -> pod IP -> workload map for the dominant flows (0.9). The VPC
    CNI attaches pod IPs to node ENIs; Description names the workload."""
    def eni(eid, ip, az, desc):
        return {
            "NetworkInterfaceId": eid,
            "AvailabilityZone": az,
            "Description": desc,
            "PrivateIpAddresses": [{"PrivateIpAddress": ip}],
            "Attachment": {"InstanceId": "i-node-" + az[-2:]},
        }
    return {"NetworkInterfaces": [
        eni("eni-a1", "10.0.1.10", AZ_A, "mimir-ingester-zone-a-0"),
        eni("eni-a2", "10.0.1.30", AZ_A, "mimir-gateway-a-0"),
        eni("eni-a3", "10.0.1.50", AZ_A, "coredns-a-0"),
        eni("eni-b1", "10.0.2.20", AZ_B, "mimir-ingester-zone-b-0"),
        eni("eni-b2", "10.0.2.40", AZ_B, "mimir-gateway-b-0"),
        eni("eni-b3", "10.0.2.60", AZ_B, "coredns-b-0"),
    ]}


def _cmd_describe_route_tables(_flags):
    return {"RouteTables": []}


def _cmd_describe_nat_gateways(_flags):
    # No NAT in the dominant flows -> intra-VPC, not internet egress.
    return {"NatGateways": []}


def _cmd_describe_volumes(_flags):
    """Small, pre-existing EBS volumes (CreateTime well before the step-
    change) -> the storage-growth rule-out: the Regional-Bytes dollars are
    cross-AZ network, not volume growth (Step E)."""
    return {"Volumes": [
        {"VolumeId": "vol-0aaa", "Size": 100, "State": "in-use",
         "AvailabilityZone": AZ_A, "VolumeType": "gp3",
         "CreateTime": "2026-01-05T00:00:00Z"},
        {"VolumeId": "vol-0bbb", "Size": 100, "State": "in-use",
         "AvailabilityZone": AZ_B, "VolumeType": "gp3",
         "CreateTime": "2026-01-05T00:00:00Z"},
    ]}


def _cmd_describe_snapshots(_flags):
    return {"Snapshots": [
        {"SnapshotId": "snap-0aaa", "VolumeSize": 100, "State": "completed",
         "StartTime": "2026-01-06T00:00:00Z"},
    ]}


def _cmd_describe_load_balancers(_flags):
    return {"LoadBalancers": [{
        "LoadBalancerArn": _ELB_ARN,
        "LoadBalancerName": "mimir-nlb",
        "Type": "network",
        "Scheme": "internal",
        "AvailabilityZones": [
            {"ZoneName": AZ_A, "SubnetId": "subnet-a1"},
            {"ZoneName": AZ_B, "SubnetId": "subnet-b1"}],
    }]}


def _cmd_describe_target_groups(_flags):
    return {"TargetGroups": [{
        "TargetGroupArn": _TG_ARN,
        "TargetGroupName": "mimir-tg",
        "Protocol": "TCP",
        "Port": NLB_PORT,
        "TargetType": "ip",
        "LoadBalancerArns": [_ELB_ARN],
    }]}


def _cmd_describe_target_health(_flags):
    """AZ_A has TWO healthy targets; AZ_B has ONLY ONE. Disabling NLB
    cross-zone would black-hole AZ_B -- this is what the NLB guardrail
    (keeps_availability=false unless gated) must catch (0.6)."""
    def t(ip, az, state):
        return {
            "Target": {"Id": ip, "Port": NLB_PORT,
                       "AvailabilityZone": az},
            "TargetHealth": {"State": state},
        }
    return {"TargetHealthDescriptions": [
        t("10.0.1.30", AZ_A, "healthy"),
        t("10.0.1.31", AZ_A, "healthy"),
        t("10.0.2.40", AZ_B, "healthy"),
    ]}


def _cmd_describe_target_group_attributes(_flags):
    return {"Attributes": [
        {"Key": "load_balancing.cross_zone.enabled", "Value": "true"},
        {"Key": ("target_group_health.dns_failover."
                 "minimum_healthy_targets.count"), "Value": "1"},
        {"Key": ("target_group_health.unhealthy_state_routing."
                 "minimum_healthy_targets.count"), "Value": "1"},
    ]}


def _cmd_describe_listeners(_flags):
    return {"Listeners": [{
        "ListenerArn": _ELB_ARN + "/listener/0abc",
        "LoadBalancerArn": _ELB_ARN,
        "Port": NLB_PORT,
        "Protocol": "TCP",
    }]}


# (service, subcommand) -> emitter. Only read-only commands appear here.
HANDLERS = {
    ("ce", "get-cost-and-usage"): _cmd_get_cost_and_usage,
    ("ce", "get-cost-and-usage-with-resources"): _cmd_get_cost_and_usage,
    ("ce", "get-cost-forecast"): _cmd_get_cost_forecast,
    ("ce", "get-usage-forecast"): _cmd_get_cost_forecast,
    ("ce", "get-anomalies"): _cmd_get_anomalies,
    ("sts", "get-caller-identity"): _cmd_get_caller_identity,
    ("cloudwatch", "get-metric-statistics"): _cmd_get_metric_statistics,
    # --- RCA (1.9) cross-AZ scenario ---
    ("cloudtrail", "lookup-events"): _cmd_lookup_events,
    ("logs", "start-query"): _cmd_start_query,
    ("logs", "get-query-results"): _cmd_get_query_results,
    ("logs", "stop-query"): _cmd_stop_query,
    ("eks", "list-clusters"): _cmd_list_clusters,
    ("eks", "describe-cluster"): _cmd_describe_cluster,
    ("eks", "list-nodegroups"): _cmd_list_nodegroups,
    ("eks", "describe-nodegroup"): _cmd_describe_nodegroup,
    ("ec2", "describe-subnets"): _cmd_describe_subnets,
    ("ec2", "describe-availability-zones"): _cmd_describe_availability_zones,
    ("ec2", "describe-network-interfaces"): _cmd_describe_network_interfaces,
    ("ec2", "describe-route-tables"): _cmd_describe_route_tables,
    ("ec2", "describe-nat-gateways"): _cmd_describe_nat_gateways,
    ("ec2", "describe-volumes"): _cmd_describe_volumes,
    ("ec2", "describe-snapshots"): _cmd_describe_snapshots,
    ("elbv2", "describe-load-balancers"): _cmd_describe_load_balancers,
    ("elbv2", "describe-target-groups"): _cmd_describe_target_groups,
    ("elbv2", "describe-target-health"): _cmd_describe_target_health,
    ("elbv2", "describe-target-group-attributes"):
        _cmd_describe_target_group_attributes,
    ("elbv2", "describe-listeners"): _cmd_describe_listeners,
}


def _locate_command(tokens):
    """Return ``(service, subcommand, rest)`` from a full argv tail.

    Skips any leading global options/values by scanning for the first
    token that is a known service name; the token after it is the
    subcommand and everything following is the parameter tail. Returns
    ``(None, None, [])`` when no service is found.
    """
    for i, tok in enumerate(tokens):
        if tok in KNOWN_SERVICES:
            service = tok
            subcommand = tokens[i + 1] if i + 1 < len(tokens) else ""
            rest = tokens[i + 2:]
            return service, subcommand, rest
    return None, None, []


def main(argv=None):
    tokens = list(sys.argv[1:] if argv is None else argv)

    # ``aws --version`` / ``help`` must succeed so availability probes and
    # accidental introspection do not look like a mutation refusal.
    if "--version" in tokens or "version" in tokens:
        sys.stdout.write("aws-cli/2.0.0-fake nr2grafana/offline\n")
        return 0
    if not tokens or tokens[0] in ("help", "--help"):
        sys.stdout.write("usage: fake aws for nr2grafana offline tests\n")
        return 0

    service, subcommand, rest = _locate_command(tokens)
    if service is None or not subcommand:
        _err("could not parse a service/subcommand from: %s"
             % " ".join(tokens))
        return 252

    # --- READ-ONLY REFUSAL: a mutating verb never runs. The only non-prefix
    # verbs permitted are the hand-audited READONLY_EXCEPTIONS (logs
    # start-query / stop-query), which read log DATA and mutate nothing.
    if (not subcommand.startswith(READONLY_VERBS)
            and (service, subcommand) not in READONLY_EXCEPTIONS):
        _err("refusing non-read-only subcommand %r on service %r -- this "
             "fake aws only serves read verbs "
             "(get-/list-/describe-/head-/lookup-/search-/batch-get-) plus "
             "logs start-query/stop-query." % (subcommand, service))
        return 254

    handler = HANDLERS.get((service, subcommand))
    if handler is None:
        # A recognised read verb we simply do not synthesise: emit a valid
        # empty document (exit 0) rather than crash. Mutations already
        # bailed above with a non-zero exit.
        _print_json({})
        return 0

    flags = _parse_flags(rest)
    _print_json(handler(flags))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
