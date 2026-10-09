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
        # 1.11 SEAM-KIND: the AGG-RULE is deterministic -> approximate.
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_counter_heuristic_count(self):
        t = tr("SELECT count(orders) FROM Metric TIMESERIES")
        self.assertEqual(t.expr,
                         "sum(increase(orders_total[$__rate_interval]))")
        self.assertEqual(t.query_type, "range")
        self.assertEqual(t.confidence, APPROXIMATE)

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

    def test_event_counter_sum_with_facet_and_matcher(self):
        # 1.11 (F2): sum() of an event-word app metric is a counter -> the
        # per-bucket increase, not avg_over_time of an assumed gauge.
        t = tr("SELECT sum(checkout.orders.completed) FROM Metric "
               "WHERE deployment.environment = 'prod' "
               "FACET k8s.namespace.name TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum by (namespace)(increase(checkout_orders_completed_total{'
            'deployment_environment="prod"}[$__interval]))')
        self.assertEqual(t.legend, "{{namespace}}")

    def test_gauge_word_sum_stays_avg_over_time(self):
        t = tr("SELECT sum(checkout.cart.size) FROM Metric "
               "FACET k8s.namespace.name TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum by (namespace)(avg_over_time(checkout_cart_size"
            "[$__rate_interval]))")

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


class RatioTests(unittest.TestCase):
    def test_count_over_count_becomes_division_with_percentunit(self):
        t = tr("SELECT count(errors)/count(requests) FROM Metric TIMESERIES")
        self.assertEqual(
            t.expr,
            "(sum(increase(errors_total[$__rate_interval]))) / "
            "(sum(increase(requests_total[$__rate_interval])))")
        # count/count is a proportion in [0, 1].
        self.assertIn("unit:percentunit", t.notes)

    def test_ratio_shares_facet_grouping_on_both_operands(self):
        t = tr("SELECT sum(bytes_in)/sum(bytes_out) FROM Metric "
               "FACET host TIMESERIES")
        self.assertEqual(
            t.expr,
            "(sum by (instance)(avg_over_time(bytes_in[$__rate_interval]))) "
            "/ (sum by (instance)(avg_over_time(bytes_out"
            "[$__rate_interval])))")
        # A sum/sum ratio is not necessarily a percentage: no unit forced.
        self.assertNotIn("unit:percentunit", t.notes)
        self.assertNotIn("unit:percent", t.notes)

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

# ---------------------------------------------------------------------------
# 1.11 translation-fidelity rules (ARCHITECTURE-1.11 section 0/1)
# ---------------------------------------------------------------------------

from unittest import mock  # noqa: E402

from nr2grafana.nrql.parser import Attr, Func, Lit, parse_nrql  # noqa: E402
from nr2grafana.translate.common import (  # noqa: E402
    Matcher, Untranslatable, assert_no_repr, has_ast_repr, k8s_attr_label,
    like_to_regex_keep_vars, map_attr, regex_escape_keep_vars, render_value,
    split_app_env, value_vars,
)
from nr2grafana.translate.metrics import (  # noqa: E402
    K8S_METRIC_MAP, infer_metric_kind, translate_to_promql,
)

CONCAT = Func("concat", args=[Lit("p-"), Attr("{{env}}")])


def no_repr(text):
    return not has_ast_repr(text)


