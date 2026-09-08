"""Unit tests for nr2grafana.store (sqlite3 persistent storage).

All tests run against a temp-file database; nothing touches
~/.nr2grafana.
"""

import os
import sqlite3
import tempfile
import threading
import unittest

from nr2grafana.store import (
    ARTIFACT_KINDS,
    SCHEMA_VERSION,
    Store,
    StoreError,
)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.store = Store(self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class SchemaTests(StoreTestCase):
    def test_wal_mode_enabled(self):
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_user_version_set(self):
        conn = sqlite3.connect(self.db_path)
        try:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(ver, SCHEMA_VERSION)

    def test_reopen_is_idempotent(self):
        # migrations must not run twice / break an existing db
        self.store.set_setting("grafana_url", "http://localhost:3000")
        self.store.close()
        with Store(self.db_path) as again:
            self.assertEqual(again.get_setting("grafana_url"),
                             "http://localhost:3000")

    def test_creates_parent_directory(self):
        path = os.path.join(self.tmp.name, "deep", "nested", "s.db")
        with Store(path) as s:
            s.set_setting("a", 1)
        self.assertTrue(os.path.exists(path))

    def test_context_manager_closes(self):
        path = os.path.join(self.tmp.name, "cm.db")
        with Store(path) as s:
            s.set_setting("x", True)
        self.assertRaises(Exception, s.set_setting, "y", 1)


class RunTests(StoreTestCase):
    def test_record_and_finish_run(self):
        run_id = self.store.record_run("convert", {"inputs": 3})
        self.assertIsInstance(run_id, int)
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["id"], run_id)
        self.assertEqual(run["kind"], "convert")
        self.assertEqual(run["meta"], {"inputs": 3})
        self.assertEqual(run["status"], "running")
        self.assertIsNone(run["finished_at"])
        self.assertTrue(run["started_at"].endswith("Z"))

        self.store.finish_run(run_id, "ok", {"dashboards": 2})
        run = self.store.list_runs()[0]
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["summary"], {"dashboards": 2})
        self.assertTrue(run["finished_at"].endswith("Z"))

    def test_list_runs_filters_and_orders(self):
        a = self.store.record_run("convert", {})
        b = self.store.record_run("test", {})
        c = self.store.record_run("convert", {})
        all_runs = self.store.list_runs()
        self.assertEqual([r["id"] for r in all_runs], [c, b, a])
        conv = self.store.list_runs(kind="convert")
        self.assertEqual([r["id"] for r in conv], [c, a])

    def test_list_runs_limit(self):
        for _ in range(5):
            self.store.record_run("convert", {})
        self.assertEqual(len(self.store.list_runs(limit=2)), 2)


class DashboardTests(StoreTestCase):
    def test_upsert_and_get_roundtrip(self):
        data = {"title": "Checkout", "panels": [{"id": 1}]}
        dash_id = self.store.upsert_dashboard(
            "checkout", "Checkout", "fixtures/x.json", "GUID1", data)
        self.assertIsInstance(dash_id, int)
        row = self.store.get_dashboard("checkout")
        self.assertEqual(row["slug"], "checkout")
        self.assertEqual(row["title"], "Checkout")
        self.assertEqual(row["source"], "fixtures/x.json")
        self.assertEqual(row["nr_guid"], "GUID1")
        self.assertEqual(row["data"], data)
        self.assertTrue(row["updated_at"].endswith("Z"))

    def test_upsert_updates_in_place(self):
        first = self.store.upsert_dashboard(
            "checkout", "Old", "a", "G", {"v": 1})
        second = self.store.upsert_dashboard(
            "checkout", "New", "b", "G2", {"v": 2})
        self.assertEqual(first, second)
        row = self.store.get_dashboard("checkout")
        self.assertEqual(row["title"], "New")
        self.assertEqual(row["data"], {"v": 2})
        self.assertEqual(len(self.store.list_dashboards()), 1)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get_dashboard("nope"))

    def test_empty_slug_rejected(self):
        with self.assertRaises(StoreError):
            self.store.upsert_dashboard("", "t", "s", "g", {})

    def test_list_dashboards_metadata_only_with_flags(self):
        self.store.upsert_dashboard(
            "checkout", "Checkout", "src", "G", {"panels": []})
        self.store.save_artifact("checkout", "requirements",
                                 {"schema": "v1"})
        self.store.save_artifact("checkout", "datatest", {"targets": []})
        rows = self.store.list_dashboards()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertNotIn("data", row)
        self.assertEqual(row["slug"], "checkout")
        self.assertEqual(row["title"], "Checkout")
        self.assertEqual(row["source"], "src")
        self.assertEqual(row["nr_guid"], "G")
        self.assertIn("updated_at", row)
        self.assertTrue(row["has_requirements"])
        self.assertTrue(row["has_datatest"])
        self.assertFalse(row["has_widget_report"])
        self.assertFalse(row["has_check"])


