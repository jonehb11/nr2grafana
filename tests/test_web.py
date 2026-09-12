"""Tests for the nr2grafana web server (nr2grafana/web/).

Starts the real ThreadingHTTPServer on an ephemeral port with a fake
in-memory Store and drives it over actual HTTP via urllib. Sibling 1.1
modules that may not exist yet (requirements, artifacts, changelog, ai,
grafana.live) are replaced with deterministic stubs in sys.modules so
these tests are independent of their implementations.
"""

import io
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
import zipfile
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

    class LocalAgent:
        def __init__(self, command, timeout=180):
            self.command = command
            self.timeout = timeout

        @property
        def available(self):
            return bool(self.command)

        def suggest_fix(self, context):
            return {"explanation": "local stub", "fixed_expr":
                    "up_local", "confidence": "medium", "actions": []}

        def chat(self, messages, system=""):
            return "local reply to: %s" % messages[-1].get("content")

        def test(self):
            return {"ok": True, "reply_excerpt": "OK",
                    "latency_ms": 5}

    def get_assistant(api_key="", model="", command=""):
        if api_key:
            return AIAssist(api_key, model)
        if command:
            return LocalAgent(command)
        return None

    m.AIError = AIError
    m.AIAssist = AIAssist
    m.LocalAgent = LocalAgent
    m.get_assistant = get_assistant
    return m


class FakeGrafanaLive:
    instances = []
    created_payloads = []
    updated = []
    deleted_uids = []
    metric_calls = []

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.created_payloads = []
        cls.updated = []
        cls.deleted_uids = []
        cls.metric_calls = []

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

    # 1.2 datasource management additions
    def permissions_report(self):
        return {"user": "sa-migrator", "role": "Admin",
                "can_admin_datasources": True,
                "can_edit_dashboards": True, "detail": "full access"}

    def create_datasource(self, payload):
        FakeGrafanaLive.created_payloads.append(payload)
        return {"datasource": {"uid": "new-ds",
                               "name": payload.get("name")},
                "message": "Datasource added"}

    def update_datasource(self, uid, payload):
        FakeGrafanaLive.updated.append((uid, payload))
        return {"datasource": {"uid": uid}, "message": "updated"}

    def delete_datasource(self, uid):
        FakeGrafanaLive.deleted_uids.append(uid)

    def datasource_health(self, uid):
        return {"status": "ok", "message": "healthy: " + uid}

    def prom_metric_names(self, uid):
        FakeGrafanaLive.metric_calls.append(uid)
        return ["up", "http_requests_total",
                "node_cpu_seconds_total"]

    def prom_label_values(self, uid, label, match=""):
        return ["api", "web"]

    def prom_series(self, uid, match, frm="now-1h"):
        return [{"__name__": "up"}]

    def loki_labels(self, uid):
        return ["job", "namespace"]

    def loki_label_values(self, uid, label):
        return ["default"]

    # 1.2 sample-review additions
    def resolve_ds_map(self, dash):
        return {}

    def ds_query(self, ds_uid, ds_type, target, frm="now-1h",
                 to="now"):
        ref = target.get("refId") or "A"
        if ds_type == "loki":
            frame = {"schema": {"refId": ref, "fields": [
                        {"name": "Time", "type": "time"},
                        {"name": "Line", "type": "string"}]},
                     "data": {"values": [
                         [1000000000000, 1000000060000],
                         ["log line one", "log line two"]]}}
        else:
            frame = {"schema": {"refId": ref, "fields": [
                        {"name": "Time", "type": "time"},
                        {"name": "Value", "type": "number",
                         "labels": {"job": "api"}}]},
                     "data": {"values": [
                         [1000000000000, 1000000060000],
                         [1.0, 2.0]]}}
        return {"results": {ref: {"status": 200,
                                  "frames": [frame]}}}


FAKE_DS_TEMPLATES = {
    "prometheus": {
        "label": "Prometheus / Mimir", "plugin_id": "prometheus",
        "core": True,
        "fields": [{"name": "url", "label": "URL", "required": True,
                    "secret": False,
                    "placeholder": "http://mimir:9009/prometheus",
                    "help": "", "path": "url"}],
        "notes": "stub template"},
}


def _stub_live():
    m = types.ModuleType("nr2grafana.grafana.live")
    m.GrafanaLive = FakeGrafanaLive
    m.DS_TEMPLATES = FAKE_DS_TEMPLATES

    def build_datasource_payload(ds_type, name, values):
        if ds_type not in FAKE_DS_TEMPLATES:
            raise ValueError("unknown datasource type %r" % ds_type)
        return {"name": name, "type": ds_type,
                "url": values.get("url", ""), "access": "proxy",
                "jsonData": {}, "secureJsonData": {}}

    m.build_datasource_payload = build_datasource_payload
    return m


def _stub_nerdgraph():
    m = types.ModuleType("nr2grafana.nerdgraph")

    class NerdGraphError(Exception):
        pass

    class NerdGraphClient:
        def __init__(self, api_key, region="US", **kwargs):
            self.api_key = api_key
            self.region = region

        def _post(self, query, variables=None, retries=3):
            return {"actor": {
                "user": {"name": "Jon", "email": "jon@example.com"},
                "accounts": [{"id": 1, "name": "Main"}]}}

        def run_nrql(self, account_id, nrql):
            return {"results": [{"count": 1}], "metadata": {}}

        def list_account_ids(self):
            return [1]

    m.NerdGraphError = NerdGraphError
    m.NerdGraphClient = NerdGraphClient
    return m


def _stub_parity():
    m = types.ModuleType("nr2grafana.parity")

    def run_parity(nr, account_ids, grafana, dash, widget_report,
                   ds_map=None, frm="now-1h", to="now", log=None):
        if log:
            log("parity stub running")
        return {"schema": "nr2grafana/parity/v1",
                "dashboard": dash.get("title", ""),
                "generated_at": "2026-01-01T00:00:00Z",
                "range": {"from": frm, "to": to},
                "panels": [{"panel_id": 1, "panel_title": "P1",
                            "refId": "A", "nrql": "", "expr": "uup",
                            "datasource": "prometheus",
                            "verdict": "match", "detail": "", "ratio":
                            1.0, "nr_summary": {}, "gf_summary": {}}],
                "score": 100, "summary": {"match": 1}}

    def readiness(parity, check_rows=None, test_rows=None,
                  review=None):
        rows = []
        if isinstance(review, dict):
            rows = list((review.get("reviews") or {}).values())
        if any(r.get("verdict") == "rejected" for r in rows):
            return {"score": 0, "grade": "blocked",
                    "reasons": ["rejected in human review"]}
        if parity:
            return {"score": 100, "grade": "ready", "reasons": []}
        return {"score": 0, "grade": "blocked",
                "reasons": ["no parity run yet"]}

    m.run_parity = run_parity
    m.readiness = readiness
    return m


def _stub_diagnose():
    m = types.ModuleType("nr2grafana.diagnose")

    def diagnose(grafana, nr=None, dash=None, requirements=None,
                 test_results=None, parity=None, cfg=None, log=None):
        if log:
            log("diagnose stub running")
        return {"schema": "nr2grafana/diagnosis/v1",
                "generated_at": "2026-01-01T00:00:00Z",
                "findings": [{
                    "id": "f1", "severity": "warn", "area": "panel",
                    "panel_id": 1,
                    "problem": "metric 'uup' not found",
                    "evidence": "did you mean 'up'?",
                    "fix": {"description": "rename uup -> up",
                            "kind": "edit-query",
                            "action": {"panel_id": 1, "refId": "A",
                                       "new_expr": "up"}}}],
                "summary": {"blocker": 0, "warn": 1, "info": 0,
                            "by_area": {"panel": 1}, "panels": [1]}}

    m.diagnose = diagnose
    return m


