"""Tests for nr2grafana.costmodel: pricing, TCO, and savings math.

Pure-math module, so no HTTP stubbing is needed. We assert exact expected
numbers for a known pricing + traffic pair, plus defensive behavior on
empty / partial input and the savings roll-up.
"""

import unittest

from nr2grafana.costmodel import (
    DEFAULT_PRICING, DAYS_PER_MONTH, SCHEMA, apply_savings, effective_pricing,
    estimate_costs,
)


# Clean override pricing chosen so hand-computed expectations are simple.
PRICING = {
    "loki": {
        "ingest_usd_per_gb": 1.0,
        "store_usd_per_gb_month": 0.1,
        "retention_days": 10,
    },
    "mimir": {
        "usd_per_1k_series_month": 1.0,
        "store_usd_per_gb_month": 0.1,
        "bytes_per_series_day": 10000.0,
        "retention_days": 10,
    },
    "resources": {"mimir_ram_bytes_per_series": 2000.0},
}


def make_traffic():
    """A traffic sample with one datasource per family."""
    return {
        "schema": "nr2grafana/traffic/v1",
        "datasources": [
            {"family": "loki", "uid": "loki",
             "loki": {"streams": 100, "bytes_per_day": 2_000_000_000.0}},
            {"family": "prometheus", "uid": "mimir",
             "prometheus": {"active_series": 50000, "histogram_series": 800}},
            {"family": "tempo", "uid": "tempo", "tempo": {"note": "n/a"}},
        ],
    }


class TestPricingMerge(unittest.TestCase):
    def test_defaults_untouched(self):
        eff = effective_pricing()
        self.assertEqual(eff["mimir"]["usd_per_1k_series_month"],
                         DEFAULT_PRICING["mimir"]["usd_per_1k_series_month"])
        # Mutating the result must not corrupt the module default.
        eff["mimir"]["usd_per_1k_series_month"] = 99.0
        self.assertNotEqual(
            DEFAULT_PRICING["mimir"]["usd_per_1k_series_month"], 99.0)

    def test_partial_override_deep_merges(self):
        eff = effective_pricing({"loki": {"ingest_usd_per_gb": 2.5}})
        # Overridden leaf wins.
        self.assertEqual(eff["loki"]["ingest_usd_per_gb"], 2.5)
        # Sibling leaves keep their defaults.
        self.assertEqual(
            eff["loki"]["store_usd_per_gb_month"],
            DEFAULT_PRICING["loki"]["store_usd_per_gb_month"])
        # Other blocks untouched.
        self.assertEqual(eff["mimir"], DEFAULT_PRICING["mimir"])


class TestEstimateCosts(unittest.TestCase):
    def test_exact_numbers(self):
        cost = estimate_costs(make_traffic(), PRICING)
        self.assertEqual(cost["schema"], SCHEMA)
        comps = {c["family"]: c for c in cost["components"]}

        # --- Loki: 2 GB/day ---
        loki = comps["loki"]
        # ingest = 2.0 GB/day * 30.44 days * $1.0
        self.assertAlmostEqual(loki["breakdown"]["ingest_usd"],
                               2.0 * DAYS_PER_MONTH, places=4)
        # storage = 2.0 GB/day * 10 days * $0.1
        self.assertAlmostEqual(loki["breakdown"]["storage_usd"], 2.0)
        self.assertAlmostEqual(loki["monthly_cost"],
                               2.0 * DAYS_PER_MONTH + 2.0, places=4)
        self.assertEqual(loki["drivers"]["streams"], 100)

        # --- Mimir: 50k series ---
        mimir = comps["prometheus"]
        self.assertAlmostEqual(mimir["breakdown"]["series_usd"], 50.0)
        # storage = 50000 * 10000 * 10 / 1e9 = 5.0 GB -> * 0.1 = 0.5
        self.assertAlmostEqual(mimir["breakdown"]["stored_gb"], 5.0)
        self.assertAlmostEqual(mimir["breakdown"]["storage_usd"], 0.5)
        self.assertAlmostEqual(mimir["monthly_cost"], 50.5)
        # RAM = 50000 * 2000 / 1e9 = 0.1 GB
        self.assertAlmostEqual(mimir["resources"]["mimir_ram_gb"], 0.1)

        # --- Tempo: no volume measured -> 0 with a note ---
        tempo = comps["tempo"]
        self.assertEqual(tempo["monthly_cost"], 0.0)
        self.assertIn("best-effort", tempo["note"])

        # --- Totals & resources ---
        self.assertAlmostEqual(cost["monthly_total"],
                               2.0 * DAYS_PER_MONTH + 2.0 + 50.5, places=4)
        self.assertAlmostEqual(cost["resources"]["mimir_ram_gb_est"], 0.1)
        self.assertAlmostEqual(cost["resources"]["storage_gb_month_est"],
                               20.0 + 5.0)

    def test_default_pricing_runs(self):
        cost = estimate_costs(make_traffic())
        self.assertGreater(cost["monthly_total"], 0.0)
        self.assertEqual(cost["pricing"]["mimir"]["usd_per_1k_series_month"],
                         DEFAULT_PRICING["mimir"]["usd_per_1k_series_month"])

    def test_empty_traffic(self):
        for arg in (None, {}, {"datasources": []}, {"datasources": "bad"}):
            cost = estimate_costs(arg, PRICING)
            self.assertEqual(cost["components"], [])
            self.assertEqual(cost["monthly_total"], 0.0)
            self.assertEqual(cost["resources"]["mimir_ram_gb_est"], 0.0)

    def test_partial_and_junk_fields(self):
        traffic = {"datasources": [
            {"family": "loki", "uid": "l"},                      # no loki blk
            {"family": "prometheus", "uid": "m",
             "prometheus": {"active_series": None}},             # junk value
            {"family": "unknown", "uid": "x"},                   # skipped
            "not-a-dict",                                        # skipped
        ]}
        cost = estimate_costs(traffic, PRICING)
        fams = [c["family"] for c in cost["components"]]
        self.assertEqual(fams, ["loki", "prometheus"])
        self.assertEqual(cost["monthly_total"], 0.0)