class ArtifactTests(StoreTestCase):
    def test_save_get_roundtrip_all_kinds(self):
        for i, kind in enumerate(ARTIFACT_KINDS):
            self.store.save_artifact("slug", kind, {"n": i})
        for i, kind in enumerate(ARTIFACT_KINDS):
            self.assertEqual(self.store.get_artifact("slug", kind),
                             {"n": i})

    def test_save_replaces_existing(self):
        self.store.save_artifact("s", "requirements", {"v": 1})
        self.store.save_artifact("s", "requirements", {"v": 2})
        self.assertEqual(self.store.get_artifact("s", "requirements"),
                         {"v": 2})

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get_artifact("s", "check"))

    def test_empty_args_rejected(self):
        with self.assertRaises(StoreError):
            self.store.save_artifact("", "requirements", {})
        with self.assertRaises(StoreError):
            self.store.save_artifact("s", "", {})


class ChangeLogTests(StoreTestCase):
    def test_log_and_list_roundtrip(self):
        change = {
            "action": "query-edit",
            "target": "panel:3/refId:A",
            "before": {"expr": "up"},
            "after": {"expr": "up{job=\"api\"}"},
            "why": "scope to api job",
            "source": "user",
        }
        change_id = self.store.log_change("checkout", change)
        self.assertIsInstance(change_id, int)
        rows = self.store.list_changes("checkout")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], change_id)
        self.assertEqual(row["slug"], "checkout")
        self.assertEqual(row["action"], "query-edit")
        self.assertEqual(row["target"], "panel:3/refId:A")
        self.assertEqual(row["before"], {"expr": "up"})
        self.assertEqual(row["after"], {"expr": "up{job=\"api\"}"})
        self.assertEqual(row["why"], "scope to api job")
        self.assertEqual(row["source"], "user")
        self.assertTrue(row["ts"].endswith("Z"))

    def test_list_changes_all_vs_slug(self):
        self.store.log_change("a", {"action": "import", "source": "auto"})
        self.store.log_change("b", {"action": "import", "source": "ai"})
        self.assertEqual(len(self.store.list_changes()), 2)
        only_a = self.store.list_changes("a")
        self.assertEqual(len(only_a), 1)
        self.assertEqual(only_a[0]["slug"], "a")

    def test_default_source_is_user(self):
        self.store.log_change("s", {"action": "panel-edit"})
        self.assertEqual(self.store.list_changes("s")[0]["source"],
                         "user")

    def test_bad_source_rejected(self):
        with self.assertRaises(StoreError):
            self.store.log_change("s", {"action": "x",
                                        "source": "robot"})

    def test_insertion_order_preserved(self):
        for i in range(3):
            self.store.log_change("s", {"action": "e%d" % i})
        actions = [c["action"] for c in self.store.list_changes("s")]
        self.assertEqual(actions, ["e0", "e1", "e2"])


class SettingsTests(StoreTestCase):
    def test_set_get_roundtrip_json_types(self):
        cases = {
            "grafana_url": "http://localhost:3000",
            "out_dir": "/tmp/out",
            "port": 8765,
            "open_browser": True,
            "dirs": ["a", "b"],
            "prefs": {"theme": "dark", "n": 2},
        }
        for key, value in cases.items():
            self.store.set_setting(key, value)
        for key, value in cases.items():
            self.assertEqual(self.store.get_setting(key), value)

    def test_overwrite(self):
        self.store.set_setting("k", 1)
        self.store.set_setting("k", 2)
        self.assertEqual(self.store.get_setting("k"), 2)

    def test_default_for_missing(self):
        self.assertIsNone(self.store.get_setting("missing"))
        self.assertEqual(self.store.get_setting("missing", "d"), "d")

    def test_secret_looking_keys_refused(self):
        for key in ("nr_api_key", "grafana_token", "anthropic_apikey",
                    "password", "client_secret", "aws_credentials"):
            with self.assertRaises(StoreError):
                self.store.set_setting(key, "sekrit")
        # and nothing got written
        conn = sqlite3.connect(self.db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM settings").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)


class ThreadSafetyTests(StoreTestCase):
    def test_concurrent_writes_from_threads(self):
        errors = []

        def worker(idx):
            try:
                for j in range(10):
                    self.store.log_change(
                        "t%d" % idx, {"action": "e%d" % j})
                    self.store.set_setting("last_%d" % idx, j)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.list_changes()), 40)
        for i in range(4):
            self.assertEqual(self.store.get_setting("last_%d" % i), 9)


if __name__ == "__main__":
    unittest.main()