def _stub_remediate():
    m = types.ModuleType("nr2grafana.remediate")
    m.apply_calls = []
    m.heal_calls = []

    def apply_fix(fix, grafana=None, dash=None, package_dir="",
                  changelog=None, slug="", push=False):
        m.apply_calls.append({"fix": fix, "slug": slug, "push": push,
                              "package_dir": package_dir})
        if fix.get("kind") == "edit-query" and isinstance(dash, dict):
            action = fix.get("action") or {}
            for p in dash.get("panels") or []:
                if p.get("id") == action.get("panel_id"):
                    for t in p.get("targets") or []:
                        if t.get("refId") == action.get("refId"):
                            t["expr"] = action.get("new_expr", "")
        return {"applied": True, "kind": fix.get("kind", ""),
                "detail": "stub applied", "verify": None}

    def auto_heal(grafana, nr, dash, widget_report, requirements,
                  slug, package_dir, changelog=None, max_rounds=3,
                  log=None):
        m.heal_calls.append({"slug": slug,
                             "package_dir": package_dir})
        if log:
            log("heal round 1")
        for p in dash.get("panels") or []:
            for t in p.get("targets") or []:
                if t.get("expr") == "uup":
                    t["expr"] = "up"
        return {"rounds": [{"round": 1, "fixed": 1}], "fixed": 1,
                "remaining_findings": []}

    m.apply_fix = apply_fix
    m.auto_heal = auto_heal
    return m


def _stub_compare():
    m = types.ModuleType("nr2grafana.compare")

    def build_comparison(nr, account_ids, grafana, nr_dashboard_raw,
                         dash, widget_report, ds_map=None, frm="now-1h",
                         to="now", limit_points=100, log=None):
        if log:
            log("compare stub building")
        panels = []
        for p in dash.get("panels") or []:
            expr = ""
            for t in p.get("targets") or []:
                expr = t.get("expr", "")
                break
            side = {"kind": "series",
                    "series": [{"name": "s",
                                "points": [[1000, 1.0], [1060, 2.0]]}],
                    "scalar": None, "rows": None, "lines": None,
                    "unit": "", "error": ""}
            panels.append({
                "panel_id": p.get("id"), "title": p.get("title", ""),
                "row": "", "grid": p.get("gridPos",
                                         {"x": 0, "y": 0, "w": 12,
                                          "h": 8}),
                "viz": p.get("type", "timeseries"),
                "nr": dict(side), "grafana": dict(side),
                "verdict": "match", "detail": "", "ratio": 1.0,
                "nrql": "", "expr": expr, "datasource": "prometheus"})
        return {"schema": "nr2grafana/comparison/v1",
                "dashboard": dash.get("title", ""),
                "uid": dash.get("uid", ""),
                "generated_at": "2026-01-01T00:00:00Z",
                "range": {"from": frm, "to": to},
                "panels": panels,
                "summary": {"match": len(panels)},
                "score": 100 if panels else 0,
                "layout": {"nr_pages": []},
                "nr_source_present": isinstance(nr_dashboard_raw, dict),
                "account_ids": list(account_ids or [])}

    def datasource_flow(grafana, requirements, dash, widget_report,
                        ds_uid=None, ds_map=None, log=None):
        if log:
            log("flow stub")
        return {"family": "prometheus", "uid": ds_uid or "",
                "health": {"status": "ok"},
                "panels_total": 2, "panels_with_data": 2,
                "panels_no_data": 0, "panels_error": 0,
                "sample_series": [{"name": "up",
                                   "points": [[1000, 1.0]]}],
                "newly_flowing": [1]}

    m.build_comparison = build_comparison
    m.datasource_flow = datasource_flow
    return m


def _stub_traffic():
    m = types.ModuleType("nr2grafana.traffic")

    def sample_traffic(grafana, ds_list, frm="now-24h", to="now",
                       log=None):
        if log:
            log("traffic stub sampling")
        return {"schema": "nr2grafana/traffic/v1",
                "generated_at": "2026-01-01T00:00:00Z",
                "range": {"from": frm, "to": to},
                "datasources": [
                    {"family": d["family"], "uid": d["uid"],
                     "health": {"status": "ok"}} for d in ds_list]}

    m.sample_traffic = sample_traffic
    return m


def _stub_usage():
    m = types.ModuleType("nr2grafana.usage")

    def collect_usage(dashboards, widget_reports):
        return {"prometheus": {"metrics": ["up"], "labels": ["job"]},
                "loki": {"stream_labels": ["namespace"],
                         "filtered_values": {}},
                "tempo": {},
                "dashboards": len(dashboards)}

    m.collect_usage = collect_usage
    return m


def _stub_costmodel():
    m = types.ModuleType("nr2grafana.costmodel")
    m.DEFAULT_PRICING = {"loki_gb_ingest": 0.5,
                         "mimir_1k_series_month": 0.6,
                         "retention_days": 30}

    def estimate_costs(traffic, pricing=None):
        return {"schema": "nr2grafana/cost/v1",
                "pricing": dict(pricing or m.DEFAULT_PRICING),
                "components": [{"family": "loki", "uid": "loki1",
                                "monthly_cost": 100.0, "breakdown": {}}],
                "monthly_total": 100.0,
                "resources": {"mimir_ram_gb_est": 1.0}}

    def apply_savings(cost, recommendations):
        saved = sum((r.get("est_savings") or {}).get("monthly_usd", 0)
                    for r in recommendations or [])
        total = cost.get("monthly_total", 0.0)
        return {"projected_total": total - saved,
                "saved_total": saved,
                "saved_pct": int(100 * saved / total) if total else 0,
                "per_component": []}

    m.estimate_costs = estimate_costs
    m.apply_savings = apply_savings
    return m


def _stub_optimize():
    m = types.ModuleType("nr2grafana.optimize")

    def recommend(traffic, usage, cost=None, pricing=None, cfg=None,
                  log=None):
        if log:
            log("optimize stub running")
        return {"schema": "nr2grafana/optimize/v1",
                "generated_at": "2026-01-01T00:00:00Z",
                "recommendations": [{
                    "id": "loki-drop-label-pod", "family": "loki",
                    "kind": "drop-label", "severity": "high",
                    "title": "Drop stream label `pod` (unused)",
                    "rationale": "no dashboard filters on pod",
                    "evidence": {"cardinality": 4213,
                                 "used_by_dashboards": 0},
                    "keeps_intact": True,
                    "est_savings": {"streams": 4213, "monthly_usd": 12.5,
                                    "confidence": "high"},
                    "config": [
                        {"target": "promtail", "language": "yaml",
                         "snippet": "pipeline_stages:\n  - labeldrop:\n"
                                    "      - pod",
                         "note": "apply at the agent to save before "
                                 "ingest"},
                        {"target": "loki-limits", "language": "yaml",
                         "snippet": "limits_config: {}", "note": ""}]}],
                "summary": {"by_family": {"loki": 1},
                            "total_est_monthly_usd": 12.5,
                            "safe_count": 1, "needs_review_count": 0}}

    m.recommend = recommend
    return m


