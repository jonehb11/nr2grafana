"""Tests for the nr2grafana web server (nr2grafana/web/).

Starts the real ThreadingHTTPServer on an ephemeral port with a fake
in-memory Store and drives it over actual HTTP via urllib. Sibling 1.1
modules that may not exist yet (requirements, artifacts, changelog, ai,
grafana.live) are replaced with deterministic stubs in sys.modules so
these tests are independent of their implementations.
"""

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

from nr2grafana.web import server as websrv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(PROJECT_ROOT, "fixtures", "newrelic",
                      "sample-service-dashboard.json")

SECRET = "glsa_SUPER_SECRET_TOKEN_12345"


# ---------------------------------------------------------------------------
# fakes / stubs
# ---------------------------------------------------------------------------

class FakeStore:
    """In-memory Store honoring the section-1 contract. Settings are
    also mirrored to a JSON file so tests can assert what would land
    on disk."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.dashboards = {}
        self.artifacts = {}
        self.changes = []
        self.runs = []
        self.settings = {}

    # runs
    def record_run(self, kind, meta):
        with self._lock:
            self.runs.append({"id": len(self.runs) + 1, "kind": kind,
                              "meta": meta, "status": "running"})
            return len(self.runs)

    def finish_run(self, run_id, status, summary):
        with self._lock:
            self.runs[run_id - 1]["status"] = status
            self.runs[run_id - 1]["summary"] = summary

    def list_runs(self, kind="", limit=50):
        with self._lock:
            return [r for r in self.runs
                    if not kind or r["kind"] == kind][:limit]

    # dashboards
    def upsert_dashboard(self, slug, title, source, nr_guid, data):
        with self._lock:
            self.dashboards[slug] = {"slug": slug, "title": title,
                                     "source": source,
                                     "nr_guid": nr_guid, "data": data}
            return 1

    def get_dashboard(self, slug):
        with self._lock:
            return self.dashboards.get(slug)

    def list_dashboards(self):
        with self._lock:
            return [{"slug": r["slug"], "title": r["title"],
                     "source": r["source"], "nr_guid": r["nr_guid"]}
                    for r in self.dashboards.values()]

    # artifacts
    def save_artifact(self, slug, kind, data):
        with self._lock:
            self.artifacts[(slug, kind)] = data

    def get_artifact(self, slug, kind):
        with self._lock:
            return self.artifacts.get((slug, kind))

    # changes
    def log_change(self, slug, change):
        with self._lock:
            row = dict(change)
            row["slug"] = slug
            row["id"] = len(self.changes) + 1
            row["ts"] = "2026-01-01T00:00:00"
            self.changes.append(row)
            return row["id"]

    def list_changes(self, slug=""):
        with self._lock:
            return [c for c in self.changes
                    if not slug or c["slug"] == slug]

    # settings (mirrored to disk like the real sqlite db would be)
    def set_setting(self, key, value):
        with self._lock:
            self.settings[key] = value
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f)

    def get_setting(self, key, default=None):
        with self._lock:
            return self.settings.get(key, default)


def _stub_requirements():
    m = types.ModuleType("nr2grafana.requirements")

    def analyze_dashboard(nr, dash, widget_report, cfg):
        return {"schema": "nr2grafana/requirements/v1",
                "dashboard": dash.get("title", ""),
                "uid": dash.get("uid", ""),
                "generated_by": "stub",
                "datasources": [{"family": "prometheus",
                                 "plugin_id": "prometheus",
                                 "core": True,
                                 "uid_ref": "${datasource}",
                                 "purpose": "metrics",
                                 "panel_ids": [],
                                 "required": True}],
                "plugins": [], "domains": [], "nr_native": [],
                "data_expectations": [],
                "import": {"steps": [], "api_example": ""}}

    m.analyze_dashboard = analyze_dashboard
    return m


def _stub_artifacts():
    m = types.ModuleType("nr2grafana.artifacts")

    def package_dashboard(out_dir, slug, dash, widget_report,
                          requirements, cfg):
        pkg = os.path.join(out_dir, slug)
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "dashboard.json"), "w",
                  encoding="utf-8") as f:
            json.dump(dash, f)
        return pkg

    def write_index(out_dir, entries):
        path = os.path.join(out_dir, "INDEX.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# index\n%d dashboards\n" % len(entries))
        return path

    m.package_dashboard = package_dashboard
    m.write_index = write_index
    return m


def _stub_changelog():
    m = types.ModuleType("nr2grafana.changelog")

    class ChangeLog:
        def __init__(self, store):
            self.store = store

        def record(self, slug, action, target, before, after,
                   why="", source="user"):
            return self.store.log_change(
                slug, {"action": action, "target": target,
                       "before": before, "after": after,
                       "why": why, "source": source})

        def suggest_config(self, slug=""):
            return {"overlay": {"label_map": {}},
                    "rationale": ["stub"]}

    m.ChangeLog = ChangeLog
    return m


def _stub_ai():
    m = types.ModuleType("nr2grafana.ai")

    class AIError(Exception):
        pass

    class AIAssist:
        def __init__(self, api_key="", model=""):
            self.api_key = api_key
            self.model = model

        @property
        def available(self):
            return bool(self.api_key)

        def suggest_fix(self, context):
            return {"explanation": "stub explanation",
                    "fixed_expr": "up", "confidence": "high",
                    "actions": []}

        def chat(self, messages, system=""):
            return "stub reply to: %s" % messages[-1].get("content")

    m.AIError = AIError
    m.AIAssist = AIAssist
    return m


class FakeGrafanaLive:
    instances = []

    def __init__(self, url, token="", **kwargs):
        self.url = url
        self.token = token
        FakeGrafanaLive.instances.append(self)

    def health(self):
        return {"database": "ok", "version": "11.0.0"}

    def datasources(self):
        return [{"name": "Mimir", "type": "prometheus",
                 "uid": "mimir", "isDefault": True}]

    def plugins(self):
        return [{"id": "prometheus"}]

    def find_or_create_folder(self, title):
        return "folder-uid" if title else ""

    def check_requirements(self, requirements):
        return [{"item": "prometheus datasource", "status": "ok",
                 "detail": "found uid mimir", "fix": ""}]

    def test_dashboard(self, dash, ds_map=None, log=None):
        out = []
        for p in dash.get("panels") or []:
            for t in p.get("targets") or []:
                out.append({"panel_id": p.get("id"),
                            "panel_title": p.get("title", ""),
                            "refId": t.get("refId", "A"),
                            "datasource": "prometheus",
                            "expr": t.get("expr", ""),
                            "status": "data", "error": "",
                            "frames": 1, "points": 10})
        return out

    def import_dashboard(self, dash, folder_uid="", overwrite=False,
                         message=""):
        return {"status": "success", "uid": dash.get("uid", ""),
                "url": "/d/" + (dash.get("uid") or "x")}

    def update_dashboard(self, dash, folder_uid="", message=""):
        return {"status": "success",
                "url": "/d/" + (dash.get("uid") or "x")}


def _stub_live():
    m = types.ModuleType("nr2grafana.grafana.live")
    m.GrafanaLive = FakeGrafanaLive
    return m


STUBS = {
    "nr2grafana.requirements": _stub_requirements(),
    "nr2grafana.artifacts": _stub_artifacts(),
    "nr2grafana.changelog": _stub_changelog(),
    "nr2grafana.ai": _stub_ai(),
    "nr2grafana.grafana.live": _stub_live(),
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http(base, method, path, body=None, raw=None):
    """Returns (status_code, parsed_or_text)."""
    data = raw
    if data is None and body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(text or "{}")
            return resp.status, text
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text or "{}")
        except json.JSONDecodeError:
            return e.code, text


def poll_job(base, jid, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, job = http(base, "GET", "/api/jobs/" + jid)
        if code != 200:
            raise AssertionError("job poll failed: %s %s" % (code, job))
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("job %s did not finish in time" % jid)


# ---------------------------------------------------------------------------
# fixture: one server for the whole module
# ---------------------------------------------------------------------------

class WebServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patcher = mock.patch.dict(sys.modules, STUBS)
        cls._patcher.start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "settings.json")
        cls.store = FakeStore(cls.db_path)
        # Fresh session so ambient env vars can't leak into tests.
        websrv.SESSION = websrv.Session()
        websrv.SESSION.nr_api_key = ""
        websrv.SESSION.grafana_url = ""
        websrv.SESSION.grafana_token = ""
        websrv.SESSION.anthropic_api_key = ""
        websrv._JOBS.clear()
        cls.httpd = websrv.create_server("127.0.0.1", 0,
                                         store=cls.store)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(
            target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()
        cls._patcher.stop()

    def api(self, method, path, body=None, raw=None):
        return http(self.base, method, path, body=body, raw=raw)


class PageAndStateTests(WebServerTestCase):
    def test_index_serves_app_shell(self):
        code, text = self.api("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("<!DOCTYPE html>", text)
        self.assertIn('id="app"', text)
        self.assertIn("nr2grafana", text)
        self.assertIn('id="sidebar"', text)

    def test_state_shape(self):
        code, st = self.api("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(st["app"], "nr2grafana")
        self.assertIn("session", st)
        self.assertIn("status", st)
        self.assertIn("db", st)
        for svc in ("newrelic", "grafana", "ai"):
            self.assertIn(st["status"][svc], ("unset", "ok", "error"))

    def test_state_never_contains_secret_values(self):
        self.api("POST", "/api/settings",
                 {"grafana_token": SECRET,
                  "grafana_url": "http://gf.example:3000"})
        code, st = self.api("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertNotIn(SECRET, json.dumps(st))
        self.assertTrue(st["session"]["grafana_token_set"])

    def test_404_is_json(self):
        code, body = self.api("GET", "/api/definitely-not-a-route")
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_post_404_is_json(self):
        code, body = self.api("POST", "/api/nope", {})
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_bad_json_body_is_400(self):
        code, body = self.api("POST", "/api/settings",
                              raw=b"{not json at all")
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_unknown_job_is_404(self):
        code, body = self.api("GET", "/api/jobs/deadbeef")
        self.assertEqual(code, 404)
        self.assertIn("error", body)


class SettingsTests(WebServerTestCase):
    def test_roundtrip_and_secret_never_hits_disk(self):
        code, resp = self.api(
            "POST", "/api/settings",
            {"grafana_url": "http://grafana.example:3000",
             "grafana_token": SECRET,
             "nr_region": "eu",
             "out_dir": os.path.join(self.tmp.name, "out")})
        self.assertEqual(code, 200)
        self.assertTrue(resp["ok"])
        code, st = self.api("GET", "/api/state")
        ses = st["session"]
        self.assertEqual(ses["grafana_url"],
                         "http://grafana.example:3000")
        self.assertEqual(ses["nr_region"], "EU")
        self.assertTrue(ses["grafana_token_set"])
        # non-secret prefs persisted, secret is NOT in the db file
        with open(self.db_path, encoding="utf-8") as f:
            on_disk = f.read()
        self.assertIn("http://grafana.example:3000", on_disk)
        self.assertNotIn(SECRET, on_disk)

    def test_invalid_region_rejected(self):
        code, body = self.api("POST", "/api/settings",
                              {"nr_region": "MARS"})
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_nr_routes_require_key(self):
        websrv.SESSION.nr_api_key = ""
        code, body = self.api("POST", "/api/nr/list", {})
        self.assertEqual(code, 400)
        self.assertIn("New Relic API key", body["error"])

    def test_grafana_routes_require_url(self):
        websrv.SESSION.grafana_url = ""
        code, body = self.api("POST", "/api/grafana/health", {})
        self.assertEqual(code, 400)
        self.assertIn("Grafana URL", body["error"])
        websrv.SESSION.grafana_url = "http://gf.local"

    def test_ai_chat_requires_key(self):
        websrv.SESSION.anthropic_api_key = ""
        code, body = self.api("POST", "/api/ai/chat",
                              {"messages": [{"role": "user",
                                             "content": "hi"}]})
        self.assertEqual(code, 400)
        self.assertIn("Anthropic", body["error"])


class ConvertJobTests(WebServerTestCase):
    """Full jobs flow: POST returns {"job": id}, polling reaches
    done, artifacts and dashboards land in the store."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.in_dir = os.path.join(cls.tmp.name, "nr")
        cls.out_dir = os.path.join(cls.tmp.name, "out")
        os.makedirs(cls.in_dir)
        with open(SAMPLE, encoding="utf-8") as f:
            sample = f.read()
        with open(os.path.join(cls.in_dir, "sample.json"), "w",
                  encoding="utf-8") as f:
            f.write(sample)
        code, resp = http(cls.base, "POST", "/api/convert",
                          {"input_dir": cls.in_dir,
                           "out_dir": cls.out_dir, "package": True})
        assert code == 200 and "job" in resp, (code, resp)
        cls.job = poll_job(cls.base, resp["job"])

    def test_job_completes(self):
        self.assertEqual(self.job["status"], "done")
        self.assertTrue(self.job["log"])
        self.assertGreaterEqual(
            len(self.job["result"]["dashboards"]), 1)

    def test_dashboard_persisted_with_artifacts(self):
        slug = self.job["result"]["dashboards"][0]["slug"]
        row = self.store.get_dashboard(slug)
        self.assertIsNotNone(row)
        self.assertIn("panels", row["data"])
        self.assertIsNotNone(
            self.store.get_artifact(slug, "requirements"))
        self.assertIsNotNone(
            self.store.get_artifact(slug, "widget-report"))

    def test_package_written(self):
        slug = self.job["result"]["dashboards"][0]["slug"]
        pkg = os.path.join(self.out_dir, slug)
        self.assertTrue(
            os.path.isfile(os.path.join(pkg, "dashboard.json")))
        self.assertTrue(
            os.path.isfile(os.path.join(self.out_dir, "INDEX.md")))

    def test_dashboards_listing_and_detail(self):
        slug = self.job["result"]["dashboards"][0]["slug"]
        code, data = self.api("GET", "/api/dashboards")
        self.assertEqual(code, 200)
        self.assertIn(slug, [d["slug"] for d in data["dashboards"]])
        code, det = self.api("GET", "/api/dashboards/" + slug)
        self.assertEqual(code, 200)
        self.assertEqual(det["slug"], slug)
        self.assertIn("panels", det["dashboard"])
        self.assertEqual(det["requirements"]["schema"],
                         "nr2grafana/requirements/v1")

    def test_detail_404_for_unknown_slug(self):
        code, body = self.api("GET", "/api/dashboards/no-such-slug")
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_convert_job_error_is_reported_not_raised(self):
        code, resp = self.api("POST", "/api/convert",
                              {"input_dir": "/no/such/dir/xyz",
                               "out_dir": self.out_dir})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "error")
        self.assertIn("input directory not found", job["error"])


