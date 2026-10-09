"""Golden-corpus regression tests (fixtures/corpus, redacted).

The corpus holds the NRQL shapes of a real 531-widget migration, the
converter's output at the time ("before") and the targets a human ended
up with ("expected"). docs/dev/ARCHITECTURE-1.11.md section 0 lists the
failure classes F1..F12 these tests pin down.

Every converter-facing test imports the translator lazily and SKIPS when
the sibling seam it exercises has not landed yet (see _seam()), so the
module is green while 1.11 is being assembled and becomes a real gate
once the seams exist.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "tools"))

import corpus_tools as ct  # noqa: E402

CORPUS_DIR = os.path.join(REPO, "fixtures", "corpus")

_CACHE = {}


def widgets():
    if "widgets" not in _CACHE:
        _CACHE["widgets"] = ct.load_corpus(CORPUS_DIR)
    return _CACHE["widgets"]


def converted():
    """{widget id: emitted targets} for every widget with NRQL."""
    if "converted" not in _CACHE:
        cfg = ct.default_cfg()
        out = {}
        for w in widgets():
            if w.get("nrql"):
                out[w["id"]] = ct.convert_widget(w, cfg)
        _CACHE["converted"] = out
    return _CACHE["converted"]


def _seam(module, name):
    """The sibling symbol, or None when it has not landed."""
    try:
        mod = __import__(module, fromlist=[name])
    except Exception:
        return None
    return getattr(mod, name, None)


def _parser_handles(nrql):
    parse = _seam("nr2grafana.nrql.parser", "parse_nrql")
    if parse is None:
        return False
    try:
        parse(nrql)
    except Exception:
        return False
    return True


RENDER = ("nr2grafana.translate.common", "render_value")
KIND = ("nr2grafana.translate.metrics", "infer_metric_kind")
K8S = ("nr2grafana.translate.metrics", "K8S_METRIC_MAP")
CW = ("nr2grafana.translate.cloudwatch", "translate_to_cloudwatch")
BIND = ("nr2grafana.bind", "bind_datasources")
HINTS = ("nr2grafana.translate.hints", "collect_hints")
ALL_SEAMS = (RENDER, KIND, K8S, CW, BIND, HINTS)


def skip_unless(*seams):
    missing = [m + "." + n for m, n in seams if _seam(m, n) is None]
    return unittest.skipIf(
        bool(missing), "sibling seam(s) not landed yet: %s"
        % ", ".join(missing))


def _joined(targets):
    return "\n".join(ct._exprs(targets))


# ---------------------------------------------------------------------------
# Fixture integrity + redaction
# ---------------------------------------------------------------------------

class CorpusFilesTests(unittest.TestCase):
    def test_index_and_page_files(self):
        with open(os.path.join(CORPUS_DIR, "index.json")) as fh:
            index = json.load(fh)
        self.assertEqual(index["schema"], ct.SCHEMA)
        self.assertEqual(index["widgets"], 531)
        self.assertEqual(len(index["files"]), index["pages"])
        self.assertEqual(len(widgets()), index["widgets"])
        for name in index["files"]:
            self.assertTrue(os.path.exists(os.path.join(CORPUS_DIR, name)))
        self.assertEqual(index["placeholders"]["cluster_prefix"],
                         "acme-cluster-")
        self.assertEqual(index["placeholders"]["app"], "acme-backend")

    def test_widget_records_are_well_formed(self):
        ids = set()
        for w in widgets():
            self.assertNotIn(w["id"], ids)
            ids.add(w["id"])
            self.assertIsInstance(w["nrql"], list)
            self.assertIn(w["before"]["confidence"],
                          ("exact", "approximate", "needs-review",
                           "untranslatable"))
            for e in w["expected"]:
                self.assertIn(e["datasource"],
                              ("prometheus", "loki", "cloudwatch"))
                if e["datasource"] == "cloudwatch":
                    self.assertTrue(e.get("namespace") or
                                    e.get("expression"))
                else:
                    self.assertTrue(e.get("expr"))
        queried = sum(1 for w in widgets() if w["nrql"])
        self.assertEqual(queried, 498)
        self.assertEqual(sum(len(w["expected"]) for w in widgets()), 563)

    def test_files_are_ascii_lf(self):
        for name in os.listdir(CORPUS_DIR):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(CORPUS_DIR, name), "rb") as fh:
                raw = fh.read()
            self.assertNotIn(b"\r", raw, name)
            raw.decode("ascii")

    def test_deterministic_load_order(self):
        a = [w["id"] for w in ct.load_corpus(CORPUS_DIR)]
        b = [w["id"] for w in ct.load_corpus(CORPUS_DIR)]
        self.assertEqual(a, b)
        self.assertEqual(a[0], "p01-w001")


class RedactionTests(unittest.TestCase):
    """The corpus must contain placeholders only. The originals are not
    available here (by design), so this checks identifier SHAPES: any
    multi-dash name, long number, hostname, GUID or ARN that is not an
    acme-* placeholder, a region, generic vocabulary or a <REDACTED_*>
    marker fails."""

    def test_no_identifier_shaped_tokens_survive(self):
        bad = []
        for name in os.listdir(CORPUS_DIR):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(CORPUS_DIR, name)) as fh:
                doc = json.load(fh)
            for s in ct.iter_strings(doc):
                for v in ct.shape_violations(s):
                    bad.append("%s: %s" % (name, v))
        self.assertEqual(bad, [], "identifier-shaped tokens in corpus:\n"
                         + "\n".join(bad[:20]))

    def test_placeholders_in_use(self):
        blob = "\n".join(s for w in widgets() for s in ct.iter_strings(w))
        self.assertIn("concat('acme-cluster-', {{env}})", blob)
        self.assertIn("acme_backend", blob)
        self.assertIn("concat('acme-backend (',{{env}},')')", blob)
        self.assertIn("acme-queue-1-", blob)
        self.assertIn("<REDACTED_ENTITY>", blob)
        self.assertIn("<REDACTED_BLOB>", blob)
        # The exact string of the contract's F1 example survives (as the
        # "before" output) so the regression is reproducible.
        self.assertIn("Lit(value='acme-cluster-')", blob)

    def test_expected_label_values_are_placeholders(self):
        rx = re.compile(r'\b(cluster|namespace|deployment|job|'
                        r'service_name)\s*(?:=~|=)\s*"([^"]*)"')
        bad = []
        for w in widgets():
            for e in w["expected"]:
                for label, value in rx.findall(e.get("expr") or ""):
                    for alt in value.split("|"):
                        alt = alt.replace("(?i)", "")
                        if alt in (".+", ".*", "") or alt.startswith("$") \
                                or alt.startswith("<REDACTED") \
                                or alt.startswith("acme"):
                            continue
                        bad.append("%s %s=%s" % (w["id"], label, alt))
        self.assertEqual(bad, [], "\n".join(bad[:20]))

    def test_derive_map_scrubs_synthetic_identifiers(self):
        # A made-up map with invented identifiers: the generator must
        # replace every one of them and prove it (no originals survive,
        # no identifier-shaped tokens remain).
        src = {
            "widgets": [
                {"page": "Ops", "widget": "Orders", "panel_id": 1,
                 "confidence": "needs-review", "visualization": "viz.line",
                 "grafana_type": "timeseries",
                 "nrql": ["SELECT sum(zz_top_backend.order.created) FROM "
                          "Metric WHERE clusterName = concat('contoso-blue"
                          "-green-', {{env}}) AND appName = concat('zz-top-"
                          "backend (', {{env}}, ')') AND ndc = 123456789 "
                          "AND host = 'db1.corp.contoso.net' TIMESERIES",
                          "SELECT latest(aws.sqs.ApproximateNumberOfMessages"
                          "Visible) FROM Metric WHERE aws.sqs.QueueName = "
                          "concat('contoso-orders-zz_top_backend-events-', "
                          "{{env}}) AND entity.guid = "
                          "'MTIzNDU2Nzg5MHxBUE18QVBQTElDQVRJT058MTIzNDU2Nzg5'",
                          "SELECT sum(aws.billing.EstimatedCharges) FROM "
                          "FinanceSample WHERE linkedAccountName IN "
                          "('ct0987654321-dev', 'ct0987654321-prod') AND "
                          "guid = '0f8fcd11-1234-4bcd-9abc-0123456789ab'"],
                 "nr2grafana_queries": [
                     {"datasource": "prometheus", "type": "range",
                      "expr": "avg_over_time(zz_top_backend_order_created"
                              "{cluster=\"Func(name='concat', args=[Lit("
                              "value='contoso-blue-green-'), Attr(name='{{"
                              "env}}')], where=None, cases=[])\"}[5m])"}],
                 "live_targets": [
                     {"datasource_uid": "mimir", "refId": "A",
                      "expr": "sum(increase(zz_top_backend_order_created_"
                              "total{cluster=\"contoso-blue-green-prod\","
                              "namespace=\"zz-top\",deployment=\"zz-top-"
                              "backend-worker\"}[$__range]))"},
                     {"datasource_uid": "cloudwatch", "refId": "B",
                      "namespace": "AWS/SQS", "metricName":
                      "ApproximateNumberOfMessagesVisible",
                      "statistic": "Average", "dimension_keys": [
                          "QueueName"], "dimensions": {"QueueName": ["*"]},
                      "queryMode": "Metrics", "region": "default",
                      "metricEditorMode": 0}],
                 "notes": ["assumed gauge 'zz_top_backend_order_created'",
                           "cluster ContosoBlueGreen-prod-us-east-1 via "
                           "ZzTop ops; see https://wiki.contoso.net/x",
                           "timefrom:now-604800s"]}]}
        rmap = ct.derive_redaction_map(src)
        out = ct.redact(src, rmap)
        self.assertEqual(ct.surviving_originals(out, rmap), [])
        blob = "\n".join(ct.iter_strings(out))
        self.assertEqual([v for s in ct.iter_strings(out)
                          for v in ct.shape_violations(s)], [])
        for token in ("contoso", "zz-top", "zz_top", "zztop", "ZzTop",
                      "123456789", "0987654321", "db1.corp", "0f8fcd11",
                      "MTIzNDU2"):
            self.assertNotIn(token.lower(), blob.lower(), token)
        self.assertIn("concat('acme-cluster-', {{env}})", blob)
        self.assertIn("concat('acme-backend (', {{env}}, ')')", blob)
        self.assertIn("acme_backend_order_created", blob)
        self.assertIn("Lit(value='acme-cluster-')", blob)
        self.assertIn('cluster="acme-cluster-prod"', blob)
        self.assertIn('namespace="acme"', blob)
        self.assertIn('deployment="acme-backend-worker"', blob)
        self.assertIn("concat('acme-queue-1-', {{env}})", blob)
        self.assertIn("'acme-account-1-dev', 'acme-account-1-prod'", blob)
        self.assertIn("<REDACTED_GUID>", blob)
        self.assertIn("<REDACTED_BLOB>", blob)
        self.assertIn("example.com", blob)
        self.assertIn("ndc = 100000001", blob)
        # Durations and scaling constants are not identifiers.
        self.assertIn("timefrom:now-604800s", blob)
        self.assertIn("Acmecluster-prod-us-east-1", blob)

    def test_build_refuses_to_write_when_redaction_is_incomplete(self):
        src = {"widgets": [{"page": "P", "widget": "W", "confidence":
                            "exact", "visualization": "viz.line",
                            "grafana_type": "timeseries",
                            "nrql": ["SELECT count(*) FROM Transaction WHERE"
                                     " appName = concat('zz-top-backend (',"
                                     " {{env}}, ')')"],
                            "nr2grafana_queries": [], "live_targets": [],
                            "notes": []}]}
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "corpus")
            with mock.patch.object(ct.RedactionMap, "apply",
                                   lambda self, text: text):
                with self.assertRaises(RuntimeError) as cm:
                    ct.build_corpus(src, out)
            self.assertIn("redaction incomplete", str(cm.exception))
            self.assertNotIn("zz-top", str(cm.exception))
            self.assertFalse(os.path.exists(out))

    def test_build_writes_index_and_pages(self):
        src = {"widgets": [{"page": "P", "widget": "W", "confidence":
                            "exact", "visualization": "viz.markdown",
                            "grafana_type": "text", "nrql": [],
                            "nr2grafana_queries": [], "live_targets": [],
                            "notes": []}]}
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "corpus")
            index = ct.build_corpus(src, out)
            self.assertEqual(index["widgets"], 1)
            self.assertEqual(sorted(os.listdir(out)),
                             ["index.json", "page-01.json"])
            self.assertEqual(ct.load_corpus(out)[0]["id"], "p01-w001")


# ---------------------------------------------------------------------------
# Normalization used by the agreement score
# ---------------------------------------------------------------------------

class NormalizeTests(unittest.TestCase):
    def test_label_order_and_whitespace(self):
        a = 'sum( rate(m_total{b="2", a="1"}[$__rate_interval]) )'
        b = 'sum(rate(m_total{a="1",b="2"}[$__rate_interval]))'
        self.assertEqual(ct.normalize_expr(a), ct.normalize_expr(b))

    def test_by_clause_position_and_order(self):
        a = 'sum(rate(m_total[5m])) by (b, a)'
        b = 'sum by (a,b) (rate(m_total[5m]))'
        self.assertEqual(ct.normalize_expr(a), ct.normalize_expr(b))

    def test_loose_unifies_windows(self):
        a = 'sum(increase(m_total[$__range]))'
        b = 'sum(increase(m_total[$__interval]))'
        self.assertNotEqual(ct.normalize_expr(a), ct.normalize_expr(b))
        self.assertEqual(ct.normalize_expr(a, True),
                         ct.normalize_expr(b, True))

    def test_quoted_commas_are_not_split(self):
        a = 'm{a="x,y",b="1"}'
        b = 'm{b="1",a="x,y"}'
        self.assertEqual(ct.normalize_expr(a), ct.normalize_expr(b))


# ---------------------------------------------------------------------------
# Current converter vs the corpus (lazy, seam-gated)
# ---------------------------------------------------------------------------

class ConverterCorpusTests(unittest.TestCase):
    """Each rule cites the failure class it guards."""

    @skip_unless(RENDER)
    def test_f1_zero_python_reprs_in_emitted_queries(self):
        hits = []
        for wid, targets in converted().items():
            for e in ct._exprs(targets):
                if ct.PY_REPR_RE.search(e):
                    hits.append("%s: %s" % (wid, e[:120]))
        self.assertEqual(hits, [], "\n".join(hits[:10]))

    @skip_unless(RENDER)
    def test_f1_concat_env_renders_as_env_var(self):
        bad = []
        for w in widgets():
            if not ct.has_concat_env(w) or w["id"] not in converted():
                continue
            targets = converted()[w["id"]]
            if ct.widget_confidence(targets) == "untranslatable" or \
                    ct.has_parse_failure(targets):
                continue
            joined = _joined(targets)
            # F10 strips ' ($env)' off appName values, so the variable may
            # leave the expression; it must then be recorded in vars.
            uses_var = any("env" in (t.get("vars") or []) for t in targets)
            if "$env" not in joined and "${env" not in joined \
                    and not uses_var:
                bad.append("%s: %s" % (w["id"], joined[:120]))
        self.assertEqual(bad, [], "\n".join(bad[:10]))

    @skip_unless(RENDER, KIND)
    def test_f2_counters_get_total_with_increase_or_rate(self):
        simple = re.compile(r"^\s*SELECT\s+(?:sum|count)\(\s*[A-Za-z_][\w.]*"
                            r"\s*\)\s*(?:AS\s+\S+\s*)?FROM\s+Metric\b", re.I)
        bad, simple_bad, total = [], [], 0
        for w in widgets():
            if not ct.is_counter_case(w) or w["id"] not in converted():
                continue
            targets = converted()[w["id"]]
            if ct.has_parse_failure(targets):
                continue
            total += 1
            joined = _joined(targets)
            ok = re.search(r"\b(?:increase|rate)\([^\n]*_total", joined)
            if not ok:
                bad.append("%s: %s" % (w["id"], joined[:120]))
                if all(simple.match(q) for q in w["nrql"]):
                    simple_bad.append(w["id"])
        self.assertGreater(total, 30)
        self.assertEqual(simple_bad, [], "plain sum()/count() of an event "
                         "metric must become increase/rate of _total:\n"
                         + "\n".join(bad[:10]))
        self.assertLessEqual(len(bad), total * 0.10,
                             "%d/%d counter cases wrong:\n%s"
                             % (len(bad), total, "\n".join(bad[:10])))

    @skip_unless(KIND)
    def test_f5_summary_metrics_become_sum_over_count(self):
        bad, total = [], 0
        for w in widgets():
            if not ct.is_summary_case(w) or w["id"] not in converted():
                continue
            total += 1
            joined = _joined(converted()[w["id"]])
            if not ("_sum" in joined and "_count" in joined):
                bad.append("%s: %s" % (w["id"], joined[:120]))
        self.assertGreater(total, 5)
        self.assertEqual(bad, [], "\n".join(bad[:10]))

    @skip_unless(K8S)
    def test_f3_k8s_metrics_mapped_to_kube_state_metrics(self):
        bad, total = [], 0
        for w in widgets():
            if not ct.is_k8s_case(w) or w["id"] not in converted():
                continue
            exp = "\n".join(e.get("expr") or "" for e in w["expected"])
            if not re.search(r"\b(?:kube_|container_|node_)", exp):
                continue  # the human kept an NR-named metric here
            if ct.has_parse_failure(converted()[w["id"]]):
                continue  # F6 territory
            total += 1
            joined = _joined(converted()[w["id"]])
            if re.search(r"\bk8s_", joined) or not re.search(
                    r"\b(?:kube_|container_|node_)", joined):
                bad.append("%s: %s" % (w["id"], joined[:120]))
        self.assertGreater(total, 20)
        self.assertEqual(bad, [], "\n".join(bad[:10]))

    @skip_unless(CW)
    def test_f4_aws_metrics_become_cloudwatch_targets(self):
        bad, total = [], 0
        for w in widgets():
            if not ct.is_aws_case(w) or w["id"] not in converted():
                continue
            targets = converted()[w["id"]]
            if ct.has_parse_failure(targets):
                continue  # F6 territory
            total += 1
            if not any(t.get("cw") for t in targets):
                bad.append("%s: %s" % (w["id"], _joined(targets)[:120]))
                continue
            for t in targets:
                if t.get("cw"):
                    self.assertEqual(t["datasource"], "cloudwatch", w["id"])
        self.assertGreater(total, 20)
        self.assertEqual(bad, [], "\n".join(bad[:10]))

    @skip_unless(CW)
    def test_f4_cloudwatch_namespace_metric_statistic_match_humans(self):
        bad, total = [], 0
        for w in widgets():
            if w["id"] not in converted():
                continue
            exp_cw = [e for e in w["expected"]
                      if e["datasource"] == "cloudwatch" and
                      e.get("namespace")]
            if not exp_cw or ct.has_parse_failure(converted()[w["id"]]):
                continue
            got = [t["cw"] for t in converted()[w["id"]] if t.get("cw")]
            keys = set((c.get("namespace"), c.get("metricName"),
                        c.get("statistic")) for c in got)
            for e in exp_cw:
                total += 1
                key = (e["namespace"], e["metricName"], e["statistic"])
                if key not in keys:
                    bad.append("%s: want %r got %r"
                               % (w["id"], key, sorted(keys)))
        self.assertGreater(total, 20)
        self.assertEqual(bad, [], "\n".join(bad[:10]))

    @skip_unless(CW)
    def test_f4_cloudwatch_facet_becomes_wildcard_dimension(self):
        # FACET aws.<svc>.<Dim> -> dimensions[Dim] = ["*"]. (WHERE-derived
        # dimensions follow the contract, not the human's wildcard.)
        facet_rx = re.compile(r"\bFACET\s+(?:`)?aws\.[a-z0-9]+\.(\w+)", re.I)
        bad, total = [], 0
        for w in widgets():
            if w["id"] not in converted():
                continue
            fix = _seam("nr2grafana.translate.cloudwatch",
                        "fix_dimension_case") or (lambda d: d)
            facets = set(fix(d) for q in w["nrql"]
                         for d in facet_rx.findall(q))
            exp_cw = [e for e in w["expected"]
                      if e["datasource"] == "cloudwatch" and
                      set(e.get("dimension_keys") or []) & facets]
            if not exp_cw or ct.has_parse_failure(converted()[w["id"]]):
                continue
            total += 1
            got_keys = set()
            for t in converted()[w["id"]]:
                cw = t.get("cw") or {}
                got_keys.update(cw.get("dimension_keys") or [])
                got_keys.update((cw.get("dimensions") or {}).keys())
            for e in exp_cw:
                for k in e["dimension_keys"]:
                    if k in facets and k not in got_keys:
                        bad.append("%s: dimension %s missing (have %s)"
                                   % (w["id"], k, sorted(got_keys)))
        self.assertGreater(total, 3)
        self.assertEqual(bad, [], "\n".join(bad[:10]))

    def test_f6_bare_boolean_and_multi_event_from_parse(self):
        probes = ["SELECT count(*) FROM Log WHERE cluster_name = 'x' AND "
                  "should_publish",
                  "SELECT count(*) FROM Log, Log_dev WHERE level = 'ERROR'"]
        if not all(_parser_handles(p) for p in probes):
            self.skipTest("parser seam (bare boolean / multi-event FROM) "
                          "not landed yet")
        # Nested sub-selects are out of scope for the parser; everything
        # else (scientific notation, bare booleans, multi-event FROM,
        # comments, two SELECTs, dropped WHERE fragments) must parse.
        subselect = re.compile(r"\(\s*select\b", re.I)
        bad = []
        for w in widgets():
            if w["id"] not in converted() or any(
                    subselect.search(q) for q in w["nrql"]):
                continue
            targets = converted()[w["id"]]
            if ct.has_parse_failure(targets):
                note = next(n for t in targets for n in t["notes"]
                            if "could not be parsed" in n or "DROPPED" in n)
                bad.append("%s: %s" % (w["id"], note[:140]))
        self.assertEqual(bad, [], "\n".join(bad[:14]))

    def test_f7_loki_search_aparse_capture_translate(self):
        probe = ("SELECT count(*) FROM Log WHERE allColumnSearch('t', "
                 "insensitive: true) FACET aparse(message, '%[TOPIC:*]%')")
        if not _parser_handles(probe):
            self.skipTest("parser seam (allColumnSearch/aparse) not landed")
        bad, total = [], 0
        for w in widgets():
            if not ct.is_log_search_case(w) or w["id"] not in converted():
                continue
            if any("filter(" in q for q in w["nrql"]):
                continue  # filter() ratios are a separate (harder) case
            targets = converted()[w["id"]]
            if ct.has_parse_failure(targets):
                continue
            total += 1
            joined = _joined(targets)
            if ct.widget_confidence(targets) == "untranslatable" or \
                    not re.search(r"\|~|\bregexp\b|\(\?i\)", joined):
                bad.append("%s: %s" % (w["id"], joined[:120]))
        self.assertGreater(total, 5)
        self.assertEqual(bad, [], "\n".join(bad[:10]))

    @skip_unless(KIND)
    def test_f10_app_env_suffix_not_kept_in_label_values(self):
        bad, total = [], 0
        for w in widgets():
            if not ct.is_app_env_case(w) or w["id"] not in converted():
                continue
            if ct.has_parse_failure(converted()[w["id"]]):
                continue
            total += 1
            joined = _joined(converted()[w["id"]])
            if re.search(r'="[^"]* \([^"]*"', joined):
                bad.append("%s: %s" % (w["id"], joined[:120]))
        self.assertGreater(total, 5)
        self.assertEqual(bad, [], "\n".join(bad[:10]))

    @skip_unless(BIND)
    def test_f12_untranslatable_become_manual_with_closest_equivalent(self):
        res = ct.builder_checks(widgets(), ct.default_cfg())
        if not res.get("available"):
            self.skipTest(res.get("reason", "builder unavailable"))
        self.assertEqual(res["untranslatable_marked_manual"],
                         res["untranslatable_entries"])
        self.assertEqual(res["untranslatable_with_closest_equivalent"],
                         res["untranslatable_entries"])
        self.assertGreaterEqual(res["manual_panels"],
                                res["untranslatable_entries"])

    @skip_unless(BIND)
    def test_f8_export_binds_every_datasource_ref(self):
        res = ct.builder_checks(widgets(), ct.default_cfg())
        if not res.get("available") or not res.get("bind_available"):
            self.skipTest("builder/bind unavailable")
        self.assertEqual(res["unbound_after_bind"], [])

    @skip_unless(*ALL_SEAMS)
    def test_confidence_thresholds(self):
        res = ct.score(widgets(), with_builder=False)
        nr = res["confidence"]["needs-review"]["share"]
        un = res["confidence"]["untranslatable"]["share"]
        self.assertLessEqual(nr, ct.NEEDS_REVIEW_MAX,
                             "needs-review share %.1f%% > %.0f%%\n%s"
                             % (nr * 100, ct.NEEDS_REVIEW_MAX * 100,
                                ct.format_report(res)))
        self.assertLessEqual(un, ct.UNTRANSLATABLE_MAX,
                             "untranslatable share %.1f%% > %.0f%%\n%s"
                             % (un * 100, ct.UNTRANSLATABLE_MAX * 100,
                                ct.format_report(res)))

    @skip_unless(*ALL_SEAMS)
    def test_agreement_with_human_targets_floor(self):
        res = ct.score(widgets(), with_builder=False)
        ag = res["agreement"]
        self.assertGreaterEqual(
            ag["loose_share"], ct.AGREEMENT_FLOOR,
            "agreement with human targets %.1f%% (%d/%d) below the %.0f%% "
            "floor\n%s" % (ag["loose_share"] * 100, ag["loose_matches"],
                           ag["human_targets"], ct.AGREEMENT_FLOOR * 100,
                           ct.format_report(res)))


class ScoreTests(unittest.TestCase):
    def test_score_is_well_formed_and_deterministic(self):
        res = ct.score(widgets(), with_builder=False)
        self.assertEqual(res["widgets_total"], 531)
        self.assertEqual(res["widgets_queried"], 498)
        self.assertEqual(list(res["failure_classes"]),
                         list(ct.FAILURE_CLASSES))
        self.assertEqual(sum(v["count"] for v in res["confidence"].values()),
                         498)
        again = ct.score(widgets(), with_builder=False)
        self.assertEqual(res["per_widget"], again["per_widget"])
        text = ct.format_report(res, show="F2", limit=2)
        self.assertIn("failure classes still present", text)
        self.assertIn("agreement vs human targets", text)

    def test_failure_classes_detect_contract_examples(self):
        w = {"id": "x", "nrql": ["SELECT sum(acme_backend.order.created) "
                                 "FROM Metric WHERE cluster = concat('acme-"
                                 "cluster-', {{env}})"],
             "expected": [{"datasource": "prometheus", "expr":
                           "sum(increase(acme_backend_order_created_total"
                           "[$__range]))"}]}
        before = [{"sibling": False, "datasource": "prometheus",
                   "type": "instant", "confidence": "needs-review",
                   "expr": "avg_over_time(acme_backend_order_created{cluster"
                           "=\"Func(name='concat', args=[Lit(value='acme-"
                           "cluster-')])\"}[$__range])",
                   "legend": "", "notes": [], "cw": None,
                   "closest_equivalent": None}]
        self.assertEqual(ct.failure_classes(w, before), {"F1", "F2"})
        after = [dict(before[0], expr="sum(increase(acme_backend_order_"
                                       "created_total{cluster=\"acme-cluster-"
                                       "$env\"}[$__range]))")]
        self.assertEqual(ct.failure_classes(w, after), set())
        cmp_ = ct.compare_targets(w, after)
        self.assertEqual((cmp_["expected"], cmp_["loose"]), (1, 0))
        after2 = [dict(after[0], expr="sum(increase(acme_backend_order_"
                                       "created_total[$__range]))")]
        self.assertEqual(ct.compare_targets(w, after2)["strict"], 1)


if __name__ == "__main__":
    unittest.main()
