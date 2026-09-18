"""Regression tests for the cost-anomaly root-cause engine (rca.py).

The centerpiece is :func:`test_reproduces_reference_rca`, which drives
``rca.analyze`` from CANNED read-only inputs and reproduces the reference
worked example from ARCHITECTURE-1.9: an ``EBS DataTransfer-Regional-Bytes``
spike whose real cause is a non-zone-aware Mimir/Loki ring (RF=3 + query
fan-out on gRPC port 9095) confined to 2 imbalanced AZs, with a cross-zone
NLB secondary, and EBS storage / RDS replica ruled out.
"""

import json
import unittest

from nr2grafana import rca


# ---------------------------------------------------------------------------
# canned inputs (GENERIC placeholders, self-consistent with 0.1)
# ---------------------------------------------------------------------------
REFERENCE_ACCOUNT = "348342704569"
REGION = "us-east-1"
AZ_A, AZ_B, AZ_C = "us-east-1a", "us-east-1b", "us-east-1c"

# A pasted human report like the reference worked example.
PASTED_REPORT = (
    "Anomaly: EBS DataTransfer-Regional-Bytes cost spike, acct "
    "348342704569 / us-east-1. EBS storage is negligible. The charge is "
    "cross-AZ network transfer, ~16,470 GB/day, ~$164/day, true "
    "step-change 2026-08-31. Anomaly score 0.92."
)

# The exact GetAnomalies JSON shape (0.7), RootCauses[].UsageType uses the
# literal <Region>-DataTransfer-Regional-Bytes usage-type code.
CE_ANOMALY = {
    "Anomalies": [{
        "AnomalyId": "abc-123",
        "AnomalyStartDate": "2026-08-31",
        "AnomalyEndDate": "2026-08-31",
        "DimensionValue": "EBS",
        "MonitorArn": "arn:aws:ce::348342704569:anomalymonitor/x",
        "AnomalyScore": {"CurrentScore": 0.88, "MaxScore": 0.92},
        "Impact": {
            "MaxImpact": 180.0,
            "TotalActualSpend": 200.0,
            "TotalExpectedSpend": 36.0,
            "TotalImpact": 164.0,
            "TotalImpactPercentage": 455.0,
        },
        "RootCauses": [{
            "Service": "EBS",
            "Region": "us-east-1",
            "LinkedAccount": "348342704569",
            "LinkedAccountName": "prod",
            "UsageType": "USE1-DataTransfer-Regional-Bytes",
            "Impact": {"Contribution": 150.0},
        }, {
            "Service": "EC2",
            "Region": "us-east-1",
            "LinkedAccount": "348342704569",
            "UsageType": "USE1-DataTransfer-Regional-Bytes",
            "Impact": {"Contribution": 14.0},
        }],
        "Feedback": "",
    }]
}


def canned_flowlogs():
    """flowlogs/v1-shaped: port 9095 dominant (91%), NLB secondary (8%)."""
    return {
        "schema": "nr2grafana/flowlogs/v1",
        "cross_az_gb_per_day": 16470.0,
        "step_change_date": "2026-08-31",
        "region": REGION,
        "azs_observed": [AZ_A, AZ_B],
        "region_azs": [AZ_A, AZ_B, AZ_C],
        "imbalanced": True,
        "private_only": True,
        "drivers": [
            {"driver": "gRPC ring replication + query fan-out",
             "port": 9095, "gb_per_day": 14988.0, "share_pct": 91.0,
             "workload": "Mimir/Loki ingesters+queriers"},
            {"driver": "cross-zone NLB", "port": 443,
             "gb_per_day": 1318.0, "share_pct": 8.0,
             "workload": "Mimir gateway NLB"},
            {"driver": "misc", "port": 53, "gb_per_day": 164.0,
             "share_pct": 1.0, "workload": ""},
        ],
    }


def canned_deepdive():
    return {
        "schema": "nr2grafana/deepdive/v1",
        "zone_aware": False,
        "replication_factor": 3,
        "workload": "Mimir/Loki ingesters",
        "findings": [{
            "area": "network", "severity": "FAIL",
            "title": "Ring is non-zone-aware (cross-AZ 9095)",
            "rationale": "replicas land in arbitrary zones",
            "evidence": {"rf": 3, "port": 9095},
        }],
    }


