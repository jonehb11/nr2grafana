"""Tests for NRQL -> PromQL translation (nr2grafana.translate.metrics).

Expected strings were derived from the translator's documented semantics:
- TIMESERIES  -> range query using $__rate_interval windows
- no TIMESERIES -> instant query using $__range windows
"""

import unittest

from nr2grafana.config import load_config
from nr2grafana.translate.common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW, UNTRANSLATABLE,
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
        # count-shaped aggregation: unit follows the aggregation (a
        # count), not the duration source metric
        self.assertIn("unit:short", t.notes)
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
            'sum(increase(%s_count{service_name="checkout"}'
            '[$__rate_interval]))' % HTTP)
        self.assertEqual(len(t.extra), 1)
        self.assertEqual(
            t.extra[0].expr,
            'sum(increase(%s_count{service_name="checkout"}'
            '[$__rate_interval] offset 1w))' % HTTP)
        self.assertEqual(t.extra[0].legend, "(1w earlier)")
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
                         "sum(increase(orders_total[$__rate_interval]))")
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

    def test_gauge_sum_with_facet_and_matcher(self):
        t = tr("SELECT sum(checkout.orders.completed) FROM Metric "
               "WHERE deployment.environment = 'prod' "
               "FACET k8s.namespace.name TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum by (namespace)(avg_over_time(checkout_orders_completed{'
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
            'sum(increase(traces_span_metrics_calls_total{'
            'service_name="checkout"}[$__rate_interval]))')
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
        self.assertEqual(
            t.expr,
            'topk(15, sum by (pod)(kube_pod_container_status_restarts_total{'
            'cluster="prod"}))')
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
        t = tr("SELECT count(*) FROM Transaction WHERE duration > 1")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("numeric comparison" in n for n in t.notes))

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

    def test_or_across_attributes_dropped_with_note(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE appName = 'a' OR host = 'b'")
        # neither predicate can be kept as an ANDed matcher
        self.assertEqual(
            t.expr,
            'sum(increase(%s_count[$__range]))' % HTTP)
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("could not be merged into a single label matcher" in n
                            for n in t.notes))

    def test_span_error_flag_mapped_to_status_code_label(self):
        t = tr("SELECT count(*) FROM Span WHERE error IS TRUE TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(increase(traces_span_metrics_calls_total{'
            'status_code="STATUS_CODE_ERROR"}[$__rate_interval]))')


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
            'sum(increase(%s_count{http_response_status_code=~"5.."}'
            '[$__rate_interval]))' % HTTP)
        self.assertEqual(t.legend, "errors")
        self.assertEqual(len(t.extra), 1)
        self.assertEqual(
            t.extra[0].expr,
            'sum(increase(%s_count{http_response_status_code=~"[1234].."}'
            '[$__rate_interval]))' % HTTP)
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
               "FACET cases(WHERE duration > 1 AS slow)")
        # duration > 1 cannot be a label matcher: single unfiltered query
        self.assertEqual(
            t.expr, 'sum(increase(%s_count[$__range]))' % HTTP)
        self.assertEqual(t.extra, [])
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("could not become label matchers" in n
                            for n in t.notes))


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
                         "predict_linear(some_gauge[$__rate_interval], 7200)")

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
        self.assertTrue(any("'Min interval' to 30m" in n for n in t.notes))

    def test_timeseries_auto_has_no_interval_hint(self):
        t = tr("SELECT count(*) FROM Transaction TIMESERIES AUTO")
        self.assertFalse(any("Min interval" in n for n in t.notes))

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


if __name__ == "__main__":
    unittest.main()
