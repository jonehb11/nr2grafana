# nr2grafana — New Relic → Grafana dashboard migrator

Converts New Relic dashboards into importable Grafana dashboards for an
LGTM stack (Mimir/Prometheus, Loki, Tempo, fed by OpenTelemetry) — and
sees the migration through: it tells you what each dashboard needs
before import, creates and health-checks the datasources, tests that
data actually flows on your Grafana, **proves the migrated panels show
the same numbers New Relic does**, explains the root cause of every
failure with one-click (or fully automatic) fixes, and folds those
fixes back into config so the next dashboard converts right the first
time.

- **Runs on your workstation** — this is a local CLI (plus a localhost
  web UI), not something you deploy into a cluster. The live check,
  test, and import steps just need network reachability to your Grafana
  (an ingress URL or a `kubectl port-forward`); fetch and convert need
  no connectivity beyond the New Relic API (or none at all with
  exported JSON files).
- **New Relic is never modified** — the tool only *reads* from New
  Relic (dashboard export and NRQL queries for parity/samples). This
  is enforced in code: the NerdGraph client refuses to send any
  GraphQL mutation. All changes happen on the Grafana side, and only
  the ones you ask for.
- **Zero dependencies** — Python 3.9+ standard library only.
- **Interactive by default** — run `n2g` (or `g2n` / `nr2grafana`) with
  no arguments and a guided wizard walks you through the whole
  migration with arrow-key menus: fetch → convert & package → validate
  → live check/test → parity → diagnose/heal → import. It remembers
  your answers (dirs, URLs, region) between runs; secrets are never
  stored.

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
- **Data parity** (`grafana parity`): run the original NRQL and the
  translated query side by side and compare the actual numbers —
  match/close/mismatch per panel, with unit-ratio hints (~1000× means
  ms vs s) and a 0–100 readiness score.
- **Human sample review** (`grafana samples` / web UI sign-off): pull
  actual raw samples from both sides — NR events vs the same log
  lines in Loki, NR aggregates vs Prometheus datapoints — side by
  side, then Confirm/Reject each panel; rejections block readiness,
  a fully confirmed dashboard is graded *human-verified*.
