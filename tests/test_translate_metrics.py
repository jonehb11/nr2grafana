"""Tests for NRQL -> PromQL translation (nr2grafana.translate.metrics).

Expected strings were derived from the translator's documented semantics:
- TIMESERIES  -> range query using $__rate_interval windows
- no TIMESERIES -> instant query using $__range windows
"""

import unittest

from nr2grafana.config import load_config
from nr2grafana.translate.common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW, UNTRANSLATABLE, offset_selectors,
)
from nr2grafana.translate.router import translate_query

HTTP = "http_server_request_duration_seconds"


def tr(nrql, cfg=None):
    return translate_query(nrql, cfg or load_config())


class TransactionTests(unittest.TestCase):
    def test_throughput_rate_count(self):
        t = tr("SELECT rate(count(*), 1 minute) FROM Transaction "
               "WHERE appName = 'checkout' TIMESERIES AUTO SINCE 1 hour ago")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{service_name="checkout"}'
            '[$__rate_interval])) * 60' % HTTP)
        self.assertEqual(t.datasource, "prometheus")
        self.assertEqual(t.query_type, "range")
        self.assertEqual(t.confidence, APPROXIMATE)
        # count-shaped aggregation: the unit follows the aggregation (a
        # request rate per minute), not the duration source metric
        self.assertIn("unit:reqpm", t.notes)
        self.assertNotIn("unit:short", t.notes)
        self.assertIn("timefrom:now-1h", t.notes)

    def test_latency_percentiles_two_targets(self):
        t = tr("SELECT percentile(duration, 95, 99) FROM Transaction "
               "WHERE appName = 'checkout' TIMESERIES SINCE 1 hour ago")
        self.assertEqual(
            t.expr,
            'histogram_quantile(0.95, sum by (le)(rate('
            '%s_bucket{service_name="checkout"}[$__rate_interval])))' % HTTP)
        self.assertEqual(t.legend, "p95")
        self.assertEqual(t.query_type, "range")
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertEqual(len(t.extra), 1)
        self.assertEqual(
            t.extra[0].expr,
            'histogram_quantile(0.99, sum by (le)(rate('
            '%s_bucket{service_name="checkout"}[$__rate_interval])))' % HTTP)
        self.assertEqual(t.extra[0].legend, "p99")

    def test_error_rate_percentage_with_status_class(self):
        t = tr("SELECT percentage(count(*), WHERE httpResponseCode >= 500) "
               "FROM Transaction WHERE appName = 'checkout' SINCE 1 hour ago")
        self.assertEqual(
            t.expr,
            '100 * (sum(increase(%s_count{service_name="checkout",'
            'http_response_status_code=~"5.."}[$__range]))) / '
            '(sum(increase(%s_count{service_name="checkout"}[$__range])))'
            % (HTTP, HTTP))
        self.assertEqual(t.query_type, "instant")
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertIn("unit:percent", t.notes)

    def test_apdex(self):
        t = tr("SELECT apdex(duration, t: 0.5) FROM Transaction "
               "WHERE appName = 'checkout' SINCE 1 hour ago")
        # (b(t) + b(4t)) / 2 / total == (satisfied + tolerating/2) / total;
        # integral bucket bounds match both le="2" and le="2.0" spellings.
        self.assertEqual(
            t.expr,
            '(sum(rate(%s_bucket{service_name="checkout",le="0.5"}'
            '[$__range])) + sum(rate(%s_bucket{service_name="checkout",'
            'le=~"2|2\\\\.0"}[$__range]))) / 2 / sum(rate(%s_count{'
            'service_name="checkout"}[$__range]))' % (HTTP, HTTP, HTTP))
        self.assertEqual(t.query_type, "instant")
        # apdex bucket-boundary caveat forces needs-review
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_facet_becomes_by_and_topk(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'checkout' "
               "FACET name LIMIT 10 SINCE 1 hour ago")
        self.assertEqual(
            t.expr,
            'topk(10, sum by (http_route)(increase(%s_count{'
            'service_name="checkout"}[$__range])))' % HTTP)
        self.assertEqual(t.group_by, ["http_route"])
        self.assertEqual(t.legend, "{{http_route}}")
        self.assertEqual(t.query_type, "instant")

    def test_compare_with_adds_offset_target(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'checkout' "
               "TIMESERIES 30 minutes SINCE 1 day ago COMPARE WITH 1 week ago")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{service_name="checkout"}'
            '[$__rate_interval])) * $__interval_ms / 1000' % HTTP)
        self.assertEqual(len(t.extra), 1)
        self.assertEqual(
            t.extra[0].expr,
            'sum(rate(%s_count{service_name="checkout"}'
            '[$__rate_interval] offset 1w)) * $__interval_ms / 1000' % HTTP)
        self.assertEqual(t.extra[0].legend, "count(*) (1w earlier)")
        self.assertIn("timefrom:now-1d", t.notes)

    def test_multi_select_extra_targets(self):
        t = tr("SELECT average(duration), percentile(duration, 95) "
               "FROM Transaction WHERE appName = 'checkout' "
               "FACET name LIMIT 25")
        self.assertEqual(
            t.expr,
            'topk(25, sum by (http_route)(rate(%s_sum{'
            'service_name="checkout"}[$__range])) / sum by (http_route)'
            '(rate(%s_count{service_name="checkout"}[$__range])))'
            % (HTTP, HTTP))
        self.assertEqual(len(t.extra), 1)
        self.assertEqual(
            t.extra[0].expr,
            'topk(25, histogram_quantile(0.95, sum by (le, http_route)'
            '(rate(%s_bucket{service_name="checkout"}[$__range]))))' % HTTP)

    def test_filter_merges_embedded_where(self):
        t = tr("SELECT filter(count(*), WHERE httpResponseCode = '500') "
               "FROM Transaction")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{http_response_status_code="500"}'
            '[$__range]))' % HTTP)


class MetricEventTests(unittest.TestCase):
    def test_gauge_heuristic_average(self):
        t = tr("SELECT average(some.gauge) FROM Metric")
        self.assertEqual(t.expr, "avg(avg_over_time(some_gauge[$__range]))")
        self.assertEqual(t.query_type, "instant")
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_counter_heuristic_count(self):
        t = tr("SELECT count(orders) FROM Metric TIMESERIES")
        self.assertEqual(t.expr,
                         "sum(rate(orders_total[$__rate_interval])) * "
                         "$__interval_ms / 1000")
        self.assertEqual(t.query_type, "range")
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_total_suffix_heuristic(self):
        t = tr("SELECT sum(my.requests_total) FROM Metric")
        self.assertEqual(t.expr,
                         "sum(increase(my_requests_total[$__range]))")
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_histogram_heuristic_percentile(self):
        t = tr("SELECT percentile(request.latency, 99) FROM Metric")
        self.assertEqual(
            t.expr,
            "histogram_quantile(0.99, sum by (le)(rate("
            "request_latency_bucket[$__range])))")
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_sum_of_unmapped_metric_is_counter_increase(self):
        # NR sum() is used on count-type metrics: increase of a counter
        # (with the configured _total suffix), flagged for review.
        t = tr("SELECT sum(checkout.orders.completed) FROM Metric "
               "WHERE deployment.environment = 'prod' "
               "FACET k8s.namespace.name TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum by (namespace)(rate(checkout_orders_completed_total{'
            'deployment_environment="prod"}[$__rate_interval])) * '
            '$__interval_ms / 1000')
        self.assertEqual(t.legend, "{{namespace}}")
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_gauge_sum_with_facet_and_matcher(self):
        cfg = load_config()
        cfg["metric_map"]["queue.depth"] = {"name": "queue_depth",
                                            "type": "gauge"}
        t = tr("SELECT sum(queue.depth) FROM Metric "
               "WHERE deployment.environment = 'prod' "
               "FACET k8s.namespace.name TIMESERIES", cfg)
        self.assertEqual(
            t.expr,
            'sum by (namespace)(avg_over_time(queue_depth{'
            'deployment_environment="prod"}[$__rate_interval]))')
        self.assertEqual(t.legend, "{{namespace}}")

    def test_metric_map_override_is_exact(self):
        cfg = load_config()
        cfg["metric_map"]["checkout.orders"] = {
            "name": "checkout_orders_total", "type": "counter"}
        t = tr("SELECT sum(checkout.orders) FROM Metric", cfg)
        self.assertEqual(t.expr,
                         "sum(increase(checkout_orders_total[$__range]))")
        self.assertEqual(t.confidence, EXACT)


class SpanMetricsTests(unittest.TestCase):
    def test_span_count_uses_calls_total(self):
        t = tr("SELECT count(*) FROM Span WHERE service.name = 'checkout' "
               "TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(traces_span_metrics_calls_total{'
            'service_name="checkout"}[$__rate_interval])) * '
            '$__interval_ms / 1000')
        self.assertEqual(t.datasource, "prometheus")
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_span_percentile_uses_duration_histogram(self):
        t = tr("SELECT percentile(duration.ms, 95) FROM Span "
               "WHERE service.name = 'checkout' FACET name LIMIT 10 "
               "TIMESERIES")
        self.assertEqual(
            t.expr,
            'topk(10, histogram_quantile(0.95, sum by (le, span_name)(rate('
            'traces_span_metrics_duration_milliseconds_bucket{'
            'service_name="checkout"}[$__rate_interval]))))')
        self.assertEqual(t.group_by, ["span_name"])
        # range topk flicker warning
        self.assertTrue(any("topk" in n for n in t.notes))

    def test_span_duration_milliseconds_unit_note(self):
        # The default otel flavor metric is
        # traces_span_metrics_duration_milliseconds; its values are
        # milliseconds, so the note must be unit:ms (not unit:s).
        t = tr("SELECT percentile(duration.ms, 95) FROM Span TIMESERIES")
        self.assertIn("unit:ms", t.notes)
        self.assertNotIn("unit:s", t.notes)


class InfraMapTests(unittest.TestCase):
    def test_systemsample_cpu_with_like_and_facet(self):
        t = tr("SELECT average(cpuPercent) FROM SystemSample "
               "WHERE hostname LIKE 'checkout-%' FACET hostname TIMESERIES")
        self.assertEqual(
            t.expr,
            '100 * (1 - avg by (instance)(rate(node_cpu_seconds_total{'
            'mode="idle",instance=~"(?i)checkout-.*"}[$__rate_interval])))')
        self.assertEqual(t.query_type, "range")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertIn("unit:percent", t.notes)
        self.assertEqual(t.legend, "{{instance}}")

    def test_k8s_restart_count_with_topk(self):
        t = tr("SELECT sum(restartCount) FROM K8sContainerSample "
               "WHERE clusterName = 'prod' FACET podName LIMIT 15 "
               "SINCE 1 day ago")
        # restartCount is sampled as a cumulative value: sum() across the
        # pod's containers is the sum of the current counts (rate() gives
        # restarts per unit of time).
        self.assertEqual(
            t.expr,
            'topk(15, sum by (pod)(last_over_time(kube_pod_container_status_'
            'restarts_total{cluster="prod"}[$__range])))')
        self.assertEqual(t.query_type, "instant")
        self.assertIn("unit:short", t.notes)

    def test_k8s_pod_count_by_phase(self):
        t = tr("SELECT count(*) FROM K8sPodSample WHERE status = 'Running'")
        self.assertEqual(t.expr,
                         'sum(kube_pod_status_phase{phase="Running"})')


