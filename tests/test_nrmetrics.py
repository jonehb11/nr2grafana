"""Tests for the built-in New Relic metric / infra knowledge
(nr2grafana.translate.nrmetrics) as seen through the translator."""

import unittest

from nr2grafana.config import load_config
from nr2grafana.translate import nrmetrics
from nr2grafana.translate.common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW, UNTRANSLATABLE,
)
from nr2grafana.translate.router import translate_query

HTTP = "http_server_request_duration_seconds"


def tr(nrql, cfg=None):
    return translate_query(nrql, cfg or load_config())


class ApmMetricTests(unittest.TestCase):
    def test_apm_transaction_duration_is_http_histogram(self):
        t = tr("FROM Metric SELECT rate(count(apm.service.transaction."
               "duration), 1 minute) WHERE appName = 'c' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{service_name="c"}[$__rate_interval])) * 60'
            % HTTP)

    def test_apm_error_count_is_5xx_count(self):
        t = tr("FROM Metric SELECT rate(sum(apm.service.error.count), "
               "1 minute) WHERE appName = 'c' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{service_name="c",http_response_status_code'
            '=~"5.."}[$__rate_interval])) * 60' % HTTP)
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_apm_error_ratio(self):
        t = tr("FROM Metric SELECT sum(apm.service.error.count) / "
               "count(apm.service.transaction.duration) WHERE appName = 'c' "
               "TIMESERIES")
        self.assertIn('http_response_status_code=~"5.."', t.expr)
        self.assertIn(") / (", t.expr)
        self.assertIn("unit:percentunit", t.notes)

    def test_transaction_type_web_dropped_on_apm_metric(self):
        t = tr("SELECT rate(count(apm.service.transaction.duration), "
               "1 minute) FROM Metric WHERE appName = 'c' AND "
               "transactionType = 'Web' TIMESERIES")
        self.assertNotIn("transactionType", t.expr)

    def test_transaction_type_other_untranslatable(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' AND "
               "transactionType = 'Other'")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("non-web" in n for n in t.notes))

    def test_golden_response_time_ms(self):
        t = tr("SELECT average(newrelic.goldenmetrics.apm.application."
               "responseTimeMs) FROM Metric WHERE entity.name = 'c' "
               "TIMESERIES")
        self.assertTrue(t.expr.startswith("1000 * (sum(rate(%s_sum" % HTTP))
        self.assertIn("unit:ms", t.notes)

    def test_golden_throughput_per_minute(self):
        t = tr("SELECT sum(newrelic.goldenmetrics.apm.application."
               "throughput) FROM Metric WHERE entity.name = 'c' TIMESERIES")
        self.assertEqual(t.expr, 'sum(rate(%s_count{service_name="c"}'
                                 '[$__rate_interval])) * 60' % HTTP)

    def test_golden_error_rate_percent(self):
        t = tr("SELECT average(newrelic.goldenmetrics.apm.application."
               "errorRate) FROM Metric WHERE entity.name = 'c' TIMESERIES")
        self.assertTrue(t.expr.startswith("100 * (sum(rate("))
        self.assertIn("unit:percent", t.notes)

    def test_overview_web_untranslatable_with_reason(self):
        t = tr("SELECT average(apm.service.overview.web) FROM Metric "
               "WHERE appName = 'c' FACET segmentName TIMESERIES")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("segment" in n for n in t.notes))

    def test_datastore_duration_maps_to_db_client_histogram(self):
        t = tr("SELECT average(apm.service.datastore.operation.duration) "
               "FROM Metric WHERE appName = 'c' FACET datastoreType "
               "TIMESERIES")
        self.assertIn("db_client_operation_duration_seconds_sum", t.expr)
        self.assertIn("by (db_system)", t.expr)

    def test_getfield_count(self):
        t = tr("SELECT getField(apm.service.transaction.duration, count) "
               "FROM Metric WHERE appName = 'c' TIMESERIES")
        self.assertEqual(t.expr, 'sum(rate(%s_count{service_name="c"}'
                                 '[$__rate_interval])) * $__interval_ms / 1000'
                         % HTTP)

    def test_timeslice_needs_metric_map(self):
        t = tr("SELECT average(newrelic.timeslice.value) FROM Metric WHERE "
               "metricTimesliceName = 'Custom/foo' AND appName = 'c' "
               "TIMESERIES")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("Custom/foo" in n for n in t.notes))

    def test_timeslice_resolved_via_metric_map(self):
        cfg = load_config()
        cfg["metric_map"]["Custom/foo"] = {"name": "foo_total",
                                           "type": "counter"}
        t = tr("SELECT rate(count(newrelic.timeslice.value), 1 minute) FROM "
               "Metric WHERE metricTimesliceName = 'Custom/foo' AND "
               "appName = 'c' TIMESERIES", cfg)
        self.assertEqual(t.expr, 'sum(rate(foo_total{service_name="c"}'
                                 '[$__rate_interval])) * 60')

    def test_metric_name_from_where(self):
        t = tr("SELECT count(*) FROM Metric WHERE metricName = "
               "'my.custom.counter' TIMESERIES")
        self.assertEqual(t.expr,
                         "sum(rate(my_custom_counter_total"
                         "[$__rate_interval])) * $__interval_ms / 1000")

    def test_otel_semconv_histogram_known(self):
        t = tr("FROM Metric SELECT count(`http.server.request.duration`) "
               "WHERE service.name = 'x' FACET http.response.status_code "
               "TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum by (http_response_status_code)(rate(%s_count{'
            'service_name="x"}[$__rate_interval])) * $__interval_ms / 1000'
            % HTTP)

    def test_otel_utilization_ratio_unit(self):
        t = tr("FROM Metric SELECT average(`system.cpu.utilization`) "
               "FACET state TIMESERIES")
        self.assertIn("system_cpu_utilization_ratio", t.expr)
        self.assertIn("unit:percentunit", t.notes)


