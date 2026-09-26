"""Tests for search-shaped FROM Span -> TraceQL translation."""

import unittest

from nr2grafana.config import load_config
from nr2grafana.translate.common import APPROXIMATE
from nr2grafana.translate.router import translate_query


def tr(nrql, cfg=None):
    return translate_query(nrql, cfg or load_config())


class TraceqlTests(unittest.TestCase):
    def test_search_shape_routes_to_tempo(self):
        t = tr("SELECT * FROM Span WHERE service.name = 'checkout'")
        self.assertEqual(t.datasource, "tempo")
        self.assertEqual(t.query_type, "traceql")
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertIn("panel-hint:traces", t.notes)

    def test_aggregated_span_query_routes_to_prometheus_instead(self):
        t = tr("SELECT count(*) FROM Span WHERE service.name = 'checkout'")
        self.assertEqual(t.datasource, "prometheus")

    def test_full_search_query(self):
        t = tr("SELECT * FROM Span WHERE service.name = 'checkout' "
               "AND duration.ms > 500 AND error IS TRUE LIMIT 50")
        self.assertEqual(
            t.expr,
            '{ resource.service.name = "checkout" && duration > 500ms '
            '&& status = error }')
        self.assertIn("limit:50", t.notes)

    def test_duration_ms_units(self):
        t = tr("SELECT * FROM Span WHERE duration.ms > 500")
        self.assertEqual(t.expr, "{ duration > 500ms }")

    def test_plain_duration_treated_as_seconds(self):
        t = tr("SELECT * FROM Span WHERE duration >= 1.5")
        self.assertEqual(t.expr, "{ duration >= 1.5s }")

    def test_error_is_true_becomes_status_error(self):
        t = tr("SELECT * FROM Span WHERE error IS TRUE")
        self.assertEqual(t.expr, "{ status = error }")

    def test_error_is_false_becomes_status_not_error(self):
        t = tr("SELECT * FROM Span WHERE error IS FALSE")
        self.assertEqual(t.expr, "{ status != error }")

    def test_like_becomes_case_insensitive_regex(self):
        t = tr("SELECT * FROM Span WHERE name LIKE '%pay%'")
        self.assertEqual(t.expr, '{ name =~ "(?i)^.*pay.*$" }')

    def test_not_like(self):
        t = tr("SELECT * FROM Span WHERE name NOT LIKE 'health%'")
        self.assertEqual(t.expr, '{ name !~ "(?i)^health.*$" }')

    def test_like_escapes_regex_metachars(self):
        t = tr("SELECT * FROM Span WHERE name LIKE '%a.b%'")
        # . escaped to \. then the backslash is doubled by string quoting
        self.assertEqual(t.expr, '{ name =~ "(?i)^.*a\\\\.b.*$" }')

    def test_in_becomes_regex_alternation(self):
        t = tr("SELECT * FROM Span WHERE http.method IN ('GET', 'POST')")
        self.assertEqual(t.expr,
                         '{ span.http.request.method =~ "^(?:GET|POST)$" }')

    def test_not_in(self):
        t = tr("SELECT * FROM Span WHERE http.method NOT IN ('DELETE')")
        self.assertEqual(t.expr, '{ span.http.request.method !~ "^(?:DELETE)$" }')

    def test_service_name_maps_to_resource_scope(self):
        t = tr("SELECT * FROM Span WHERE service.name = 'api'")
        self.assertEqual(t.expr, '{ resource.service.name = "api" }')

    def test_appname_alias_maps_to_resource_service_name(self):
        t = tr("SELECT * FROM Span WHERE appName = 'api'")
        self.assertEqual(t.expr, '{ resource.service.name = "api" }')

    def test_span_kind_unquoted(self):
        t = tr("SELECT * FROM Span WHERE span.kind = 'server'")
        self.assertEqual(t.expr, "{ kind = server }")

    def test_http_status_code_mapping(self):
        t = tr("SELECT * FROM Span WHERE http.statusCode = 500")
        self.assertEqual(t.expr,
                         '{ span.http.response.status_code = 500 }')

    def test_unknown_attribute_scope_agnostic(self):
        t = tr("SELECT * FROM Span WHERE customer.tier = 'gold'")
        self.assertEqual(t.expr, '{ .customer.tier = "gold" }')
        self.assertTrue(any("scope-agnostic" in n for n in t.notes))

    def test_or_condition(self):
        t = tr("SELECT * FROM Span WHERE service.name = 'a' "
               "OR service.name = 'b'")
        self.assertEqual(
            t.expr,
            '{ resource.service.name = "a" || resource.service.name = "b" }')

    def test_null_check(self):
        t = tr("SELECT * FROM Span WHERE http.method IS NOT NULL")
        self.assertEqual(t.expr, "{ span.http.request.method != nil }")

    def test_no_where_searches_all_traces(self):
        t = tr("SELECT * FROM Span")
        self.assertEqual(t.expr, "{ }")
        self.assertTrue(any("searches all traces" in n for n in t.notes))

    def test_nr_variable_placeholder(self):
        t = tr("SELECT * FROM Span WHERE service.name = '{{svc}}'")
        self.assertEqual(t.expr, '{ resource.service.name = "$svc" }')

    def test_facet_dropped_with_note(self):
        t = tr("SELECT * FROM Span WHERE service.name = 'x' FACET name")
        self.assertEqual(t.expr, '{ resource.service.name = "x" }')
        self.assertTrue(any("FACET has no effect" in n for n in t.notes))

    def test_compare_with_dropped_with_note(self):
        t = tr("SELECT * FROM Span WHERE service.name = 'x' "
               "COMPARE WITH 1 day ago")
        self.assertTrue(any("COMPARE WITH is not applicable" in n
                            for n in t.notes))


