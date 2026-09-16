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
``batch-get-``). Any other subcommand -- anything that would *change* AWS
state -- makes it print an error to stderr and exit non-zero, so a test
(or the guard) can prove that a mutating command is refused even at the
CLI boundary.

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

# Known AWS service names we might be asked about. Used only to locate the
# SERVICE positional past any leading global flags/values.
KNOWN_SERVICES = ("ce", "sts", "cloudwatch", "s3api", "ec2", "pricing",
                  "organizations", "iam", "logs", "resourcegroupstaggingapi")

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


def _cmd_get_anomalies(flags):
    di = _kv(_first(flags, "date-interval", ""))
    end = di.get("EndDate", "2026-09-01")
    # Anchor the single anomaly a few days before the interval end.
    ey, em = int(end[0:4]), int(end[5:7])
    a_start = "%04d-%02d-12" % (ey, em)
    a_end = "%04d-%02d-16" % (ey, em)
    anomaly = {
        "AnomalyId": "fake-anomaly-0001",
        "AnomalyStartDate": a_start,
        "AnomalyEndDate": a_end,
        "DimensionValue": "Amazon Elastic Compute Cloud - Compute",
        "AnomalyScore": {"MaxScore": 0.98, "CurrentScore": 0.91},
        "Impact": {
            "MaxImpact": 320.5,
            "TotalImpact": 812.75,
            "TotalActualSpend": 2412.75,
            "TotalExpectedSpend": 1600.0,
            "TotalImpactPercentage": 50.8,
        },
        "MonitorArn": ("arn:aws:ce::123456789012:anomalymonitor/"
                       "fake-monitor-0001"),
        "Feedback": "",
        "RootCauses": [{
            "Service": "Amazon Elastic Compute Cloud - Compute",
            "Region": "us-east-1",
            "UsageType": "USE1-BoxUsage:m5.2xlarge",
            "LinkedAccount": "123456789012",
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


# (service, subcommand) -> emitter. Only read-only commands appear here.
HANDLERS = {
    ("ce", "get-cost-and-usage"): _cmd_get_cost_and_usage,
    ("ce", "get-cost-and-usage-with-resources"): _cmd_get_cost_and_usage,
    ("ce", "get-cost-forecast"): _cmd_get_cost_forecast,
    ("ce", "get-usage-forecast"): _cmd_get_cost_forecast,
    ("ce", "get-anomalies"): _cmd_get_anomalies,
    ("sts", "get-caller-identity"): _cmd_get_caller_identity,
    ("cloudwatch", "get-metric-statistics"): _cmd_get_metric_statistics,
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

    # --- READ-ONLY REFUSAL: a mutating verb never runs. ---
    if not subcommand.startswith(READONLY_VERBS):
        _err("refusing non-read-only subcommand %r on service %r -- this "
             "fake aws only serves read verbs "
             "(get-/list-/describe-/head-/lookup-/search-/batch-get-)."
             % (subcommand, service))
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