class WhereOperatorTests(unittest.TestCase):
    def test_like_regex_with_metachar_escaping(self):
        # NRQL LIKE is case-insensitive -> (?i); % -> .*, regex metachars
        # escaped, then backslashes doubled by PromQL string quoting.
        t = tr("SELECT count(*) FROM Transaction WHERE name LIKE '%foo.bar%'")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{http_route=~"(?i).*foo\\\\.bar.*"}'
            '[$__range]))' % HTTP)

    def test_not_like(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE name NOT LIKE 'x%'")
        self.assertIn('http_route!~"(?i)x.*"', t.expr)

    def test_rlike_passthrough(self):
        t = tr("SELECT count(*) FROM Transaction WHERE name RLIKE 'a.+b'")
        self.assertIn('http_route=~"a.+b"', t.expr)

    def test_in_list_becomes_alternation(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE level IN ('warn', 'error')")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{level=~"warn|error"}[$__range]))' % HTTP)

    def test_not_in_list(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE level NOT IN ('debug')")
        self.assertIn('level!~"debug"', t.expr)

    def test_is_null_becomes_empty_label(self):
        t = tr("SELECT count(*) FROM Transaction WHERE userAgent IS NULL")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{userAgent=""}[$__range]))' % HTTP)

    def test_is_not_null_becomes_nonempty_label(self):
        t = tr("SELECT count(*) FROM Transaction WHERE userAgent IS NOT NULL")
        self.assertIn('userAgent!=""', t.expr)

    def test_http_response_code_ge_500_becomes_5xx_regex(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE httpResponseCode >= 500")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{http_response_status_code=~"5.."}'
            '[$__range]))' % HTTP)

    def test_http_response_code_ge_400(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE httpResponseCode >= 400")
        self.assertIn('http_response_status_code=~"4..|5.."', t.expr)

    def test_numeric_compare_on_plain_label_is_dropped(self):
        t = tr("SELECT count(*) FROM Transaction WHERE responseSize > 1")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("numeric comparison" in n for n in t.notes))
        self.assertEqual(t.expr, 'sum(increase(%s_count[$__range]))' % HTTP)

    def test_duration_threshold_becomes_bucket_arithmetic(self):
        # count(*) WHERE duration > 1 on a histogram source is exactly
        # total - bucket{le="1"} (given a bucket boundary at 1).
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' "
               "AND duration > 1 TIMESERIES")
        self.assertEqual(
            t.expr,
            '(sum(rate(%s_count{service_name="c"}[$__rate_interval])) - '
            'sum(rate(%s_bucket{service_name="c",le=~"1|1\\\\.0"}'
            '[$__rate_interval]))) * $__interval_ms / 1000' % (HTTP, HTTP))
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("bucket boundary" in n for n in t.notes))

    def test_duration_band_becomes_bucket_difference(self):
        t = tr("SELECT count(*) FROM Transaction WHERE duration >= 0.5 "
               "AND duration < 2")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_bucket{le=~"2|2\\\\.0"}[$__range])) - '
            'sum(increase(%s_bucket{le="0.5"}[$__range]))' % (HTTP, HTTP))

    def test_duration_threshold_with_arithmetic_folds(self):
        t = tr("SELECT count(*) FROM Transaction WHERE duration * 1000 > 500")
        self.assertIn('le="0.5"', t.expr)

    def test_nr_variable_equality_becomes_regex_var(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = '{{app}}'")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{service_name=~"${app:regex}"}'
            '[$__range]))' % HTTP)

    def test_nr_variable_in_list(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE appName IN ('{{app}}')")
        self.assertIn('service_name=~"${app:regex}"', t.expr)

    def test_or_on_same_attribute_merges_to_alternation(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE appName = 'a' OR appName = 'b'")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{service_name=~"a|b"}[$__range]))' % HTTP)
        # a clean merge does not degrade confidence further
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_or_across_attributes_becomes_union(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE appName = 'a' OR host = 'b'")
        # a PromQL `or` union of the two filtered selectors; series are
        # deduplicated by label set so nothing is double counted.
        self.assertEqual(
            t.expr,
            'sum((increase(%s_count{service_name="a"}[$__range]) or '
            'increase(%s_count{instance="b"}[$__range])))' % (HTTP, HTTP))
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertTrue(any("`or` union" in n for n in t.notes))

    def test_or_with_shared_and_distributes(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'a' "
               "AND (host = 'x' OR name = 'y')")
        self.assertIn('service_name="a",instance="x"', t.expr)
        self.assertIn('service_name="a",http_route="y"', t.expr)
        self.assertIn(" or ", t.expr)

    def test_negated_and_becomes_union(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE NOT (appName = 'a' AND host = 'b')")
        self.assertIn('service_name!="a"', t.expr)
        self.assertIn('instance!="b"', t.expr)
        self.assertIn(" or ", t.expr)

    def test_too_many_or_branches_dropped_with_note(self):
        conds = " OR ".join("(a%d = '1' AND b%d = '2')" % (i, i)
                            for i in range(5))
        t = tr("SELECT count(*) FROM Transaction WHERE (x = '1' OR y = '2') "
               "AND (%s)" % conds)
        self.assertTrue(any("OR alternatives" in n for n in t.notes))

    def test_span_error_flag_mapped_to_status_code_label(self):
        t = tr("SELECT count(*) FROM Span WHERE error IS TRUE TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(traces_span_metrics_calls_total{'
            'status_code="STATUS_CODE_ERROR"}[$__rate_interval])) * '
            '$__interval_ms / 1000')


class IfCasesTests(unittest.TestCase):
    def test_count_if_becomes_filtered_count(self):
        t = tr("SELECT count(if(error IS TRUE, 1)) FROM Transaction")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{http_response_status_code=~"5.."}'
            '[$__range]))' % HTTP)
        self.assertTrue(any("filtered aggregation" in n for n in t.notes))

    def test_sum_if_one_zero_becomes_filtered_count(self):
        t = tr("SELECT sum(if(httpResponseCode = '500', 1, 0)) "
               "FROM Transaction")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{http_response_status_code="500"}'
            '[$__range]))' % HTTP)

    def test_agg_if_no_else_becomes_filtered_agg(self):
        t = tr("SELECT average(if(httpResponseCode = '200', duration)) "
               "FROM Transaction")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_sum{http_response_status_code="200"}[$__range])) '
            '/ sum(rate(%s_count{http_response_status_code="200"}'
            '[$__range]))' % (HTTP, HTTP))

    def test_if_with_nontrivial_else_untranslatable(self):
        t = tr("SELECT average(if(error IS TRUE, duration, 0)) "
               "FROM Transaction")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("ELSE value" in n for n in t.notes))

    def test_facet_cases_becomes_per_case_targets(self):
        t = tr("SELECT count(*) FROM Transaction FACET cases("
               "WHERE httpResponseCode >= 500 AS 'errors', "
               "WHERE httpResponseCode < 500 AS 'ok') TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{http_response_status_code=~"5.."}'
            '[$__rate_interval])) * $__interval_ms / 1000' % HTTP)
        self.assertEqual(t.legend, "errors")
        self.assertEqual(len(t.extra), 1)
        self.assertEqual(
            t.extra[0].expr,
            'sum(rate(%s_count{http_response_status_code=~"[1234].."}'
            '[$__rate_interval])) * $__interval_ms / 1000' % HTTP)
        self.assertEqual(t.extra[0].legend, "ok")
        self.assertTrue(any("'Other' bucket" in n for n in t.notes))

    def test_facet_cases_without_alias_uses_condition_text(self):
        t = tr("SELECT count(*) FROM Transaction FACET cases("
               "WHERE appName = 'a', WHERE appName = 'b')")
        self.assertEqual(t.legend, "appName = a")
        self.assertEqual(t.extra[0].legend, "appName = b")
        self.assertIn('service_name="a"', t.expr)
        self.assertIn('service_name="b"', t.extra[0].expr)

    def test_facet_cases_unconvertible_falls_back_with_note(self):
        t = tr("SELECT count(*) FROM Transaction "
               "FACET cases(WHERE responseSize > 1 AS big)")
        # responseSize > 1 cannot be a label matcher: single unfiltered query
        self.assertEqual(
            t.expr, 'sum(increase(%s_count[$__range]))' % HTTP)
        self.assertEqual(t.extra, [])
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("could not become label matchers" in n
                            for n in t.notes))

    def test_facet_cases_on_duration_uses_bucket_arithmetic(self):
        t = tr("SELECT count(*) FROM Transaction "
               "FACET cases(WHERE duration < 0.1 AS fast, "
               "WHERE duration >= 0.1 AS slow)")
        self.assertEqual(
            t.expr, 'sum(increase(%s_bucket{le="0.1"}[$__range]))' % HTTP)
        self.assertEqual(t.legend, "fast")
        self.assertEqual(len(t.extra), 1)
        self.assertEqual(t.extra[0].legend, "slow")
        self.assertIn('_count[$__range])) - sum(increase(%s_bucket{le="0.1"}'
                      % HTTP, t.extra[0].expr)


