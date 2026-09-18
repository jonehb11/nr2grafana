"""Tests for nr2grafana.mitigate -- the reliability-safe mitigation planner.

Two things are asserted hard:

1. Given an RCA that matches the reference cross-AZ investigation
   (DataTransfer-Regional-Bytes = cross-AZ ring on gRPC 9095, secondary
   cross-zone NLB), the planner reproduces the reference mitigation set
   (Mimir/Loki zone-aware, PreferClose, Karpenter 3-AZ subnet, gated NLB)
   with reliability guardrails present on every item, GENERIC paste-ready
   configs (placeholders, no customer values), and owner = GitOps proposal.

2. The SAFETY invariant: no mitigation is ever marked safe (or keeps_*=true)
   if it would reduce availability / durability / performance or the ability
   to serve the current traffic rate. Every mitigation's flags agree with
   :func:`nr2grafana.reliability.check`, and any change with a violation is
   demoted below the safe ones with a loud caveat.

The tests drive the real :mod:`nr2grafana.reliability` (no mocking) so the
planner is exercised against the guardrail library it consumes.
"""

import re
import unittest

from nr2grafana import mitigate, reliability
from nr2grafana.mitigate import SCHEMA, plan


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _rca(dom_share=0.91, sec_share=0.08, usd_per_day=164.0,
         gb_per_day=16470.0, dom_class="CROSS_AZ_NETWORK",
         with_secondary=True):
    """An RCA result shaped like nr2grafana/rca/v1 for the reference case."""
    cause = {
        "dominant": {
            "class": dom_class,
            "share": dom_share,
            "summary": "non-zone-aware LGTM ring; RF=3 + query fan-out "
                       "over gRPC 9095 confined to 2 imbalanced AZs",
            "evidence": ["vpc-flow-logs: port 9095 ~91% of cross-AZ bytes"],
        },
        "secondary": [],
        "ruled_out": ["EBS storage flat across the step-change"],
    }
    if with_secondary:
        cause["secondary"].append({
            "class": "NLB_CROSS_ZONE",
            "share": sec_share,
            "summary": "cross-zone-enabled Mimir NLB",
        })
    return {
        "schema": "nr2grafana/rca/v1",
        "incident": {
            "usage_type": "USE1-DataTransfer-Regional-Bytes",
            "service": "EBS",
            "account": "348342704569",
            "region": "us-east-1",
            "usd_per_day": usd_per_day,
            "gb_per_day": gb_per_day,
            "onset": "2026-08-31",
            "step_change": "2026-08-31",
        },
        "cause": cause,
        "evidence_convergence": ["vpc-flow-logs", "eks-control-plane",
                                 "lgtm-self-metrics"],
        "confidence": "HIGH",
    }


def _healthy_capacity():
    """A 3-AZ, balanced, healthy context: everything should be SAFE."""
    zones = ["us-east-1a", "us-east-1b", "us-east-1c"]
    return {
        "available_zones": zones,
        "healthy_zones": 3,
        "serving_zones": zones,
        "endpoint_zones": zones,
        "per_zone_replicas": {z: 3 for z in zones},
        "per_zone_required_replicas": {z: 2 for z in zones},
        "per_zone_current_rate": {z: 100 for z in zones},
        "per_zone_capacity_after": {z: 150 for z in zones},
        "nlb_target_health": {z: {"healthy": 3} for z in zones},
        "nlb_enabled_azs": zones,
    }


def _by_kind(plan_result):
    return {m["kind"]: m for m in plan_result["mitigations"]}


# 12-digit AWS account id / a real-looking cluster name must NOT leak into a
# generated config.
_ACCOUNT_RE = re.compile(r"\b\d{12}\b")