class HostAndK8sMetricTests(unittest.TestCase):
    def test_host_cpu_percent_alias(self):
        t = tr("SELECT average(host.cpuPercent) FROM Metric WHERE "
               "host.hostname = 'web-1' TIMESERIES")
        self.assertEqual(
            t.expr,
            '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle",'
            'instance="web-1"}[$__rate_interval])))')
        self.assertIn("unit:percent", t.notes)

    def test_host_disk_alias_goes_to_storage_sample(self):
        t = tr("SELECT average(host.disk.usedPercent) FROM Metric FACET "
               "host.hostname, mountPoint")
        self.assertIn("node_filesystem_avail_bytes", t.expr)
        self.assertIn("by (instance, mountpoint)", t.expr)

    def test_k8s_container_cpu_alias(self):
        t = tr("SELECT average(k8s.container.cpuUsedCores) FROM Metric "
               "WHERE k8s.namespaceName = 'prod' FACET k8s.podName "
               "TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg by (pod)(rate(container_cpu_usage_seconds_total{container'
            '!="",namespace="prod"}[$__rate_interval]))')

    def test_aws_yace_naming_and_dimension_labels(self):
        t = tr("SELECT average(aws.ec2.CPUUtilization) FROM Metric WHERE "
               "aws.accountId = '123' FACET aws.ec2.InstanceId TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg by (dimension_InstanceId)(avg_over_time('
            'aws_ec2_cpuutilization_average{account_id="123"}'
            '[$__rate_interval]))')
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("YACE" in n for n in t.notes))

    def test_aws_sum_statistic_and_snake_case(self):
        t = tr("SELECT sum(aws.applicationelb.HTTPCode_Target_5XX_Count) "
               "FROM Metric FACET aws.applicationelb.LoadBalancer TIMESERIES")
        self.assertIn("aws_applicationelb_httpcode_target_5_xx_count_sum",
                      t.expr)
        self.assertIn("by (dimension_LoadBalancer)", t.expr)


