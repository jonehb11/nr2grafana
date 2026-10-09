"""Tests for nr2grafana.nrql.parser: tokenizer and NRQL parser."""

import unittest

from nr2grafana.nrql.parser import (
    Attr, BoolOp, Cmp, FacetItem, Func, InList, Lit, NotOp, NrqlParseError,
    NullCheck, OrderBy, SelectItem, Star, TimeseriesSpec, parse_nrql,
    strip_comments, tokenize, _unquote_string,
)


class TokenizerTests(unittest.TestCase):
    def test_kinds_and_text(self):
        toks = tokenize("SELECT count(*) FROM `A B` WHERE x = 'it''s' AND y >= -5.5")
        self.assertEqual(
            [(t.kind, t.text) for t in toks],
            [("ident", "SELECT"), ("ident", "count"), ("lparen", "("),
             ("star", "*"), ("rparen", ")"), ("ident", "FROM"),
             ("qident", "`A B`"), ("ident", "WHERE"), ("ident", "x"),
             ("op", "="), ("string", "'it''s'"), ("ident", "AND"),
             ("ident", "y"), ("op", ">="), ("number", "-5.5")])

    def test_token_positions(self):
        toks = tokenize("SELECT x")
        self.assertEqual(toks[0].pos, 0)
        self.assertEqual(toks[1].pos, 7)

    def test_ident_allows_dots_and_specials(self):
        toks = tokenize("k8s.pod.name deployment.environment a-b/c:d")
        self.assertEqual([t.kind for t in toks], ["ident"] * 3)

    def test_operators(self):
        toks = tokenize("= != <> <= >= < >")
        self.assertEqual([t.text for t in toks],
                         ["=", "!=", "<>", "<=", ">=", "<", ">"])
        self.assertTrue(all(t.kind == "op" for t in toks))

    def test_unexpected_character_raises(self):
        with self.assertRaises(NrqlParseError):
            tokenize("SELECT @")

    def test_unquote_string(self):
        self.assertEqual(_unquote_string("'it''s'"), "it's")
        self.assertEqual(_unquote_string(r"'a\'b'"), "a'b")


