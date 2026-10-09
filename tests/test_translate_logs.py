"""Tests for NRQL (FROM Log) -> LogQL translation."""

import unittest
from unittest import mock

from nr2grafana.config import load_config
from nr2grafana.nrql.parser import (
    Attr, BoolOp, Cmp, Func, InList, Lit, NotOp, NrqlParseError, NrqlQuery,
    SelectItem, Star, TimeseriesSpec, parse_nrql,
)
from nr2grafana.translate import logs
from nr2grafana.translate.common import (
    APPROXIMATE, EXACT, NEEDS_REVIEW, UNTRANSLATABLE,
)
from nr2grafana.translate.logs import translate_to_logql
from nr2grafana.translate.router import translate_query


def tr(nrql, cfg=None):
    return translate_query(nrql, cfg or load_config())


def cfg_with(**overrides):
    cfg = load_config()
    cfg.update(overrides)
    return cfg


def count_query(where, timeseries=False, facet=None):
    """Build a `SELECT count(*) FROM Log WHERE <where>` AST directly (for
    WHERE shapes the parser may not accept yet)."""
    return NrqlQuery(
        raw="<ast>", select=[SelectItem(expr=Func("count", args=[Star()]))],
        from_=["Log"], where=where, facet=facet or [],
        timeseries=TimeseriesSpec() if timeseries else None)


SVC = Cmp(Attr("service.name"), "=", Lit("x"))


class MatcherSplitTests(unittest.TestCase):
    def test_stream_labels_vs_line_filter(self):
        # service_name and level are configured stream labels; message
        # predicates become line filters. Level matchers are case-
        # insensitive by default (loki_case_insensitive_levels).
        t = tr("SELECT * FROM Log WHERE service.name = 'checkout' "
               "AND level = 'error' AND message LIKE '%payment%' LIMIT 100")
        self.assertEqual(
            t.expr,
            '{service_name="checkout", level=~"(?i)error"} '
            '|~ "(?i).*payment.*"')
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


