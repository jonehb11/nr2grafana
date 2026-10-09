"""Tests for NRQL -> CloudWatch target translation (SEAM-CW).

Expected shapes follow docs/dev/ARCHITECTURE-1.11.md: aws.<svc>.<Metric>
-> {namespace, metricName, statistic, dimensions, dimension_keys, region}
in builder mode, or a SEARCH(...) expression (metricEditorMode 1) for
filter()/multi-resource sums. Values rendered via SEAM-RENDER must never
be Python reprs (failure class F1).
"""

import json
import unittest

from nr2grafana.config import load_config
from nr2grafana.nrql.parser import parse_nrql
from nr2grafana.translate.cloudwatch import (
    NAMESPACES, PRIMARY_DIMENSION, fix_dimension_case, fix_metric_case,
    is_cloudwatch_query, namespace_for, translate_to_cloudwatch,
)
from nr2grafana.translate.common import (
    APPROXIMATE, NEEDS_REVIEW, UNTRANSLATABLE, Untranslatable,
)
from nr2grafana.translate.router import translate_query


def tr(nrql, cfg=None):
    return translate_query(nrql, cfg or load_config())


def cw(nrql, cfg=None):
    t = tr(nrql, cfg)
    assert t.datasource == "cloudwatch", (t.datasource, t.notes)
    return t


def assert_no_reprs(testcase, t):
    blob = json.dumps([getattr(x, "cw", {}) for x in [t] + t.extra])
    for marker in ("Func(", "Lit(", "Attr("):
        testcase.assertNotIn(marker, blob)
    testcase.assertNotIn("{{", blob)


class RoutingTests(unittest.TestCase):
    def test_aws_metric_routes_to_cloudwatch(self):
        t = tr("SELECT average(aws.rds.CPUUtilization) FROM Metric "
               "TIMESERIES")
        self.assertEqual(t.datasource, "cloudwatch")
        self.assertEqual(t.expr, "")
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertTrue(hasattr(t, "cw"))

    def test_non_aws_metric_stays_prometheus(self):
        t = tr("SELECT average(acme_backend.queue.depth) FROM Metric "
               "TIMESERIES")
        self.assertEqual(t.datasource, "prometheus")
        self.assertFalse(hasattr(t, "cw"))

    def test_sample_events_route_to_cloudwatch(self):
        for et in ("DatastoreSample", "QueueSample", "ServerlessSample",
                   "ComputeSample", "LoadBalancerSample",
                   "BlockDeviceSample"):
            self.assertTrue(is_cloudwatch_query(parse_nrql(
                "SELECT average(provider.x.Average) FROM %s" % et)), et)
        self.assertFalse(is_cloudwatch_query(parse_nrql(
            "SELECT count(*) FROM Transaction")))
        self.assertFalse(is_cloudwatch_query(parse_nrql(
            "SELECT count(*) FROM Log")))

    def test_nested_rate_sum_detected(self):
        self.assertTrue(is_cloudwatch_query(parse_nrql(
            "SELECT rate(sum(aws.sqs.NumberOfMessagesSent), 1 minute) "
            "FROM Metric")))

    def test_router_keeps_timing_notes(self):
        t = cw("SELECT average(aws.rds.CPUUtilization) FROM Metric "
               "SINCE 1 hour ago")
        self.assertIn("timefrom:now-1h", t.notes)
        self.assertEqual(t.query_type, "instant")


