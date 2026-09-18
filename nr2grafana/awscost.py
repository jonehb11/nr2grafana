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
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

# Read-only verb prefixes. A subcommand must begin with one of these AND
# appear in ALLOWED. This is a belt-and-suspenders second gate: every
# entry in ALLOWED already starts with one of these, but requiring the
# prefix too means a typo in ALLOWED can never smuggle in a write verb.
READONLY_VERBS = ("get-", "list-", "describe-", "head-", "lookup-",
                  "search-", "batch-get-")

# Justified read-only EXCEPTIONS to the verb-prefix rule. These are the
# ONLY (service, subcommand) pairs allowed even though the subcommand does
# not begin with a READONLY_VERBS prefix. Each was audited to READ existing
# data only -- it creates no infrastructure, writes nothing to S3, and
# mutates no AWS state:
#   - logs start-query : merely INITIATES a CloudWatch Logs Insights query
#     over log DATA that already exists (VPC Flow Logs). It creates no log
#     group/stream. IAM: logs:StartQuery.
#   - logs stop-query  : only CANCELS a running query. IAM: logs:StopQuery.
# (logs get-query-results is already prefix-covered by "get-"; cloudtrail
# lookup-events by "lookup-" -- both live in ALLOWED below, not here.)
# See ARCHITECTURE-1.9 section 0.8 (docs.aws.amazon.com API_StartQuery /
# API_StopQuery). This set is intentionally tiny and hand-audited; nothing
# else may ever bypass the prefix gate.
READONLY_EXCEPTIONS = frozenset([
    ("logs", "start-query"),
    ("logs", "stop-query"),
])

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
        # RCA 1.9: cross-AZ topology + storage rule-out (all read-only).
        "describe-network-interfaces",
        "describe-subnets",
        "describe-route-tables",
        "describe-nat-gateways",
        "describe-availability-zones",
        "describe-volumes",
        "describe-snapshots",
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
    # RCA 1.9: CloudTrail correlates the cost step-change to a config
    # action (e.g. a Karpenter scale-up). lookup-events reads management
    # events only. lookup- is a READONLY_VERBS prefix.
    "cloudtrail": {
        "lookup-events",
    },
    # RCA 1.9: CloudWatch Logs Insights over VPC Flow Logs. start-query /
    # stop-query are the sole non-prefix verbs, whitelisted via
    # READONLY_EXCEPTIONS (see 0.8); the rest are prefix-covered reads.
    "logs": {
        "get-query-results",
        "describe-log-groups",
        "describe-queries",
    },
    # RCA 1.9: EKS control plane -> name the workload behind the flows.
    "eks": {
        "describe-cluster",
        "list-clusters",
        "list-nodegroups",
        "describe-nodegroup",
        "list-fargate-profiles",
    },
    # RCA 1.9: NLB cross-zone + per-AZ target health (never blind-disable).
    "elbv2": {
        "describe-load-balancers",
        "describe-target-groups",
        "describe-target-health",
        "describe-listeners",
        "describe-target-group-attributes",
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


def aws_vault_available() -> bool:
    """True when the ``aws-vault`` helper is resolvable on PATH.

    Purely informational (lets the UI/CLI offer aws-vault). Enabling the
    wrapper is opt-in via :envvar:`N2G_AWS_WRAP` -- discovery is never
    silently re-routed through aws-vault just because it is installed.
    """
    return shutil.which("aws-vault") is not None


def _aws_wrap_prefix(profile: str = "") -> List[str]:
    """Return the credential-wrapper argv prefix, or ``[]`` for none.

    Controlled by :envvar:`N2G_AWS_WRAP`. Two forms:

    * The exact value ``aws-vault`` (with a ``profile`` given) expands to
      ``["aws-vault", "exec", <profile>, "--"]`` -- the common ergonomic.
    * Any other value is treated as a full command prefix, split with
      :func:`shlex.split`; the literal token ``{profile}`` is replaced by
      the profile value. This lets a user front the ``aws`` call with SSO
      helpers, ``aws-vault exec {profile} --``, etc.

    The wrapper only injects credentials around the already-vetted,
    allow-listed read-only ``aws`` command; it cannot change what runs.
    Control characters in any token are refused (defense in depth).
    """
    raw = os.environ.get("N2G_AWS_WRAP", "").strip()
    if not raw:
        return []
    if raw == "aws-vault" and profile:
        prefix = ["aws-vault", "exec", profile, "--"]
    else:
        try:
            parts = shlex.split(raw)
        except ValueError as e:
            raise AWSError(
                "N2G_AWS_WRAP is not a valid command prefix: %s" % e)
        prefix = [p.replace("{profile}", profile or "") for p in parts]
    for tok in prefix:
        _reject_control(tok, "wrap token")
    return prefix


def list_profiles() -> List[str]:
    """List AWS profile names from the local config (read-only).

    Parses ``~/.aws/config`` and ``~/.aws/credentials`` (honoring
    :envvar:`AWS_CONFIG_FILE` / :envvar:`AWS_SHARED_CREDENTIALS_FILE`) for
    profile section headers. In ``config`` these look like ``[default]`` or
    ``[profile NAME]``; ``sso-session``/``services`` sections are skipped.
    In ``credentials`` the section name IS the profile. No credential
    VALUES are ever read -- only the section names. Returns a sorted, de-
    duplicated list; a missing/unreadable file contributes nothing.
    """
    names = set()  # type: set
    config = (os.environ.get("AWS_CONFIG_FILE")
              or os.path.expanduser(os.path.join("~", ".aws", "config")))
    creds = (os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
             or os.path.expanduser(
                 os.path.join("~", ".aws", "credentials")))
    for name in _profiles_from_file(config, is_config=True):
        names.add(name)
    for name in _profiles_from_file(creds, is_config=False):
        names.add(name)
    return sorted(names)


def _profiles_from_file(path: str, is_config: bool) -> List[str]:
    """Scan an INI-ish AWS config file for profile section names.

    A hand-rolled header scan (not :mod:`configparser`) because AWS config
    files use nested/indented subsections (``s3 =`` blocks, ``sso_*``) that
    make a strict INI parser raise. We only need the ``[...]`` headers.
    """
    out = []  # type: List[str]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("[") or not line.endswith("]"):
                    continue
                section = line[1:-1].strip()
                if not section:
                    continue
                if is_config:
                    if section == "default":
                        out.append("default")
                    elif section.startswith("profile "):
                        name = section[len("profile "):].strip()
                        if name:
                            out.append(name)
                    # sso-session / services / etc. are not profiles.
                else:
                    out.append(section)
    except OSError:
        pass
    return out


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
    """True iff (service, subcommand) is a permitted read-only command.

    Two ways in, both closed sets: either the pair is one of the tiny,
    hand-audited :data:`READONLY_EXCEPTIONS` (read-of-data verbs that do
    not match a prefix), or it is in :data:`ALLOWED` AND its subcommand
    begins with a :data:`READONLY_VERBS` prefix. Anything else is refused.
    """
    if not isinstance(service, str) or not isinstance(subcommand, str):
        return False
    if (service, subcommand) in READONLY_EXCEPTIONS:
        return True
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

    # Optional aws-vault / SSO wrapper (e.g. `aws-vault exec <profile> --`).
    # When a wrapper is active it OWNS credential resolution, so we do NOT
    # also pass --profile to the inner aws (that would fight the wrapper).
    # The wrapper is still read-only: it only injects creds around the same
    # allow-listed aws command already vetted above.
    wrap = _aws_wrap_prefix(profile)

    argv = list(wrap)
    argv += [aws, "--output", "json", "--region", region]
    if profile and not wrap:
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
            "could not execute %r -- check the installation (or "
            "N2G_AWS_BIN / N2G_AWS_WRAP)." % (argv[0] if argv else aws))
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


