"""Tests for nr2grafana.reliability -- the mitigation guardrail library.

These assert each guardrail rule from ARCHITECTURE-1.9 section 5: the
RF/quorum math, zone-aware ring migration safety, the PreferClose
overload hazard, per-AZ NLB target health (never blind-disable
cross-zone -- including the single-target-AZ black-hole), Karpenter
multi-AZ discovery + budgets, never CPU-limit ingesters, keep
RF/retention, and keep enough per-zone headroom for current traffic.
Every case checks that ``check`` never raises and returns the documented
shape.
"""

import unittest

from nr2grafana import reliability
from nr2grafana.reliability import (
    AVAILABILITY, DURABILITY, PERFORMANCE,
    KIND_KARPENTER_SUBNETS, KIND_LOKI_ZONE_AWARE, KIND_MIMIR_ZONE_AWARE,
    KIND_NLB_DISABLE_CROSS_ZONE, KIND_PREFERCLOSE, KIND_UNKNOWN,
    check, check_all, normalize_kind, quorum)


def _impacts(result):
    """Flatten the impact dimensions across all violations."""
    out = set()
    for v in result["violations"]:
        out.update(v.get("impacts", []))
    return out


def _rules(result):
    return set(v["rule"] for v in result["violations"])


class TestShapeAndBasics(unittest.TestCase):
    def test_result_shape(self):
        r = check({"kind": KIND_PREFERCLOSE}, {})
        self.assertIn("safe", r)
        self.assertIn("violations", r)
        self.assertIn("required_preconditions", r)
        self.assertIsInstance(r["safe"], bool)
        self.assertIsInstance(r["violations"], list)
        self.assertIsInstance(r["required_preconditions"], list)

    def test_preconditions_present_even_when_safe(self):
        # A clean zone-aware change still lists its must-hold assumptions.
        m = {
            "kind": KIND_MIMIR_ZONE_AWARE,
            "replication_factor": 3,
            "zones": ["a", "b", "c"],
            "cpu_limit_ingesters": False,
            "pdb_present": True,
            "migration": {
                "one_zone_at_a_time": True,
                "existing_ingester_pdb_max_unavailable": 0,
                "limits_doubled": True,
                "write_path_before_read_path": True,
                "wait_observed": True,
            },
        }
        ctx = {"replication_factor": 3, "healthy_zones": 3}
        r = check(m, ctx)
        self.assertTrue(r["safe"], r["violations"])
        self.assertTrue(r["required_preconditions"])

    def test_never_raises_on_junk(self):
        for junk in (None, 42, "hello", [], {"kind": 123}, object()):
            r = check(junk, None)
            self.assertIn("safe", r)
            self.assertIsInstance(r["violations"], list)

    def test_non_dict_mitigation_is_unsafe(self):
        r = check("not a dict", {})
        self.assertFalse(r["safe"])
        self.assertIn("invalid_mitigation", _rules(r))

    def test_context_may_be_omitted(self):
        r = check({"kind": KIND_NLB_DISABLE_CROSS_ZONE})
        self.assertIn("safe", r)


class TestQuorumMath(unittest.TestCase):
    def test_quorum_values(self):
        self.assertEqual(quorum(3), 2)
        self.assertEqual(quorum(1), 1)
        self.assertEqual(quorum(5), 3)
        self.assertEqual(quorum(2), 2)

    def test_quorum_junk(self):
        self.assertEqual(quorum(0), 0)
        self.assertEqual(quorum(-1), 0)
        self.assertEqual(quorum("x"), 0)


class TestNormalizeKind(unittest.TestCase):
    def test_explicit_kinds(self):
        self.assertEqual(
            normalize_kind({"kind": KIND_MIMIR_ZONE_AWARE}),
            KIND_MIMIR_ZONE_AWARE)

    def test_inference_from_title(self):
        self.assertEqual(
            normalize_kind({"title": "Enable Loki zone-aware replication"}),
            KIND_LOKI_ZONE_AWARE)
        self.assertEqual(
            normalize_kind({"id": "M3", "title": "trafficDistribution "
                            "PreferClose"}),
            KIND_PREFERCLOSE)
        self.assertEqual(
            normalize_kind({"title": "Disable NLB cross-zone LB"}),
            KIND_NLB_DISABLE_CROSS_ZONE)
        self.assertEqual(
            normalize_kind({"title": "Karpenter NodePool 3-AZ subnets"}),
            KIND_KARPENTER_SUBNETS)

    def test_unknown(self):
        self.assertEqual(normalize_kind({"title": "buy a bigger box"}),
                         KIND_UNKNOWN)
        self.assertEqual(normalize_kind({}), KIND_UNKNOWN)
        self.assertEqual(normalize_kind(None), KIND_UNKNOWN)

    def test_unknown_kind_fails_closed(self):
        r = check({"kind": "totally_novel_thing"}, {})
        self.assertFalse(r["safe"])
        self.assertIn("unknown_mitigation", _rules(r))