class LevelCaseTests(unittest.TestCase):
    """F7: log_level='ERROR' -> log_level=~"(?i)ERROR" by default."""

    def test_parsed_log_level_is_case_insensitive(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "AND log_level = 'ERROR' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x"} | json '
            '| log_level=~"(?i)ERROR" | __error__="" [$__auto]))')
        self.assertTrue(any("case-insensitive" in n for n in t.notes))

    def test_not_equal_level(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND level != 'debug'")
        self.assertEqual(t.expr,
                         '{service_name="x", level!~"(?i)debug"}')

    def test_in_list_gets_single_flag(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND level IN ('error', 'warn')")
        self.assertEqual(t.expr,
                         '{service_name="x", level=~"(?i)error|warn"}')

    def test_metadata_detected_level(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND detected_level = 'Error'")
        self.assertEqual(
            t.expr, '{service_name="x"} | detected_level=~"(?i)Error"')

    def test_variable_values_untouched(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND level = {{lvl}}")
        self.assertEqual(t.expr,
                         '{service_name="x", level=~"${lvl:regex}"}')

    def test_config_opt_out_keeps_exact_match(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND level = 'error'",
               cfg_with(loki_case_insensitive_levels=False))
        self.assertEqual(t.expr, '{service_name="x", level="error"}')
        self.assertFalse(any("case-insensitive" in n for n in t.notes))

    def test_only_level_like_labels_are_relaxed(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND namespace = 'Prod'")
        self.assertEqual(t.expr,
                         '{service_name="x", namespace="Prod"}')


class RenderSeamTests(unittest.TestCase):
    """F1: concat()/{{var}} in label values render as text, never reprs."""

    def assertNoRepr(self, expr):
        for marker in ("Func(", "Lit(", "Attr("):
            self.assertNotIn(marker, expr)

    def test_concat_in_stream_label(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "AND cluster = concat('p-', {{env}}) TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x", cluster="p-$env"} '
            '[$__auto]))')
        self.assertNoRepr(t.expr)

    def test_concat_with_var_rename(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND cluster = concat('p-', {{datasource}})",
               cfg_with(var_renames={"datasource": "nr_datasource"}))
        self.assertEqual(
            t.expr, '{service_name="x", cluster="p-$nr_datasource"}')

    def test_concat_in_pipeline_filter(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND region = concat({{env}}, '-east')")
        self.assertEqual(
            t.expr,
            '{service_name="x"} | json | region="$env-east" | __error__=""')
        self.assertNoRepr(t.expr)

    def test_in_list_with_concat_becomes_alternation(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' AND cluster IN "
               "(concat('p-', {{env}}), 'q-stage')")
        self.assertEqual(
            t.expr, '{service_name="x", cluster=~"p-$env|q-stage"}')

    def test_not_in_list_with_vars(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' AND cluster "
               "NOT IN ({{a}}, {{b}})")
        self.assertEqual(t.expr, '{service_name="x", cluster!~"$a|$b"}')

    def test_in_list_literals_still_escaped(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' AND cluster IN "
               "(concat('p-', {{env}}), 'q.stage')")
        self.assertEqual(
            t.expr, '{service_name="x", cluster=~"p-$env|q\\\\.stage"}')

    def test_like_with_concat_keeps_var(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' AND cluster "
               "LIKE concat('p-', {{env}}, '%')")
        self.assertEqual(
            t.expr, '{service_name="x", cluster=~"(?i)p-$env.*"}')

    def test_literal_only_concat(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'x' "
               "AND cluster = concat('acme-cluster-', 'prod')")
        self.assertEqual(
            t.expr, '{service_name="x", cluster="acme-cluster-prod"}')
        self.assertEqual(t.confidence, EXACT)

    def test_fallback_renderer_when_seam_missing(self):
        with mock.patch.object(logs, "_render_value", None):
            t = translate_to_logql(count_query(BoolOp("and", [
                SVC, Cmp(Attr("cluster"), "=",
                         Func("concat", args=[Lit("p-"), Attr("{{env}}")]))
            ])), load_config())
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x", cluster="p-$env"} '
            '[$__range]))')

    def test_fallback_renderer_kinds(self):
        cfg = load_config()
        self.assertEqual(logs._fallback_render_value(Lit("p-"), cfg),
                         ("p-", "literal"))
        self.assertEqual(logs._fallback_render_value(Attr("{{env}}"), cfg),
                         ("$env", "var"))
        self.assertEqual(
            logs._fallback_render_value(
                Func("concat", args=[Lit("p-"), Attr("{{env}}")]), cfg),
            ("p-$env", "mixed"))

    def test_seam_renderer_is_used_when_present(self):
        def fake(node, cfg):
            return "rendered", "mixed"
        with mock.patch.object(logs, "_render_value", fake):
            t = translate_to_logql(count_query(BoolOp("and", [
                SVC, Cmp(Attr("cluster"), "=",
                         Func("concat", args=[Lit("p-"), Attr("{{env}}")]))
            ])), load_config())
        self.assertIn('cluster="rendered"', t.expr)

    def test_unknown_function_value_is_noted_not_reprd(self):
        nrql = ("SELECT * FROM Log WHERE service.name = 'x' "
                "AND cluster = concat('p-', lower({{env}}))")
        # whichever renderer is active, the emitted query carries no repr
        t = tr(nrql)
        self.assertNoRepr(t.expr)
        self.assertNoRepr(" ".join(t.notes))
        # the local fallback cannot render lower(): flagged for review
        with mock.patch.object(logs, "_render_value", None):
            t = tr(nrql)
        self.assertNoRepr(t.expr)
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("lower" in n for n in t.notes))


class AllColumnSearchTests(unittest.TestCase):
    """F7: allColumnSearch('t', insensitive: true) -> |~ "(?i)t"."""

    def search(self, *args):
        return Func("allcolumnsearch", args=list(args))

    def test_insensitive_search_as_bare_predicate(self):
        t = translate_to_logql(count_query(BoolOp("and", [
            SVC, self.search(Lit("timeout"), Lit("insensitive:True"))
        ]), timeseries=True), load_config())
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x"} |~ "(?i)timeout" '
            '[$__auto]))')
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertTrue(any("allColumnSearch" in n for n in t.notes))

    def test_insensitive_search_as_boolean_comparison(self):
        # The `fn = true` shape the parser uses for bare boolean predicates.
        t = translate_to_logql(count_query(BoolOp("and", [
            SVC, Cmp(self.search(Lit("Timeout"), Lit(True)), "=", Lit(True))
        ])), load_config())
        self.assertIn('|~ "(?i)Timeout"', t.expr)

    def test_case_sensitive_search_is_substring_filter(self):
        t = translate_to_logql(count_query(BoolOp("and", [
            SVC, self.search(Lit("OutOfMemory"))
        ])), load_config())
        self.assertIn('{service_name="x"} |= "OutOfMemory"', t.expr)

    def test_search_text_is_regex_escaped(self):
        t = translate_to_logql(count_query(BoolOp("and", [
            SVC, self.search(Lit("a.b (c)"), Lit("insensitive: true"))
        ])), load_config())
        self.assertIn('|~ "(?i)a\\\\.b \\\\(c\\\\)"', t.expr)

    def test_negated_search(self):
        t = translate_to_logql(count_query(BoolOp("and", [
            SVC, NotOp(self.search(Lit("debug"), Lit("insensitive:True")))
        ])), load_config())
        self.assertIn('{service_name="x"} !~ "(?i)debug"', t.expr)
        t = translate_to_logql(count_query(BoolOp("and", [
            SVC, Cmp(self.search(Lit("debug")), "=", Lit(False))
        ])), load_config())
        self.assertIn('{service_name="x"} != "debug"', t.expr)

    def test_search_only_where_scans_all_streams(self):
        t = translate_to_logql(
            count_query(self.search(Lit("t"), Lit("insensitive:True"))),
            load_config())
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name=~".+"} |~ "(?i)t" '
            '[$__range]))')
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_search_inside_or_is_dropped_with_note(self):
        t = translate_to_logql(count_query(BoolOp("and", [
            SVC, BoolOp("or", [self.search(Lit("a")),
                               Cmp(Attr("level"), "=", Lit("error"))])
        ])), load_config())
        self.assertEqual(
            t.expr, 'sum(count_over_time({service_name="x"} [$__range]))')
        self.assertEqual(t.confidence, NEEDS_REVIEW)
        self.assertTrue(any("OR clause" in n for n in t.notes))

    def test_search_inside_filter_aggregation(self):
        nq = NrqlQuery(
            raw="<ast>", from_=["Log"], where=SVC,
            select=[SelectItem(expr=Func(
                "filter", args=[Func("count", args=[Star()])],
                where=self.search(Lit("boom"), Lit("insensitive:True"))))])
        t = translate_to_logql(nq, load_config())
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x"} |~ "(?i)boom" '
            '[$__range]))')

    def test_search_without_text_is_dropped(self):
        t = translate_to_logql(count_query(BoolOp("and", [
            SVC, self.search()
        ])), load_config())
        self.assertEqual(
            t.expr, 'sum(count_over_time({service_name="x"} [$__range]))')
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_end_to_end_through_parser(self):
        nrql = ("SELECT count(*) FROM Log WHERE service.name = 'x' AND "
                "allColumnSearch('timeout', insensitive: true) TIMESERIES")
        try:
            parse_nrql(nrql)
        except NrqlParseError:
            self.skipTest("parser support for bare allColumnSearch() "
                          "predicates is owned by the parser module")
        t = tr(nrql)
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x"} |~ "(?i)timeout" '
            '[$__auto]))')


