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
from unittest import mock

import nr2grafana
from nr2grafana import cli
from nr2grafana.config import DEFAULT_CONFIG
from nr2grafana.store import Store

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

    def test_new_commands_listed_in_help(self):
        code, out, err = run_cli([])
        for name in ("analyze", "grafana", "changes", "web"):
            self.assertIn(name, out)


class VersionTests(unittest.TestCase):
    def test_package_version(self):
        self.assertEqual(nr2grafana.__version__, "1.1.0")


class _TempDbMixin:
    """Points the CLI's Store at a temp db so tests never touch
    ~/.nr2grafana."""

    def setUp(self):
        self._db_tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._db_tmp.name, "test.db")
        self._old_db = os.environ.get("N2G_DB")
        os.environ["N2G_DB"] = self.db_path

    def tearDown(self):
        if self._old_db is None:
            os.environ.pop("N2G_DB", None)
        else:
            os.environ["N2G_DB"] = self._old_db
        self._db_tmp.cleanup()


PACKAGE_FILES = ("dashboard.json", "requirements.json",
                 "widget-report.json", "datatest.json", "README.md",
                 "test.sh")


class ConvertPackageTests(_TempDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = self.tmp.name
        self.code, self.stdout, self.stderr = run_cli(
            ["convert", SAMPLE, "-o", self.out_dir, "--package"])
        self.pkg = os.path.join(self.out_dir,
                                "checkout-service-overview")

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_exit_code_zero(self):
        self.assertEqual(self.code, 0)

    def test_package_dir_with_all_artifacts(self):
        self.assertTrue(os.path.isdir(self.pkg))
        for name in PACKAGE_FILES:
            self.assertTrue(
                os.path.isfile(os.path.join(self.pkg, name)),
                "missing %s" % name)

    def test_test_sh_is_executable(self):
        mode = os.stat(os.path.join(self.pkg, "test.sh")).st_mode
        self.assertTrue(mode & 0o111)

    def test_no_flat_dashboard_file(self):
        self.assertFalse(os.path.exists(
            os.path.join(self.out_dir,
                         "checkout-service-overview.json")))

    def test_index_written(self):
        idx = os.path.join(self.out_dir, "INDEX.md")
        self.assertTrue(os.path.isfile(idx))
        with open(idx, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Checkout Service Overview", text)
        self.assertIn("checkout-service-overview/", text)

    def test_requirements_schema(self):
        with open(os.path.join(self.pkg, "requirements.json"),
                  encoding="utf-8") as f:
            req = json.load(f)
        self.assertEqual(req["schema"], "nr2grafana/requirements/v1")
        self.assertTrue(req["datasources"])

    def test_migration_report_points_into_package(self):
        with open(os.path.join(self.out_dir, "migration-report.json"),
                  encoding="utf-8") as f:
            report = json.load(f)
        out = report["reports"][0]["output"]
        self.assertTrue(out.endswith(
            os.path.join("checkout-service-overview",
                         "dashboard.json")))

    def test_persisted_to_store(self):
        with Store(self.db_path) as store:
            row = store.get_dashboard("checkout-service-overview")
            self.assertIsNotNone(row)
            req = store.get_artifact("checkout-service-overview",
                                     "requirements")
            self.assertEqual(req["schema"],
                             "nr2grafana/requirements/v1")
            wr = store.get_artifact("checkout-service-overview",
                                    "widget-report")
            self.assertTrue(wr["widgets"])
            pkg = store.get_setting(
                "package_dir.checkout-service-overview", "")
            self.assertEqual(os.path.realpath(pkg),
                             os.path.realpath(self.pkg))
            runs = store.list_runs("convert")
            self.assertTrue(runs)
            self.assertEqual(runs[0]["status"], "ok")


class AnalyzeCommandTests(_TempDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = self.tmp.name
        code, _, _ = run_cli(["convert", SAMPLE, "-o", self.out_dir])
        assert code == 0

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_analyze_converted_dir(self):
        code, out, err = run_cli(["analyze", self.out_dir])
        self.assertEqual(code, 0)
        pkg = os.path.join(self.out_dir, "checkout-service-overview")
        for name in PACKAGE_FILES:
            self.assertTrue(
                os.path.isfile(os.path.join(pkg, name)),
                "missing %s" % name)
        self.assertTrue(os.path.isfile(
            os.path.join(self.out_dir, "INDEX.md")))
        # widget report recovered from migration-report.json
        with open(os.path.join(pkg, "widget-report.json"),
                  encoding="utf-8") as f:
            self.assertTrue(json.load(f))

    def test_analyze_nr_json_directly(self):
        with tempfile.TemporaryDirectory() as out:
            code, _, err = run_cli(["analyze", SAMPLE, "-o", out])
            self.assertEqual(code, 0)
            pkg = os.path.join(out, "checkout-service-overview")
            self.assertTrue(os.path.isfile(
                os.path.join(pkg, "dashboard.json")))

    def test_analyze_packaged_output_dir(self):
        # `convert --package` output: dashboards live in per-dashboard
        # subdirectories, and analyze must find them there.
        with tempfile.TemporaryDirectory() as out:
            code, _, _ = run_cli(
                ["convert", SAMPLE, "-o", out, "--package"])
            self.assertEqual(code, 0)
            pkg = os.path.join(out, "checkout-service-overview")
            code, _, err = run_cli(["analyze", out])
            self.assertEqual(code, 0, err)
            for name in PACKAGE_FILES:
                self.assertTrue(
                    os.path.isfile(os.path.join(pkg, name)),
                    "missing %s" % name)
            # widget report preserved (via migration-report.json keyed
            # by slug, or the package's own widget-report.json)
            with open(os.path.join(pkg, "widget-report.json"),
                      encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)), 18)

    def test_analyze_single_package_dir(self):
        # analyze on one package dir regenerates it in place instead of
        # nesting a new package inside it.
        with tempfile.TemporaryDirectory() as out:
            code, _, _ = run_cli(
                ["convert", SAMPLE, "-o", out, "--package"])
            self.assertEqual(code, 0)
            pkg = os.path.join(out, "checkout-service-overview")
            code, _, err = run_cli(["analyze", pkg])
            self.assertEqual(code, 0, err)
            self.assertFalse(os.path.isdir(
                os.path.join(pkg, "checkout-service-overview")))
            with open(os.path.join(pkg, "widget-report.json"),
                      encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)), 18)

    def test_analyze_nothing_packagable(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "junk.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"neither": True}, f)
            code, _, err = run_cli(["analyze", path])
        self.assertEqual(code, 1)
        self.assertIn("not a Grafana or New Relic dashboard", err)


def _fake_live(**kwargs):
    """A GrafanaLive stand-in whose class-level mock records the
    constructor args."""
    fake = mock.MagicMock()
    fake.check_requirements.return_value = kwargs.get("check_rows", [])
    fake.test_dashboard.return_value = kwargs.get("test_rows", [])
    fake.health.return_value = {"version": "12.0.0"}
    fake.find_or_create_folder.return_value = "fold1"
    fake.import_dashboard.return_value = {"url": "/d/abc"}
    return fake


class GrafanaCheckTests(_TempDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.req_path = os.path.join(self.tmp.name, "requirements.json")
        with open(self.req_path, "w", encoding="utf-8") as f:
            json.dump({"schema": "nr2grafana/requirements/v1",
                       "dashboard": "Checkout",
                       "datasources": [{"family": "prometheus",
                                        "plugin_id": "prometheus",
                                        "core": True,
                                        "uid_ref": "${datasource}"}]},
                      f)

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_all_ok_exits_zero(self):
        fake = _fake_live(check_rows=[
            {"item": "datasource:prometheus", "status": "ok",
             "detail": "uid prom1", "fix": ""}])
        with mock.patch.object(cli, "GrafanaLive",
                               return_value=fake) as ctor:
            code, out, err = run_cli(
                ["grafana", "check", self.req_path,
                 "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 0)
        ctor.assert_called_once_with("http://gr:3000", token="tok",
                                     insecure=False)
        fake.check_requirements.assert_called_once()
        self.assertIn("ok", out)

    def test_missing_exits_one_with_fix(self):
        fake = _fake_live(check_rows=[
            {"item": "datasource:loki", "status": "missing",
             "detail": "no loki datasource",
             "fix": "Add a Loki datasource"}])
        with mock.patch.object(cli, "GrafanaLive", return_value=fake):
            code, out, err = run_cli(
                ["grafana", "check", self.req_path,
                 "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 1)
        self.assertIn("Add a Loki datasource", out)

    def test_url_from_environment(self):
        fake = _fake_live(check_rows=[])
        env = {"GRAFANA_URL": "http://env-gr:3000",
               "GRAFANA_TOKEN": "envtok"}
        with mock.patch.object(cli, "GrafanaLive",
                               return_value=fake) as ctor, \
                mock.patch.dict(os.environ, env):
            code, _, _ = run_cli(["grafana", "check", self.req_path])
        self.assertEqual(code, 0)
        ctor.assert_called_once_with("http://env-gr:3000",
                                     token="envtok", insecure=False)

    def test_missing_url_exits_two(self):
        env = dict(os.environ)
        env.pop("GRAFANA_URL", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                run_cli(["grafana", "check", self.req_path])
        self.assertEqual(ctx.exception.code, 2)

    def test_no_requirements_found_exits_two(self):
        with tempfile.TemporaryDirectory() as empty:
            code, _, err = run_cli(
                ["grafana", "check", empty,
                 "--url", "http://gr:3000", "--token", "t"])
        self.assertEqual(code, 2)
        self.assertIn("requirements.json", err)


class GrafanaTestCommandTests(_TempDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.pkg = os.path.join(self.tmp.name, "checkout")
        os.makedirs(self.pkg)
        self.dash_path = os.path.join(self.pkg, "dashboard.json")
        with open(self.dash_path, "w", encoding="utf-8") as f:
            json.dump({"title": "Checkout", "panels": []}, f)

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def _run(self, rows):
        fake = _fake_live(test_rows=rows)
        with mock.patch.object(cli, "GrafanaLive", return_value=fake):
            return run_cli(
                ["grafana", "test", self.pkg,
                 "--url", "http://gr:3000", "--token", "tok"])

    def test_no_data_is_warning_not_failure(self):
        code, out, err = self._run([
            {"panel_id": 1, "refId": "A", "status": "data",
             "error": "", "frames": 1, "points": 5},
            {"panel_id": 2, "refId": "A", "status": "no-data",
             "error": "", "frames": 0, "points": 0}])
        self.assertEqual(code, 0)
        results = os.path.join(self.pkg, "datatest-results.json")
        self.assertTrue(os.path.isfile(results))
        with open(results, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(payload["summary"],
                         {"data": 1, "no-data": 1})

    def test_error_panel_exits_one(self):
        code, _, _ = self._run([
            {"panel_id": 1, "refId": "A", "status": "error",
             "error": "parse error", "frames": 0, "points": 0}])
        self.assertEqual(code, 1)

    def test_results_persisted_to_store(self):
        self._run([{"panel_id": 1, "refId": "A", "status": "data",
                    "error": "", "frames": 1, "points": 2}])
        with Store(self.db_path) as store:
            dt = store.get_artifact("checkout", "datatest")
        self.assertEqual(dt["summary"], {"data": 1})


class GrafanaImportCommandTests(_TempDbMixin, unittest.TestCase):
    def test_import_package_dir(self):
        fake = _fake_live()
        with tempfile.TemporaryDirectory() as work:
            pkg = os.path.join(work, "checkout")
            os.makedirs(pkg)
            with open(os.path.join(pkg, "dashboard.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"title": "Checkout", "panels": []}, f)
            with mock.patch.object(cli, "GrafanaLive",
                                   return_value=fake):
                code, out, err = run_cli(
                    ["grafana", "import", work, "--folder", "Team",
                     "--overwrite",
                     "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 0)
        fake.find_or_create_folder.assert_called_once_with("Team")
        _, kwargs = fake.import_dashboard.call_args
        self.assertEqual(kwargs["folder_uid"], "fold1")
        self.assertTrue(kwargs["overwrite"])
        self.assertIn("1 imported, 0 failed", err)

    def test_grafana_without_subcommand_prints_help(self):
        code, out, err = run_cli(["grafana"])
        self.assertEqual(code, 2)
        self.assertIn("check", out)
        self.assertIn("import", out)


class ChangesCommandTests(_TempDbMixin, unittest.TestCase):
    def test_report_empty_db(self):
        code, out, err = run_cli(["changes", "report"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["schema"], "nr2grafana/changes/v1")
        self.assertEqual(report["total"], 0)

    def test_report_markdown_empty(self):
        code, out, err = run_cli(["changes", "report", "--markdown"])
        self.assertEqual(code, 0)
        self.assertIn("No recorded changes", out)

    def test_suggest_config_empty(self):
        code, out, err = run_cli(["changes", "suggest-config"])
        self.assertEqual(code, 0)
        suggestion = json.loads(out)
        self.assertEqual(suggestion["overlay"], {})

    def test_report_after_recorded_change(self):
        with Store(self.db_path) as store:
            store.log_change("checkout", {
                "action": "query-edit", "target": "panel 1/A",
                "before": "up", "after": "up == 1",
                "why": "test", "source": "user"})
        code, out, err = run_cli(
            ["changes", "report", "--slug", "checkout"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["total"], 1)

    def test_changes_without_subcommand_prints_help(self):
        code, out, err = run_cli(["changes"])
        self.assertEqual(code, 2)
        self.assertIn("suggest-config", out)


class WebCommandTests(unittest.TestCase):
    def test_web_command_registered(self):
        code, out, err = run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("web", out)

    def test_web_flags_parse(self):
        # --help exits 0 via argparse and mentions the flags
        with self.assertRaises(SystemExit) as ctx:
            run_cli(["web", "--help"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