class TestRfQuorumRetention(unittest.TestCase):
    def test_fewer_zones_than_rf_is_data_loss(self):
        m = {"kind": KIND_MIMIR_ZONE_AWARE, "replication_factor": 3,
             "zones": ["a", "b"]}
        r = check(m, {"replication_factor": 3})
        self.assertFalse(r["safe"])
        self.assertIn("rf_quorum", _rules(r))
        self.assertIn(DURABILITY, _impacts(r))

    def test_enough_zones_ok(self):
        m = {"kind": KIND_MIMIR_ZONE_AWARE, "replication_factor": 3,
             "zones": ["a", "b", "c"], "pdb_present": True,
             "cpu_limit_ingesters": False}
        r = check(m, {"replication_factor": 3, "healthy_zones": 3})
        self.assertNotIn("rf_quorum", _rules(r))

    def test_healthy_zones_below_quorum(self):
        m = {"kind": KIND_LOKI_ZONE_AWARE, "replication_factor": 3,
             "zones": ["a", "b", "c"]}
        r = check(m, {"replication_factor": 3, "healthy_zones": 1})
        self.assertFalse(r["safe"])
        self.assertIn("rf_quorum", _rules(r))
        self.assertIn(AVAILABILITY, _impacts(r))

    def test_reducing_rf_flagged(self):
        m = {"kind": KIND_MIMIR_ZONE_AWARE, "replication_factor": 2,
             "zones": ["a", "b", "c"]}
        r = check(m, {"replication_factor": 3, "healthy_zones": 3})
        self.assertFalse(r["safe"])
        self.assertIn("keep_rf", _rules(r))
        self.assertIn(DURABILITY, _impacts(r))

    def test_reducing_retention_flagged(self):
        m = {"kind": KIND_MIMIR_ZONE_AWARE, "replication_factor": 3,
             "zones": ["a", "b", "c"], "retention_days": 7}
        r = check(m, {"replication_factor": 3, "healthy_zones": 3,
                      "retention_days": 30})
        self.assertFalse(r["safe"])
        self.assertIn("keep_retention", _rules(r))

    def test_default_rf_when_unspecified(self):
        # No RF anywhere -> defaults to 3 -> two zones is data-loss.
        m = {"kind": KIND_MIMIR_ZONE_AWARE, "zones": ["a", "b"]}
        r = check(m, {})
        self.assertIn("rf_quorum", _rules(r))


class TestZoneMigrationSafety(unittest.TestCase):
    def _base(self):
        return {
            "kind": KIND_MIMIR_ZONE_AWARE, "replication_factor": 3,
            "zones": ["a", "b", "c"], "pdb_present": True,
            "cpu_limit_ingesters": False,
            "migration": {
                "one_zone_at_a_time": True,
                "existing_ingester_pdb_max_unavailable": 0,
                "limits_doubled": True,
                "write_path_before_read_path": True,
                "wait_observed": True,
            },
        }

    def _ctx(self):
        return {"replication_factor": 3, "healthy_zones": 3}

    def test_clean_migration_safe(self):
        r = check(self._base(), self._ctx())
        self.assertTrue(r["safe"], r["violations"])

    def test_multi_zone_rollout_flagged(self):
        m = self._base()
        m["migration"]["one_zone_at_a_time"] = False
        r = check(m, self._ctx())
        self.assertFalse(r["safe"])
        self.assertIn("zone_migration", _rules(r))

    def test_existing_pdb_not_zero_flagged(self):
        m = self._base()
        m["migration"]["existing_ingester_pdb_max_unavailable"] = 1
        r = check(m, self._ctx())
        self.assertFalse(r["safe"])
        self.assertIn("zone_migration", _rules(r))

    def test_limits_not_doubled_flagged(self):
        m = self._base()
        m["migration"]["limits_doubled"] = False
        r = check(m, self._ctx())
        self.assertFalse(r["safe"])

    def test_read_before_write_flagged(self):
        m = self._base()
        m["migration"]["write_path_before_read_path"] = False
        r = check(m, self._ctx())
        self.assertFalse(r["safe"])
        self.assertIn(DURABILITY, _impacts(r))

    def test_wait_not_observed_flagged(self):
        m = self._base()
        m["migration"]["wait_observed"] = False
        r = check(m, self._ctx())
        self.assertFalse(r["safe"])

    def test_migration_absent_lists_preconditions_but_safe(self):
        m = self._base()
        del m["migration"]
        r = check(m, self._ctx())
        self.assertTrue(r["safe"], r["violations"])
        joined = " ".join(r["required_preconditions"])
        self.assertIn("one zone at a time", joined)
        self.assertIn("maxUnavailable:0", joined)