def canned_packing():
    return {
        "schema": "nr2grafana/packing/v1",
        "karpenter_discovery_azs": [AZ_A, AZ_B],
        "region_azs": [AZ_A, AZ_B, AZ_C],
        "workload": "Mimir/Loki node pool",
    }


def canned_tco():
    return {"schema": "nr2grafana/tco/v1", "storage_flat": True}


def canned_aws():
    return {
        "cloudtrail_events": [{"EventName": "CreateFleet",
                               "EventTime": "2026-08-31T02:00:00Z"}],
        "volumes_flat": True,
        "snapshots_flat": True,
        "rds_multi_az": False,
        "rds_enis_in_flows": False,
    }


# ---------------------------------------------------------------------------
# classify_usage_type
# ---------------------------------------------------------------------------
class TestClassify(unittest.TestCase):
    def test_regional_bytes_is_cross_az(self):
        self.assertEqual(
            rca.classify_usage_type("USE1-DataTransfer-Regional-Bytes"),
            rca.CROSS_AZ_NETWORK)

    def test_regional_bytes_any_service_tag(self):
        # The service prefix is irrelevant; the usage-type drives the class.
        for ut in ("EBS:DataTransfer-Regional-Bytes",
                   "USW2-EC2-DataTransfer-Regional-Bytes",
                   "DataTransfer-Regional-Bytes"):
            self.assertEqual(rca.classify_usage_type(ut),
                             rca.CROSS_AZ_NETWORK, ut)

    def test_interzone_is_cross_az(self):
        self.assertEqual(rca.classify_usage_type("Foo-InterZone-Bytes"),
                         rca.CROSS_AZ_NETWORK)

    def test_out_bytes_is_egress(self):
        self.assertEqual(
            rca.classify_usage_type("USE1-DataTransfer-Out-Bytes"),
            rca.INTERNET_EGRESS)

    def test_storage_types(self):
        for ut in ("EBS:VolumeUsage.gp3", "EBS:SnapshotUsage",
                   "TimedStorage-ByteHrs"):
            self.assertEqual(rca.classify_usage_type(ut),
                             rca.STORAGE_GROWTH, ut)

    def test_unknown(self):
        self.assertEqual(rca.classify_usage_type("Requests-Tier1"),
                         rca.UNKNOWN)
        self.assertEqual(rca.classify_usage_type(""), rca.UNKNOWN)
        self.assertEqual(rca.classify_usage_type(None), rca.UNKNOWN)


# ---------------------------------------------------------------------------
# parse_anomaly_report
# ---------------------------------------------------------------------------
class TestParsePasted(unittest.TestCase):
    def setUp(self):
        self.inc = rca.parse_anomaly_report(PASTED_REPORT)

    def test_schema_and_source(self):
        self.assertEqual(self.inc["schema"], rca.INCIDENT_SCHEMA)
        self.assertEqual(self.inc["source"], "pasted-report")

    def test_usage_type_and_class(self):
        self.assertIn("DataTransfer-Regional-Bytes", self.inc["usage_type"])
        self.assertEqual(self.inc["hypothesis_class"], rca.CROSS_AZ_NETWORK)

    def test_service_account_region(self):
        self.assertEqual(self.inc["service"], "EBS")
        self.assertEqual(self.inc["account"], REFERENCE_ACCOUNT)
        self.assertEqual(self.inc["region"], REGION)

    def test_dollars_and_gb(self):
        self.assertEqual(self.inc["dollars_per_day"], 164.0)
        self.assertEqual(self.inc["gb_per_day"], 16470.0)

    def test_step_change_and_score(self):
        self.assertEqual(self.inc["step_change"], "2026-08-31")
        self.assertEqual(self.inc["score"], 0.92)


