"""Tests for NRQL (FROM Log) -> LogQL translation."""

import unittest

from nr2grafana.config import load_config
from nr2grafana.translate.common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW,
)
from nr2grafana.translate.router import translate_query


def tr(nrql, cfg=None):
    return translate_query(nrql, cfg or load_config())


class MatcherSplitTests(unittest.TestCase):
    def test_stream_labels_vs_line_filter(self):
        # service_name and level are configured stream labels; message
        # predicates become line filters.
        t = tr("SELECT * FROM Log WHERE service.name = 'checkout' "
               "AND level = 'error' AND message LIKE '%payment%' LIMIT 100")
        self.assertEqual(
            t.expr,
            '{service_name="checkout", level="error"} |~ "(?i).*payment.*"')
        self.assertEqual(t.datasource, "loki")
        self.assertEqual(t.confidence, EXACT)

    def test_pipeline_filter_for_non_stream_attribute(self):
        t = tr("SELECT count(*) FROM Log WHERE requestPath = '/pay'")
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name=~".+"} | json '
            '| requestPath="/pay" | __error__="" [$__range]))')
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        # both the all-streams scan and the parsed-field assumption are noted
        self.assertTrue(any("no stream-label filter" in n for n in t.notes))
        self.assertTrue(any("requestPath" in n for n in t.notes))

    def test_message_equality_becomes_substring_filter(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND message = 'boom'")
        self.assertEqual(t.expr, '{service_name="x"} |= "boom"')
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_message_not_like(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND message NOT LIKE '%debug%'")
        self.assertEqual(t.expr, '{service_name="x"} !~ "(?i).*debug.*"')


class LogsPanelTests(unittest.TestCase):
    def test_select_star_hint_and_maxlines(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'checkout' LIMIT 100")
        self.assertEqual(t.expr, '{service_name="checkout"}')
        self.assertEqual(t.query_type, "range")
        self.assertIn("panel-hint:logs", t.notes)
        self.assertIn("maxlines:100", t.notes)

    def test_column_projection_noted(self):
        t = tr("SELECT message, level FROM Log WHERE service.name = 'x' "
               "LIMIT 20")
        self.assertEqual(t.expr, '{service_name="x"}')
        self.assertIn("maxlines:20", t.notes)
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertTrue(any("column projection (message, level)" in n
                            for n in t.notes))


class AggregationTests(unittest.TestCase):
    def test_count_with_facet_timeseries(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'checkout' "
               "FACET level TIMESERIES SINCE 6 hours ago")
        self.assertEqual(
            t.expr,
            'sum by (level)(count_over_time({service_name="checkout"} '
            '[$__auto]))')
        self.assertEqual(t.query_type, "range")
        self.assertEqual(t.legend, "{{level}}")
        self.assertEqual(t.group_by, ["level"])
        self.assertIn("timefrom:now-6h", t.notes)

    def test_count_instant_uses_range_window(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x'")
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x"} [$__range]))')
        self.assertEqual(t.query_type, "instant")

    def test_rate_per_minute(self):
        t = tr("SELECT rate(count(*), 1 minute) FROM Log "
               "WHERE service.name = 'checkout' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(rate({service_name="checkout"} [$__auto])) * 60')

    def test_rate_per_second_no_multiplier(self):
        t = tr("SELECT rate(count(*), 1 second) FROM Log "
               "WHERE service.name = 'x' TIMESERIES")
        self.assertEqual(t.expr,
                         'sum(rate({service_name="x"} [$__auto]))')

    def test_average_unwrap(self):
        t = tr("SELECT average(duration) FROM Log "
               "WHERE service.name = 'checkout' TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg_over_time({service_name="checkout"} | json '
            '| unwrap duration | __error__="" [$__auto]) by ()')
        self.assertEqual(t.confidence, APPROXIMATE)

    def test_max_unwrap_with_facet(self):
        t = tr("SELECT max(duration) FROM Log WHERE service.name = 'x' "
               "FACET level TIMESERIES")
        self.assertEqual(
            t.expr,
            'max_over_time({service_name="x"} | json '
            '| unwrap duration | __error__="" [$__auto]) by (level)')

    def test_percentile_unwrap(self):
        t = tr("SELECT percentile(duration, 95) FROM Log "
               "WHERE service.name = 'checkout'")
        self.assertEqual(
            t.expr,
            'quantile_over_time(0.95, {service_name="checkout"} | json '
            '| unwrap duration | __error__="" [$__range]) by ()')
        self.assertEqual(t.legend, "p95")
        self.assertEqual(t.query_type, "instant")

    def test_percentile_multiple_extra_targets(self):
        t = tr("SELECT percentile(duration, 50, 99) FROM Log "
               "WHERE service.name = 'x' TIMESERIES")
        self.assertIn("quantile_over_time(0.5,", t.expr)
        self.assertEqual(len(t.extra), 1)
        self.assertIn("quantile_over_time(0.99,", t.extra[0].expr)
        self.assertEqual(t.extra[0].legend, "p99")

    def test_uniquecount(self):
        t = tr("SELECT uniqueCount(user.id) FROM Log "
               "WHERE service.name = 'checkout'")
        self.assertEqual(
            t.expr,
            'count(sum by (user_id)(count_over_time({'
            'service_name="checkout"} | json | __error__="" [$__range])))')
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_facet_on_non_stream_label_forces_parser(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "FACET requestPath TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum by (requestPath)(count_over_time({service_name="x"} '
            '| json | __error__="" [$__auto]))')
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_facet_limit_topk(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "FACET level LIMIT 5")
        self.assertEqual(
            t.expr,
            'topk(5, sum by (level)(count_over_time({service_name="x"} '
            '[$__range])))')

    def test_average_needs_attribute(self):
        # average(*) has no numeric attribute -> untranslatable
        t = tr("SELECT average(*) FROM Log WHERE service.name = 'x'")
        self.assertEqual(t.confidence, "untranslatable")

    def test_compare_with_becomes_offset_target(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "TIMESERIES COMPARE WITH 1 day ago")
        self.assertEqual(
            t.expr, 'sum(count_over_time({service_name="x"} [$__auto]))')
        self.assertEqual(len(t.extra), 1)
        self.assertEqual(
            t.extra[0].expr,
            'sum(count_over_time({service_name="x"} [$__auto] offset 1d))')
        self.assertEqual(t.extra[0].legend, "(1d earlier)")
        # flagged for review: LogQL offsets need Loki 2.3+
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("COMPARE WITH" in n for n in t.notes))

    def test_compare_with_on_stream_panel_dropped_with_note(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "COMPARE WITH 1 day ago")
        self.assertEqual(t.expr, '{service_name="x"}')
        self.assertEqual(t.extra, [])
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("COMPARE WITH" in n for n in t.notes))


class FilterIfTests(unittest.TestCase):
    def test_filter_count_merges_stream_label_cleanly(self):
        # The embedded WHERE supplies the stream selector; no spurious
        # "no stream-label filter" scan-all warning may fire.
        t = tr("SELECT filter(count(*), WHERE service.name = 'x') FROM Log")
        self.assertEqual(
            t.expr, 'sum(count_over_time({service_name="x"} [$__range]))')
        self.assertEqual(t.confidence, EXACT)
        self.assertFalse(any("no stream-label filter" in n for n in t.notes))

    def test_filter_combines_outer_and_embedded_where(self):
        t = tr("SELECT filter(count(*), WHERE level = 'error') FROM Log "
               "WHERE service.name = 'x' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x", level="error"} '
            '[$__auto]))')

    def test_filter_supports_unwrap_aggregations(self):
        t = tr("SELECT filter(average(duration), WHERE level = 'error') "
               "FROM Log WHERE service.name = 'x' TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg_over_time({service_name="x", level="error"} | json '
            '| unwrap duration | __error__="" [$__auto]) by ()')

    def test_count_if_becomes_filtered_count(self):
        t = tr("SELECT count(if(level = 'error', 1)) FROM Log "
               "WHERE service.name = 'x' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x", level="error"} '
            '[$__auto]))')
        self.assertTrue(any("filtered aggregation" in n for n in t.notes))

    def test_sum_if_one_zero_becomes_filtered_count(self):
        t = tr("SELECT sum(if(level = 'error', 1, 0)) FROM Log "
               "WHERE service.name = 'x'")
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x", level="error"} '
            '[$__range]))')

    def test_if_with_nontrivial_else_untranslatable(self):
        t = tr("SELECT average(if(level = 'error', duration, 5)) FROM Log "
               "WHERE service.name = 'x'")
        self.assertEqual(t.confidence, "untranslatable")
        self.assertTrue(any("ELSE value" in n for n in t.notes))

    def test_earliest_becomes_first_over_time(self):
        t = tr("SELECT earliest(duration) FROM Log "
               "WHERE service.name = 'x'")
        self.assertEqual(
            t.expr,
            'first_over_time({service_name="x"} | json '
            '| unwrap duration | __error__="" [$__range]) by ()')

    def test_facet_without_limit_cardinality_note(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "FACET level TIMESERIES")
        self.assertTrue(any("top 10 groups" in n for n in t.notes))


if __name__ == "__main__":
    unittest.main()