class InfraSampleTests(unittest.TestCase):
    def test_system_sample_multiple_items_each_translated(self):
        t = tr("SELECT max(cpuPercent), max(memoryUsedPercent) FROM "
               "SystemSample FACET hostname")
        self.assertEqual(
            t.expr,
            '100 * (1 - min by (instance)(rate(node_cpu_seconds_total{'
            'mode="idle"}[$__range])))')
        self.assertEqual(len(t.extra), 1)
        self.assertIn("node_memory_MemAvailable_bytes", t.extra[0].expr)

    def test_system_sample_uptime(self):
        t = tr("SELECT latest(uptime) FROM SystemSample FACET hostname")
        self.assertEqual(t.expr,
                         "avg by (instance)(time() - node_boot_time_seconds)")
        self.assertIn("unit:s", t.notes)

    def test_unique_hosts(self):
        t = tr("SELECT uniqueCount(hostname) FROM SystemSample WHERE "
               "`tags.env` = 'prod'")
        self.assertEqual(t.expr,
                         'count(node_uname_info{deployment_environment='
                         '"prod"})')

    def test_network_sample_facet_interface_maps_to_device(self):
        t = tr("SELECT average(receiveBytesPerSecond) FROM NetworkSample "
               "FACET interfaceName TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg by (device)(rate(node_network_receive_bytes_total{'
            'device!="lo"}[$__rate_interval]))')
        self.assertIn("unit:Bps", t.notes)

    def test_process_sample_uses_process_exporter_groups(self):
        t = tr("SELECT average(cpuPercent) FROM ProcessSample WHERE "
               "processDisplayName = 'nginx' FACET hostname TIMESERIES")
        self.assertEqual(
            t.expr,
            '100 * avg by (instance)(rate(namedprocess_namegroup_cpu_'
            'seconds_total{groupname="nginx"}[$__rate_interval]))')

    def test_container_sample_name_label(self):
        t = tr("SELECT latest(memoryUsageBytes) FROM ContainerSample "
               "FACET name")
        self.assertEqual(
            t.expr,
            'max by (name)(container_memory_usage_bytes{container!=""})')
        self.assertIn("unit:bytes", t.notes)

    def test_container_sample_state_filter_is_implicit(self):
        t = tr("SELECT count(*) FROM ContainerSample WHERE state = 'running'")
        self.assertEqual(t.expr, 'count(container_last_seen{container!=""})')
        self.assertTrue(any("running" in n for n in t.notes))

    def test_pod_population_with_phase_and_facet(self):
        t = tr("SELECT uniqueCount(podName) FROM K8sPodSample WHERE "
               "status = 'Running' FACET namespaceName")
        self.assertEqual(
            t.expr,
            'sum by (namespace)(kube_pod_status_phase{phase="Running"})')

    def test_pod_count_facet_status_becomes_phase(self):
        t = tr("SELECT count(*) FROM K8sPodSample WHERE status != 'Running' "
               "FACET status")
        self.assertEqual(
            t.expr, 'sum by (phase)(kube_pod_status_phase{phase!="Running"})')

    def test_latest_pod_status(self):
        t = tr("SELECT latest(status) FROM K8sPodSample FACET podName")
        self.assertEqual(t.expr,
                         "max by (pod, phase)(kube_pod_status_phase == 1)")
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_container_utilization_join(self):
        t = tr("SELECT average(cpuCoresUtilization) FROM K8sContainerSample "
               "FACET podName")
        # The join's right side is reduced so duplicate KSM replicas cannot
        # produce a many-to-many matching error.
        self.assertIn("/ on (namespace, pod, container) max by (namespace, "
                      "pod, container)(kube_pod_container_resource_limits{"
                      "resource=\"cpu\"}))", t.expr)
        self.assertIn("unit:percent", t.notes)

    def test_deployment_multi_latest(self):
        t = tr("SELECT latest(podsDesired), latest(podsAvailable) FROM "
               "K8sDeploymentSample FACET deploymentName")
        self.assertEqual(t.expr,
                         "max by (deployment)(kube_deployment_spec_replicas)")
        self.assertEqual(
            t.extra[0].expr,
            "max by (deployment)(kube_deployment_status_replicas_available)")

    def test_hpa_display_name_label(self):
        t = tr("SELECT latest(currentReplicas) FROM K8sHpaSample "
               "FACET displayName")
        self.assertEqual(
            t.expr,
            "max by (horizontalpodautoscaler)(kube_horizontalpodautoscaler_"
            "status_current_replicas)")

    def test_node_condition(self):
        t = tr("SELECT latest(condition.Ready) FROM K8sNodeSample FACET "
               "nodeName")
        self.assertEqual(
            t.expr,
            'max by (node)(kube_node_status_condition{condition="Ready",'
            'status="true"})')

    def test_volume_used_percent(self):
        t = tr("SELECT latest(fsUsedPercent) FROM K8sVolumeSample FACET "
               "pvcName")
        self.assertEqual(
            t.expr,
            "100 * avg by (persistentvolumeclaim)(kubelet_volume_stats_used_"
            "bytes / kubelet_volume_stats_capacity_bytes)")

    def test_unknown_infra_attribute_is_precise(self):
        t = tr("SELECT average(someNewAttr) FROM K8sPodSample")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("K8sPodSample.someNewAttr" in n for n in t.notes))

    def test_k8s_event_names_loki(self):
        t = tr("SELECT count(*) FROM K8sEvent WHERE event.reason = "
               "'OOMKilled' TIMESERIES")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("Loki" in n for n in t.notes))

    def test_deployment_event_names_annotations(self):
        t = tr("SELECT count(*) FROM Deployment FACET appName")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("annotations" in n for n in t.notes))

    def test_alert_events_name_grafana_alerting(self):
        t = tr("SELECT count(*) FROM NrAiIncident WHERE event = 'open'")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("ALERTS" in n for n in t.notes))


