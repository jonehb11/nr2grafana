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


if __name__ == "__main__":
    unittest.main()