class ConstructCoverageTests(unittest.TestCase):
    def test_derivative_gauge(self):
        t = tr("SELECT derivative(some.gauge, 1 minute) FROM Metric "
               "TIMESERIES")
        self.assertEqual(t.expr, "deriv(some_gauge[$__rate_interval]) * 60")

    def test_derivative_on_histogram_untranslatable(self):
        t = tr("SELECT derivative(duration, 1 minute) FROM Transaction")
        self.assertEqual(t.confidence, UNTRANSLATABLE)

    def test_predict_linear_horizon_seconds(self):
        t = tr("SELECT predictLinear(some.gauge, 2 hours) FROM Metric "
               "TIMESERIES")
        self.assertEqual(t.expr,
                         "predict_linear(some_gauge[$__range], 7200)")

    def test_stddev_gauge_over_time(self):
        t = tr("SELECT stddev(some.gauge) FROM Metric")
        self.assertEqual(t.expr, "stddev_over_time(some_gauge[$__range])")

    def test_stddev_histogram_untranslatable_with_reason(self):
        t = tr("SELECT stddev(duration) FROM Transaction")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("sum-of-squares" in n for n in t.notes))

    def test_earliest_untranslatable_on_metrics(self):
        t = tr("SELECT earliest(some.gauge) FROM Metric")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("first_over_time" in n for n in t.notes))

    def test_count_on_metric_counter_notes_datapoint_semantics(self):
        t = tr("SELECT count(orders) FROM Metric TIMESERIES")
        self.assertTrue(any("counts datapoints" in n for n in t.notes))

    def test_prefix_multiplier_preserved(self):
        t = tr("SELECT 1000 * average(duration) FROM Transaction")
        self.assertTrue(t.expr.endswith(") * 1000"))
        self.assertTrue(any("'* 1000' preserved" in n for n in t.notes))

    def test_slide_by_noted(self):
        t = tr("SELECT count(*) FROM Transaction TIMESERIES 5 minutes "
               "SLIDE BY 1 minute")
        self.assertTrue(any("SLIDE BY 1 minute" in n for n in t.notes))

    def test_timeseries_interval_hint_noted(self):
        t = tr("SELECT count(*) FROM Transaction TIMESERIES 30 minutes")
        self.assertIn("interval:30m", t.notes)
        self.assertTrue(any("min interval is set to 30m" in n
                            for n in t.notes))

    def test_timeseries_auto_has_no_interval_hint(self):
        t = tr("SELECT count(*) FROM Transaction TIMESERIES AUTO")
        self.assertFalse(any(n.startswith("interval:") for n in t.notes))

    def test_facet_without_limit_cardinality_note(self):
        t = tr("SELECT count(*) FROM Transaction FACET name TIMESERIES")
        self.assertTrue(any("top 10 groups" in n for n in t.notes))

    def test_facet_with_limit_no_cardinality_note(self):
        t = tr("SELECT count(*) FROM Transaction FACET name LIMIT 10")
        self.assertFalse(any("top 10 groups" in n for n in t.notes))

    def test_order_by_noted_on_faceted_query(self):
        t = tr("SELECT count(*) FROM Transaction FACET name "
               "ORDER BY count LIMIT 5")
        self.assertTrue(any("ORDER BY is not preserved" in n
                            for n in t.notes))

    def test_median_is_p50(self):
        t = tr("SELECT median(duration) FROM Transaction")
        self.assertTrue(t.expr.startswith("histogram_quantile(0.5,"))


class QueryShapeTests(unittest.TestCase):
    def test_timeseries_is_range_with_rate_interval(self):
        t = tr("SELECT count(*) FROM Transaction TIMESERIES")
        self.assertEqual(t.query_type, "range")
        self.assertIn("[$__rate_interval]", t.expr)
        self.assertNotIn("$__range", t.expr)

    def test_no_timeseries_is_instant_with_range_window(self):
        t = tr("SELECT count(*) FROM Transaction")
        self.assertEqual(t.query_type, "instant")
        self.assertIn("[$__range]", t.expr)
        self.assertNotIn("$__rate_interval", t.expr)

    def test_select_star_from_transaction_untranslatable(self):
        t = tr("SELECT * FROM Transaction")
        self.assertEqual(t.confidence, UNTRANSLATABLE)

    def test_unknown_event_type_untranslatable(self):
        t = tr("SELECT count(*) FROM SomethingWeird")
        self.assertEqual(t.confidence, UNTRANSLATABLE)

    def test_parse_error_untranslatable(self):
        t = tr("THIS IS NOT NRQL AT ALL !!!")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("could not be parsed" in n for n in t.notes))

    def test_funnel_untranslatable(self):
        t = tr("SELECT funnel(session, WHERE a = 1) FROM PageView")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        # Must give the funnel-specific explanation, not the generic
        # "no metric mapping for FROM PageView".
        self.assertTrue(any("funnel()" in n and "event-sequence" in n
                            for n in t.notes), t.notes)

    def test_funnel_untranslatable_on_mapped_event(self):
        t = tr("SELECT funnel(session, WHERE a = 1) FROM Transaction")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("funnel()" in n for n in t.notes), t.notes)

    def test_browser_event_untranslatable_names_faro(self):
        t = tr("SELECT count(*) FROM PageView")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("Faro" in n for n in t.notes), t.notes)

    def test_synthetic_event_untranslatable_names_blackbox(self):
        t = tr("SELECT count(*) FROM SyntheticCheck")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("blackbox_exporter" in n for n in t.notes),
                        t.notes)

    def test_nr_only_event_untranslatable_names_nr_plugin(self):
        t = tr("SELECT sum(consumption) FROM NrConsumption")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("New Relic datasource plugin" in n
                            for n in t.notes), t.notes)


class RatioTests(unittest.TestCase):
    def test_count_over_count_becomes_division_with_percentunit(self):
        t = tr("SELECT count(errors)/count(requests) FROM Metric TIMESERIES")
        self.assertEqual(
            t.expr,
            "(sum(rate(errors_total[$__rate_interval]))) / "
            "(sum(rate(requests_total[$__rate_interval])))")
        # ...and the per-step factor of the two counts cancelled out.
        self.assertNotIn("$__interval_ms", t.expr)
        # count/count is a proportion in [0, 1].
        self.assertIn("unit:percentunit", t.notes)

    def test_ratio_shares_facet_grouping_on_both_operands(self):
        cfg = load_config()
        cfg["metric_map"]["bytes_in"] = {"name": "bytes_in", "type": "gauge",
                                         "unit": "bytes"}
        cfg["metric_map"]["bytes_out"] = {"name": "bytes_out",
                                          "type": "gauge", "unit": "bytes"}
        t = tr("SELECT sum(bytes_in)/sum(bytes_out) FROM Metric "
               "FACET host TIMESERIES", cfg)
        self.assertEqual(
            t.expr,
            "(sum by (instance)(avg_over_time(bytes_in[$__rate_interval]))) "
            "/ (sum by (instance)(avg_over_time(bytes_out"
            "[$__rate_interval])))")
        # A ratio of two gauge sums is not a proportion: no unit forced.
        self.assertNotIn("unit:percentunit", t.notes)
        self.assertNotIn("unit:percent", t.notes)

    def test_ratio_of_two_counter_sums_is_a_proportion(self):
        t = tr("SELECT sum(errors)/sum(requests) FROM Metric TIMESERIES")
        self.assertIn("unit:percentunit", t.notes)

    def test_ratio_confidence_and_note(self):
        t = tr("SELECT count(errors)/count(requests) FROM Metric")
        self.assertIn(t.confidence, (APPROXIMATE, NEEDS_REVIEW))
        self.assertTrue(any("ratio" in n and "FACET grouping" in n
                            for n in t.notes), t.notes)

    def test_chained_ratio_nests_left(self):
        t = tr("SELECT count(a)/count(b)/count(c) FROM Metric")
        self.assertEqual(
            t.expr,
            "((sum(increase(a_total[$__range]))) / "
            "(sum(increase(b_total[$__range])))) / "
            "(sum(increase(c_total[$__range])))")


class GaugePercentileTests(unittest.TestCase):
    def test_unmapped_gauge_percentile_uses_quantile_over_time(self):
        # Not a histogram-shaped name and not in metric_map: must NOT emit
        # histogram_quantile over a nonexistent my_gauge_bucket family.
        t = tr("SELECT percentile(my_gauge, 95) FROM Metric TIMESERIES")
        self.assertEqual(
            t.expr, "quantile_over_time(0.95, my_gauge[$__rate_interval])")
        self.assertNotIn("_bucket", t.expr)
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_median_unmapped_gauge_uses_quantile_over_time(self):
        t = tr("SELECT median(cpu_temp) FROM Metric")
        self.assertEqual(t.expr,
                         "quantile_over_time(0.5, cpu_temp[$__range])")

    def test_duration_name_still_resolves_to_histogram(self):
        t = tr("SELECT percentile(request.latency, 99) FROM Metric")
        self.assertEqual(
            t.expr,
            "histogram_quantile(0.99, sum by (le)(rate("
            "request_latency_bucket[$__range])))")

    def test_counter_percentile_is_untranslatable_not_phantom_bucket(self):
        cfg = load_config()
        cfg["metric_map"]["reqs"] = {"name": "reqs_total", "type": "counter"}
        t = tr("SELECT percentile(reqs, 95) FROM Metric", cfg)
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("no sound PromQL equivalent" in n
                            for n in t.notes), t.notes)


if __name__ == "__main__":
    unittest.main()


class EmbeddedVariableTests(unittest.TestCase):
    """{{var}} placeholders that are part of a longer literal."""

    def test_variable_inside_like_pattern(self):
        t = tr("SELECT count(*) FROM Transaction WHERE host LIKE '%{{host}}%' "
               "TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{instance=~"(?i).*${host:regex}.*"}'
            '[$__rate_interval])) * $__interval_ms / 1000' % HTTP)

    def test_variable_inside_equality_literal(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'prod-{{svc}}' "
               "SINCE 1 hour ago")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{service_name=~"prod-${svc:regex}"}'
            '[$__range]))' % HTTP)

    def test_variable_inside_rlike(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName RLIKE "
               "'prod-{{svc}}.*'")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{service_name=~"prod-${svc:regex}.*"}'
            '[$__range]))' % HTTP)

    def test_variable_inside_in_list(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName IN "
               "('a-{{env}}', 'b')")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{service_name=~"a-${env:regex}|b"}'
            '[$__range]))' % HTTP)

    def test_embedded_variable_is_not_merged_into_same_attribute_or(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'a-{{env}}' "
               "OR appName = 'b'")
        self.assertEqual(
            t.expr,
            'sum((increase(%s_count{service_name=~"a-${env:regex}"}[$__range])'
            ' or increase(%s_count{service_name="b"}[$__range])))'
            % (HTTP, HTTP))

    def test_variable_as_facet_attribute(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'x' "
               "FACET {{facetAttr}} TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum by ($facetAttr)(rate(%s_count{service_name="x"}'
            '[$__rate_interval])) * $__interval_ms / 1000' % HTTP)
        self.assertEqual(t.legend, "{{$facetAttr}}")
        self.assertFalse(any("not in label_map" in n for n in t.notes))