class TransactionAttributeTests(unittest.TestCase):
    def test_database_duration_maps_to_db_client(self):
        t = tr("SELECT average(databaseDuration) FROM Transaction WHERE "
               "appName = 'c' TIMESERIES")
        self.assertIn("db_client_operation_duration_seconds_sum", t.expr)
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_external_duration_maps_to_http_client(self):
        t = tr("SELECT average(externalDuration) FROM Transaction WHERE "
               "appName = 'c' TIMESERIES")
        self.assertIn("http_client_request_duration_seconds_sum", t.expr)

    def test_unknown_attribute_is_untranslatable_not_guessed(self):
        t = tr("SELECT average(customScore) FROM Transaction WHERE "
               "appName = 'c' TIMESERIES")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("customScore" in n for n in t.notes))

    def test_latest_timestamp(self):
        t = tr("SELECT latest(timestamp) FROM Transaction WHERE appName = 'c'")
        self.assertEqual(
            t.expr, 'max(timestamp(%s_count{service_name="c"})) * 1000' % HTTP)
        self.assertIn("unit:dateTimeAsIso", t.notes)

    def test_multi_item_partial_failure_keeps_others(self):
        t = tr("SELECT max(duration), min(duration), stddev(duration) FROM "
               "Transaction WHERE appName = 'c'")
        self.assertNotEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(t.expr.startswith("histogram_quantile(1,"))
        self.assertEqual(len(t.extra), 1)
        self.assertTrue(any("stddev" in n and "dropped" in n
                            for n in t.notes))

    def test_sum_if_error_becomes_filtered_count(self):
        t = tr("SELECT sum(if(error IS TRUE, 1, 0)) FROM Transaction WHERE "
               "appName = 'c' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{service_name="c",http_response_status_'
            'code=~"5.."}[$__rate_interval])) * $__interval_ms / 1000'
            % HTTP)


class SelectArithmeticTests(unittest.TestCase):
    def test_parenthesised_ratio_times_100_is_percent(self):
        t = tr("SELECT (filter(count(*), WHERE error IS TRUE) / count(*)) "
               "* 100 FROM Transaction WHERE appName = 'c' TIMESERIES")
        self.assertTrue(t.expr.endswith(") * 100"))
        self.assertIn("unit:percent", t.notes)

    def test_average_times_1000_sets_ms(self):
        t = tr("SELECT average(duration) * 1000 FROM Transaction TIMESERIES")
        self.assertIn("unit:ms", t.notes)
        self.assertNotIn("unit:s", t.notes)

    def test_argument_scale_lifted(self):
        t = tr("SELECT average(duration * 1000) FROM Transaction TIMESERIES")
        self.assertTrue(t.expr.endswith(") * 1000"))
        self.assertIn("unit:ms", t.notes)

    def test_with_clause_substituted(self):
        t = tr("WITH duration * 1000 AS durMs SELECT average(durMs) FROM "
               "Transaction WHERE appName = 'c' TIMESERIES")
        self.assertTrue(t.expr.endswith(") * 1000"))

    def test_difference_of_aggregations(self):
        t = tr("SELECT count(*) - filter(count(*), WHERE error IS TRUE) "
               "FROM Transaction WHERE appName = 'c' TIMESERIES")
        self.assertIn(") - (", t.expr)
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_comment_stripped(self):
        t = tr("SELECT count(*) FROM Transaction /* all */ WHERE appName = "
               "'c' -- trailing\nTIMESERIES")
        self.assertIn('service_name="c"', t.expr)
        self.assertEqual(t.query_type, "range")

    def test_named_apdex_threshold_without_space(self):
        t = tr("SELECT apdex(duration, t:0.3) FROM Transaction")
        # 0.3 and 1.2 are not OTel default buckets: the next defaults
        # (0.5, 2.5) are accepted as fallbacks
        self.assertIn('le=~"0\\.3|0\\.5"', t.expr)
        self.assertIn('le=~"1\\.2|2\\.5"', t.expr)


