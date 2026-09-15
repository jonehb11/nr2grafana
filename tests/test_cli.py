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
from nr2grafana.grafana.client import GrafanaError
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
        self.assertEqual(nr2grafana.__version__, "1.6.0")


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


def _fake_parity_report(summary, score=90):
    panels = []
    n = 0
    for verdict in sorted(summary):
        for _ in range(summary[verdict]):
            n += 1
            panels.append({
                "panel_id": n, "panel_title": "Panel %d" % n,
                "refId": "A", "nrql": "SELECT count(*) FROM Txn",
                "expr": "up", "datasource": "prom1",
                "verdict": verdict, "detail": "detail %d" % n,
                "ratio": None, "nr_summary": {}, "gf_summary": {}})
    return {"schema": "nr2grafana/parity/v1", "dashboard": "Checkout",
            "generated_at": "2026-01-01T00:00:00Z",
            "range": {"from": "now-1h", "to": "now"},
            "panels": panels, "score": score, "summary": summary}


class _PackageMixin(_TempDbMixin):
    """A minimal on-disk package dir for parity/diagnose/heal tests."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.pkg = os.path.join(self.tmp.name, "checkout")
        os.makedirs(self.pkg)
        self.dash = {"title": "Checkout", "uid": "u1",
                     "panels": [{"id": 1, "type": "timeseries",
                                 "title": "Panel 1",
                                 "targets": [{"refId": "A",
                                              "expr": "up"}]}]}
        with open(os.path.join(self.pkg, "dashboard.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.dash, f)
        with open(os.path.join(self.pkg, "widget-report.json"), "w",
                  encoding="utf-8") as f:
            json.dump([{"panel_id": 1, "widget": "w",
                        "confidence": "exact",
                        "nrql": ["SELECT count(*) FROM Txn"],
                        "account_ids": [1234], "queries": [],
                        "notes": []}], f)
        with open(os.path.join(self.pkg, "requirements.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"schema": "nr2grafana/requirements/v1",
                       "dashboard": "Checkout",
                       "datasources": []}, f)

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()


class GrafanaParityTests(_PackageMixin, unittest.TestCase):
    def _run(self, report, extra=()):
        fake = _fake_live(check_rows=[])
        env = {"NEW_RELIC_ACCOUNT_ID": ""}
        with mock.patch.object(cli, "GrafanaLive",
                               return_value=fake) as live_ctor, \
                mock.patch.object(cli, "NerdGraphClient") as nr_ctor, \
                mock.patch.object(cli, "run_parity",
                                  return_value=report) as rp, \
                mock.patch.dict(os.environ, env):
            code, out, err = run_cli(
                ["grafana", "parity", self.pkg,
                 "--url", "http://gr:3000", "--token", "tok",
                 "--api-key", "NRAK-x"] + list(extra))
        return code, out, err, fake, live_ctor, nr_ctor, rp

    def test_match_exits_zero_and_writes_results(self):
        report = _fake_parity_report({"match": 2, "close": 1})
        code, out, err, fake, live_ctor, nr_ctor, rp = \
            self._run(report)
        self.assertEqual(code, 0)
        live_ctor.assert_called_once_with("http://gr:3000",
                                          token="tok", insecure=False)
        nr_ctor.assert_called_once_with("NRAK-x", region="US")
        res = os.path.join(self.pkg, "parity-results.json")
        self.assertTrue(os.path.isfile(res))
        with open(res, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["score"], 90)
        self.assertIn("match", out)
        self.assertIn("score 90/100", out)
        self.assertIn("readiness: ready", out)

    def test_account_ids_from_widget_report(self):
        report = _fake_parity_report({"match": 1})
        _, _, _, _, _, nr_ctor, rp = self._run(report)
        args, kwargs = rp.call_args
        self.assertIs(args[0], nr_ctor.return_value)
        self.assertEqual(args[1], [1234])
        self.assertEqual(args[3]["title"], "Checkout")
        self.assertEqual(kwargs["frm"], "now-1h")
        self.assertEqual(kwargs["to"], "now")

    def test_account_id_flag_wins(self):
        report = _fake_parity_report({"match": 1})
        _, _, _, _, _, _, rp = self._run(
            report, extra=["--account-id", "777",
                           "--from", "now-6h", "--to", "now-1h"])
        args, kwargs = rp.call_args
        self.assertEqual(args[1], [777])
        self.assertEqual(kwargs["frm"], "now-6h")
        self.assertEqual(kwargs["to"], "now-1h")

    def test_gf_error_exits_one(self):
        report = _fake_parity_report({"match": 1, "gf-error": 1},
                                     score=45)
        code, out, _, _, _, _, _ = self._run(report)
        self.assertEqual(code, 1)

    def test_value_mismatch_is_not_failure(self):
        report = _fake_parity_report({"value-mismatch": 1}, score=25)
        code, out, _, _, _, _, _ = self._run(report)
        self.assertEqual(code, 0)
        self.assertIn("readiness: blocked", out)

    def test_persisted_to_store(self):
        self._run(_fake_parity_report({"match": 1}))
        with Store(self.db_path) as store:
            art = store.get_artifact("checkout", "parity")
        self.assertEqual(art["schema"], "nr2grafana/parity/v1")

    def test_missing_api_key_exits_two(self):
        env = dict(os.environ)
        env.pop("NEW_RELIC_API_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                run_cli(["grafana", "parity", self.pkg,
                         "--url", "http://gr:3000", "--token", "t"])
        self.assertEqual(ctx.exception.code, 2)


def _fake_diagnosis(blockers=0, warns=0):
    findings = []
    for i in range(blockers):
        findings.append({
            "id": "b%d" % i, "severity": "blocker",
            "area": "datasource", "problem": "no loki datasource",
            "evidence": "0 of type loki",
            "fix": {"description": "Add a Loki datasource",
                    "kind": "add-datasource", "action": {}}})
    for i in range(warns):
        findings.append({
            "id": "w%d" % i, "severity": "warn", "area": "panel",
            "panel_id": 1, "problem": "metric absent",
            "evidence": "",
            "fix": {"description": "rename the metric",
                    "kind": "edit-query", "action": {}}})
    return {"schema": "nr2grafana/diagnosis/v1",
            "generated_at": "2026-01-01T00:00:00Z",
            "findings": findings,
            "summary": {"findings": len(findings),
                        "blocker": blockers, "warn": warns,
                        "info": 0, "by_area": {}, "panels": []}}


class GrafanaDiagnoseTests(_PackageMixin, unittest.TestCase):
    def _run(self, diagnosis):
        fake = _fake_live()
        env = {"NEW_RELIC_API_KEY": ""}
        with mock.patch.object(cli, "GrafanaLive",
                               return_value=fake) as live_ctor, \
                mock.patch.object(cli, "diagnose",
                                  return_value=diagnosis) as dg, \
                mock.patch.dict(os.environ, env):
            code, out, err = run_cli(
                ["grafana", "diagnose", self.pkg,
                 "--url", "http://gr:3000", "--token", "tok"])
        return code, out, err, fake, live_ctor, dg

    def test_clean_run_exits_zero(self):
        code, out, err, fake, live_ctor, dg = self._run(
            _fake_diagnosis())
        self.assertEqual(code, 0)
        live_ctor.assert_called_once_with("http://gr:3000",
                                          token="tok", insecure=False)
        self.assertIn("no problems found", out)
        self.assertTrue(os.path.isfile(
            os.path.join(self.pkg, "diagnosis.json")))

    def test_diagnose_inputs_wired(self):
        _, _, _, fake, _, dg = self._run(_fake_diagnosis())
        args, kwargs = dg.call_args
        self.assertIs(args[0], fake)
        self.assertIsNone(kwargs["nr"])  # no NR key configured
        self.assertEqual(kwargs["dash"]["title"], "Checkout")
        self.assertEqual(kwargs["requirements"]["schema"],
                         "nr2grafana/requirements/v1")

    def test_blockers_exit_one_and_print_fix(self):
        code, out, err, _, _, _ = self._run(
            _fake_diagnosis(blockers=1, warns=1))
        self.assertEqual(code, 1)
        self.assertIn("no loki datasource", out)
        self.assertIn("Add a Loki datasource", out)
        self.assertIn("blocker", out)
        self.assertIn("1 blocker(s)", err)

    def test_persisted_to_store(self):
        self._run(_fake_diagnosis(warns=1))
        with Store(self.db_path) as store:
            art = store.get_artifact("checkout", "diagnosis")
        self.assertEqual(art["schema"], "nr2grafana/diagnosis/v1")


class GrafanaHealTests(_PackageMixin, unittest.TestCase):
    def _run(self, result, extra=()):
        fake = _fake_live()
        env = {"NEW_RELIC_API_KEY": ""}
        with mock.patch.object(cli, "GrafanaLive",
                               return_value=fake) as live_ctor, \
                mock.patch.object(cli, "auto_heal",
                                  return_value=result) as ah, \
                mock.patch.dict(os.environ, env):
            code, out, err = run_cli(
                ["grafana", "heal", self.pkg,
                 "--url", "http://gr:3000", "--token", "tok"]
                + list(extra))
        return code, out, err, fake, live_ctor, ah

    @staticmethod
    def _result(fixed=1, error=""):
        out = {"rounds": [{"round": 1, "tests": {"data": 1,
                                                 "no-data": 1},
                           "findings": 2, "applied": [],
                           "fixed": fixed}],
               "fixed": fixed, "remaining_findings": [],
               "converged": True}
        if error:
            out["error"] = error
        return out

    def test_heal_prints_round_summary(self):
        code, out, err, fake, _, ah = self._run(self._result())
        self.assertEqual(code, 0)
        self.assertIn("round 1:", out)
        self.assertIn("1 data", out)
        self.assertIn("2 finding(s), 1 fixed", out)
        self.assertIn("1 fix(es) applied, 0 finding(s) remaining",
                      out)
        self.assertIn("(converged)", out)

    def test_auto_heal_wiring(self):
        _, _, _, fake, _, ah = self._run(self._result())
        args, kwargs = ah.call_args
        self.assertIs(args[0], fake)
        self.assertIsNone(args[1])  # no NR key
        self.assertEqual(args[2]["title"], "Checkout")
        self.assertEqual(args[3][0]["panel_id"], 1)  # widget report
        self.assertEqual(args[5], "checkout")  # slug
        self.assertEqual(os.path.realpath(args[6]),
                         os.path.realpath(self.pkg))
        self.assertFalse(kwargs["push"])
        self.assertEqual(kwargs["max_rounds"], 3)

    def test_push_flag_forwarded(self):
        _, _, _, _, _, ah = self._run(self._result(),
                                      extra=["--push"])
        self.assertTrue(ah.call_args[1]["push"])

    def test_error_exits_one(self):
        code, _, err, _, _, _ = self._run(
            self._result(fixed=0, error="cannot test dashboard"))
        self.assertEqual(code, 1)
        self.assertIn("cannot test dashboard", err)

    def test_heal_result_persisted(self):
        self._run(self._result())
        with Store(self.db_path) as store:
            art = store.get_artifact("checkout", "heal")
        self.assertEqual(art["fixed"], 1)


class GrafanaDatasourcesTests(unittest.TestCase):
    def test_list_with_health(self):
        fake = _fake_live()
        fake.datasources.return_value = [
            {"uid": "p1", "name": "Mimir", "type": "prometheus",
             "isDefault": True},
            {"uid": "l1", "name": "Loki", "type": "loki"}]
        fake.datasource_health.side_effect = [
            {"status": "ok", "message": "OK"},
            {"status": "error", "message": "connection refused"}]
        with mock.patch.object(cli, "GrafanaLive",
                               return_value=fake) as ctor:
            code, out, err = run_cli(
                ["grafana", "datasources",
                 "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 0)
        ctor.assert_called_once_with("http://gr:3000", token="tok",
                                     insecure=False)
        self.assertIn("Mimir", out)
        self.assertIn("Loki", out)
        self.assertIn("ok", out)
        self.assertIn("connection refused", out)

    def test_connection_error_exits_one(self):
        fake = _fake_live()
        fake.datasources.side_effect = GrafanaError("401 unauthorized")
        with mock.patch.object(cli, "GrafanaLive", return_value=fake):
            code, out, err = run_cli(
                ["grafana", "datasources",
                 "--url", "http://gr:3000", "--token", "bad"])
        self.assertEqual(code, 1)
        self.assertIn("401", err)

    def test_empty_list_ok(self):
        fake = _fake_live()
        fake.datasources.return_value = []
        with mock.patch.object(cli, "GrafanaLive", return_value=fake):
            code, out, err = run_cli(
                ["grafana", "datasources",
                 "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 0)
        self.assertIn("no datasources", err)


class GrafanaAddDatasourceTests(unittest.TestCase):
    def _run(self, argv, fake=None):
        fake = fake or _fake_live()
        fake.create_datasource.return_value = {
            "datasource": {"uid": "new1"}}
        fake.datasource_health.return_value = {"status": "ok",
                                               "message": ""}
        with mock.patch.object(cli, "GrafanaLive",
                               return_value=fake) as ctor:
            code, out, err = run_cli(argv)
        return code, out, err, fake, ctor

    def test_create_prometheus(self):
        code, out, err, fake, ctor = self._run(
            ["grafana", "add-datasource", "--type", "prometheus",
             "--name", "Mimir", "--set",
             "url=http://mimir:9009/prometheus",
             "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 0)
        ctor.assert_called_once_with("http://gr:3000", token="tok",
                                     insecure=False)
        payload = fake.create_datasource.call_args[0][0]
        self.assertEqual(payload, {
            "name": "Mimir", "type": "prometheus",
            "access": "proxy",
            "url": "http://mimir:9009/prometheus"})
        fake.datasource_health.assert_called_once_with("new1")
        self.assertIn("new1", out)
        self.assertIn("ok", out)

    def test_unknown_type_exits_two(self):
        code, out, err, _, _ = self._run(
            ["grafana", "add-datasource", "--type", "influxdb",
             "--name", "X",
             "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 2)
        self.assertIn("unknown datasource type", err)
        self.assertIn("prometheus", err)  # known types listed

    def test_missing_required_field_exits_two(self):
        code, out, err, _, _ = self._run(
            ["grafana", "add-datasource", "--type", "tempo",
             "--name", "Tempo",
             "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 2)
        self.assertIn("url", err)
        self.assertIn("--set url=", err)

    def test_bad_set_syntax_exits_two(self):
        code, out, err, _, _ = self._run(
            ["grafana", "add-datasource", "--type", "tempo",
             "--name", "Tempo", "--set", "nonsense",
             "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 2)
        self.assertIn("field=value", err)

    def test_secret_fields_prompted_on_tty(self):
        fake = _fake_live()
        fake.create_datasource.return_value = {
            "datasource": {"uid": "cw1"}}
        fake.datasource_health.return_value = {"status": "ok",
                                               "message": ""}
        with mock.patch.object(cli, "GrafanaLive",
                               return_value=fake), \
                mock.patch.object(cli.sys.stdin, "isatty",
                                  return_value=True), \
                mock.patch("getpass.getpass",
                           side_effect=["AKID", "SECRET"]):
            code, out, err = run_cli(
                ["grafana", "add-datasource", "--type", "cloudwatch",
                 "--name", "CW",
                 "--set", "authType=keys",
                 "--set", "defaultRegion=us-east-1",
                 "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 0)
        payload = fake.create_datasource.call_args[0][0]
        self.assertEqual(payload["secureJsonData"],
                         {"accessKey": "AKID", "secretKey": "SECRET"})
        self.assertEqual(payload["jsonData"]["authType"], "keys")
        # secrets never echoed
        self.assertNotIn("SECRET", out)
        self.assertNotIn("SECRET", err)

    def test_health_error_exits_one(self):
        fake = _fake_live()
        fake.create_datasource.return_value = {
            "datasource": {"uid": "new1"}}
        fake.datasource_health.return_value = {
            "status": "error", "message": "unreachable"}
        with mock.patch.object(cli, "GrafanaLive",
                               return_value=fake):
            code, out, err = run_cli(
                ["grafana", "add-datasource", "--type", "tempo",
                 "--name", "Tempo", "--set", "url=http://tempo:3200",
                 "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 1)
        self.assertIn("unreachable", out)

    def test_create_failure_exits_one(self):
        fake = _fake_live()
        fake.create_datasource.side_effect = GrafanaError("403")
        with mock.patch.object(cli, "GrafanaLive",
                               return_value=fake):
            code, out, err = run_cli(
                ["grafana", "add-datasource", "--type", "tempo",
                 "--name", "Tempo", "--set", "url=http://tempo:3200",
                 "--url", "http://gr:3000", "--token", "tok"])
        self.assertEqual(code, 1)
        self.assertIn("Admin", err)


class NewGrafanaCommandsListedTests(unittest.TestCase):
    def test_grafana_help_lists_new_commands(self):
        code, out, err = run_cli(["grafana"])
        self.assertEqual(code, 2)
        for name in ("parity", "diagnose", "heal", "datasources",
                     "add-datasource"):
            self.assertIn(name, out)


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


# ---------------------------------------------------------------------------
# cost analyze / pricing (1.5) -- sibling modules stubbed in sys.modules
# ---------------------------------------------------------------------------

def _cost_stub_modules():
    """Fake traffic/usage/costmodel/optimize modules matching the
    section 2-5 contracts, installed under their nr2grafana.* names."""
    import types

    traffic = types.ModuleType("nr2grafana.traffic")
    traffic.calls = []

    def sample_traffic(grafana, ds_list, frm="now-24h", to="now",
                       log=None):
        traffic.calls.append({"ds_list": ds_list, "frm": frm, "to": to})
        if log:
            log("traffic stub")
        return {"schema": "nr2grafana/traffic/v1",
                "range": {"from": frm, "to": to},
                "datasources": [{"family": d["family"], "uid": d["uid"]}
                                for d in ds_list]}

    traffic.sample_traffic = sample_traffic

    usage = types.ModuleType("nr2grafana.usage")
    usage.calls = []

    def collect_usage(dashboards, widget_reports):
        usage.calls.append({"dashboards": len(dashboards),
                            "reports": len(widget_reports)})
        return {"prometheus": {"metrics": ["up"], "labels": ["job"]},
                "loki": {"stream_labels": ["namespace"]}, "tempo": {}}

    usage.collect_usage = collect_usage

    costmodel = types.ModuleType("nr2grafana.costmodel")
    costmodel.DEFAULT_PRICING = {"loki_gb_ingest": 0.5,
                                 "mimir_1k_series_month": 0.6}

    def estimate_costs(traffic_data, pricing=None):
        return {"schema": "nr2grafana/cost/v1",
                "pricing": dict(pricing or costmodel.DEFAULT_PRICING),
                "components": [], "monthly_total": 100.0,
                "resources": {}}

    def apply_savings(cost, recommendations):
        saved = sum((r.get("est_savings") or {}).get("monthly_usd", 0)
                    for r in recommendations or [])
        return {"projected_total": 100.0 - saved, "saved_total": saved,
                "saved_pct": int(saved), "per_component": []}

    costmodel.estimate_costs = estimate_costs
    costmodel.apply_savings = apply_savings

    optimize = types.ModuleType("nr2grafana.optimize")

    def recommend(traffic_data, usage_data, cost=None, pricing=None,
                  cfg=None, log=None):
        if log:
            log("optimize stub")
        return {"schema": "nr2grafana/optimize/v1",
                "recommendations": [{
                    "id": "loki-drop-label-pod", "family": "loki",
                    "kind": "drop-label", "severity": "high",
                    "title": "Drop stream label pod (unused)",
                    "keeps_intact": True,
                    "est_savings": {"monthly_usd": 12.5,
                                    "confidence": "high"},
                    "config": [{"target": "promtail", "language": "yaml",
                                "snippet": "pipeline_stages:\n  - "
                                           "labeldrop:\n      - pod",
                                "note": "apply at the agent"}]}],
                "summary": {"safe_count": 1}}

    optimize.recommend = recommend
    return {"nr2grafana.traffic": traffic,
            "nr2grafana.usage": usage,
            "nr2grafana.costmodel": costmodel,
            "nr2grafana.optimize": optimize}


class CostAnalyzeTests(_PackageMixin, unittest.TestCase):
    """cost analyze plumbing with a stubbed GrafanaLive + siblings."""

    def _fake_client(self):
        fake = mock.MagicMock()
        fake.datasources.return_value = [
            {"name": "Mimir", "type": "prometheus", "uid": "mimir",
             "isDefault": True},
            {"name": "Loki", "type": "loki", "uid": "loki1"},
            {"name": "CW", "type": "cloudwatch", "uid": "cw"}]
        return fake

    def test_analyze_writes_report_and_config(self):
        out = os.path.join(self.tmp.name, "cost-out")
        stubs = _cost_stub_modules()
        with mock.patch.dict("sys.modules", stubs), \
                mock.patch.object(cli, "GrafanaLive",
                                  return_value=self._fake_client()):
            code, sout, serr = run_cli(
                ["cost", "analyze", self.pkg, "--url", "http://gr:3000",
                 "--token", "tok", "-o", out])
        self.assertEqual(code, 0)
        # report + config snippets on disk
        report_path = os.path.join(out, "cost-report.json")
        self.assertTrue(os.path.isfile(report_path))
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["schema"], "nr2grafana/cost-report/v1")
        self.assertEqual(report["optimize"]["schema"],
                         "nr2grafana/optimize/v1")
        promtail = os.path.join(out, "config", "promtail.yaml")
        self.assertTrue(os.path.isfile(promtail))
        with open(promtail, encoding="utf-8") as f:
            self.assertIn("labeldrop", f.read())
        self.assertTrue(os.path.isfile(
            os.path.join(out, "config", "README.md")))
        # ranked table + estimate + non-exact-bill disclaimer printed
        self.assertIn("EST $/MO SAVED", sout)
        self.assertIn("Drop stream label pod", sout)
        self.assertIn("$12.50", sout)
        self.assertIn("current:", sout)
        self.assertIn("ESTIMATES", sout)
        # only prometheus/loki datasources were sampled (cloudwatch out)
        sampled = stubs["nr2grafana.traffic"].calls[-1]["ds_list"]
        fams = sorted(d["family"] for d in sampled)
        self.assertEqual(fams, ["loki", "prometheus"])
        # usage was computed from the packaged dashboard
        self.assertEqual(stubs["nr2grafana.usage"].calls[-1]["dashboards"],
                         1)

    def test_analyze_no_datasources_exits_one(self):
        fake = mock.MagicMock()
        fake.datasources.return_value = [
            {"name": "CW", "type": "cloudwatch", "uid": "cw"}]
        with mock.patch.dict("sys.modules", _cost_stub_modules()), \
                mock.patch.object(cli, "GrafanaLive",
                                  return_value=fake):
            code, sout, serr = run_cli(
                ["cost", "analyze", self.pkg, "--url", "http://gr:3000",
                 "--token", "tok", "-o", self.tmp.name])
        self.assertEqual(code, 1)
        self.assertIn("no Loki/Prometheus/Tempo", serr)

    def test_analyze_missing_url_exits_two(self):
        env = dict(os.environ)
        env.pop("GRAFANA_URL", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                run_cli(["cost", "analyze", self.pkg])
        self.assertEqual(ctx.exception.code, 2)


class CostPricingTests(_TempDbMixin, unittest.TestCase):
    def test_pricing_prints_defaults(self):
        with mock.patch.dict("sys.modules", _cost_stub_modules()):
            code, sout, serr = run_cli(["cost", "pricing"])
        self.assertEqual(code, 0)
        pricing = json.loads(sout)
        self.assertEqual(pricing["loki_gb_ingest"], 0.5)
        self.assertIn("mimir_1k_series_month", pricing)

    def test_pricing_set_persists_to_store(self):
        with mock.patch.dict("sys.modules", _cost_stub_modules()):
            code, sout, serr = run_cli(
                ["cost", "pricing", "--set", "loki_gb_ingest=0.9",
                 "--set", "retention_days=14"])
        self.assertEqual(code, 0)
        pricing = json.loads(sout)
        self.assertEqual(pricing["loki_gb_ingest"], 0.9)
        self.assertEqual(pricing["retention_days"], 14)
        # persisted as the non-secret web.pricing setting
        store = Store(self.db_path)
        try:
            saved = store.get_setting("web.pricing")
        finally:
            store.close()
        self.assertEqual(saved["loki_gb_ingest"], 0.9)
        self.assertEqual(saved["retention_days"], 14)

    def test_pricing_bad_set_exits_two(self):
        with mock.patch.dict("sys.modules", _cost_stub_modules()):
            code, sout, serr = run_cli(
                ["cost", "pricing", "--set", "noequalssign"])
        self.assertEqual(code, 2)
        self.assertIn("key=value", serr)

    def test_cost_without_subcommand_prints_help(self):
        code, out, err = run_cli(["cost"])
        self.assertEqual(code, 2)
        self.assertIn("analyze", out)


# ---------------------------------------------------------------------------
# deep-dive / ai-context / mcp (1.6) -- siblings stubbed in sys.modules
# ---------------------------------------------------------------------------

def _deepdive_stub_modules(kubectl=True):
    """Fake deepdive + packing modules matching the section 1-2
    contracts."""
    import types

    deepdive = types.ModuleType("nr2grafana.deepdive")
    deepdive.calls = []

    def analyze(prom=None, mimir=None, loki=None, grafana=None,
                cfg=None, log=None):
        deepdive.calls.append({"prom": prom, "mimir": mimir,
                               "loki": loki, "cfg": cfg})
        if log:
            log("deepdive stub analyzing")
        return {"schema": "nr2grafana/deepdive/v1",
                "findings": [
                    {"severity": "FAIL", "area": "capacity",
                     "title": "Ingester near GOMEMLIMIT series capacity",
                     "evidence": {"series": 9500000},
                     "rationale": "10M limit is not capacity",
                     "config": [], "est_savings": {},
                     "keeps_performance": True, "keeps_durability": True,
                     "keeps_availability": True},
                    {"severity": "WARN", "area": "cardinality",
                     "title": "Drop unused high-cardinality metric",
                     "evidence": {"series": 400000},
                     "rationale": "no dashboard queries it",
                     "config": [{"target": "prometheus-relabel",
                                 "language": "yaml",
                                 "snippet": "write_relabel_configs:\n"
                                            "  - action: drop",
                                 "note": "drop at remote_write"}],
                     "est_savings": {"monthly_usd": 30.0,
                                     "compute": {"cores": 2.0}},
                     "keeps_performance": True, "keeps_durability": True,
                     "keeps_availability": True}]}

    deepdive.analyze = analyze

    packing = types.ModuleType("nr2grafana.packing")
    packing.calls = []

    def kubectl_available():
        return kubectl

    def pack_analyze(cfg=None, prices=None, log=None):
        packing.calls.append({"cfg": cfg, "prices": prices})
        if log:
            log("packing stub analyzing")
        return {"schema": "nr2grafana/packing/v1", "available": True,
                "findings": [
                    {"severity": "FAIL", "area": "durability",
                     "title": "Ingester has a CPU limit",
                     "evidence": {"limit": "2"},
                     "config": [],
                     "keeps_durability": False,
                     "keeps_availability": True,
                     "keeps_performance": False}],
                "karpenter": {
                    "nodepools": [],
                    "findings": [],
                    "proposed_nodepool_yaml":
                        "apiVersion: karpenter.sh/v1\nkind: NodePool\n",
                    "est_savings": {"monthly_usd": 120.0, "nodes": 2,
                                    "keeps_availability": True}}}

    packing.kubectl_available = kubectl_available
    packing.analyze = pack_analyze
    return {"nr2grafana.deepdive": deepdive,
            "nr2grafana.packing": packing}


class DeepDiveCommandTests(_TempDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "dd-out")

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def _run(self, argv, kubectl=True):
        env = dict(os.environ)
        env.pop("GRAFANA_URL", None)
        stubs = _deepdive_stub_modules(kubectl=kubectl)
        with mock.patch.dict("sys.modules", stubs), \
                mock.patch.dict(os.environ, env, clear=True):
            os.environ["N2G_DB"] = self.db_path
            code, sout, serr = run_cli(argv)
        return code, sout, serr, stubs

    def test_deepdive_prints_findings_and_writes_report(self):
        code, sout, serr, stubs = self._run(
            ["deepdive", "--prom", "http://mimir:9090", "-o", self.out])
        # exit 1: a FAIL-severity finding is present
        self.assertEqual(code, 1)
        self.assertIn("LGTM stack findings", sout)
        self.assertIn("Ingester near GOMEMLIMIT", sout)
        self.assertIn("$30.00", sout)
        # safe savings headline (WARN finding keeps everything)
        self.assertIn("saveable without reducing durability", sout)
        # report + config snippet on disk
        report_path = os.path.join(self.out, "deepdive-report.json")
        self.assertTrue(os.path.isfile(report_path))
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["deepdive"]["schema"],
                         "nr2grafana/deepdive/v1")
        relabel = os.path.join(self.out, "config",
                               "prometheus-relabel.yaml")
        self.assertTrue(os.path.isfile(relabel))
        with open(relabel, encoding="utf-8") as f:
            self.assertIn("action: drop", f.read())
        # prom url threaded through to analyze
        self.assertEqual(stubs["nr2grafana.deepdive"].calls[-1]["prom"],
                         "http://mimir:9090")
        # packing not run without --kube
        self.assertEqual(stubs["nr2grafana.packing"].calls, [])

    def test_deepdive_kube_runs_packing_and_writes_nodepool(self):
        code, sout, serr, stubs = self._run(
            ["deepdive", "--kube", "-o", self.out], kubectl=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(stubs["nr2grafana.packing"].calls), 1)
        self.assertIn("Karpenter", sout)
        nodepool = os.path.join(self.out, "config",
                                "karpenter-nodepool.yaml")
        self.assertTrue(os.path.isfile(nodepool))
        with open(nodepool, encoding="utf-8") as f:
            self.assertIn("kind: NodePool", f.read())
        with open(os.path.join(self.out, "deepdive-report.json"),
                  encoding="utf-8") as f:
            report = json.load(f)
        self.assertIn("packing", report)

    def test_deepdive_kube_without_kubectl_degrades(self):
        code, sout, serr, stubs = self._run(
            ["deepdive", "--kube", "-o", self.out], kubectl=False)
        # still succeeds-with-findings; packing skipped, note printed
        self.assertEqual(stubs["nr2grafana.packing"].calls, [])
        self.assertIn("kubectl not available", serr)


def _aicontext_stub_module():
    import types
    m = types.ModuleType("nr2grafana.aicontext")
    m.calls = []

    def build_context(store, slug="", include=None, grafana=None,
                      deepdive=None, redact=True):
        m.calls.append({"slug": slug, "deepdive": deepdive,
                        "redact": redact})
        return {"schema": "nr2grafana/ai-context/v1", "slug": slug,
                "preamble": "you are troubleshooting", "artifacts": {}}

    def to_markdown(context):
        return "# AI context\n\nslug: %s\n" % context.get("slug", "")

    def troubleshoot(assistant, context, question=""):
        return {"answer": "stub answer to: %s" % question,
                "backend": getattr(assistant, "backend", "api")}

    m.build_context = build_context
    m.to_markdown = to_markdown
    m.troubleshoot = troubleshoot
    return m


class AiContextCommandTests(_TempDbMixin, unittest.TestCase):
    def test_ai_context_json_to_stdout(self):
        stub = _aicontext_stub_module()
        with mock.patch.dict("sys.modules",
                             {"nr2grafana.aicontext": stub}):
            code, sout, serr = run_cli(["ai-context"])
        self.assertEqual(code, 0)
        bundle = json.loads(sout)
        self.assertEqual(bundle["schema"], "nr2grafana/ai-context/v1")
        self.assertTrue(stub.calls[-1]["redact"])

    def test_ai_context_markdown_to_file(self):
        stub = _aicontext_stub_module()
        out = os.path.join(self._db_tmp.name, "ctx.md")
        with mock.patch.dict("sys.modules",
                             {"nr2grafana.aicontext": stub}):
            code, sout, serr = run_cli(
                ["ai-context", "mydash", "--markdown", "-o", out])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(out))
        with open(out, encoding="utf-8") as f:
            self.assertIn("# AI context", f.read())
        self.assertEqual(stub.calls[-1]["slug"], "mydash")


class AiTroubleshootCommandTests(_TempDbMixin, unittest.TestCase):
    def test_troubleshoot_no_backend_exits_two(self):
        stub = _aicontext_stub_module()
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("N2G_AI_COMMAND", None)
        with mock.patch.dict("sys.modules",
                             {"nr2grafana.aicontext": stub}), \
                mock.patch.dict(os.environ, env, clear=True):
            os.environ["N2G_DB"] = self.db_path
            code, sout, serr = run_cli(
                ["ai", "troubleshoot", "-q", "why no data?"])
        self.assertEqual(code, 2)
        self.assertIn("no AI backend", serr)

    def test_troubleshoot_with_api_key(self):
        stub = _aicontext_stub_module()
        env = dict(os.environ)
        env["ANTHROPIC_API_KEY"] = "sk-ant-test"
        with mock.patch.dict("sys.modules",
                             {"nr2grafana.aicontext": stub}), \
                mock.patch.dict(os.environ, env, clear=True):
            os.environ["N2G_DB"] = self.db_path
            code, sout, serr = run_cli(
                ["ai", "troubleshoot", "-q", "why no data?"])
        self.assertEqual(code, 0)
        self.assertIn("stub answer to: why no data?", sout)


class McpCommandTests(unittest.TestCase):
    def test_mcp_config_references_env_never_token(self):
        code, sout, serr = run_cli(
            ["mcp", "config", "--kind", "claude",
             "--url", "http://gf.local:3000"])
        self.assertEqual(code, 0)
        cfg = json.loads(sout)
        self.assertIn("mcpServers", cfg)
        entry = cfg["mcpServers"]["grafana"]
        # token is referenced via env var, never embedded
        self.assertEqual(entry["env"]["GRAFANA_SERVICE_ACCOUNT_TOKEN"],
                         "${GRAFANA_SERVICE_ACCOUNT_TOKEN}")
        self.assertNotIn("glsa_", sout)

    def test_mcp_config_writes_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = os.path.join(tmp.name, "mcp.json")
        code, sout, serr = run_cli(
            ["mcp", "config", "--kind", "kiro", "-o", out])
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("grafana", cfg["mcpServers"])

    def test_mcp_config_no_grafana(self):
        code, sout, serr = run_cli(["mcp", "config", "--no-grafana"])
        self.assertEqual(code, 0)
        cfg = json.loads(sout)
        self.assertNotIn("grafana", cfg["mcpServers"])

    def test_mcp_probe_requires_target(self):
        code, sout, serr = run_cli(["mcp", "probe"])
        self.assertEqual(code, 2)
        self.assertIn("--url", serr)

    def test_mcp_probe_ok(self):
        import types
        stub = types.ModuleType("nr2grafana.mcp")

        def probe(command=None, url=""):
            return {"ok": True, "tools": ["search_dashboards"],
                    "server": {"name": "grafana"}}

        stub.probe = probe
        with mock.patch.dict("sys.modules", {"nr2grafana.mcp": stub}):
            code, sout, serr = run_cli(
                ["mcp", "probe", "--command", "mcp-grafana"])
        self.assertEqual(code, 0)
        self.assertIn("reachable", sout)
        self.assertIn("search_dashboards", sout)

    def test_mcp_probe_failure_exits_one(self):
        import types
        stub = types.ModuleType("nr2grafana.mcp")
        stub.probe = lambda command=None, url="": {
            "ok": False, "tools": [], "error": "connection refused"}
        with mock.patch.dict("sys.modules", {"nr2grafana.mcp": stub}):
            code, sout, serr = run_cli(
                ["mcp", "probe", "--url", "http://x:1/sse"])
        self.assertEqual(code, 1)
        self.assertIn("connection refused", serr)

    def test_mcp_without_subcommand_prints_help(self):
        code, out, err = run_cli(["mcp"])
        self.assertEqual(code, 2)
        self.assertIn("config", out)


class NewCommandsListedTests(unittest.TestCase):
    def test_new_commands_appear_in_top_level_help(self):
        code, out, err = run_cli([])
        self.assertEqual(code, 2)
        for name in ("deepdive", "ai-context", "ai", "mcp"):
            self.assertIn(name, out)


if __name__ == "__main__":
    unittest.main()
