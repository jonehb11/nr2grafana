# nr2grafana — New Relic → Grafana dashboard migrator

Converts New Relic dashboards into Grafana dashboards for an LGTM stack
(Mimir/Prometheus, Loki, Tempo, fed by OpenTelemetry) — and proves the
result: it validates every dashboard, checks the datasources your Grafana
actually has against the ones each dashboard needs, creates the dashboards,
reads them back to verify, tells you the name of every Grafana dashboard it
created and the New Relic dashboard it came from, and names exactly which
New Relic widgets cannot exist in Grafana and why.

- **Four commands.** `import` → `convert` → `validate` → `export`. Two more
  exist to *understand* a dashboard: `inspect` and `explain`.
- **Runs on your workstation.** A local CLI; needs network reachability to
  New Relic (import) and Grafana (validate/export) only.
- **New Relic is never modified.** The NerdGraph client refuses to send any
  GraphQL mutation. All changes happen on the Grafana side, only when you
  run `export`.
- **Zero dependencies.** Python 3.9+ standard library only.
- **Honest output.** Every panel carries the original NRQL and a confidence
  tag (`exact` / `approximate` / `needs-review` / `untranslatable`) with the
  exact assumption to verify. Nothing is guessed silently.
- **Agent-native.** `--json` before any command returns one JSON object;
  `AGENTS.md` / `CLAUDE.md` / Cursor rules / Kiro steering / a Claude Code
  skill tell a local AI agent how to run the migration and fix flagged
  panels; `mcp serve` exposes the same operations as MCP tools.

## Install

```bash
pipx install /path/to/nr2grafana     # puts n2g / nr2grafana on PATH
# or run from the checkout:
python3 -m nr2grafana ...
```

## The workflow

```bash
export NEW_RELIC_API_KEY=NRAK-...        # a USER key (read-only use)
export GRAFANA_URL=https://grafana.example.com
export GRAFANA_TOKEN=glsa_...            # service-account token, Editor role

# 1. import: New Relic -> NR dashboard JSON on disk (+ import-manifest.json)
n2g import -o ./nr --name "checkout"     # or --guid <GUID>, or nothing for all
n2g import -o ./nr exported.json         # or validate local "Copy JSON" exports

# 2. convert: NR JSON -> Grafana JSON + migration-report.json
n2g convert ./nr -o ./grafana [-c mappings.json]

# 3. validate: static checks; with a Grafana URL also the datasources it
#    has vs. needs, and with --test every panel query
n2g validate ./grafana --grafana-url $GRAFANA_URL --test

# 4. export: create the dashboards, verify each, name the source
n2g export ./grafana --grafana-url $GRAFANA_URL --folder "Migrated from New Relic" --test
```

`export` prints, per dashboard:

```
created: 'Checkout Service Overview'  https://grafana/d/nr-checkout-service-overview/...  (uid nr-checkout-service-overview, folder Migrated from New Relic)
    sourced from New Relic dashboard 'Checkout Service Overview' (guid MjUy...)
    verified: yes (18 panels, version 1)
    data test: 17 data, 1 no-data
```

`convert` ends with the list that matters most:

```
Widgets that cannot be migrated to Grafana (2):
  - Checkout Service Overview / Golden Signals / Checkout funnel (viz.funnel)
      NRQL: SELECT funnel(session, WHERE pageUrl LIKE '%/cart%' AS 'Cart', ...) FROM PageView
      why: funnel charts are per-user step conversion over event sequences
      closest Grafana equivalent: no Grafana panel; keep the widget in New Relic or rebuild from Faro
      in the output: text placeholder panel titled '... [MANUAL]'
```

Exit codes: `0` ok · `1` problems found · `2` bad input/usage · `3` cannot
reach or authenticate with New Relic / Grafana. `--json` before the command
(`n2g --json convert ...`) puts one JSON object on stdout and all human text
on stderr.

## Understanding a dashboard first

```bash
n2g inspect ./nr/checkout.json
```

prints every page and widget, each NRQL parsed into its clauses, the
attributes and variables it uses, the translation plan per query (target
datasource, the PromQL/LogQL/TraceQL that will be emitted, confidence and
every assumption), the Grafana datasource types the dashboard will need,
and the widgets that cannot migrate with the reason and the closest
Grafana equivalent. `--json` gives the same as a structured model.

