"""Tests for nr2grafana.aicontext (the AI-first context bundle).

The bundle must be COMPACT (summaries + top-N, not raw dumps), STABLE
(deterministic, no wall-clock timestamp), and SAFE (secret-looking
values redacted). troubleshoot() must never raise -- AI errors turn
into actionable text.
"""

import json
import os
import tempfile
import unittest

from nr2grafana import aicontext
from nr2grafana.store import Store


# ---------------------------------------------------------------------------
# fixtures: a dashboard + one artifact of every kind
# ---------------------------------------------------------------------------

_DASH = {
    "title": "Service Overview",
    "uid": "svc-1",
    "panels": [
        {"id": 1, "title": "RPS", "targets": [{
            "refId": "A",
            "datasource": {"type": "prometheus", "uid": "p"},
            "expr": "sum(rate(http_requests_total[5m]))"}]},
        {"id": 2, "title": "Logs", "targets": [{
            "refId": "A",
            "datasource": {"type": "loki", "uid": "l"},
            "expr": '{app="api"}'}]},
    ],
}

_REQUIREMENTS = {
    "schema": "nr2grafana/requirements/v1",
    "dashboard": "Service Overview",
    "datasources": [{"family": "prometheus", "uid": "p"},
                    {"family": "loki", "uid": "l"}],
    "plugins": [{"id": "grafana-piechart-panel"}],
    "domains": [{"domain": "http"}, {"domain": "logs"}],
    "nr_native": [{"feature": "billboard sparkline"}],
}

_DIAGNOSIS = {
    "schema": "nr2grafana/diagnosis/v1",
    "generated_at": "2026-01-01T00:00:00Z",
    "findings": [
        {"id": "d1", "severity": "info", "area": "config",
         "problem": "minor cosmetic gap", "fix": "ignore"},
        {"id": "d2", "severity": "blocker", "area": "datasource",
         "problem": "prometheus datasource missing",
         "fix": "add the prometheus datasource", "panel_id": 1},
        {"id": "d3", "severity": "warn", "area": "panel",
         "problem": "unit mismatch", "fix": "set unit to reqps"},
    ],
    "summary": {"findings": 3, "blocker": 1, "warn": 1, "info": 1,
                "by_area": {"datasource": 1, "panel": 1, "config": 1}},
}

_PARITY = {
    "schema": "nr2grafana/parity/v1",
    "score": 67,
    "panels": [
        {"panel_title": "RPS", "refId": "A", "verdict": "match",
         "detail": "within 1%"},
        {"panel_title": "Logs", "refId": "A", "verdict": "mismatch",
         "detail": "grafana returned no rows"},
    ],
    "summary": {"match": 1, "mismatch": 1},
}

_SAMPLES = {
    "schema": "nr2grafana/samples/v1",
    "panels": [
        {"panel_title": "RPS", "refId": "A",
         "nr": {"kind": "timeseries"}, "grafana": {"kind": "timeseries"}},
        {"panel_title": "Logs", "refId": "A",
         "nr": {"kind": "logs"}, "grafana": {"kind": "empty"}},
    ],
}

_COST = {
    "schema": "nr2grafana/cost/v1",
    "monthly_total": 993.0,
    "components": [
        {"name": "mimir", "family": "prometheus", "monthly_cost": 700.0},
        {"name": "loki", "family": "loki", "monthly_cost": 200.0},
        {"name": "tempo", "family": "tempo", "monthly_cost": 93.0},
    ],
    "resources": {"mimir_ram_gb_est": 30.0},
}

_OPTIMIZE = {
    "schema": "nr2grafana/optimize/v1",
    "recommendations": [
        {"family": "prometheus", "kind": "drop-metric", "severity": "high",
         "title": "Drop unused metric apiserver_request_duration_bucket",
         "keeps_intact": True, "needs_review": False,
         "est_savings": {"monthly_usd": 120.0, "series": 40000},
         "config": [{"target": "prometheus-relabel", "language": "yaml",
                     "snippet": "x" * 2000}]},
        {"family": "prometheus", "kind": "drop-label", "severity": "medium",
         "title": "Review high-cardinality label pod",
         "keeps_intact": False, "needs_review": True,
         "est_savings": {"monthly_usd": 30.0}},
    ],
    "summary": {"total_est_monthly_usd": 150.0, "count": 2,
                "safe_count": 1, "needs_review_count": 1},
}

