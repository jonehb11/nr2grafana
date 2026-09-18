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


_FLOWLOGS = {
    "schema": "nr2grafana/flowlogs/v1",
    "dominant_port": 9095,
    "gb_per_day": 16470.0,
    "cross_az_pct": 0.99,
    "step_change_date": "2026-08-31",
    "drivers": [
        {"driver": "mimir/loki gRPC", "port": 9095,
         "gb_per_day": 14990.0, "pct_of_cross_az": 0.91},
        {"driver": "nlb cross-zone", "port": 443,
         "gb_per_day": 1318.0, "pct_of_cross_az": 0.08},
    ],
    "top_flows": [
        {"srcAddr": "10.0.1.10", "dstAddr": "10.0.2.20", "dstPort": 9095,
         "az_pair": "us-east-1a->us-east-1b", "gb_per_day": 5000.0},
    ],
}

_RCA = {
    "schema": "nr2grafana/rca/v1",
    "incident": {
        "usage_type": "USE1-DataTransfer-Regional-Bytes",
        "service": "EBS", "account": "348342704569",
        "region": "us-east-1", "usd_per_day": 164.0,
        "gb_per_day": 16470.0, "hypothesis_class": "CROSS_AZ_NETWORK",
        "step_change_date": "2026-08-31", "score": 0.98,
    },
    "cause": {
        "dominant": {
            "share": 0.91,
            "driver": "non-zone-aware LGTM ring replication + fan-out",
            "summary": "RF=3 gRPC (port 9095) crosses AZ boundaries; "
                       "ring confined to 2 imbalanced AZs.",
            "evidence": [
                "vpc-flow-logs: port 9095 = 91% of cross-AZ bytes",
                "eks-control-plane: mimir/loki ingesters in 2 AZs",
                "lgtm-self-metrics: ring non-zone-aware, RF=3"],
        },
        "secondary": [
            {"share": 0.08, "driver": "cross-zone-enabled Mimir NLB",
             "summary": "NLB routes across AZs incurring transfer."},
        ],
        "ruled_out": [
            {"hypothesis": "EBS storage growth",
             "evidence": "ce storage usage-type flat; volume/snapshot "
                         "count flat across the step-change"},
            {"hypothesis": "RDS cross-AZ replica",
             "evidence": "no Multi-AZ replica; no matching RDS ENIs"},
        ],
    },
    "evidence_convergence": ["cost-explorer", "cloudtrail",
                             "vpc-flow-logs", "eks-control-plane",
                             "lgtm-self-metrics"],
    "confidence": "high",
}

_MITIGATION = {
    "schema": "nr2grafana/mitigation/v1",
    "mitigations": [
        {"title": "Enable Mimir/Loki zone-aware replication",
         "change": "Set zone_awareness_enabled + zone labels; migrate "
                   "ring zone-by-zone via rollout-operator.",
         "owner": "GitOps/IaC -- proposal only, never executed",
         "est_savings": {"usd_per_day": 149.0, "pct_saved": 0.91},
         "keeps_availability": True, "keeps_durability": True,
         "keeps_performance": True, "handles_current_traffic": True,
         "reliability_guardrails": [
             "deploy across >= RF zones (RF=3 -> 3 AZs) or writes fail",
             "roll one zone at a time; existing PDB maxUnavailable:0",
             "double max-series/stream limits before reshuffle"],
         "config": [{"target": "mimir-values", "language": "yaml",
                     "snippet": "z" * 2000},
                    {"target": "loki-values", "language": "yaml",
                     "snippet": "z" * 2000}]},
        {"title": "Disable cross-zone on the Mimir NLB",
         "change": "load_balancing.cross_zone.enabled=false w/ gates.",
         "owner": "GitOps/IaC -- proposal only, never executed",
         "est_savings": {"usd_per_day": 13.0, "pct_saved": 0.08},
         "keeps_availability": False, "keeps_durability": True,
         "keeps_performance": True, "handles_current_traffic": True,
         "reliability_guardrails": [
             "black-hole risk: confirm >=1 healthy target in EVERY "
             "enabled AZ before disabling cross-zone"],
         "config": [{"target": "nlb-service", "language": "yaml",
                     "snippet": "z" * 2000}]},
    ],
    "summary": {"count": 2, "total_usd_per_day_saved": 162.0},
    "total_savings": {"usd_per_day": 162.0, "pct": 0.99},
}