class RdsTests(unittest.TestCase):
    def test_average_with_facet_and_concat_env(self):
        t = cw("SELECT average(aws.rds.CPUUtilization) FROM Metric WHERE "
               "aws.rds.DBClusterIdentifier = concat('acme-cluster-', "
               "{{env}}) FACET aws.rds.DBInstanceIdentifier TIMESERIES")
        self.assertEqual(t.cw, {
            "namespace": "AWS/RDS", "metricName": "CPUUtilization",
            "statistic": "Average",
            "dimensions": {"DBClusterIdentifier": ["acme-cluster-$env"],
                           "DBInstanceIdentifier": ["*"]},
            "dimension_keys": ["DBInstanceIdentifier"],
            "region": "default", "queryMode": "Metrics",
            "metricEditorMode": 0, "expression": ""})
        self.assertEqual(t.legend, "{{DBInstanceIdentifier}}")
        self.assertEqual(t.group_by, ["DBInstanceIdentifier"])
        self.assertEqual(t.query_type, "range")
        self.assertIn("unit:percent", t.notes)
        assert_no_reprs(self, t)

    def test_percentile_becomes_pN(self):
        t = cw("SELECT percentile(aws.rds.ReadLatency, 95) FROM Metric "
               "WHERE aws.rds.dbInstanceIdentifier = 'acme-db' TIMESERIES")
        self.assertEqual(t.cw["statistic"], "p95")
        self.assertEqual(t.cw["metricName"], "ReadLatency")
        # case-fix table: dbInstanceIdentifier -> DBInstanceIdentifier
        self.assertEqual(t.cw["dimensions"],
                         {"DBInstanceIdentifier": ["acme-db"]})
        self.assertEqual(t.cw["metricEditorMode"], 0)
        self.assertEqual(t.legend, "p95")
        self.assertIn("unit:s", t.notes)

    def test_two_percentiles_give_two_targets(self):
        t = cw("SELECT percentile(aws.rds.ReadLatency, 95, 99) FROM Metric "
               "WHERE aws.rds.DBInstanceIdentifier = 'acme-db' TIMESERIES")
        self.assertEqual(t.cw["statistic"], "p95")
        self.assertEqual(len(t.extra), 1)
        self.assertEqual(t.extra[0].cw["statistic"], "p99")
        self.assertEqual(t.extra[0].legend, "p99")
        self.assertEqual(t.extra[0].datasource, "cloudwatch")

    def test_max_min_count_statistics(self):
        self.assertEqual(cw("SELECT max(aws.rds.DatabaseConnections) FROM "
                            "Metric WHERE aws.rds.DBInstanceIdentifier = "
                            "'acme-db'").cw["statistic"], "Maximum")
        self.assertEqual(cw("SELECT min(aws.rds.FreeableMemory) FROM "
                            "Metric WHERE aws.rds.DBInstanceIdentifier = "
                            "'acme-db'").cw["statistic"], "Minimum")
        self.assertEqual(cw("SELECT count(aws.rds.CPUUtilization) FROM "
                            "Metric WHERE aws.rds.DBInstanceIdentifier = "
                            "'acme-db'").cw["statistic"], "SampleCount")

    def test_latest_is_average_with_note(self):
        t = cw("SELECT latest(aws.rds.CPUUtilization) FROM Metric WHERE "
               "aws.rds.DBInstanceIdentifier = {{db}}")
        self.assertEqual(t.cw["statistic"], "Average")
        self.assertEqual(t.cw["dimensions"],
                         {"DBInstanceIdentifier": ["$db"]})
        self.assertTrue(any("latest()" in n for n in t.notes))
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_average_across_all_instances_uses_search_avg(self):
        t = cw("SELECT average(aws.rds.CPUUtilization) FROM Metric "
               "TIMESERIES")
        self.assertEqual(t.cw["metricEditorMode"], 1)
        self.assertEqual(
            t.cw["expression"],
            "AVG(SEARCH('{AWS/RDS,DBInstanceIdentifier} "
            "MetricName=\"CPUUtilization\"', 'Average', 300))")
        self.assertEqual(t.cw["dimensions"], {})