def cloudtrail_lookup(attribute_key: str = "", attribute_value: str = "",
                      start: str = "", end: str = "", max_results: int = 0,
                      region: str = "us-east-1",
                      profile: str = "") -> List[Dict[str, Any]]:
    """CloudTrail ``lookup-events`` (read management events).

    Used by the RCA engine to correlate the cost step-change to a config
    action (e.g. a Karpenter scale-up / EC2NodeClass change). Optionally
    filter by a single lookup attribute (``EventName``, ``Username``,
    ``ResourceType``, ...) and a time window (``start``/``end`` are
    ISO-8601 or epoch strings passed straight to the CLI). Returns the
    ``Events`` list (possibly empty).
    """
    args = []  # type: List[str]
    if attribute_key and attribute_value:
        args += ["--lookup-attributes",
                 "AttributeKey=%s,AttributeValue=%s"
                 % (attribute_key, attribute_value)]
    if start:
        args += ["--start-time", start]
    if end:
        args += ["--end-time", end]
    if max_results:
        args += ["--max-results", str(int(max_results))]
    data = run_aws("cloudtrail", "lookup-events", args, region=region,
                   profile=profile)
    if not isinstance(data, dict):
        return []
    events = data.get("Events")
    return events if isinstance(events, list) else []


def logs_insights_query(log_group: Any, query: str,
                        start_epoch: int, end_epoch: int,
                        limit: int = 1000, poll_interval: float = 1.0,
                        max_polls: int = 60, region: str = "us-east-1",
                        profile: str = "") -> Dict[str, Any]:
    """Run a CloudWatch Logs Insights query, bounded (read-only).

    Flow (all read-of-data verbs; see ARCHITECTURE-1.9 0.8): ``start-query``
    to initiate, then a BOUNDED poll of ``get-query-results`` until the
    query is ``Complete`` (or a terminal failure), and ``stop-query`` to
    cancel if the poll budget is exhausted. Nothing is written or created.

    ``log_group`` may be a single name (``--log-group-name``) or a
    list/tuple (``--log-group-names``). ``start_epoch``/``end_epoch`` are
    UNIX seconds. Returns ``{"status", "results", "statistics",
    "query_id", "polls"}`` where ``results`` is the raw Logs Insights list
    of rows (each a list of ``{"field","value"}`` dicts). Raises
    :class:`AWSError` on a failed/timed-out query -- callers that must not
    fail (flowlogs.py) catch it and degrade.
    """
    if isinstance(log_group, (list, tuple)):
        lg_args = ["--log-group-names"] + [str(g) for g in log_group]
    else:
        lg_args = ["--log-group-name", str(log_group)]
    start_args = lg_args + [
        "--start-time", str(int(start_epoch)),
        "--end-time", str(int(end_epoch)),
        "--query-string", str(query),
        "--limit", str(int(limit))]
    started = run_aws("logs", "start-query", start_args, region=region,
                      profile=profile)
    query_id = ""
    if isinstance(started, dict):
        query_id = str(started.get("queryId") or "")
    if not query_id:
        raise AWSError(
            "CloudWatch Logs start-query returned no queryId; cannot "
            "retrieve results. Check the log group name and time window.")

    max_polls = max(1, int(max_polls))
    polls = 0
    while polls < max_polls:
        polls += 1
        res = run_aws("logs", "get-query-results", ["--query-id", query_id],
                      region=region, profile=profile)
        status = ""
        results = []  # type: List[Any]
        stats = {}  # type: Any
        if isinstance(res, dict):
            status = str(res.get("status") or "")
            r = res.get("results")
            if isinstance(r, list):
                results = r
            stats = res.get("statistics") or {}
        if status == "Complete":
            return {"status": status, "results": results,
                    "statistics": stats, "query_id": query_id,
                    "polls": polls}
        if status in ("Failed", "Cancelled", "Timeout"):
            raise AWSError(
                "CloudWatch Logs Insights query %s ended with status "
                "%s." % (query_id, status))
        if polls < max_polls:
            time.sleep(max(0.0, float(poll_interval)))

    # Poll budget exhausted while still Running/Scheduled -> cancel it and
    # report actionably (best effort; a failed cancel must not mask this).
    try:
        run_aws("logs", "stop-query", ["--query-id", query_id],
                region=region, profile=profile)
    except AWSError:
        pass
    raise AWSError(
        "CloudWatch Logs Insights query %s did not complete within %d "
        "polls; narrow the time window or raise max_polls."
        % (query_id, max_polls))


