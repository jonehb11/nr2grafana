# nr2grafana 1.5 — cost & efficiency optimization

New capability: **evaluate LGTM-stack cost from real traffic and propose
concrete, safe ways to cut TCO** — especially Loki (label cardinality is
the #1 Loki cost driver) and Mimir/Prometheus (active-series cardinality).

The tool's unique leverage: it already knows exactly which metrics, labels,
and log-stream selectors the migrated dashboards **use**. So it can sample
what your datasources actually **ingest/store**, subtract what's **needed**,
and confidently flag the delta as waste — with estimated savings and
ready-to-apply config, never proposing to drop something a dashboard uses.

Builds on 1.1–1.4 (all contracts hold). Version 1.5.0. **Zero deps: Python
3.9+ stdlib only.** Secrets in memory only; New Relic strictly read-only.

## Domain grounding (the recommendation engine MUST encode this)

### Loki cost drivers & fixes
- **Stream = one unique combination of stream-label values.** Cost scales
  with the number of active streams (index size, chunk count) and bytes
  ingested. High-cardinality stream labels cause "stream explosion".
- **Never use as stream labels** (high-cardinality / unbounded / high-churn):
  ids (trace_id, span_id, request_id, order_id, user_id, session_id), ip,
  pod name, container id, instance, timestamp, url/path with ids, any label
  with thousands of values. Detect these by label-value cardinality and by
  name patterns.
- **Recommended stream labels**: only low-cardinality dimensions you filter
  on — typically cluster, namespace, app/service_name, level, job. Anything
  else → **structured metadata** (Loki 3.x, queryable without a stream
  label) or leave in the line and filter at query time.
- **Unused labels**: a stream label that NO migrated dashboard's stream
  selector filters on is a drop candidate (move to structured metadata).
- **Volume hotspots**: use `/loki/api/v1/index/volume` and
  `/loki/api/v1/index/volume_range` to rank streams/matchers by bytes over
  a window (this is the "traffic sampling"). Noisy debug logs, a chatty
  namespace, etc.
- **Other levers**: per-stream retention (limits_config /
  overrides), compaction + retention, tsdb shipper, chunk_target_size,
  drop debug logs at the agent, dedupe.
- **Where to apply**: at the collector/agent (Promtail/Grafana Alloy
  `relabel_configs` / `pipeline_stages` to drop labels/lines BEFORE
  ingest — cheapest), or Loki `limits_config`/retention, or OTel Collector
  (`attributes` delete, `filter/logs` drop, `transform`).

### Mimir/Prometheus cost drivers & fixes
- **Active series (cardinality) is the cost.** `GET /api/v1/status/tsdb`
  (via datasource proxy) returns seriesCountByMetricName,
  labelValueCountByLabelName, seriesCountByLabelValuePair,
  memoryInBytesByLabelName — the traffic sample. Also
  `count by (__name__)({__name__=~".+"})` and per-metric `count(metric)`.
- **Unused metrics**: metric names ingested but referenced by ZERO migrated
  dashboards (and, when available, no recording/alerting rule) → drop
  candidates.
- **High-cardinality labels**: a label with huge value count that no
  dashboard groups/filters by (e.g. id, pod, path with ids) → drop the
  label. Histograms (`_bucket`) multiply by `le` × other labels — flag
  bucket bloat; suggest native histograms or fewer buckets.
- **Other levers**: scrape interval increase for non-critical targets,
  drop unused `le`/quantile detail, recording rules to precompute
  (query-cost not ingest), per-tenant limits.
- **Where to apply**: `metric_relabel_configs` (keep/drop metrics via
  `__name__` regex; drop labels via `labeldrop`) at the scrape/agent, OTel
  Collector `filter/metrics` + `attributes` processors, or Mimir limits.

### Tempo (lighter)
- Trace/span volume and span-attribute cardinality (metrics-generator).
  Suggest sampling rate and attribute pruning; keep this best-effort.

### TCO / savings math
- User-editable **pricing assumptions** with sane defaults: Loki
  $/GB-ingested, $/GB-stored-month, retention days; Mimir $/1k-active-
  series-month (default ~$0.60/1k/mo as an editable placeholder — clearly
  labeled an assumption), $/GB-stored; Tempo $/GB. Also rough resource
  heuristics (Mimir ~ few KB RAM/active series; bytes → storage).
- Every recommendation carries **estimated savings** in native units
  (series dropped, GB/day avoided) AND dollars via the cost model, plus a
  confidence. Current vs projected monthly cost per component + total.
- NEVER claim exact bills — everything is "estimated, based on your inputs".

## 1. `nr2grafana/grafana/live.py` additions (owner: traffic agent)

```python
def prom_tsdb_status(self, uid: str) -> Dict     # /api/v1/status/tsdb (proxy)
def prom_series_count(self, uid: str, match: str) -> int   # count(match)
def prom_top_metrics(self, uid: str, n: int = 50) -> List[Dict]
    # [{"metric","series"}] from tsdb status seriesCountByMetricName
def prom_label_cardinality(self, uid: str) -> List[Dict]
    # [{"label","values"}] from labelValueCountByLabelName
def loki_volume(self, uid: str, matcher: str = '{}',
                frm: str = "now-24h", to: str = "now") -> List[Dict]
    # [{"stream"/"matcher","bytes"}] via /loki/api/v1/index/volume
def loki_stream_cardinality(self, uid: str,
                            labels: Optional[List[str]] = None) -> List[Dict]
    # [{"label","values"}] via label values counts
```
All proxy calls degrade to [] / {} with an optional errors out-param (match
the existing prom_*/loki_* helper conventions). Extend tests/test_grafana_live.py.

## 2. `nr2grafana/traffic.py` — sample real datasource traffic (owner: traffic agent)

```python
def sample_traffic(grafana, ds_list, frm="now-24h", to="now",
                   log=None) -> Dict
```
ds_list: [{"family","uid","type"}]. Returns schema
"nr2grafana/traffic/v1":
```jsonc
{ "schema":"nr2grafana/traffic/v1", "generated_at":"...",
  "range":{"from":"now-24h","to":"now"},
  "datasources":[{
    "family":"loki|prometheus|tempo", "uid":"...", "health":{...},
    "prometheus": {"active_series": int, "top_metrics":[{"metric","series"}],
       "label_cardinality":[{"label","values"}], "histogram_series": int},
    "loki": {"streams": int, "bytes_window": int, "bytes_per_day": float,
       "top_streams":[{"labels":{...},"bytes"}],
       "label_cardinality":[{"label","values"}]},
    "tempo": {"note": "...best-effort..."},
    "errors":[...] }] }
```
Never raise per datasource. Owns tests/test_traffic.py (stub grafana).

## 3. `nr2grafana/usage.py` — what the dashboards actually need (owner: optimize agent)

```python
def collect_usage(dashboards, widget_reports) -> Dict
    # scan converted Grafana dashboards + reports for referenced:
    # metrics (PromQL __name__ heads), prom labels used in matchers/by(),
    # loki stream-selector labels + label values filtered on, per family.
    # -> {"prometheus":{"metrics":set-as-list,"labels":[...]},
    #     "loki":{"stream_labels":[...],"filtered_values":{...}},
    #     "tempo":{...}}  (reuse requirements.data_expectations logic;
    # import helpers from requirements.py if public, else reimplement small)
```
Owns tests/test_usage.py.

## 4. `nr2grafana/costmodel.py` — pricing + TCO (owner: costmodel agent)

```python
DEFAULT_PRICING: Dict   # documented, clearly-labeled assumptions
def estimate_costs(traffic, pricing=None) -> Dict
    # per datasource + total monthly $; schema "nr2grafana/cost/v1"
    # {"pricing":{...effective...}, "components":[{"family","uid",
    #   "drivers":{"active_series"|"bytes_per_day"|...},
    #   "monthly_cost": float, "breakdown":{...}}], "monthly_total": float,
    #   "resources":{"mimir_ram_gb_est":..,"storage_gb_month_est":..}}
def apply_savings(cost, recommendations) -> Dict
    # projected cost after recs: {"projected_total", "saved_total",
    #   "saved_pct", "per_component":[...]}
```
Pure math, no network. Owns tests/test_costmodel.py.

## 5. `nr2grafana/optimize.py` — the recommendation engine (owner: optimize agent)

```python
def recommend(traffic, usage, cost=None, pricing=None, cfg=None,
              log=None) -> Dict
```
Cross-reference traffic vs usage; emit schema "nr2grafana/optimize/v1":
```jsonc
{ "schema":"nr2grafana/optimize/v1", "generated_at":"...",
  "recommendations":[{
    "id":"loki-drop-label-pod",
    "family":"loki|prometheus|tempo",
    "kind":"drop-label|drop-metric|to-structured-metadata|
            recommend-stream-labels|retention|scrape-interval|
            histogram|sampling",
    "severity":"high|medium|low",
    "title":"Drop stream label `pod` (4213 values, unused by dashboards)",
    "rationale":"... plain-language why, with the domain reason ...",
    "evidence":{"cardinality":4213,"used_by_dashboards":0,"bytes_share":..},
    "keeps_intact": true,           // proven safe: not in usage set
    "est_savings":{"streams":..,"series":..,"gb_per_day":..,
                   "monthly_usd":..,"confidence":"high|med|low"},
    "config":[{"target":"promtail|alloy|otel-collector|loki-limits|
                         prometheus-relabel|mimir-limits",
               "language":"yaml","snippet":"...ready to paste...",
               "note":"apply at the agent to save before ingest"}]
  }],
  "summary":{"by_family":{...},"total_est_monthly_usd":..,
             "safe_count":..,"needs_review_count":..} }
```
Rules to implement (see Domain grounding):
- **Loki**: recommended stream-label set = usage.loki.stream_labels ∩
  low-cardinality; for each current stream label NOT in usage → drop-label
  or to-structured-metadata (id-like names always structured-metadata);
  high-cardinality labels flagged even if used (suggest structured
  metadata); volume hotspots (top_streams bytes) → retention/drop-line;
  emit Promtail/Alloy relabel + Loki limits snippets + structured_metadata.
- **Prometheus**: metrics in top_metrics NOT in usage.prometheus.metrics →
  drop-metric (metric_relabel_configs keep-list from the usage set is the
  safest form — emit both an explicit drop of the biggest offenders AND a
  keep-list option); high-cardinality labels not in usage.labels →
  labeldrop; histogram bucket bloat → native-histogram/bucket-reduction
  note; emit prometheus relabel + OTel filter/attributes snippets.
- Mark `keeps_intact=true` and `severity` accordingly; anything that
  touches a used dimension is `needs-review`, never auto-safe.
Owns tests/test_optimize.py (stub traffic+usage covering every kind, and
the safety guarantee: never recommends dropping a used metric/label).

## 6. Web + CLI + Store (owner: server/cli agent)

server.py routes (existing job/Session conventions):
- `POST /api/traffic {ds_uids?, from?, to?}` -> job; persists "traffic".
- `POST /api/cost {slug?, pricing?}` -> traffic (cached or fresh) + usage
  (from stored dashboards) -> costmodel + optimize; persists "cost" and
  "optimize"; returns both. `slug` optional (whole-instance if omitted;
  usage then spans all stored dashboards).
- `GET /api/pricing` / `POST /api/pricing` (persist non-secret pricing
  assumptions in Store settings `web.pricing`).
- `GET /download/cost-config.zip?slug=` -> all recommendation config
  snippets as files (promtail.yaml, prometheus-relabel.yaml,
  otel-collector.yaml, loki-limits.yaml, README).
- Store ARTIFACT_KINDS += "traffic","cost","optimize". /api/state
  features += "cost".
cli.py: `cost analyze [<package|dir>...] --url --token [--from --to]
[--pricing FILE] [-o OUT]` — samples traffic, computes usage from the
inputs (or all converted output), prints a ranked table (recommendation,
family, est monthly savings, safe?) + current/projected/total, writes
cost-report.json + a config/ dir of snippets; `cost pricing` prints/edits
assumptions. Wizard: add "Analyze cost & efficiency". Extend test_cli.py.
Owns: server.py, cli.py, interactive.py, store.py, tests/test_web.py,
tests/test_cli.py.

## 7. Web UI — Cost view (owner: ui agent, ui.py ONLY)

New nav item **Cost**. Sections:
- **Sample traffic** button (job) → per-datasource cards: active series /
  streams / bytes-per-day with the inline-SVG charts (top metrics bar,
  top streams bar, label-cardinality bar).
- **Cost breakdown**: current estimated monthly $ per component (Loki /
  Mimir / Tempo) as a bar/donut, editable **pricing assumptions** panel
  (clearly labeled assumptions, defaults, live re-compute), and a
  **current → projected** savings summary with a big "estimated N% / $X
  saved" figure.
- **Recommendations**: ranked list, each a card — title, plain rationale,
  evidence numbers, a green "safe — nothing your dashboards use" chip (or
  amber "review"), estimated savings, and the ready-to-paste config in a
  copyable code block with a target selector (Promtail / Alloy / OTel /
  Loki / Prometheus). A **Download all config** button.
- Friendliness: tooltips explaining cardinality/streams/active-series in
  plain terms; empty states; loading skeletons. esc() discipline; keep
  test hooks; single module string; no external assets; < 430KB.

## 8. Mock stack + e2e (owner: mock agent)

Extend tools/mock_stack.py: fake Grafana proxy serves `/api/v1/status/tsdb`
(seriesCountByMetricName incl. a couple of obviously-unused high-series
metrics + a high-cardinality label like `pod`/`path`), Prom `count(...)`,
and Loki `/loki/api/v1/index/volume(_range)` with a chatty stream and an
id-like high-cardinality label. Extend tests/test_e2e_mock.py: drive
sample_traffic -> collect_usage (over the fixture) -> costmodel ->
optimize end to end; assert it recommends dropping an unused metric and an
unused/high-cardinality Loki label, marks them keeps_intact, and that
apply_savings shows a positive saved_pct.

## Cross-cutting
1. stdlib only, py3.9, ASCII/LF/4-space/79-col, docstrings.
2. Recommendations MUST be safe: never drop anything in the usage set;
   used-but-costly dimensions become needs-review, not auto-drop.
3. Config snippets must be valid, commented, and paste-ready.
4. Every module tested; full suite stays green
   (`N2G_DB=$(mktemp) python3 -m unittest discover -s tests`).
5. Coordinator bumps version to 1.5.0 and writes docs/cost-optimization.md
   + README section after build.