class FacetFunctionTests(unittest.TestCase):
    def test_capture_becomes_label_replace(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' FACET "
               "capture(name, r'WebTransaction/(?P<ctrl>[^/]+)/.*')")
        self.assertTrue(t.expr.startswith("sum by (ctrl)(label_replace("))
        self.assertIn('"ctrl", "$1", "http_route"', t.expr)
        self.assertEqual(t.legend, "{{ctrl}}")

    def test_concat_groups_by_all_and_builds_legend(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' FACET "
               "concat(host, ':', name)")
        self.assertIn("by (instance, http_route)", t.expr)
        self.assertEqual(t.legend, "{{instance}}:{{http_route}}")

    def test_string_wrapper_unwrapped(self):
        t = tr("SELECT count(*) FROM Transaction FACET string(httpResponseCode)")
        self.assertIn("by (http_response_status_code)", t.expr)

    def test_if_facet_becomes_two_cases(self):
        t = tr("SELECT count(*) FROM Transaction FACET IF(duration > 1, "
               "'slow', 'fast')")
        self.assertEqual(t.legend, "slow")
        self.assertEqual(t.extra[0].legend, "fast")

    def test_time_facet_noted(self):
        t = tr("SELECT count(*) FROM Transaction FACET hourOf(timestamp)")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("1h" in n for n in t.notes))

    def test_tolower_in_where_is_case_insensitive(self):
        t = tr("SELECT count(*) FROM Transaction WHERE toLower(name) = 'x'")
        self.assertIn('http_route=~"(?i)x"', t.expr)

    def test_numeric_wrapper_status_class(self):
        t = tr("SELECT count(*) FROM Transaction WHERE "
               "numeric(httpResponseCode) >= 500")
        self.assertIn('http_response_status_code=~"5.."', t.expr)


class EventMapTests(unittest.TestCase):
    def test_custom_event_routed_to_loki(self):
        cfg = load_config()
        cfg["event_map"] = {"Purchase": {"family": "logs",
                                         "labels": {"job": "purchases"}}}
        t = tr("SELECT count(*) FROM Purchase WHERE currency = 'USD' "
               "TIMESERIES", cfg)
        self.assertEqual(t.datasource, "loki")
        self.assertIn('{job="purchases"}', t.expr)
        self.assertIn('currency="USD"', t.expr)

    def test_custom_event_routed_to_metric(self):
        cfg = load_config()
        cfg["event_map"] = {"Purchase": {"family": "metrics",
                                         "metric": "purchases.total"}}
        cfg["metric_map"]["purchases.total"] = {"name": "purchases_total",
                                                "type": "counter"}
        t = tr("SELECT count(*) FROM Purchase TIMESERIES", cfg)
        self.assertEqual(t.expr,
                         "sum(rate(purchases_total[$__rate_interval])) * "
                         "$__interval_ms / 1000")

    def test_unknown_custom_event_message_mentions_event_map(self):
        t = tr("SELECT count(*) FROM MyCustomEvent TIMESERIES")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("event_map" in n for n in t.notes))


class TimeRangeTests(unittest.TestCase):
    def test_since_until_relative_becomes_timeshift(self):
        t = tr("SELECT count(*) FROM Transaction SINCE 1 day ago UNTIL 1 "
               "hour ago TIMESERIES")
        self.assertIn("timefrom:now-23h", t.notes)
        self.assertIn("timeshift:1h", t.notes)
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_until_now_is_ignored(self):
        t = tr("SELECT count(*) FROM Transaction SINCE 30 minutes ago "
               "UNTIL now")
        self.assertIn("timefrom:now-30m", t.notes)
        self.assertFalse(any("UNTIL" in n for n in t.notes))

    def test_last_week(self):
        t = tr("SELECT count(*) FROM Transaction SINCE last week")
        self.assertIn("timefrom:now-1w/w", t.notes)


class KnowledgeTableTests(unittest.TestCase):
    def test_every_infra_spec_is_well_formed(self):
        for key, spec in nrmetrics.INFRA.items():
            self.assertIn(spec.kind, ("gauge", "counter", "rate",
                                      "histogram", "expr", "count", "none",
                                      "http", "http-errors"), key)
            if spec.kind in ("gauge", "counter", "rate", "histogram",
                             "count"):
                self.assertTrue(spec.name, key)
            if spec.kind == "expr":
                self.assertTrue(spec.expr, key)
                self.assertIn("<", spec.expr, key)
            if spec.kind == "none":
                self.assertTrue(spec.reason, key)

    def test_every_metric_spec_is_well_formed(self):
        for name, spec in nrmetrics.METRICS.items():
            self.assertEqual(name, name.lower() if name.islower() else name)
            if spec.kind == "none":
                self.assertTrue(spec.reason, name)


if __name__ == "__main__":
    unittest.main()