class RenderValueTests(unittest.TestCase):
    """SEAM-RENDER: render_value(node, cfg) -> (text, kind)."""

    def test_concat_literal_and_var_is_mixed(self):
        self.assertEqual(render_value(CONCAT, {}), ("p-$env", "mixed"))

    def test_bare_var_attr_is_var(self):
        self.assertEqual(render_value(Attr("{{env}}"), {}), ("$env", "var"))

    def test_bare_var_literal_is_var(self):
        self.assertEqual(render_value(Lit("{{env}}"), {}), ("$env", "var"))

    def test_literal_is_literal(self):
        self.assertEqual(render_value(Lit("prod"), {}), ("prod", "literal"))
        self.assertEqual(render_value(Lit(3.0), {}), ("3", "literal"))

    def test_var_rename_applied(self):
        self.assertEqual(
            render_value(Attr("{{datasource}}"),
                         {"var_renames": {"datasource": "nr_datasource"}}),
            ("$nr_datasource", "var"))

    def test_var_followed_by_word_char_is_braced(self):
        node = Func("concat", args=[Attr("{{env}}"), Lit("x")])
        self.assertEqual(render_value(node, {}), ("${env}x", "mixed"))

    def test_var_inside_literal_text(self):
        self.assertEqual(render_value(Lit("p-{{env}}-a"), {}),
                         ("p-$env-a", "mixed"))

    def test_concat_of_literals_is_literal(self):
        node = Func("concat", args=[Lit("a"), Lit("b")])
        self.assertEqual(render_value(node, {}), ("ab", "literal"))

    def test_lower_of_literal_applied(self):
        self.assertEqual(render_value(Func("lower", args=[Lit("ABC")]), {}),
                         ("abc", "literal"))

    def test_unknown_function_is_unsupported_not_repr(self):
        text, kind = render_value(Func("someFn", args=[Lit("x")]), {})
        self.assertEqual(kind, "unsupported")
        self.assertTrue(no_repr(text), text)

    def test_value_vars(self):
        self.assertEqual(value_vars(CONCAT, {}), ["env"])
        self.assertEqual(value_vars(Lit("x"), {}), [])

    def test_regex_escape_keeps_vars(self):
        self.assertEqual(regex_escape_keep_vars("p-$env.x"), "p-$env\\.x")
        self.assertEqual(regex_escape_keep_vars("${env:regex}"),
                         "${env:regex}")

    def test_like_to_regex_keeps_vars(self):
        self.assertEqual(like_to_regex_keep_vars("p-$env%"), "p-$env.*")

    def test_repr_guard_detects_and_raises(self):
        self.assertTrue(has_ast_repr(
            "cluster=\"Func(name='concat', args=[Lit(value='p-')])\""))
        self.assertFalse(has_ast_repr('cluster="p-$env"'))
        with self.assertRaises(Untranslatable):
            assert_no_repr("ok", "x{a=\"Attr(name='b')\"}")
        assert_no_repr('x{a="b"}')


class F1ConcatRenderingTests(unittest.TestCase):
    """F1: concat('p-', {{env}}) in WHERE must render as p-$env."""

    def test_equality_concat_renders_var(self):
        t = tr("SELECT sum(acme_backend.order.created) FROM Metric "
               "WHERE k8s.clusterName = concat('p-', {{env}}) TIMESERIES")
        self.assertIn('cluster="p-$env"', t.expr)
        self.assertTrue(no_repr(t.expr), t.expr)
        self.assertEqual(t.vars, ["env"])

    def test_in_list_with_concat_is_alternation(self):
        t = tr("SELECT sum(acme_backend.order.created) FROM Metric "
               "WHERE k8s.clusterName IN (concat('p-', {{env}}), 'q-x')")
        self.assertIn('cluster=~"p-$env|q-x"', t.expr)
        self.assertTrue(no_repr(t.expr), t.expr)

    def test_or_with_concat_merges(self):
        t = tr("SELECT sum(acme_backend.order.created) FROM Metric "
               "WHERE k8s.clusterName = concat('p-', {{env}}) "
               "OR k8s.clusterName = concat('q-', {{env}})")
        self.assertIn('cluster=~"p-$env|q-$env"', t.expr)
        self.assertEqual(t.vars, ["env"])

    def test_like_with_concat_keeps_var(self):
        t = tr("SELECT sum(acme_backend.events) FROM Metric "
               "WHERE cluster LIKE concat('p-', {{env}}, '%')")
        self.assertIn('cluster=~"(?i)p-$env.*"', t.expr)

    def test_unsupported_value_function_dropped_with_note(self):
        t = tr("SELECT sum(acme_backend.messages) FROM Metric "
               "WHERE cluster = someFn('X')")
        self.assertEqual(t.expr,
                         "sum(increase(acme_backend_messages_total"
                         "[$__range]))")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("predicate DROPPED" in n for n in t.notes))
        self.assertTrue(all(no_repr(n) for n in t.notes), t.notes)

    def test_lower_literal_applied_in_where(self):
        t = tr("SELECT sum(acme_backend.messages) FROM Metric "
               "WHERE cluster = lower('X')")
        self.assertIn('cluster="x"', t.expr)

    def test_hard_guard_catches_leaked_repr(self):
        q = parse_nrql("SELECT sum(acme_backend.events) FROM Metric "
                       "WHERE cluster = 'x'")
        leaked = [Matcher("cluster", "=", "Func(name='concat', args=[])")]
        with mock.patch("nr2grafana.translate.metrics.cond_to_matchers",
                        return_value=leaked):
            with self.assertRaises(Untranslatable) as cm:
                translate_to_promql(q, load_config())
        self.assertIn("Python AST repr", str(cm.exception))


