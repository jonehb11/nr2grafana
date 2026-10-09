"""NRQL -> Grafana CloudWatch targets (SEAM-CW).

Covers ``FROM Metric`` queries over New Relic's CloudWatch Metric Streams
names (``aws.rds.CPUUtilization``, ``aws.sqs.NumberOfMessagesSent``, ...)
and the older AWS polling-integration sample events (``DatastoreSample``,
``QueueSample``, ``ServerlessSample``, ``ComputeSample``, ...). These
metrics never reach Mimir in a typical LGTM stack, so a PromQL
translation returns no data (failure class F4); the honest target is the
Grafana CloudWatch datasource.

The result is a :class:`~nr2grafana.translate.common.Translation` with
``datasource="cloudwatch"`` and an empty ``expr``. The CloudWatch query
itself lives in an ADDITIVE instance attribute ``cw`` (``Translation`` is
a plain dataclass without ``__slots__``; ``translate/common.py`` belongs
to another module, so the class is not extended there)::

    t.cw = {
        "namespace": "AWS/RDS",
        "metricName": "CPUUtilization",
        "statistic": "Average",          # Average|Sum|Maximum|Minimum|
                                         # SampleCount|pN
        "dimensions": {"DBInstanceIdentifier": ["*"]},
        "dimension_keys": ["DBInstanceIdentifier"],   # FACET dimensions
        "region": "default",
        "queryMode": "Metrics",
        "metricEditorMode": 0,           # 0 = builder, 1 = code (SEARCH)
        "expression": "",                # SEARCH(...) when mode is 1
    }

Readers use ``getattr(t, "cw", None)``. Sibling targets (several SELECT
items, ``percentile(m, 95, 99)``) are ``t.extra`` entries carrying their
own ``cw``. The builder turns ``cw`` into a real CloudWatch target.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..nrql.parser import (
    Attr, BoolOp, Cmp, Cond, Func, InList, Lit, NotOp, NrqlQuery,
    NullCheck, SelectItem, Star,
)
from .common import (
    APPROXIMATE, NEEDS_REVIEW, Translation, Untranslatable, grafana_var,
    is_nr_variable,
)

try:  # SEAM-RENDER (metrics agent); a local copy covers its absence.
    from .common import render_value as _seam_render_value
except ImportError:  # pragma: no cover - depends on sibling module state
    _seam_render_value = None


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

# NR metric-stream service segment (aws.<svc>.<Metric>) -> CloudWatch
# namespace. Extensible via cfg["cloudwatch_namespaces"].
NAMESPACES: Dict[str, str] = {
    "rds": "AWS/RDS", "sqs": "AWS/SQS", "lambda": "AWS/Lambda",
    "ec2": "AWS/EC2", "ebs": "AWS/EBS", "elb": "AWS/ELB",
    "alb": "AWS/ApplicationELB", "applicationelb": "AWS/ApplicationELB",
    "nlb": "AWS/NetworkELB", "networkelb": "AWS/NetworkELB",
    "dynamodb": "AWS/DynamoDB", "s3": "AWS/S3", "kinesis": "AWS/Kinesis",
    "sns": "AWS/SNS", "ecs": "AWS/ECS", "eks": "ContainerInsights",
    "containerinsights": "ContainerInsights",
    "elasticache": "AWS/ElastiCache", "apigateway": "AWS/ApiGateway",
    "cloudfront": "AWS/CloudFront", "firehose": "AWS/Firehose",
    "es": "AWS/ES", "elasticsearch": "AWS/ES", "opensearch": "AWS/ES",
    "redshift": "AWS/Redshift", "efs": "AWS/EFS", "states": "AWS/States",
    "stepfunctions": "AWS/States", "kafka": "AWS/Kafka", "msk": "AWS/Kafka",
    "natgateway": "AWS/NATGateway", "route53": "AWS/Route53",
    "ses": "AWS/SES", "billing": "AWS/Billing",
    "autoscaling": "AWS/AutoScaling", "docdb": "AWS/DocDB",
    "neptune": "AWS/Neptune", "mq": "AWS/AmazonMQ", "amazonmq": "AWS/AmazonMQ",
    "vpn": "AWS/VPN", "transitgateway": "AWS/TransitGateway",
    "usage": "AWS/Usage", "waf": "AWS/WAFV2", "wafv2": "AWS/WAFV2",
    "ec2spot": "AWS/EC2Spot", "fsx": "AWS/FSx", "glue": "AWS/Glue",
    "eventbridge": "AWS/Events", "events": "AWS/Events",
    "cognito": "AWS/Cognito", "ebsvolume": "AWS/EBS",
}

# The dimension that identifies one resource in each namespace; used when
# a query has no dimension filter or FACET (wildcard / SEARCH schema).
PRIMARY_DIMENSION: Dict[str, str] = {
    "AWS/RDS": "DBInstanceIdentifier", "AWS/SQS": "QueueName",
    "AWS/Lambda": "FunctionName", "AWS/EC2": "InstanceId",
    "AWS/EBS": "VolumeId", "AWS/ELB": "LoadBalancerName",
    "AWS/ApplicationELB": "LoadBalancer", "AWS/NetworkELB": "LoadBalancer",
    "AWS/DynamoDB": "TableName", "AWS/S3": "BucketName",
    "AWS/Kinesis": "StreamName", "AWS/SNS": "TopicName",
    "AWS/ECS": "ServiceName", "ContainerInsights": "ClusterName",
    "AWS/ElastiCache": "CacheClusterId", "AWS/ApiGateway": "ApiName",
    "AWS/CloudFront": "DistributionId",
    "AWS/Firehose": "DeliveryStreamName", "AWS/ES": "DomainName",
    "AWS/Redshift": "ClusterIdentifier", "AWS/EFS": "FileSystemId",
    "AWS/States": "StateMachineArn", "AWS/Kafka": "Cluster Name",
    "AWS/NATGateway": "NatGatewayId", "AWS/Billing": "Currency",
    "AWS/AutoScaling": "AutoScalingGroupName",
    "AWS/DocDB": "DBClusterIdentifier", "AWS/Neptune": "DBClusterIdentifier",
    "AWS/AmazonMQ": "Broker", "AWS/Events": "RuleName",
    "AWS/WAFV2": "WebACL", "AWS/FSx": "FileSystemId",
}

# Dimension names as CloudWatch spells them, keyed by lower-case. NR
# lower-camels some of them (dbClusterIdentifier) and the polling
# integrations expose provider.<camelCase> attributes.
DIMENSION_CASE: Dict[str, str] = {
    "dbinstanceidentifier": "DBInstanceIdentifier",
    "dbclusteridentifier": "DBClusterIdentifier",
    "databaseclass": "DatabaseClass", "engine": "Engine",
    "enginename": "EngineName", "queuename": "QueueName",
    "functionname": "FunctionName", "resource": "Resource",
    "executedversion": "ExecutedVersion", "instanceid": "InstanceId",
    "instancetype": "InstanceType", "imageid": "ImageId",
    "autoscalinggroupname": "AutoScalingGroupName",
    "volumeid": "VolumeId", "loadbalancer": "LoadBalancer",
    "loadbalancername": "LoadBalancerName", "targetgroup": "TargetGroup",
    "availabilityzone": "AvailabilityZone", "tablename": "TableName",
    "globalsecondaryindexname": "GlobalSecondaryIndexName",
    "operation": "Operation", "bucketname": "BucketName",
    "storagetype": "StorageType", "filterid": "FilterId",
    "streamname": "StreamName", "shardid": "ShardId",
    "topicname": "TopicName", "clustername": "ClusterName",
    "servicename": "ServiceName", "cacheclusterid": "CacheClusterId",
    "cachenodeid": "CacheNodeId", "apiname": "ApiName", "stage": "Stage",
    "method": "Method", "distributionid": "DistributionId",
    "region": "Region", "deliverystreamname": "DeliveryStreamName",
    "domainname": "DomainName", "clientid": "ClientId",
    "clusteridentifier": "ClusterIdentifier", "nodeid": "NodeId",
    "filesystemid": "FileSystemId", "statemachinearn": "StateMachineArn",
    "natgatewayid": "NatGatewayId", "currency": "Currency",
    "linkedaccount": "LinkedAccount",
    "podname": "PodName", "namespace": "Namespace", "nodename": "NodeName",
    "service": "Service", "class": "Class", "type": "Type",
    "broker": "Broker", "queue": "Queue", "topic": "Topic",
    "rulename": "RuleName", "webacl": "WebACL", "rule": "Rule",
    "tunnelipaddress": "TunnelIpAddress", "vpnid": "VpnId",
    "transitgateway": "TransitGateway", "cluster name": "Cluster Name",
    "broker id": "Broker ID",
}

# Metric names whose canonical CloudWatch spelling is not "first letter
# upper-cased", keyed by lower-case (polling-integration attributes are
# lowerCamel: provider.cpuUtilization.Average).
METRIC_CASE: Dict[str, str] = {
    # NR polling-integration attribute names that humans remapped to the
    # CloudWatch metric they stand for.
    "allocatedstoragebytes": "AllocatedStorage",
    "cpuutilization": "CPUUtilization", "cpucreditusage": "CPUCreditUsage",
    "cpucreditbalance": "CPUCreditBalance",
    "cpusurpluscreditbalance": "CPUSurplusCreditBalance",
    "cpusurpluscreditscharged": "CPUSurplusCreditsCharged",
    "ebsreadbytes": "EBSReadBytes", "ebswritebytes": "EBSWriteBytes",
    "ebsreadops": "EBSReadOps", "ebswriteops": "EBSWriteOps",
    "ebsbytebalance": "EBSByteBalance%", "ebsiobalance": "EBSIOBalance%",
    "httpcode_elb_5xx_count": "HTTPCode_ELB_5XX_Count",
    "httpcode_elb_4xx_count": "HTTPCode_ELB_4XX_Count",
    "httpcode_target_5xx_count": "HTTPCode_Target_5XX_Count",
    "httpcode_target_4xx_count": "HTTPCode_Target_4XX_Count",
    "httpcode_target_2xx_count": "HTTPCode_Target_2XX_Count",
    "httpcode_target_3xx_count": "HTTPCode_Target_3XX_Count",
    "httpcode_backend_5xx": "HTTPCode_Backend_5XX",
    "httpcode_backend_4xx": "HTTPCode_Backend_4XX",
    "httpcode_backend_2xx": "HTTPCode_Backend_2XX",
    "httpcode_elb_5xx": "HTTPCode_ELB_5XX",
    "httpcode_elb_4xx": "HTTPCode_ELB_4XX",
    "jvmmemorypressure": "JVMMemoryPressure",
    "iteratoragemilliseconds": "IteratorAgeMilliseconds",
    "readiops": "ReadIOPS", "writeiops": "WriteIOPS",
    "diskqueuedepth": "DiskQueueDepth", "freeablememory": "FreeableMemory",
    "freestoragespace": "FreeStorageSpace",
    "databaseconnections": "DatabaseConnections",
    "readlatency": "ReadLatency", "writelatency": "WriteLatency",
    "readthroughput": "ReadThroughput", "writethroughput": "WriteThroughput",
    "networkreceivethroughput": "NetworkReceiveThroughput",
    "networktransmitthroughput": "NetworkTransmitThroughput",
    "approximatenumberofmessagesvisible":
        "ApproximateNumberOfMessagesVisible",
    "approximatenumberofmessagesnotvisible":
        "ApproximateNumberOfMessagesNotVisible",
    "approximatenumberofmessagesdelayed":
        "ApproximateNumberOfMessagesDelayed",
    "approximateageofoldestmessage": "ApproximateAgeOfOldestMessage",
    "numberofmessagessent": "NumberOfMessagesSent",
    "numberofmessagesreceived": "NumberOfMessagesReceived",
    "numberofmessagesdeleted": "NumberOfMessagesDeleted",
    "numberofemptyreceives": "NumberOfEmptyReceives",
    "sentmessagesize": "SentMessageSize",
    "invocations": "Invocations", "errors": "Errors", "throttles": "Throttles",
    "duration": "Duration", "concurrentexecutions": "ConcurrentExecutions",
    "iteratorage": "IteratorAge",
}

# Polling-integration sample events -> default service segment.
AWS_SAMPLE_EVENTS: Dict[str, str] = {
    "datastoresample": "rds", "queuesample": "sqs",
    "serverlesssample": "lambda", "computesample": "ec2",
    "loadbalancersample": "elb", "blockdevicesample": "ebs",
}

# NR polling-integration ``provider`` values -> service segment.
PROVIDER_SERVICES: Dict[str, str] = {
    "rdsdbinstance": "rds", "rdsdbcluster": "rds", "sqsqueue": "sqs",
    "lambdafunction": "lambda", "ec2instance": "ec2", "elb": "elb",
    "alb": "alb", "nlb": "nlb", "dynamodbtable": "dynamodb",
    "s3bucket": "s3", "kinesisstream": "kinesis", "snstopic": "sns",
    "elasticacheredisnode": "elasticache",
    "elasticacheredisnodecluster": "elasticache",
    "elasticachememcachednode": "elasticache", "ebsvolume": "ebs",
    "apigatewayapi": "apigateway", "ecscluster": "ecs",
    "ecsservice": "ecs", "cloudfrontdistribution": "cloudfront",
    "redshiftcluster": "redshift", "efsfilesystem": "efs",
}

# NRQL aggregation -> CloudWatch statistic.
STATISTICS: Dict[str, str] = {
    "average": "Average", "avg": "Average", "max": "Maximum",
    "min": "Minimum", "sum": "Sum", "count": "SampleCount",
    "latest": "Average",
}
_VALID_STATS = {"Average", "Sum", "Maximum", "Minimum", "SampleCount"}
# Statistic -> Metric Math aggregator wrapped around SEARCH(...) when the
# NR query aggregates across every matching resource (no FACET).
_SEARCH_AGG = {"Average": "AVG", "Sum": "SUM", "Maximum": "MAX",
               "Minimum": "MIN", "SampleCount": "SUM"}

_REGION_ATTRS = {"aws.region", "awsregion", "region", "aws.awsregion"}
_ACCOUNT_ATTRS = {"aws.accountid", "awsaccountid", "accountid",
                  "aws.account.id", "provider.accountid"}
_ENTITY_ATTRS = {"displayname", "entityname", "entity.name",
                 "provider.name", "name"}

_UNIT_HINTS = (
    (re.compile(r"(?i)(utilization|percent|pressure|balance%)$"), "percent"),
    (re.compile(r"(?i)(latency)$"), "s"),
    (re.compile(r"(?i)(duration|milliseconds|initduration)$"), "ms"),
    (re.compile(r"(?i)(bytes|memory|storagespace|size)$"), "bytes"),
    (re.compile(r"(?i)(throughput)$"), "Bps"),
    (re.compile(r"(?i)(ageofoldestmessage|age)$"), "s"),
)

_SEARCH_WILDCARDS = re.compile(r"[%_]+")


# ---------------------------------------------------------------------------
# Routing helper
# ---------------------------------------------------------------------------

def _innermost_name(node: Any) -> Optional[str]:
    """Metric name argument of agg(...) / rate(sum(m)) / filter(agg(m))."""
    seen = 0
    while isinstance(node, Func) and seen < 8:
        seen += 1
        if not node.args:
            return None
        node = node.args[0]
    if isinstance(node, Attr):
        return node.name
    if isinstance(node, Lit) and isinstance(node.value, str):
        return node.value
    return None


def is_cloudwatch_query(q: NrqlQuery) -> bool:
    """True for FROM Metric with an aws.* metric name in the first
    aggregation, and for the AWS polling-integration sample events."""
    et = (q.from_[0] if q.from_ else "Metric").lower()
    if et in AWS_SAMPLE_EVENTS:
        return True
    if et != "metric":
        return False
    for item in q.select:
        if isinstance(item.expr, Func):
            name = _innermost_name(item.expr) or ""
            return name.lower().startswith("aws.")
    return False


# ---------------------------------------------------------------------------
# Value rendering (SEAM-RENDER with a local fallback)
# ---------------------------------------------------------------------------

def _render_value(node: Any, cfg: Dict[str, Any]) -> Tuple[str, str]:
    """-> (text, kind) with kind in literal|var|mixed; concat('p-',
    {{env}}) -> 'p-$env'. Never a Python repr."""
    if _seam_render_value is not None:
        try:
            text, kind = _seam_render_value(node, cfg)
            if isinstance(text, str) and isinstance(kind, str):
                return text, kind
        except Exception:  # noqa: BLE001 - fall back to the local copy
            pass
    return _local_render(node, cfg)


def _local_render(node: Any, cfg: Dict[str, Any]) -> Tuple[str, str]:
    var = is_nr_variable(node)
    if var:
        return "$" + grafana_var(var, cfg), "var"
    if isinstance(node, Lit):
        v = node.value
        if isinstance(v, bool):
            return ("true" if v else "false"), "literal"
        if isinstance(v, float) and v == int(v):
            return str(int(v)), "literal"
        return ("" if v is None else str(v)), "literal"
    if isinstance(node, Attr):
        return node.name, "literal"
    if isinstance(node, Func) and node.name == "concat":
        parts: List[str] = []
        kinds = set()
        for arg in node.args:
            text, kind = _local_render(arg, cfg)
            parts.append(text)
            kinds.add(kind)
        kind = "literal"
        if "var" in kinds or "mixed" in kinds:
            kind = "mixed" if len(parts) > 1 else "var"
        return "".join(parts), kind
    return (node.name if isinstance(node, Func)
            else type(node).__name__), "unsupported"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def fix_dimension_case(name: str) -> str:
    """dbClusterIdentifier -> DBClusterIdentifier (case-fix table, else
    upper-case the first letter)."""
    key = name.strip().lower()
    if key in DIMENSION_CASE:
        return DIMENSION_CASE[key]
    return name[:1].upper() + name[1:] if name else name


def fix_metric_case(name: str) -> str:
    key = name.strip().lower()
    if key in METRIC_CASE:
        return METRIC_CASE[key]
    return name[:1].upper() + name[1:] if name else name


def namespace_for(service: str, cfg: Dict[str, Any]) -> Tuple[str, bool]:
    """(namespace, known) for an NR service segment."""
    svc = service.lower()
    user = cfg.get("cloudwatch_namespaces") or {}
    for table in (user, NAMESPACES):
        for k, v in table.items():
            if str(k).lower() == svc:
                return str(v), True
    return "AWS/" + service[:1].upper() + service[1:], False


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

class _DimFilter:
    """One WHERE predicate on a dimension."""

    def __init__(self, name: str, values: List[str], negated: bool = False,
                 partial: bool = False, has_var: bool = False):
        self.name = name
        self.values = values
        self.negated = negated
        self.partial = partial      # LIKE pattern -> SEARCH token match
        self.has_var = has_var

    @property
    def pinned(self) -> bool:
        """A single exact value: expressible in the builder editor."""
        return len(self.values) == 1 and not self.negated \
            and not self.partial


class _Ctx:
    def __init__(self, q: NrqlQuery, cfg: Dict[str, Any], t: Translation):
        self.q = q
        self.cfg = cfg
        self.t = t
        self.region = str(cfg.get("cloudwatch_region") or "default")
        self.period = int(cfg.get("cloudwatch_period") or 300)
        self.service = ""        # from FROM <Sample> / provider = '...'
        self.filters: List[_DimFilter] = []
        self.facet_dims: List[str] = []   # "__primary__" resolved later
        self.embedded: List[_DimFilter] = []

    def note(self, msg: str, conf: Optional[str] = None) -> None:
        self.t.note(msg, conf)


# ---------------------------------------------------------------------------
# WHERE / FACET collection
# ---------------------------------------------------------------------------

def _dim_name(attr: str, ctx: _Ctx) -> Optional[str]:
    """NR attribute -> CloudWatch dimension name, or None when the attribute
    is not a dimension (handled or dropped by the caller)."""
    low = attr.lower()
    if low in _REGION_ATTRS or low in _ACCOUNT_ATTRS or low == "provider":
        return None
    if low in _ENTITY_ATTRS:
        return "__primary__"
    parts = attr.split(".")
    if low.startswith("aws.") and len(parts) >= 3:
        # aws.<svc>.<Dim>  (metric streams)
        return fix_dimension_case(".".join(parts[2:]))
    if low.startswith("aws.") and len(parts) == 2:
        return fix_dimension_case(parts[1])
    if low.startswith("provider.") and len(parts) == 2:
        return fix_dimension_case(parts[1])
    if low.startswith("tags.") or low.startswith("label."):
        return None
    if "." not in attr:
        return fix_dimension_case(attr)
    return None


def _collect_where(cond: Optional[Cond], ctx: _Ctx, out: List[_DimFilter],
                   negate: bool = False) -> None:
    if cond is None:
        return
    if isinstance(cond, BoolOp):
        if cond.op == "or" and not negate:
            merged = _merge_or(cond, ctx)
            if merged is not None:
                out.append(merged)
                return
            ctx.note("an OR clause in WHERE spans different attributes "
                     "or predicate shapes; CloudWatch dimensions cannot "
                     "express it, clause DROPPED - verify the filter",
                     NEEDS_REVIEW)
            return
        if cond.op == "and" and negate:
            ctx.note("negated AND in WHERE cannot become dimension "
                     "filters; clause dropped - verify the filter",
                     NEEDS_REVIEW)
            return
        for item in cond.items:
            _collect_where(item, ctx, out, negate)
        return
    if isinstance(cond, NotOp):
        _collect_where(cond.item, ctx, out, not negate)
        return
    if isinstance(cond, Cmp):
        f = _cmp_filter(cond, ctx, negate)
        if f is not None:
            out.append(f)
        return
    if isinstance(cond, InList):
        if not isinstance(cond.left, Attr):
            ctx.note("unsupported IN list dropped from the CloudWatch "
                     "filter", NEEDS_REVIEW)
            return
        name = _dim_name(cond.left.name, ctx)
        if name is None:
            _note_non_dimension(cond.left.name, ctx)
            return
        values: List[str] = []
        has_var = False
        for v in cond.values:
            text, kind = _render_value(v, ctx.cfg)
            if kind == "unsupported":
                ctx.note("IN value %r is not a literal/variable; skipped"
                         % text, NEEDS_REVIEW)
                continue
            values.append(text)
            has_var = has_var or kind != "literal"
        if not values:
            return
        out.append(_DimFilter(name, values, negated=cond.negated != negate,
                              has_var=has_var))
        return
    if isinstance(cond, NullCheck):
        ctx.note("IS [NOT] NULL has no CloudWatch dimension equivalent; "
                 "predicate dropped", NEEDS_REVIEW)
        return
    ctx.note("unsupported WHERE construct dropped: %r" % (cond,),
             NEEDS_REVIEW)


def _note_non_dimension(attr: str, ctx: _Ctx) -> None:
    low = attr.lower()
    if low in _ACCOUNT_ATTRS:
        ctx.t.notes.append(
            "WHERE %s dropped: the AWS account is selected by the "
            "CloudWatch datasource's credentials (one datasource per "
            "account, or use the datasource's account picker)" % attr)
        return
    if low == "provider":
        return
    ctx.note("WHERE on %r is not a CloudWatch dimension (tags and entity "
             "metadata are not queryable in CloudWatch); predicate "
             "dropped - verify the filter" % attr, NEEDS_REVIEW)


def _cmp_filter(cmp_: Cmp, ctx: _Ctx, negate: bool) -> Optional[_DimFilter]:
    if not isinstance(cmp_.left, Attr):
        ctx.note("comparison with a non-attribute left side dropped from "
                 "the CloudWatch filter", NEEDS_REVIEW)
        return None
    attr = cmp_.left.name
    low = attr.lower()
    op = cmp_.op
    if low == "provider":
        if op == "=" and isinstance(cmp_.right, Lit):
            prov = str(cmp_.right.value).lower()
            ctx.service = PROVIDER_SERVICES.get(prov, ctx.service)
        return None
    if low in _REGION_ATTRS:
        if op == "=":
            text, _ = _render_value(cmp_.right, ctx.cfg)
            ctx.region = text
        else:
            ctx.note("only 'region = value' can select the CloudWatch "
                     "region; %r predicate dropped" % op, NEEDS_REVIEW)
        return None
    name = _dim_name(attr, ctx)
    if name is None:
        _note_non_dimension(attr, ctx)
        return None
    if op in ("=", "!="):
        text, kind = _render_value(cmp_.right, ctx.cfg)
        if kind == "unsupported":
            ctx.note("value expression %r for dimension %s is not a "
                     "literal/variable/concat(); predicate dropped"
                     % (text, name), NEEDS_REVIEW)
            return None
        neg = (op == "!=") != negate
        return _DimFilter(name, [text], negated=neg,
                          has_var=kind != "literal")
    if op in ("LIKE", "NOT LIKE"):
        text, kind = _render_value(cmp_.right, ctx.cfg)
        neg = (op == "NOT LIKE") != negate
        token = _SEARCH_WILDCARDS.sub(" ", text).strip(" -./:")
        ctx.note("LIKE %r approximated by a CloudWatch SEARCH partial "
                 "(token) match on %r; verify the matched resources"
                 % (text, token), NEEDS_REVIEW)
        return _DimFilter(name, [token], negated=neg, partial=True,
                          has_var=kind != "literal")
    ctx.note("operator %r on dimension %s has no CloudWatch equivalent; "
             "predicate dropped" % (op, name), NEEDS_REVIEW)
    return None


def _merge_or(cond: BoolOp, ctx: _Ctx) -> Optional[_DimFilter]:
    name: Optional[str] = None
    values: List[str] = []
    has_var = False
    for item in cond.items:
        if isinstance(item, Cmp) and isinstance(item.left, Attr) \
                and item.op == "=":
            n = _dim_name(item.left.name, ctx)
            text, kind = _render_value(item.right, ctx.cfg)
            if kind == "unsupported":
                return None
            vals = [text]
            has_var = has_var or kind != "literal"
        elif isinstance(item, InList) and isinstance(item.left, Attr) \
                and not item.negated:
            n = _dim_name(item.left.name, ctx)
            vals = []
            for v in item.values:
                text, kind = _render_value(v, ctx.cfg)
                vals.append(text)
                has_var = has_var or kind != "literal"
        else:
            return None
        if n is None:
            return None
        if name is None:
            name = n
        elif name != n:
            return None
        values.extend(vals)
    if name is None or not values:
        return None
    return _DimFilter(name, values, has_var=has_var)


def _collect_facet(ctx: _Ctx) -> None:
    for item in ctx.q.facet:
        if isinstance(item.expr, Attr):
            name = _dim_name(item.expr.name, ctx)
            if name is None:
                ctx.note("FACET %r is not a CloudWatch dimension; grouping "
                         "dropped" % item.expr.name, NEEDS_REVIEW)
                continue
            if name not in ctx.facet_dims:
                ctx.facet_dims.append(name)
        elif isinstance(item.expr, Func):
            ctx.note("FACET %s(...) has no CloudWatch dimension "
                     "equivalent; grouping dropped" % item.expr.name,
                     NEEDS_REVIEW)
        else:
            ctx.note("unsupported FACET expression dropped", NEEDS_REVIEW)


# ---------------------------------------------------------------------------
# SELECT items -> (namespace, metricName, statistic) specs
# ---------------------------------------------------------------------------

class _Spec:
    def __init__(self, namespace: str, metric: str, statistic: str,
                 legend: str, embedded: List[_DimFilter],
                 known_ns: bool):
        self.namespace = namespace
        self.metric = metric
        self.statistic = statistic
        self.legend = legend
        self.embedded = embedded
        self.known_ns = known_ns
        self.force_search = False
        # Metric Math: operand queries carry an id (m1, m2, ...) and are
        # hidden; the combining query carries the expression instead.
        self.math_id = ""
        self.math_expr = ""


def _math_specs(fn: Func, item: SelectItem, ctx: _Ctx,
                counter: List[int]) -> Tuple[str, List["_Spec"]]:
    """agg(x) / agg(y), agg(x) + agg(y) - ...: one CloudWatch query per
    leaf aggregation (ids m1, m2, ...) plus a Metric Math expression over
    them -> (expression, operand specs)."""
    if isinstance(fn, Func) and fn.name in ("_ratio", "_arith"):
        op = "/" if fn.name == "_ratio" else str(
            getattr(fn.args[2], "value", "+") if len(fn.args) > 2 else "+")
        left, lspecs = _math_specs(fn.args[0], item, ctx, counter)
        right, rspecs = _math_specs(fn.args[1], item, ctx, counter)
        if op in ("*", "/") and len(rspecs) > 1:
            right = "(%s)" % right
        return "%s %s %s" % (left, op, right), lspecs + rspecs
    if not isinstance(fn, Func):
        raise Untranslatable(
            "a SELECT arithmetic over CloudWatch metrics must combine "
            "aggregations (agg(x) / agg(y), agg(x) + agg(y))")
    specs = _item_specs(SelectItem(expr=fn), ctx)
    if len(specs) != 1:
        raise Untranslatable(
            "a Metric Math operand must be a single statistic "
            "(percentile(m, 95) with one percentile)")
    counter[0] += 1
    specs[0].math_id = "m%d" % counter[0]
    return specs[0].math_id, specs


def _metric_ref(name: str, ctx: _Ctx) -> Tuple[str, str, bool, str]:
    """NR metric name -> (namespace, metricName, known, stat_from_name)."""
    parts = name.split(".")
    low = name.lower()
    if low.startswith("aws.") and len(parts) >= 3:
        ns, known = namespace_for(parts[1], ctx.cfg)
        metric = ".".join(parts[2:])
        # Metric Streams names already carry CloudWatch's case; only the
        # explicit alias table (polling-integration attribute names such
        # as allocatedStorageBytes) rewrites them.
        metric = METRIC_CASE.get(metric.lower(), metric)
        return ns, metric, known, ""
    if low.startswith("provider.") and len(parts) >= 2:
        # provider.cpuUtilization.Average (polling integration)
        metric = parts[1]
        stat = parts[2] if len(parts) > 2 else ""
        stat = {"average": "Average", "sum": "Sum", "maximum": "Maximum",
                "minimum": "Minimum", "samplecount": "SampleCount",
                "max": "Maximum", "min": "Minimum"}.get(stat.lower(), "")
        svc = ctx.service or AWS_SAMPLE_EVENTS.get(
            (ctx.q.from_[0] if ctx.q.from_ else "").lower(), "")
        if not svc:
            raise Untranslatable(
                "cannot tell which AWS service %r belongs to; add WHERE "
                "provider = '<RdsDbInstance|SqsQueue|...>'" % name)
        ns, known = namespace_for(svc, ctx.cfg)
        return ns, fix_metric_case(metric), known, stat
    if ctx.service:
        ns, known = namespace_for(ctx.service, ctx.cfg)
        return ns, fix_metric_case(parts[-1]), known, ""
    raise Untranslatable(
        "%r is not an AWS CloudWatch metric (expected aws.<service>."
        "<MetricName> or provider.<metric>.<Statistic>)" % name)


def _item_specs(item: SelectItem, ctx: _Ctx) -> List[_Spec]:
    fn = item.expr
    if not isinstance(fn, Func):
        raise Untranslatable("CloudWatch needs an aggregation such as "
                             "average(aws.rds.CPUUtilization)")
    embedded: List[_DimFilter] = []
    force_search = False
    agg_fn = fn
    if fn.name == "filter":
        inner = fn.args[0] if fn.args else None
        if not isinstance(inner, Func):
            raise Untranslatable("filter() needs an inner aggregation")
        _collect_where(fn.where, ctx, embedded)
        force_search = True
        agg_fn = inner
    if agg_fn.name in ("_ratio", "_arith"):
        expr, operands = _math_specs(agg_fn, item, ctx, [0])
        for s in operands:
            s.force_search = s.force_search or force_search
            s.embedded = list(embedded) + list(s.embedded)
        math = _Spec("", "", "", item.alias or expr, [], True)
        math.math_expr = expr
        ctx.note("SELECT arithmetic over CloudWatch metrics emitted as a "
                 "Metric Math expression %r over hidden operand queries "
                 "%s" % (expr, ", ".join(s.math_id for s in operands)),
                 APPROXIMATE)
        return operands + [math]
    if agg_fn.name in ("uniquecount", "cardinality", "funnel",
                       "histogram", "apdex", "percentage", "if"):
        raise Untranslatable(
            "%s() has no CloudWatch statistic equivalent" % agg_fn.name)
    rate_note = ""
    if agg_fn.name == "rate":
        inner = agg_fn.args[0] if agg_fn.args else None
        if not isinstance(inner, Func):
            raise Untranslatable("rate() needs an inner aggregation")
        rate_note = ("rate(%s(..)) approximated by the per-period %s "
                     "statistic; CloudWatch has no per-second rate - "
                     "divide by the period with Metric Math if needed"
                     % (inner.name, STATISTICS.get(inner.name, "Sum")))
        agg_fn = inner
    name = _innermost_name(agg_fn)
    if not name:
        raise Untranslatable("%s() needs a metric name argument"
                             % agg_fn.name)
    ns, metric, known, stat_from_name = _metric_ref(name, ctx)
    if not known:
        ctx.note("AWS service %r is not in the namespace map; guessed "
                 "namespace %r - set cloudwatch_namespaces in the config "
                 "if it is wrong" % (name.split(".")[1]
                                     if name.lower().startswith("aws.")
                                     else ctx.service, ns), NEEDS_REVIEW)
    specs: List[_Spec] = []
    alias = item.alias or ""
    if agg_fn.name == "percentile":
        pcts = [a.value for a in agg_fn.args[1:]
                if isinstance(a, Lit) and isinstance(a.value, (int, float))]
        if not pcts:
            pcts = [95]
        for p in pcts:
            pn = "p%g" % p
            specs.append(_Spec(ns, metric, pn, alias or pn, embedded,
                               known))
    elif agg_fn.name == "median":
        specs.append(_Spec(ns, metric, "p50", alias or "p50", embedded,
                           known))
    else:
        stat = STATISTICS.get(agg_fn.name)
        if stat is None:
            raise Untranslatable(
                "%s() has no CloudWatch statistic equivalent (use "
                "average/sum/max/min/count/latest/percentile)"
                % agg_fn.name)
        if stat_from_name and stat_from_name in _VALID_STATS:
            stat = stat_from_name
        if agg_fn.name == "latest":
            ctx.note("latest() has no CloudWatch statistic; Average of the "
                     "most recent period is used - set the panel reducer "
                     "to 'Last'", APPROXIMATE)
        specs.append(_Spec(ns, metric, stat, alias, embedded, known))
    if rate_note:
        ctx.note(rate_note, APPROXIMATE)
    if item.multiplier and item.multiplier != 1:
        ctx.note("SELECT arithmetic '* %g' is not applied to the "
                 "CloudWatch target; add a Metric Math query or a panel "
                 "unit scale" % item.multiplier, NEEDS_REVIEW)
    for s in specs:
        s.force_search = force_search
    return specs


# ---------------------------------------------------------------------------
# Payload assembly
# ---------------------------------------------------------------------------

def _search_quote(value: str) -> str:
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _search_term(f: _DimFilter) -> str:
    if f.partial:
        term = " ".join(f.values)
        return ("NOT %s" % term) if f.negated else term
    if len(f.values) == 1:
        term = "%s=%s" % (f.name, _search_quote(f.values[0]))
        return ("NOT %s" % term) if f.negated else term
    alts = " OR ".join("%s=%s" % (f.name, _search_quote(v))
                       for v in f.values)
    term = "(%s)" % alts
    return ("NOT %s" % term) if f.negated else term


def _build_payload(spec: _Spec, ctx: _Ctx) -> Dict[str, Any]:
    primary = PRIMARY_DIMENSION.get(spec.namespace, "")
    filters = list(ctx.filters) + list(spec.embedded)

    def resolve(name: str) -> str:
        if name != "__primary__":
            return name
        if not primary:
            ctx.note("entity name filter/grouping needs the namespace's "
                     "identifying dimension, which is unknown for %r; "
                     "set it manually" % spec.namespace, NEEDS_REVIEW)
            return "Name"
        return primary

    filters = [_DimFilter(resolve(f.name), list(f.values), f.negated,
                          f.partial, f.has_var) for f in filters]
    facet = []
    for d in ctx.facet_dims:
        d = resolve(d)
        if d not in facet:
            facet.append(d)

    pinned = [f for f in filters if f.pinned]
    unpinned = [f for f in filters if not f.pinned]
    search = bool(unpinned) or spec.force_search
    aggregate_all = not facet and not pinned
    if aggregate_all and not search:
        # sum(aws.sqs.X) across every queue: NR aggregates over all
        # resources; the builder editor would return one series per
        # resource instead, so use SEARCH wrapped in the aggregator.
        search = bool(primary)

    payload: Dict[str, Any] = {
        "namespace": spec.namespace, "metricName": spec.metric,
        "statistic": spec.statistic, "dimensions": {},
        "dimension_keys": list(facet), "region": ctx.region,
        "queryMode": "Metrics", "metricEditorMode": 0, "expression": "",
    }
    if not search:
        dims: Dict[str, List[str]] = {}
        for f in pinned:
            dims[f.name] = list(f.values)
        for d in facet:
            dims.setdefault(d, ["*"])
        payload["dimensions"] = dims
        return payload

    schema = []
    for n in [f.name for f in filters] + facet + ([primary] if primary
                                                   else []):
        if n and n not in schema:
            schema.append(n)
    terms = ["MetricName=%s" % _search_quote(spec.metric)]
    terms.extend(_search_term(f) for f in filters)
    expr = "SEARCH('{%s} %s', '%s', %d)" % (
        ",".join([spec.namespace] + schema), " ".join(terms),
        spec.statistic, ctx.period)
    if not facet:
        wrapper = _SEARCH_AGG.get(spec.statistic, "AVG")
        if spec.statistic.startswith("p"):
            ctx.note("%s across several resources: CloudWatch computes the "
                     "percentile per resource, the panel shows their "
                     "average" % spec.statistic, APPROXIMATE)
        expr = "%s(%s)" % (wrapper, expr)
    else:
        ctx.note("FACET with a multi-value filter: one series per matching "
                 "resource via SEARCH", APPROXIMATE)
    payload["metricEditorMode"] = 1
    payload["expression"] = expr
    dims = {}
    for f in pinned:
        dims[f.name] = list(f.values)
    for d in facet:
        dims.setdefault(d, ["*"])
    payload["dimensions"] = dims
    return payload


def _unit_for(metric: str) -> str:
    for rx, unit in _UNIT_HINTS:
        if rx.search(metric):
            return unit
    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def translate_to_cloudwatch(q: NrqlQuery, cfg: Dict[str, Any]) -> Translation:
    """FROM Metric aws.* / AWS sample events -> Translation(datasource=
    "cloudwatch") with a ``cw`` payload (see module docstring)."""
    t = Translation(datasource="cloudwatch", confidence=APPROXIMATE)
    t.query_type = "range" if q.timeseries is not None else "instant"
    ctx = _Ctx(q, cfg, t)
    et = (q.from_[0] if q.from_ else "metric").lower()
    ctx.service = AWS_SAMPLE_EVENTS.get(et, "")

    items = [i for i in q.select if not isinstance(i.expr, Star)]
    if not items or not any(isinstance(i.expr, Func) for i in items):
        raise Untranslatable(
            "CloudWatch needs an aggregation (average/sum/max/min/count/"
            "percentile) of an aws.* metric; raw samples cannot be listed")

    _collect_where(q.where, ctx, ctx.filters)
    _collect_facet(ctx)

    first = True
    for item in items:
        if not isinstance(item.expr, Func):
            t.note("non-aggregated SELECT item %r dropped" % item,
                   NEEDS_REVIEW)
            continue
        try:
            specs = _item_specs(item, ctx)
        except Untranslatable as e:
            if first:
                raise
            t.note("SELECT item dropped: %s" % e, NEEDS_REVIEW)
            continue
        for spec in specs:
            if spec.math_expr:
                payload = {
                    "namespace": "", "metricName": "", "statistic": "",
                    "dimensions": {}, "dimension_keys": [],
                    "region": cfg.get("cloudwatch_region") or "default",
                    "queryMode": "Metrics", "metricEditorMode": 1,
                    "expression": spec.math_expr,
                }
            else:
                payload = _build_payload(spec, ctx)
                if spec.math_id:
                    payload["id"] = spec.math_id
                    payload["hide"] = True
            legend = spec.legend
            if payload["dimension_keys"]:
                legend = " / ".join("{{%s}}" % d
                                    for d in payload["dimension_keys"])
                if spec.legend and spec.legend != spec.statistic:
                    legend = spec.legend + " " + legend
            unit = _unit_for(spec.metric)
            if first:
                t.expr = ""
                t.legend = legend
                t.cw = payload  # type: ignore[attr-defined]
                if unit:
                    t.notes.append("unit:" + unit)
                first = False
            else:
                extra = Translation(expr="", datasource="cloudwatch",
                                    query_type=t.query_type, legend=legend,
                                    confidence=APPROXIMATE,
                                    group_by=list(payload["dimension_keys"]))
                extra.cw = payload  # type: ignore[attr-defined]
                if unit:
                    extra.notes.append("unit:" + unit)
                t.extra.append(extra)
    if first:
        raise Untranslatable("no translatable SELECT items")
    t.group_by = list(getattr(t, "cw")["dimension_keys"])
    if q.timeseries is None and getattr(t, "cw")["statistic"] in (
            "Sum", "SampleCount"):
        t.notes.append("CloudWatch returns one value per period; for the "
                       "NR 'total over the window' set the panel reducer "
                       "to Total (the builder does this for stat panels)")
    if q.compare_with:
        t.note("COMPARE WITH has no CloudWatch equivalent; add a second "
               "query with a relative time shift in the panel", NEEDS_REVIEW)
    t.notes.append(
        "CloudWatch datasource target (NR aws.* metrics are not in Mimir); "
        "requires a CloudWatch datasource with read access to the account")
    return t
