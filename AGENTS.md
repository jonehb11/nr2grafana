# nr2grafana — guide for AI coding agents (Claude Code, Cursor, Kiro, Codex, …)

nr2grafana migrates New Relic dashboards to Grafana (LGTM: Mimir/Prometheus,
Loki, Tempo). It is a local Python 3.9+ CLI with **zero dependencies**. New
Relic is only ever *read*. This file tells an agent how to drive the tool and
how to reason about a New Relic dashboard while doing so.

## The workflow (four commands, in order)

```bash
python3 -m nr2grafana import  -o ./nr [--name TEXT | --guid GUID] # NR -> NR JSON files (+ import-manifest.json)
python3 -m nr2grafana import  -o ./nr exported.json               # ...or validate/normalise local "Copy JSON" exports
python3 -m nr2grafana convert ./nr -o ./grafana [-c mappings.json] # NR JSON -> Grafana JSON + migration-report.json
python3 -m nr2grafana validate ./grafana [--grafana-url URL --test] # static checks; live: datasource types + per-panel data test
python3 -m nr2grafana export  ./grafana --grafana-url URL --folder "Migrated" [--test] # create + verify on Grafana
```

Two commands exist purely so an agent can *understand* a dashboard before
and after converting it:

```bash
python3 -m nr2grafana inspect ./nr/checkout.json   # every widget + NRQL clause + translation plan + what cannot migrate
python3 -m nr2grafana explain "SELECT count(*) FROM Transaction WHERE appName = 'x' TIMESERIES"
```

**Always put `--json` before the command** when you (the agent) need the
result: stdout is then exactly one JSON object `{"ok", "command", "result",
"error"?}` and every human/log line goes to stderr.

Exit codes: `0` ok · `1` problems found (validation errors, failed inputs,
failed exports) · `2` bad input/usage · `3` cannot reach or authenticate with
New Relic / Grafana. Credentials come from `NEW_RELIC_API_KEY` (a USER key),
`GRAFANA_URL`, `GRAFANA_TOKEN` (service-account token, Editor role; Admin to
create folders). Never print or commit them.

`python3 -m nr2grafana mcp serve` exposes the same operations as MCP tools
(`inspect`, `explain`, `import`, `convert_files`, `validate_dashboards`,
`export`) for clients that prefer tools over a shell.

## What the outputs mean

- `migration-report.json` (from `convert`): per dashboard — `source_dashboard`
  (NR name/guid/account), `output`, `uid`, `datasources` (types the dashboard
  binds to), `cannot_migrate` (page, widget, viz, NRQL, **reason**, closest
  Grafana `equivalent`), `needs_review` (panel + the assumptions to verify),
  `validation`, and `widgets` (every panel: original NRQL, emitted queries,
  confidence, notes).
- Confidence per panel: `exact` · `approximate` (documented bounded drift) ·
  `needs-review` (correct only under a stated assumption — the note says
  which) · `untranslatable` (placeholder panel titled `... [MANUAL]`, or an
  NRQL passthrough panel with `--passthrough`).
- Panels needing review carry ` [REVIEW]` in the title and the full
  explanation in the panel description. Remove the suffix only after the
  query verifiably returns data.
- `validate --grafana-url` prints, per dashboard, each datasource *type* the
  dashboard needs, whether the instance has one, and which uid it binds to;
  a missing type is an error with the exact fix (add a datasource of that
  type, or install the plugin for the New Relic passthrough). `--test` runs
  every panel query (`data` / `no-data` / `error`); a `no-data` panel is
  usually a metric or label name to fix in the config, an `error` is a query
  Grafana rejected.
- `export` prints `created: 'Title' URL (uid, folder)` and `sourced from New
  Relic dashboard 'Name' (guid …)`, then `verified: yes (N panels)` after
  reading the dashboard back. It refuses dashboards with validation errors or
  missing datasource types (override with `--allow-missing`).

## How to fix a flagged panel (and never fix it twice)

1. Read the panel's notes (`inspect`, or `migration-report.json`). Each note
   names the assumption: a label name, a metric name/type, a Loki stream label,
   a span-metrics or HTTP-metrics naming flavor.
2. Verify the assumption against the live stack through Grafana's datasource
   proxy (`/api/datasources/proxy/uid/<uid>/api/v1/label/__name__/values`,
   `/api/v1/labels`, Loki `/loki/api/v1/labels`) or with
   `validate --grafana-url … --test` (error / no-data per panel).
3. Put the fix in the **mapping config**, not in the dashboard JSON, then
   re-run `convert`:
   - wrong label name → `label_map` (`"appName": "service_name"`)
   - wrong metric name/type/unit → `metric_map` (`"orders.completed": {"name": "orders_total", "type": "counter"}`; full PromQL templates via `"expr"`)
   - legacy timeslice metric → `metric_map` keyed by its `metricTimesliceName`
   - Loki index labels / parser → `loki_stream_labels`, `loki_parser`, `loki_metadata_labels`
   - span metrics naming → `spanmetrics_flavor` / `span_metrics`; HTTP semconv generation → `http_metrics_flavor`
   - counters without `_total` → `metric_total_suffix: false`
   - custom NR event types → `event_map` (route to Loki with stream labels, or to a metric)
   - aggregated `FROM Span` via TraceQL metrics instead of span metrics → `span_aggregations: "traceql"`
   `python3 -m nr2grafana example-config` prints every key with defaults.

## New Relic semantics the agent must respect

