"""End-to-end tests for the nr2grafana CLI (convert / validate /
example-config), driven through cli.main().

Note: fixtures/newrelic/ also contains edge-* fixtures (some deliberately
malformed), so the happy-path convert tests target the sample dashboard
file directly.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from nr2grafana import cli
from nr2grafana.config import DEFAULT_CONFIG

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(PROJECT_ROOT, "fixtures", "newrelic")
SAMPLE = os.path.join(FIXTURE_DIR, "sample-service-dashboard.json")


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class ConvertCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out_dir = cls.tmp.name
        cls.code, cls.stdout, cls.stderr = run_cli(
            ["convert", SAMPLE, "-o", cls.out_dir])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_exit_code_zero(self):
        self.assertEqual(self.code, 0)

    def test_output_files_exist(self):
        names = sorted(os.listdir(self.out_dir))
        self.assertIn("checkout-service-overview.json", names)
        self.assertIn("migration-report.json", names)

    def test_output_dashboard_is_valid_grafana_json(self):
        path = os.path.join(self.out_dir, "checkout-service-overview.json")
        with open(path, encoding="utf-8") as f:
            dash = json.load(f)
        self.assertEqual(dash["schemaVersion"], 39)
        self.assertIsNone(dash["id"])
        self.assertEqual(dash["uid"], "nr-checkout-service-overview")
        self.assertTrue(dash["panels"])

    def test_migration_report_structure(self):
        path = os.path.join(self.out_dir, "migration-report.json")
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(set(report), {"reports", "failed_inputs"})
        self.assertEqual(len(report["reports"]), 1)
        entry = report["reports"][0]
        for key in ("source", "output", "dashboard", "widgets", "summary"):
            self.assertIn(key, entry)
        self.assertEqual(entry["dashboard"], "Checkout Service Overview")
        self.assertEqual(len(entry["widgets"]), 18)
        for w in entry["widgets"]:
            for key in ("page", "widget", "confidence", "nrql", "queries",
                        "notes", "panel_id", "panel_type"):
                self.assertIn(key, w)
            self.assertIn(w["confidence"],
                          ("exact", "approximate", "needs-review",
                           "untranslatable"))
        # summary counts add up to the number of widgets
        self.assertEqual(sum(entry["summary"].values()),
                         len(entry["widgets"]))

    def test_validate_command_passes_on_output(self):
        code, out, err = run_cli(["validate", self.out_dir])
        self.assertEqual(code, 0)
        self.assertIn("OK", out)
        # migration-report.json is skipped, not flagged
        self.assertNotIn("migration-report", out)


class ConvertErrorHandlingTests(unittest.TestCase):
    def test_missing_input(self):
        with tempfile.TemporaryDirectory() as out_dir:
            code, out, err = run_cli(
                ["convert", "/nonexistent/path.json", "-o", out_dir])
        self.assertEqual(code, 2)
        self.assertIn("no input files", err)

    def test_dashboard_without_pages_fails_gracefully(self):
        with tempfile.TemporaryDirectory() as out_dir:
            code, out, err = run_cli(
                ["convert",
                 os.path.join(FIXTURE_DIR, "edge-malformed-nopages.json"),
                 "-o", out_dir])
        self.assertEqual(code, 1)
        self.assertIn("pages", err)

    def test_non_object_json_fails_gracefully(self):
        with tempfile.TemporaryDirectory() as out_dir:
            code, out, err = run_cli(
                ["convert",
                 os.path.join(FIXTURE_DIR, "edge-malformed-array.json"),
                 "-o", out_dir])
        self.assertEqual(code, 1)
        self.assertIn("must be an object", err)

    def test_truncated_json_fails_gracefully(self):
        with tempfile.TemporaryDirectory() as out_dir:
            code, out, err = run_cli(
                ["convert",
                 os.path.join(FIXTURE_DIR, "edge-malformed-syntax.json"),
                 "-o", out_dir])
        self.assertEqual(code, 1)

    def test_pages_as_string_fails_gracefully(self):
        # A non-list 'pages' must be reported as a per-file error (exit 1)
        # without a traceback and without aborting the batch.
        with tempfile.TemporaryDirectory() as out_dir:
            code, out, err = run_cli(
                ["convert",
                 os.path.join(FIXTURE_DIR,
                              "edge-malformed-pages-string.json"),
                 "-o", out_dir])
        self.assertEqual(code, 1)

    def test_missing_config_file(self):
        with tempfile.TemporaryDirectory() as out_dir:
            code, out, err = run_cli(
                ["convert", SAMPLE, "-o", out_dir,
                 "-c", "/nonexistent/config.json"])
        self.assertEqual(code, 2)
        self.assertIn("config file not found", err)

    def test_passthrough_flag(self):
        with tempfile.TemporaryDirectory() as out_dir:
            code, _, _ = run_cli(
                ["convert", SAMPLE, "-o", out_dir, "--passthrough"])
            self.assertEqual(code, 0)
            path = os.path.join(out_dir, "checkout-service-overview.json")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("NRQL PASSTHROUGH", text)

    def test_page_strategy_split_flag(self):
        with tempfile.TemporaryDirectory() as out_dir:
            code, _, _ = run_cli(
                ["convert", SAMPLE, "-o", out_dir,
                 "--page-strategy", "split"])
            self.assertEqual(code, 0)
            names = sorted(n for n in os.listdir(out_dir)
                           if n != "migration-report.json")
        self.assertEqual(names, [
            "checkout-service-overview--golden-signals.json",
            "checkout-service-overview--logs.json",
            "checkout-service-overview--traces-infra.json"])


class ValidateCommandTests(unittest.TestCase):
    def test_validate_flags_broken_dashboard(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "broken.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"id": 7, "uid": "bad uid!", "title": "",
                           "panels": []}, f)
            code, out, err = run_cli(["validate", path])
        self.assertEqual(code, 1)
        self.assertIn("problem(s)", out)

    def test_validate_flags_invalid_json(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "junk.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            code, out, err = run_cli(["validate", path])
        self.assertEqual(code, 1)
        self.assertIn("INVALID JSON", out)


class ExampleConfigTests(unittest.TestCase):
    def test_prints_valid_json_matching_defaults(self):
        code, out, err = run_cli(["example-config"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), DEFAULT_CONFIG)


class NoCommandTests(unittest.TestCase):
    def test_no_command_prints_help(self):
        code, out, err = run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("usage", out.lower())


if __name__ == "__main__":
    unittest.main()