class PerStepCountTests(unittest.TestCase):
    """TIMESERIES counts are rate * step; instant counts are the increase
    over the whole range."""

    def test_instant_count_keeps_increase_over_range(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' "
               "SINCE 1 hour ago")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{service_name="c"}[$__range]))' % HTTP)
        self.assertNotIn("$__interval_ms", t.expr)

    def test_percentage_cancels_the_step_factor(self):
        t = tr("SELECT percentage(count(*), WHERE error IS TRUE) "
               "FROM Transaction WHERE appName = 'c' TIMESERIES")
        self.assertEqual(
            t.expr,
            '100 * (sum(rate(%s_count{service_name="c",'
            'http_response_status_code=~"5.."}[$__rate_interval]))) / '
            '(sum(rate(%s_count{service_name="c"}[$__rate_interval])))'
            % (HTTP, HTTP))

    def test_filter_over_count_ratio_cancels(self):
        t = tr("SELECT filter(count(*), WHERE error IS TRUE) / count(*) "
               "FROM Transaction WHERE appName = 'c' TIMESERIES")
        self.assertEqual(
            t.expr,
            '(sum(rate(%s_count{service_name="c",'
            'http_response_status_code=~"5.."}[$__rate_interval]))) / '
            '(sum(rate(%s_count{service_name="c"}[$__rate_interval])))'
            % (HTTP, HTTP))
        self.assertIn("unit:percentunit", t.notes)

    def test_sum_over_count_average_cancels(self):
        t = tr("SELECT sum(duration) / count(*) FROM Transaction "
               "WHERE appName = 'c' TIMESERIES")
        self.assertEqual(
            t.expr,
            '(sum(rate(%s_sum{service_name="c"}[$__rate_interval]))) / '
            '(sum(rate(%s_count{service_name="c"}[$__rate_interval])))'
            % (HTTP, HTTP))

    def test_difference_keeps_both_factors(self):
        t = tr("SELECT count(*) - filter(count(*), WHERE error IS TRUE) "
               "FROM Transaction WHERE appName = 'c' TIMESERIES")
        self.assertEqual(
            t.expr,
            '(sum(rate(%s_count{service_name="c"}[$__rate_interval])) * '
            '$__interval_ms / 1000) - (sum(rate(%s_count{service_name="c",'
            'http_response_status_code=~"5.."}[$__rate_interval])) * '
            '$__interval_ms / 1000)' % (HTTP, HTTP))

    def test_bucket_band_is_scaled_once(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' "
               "AND duration > 1 AND duration <= 4 TIMESERIES")
        self.assertEqual(
            t.expr,
            '(sum(rate(%s_bucket{service_name="c",le=~"4|4\\\\.0"}'
            '[$__rate_interval])) - sum(rate(%s_bucket{service_name="c",'
            'le=~"1|1\\\\.0"}[$__rate_interval]))) * $__interval_ms / 1000'
            % (HTTP, HTTP))

    def test_nr_rate_is_untouched(self):
        t = tr("SELECT rate(count(*), 1 minute) FROM Transaction "
               "WHERE appName = 'c' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{service_name="c"}[$__rate_interval])) * 60'
            % HTTP)

    def test_macro_substitution_handles_interval_ms(self):
        from nr2grafana.livecheck import substitute
        self.assertEqual(
            substitute("sum(rate(m[$__rate_interval])) * $__interval_ms / 1000"),
            "sum(rate(m[5m])) * 60000 / 1000")


class SpanKindTests(unittest.TestCase):
    def test_span_kind_uses_enum_names(self):
        t = tr("SELECT count(*) FROM Span WHERE span.kind = 'server' "
               "AND service.name = 'x' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(traces_span_metrics_calls_total{'
            'span_kind="SPAN_KIND_SERVER",service_name="x"}'
            '[$__rate_interval])) * $__interval_ms / 1000')

    def test_span_kind_in_list(self):
        t = tr("SELECT count(*) FROM Span WHERE span.kind IN "
               "('server', 'consumer') FACET service.name")
        self.assertEqual(
            t.expr,
            'sum by (service_name)(increase(traces_span_metrics_calls_total{'
            'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"}[$__range]))')


class ImpliedTimeseriesTests(unittest.TestCase):
    NRQL = "SELECT count(*) FROM Transaction WHERE appName = 'c' SINCE 1 hour ago"

    def test_line_widget_implies_a_range_query(self):
        t = translate_query(self.NRQL, load_config(), "viz.line")
        self.assertEqual(t.query_type, "range")
        self.assertIn("$__rate_interval", t.expr)
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertTrue(any("no TIMESERIES clause" in n for n in t.notes))

    def test_billboard_keeps_the_instant_query(self):
        t = translate_query(self.NRQL, load_config(), "viz.billboard")
        self.assertEqual(t.query_type, "instant")
        self.assertFalse(any("no TIMESERIES clause" in n for n in t.notes))

    def test_explicit_timeseries_is_not_noted(self):
        t = translate_query(self.NRQL + " TIMESERIES", load_config(),
                            "viz.line")
        self.assertFalse(any("no TIMESERIES clause" in n for n in t.notes))

    def test_raw_log_listing_is_not_turned_into_a_range_query(self):
        t = translate_query("SELECT * FROM Log WHERE level = 'error'",
                            load_config(), "viz.line")
        self.assertEqual(t.expr, '{level=~"(?i)error"}')
        self.assertFalse(any("no TIMESERIES clause" in n for n in t.notes))


class MathFunctionTests(unittest.TestCase):
    AVG = ('sum(rate(%s_sum{service_name="checkout"}[$__rate_interval])) / '
           'sum(rate(%s_count{service_name="checkout"}[$__rate_interval]))'
           % (HTTP, HTTP))

    def test_round_with_places_and_inner_arithmetic(self):
        t = tr("SELECT round(average(duration) * 1000, 2) FROM Transaction "
               "WHERE appName = 'checkout' TIMESERIES")
        self.assertEqual(t.expr, "round((%s * 1000), 0.01)" % self.AVG)
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_abs_of_a_difference(self):
        t = tr("SELECT abs(average(duration) - 0.5) FROM Transaction "
               "WHERE appName = 'checkout' TIMESERIES")
        self.assertEqual(t.expr, "abs((%s - 0.5))" % self.AVG)

    def test_clamp_max_on_a_derived_infra_expression(self):
        t = tr("SELECT clamp_max(average(cpuPercent), 100) FROM SystemSample "
               "FACET hostname")
        self.assertEqual(
            t.expr,
            'clamp_max(100 * (1 - avg by (instance)(rate(node_cpu_seconds_total'
            '{mode="idle"}[$__range]))), 100)')

    def test_log_is_the_natural_logarithm_and_drops_the_unit(self):
        t = tr("SELECT log(average(duration)) FROM Transaction "
               "WHERE appName = 'checkout'")
        self.assertTrue(t.expr.startswith("ln(sum(rate("))
        self.assertFalse(any(n.startswith("unit:") for n in t.notes))

    def test_scale_text(self):
        from nr2grafana.translate.metrics import _fmt_num, _scale_text
        self.assertEqual(_scale_text(1 / 60.0), "/ 60")
        self.assertEqual(_scale_text(1000.0), "* 1000")
        self.assertEqual(_scale_text(0.5), "/ 2")
        self.assertEqual(_scale_text(0.3), "* 0.3")
        self.assertEqual(_fmt_num(1 / 1048576.0), "9.5367431640625e-07")
        t = tr("SELECT count(*) / 60 FROM Transaction WHERE appName = 'c' "
               "TIMESERIES 1 minute")
        self.assertTrue(t.expr.endswith(") / 60"), t.expr)


class UniquesTests(unittest.TestCase):
    def test_uniques_lists_label_values_as_a_table(self):
        t = tr("SELECT uniques(host) FROM Transaction WHERE appName = 'checkout'")
        self.assertEqual(
            t.expr,
            'group by (instance)(%s_count{service_name="checkout"})' % HTTP)
        self.assertEqual(t.query_type, "instant")
        self.assertEqual(t.legend, "{{instance}}")
        self.assertIn("panel-hint:table", t.notes)
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_uniques_on_an_infra_entity_metric(self):
        t = tr("SELECT uniques(hostname) FROM SystemSample "
               "WHERE hostname LIKE 'web%'")
        self.assertEqual(
            t.expr, 'group by (instance)(node_uname_info{instance=~"(?i)web.*"})')

    def test_string_attribute_has_a_real_reason(self):
        t = tr("SELECT latest(error.message) FROM TransactionError "
               "WHERE appName = 'checkout'")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("string attributes" in n for n in t.notes), t.notes)


class VariableEverywhereTests(unittest.TestCase):
    def test_percentile_from_a_variable(self):
        t = tr("SELECT percentile(duration, {{pct}}) FROM Transaction "
               "WHERE appName = 'checkout' TIMESERIES")
        self.assertEqual(
            t.expr,
            'histogram_quantile($pct / 100, sum by (le)(rate(%s_bucket{'
            'service_name="checkout"}[$__rate_interval])))' % HTTP)
        t = tr("SELECT percentile(duration, {{pct}}, 99) FROM Transaction "
               "WHERE appName = 'checkout' TIMESERIES")
        self.assertEqual(t.legend, "p$pct")
        self.assertEqual(t.extra[0].legend, "p99")

    def test_limit_from_a_variable(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'checkout' "
               "FACET name LIMIT {{limit}}")
        self.assertTrue(t.expr.startswith("topk($limit, sum by (http_route)("),
                        t.expr)

    def test_metric_name_from_a_variable(self):
        t = tr("SELECT count(*) FROM Metric WHERE metricName = '{{metric}}' "
               "TIMESERIES")
        self.assertEqual(
            t.expr, "sum(rate($metric[$__rate_interval])) * $__interval_ms / 1000")
        self.assertTrue(any("label_values(__name__)" in n for n in t.notes))
        t = tr("SELECT average({{metric}}) FROM Metric WHERE host = '{{host}}' "
               "TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg(avg_over_time($metric{instance=~"${host:regex}"}'
            '[$__rate_interval]))')

    def test_since_and_timeseries_from_variables(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'checkout' "
               "SINCE {{since}} TIMESERIES {{interval}}")
        self.assertIn("timefrom:$since", t.notes)
        self.assertIn("interval:$interval", t.notes)
        self.assertFalse(any("DROPPED" in n for n in t.notes))

    def test_threshold_from_a_variable_is_explained(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'checkout' "
               "AND duration > {{threshold}} TIMESERIES")
        self.assertTrue(any("takes its threshold from a dashboard variable"
                            in n for n in t.notes), t.notes)
        self.assertFalse(any("'duration' not in label_map" in n
                             for n in t.notes), t.notes)


class FacetCaseShapeTests(unittest.TestCase):
    def test_if_with_a_bare_boolean_condition(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'checkout' "
               "FACET if(error, 'error', 'ok') TIMESERIES")
        self.assertIn('http_response_status_code=~"5.."', t.expr)
        self.assertEqual(t.legend, "error")
        self.assertEqual(len(t.extra), 1)
        self.assertIn('http_response_status_code!~"5.."', t.extra[0].expr)
        self.assertEqual(t.extra[0].legend, "ok")

    def test_cases_combined_with_an_attribute_facet(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'checkout' "
               "FACET cases(WHERE duration < 0.1 AS 'fast', "
               "WHERE duration >= 0.1 AS 'slow'), name")
        self.assertEqual(
            t.expr,
            'sum by (http_route)(increase(%s_bucket{service_name="checkout",'
            'le="0.1"}[$__range]))' % HTTP)
        self.assertEqual(t.legend, "fast {{http_route}}")
        self.assertEqual(t.extra[0].legend, "slow {{http_route}}")
        self.assertEqual(t.group_by, ["http_route"])

    def test_sum_if_bare_boolean_over_count(self):
        t = tr("SELECT sum(if(error, 1, 0)) / count(*) FROM Transaction "
               "WHERE appName = 'checkout' TIMESERIES")
        self.assertTrue(t.expr.startswith(
            '(sum(rate(%s_count{service_name="checkout",'
            'http_response_status_code=~"5.."}' % HTTP), t.expr)