- `TIMESERIES` → range query (`$__rate_interval` / `$__auto`); no `TIMESERIES`
  → **instant** query over `$__range` (NR aggregates the whole SINCE window).
  Counts per bucket are `sum(rate(m[$__rate_interval])) * $__interval_ms / 1000`
  (events per Grafana step); instant counts are `increase(m[$__range])`.
  `TIMESERIES 5 minutes` sets the panel's min interval. A line/area/bar
  widget whose NRQL lacks `TIMESERIES` is still translated as a range query
  (noted as approximate).
- `SINCE x` → panel/dashboard time range; `SINCE a UNTIL b` → panel
  `timeFrom`/`timeShift` (`SINCE yesterday UNTIL today` → `now/d` +
  `1d/d`, the whole previous day); `COMPARE WITH` → a second target with
  `offset`. `rate(count(*), 1 minute)` → `rate(m[W]) * 60` with the panel
  unit `reqpm` (HTTP / span metrics) or `cpm`.
- `FACET a` → `by (a)` + legend; `FACET … LIMIT n` → `topk(n, …)`;
  `FACET cases(...)`/`if(...)` → one filtered target per case. Several
  aggregations in one SELECT become one target each, legends `{{label}}
  <alias or expression>`.
- `FROM Transaction` → OTel HTTP server histogram (`duration`), DB client
  histogram (`databaseDuration`), HTTP client histogram (`externalDuration`);
  `error IS TRUE` ≈ 5xx; `transactionType = 'Web'` is implicit; other
  attributes have no metric and are reported, never guessed.
- `FROM Metric` names: New Relic's own (`apm.service.*`,
  `newrelic.goldenmetrics.*`, `host.*`, `k8s.*`), OTel semconv, and `aws.*`
  (YACE convention) are known; anything else is name-normalised and
  type-guessed → needs-review.
- `FROM Log` → LogQL: stream labels from `loki_stream_labels`, `message`
  predicates become line filters, everything else a parsed-field filter.
- `FROM Span` raw → TraceQL search; aggregated → span metrics (or TraceQL
  metrics); `uniqueCount(trace.id)` → root-span count.
- Infra samples (`SystemSample`, `K8s*Sample`, `ProcessSample`,
  `ContainerSample`, …) → node_exporter / kube-state-metrics / cAdvisor /
  process-exporter / kubelet metrics.
- Legacy AWS polling samples (`ComputeSample`, `DatastoreSample`,
  `QueueSample`, `LoadBalancerSample`, …) need `WHERE provider = '<type>'`;
  `provider.<Metric>.<Stat>` → YACE `aws_<ns>_<metric>_<stat>` (needs-review:
  verify the name, pin it with `metric_map` keyed `"<Event>.<attribute>"`).
- `{{var}}` anywhere in a literal stays a variable (`'%{{host}}%'` →
  `=~".*${host:regex}.*"`); `FACET {{attr}}` → `by ($attr)`; `LIMIT {{n}}` →
  `topk($n, …)`; `percentile(x, {{p}})` → `histogram_quantile($p / 100, …)`;
  `SINCE {{since}}` / `TIMESERIES {{interval}}` → panel `timeFrom` /
  `interval` set to the variable (its value must be a Grafana span). NRQL
  dashboard variables become `label_values(<metric>{<WHERE>}, label)` on the
  datasource family their FROM maps to (Prometheus / Loki / Tempo).
- Math around an aggregation (`round`, `abs`, `floor`, `ceil`, `sqrt`, `exp`,
  `log`, `clamp_max`, `pow`, …) becomes the PromQL function; `uniques(attr)`
  becomes a `group by (label)` table; `predictLinear`, `bucketPercentile`,
  `getCdfValue` map; `rate()` of anything but `count()`/`sum()` and
  `derivative()` of a per-second attribute are refused with the reason.
- Widget settings that Grafana can express are carried over: thresholds,
  units, legend, series colours, right axis, null handling (`preserve`),
  entity links, refresh interval, "ignore time picker" (a panel time
  override). A pie/table/bar with `TIMESERIES` runs as an instant query.
- On-host integration samples (`NginxSample`, `MysqlSample`,
  `PostgresqlDatabaseSample`, `RedisSample`, `KafkaOffsetSample`,
  `ElasticsearchClusterSample`, `RabbitmqQueueSample`, …) map their common
  attributes to the matching Prometheus exporter; anything else names the
  exporter to rebuild on.
- Nested queries `SELECT avg(c) FROM (SELECT count(*) AS c FROM X FACET a)`
  fold the inner per-facet vector (`avg(sum by (a)(...))`, `count((...) >
  n)`, `quantile(0.95, ...)`); the outer FACET must name inner facets.
- Never translatable: `funnel()`, service maps, custom nerdpack
  visualizations, browser/mobile RUM (Faro is the LGTM equivalent), synthetics
  (blackbox_exporter), `Nr*` account data, subqueries in WHERE/IN. The tool
  says so per widget; do not invent look-alikes.

## Working on the code

- Core: `nr2grafana/nrql/parser.py` (NRQL AST), `nr2grafana/translate/`
  (`common.py` matchers/DNF, `metrics.py` PromQL, `logs.py` LogQL,
  `traces.py` TraceQL, `nrmetrics.py` built-in NR/OTel/K8s knowledge,
  `router.py`), `nr2grafana/grafana/builder.py` (panels), `validate.py`,
  `live.py` (Grafana API), `nr2grafana/pipeline.py` (the four operations),
  `nr2grafana/inspect.py`, `nr2grafana/cli.py`.
- Tests: `python3 -m unittest discover -s tests` (stdlib only; the
  `test_awscost` cases need the AWS CLI and are expected to fail without it).
- Every translation change needs a test asserting the exact emitted query and
  the confidence, and a note in `docs/translation-notes.md`.
- Keep New Relic read-only: the NerdGraph client refuses mutations by design.
