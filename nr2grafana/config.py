"""Conversion configuration.

Everything site-specific about the target LGTM stack lives here so the same
converter works against any environment: datasource UIDs, attribute→label
renames, metric-name overrides, and behavioral toggles.

Config file is JSON (see config/mappings.example.json). All keys optional;
defaults below are sensible for an OTel-collector-fed LGTM stack.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict


DEFAULT_CONFIG: Dict[str, Any] = {
    # Grafana datasource references. Using ${DS_*} placeholders keeps the
    # output importable anywhere (Grafana prompts for the datasource on
    # import). Set concrete uids (e.g. "mimir") to skip the prompt.
    # Grafana datasource references. Default: "${...}" placeholders that the
    # converter turns into `type: datasource` template variables, which bind
    # to the default datasource of each type on import and work through both
    # the UI import and POST /api/dashboards/db. Set concrete uids (e.g.
    # "mimir") to pin panels to a specific datasource instead.
    "datasources": {
        "prometheus": {"type": "prometheus", "uid": "${datasource}"},
        "loki": {"type": "loki", "uid": "${loki_datasource}"},
        "tempo": {"type": "tempo", "uid": "${tempo_datasource}"},
        # Optional fallback: the official New Relic datasource plugin for
        # Grafana. When "passthrough_fallback" is true, untranslatable
        # widgets become working panels that run the original NRQL through
        # this datasource instead of dead placeholders.
        "newrelic": {"type": "nrgrafanaplugin-newrelic-datasource",
                     "uid": "${newrelic_datasource}"},
    },

    # New Relic attribute name -> Prometheus/Loki label name.
    # Applied in WHERE clauses, FACET clauses, and variable queries.
    "label_map": {
        "appName": "service_name",
        "appId": "service_name",
        "entity.name": "service_name",
        "entityName": "service_name",
        "service.name": "service_name",
        "serviceName": "service_name",
        "host": "instance",
        "hostname": "instance",
        "host.name": "instance",
        "fullHostname": "instance",
        "environment": "deployment_environment",
        "env": "deployment_environment",
        "deployment.environment": "deployment_environment",
        "k8s.cluster.name": "cluster",
        "clusterName": "cluster",
        "k8s.namespace.name": "namespace",
        "namespaceName": "namespace",
        "namespace_name": "namespace",
        "namespace": "namespace",
        "k8s.pod.name": "pod",
        "podName": "pod",
        "pod_name": "pod",
        "k8s.container.name": "container",
        "containerName": "container",
        "container_name": "container",
        "kubernetes.pod_name": "pod",
        "kubernetes.namespace_name": "namespace",
        "kubernetes.container_name": "container",
        "kubernetes.node_name": "node",
        "kubernetes.cluster_name": "cluster",
        "cluster_name": "cluster",
        "k8s.deployment.name": "deployment",
        "deploymentName": "deployment",
        "http.statusCode": "http_response_status_code",
        "httpResponseCode": "http_response_status_code",
        "response.status": "http_response_status_code",
        "http.method": "http_request_method",
        "request.method": "http_request_method",
        "name": "span_name",
        "transactionName": "span_name",
        "level": "level",
        "severity": "level",
        "message": "message",
        # Identity entries: names already correct for a typical LGTM stack,
        # listed so they don't get flagged as unverified.
        "service_name": "service_name",
        "job": "job",
        "instance": "instance",
        "cluster": "cluster",
        "pod": "pod",
        "container": "container",
        "deployment": "deployment",
        "deployment_environment": "deployment_environment",
        "span_name": "span_name",
        "status_code": "status_code",
        "error": "error",
        "le": "le",
        "device": "device",
        "topic": "topic",
        "queue": "queue",
        "error.class": "error_type",
        "errorClass": "error_type",
        "error.type": "error_type",
        "request.uri": "http_route",
        "http.route": "http_route",
        # Infrastructure agent dimensional metrics (host.* / k8s.*) name
        # their entities with these attributes.
        "host.hostname": "instance",
        "host.id": "instance",
        "host.displayName": "instance",
        "k8s.podName": "pod",
        "k8s.pod.status": "phase",
        "k8s.container.status": "status",
        "k8s.namespaceName": "namespace",
        "k8s.nodeName": "node",
        "k8s.clusterName": "cluster",
        "k8s.containerName": "container",
        "k8s.deploymentName": "deployment",
        "k8s.daemonsetName": "daemonset",
        "k8s.statefulsetName": "statefulset",
        "k8s.replicasetName": "replicaset",
        "k8s.hpaName": "horizontalpodautoscaler",
        "k8s.node.name": "node",
        "nodeName": "node",
        "node": "node",
        "mountPoint": "mountpoint",
        "interfaceName": "device",
        "datastoreType": "db_system",
        "db.system": "db_system",
        "external.host": "server_address",
        "server.address": "server_address",
        "http.url": "url_full",
        "service.instance.id": "service_instance_id",
        "entity.guid": "service_instance_id",
        "entityGuid": "service_instance_id",
        "containerId": "container_id",
        "container.id": "container_id",
        "metricTimesliceName": "metricTimesliceName",
        "state": "state",
        "phase": "phase",
        "reason": "reason",
        "user.id": "user_id",
        "trace.id": "trace_id",
        "span.id": "span_id",
        "span.kind": "span_kind",
        "otel.status_code": "status_code",
        "http.request.method": "http_request_method",
        "http.response.status_code": "http_response_status_code",
        "url.path": "http_route",
        "messaging.destination.name": "messaging_destination_name",
        "messaging.system": "messaging_system",
        "rpc.service": "rpc_service",
        "rpc.method": "rpc_method",
    },

    # NR metric name (FROM Metric SELECT ...(`metric.name`)) -> Prometheus
    # metric name. Checked before the built-in knowledge of New Relic's own
    # metric names (apm.service.*, newrelic.goldenmetrics.*, host.*, k8s.*,
    # aws.*, OTel semconv) and the dot->underscore heuristics. Entries:
    #   "orders.completed": {"name": "orders_total", "type": "counter"}
    #   "queue.depth": {"name": "queue_depth", "type": "gauge",
    #                   "unit": "short", "matchers": {"queue": "main"}}
    #   "custom.ratio": {"expr": "<AGG> <BY>(my_ratio{<SELBARE>})",
    #                    "unit": "percentunit"}   # full PromQL template
    # Legacy timeslice metrics are keyed by their metricTimesliceName:
    #   "Custom/checkout/orders": {"name": "checkout_orders_total",
    #                              "type": "counter"}
    "metric_map": {},

    # Attributes that exist as Loki *stream labels* in your setup. WHERE
    # filters on these become stream selectors; everything else becomes a
    # pipeline filter (| attr = "..." after parsing, or line filter).
    "loki_stream_labels": ["service_name", "namespace", "cluster", "container",
                           "pod", "level", "job", "instance"],

    # Loki: parser stage to insert when filtering on non-label attributes.
    # "logfmt", "json", or "" (structured metadata only; no parser stage).
    "loki_parser": "json",

    # Labels stored as Loki *structured metadata* (queryable with
    # `| label = "v"` WITHOUT a parser stage). Filters on these are placed
    # before the parser and don't force one.
    "loki_metadata_labels": ["detected_level", "trace_id", "span_id"],

    # Span-metrics naming generation produced by your spanmetrics pipeline:
    #   "otel"         -> traces_span_metrics_duration_milliseconds /
    #                     traces_span_metrics_calls_total (OTel collector
    #                     spanmetrics connector default)
    #   "otel-seconds" -> traces_span_metrics_duration_seconds / ..._calls_total
    #   "tempo"        -> traces_spanmetrics_latency /
    #                     traces_spanmetrics_calls_total (Tempo
    #                     metrics-generator)
    #   "legacy"       -> duration_milliseconds / calls_total
    # Or set explicit names in span_metrics below (they win over the flavor).
    "spanmetrics_flavor": "otel",
    "span_metrics": {
        "duration_histogram": "",  # override; empty = derive from flavor
        "calls_total": "",
        "unit": "",                # override histogram unit ("ms"/"s")
    },
    # Label carrying the service identity on span metrics. Tempo's
    # metrics-generator emits `service`; the OTel spanmetrics connector
    # emits `service_name`. Empty = keep the label_map result.
    "span_service_label": "",

    # OTel HTTP server metrics naming in your stack:
    #   "semconv" -> http_server_request_duration_seconds (new semconv)
    #   "legacy"  -> http_server_duration_milliseconds (old semconv)
    # http_metrics overrides win over the flavor (for hybrid stacks, e.g.
    # http_server_duration_seconds).
    "http_metrics_flavor": "semconv",
    "http_metrics": {
        "duration_histogram": "",  # e.g. "http_server_duration_seconds"
        "unit": "",                # its unit ("s"/"ms")
    },

    # Whether your metrics pipeline appends the Prometheus "_total" suffix
    # to counters. The OTel collector's prometheusremotewrite exporter with
    # default settings does NOT; the prometheus exporter does.
    "metric_total_suffix": True,

    # How aggregated FROM Span queries are answered:
    #   "spanmetrics" -> PromQL over span metrics in Mimir (default)
    #   "traceql"     -> TraceQL metrics in Tempo 2.4+ ({...} | rate() by ())
    # uniqueCount(trace.id) always uses TraceQL metrics (root-span count).
    "span_aggregations": "spanmetrics",

    # Custom event types -> where that data lives in your stack. Keys are
    # NR event types; a "logs" entry routes the query to Loki with these
    # stream labels, a "metrics" entry names the Prometheus metric that
    # carries the event's numeric attribute (then metric_map applies).
    #   "event_map": {
    #     "Purchase":  {"family": "logs", "labels": {"job": "purchases"}},
    #     "MyMetricEvent": {"family": "metrics", "metric": "my.metric"}
    #   }
    "event_map": {},

    # When true, widgets whose NRQL cannot be translated become panels that
    # query the New Relic Grafana datasource plugin with the original NRQL.
    # When false, they become text panels containing the original query and
    # the reason translation failed.
    "passthrough_fallback": False,

    # One Grafana dashboard per NR page ("split") or a single dashboard with
    # a row per page ("rows").
    "page_strategy": "rows",

    # Extra template variables to add to every generated dashboard, as raw
    # Grafana templating entries. Example: a datasource variable.
    "extra_variables": [],

    # Tags applied to every generated dashboard.
    "tags": ["newrelic-migration"],

    # Default height multiplier: NR layout rows -> Grafana grid units.
    # NR row unit is visually ~3 Grafana units.
    "row_height_units": 3,
}


def load_config(path: str = "") -> Dict[str, Any]:
    """Load config from JSON file, merged over defaults (deep merge, dicts
    merge key-wise, everything else replaces)."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not path:
        return cfg
    if not os.path.exists(path):
        raise FileNotFoundError("config file not found: %s" % path)
    with open(path) as f:
        user = json.load(f)
    _merge(cfg, user)
    return cfg


def _merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> None:
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