- **Root-cause diagnostics** (`grafana diagnose`): *why* is a panel
  empty — bad token role, missing datasource, renamed metric ("did
  you mean"), wrong label value, or a whole missing ingestion
  pipeline — each finding with a concrete, often machine-applicable
  fix.
- **Auto-heal** (`grafana heal`): test → diagnose → apply the safe
  fixes → re-test, automatically. Never creates datasources, never
  pushes without `--push`.
- **Datasource management**: list/health-check/create/edit/delete
  Grafana datasources from the CLI or web UI, with guided templates
  for Prometheus/Mimir, Loki, Tempo, CloudWatch, Google Cloud
  Monitoring, Azure Monitor, and the New Relic passthrough plugin.
- **Web UI** (`web`): the whole flow in a browser — stepper workflow,
  per-panel NR-vs-Grafana comparison, one-click fixes, downloads of
  the validated JSON.
- **Side-by-side compare** (web UI **Compare**): your New Relic
  dashboard and the migrated Grafana dashboard drawn next to each
  other with **real data**, panel-by-panel, each pair badged
  match/close/mismatch/no-data — plus a score ring, time-range picker,
  sync-hover, and a "show only disagreements" filter. See
  [docs/compare-view.md](docs/compare-view.md).
- **Add a datasource and watch it flow**: create a datasource from the
  guided form and immediately see a before/after — "0 panels had data
  → N flowing now" — with a live sample chart proving data is arriving.
- **Change tracking**: every fix is logged locally and can be codified
  back into the mapping config.
- **Optional AI assistance**: Claude diagnoses and fixes failing panels
  when you provide an Anthropic API key.
- **Mock stack** (`python3 tools/mock_stack.py`): a stdlib fake
  Grafana + NerdGraph, so the whole product can be demoed and e2e
  tested offline.

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

## Zero-touch workflow

Since 1.2 the web UI is a one-stop shop: the only things you type are
two API keys.

1. `n2g web`, then on the **Connect** step paste a New Relic USER key
   and a Grafana service-account token. Both are tested on the spot —
   including exactly what the Grafana token's role can do.
2. Everything else happens in the app: fetch and convert your
   dashboards, create the datasources the requirements analysis says
   are missing (guided forms, immediate health checks), run the data
   tests and the NR-vs-Grafana parity comparison, and work through
   the diagnosis — every finding has a one-click Fix, or press
   **Auto-heal** and let the safe fixes apply themselves.
3. When the readiness ring goes green, **Download** the validated
   dashboard JSON (single file, package zip, or everything) — or push
   straight into Grafana from the app.

No New Relic UI, no Grafana admin pages, no editing JSON by hand. To
try the whole loop without any real infrastructure, use the bundled
[mock stack](docs/mock-stack.md).

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

# 4. Check requirements; create whatever is missing
python3 -m nr2grafana grafana check ./out/*/          # exit 1 if anything is missing
python3 -m nr2grafana grafana add-datasource --type loki --name Loki \
    --set url=http://loki.monitoring.svc:3100         # guided, health-checked

# 5. Test that data flows, auto-heal the safe failures, diagnose the rest
python3 -m nr2grafana grafana test ./out/*/           # exit 1 on error panels
python3 -m nr2grafana grafana heal ./out/*/           # safe fixes, applied
python3 -m nr2grafana grafana diagnose ./out/*/       # root cause per failure

# 6. Import, then prove the panels show the same data New Relic does
python3 -m nr2grafana grafana import ./out --folder "Migrated from NR"
python3 -m nr2grafana grafana parity ./out/*/ --account-id 1234567

# 7. Codify the fixes so the next conversion is right the first time
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
- `grafana parity <package-dir...>` — the proof: original NRQL via
  NerdGraph vs translated query via Grafana, values compared per
  panel. `match` / `close` (with unit-ratio hints like "~1000× —
  likely ms vs s") / mismatch / empty verdicts, a 0–100 score, and a
  ready/almost/blocked readiness grade.
- `grafana diagnose <package-dir...>` — names the root cause of every
  failure (auth, datasources, metric/label renames, missing
  pipelines) with machine-applicable fixes; exit 1 on blockers.
- `grafana heal <package-dir...>` — applies the safe subset of those
  fixes in a test→diagnose→fix loop. `--push` to update Grafana.
- `grafana datasources` / `grafana add-datasource` — live datasource
  inventory with health, and guided creation
  ([docs/datasource-management.md](docs/datasource-management.md)).
- `grafana import <dir> [--folder F] [--overwrite]` — bulk import.

Each package also ships `test.sh` — the same smoke test in portable
`curl` + `python3` form, so any operator can run it without installing
nr2grafana. Details: [docs/live-testing.md](docs/live-testing.md) and
[docs/parity-and-diagnostics.md](docs/parity-and-diagnostics.md).

## Web UI

```bash
python3 -m nr2grafana web    # 127.0.0.1:8765, opens your browser
```

The whole migration in a browser, as a stepper: Connect → Fetch →
Convert → Datasources → Validate → Fix → Import → Verify → Download.
Keys stay in process memory, never on disk; long jobs stream logs
live. Per panel you get confidence, test and parity badges, the NR
result next to the Grafana result (sparklines), the diagnosis
findings with one-click Fix buttons, and a query editor with
metric-name autocomplete, **Test** / **Ask AI** / **Save & Push**.
Plus a datasources manager with guided "Add datasource" forms,
**Auto-heal**, a readiness score, and download buttons for the
validated JSON. Localhost-only by default; `--port`, `--host`,
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
| `grafana parity` | Compare real data New Relic vs Grafana per panel; writes `parity-results.json` + readiness. `--account-id`, `--from`, `--to`. |
| `grafana diagnose` | Root-cause failing/empty panels (auth, datasources, renames, pipelines); writes `diagnosis.json`; exit 1 on blockers. |
| `grafana heal` | Auto-apply the safe fixes in a test→diagnose→fix loop. `--push`, `--max-rounds`. |
| `grafana datasources` | List the instance's datasources with live health. |
| `grafana add-datasource` | Create a datasource from a guided template. `--type`, `--name`, `--set field=value`. |
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
`changelog.py` (local persistence), `nr2grafana/parity.py` +
`diagnose.py` + `remediate.py` (data parity, root-cause engine,
fix application), `nr2grafana/web/` (localhost UI), `nr2grafana/ai.py`
(Claude client), `nr2grafana/nerdgraph.py` (bulk export client),
`nr2grafana/cli.py`. More docs in [docs/](docs/):
[translation-spec.md](docs/translation-spec.md),
[requirements-analysis.md](docs/requirements-analysis.md),
[live-testing.md](docs/live-testing.md),
[parity-and-diagnostics.md](docs/parity-and-diagnostics.md),
[datasource-management.md](docs/datasource-management.md),
[web-ui.md](docs/web-ui.md), [ai-assist.md](docs/ai-assist.md),
[changes-and-codify.md](docs/changes-and-codify.md),
[mock-stack.md](docs/mock-stack.md).