_DEEPDIVE = {
    "schema": "nr2grafana/deepdive/v1",
    "findings": [
        {"severity": "WARN", "area": "capacity",
         "title": "Ingesters near real series ceiling",
         "rationale": "GOMEMLIMIT-based capacity is ~4M, not the "
                      "configured 10M limit.",
         "evidence": {"bytes_per_series": 4300, "rss_gib": 7.1},
         "keeps_performance": True, "keeps_durability": True,
         "keeps_availability": True,
         "config": [{"target": "mimir-limits", "language": "yaml",
                     "snippet": "y" * 2000}]},
        {"severity": "FAIL", "area": "cardinality",
         "title": "Duplicate Prometheus replicas double ingest",
         "rationale": "Two replicas, no HA tracker.",
         "evidence": {"replicas": 2},
         "est_savings": {"monthly_usd": 50.0, "series": 100000},
         "keeps_performance": True, "keeps_durability": True,
         "keeps_availability": True},
    ],
    "summary": {"findings": 2, "fail": 1, "warn": 1},
}

_PACKING = {
    "schema": "nr2grafana/packing/v1",
    "packing_sim": {
        "floor": "r6a.2xlarge",
        "candidates": [
            {"shape": "r6a.2xlarge", "nodes": 3, "monthly_usd": 993.0,
             "mem_util": 0.68},
            {"shape": "m6a.2xlarge", "nodes": 3, "monthly_usd": 757.0,
             "mem_util": 0.9},
        ],
    },
    "rightsizing": {"compute_saved": "4 cores", "monthly_usd": 500.0,
                    "keeps_performance": True},
    "durability": [
        {"severity": "FAIL", "title": "PDB allows 0 on mimir ingester"},
    ],
    "karpenter": {
        "findings": [
            {"severity": "WARN", "title": "m-class on memory-bound pool"},
        ],
        "proposed_nodepool_yaml": "apiVersion: karpenter.sh/v1\n...",
        "est_savings": {"monthly_usd": 500.0, "nodes": 3,
                        "keeps_availability": True},
    },
}


def _seed_store(path):
    store = Store(path)
    store.upsert_dashboard("svc-1", "Service Overview", "nr-acct",
                           "GUID", _DASH)
    store.save_artifact("svc-1", "requirements", _REQUIREMENTS)
    store.save_artifact("svc-1", "diagnosis", _DIAGNOSIS)
    store.save_artifact("svc-1", "parity", _PARITY)
    store.save_artifact("svc-1", "samples", _SAMPLES)
    store.save_artifact("svc-1", "cost", _COST)
    store.save_artifact("svc-1", "optimize", _OPTIMIZE)
    store.save_artifact("svc-1", "deepdive", _DEEPDIVE)
    store.save_artifact("svc-1", "packing", _PACKING)
    return store


class _FakeAssistant(object):
    """Duck-typed AIAssist: records the prompt, returns a canned reply."""

    def __init__(self, reply="ANSWER", available=True, boom=False):
        self.reply = reply
        self.available = available
        self.boom = boom
        self.seen = None
        self.system = None

    def chat(self, messages, system=""):
        self.seen = messages
        self.system = system
        if self.boom:
            raise RuntimeError("api exploded")
        return self.reply