class SelectTests(unittest.TestCase):
    def test_simple_aggregation(self):
        q = parse_nrql("SELECT average(duration) FROM Transaction")
        self.assertEqual(len(q.select), 1)
        fn = q.select[0].expr
        self.assertIsInstance(fn, Func)
        self.assertEqual(fn.name, "average")
        self.assertEqual(fn.args, [Attr("duration")])
        self.assertEqual(q.from_, ["Transaction"])
        self.assertIsNone(q.select[0].alias)

    def test_count_star(self):
        q = parse_nrql("SELECT count(*) FROM Transaction")
        fn = q.select[0].expr
        self.assertEqual(fn.name, "count")
        self.assertEqual(len(fn.args), 1)
        self.assertIsInstance(fn.args[0], Star)

    def test_multi_select_with_aliases(self):
        q = parse_nrql(
            "SELECT average(duration) AS 'Avg dur', max(duration) AS mx "
            "FROM Transaction")
        self.assertEqual(len(q.select), 2)
        self.assertEqual(q.select[0].alias, "Avg dur")
        self.assertEqual(q.select[1].alias, "mx")
        self.assertEqual(q.select[0].expr.name, "average")
        self.assertEqual(q.select[1].expr.name, "max")

    def test_function_name_lowercased(self):
        q = parse_nrql("SELECT uniqueCount(user) FROM T")
        self.assertEqual(q.select[0].expr.name, "uniquecount")

    def test_percentile_multiple_args(self):
        q = parse_nrql("SELECT percentile(duration, 95, 99) FROM Transaction")
        fn = q.select[0].expr
        self.assertEqual(fn.args,
                         [Attr("duration"), Lit(95), Lit(99)])

    def test_rate_count_star_duration_absorbed(self):
        q = parse_nrql("SELECT rate(count(*), 1 minute) FROM Transaction")
        fn = q.select[0].expr
        self.assertEqual(fn.name, "rate")
        self.assertEqual(len(fn.args), 2)
        inner = fn.args[0]
        self.assertIsInstance(inner, Func)
        self.assertEqual(inner.name, "count")
        self.assertIsInstance(inner.args[0], Star)
        # "1 minute" normalized to seconds
        self.assertEqual(fn.args[1], Lit(60.0))

    def test_rate_hour_duration(self):
        q = parse_nrql("SELECT rate(count(*), 2 hours) FROM T")
        self.assertEqual(q.select[0].expr.args[1], Lit(7200.0))

    def test_filter_with_embedded_where(self):
        q = parse_nrql(
            "SELECT filter(count(*), WHERE error IS TRUE) FROM Transaction")
        fn = q.select[0].expr
        self.assertEqual(fn.name, "filter")
        self.assertEqual(len(fn.args), 1)
        self.assertEqual(fn.args[0].name, "count")
        # IS TRUE becomes an equality with a boolean literal
        self.assertEqual(fn.where, Cmp(Attr("error"), "=", Lit(True)))

    def test_apdex_named_threshold(self):
        q = parse_nrql("SELECT apdex(duration, t: 0.5) FROM Transaction")
        fn = q.select[0].expr
        self.assertEqual(fn.name, "apdex")
        self.assertEqual(fn.args, [Attr("duration"), Lit("t:0.5")])

    def test_select_star(self):
        q = parse_nrql("SELECT * FROM Log")
        self.assertIsInstance(q.select[0].expr, Star)

    def test_select_plain_attributes(self):
        q = parse_nrql("SELECT message, level FROM Log")
        self.assertEqual(q.select[0].expr, Attr("message"))
        self.assertEqual(q.select[1].expr, Attr("level"))

    def test_from_first_form(self):
        q = parse_nrql("FROM Transaction SELECT count(*) WHERE appName = 'x'")
        self.assertEqual(q.from_, ["Transaction"])
        self.assertEqual(q.select[0].expr.name, "count")
        self.assertEqual(q.where, Cmp(Attr("appName"), "=", Lit("x")))

    def test_multiple_from_event_types(self):
        q = parse_nrql("SELECT count(*) FROM T, U")
        self.assertEqual(q.from_, ["T", "U"])

    def test_backquoted_metric_name_with_dots(self):
        q = parse_nrql("SELECT average(`k8s.pod.cpu`) FROM Metric")
        self.assertEqual(q.select[0].expr.args[0], Attr("k8s.pod.cpu"))

    def test_backquoted_event_type(self):
        q = parse_nrql("SELECT count(*) FROM `My Custom Event`")
        self.assertEqual(q.from_, ["My Custom Event"])

    def test_dotted_identifier_unquoted(self):
        q = parse_nrql("SELECT sum(checkout.orders.completed) FROM Metric")
        self.assertEqual(q.select[0].expr.args[0],
                         Attr("checkout.orders.completed"))

    def test_raw_preserved(self):
        raw = "SELECT count(*) FROM T"
        self.assertEqual(parse_nrql("  " + raw + "  ").raw, raw)

    def test_trailing_multiplier(self):
        q = parse_nrql("SELECT average(duration) * 1000 FROM T")
        self.assertEqual(q.select[0].multiplier, 1000.0)
        self.assertEqual(q.select[0].expr.name, "average")

    def test_prefix_multiplier(self):
        q = parse_nrql("SELECT 1000 * average(duration) FROM T")
        self.assertEqual(q.select[0].multiplier, 1000.0)
        self.assertIsInstance(q.select[0].expr, Func)
        self.assertEqual(q.select[0].expr.name, "average")

    def test_prefix_and_trailing_multiplier_combine(self):
        q = parse_nrql("SELECT 2 * average(duration) * 3 FROM T")
        self.assertEqual(q.select[0].multiplier, 6.0)

    def test_prefix_multiplier_with_division(self):
        q = parse_nrql("SELECT 100 * sum(x) / 60 FROM Metric")
        self.assertEqual(q.select[0].multiplier, 100.0 / 60.0)

    def test_ratio_of_two_aggregations_builds_ratio_node(self):
        q = parse_nrql("SELECT count(errors)/count(requests) FROM Metric")
        expr = q.select[0].expr
        self.assertIsInstance(expr, Func)
        self.assertEqual(expr.name, "_ratio")
        self.assertEqual(len(expr.args), 2)
        self.assertEqual(expr.args[0].name, "count")
        self.assertEqual(expr.args[0].args, [Attr("errors")])
        self.assertEqual(expr.args[1].name, "count")
        self.assertEqual(expr.args[1].args, [Attr("requests")])

    def test_chained_ratio_nests_left(self):
        q = parse_nrql("SELECT sum(a)/sum(b)/sum(c) FROM Metric")
        expr = q.select[0].expr
        self.assertEqual(expr.name, "_ratio")
        # ((a/b)/c): the left operand is itself a ratio.
        self.assertEqual(expr.args[0].name, "_ratio")
        self.assertEqual(expr.args[1].name, "sum")
        self.assertEqual(expr.args[1].args, [Attr("c")])

    def test_if_embedded_condition_and_then_value(self):
        q = parse_nrql("SELECT count(if(error IS TRUE, 1)) FROM T")
        outer = q.select[0].expr
        self.assertEqual(outer.name, "count")
        branch = outer.args[0]
        self.assertIsInstance(branch, Func)
        self.assertEqual(branch.name, "if")
        self.assertEqual(branch.where, Cmp(Attr("error"), "=", Lit(True)))
        self.assertEqual(branch.args, [Lit(1)])

    def test_if_with_else_value(self):
        q = parse_nrql("SELECT sum(if(a = 1, 1, 0)) FROM T")
        branch = q.select[0].expr.args[0]
        self.assertEqual(branch.args, [Lit(1), Lit(0)])
        self.assertEqual(branch.where, Cmp(Attr("a"), "=", Lit(1)))

    def test_cases_collects_conditions_and_aliases(self):
        q = parse_nrql("SELECT count(*) FROM T FACET cases("
                       "WHERE a = 1 AS one, WHERE b = 2 AS 'two')")
        fn = q.facet[0].expr
        self.assertEqual(fn.name, "cases")
        self.assertEqual(fn.cases, [
            (Cmp(Attr("a"), "=", Lit(1)), "one"),
            (Cmp(Attr("b"), "=", Lit(2)), "two"),
        ])