class LegacyAwsSampleTests(unittest.TestCase):
    """ComputeSample / DatastoreSample / QueueSample ... (API-polling AWS
    integrations) -> YACE metric names."""

    def test_ec2_metric_attribute(self):
        t = tr("SELECT average(provider.cpuUtilization.Average) "
               "FROM ComputeSample WHERE provider = 'Ec2Instance' "
               "AND awsRegion = 'us-east-1' FACET ec2InstanceId TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg by (dimension_InstanceId)(avg_over_time('
            'aws_ec2_cpu_utilization_average{region="us-east-1"}'
            '[$__rate_interval]))')
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertIn("unit:percent", t.notes)
        self.assertTrue(any("YACE" in n for n in t.notes))
        # `provider` picked the namespace; it is not a label to verify.
        self.assertFalse(any("'provider' not in label_map" in n
                             for n in t.notes))

    def test_statistic_from_the_attribute_wins(self):
        t = tr("SELECT max(provider.databaseConnections.Maximum) "
               "FROM DatastoreSample WHERE provider = 'RdsDbInstance' "
               "FACET dbInstanceIdentifier")
        self.assertEqual(
            t.expr,
            'max by (dimension_DBInstanceIdentifier)(max_over_time('
            'aws_rds_database_connections_maximum[$__range]))')

    def test_statistic_from_the_aggregation_otherwise(self):
        t = tr("SELECT sum(provider.numberOfMessagesSent) FROM QueueSample "
               "WHERE provider = 'SqsQueue'")
        self.assertIn("aws_sqs_number_of_messages_sent_sum", t.expr)

    def test_tags_become_tag_labels(self):
        t = tr("SELECT latest(provider.approximateNumberOfMessagesVisible.Sum) "
               "FROM QueueSample WHERE provider = 'SqsQueue' "
               "AND label.Team = 'checkout' FACET queueName")
        self.assertEqual(
            t.expr,
            'max by (dimension_QueueName)('
            'aws_sqs_approximate_number_of_messages_visible_sum{'
            'tag_Team="checkout"})')
        self.assertFalse(any("'label.Team' not in label_map" in n
                             for n in t.notes), t.notes)

    def test_count_star_counts_the_info_series(self):
        t = tr("SELECT count(*) FROM ComputeSample WHERE provider = "
               "'Ec2Instance'")
        self.assertEqual(t.expr, "count(aws_ec2_info)")
        self.assertTrue(any("discovered" in n for n in t.notes))

    def test_unique_count_of_a_dimension(self):
        t = tr("SELECT uniqueCount(ec2InstanceId) FROM ComputeSample "
               "WHERE provider = 'Ec2Instance' FACET awsRegion")
        self.assertEqual(
            t.expr,
            "count by (region)(count by (dimension_InstanceId, region)"
            "(aws_ec2_info))")

    def test_missing_provider_is_untranslatable_with_the_choices(self):
        t = tr("SELECT average(provider.cpuUtilization.Average) "
               "FROM ComputeSample")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("WHERE provider = " in n and "ec2instance" in n
                            for n in t.notes), t.notes)

    def test_unknown_provider_is_untranslatable(self):
        t = tr("SELECT average(provider.cpuUtilization.Average) "
               "FROM ComputeSample WHERE provider = 'Foo'")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("unknown AWS resource type" in n
                            for n in t.notes), t.notes)

    def test_metric_map_pins_the_metric(self):
        cfg = load_config()
        cfg["metric_map"]["ComputeSample.provider.cpuUtilization.Average"] = {
            "name": "aws_ec2_cpuutilization_average", "type": "gauge",
            "unit": "percent"}
        t = tr("SELECT average(provider.cpuUtilization.Average) "
               "FROM ComputeSample WHERE provider = 'Ec2Instance' TIMESERIES",
               cfg)
        self.assertEqual(
            t.expr,
            "avg(avg_over_time(aws_ec2_cpuutilization_average"
            "[$__rate_interval]))")
        self.assertEqual(t.confidence, EXACT)

    def test_spec_table(self):
        spec = nrmetrics.legacy_aws_spec("LoadBalancerSample", "Alb",
                                         "provider.requestCount.Sum", "sum")
        self.assertEqual(spec.name, "aws_applicationelb_request_count_sum")
        self.assertIsNone(nrmetrics.legacy_aws_spec(
            "LoadBalancerSample", "Alb", "entityName", "latest"))
        self.assertEqual(nrmetrics.legacy_aws_namespace("BlockDeviceSample",
                                                        "EbsVolume"), "ebs")


class JoinSafetyTests(unittest.TestCase):
    def test_node_utilization_join_is_reduced(self):
        t = tr("SELECT average(cpuUsedCoresUtilization) FROM K8sNodeSample "
               "FACET nodeName TIMESERIES")
        self.assertIn('/ on (node) max by (node)(kube_node_status_allocatable{'
                      'resource="cpu"}))', t.expr)