class TestApplySavings(unittest.TestCase):
    def test_rollup_by_family(self):
        cost = estimate_costs(make_traffic(), PRICING)
        recs = [
            {"family": "loki",
             "est_savings": {"monthly_usd": 10.0, "gb_per_day": 0.5,
                             "streams": 30}},
            {"family": "loki",
             "est_savings": {"monthly_usd": 5.0, "streams": 10}},
            {"family": "prometheus",
             "est_savings": {"monthly_usd": 20.0, "series": 8000}},
        ]
        out = apply_savings(cost, recs)
        by_fam = {c["family"]: c for c in out["per_component"]}

        self.assertAlmostEqual(by_fam["loki"]["saved"], 15.0)
        self.assertEqual(by_fam["loki"]["native_savings"]["streams"], 40)
        self.assertAlmostEqual(
            by_fam["loki"]["native_savings"]["gb_per_day"], 0.5)
        self.assertAlmostEqual(by_fam["prometheus"]["saved"], 20.0)
        self.assertEqual(by_fam["prometheus"]["native_savings"]["series"],
                         8000)

        self.assertAlmostEqual(out["saved_total"], 35.0)
        self.assertAlmostEqual(out["current_total"], cost["monthly_total"])
        self.assertAlmostEqual(out["projected_total"],
                               cost["monthly_total"] - 35.0, places=4)
        self.assertAlmostEqual(
            out["saved_pct"],
            round(35.0 / cost["monthly_total"] * 100.0, 2), places=2)
        self.assertEqual(out["native_savings"]["streams"], 40)
        self.assertEqual(out["native_savings"]["series"], 8000)

    def test_savings_capped_at_component_cost(self):
        cost = estimate_costs(make_traffic(), PRICING)
        recs = [{"family": "prometheus",
                 "est_savings": {"monthly_usd": 999999.0}}]
        out = apply_savings(cost, recs)
        prom = next(c for c in out["per_component"]
                    if c["family"] == "prometheus")
        # Capped at the family's current cost; never projects below zero.
        self.assertAlmostEqual(prom["saved"], 50.5)
        self.assertAlmostEqual(prom["projected"], 0.0)
        self.assertAlmostEqual(prom["saved_requested"], 999999.0)
        self.assertGreaterEqual(out["projected_total"], 0.0)

    def test_accepts_optimize_document(self):
        cost = estimate_costs(make_traffic(), PRICING)
        doc = {"schema": "nr2grafana/optimize/v1",
               "recommendations": [
                   {"family": "loki",
                    "est_savings": {"monthly_usd": 3.0}}]}
        out = apply_savings(cost, doc)
        self.assertAlmostEqual(out["saved_total"], 3.0)

    def test_defensive_empty(self):
        out = apply_savings(None, None)
        self.assertEqual(out["saved_total"], 0.0)
        self.assertEqual(out["projected_total"], 0.0)
        self.assertEqual(out["saved_pct"], 0.0)
        self.assertEqual(out["per_component"], [])

    def test_missing_est_savings(self):
        cost = estimate_costs(make_traffic(), PRICING)
        out = apply_savings(cost, [{"family": "loki"}])  # no est_savings
        self.assertEqual(out["saved_total"], 0.0)


if __name__ == "__main__":
    unittest.main()