STUBS = {
    "nr2grafana.requirements": _stub_requirements(),
    "nr2grafana.artifacts": _stub_artifacts(),
    "nr2grafana.changelog": _stub_changelog(),
    "nr2grafana.ai": _stub_ai(),
    "nr2grafana.grafana.live": _stub_live(),
    "nr2grafana.nerdgraph": _stub_nerdgraph(),
    "nr2grafana.parity": _stub_parity(),
    "nr2grafana.diagnose": _stub_diagnose(),
    "nr2grafana.remediate": _stub_remediate(),
    "nr2grafana.compare": _stub_compare(),
    "nr2grafana.traffic": _stub_traffic(),
    "nr2grafana.usage": _stub_usage(),
    "nr2grafana.costmodel": _stub_costmodel(),
    "nr2grafana.optimize": _stub_optimize(),
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


def http_bin(base, path):
    """GET returning (status, headers dict, raw bytes) -- downloads."""
    req = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


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

    def test_nr_source_saved_on_convert(self):
        """Convert additionally persists the original New Relic
        dashboard json as the "nr-source" artifact so Compare has the
        NR side offline."""
        slug = self.job["result"]["dashboards"][0]["slug"]
        nr_src = self.store.get_artifact(slug, "nr-source")
        self.assertIsInstance(nr_src, dict)
        self.assertTrue(nr_src)  # non-empty raw NR json

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

    def test_panel_update_tempo_target_writes_query_key(self):
        """Tempo targets carry TraceQL in "query", not "expr" -- a
        manual edit must land in the key Grafana actually reads."""
        slug = "tempo-edit-dash"
        dash = {"title": "T", "uid": slug, "templating": {"list": []},
                "panels": [{"id": 7, "title": "Traces",
                            "type": "table",
                            "targets": [{"refId": "A",
                                         "queryType": "traceql",
                                         "query": "{ status = error }",
                                         "datasource": {
                                             "type": "tempo",
                                             "uid": "tempo"}}]}]}
        self.store.upsert_dashboard(slug, "T", "seed", "", dash)
        code, body = self.api(
            "POST", "/api/panel/update",
            {"slug": slug, "panel_id": 7, "refId": "A",
             "expr": "{ duration > 1s }"})
        self.assertEqual(code, 200)
        self.assertEqual(body["before"], "{ status = error }")
        tgt = self.store.get_dashboard(
            slug)["data"]["panels"][0]["targets"][0]
        self.assertEqual(tgt["query"], "{ duration > 1s }")
        self.assertNotIn("expr", tgt)

    def test_panel_update_missing_panel_is_404(self):
        code, body = self.api(
            "POST", "/api/panel/update",
            {"slug": self.slug, "panel_id": 999, "refId": "A",
             "expr": "x"})
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_keepalive_connection_survives_bodyless_handler(self):
        """Handlers that ignore their POST body (health, datasources,
        nr/list ...) must still drain it, or the unread bytes corrupt
        the next request on the same keep-alive connection (browsers
        reuse connections; curl does not)."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=10)
        try:
            # A body-less GET first: the handler INSTANCE is reused
            # for every request on this connection, so a stale cached
            # "empty body" from this request must not stop the next
            # request's body from being drained.
            conn.request("GET", "/api/state")
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 200)
            conn.request("POST", "/api/grafana/health", body=b"{}",
                         headers={"Content-Type":
                                  "application/json"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200)
            # Third request on the SAME socket must not see stray
            # bytes from the POST's unread body ("{}GET ..." -> 501).
            conn.request("GET", "/api/state")
            resp2 = conn.getresponse()
            body2 = resp2.read()
            self.assertEqual(resp2.status, 200)
            self.assertIn(b"nr2grafana", body2)
        finally:
            conn.close()

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


class LocalAITests(WebServerTestCase):
    """Local console AI backend: ai_command settings persistence,
    backend resolution, /api/ai/test, and routing chat/suggest
    through LocalAgent when no API key is set."""

    CMD = "claude -p {prompt}"

    def setUp(self):
        websrv.SESSION.anthropic_api_key = ""
        websrv.SESSION.ai_command = ""
        websrv.SESSION.status["ai"] = "unset"

    def test_settings_persist_command_but_never_keys(self):
        code, resp = self.api(
            "POST", "/api/settings",
            {"ai_command": self.CMD,
             "anthropic_api_key": "sk-ant-should-not-persist"})
        self.assertEqual(code, 200)
        ses = resp["session"]
        self.assertEqual(ses["ai_command"], self.CMD)
        self.assertTrue(ses["ai_command_set"])
        # the API key wins while it is set
        self.assertEqual(ses["ai_backend"], "api")
        # command persisted as a non-secret pref; key stays out of
        # the store entirely
        self.assertEqual(
            self.store.get_setting("web.ai_command"), self.CMD)
        with open(self.db_path, encoding="utf-8") as f:
            on_disk = f.read()
        self.assertIn(self.CMD, on_disk)
        self.assertNotIn("sk-ant-should-not-persist", on_disk)

    def test_backend_local_when_only_command(self):
        self.api("POST", "/api/settings", {"ai_command": self.CMD})
        code, st = self.api("GET", "/api/state")
        self.assertEqual(code, 200)
        ses = st["session"]
        self.assertEqual(ses["ai_backend"], "local")
        self.assertTrue(ses["ai_command_set"])
        self.assertFalse(ses["anthropic_key_set"])
        self.assertEqual(st["status"]["ai"], "ok")

    def test_empty_string_clears_command(self):
        self.api("POST", "/api/settings", {"ai_command": self.CMD})
        code, resp = self.api("POST", "/api/settings",
                              {"ai_command": ""})
        self.assertEqual(code, 200)
        ses = resp["session"]
        self.assertFalse(ses["ai_command_set"])
        self.assertEqual(ses["ai_backend"], "none")
        self.assertEqual(self.store.get_setting("web.ai_command"), "")
        code, st = self.api("GET", "/api/state")
        self.assertEqual(st["status"]["ai"], "unset")

    def test_state_features_ai_local(self):
        code, st = self.api("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertTrue(st["features"].get("ai_local"))

    def test_chat_routes_through_local_agent(self):
        websrv.SESSION.ai_command = self.CMD
        code, body = self.api(
            "POST", "/api/ai/chat",
            {"messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(code, 200)
        self.assertIn("local reply", body["reply"])

    def test_suggest_routes_through_local_agent(self):
        websrv.SESSION.ai_command = self.CMD
        code, body = self.api(
            "POST", "/api/ai/suggest",
            {"expr": "uup", "error": "unknown metric",
             "datasource": "prometheus"})
        self.assertEqual(code, 200)
        self.assertEqual(body["fixed_expr"], "up_local")
        self.assertEqual(body["confidence"], "medium")

    def test_ai_routes_400_without_any_backend(self):
        code, body = self.api(
            "POST", "/api/ai/chat",
            {"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 400)
        self.assertIn("local console agent", body["error"])
        self.assertIn("Anthropic", body["error"])

    def test_ai_test_local_backend(self):
        websrv.SESSION.ai_command = self.CMD
        code, body = self.api("POST", "/api/ai/test", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["backend"], "local")
        self.assertEqual(body["reply_excerpt"], "OK")
        self.assertIn("latency_ms", body)
        self.assertEqual(websrv.SESSION.status["ai"], "ok")

    def test_ai_test_api_backend(self):
        websrv.SESSION.anthropic_api_key = "sk-ant-test"
        websrv.SESSION.ai_command = self.CMD  # key must win
        code, body = self.api("POST", "/api/ai/test", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["backend"], "api")
        self.assertIn("stub reply", body["reply_excerpt"])

    def test_ai_test_unconfigured_400(self):
        code, body = self.api("POST", "/api/ai/test", {})
        self.assertEqual(code, 400)
        self.assertIn("error", body)


def _seed_dash(store, slug, expr="uup"):
    """Store a one-panel dashboard whose target expr is ``expr``."""
    dash = {"title": "T " + slug, "uid": slug,
            "templating": {"list": []},
            "panels": [{"id": 1, "title": "P1", "type": "timeseries",
                        "targets": [{"refId": "A", "expr": expr,
                                     "datasource": {
                                         "type": "prometheus",
                                         "uid": "mimir"}}]}]}
    store.upsert_dashboard(slug, "T " + slug, "seed", "", dash)
    return dash


class ConnectTestTests(WebServerTestCase):
    """POST /api/nr/test-key and /api/grafana/test-token."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        websrv.SESSION.nr_api_key = "NRAK-TEST"
        websrv.SESSION.grafana_url = "http://gf.local:3000"
        websrv.SESSION.grafana_token = "tok"

    def test_nr_test_key(self):
        code, body = self.api("POST", "/api/nr/test-key", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["user"]["email"], "jon@example.com")
        self.assertEqual(len(body["accounts"]), 1)
        self.assertEqual(websrv.SESSION.status["newrelic"], "ok")

    def test_nr_test_key_requires_key(self):
        websrv.SESSION.nr_api_key = ""
        try:
            code, body = self.api("POST", "/api/nr/test-key", {})
            self.assertEqual(code, 400)
            self.assertIn("error", body)
        finally:
            websrv.SESSION.nr_api_key = "NRAK-TEST"

    def test_grafana_test_token(self):
        code, body = self.api("POST", "/api/grafana/test-token", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["permissions"]["role"], "Admin")
        self.assertTrue(body["permissions"]["can_admin_datasources"])
        self.assertEqual(body["health"]["version"], "11.0.0")

    def test_state_has_feature_flags(self):
        code, st = self.api("GET", "/api/state")
        self.assertEqual(code, 200)
        feats = st["features"]
        for name in ("parity", "diagnose", "remediate", "samples",
                     "ds_templates"):
            self.assertTrue(feats.get(name), name)


class DatasourceRouteTests(WebServerTestCase):
    """DS_TEMPLATES passthrough and datasource CRUD + health."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        websrv.SESSION.grafana_url = "http://gf.local:3000"
        websrv.SESSION.grafana_token = "tok"
        FakeGrafanaLive.reset()

    def test_ds_templates_passthrough(self):
        code, body = self.api("GET", "/api/grafana/ds-templates")
        self.assertEqual(code, 200)
        self.assertEqual(body, FAKE_DS_TEMPLATES)

    def test_create_datasource_with_health(self):
        code, body = self.api(
            "POST", "/api/grafana/datasource",
            {"type": "prometheus", "name": "Mimir2",
             "values": {"url": "http://mimir:9009/prometheus"}})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["uid"], "new-ds")
        self.assertEqual(body["health"]["status"], "ok")
        payload = FakeGrafanaLive.created_payloads[-1]
        self.assertEqual(payload["name"], "Mimir2")
        self.assertEqual(payload["url"],
                         "http://mimir:9009/prometheus")
        self.assertTrue(any(c["action"] == "datasource-created"
                            for c in self.store.list_changes()))

    def test_create_datasource_unknown_type_400(self):
        code, body = self.api(
            "POST", "/api/grafana/datasource",
            {"type": "no-such-type", "name": "X", "values": {}})
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_create_datasource_missing_name_400(self):
        code, body = self.api("POST", "/api/grafana/datasource",
                              {"type": "prometheus"})
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_update_datasource(self):
        code, body = self.api("PUT", "/api/grafana/datasource/mimir",
                              {"name": "Mimir", "url": "http://new"})
        self.assertEqual(code, 200)
        self.assertEqual(body["datasource"]["uid"], "mimir")
        self.assertEqual(FakeGrafanaLive.updated[-1][0], "mimir")

    def test_delete_datasource(self):
        code, body = self.api("DELETE",
                              "/api/grafana/datasource/old-ds")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIn("old-ds", FakeGrafanaLive.deleted_uids)

    def test_datasource_health_route(self):
        code, body = self.api(
            "POST", "/api/grafana/datasource/mimir/health", {})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")

    def test_put_without_uid_404(self):
        code, body = self.api("PUT", "/api/grafana/datasource/", {})
        self.assertEqual(code, 404)
        self.assertIn("error", body)


class ParityDiagnoseFixTests(WebServerTestCase):
    """Parity + diagnose job flow, /api/fix dispatch, /api/heal."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        websrv.SESSION.grafana_url = "http://gf.local:3000"
        websrv.SESSION.grafana_token = "tok"
        websrv.SESSION.nr_api_key = "NRAK-TEST"

    def test_parity_job_persists_artifact(self):
        slug = "parity-dash"
        _seed_dash(self.store, slug)
        self.store.save_artifact(slug, "widget-report",
                                 {"widgets": [{"panel_id": 1,
                                               "accountIds": [1]}]})
        code, resp = self.api("POST", "/api/parity",
                              {"slug": slug, "from": "now-6h"})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        self.assertTrue(any("parity stub running" in ln
                            for ln in job["log"]))
        art = self.store.get_artifact(slug, "parity")
        self.assertIsNotNone(art)
        self.assertEqual(art["schema"], "nr2grafana/parity/v1")
        self.assertEqual(art["score"], 100)
        self.assertEqual(art["range"]["from"], "now-6h")

    def test_parity_requires_slug(self):
        code, resp = self.api("POST", "/api/parity", {})
        self.assertEqual(code, 200)  # job starts, then errors
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "error")
        self.assertIn("slug", job["error"])

    def test_parity_requires_nr_key(self):
        websrv.SESSION.nr_api_key = ""
        try:
            code, body = self.api("POST", "/api/parity",
                                  {"slug": "x"})
            self.assertEqual(code, 400)
            self.assertIn("New Relic API key", body["error"])
        finally:
            websrv.SESSION.nr_api_key = "NRAK-TEST"

    def test_diagnose_job_persists_artifact(self):
        slug = "diag-dash"
        _seed_dash(self.store, slug)
        code, resp = self.api("POST", "/api/diagnose", {"slug": slug})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        self.assertTrue(any("diagnose stub running" in ln
                            for ln in job["log"]))
        art = self.store.get_artifact(slug, "diagnosis")
        self.assertIsNotNone(art)
        self.assertEqual(art["schema"], "nr2grafana/diagnosis/v1")
        self.assertEqual(art["findings"][0]["id"], "f1")

    def test_fix_dispatches_and_updates_store(self):
        slug = "fix-dash"
        _seed_dash(self.store, slug, expr="uup")
        self.store.save_artifact(
            slug, "diagnosis",
            STUBS["nr2grafana.diagnose"].diagnose(None))
        code, body = self.api("POST", "/api/fix",
                              {"slug": slug, "finding_id": "f1"})
        self.assertEqual(code, 200)
        self.assertTrue(body["applied"])
        self.assertEqual(body["kind"], "edit-query")
        self.assertEqual(body["finding_id"], "f1")
        call = STUBS["nr2grafana.remediate"].apply_calls[-1]
        self.assertEqual(call["slug"], slug)
        self.assertEqual(call["fix"]["kind"], "edit-query")
        row = self.store.get_dashboard(slug)
        self.assertEqual(
            row["data"]["panels"][0]["targets"][0]["expr"], "up")

    def test_fix_add_datasource_with_inline_values(self):
        """/api/fix accepts "values" that complete an add-datasource
        action's needs_input fields via build_datasource_payload, so
        the datasource can be created from the Diagnostics view."""
        slug = "fix-ds-dash"
        _seed_dash(self.store, slug)
        self.store.save_artifact(slug, "diagnosis", {
            "schema": "nr2grafana/diagnosis/v1",
            "findings": [{
                "id": "ds1", "severity": "blocker",
                "area": "datasource",
                "problem": "prometheus datasource missing",
                "fix": {"description": "create it",
                        "kind": "add-datasource",
                        "action": {"name": "Prometheus",
                                   "type": "prometheus",
                                   "access": "proxy", "url": "",
                                   "needs_input": ["url"]}}}],
            "summary": {"blocker": 1}})
        code, body = self.api(
            "POST", "/api/fix",
            {"slug": slug, "finding_id": "ds1",
             "values": {"url": "http://mimir:9009/prometheus"}})
        self.assertEqual(code, 200)
        call = STUBS["nr2grafana.remediate"].apply_calls[-1]
        action = call["fix"]["action"]
        self.assertEqual(action["url"],
                         "http://mimir:9009/prometheus")
        self.assertNotIn("needs_input", action)

    def test_fix_add_datasource_bad_type_with_values_400(self):
        slug = "fix-ds-dash-2"
        _seed_dash(self.store, slug)
        self.store.save_artifact(slug, "diagnosis", {
            "findings": [{
                "id": "ds2", "severity": "blocker",
                "area": "datasource", "problem": "x",
                "fix": {"kind": "add-datasource",
                        "action": {"type": "no-such-type",
                                   "needs_input": ["url"]}}}]})
        code, body = self.api(
            "POST", "/api/fix",
            {"slug": slug, "finding_id": "ds2",
             "values": {"url": "http://x"}})
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_fix_unknown_finding_404(self):
        slug = "fix-dash-2"
        _seed_dash(self.store, slug)
        self.store.save_artifact(
            slug, "diagnosis",
            STUBS["nr2grafana.diagnose"].diagnose(None))
        code, body = self.api("POST", "/api/fix",
                              {"slug": slug,
                               "finding_id": "nope"})
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_fix_without_diagnosis_404(self):
        slug = "fix-dash-3"
        _seed_dash(self.store, slug)
        code, body = self.api("POST", "/api/fix",
                              {"slug": slug, "finding_id": "f1"})
        self.assertEqual(code, 404)
        self.assertIn("Diagnose", body["error"])

    def test_heal_job_persists_and_fixes(self):
        slug = "heal-dash"
        _seed_dash(self.store, slug, expr="uup")
        code, resp = self.api("POST", "/api/heal",
                              {"slug": slug, "push": True})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        self.assertTrue(any("heal round 1" in ln
                            for ln in job["log"]))
        self.assertEqual(job["result"]["fixed"], 1)
        self.assertIn("push", job["result"])
        row = self.store.get_dashboard(slug)
        self.assertEqual(
            row["data"]["panels"][0]["targets"][0]["expr"], "up")
        self.assertIsNotNone(self.store.get_artifact(slug, "heal"))

    def test_readiness_route(self):
        slug = "ready-dash"
        _seed_dash(self.store, slug)
        code, body = self.api("GET", "/api/readiness?slug=" + slug)
        self.assertEqual(code, 200)
        self.assertEqual(body["grade"], "blocked")  # no parity yet
        self.store.save_artifact(slug, "parity",
                                 {"score": 100, "summary": {}})
        code, body = self.api("GET", "/api/readiness?slug=" + slug)
        self.assertEqual(code, 200)
        self.assertEqual(body["grade"], "ready")

    def test_readiness_requires_slug(self):
        code, body = self.api("GET", "/api/readiness")
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_readiness_unknown_slug_404(self):
        code, body = self.api("GET", "/api/readiness?slug=zzz")
        self.assertEqual(code, 404)
        self.assertIn("error", body)