class SqsTests(unittest.TestCase):
    def test_multi_queue_sum_is_search_expression(self):
        t = cw("SELECT sum(aws.sqs.NumberOfMessagesSent) FROM Metric "
               "WHERE aws.sqs.QueueName IN ('acme-queue-a', "
               "'acme-queue-b') TIMESERIES")
        self.assertEqual(t.cw["namespace"], "AWS/SQS")
        self.assertEqual(t.cw["metricName"], "NumberOfMessagesSent")
        self.assertEqual(t.cw["statistic"], "Sum")
        self.assertEqual(t.cw["metricEditorMode"], 1)
        self.assertEqual(
            t.cw["expression"],
            "SUM(SEARCH('{AWS/SQS,QueueName} "
            "MetricName=\"NumberOfMessagesSent\" "
            "(QueueName=\"acme-queue-a\" OR QueueName=\"acme-queue-b\")', "
            "'Sum', 300))")
        self.assertEqual(t.cw["dimension_keys"], [])
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_or_on_same_dimension_merges(self):
        t = cw("SELECT sum(aws.sqs.NumberOfMessagesReceived) FROM Metric "
               "WHERE aws.sqs.QueueName = 'acme-queue-a' OR "
               "aws.sqs.QueueName = 'acme-queue-b'")
        self.assertIn("(QueueName=\"acme-queue-a\" OR "
                      "QueueName=\"acme-queue-b\")", t.cw["expression"])

    def test_filter_forces_search(self):
        t = cw("SELECT filter(sum(aws.sqs.ApproximateNumberOfMessages"
               "Visible), WHERE aws.sqs.QueueName = 'acme-queue-a') "
               "FROM Metric TIMESERIES")
        self.assertEqual(t.cw["metricEditorMode"], 1)
        self.assertEqual(
            t.cw["expression"],
            "SUM(SEARCH('{AWS/SQS,QueueName} "
            "MetricName=\"ApproximateNumberOfMessagesVisible\" "
            "QueueName=\"acme-queue-a\"', 'Sum', 300))")
        # the pinned value is still carried in dimensions for readers
        self.assertEqual(t.cw["dimensions"],
                         {"QueueName": ["acme-queue-a"]})

    def test_like_becomes_partial_token_match_needs_review(self):
        t = cw("SELECT sum(aws.sqs.NumberOfMessagesSent) FROM Metric "
               "WHERE aws.sqs.QueueName LIKE 'acme-queue-%' TIMESERIES")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertIn("MetricName=\"NumberOfMessagesSent\" acme-queue'",
                      t.cw["expression"])
        self.assertTrue(any("LIKE" in n for n in t.notes))

    def test_facet_with_multi_filter_has_no_wrapper(self):
        t = cw("SELECT sum(aws.sqs.NumberOfMessagesSent) FROM Metric "
               "WHERE aws.sqs.QueueName IN ('acme-queue-a', "
               "'acme-queue-b') FACET aws.sqs.QueueName TIMESERIES")
        self.assertTrue(t.cw["expression"].startswith("SEARCH("))
        self.assertEqual(t.cw["dimension_keys"], ["QueueName"])
        self.assertEqual(t.legend, "{{QueueName}}")

    def test_sum_without_timeseries_notes_total_reducer(self):
        t = cw("SELECT sum(aws.sqs.NumberOfMessagesSent) FROM Metric "
               "WHERE aws.sqs.QueueName = 'acme-queue-a' SINCE 1 day ago")
        self.assertEqual(t.query_type, "instant")
        self.assertTrue(any("reducer" in n for n in t.notes))

    def test_not_equal_becomes_not_term(self):
        t = cw("SELECT sum(aws.sqs.NumberOfMessagesSent) FROM Metric "
               "WHERE aws.sqs.QueueName != 'acme-queue-dlq'")
        self.assertIn("NOT QueueName=\"acme-queue-dlq\"",
                      t.cw["expression"])

    def test_period_configurable(self):
        cfg = load_config()
        cfg["cloudwatch_period"] = 60
        t = cw("SELECT sum(aws.sqs.NumberOfMessagesSent) FROM Metric "
               "WHERE aws.sqs.QueueName IN ('a', 'b')", cfg)
        self.assertTrue(t.cw["expression"].endswith("'Sum', 60))"))


class LambdaTests(unittest.TestCase):
    def test_multiple_select_items_facet_function(self):
        t = cw("SELECT max(aws.lambda.Duration), sum(aws.lambda.Errors) "
               "FROM Metric FACET aws.lambda.FunctionName TIMESERIES")
        self.assertEqual(t.cw["namespace"], "AWS/Lambda")
        self.assertEqual(t.cw["metricName"], "Duration")
        self.assertEqual(t.cw["statistic"], "Maximum")
        self.assertEqual(t.cw["dimensions"], {"FunctionName": ["*"]})
        self.assertEqual(t.cw["dimension_keys"], ["FunctionName"])
        self.assertEqual(len(t.extra), 1)
        self.assertEqual(t.extra[0].cw["metricName"], "Errors")
        self.assertEqual(t.extra[0].cw["statistic"], "Sum")
        self.assertEqual(t.extra[0].legend, "{{FunctionName}}")
        self.assertIn("unit:ms", t.notes)

    def test_account_filter_dropped_with_note_not_review(self):
        t = cw("SELECT sum(aws.lambda.Invocations) FROM Metric WHERE "
               "aws.accountId = '111122223333' TIMESERIES")
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertTrue(any("aws.accountId" in n and "credentials" in n
                            for n in t.notes))
        self.assertEqual(
            t.cw["expression"],
            "SUM(SEARCH('{AWS/Lambda,FunctionName} "
            "MetricName=\"Invocations\"', 'Sum', 300))")

    def test_region_from_where(self):
        t = cw("SELECT sum(aws.lambda.Invocations) FROM Metric WHERE "
               "aws.region = 'us-west-2' AND aws.lambda.FunctionName = "
               "'acme-fn'")
        self.assertEqual(t.cw["region"], "us-west-2")
        self.assertEqual(t.cw["dimensions"], {"FunctionName": ["acme-fn"]})

    def test_region_from_config(self):
        cfg = load_config()
        cfg["cloudwatch_region"] = "eu-central-1"
        t = cw("SELECT sum(aws.lambda.Invocations) FROM Metric WHERE "
               "aws.lambda.FunctionName = 'acme-fn'", cfg)
        self.assertEqual(t.cw["region"], "eu-central-1")

    def test_rate_of_sum_is_approximate_sum(self):
        t = cw("SELECT rate(sum(aws.lambda.Invocations), 1 minute) FROM "
               "Metric WHERE aws.lambda.FunctionName = 'acme-fn' "
               "TIMESERIES")
        self.assertEqual(t.cw["statistic"], "Sum")
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertTrue(any("rate(" in n for n in t.notes))

    def test_alias_becomes_legend(self):
        t = cw("SELECT sum(aws.lambda.Errors) AS 'Errors' FROM Metric "
               "WHERE aws.lambda.FunctionName = 'acme-fn' TIMESERIES")
        self.assertEqual(t.legend, "Errors")