```bash
n2g explain "SELECT percentage(count(*), WHERE error IS TRUE) FROM Transaction WHERE appName = 'checkout' TIMESERIES"
```

does it for one query.

## What `validate` and `export` check

- **Static** (always): Grafana schema requirements, unique panel ids and
  refIds, grid bounds, balanced expressions, query-language sanity (leaked
  NRQL, leftover placeholders, empty Loki selectors, unwrap-less range
  aggregations, TraceQL shape), every `$variable` defined, `timeFrom`/
  `timeShift` format, row structure.
- **Datasources** (`--grafana-url`): for each datasource *type* the
  dashboard binds to (prometheus, loki, tempo, the New Relic passthrough
  plugin) whether the instance has one, which uid it will bind to (the
  default of that type, or `--datasource prometheus=<uid|name>`), and — when
  missing — the exact fix (add a datasource of that type, or install the
  plugin) and how many panels need it.
- **Data** (`--test`): every panel query through Grafana's `/api/ds/query`
  (TraceQL searches through Tempo's own search API, which Grafana runs in
  the browser rather than on its backend), classified `data` / `no-data`
  (the metric or labels do not exist in your stack yet) / `error` (with
  Grafana's error text).
- **After creation** (`export`): the dashboard is read back by uid and its
  title and panel count compared with what was sent; datasource variables
  are pinned to the chosen instances so the dashboard works without a
  manual pick; results go to `*.export-results.json`.

`export` refuses a dashboard with validation errors or a missing datasource
type (override with `--allow-missing`), and asks for `--overwrite` when a
dashboard with the same uid/title already exists.

## How the conversion works

**Widgets → panels**

| New Relic | Grafana |
|---|---|
| viz.line / viz.area / viz.scatter | timeseries (area = fill, scatter = points) |
| viz.stacked-bar | timeseries, bars + stacking |
| viz.billboard / viz.billboard-comparison / viz.sparkline / viz.traffic-light | stat (+ thresholds, percent change, sparkline, background colour) |
| viz.bullet | gauge (limit → max) |
| viz.bar | bargauge |
| viz.pie | piechart |
| viz.table / viz.json / viz.event-feed | table (+ sorting) |
| viz.markdown | text |
| viz.heatmap / viz.histogram | heatmap / histogram |
| logger.log-table-widget, `SELECT * FROM Log` | logs panel (Loki) |
| `SELECT * FROM Span` | table with TraceQL search (Tempo) |
| viz.funnel, service maps, inventory, geo map, custom nerdpack visualizations | **cannot migrate**: placeholder panel + reason + closest equivalent (or NRQL passthrough with `--passthrough`) |

**Queries** are routed by `FROM`:

- `Transaction` / `TransactionError` → OTel HTTP server histogram
  (`duration`, `totalTime`), DB client histogram (`databaseDuration`), HTTP
  client histogram (`externalDuration`); `error IS TRUE` ≈ 5xx;
  `transactionType = 'Web'` is implicit. Other attributes are reported, not
  guessed.
- `Metric` → **PromQL** (Mimir). New Relic's own names are known:
  `apm.service.*`, `newrelic.goldenmetrics.*`, `host.*`, `k8s.*`, OTel
  semantic-convention names, Micrometer / Spring Boot names,
  `aws.<namespace>.<Metric>` (YACE naming).
  Anything else is normalised (dots → underscores) and type-guessed
  (counter → `rate`/`increase`, histogram → `histogram_quantile`, gauge →
  `avg_over_time`) and flagged `needs-review`; `metric_map` makes it exact.
- `SystemSample`, `ProcessSample`, `NetworkSample`, `StorageSample`,
  `ContainerSample`, every `K8s*Sample` → node_exporter, process-exporter,
  cAdvisor, kube-state-metrics and kubelet metrics (~200 attributes).
- `Log` → **LogQL** (Loki): stream selectors from `loki_stream_labels`,
  `message` predicates as line filters, everything else as parsed-field
  filters (numeric comparisons included).
- `Span` searches → **TraceQL**; `Span` aggregations → PromQL over span
  metrics (or TraceQL metrics with `span_aggregations: "traceql"`);
  `uniqueCount(trace.id)` → root-span count.
- Nested queries (`SELECT average(c) FROM (SELECT count(*) AS c FROM
  Transaction FACET host)`) → the outer aggregation over the inner
  per-facet vector (`avg(sum by (instance)(...))`).
- Browser/mobile RUM, synthetics, `Nr*` account data, subqueries in
  WHERE/IN, `funnel()` → untranslatable, each with the LGTM equivalent named.

**Semantics preserved**: `TIMESERIES` → range queries with
`$__rate_interval`; no `TIMESERIES` → instant queries over `$__range` (NR
aggregates the whole window); `FACET` → `by (...)` + legend; `FACET ...
LIMIT n` → `topk(n, ...)`; `FACET cases(...)` / `if(...)` → one target per
case; `COMPARE WITH` → second target with `offset`; `SINCE` → panel/
dashboard range; `SINCE a UNTIL b` → `timeFrom` + `timeShift`; arithmetic
in SELECT (`(a / b) * 100`, `a - b`) and WHERE (`duration * 1000 > 500`);
OR across attributes → a PromQL `or` union (never silently dropped);
`count(*) WHERE duration > T` on a histogram → bucket arithmetic;
`{{variables}}` → `$variables`; multi-page dashboards → collapsed rows (or
`--page-strategy split`). The full matrix: [docs/translation-notes.md](docs/translation-notes.md).

## Adapting to *your* stack

Label names, metric names and span-metrics flavours vary by deployment, so
the converter flags every guess and the loop is: convert → validate
`--test` → put the fix in the config → convert again. Copy
`config/mappings.example.json` (or `n2g example-config`) and set:

- `label_map` — NR attribute → your Prometheus/Loki label names
- `metric_map` — NR metric → exact Prometheus name/type/unit, fixed
  matchers, or a full PromQL template; legacy timeslice names too
- `loki_stream_labels` / `loki_parser` / `loki_metadata_labels`
- `spanmetrics_flavor` / `span_metrics` / `span_aggregations`
- `http_metrics_flavor` / `http_metrics`, `metric_total_suffix`
- `event_map` — custom NR event types → Loki stream labels or a metric

## Working with an AI agent

`AGENTS.md` at the repository root explains the workflow, the output
contracts, how to fix flagged panels through the config, and the New Relic
semantics the converter guarantees; `CLAUDE.md` includes it, and
`.cursor/rules/` and `.kiro/steering/` point Cursor and Kiro at it. The
`.claude/skills/nr2grafana-migrate` skill is the end-to-end runbook for
Claude Code; `.claude/skills/nr-dashboard-tailor` finishes flagged panels
against a live stack. `n2g mcp serve` runs nr2grafana as an MCP server with
`inspect`, `explain`, `import`, `convert_files`, `validate_dashboards`,
`export` tools ([docs/api.md](docs/api.md)).

## Advanced commands

Everything from earlier releases is still there, hidden from `--help`:
`analyze` (packages with README/requirements/smoke test), `grafana check |
test | parity | samples | diagnose | heal | datasources | add-datasource |
import`, `changes report | suggest-config`, `cost`, `deepdive`,
`ai-context`, `ai`, `mcp`, `tco`, `aws`, `api`, `web` (the localhost UI) and
the interactive wizard (`n2g interactive`). See `docs/` for each.

## Development

```bash
python3 -m unittest discover -s tests   # stdlib only; test_awscost needs the AWS CLI
```

Layout: `nr2grafana/nrql/` (NRQL parser), `nr2grafana/translate/`
(`common.py` matchers/DNF, `metrics.py` PromQL, `logs.py` LogQL,
`traces.py` TraceQL, `nrmetrics.py` built-in NR/OTel/K8s knowledge,
`router.py`), `nr2grafana/grafana/` (builder, validator, live client),
`nr2grafana/pipeline.py` (the four operations), `nr2grafana/inspect.py`,
`nr2grafana/cli.py`. Design notes per release in `docs/dev/`.
