# nr2grafana 1.1 — module contracts

Version 1.1.0 turns the converter into an end-to-end migration product:
per-dashboard requirement analysis + packaged artifact directories, live
Grafana validation/testing through a service-account token, persistent local
storage, a change log that can be codified back into config, optional Claude
AI assistance, and a localhost web UI. **Zero runtime dependencies remains a
hard rule: Python 3.9+ stdlib only.**

Every module below is a NEW file unless noted. Public APIs are contracts —
implement exactly these signatures so parallel work integrates cleanly.
Style: match existing code (PEP8, 79-col-ish, docstrings, `from __future__
import annotations`, typing via `typing`).

## Existing building blocks (do not break)

- `model.py`: `NRDashboard(name, description, pages, variables, guid,
  permissions)`, `NRPage(name, widgets)`, `NRWidget(title, viz_id, layout,
  raw_configuration)`; `widget.nrql_queries -> [{"accountIds", "query"}]`.
- `grafana/builder.py`: `build_dashboards(nr, cfg) -> [(filename, dash_json,
  widget_report)]`. Widget report entries: `{"page", "widget_title",
  "panel_id", "panel_type", "confidence": "exact|approximate|needs-review|
  untranslatable", "nrql": [...], "queries": [{"datasource": "prometheus|
  loki|tempo|newrelic", "expr", "type"}], "notes": [...], "fallback"?}`.
- `grafana/client.py`: `GrafanaClient(url, token, basic, timeout, insecure)`
  with `.health() .datasources() .find_or_create_folder(title)
  .import_dashboard(dash, folder_uid, overwrite, message)`; raises
  `GrafanaError`.
- `config.py`: `load_config(path) -> cfg dict` (see DEFAULT_CONFIG).
- `livecheck.py`: direct Prom/Loki syntax checks (`substitute`,
  `iter_targets`, `collect_files`, `check_files`).
- `cli.py` commands: list/fetch/convert/validate/example-config/interactive.

## 1. `nr2grafana/store.py` — persistent local storage (sqlite3)

DB at `~/.nr2grafana/nr2grafana.db` (dir created 0700). **Never store
secrets/API keys.**

```python
DEFAULT_DIR = os.path.expanduser("~/.nr2grafana")

class Store:  # context manager; __init__(self, path: str = "") -> default db
    def record_run(self, kind: str, meta: Dict[str, Any]) -> int
    def finish_run(self, run_id: int, status: str,
                   summary: Dict[str, Any]) -> None
    def list_runs(self, kind: str = "", limit: int = 50) -> List[Dict]
    def upsert_dashboard(self, slug: str, title: str, source: str,
                         nr_guid: str, data: Dict[str, Any]) -> int
    def get_dashboard(self, slug: str) -> Optional[Dict[str, Any]]
    def list_dashboards(self) -> List[Dict[str, Any]]  # metadata rows
    def save_artifact(self, slug: str, kind: str,
                      data: Dict[str, Any]) -> None
        # kind: "requirements" | "widget-report" | "datatest" | "check"
    def get_artifact(self, slug: str, kind: str) -> Optional[Dict[str, Any]]
    def log_change(self, slug: str, change: Dict[str, Any]) -> int
        # change: {"action", "target", "before", "after", "why", "source"}
        # source: "user" | "ai" | "auto"; store adds "ts" (iso8601) + "id"
    def list_changes(self, slug: str = "") -> List[Dict[str, Any]]
    def set_setting(self, key: str, value: Any) -> None   # JSON-encoded
    def get_setting(self, key: str, default: Any = None) -> Any
```

Schema versioned via `PRAGMA user_version`; WAL mode; thread-safe (one
connection per call or a lock — web server calls from threads).

## 2. `nr2grafana/requirements.py` — datasource requirement analyzer

```python
def analyze_dashboard(nr: Optional[NRDashboard], dash: Dict[str, Any],
                      widget_report: List[Dict[str, Any]],
                      cfg: Dict[str, Any]) -> Dict[str, Any]
```

Returns a `requirements` dict:

```jsonc
{
  "schema": "nr2grafana/requirements/v1",
  "dashboard": "...title...", "uid": "...", "generated_by": "nr2grafana 1.1.0",
  "datasources": [            // what must exist in Grafana before import
    {"family": "prometheus", "plugin_id": "prometheus", "core": true,
     "uid_ref": "${datasource}",       // as referenced in dashboard.json
     "purpose": "metrics (Mimir/Prometheus)", "panel_ids": [1,2],
     "required": true}
  ],
  "plugins": [{"id": "nrgrafanaplugin-newrelic-datasource",
               "reason": "passthrough panels", "grafana_cli":
               "grafana cli plugins install ..."}],
  "domains": [                // detected NR-native data domains + equivalents
    {"domain": "aws-lambda", "evidence": ["FROM AwsLambdaInvocation", "metric aws.lambda.Duration"],
     "panel_ids": [3],
     "options": [
       {"kind": "datasource", "plugin_id": "cloudwatch", "core": true,
        "note": "CloudWatch datasource; needs AWS credentials/role"},
       {"kind": "pipeline", "note": "YACE/cloudwatch-exporter or OTel collector awscloudwatch receiver -> Mimir; then metrics appear as aws_lambda_*"}]}
  ],
  "nr_native": [              // widgets that have no LGTM equivalent as-is
    {"panel_id": 5, "widget": "viz.billboard", "why": "...",
     "equivalent": "stat panel + <what data source>"}],
  "data_expectations": [      // per translated panel, what must exist
    {"panel_id": 1, "datasource": "prometheus",
     "needs": {"metrics": ["http_server_request_duration_seconds_bucket"],
               "labels": ["service_name"]}},
    {"panel_id": 2, "datasource": "loki",
     "needs": {"stream_selector": "{service_name=\"x\"}", "labels": [...]}}
  ],
  "import": {"steps": ["1. create datasources ...", "2. paste dashboard.json ..."],
             "api_example": "curl -s -H \"Authorization: Bearer $GRAFANA_TOKEN\" ..."}
}
```

Domain knowledge table (module-level, extensible via
`cfg.get("domain_map")`): map NR event types / metric prefixes to domains
and Grafana equivalents. Cover at least: `aws.lambda*` / `AwsLambda*` →
cloudwatch; `aws.*` generic → cloudwatch; `gcp.*` → stackdriver
(Google Cloud Monitoring); `azure.*` → grafana-azure-monitor-datasource;
`ProcessSample`/`SystemSample`/`NetworkSample`/`StorageSample` →
node_exporter metrics in Mimir; `K8s*Sample` → kube-state-metrics/cAdvisor;
`Log` → loki (lambda logs alternative: cloudwatch logs);
`Span`/`DistributedTrace*` → tempo + span-metrics; `Transaction`/
`TransactionError` → OTel APM metrics in Mimir; `SyntheticCheck` →
blackbox_exporter or Grafana Synthetic Monitoring; `PageView*`/
`Browser*`/`JavaScriptError` → Grafana Faro (RUM); `Mobile*` → Faro;
`NrConsumption`/`NrUsage`/`NrAuditEvent` → New Relic-only (needs NR plugin
passthrough). Extract metric names / label needs from the translated PromQL
(regex over expr is fine) and LogQL stream selectors.

## 3. `nr2grafana/artifacts.py` — per-dashboard package directories

```python
def package_dashboard(out_dir: str, slug: str, dash: Dict[str, Any],
                      widget_report: List[Dict[str, Any]],
                      requirements: Dict[str, Any],
                      cfg: Dict[str, Any]) -> str  # returns package dir
def write_index(out_dir: str, entries: List[Dict[str, Any]]) -> str
```

`package_dashboard` writes `<out>/<slug>/`: `dashboard.json`,
`requirements.json`, `widget-report.json`, `README.md` (human guide:
required datasources table w/ plugin ids, NR-native widget notes with
equivalents, exact import steps for UI paste AND API, troubleshooting), and
`test.sh` (executable; curl-based smoke test hitting Grafana
`/api/ds/query` for each panel query — reads `GRAFANA_URL`/`GRAFANA_TOKEN`
env; degrade gracefully) plus `datatest.json` (machine manifest of per-panel
test queries used by grafana/live.py and test.sh). `write_index` writes
`<out>/INDEX.md` summarizing all dashboards (name, panels, confidence
counts, required datasources, package dir).