class MetricNameHandlingTests(unittest.TestCase):
    def test_rate_of_sum_uses_the_histogram_sum_series(self):
        t = tr("SELECT rate(sum(jvm.gc.duration), 1 minute) FROM Metric "
               "WHERE service.name = 'checkout' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(jvm_gc_duration_seconds_sum{service_name="checkout"}'
            '[$__rate_interval])) * 60')

    def test_count_suffix_without_a_histogram_hint_is_a_gauge(self):
        t = tr("SELECT average(process.runtime.jvm.threads.count) FROM Metric "
               "WHERE service.name = 'checkout' TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg(avg_over_time(process_runtime_jvm_threads_count{'
            'service_name="checkout"}[$__rate_interval]))')
        t = tr("SELECT sum(custom.count) FROM Metric WHERE custom.count IS "
               "NOT NULL TIMESERIES")
        self.assertEqual(t.expr,
                         "sum(avg_over_time(custom_count[$__rate_interval]))")

    def test_metric_name_restated_in_where_is_consumed(self):
        t = tr("SELECT average(custom.temp) FROM Metric WHERE metricName = "
               "'custom.temp' TIMESERIES")
        self.assertEqual(t.expr,
                         "avg(avg_over_time(custom_temp[$__rate_interval]))")
        t = tr("SELECT average(custom.temp) FROM Metric WHERE metricName = "
               "'other.metric' TIMESERIES")
        self.assertEqual(t.expr,
                         "avg(avg_over_time(custom_temp[$__rate_interval]))")
        self.assertTrue(any("different metric" in n for n in t.notes))

    def test_unique_count_of_metric_names(self):
        t = tr("SELECT uniqueCount(metricName) FROM Metric WHERE metricName "
               "LIKE 'custom.%'")
        self.assertEqual(
            t.expr, 'count(count by (__name__)({__name__=~"(?i)custom_.*"}))')

    def test_cloudwatch_sum_statistics_add_datapoints(self):
        t = tr("SELECT sum(aws.sqs.NumberOfMessagesSent) FROM Metric "
               "FACET aws.sqs.QueueName TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum by (dimension_QueueName)(sum_over_time("
            "aws_sqs_number_of_messages_sent_sum[$__interval]))")
        t = tr("SELECT sum(aws.sqs.NumberOfMessagesSent) FROM Metric")
        self.assertEqual(
            t.expr,
            "sum(sum_over_time(aws_sqs_number_of_messages_sent_sum[$__range]))")

    def test_span_metrics_status_code_words(self):
        t = tr("SELECT percentage(count(*), WHERE otel.status_code = 'ERROR') "
               "FROM Span WHERE service.name = 'checkout' FACET name")
        self.assertIn('status_code="STATUS_CODE_ERROR"', t.expr)
        self.assertNotIn('status_code="ERROR"', t.expr)


class Iteration4MetricTests(unittest.TestCase):
    AVG = ('sum(rate(%s_sum{service_name="c"}[$__rate_interval])) / '
           'sum(rate(%s_count{service_name="c"}[$__rate_interval]))'
           % (HTTP, HTTP))

    def test_boolean_predicates(self):
        t = tr("SELECT count(*) FROM Transaction WHERE true TIMESERIES")
        self.assertEqual(t.expr, "sum(rate(%s_count[$__rate_interval])) * "
                         "$__interval_ms / 1000" % HTTP)
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' AND false")
        self.assertTrue(any("always false" in n for n in t.notes))

    def test_multiplier_scales_every_percentile_target(self):
        t = tr("SELECT percentile(duration * 1000, 50, 95) FROM Transaction "
               "WHERE appName = 'c' TIMESERIES")
        self.assertTrue(t.expr.endswith(") * 1000"))
        self.assertTrue(t.extra[0].expr.endswith(") * 1000"), t.extra[0].expr)
        self.assertEqual(t.extra[0].legend, "p95")

    def test_predict_linear_over_a_histogram_average(self):
        t = tr("SELECT predictLinear(average(duration), 1 hour) "
               "FROM Transaction WHERE appName = 'c' TIMESERIES")
        self.assertEqual(t.expr,
                         "predict_linear((%s)[$__range:], 3600)" % self.AVG)

    def test_bucket_percentile_and_cdf(self):
        t = tr("SELECT bucketPercentile(duration, 95) FROM Transaction "
               "WHERE appName = 'c' TIMESERIES")
        self.assertTrue(t.expr.startswith("histogram_quantile(0.95, "))
        t = tr("SELECT getCdfValue(duration, 0.5) FROM Transaction "
               "WHERE appName = 'c'")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_bucket{service_name="c",le="0.5"}[$__range])) / '
            'sum(rate(%s_count{service_name="c"}[$__range]))' % (HTTP, HTTP))
        self.assertIn("unit:percentunit", t.notes)

    def test_rate_of_a_non_count_is_refused(self):
        t = tr("SELECT rate(uniqueCount(host), 1 minute) FROM Transaction "
               "WHERE appName = 'c' TIMESERIES")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("only rate(count(...))" in n for n in t.notes))

    def test_order_by_asc_is_bottomk(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' "
               "FACET name ORDER BY count(*) ASC LIMIT 5")
        self.assertTrue(t.expr.startswith("bottomk(5, "), t.expr)

    def test_compare_with_is_not_emitted_for_cases(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' "
               "FACET cases(WHERE error IS TRUE AS 'err', "
               "WHERE error IS FALSE AS 'ok') COMPARE WITH 1 day ago")
        self.assertEqual([e.legend for e in t.extra], ["ok"])
        self.assertFalse(any("offset" in e.expr for e in t.extra))

    def test_nested_if_facets(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' FACET "
               "if(httpResponseCode LIKE '5%', 'server-error', "
               "if(httpResponseCode LIKE '4%', 'client-error', 'ok'))")
        self.assertEqual(t.legend, "server-error")
        self.assertEqual([e.legend for e in t.extra],
                         ["client-error", "ok"])
        self.assertIn('http_response_status_code!~"(?i)5.*",'
                      'http_response_status_code=~"(?i)4.*"', t.extra[0].expr)
        self.assertIn('http_response_status_code!~"(?i)5.*",'
                      'http_response_status_code!~"(?i)4.*"', t.extra[1].expr)

    def test_always_present_http_attributes(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' AND host "
               "IS NOT NULL AND host != '' AND duration IS NOT NULL AND error "
               "IS NOT NULL")
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count{service_name="c",instance!=""}[$__range]))'
            % HTTP)

    def test_unique_count_of_name_groups_by_route(self):
        t = tr("SELECT count(*) / uniqueCount(name) FROM Transaction "
               "WHERE appName = 'c' FACET host")
        self.assertIn("count by (http_route, instance)", t.expr)
        self.assertNotIn("span_name", t.expr)

    def test_latest_of_a_label_attribute_lists_values(self):
        t = tr("SELECT latest(host) FROM Transaction WHERE appName = 'c'")
        self.assertEqual(
            t.expr, 'group by (instance)(%s_count{service_name="c"})' % HTTP)
        self.assertTrue(any("every value" in n for n in t.notes))

    def test_like_escapes(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' "
               "AND name LIKE '%\\_%' AND request.uri LIKE '50\\%'")
        self.assertIn('http_route=~"(?i).*_.*"', t.expr)
        self.assertIn('=~"(?i)50%"', t.expr)

    def test_byte_count_estimate_is_introspection(self):
        t = tr("SELECT bytecountestimate() FROM Transaction")
        self.assertEqual(t.confidence, UNTRANSLATABLE)

    def test_snapshot_widgets_drop_timeseries(self):
        t = translate_query("SELECT count(*) FROM Transaction WHERE appName = "
                            "'c' FACET name TIMESERIES", load_config(),
                            "viz.pie")
        self.assertEqual(t.query_type, "instant")
        self.assertTrue(any("TIMESERIES dropped" in n for n in t.notes))


class Iteration5MetricTests(unittest.TestCase):
    def test_numeric_string_comparison(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'App' "
               "AND response.status >= '500' TIMESERIES AUTO")
        self.assertIn('http_response_status_code=~"5.."', t.expr)

    def test_error_expected_is_explained(self):
        t = tr("SELECT count(*) FROM TransactionError WHERE appName = 'App' "
               "AND error.expected IS FALSE FACET error.class TIMESERIES")
        self.assertNotIn("error_expected", t.expr)
        self.assertTrue(any("error.expected has no label" in n
                            for n in t.notes))

    def test_call_counts_per_request(self):
        t = tr("SELECT average(databaseCallCount) FROM Transaction "
               "WHERE appName = 'App' TIMESERIES AUTO")
        self.assertEqual(
            t.expr,
            'sum(rate(db_client_operation_duration_seconds_count{'
            'service_name="App"}[$__rate_interval])) / sum(rate(%s_count{'
            'service_name="App"}[$__rate_interval]))' % HTTP)
        t = tr("SELECT sum(databaseCallCount) FROM Transaction "
               "WHERE appName = 'App' TIMESERIES AUTO")
        self.assertTrue(t.expr.startswith(
            "sum(rate(db_client_operation_duration_seconds_count{"))

    def test_gauge_count_counts_datapoints(self):
        # A known gauge (an unknown name used with count() is assumed to be
        # a counter, as before).
        t = tr("FROM Metric SELECT count(system.cpu.utilization) "
               "WHERE host.name = 'h' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(count_over_time(system_cpu_utilization_ratio{instance="h"}'
            '[$__interval]))')

    def test_string_metric_map_infers_counters(self):
        cfg = load_config()
        cfg["metric_map"]["checkout.orders.completed"] = "orders_completed_total"
        t = tr("SELECT sum(checkout.orders.completed) FROM Metric TIMESERIES",
               cfg)
        self.assertEqual(t.expr, "sum(rate(orders_completed_total"
                         "[$__rate_interval])) * $__interval_ms / 1000")

    def test_nr_ingest_metadata_is_dropped(self):
        t = tr("FROM Metric SELECT average(aws.ec2.CPUUtilization) WHERE "
               "collector.name = 'cloudwatch-metric-streams' AND tags.Name "
               "LIKE 'web%' FACET tags.Name TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg by (tag_Name)(avg_over_time(aws_ec2_cpuutilization_average{'
            'tag_Name=~"(?i)web.*"}[$__rate_interval]))')
        self.assertTrue(any("ingest metadata" in n for n in t.notes))

    def test_aws_dimension_unique_count(self):
        t = tr("FROM Metric SELECT uniqueCount(aws.ec2.InstanceId) "
               "WHERE aws.accountId = '1'")
        self.assertEqual(
            t.expr,
            'count(count by (dimension_InstanceId)(aws_ec2_info{'
            'account_id="1"}))')

    def test_traceql_mode_falls_back_to_span_metrics(self):
        cfg = load_config()
        cfg["span_aggregations"] = "traceql"
        t = tr("SELECT percentage(count(*), WHERE error IS TRUE) FROM Span "
               "WHERE service.name = 'c' TIMESERIES", cfg)
        self.assertEqual(t.datasource, "prometheus")
        self.assertTrue(any("TraceQL metrics cannot express" in n
                            for n in t.notes))