class WhereTests(unittest.TestCase):
    def _where(self, cond_text):
        return parse_nrql("SELECT count(*) FROM T WHERE " + cond_text).where

    def test_and_or_not_with_parens(self):
        w = self._where("(a = 1 OR b = 2) AND NOT c = 3")
        self.assertEqual(w, BoolOp("and", [
            BoolOp("or", [Cmp(Attr("a"), "=", Lit(1)),
                          Cmp(Attr("b"), "=", Lit(2))]),
            NotOp(Cmp(Attr("c"), "=", Lit(3))),
        ]))

    def test_in_and_not_in(self):
        w = self._where("x IN ('a','b') AND y NOT IN (1, 2)")
        self.assertEqual(w.items[0], InList(Attr("x"),
                                            [Lit("a"), Lit("b")], False))
        self.assertEqual(w.items[1], InList(Attr("y"),
                                            [Lit(1), Lit(2)], True))

    def test_like_variants(self):
        w = self._where("m LIKE '%x%' AND n NOT LIKE 'y_' "
                        "AND o RLIKE 'z.*' AND p NOT RLIKE 'w'")
        self.assertEqual([c.op for c in w.items],
                         ["LIKE", "NOT LIKE", "RLIKE", "NOT RLIKE"])
        self.assertEqual(w.items[0].right, Lit("%x%"))

    def test_null_and_boolean_checks(self):
        w = self._where("a IS NULL AND b IS NOT NULL AND c IS TRUE "
                        "AND d IS FALSE")
        self.assertEqual(w.items[0], NullCheck(Attr("a"), negated=False))
        self.assertEqual(w.items[1], NullCheck(Attr("b"), negated=True))
        self.assertEqual(w.items[2], Cmp(Attr("c"), "=", Lit(True)))
        self.assertEqual(w.items[3], Cmp(Attr("d"), "=", Lit(False)))

    def test_is_not_true(self):
        w = self._where("c IS NOT TRUE")
        self.assertEqual(w, Cmp(Attr("c"), "!=", Lit(True)))

    def test_numeric_comparisons(self):
        w = self._where("a >= 5 AND b < 2.5 AND c != 'x' AND d <> 'y'")
        self.assertEqual(w.items[0], Cmp(Attr("a"), ">=", Lit(5)))
        self.assertEqual(w.items[1], Cmp(Attr("b"), "<", Lit(2.5)))
        # <> is normalized to !=
        self.assertEqual(w.items[2], Cmp(Attr("c"), "!=", Lit("x")))
        self.assertEqual(w.items[3], Cmp(Attr("d"), "!=", Lit("y")))

    def test_negative_number_literal(self):
        w = self._where("delta < -5")
        self.assertEqual(w, Cmp(Attr("delta"), "<", Lit(-5)))

    def test_variable_placeholder_in_string(self):
        w = self._where("appName = '{{app}}'")
        self.assertEqual(w, Cmp(Attr("appName"), "=", Lit("{{app}}")))

    def test_variable_placeholder_in_in_list(self):
        w = self._where("appName IN ('{{app}}')")
        self.assertEqual(w, InList(Attr("appName"), [Lit("{{app}}")], False))

    def test_string_escapes(self):
        w = self._where("x = 'it''s'")
        self.assertEqual(w.right, Lit("it's"))