class LogsPanelTests(unittest.TestCase):
    def test_select_star_hint_and_maxlines(self):
        t = tr("SELECT * FROM Log WHERE service.name = 'checkout' LIMIT 100")
        self.assertEqual(t.expr, '{service_name="checkout"}')
        self.assertEqual(t.query_type, "range")
        self.assertIn("panel-hint:logs", t.notes)
        self.assertIn("maxlines:100", t.notes)

    def test_column_projection_uses_line_format(self):
        t = tr("SELECT message, level FROM Log WHERE service.name = 'x' "
               "LIMIT 20")
        self.assertEqual(
            t.expr,
            '{service_name="x"} | json | line_format '
            '"{{.message}} {{.level}}"')
        self.assertIn("maxlines:20", t.notes)
        self.assertIn("panel-hint:logs", t.notes)
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertTrue(any("column projection (message, level)" in n
                            for n in t.notes))

    def test_projection_drops_timestamp_and_flattens_dotted_fields(self):
        t = tr("SELECT timestamp, user.id, message FROM Log "
               "WHERE service.name = 'x'")
        self.assertEqual(
            t.expr,
            '{service_name="x"} | json | line_format '
            '"{{.user_id}} {{.message}}"')
        self.assertTrue(any("timestamp column is omitted" in n
                            for n in t.notes))

    def test_projection_after_parsed_filter_keeps_error_guard(self):
        t = tr("SELECT message FROM Log WHERE service.name = 'x' "
               "AND requestPath = '/pay'")
        self.assertEqual(
            t.expr,
            '{service_name="x"} | json | requestPath="/pay" | __error__="" '
            '| line_format "{{.message}}"')

    def test_projection_with_parser_disabled_falls_back(self):
        t = tr("SELECT message FROM Log WHERE service.name = 'x'",
               cfg_with(loki_parser=""))
        self.assertEqual(t.expr, '{service_name="x"}')
        self.assertTrue(any("full log line is shown" in n for n in t.notes))

    def test_timestamp_only_projection_is_plain_stream(self):
        t = tr("SELECT timestamp FROM Log WHERE service.name = 'x'")
        self.assertEqual(t.expr, '{service_name="x"}')


