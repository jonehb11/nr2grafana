# nr2grafana 1.4 — module contracts

Theme: **make it genuinely usable**. Three headline capabilities:
1. **Side-by-side dashboard comparison** — the whole New Relic dashboard
   rendered next to the whole Grafana dashboard, every panel showing REAL
   DATA as actual charts (not just sparklines), aligned panel-by-panel,
   with per-panel agreement highlighting (match / close / mismatch /
   no-data). This is the flagship view.
2. **Add datasources in real time and watch data flow** — after creating a
   datasource, immediately probe whether real data is coming through it and
   which previously-blocked panels now light up, with visual before/after
   feedback — no page reload, no leaving the app.
3. **Extreme friendliness** — plain language, guided steps, tooltips,
   great empty states, obvious primary action at every step.

Builds on 1.1–1.3 (all prior contracts hold). Version 1.4.0. **Zero runtime
deps: Python 3.9+ stdlib only.** Style matches the codebase. Secrets stay in
memory; New Relic stays strictly read-only (nerdgraph refuses mutations).

## 1. `nr2grafana/compare.py` — side-by-side render model (NEW)

Produces everything the UI needs to draw both dashboards with real data.
Reuses parity/samples building blocks (import them; do NOT duplicate the
whole thing, but a couple of tiny helpers may be copied if it avoids a
circular import with web tests — document if so).

```python
def build_comparison(nr, account_ids, grafana, nr_dashboard_raw, dash,
                     widget_report, ds_map=None, frm="now-1h", to="now",
                     limit_points=100, log=None) -> Dict[str, Any]
```
Returns schema "nr2grafana/comparison/v1":
```jsonc
{
  "schema": "nr2grafana/comparison/v1",
  "dashboard": "...", "uid": "...", "generated_at": "...",
  "range": {"from": "now-1h", "to": "now"},
  "panels": [{
    "panel_id": 1, "title": "Throughput", "row": "Golden Signals",
    "grid": {"x":0,"y":0,"w":12,"h":8},          // Grafana gridPos
    "viz": "timeseries|stat|bar|table|logs|gauge|piechart|text|unsupported",
    "nr":  {"kind":"series|scalar|table|logs|error|empty",
            "series":[{"name":str,"points":[[epoch_s,float],...]}],
            "scalar": Optional[float], "rows": Optional[[...]],
            "lines": Optional[[{"ts","line"}]], "unit": str, "error": str},
    "grafana": { ...same shape... },
    "verdict": "match|close|value-mismatch|shape-mismatch|nr-empty|"
               "gf-empty|both-empty|nr-error|gf-error",
    "detail": str, "ratio": Optional[float],
    "nrql": str, "expr": str, "datasource": "prometheus|loki|tempo|..."
  }],
  "summary": {verdict: count}, "score": 0-100,
  "layout": {"nr_pages": [...page/widget layout for NR side...]}
}
```
- NR side: for aggregate panels run the original NRQL (via nerdgraph;
  append TIMESERIES when the panel is a graph and the NRQL lacks it so the
  NR side returns a real series to chart) and normalize to `series`; billboard/
  scalar -> `scalar`; table -> `rows`; log panels -> `lines` (derive raw
  `SELECT * ... LIMIT` like samples.py). Downsample to <= limit_points.
- Grafana side: via GrafanaLive.ds_query with resolved ds_map; timeseries
  -> series (<= limit_points, <= 8 series), stat -> scalar, table -> rows,
  loki -> lines. Never raise per panel; per-side failures -> *-error verdict
  with the message.
- `viz` mirrors the Grafana panel type so the UI picks the same chart on
  both sides. Preserve `grid` and `row` so the UI can lay the Grafana side
  out exactly like Grafana and group the NR side to match.
- Verdict/score/ratio: reuse parity.compare semantics.
Also: `def datasource_flow(grafana, requirements, dash, widget_report,
ds_uid=None, ds_map=None, log=None) -> Dict` returning, per datasource
family required by the dashboard: `{"family","uid","health":{...},
"panels_total","panels_with_data","panels_no_data","panels_error",
"sample_series": [...one real series to prove flow...], "newly_flowing":
[panel_ids]}` — used by the "add datasource and watch it flow" loop
(caller diffs panels_with_data before/after).

Tests: tests/test_compare.py — stub nr+grafana, cover every viz/verdict,
NR TIMESERIES augmentation, downsampling caps, datasource_flow counts,
per-side errors.

## 2. `web/server.py` — routes (owner: server agent)

Follow existing job/route/Session conventions exactly.
- `POST /api/compare {slug, from?, to?}` -> JOB; persists artifact
  "comparison"; returns build_comparison output. Uses the stored NR raw
  dashboard json (add it to Store on convert if not already — coordinate:
  store the NR source under artifact "nr-source" during convert; if absent,
  compare still works with dash-only, NR side best-effort).
- `POST /api/datasource/<uid>/verify-flow {slug}` -> compare.datasource_flow
  for that uid against the dashboard (fast; not a full job).