class SamplesReviewTests(WebServerTestCase):
    """/api/samples job, /api/review roundtrip, readiness folding.

    Uses the REAL nr2grafana.samples module (it has no stubbed
    dependencies) against the fake GrafanaLive/NerdGraph, so the
    whole route -> collect -> persist path is exercised."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        websrv.SESSION.grafana_url = "http://gf.local:3000"
        websrv.SESSION.grafana_token = "tok"
        websrv.SESSION.nr_api_key = "NRAK-TEST"
        cls.slug = "samples-dash"
        dash = {"title": "S", "uid": cls.slug,
                "templating": {"list": []},
                "panels": [
                    {"id": 1, "title": "P1", "type": "timeseries",
                     "targets": [{"refId": "A", "expr": "up",
                                  "datasource": {
                                      "type": "prometheus",
                                      "uid": "mimir"}}]},
                    {"id": 2, "title": "Logs", "type": "logs",
                     "targets": [{
                         "refId": "A",
                         "expr": '{service_name="checkout"} | json',
                         "datasource": {"type": "loki",
                                        "uid": "loki1"}}]}]}
        cls.store.upsert_dashboard(cls.slug, "S", "seed", "", dash)
        cls.store.save_artifact(cls.slug, "widget-report", {
            "widgets": [
                {"panel_id": 1, "account_ids": [1],
                 "nrql": ["SELECT count(*) FROM Transaction"],
                 "queries": [{"expr": "up"}]},
                {"panel_id": 2, "account_ids": [1],
                 "nrql": ["SELECT count(*) FROM Log "
                          "WHERE service = 'checkout'"],
                 "queries": [{"expr": "x"}]}]})

    def test_samples_job_and_panel_merge(self):
        code, resp = self.api("POST", "/api/samples",
                              {"slug": self.slug, "limit": 3})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        art = self.store.get_artifact(self.slug, "samples")
        self.assertIsNotNone(art)
        self.assertEqual(art["schema"], "nr2grafana/samples/v1")
        rows = {r["panel_id"]: r for r in art["panels"]}
        self.assertEqual(sorted(rows), [1, 2])
        # prometheus side: datapoints from the fake frames
        gf1 = rows[1]["grafana"]
        self.assertEqual(gf1["kind"], "points")
        self.assertEqual(gf1["samples"][0]["points"][-1][1], 2.0)
        # loki side: raw log lines with timestamps
        gf2 = rows[2]["grafana"]
        self.assertEqual(gf2["kind"], "logs")
        self.assertEqual([s["line"] for s in gf2["samples"]],
                         ["log line one", "log line two"])
        self.assertTrue(gf2["samples"][0]["ts"])
        # NR side: aggregate rows for metrics, derived SELECT * for
        # the log panel
        self.assertEqual(rows[1]["nr"]["kind"], "rows")
        self.assertEqual(rows[1]["nr"]["samples"], [{"count": 1}])
        self.assertEqual(rows[2]["nr"]["kind"], "events")
        self.assertIn("SELECT * FROM Log", rows[2]["nr"]["nrql"])
        self.assertIn("LIMIT 3", rows[2]["nr"]["nrql"])
        # panel-scoped re-pull merges into the stored artifact
        code, resp = self.api("POST", "/api/samples",
                              {"slug": self.slug, "panel_id": 2})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        art = self.store.get_artifact(self.slug, "samples")
        self.assertEqual(sorted(r["panel_id"]
                                for r in art["panels"]), [1, 2])

    def test_samples_missing_slug_job_errors(self):
        code, resp = self.api("POST", "/api/samples", {})
        self.assertEqual(code, 200)  # job starts, then errors
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "error")
        self.assertIn("slug", job["error"])

    def test_review_roundtrip(self):
        slug = "review-dash"
        _seed_dash(self.store, slug)
        code, body = self.api(
            "POST", "/api/review",
            {"slug": slug, "panel_id": 1, "refId": "A",
             "verdict": "confirmed", "note": "looks right"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["review"]["verdict"], "confirmed")
        self.assertEqual(body["summary"]["confirmed"], 1)
        self.assertEqual(body["summary"]["unreviewed"], 0)
        code, got = self.api("GET", "/api/review?slug=" + slug)
        self.assertEqual(code, 200)
        self.assertEqual(got["reviews"]["1:A"]["note"],
                         "looks right")
        self.assertEqual(got["summary"]["confirmed"], 1)
        changes = self.store.list_changes(slug)
        self.assertTrue(any(c["action"] == "panel-review"
                            for c in changes))
        # verdicts merge: rejecting the same target replaces it
        code, body = self.api(
            "POST", "/api/review",
            {"slug": slug, "panel_id": 1, "refId": "A",
             "verdict": "rejected", "note": "wrong stream"})
        self.assertEqual(code, 200)
        self.assertEqual(body["summary"]["rejected"], 1)
        self.assertEqual(body["summary"]["confirmed"], 0)

    def test_review_invalid_verdict_400(self):
        slug = "review-dash-bad"
        _seed_dash(self.store, slug)
        code, body = self.api(
            "POST", "/api/review",
            {"slug": slug, "panel_id": 1, "refId": "A",
             "verdict": "maybe"})
        self.assertEqual(code, 400)
        self.assertIn("verdict", body["error"])

    def test_review_unknown_slug_404(self):
        code, body = self.api(
            "POST", "/api/review",
            {"slug": "zzz-none", "panel_id": 1, "refId": "A",
             "verdict": "confirmed"})
        self.assertEqual(code, 404)
        self.assertIn("error", body)
        code, body = self.api("GET", "/api/review?slug=zzz-none")
        self.assertEqual(code, 404)

    def test_readiness_folds_in_review(self):
        slug = "review-ready-dash"
        _seed_dash(self.store, slug)
        self.store.save_artifact(slug, "parity",
                                 {"score": 100, "summary": {}})
        code, body = self.api("GET", "/api/readiness?slug=" + slug)
        self.assertEqual(code, 200)
        self.assertEqual(body["grade"], "ready")
        self.api("POST", "/api/review",
                 {"slug": slug, "panel_id": 1, "refId": "A",
                  "verdict": "rejected", "note": "not my logs"})
        code, body = self.api("GET", "/api/readiness?slug=" + slug)
        self.assertEqual(code, 200)
        self.assertEqual(body["grade"], "blocked")
        self.assertEqual(body["review"]["rejected"], 1)

    def test_detail_and_listing_include_review(self):
        slug = "review-detail-dash"
        _seed_dash(self.store, slug)
        self.api("POST", "/api/review",
                 {"slug": slug, "panel_id": 1, "refId": "A",
                  "verdict": "confirmed"})
        code, det = self.api("GET", "/api/dashboards/" + slug)
        self.assertEqual(code, 200)
        self.assertIn("samples", det)
        self.assertIn("1:A", (det["review"] or {})["reviews"])
        code, data = self.api("GET", "/api/dashboards")
        self.assertEqual(code, 200)
        row = next(d for d in data["dashboards"]
                   if d["slug"] == slug)
        self.assertEqual(row["review_summary"]["confirmed"], 1)


class DownloadTests(WebServerTestCase):
    """Downloads stream attachments; slugs validated via the store."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.slug = "dl-dash"
        cls.dash = _seed_dash(cls.store, cls.slug, expr="up")
        cls.pkg = os.path.join(cls.tmp.name, "pkgs", cls.slug)
        os.makedirs(cls.pkg)
        with open(os.path.join(cls.pkg, "dashboard.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cls.dash, f)
        with open(os.path.join(cls.pkg, "README.md"), "w",
                  encoding="utf-8") as f:
            f.write("# readme\n")
        cls.store.set_setting("package_dir." + cls.slug, cls.pkg)

    def test_download_dashboard_json(self):
        code, headers, raw = http_bin(
            self.base, "/download/dashboard/%s.json" % self.slug)
        self.assertEqual(code, 200)
        self.assertIn("attachment",
                      headers.get("Content-Disposition", ""))
        self.assertIn(self.slug + ".json",
                      headers.get("Content-Disposition", ""))
        dash = json.loads(raw.decode("utf-8"))
        self.assertEqual(dash["uid"], self.slug)
        self.assertIn("panels", dash)

    def test_download_package_zip_is_valid_zip(self):
        code, headers, raw = http_bin(
            self.base, "/download/package/%s.zip" % self.slug)
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Type"),
                         "application/zip")
        self.assertIn("attachment",
                      headers.get("Content-Disposition", ""))
        zf = zipfile.ZipFile(io.BytesIO(raw))
        self.assertIsNone(zf.testzip())
        names = zf.namelist()
        self.assertIn(self.slug + "/dashboard.json", names)
        self.assertIn(self.slug + "/README.md", names)
        inner = json.loads(zf.read(self.slug + "/dashboard.json"))
        self.assertEqual(inner["uid"], self.slug)

    def test_download_all_zip(self):
        code, headers, raw = http_bin(self.base, "/download/all.zip")
        self.assertEqual(code, 200)
        zf = zipfile.ZipFile(io.BytesIO(raw))
        self.assertIsNone(zf.testzip())
        self.assertIn(self.slug + "/dashboard.json", zf.namelist())

    def test_download_unknown_slug_404(self):
        for path in ("/download/dashboard/no-such.json",
                     "/download/package/no-such.zip"):
            code, headers, raw = http_bin(self.base, path)
            self.assertEqual(code, 404, path)
            self.assertIn("error",
                          json.loads(raw.decode("utf-8")))

    def test_download_traversal_is_404(self):
        for path in ("/download/dashboard/..%2f..%2fetc%2fpasswd.json",
                     "/download/package/..%2f..%2fsecret.zip",
                     "/download/dashboard/../../etc/passwd.json"):
            code, headers, raw = http_bin(self.base, path)
            self.assertEqual(code, 404, path)

    def test_download_package_missing_dir_404(self):
        slug = "no-pkg-dash"
        _seed_dash(self.store, slug)
        code, headers, raw = http_bin(
            self.base, "/download/package/%s.zip" % slug)
        self.assertEqual(code, 404)


class MetricsAndLabelsTests(WebServerTestCase):
    """/api/metrics autocomplete (with 60s cache) and /api/labels."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        websrv.SESSION.grafana_url = "http://gf.local:3000"
        websrv.SESSION.grafana_token = "tok"

    def setUp(self):
        with websrv._METRICS_LOCK:
            websrv._METRICS_CACHE.clear()
        FakeGrafanaLive.metric_calls = []

    def test_metrics_fetch_filter_and_cache(self):
        code, body = self.api("GET", "/api/metrics?uid=mimir")
        self.assertEqual(code, 200)
        self.assertEqual(body["total"], 3)
        self.assertIn("up", body["metrics"])
        # second call with a filter must be served from the cache
        code, body = self.api("GET", "/api/metrics?uid=mimir&q=total")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(body["metrics"]),
                         ["http_requests_total",
                          "node_cpu_seconds_total"])
        self.assertEqual(FakeGrafanaLive.metric_calls, ["mimir"])

    def test_metrics_cache_expires(self):
        with websrv._METRICS_LOCK:
            websrv._METRICS_CACHE["mimir"] = (
                time.time() - websrv._METRICS_TTL - 1,
                ["stale_metric"])
        code, body = self.api("GET", "/api/metrics?uid=mimir")
        self.assertEqual(code, 200)
        self.assertNotIn("stale_metric", body["metrics"])
        self.assertEqual(FakeGrafanaLive.metric_calls, ["mimir"])

    def test_metrics_capped_at_200(self):
        with websrv._METRICS_LOCK:
            websrv._METRICS_CACHE["big"] = (
                time.time(), ["m%03d" % i for i in range(300)])
        code, body = self.api("GET", "/api/metrics?uid=big")
        self.assertEqual(code, 200)
        self.assertEqual(body["total"], 300)
        self.assertEqual(len(body["metrics"]), 200)
        self.assertEqual(FakeGrafanaLive.metric_calls, [])

    def test_metrics_requires_uid(self):
        code, body = self.api("GET", "/api/metrics")
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_labels_prometheus_values(self):
        code, body = self.api(
            "GET", "/api/labels?uid=mimir&type=prometheus&label=job")
        self.assertEqual(code, 200)
        self.assertEqual(body["values"], ["api", "web"])

    def test_labels_loki(self):
        code, body = self.api("GET",
                              "/api/labels?uid=loki1&type=loki")
        self.assertEqual(code, 200)
        self.assertEqual(body["labels"], ["job", "namespace"])
        code, body = self.api(
            "GET", "/api/labels?uid=loki1&type=loki&label=namespace")
        self.assertEqual(code, 200)
        self.assertEqual(body["values"], ["default"])

    def test_labels_bad_type_400(self):
        code, body = self.api("GET",
                              "/api/labels?uid=x&type=graphite")
        self.assertEqual(code, 400)
        self.assertIn("error", body)


class CompareRouteTests(WebServerTestCase):
    """1.4 comparison + live datasource-flow routes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        websrv.SESSION.grafana_url = "http://gf.local:3000"
        websrv.SESSION.grafana_token = "tok"
        websrv.SESSION.nr_api_key = "NRAK-TEST"
        cls.slug = "compare-dash"
        cls.dash = {"title": "Cmp", "uid": cls.slug,
                    "templating": {"list": []},
                    "panels": [
                        {"id": 1, "title": "Throughput",
                         "type": "timeseries",
                         "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                         "targets": [{"refId": "A", "expr": "up",
                                      "datasource": {
                                          "type": "prometheus",
                                          "uid": "mimir"}}]},
                        {"id": 2, "title": "Errors",
                         "type": "stat",
                         "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
                         "targets": [{"refId": "A",
                                      "expr": "errors_total",
                                      "datasource": {
                                          "type": "prometheus",
                                          "uid": "mimir"}}]}]}
        cls.store.upsert_dashboard(cls.slug, "Cmp", "seed", "", cls.dash)
        cls.store.save_artifact(cls.slug, "widget-report", {
            "widgets": [{"panel_id": 1, "account_ids": [1]},
                        {"panel_id": 2, "account_ids": [1]}]})
        cls.store.save_artifact(cls.slug, "nr-source",
                                {"name": "Cmp", "pages": [{"widgets": []}]})
        cls.store.save_artifact(
            cls.slug, "requirements",
            STUBS["nr2grafana.requirements"].analyze_dashboard(
                None, cls.dash, [], {}))

    def test_compare_job_persists_artifact_and_returns_panels(self):
        code, resp = self.api("POST", "/api/compare",
                              {"slug": self.slug, "from": "now-6h"})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        self.assertTrue(any("compare stub building" in ln
                            for ln in job["log"]))
        result = job["result"]
        self.assertEqual(result["schema"], "nr2grafana/comparison/v1")
        self.assertEqual(result["range"]["from"], "now-6h")
        self.assertEqual([p["panel_id"] for p in result["panels"]],
                         [1, 2])
        # NR source json was threaded through from the stored artifact
        self.assertTrue(result["nr_source_present"])
        # account ids came from the widget report
        self.assertEqual(result["account_ids"], [1])
        art = self.store.get_artifact(self.slug, "comparison")
        self.assertIsNotNone(art)
        self.assertEqual(art["score"], 100)

    def test_compare_dash_only_when_no_nr_source(self):
        slug = "compare-nosrc"
        _seed_dash(self.store, slug, expr="up")
        code, resp = self.api("POST", "/api/compare", {"slug": slug})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        self.assertFalse(job["result"]["nr_source_present"])

    def test_compare_missing_slug_job_errors(self):
        code, resp = self.api("POST", "/api/compare", {})
        self.assertEqual(code, 200)  # job starts, then errors
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "error")
        self.assertIn("slug", job["error"])

    def test_compare_requires_grafana_url(self):
        websrv.SESSION.grafana_url = ""
        try:
            code, body = self.api("POST", "/api/compare",
                                  {"slug": self.slug})
            self.assertEqual(code, 400)
            self.assertIn("Grafana URL", body["error"])
        finally:
            websrv.SESSION.grafana_url = "http://gf.local:3000"

    def test_verify_flow_counts(self):
        code, body = self.api(
            "POST", "/api/datasource/new-ds/verify-flow",
            {"slug": self.slug})
        self.assertEqual(code, 200)
        self.assertEqual(body["uid"], "new-ds")
        flow = body["flow"]
        self.assertEqual(flow["panels_with_data"], 2)
        self.assertEqual(flow["panels_total"], 2)
        self.assertEqual(flow["uid"], "new-ds")
        self.assertEqual(flow["newly_flowing"], [1])

    def test_verify_flow_bad_slug_404(self):
        code, body = self.api(
            "POST", "/api/datasource/new-ds/verify-flow",
            {"slug": "no-such-slug"})
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_verify_flow_requires_slug(self):
        code, body = self.api(
            "POST", "/api/datasource/new-ds/verify-flow", {})
        self.assertEqual(code, 400)
        self.assertIn("slug", body["error"])

    def test_create_datasource_includes_flow_block(self):
        code, body = self.api(
            "POST", "/api/grafana/datasource",
            {"type": "prometheus", "name": "Mimir3",
             "values": {"url": "http://mimir:9009/prometheus"},
             "slug": self.slug})
        self.assertEqual(code, 200)
        self.assertEqual(body["uid"], "new-ds")
        self.assertIn("flow", body)
        self.assertEqual(body["flow"]["panels_with_data"], 2)
        self.assertEqual(body["flow"]["uid"], "new-ds")

    def test_create_datasource_without_slug_has_no_flow(self):
        code, body = self.api(
            "POST", "/api/grafana/datasource",
            {"type": "prometheus", "name": "Mimir4",
             "values": {"url": "http://mimir:9009/prometheus"}})
        self.assertEqual(code, 200)
        self.assertNotIn("flow", body)

    def test_panel_data_one_side(self):
        code, body = self.api(
            "GET", "/api/panel-data?slug=%s&panel_id=1&side=grafana"
            % self.slug)
        self.assertEqual(code, 200)
        self.assertEqual(body["panel_id"], 1)
        self.assertEqual(body["side"], "grafana")
        self.assertEqual(body["viz"], "timeseries")
        self.assertEqual(body["data"]["kind"], "series")
        self.assertEqual(body["data"]["series"][0]["points"][-1],
                         [1060, 2.0])

    def test_panel_data_nr_side(self):
        code, body = self.api(
            "GET", "/api/panel-data?slug=%s&panel_id=2&side=nr"
            % self.slug)
        self.assertEqual(code, 200)
        self.assertEqual(body["panel_id"], 2)
        self.assertEqual(body["side"], "nr")

    def test_panel_data_bad_side_400(self):
        code, body = self.api(
            "GET", "/api/panel-data?slug=%s&panel_id=1&side=middle"
            % self.slug)
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_panel_data_unknown_panel_404(self):
        code, body = self.api(
            "GET", "/api/panel-data?slug=%s&panel_id=999&side=nr"
            % self.slug)
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_panel_data_bad_slug_404(self):
        code, body = self.api(
            "GET", "/api/panel-data?slug=zzz&panel_id=1&side=nr")
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_panel_data_requires_slug_and_panel(self):
        code, body = self.api("GET", "/api/panel-data?panel_id=1")
        self.assertEqual(code, 400)
        code, body = self.api("GET",
                              "/api/panel-data?slug=" + self.slug)
        self.assertEqual(code, 400)

    def test_state_features_include_compare(self):
        code, st = self.api("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertTrue(st["features"].get("compare"))


class CostRouteTests(WebServerTestCase):
    """1.5 cost & efficiency routes: traffic/cost jobs, pricing
    roundtrip, cost-config zip, and the cost feature flag."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        websrv.SESSION.grafana_url = "http://gf.local:3000"
        websrv.SESSION.grafana_token = "tok"
        cls.slug = "cost-dash"
        _seed_dash(cls.store, cls.slug, expr="up")
        cls.store.save_artifact(cls.slug, "widget-report",
                                {"widgets": [{"panel_id": 1}]})

    def test_traffic_job_persists_instance_artifact(self):
        code, resp = self.api("POST", "/api/traffic",
                              {"from": "now-12h"})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        self.assertTrue(any("traffic stub sampling" in ln
                            for ln in job["log"]))
        self.assertEqual(job["result"]["schema"],
                         "nr2grafana/traffic/v1")
        self.assertEqual(job["result"]["range"]["from"], "now-12h")
        art = self.store.get_artifact(websrv._INSTANCE_SLUG, "traffic")
        self.assertIsNotNone(art)
        self.assertEqual(art["schema"], "nr2grafana/traffic/v1")
        # sampled from the fake instance's prometheus datasource
        self.assertEqual(art["datasources"][0]["uid"], "mimir")

    def test_traffic_requires_grafana_url(self):
        websrv.SESSION.grafana_url = ""
        try:
            code, body = self.api("POST", "/api/traffic", {})
            self.assertEqual(code, 400)
            self.assertIn("Grafana URL", body["error"])
        finally:
            websrv.SESSION.grafana_url = "http://gf.local:3000"

    def test_cost_job_persists_cost_and_optimize(self):
        code, resp = self.api("POST", "/api/cost", {})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        self.assertTrue(any("optimize stub running" in ln
                            for ln in job["log"]))
        result = job["result"]
        self.assertEqual(result["cost"]["schema"], "nr2grafana/cost/v1")
        self.assertEqual(result["optimize"]["schema"],
                         "nr2grafana/optimize/v1")
        self.assertEqual(result["savings"]["saved_total"], 12.5)
        # instance-wide run persists under the instance slug
        self.assertIsNotNone(
            self.store.get_artifact(websrv._INSTANCE_SLUG, "cost"))
        self.assertIsNotNone(
            self.store.get_artifact(websrv._INSTANCE_SLUG, "optimize"))

    def test_cost_job_with_slug_persists_per_dashboard(self):
        code, resp = self.api("POST", "/api/cost",
                              {"slug": self.slug})
        self.assertEqual(code, 200)
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["slug"], self.slug)
        self.assertIsNotNone(
            self.store.get_artifact(self.slug, "optimize"))

    def test_cost_unknown_slug_errors(self):
        code, resp = self.api("POST", "/api/cost", {"slug": "zzz-no"})
        self.assertEqual(code, 200)  # job starts, then errors
        job = poll_job(self.base, resp["job"])
        self.assertEqual(job["status"], "error")
        self.assertIn("zzz-no", job["error"])

    def test_pricing_roundtrip_persists_non_secret(self):
        code, body = self.api("GET", "/api/pricing")
        self.assertEqual(code, 200)
        self.assertIn("loki_gb_ingest", body["pricing"])
        self.assertIn("defaults", body)
        code, body = self.api("POST", "/api/pricing",
                              {"pricing": {"loki_gb_ingest": 0.9}})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["pricing"]["loki_gb_ingest"], 0.9)
        # persisted to disk as a plain non-secret setting
        with open(self.db_path, encoding="utf-8") as f:
            on_disk = f.read()
        self.assertIn("web.pricing", on_disk)
        self.assertIn("0.9", on_disk)
        # GET now reflects the override merged over defaults
        code, body = self.api("GET", "/api/pricing")
        self.assertEqual(body["pricing"]["loki_gb_ingest"], 0.9)
        self.assertIn("mimir_1k_series_month", body["pricing"])

    def test_pricing_empty_400(self):
        code, body = self.api("POST", "/api/pricing", {})
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_cost_config_zip_is_valid_zip(self):
        # run a cost analysis so the optimize artifact exists
        code, resp = self.api("POST", "/api/cost", {})
        self.assertEqual(code, 200)
        poll_job(self.base, resp["job"])
        code, headers, raw = http_bin(self.base,
                                      "/download/cost-config.zip")
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Type"),
                         "application/zip")
        self.assertIn("attachment",
                      headers.get("Content-Disposition", ""))
        zf = zipfile.ZipFile(io.BytesIO(raw))
        self.assertIsNone(zf.testzip())
        names = zf.namelist()
        self.assertIn("promtail.yaml", names)
        self.assertIn("loki-limits.yaml", names)
        self.assertIn("README.md", names)
        self.assertIn("labeldrop", zf.read("promtail.yaml").decode())

    def test_cost_config_zip_without_run_404(self):
        # a fresh slug with no optimize artifact
        slug = "cost-noopt"
        _seed_dash(self.store, slug)
        code, headers, raw = http_bin(
            self.base, "/download/cost-config.zip?slug=" + slug)
        self.assertEqual(code, 404)
        self.assertIn("error", json.loads(raw.decode("utf-8")))

    def test_state_features_include_cost(self):
        code, st = self.api("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertTrue(st["features"].get("cost"))


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