class FacetParseTests(unittest.TestCase):
    """F7: FACET aparse()/capture() -> | regexp "(?P<name>...)" + by."""

    def test_aparse_on_message(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "FACET aparse(message, '%[TOPIC:*]%') AS topic TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum by (topic)(count_over_time({service_name="x"} '
            '| regexp `.*\\[TOPIC:(?P<topic>.*)\\].*` [$__auto]))')
        self.assertEqual(t.legend, "{{topic}}")
        self.assertEqual(t.group_by, ["topic"])
        self.assertEqual(t.confidence, APPROXIMATE)
        # no parser stage is needed: regexp runs on the raw line
        self.assertNotIn("| json", t.expr)

    def test_aparse_default_name_and_multiple_captures(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "FACET aparse(message, 'method=* path=*')")
        self.assertIn('| regexp "method=(?P<aparse>.*) path='
                      '(?P<aparse_2>.*)"', t.expr)
        self.assertEqual(t.group_by, ["aparse", "aparse_2"])

    def test_aparse_without_capture_wraps_whole_pattern(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "FACET aparse(message, 'code=%') AS code")
        self.assertIn('| regexp "(?P<code>code=.*)"', t.expr)

    def test_aparse_on_parsed_field_uses_line_format(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "FACET aparse(requestPath, '/api/*/%') AS api")
        self.assertEqual(
            t.expr,
            'sum by (api)(count_over_time({service_name="x"} | json '
            '| __error__="" | line_format "{{.requestPath}}" '
            '| regexp "/api/(?P<api>.*)/.*" [$__range]))')
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_capture_with_alias_renames_group(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' FACET "
               "capture(message, r'order=(?P<oid>\\d+)') AS order_id")
        self.assertEqual(
            t.expr,
            'sum by (order_id)(count_over_time({service_name="x"} '
            '| regexp `order=(?P<order_id>\\d+)` [$__range]))')
        self.assertEqual(t.legend, "{{order_id}}")

    def test_capture_keeps_named_groups_without_alias(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' FACET "
               "capture(message, r'(?P<method>GET|POST) (?P<path>\\S+)')")
        self.assertEqual(t.group_by, ["method", "path"])
        self.assertIn("| regexp `(?P<method>GET|POST) (?P<path>\\S+)`",
                      t.expr)

    def test_capture_without_group_wraps_pattern(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' FACET "
               "capture(message, 'id=[0-9]+') AS ident")
        self.assertIn('| regexp "(?P<ident>id=[0-9]+)"', t.expr)

    def test_capture_legacy_raw_prefix_token(self):
        # Older parser shape: the r'...' prefix came through as Attr('r').
        nq = count_query(SVC, facet=[logs_facet(
            Func("capture", args=[Attr("message"), Attr("r"),
                                  Lit("x=(?P<x>\\d+)")]))])
        t = translate_to_logql(nq, load_config())
        self.assertIn("| regexp `x=(?P<x>\\d+)`", t.expr)
        self.assertEqual(t.group_by, ["x"])

    def test_capture_needs_pattern(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' FACET "
               "capture(message)")
        self.assertEqual(t.confidence, "untranslatable")
        self.assertTrue(any("string pattern" in n for n in t.notes))

    def test_regex_facet_with_unwrap_and_topk(self):
        t = tr("SELECT average(duration) FROM Log WHERE service.name = 'x' "
               "FACET aparse(message, '[TOPIC:*]') AS topic LIMIT 5")
        self.assertEqual(
            t.expr,
            'topk(5, avg_over_time({service_name="x"} | json '
            '| regexp `\\[TOPIC:(?P<topic>.*)\\]` | unwrap duration '
            '| __error__="" [$__range]) by (topic))')

    def test_regex_facet_mixed_with_attribute_facet(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "FACET level, aparse(message, 'op=*') AS op")
        self.assertEqual(t.group_by, ["level", "op"])
        self.assertIn("sum by (level, op)(", t.expr)
        self.assertNotIn("| json", t.expr)

    def test_other_facet_function_still_dropped(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "FACET buckets(duration, 10, 5)")
        self.assertEqual(t.group_by, [])
        self.assertEqual(t.confidence, NEEDS_REVIEW)