## 4. `nr2grafana/grafana/live.py` — live Grafana ops via service account

```python
class GrafanaLive(GrafanaClient):
    def plugins(self) -> List[Dict[str, Any]]                # GET /api/plugins
    def datasource_by_uid(self, uid: str) -> Optional[Dict]
    def search_dashboards(self, query: str = "") -> List[Dict]
    def get_dashboard_by_uid(self, uid: str) -> Dict          # /api/dashboards/uid/<uid>
    def create_datasource(self, payload: Dict) -> Dict
    def resolve_ds_map(self, dash: Dict,
                       preferred: Optional[Dict[str, str]] = None) -> Dict[str, str]
        # map template var name / uid_ref -> concrete datasource uid on this
        # instance (first ds of matching type unless preferred overrides)
    def check_requirements(self, requirements: Dict) -> List[Dict]
        # per required datasource/plugin: {"item", "status": "ok|missing|
        #  no-default|wrong-type", "detail", "fix"}  (fix = actionable text)
    def ds_query(self, ds_uid: str, ds_type: str, target: Dict,
                 frm: str = "now-1h", to: str = "now") -> Dict
        # POST /api/ds/query with correct body per type (prometheus/loki/
        # tempo/cloudwatch passthrough-generic)
    def test_dashboard(self, dash: Dict,
                       ds_map: Optional[Dict[str, str]] = None,
                       log=None) -> List[Dict]
        # per target: {"panel_id","panel_title","refId","datasource",
        #  "expr","status":"data|no-data|error","error":str,"frames":int,
        #  "points":int}
    def update_dashboard(self, dash: Dict, folder_uid: str = "",
                         message: str = "") -> Dict   # overwrite=True import
```

`test_dashboard` substitutes template vars using `livecheck.substitute`
plus the resolved ds_map; never raises per-panel (collect errors).

## 5. `nr2grafana/changelog.py` — change tracking & codify

```python
class ChangeLog:
    def __init__(self, store: Store): ...
    def record(self, slug: str, action: str, target: str,
               before: Any, after: Any, why: str = "",
               source: str = "user") -> int
    def report(self, slug: str = "") -> Dict[str, Any]   # json report
    def report_markdown(self, slug: str = "") -> str
    def suggest_config(self, slug: str = "") -> Dict[str, Any]
        # infer config overlay from recorded query edits: label renames ->
        # label_map entries, metric renames -> metric_map, ds uid changes ->
        # datasources.*.uid; returns a mergeable config overlay + rationale
```

Actions: `"query-edit" | "datasource-set" | "panel-edit" | "import" |
"datasource-created" | "dashboard-updated"`.

## 6. `nr2grafana/ai.py` — Claude assistance (optional)

Stdlib urllib to `https://api.anthropic.com/v1/messages`, headers
`x-api-key`, `anthropic-version: 2023-06-01`, `content-type:
application/json`. Key from arg or `ANTHROPIC_API_KEY` env. Default model
`"claude-sonnet-5"`, overridable. Never log/store the key.

```python
class AIError(Exception): ...
class AIAssist:
    def __init__(self, api_key: str = "", model: str = "")
    @property
    def available(self) -> bool
    def suggest_fix(self, context: Dict[str, Any]) -> Dict[str, Any]
        # context: {"panel","expr","error","datasource","requirements",
        #  "instance": {"datasources":[...], "metrics_sample":[...]}, "nrql"}
        # returns {"explanation": str, "fixed_expr": Optional[str],
        #  "confidence": "high|medium|low", "actions": [str]}
        # (ask Claude for STRICT JSON; parse defensively, fall back to
        #  {"explanation": raw_text})
    def chat(self, messages: List[Dict[str, str]],
             system: str = "") -> str
```

## 7. `nr2grafana/web/` — localhost web UI