class TestNoCpuLimitIngesters(unittest.TestCase):
    def test_cpu_limit_flagged(self):
        m = {"kind": KIND_MIMIR_ZONE_AWARE, "replication_factor": 3,
             "zones": ["a", "b", "c"], "cpu_limit_ingesters": True,
             "pdb_present": True}
        r = check(m, {"replication_factor": 3, "healthy_zones": 3})
        self.assertFalse(r["safe"])
        self.assertIn("no_cpu_limit_ingesters", _rules(r))
        self.assertIn(PERFORMANCE, _impacts(r))

    def test_precondition_always_present(self):
        m = {"kind": KIND_LOKI_ZONE_AWARE, "replication_factor": 3,
             "zones": ["a", "b", "c"], "pdb_present": True,
             "cpu_limit_ingesters": False}
        r = check(m, {"replication_factor": 3, "healthy_zones": 3})
        joined = " ".join(r["required_preconditions"])
        self.assertIn("no CPU limit", joined)


class TestPdb(unittest.TestCase):
    def test_missing_pdb_flagged(self):
        m = {"kind": KIND_MIMIR_ZONE_AWARE, "replication_factor": 3,
             "zones": ["a", "b", "c"], "pdb_present": False,
             "cpu_limit_ingesters": False}
        r = check(m, {"replication_factor": 3, "healthy_zones": 3})
        self.assertFalse(r["safe"])
        self.assertIn("pdb", _rules(r))


class TestPreferClose(unittest.TestCase):
    def test_balanced_is_safe(self):
        m = {"kind": KIND_PREFERCLOSE}
        ctx = {
            "serving_zones": ["a", "b", "c"],
            "per_zone_replicas": {"a": 3, "b": 3, "c": 3},
            "per_zone_required_replicas": {"a": 2, "b": 2, "c": 2},
        }
        r = check(m, ctx)
        self.assertTrue(r["safe"], r["violations"])

    def test_missing_endpoints_in_serving_zone_hotspots(self):
        m = {"kind": KIND_PREFERCLOSE}
        ctx = {
            "serving_zones": ["a", "b", "c"],
            "endpoint_zones": ["a", "b"],  # nothing in c
        }
        r = check(m, ctx)
        self.assertFalse(r["safe"])
        self.assertIn("preferclose_overload", _rules(r))

    def test_insufficient_replicas_hotspots(self):
        m = {"kind": KIND_PREFERCLOSE}
        ctx = {
            "serving_zones": ["a", "b", "c"],
            "per_zone_replicas": {"a": 3, "b": 3, "c": 1},
            "per_zone_required_replicas": {"a": 2, "b": 2, "c": 2},
        }
        r = check(m, ctx)
        self.assertFalse(r["safe"])
        self.assertIn("preferclose_overload", _rules(r))
        self.assertIn(PERFORMANCE, _impacts(r))

    def test_precondition_names_overload_safeguard(self):
        r = check({"kind": KIND_PREFERCLOSE}, {})
        joined = " ".join(r["required_preconditions"])
        self.assertIn("no overload safeguard", joined)