class NamespaceTests(unittest.TestCase):
    def test_contract_namespace_map(self):
        expected = {
            "rds": "AWS/RDS", "sqs": "AWS/SQS", "lambda": "AWS/Lambda",
            "ec2": "AWS/EC2", "ebs": "AWS/EBS", "elb": "AWS/ELB",
            "alb": "AWS/ApplicationELB", "nlb": "AWS/NetworkELB",
            "dynamodb": "AWS/DynamoDB", "s3": "AWS/S3",
            "kinesis": "AWS/Kinesis", "sns": "AWS/SNS", "ecs": "AWS/ECS",
            "eks": "ContainerInsights", "elasticache": "AWS/ElastiCache",
            "apigateway": "AWS/ApiGateway", "cloudfront": "AWS/CloudFront",
        }
        for k, v in expected.items():
            self.assertEqual(NAMESPACES[k], v, k)
            self.assertIn(v, PRIMARY_DIMENSION, v)

    def test_unknown_service_guessed_and_flagged(self):
        t = cw("SELECT average(aws.foo.Bar) FROM Metric WHERE "
               "aws.foo.Thing = 'x'")
        self.assertEqual(t.cw["namespace"], "AWS/Foo")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("cloudwatch_namespaces" in n for n in t.notes))

    def test_config_extends_namespaces(self):
        cfg = load_config()
        cfg["cloudwatch_namespaces"] = {"foo": "ACME/Foo"}
        self.assertEqual(namespace_for("foo", cfg), ("ACME/Foo", True))
        t = cw("SELECT average(aws.foo.Bar) FROM Metric WHERE "
               "aws.foo.Thing = 'x'", cfg)
        self.assertEqual(t.cw["namespace"], "ACME/Foo")
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_case_fix_tables(self):
        self.assertEqual(fix_dimension_case("dbClusterIdentifier"),
                         "DBClusterIdentifier")
        self.assertEqual(fix_dimension_case("queueName"), "QueueName")
        self.assertEqual(fix_dimension_case("loadBalancer"), "LoadBalancer")
        self.assertEqual(fix_dimension_case("customDim"), "CustomDim")
        self.assertEqual(fix_metric_case("cpuUtilization"), "CPUUtilization")
        self.assertEqual(fix_metric_case("databaseConnections"),
                         "DatabaseConnections")

    def test_alb_metric_name_kept_verbatim(self):
        t = cw("SELECT sum(aws.applicationelb.HTTPCode_Target_5XX_Count) "
               "FROM Metric FACET aws.applicationelb.LoadBalancer "
               "TIMESERIES")
        self.assertEqual(t.cw["namespace"], "AWS/ApplicationELB")
        self.assertEqual(t.cw["metricName"], "HTTPCode_Target_5XX_Count")
        self.assertEqual(t.cw["dimensions"], {"LoadBalancer": ["*"]})


