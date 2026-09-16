"""Read-only AWS discovery via the local ``aws`` CLI (stdlib only).

The whole point of this module is a **hard read-only guard**. It shells
out to the user's existing ``aws`` CLI -- which uses whatever credentials
the local chain provides (env vars, shared profile, SSO, assumed role) --
but a command may run ONLY when it matches an explicit allow-list of
``(service, subcommand)`` pairs *and* the subcommand begins with a
read-only verb. Anything mutating is refused BEFORE exec: it is
impossible for this module to run a command that changes AWS state.

AWS credentials are never read, logged, or written by this module. It
only invokes ``aws``; the CLI resolves credentials itself. The command
is always run with ``shell=False`` and an explicit argv list, so no
argument is ever interpolated into a shell.

The ``aws`` binary path may be overridden with the ``N2G_AWS_BIN``
environment variable (used by tests to point at a fake ``aws``). The
CLI is OPTIONAL: when it is absent the functions raise :class:`AWSError`
with an actionable message rather than crashing.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

# Read-only verb prefixes. A subcommand must begin with one of these AND
# appear in ALLOWED. This is a belt-and-suspenders second gate: every
# entry in ALLOWED already starts with one of these, but requiring the
# prefix too means a typo in ALLOWED can never smuggle in a write verb.
READONLY_VERBS = ("get-", "list-", "describe-", "head-", "lookup-",
                  "search-", "batch-get-")

# Explicit allow-list: service -> set of permitted subcommands. This is an
# allow-set, never a deny-set. If a (service, subcommand) pair is not in
# here it is refused, full stop. Every subcommand here is read-only.
ALLOWED = {
    "ce": {
        "get-cost-and-usage",
        "get-cost-and-usage-with-resources",
        "get-cost-forecast",
        "get-dimension-values",
        "get-tags",
        "get-anomalies",
        "get-anomaly-monitors",
        "get-cost-categories",
        "list-cost-allocation-tags",
        "get-usage-forecast",
        "get-savings-plans-utilization",
        "get-reservation-utilization",
    },
    "sts": {"get-caller-identity"},
    "cloudwatch": {
        "get-metric-statistics",
        "get-metric-data",
        "list-metrics",
    },
    "s3api": {
        "list-buckets",
        "get-bucket-location",
        "get-bucket-lifecycle-configuration",
        "get-bucket-tagging",
    },
    "ec2": {
        "describe-instances",
        "describe-instance-types",
        "describe-regions",
    },
    "pricing": {
        "get-products",
        "get-attribute-values",
        "describe-services",
    },
    "organizations": {
        "list-accounts",
        "describe-organization",
    },
}


class AWSError(Exception):
    """Raised when an AWS command cannot run or is refused.

    Also raised (before any exec) when a command is refused by the
    read-only guard -- a mutating or non-allow-listed command.
    """


def _aws_bin() -> Optional[str]:
    """Resolve the ``aws`` executable path, honoring N2G_AWS_BIN.

    Returns the resolved path, or ``None`` when no usable binary is
    found. The override lets tests point at a fake ``aws``.
    """
    override = os.environ.get("N2G_AWS_BIN", "").strip()
    if override:
        # An explicit override may be a bare name on PATH or a full path.
        if os.path.sep in override or (os.altsep and os.altsep in override):
            return override if os.path.exists(override) else None
        return shutil.which(override) or (
            override if os.path.exists(override) else None)
    return shutil.which("aws")


def aws_available() -> bool:
    """True when an ``aws`` binary is resolvable (PATH or N2G_AWS_BIN)."""
    return _aws_bin() is not None


def _reject_control(value: str, what: str) -> None:
    """Refuse a NUL/newline in a value we place on the argv.

    Under ``shell=False`` shell metacharacters are already inert -- they
    become literal argv bytes and cannot spawn a second process -- but
    NUL and newline never legitimately appear in an AWS token, so we
    refuse them outright as a defense-in-depth signal.
    """
    if "\x00" in value or "\n" in value or "\r" in value:
        raise AWSError(
            "refusing %s containing a control character: %r"
            % (what, value))


def _is_allowed(service: str, subcommand: str) -> bool:
    """True iff (service, subcommand) is allow-listed AND read-only."""
    if not isinstance(service, str) or not isinstance(subcommand, str):
        return False
    subs = ALLOWED.get(service)
    if not subs or subcommand not in subs:
        return False
    return subcommand.startswith(READONLY_VERBS)


def run_aws(service: str, subcommand: str,
            args: Optional[List[str]] = None,
            region: str = "us-east-1", profile: str = "",
            timeout: int = 120) -> Any:
    """Run a single allow-listed, read-only ``aws`` command.

    Refuses (raises :class:`AWSError`) BEFORE exec unless
    ``(service, subcommand)`` is in :data:`ALLOWED` and ``subcommand``
    starts with a :data:`READONLY_VERBS` prefix. Always runs with
    ``shell=False`` and an argv list; ``--output json`` is forced and
    stdout is parsed as JSON.

    ``args`` are extra CLI arguments (flags/values) appended verbatim as
    separate argv elements -- because there is no shell, they cannot
    smuggle a second command or expand a metacharacter. Each must be a
    string free of NUL/newline.
    """
    # --- READ-ONLY GUARD: refuse before doing anything else. ---
    if not _is_allowed(service, subcommand):
        raise AWSError(
            "refused: (%r, %r) is not an allow-listed read-only AWS "
            "command -- nr2grafana only runs read-only AWS discovery "
            "(get-/list-/describe-/head-/lookup-/search-/batch-get-)"
            % (service, subcommand))

    _reject_control(service, "service")
    _reject_control(subcommand, "subcommand")
    _reject_control(region, "region")
    if profile:
        _reject_control(profile, "profile")

    argv_args: List[str] = []
    for a in args or []:
        if not isinstance(a, str):
            raise AWSError(
                "AWS argument is not a string: %r" % (a,))
        _reject_control(a, "argument")
        argv_args.append(a)

    aws = _aws_bin()
    if not aws:
        raise AWSError(
            "the 'aws' CLI was not found. Install the AWS CLI v2 and "
            "ensure it is on PATH (or set N2G_AWS_BIN). AWS cost "
            "discovery is optional; the rest of nr2grafana works "
            "without it.")

    argv = [aws, "--output", "json", "--region", region]
    if profile:
        argv += ["--profile", profile]
    argv += [service, subcommand] + argv_args

    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, timeout=timeout)
    except FileNotFoundError:
        raise AWSError(
            "could not execute the 'aws' CLI at %r -- check the "
            "installation (or N2G_AWS_BIN)." % aws)
    except subprocess.TimeoutExpired:
        raise AWSError(
            "the 'aws %s %s' command timed out after %d seconds; try a "
            "narrower time range or higher --timeout."
            % (service, subcommand, timeout))
    except OSError as e:
        raise AWSError(
            "could not run the 'aws' CLI (%s)." % e)

    if proc.returncode != 0:
        raise AWSError(_error_message(service, subcommand, proc.stderr))

    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out:
        # Some read commands legitimately return nothing.
        return {}
    try:
        return json.loads(out)
    except ValueError:
        raise AWSError(
            "the 'aws %s %s' command returned output that was not JSON."
            % (service, subcommand))


def _error_message(service: str, subcommand: str, stderr: bytes) -> str:
    """Turn an ``aws`` failure into an actionable, tracebacks-free note."""
    tail = stderr.decode("utf-8", "replace").strip()
    low = tail.lower()
    cmd = "aws %s %s" % (service, subcommand)
    if ("unable to locate credentials" in low
            or "could not be found" in low
            or "you must specify a region" in low
            or "no credentials" in low
            or "expired" in low
            or "sso session" in low):
        hint = ("AWS is not configured. Run 'aws configure' (or 'aws sso "
                "login'), or set a working AWS_PROFILE. Credentials are "
                "read from your local chain; nr2grafana never stores "
                "them.")
    elif ("accessdenied" in low or "access denied" in low
          or "not authorized" in low
          or "unauthorizedoperation" in low
          or "explicit deny" in low):
        hint = ("access denied. The credentials lack read permission for "
                "this call (e.g. ce:GetCostAndUsage). Grant read-only "
                "Cost Explorer / CloudWatch access and retry.")
    elif ("throttl" in low or "rate exceeded" in low
          or "toomanyrequests" in low or "requestlimitexceeded" in low):
        hint = ("throttled by AWS. Wait a few seconds and retry; Cost "
                "Explorer has low rate limits.")
    else:
        hint = "the AWS CLI reported an error."
    detail = (" Detail: " + tail[-500:]) if tail else ""
    return "%s failed -- %s%s" % (cmd, hint, detail)


def caller_identity(region: str = "us-east-1", profile: str = "",
                    timeout: int = 60) -> Dict[str, Any]:
    """Return ``sts get-caller-identity`` (who the local creds are).

    Read-only: shows the account id, ARN and user id so the UI can
    display which account/role discovery would run against.
    """
    data = run_aws("sts", "get-caller-identity", region=region,
                   profile=profile, timeout=timeout)
    return data if isinstance(data, dict) else {}


def _group_by_tokens(group_by: Any) -> List[str]:
    """Render group_by into ``Type=..,Key=..`` shorthand tokens.

    Accepts a list of dicts ``{"Type","Key"}``, a list of plain strings
    (treated as DIMENSION keys), or a single string.
    """
    if not group_by:
        return []
    if isinstance(group_by, str):
        group_by = [group_by]
    tokens: List[str] = []
    for g in group_by:
        if isinstance(g, dict):
            gtype = str(g.get("Type", "DIMENSION"))
            gkey = str(g.get("Key", ""))
        else:
            gtype = "DIMENSION"
            gkey = str(g)
        if not gkey:
            continue
        tokens.append("Type=%s,Key=%s" % (gtype, gkey))
    return tokens


def get_cost_and_usage(start: str, end: str, granularity: str = "MONTHLY",
                       group_by: Any = None,
                       metrics: Optional[List[str]] = None,
                       filt: Optional[Dict[str, Any]] = None,
                       region: str = "us-east-1",
                       profile: str = "") -> Dict[str, Any]:
    """Cost Explorer ``get-cost-and-usage`` for a time window.

    ``start``/``end`` are ``YYYY-MM-DD`` (end exclusive, per CE).
    ``metrics`` defaults to ``["UnblendedCost"]``. ``group_by`` may be a
    dimension key string, a list of such, or CE group dicts. ``filt`` is
    a CE Expression dict, JSON-encoded as ``--filter``.
    """
    metrics = metrics or ["UnblendedCost"]
    args: List[str] = [
        "--time-period", "Start=%s,End=%s" % (start, end),
        "--granularity", granularity,
        "--metrics"] + [str(m) for m in metrics]
    tokens = _group_by_tokens(group_by)
    if tokens:
        args.append("--group-by")
        args += tokens
    if filt:
        args += ["--filter", json.dumps(filt, sort_keys=True)]
    data = run_aws("ce", "get-cost-and-usage", args, region=region,
                   profile=profile)
    return data if isinstance(data, dict) else {}


def get_cost_forecast(start: str, end: str,
                      metric: str = "UNBLENDED_COST",
                      granularity: str = "MONTHLY",
                      prediction_interval: int = 0,
                      region: str = "us-east-1",
                      profile: str = "") -> Dict[str, Any]:
    """Cost Explorer ``get-cost-forecast`` for a future window.

    ``metric`` is a CE forecast metric (e.g. ``UNBLENDED_COST``).
    ``prediction_interval`` (50-99) sets the confidence band when > 0.
    """
    args: List[str] = [
        "--time-period", "Start=%s,End=%s" % (start, end),
        "--metric", metric,
        "--granularity", granularity]
    if prediction_interval:
        args += ["--prediction-interval-level", str(int(prediction_interval))]
    data = run_aws("ce", "get-cost-forecast", args, region=region,
                   profile=profile)
    return data if isinstance(data, dict) else {}


def get_anomalies(start: str, end: str,
                  monitor_arn: str = "", max_results: int = 0,
                  region: str = "us-east-1",
                  profile: str = "") -> List[Dict[str, Any]]:
    """Cost Explorer ``get-anomalies`` over a date interval.

    Returns the ``Anomalies`` list (possibly empty). ``start``/``end``
    are ``YYYY-MM-DD``.
    """
    args: List[str] = [
        "--date-interval", "StartDate=%s,EndDate=%s" % (start, end)]
    if monitor_arn:
        args += ["--monitor-arn", monitor_arn]
    if max_results:
        args += ["--max-results", str(int(max_results))]
    data = run_aws("ce", "get-anomalies", args, region=region,
                   profile=profile)
    if not isinstance(data, dict):
        return []
    anomalies = data.get("Anomalies")
    return anomalies if isinstance(anomalies, list) else []


def _latest_datapoint(data: Dict[str, Any]) -> Optional[float]:
    """Return the most recent CloudWatch datapoint's Average value."""
    if not isinstance(data, dict):
        return None
    points = data.get("Datapoints")
    if not isinstance(points, list) or not points:
        return None
    best = None
    best_ts = None
    for p in points:
        if not isinstance(p, dict):
            continue
        ts = p.get("Timestamp")
        val = p.get("Average")
        if val is None:
            continue
        if best_ts is None or (ts is not None and ts > best_ts):
            best_ts = ts
            try:
                best = float(val)
            except (TypeError, ValueError):
                best = None
    return best


