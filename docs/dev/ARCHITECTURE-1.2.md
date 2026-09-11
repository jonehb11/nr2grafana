# nr2grafana 1.2 — module contracts

Goal: a one-stop shop. After entering a New Relic API key and a Grafana
service-account token, the user should NEVER need to open New Relic or
Grafana directly: fetch, convert, see exactly what is required, create and
health-test datasources from the UI, compare real data New Relic vs
Grafana side by side, get root-cause explanations for every failure with
one-click fixes, push fixes live, and download validated dashboard JSON.

Builds on 1.1 (see ARCHITECTURE-1.1.md — all 1.1 contracts still hold).
**Zero runtime dependencies: Python 3.9+ stdlib only.** Style matches the
existing codebase. Version 1.2.0.

## 1. `nerdgraph.py` — run NRQL (read-only; owned by parity agent)

Add to `NerdGraphClient`:

```python
def run_nrql(self, account_id: int, nrql: str) -> Dict[str, Any]
    # NerdGraph: {actor{account(id:$id){nrql(query:$q, timeout:30)
    #   {results metadata{facets timeWindow{begin end}}}}}}
    # -> {"results":[...], "metadata": {...}}; raises NerdGraphError with
    # actionable text on GraphQL errors (bad key, bad account, NRQL syntax)
```

Strictly read-only — never add mutations.

## 2. `nr2grafana/parity.py` — NR vs Grafana data comparison

The killer feature: prove the migrated panel shows the same data.

```python
def normalize_nr(results: List[Dict], nrql: str) -> List[Dict]
def normalize_grafana(ds_query_response: Dict, ref_id: str) -> List[Dict]
    # both -> Series list: {"labels": {..}, "points": [[epoch_s, float],...]}
    #   scalars become one-point series; faceted NR results -> one series
    #   per facet with labels {"facet": value} (or attr name when known)
def compare(nr_series, gf_series, tolerance: float = 0.15) -> Dict
    # {"verdict": "match|close|value-mismatch|shape-mismatch|nr-empty|
    #   gf-empty|both-empty", "detail": str, "ratio": Optional[float],
    #   "nr_summary": {...}, "gf_summary": {...}}
    # 'close': within tolerance OR a consistent constant ratio (unit hints:
    # ~1000 -> ms vs s; ~60 -> per-min vs per-s; report the hint in detail)
def run_parity(nr, account_ids, grafana, dash, widget_report,
               ds_map=None, frm="now-1h", to="now", log=None) -> Dict
    # per panel/target with original NRQL + translated expr: run both sides
    # (NR via nerdgraph.run_nrql over the widget's accountIds; Grafana via
    # GrafanaLive.ds_query), normalize, compare. Per-side errors become
    # verdict nr-error / gf-error with the error text; never raises per
    # panel. Returns schema "nr2grafana/parity/v1":
    # {"schema", "dashboard", "generated_at", "range": {"from","to"},
    #  "panels": [{"panel_id","panel_title","refId","nrql","expr",
    #    "datasource","verdict","detail","ratio","nr_summary","gf_summary"}],
    #  "score": 0-100, "summary": {verdict: count}}
def readiness(parity, check_rows=None, test_rows=None) -> Dict
    # {"score": 0-100, "grade": "ready|almost|blocked", "reasons":[...]}
```

Scoring: match=1.0, close=0.8, gf data w/o NR comparison=0.6, empty both
=0.5, mismatch=0.25, errors=0. Store artifact kind "parity".

## 3. `nr2grafana/diagnose.py` — root-cause engine

```python
def diagnose(grafana, nr=None, dash=None, requirements=None,
             test_results=None, parity=None, cfg=None, log=None) -> Dict
```

Layered checks, each producing findings; schema
"nr2grafana/diagnosis/v1": `{"schema", "generated_at", "findings": [
{"id", "severity": "blocker|warn|info", "area": "auth|datasource|panel|
data|config", "panel_id": opt, "problem", "evidence", "fix": {
"description", "kind": "add-datasource|edit-query|config-overlay|
install-plugin|credentials|pipeline|none", "action": opt machine payload}}],
"summary": {...}}`.

Checks (each degrades gracefully if a client is absent):
1. **Auth**: Grafana `/api/user` or `/api/org` (401 -> bad token; 403 ->
   insufficient role — say exactly what role is needed: Editor+ for
   dashboards, Admin for datasource creation). NR: cheap NerdGraph
   `{actor{user{email}}}` (bad key text). Report as findings, not raises.
2. **Datasources**: from requirements — missing (fix kind add-datasource
   with an `action` = ready `create_datasource` payload template),
   wrong-type, and for existing ones call `GrafanaLive.datasource_health`
   — failing health includes the health message + likely causes (URL
   unreachable from Grafana server, auth, TLS).