class MoreKubernetesTests(unittest.TestCase):
    def test_jobs_and_cronjobs(self):
        t = tr("SELECT latest(failed) FROM K8sJobSample FACET jobName")
        self.assertEqual(t.expr, "max by (job_name)(kube_job_status_failed)")
        t = tr("SELECT latest(isActive) FROM K8sCronjobSample FACET cronjobName")
        self.assertEqual(t.expr, "max by (cronjob)(kube_cronjob_status_active)")

    def test_namespace_aggregates(self):
        t = tr("SELECT latest(cpuUsedCores) FROM K8sNamespaceSample "
               "FACET namespaceName")
        self.assertEqual(
            t.expr,
            'avg by (namespace)(sum by (namespace)(rate('
            'container_cpu_usage_seconds_total{container!=""}'
            '[$__rate_interval])))')

    def test_node_running_pods_and_container_memory_limit(self):
        t = tr("SELECT latest(allocatablePods) - latest(runningPods) "
               "FROM K8sNodeSample FACET nodeName")
        self.assertEqual(
            t.expr,
            '(max by (node)(kube_node_status_allocatable{resource="pods"})) - '
            '(max by (node)(kubelet_running_pods))')
        t = tr("SELECT average(memoryUsageBytes) / average(memoryLimitBytes) "
               "FROM ContainerSample FACET name TIMESERIES")
        self.assertIn("container_spec_memory_limit_bytes", t.expr)


class OnHostIntegrationTests(unittest.TestCase):
    def test_redis(self):
        t = tr("SELECT average(net.commandsProcessedPerSecond) FROM RedisSample "
               "TIMESERIES")
        self.assertEqual(
            t.expr, "avg(rate(redis_commands_processed_total[$__rate_interval]))")
        self.assertEqual(tr("SELECT count(*) FROM RedisSample").expr,
                         "count(redis_up)")

    def test_postgres_database_label(self):
        t = tr("SELECT latest(db.connections) FROM PostgresqlDatabaseSample "
               "FACET database")
        self.assertEqual(t.expr, "max by (datname)(pg_stat_database_numbackends)")

    def test_mysql_command_counters(self):
        t = tr("SELECT average(query.comSelectPerSecond) FROM MysqlSample "
               "FACET hostname TIMESERIES")
        self.assertIn('mysql_global_status_commands_total{command="select"}',
                      t.expr)

    def test_unknown_attribute_names_the_exporter(self):
        t = tr("SELECT average(memoryUsedBytes) FROM MemcachedSample TIMESERIES")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("memcached_exporter" in n for n in t.notes))
        t = tr("SELECT latest(cluster.status) FROM ElasticsearchClusterSample")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("elasticsearch_cluster_health_status" in n
                            for n in t.notes))


class Iteration4KnowledgeTests(unittest.TestCase):
    def test_pod_status_facet_uses_the_phase_metric(self):
        t = tr("SELECT uniqueCount(podName) FROM K8sPodSample FACET status "
               "TIMESERIES")
        self.assertEqual(t.expr, "sum by (phase)(kube_pod_status_phase == 1)")
        self.assertEqual(t.legend, "{{phase}}")

    def test_restart_count_is_a_counter(self):
        t = tr("SELECT rate(sum(restartCount), 1 hour) FROM K8sContainerSample "
               "FACET podName TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum by (pod)(rate(kube_pod_container_status_restarts_total"
            "[$__rate_interval])) * 3600")
        t = tr("SELECT latest(restartCount) FROM K8sContainerSample FACET podName")
        self.assertEqual(t.expr,
                         "max by (pod)(kube_pod_container_status_restarts_total)")

    def test_compare_with_offsets_derived_instant_selectors(self):
        t = tr("SELECT latest(podsDesired) FROM K8sDeploymentSample "
               "FACET deploymentName COMPARE WITH 1 hour ago")
        self.assertEqual(t.extra[0].expr,
                         "max by (deployment)(kube_deployment_spec_replicas "
                         "offset 1h)")

    def test_nested_aggregations_on_infra_events(self):
        t = tr("SELECT rate(sum(readBytesPerSecond), 1 second) FROM StorageSample "
               "FACET hostname TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum by (instance)(rate(node_disk_read_bytes_total"
            "[$__rate_interval]))")

    def test_derivative_of_a_rate_is_refused(self):
        t = tr("SELECT derivative(receiveBytesPerSecond, 1 minute) "
               "FROM NetworkSample FACET interfaceName TIMESERIES")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("second derivative" in n for n in t.notes))

    def test_lambda_events(self):
        t = tr("SELECT count(*) FROM AwsLambdaInvocation "
               "FACET aws.lambda.functionName TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum by (dimension_FunctionName)(sum_over_time("
            "aws_lambda_invocations_sum[$__interval]))")
        t = tr("SELECT count(*) FROM AwsLambdaInvocationError "
               "WHERE aws.lambda.functionName = 'f'")
        self.assertEqual(
            t.expr,
            'sum(sum_over_time(aws_lambda_errors_sum{dimension_FunctionName="f"}'
            '[$__range]))')
        t = tr("SELECT average(duration) FROM AwsLambdaInvocation TIMESERIES")
        self.assertIn("aws_lambda_duration_average", t.expr)