class GrafanaRouteTests(WebServerTestCase):
    """Grafana routes through the stubbed GrafanaLive."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        websrv.SESSION.grafana_url = "http://gf.local:3000"
        websrv.SESSION.grafana_token = "tok"
        # seed one dashboard + requirements
        cls.slug = "seeded-dash"
        cls.dash = {"title": "Seeded", "uid": "seeded",
                    "templating": {"list": []},
                    "panels": [{"id": 1, "title": "P1",
                                "type": "timeseries",
                                "targets": [{"refId": "A",
                                             "expr": "up",
                                             "datasource": {
                                                 "type": "prometheus",
                                                 "uid": "mimir"}}]}]}
        cls.store.upsert_dashboard(cls.slug, "Seeded", "seed", "",
                                   cls.dash)
        cls.store.save_artifact(
            cls.slug, "requirements",
            STUBS["nr2grafana.requirements"].analyze_dashboard(
                None, cls.dash, [], {}))

    def test_health(self):
        code, body = self.api("POST", "/api/grafana/health", {})
        self.assertEqual(code, 200)
        self.assertEqual(body["version"], "11.0.0")

    def test_check_requirements(self):
        code, body = self.api("POST", "/api/grafana/check",
                              {"slug": self.slug})
        self.assertEqual(code, 200)
        self.assertEqual(body["items"][0]["status"], "ok")
        self.assertIsNotNone(
            self.store.get_artifact(self.slug, "check"))

    def test_test_job_persists_datatest(self):
        code, resp = self.api("POST", "/api/grafana/test",
                              {"slug": self.slug})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["summary"], {"data": 1})
        art = self.store.get_artifact(self.slug, "datatest")
        self.assertEqual(art["results"][0]["status"], "data")

    def test_import_job(self):
        code, resp = self.api("POST", "/api/grafana/import",
                              {"slugs": [self.slug],
                               "folder": "Migrated",
                               "overwrite": True})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["ok"], 1)
        self.assertEqual(job["result"]["results"][0]["status"], "ok")

    def test_panel_test_does_not_save(self):
        code, body = self.api("POST", "/api/panel/test",
                              {"slug": self.slug, "panel_id": 1,
                               "refId": "A",
                               "expr": "candidate_metric"})
        self.assertEqual(code, 200)
        self.assertEqual(body["results"][0]["expr"],
                         "candidate_metric")
        row = self.store.get_dashboard(self.slug)
        self.assertEqual(
            row["data"]["panels"][0]["targets"][0]["expr"], "up")

    def test_panel_update_edits_and_records_change(self):
        code, body = self.api(
            "POST", "/api/panel/update",
            {"slug": self.slug, "panel_id": 1, "refId": "A",
             "expr": "sum(up)", "why": "unit test"})
        self.assertEqual(code, 200)
        self.assertEqual(body["before"], "up")
        row = self.store.get_dashboard(self.slug)
        self.assertEqual(
            row["data"]["panels"][0]["targets"][0]["expr"],
            "sum(up)")
        changes = self.store.list_changes(self.slug)
        self.assertTrue(any(c["action"] == "query-edit"
                            for c in changes))
        code, data = self.api(
            "GET", "/api/changes?slug=" + self.slug)
        self.assertEqual(code, 200)
        self.assertTrue(data["changes"])

    def test_panel_update_missing_panel_is_404(self):
        code, body = self.api(
            "POST", "/api/panel/update",
            {"slug": self.slug, "panel_id": 999, "refId": "A",
             "expr": "x"})
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_suggest_config_route(self):
        code, body = self.api(
            "GET", "/api/changes/suggest-config?slug=" + self.slug)
        self.assertEqual(code, 200)
        self.assertIn("overlay", body)


class AIRouteTests(WebServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        websrv.SESSION.anthropic_api_key = "sk-ant-test"
        websrv.SESSION.grafana_url = ""

    def test_chat(self):
        code, body = self.api(
            "POST", "/api/ai/chat",
            {"messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(code, 200)
        self.assertIn("stub reply", body["reply"])

    def test_suggest(self):
        code, body = self.api(
            "POST", "/api/ai/suggest",
            {"expr": "uup", "error": "unknown metric",
             "datasource": "prometheus"})
        self.assertEqual(code, 200)
        self.assertEqual(body["fixed_expr"], "up")
        self.assertEqual(body["confidence"], "high")

    def test_chat_requires_messages(self):
        code, body = self.api("POST", "/api/ai/chat", {})
        self.assertEqual(code, 400)
        self.assertIn("error", body)


class JobInternalsTests(unittest.TestCase):
    def test_start_job_captures_errors(self):
        def boom(job):
            job.add("about to fail")
            raise RuntimeError("kaput")
        jid = websrv._start_job("test", boom)
        for _ in range(100):
            job = websrv._JOBS[jid]
            if job.status != "running":
                break
            time.sleep(0.05)
        self.assertEqual(job.status, "error")
        self.assertIn("kaput", job.error)
        d = job.to_dict()
        self.assertEqual(d["status"], "error")
        self.assertIn("about to fail", d["log"][0])

    def test_start_job_success_result(self):
        jid = websrv._start_job("test", lambda job: {"n": 7})
        for _ in range(100):
            job = websrv._JOBS[jid]
            if job.status != "running":
                break
            time.sleep(0.05)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.result, {"n": 7})


if __name__ == "__main__":
    unittest.main()