def eks_describe(cluster: str = "", region: str = "us-east-1",
                 profile: str = "") -> Dict[str, Any]:
    """Read EKS control-plane facts (read-only).

    With no ``cluster`` name, returns ``{"clusters": [...]}`` from
    ``list-clusters``. With a name, returns ``{"cluster": {...},
    "nodegroups": [{...}, ...]}`` combining ``describe-cluster`` with
    ``list-nodegroups`` + ``describe-nodegroup`` for each. Used to map the
    dominant cross-AZ ENIs/IPs back to the workload (Mimir/Loki/Tempo).
    """
    if not cluster:
        data = run_aws("eks", "list-clusters", region=region,
                       profile=profile)
        clusters = data.get("clusters") if isinstance(data, dict) else None
        return {"clusters": clusters if isinstance(clusters, list) else []}

    out = {}  # type: Dict[str, Any]
    cl = run_aws("eks", "describe-cluster", ["--name", cluster],
                 region=region, profile=profile)
    out["cluster"] = cl.get("cluster", {}) if isinstance(cl, dict) else {}
    ng = run_aws("eks", "list-nodegroups", ["--cluster-name", cluster],
                 region=region, profile=profile)
    names = ng.get("nodegroups") if isinstance(ng, dict) else None
    names = names if isinstance(names, list) else []
    groups = []  # type: List[Any]
    for name in names:
        d = run_aws("eks", "describe-nodegroup",
                    ["--cluster-name", cluster, "--nodegroup-name",
                     str(name)], region=region, profile=profile)
        groups.append(d.get("nodegroup", {}) if isinstance(d, dict) else {})
    out["nodegroups"] = groups
    return out