class MetricKindTests(unittest.TestCase):
    """SEAM-KIND: infer_metric_kind(nr_name, agg, cfg, hints)."""

    def kind(self, name, agg, cfg=None, hints=None):
        return infer_metric_kind(name, agg, cfg or load_config(), hints)

    def test_deterministic_rename_dots_dashes_camel(self):
        src = self.kind("acme-backend.orderCreated.count", "latest")
        self.assertEqual(src.base, "acme_backend_orderCreated_count")
        self.assertEqual(src.mtype, "gauge")

    def test_event_word_sum_is_counter_with_total(self):
        src = self.kind("acme_backend.order.created", "sum")
        self.assertEqual((src.base, src.mtype),
                         ("acme_backend_order_created_total", "counter"))
        self.assertEqual(src.kind_source, "name_rule")
        self.assertEqual(src.confidence, APPROXIMATE)

    def test_gauge_words_match_whole_words_only(self):
        # 'titration' must not read as 'ratio', 'storage' not as 'age'.
        src = self.kind("acme_backend.sched_task.titration_refill", "count")
        self.assertEqual(src.mtype, "counter")
        self.assertTrue(src.base.endswith("_total"))
        src = self.kind("host.memoryUsedPercent", "average")
        self.assertEqual((src.mtype, src.kind_source), ("gauge", "name_rule"))
        src = self.kind("acme_backend.queue_size_by_priority", "sum")
        self.assertEqual(src.mtype, "gauge")
        src = self.kind("acme_backend.unknown.thing", "percentile")
        self.assertEqual(src.confidence, NEEDS_REVIEW)

    def test_event_word_count_is_counter(self):
        for name in ("x.requests", "x.errors", "x.status_500",
                     "x.messages", "x.tasks", "x.signal", "x.retries"):
            self.assertEqual(self.kind(name, "count").mtype, "counter",
                             name)

    def test_event_word_with_latest_is_not_counter(self):
        # The event-word rule only fires for sum()/count().
        src = self.kind("acme_backend.order.created", "latest")
        self.assertEqual(src.mtype, "gauge")
        self.assertEqual(src.base, "acme_backend_order_created")

    def test_total_suffix_config_off(self):
        cfg = load_config()
        cfg["metric_total_suffix"] = False
        src = self.kind("acme_backend.order.created", "sum", cfg)
        self.assertEqual(src.base, "acme_backend_order_created")

    def test_gauge_words(self):
        for name in ("x.cpuPercent", "x.utilization", "x.error.ratio",
                     "x.heap.bytes", "x.cores", "x.queue.count",
                     "x.pool.size", "x.disk.free", "x.pods.desired",
                     "x.pods.missing", "x.consumer.lag", "x.queue.depth",
                     "x.messages.visible", "x.requests.inflight",
                     "x.db.connections", "x.oldest.age"):
            self.assertEqual(self.kind(name, "sum").mtype, "gauge", name)

    def test_gauge_word_beats_sum_but_event_word_beats_gauge_word(self):
        # last segment "count" -> gauge even under sum()
        self.assertEqual(self.kind("q.message.count", "sum").mtype, "gauge")
        # "messages" ends the name -> counter under sum()
        self.assertEqual(self.kind("q.messages", "sum").mtype, "counter")

    def test_summary_words(self):
        for name in ("x.latency.mean", "x.latency.median", "x.upper",
                     "x.lower", "x.upper.percentiles", "x.timer.summary",
                     "x.latency.p99", "x.latency.stddev"):
            src = self.kind(name, "average")
            self.assertEqual(src.mtype, "summary", name)
            self.assertFalse(src.base.endswith("_sum"))

    def test_histogram_word_needs_quantile_agg(self):
        self.assertEqual(self.kind("x.request.duration", "percentile").mtype,
                         "histogram")
        self.assertEqual(self.kind("x.request.latency", "median").mtype,
                         "histogram")
        self.assertEqual(self.kind("x.seconds", "histogram").mtype,
                         "histogram")
        # average() of a duration-named metric is a gauge (AGG-RULES)
        self.assertEqual(self.kind("x.request.duration", "average").mtype,
                         "gauge")

    def test_agg_rules_for_unknown_names(self):
        self.assertEqual(self.kind("x.foo", "sum").mtype, "counter")
        self.assertEqual(self.kind("x.foo", "sum").base, "x_foo_total")
        self.assertEqual(self.kind("x.foo", "count").mtype, "counter")
        self.assertEqual(self.kind("x.foo", "rate").mtype, "counter")
        for agg in ("latest", "average", "max", "min"):
            self.assertEqual(self.kind("x.foo", agg).mtype, "gauge", agg)
        # percentile of an unknown: gauge (quantile_over_time), never a
        # phantom _bucket family
        self.assertEqual(self.kind("x.foo", "percentile").mtype, "gauge")
        self.assertEqual(self.kind("x.foo", "apdex").mtype, "histogram")

    def test_metric_map_wins_over_everything(self):
        cfg = load_config()
        cfg["metric_map"]["x.order.created"] = {
            "name": "orders_created_total", "type": "counter"}
        cfg["live_hints"] = {"metric_types": {"x_order_created": "gauge"}}
        src = self.kind("x.order.created", "sum", cfg)
        self.assertEqual((src.base, src.mtype, src.confidence),
                         ("orders_created_total", "counter", EXACT))
        self.assertEqual(src.kind_source, "metric_map")

    def test_metric_kinds_wins_over_hints_and_rules(self):
        cfg = load_config()
        cfg["metric_kinds"] = {"x.pods.desired": "counter"}
        cfg["live_hints"] = {"metric_types": {"x_pods_desired": "gauge"}}
        src = self.kind("x.pods.desired", "sum", cfg)
        self.assertEqual((src.base, src.mtype),
                         ("x_pods_desired_total", "counter"))
        self.assertEqual(src.kind_source, "metric_kinds")

    def test_live_hints_metric_types_win_over_rules(self):
        cfg = load_config()
        cfg["live_hints"] = {
            "metric_types": {"x_order_created": "gauge"}}
        src = self.kind("x.order.created", "sum", cfg)
        self.assertEqual((src.base, src.mtype, src.confidence),
                         ("x_order_created", "gauge", EXACT))
        self.assertEqual(src.kind_source, "live_hints")

    def test_live_hints_total_name_is_data_driven(self):
        hints = {"metric_types": {"x_foo_total": "counter"}}
        src = self.kind("x.foo", "latest", None, hints)
        self.assertEqual((src.base, src.mtype), ("x_foo_total", "counter"))

    def test_live_hints_existing_bare_name_suppresses_total(self):
        hints = {"metric_names": ["x_order_created"]}
        src = self.kind("x.order.created", "sum", None, hints)
        self.assertEqual(src.base, "x_order_created")
        self.assertEqual(src.mtype, "counter")

    def test_live_hints_histogram_family_name_normalized(self):
        hints = {"metric_types": {"x_lat": "histogram"}}
        src = self.kind("x.lat", "percentile", None, hints)
        self.assertEqual((src.base, src.mtype), ("x_lat", "histogram"))

    def test_suffix_rules(self):
        self.assertEqual(self.kind("x.reqs_total", "latest").mtype,
                         "counter")
        src = self.kind("x.lat_bucket", "latest")
        self.assertEqual((src.base, src.mtype), ("x_lat", "histogram"))

    def test_unmapped_k8s_metric_is_flagged(self):
        src = self.kind("k8s.container.fooBar", "latest")
        self.assertEqual(src.confidence, NEEDS_REVIEW)
        self.assertIn("k8s_metric_map", src.note)

    def test_note_is_specific(self):
        src = self.kind("acme_backend.order.created", "sum")
        self.assertIn("acme_backend_order_created_total", src.note)
        self.assertIn("convert --live", src.note)