class Iteration6MetricTests(unittest.TestCase):
    T = "SELECT %s FROM Transaction WHERE appName = 'checkout'%s"

    def q(self, select, tail=" TIMESERIES"):
        return tr(self.T % (select, tail))

    def test_rate_units_follow_the_period_and_the_metric(self):
        cases = [
            ("rate(count(*), 1 minute)", "reqpm"),
            ("rate(count(*), 1 second)", "reqps"),
            ("rate(count(*), 1 hour)", "short"),
            ("rate(sum(duration), 1 minute)", "short"),
        ]
        for select, unit in cases:
            t = self.q(select)
            self.assertIn("unit:%s" % unit, t.notes, select)
            self.assertEqual(
                [n for n in t.notes if n.startswith("unit:")],
                ["unit:%s" % unit], select)
        t = tr("SELECT rate(sum(orders.completed), 1 minute) FROM Metric "
               "TIMESERIES")
        self.assertIn("unit:cpm", t.notes)
        t = tr("SELECT rate(count(*), 1 second) FROM Span "
               "WHERE service.name = 'checkout' TIMESERIES")
        self.assertIn("unit:reqps", t.notes)
        t = tr("SELECT rate(sum(system.network.io), 1 second) FROM Metric "
               "TIMESERIES")
        self.assertIn("unit:Bps", t.notes)
        t = tr("SELECT derivative(sum(orders.completed), 1 minute) FROM "
               "Metric TIMESERIES")
        self.assertIn("unit:cpm", t.notes)

    def test_rate_of_filter_keeps_the_embedded_where(self):
        t = self.q("rate(filter(count(*), WHERE error IS TRUE), 1 minute)")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{service_name="checkout",'
            'http_response_status_code=~"5.."}[$__rate_interval])) * 60'
            % HTTP)
        self.assertIn("unit:reqpm", t.notes)
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_multiple_aggregations_get_distinct_legends_with_facet(self):
        t = self.q("average(duration) AS 'Avg', percentile(duration, 95) "
                   "AS 'p95', max(duration)", " FACET name TIMESERIES")
        self.assertEqual(t.legend, "{{http_route}} Avg")
        self.assertEqual([x.legend for x in t.extra],
                         ["{{http_route}} p95",
                          "{{http_route}} max(duration)"])

    def test_multiple_aggregations_get_distinct_legends_without_facet(self):
        t = self.q("average(totalTime), average(duration), "
                   "average(databaseDuration)")
        self.assertEqual(t.legend, "average(totalTime)")
        self.assertEqual([x.legend for x in t.extra],
                         ["average(duration)", "average(databaseDuration)"])
        # aliases still win, and a single aggregation keeps the plain legend
        t = self.q("count(*) AS 'Requests', filter(count(*), WHERE error "
                   "IS TRUE) AS 'Errors'")
        self.assertEqual(t.legend, "Requests")
        self.assertEqual(t.extra[0].legend, "Errors")
        t = self.q("average(duration)", " FACET name TIMESERIES")
        self.assertEqual(t.legend, "{{http_route}}")
        t = self.q("average(duration)")
        self.assertEqual(t.legend, "average(duration)")

    def test_status_code_bands_intersect(self):
        t = self.q("count(*)", " AND httpResponseCode >= 400 AND "
                   "httpResponseCode < 500 TIMESERIES")
        self.assertIn('http_response_status_code=~"4.."', t.expr)
        self.assertEqual(t.expr.count("http_response_status_code"), 1)
        t = self.q("count(*)", " AND httpResponseCode >= 300 AND "
                   "httpResponseCode < 500 TIMESERIES")
        self.assertIn('http_response_status_code=~"[34].."', t.expr)
        # contradictory bands are left alone (no data, as in New Relic)
        t = self.q("count(*)", " AND httpResponseCode >= 500 AND "
                   "httpResponseCode < 400 TIMESERIES")
        self.assertIn('http_response_status_code=~"5.."', t.expr)
        self.assertIn('http_response_status_code=~"[123].."', t.expr)

    def test_ratio_unit_is_a_proportion_only_for_counts_over_counts(self):
        t = self.q("count(*) / uniqueCount(host)", "")
        self.assertFalse(any(n.startswith("unit:") for n in t.notes))
        t = self.q("uniqueCount(name) / uniqueCount(host)", "")
        self.assertFalse(any(n.startswith("unit:") for n in t.notes))
        t = self.q("filter(count(*), WHERE error IS TRUE) / count(*)")
        self.assertIn("unit:percentunit", t.notes)

    def test_transaction_type_and_error_expected_have_no_label_notes(self):
        t = self.q("count(*)", " AND transactionType = 'Web' TIMESERIES")
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertFalse(any("not in label_map" in n for n in t.notes))
        self.assertIn("transactionType = 'Web' is implicit for HTTP server "
                      "metrics; filter dropped", t.notes)
        t = tr("SELECT count(*) FROM TransactionError WHERE appName = 'c' "
               "AND error.expected IS FALSE")
        self.assertFalse(any("not in label_map" in n for n in t.notes))
        self.assertTrue(any("error.expected has no label" in n
                            for n in t.notes))

    def test_is_not_null_on_always_present_attributes(self):
        t = self.q("count(*)", " AND duration IS NOT NULL AND name IS NOT "
                   "NULL TIMESERIES")
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{service_name="checkout"}[$__rate_interval]))'
            ' * $__interval_ms / 1000' % HTTP)
        self.assertIn("duration IS NOT NULL is always true on HTTP server "
                      "metrics (every request carries it); dropped", t.notes)
        self.assertFalse(any("not in label_map" in n for n in t.notes))
        t = self.q("count(*)", " AND databaseDuration IS NOT NULL TIMESERIES")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertNotIn("databaseDuration", t.expr)
        self.assertTrue(any("cannot tell them apart" in n for n in t.notes))

    def test_variable_used_as_a_whole_condition_is_dropped(self):
        t = self.q("count(*)", " AND {{where}} TIMESERIES")
        self.assertNotIn("$where", t.expr)
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("used as a whole WHERE condition" in n
                            for n in t.notes))

    def test_earliest_on_derived_infra_expressions_is_refused(self):
        t = tr("SELECT earliest(cpuPercent) FROM SystemSample TIMESERIES")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any("first_over_time" in n for n in t.notes))

    def test_facet_cases_on_a_duration_threshold_needs_count(self):
        t = self.q("average(duration)", " FACET cases(WHERE duration < 1 "
                   "AS 'fast', WHERE duration >= 1 AS 'slow') TIMESERIES")
        self.assertEqual(t.extra, [])
        self.assertEqual(t.legend, "average(duration)")
        self.assertTrue(any("only count(*) can split" in n for n in t.notes))
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        # count(*) still splits into one bucket-arithmetic target per case
        t = self.q("count(*)", " FACET cases(WHERE duration < 1 AS 'fast', "
                   "WHERE duration >= 1 AS 'slow') TIMESERIES")
        self.assertEqual(t.legend, "fast")
        self.assertEqual(t.extra[0].legend, "slow")

    def test_facet_cases_or_other_bucket_is_parsed_and_noted(self):
        t = self.q("count(*)", " FACET cases(WHERE duration < 1 AS 'fast', "
                   "WHERE duration < 5 AS 'medium') OR 'slow' TIMESERIES")
        self.assertEqual([t.legend] + [x.legend for x in t.extra],
                         ["fast", "medium"])
        self.assertFalse(any("not understood" in n for n in t.notes))
        self.assertTrue(any("OR 'slow': the catch-all bucket" in n
                            for n in t.notes))

    def test_todatetime_and_case_folding_facets(self):
        t = self.q("count(*)", " FACET toDatetime(timestamp, "
                   "'yyyy-MM-dd HH:mm')")
        self.assertEqual(t.group_by, [])
        self.assertNotIn("timestamp", t.expr)
        self.assertTrue(any("toDatetime(...) buckets by time" in n
                            and "1m interval" in n for n in t.notes))
        t = self.q("count(*)", " FACET toDatetime(timestamp, 'yyyy-MM-dd')")
        self.assertTrue(any("1d interval" in n for n in t.notes))
        t = self.q("count(*)", " FACET lower(name)")
        self.assertEqual(t.group_by, ["http_route"])
        self.assertTrue(any("FACET lower(name): Prometheus label values keep "
                            "their case" in n for n in t.notes))

    def test_whole_previous_calendar_units(self):
        t = self.q("count(*)", " TIMESERIES SINCE yesterday UNTIL today")
        self.assertIn("timefrom:now/d", t.notes)
        self.assertIn("timeshift:1d/d", t.notes)
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertFalse(any("cannot be expressed" in n for n in t.notes))
        t = self.q("count(*)", " SINCE last month UNTIL this month")
        self.assertIn("timefrom:now/M", t.notes)
        self.assertIn("timeshift:1M/M", t.notes)
        # SINCE yesterday alone still means "since the start of yesterday"
        t = self.q("count(*)", " TIMESERIES SINCE yesterday")
        self.assertIn("timefrom:now-1d/d", t.notes)
        self.assertFalse(any(n.startswith("timeshift:") for n in t.notes))

    def test_variable_relative_range(self):
        t = self.q("count(*)", " TIMESERIES SINCE {{since}} minutes ago")
        self.assertIn("timefrom:now-${since}m", t.notes)
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("whole number" in n for n in t.notes))
        t = self.q("count(*)", " SINCE {{n}} days ago")
        self.assertIn("timefrom:now-${n}d", t.notes)


class Iteration7MetricTests(unittest.TestCase):
    """Findings from exporting to a real Grafana 12 and rendering."""

    def test_unaliased_single_aggregation_names_its_series(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' "
               "TIMESERIES")
        self.assertEqual(t.legend, "count(*)")
        t = tr("SELECT rate(count(*), 1 minute) FROM Transaction WHERE "
               "appName = 'c' TIMESERIES")
        self.assertEqual(t.legend, "rate(count(*), 1 minute)")
        t = tr("SELECT rate(sum(bytes), 15 minutes) FROM Metric TIMESERIES")
        self.assertEqual(t.legend, "rate(sum(bytes), 15 minutes)")
        t = tr("SELECT filter(count(*), WHERE error IS TRUE) FROM "
               "Transaction WHERE appName = 'c' TIMESERIES")
        self.assertEqual(t.legend, "filter(count(*), WHERE error = true)")
        t = tr("SELECT count(*) AS 'Requests' FROM Transaction WHERE "
               "appName = 'c' TIMESERIES")
        self.assertEqual(t.legend, "Requests")
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' "
               "TIMESERIES COMPARE WITH 1 day ago")
        self.assertEqual(t.legend, "count(*)")
        self.assertEqual(t.extra[0].legend, "count(*) (1d earlier)")
        # grouped, multi-value and multi-item legends are unchanged
        t = tr("SELECT count(*) FROM Transaction WHERE appName = 'c' "
               "FACET name TIMESERIES")
        self.assertEqual(t.legend, "{{http_route}}")
        t = tr("SELECT percentile(duration, 50, 95) FROM Transaction WHERE "
               "appName = 'c' TIMESERIES")
        self.assertEqual([t.legend] + [x.legend for x in t.extra],
                         ["p50", "p95"])


