# nr2grafana — New Relic → Grafana dashboard migrator

Converts New Relic dashboards into importable Grafana dashboards for an
LGTM stack (Mimir/Prometheus, Loki, Tempo, fed by OpenTelemetry) — and
since 1.1, sees the migration through: it tells you what each dashboard
needs before import, tests that data actually flows on your Grafana,
tracks every fix you make, and folds those fixes back into config so
the next dashboard converts right the first time.

- **Runs on your workstation** — this is a local CLI (plus a localhost
  web UI), not something you deploy into a cluster. The live check,
  test, and import steps just need network reachability to your Grafana
  (an ingress URL or a `kubectl port-forward`); fetch and convert need
  no connectivity beyond the New Relic API (or none at all with
  exported JSON files).
- **Zero dependencies** — Python 3.9+ standard library only.
- **Interactive by default** — run `n2g` (or `g2n` / `nr2grafana`) with
  no arguments and a guided wizard walks you through the whole
  migration with arrow-key menus: fetch → convert & package → validate
  → live check/test → import. It remembers your answers (dirs, URLs,
  region) between runs; secrets are never stored.

What it does:

- **Bulk export** straight from New Relic (NerdGraph API) or from files
  you copy out of the UI ("Copy JSON").
- **Batch convert** whole directories; every dashboard becomes a
  Grafana JSON file (schemaVersion 39, imports into Grafana 10.3+).
- **Honest output**: every panel carries the original NRQL and a
  confidence tag (`exact` / `approximate` / `needs-review` /
  `untranslatable`), and a machine-readable `migration-report.json`
  summarizes the run.