class ClauseTests(unittest.TestCase):
    def test_facet_multi_and_alias(self):
        q = parse_nrql("SELECT count(*) FROM T FACET name, host AS server")
        self.assertEqual(q.facet, [
            FacetItem(Attr("name"), None),
            FacetItem(Attr("host"), "server"),
        ])

    def test_facet_function(self):
        q = parse_nrql("SELECT count(*) FROM T FACET cases(WHERE duration > 1)")
        self.assertIsInstance(q.facet[0].expr, Func)
        self.assertEqual(q.facet[0].expr.name, "cases")
        self.assertEqual(q.facet[0].expr.where,
                         Cmp(Attr("duration"), ">", Lit(1)))

    def test_timeseries_auto(self):
        q = parse_nrql("SELECT count(*) FROM T TIMESERIES AUTO")
        self.assertEqual(q.timeseries,
                         TimeseriesSpec(auto=True, max=False,
                                        interval_seconds=None, slide_by=None))

    def test_timeseries_bare(self):
        q = parse_nrql("SELECT count(*) FROM T TIMESERIES")
        self.assertIsNotNone(q.timeseries)
        self.assertTrue(q.timeseries.auto)

    def test_timeseries_max(self):
        q = parse_nrql("SELECT count(*) FROM T TIMESERIES MAX")
        self.assertFalse(q.timeseries.auto)
        self.assertTrue(q.timeseries.max)

    def test_timeseries_interval(self):
        q = parse_nrql("SELECT count(*) FROM T TIMESERIES 30 minutes")
        self.assertFalse(q.timeseries.auto)
        self.assertEqual(q.timeseries.interval_seconds, 1800.0)

    def test_slide_by_attaches_to_timeseries(self):
        q = parse_nrql(
            "SELECT count(*) FROM T TIMESERIES 30 minutes SLIDE BY 5 minutes")
        self.assertEqual(q.timeseries.slide_by, "5 minutes")

    def test_slide_by_without_timeseries_goes_to_extras(self):
        q = parse_nrql("SELECT count(*) FROM T SLIDE BY 5 minutes")
        self.assertIsNone(q.timeseries)
        self.assertEqual(q.extras, ["SLIDE BY 5 minutes"])

    def test_since_until_compare_with(self):
        q = parse_nrql("SELECT count(*) FROM T SINCE 1 hour ago "
                       "UNTIL 30 minutes ago COMPARE WITH 1 week ago")
        self.assertEqual(q.since, "1 hour ago")
        self.assertEqual(q.until, "30 minutes ago")
        self.assertEqual(q.compare_with, "1 week ago")

    def test_limit_int_and_max(self):
        self.assertEqual(parse_nrql("SELECT count(*) FROM T LIMIT 10").limit,
                         10)
        self.assertEqual(parse_nrql("SELECT count(*) FROM T LIMIT MAX").limit,
                         "MAX")

    def test_order_by(self):
        q = parse_nrql("SELECT count(*) FROM T LIMIT 10 "
                       "ORDER BY duration DESC")
        self.assertEqual(q.order_by, OrderBy(Attr("duration"), "DESC"))
        q2 = parse_nrql("SELECT count(*) FROM T ORDER BY duration")
        self.assertEqual(q2.order_by.direction, "ASC")

    def test_with_timezone_and_extrapolate(self):
        q = parse_nrql("SELECT count(*) FROM T "
                       "WITH TIMEZONE 'America/New_York' EXTRAPOLATE")
        # clause text keeps the raw (quoted) token
        self.assertEqual(q.timezone, "America/New_York")
        self.assertTrue(q.extrapolate)

    def test_extrapolate_defaults_false(self):
        self.assertFalse(parse_nrql("SELECT count(*) FROM T").extrapolate)


class ParseErrorTests(unittest.TestCase):
    def assert_error(self, text):
        with self.assertRaises(NrqlParseError):
            parse_nrql(text)

    def test_empty_query(self):
        self.assert_error("")

    def test_select_only(self):
        self.assert_error("SELECT")

    def test_from_without_select(self):
        self.assert_error("FROM Transaction")

    def test_unterminated_function_call(self):
        self.assert_error("SELECT count(* FROM T")

    def test_dangling_where(self):
        self.assert_error("SELECT count(*) FROM T WHERE")

    def test_is_needs_null_true_false(self):
        self.assert_error("SELECT count(*) FROM T WHERE x IS BANANA")

    def test_unexpected_character(self):
        self.assert_error("SELECT count(*) FROM T WHERE x @ 1")

    def test_bad_limit(self):
        self.assert_error("SELECT count(*) FROM T LIMIT abc")

    def test_missing_select_keyword(self):
        self.assert_error("count(*) FROM T")

    def test_error_carries_position_context(self):
        try:
            parse_nrql("SELECT count(*) FROM T WHERE x @ 1")
        except NrqlParseError as e:
            self.assertGreaterEqual(e.pos, 0)
            self.assertIn("near:", str(e))
        else:
            self.fail("expected NrqlParseError")

    def test_bare_literal_is_not_a_predicate(self):
        self.assert_error("SELECT count(*) FROM T WHERE 'x'")

    def test_dangling_operator_still_errors(self):
        self.assert_error("SELECT count(*) FROM T WHERE flag =")