class TestNlbTargetHealth(unittest.TestCase):
    def test_black_hole_single_target_az_unsafe(self):
        # The headline case: disable cross-zone with a single-target AZ
        # and no health gates -> unsafe, with a required precondition.
        m = {"kind": KIND_NLB_DISABLE_CROSS_ZONE}  # no gates set
        ctx = {
            "nlb_enabled_azs": ["a", "b", "c"],
            "nlb_target_health": {
                "a": {"healthy": 3, "total": 3},
                "b": {"healthy": 3, "total": 3},
                "c": {"healthy": 1, "total": 1},  # single target
            },
        }
        r = check(m, ctx)
        self.assertFalse(r["safe"])
        self.assertIn("nlb_target_health", _rules(r))
        self.assertIn(AVAILABILITY, _impacts(r))
        self.assertTrue(r["required_preconditions"])
        joined = " ".join(r["required_preconditions"])
        self.assertIn("single healthy target", joined)

    def test_zero_healthy_az_is_hard_black_hole(self):
        m = {"kind": KIND_NLB_DISABLE_CROSS_ZONE,
             "health_gates_set": True}
        ctx = {
            "nlb_enabled_azs": ["a", "b"],
            "nlb_target_health": {
                "a": {"healthy": 3},
                "b": {"healthy": 0},
            },
        }
        r = check(m, ctx)
        self.assertFalse(r["safe"])
        self.assertIn("nlb_target_health", _rules(r))
        self.assertIn(AVAILABILITY, _impacts(r))

    def test_gated_and_healthy_is_safe(self):
        m = {"kind": KIND_NLB_DISABLE_CROSS_ZONE,
             "dns_failover_min_healthy": 1,
             "unhealthy_state_routing_min_healthy": 1}
        ctx = {
            "nlb_enabled_azs": ["a", "b", "c"],
            "nlb_target_health": {
                "a": {"healthy": 2},
                "b": {"healthy": 2},
                "c": {"healthy": 2},
            },
        }
        r = check(m, ctx)
        self.assertTrue(r["safe"], r["violations"])

    def test_single_target_but_gated_is_safe_with_caveat(self):
        # Gates cover a thin AZ (fail-open), so it is safe but the thin
        # AZ is recorded as a precondition to fix.
        m = {"kind": KIND_NLB_DISABLE_CROSS_ZONE, "health_gates_set": True}
        ctx = {
            "nlb_enabled_azs": ["a", "b", "c"],
            "nlb_target_health": {
                "a": {"healthy": 2},
                "b": {"healthy": 2},
                "c": {"healthy": 1},
            },
        }
        r = check(m, ctx)
        self.assertTrue(r["safe"], r["violations"])
        joined = " ".join(r["required_preconditions"])
        self.assertIn("single healthy target", joined)

    def test_no_gates_unsafe_even_if_all_healthy(self):
        m = {"kind": KIND_NLB_DISABLE_CROSS_ZONE}  # no gates
        ctx = {
            "nlb_enabled_azs": ["a", "b"],
            "nlb_target_health": {"a": {"healthy": 3}, "b": {"healthy": 3}},
        }
        r = check(m, ctx)
        self.assertFalse(r["safe"])
        self.assertIn("nlb_target_health", _rules(r))

    def test_unknown_health_requires_verification(self):
        m = {"kind": KIND_NLB_DISABLE_CROSS_ZONE, "health_gates_set": True}
        ctx = {"nlb_enabled_azs": ["a", "b", "c"],
               "nlb_target_health": {"a": {"healthy": 2}, "b": {"healthy": 2}}}
        r = check(m, ctx)
        joined = " ".join(r["required_preconditions"])
        self.assertIn("verify healthy target count for enabled AZ c", joined)


class TestKarpenter(unittest.TestCase):
    def test_two_az_discovery_flagged(self):
        m = {"kind": KIND_KARPENTER_SUBNETS,
             "discovery_subnet_azs": ["us-east-1a", "us-east-1b"]}
        r = check(m, {"replication_factor": 3})
        self.assertFalse(r["safe"])
        self.assertIn("karpenter_subnets", _rules(r))
        self.assertIn(DURABILITY, _impacts(r))

    def test_three_az_discovery_ok(self):
        m = {"kind": KIND_KARPENTER_SUBNETS,
             "discovery_subnet_azs": ["us-east-1a", "us-east-1b",
                                      "us-east-1c"]}
        r = check(m, {"replication_factor": 3})
        self.assertNotIn("karpenter_subnets", _rules(r))

    def test_no_budgets_flagged(self):
        m = {"kind": KIND_KARPENTER_SUBNETS,
             "discovery_subnet_azs": ["a", "b", "c"],
             "consolidation_budgets": False}
        r = check(m, {"replication_factor": 3})
        self.assertFalse(r["safe"])
        self.assertIn("karpenter_budgets", _rules(r))

    def test_pdb_missing_flagged(self):
        m = {"kind": KIND_KARPENTER_SUBNETS,
             "discovery_subnet_azs": ["a", "b", "c"],
             "pdb_present": False}
        r = check(m, {"replication_factor": 3})
        self.assertFalse(r["safe"])
        self.assertIn("pdb", _rules(r))

    def test_preconditions_mention_missing_az(self):
        r = check({"kind": KIND_KARPENTER_SUBNETS,
                   "discovery_subnet_azs": ["a", "b", "c"]},
                  {"replication_factor": 3})
        joined = " ".join(r["required_preconditions"])
        self.assertIn("discovery subnet", joined)
        self.assertIn("disruption budgets", joined)