class SampleEventTests(unittest.TestCase):
    def test_datastore_sample_rds(self):
        t = cw("SELECT average(provider.cpuUtilization.Average) FROM "
               "DatastoreSample WHERE provider = 'RdsDbInstance' "
               "FACET displayName TIMESERIES")
        self.assertEqual(t.cw["namespace"], "AWS/RDS")
        self.assertEqual(t.cw["metricName"], "CPUUtilization")
        self.assertEqual(t.cw["statistic"], "Average")
        # displayName -> the namespace's identifying dimension
        self.assertEqual(t.cw["dimensions"],
                         {"DBInstanceIdentifier": ["*"]})
        self.assertEqual(t.cw["dimension_keys"], ["DBInstanceIdentifier"])

    def test_queue_sample_statistic_from_attribute_suffix(self):
        t = cw("SELECT latest(provider.approximateNumberOfMessagesVisible"
               ".Sum) FROM QueueSample WHERE provider.queueName = "
               "'acme-queue-a'")
        self.assertEqual(t.cw["namespace"], "AWS/SQS")
        self.assertEqual(t.cw["metricName"],
                         "ApproximateNumberOfMessagesVisible")
        self.assertEqual(t.cw["statistic"], "Sum")
        self.assertEqual(t.cw["dimensions"],
                         {"QueueName": ["acme-queue-a"]})

    def test_serverless_sample_default_service(self):
        t = cw("SELECT sum(provider.invocations.Sum) FROM ServerlessSample "
               "FACET provider.functionName TIMESERIES")
        self.assertEqual(t.cw["namespace"], "AWS/Lambda")
        self.assertEqual(t.cw["metricName"], "Invocations")
        self.assertEqual(t.cw["dimension_keys"], ["FunctionName"])


class DroppedAndUntranslatableTests(unittest.TestCase):
    def test_tag_filter_dropped_needs_review(self):
        t = cw("SELECT average(aws.rds.CPUUtilization) FROM Metric WHERE "
               "tags.Environment = 'prod' AND aws.rds.DBInstanceIdentifier "
               "= 'acme-db'")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertEqual(t.cw["dimensions"],
                         {"DBInstanceIdentifier": ["acme-db"]})
        self.assertTrue(any("tags.Environment" in n for n in t.notes))

    def test_or_across_dimensions_dropped(self):
        t = cw("SELECT average(aws.rds.CPUUtilization) FROM Metric WHERE "
               "aws.rds.DBInstanceIdentifier = 'a' OR aws.rds.Engine = "
               "'mysql'")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("OR clause" in n for n in t.notes))

    def test_facet_function_dropped(self):
        t = cw("SELECT average(aws.rds.CPUUtilization) FROM Metric WHERE "
               "aws.rds.DBInstanceIdentifier = 'a' FACET "
               "cases(WHERE aws.rds.Engine = 'mysql')")
        self.assertEqual(t.cw["dimension_keys"], [])
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_uniquecount_untranslatable_via_router(self):
        t = tr("SELECT uniqueCount(aws.sqs.QueueName) FROM Metric")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("uniquecount()" in n for n in t.notes))

    def test_ratio_becomes_metric_math_over_hidden_operands(self):
        t = tr("SELECT sum(aws.lambda.Errors) / sum(aws.lambda.Invocations)"
               " FROM Metric")
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertTrue(any("Metric Math" in n for n in t.notes))
        parts = [t] + t.extra
        self.assertEqual([p.cw.get("id") for p in parts], ["m1", "m2", None])
        self.assertEqual([p.cw["metricName"] for p in parts],
                         ["Errors", "Invocations", ""])
        self.assertTrue(all(p.cw.get("hide") for p in parts[:2]))
        self.assertEqual(parts[2].cw["metricEditorMode"], 1)
        self.assertEqual(parts[2].cw["expression"], "m1 / m2")

    def test_sum_of_filters_becomes_metric_math(self):
        t = tr("SELECT filter(latest(aws.sqs.ApproximateNumberOfMessages"
               "Visible), WHERE aws.sqs.QueueName = 'a') + filter(latest("
               "aws.sqs.ApproximateNumberOfMessagesVisible), WHERE "
               "aws.sqs.QueueName = 'b') AS 'DLQ' FROM Metric TIMESERIES")
        parts = [t] + t.extra
        self.assertEqual(parts[-1].cw["expression"], "m1 + m2")
        self.assertEqual(len(parts), 3)

    def test_raw_select_untranslatable(self):
        with self.assertRaises(Untranslatable):
            translate_to_cloudwatch(parse_nrql(
                "SELECT aws.rds.CPUUtilization FROM Metric"), load_config())

    def test_second_item_non_aws_dropped_first_kept(self):
        t = cw("SELECT average(aws.rds.CPUUtilization), "
               "average(acme_backend.queue.depth) FROM Metric WHERE "
               "aws.rds.DBInstanceIdentifier = 'a'")
        self.assertEqual(t.cw["metricName"], "CPUUtilization")
        self.assertEqual(t.extra, [])
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_compare_with_noted(self):
        t = cw("SELECT average(aws.rds.CPUUtilization) FROM Metric WHERE "
               "aws.rds.DBInstanceIdentifier = 'a' COMPARE WITH 1 week ago")
        self.assertTrue(any("COMPARE WITH" in n for n in t.notes))


if __name__ == "__main__":
    unittest.main()