class CounterSemanticsTests(unittest.TestCase):
    """SEAM-COUNTER-SEMANTICS (F2)."""

    def test_sum_counter_instant_is_increase_over_range(self):
        t = tr("SELECT sum(acme_backend.order.created) FROM Metric")
        self.assertEqual(
            t.expr,
            "sum(increase(acme_backend_order_created_total[$__range]))")
        self.assertEqual(t.query_type, "instant")
        self.assertEqual(t.metric_kind, "counter")

    def test_sum_counter_timeseries_is_increase_per_bucket(self):
        t = tr("SELECT sum(acme_backend.order.created) FROM Metric "
               "TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum(increase(acme_backend_order_created_total[$__interval]))")
        self.assertEqual(t.query_type, "range")

    def test_rate_sum_per_second_is_sum_rate(self):
        t = tr("SELECT rate(sum(acme_backend.order.created), 1 second) "
               "FROM Metric TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum(rate(acme_backend_order_created_total[$__rate_interval]))")

    def test_rate_sum_per_minute_is_scaled(self):
        t = tr("SELECT rate(sum(acme_backend.order.created), 1 minute) "
               "FROM Metric TIMESERIES")
        self.assertTrue(t.expr.endswith(") * 60"), t.expr)

    def test_count_of_counter_is_increase(self):
        t = tr("SELECT count(acme_backend.order.created) FROM Metric")
        self.assertEqual(
            t.expr,
            "sum(increase(acme_backend_order_created_total[$__range]))")

    def test_facet_becomes_by_and_limit_becomes_topk(self):
        t = tr("SELECT sum(acme_backend.order.created) FROM Metric "
               "FACET k8s.deploymentName LIMIT 5")
        self.assertEqual(
            t.expr,
            "topk(5, sum by (deployment)(increase("
            "acme_backend_order_created_total[$__range])))")
        self.assertEqual(t.group_by, ["deployment"])

    def test_latest_gauge_instant_is_last_over_time_range(self):
        t = tr("SELECT latest(acme_backend.queue.depth) FROM Metric")
        self.assertEqual(t.expr,
                         "last_over_time(acme_backend_queue_depth[$__range])")

    def test_latest_gauge_timeseries_with_facet(self):
        t = tr("SELECT latest(acme_backend.queue.depth) FROM Metric "
               "FACET queue TIMESERIES")
        self.assertEqual(
            t.expr,
            "max by (queue)(last_over_time(acme_backend_queue_depth"
            "[$__interval]))")

    def test_average_gauge_is_avg_over_time(self):
        t = tr("SELECT average(acme_backend.cpu.percent) FROM Metric "
               "TIMESERIES")
        self.assertEqual(
            t.expr,
            "avg(avg_over_time(acme_backend_cpu_percent[$__rate_interval]))")
        self.assertEqual(t.metric_kind, "gauge")

    def test_unique_count_is_count_of_count_by(self):
        t = tr("SELECT uniqueCount(k8s.podName) FROM Metric "
               "WHERE k8s.namespaceName = 'acme' TIMESERIES")
        self.assertTrue(t.expr.startswith("count(count by (pod)("), t.expr)
        self.assertIn('namespace="acme"', t.expr)

    def test_compare_with_kept_for_counter(self):
        t = tr("SELECT sum(acme_backend.events) FROM Metric "
               "COMPARE WITH 1 week ago")
        self.assertEqual(len(t.extra), 1)
        self.assertIn("offset 1w", t.extra[0].expr)

    def test_no_phantom_histogram_quantile(self):
        t = tr("SELECT percentile(acme_backend.order.created, 95) "
               "FROM Metric")
        self.assertNotIn("histogram_quantile", t.expr)
        self.assertNotIn("_bucket", t.expr)