- **No dead panels**: untranslatable widgets become documented
  placeholders — or, with `--passthrough`, live panels that run the
  original NRQL through the official [New Relic Grafana datasource plugin](https://grafana.com/grafana/plugins/nrgrafanaplugin-newrelic-datasource/).
- **Requirements analysis + packages** (`--package`): per-dashboard
  directories with a README, requirements manifest, and smoke test.
- **Live testing**: verify requirements and pull real data through your
  Grafana with a service account token.
- **Web UI** (`web`): the whole flow in a browser, with a per-panel
  query editor.
- **Change tracking**: every fix is logged locally and can be codified
  back into the mapping config.
- **Optional AI assistance**: Claude diagnoses and fixes failing panels
  when you provide an Anthropic API key.

## Install

```bash
pipx install /path/to/Nr2Graf     # recommended: puts n2g / g2n / nr2grafana on PATH
# or: pip install --user .        # same commands via pip
# or run without installing (from the repo root):
python3 -m nr2grafana             # bin/nr2grafana also works from anywhere
```

## Quick start (interactive)

```bash
export NEW_RELIC_API_KEY=NRAK-...   # optional; the wizard prompts if unset
n2g                                  # pick "Full migration" and follow along
```

Prefer a browser? `n2g web` opens the same flow as a localhost app —
see [Web UI](#web-ui). The subcommands below remain for scripting and
CI.

## Quick start (scripted)

```bash
# 1. Bulk-export every dashboard you can see (US region; --region EU if needed)
export NEW_RELIC_API_KEY=NRAK-...        # a USER key, not a license key
python3 -m nr2grafana fetch -o ./newrelic-dashboards
# ...or a specific dashboard: fetch -g MjUyNjc4...GUID -o ./newrelic-dashboards
# ...or skip the API: New Relic UI -> dashboard -> "..." -> Copy JSON
#    and save it as a .json file in ./newrelic-dashboards/

# 2. Convert everything into per-dashboard packages
python3 -m nr2grafana convert ./newrelic-dashboards -o ./out --package \
    --config config/mappings.example.json

# 3. Point the grafana subcommands at your instance (service account token)
export GRAFANA_URL=https://grafana.example.com
export GRAFANA_TOKEN=glsa_...

# 4. Check requirements, import, test that data flows
python3 -m nr2grafana grafana check ./out/*/          # exit 1 if anything is missing
python3 -m nr2grafana grafana import ./out --folder "Migrated from NR"
python3 -m nr2grafana grafana test ./out/*/           # exit 1 on error panels

# 5. Fix flagged panels (web UI is easiest), then codify the fixes
python3 -m nr2grafana changes suggest-config          # -> config overlay to merge
```

Without `--package` you get the 1.0 behavior — flat JSON files plus
`migration-report.json` — importable via the UI or a `curl` loop
against `POST /api/dashboards/db`. Import semantics worth knowing
either way: Grafana's `overwrite: true` matches by **title within a
folder**, so it can silently replace a different dashboard that shares
a name. The converter renames duplicate titles within a batch
(`Team Dashboard (2)`), but keep overwrite off for first imports
(collisions then fail loudly) and turn it on only when re-importing
updated versions of the same dashboards.

## Know what you need before you import

`convert --package` (or `analyze` on already-converted output) runs a
requirements analysis per dashboard and writes one self-contained
package directory each, plus an `INDEX.md` for the run:

```
out/
├── INDEX.md                        # one summary row per dashboard
└── checkout-service-overview/
    ├── dashboard.json              # import this
    ├── README.md                   # datasources to create, import steps, troubleshooting
    ├── requirements.json           # machine-readable requirements
    ├── widget-report.json          # per-panel confidence + original NRQL
    ├── datatest.json               # per-panel test queries
    └── test.sh                     # executable data smoke test
```

The analysis reads the original NRQL, so it knows what the *data* side
needs, not just the datasources: which NR data domains the dashboard
drew from (AWS/GCP/Azure integrations, infra samples, K8s, logs,
traces, APM, RUM) and what pipeline or datasource produces the
equivalent — down to per-panel metric names, labels, and Loki stream
selectors. Widgets that are New Relic-native get an explicit
equivalent suggestion instead of silence. Details:
[docs/requirements-analysis.md](docs/requirements-analysis.md).

## Test that data actually flows

`validate` checks syntax; the `grafana` subcommands check reality,
through a Grafana service account token (`GRAFANA_URL` /
`GRAFANA_TOKEN`, or `--grafana-url` / `--grafana-token`):

- `grafana check <package-dir...>` — are the required datasources and
  plugins present? Actionable fix per missing item; exit 1 if not
  (CI-gateable).
- `grafana test <package-dir...>` — runs every panel query through
  `POST /api/ds/query` and classifies each as `data`, `no-data`
  (warning — the metric/labels don't exist in your stack yet), or
  `error` (with Grafana's actual error text). Writes
  `datatest-results.json` into the package; exit 1 only on errors.
- `grafana import <dir> [--folder F] [--overwrite]` — bulk import.

Each package also ships `test.sh` — the same smoke test in portable
`curl` + `python3` form, so any operator can run it without installing
nr2grafana. Details: [docs/live-testing.md](docs/live-testing.md).

## Web UI

```bash
python3 -m nr2grafana web    # 127.0.0.1:8765, opens your browser
```

The whole migration in a browser: setup (keys stay in process memory,
never on disk), fetch/convert as background jobs with live logs,
per-panel confidence badges and test status, an inline query editor
with **Test** / **Ask AI** / **Save & Push**, a "install these first"
requirements card, bulk import, and the change log with its suggested
config overlay. Localhost-only by default; `--port`, `--host`,
`--no-browser` to taste. Details: [docs/web-ui.md](docs/web-ui.md).

## AI assistance

Optional. Set `ANTHROPIC_API_KEY` (or paste a key in the web UI) and
"Ask AI" on a failing panel sends Claude the query, the actual Grafana
error, the original NRQL, and a sample of what exists on your instance
— and gets back an explanation plus a corrected query you can test and
push in one click, or the manual steps when no query fix applies. Note
that panel queries and error messages are sent to the Anthropic API
when (and only when) you use it; your Grafana/NR credentials never
are. Without a key, everything else works unchanged. Details:
[docs/ai-assist.md](docs/ai-assist.md).

## Change tracking

Everything the tool touches is recorded in a local sqlite database at
`~/.nr2grafana/` — converted dashboards, test results, run history,
and every query/datasource fix, whether made by you or the AI. No
secrets are ever stored there (the store refuses credential-looking
keys outright). `changes report [--markdown]` shows what changed and
why; `changes suggest-config` turns the recorded fixes into a
mergeable config overlay (`label_map` / `metric_map` /
`datasources.*.uid`), which closes the loop: fix once, reconvert,
never fix that panel again. Details:
[docs/changes-and-codify.md](docs/changes-and-codify.md).

## Commands

| Command | What it does |
|---|---|
| `fetch` | Bulk-export dashboards from New Relic via NerdGraph. `--guid` to cherry-pick, `--region US\|EU`, `--out DIR`. |
| `list` | List all dashboards (guid, name, account) visible to the key. |
| `convert` | Convert NR dashboard JSON files/dirs → Grafana JSON + `migration-report.json`. `--config`, `--page-strategy rows\|split`, `--passthrough`, `--package` for per-dashboard package dirs + `INDEX.md`. |
| `analyze` | (Re)generate requirements + packages for already-converted output. `-o DIR`. |
| `validate` | Statically validate Grafana dashboard JSON (schema requirements, unique panel ids, balanced query expressions, datasource variable wiring, grid bounds). |
| `grafana check` | Verify required datasources/plugins exist on a live instance; exit 1 on missing. |
| `grafana test` | Pull real data for every panel query; writes `datatest-results.json`; exit 1 on error panels. |
| `grafana import` | Bulk import dashboards. `--folder`, `--overwrite`. |
| `changes report` | Show the recorded change log. `--slug`, `--markdown`. |
| `changes suggest-config` | Infer a config overlay from recorded fixes. |
| `web` | Localhost web UI. `--port`, `--host`, `--no-browser`. |
| `example-config` | Print the full default mapping config. |

## How the conversion works

**Widgets → panels**

| New Relic | Grafana |
|---|---|
| viz.line / viz.area | timeseries (area = fill 30) |
| viz.stacked-bar | timeseries, bars + stacking |
| viz.billboard (+thresholds) | stat (+threshold steps) |
| viz.bullet | gauge (limit → max) |
| viz.bar | bargauge |
| viz.pie | piechart |
| viz.table | table (+sorting) |
| viz.markdown | text |
| viz.heatmap / viz.histogram | heatmap / histogram |
| logger.log-table-widget, `SELECT * FROM Log` | logs panel (Loki) |
| `SELECT * FROM Span` | table with TraceQL search (Tempo) |
| viz.funnel, service maps, inventory, custom viz | placeholder (or NRQL passthrough) |

**Queries** are routed by `FROM`:

- `Metric`, `Transaction`, `SystemSample`, `K8s*Sample`, ... → **PromQL**
  (Mimir). APM events map to OTel semconv metrics
  (`http_server_request_duration_seconds_*`); infra samples map to
  node_exporter / kube-state-metrics / cAdvisor metrics; `FROM Metric` names
  are normalized (dots→underscores) with type-aware aggregation
  (counter→`rate`/`increase`, histogram→`histogram_quantile`,
  gauge→`avg_over_time`).
- `Log` → **LogQL** (Loki). WHERE splits into stream selectors
  (configurable label set), line filters (`message` predicates), and
  parsed-field pipeline filters.
- `Span` aggregations → **PromQL over span metrics**; span searches →
  **TraceQL**.

**Semantics preserved**: `TIMESERIES` → range queries with
`$__rate_interval`; no `TIMESERIES` → instant queries over `$__range` (NR
aggregates the whole window — a naive instant query would not);
`FACET` → `by (...)` + legend; `FACET ... LIMIT n` → `topk(n, ...)`;
`COMPARE WITH` → second target with `offset`; `SINCE` → dashboard/panel time
range; NR `{{variables}}` → Grafana `$variables`; multi-page dashboards →
collapsed rows (or `--page-strategy split` → one dashboard per page with a
linked dropdown).

## Adapting to *your* stack (important)

The defaults assume a common OTel-collector → LGTM setup, but label names,
metric names, and span-metrics flavors vary by deployment. The easiest way
to encode yours is the wizard: `n2g` → **⚙️ Choose / create a mapping
config** runs a guided questionnaire (HTTP metric flavor, span-metrics
generation, `_total` suffix behavior, Loki stream labels and parser,
datasource uids) and writes the config for you — it even shows the curl
commands that discover each answer from your live stack. Or copy
`config/mappings.example.json` and set by hand:

- `label_map` — NR attribute → your Prometheus/Loki label names
- `metric_map` — your custom NR metrics → exact Prometheus names + types
- `loki_stream_labels` — which labels are Loki *index* labels in your setup
- `spanmetrics_flavor` / `http_metrics_flavor` — your metric naming generation
- `domain_map` — extend the requirements analyzer's NR-domain table

Everything the converter had to guess is flagged `needs-review`, so the
loop is: convert → import → test → fix the flagged panels → codify. In
1.1 the last step is automated — fixes made through the web UI (or by
the AI) are recorded, and `changes suggest-config` emits the
`label_map` / `metric_map` / datasource-uid entries for you. The config
makes the second dashboard cheaper than the first.

For per-dashboard fine-tuning with Claude, see the bundled skill in
`.claude/skills/nr-dashboard-tailor/` — it walks Claude through verifying a
converted dashboard against your live stack (label/metric existence, query
semantics) and fixing residual issues.

## Verifying against a live stack

The `grafana` subcommands above test through Grafana. If you'd rather
hit Mimir/Loki directly, `tools/check_queries.py` submits every
generated query and reports parse/validity failures (empty results are
fine — it checks syntax, not data):

```bash
python3 tools/check_queries.py ./grafana-dashboards \
    --prom http://localhost:9090 --loki http://localhost:3100
```

## Development

```bash
python3 -m unittest discover -s tests -v   # run the test suite
```

Project layout: `nr2grafana/nrql/` (NRQL parser), `nr2grafana/translate/`
(PromQL/LogQL/TraceQL translators + router), `nr2grafana/grafana/` (panel
builder, validator, live client), `nr2grafana/requirements.py` +
`artifacts.py` (analysis + packaging), `nr2grafana/store.py` +
`changelog.py` (local persistence), `nr2grafana/web/` (localhost UI),
`nr2grafana/ai.py` (Claude client), `nr2grafana/nerdgraph.py` (bulk
export client), `nr2grafana/cli.py`. More docs in
[docs/](docs/): [translation-spec.md](docs/translation-spec.md),
[requirements-analysis.md](docs/requirements-analysis.md),
[live-testing.md](docs/live-testing.md), [web-ui.md](docs/web-ui.md),
[ai-assist.md](docs/ai-assist.md),
[changes-and-codify.md](docs/changes-and-codify.md).