class Iteration5KnowledgeTests(unittest.TestCase):
    def test_dimensional_k8s_identity_attributes(self):
        t = tr("FROM Metric SELECT uniqueCount(k8s.podName) "
               "WHERE k8s.pod.status = 'Pending'")
        self.assertEqual(t.expr, 'sum(kube_pod_status_phase{phase="Pending"})')
        t = tr("FROM Metric SELECT latest(k8s.pod.status) "
               "WHERE k8s.clusterName = 'c' FACET k8s.podName")
        self.assertEqual(
            t.expr, 'max by (pod, phase)(kube_pod_status_phase{cluster="c"} == 1)')
        t = tr("FROM Metric SELECT uniqueCount(k8s.nodeName) "
               "WHERE k8s.clusterName = 'c'")
        self.assertEqual(t.expr, 'count(kube_node_info{cluster="c"})')

    def test_metric_valued_attribute_filters_become_series_filters(self):
        t = tr("SELECT uniqueCount(podName) FROM K8sPodSample WHERE isReady = 0 "
               "AND status = 'Running' FACET namespaceName")
        self.assertEqual(
            t.expr,
            'count by (namespace)((kube_pod_status_ready{condition="true"} '
            '== 0) and on (namespace, pod) (kube_pod_status_phase{phase='
            '"Running"} == 1))')
        self.assertTrue(any("selected by the attribute's own series" in n
                            for n in t.notes))
        # a non-numeric comparison on a metric-valued attribute still says so
        t = tr("SELECT uniqueCount(podName) FROM K8sPodSample WHERE "
               "isReady = 'no'")
        self.assertTrue(any("metric-valued attribute" in n and
                            "kube_pod_status_ready" in n for n in t.notes))

    def test_container_state_reasons(self):
        t = tr("SELECT count(*) FROM K8sContainerSample WHERE status = "
               "'Waiting' AND reason = 'CrashLoopBackOff' FACET podName")
        self.assertEqual(
            t.expr,
            'sum by (pod)(kube_pod_container_status_waiting_reason{'
            'reason="CrashLoopBackOff"})')
        t = tr("SELECT latest(reason) FROM K8sContainerSample "
               "WHERE status != 'Running' FACET podName, containerName")
        self.assertEqual(
            t.expr,
            "avg by (pod, container, reason)((kube_pod_container_status_"
            "waiting_reason == 1) or (kube_pod_container_status_terminated_"
            "reason == 1))")

    def test_rate_over_derived_templates(self):
        t = tr("SELECT rate(sum(net.errorsPerSecond), 1 minute) FROM K8sPodSample "
               "WHERE clusterName = 'c' TIMESERIES")
        self.assertTrue(t.expr.endswith(") * 60"), t.expr)
        t = tr("SELECT rate(sum(restartCount), 1 hour) FROM K8sPodSample "
               "FACET podName TIMESERIES")
        self.assertEqual(
            t.expr,
            "(sum by (pod)(sum by (namespace, pod)(rate(kube_pod_container_"
            "status_restarts_total[$__rate_interval])))) * 3600")


class Iteration9KnowledgeTests(unittest.TestCase):
    def test_micrometer_specs_and_pod_reason(self):
        from nr2grafana.translate import nrmetrics
        spec = nrmetrics.metric_spec("jvm.gc.pause")
        self.assertEqual((spec.kind, spec.name), ("histogram",
                                                  "jvm_gc_pause_seconds"))
        spec = nrmetrics.metric_spec("http.server.requests")
        self.assertEqual(spec.name, "http_server_requests_seconds")
        self.assertIn("percentiles-histogram", spec.note)
        self.assertEqual(nrmetrics.metric_spec("jvm.memory.max").unit, "bytes")
        self.assertEqual(nrmetrics.metric_spec("logback.events").kind,
                         "counter")
        self.assertTrue(nrmetrics.infra_lookup("K8sContainerSample",
                                               "restartCount").cumulative)
        self.assertFalse(nrmetrics.infra_lookup("K8sContainerSample",
                                                "cpuUsedCores").cumulative)
        reason = nrmetrics.infra_lookup("K8sPodSample", "reason")
        self.assertEqual(reason.kind, "expr")
        self.assertIn("kube_pod_status_reason", reason.expr)