# ---------------------------------------------------------------------------
# 1.11 translation-fidelity fixes (contract F6/F7 shapes)
# ---------------------------------------------------------------------------

TRUE_FLAG = Cmp(Attr("should_publish"), "=", Lit(True))


class CommentTests(unittest.TestCase):
    """``--`` comments are stripped anywhere, including mid-query lines."""

    def test_tokenizer_drops_line_comments(self):
        toks = tokenize("SELECT a -- the a\nFROM T")
        self.assertEqual([t.text for t in toks], ["SELECT", "a", "FROM", "T"])

    def test_tokenizer_drops_slash_and_block_comments(self):
        toks = tokenize("SELECT a // c\n/* multi\nline */ FROM T")
        self.assertEqual([t.text for t in toks], ["SELECT", "a", "FROM", "T"])

    def test_double_dash_inside_string_is_data(self):
        q = parse_nrql("SELECT count(*) FROM T WHERE name = 'a--b' -- c")
        self.assertEqual(q.where, Cmp(Attr("name"), "=", Lit("a--b")))

    def test_comments_anywhere_in_multiline_query(self):
        q = parse_nrql(
            "-- leading comment\n"
            "SELECT count(*) FROM Log -- trailing\n"
            "WHERE level = 'ERROR' -- mid-query line\n"
            "FACET host -- last\n")
        self.assertEqual(q.from_, ["Log"])
        self.assertEqual(q.where, Cmp(Attr("level"), "=", Lit("ERROR")))
        self.assertEqual(q.facet, [FacetItem(Attr("host"), None)])
        self.assertEqual(q.extras, [])

    def test_comment_between_clauses_does_not_leak_into_since(self):
        q = parse_nrql("SELECT count(*) FROM T SINCE 1 hour ago -- x\n"
                       "LIMIT 5")
        self.assertEqual(q.since, "1 hour ago")
        self.assertEqual(q.limit, 5)

    def test_raw_keeps_original_text(self):
        text = "SELECT count(*) FROM T -- c"
        self.assertEqual(parse_nrql(text).raw, text)

    def test_strip_comments_helper(self):
        self.assertEqual(
            strip_comments("SELECT a /* b */ FROM T -- c\nWHERE x = '--'"),
            "SELECT a FROM T WHERE x = '--'")


class BareBooleanPredicateTests(unittest.TestCase):
    """F6: ``... AND should_publish`` means ``should_publish = true``."""

    def _where(self, cond_text):
        return parse_nrql("SELECT count(*) FROM Log WHERE " + cond_text).where

    def test_contract_shape_and_flag_at_end(self):
        w = self._where("cluster = 'acme-cluster-prod' AND should_publish")
        self.assertEqual(w, BoolOp("and", [
            Cmp(Attr("cluster"), "=", Lit("acme-cluster-prod")),
            TRUE_FLAG,
        ]))

    def test_flag_first_then_and(self):
        w = self._where("should_publish AND level = 'ERROR'")
        self.assertEqual(w, BoolOp("and", [
            TRUE_FLAG, Cmp(Attr("level"), "=", Lit("ERROR"))]))

    def test_flag_alone(self):
        self.assertEqual(self._where("should_publish"), TRUE_FLAG)

    def test_not_flag(self):
        self.assertEqual(self._where("NOT should_publish"), NotOp(TRUE_FLAG))

    def test_backticked_flag(self):
        self.assertEqual(self._where("`should_publish`"), TRUE_FLAG)

    def test_flag_inside_parens_and_or(self):
        w = self._where("(a = 1 OR should_publish) AND other_flag")
        self.assertEqual(w, BoolOp("and", [
            BoolOp("or", [Cmp(Attr("a"), "=", Lit(1)), TRUE_FLAG]),
            Cmp(Attr("other_flag"), "=", Lit(True)),
        ]))

    def test_flag_before_facet_and_limit(self):
        q = parse_nrql("SELECT count(*) FROM Log WHERE should_publish "
                       "FACET host LIMIT 5")
        self.assertEqual(q.where, TRUE_FLAG)
        self.assertEqual(q.facet, [FacetItem(Attr("host"), None)])
        self.assertEqual(q.limit, 5)

    def test_flag_equivalent_to_explicit_forms(self):
        self.assertEqual(self._where("should_publish"),
                         self._where("should_publish = true"))
        self.assertEqual(self._where("should_publish"),
                         self._where("should_publish IS TRUE"))

    def test_flag_in_filter_where(self):
        q = parse_nrql("SELECT filter(count(*), WHERE should_publish) "
                       "FROM Log")
        self.assertEqual(q.select[0].expr.where, TRUE_FLAG)

    def test_flag_in_cases_with_alias(self):
        q = parse_nrql("SELECT count(*) FROM Log FACET cases("
                       "WHERE should_publish AS 'on', WHERE NOT "
                       "should_publish AS 'off')")
        self.assertEqual(q.facet[0].expr.cases, [
            (TRUE_FLAG, "on"), (NotOp(TRUE_FLAG), "off")])

    def test_if_with_bare_condition(self):
        q = parse_nrql("SELECT sum(if(error, 1, 0)) FROM T")
        branch = q.select[0].expr.args[0]
        self.assertEqual(branch.where, Cmp(Attr("error"), "=", Lit(True)))
        self.assertEqual(branch.args, [Lit(1), Lit(0)])

    def test_flag_followed_by_comparison_still_comparison(self):
        # Regression guard: a bare attr followed by an operator is NOT a
        # truthy test.
        self.assertEqual(self._where("should_publish != false"),
                         Cmp(Attr("should_publish"), "!=", Lit(False)))

    def test_bare_attr_before_not_in_is_in_list(self):
        self.assertEqual(self._where("x NOT IN (1)"),
                         InList(Attr("x"), [Lit(1)], True))