3. **Per-panel no-data root cause** (uses test_results/parity rows with
   gf-empty/error): for prometheus panels — fetch metric names once via
   `GrafanaLive.prom_metric_names`, exact-match the expr's metrics;
   missing -> difflib.get_close_matches over the instance's metrics ->
   "did you mean" with fix kind edit-query (action: {"panel_id","refId",
   "new_expr"}) AND config-overlay (metric_map entry); if metric exists
   but series empty -> drop matchers one at a time (via instant queries)
   to find the offending label matcher, then list that label's actual
   values (`prom_label_values`) -> fix edit-query / config-overlay
   (label_map). Check `_total` suffix flip specifically (config
   metric_total_suffix). For loki — `loki_labels`/`loki_label_values`:
   verify stream selector labels exist, suggest close matches, flag
   parser-stage mismatches (json vs logfmt) when `| json` yields nothing.
4. **Data pipeline**: NR side has data (parity nr_summary) but Grafana
   empty and metric truly absent -> explain the missing pipeline (from
   requirements domains, e.g. "no aws_lambda_* metrics in Mimir: install
   cloudwatch datasource OR deploy cloudwatch-exporter") — fix kind
   add-datasource or pipeline.
5. **Config**: recurring rename patterns across panels -> one
   config-overlay finding consolidating them.

## 4. `grafana/live.py` additions — datasource management (owned by ds-mgmt agent)

```python
def update_datasource(self, uid: str, payload: Dict) -> Dict   # PUT
def delete_datasource(self, uid: str) -> None
def datasource_health(self, uid: str) -> Dict
    # GET /api/datasources/uid/<uid>/health -> {"status":"ok|error",
    #  "message"}; some types 404 -> fall back to a probe query via
    #  ds_query; never raises, returns status dict
def prom_metric_names(self, uid: str) -> List[str]
    # GET /api/datasources/proxy/uid/<uid>/api/v1/label/__name__/values
def prom_label_values(self, uid: str, label: str,
                      match: str = "") -> List[str]
def prom_series(self, uid: str, match: str, frm="now-1h") -> List[Dict]
def loki_labels(self, uid: str) -> List[str]
def loki_label_values(self, uid: str, label: str) -> List[str]
def permissions_report(self) -> Dict
    # what this token can do: {"user": ..., "role", "can_admin_datasources":
    #  bool, "can_edit_dashboards": bool, "detail": str} — probe with GETs,
    #  never destructive

DS_TEMPLATES: Dict[str, Dict]  # guided add-datasource form specs:
# prometheus, loki, tempo, cloudwatch, stackdriver,
# grafana-azure-monitor-datasource, nrgrafanaplugin-newrelic-datasource.
# Each: {"label", "plugin_id", "core": bool, "fields": [{"name","label",
#  "required","secret": bool,"placeholder","help","path": "url|jsonData.X|
#  secureJsonData.X"}], "notes": str}
def build_datasource_payload(ds_type: str, name: str,
                             values: Dict[str, str]) -> Dict
    # folds values into {name,type,url,access:"proxy",jsonData,
    #  secureJsonData} per the template paths
```

Proxy endpoints may 404 on some setups — every helper returns [] / a
status dict with the error rather than raising.

## 5. `nr2grafana/remediate.py` — apply fixes & auto-heal

```python
def apply_fix(fix: Dict, grafana=None, dash=None, package_dir="",
              changelog=None, slug="", push=False) -> Dict
    # dispatch on fix["kind"]:
    #  edit-query: patch dash json (panel_id+refId -> new expr), rewrite
    #    package dashboard.json + datatest.json, log change, optional
    #    live push (update_dashboard)
    #  add-datasource: create_datasource(action payload), health-check it,
    #    log change
    #  config-overlay: merge into <package parent>/config-overlay.json
    #    (create if absent), log change
    #  install-plugin / credentials / pipeline / none: no-op with
    #    instructions in result
    # -> {"applied": bool, "kind", "detail", "verify": opt result}
def auto_heal(grafana, nr, dash, widget_report, requirements, slug,
              package_dir, changelog=None, max_rounds=3, log=None) -> Dict
    # loop: test_dashboard -> diagnose -> apply SAFE fixes only
    # (edit-query with high-confidence did-you-mean, config-overlay;
    #  NEVER auto-creates datasources or pushes without push=True) ->
    # re-test; stop when no new safe fixes or max_rounds.
    # -> {"rounds": [...], "fixed": n, "remaining_findings": [...]}
```

## 6. Web API additions (server.py owner; ui.py consumes)

All JSON; long ops use the existing jobs pattern.

- `POST /api/nr/test-key` -> {"ok", "user", "accounts": [...]} ; `POST
  /api/grafana/test-token` -> permissions_report + health.
- `GET /api/grafana/ds-templates` -> DS_TEMPLATES.
- `POST /api/grafana/datasource` {type,name,values} -> create via
  build_datasource_payload + immediate health -> result. `PUT .../<uid>`,
  `DELETE .../<uid>`, `POST /api/grafana/datasource/<uid>/health`.
- `POST /api/parity {slug, from?, to?}` (job) -> parity report; persisted
  as artifact "parity".
- `POST /api/diagnose {slug}` (job) -> diagnosis; persisted "diagnosis".
- `POST /api/fix {slug, finding_id, push?}` -> apply_fix result.
- `POST /api/heal {slug, push?}` (job) -> auto_heal result.
- `GET /api/metrics?uid=&q=` -> up to 200 matching metric names (editor
  autocomplete). `GET /api/labels?uid=&type=prometheus|loki`.
- Downloads (Content-Disposition attachment):
  `GET /download/dashboard/<slug>.json` (current, post-fix JSON),
  `GET /download/package/<slug>.zip` (zipfile of the package dir),
  `GET /download/all.zip`. Slugs validated against the store (no path
  traversal); zips built with stdlib zipfile into memory.
- `GET /api/readiness?slug=` -> readiness() over stored artifacts.

## 7. `web/ui.py` — full redesign (single embedded page, no CDN)

Professional product feel. Keep: vanilla JS, hash router, api() helper,
dark default + light. New IA:

- **Stepper workflow** across the top of a dashboard workspace: Connect ->
  Fetch -> Convert -> Datasources -> Validate -> Fix -> Import -> Verify ->
  Download; each step shows state (done/attention/blocked) and jumps to it.
- **Overview**: cards per dashboard with a readiness ring (score), verdict
  chips, primary action button ("2 blockers — fix now").
- **Datasources view**: table of instance datasources (name, type, uid,
  default, health badge with live re-check), "Add datasource" flyout
  driven ENTIRELY by /api/grafana/ds-templates (labeled fields, secret
  masking, inline help), create -> immediate health result inline; edit +
  delete (delete asks typed confirmation).
- **Dashboard workspace**: per panel row — confidence badge, test badge,
  parity badge (match/close/mismatch with ratio hint), expandable detail:
  original NRQL, translated expr, NR result vs Grafana result side by side
  (mini inline SVG sparklines from points + last value; tables for
  scalars), error text verbatim, diagnosis findings for this panel with
  one-click Fix buttons (shows exactly what will change before applying),
  query editor with metric-name autocomplete (/api/metrics) + Test +
  Ask AI + Save + Save&Push.
- **Diagnostics view**: all findings ordered blocker->info, each with
  evidence, exact remediation, Fix/Apply buttons, and "Auto-heal" (runs
  /api/heal, streams round results into the log panel).
- **Verify & Download**: run parity across all panels, readiness summary,
  and download buttons (single JSON, package zip, everything zip) that
  only "arm" green when readiness is ready/almost (still allowed always,
  with a warning tooltip when blocked).
- Global: job log drawer, toasts, empty states with the next action,
  keyboard-focusable, no layout jumps.

## 8. Translation-fidelity pass (owned by translate agent)

Files: `nrql/parser.py`, `translate/*.py`, their tests. Review and improve
correctness; add or verify (translating where a sound mapping exists,
clean needs-review/untranslatable notes where not): percentile() ->
histogram_quantile over _bucket; apdex(t) -> formula over histogram
buckets; filter(agg, WHERE ...); if()/cases(); rate(count(*), N unit);
derivative/predictLinear; stddev; uniqueCount -> count by group; histogram()
-> heatmap note; COMPARE WITH -> second target with timeShift note or
needs-review; SLIDE BY; TIMESERIES bucket -> $__interval mapping;
latest()/earliest(); percentage(); funnel -> untranslatable with clear
explanation. No regressions: full existing suite must stay green; add
focused tests per construct. Update `docs/translation-notes.md` (create)
with an honest support matrix.

## Cross-cutting

1. stdlib only, py3.9; secrets in memory only; every HTTP failure ->
   actionable message.
2. New artifact kinds in Store: "parity", "diagnosis" (extend
   ARTIFACT_KINDS if the store validates kinds).
3. Every new module: unit tests with stubbed HTTP; suite must stay green.
4. `tools/mock_stack.py` (owned by mock-stack agent): stdlib fake Grafana
   + fake NerdGraph HTTP server (in-memory datasources CRUD + health,
   /api/ds/query returning deterministic frames for known exprs, NerdGraph
   nrql endpoint with canned results) so the whole product can be demoed
   and e2e-tested offline: `python3 tools/mock_stack.py --port 3000
   --nr-port 3001`. Used by e2e tests (tests/test_e2e_mock.py): fetch ->
   convert --package -> check -> test -> parity -> diagnose -> fix ->
   import against the mock, asserting the full loop works.