class SummaryMetricTests(unittest.TestCase):
    """F5: NR summary metrics -> Prometheus summary _sum/_count."""

    SUMM = "acme_backend_latency_upper_percentiles"

    def test_average_is_sum_over_count(self):
        t = tr("SELECT average(acme_backend.latency.upper.percentiles) "
               "FROM Metric TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum(rate(%s_sum[$__rate_interval])) / "
            "sum(rate(%s_count[$__rate_interval]))" % (self.SUMM, self.SUMM))
        self.assertEqual(t.metric_kind, "summary")

    def test_average_median_summary_with_facet(self):
        t = tr("SELECT average(acme_backend.latency.median) FROM Metric "
               "FACET k8s.podName")
        self.assertEqual(
            t.expr,
            "sum by (pod)(rate(acme_backend_latency_median_sum[$__range]))"
            " / sum by (pod)(rate(acme_backend_latency_median_count"
            "[$__range]))")

    def test_sum_is_increase_of_sum_series(self):
        t = tr("SELECT sum(acme_backend.latency.mean) FROM Metric")
        self.assertEqual(
            t.expr,
            "sum(increase(acme_backend_latency_mean_sum[$__range]))")

    def test_count_is_increase_of_count_series(self):
        t = tr("SELECT count(acme_backend.latency.mean) FROM Metric")
        self.assertEqual(
            t.expr,
            "sum(increase(acme_backend_latency_mean_count[$__range]))")

    def test_percentile_uses_quantile_label_not_buckets(self):
        t = tr("SELECT percentile(acme_backend.latency.upper.percentiles, "
               "95) FROM Metric")
        self.assertEqual(t.expr,
                         'avg(%s{quantile="0.95"})' % self.SUMM)
        self.assertNotIn("histogram_quantile", t.expr)
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_median_summary(self):
        t = tr("SELECT median(acme_backend.latency.upper.percentiles) "
               "FROM Metric")
        self.assertIn('quantile="0.5"', t.expr)

    def test_max_summary_uses_p99_with_note(self):
        t = tr("SELECT max(acme_backend.latency.upper.percentiles) "
               "FROM Metric")
        self.assertIn('quantile="0.99"', t.expr)
        self.assertTrue(any("has no max" in n for n in t.notes), t.notes)

    def test_min_summary_untranslatable_with_closest_equivalent(self):
        q = parse_nrql("SELECT min(acme_backend.latency.upper.percentiles)"
                       " FROM Metric")
        with self.assertRaises(Untranslatable) as cm:
            translate_to_promql(q, load_config())
        self.assertIn("_sum", cm.exception.closest_equivalent[
            "example_query"])

    def test_rate_summary_uses_count(self):
        t = tr("SELECT rate(count(acme_backend.latency.mean), 1 second) "
               "FROM Metric TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum(rate(acme_backend_latency_mean_count[$__rate_interval]))")

    def test_latest_summary_is_recent_average(self):
        t = tr("SELECT latest(acme_backend.latency.mean) FROM Metric")
        self.assertIn("_sum[$__rate_interval]", t.expr)
        self.assertIn("_count[$__rate_interval]", t.expr)