_WIDGET_REPORT = {
    "widgets": [
        {"panel_id": 1, "widget": "RPS", "confidence": "exact",
         "nrql": ["SELECT rate(count(*), 1 minute) FROM Transaction"],
         "notes": []},
        {"panel_id": 2, "widget": "Apdex", "confidence": "untranslatable",
         "nrql": ["SELECT apdex(duration, t: 0.5) FROM Transaction"],
         "notes": ["apdex() has no LGTM equivalent",
                   "needs a from-scratch histogram query"]},
        {"panel_id": 3, "widget": "P95", "confidence": "needs-review",
         "nrql": ["SELECT percentile(duration, 95) FROM Transaction"],
         "notes": ["percentile mapped to histogram_quantile approx"]},
    ]
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
    store.save_artifact("svc-1", "flowlogs", _FLOWLOGS)
    store.save_artifact("svc-1", "rca", _RCA)
    store.save_artifact("svc-1", "mitigation", _MITIGATION)
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
        # The raw optimize/deepdive/mitigation artifacts carry 2000-char
        # snippets; the compact bundle must not embed them.
        self.assertNotIn("x" * 500, blob)
        self.assertNotIn("y" * 500, blob)
        self.assertNotIn("z" * 500, blob)
        # Config is represented by target labels only.
        opt = ctx["artifacts"]["optimize"]["top_recommendations"][0]
        self.assertIn("config_targets", opt)
        self.assertTrue(any("prometheus-relabel" in t
                            for t in opt["config_targets"]))
        # The whole bundle is far smaller than the raw artifacts.
        raw_total = sum(len(json.dumps(a)) for a in (
            _REQUIREMENTS, _DIAGNOSIS, _PARITY, _SAMPLES, _COST,
            _OPTIMIZE, _DEEPDIVE, _PACKING, _FLOWLOGS, _RCA,
            _MITIGATION))
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

    # -- per-panel translations (SEAM-1 fields) ------------------------

    def test_translations_carry_nrql_and_notes(self):
        self.store.save_artifact("svc-1", "widget-report", _WIDGET_REPORT)
        ctx = aicontext.build_context(self.store, "svc-1")
        self.assertIn("translations", ctx)
        rows = ctx["translations"]
        # The exact panel is omitted; only the flagged panels remain.
        self.assertEqual(sorted(r["panel_id"] for r in rows), [2, 3])
        # Worst confidence first (untranslatable before needs-review).
        self.assertEqual(rows[0]["panel_id"], 2)
        self.assertEqual(rows[0]["confidence"], "untranslatable")
        self.assertIn("apdex(duration", rows[0]["original_nrql"][0])
        self.assertTrue(any("no LGTM equivalent" in n
                            for n in rows[0]["translation_notes"]))
        self.assertEqual(rows[1]["confidence"], "needs-review")

    def test_translations_absent_without_widget_report(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        self.assertNotIn("translations", ctx)

    def test_translations_deterministic(self):
        self.store.save_artifact("svc-1", "widget-report", _WIDGET_REPORT)
        a = aicontext.build_context(self.store, "svc-1")
        b = aicontext.build_context(self.store, "svc-1")
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))

    def test_translations_rendered_in_markdown(self):
        self.store.save_artifact("svc-1", "widget-report", _WIDGET_REPORT)
        ctx = aicontext.build_context(self.store, "svc-1")
        md = aicontext.to_markdown(ctx)
        self.assertIn("Panel translations to review", md)
        self.assertIn("apdex(duration", md)
        self.assertIn("no LGTM equivalent", md)

    def test_translations_legend_entry(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        self.assertIn("translations", ctx["legend"])

    # -- cost-anomaly trio: flowlogs / rca / mitigation ----------------

    def test_cost_trio_in_order_and_legend(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        for kind in ("flowlogs", "rca", "mitigation"):
            self.assertIn(kind, aicontext.ARTIFACT_ORDER)
            self.assertIn(kind, ctx["artifacts"], kind)
            self.assertIn(kind, ctx["legend"], kind)
        # rca must follow flowlogs, mitigation must follow rca.
        order = list(aicontext.ARTIFACT_ORDER)
        self.assertLess(order.index("flowlogs"), order.index("rca"))
        self.assertLess(order.index("rca"), order.index("mitigation"))

    def test_flowlogs_summary(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        fl = ctx["artifacts"]["flowlogs"]
        self.assertEqual(fl["dominant_port"], 9095)
        self.assertEqual(fl["gb_per_day"], 16470.0)
        self.assertEqual(fl["step_change_date"], "2026-08-31")
        # The dominant driver carries its %-of-cross-AZ share.
        top = fl["drivers"][0]
        self.assertEqual(top["port"], 9095)
        self.assertEqual(top["pct_of_cross_az"], 0.91)
        self.assertTrue(fl["top_flows"])

    def test_flowlogs_degrades_to_note(self):
        self.store.save_artifact("svc-1", "flowlogs", {
            "schema": "nr2grafana/flowlogs/v1",
            "note": "no flow logs configured for this VPC"})
        ctx = aicontext.build_context(self.store, "svc-1")
        self.assertIn("note", ctx["artifacts"]["flowlogs"])
        self.assertNotIn("drivers", ctx["artifacts"]["flowlogs"])

    def test_rca_summary(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        rca = ctx["artifacts"]["rca"]
        inc = rca["incident"]
        self.assertEqual(inc["usage_type"],
                         "USE1-DataTransfer-Regional-Bytes")
        self.assertEqual(inc["usd_per_day"], 164.0)
        self.assertEqual(inc["hypothesis_class"], "CROSS_AZ_NETWORK")
        dom = rca["dominant"]
        self.assertEqual(dom["share"], 0.91)
        self.assertTrue(dom["evidence"])
        # Secondary and ruled-out are preserved.
        self.assertEqual(rca["secondary"][0]["share"], 0.08)
        hyps = [r["hypothesis"] for r in rca["ruled_out"]]
        self.assertIn("EBS storage growth", hyps)
        self.assertIn("cost-explorer", rca["evidence_convergence"])
        self.assertEqual(rca["confidence"], "high")

    def test_rca_degrades_on_unknown_shape(self):
        self.store.save_artifact("svc-1", "rca", {
            "schema": "nr2grafana/rca/v1", "mystery": 1})
        ctx = aicontext.build_context(self.store, "svc-1")
        self.assertIn("keys", ctx["artifacts"]["rca"])

    def test_mitigation_summary_flags_and_configs(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        mit = ctx["artifacts"]["mitigation"]
        m0 = mit["mitigations"][0]
        self.assertIn("zone-aware", m0["title"])
        # keeps_* land under the risk flags; handles_current_traffic too.
        self.assertTrue(m0["risk"]["keeps_availability"])
        self.assertTrue(m0["handles_current_traffic"])
        self.assertTrue(m0["reliability_guardrails"])
        self.assertIn("owner", m0)
        # Config is represented by target labels only, never the snippet.
        self.assertTrue(any("mimir-values" in t
                            for t in m0["config_targets"]))
        self.assertEqual(m0["est_savings"]["usd_per_day"], 149.0)
        # The unsafe NLB disable keeps_availability=false is preserved.
        m1 = mit["mitigations"][1]
        self.assertFalse(m1["risk"]["keeps_availability"])
        self.assertTrue(any("black-hole" in g
                            for g in m1["reliability_guardrails"]))

    def test_mitigation_preserves_planner_ranking(self):
        # A demoted (unsafe) item stays where the planner put it.
        ctx = aicontext.build_context(self.store, "svc-1")
        titles = [m["title"]
                  for m in ctx["artifacts"]["mitigation"]["mitigations"]]
        self.assertEqual(titles[0],
                         "Enable Mimir/Loki zone-aware replication")

    def test_cost_trio_rendered_in_markdown(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        md = aicontext.to_markdown(ctx)
        for kind in ("flowlogs", "rca", "mitigation"):
            self.assertIn("## %s" % kind, md)
        self.assertIn("USE1-DataTransfer-Regional-Bytes", md)
        self.assertIn("zone-aware replication", md)
        # No raw config snippet leaks into the rendered markdown.
        self.assertNotIn("z" * 500, md)

    # -- analyze_cost (RCA/mitigation AI flow) -------------------------

    _PLAN_JSON = json.dumps({
        "root_cause": "cross-AZ ring replication on port 9095",
        "mitigations": [
            {"title": "zone-aware Mimir/Loki", "saving": "$149/day",
             "change": "enable zone_awareness_enabled",
             "preconditions": "deploy across >= RF zones",
             "keeps_availability": True, "keeps_durability": True,
             "keeps_performance": True}],
        "config_notes": "generic placeholders only"})

    def test_analyze_cost_happy_parses_plan(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        fake = _FakeAssistant(reply=self._PLAN_JSON)
        out = aicontext.analyze_cost(fake, ctx)
        self.assertEqual(out["backend"], "_FakeAssistant")
        self.assertIn("plan", out)
        self.assertIn("root_cause", out["plan"])
        self.assertEqual(len(out["plan"]["mitigations"]), 1)
        # The RCA system framing was used and the bundle was carried.
        self.assertIn("DataTransfer-Regional-Bytes", fake.system)
        self.assertIn("CROSS-AZ NETWORK", fake.system)
        self.assertIn("# nr2grafana AI context", fake.seen[0]["content"])

    def test_analyze_cost_default_question_is_rca(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        fake = _FakeAssistant(reply="ok")
        aicontext.analyze_cost(fake, ctx)
        self.assertIn("reliability-safe", fake.seen[0]["content"])

    def test_analyze_cost_parses_fenced_plan(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        fenced = "```json\n" + self._PLAN_JSON + "\n```"
        fake = _FakeAssistant(reply=fenced)
        out = aicontext.analyze_cost(fake, ctx)
        self.assertIn("plan", out)
        self.assertIn("root_cause", out["plan"])

    def test_analyze_cost_prose_reply_has_no_plan(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        fake = _FakeAssistant(reply="Here is the analysis in prose.")
        out = aicontext.analyze_cost(fake, ctx)
        self.assertEqual(out["answer"], "Here is the analysis in prose.")
        self.assertNotIn("plan", out)

    def test_analyze_cost_no_backend(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        out = aicontext.analyze_cost(None, ctx)
        self.assertEqual(out["backend"], "none")
        self.assertIn("No AI backend", out["answer"])

    def test_analyze_cost_unavailable_backend(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        out = aicontext.analyze_cost(_FakeAssistant(available=False), ctx)
        self.assertEqual(out["backend"], "none")

    def test_analyze_cost_backend_error_is_text(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        out = aicontext.analyze_cost(_FakeAssistant(boom=True), ctx)
        self.assertIn("could not analyze", out["answer"])
        self.assertIn("api exploded", out["answer"])
        self.assertNotIn("plan", out)

    def test_analyze_cost_empty_reply(self):
        ctx = aicontext.build_context(self.store, "svc-1")
        out = aicontext.analyze_cost(_FakeAssistant(reply="   "), ctx)
        self.assertIn("empty reply", out["answer"])

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