if __name__ == "__main__":
    unittest.main()


class StatusAndMetricsTests(unittest.TestCase):
    def test_otel_status_code_error_is_status_error(self):
        t = tr("SELECT * FROM Span WHERE otel.status_code = 'ERROR'")
        self.assertEqual(t.expr, "{ status = error }")

    def test_otel_status_code_ok(self):
        t = tr("SELECT * FROM Span WHERE otel.status_code != 'OK'")
        self.assertEqual(t.expr, "{ status != ok }")

    def test_unique_trace_count_uses_traceql_metrics_root_spans(self):
        t = tr("SELECT uniqueCount(trace.id) FROM Span WHERE service.name = "
               "'checkout' TIMESERIES")
        self.assertEqual(t.datasource, "tempo")
        self.assertEqual(t.query_type, "traceql-metrics")
        self.assertEqual(
            t.expr,
            '{ nestedSetParent < 0 && resource.service.name = "checkout" } '
            '| count_over_time()')
        self.assertIn("panel-hint:traceql-metrics", t.notes)

    def test_traceql_metrics_mode_for_aggregations(self):
        cfg = load_config()
        cfg["span_aggregations"] = "traceql"
        t = tr("SELECT percentile(duration.ms, 95) FROM Span WHERE "
               "service.name = 'c' FACET name TIMESERIES", cfg)
        self.assertEqual(
            t.expr,
            '{ resource.service.name = "c" } | quantile_over_time(duration, '
            '0.95) by (name)')
        self.assertEqual(t.legend, "{{name}}")
        t2 = tr("SELECT rate(count(*), 1 second) FROM Span WHERE "
                "service.name = 'c' TIMESERIES", cfg)
        self.assertEqual(t2.expr, '{ resource.service.name = "c" } | rate()')

    def test_span_metrics_unknown_attribute_untranslatable(self):
        t = tr("SELECT average(customAttr) FROM Span WHERE service.name = "
               "'c' TIMESERIES")
        self.assertEqual(t.confidence, "untranslatable")


class TypedAttributeTests(unittest.TestCase):
    """TraceQL is typed: int attributes compare with numbers, never with
    strings or regexes."""

    def test_status_code_string_literal_becomes_an_int(self):
        t = tr("SELECT * FROM Span WHERE http.statusCode = '500' "
               "AND service.name = 'x'")
        self.assertEqual(
            t.expr,
            '{ span.http.response.status_code = 500 && '
            'resource.service.name = "x" }')

    def test_in_list_on_int_attribute_is_an_equality_alternation(self):
        t = tr("SELECT * FROM Span WHERE http.statusCode IN (500, 502, 503)")
        self.assertEqual(
            t.expr,
            "{ span.http.response.status_code = 500 || "
            "span.http.response.status_code = 502 || "
            "span.http.response.status_code = 503 }")

    def test_not_in_list_on_int_attribute(self):
        t = tr("SELECT * FROM Span WHERE http.statusCode NOT IN ('500', '502')")
        self.assertEqual(
            t.expr,
            "{ span.http.response.status_code != 500 && "
            "span.http.response.status_code != 502 }")

    def test_status_class_like_becomes_a_band(self):
        t = tr("SELECT * FROM Span WHERE http.statusCode LIKE '5%'")
        self.assertEqual(
            t.expr,
            "{ span.http.response.status_code >= 500 && "
            "span.http.response.status_code < 600 }")

    def test_status_class_not_like(self):
        t = tr("SELECT * FROM Span WHERE http.statusCode NOT LIKE '50%'")
        self.assertEqual(
            t.expr,
            "{ span.http.response.status_code < 500 || "
            "span.http.response.status_code >= 510 }")

    def test_like_on_another_int_attribute_is_dropped_with_a_note(self):
        t = tr("SELECT * FROM Span WHERE net.peer.port LIKE '8%'")
        self.assertEqual(t.expr, "{ }")
        self.assertTrue(any("integer attribute" in n for n in t.notes))

    def test_string_attribute_in_list_stays_an_anchored_regex(self):
        t = tr("SELECT * FROM Span WHERE http.method IN ('GET', 'POST')")
        self.assertEqual(t.expr,
                         '{ span.http.request.method =~ "^(?:GET|POST)$" }')

    def test_variable_field_mapping(self):
        from nr2grafana.translate.traces import variable_field
        self.assertEqual(variable_field("service.name"),
                         "resource.service.name")
        self.assertEqual(variable_field("http.statusCode"),
                         "span.http.response.status_code")
        self.assertEqual(variable_field("custom.attr"), ".custom.attr")