class IsPredicateTests(unittest.TestCase):
    def _where(self, cond_text):
        return parse_nrql("SELECT count(*) FROM Log WHERE " + cond_text).where

    def test_is_forms_with_backticks(self):
        w = self._where("`a.b` IS NULL AND `c.d` IS NOT NULL AND "
                        "`e` IS TRUE AND `f` IS FALSE AND `g` IS NOT FALSE")
        self.assertEqual(w.items, [
            NullCheck(Attr("a.b"), negated=False),
            NullCheck(Attr("c.d"), negated=True),
            Cmp(Attr("e"), "=", Lit(True)),
            Cmp(Attr("f"), "=", Lit(False)),
            Cmp(Attr("g"), "!=", Lit(False)),
        ])

    def test_is_case_insensitive(self):
        self.assertEqual(self._where("a is not null"),
                         NullCheck(Attr("a"), negated=True))

    def test_is_null_inside_filter(self):
        q = parse_nrql("SELECT filter(count(*), WHERE `k8s.pod.name` IS "
                       "NOT NULL) FROM K8sContainerSample")
        self.assertEqual(q.select[0].expr.where,
                         NullCheck(Attr("k8s.pod.name"), negated=True))


class BacktickEverywhereTests(unittest.TestCase):
    def test_backticks_in_every_position(self):
        q = parse_nrql(
            "SELECT average(`response time`) AS `avg rt`, `plain` "
            "FROM `My Event` WHERE `k8s.pod.name` IN ('a') AND `flag` "
            "FACET `k8s.namespace.name` AS `ns` ORDER BY `avg rt` DESC")
        self.assertEqual(q.select[0].expr.args, [Attr("response time")])
        self.assertEqual(q.select[0].alias, "avg rt")
        self.assertEqual(q.select[1].expr, Attr("plain"))
        self.assertEqual(q.from_, ["My Event"])
        self.assertEqual(q.where, BoolOp("and", [
            InList(Attr("k8s.pod.name"), [Lit("a")], False),
            Cmp(Attr("flag"), "=", Lit(True)),
        ]))
        self.assertEqual(q.facet, [FacetItem(Attr("k8s.namespace.name"),
                                             "ns")])
        self.assertEqual(q.order_by, OrderBy(Attr("avg rt"), "DESC"))

    def test_backticked_function_arguments(self):
        q = parse_nrql("SELECT percentile(`duration.ms`, 95) FROM T "
                       "FACET tuple(`a.b`, `c`)")
        self.assertEqual(q.select[0].expr.args, [Attr("duration.ms"),
                                                 Lit(95)])
        self.assertEqual(q.facet[0].expr.args, [Attr("a.b"), Attr("c")])


class MultiEventFromTests(unittest.TestCase):
    """``FROM Log, Log_dev``: all events kept in from_, first is primary."""

    def test_contract_shape(self):
        q = parse_nrql("SELECT count(*) FROM Log, Log_dev "
                       "WHERE level = 'ERROR' FACET host")
        self.assertEqual(q.from_, ["Log", "Log_dev"])
        self.assertEqual(q.where, Cmp(Attr("level"), "=", Lit("ERROR")))
        self.assertEqual(len(q.notes), 1)
        self.assertIn("Log, Log_dev", q.notes[0])
        self.assertIn("first (Log)", q.notes[0])
        # Not an error class: nothing is dropped from the translation.
        self.assertEqual(q.extras, [])

    def test_from_first_form(self):
        q = parse_nrql("FROM Log, Log_dev SELECT count(*)")
        self.assertEqual(q.from_, ["Log", "Log_dev"])
        self.assertEqual(len(q.notes), 1)

    def test_single_event_has_no_note(self):
        self.assertEqual(parse_nrql("SELECT count(*) FROM Log").notes, [])