class K8sMapTests(unittest.TestCase):
    """SEAM-K8S (F3)."""

    def test_cpu_requested_cores_latest_with_k8s_attrs(self):
        t = tr("SELECT latest(k8s.container.cpuRequestedCores) FROM Metric "
               "WHERE k8s.namespaceName = 'acme' AND "
               "k8s.deploymentName = 'acme-backend' FACET k8s.podName")
        self.assertEqual(
            t.expr,
            'max by (pod)(last_over_time(kube_pod_container_resource_'
            'requests{namespace="acme",deployment="acme-backend",'
            'resource="cpu"}[$__range]))')
        self.assertTrue(t.k8s_mapped)
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertIn("unit:short", t.notes)

    def test_cpu_limit_cores(self):
        t = tr("SELECT latest(k8s.container.cpuLimitCores) FROM Metric")
        self.assertIn('kube_pod_container_resource_limits{resource="cpu"}',
                      t.expr)

    def test_memory_requested_and_limit_bytes(self):
        t = tr("SELECT latest(k8s.container.memoryRequestedBytes) "
               "FROM Metric")
        self.assertIn('kube_pod_container_resource_requests{'
                      'resource="memory"}', t.expr)
        self.assertIn("unit:bytes", t.notes)
        t = tr("SELECT latest(k8s.container.memoryLimitBytes) FROM Metric")
        self.assertIn('kube_pod_container_resource_limits{'
                      'resource="memory"}', t.expr)

    def test_memory_working_set_bytes(self):
        t = tr("SELECT average(k8s.container.memoryWorkingSetBytes) "
               "FROM Metric FACET k8s.containerName TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg by (container)(avg_over_time(container_memory_working_'
            'set_bytes{container!=""}[$__rate_interval]))')

    def test_cpu_used_cores_is_rate(self):
        t = tr("SELECT average(k8s.container.cpuUsedCores) FROM Metric "
               "FACET k8s.containerName TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg by (container)(rate(container_cpu_usage_seconds_total{'
            'container!=""}[$__rate_interval]))')

    def test_cpu_cores_utilization_formula(self):
        t = tr("SELECT latest(k8s.container.cpuCoresUtilization) "
               "FROM Metric WHERE k8s.namespaceName = 'acme' "
               "FACET k8s.podName TIMESERIES")
        self.assertEqual(
            t.expr,
            'max by (pod)(100 * sum by (namespace, pod, container)(rate('
            'container_cpu_usage_seconds_total{container!="",'
            'namespace="acme"}[$__rate_interval])) / on(namespace, pod, '
            'container) max by (namespace, pod, container)(kube_pod_'
            'container_resource_limits{resource="cpu",namespace="acme"}))')
        self.assertIn("unit:percent", t.notes)

    def test_memory_utilization_formula(self):
        t = tr("SELECT average(k8s.container.memoryUtilization) "
               "FROM Metric WHERE k8s.clusterName = concat('p-', {{env}})")
        self.assertEqual(
            t.expr,
            'avg(100 * sum by (namespace, pod, container)('
            'container_memory_working_set_bytes{container!="",'
            'cluster="p-$env"}) / on(namespace, pod, container) max by ('
            'namespace, pod, container)(kube_pod_container_resource_limits'
            '{resource="memory",cluster="p-$env"}))')
        self.assertEqual(t.vars, ["env"])

    def test_deployment_pods(self):
        t = tr("SELECT latest(k8s.deployment.podsAvailable) FROM Metric")
        self.assertIn("kube_deployment_status_replicas_available", t.expr)
        t = tr("SELECT latest(k8s.deployment.podsDesired) FROM Metric")
        self.assertIn("kube_deployment_spec_replicas", t.expr)
        t = tr("SELECT sum(k8s.deployment.podsMissing) FROM Metric "
               "FACET k8s.deploymentName")
        self.assertEqual(
            t.expr,
            "sum by (deployment)(kube_deployment_spec_replicas - "
            "kube_deployment_status_replicas_available)")

    def test_pod_restarts_and_node_allocatable(self):
        t = tr("SELECT latest(k8s.pod.restartCount) FROM Metric")
        self.assertIn("kube_pod_container_status_restarts_total", t.expr)
        t = tr("SELECT latest(k8s.node.allocatableCpuCores) FROM Metric")
        self.assertIn('kube_node_status_allocatable{resource="cpu"}', t.expr)

    def test_k8s_attr_normalization(self):
        cfg = load_config()
        for attr, label in (("k8s.clusterName", "cluster"),
                            ("k8s.namespaceName", "namespace"),
                            ("k8s.deploymentName", "deployment"),
                            ("k8s.podName", "pod"),
                            ("k8s.containerName", "container"),
                            ("k8s.nodeName", "node"),
                            ("k8s.cluster.name", "cluster"),
                            ("k8s.daemonsetName", "daemonset"),
                            ("k8s.fooBarName", "foo_bar")):
            self.assertEqual(map_attr(attr, cfg), (label, True), attr)
        self.assertEqual(k8s_attr_label("clusterName", {}), "")

    def test_k8s_attrs_not_flagged_for_review(self):
        t = tr("SELECT latest(k8s.container.cpuLimitCores) FROM Metric "
               "WHERE k8s.nodeName = 'n1' FACET k8s.clusterName")
        self.assertIn('node="n1"', t.expr)
        self.assertFalse(any("not in label_map" in n for n in t.notes),
                         t.notes)

    def test_container_sample_cfs_throttling_ratio(self):
        t = tr("SELECT sum(containerCpuCfsThrottledPeriodsDelta)/"
               "sum(containerCpuCfsPeriodsDelta) FROM K8sContainerSample "
               "WHERE containerName = 'acme-backend' FACET podName "
               "TIMESERIES")
        self.assertEqual(
            t.expr,
            '(sum by (pod)(rate(container_cpu_cfs_throttled_periods_total{'
            'container="acme-backend"}[$__rate_interval]))) / (sum by (pod)'
            '(rate(container_cpu_cfs_periods_total{container="acme-backend"'
            '}[$__rate_interval])))')
        self.assertTrue(t.k8s_mapped)

    def test_container_sample_reuses_k8s_map(self):
        t = tr("SELECT latest(cpuRequestedCores) FROM K8sContainerSample "
               "WHERE namespaceName = 'acme'")
        self.assertEqual(
            t.expr,
            'last_over_time(kube_pod_container_resource_requests{'
            'namespace="acme",resource="cpu"}[$__range])')

    def test_cfg_k8s_metric_map_extends_table(self):
        cfg = load_config()
        cfg["k8s_metric_map"] = {
            "k8s.container.fooBar": {"metric": "kube_foo_bar",
                                     "kind": "gauge", "unit": "short",
                                     "labels": {"resource": "foo"}}}
        t = tr("SELECT latest(k8s.container.fooBar) FROM Metric", cfg)
        self.assertEqual(
            t.expr, 'last_over_time(kube_foo_bar{resource="foo"}[$__range])')
        self.assertTrue(t.k8s_mapped)
        self.assertIn("k8s.container.cpuRequestedCores", K8S_METRIC_MAP)

    def test_unmapped_k8s_metric_flags_review(self):
        t = tr("SELECT latest(k8s.container.fooBar) FROM Metric")
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("k8s_metric_map" in n for n in t.notes))

    def test_unmapped_sample_attribute_has_closest_equivalent(self):
        q = parse_nrql("SELECT latest(fooBar) FROM K8sContainerSample")
        with self.assertRaises(Untranslatable) as cm:
            translate_to_promql(q, load_config())
        self.assertIn("k8s_metric_map", str(cm.exception))
        self.assertEqual(cm.exception.closest_equivalent["datasource"],
                         "prometheus")

    def test_existing_infra_map_still_used_for_system_sample(self):
        t = tr("SELECT average(memoryUsedPercent) FROM SystemSample "
               "WHERE hostname = 'web.example.com'")
        self.assertIn("node_memory_MemAvailable_bytes", t.expr)
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertFalse(t.k8s_mapped)