class TestCurrentTraffic(unittest.TestCase):
    def test_insufficient_capacity_flagged(self):
        m = {"kind": KIND_PREFERCLOSE}
        ctx = {
            "per_zone_current_rate": {"a": 100.0, "b": 100.0},
            "per_zone_capacity_after": {"a": 150.0, "b": 50.0},
        }
        r = check(m, ctx)
        self.assertFalse(r["safe"])
        self.assertIn("handles_current_traffic", _rules(r))
        self.assertIn(PERFORMANCE, _impacts(r))

    def test_sufficient_capacity_ok(self):
        m = {"kind": KIND_PREFERCLOSE}
        ctx = {
            "serving_zones": ["a", "b"],
            "endpoint_zones": ["a", "b"],
            "per_zone_current_rate": {"a": 100.0, "b": 100.0},
            "per_zone_capacity_after": {"a": 150.0, "b": 150.0},
        }
        r = check(m, ctx)
        self.assertTrue(r["safe"], r["violations"])

    def test_capacity_data_absent_is_precondition_not_violation(self):
        r = check({"kind": KIND_PREFERCLOSE}, {})
        self.assertNotIn("handles_current_traffic", _rules(r))
        joined = " ".join(r["required_preconditions"])
        self.assertIn("per-zone capacity", joined)


class TestReferenceScenario(unittest.TestCase):
    """The worked example: a non-zone-aware ring across 2 imbalanced AZs.

    Reproduces the class of reasoning in ARCHITECTURE-1.9's reference
    example -- the primary zone-aware fix is safe when migrated correctly,
    while a blind NLB cross-zone disable against a thin AZ is not.
    """

    def test_primary_zone_aware_fix_safe(self):
        m = {
            "kind": KIND_MIMIR_ZONE_AWARE, "replication_factor": 3,
            "zones": ["us-east-1a", "us-east-1b", "us-east-1c"],
            "cpu_limit_ingesters": False, "pdb_present": True,
            "migration": {
                "one_zone_at_a_time": True,
                "existing_ingester_pdb_max_unavailable": 0,
                "limits_doubled": True,
                "write_path_before_read_path": True,
                "wait_observed": True,
            },
        }
        ctx = {"replication_factor": 3, "healthy_zones": 3,
               "per_zone_current_rate": {"us-east-1a": 50, "us-east-1b": 50,
                                         "us-east-1c": 50},
               "per_zone_capacity_after": {"us-east-1a": 100,
                                           "us-east-1b": 100,
                                           "us-east-1c": 100}}
        r = check(m, ctx)
        self.assertTrue(r["safe"], r["violations"])

    def test_secondary_nlb_disable_demoted_when_thin(self):
        m = {"kind": KIND_NLB_DISABLE_CROSS_ZONE}
        ctx = {
            "nlb_enabled_azs": ["us-east-1a", "us-east-1b"],
            "nlb_target_health": {
                "us-east-1a": {"healthy": 4},
                "us-east-1b": {"healthy": 1},  # single target -> black-hole
            },
        }
        r = check(m, ctx)
        self.assertFalse(r["safe"])
        self.assertIn(AVAILABILITY, _impacts(r))

    def test_check_all_over_a_plan(self):
        plan = [
            {"kind": KIND_MIMIR_ZONE_AWARE, "replication_factor": 3,
             "zones": ["a", "b", "c"], "cpu_limit_ingesters": False,
             "pdb_present": True},
            {"kind": KIND_NLB_DISABLE_CROSS_ZONE},
        ]
        ctx = {"replication_factor": 3, "healthy_zones": 3}
        results = check_all(plan, ctx)
        self.assertEqual(len(results), 2)
        self.assertTrue(all("safe" in r for r in results))


if __name__ == "__main__":
    unittest.main()