class LogFunctionTests(unittest.TestCase):
    """F7 inputs: allColumnSearch / aparse / capture / tuple."""

    def test_all_column_search_as_predicate(self):
        q = parse_nrql("SELECT count(*) FROM Log WHERE "
                       "allColumnSearch('t', insensitive: true)")
        self.assertEqual(q.where, Cmp(
            Func("allcolumnsearch", args=[Lit("t"),
                                          Lit("insensitive:true")]),
            "=", Lit(True)))

    def test_all_column_search_combined_with_other_predicates(self):
        q = parse_nrql("SELECT count(*) FROM Log WHERE "
                       "allColumnSearch('timeout', insensitive: true) "
                       "AND cluster = 'acme-cluster-prod' TIMESERIES")
        self.assertIsInstance(q.where, BoolOp)
        self.assertEqual(q.where.items[0].left.name, "allcolumnsearch")
        self.assertEqual(q.where.items[1],
                         Cmp(Attr("cluster"), "=", Lit("acme-cluster-prod")))
        self.assertTrue(q.timeseries.auto)

    def test_named_arg_false_lowercased(self):
        q = parse_nrql("SELECT count(*) FROM Log WHERE "
                       "allColumnSearch('t', insensitive: false)")
        self.assertEqual(q.where.left.args[1], Lit("insensitive:false"))

    def test_apdex_named_arg_unchanged(self):
        q = parse_nrql("SELECT apdex(duration, t: 0.5) FROM T")
        self.assertEqual(q.select[0].expr.args, [Attr("duration"),
                                                 Lit("t:0.5")])

    def test_aparse_facet(self):
        q = parse_nrql("SELECT count(*) FROM Log "
                       "FACET aparse(message, '%[TOPIC:*]%')")
        fn = q.facet[0].expr
        self.assertEqual(fn, Func("aparse", args=[Attr("message"),
                                                  Lit("%[TOPIC:*]%")]))

    def test_aparse_with_alias(self):
        q = parse_nrql("SELECT count(*) FROM Log "
                       "FACET aparse(message, '%[TOPIC:*]%') AS topic")
        self.assertEqual(q.facet[0].alias, "topic")

    def test_capture_raw_regex_keeps_backslashes(self):
        q = parse_nrql(
            "SELECT count(*) FROM Log "
            "FACET capture(message, r'\\[TOPIC:(?P<topic>[^\\]]*)\\]')")
        fn = q.facet[0].expr
        self.assertEqual(fn.name, "capture")
        self.assertEqual(fn.args, [
            Attr("message"), Lit("\\[TOPIC:(?P<topic>[^\\]]*)\\]")])

    def test_capture_plain_string_regex(self):
        q = parse_nrql("SELECT count(*) FROM Log "
                       "FACET capture(message, '(?P<code>[0-9]+)')")
        self.assertEqual(q.facet[0].expr.args[1], Lit("(?P<code>[0-9]+)"))

    def test_raw_string_tokenizes_as_string(self):
        toks = tokenize("r'a\\d' R'b'")
        self.assertEqual([(t.kind, t.text) for t in toks],
                         [("string", "r'a\\d'"), ("string", "R'b'")])
        self.assertEqual(_unquote_string("r'a\\d'"), "a\\d")
        self.assertEqual(_unquote_string("r'it''s'"), "it's")

    def test_identifier_named_r_is_still_an_identifier(self):
        q = parse_nrql("SELECT r FROM T WHERE r = 'x'")
        self.assertEqual(q.select[0].expr, Attr("r"))
        self.assertEqual(q.where, Cmp(Attr("r"), "=", Lit("x")))

    def test_tuple_facet(self):
        q = parse_nrql("SELECT count(*) FROM Log FACET tuple(host, level)")
        self.assertEqual(q.facet[0].expr,
                         Func("tuple", args=[Attr("host"), Attr("level")]))

    def test_select_tuple(self):
        q = parse_nrql("SELECT uniqueCount(tuple(a, b)) FROM Log")
        self.assertEqual(q.select[0].expr.args[0].name, "tuple")