class AppNameSuffixTests(unittest.TestCase):
    """F10: appName = 'svc (env)' -> job="svc"."""

    def cfg(self):
        cfg = load_config()
        cfg["label_map"]["appName"] = "job"
        return cfg

    def test_split_app_env(self):
        self.assertEqual(split_app_env("acme-backend (prod)"),
                         ("acme-backend", "prod"))
        self.assertEqual(split_app_env("acme-backend"),
                         ("acme-backend", ""))

    def test_equality_strips_suffix_to_job(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE appName = 'acme-backend (prod)'", self.cfg())
        self.assertIn('job="acme-backend"', t.expr)
        self.assertNotIn("(prod)", t.expr)
        self.assertTrue(any("'prod'" in n and "--env prod" in n
                            for n in t.notes), t.notes)

    def test_in_list_strips_each_value(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName IN "
               "('acme-backend (prod)', 'acme-worker (prod)')", self.cfg())
        self.assertIn('job=~"acme-backend|acme-worker"', t.expr)

    def test_or_merge_strips_each_value(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = "
               "'a (prod)' OR appName = 'b (prod)'", self.cfg())
        self.assertIn('job=~"a|b"', t.expr)

    def test_default_label_map_keeps_service_name(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE appName = 'acme-backend (staging)'")
        self.assertIn('service_name="acme-backend"', t.expr)

    def test_no_suffix_untouched(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE appName = 'acme-backend'", self.cfg())
        self.assertIn('job="acme-backend"', t.expr)
        self.assertFalse(any("suffix" in n for n in t.notes))

    def test_target_env_and_env_map_in_note(self):
        cfg = self.cfg()
        cfg["target_env"] = "p"
        cfg["env_map"] = {"prod": "p"}
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE appName = 'acme-backend (prod)'", cfg)
        self.assertTrue(any("env_map -> 'p'" in n and "pinned" in n
                            for n in t.notes), t.notes)


class EntityGuidTests(unittest.TestCase):
    """F9: entity.guid = '<GUID>' via live hints."""

    def test_resolved_entity_matches_on_name(self):
        cfg = load_config()
        cfg["live_hints"] = {"entities": {
            "ABC123": {"name": "acme-backend (prod)", "type": "APPLICATION"}}}
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE entity.guid = 'ABC123'", cfg)
        self.assertIn('service_name="acme-backend"', t.expr)
        self.assertNotEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("resolved via live" in n for n in t.notes))

    def test_entity_service_label_and_cfg_entity_label(self):
        cfg = load_config()
        cfg["entity_label"] = "job"
        cfg["live_hints"] = {"entities": {
            "ABC123": {"name": "acme-backend"}}}
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE entity.guid = 'ABC123'", cfg)
        self.assertIn('job="acme-backend"', t.expr)
        cfg["live_hints"]["entities"]["ABC123"]["service_label"] = "svc"
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE entity.guid = 'ABC123'", cfg)
        self.assertIn('svc="acme-backend"', t.expr)

    def test_unresolved_guid_dropped_with_closest_equivalent(self):
        t = tr("SELECT count(*) FROM Transaction "
               "WHERE entity.guid = 'ABC123'")
        self.assertNotIn("ABC123", t.expr)
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertEqual(t.closest_equivalent["datasource"], "prometheus")
        self.assertIn("service_name", t.closest_equivalent["example_query"])
        self.assertTrue(any("convert --live" in n for n in t.notes))

    def test_unresolved_guid_in_metric_query(self):
        t = tr("SELECT sum(acme_backend.order.created) FROM Metric "
               "WHERE entity.guid = 'ABC123' TIMESERIES")
        self.assertEqual(
            t.expr,
            "sum(increase(acme_backend_order_created_total[$__interval]))")


class ReportFieldTests(unittest.TestCase):
    """SEAM-REPORT carriers on Translation / Untranslatable."""

    def test_metric_kind_and_vars_and_k8s_flags(self):
        t = tr("SELECT sum(acme_backend.order.created) FROM Metric "
               "WHERE cluster = concat('p-', {{env}})")
        self.assertEqual(t.metric_kind, "counter")
        self.assertEqual(t.vars, ["env"])
        self.assertFalse(t.k8s_mapped)
        self.assertIsNone(t.closest_equivalent)

    def test_bare_var_recorded(self):
        t = tr("SELECT count(*) FROM Transaction WHERE appName = '{{app}}'")
        self.assertEqual(t.vars, ["app"])

    def test_finance_sample_closest_equivalent(self):
        q = parse_nrql("SELECT sum(cost) FROM FinanceSample")
        with self.assertRaises(Untranslatable) as cm:
            translate_to_promql(q, load_config())
        self.assertEqual(cm.exception.closest_equivalent["datasource"],
                         "cloudwatch")
        self.assertIn("Cost Explorer", str(cm.exception))

    def test_deployment_event_closest_equivalent(self):
        q = parse_nrql("SELECT count(*) FROM Deployment")
        with self.assertRaises(Untranslatable) as cm:
            translate_to_promql(q, load_config())
        self.assertIn("annotation", str(cm.exception))
        self.assertEqual(cm.exception.closest_equivalent["datasource"],
                         "loki")

if __name__ == "__main__":
    unittest.main()