class Iteration8NestedQueryTests(unittest.TestCase):
    INNER = ("(SELECT count(*) AS c FROM Transaction WHERE appName = "
             "'checkout' FACET host)")
    PER_HOST = ('sum by (instance)(rate(%s_count{service_name="checkout"}'
                '[$__rate_interval])) * $__interval_ms / 1000' % HTTP)

    def test_average_of_per_group_counts(self):
        t = tr("SELECT average(c) FROM %s TIMESERIES" % self.INNER)
        self.assertEqual(t.expr, "avg(%s)" % self.PER_HOST)
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertEqual(t.legend, "average(c)")
        self.assertEqual(t.query_type, "range")
        self.assertIn("unit:short", t.notes)
        self.assertTrue(any(n.startswith("nested query: average(c) over the "
                                         "inner per-host count(*)")
                            for n in t.notes))
        t = tr("SELECT average(c) FROM %s" % self.INNER)
        self.assertEqual(
            t.expr,
            'avg(sum by (instance)(increase(%s_count{service_name='
            '"checkout"}[$__range])))' % HTTP)
        self.assertEqual(t.query_type, "instant")

    def test_outer_where_count_and_percentile(self):
        t = tr("SELECT count(*) FROM %s WHERE c > 100 TIMESERIES"
               % self.INNER)
        self.assertEqual(t.expr, "count((%s) > 100)" % self.PER_HOST)
        self.assertIn("unit:short", t.notes)
        t = tr("SELECT percentile(c, 95), max(c) FROM %s TIMESERIES"
               % self.INNER)
        self.assertEqual(t.expr, "quantile(0.95, %s)" % self.PER_HOST)
        self.assertEqual(t.extra[0].expr, "max(%s)" % self.PER_HOST)
        self.assertEqual([t.legend, t.extra[0].legend],
                         ["percentile(c, 95)", "max(c)"])
        t = tr("SELECT average(c) FROM %s WHERE c > 10 AND c < 1000 "
               "TIMESERIES" % self.INNER)
        self.assertEqual(t.expr, "avg(((%s) > 10) < 1000)" % self.PER_HOST)

    def test_outer_facet_over_inner_facets_and_limit(self):
        t = tr("SELECT average(p95) FROM (SELECT percentile(duration, 95) AS "
               "p95 FROM Transaction WHERE appName = 'checkout' FACET name, "
               "host) FACET name LIMIT 5")
        self.assertEqual(
            t.expr,
            'topk(5, avg by (http_route)(histogram_quantile(0.95, sum by '
            '(le, http_route, instance)(rate(%s_bucket{service_name='
            '"checkout"}[$__range])))))' % HTTP)
        self.assertEqual(t.legend, "{{http_route}} average(p95)")
        self.assertEqual(t.group_by, ["http_route"])
        self.assertIn("unit:s", t.notes)

    def test_ratio_over_nested_query(self):
        t = tr("SELECT sum(errors) / sum(total) FROM (SELECT filter(count(*), "
               "WHERE error IS TRUE) AS errors, count(*) AS total FROM "
               "Transaction WHERE appName = 'checkout' FACET host) TIMESERIES")
        errors = ('sum(sum by (instance)(rate(%s_count{service_name='
                  '"checkout",http_response_status_code=~"5.."}'
                  '[$__rate_interval])) * $__interval_ms / 1000)' % HTTP)
        self.assertEqual(t.expr, "(%s) / (sum(%s))" % (errors, self.PER_HOST))
        self.assertIn("unit:percentunit", t.notes)
        self.assertEqual(t.legend, "sum(errors) / sum(total)")

    def test_nested_over_infra_and_span_aggregations(self):
        t = tr("SELECT max(c) FROM (SELECT average(cpuPercent) AS c FROM "
               "SystemSample FACET hostname) TIMESERIES")
        self.assertEqual(
            t.expr,
            'max(100 * (1 - avg by (instance)(rate(node_cpu_seconds_total'
            '{mode="idle"}[$__rate_interval]))))')
        self.assertIn("unit:percent", t.notes)
        t = tr("SELECT average(c) FROM (SELECT count(*) AS c FROM Span WHERE "
               "service.name = 'x' FACET name) TIMESERIES")
        self.assertTrue(t.expr.startswith("avg(sum by (span_name)("))

    def test_nested_refusals_say_why(self):
        cases = [
            ("SELECT average(c) FROM (SELECT count(*) AS c FROM Transaction "
             "WHERE appName = 'x') TIMESERIES", "no FACET"),
            ("SELECT uniqueCount(c) FROM %s TIMESERIES" % self.INNER,
             "uniqueCount() over a nested query"),
            ("SELECT average(c) FROM %s FACET name TIMESERIES" % self.INNER,
             "must be one of the inner FACET attributes"),
            ("SELECT average(x) FROM %s TIMESERIES" % self.INNER,
             "not an alias of the inner SELECT"),
            ("SELECT average(c) FROM (SELECT average(c) FROM (SELECT count(*) "
             "AS c FROM Transaction FACET host) FACET host) TIMESERIES",
             "nested query inside a nested query"),
        ]
        for nrql, reason in cases:
            t = tr(nrql)
            self.assertEqual(t.confidence, UNTRANSLATABLE, nrql)
            self.assertTrue(any(reason in n for n in t.notes), (nrql, t.notes))


class Iteration9MetricTests(unittest.TestCase):
    def test_count_and_sum_suffixed_names_pick_their_histogram_series(self):
        t = tr("SELECT sum(http.server.request.duration.count) FROM Metric "
               "WHERE service.name = 'checkout' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(%s_count{service_name="checkout"}[$__rate_interval]))'
            ' * $__interval_ms / 1000' % HTTP)
        self.assertIn("unit:short", t.notes)
        self.assertNotIn("unit:s", t.notes)
        t = tr("SELECT sum(http.server.request.duration.sum) / "
               "sum(http.server.request.duration.count) FROM Metric WHERE "
               "service.name = 'checkout' TIMESERIES")
        self.assertEqual(
            t.expr,
            '(sum(rate(%s_sum{service_name="checkout"}[$__rate_interval]))) '
            '/ (sum(rate(%s_count{service_name="checkout"}'
            '[$__rate_interval])))' % (HTTP, HTTP))
        t = tr("SELECT rate(sum(http.server.requests.count), 1 minute) FROM "
               "Metric WHERE service.name = 'checkout' FACET uri TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum by (uri)(rate(http_server_requests_seconds_count'
            '{service_name="checkout"}[$__rate_interval])) * 60')
        self.assertIn("unit:reqpm", t.notes)

    def test_micrometer_names(self):
        t = tr("SELECT average(jvm.memory.used) / average(jvm.memory.max) "
               "* 100 FROM Metric WHERE service.name = 'checkout' TIMESERIES")
        self.assertIn("jvm_memory_max_bytes", t.expr)
        self.assertFalse(any("assumed gauge" in n for n in t.notes))
        t = tr("SELECT average(jvm.gc.pause) FROM Metric WHERE service.name "
               "= 'checkout' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate(jvm_gc_pause_seconds_sum{service_name="checkout"}'
            '[$__rate_interval])) / sum(rate(jvm_gc_pause_seconds_count'
            '{service_name="checkout"}[$__rate_interval]))')
        self.assertIn("unit:s", t.notes)
        t = tr("SELECT percentile(http.server.requests, 95) FROM Metric "
               "WHERE service.name = 'checkout' FACET uri TIMESERIES")
        self.assertIn("http_server_requests_seconds_bucket", t.expr)
        self.assertTrue(any("percentiles-histogram" in n for n in t.notes))
        t = tr("SELECT sum(logback.events) FROM Metric WHERE service.name = "
               "'checkout' FACET level TIMESERIES")
        self.assertIn("logback_events_total", t.expr)

    def test_cumulative_sampled_counters(self):
        t = tr("SELECT max(restartCount), latest(restartCount), "
               "sum(restartCount), average(restartCount) FROM "
               "K8sContainerSample FACET podName TIMESERIES")
        exprs = [t.expr] + [x.expr for x in t.extra]
        self.assertEqual(exprs, [
            "max by (pod)(max_over_time(kube_pod_container_status_restarts_"
            "total[$__rate_interval]))",
            "max by (pod)(last_over_time(kube_pod_container_status_restarts_"
            "total[$__interval]))",
            "sum by (pod)(last_over_time(kube_pod_container_status_restarts_"
            "total[$__interval]))",
            "avg by (pod)(avg_over_time(kube_pod_container_status_restarts_"
            "total[$__rate_interval]))"])
        self.assertTrue(any("sum() of a cumulative sampled counter" in n
                            for n in t.notes))
        t = tr("SELECT rate(sum(restartCount), 1 hour) FROM "
               "K8sContainerSample TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum(rate(kube_pod_container_status_restarts_total"
            "[$__rate_interval])) * 3600")

    def test_pod_status_reason(self):
        t = tr("SELECT latest(reason) FROM K8sPodSample FACET podName")
        self.assertEqual(t.expr,
                         "avg by (pod, reason)(kube_pod_status_reason == 1)")
        self.assertEqual(t.group_by, ["pod", "reason"])
        t = tr("SELECT uniqueCount(podName) FROM K8sPodSample WHERE "
               "reason = 'Evicted' AND status = 'Failed'")
        self.assertEqual(
            t.expr,
            'count(kube_pod_status_reason{reason="Evicted"} == 1)')
        self.assertTrue(any("implied by the reason filter" in n
                            for n in t.notes))

    def test_cast_wrappers_and_unit_note_wording(self):
        t = tr("SELECT average(numeric(duration_ms)) FROM Log WHERE "
               "service_name = 'checkout' TIMESERIES")
        self.assertEqual(t.legend, "average(duration_ms)")
        t = tr("SELECT max(memoryUsedBytes) / max(memoryTotalBytes) * 100 "
               "FROM SystemSample FACET hostname TIMESERIES")
        self.assertIn("SELECT arithmetic '* 100' preserved; panel unit set "
                      "to percent", t.notes)
        self.assertFalse(any("(was )" in n for n in t.notes))