def _ec2_list(subcommand: str, key: str, extra: Optional[List[str]] = None,
              region: str = "us-east-1", profile: str = "") -> List[Any]:
    """Run an allow-listed ec2 describe-* and return its top-level list."""
    data = run_aws("ec2", subcommand, extra or [], region=region,
                   profile=profile)
    if not isinstance(data, dict):
        return []
    val = data.get(key)
    return val if isinstance(val, list) else []


def ec2_network_topology(region: str = "us-east-1", profile: str = "",
                         interface_ids: Optional[List[str]] = None,
                         subnet_ids: Optional[List[str]] = None
                         ) -> Dict[str, Any]:
    """Read the EC2 network topology needed for cross-AZ attribution.

    Returns ``{"network_interfaces", "subnets", "route_tables",
    "nat_gateways", "availability_zones"}`` -- the raw describe-* lists.
    ENIs map srcAddr/dstAddr -> node/pod and subnet -> AZ, so the RCA can
    prove a flow is cross-AZ (and rule out NAT/cross-region). Optionally
    narrow to specific ``interface_ids`` / ``subnet_ids``. All read-only.
    """
    eni_args = []  # type: List[str]
    if interface_ids:
        eni_args = ["--network-interface-ids"] + [
            str(i) for i in interface_ids]
    subnet_args = []  # type: List[str]
    if subnet_ids:
        subnet_args = ["--subnet-ids"] + [str(s) for s in subnet_ids]
    return {
        "network_interfaces": _ec2_list(
            "describe-network-interfaces", "NetworkInterfaces", eni_args,
            region=region, profile=profile),
        "subnets": _ec2_list(
            "describe-subnets", "Subnets", subnet_args,
            region=region, profile=profile),
        "route_tables": _ec2_list(
            "describe-route-tables", "RouteTables",
            region=region, profile=profile),
        "nat_gateways": _ec2_list(
            "describe-nat-gateways", "NatGateways",
            region=region, profile=profile),
        "availability_zones": _ec2_list(
            "describe-availability-zones", "AvailabilityZones",
            region=region, profile=profile),
    }


def elbv2_describe(region: str = "us-east-1", profile: str = "",
                   load_balancer_arns: Optional[List[str]] = None
                   ) -> Dict[str, Any]:
    """Read NLB/ALB facts for the cross-zone / per-AZ health analysis.

    Returns ``{"load_balancers", "target_groups", "target_health",
    "attributes", "listeners"}``. ``target_health`` and ``attributes`` are
    keyed by target-group ARN; ``listeners`` by load-balancer ARN. The
    per-AZ target health is what gates a "disable NLB cross-zone"
    mitigation -- an AZ with a single healthy target must NOT be black-
    holed (see 0.6). Optionally narrow to specific
    ``load_balancer_arns``. All read-only.
    """
    lb_args = []  # type: List[str]
    if load_balancer_arns:
        lb_args = ["--load-balancer-arns"] + [
            str(a) for a in load_balancer_arns]
    lbs = _elbv2_list("describe-load-balancers", "LoadBalancers", lb_args,
                      region=region, profile=profile)
    # describe-target-groups takes a SINGLE --load-balancer-arn, not the
    # plural filter; discover all target groups instead of misapplying it.
    tgs = _elbv2_list("describe-target-groups", "TargetGroups", None,
                      region=region, profile=profile)

    out = {"load_balancers": lbs, "target_groups": tgs,
           "target_health": {}, "attributes": {},
           "listeners": {}}  # type: Dict[str, Any]

    for tg in tgs:
        if not isinstance(tg, dict):
            continue
        arn = tg.get("TargetGroupArn")
        if not arn:
            continue
        out["target_health"][arn] = _elbv2_list(
            "describe-target-health", "TargetHealthDescriptions",
            ["--target-group-arn", str(arn)], region=region,
            profile=profile)
        out["attributes"][arn] = _elbv2_list(
            "describe-target-group-attributes", "Attributes",
            ["--target-group-arn", str(arn)], region=region,
            profile=profile)

    for lb in lbs:
        if not isinstance(lb, dict):
            continue
        arn = lb.get("LoadBalancerArn")
        if not arn:
            continue
        out["listeners"][arn] = _elbv2_list(
            "describe-listeners", "Listeners",
            ["--load-balancer-arn", str(arn)], region=region,
            profile=profile)
    return out


def _elbv2_list(subcommand: str, key: str,
                extra: Optional[List[str]] = None,
                region: str = "us-east-1", profile: str = "") -> List[Any]:
    """Run an allow-listed elbv2 describe-* and return its top-level list."""
    data = run_aws("elbv2", subcommand, extra or [], region=region,
                   profile=profile)
    if not isinstance(data, dict):
        return []
    val = data.get(key)
    return val if isinstance(val, list) else []