class ReferenceSetTest(unittest.TestCase):
    def setUp(self):
        self.res = plan(_rca(), capacity=_healthy_capacity())

    def test_schema_and_envelope(self):
        self.assertEqual(self.res["schema"], SCHEMA)
        self.assertIn("generated_at", self.res)
        self.assertEqual(self.res["generated_by"], "nr2grafana 1.9.0")
        self.assertIn("summary", self.res)
        self.assertIn("headline", self.res["summary"])

    def test_reference_mitigation_set_present(self):
        kinds = set(_by_kind(self.res))
        self.assertEqual(kinds, {
            "mimir_zone_aware", "loki_zone_aware", "prefer_close",
            "karpenter_3az", "nlb_cross_zone"})

    def test_every_mitigation_has_guardrails_and_owner(self):
        for m in self.res["mitigations"]:
            self.assertTrue(m["reliability_guardrails"],
                            "%s has no guardrails" % m["id"])
            self.assertIn("proposal only", m["owner"])
            self.assertIn("expected", m)
            self.assertIn("config", m)
            self.assertEqual(m["config"]["format"], "yaml")

    def test_healthy_case_all_primary_safe(self):
        by = _by_kind(self.res)
        for kind in ("mimir_zone_aware", "loki_zone_aware", "prefer_close",
                     "karpenter_3az"):
            m = by[kind]
            self.assertTrue(m["safe"], "%s should be safe" % kind)
            self.assertTrue(m["keeps_availability"])
            self.assertTrue(m["keeps_durability"])
            self.assertTrue(m["keeps_performance"])
            self.assertTrue(m["handles_current_traffic"])

    def test_ranked_safe_first_then_by_dollars(self):
        ms = self.res["mitigations"]
        # safe ones come before demoted ones
        safe_flags = [m["safe"] for m in ms]
        self.assertEqual(safe_flags, sorted(safe_flags,
                                            key=lambda s: 0 if s else 1))
        # within the safe block, non-increasing $/day
        safe_usd = [m["expected"]["usd_per_day"] for m in ms if m["safe"]]
        self.assertEqual(safe_usd, sorted(safe_usd, reverse=True))

    def test_savings_not_double_counted(self):
        # The primary set collectively addresses the dominant share; the sum
        # must not exceed the anomaly's $/day.
        total = self.res["incident_ref"]["usd_per_day"]
        allsum = self.res["summary"]["total_est_usd_per_day_saved_if_all"]
        self.assertLessEqual(allsum, total + 0.5)
        # dominant ~91% + secondary ~8% ~= 99% of 164 ~= 162
        self.assertGreater(allsum, 150.0)

    def test_guardrails_come_from_reliability(self):
        # Each mitigation's guardrails are reliability.check's preconditions.
        ctx = mitigate._build_context(_rca(), None, None,
                                      _healthy_capacity(), {})
        for m in self.res["mitigations"]:
            probe = dict(m)
            probe.update(mitigate._safety_fields(m["kind"], ctx))
            chk = reliability.check(probe, ctx)
            self.assertEqual(m["reliability_guardrails"],
                             chk["required_preconditions"])


class GenericConfigTest(unittest.TestCase):
    def setUp(self):
        self.by = _by_kind(plan(_rca(), capacity=_healthy_capacity()))

    def _cfg(self, kind):
        return self.by[kind]["config"]["content"]

    def test_no_customer_values_in_any_config(self):
        for m in self.by.values():
            body = m["config"]["content"]
            self.assertIsNone(_ACCOUNT_RE.search(body),
                              "account id leaked into %s" % m["kind"])
            # the reference customer's cluster/account must not appear
            self.assertNotIn("348342704569", body)
            # generic templates use angle-bracket placeholders
            self.assertIn("<", body)

    def test_mimir_zone_aware_flags(self):
        body = self._cfg("mimir_zone_aware")
        self.assertIn("zone_awareness_enabled: true", body)
        self.assertIn("replication_factor", body)
        self.assertIn("rollout_operator", body)

    def test_loki_zone_aware_flags(self):
        body = self._cfg("loki_zone_aware")
        self.assertIn("zone_awareness_enabled: true", body)
        self.assertIn("replication_factor", body)

    def test_preferclose_service(self):
        body = self._cfg("prefer_close")
        self.assertIn("trafficDistribution: PreferClose", body)
        self.assertIn("kind: Service", body)

    def test_karpenter_three_az(self):
        body = self._cfg("karpenter_3az")
        self.assertIn("kind: EC2NodeClass", body)
        self.assertIn("kind: NodePool", body)
        self.assertIn("topology.kubernetes.io/zone", body)
        self.assertIn("karpenter.sh/discovery", body)
        self.assertIn("<AZ_1>", body)
        self.assertIn("<AZ_3>", body)
        self.assertIn("budgets", body)

    def test_nlb_gated_config(self):
        body = self._cfg("nlb_cross_zone")
        self.assertIn("load_balancing.cross_zone.enabled=false", body)
        # BOTH health gates present so a thin AZ fails safe
        self.assertIn(
            "target_group_health.dns_failover.minimum_healthy_targets"
            ".count=1", body)
        self.assertIn(
            "target_group_health.unhealthy_state_routing"
            ".minimum_healthy_targets.count=1", body)
        # the safety gate is documented
        self.assertIn("describe-target-health", body)