`nr2grafana/web/__init__.py` (exports `serve`), `server.py`, `ui.py`
(embedded single-page HTML/CSS/JS as a module string — no external assets,
no CDN). `serve(host="127.0.0.1", port=8765, open_browser=True, store=None)`
uses `ThreadingHTTPServer`. Binds localhost only by default.

JSON API (all under `/api/`, request/response JSON; errors as
`{"error": msg}` with 4xx/5xx):

- `GET /api/state` — session config status (which keys set, urls), db stats
- `POST /api/settings` — set NR/Grafana/Anthropic keys + urls **in process
  memory only** (non-secret prefs like urls/dirs also go to Store settings)
- `POST /api/nr/list`, `POST /api/nr/fetch {guids?, out}` — via nerdgraph
- `POST /api/convert {input_dir, out_dir, config_path?, package: true}` —
  convert + analyze + package; persists dashboards/artifacts to Store
- `GET /api/dashboards`, `GET /api/dashboards/<slug>` (incl. requirements,
  reports, changes, test results)
- `POST /api/grafana/health|datasources|plugins`
- `POST /api/grafana/check {slug}` — check_requirements
- `POST /api/grafana/test {slug}` — test_dashboard, persist results
- `POST /api/grafana/import {slug, folder, overwrite}`
- `POST /api/panel/update {slug, panel_id, refId, expr, why}` — edit query
  in stored dashboard json, record change, optional re-test, optional live
  `update_dashboard` when `{push: true}`
- `GET /api/changes?slug=`, `GET /api/changes/suggest-config?slug=`
- `POST /api/ai/suggest`, `POST /api/ai/chat`

Long operations (fetch/convert/test) run in a background thread with a job
id: `POST` returns `{"job": id}`; `GET /api/jobs/<id>` returns
`{"status","log":[...],"result"}`. UI polls.

UI requirements: clean, readable, dark theme default with light support;
left nav (Setup, Dashboards, Convert, Validate & Test, Changes, AI
Assistant); dashboard detail view shows panels with confidence badges,
per-panel test status (data/no-data/error with the actual error text), an
inline query editor with "Test" / "Ask AI" / "Save & push to Grafana"
buttons; requirements card ("install these datasources first"); bulk
import/export buttons; every error surfaced human-readably. No frameworks —
vanilla JS, fetch(), ~single file. It must look professional (spacing,
type scale, subtle borders), not like a demo page.

## 8. CLI additions (`cli.py`) — keep every existing command working

- `convert ... --package` — after building each dashboard, run
  requirements.analyze_dashboard + artifacts.package_dashboard (per-dash
  dirs instead of flat files; flat remains default), write INDEX.md, and
  persist to Store.
- `analyze <converted-dir-or-nr-json...> [-o out]` — (re)generate
  requirements/packages for already-converted output (uses
  migration-report.json next to the files when present).
- `grafana check <package-dir|requirements.json...> --url --token` — live
  requirement check; exit 1 on missing.
- `grafana test <package-dir|dashboard.json...> --url --token` — per-panel
  data tests; writes datatest-results.json in package dir; exit 1 on error
  panels (no-data is a warning, not failure).
- `grafana import <dir|files> --url --token [--folder F] [--overwrite]`
- `changes report [--slug S] [--markdown]`, `changes suggest-config`
- `web [--port 8765] [--no-browser] [--host 127.0.0.1]`
- Grafana args: `--grafana-url` / `GRAFANA_URL`, `--grafana-token` /
  `GRAFANA_TOKEN` (service account token).
- `interactive.py`: add wizard entries for package/check/test/web (reuse its
  existing menu helpers; read the file first and follow its patterns).
- Bump version to 1.1.0 in pyproject.toml; `__init__.py` gets
  `__version__ = "1.1.0"`.

## Cross-cutting rules

1. stdlib only; Python 3.9 compatible (no `match`, no `X | Y` types).
2. Secrets live in process memory/env only — never in Store, files, or logs.
3. Every module gets unit tests in `tests/test_<module>.py` (stdlib
   unittest, follow existing test style; mock HTTP with local
   http.server or by stubbing `_req`/`urlopen`).
4. Network failures must degrade to actionable messages, never tracebacks.
5. All new files ASCII, LF, 4-space indent.