class AiContextTest(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = _seed_store(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    # -- build_context -------------------------------------------------

    def test_schema_and_sections(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        self.assertEqual(ctx["schema"], "nr2grafana/ai-context/v1")
        self.assertIn("preamble", ctx)
        self.assertIn("legend", ctx)
        self.assertIsNotNone(ctx["dashboard"])
        self.assertEqual(ctx["dashboard"]["slug"], "svc-1")
        self.assertEqual(ctx["dashboard"]["panel_count"], 2)
        # Every artifact kind should be summarized.
        for kind in aicontext.ARTIFACT_ORDER:
            self.assertIn(kind, ctx["artifacts"], kind)
        self.assertEqual(ctx["available_artifacts"],
                         list(aicontext.ARTIFACT_ORDER))
        self.assertEqual(ctx["missing_artifacts"], [])

    def test_preamble_carries_safety_rules(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        pre = ctx["preamble"].lower()
        self.assertIn("read-only", pre)
        self.assertIn("peak", pre)
        for term in ("retention", "replication", "durability"):
            self.assertIn(term, pre)

    def test_compactness_top_n_and_no_raw_snippets(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        blob = json.dumps(ctx)
        # The raw optimize/deepdive artifacts carry 2000-char snippets;
        # the compact bundle must not embed them.
        self.assertNotIn("x" * 500, blob)
        self.assertNotIn("y" * 500, blob)
        # Config is represented by target labels only.
        opt = ctx["artifacts"]["optimize"]["top_recommendations"][0]
        self.assertIn("config_targets", opt)
        self.assertTrue(any("prometheus-relabel" in t
                            for t in opt["config_targets"]))
        # The whole bundle is far smaller than the raw artifacts.
        raw_total = sum(len(json.dumps(a)) for a in (
            _REQUIREMENTS, _DIAGNOSIS, _PARITY, _SAMPLES, _COST,
            _OPTIMIZE, _DEEPDIVE, _PACKING))
        self.assertLess(len(blob), raw_total)

    def test_top_n_cap(self):
        many = {"schema": "nr2grafana/optimize/v1",
                "recommendations": [
                    {"family": "prometheus", "kind": "drop-metric",
                     "severity": "high", "title": "rec %d" % i,
                     "est_savings": {"monthly_usd": float(i)}}
                    for i in range(40)]}
        self.store.save_artifact("svc-1", "optimize", many)
        ctx = aicontext.build_context(self.store, "svc-1")
        recs = ctx["artifacts"]["optimize"]["top_recommendations"]
        self.assertLessEqual(len(recs), aicontext.TOP)

    def test_determinism(self):
        a = aicontext.build_context(self.store, "svc-1")
        b = aicontext.build_context(self.store, "svc-1")
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))
        # No wall-clock timestamp leaked into the bundle.
        self.assertNotIn("generated_at", a)

    def test_findings_ranked_by_severity(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        top = ctx["artifacts"]["diagnosis"]["top_findings"]
        self.assertEqual(top[0]["severity"], "blocker")
        # deepdive FAIL should outrank WARN.
        dfind = ctx["artifacts"]["deepdive"]["top_findings"]
        self.assertEqual(dfind[0]["severity"], "FAIL")

    def test_risk_flags_preserved(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        dfind = ctx["artifacts"]["deepdive"]["top_findings"]
        risk = dfind[0]["risk"]
        self.assertTrue(risk["keeps_durability"])
        self.assertTrue(risk["keeps_availability"])

    def test_include_filter(self):
        ctx = aicontext.build_context(self.store, "svc-1",
                                      include=["cost", "optimize"])
        self.assertEqual(sorted(ctx["artifacts"].keys()),
                         ["cost", "optimize"])
        self.assertIn("requirements", ctx["missing_artifacts"])

    def test_empty_slug_picks_first_dashboard(self):
        ctx = aicontext.build_context(self.store, "")
        self.assertIsNotNone(ctx["dashboard"])
        self.assertEqual(ctx["dashboard"]["slug"], "svc-1")

    def test_deepdive_arg_overrides_store_and_unpacks_packing(self):
        empty = Store(self.path + "-2")
        empty.upsert_dashboard("d", "D", "", "", {"panels": []})
        try:
            bundled = dict(_DEEPDIVE)
            bundled["packing"] = _PACKING
            ctx = aicontext.build_context(empty, "d", deepdive=bundled)
            self.assertIn("deepdive", ctx["artifacts"])
            self.assertIn("packing", ctx["artifacts"])
            self.assertIn("floor",
                          ctx["artifacts"]["packing"]["packing"])
        finally:
            empty.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(self.path + "-2" + suffix)
                except OSError:
                    pass

    def test_packing_degrades_on_unknown_shape(self):
        weird = {"schema": "nr2grafana/packing/v1", "mystery": 1}
        ctx = aicontext.build_context(self.store, "svc-1",
                                      deepdive={"packing": weird,
                                                "findings": []})
        self.assertIn("keys", ctx["artifacts"]["packing"])

    # -- redaction -----------------------------------------------------

    def test_redaction_by_key(self):
        # Evidence keys survive summarization, so a secret-looking key
        # there exercises the whole-bundle redaction pass.
        deep = {"schema": "nr2grafana/deepdive/v1", "findings": [
            {"severity": "WARN", "area": "capacity", "title": "t",
             "evidence": {"api_key": "supersecretvalue123",
                          "replicas": 2}}]}
        self.store.save_artifact("svc-1", "deepdive", deep)
        ctx = aicontext.build_context(self.store, "svc-1", redact=True)
        blob = json.dumps(ctx)
        self.assertNotIn("supersecretvalue123", blob)
        self.assertIn(aicontext.REDACTED, blob)

    def test_redact_function_direct(self):
        out = aicontext.redact({"password": "hunter2", "keep": "ok"})
        self.assertEqual(out["password"], aicontext.REDACTED)
        self.assertEqual(out["keep"], "ok")

    def test_redaction_by_value_pattern(self):
        self.store.save_artifact("svc-1", "requirements", {
            "schema": "nr2grafana/requirements/v1",
            "domains": [{"domain": "note sk-abcdEFGH1234567890xyz here"}]})
        ctx = aicontext.build_context(self.store, "svc-1", redact=True)
        self.assertNotIn("sk-abcdEFGH1234567890xyz", json.dumps(ctx))

    def test_redact_off(self):
        deep = {"schema": "nr2grafana/deepdive/v1", "findings": [
            {"severity": "WARN", "area": "capacity", "title": "t",
             "evidence": {"token": "plainsecret", "replicas": 2}}]}
        self.store.save_artifact("svc-1", "deepdive", deep)
        ctx = aicontext.build_context(self.store, "svc-1", redact=False)
        self.assertIn("plainsecret", json.dumps(ctx))

    def test_grafana_note_strips_userinfo(self):
        class G(object):
            base = "http://user:pass@localhost:3000"
        ctx = aicontext.build_context(self.store, "svc-1",
                                      grafana=G())
        self.assertIn("grafana", ctx)
        self.assertNotIn("pass", ctx["grafana"]["base_url"])
        self.assertIn("localhost:3000", ctx["grafana"]["base_url"])

    # -- markdown ------------------------------------------------------

    def test_markdown_shape(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        md = aicontext.to_markdown(ctx)
        self.assertTrue(md.startswith("# nr2grafana AI context"))
        self.assertIn("## Dashboard", md)
        self.assertIn("## Legend", md)
        for kind in aicontext.ARTIFACT_ORDER:
            self.assertIn("## %s" % kind, md)
        # A finding's title should appear in the rendered markdown.
        self.assertIn("prometheus datasource missing", md)
        self.assertIn("r6a.2xlarge", md)

    def test_markdown_no_raw_snippet(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        md = aicontext.to_markdown(ctx)
        self.assertNotIn("x" * 500, md)

    # -- prompt --------------------------------------------------------

    def test_prompt_includes_question(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        p = aicontext.to_prompt(ctx, "why is the Logs panel empty?")
        self.assertIn("## Question", p)
        self.assertIn("why is the Logs panel empty?", p)
        self.assertIn("# nr2grafana AI context", p)

    def test_prompt_default_question(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        p = aicontext.to_prompt(ctx)
        self.assertIn("## Question", p)
        self.assertIn("highest-priority", p)

    # -- troubleshoot --------------------------------------------------

    def test_troubleshoot_happy(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        fake = _FakeAssistant(reply="Fix the datasource first.")
        out = aicontext.troubleshoot(fake, ctx, "what first?")
        self.assertEqual(out["answer"], "Fix the datasource first.")
        self.assertEqual(out["backend"], "_FakeAssistant")
        # The prompt actually carried the bundle + question + system.
        self.assertIn("what first?", fake.seen[0]["content"])
        self.assertIn("SRE", fake.system)

    def test_troubleshoot_no_backend(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        out = aicontext.troubleshoot(None, ctx, "q")
        self.assertEqual(out["backend"], "none")
        self.assertIn("No AI backend", out["answer"])

    def test_troubleshoot_unavailable_backend(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        fake = _FakeAssistant(available=False)
        out = aicontext.troubleshoot(fake, ctx, "q")
        self.assertEqual(out["backend"], "none")

    def test_troubleshoot_backend_error_is_text(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        fake = _FakeAssistant(boom=True)
        out = aicontext.troubleshoot(fake, ctx, "q")
        self.assertIn("could not answer", out["answer"])
        self.assertIn("api exploded", out["answer"])

    def test_troubleshoot_empty_reply(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        fake = _FakeAssistant(reply="   ")
        out = aicontext.troubleshoot(fake, ctx, "q")
        self.assertIn("empty reply", out["answer"])


if __name__ == "__main__":
    unittest.main()