class Iteration3TraceTests(unittest.TestCase):
    def test_embedded_variables(self):
        t = tr("SELECT * FROM Span WHERE service.name = '{{svc}}' "
               "AND name LIKE '%{{op}}%' LIMIT 20")
        self.assertEqual(
            t.expr,
            '{ resource.service.name = "$svc" && name =~ "(?i)^.*${op:regex}.*$" }')
        t = tr("SELECT * FROM Span WHERE service.name = 'prod-{{svc}}'")
        self.assertEqual(t.expr, '{ resource.service.name = "prod-$svc" }')

    def test_root_span_predicates(self):
        t = tr("SELECT * FROM Span WHERE service.name = 'checkout' "
               "AND parentId IS NULL")
        self.assertEqual(
            t.expr, '{ resource.service.name = "checkout" && nestedSetParent < 0 }')
        t = tr("SELECT * FROM Span WHERE parentId IS NOT NULL")
        self.assertEqual(t.expr, "{ nestedSetParent >= 0 }")
        t = tr("SELECT * FROM Span WHERE nr.entryPoint IS TRUE AND duration > 2")
        self.assertEqual(t.expr, "{ nestedSetParent < 0 && duration > 2s }")

    def test_distributed_trace_summary_uses_root_spans(self):
        t = tr("SELECT count(*) FROM DistributedTraceSummary "
               "WHERE root.entity.name = 'checkout' TIMESERIES")
        self.assertEqual(t.datasource, "tempo")
        self.assertEqual(t.query_type, "traceql-metrics")
        self.assertEqual(
            t.expr,
            '{ nestedSetParent < 0 && resource.service.name = "checkout" } '
            '| count_over_time()')
        t = tr("SELECT average(duration.ms) FROM DistributedTraceSummary "
               "WHERE root.entity.name = 'checkout' TIMESERIES")
        self.assertTrue(t.expr.endswith("| avg_over_time(duration)"))
        self.assertTrue(any("Tempo 2.6+" in n for n in t.notes))


class Iteration11TraceTests(unittest.TestCase):
    def test_error_count_becomes_a_trace_level_condition(self):
        from nr2grafana.config import load_config
        from nr2grafana.translate.router import translate_query
        cfg = load_config()
        t = translate_query("SELECT count(*) FROM DistributedTraceSummary "
                            "WHERE root.entity.name = 'checkout' AND "
                            "errorCount > 0 TIMESERIES", cfg)
        self.assertEqual(
            t.expr,
            '{ nestedSetParent < 0 && resource.service.name = "checkout" } '
            '&& { status = error } | count_over_time()')
        self.assertEqual(t.query_type, "traceql-metrics")
        t = translate_query("SELECT * FROM DistributedTraceSummary WHERE "
                            "root.entity.name = 'checkout' AND errorCount >= 1 "
                            "LIMIT 20", cfg)
        self.assertEqual(
            t.expr,
            '{ resource.service.name = "checkout" } && { status = error }')
        t = translate_query("SELECT count(*) FROM DistributedTraceSummary "
                            "WHERE errorCount = 0 TIMESERIES", cfg)
        self.assertEqual(t.expr, "{ nestedSetParent < 0 } | count_over_time()")
        self.assertTrue(any("errorCount = 0 cannot be expressed" in n
                            for n in t.notes))