class TestParseCE(unittest.TestCase):
    def setUp(self):
        self.inc = rca.parse_anomaly_report(CE_ANOMALY)

    def test_source_and_class(self):
        self.assertEqual(self.inc["source"], "ce-anomaly")
        self.assertEqual(self.inc["hypothesis_class"], rca.CROSS_AZ_NETWORK)

    def test_picks_largest_root_cause(self):
        self.assertEqual(self.inc["usage_type"],
                         "USE1-DataTransfer-Regional-Bytes")
        self.assertEqual(self.inc["service"], "EBS")
        self.assertEqual(self.inc["account"], REFERENCE_ACCOUNT)
        self.assertEqual(self.inc["region"], REGION)

    def test_dollars_per_day_single_day(self):
        # TotalImpact 164 over a 1-day interval -> 164/day.
        self.assertEqual(self.inc["dollars_per_day"], 164.0)
        self.assertEqual(self.inc["days"], 1)

    def test_score_prefers_max(self):
        self.assertEqual(self.inc["score"], 0.92)

    def test_accepts_json_string(self):
        inc = rca.parse_anomaly_report(json.dumps(CE_ANOMALY))
        self.assertEqual(inc["usage_type"],
                         "USE1-DataTransfer-Regional-Bytes")

    def test_accepts_single_anomaly_dict(self):
        inc = rca.parse_anomaly_report(CE_ANOMALY["Anomalies"][0])
        self.assertEqual(inc["account"], REFERENCE_ACCOUNT)

    def test_multi_day_divides_impact(self):
        anom = json.loads(json.dumps(CE_ANOMALY))
        anom["Anomalies"][0]["AnomalyEndDate"] = "2026-09-05"
        anom["Anomalies"][0]["Impact"]["TotalImpact"] = 984.0
        inc = rca.parse_anomaly_report(anom)
        self.assertEqual(inc["days"], 6)
        self.assertEqual(inc["dollars_per_day"], 164.0)


# ---------------------------------------------------------------------------
# the reference reproduction (Steps A-F)
# ---------------------------------------------------------------------------
class TestReferenceRCA(unittest.TestCase):
    def setUp(self):
        self.rep = rca.analyze(
            PASTED_REPORT,
            aws=canned_aws(),
            flowlogs=canned_flowlogs(),
            deepdive=canned_deepdive(),
            packing=canned_packing(),
            tco=canned_tco(),
        )

    def test_schema(self):
        self.assertEqual(self.rep["schema"], "nr2grafana/rca/v1")
        self.assertEqual(self.rep["generated_by"], "nr2grafana 1.9.0")

    def test_incident_frame(self):
        inc = self.rep["incident"]
        self.assertEqual(inc["hypothesis_class"], rca.CROSS_AZ_NETWORK)
        self.assertEqual(inc["account"], REFERENCE_ACCOUNT)
        self.assertEqual(inc["region"], REGION)
        self.assertEqual(inc["dollars_per_day"], 164.0)
        self.assertEqual(inc["gb_per_day"], 16470.0)
        self.assertEqual(inc["step_change"], "2026-08-31")

    def test_dominant_is_cross_az_ring_on_9095(self):
        dom = self.rep["cause"]["dominant"]
        self.assertEqual(dom["share"], 0.91)
        self.assertEqual(dom["share_pct"], 91.0)
        self.assertEqual(dom["port"], 9095)
        low = dom["summary"].lower()
        self.assertIn("non-zone-aware", low)
        self.assertIn("ring", low)
        self.assertIn("query fan-out", low)

    def test_dominant_evidence_converges(self):
        dom = self.rep["cause"]["dominant"]
        blob = " ".join(dom["evidence"]).lower()
        # cost-explorer classification, flow logs, lgtm, eks, cloudtrail
        self.assertIn("regional-bytes == cross-az", blob)
        self.assertIn("9095", blob)
        self.assertIn("91", blob)
        self.assertIn("karpenter", blob)
        self.assertIn("cloudtrail", blob)

    def test_secondary_is_cross_zone_nlb(self):
        secs = self.rep["cause"]["secondary"]
        self.assertTrue(secs)
        nlb = secs[0]
        self.assertEqual(nlb["share_pct"], 8.0)
        self.assertIn("nlb", nlb["driver"].lower())
        self.assertIn("cross-zone", nlb["summary"].lower())

    def test_ruled_out_storage_and_rds(self):
        hyps = [r["hypothesis"].lower()
                for r in self.rep["cause"]["ruled_out"]]
        self.assertTrue(any("storage" in h for h in hyps))
        self.assertTrue(any("rds" in h for h in hyps))
        self.assertTrue(any("cross-region" in h for h in hyps))
        self.assertTrue(any("egress" in h or "nat" in h for h in hyps))
        # every ruled-out entry carries disproving evidence
        for r in self.rep["cause"]["ruled_out"]:
            self.assertTrue(r["evidence"].strip())

    def test_storage_ruleout_explains_artifact(self):
        st = [r for r in self.rep["cause"]["ruled_out"]
              if "storage" in r["hypothesis"].lower()][0]
        self.assertIn("artifact", st["evidence"].lower())

    def test_all_five_sources_converge(self):
        conv = self.rep["evidence_convergence"]
        for src in (rca.SRC_COST_EXPLORER, rca.SRC_FLOW_LOGS,
                    rca.SRC_LGTM, rca.SRC_EKS, rca.SRC_CLOUDTRAIL):
            self.assertIn(src, conv)

    def test_confidence_high(self):
        self.assertEqual(self.rep["confidence"], "HIGH")

    def test_assumptions_present(self):
        blob = " ".join(self.rep["incident"]["assumptions"]).lower()
        self.assertIn("cross-az", blob)
        self.assertIn("artifact", blob)