class MetricFormatTests(unittest.TestCase):
    def test_with_metric_format_unquoted(self):
        q = parse_nrql("SELECT sum(acme_backend.orders) FROM Metric "
                       "WITH METRIC_FORMAT 'acme.{name}' TIMESERIES")
        self.assertEqual(q.metric_format, "acme.{name}")
        self.assertIsNotNone(q.timeseries)
        self.assertEqual(q.extras, [])

    def test_with_metric_format_bare(self):
        q = parse_nrql("SELECT sum(x) FROM Metric WITH METRIC_FORMAT "
                       "acme.{name}")
        self.assertEqual(q.metric_format, "acme.{name}")


class CalendarFunctionTests(unittest.TestCase):
    def test_facet_dateof_keeps_node_and_adds_extras_note(self):
        q = parse_nrql("SELECT count(*) FROM Log FACET dateOf(timestamp)")
        self.assertEqual(q.facet[0].expr,
                         Func("dateof", args=[Attr("timestamp")]))
        self.assertEqual(len(q.extras), 1)
        self.assertTrue(q.extras[0].startswith("dateof(timestamp)"))
        self.assertIn("no Grafana equivalent", q.extras[0])

    def test_hourof_and_weekof_each_noted(self):
        q = parse_nrql("SELECT count(*) FROM Log "
                       "FACET hourOf(timestamp), weekOf(timestamp)")
        self.assertEqual([e.split(" ")[0] for e in q.extras],
                         ["hourof(timestamp)", "weekof(timestamp)"])

    def test_calendar_function_in_select(self):
        q = parse_nrql("SELECT dateOf(timestamp), count(*) FROM Log")
        self.assertEqual(len(q.extras), 1)

    def test_same_function_noted_once(self):
        q = parse_nrql("SELECT count(*) FROM Log "
                       "FACET dateOf(timestamp), dateOf(created)")
        self.assertEqual(len(q.extras), 1)

    def test_ordinary_functions_do_not_add_extras(self):
        q = parse_nrql("SELECT count(*) FROM Log FACET string(host), "
                       "cases(WHERE a = 1)")
        self.assertEqual(q.extras, [])

    def test_no_python_repr_in_note(self):
        q = parse_nrql("SELECT count(*) FROM Log "
                       "FACET weekOf(timestamp, 'UTC')")
        self.assertNotIn("Attr(", q.extras[0])
        self.assertNotIn("Lit(", q.extras[0])
        self.assertNotIn("Func(", q.extras[0])


class MultiStatementTests(unittest.TestCase):
    """Two SELECTs in one string: first parsed, second -> extras + note."""

    def test_second_select_goes_to_extras(self):
        q = parse_nrql("SELECT count(*) FROM Log WHERE a = 1 "
                       "SELECT count(*) FROM Log WHERE b = 2")
        # The first statement's WHERE must NOT be overwritten.
        self.assertEqual(q.where, Cmp(Attr("a"), "=", Lit(1)))
        self.assertEqual(q.extras,
                         ["SELECT count(*) FROM Log WHERE b = 2"])
        self.assertEqual(len(q.notes), 1)
        self.assertIn("more than one statement", q.notes[0])

    def test_semicolon_separated(self):
        q = parse_nrql("SELECT count(*) FROM Log WHERE a = 1; "
                       "SELECT count(*) FROM Log WHERE b = 2;")
        self.assertEqual(q.where, Cmp(Attr("a"), "=", Lit(1)))
        self.assertEqual(q.extras,
                         ["SELECT count(*) FROM Log WHERE b = 2"])

    def test_trailing_semicolon_alone_is_ignored(self):
        q = parse_nrql("SELECT count(*) FROM Log SINCE 1 hour ago;")
        self.assertEqual(q.since, "1 hour ago")
        self.assertEqual(q.extras, [])
        self.assertEqual(q.notes, [])

    def test_from_first_restart(self):
        q = parse_nrql("SELECT count(*) FROM Log WHERE a = 1 "
                       "FROM Log SELECT count(*) WHERE b = 2")
        self.assertEqual(q.where, Cmp(Attr("a"), "=", Lit(1)))
        self.assertEqual(q.extras, ["FROM Log SELECT count(*) WHERE b = 2"])

    def test_second_statement_text_is_comment_free(self):
        q = parse_nrql("SELECT count(*) FROM Log\n"
                       "-- second query\n"
                       "SELECT count(*) FROM Log -- tail\n"
                       "WHERE b = 2")
        self.assertEqual(q.extras, ["SELECT count(*) FROM Log WHERE b = 2"])

    def test_late_from_still_honored(self):
        # Regression guard for the pre-existing late-FROM path.
        q = parse_nrql("SELECT count(*) WHERE a = 1 FROM Log")
        self.assertEqual(q.from_, ["Log"])
        self.assertEqual(q.extras, [])


if __name__ == "__main__":
    unittest.main()