- `POST /api/datasource` (create) ALREADY exists (1.2). Extend its response
  to include an immediate `flow` block (datasource_flow for the new uid vs
  the currently-open dashboard slug when the request carries `slug`), so the
  UI can show data flowing the instant it's created.
- `GET /api/panel-data {slug, panel_id, side}` -> re-fetch one panel's
  render data for one side (for per-panel refresh without a full compare).
- Extend Store ARTIFACT_KINDS with "comparison", "nr-source".
- `/api/state` features += "compare".
Tests: extend test_web.py — compare job + artifact, verify-flow counts,
create-with-flow, panel-data, bad slug 404.

## 3. `web/ui.py` — the usable UI (owner: ui agent; OWNS THIS FILE ALONE)

Keep every existing feature/route working. Additions:

### 3a. Inline SVG chart renderer (no libraries, no CDN)
A small charting module inside the page: `chart(kind, data, opts)` drawing
into inline SVG, theme-aware (uses CSS vars), responsive (viewBox +
100% width). Kinds: `timeseries` (multi-series lines, subtle grid, time
axis ticks, value axis, hover tooltip with nearest point, legend),
`bar`, `stat` (big number + unit + optional sparkline + threshold color),
`gauge`, `table` (the rows), `logs` (mono line list with timestamps),
`piechart`. Must handle empty/error states as a clean in-panel placeholder
("no data" / the error, never blank). Downsample defensively. Numbers
formatted human-friendly (SI units, ms/s, %, tabular).

### 3b. Side-by-side comparison view (flagship)
New nav item "Compare". For the selected dashboard: a two-column canvas —
left "New Relic", right "Grafana" — laid out panel-by-panel in the
Grafana grid order, each panel rendered with 3a from the /api/compare
data, same chart kind on both sides. Between/over each panel pair: an
agreement badge (match=green / close=teal with ratio / mismatch=amber /
no-data=gray / error=red) and a one-line "why". A top bar: overall score
ring, verdict tally, a time-range picker (now-15m/1h/6h/24h + custom) that
re-runs compare, and a "sync hover" so hovering a point on one side marks
the aligned time on the other. A filter: "show only disagreements".
Clicking a panel opens the existing panel detail (editor, diagnose, fix).
Must stay smooth with 20+ panels (render lazily / on scroll is fine).

### 3c. Datasource "add and watch it flow" loop
In the Datasources view and anywhere a diagnosis says "add datasource":
the guided add form, on submit, creates the datasource AND immediately
shows a live flow result from the create response `flow` block — a
before/after: "0 panels had data → 7 now flowing", a real sample chart
from the new datasource proving data, and health status; if still no data,
the structured error card with the exact next action. A "Re-check flow"
button re-runs /api/datasource/<uid>/verify-flow. Health/flow badges on
every datasource row, re-checkable in place.

### 3d. Extreme friendliness pass (whole app)
- A first-run **welcome / guided path**: a dismissible checklist that walks
  Connect → Fetch → Convert → Datasources → Compare → Fix → Download, each
  item deep-links its step and shows done/next state.
- Plain-language everywhere: replace jargon in labels/empty states with
  human phrasing (keep technical detail available on hover/expand). Every
  empty state says what to do next with a button. Every primary action per
  view is visually obvious (one accent button). Tooltips on badges/verdicts
  explaining what they mean. Loading skeletons instead of blank panels.
  Keyboard: `?` opens a shortcuts/help sheet; `g` then a letter jumps views.
- Keep esc() on all server strings; keep test hooks/ids test_web.py asserts;
  `node --check` clean; single module string, no external assets; < 380KB.

## 4. `tools/mock_stack.py` + e2e (owner: mock agent)

Extend the mock so the comparison view has believable data to draw:
- Fake Grafana /api/ds/query returns realistic multi-point timeseries
  (>= 30 points, gentle noise, deterministic) for known metrics; distinct
  shapes per metric so charts differ; a couple of metrics intentionally
  disagree with the NR side (for the mismatch demo) and a couple match.
- Fake NerdGraph nrql returns TIMESERIES arrays (matching begin/end
  buckets) consistent (match) or scaled (mismatch) vs Grafana per metric,
  plus scalar and log cases.
- Add a datasource-flow scenario: before a Loki datasource exists, log
  panels are gf-empty; after creating it (mock now has it), they flow.
- Extend tests/test_e2e_mock.py: drive /api build_comparison over the
  fixture end to end (both sides return chartable series, verdicts include
  at least one match and one mismatch), and the datasource_flow
  before/after (create loki -> newly_flowing non-empty).

## Cross-cutting
1. stdlib only, py3.9, ASCII/LF/4-space/79-col, docstrings.
2. Coordinator bumps version to 1.4.0 and reconciles README + docs
   (docs/compare-view.md new) after build.
3. Every new module/route gets tests; full suite stays green
   (`N2G_DB=$(mktemp) python3 -m unittest discover -s tests`).
4. Convert must persist the NR source dashboard json as Store artifact
   "nr-source" per slug so Compare has the NR side offline (owner: whoever
   touches the convert path — assign to server agent; keep it additive).