class SafetyInvariantTest(unittest.TestCase):
    """No mitigation may be marked safe if it reduces reliability / current
    traffic. This is the binding cross-cutting rule."""

    def _assert_invariant(self, res):
        for m in res["mitigations"]:
            # agreement with reliability.check
            self.assertIn("safe", m)
            if m["safe"]:
                # a safe mitigation keeps EVERYTHING
                self.assertTrue(m["keeps_availability"], m["id"])
                self.assertTrue(m["keeps_durability"], m["id"])
                self.assertTrue(m["keeps_performance"], m["id"])
                self.assertTrue(m["handles_current_traffic"], m["id"])
                self.assertEqual(m["reliability"]["violations"], [])
            else:
                # a demoted mitigation drops at least one flag AND carries a
                # loud caveat
                dropped = not all([
                    m["keeps_availability"], m["keeps_durability"],
                    m["keeps_performance"], m["handles_current_traffic"]])
                self.assertTrue(dropped, "%s unsafe but keeps all" % m["id"])
                self.assertTrue(any("DEMOTED" in c for c in m["caveats"]))
                self.assertTrue(m["reliability"]["violations"])

    def test_healthy_case_invariant(self):
        self._assert_invariant(plan(_rca(), capacity=_healthy_capacity()))

    def test_nlb_zero_target_az_black_hole_is_demoted(self):
        cap = _healthy_capacity()
        cap["nlb_target_health"] = {
            "us-east-1a": {"healthy": 3},
            "us-east-1b": {"healthy": 0},   # black-hole AZ
            "us-east-1c": {"healthy": 3},
        }
        res = plan(_rca(), capacity=cap)
        self._assert_invariant(res)
        nlb = _by_kind(res)["nlb_cross_zone"]
        self.assertFalse(nlb["safe"])
        self.assertFalse(nlb["keeps_availability"])
        # demoted below the safe primaries
        ids = [m["id"] for m in res["mitigations"]]
        self.assertEqual(ids[-1], nlb["id"])

    def test_preferclose_thin_zone_hotspot_is_demoted(self):
        cap = _healthy_capacity()
        # zone c cannot carry its current per-zone traffic on its own
        cap["per_zone_replicas"]["us-east-1c"] = 1
        cap["per_zone_required_replicas"]["us-east-1c"] = 3
        res = plan(_rca(), capacity=cap)
        self._assert_invariant(res)
        pc = _by_kind(res)["prefer_close"]
        self.assertFalse(pc["safe"])
        self.assertFalse(pc["handles_current_traffic"])

    def test_capacity_below_current_traffic_breaks_performance(self):
        cap = _healthy_capacity()
        # post-change capacity in one zone falls below its current rate
        cap["per_zone_capacity_after"]["us-east-1c"] = 40
        cap["per_zone_current_rate"]["us-east-1c"] = 120
        res = plan(_rca(), capacity=cap)
        self._assert_invariant(res)
        m1 = _by_kind(res)["mimir_zone_aware"]
        self.assertFalse(m1["handles_current_traffic"])
        self.assertFalse(m1["keeps_performance"])

    def test_no_reliability_data_still_lists_preconditions(self):
        # With no per-AZ facts, reliability cannot PROVE a hard violation for
        # zone-aware, but the preconditions (guardrails) must still be
        # surfaced so the operator verifies them.
        res = plan(_rca(), capacity=None)
        self._assert_invariant(res)
        for m in res["mitigations"]:
            self.assertTrue(m["reliability_guardrails"])


class DegradeAndConfigTest(unittest.TestCase):
    def test_empty_rca_is_graceful(self):
        res = plan({})
        self.assertEqual(res["schema"], SCHEMA)
        self.assertEqual(res["mitigations"], [])
        self.assertTrue(res["notes"])

    def test_none_rca_is_graceful(self):
        res = plan(None)
        self.assertEqual(res["schema"], SCHEMA)
        self.assertEqual(res["mitigations"], [])

    def test_non_cross_az_dominant_notes_only(self):
        rca = _rca(dom_class="STORAGE_GROWTH", with_secondary=False)
        res = plan(rca, capacity=_healthy_capacity())
        self.assertEqual(res["mitigations"], [])
        self.assertTrue(res["notes"])

    def test_secondary_only_nlb(self):
        # dominant cross-AZ absent but an NLB secondary present
        rca = _rca(dom_class="STORAGE_GROWTH")
        res = plan(rca, capacity=_healthy_capacity())
        kinds = set(_by_kind(res))
        self.assertEqual(kinds, {"nlb_cross_zone"})

    def test_share_accepts_percent_or_fraction(self):
        frac = plan(_rca(dom_share=0.91), capacity=_healthy_capacity())
        pct = plan(_rca(dom_share=91), capacity=_healthy_capacity())
        self.assertAlmostEqual(
            frac["summary"]["total_est_usd_per_day_saved_if_all"],
            pct["summary"]["total_est_usd_per_day_saved_if_all"], places=1)

    def test_cfg_split_override(self):
        cfg = {"mitigate": {"split": {
            "mimir_zone_aware": 1.0, "loki_zone_aware": 0.0,
            "prefer_close": 0.0, "karpenter_3az": 0.0}}}
        res = plan(_rca(with_secondary=False), capacity=_healthy_capacity(),
                   cfg=cfg)
        by = _by_kind(res)
        self.assertGreater(by["mimir_zone_aware"]["expected"]["usd_per_day"],
                           0.0)
        self.assertEqual(by["loki_zone_aware"]["expected"]["usd_per_day"],
                         0.0)

    def test_never_raises_on_junk(self):
        for junk in ({"cause": "nope"}, {"incident": 5},
                     {"cause": {"dominant": []}}):
            res = plan(junk, capacity=_healthy_capacity())
            self.assertEqual(res["schema"], SCHEMA)


if __name__ == "__main__":
    unittest.main()