class Iteration12TraceTests(unittest.TestCase):
    """TraceQL shapes the randomised sweep found Tempo 2.7 rejects."""

    def setUp(self):
        self.cfg = load_config()
        self.cfg["span_aggregations"] = "traceql"

    def test_variable_facets_are_dropped_with_a_note(self):
        t = tr("SELECT count(*) FROM DistributedTraceSummary FACET {{f}}",
               self.cfg)
        self.assertEqual(t.expr, "{ nestedSetParent < 0 } | count_over_time()")
        self.assertTrue(any("FACET {{f}}: a dashboard variable cannot name"
                            in n for n in t.notes))

    def test_sum_over_time_needs_tempo_28(self):
        from nr2grafana.translate.common import NEEDS_REVIEW as review
        t = tr("SELECT sum(duration) FROM DistributedTraceSummary TIMESERIES",
               self.cfg)
        self.assertEqual(t.expr,
                         "{ nestedSetParent < 0 } | sum_over_time(duration)")
        self.assertEqual(t.confidence, review)
        self.assertIn("sum_over_time() needs Tempo 2.8+ (2.7 rejects it)",
                      t.notes)

    def test_facets_resolve_fields_like_where(self):
        t = tr("SELECT count(*) FROM DistributedTraceSummary WHERE "
               "root.entity.name IS NULL FACET root.entity.name", self.cfg)
        self.assertEqual(t.expr, "{ nestedSetParent < 0 && resource.service."
                                 "name = nil } | count_over_time() by "
                                 "(resource.service.name)")
        self.assertTrue(any("Tempo 2.7 rejects it" in n for n in t.notes))

    def test_is_not_null_has_no_caveat(self):
        t = tr("SELECT count(*) FROM Span WHERE service.name = 'a' AND "
               "http.url IS NOT NULL FACET name TIMESERIES", self.cfg)
        self.assertEqual(t.expr, '{ resource.service.name = "a" && .http.url '
                                 '!= nil } | count_over_time() by (name)')
        self.assertFalse(any("= nil" in n for n in t.notes))


class Iteration13TraceTests(unittest.TestCase):
    """OR precedence under the root filter, typed literals, wrappers."""

    def setUp(self):
        self.cfg = load_config()
        self.cfg["span_aggregations"] = "traceql"

    def test_or_keeps_its_parentheses_under_the_root_filter(self):
        t = tr("SELECT count(*) FROM DistributedTraceSummary WHERE "
               "trace.id = 500 OR trace.id = 503", self.cfg)
        self.assertEqual(t.expr, '{ nestedSetParent < 0 && (trace:id = "500" '
                                 '|| trace:id = "503") } | count_over_time()')
        t = tr("SELECT * FROM Span WHERE (name = 'a' OR name = 'b') AND "
               "service.name = 'x'", self.cfg)
        self.assertEqual(t.expr, '{ (name = "a" || name = "b") && '
                                 'resource.service.name = "x" }')

    def test_trace_level_counts_are_dropped_with_a_note(self):
        t = tr("SELECT average(numeric(duration)) FROM DistributedTraceSummary "
               "WHERE spanCount > 5", self.cfg)
        self.assertEqual(t.expr,
                         "{ nestedSetParent < 0 } | avg_over_time(duration)")
        self.assertTrue(any("span/entity count has no TraceQL field" in n
                            for n in t.notes))

    def test_math_wrappers_are_dropped_and_root_span_name_maps(self):
        t = tr("SELECT round(percentile(duration, 99), 1) FROM "
               "DistributedTraceSummary", self.cfg)
        self.assertEqual(t.expr, "{ nestedSetParent < 0 } | "
                                 "quantile_over_time(duration, 0.99)")
        self.assertTrue(any(n.startswith("round() dropped") for n in t.notes))
        t = tr("SELECT count(*) FROM DistributedTraceSummary WHERE "
               "root.span.name = 'GET /x'", self.cfg)
        self.assertEqual(t.expr, '{ nestedSetParent < 0 && name = "GET /x" } '
                                 '| count_over_time()')

    def test_arithmetic_is_refused_with_a_plain_reason(self):
        t = tr("SELECT count(*) - filter(count(*), WHERE trace.id = 'a') "
               "FROM DistributedTraceSummary", self.cfg)
        self.assertTrue(any(n.startswith("arithmetic between aggregations has "
                                         "no TraceQL metrics equivalent")
                            for n in t.notes), t.notes)


class Iteration17KindTests(unittest.TestCase):
    def test_unknown_span_kind_values_are_dropped_with_a_note(self):
        t = tr("SELECT * FROM Span WHERE service.name = 'a' AND span.kind = 'b'")
        self.assertEqual(t.expr, '{ resource.service.name = "a" }')
        self.assertTrue(any("is not a span kind" in n for n in t.notes))
        t = tr("SELECT * FROM Span WHERE span.kind = 'server'")
        self.assertEqual(t.expr, "{ kind = server }")