class Iteration10MetricTests(unittest.TestCase):
    def test_numeric_where_on_infra_counts_filters_the_population(self):
        t = tr("SELECT count(*) FROM K8sDaemonsetSample WHERE podsMissing > 0")
        self.assertEqual(
            t.expr,
            "count(((avg by (namespace, daemonset)(kube_daemonset_status_"
            "desired_number_scheduled - kube_daemonset_status_number_ready))"
            " > 0))")
        self.assertTrue(any("selected by the attribute's own series" in n
                            for n in t.notes))
        t = tr("SELECT uniqueCount(podName) FROM K8sContainerSample WHERE "
               "restartCount > 5")
        self.assertEqual(
            t.expr,
            "count(count by (pod)((kube_pod_container_status_restarts_total "
            "> 5)))")
        t = tr("SELECT uniqueCount(hostname) FROM SystemSample WHERE "
               "cpuPercent > 90")
        self.assertEqual(
            t.expr,
            'count(((100 * (1 - avg by (instance)(rate(node_cpu_seconds_total'
            '{mode="idle"}[$__range])))) > 90))')
        t = tr("SELECT count(*) FROM K8sContainerSample WHERE status = "
               "'Running' AND restartCount > 3 FACET namespaceName")
        self.assertEqual(
            t.expr,
            "count by (namespace)((kube_pod_container_status_restarts_total "
            "> 3) and on (namespace, pod, container) "
            "(kube_pod_container_status_running == 1))")
        # attr = <number> on a metric-valued attribute is the same filter
        t = tr("SELECT count(*) FROM K8sPodSample WHERE isReady = 0")
        self.assertEqual(
            t.expr, 'count((kube_pod_status_ready{condition="true"} == 0))')

    def test_numeric_where_a_derived_average_cannot_take_is_reported(self):
        t = tr("SELECT average(cpuPercent) FROM SystemSample WHERE "
               "memoryUsedPercent > 90 FACET hostname")
        self.assertNotIn("90", t.expr)
        self.assertTrue(any("memoryUsedPercent > 90 cannot become a label "
                            "matcher for this derived expression" in n
                            for n in t.notes))
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_ratio_units_only_for_counts(self):
        t = tr("SELECT average(loadAverageOneMinute) / latest(coreCount) FROM "
               "SystemSample FACET hostname TIMESERIES")
        self.assertFalse(any(n.startswith("unit:") for n in t.notes))
        t = tr("SELECT sum(errors) / sum(requests) FROM Metric TIMESERIES")
        self.assertIn("unit:percentunit", t.notes)

    def test_node_allocatable_utilization(self):
        t = tr("SELECT average(allocatableCpuCoresUtilization), "
               "average(allocatableMemoryUtilization) FROM K8sNodeSample "
               "FACET nodeName TIMESERIES")
        self.assertTrue(t.expr.startswith(
            "100 * avg by (node)(sum by (node)(rate(container_cpu_usage_"
            "seconds_total"))
        self.assertIn("kube_node_status_allocatable", t.extra[0].expr)
        self.assertIn("unit:percent", t.notes)


class Iteration11MetricTests(unittest.TestCase):
    def test_root_span_filters_on_span_metrics(self):
        t = tr("SELECT count(*) FROM Span WHERE service.name = 'checkout' "
               "AND parentId IS NULL FACET name TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum by (span_name)(rate(traces_span_metrics_calls_total'
            '{service_name="checkout",span_kind=~"SPAN_KIND_SERVER|'
            'SPAN_KIND_CONSUMER"}[$__rate_interval])) * $__interval_ms / 1000')
        self.assertTrue(any("root spans) approximated as server/consumer" in n
                            for n in t.notes))
        self.assertFalse(any("not in label_map" in n for n in t.notes))
        t = tr("SELECT count(*) FROM Span WHERE service.name = 'checkout' "
               "AND nr.entryPoint IS FALSE TIMESERIES")
        self.assertIn('span_kind!~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"',
                      t.expr)


class Iteration12FuzzTests(unittest.TestCase):
    """Randomised sweep findings: every emitted query must parse, and a
    COMPARE WITH target must come from the same WHERE as the primary."""

    def test_count_spec_with_fixed_matchers_is_not_doubled(self):
        t = tr("SELECT count(*) FROM StorageSample WHERE mountPoint = '/'")
        self.assertEqual(t.expr, 'count(node_filesystem_size_bytes{fstype!~'
                                 '"tmpfs|overlay|squashfs",mountpoint="/"})')

    def test_compare_with_restores_the_phase_filter(self):
        t = tr("SELECT count(*) FROM K8sPodSample WHERE status = 'Running' "
               "COMPARE WITH 1 day ago TIMESERIES")
        self.assertEqual(t.expr, 'sum(kube_pod_status_phase{phase="Running"})')
        self.assertEqual(t.extra[0].expr,
                         'sum(kube_pod_status_phase{phase="Running"} offset 1d)')
        self.assertEqual(t.extra[0].legend, "count(*) (1d earlier)")

    def test_compare_with_keeps_numeric_population_filters(self):
        t = tr("SELECT count(*) FROM K8sDeploymentSample WHERE podsMissing > 0 "
               "COMPARE WITH 1 day ago")
        self.assertEqual(
            t.extra[0].expr,
            "count(((avg by (namespace, deployment)(kube_deployment_spec_"
            "replicas offset 1d - kube_deployment_status_replicas_available "
            "offset 1d)) > 0))")

    def test_offset_applies_to_every_selector_of_a_template(self):
        t = tr("SELECT average(memoryUsedPercent) FROM SystemSample TIMESERIES "
               "COMPARE WITH 1 day ago")
        self.assertEqual(t.extra[0].expr,
                         "100 * (1 - avg(node_memory_MemAvailable_bytes offset "
                         "1d / node_memory_MemTotal_bytes offset 1d))")
        t = tr("SELECT count(*) FROM K8sPodSample WHERE namespaceName = 'shop' "
               "COMPARE WITH 1 week ago TIMESERIES")
        self.assertEqual(t.extra[0].expr,
                         'count(kube_pod_info{namespace="shop"} offset 1w)')

    def test_offset_skips_braces_inside_quoted_regexes(self):
        t = tr("SELECT sum(cpuPercent) FROM ProcessSample WHERE "
               "processDisplayName LIKE '%{{proc}}%' COMPARE WITH 1 week ago "
               "TIMESERIES")
        self.assertEqual(
            t.extra[0].expr,
            '100 * sum(rate(namedprocess_namegroup_cpu_seconds_total{groupname'
            '=~"(?i).*${proc:regex}.*"}[$__rate_interval] offset 1w))')

    def test_offset_selectors_helper(self):
        self.assertEqual(
            offset_selectors('max_over_time(rate(m{a="b}"}[5m])[1h:])',
                             " offset 1w"),
            'max_over_time(rate(m{a="b}"}[5m] offset 1w)[1h:])')
        self.assertEqual(
            offset_selectors('count(up{}) + m{a=~"${v:regex}"} offset 5m',
                             " offset 1w"),
            'count(up{} offset 1w) + m{a=~"${v:regex}"} offset 5m')
        self.assertEqual(offset_selectors("42", " offset 1w"), "42")

    def test_unique_count_of_process_groups_counts_distinct_labels(self):
        t = tr("SELECT uniqueCount(processDisplayName) FROM ProcessSample "
               "FACET hostname")
        self.assertEqual(t.expr, "count by (instance)(count by (groupname, "
                                 "instance)(namedprocess_namegroup_num_procs))")
        t = tr("SELECT uniqueCount(hostname) FROM ProcessSample")
        self.assertEqual(t.expr, "count(count by (instance)("
                                 "namedprocess_namegroup_num_procs))")
        self.assertEqual(t.legend, "uniqueCount(hostname)")

    def test_unique_count_of_process_ids_is_the_process_count(self):
        t = tr("SELECT uniqueCount(processId) FROM ProcessSample "
               "WHERE hostname = 'web-1'")
        self.assertEqual(t.expr,
                         'sum(namedprocess_namegroup_num_procs{instance="web-1"})')
        self.assertEqual(t.legend, "uniqueCount(processId)")

    def test_numeric_filters_on_an_aggregated_population(self):
        t = tr("SELECT count(*) FROM ProcessSample WHERE cpuPercent > 50 "
               "FACET hostname")
        self.assertEqual(t.expr, "count by (instance)(((100 * (rate("
                                 "namedprocess_namegroup_cpu_seconds_total"
                                 "[$__range]))) > 50))")
        t = tr("SELECT uniqueCount(processDisplayName) FROM ProcessSample "
               "WHERE memoryResidentSizeBytes > 1000000")
        self.assertEqual(t.expr, 'count(count by (groupname)(('
                                 'namedprocess_namegroup_memory_bytes{memtype='
                                 '"resident"} > 1000000)))')

    def test_over_time_functions_of_derived_expressions_use_subqueries(self):
        t = tr("SELECT predictLinear(diskUsedPercent, 4 hours) FROM "
               "StorageSample WHERE mountPoint = '/' SINCE 1 day ago")
        fs = '{fstype!~"tmpfs|overlay|squashfs",mountpoint="/"}'
        self.assertEqual(t.expr, "predict_linear((100 * (1 - avg("
                                 "node_filesystem_avail_bytes%s / "
                                 "node_filesystem_size_bytes%s)))[$__range:], "
                                 "14400)" % (fs, fs))
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertEqual(t.legend, "predictLinear(diskUsedPercent, 4 hours)")
        t = tr("SELECT derivative(cpuPercent, 1 minute) FROM SystemSample "
               "FACET hostname TIMESERIES")
        self.assertEqual(t.expr, 'deriv((100 * (1 - avg by (instance)(rate('
                                 'node_cpu_seconds_total{mode="idle"}'
                                 '[$__rate_interval]))))[$__rate_interval:]) '
                                 '* 60')
        t = tr("SELECT stddev(cpuPercent) FROM SystemSample WHERE "
               "hostname = 'web-1'")
        self.assertEqual(t.expr, 'stddev_over_time((100 * (1 - avg(rate('
                                 'node_cpu_seconds_total{mode="idle",instance='
                                 '"web-1"}[$__range]))))[$__range:])')

    def test_rate_of_count_on_infra_samples_is_refused_with_the_reason(self):
        t = tr("SELECT rate(count(*), 1 minute) FROM K8sContainerSample")
        self.assertEqual(t.confidence, UNTRANSLATABLE)
        self.assertTrue(any(n.startswith(
            "rate(count(*), 1 minute): rate(count(*)) FROM K8sContainerSample "
            "measures New Relic's sampling rate") for n in t.notes), t.notes)

    def test_container_status_lists_select_the_state_series(self):
        t = tr("SELECT count(*) FROM K8sContainerSample WHERE status IN "
               "('Waiting', 'Terminated') FACET containerName TIMESERIES")
        self.assertEqual(t.expr, 'sum by (container)({__name__=~"kube_pod_'
                                 'container_status_(waiting|terminated)"})')
        self.assertEqual(t.confidence, APPROXIMATE)
        t = tr("SELECT count(*) FROM K8sContainerSample WHERE status LIKE "
               "'Wait%' FACET containerName")
        self.assertEqual(t.expr,
                         "sum by (container)(kube_pod_container_status_waiting)")
        t = tr("SELECT count(*) FROM K8sContainerSample WHERE status IN "
               "('a', 'b')")
        self.assertEqual(t.expr, "count(kube_pod_container_info)")
        self.assertTrue(any("matches none of the container states" in n
                            for n in t.notes))

    def test_consumed_status_filters_leave_no_label_map_note(self):
        t = tr("SELECT count(*) FROM K8sContainerSample WHERE status = 'Waiting'")
        self.assertEqual(t.expr, "sum(kube_pod_container_status_waiting)")
        self.assertFalse(any("not in label_map" in n for n in t.notes), t.notes)
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_one_note_about_counting_exporter_series(self):
        t = tr("SELECT count(*) FROM K8sPodSample WHERE namespaceName = 'shop'")
        self.assertEqual(
            sum(1 for n in t.notes if "exporter's series" in n), 1, t.notes)