# ---------------------------------------------------------------------------
# degradation & confidence scaling
# ---------------------------------------------------------------------------
class TestDegradation(unittest.TestCase):
    def test_no_flowlogs_is_low_confidence_no_share(self):
        rep = rca.analyze(PASTED_REPORT, deepdive=canned_deepdive())
        self.assertEqual(rep["confidence"], "LOW")
        self.assertIsNone(rep["cause"]["dominant"]["share"])
        blob = " ".join(rep["notes"]).lower()
        self.assertIn("no vpc flow logs", blob)

    def test_no_share_never_fabricated(self):
        rep = rca.analyze(PASTED_REPORT)
        self.assertIsNone(rep["cause"]["dominant"]["share"])

    def test_two_sources_is_medium(self):
        # cost-explorer + flow-logs only -> MEDIUM
        rep = rca.analyze(PASTED_REPORT, flowlogs=canned_flowlogs())
        self.assertEqual(rep["confidence"], "MEDIUM")

    def test_ce_path_converts_dollars_to_gb(self):
        # No GB in the CE anomaly -> derive GB from $/day at $0.02 two-way.
        rep = rca.analyze(CE_ANOMALY, flowlogs=canned_flowlogs())
        # flowlogs supplies cross_az gb only when incident lacks it; here the
        # $->GB conversion runs first, so gb_per_day reflects the estimate.
        self.assertEqual(rep["incident"]["gb_per_day"], 8200.0)
        blob = " ".join(rep["incident"]["assumptions"]).lower()
        self.assertIn("two-way", blob)

    def test_never_raises_on_garbage(self):
        for bad in (None, "", 42, [], {"foo": "bar"}, "not a report"):
            rep = rca.analyze(bad)
            self.assertEqual(rep["schema"], "nr2grafana/rca/v1")
            self.assertIn("confidence", rep)


# ---------------------------------------------------------------------------
# tolerant input shapes
# ---------------------------------------------------------------------------
class TestTolerantShapes(unittest.TestCase):
    def test_share_as_fraction(self):
        fl = canned_flowlogs()
        for d in fl["drivers"]:
            d["share"] = d.pop("share_pct") / 100.0
        rep = rca.analyze(PASTED_REPORT, flowlogs=fl)
        self.assertEqual(rep["cause"]["dominant"]["share_pct"], 91.0)

    def test_by_port_key_alias(self):
        fl = {"cross_az_gb_per_day": 100.0,
              "by_port": [{"port": 9095, "share_pct": 90.0}]}
        rep = rca.analyze(PASTED_REPORT, flowlogs=fl)
        self.assertEqual(rep["cause"]["dominant"]["port"], 9095)

    def test_missing_azs_derived_from_discovery(self):
        rep = rca.analyze(PASTED_REPORT, flowlogs=canned_flowlogs(),
                          packing=canned_packing())
        blob = " ".join(rep["cause"]["dominant"]["evidence"]).lower()
        self.assertIn("us-east-1c", blob)

    def test_deepdive_findings_scan_without_hints(self):
        dd = {"findings": [{
            "area": "network",
            "title": "cross-AZ replication on 9095 (non-zone-aware ring)",
            "rationale": "zone-aware disabled",
            "evidence": {"rf": 3},
        }]}
        rep = rca.analyze(PASTED_REPORT, flowlogs=canned_flowlogs(),
                          deepdive=dd)
        self.assertIn(rca.SRC_LGTM, rep["evidence_convergence"])


if __name__ == "__main__":
    unittest.main()