def s3_bucket_sizes(buckets: List[str], region: str = "us-east-1",
                    profile: str = "") -> Dict[str, Dict[str, Any]]:
    """Estimate size/object-count per S3 bucket via CloudWatch.

    Uses ``cloudwatch get-metric-statistics`` on the daily ``AWS/S3``
    ``BucketSizeBytes`` (StandardStorage) and ``NumberOfObjects``
    (AllStorageTypes) metrics, taking the most recent datapoint. Any
    bucket that errors is recorded with an ``error`` key rather than
    aborting the whole batch (read-only; best effort).

    Returns ``{bucket: {"bytes": float|None, "objects": float|None}}``.
    """
    now = datetime.datetime.utcnow()
    start = (now - datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    out: Dict[str, Dict[str, Any]] = {}
    for bucket in buckets or []:
        entry: Dict[str, Any] = {"bytes": None, "objects": None}
        try:
            size = run_aws(
                "cloudwatch", "get-metric-statistics",
                ["--namespace", "AWS/S3",
                 "--metric-name", "BucketSizeBytes",
                 "--start-time", start, "--end-time", end,
                 "--period", "86400", "--statistics", "Average",
                 "--dimensions",
                 "Name=BucketName,Value=%s" % bucket,
                 "Name=StorageType,Value=StandardStorage"],
                region=region, profile=profile)
            entry["bytes"] = _latest_datapoint(size)
            count = run_aws(
                "cloudwatch", "get-metric-statistics",
                ["--namespace", "AWS/S3",
                 "--metric-name", "NumberOfObjects",
                 "--start-time", start, "--end-time", end,
                 "--period", "86400", "--statistics", "Average",
                 "--dimensions",
                 "Name=BucketName,Value=%s" % bucket,
                 "Name=StorageType,Value=AllStorageTypes"],
                region=region, profile=profile)
            entry["objects"] = _latest_datapoint(count)
        except AWSError as e:
            entry["error"] = str(e)
        out[bucket] = entry
    return out
