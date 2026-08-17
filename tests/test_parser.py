"""Tests for nr2grafana.nrql.parser: tokenizer and NRQL parser."""

import unittest

from nr2grafana.nrql.parser import (
    Attr, BoolOp, Cmp, FacetItem, Func, InList, Lit, NotOp, NrqlParseError,
    NullCheck, OrderBy, SelectItem, Star, TimeseriesSpec, parse_nrql,
    tokenize, _unquote_string,
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


if __name__ == "__main__":
    unittest.main()