def logs_facet(expr, alias=None):
    from nr2grafana.nrql.parser import FacetItem
    return FacetItem(expr=expr, alias=alias)


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

    def test_bytecountestimate_becomes_bytes_over_time(self):
        t = tr("SELECT bytecountestimate() FROM Log "
               "WHERE service.name = 'x' TIMESERIES")
        self.assertEqual(
            t.expr, 'sum(bytes_over_time({service_name="x"} [$__auto]))')
        self.assertIn("unit:bytes", t.notes)
        self.assertEqual(t.confidence, EXACT)

    def test_bytecountestimate_with_facet(self):
        t = tr("SELECT bytecountestimate() FROM Log "
               "WHERE service.name = 'x' FACET level")
        self.assertEqual(
            t.expr,
            'sum by (level)(bytes_over_time({service_name="x"} '
            '[$__range]))')

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
            'count(count by (user_id)(count_over_time({'
            'service_name="checkout"} | json | __error__="" [$__range])))')
        self.assertEqual(t.confidence, NEEDS_REVIEW)

    def test_uniquecount_of_stream_label_needs_no_parser(self):
        t = tr("SELECT uniqueCount(pod) FROM Log "
               "WHERE service.name = 'x' TIMESERIES")
        self.assertEqual(
            t.expr,
            'count(count by (pod)(count_over_time({service_name="x"} '
            '[$__auto])))')

    def test_uniquecount_with_facet_groups_outer_count(self):
        t = tr("SELECT uniqueCount(user.id) FROM Log "
               "WHERE service.name = 'x' FACET level")
        self.assertEqual(
            t.expr,
            'count by (level)(count by (level, user_id)(count_over_time({'
            'service_name="x"} | json | __error__="" [$__range])))')
        self.assertEqual(t.group_by, ["level"])

    def test_facet_on_non_stream_label_forces_parser(self):
        t = tr("SELECT count(*) FROM Log WHERE service.name = 'x' "
               "FACET requestPath TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum by (requestPath)(count_over_time({service_name="x"} '
            '| json | __error__="" [$__auto]))')
        # 1.11: parsed-field grouping is deterministic (approximate), not
        # a review item.
        self.assertEqual(t.confidence, APPROXIMATE)
        self.assertTrue(any("parser" in n for n in t.notes))

    def test_filter_ratio_divides_two_log_aggregations(self):
        t = tr("SELECT filter(count(*), WHERE allColumnSearch('dmn', "
               "insensitive: true) AND ok = true) * 100.0 / count(*) "
               "AS 'pct' FROM Log WHERE service.name = 'x'")
        self.assertEqual(t.datasource, "loki")
        self.assertTrue(t.expr.startswith("100 * ("))
        self.assertIn(") / (", t.expr)
        self.assertIn('|~ "(?i)dmn"', t.expr)
        self.assertIn("unit:percent", t.notes)
        self.assertNotEqual(t.confidence, UNTRANSLATABLE)

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

    def test_multi_event_from_uses_first_event(self):
        t = tr("SELECT count(*) FROM Log, Log_dev WHERE service.name = 'x' "
               "AND log_level = 'ERROR'")
        self.assertEqual(t.datasource, "loki")
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x"} | json '
            '| log_level=~"(?i)ERROR" | __error__="" [$__range]))')
        self.assertTrue(any("Log, Log_dev" in n for n in t.notes))

    def test_percentage_with_search_in_embedded_where(self):
        t = translate_to_logql(NrqlQuery(
            raw="<ast>", from_=["Log"], where=SVC,
            select=[SelectItem(expr=Func(
                "percentage", args=[Func("count", args=[Star()])],
                where=Func("allcolumnsearch",
                           args=[Lit("fail"), Lit("insensitive:True")])))]),
            load_config())
        self.assertEqual(
            t.expr,
            '100 * sum(count_over_time({service_name="x"} |~ "(?i)fail" '
            '[$__range])) / sum(count_over_time({service_name="x"} '
            '[$__range]))')


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
            'sum(count_over_time({service_name="x", level=~"(?i)error"} '
            '[$__auto]))')

    def test_filter_supports_unwrap_aggregations(self):
        t = tr("SELECT filter(average(duration), WHERE level = 'error') "
               "FROM Log WHERE service.name = 'x' TIMESERIES")
        self.assertEqual(
            t.expr,
            'avg_over_time({service_name="x", level=~"(?i)error"} | json '
            '| unwrap duration | __error__="" [$__auto]) by ()')

    def test_count_if_becomes_filtered_count(self):
        t = tr("SELECT count(if(level = 'error', 1)) FROM Log "
               "WHERE service.name = 'x' TIMESERIES")
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x", level=~"(?i)error"} '
            '[$__auto]))')
        self.assertTrue(any("filtered aggregation" in n for n in t.notes))

    def test_sum_if_one_zero_becomes_filtered_count(self):
        t = tr("SELECT sum(if(level = 'error', 1, 0)) FROM Log "
               "WHERE service.name = 'x'")
        self.assertEqual(
            t.expr,
            'sum(count_over_time({service_name="x", level=~"(?i)error"} '
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
