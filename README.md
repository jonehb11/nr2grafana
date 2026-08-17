# nr2grafana — New Relic → Grafana dashboard migrator

Converts New Relic dashboards into importable Grafana dashboards for an
LGTM stack (Mimir/Prometheus, Loki, Tempo, fed by OpenTelemetry). One-to-one
widget mapping, NRQL translated to PromQL / LogQL / TraceQL, batch in, batch
out.

- **Runs on your workstation** — this is a local CLI, not something you
  deploy into a cluster. For the optional live-check and import steps it
  just needs network reachability to your Mimir/Loki/Grafana endpoints
  (ingress URLs or a `kubectl port-forward`); fetch and convert need no
  connectivity beyond the New Relic API (or none at all with exported
  JSON files).
- **Zero dependencies** — Python 3.9+ standard library only.
- **Interactive by default** — run `n2g` (or `g2n` / `nr2grafana`) with no
  arguments in a terminal and a guided wizard walks you through the whole
  migration with arrow-key menus: fetch → convert → validate → live-check →
  import into Grafana. It remembers your answers (dirs, URLs, region)
  between runs; secrets are never stored.

## Install

```bash
pipx install /path/to/Nr2Graf     # recommended: puts n2g / g2n / nr2grafana on PATH
# or: pip install --user .        # same commands via pip
# or run without installing (from the repo root):
python3 -m nr2grafana             # bin/nr2grafana also works from anywhere
```

Then just type:

```bash
n2g
```
- **Bulk export** straight from New Relic (NerdGraph API) or from files you
  copy out of the UI ("Copy JSON").
- **Batch convert** whole directories; every dashboard becomes a Grafana
  JSON file (schemaVersion 39, imports into Grafana 10.3+ via UI or API).
- **Honest output**: every panel carries the original NRQL and a confidence
  tag (`exact` / `approximate` / `needs-review` / `untranslatable`) in its
  description, and a machine-readable `migration-report.json` summarizes the
  whole run.
- **No dead panels**: untranslatable widgets become documented placeholders —
  or, with `--passthrough`, live panels that run the original NRQL through
  the official [New Relic Grafana datasource plugin](https://grafana.com/grafana/plugins/nrgrafanaplugin-newrelic-datasource/).

## Quick start (interactive)

```bash
export NEW_RELIC_API_KEY=NRAK-...   # optional; the wizard prompts if unset
n2g                                  # pick "Full migration" and follow along
```

The wizard covers everything below; the subcommands remain for scripting
and CI.

## Quick start (scripted)

```bash
# 1. Bulk-export every dashboard you can see (US region; use --region EU if needed)
export NEW_RELIC_API_KEY=NRAK-...        # a USER key, not a license key
python3 -m nr2grafana fetch -o ./newrelic-dashboards

# ...or export a specific dashboard
python3 -m nr2grafana fetch -g MjUyNjc4...GUID -o ./newrelic-dashboards
# ...or skip the API entirely: New Relic UI -> dashboard -> "..." -> Copy JSON
#    and save it as a .json file in ./newrelic-dashboards/

# 2. Convert everything (batch in, batch out)
python3 -m nr2grafana convert ./newrelic-dashboards -o ./grafana-dashboards \
    --config config/mappings.example.json

# 3. Validate (also runs automatically during convert)
python3 -m nr2grafana validate ./grafana-dashboards

# 4. Import into Grafana: UI (Dashboards -> New -> Import) or API:
for f in grafana-dashboards/*.json; do
  [ "$(basename "$f")" = migration-report.json ] && continue
  jq -n --slurpfile d "$f" '{dashboard: $d[0], overwrite: false, folderUid: ""}' \
  | curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" -H "Content-Type: application/json" \
      -X POST "$GRAFANA_URL/api/dashboards/db" -d @-
  echo   # newline per response; check each line says "status":"success"
done
```

Import semantics worth knowing: Grafana's `overwrite: true` matches by
**title within a folder**, not just uid — so it can silently replace a
different dashboard that happens to share a name. The converter already
renames duplicate titles across one batch (`Team Dashboard (2)`), but keep
`overwrite: false` for first imports (collisions then fail loudly with
`name-exists`/`version-mismatch` instead of overwriting) and switch to
`overwrite: true` only when re-importing updated versions of the same
dashboards.

Then open `migration-report.json`, and review every widget tagged
`needs-review` / `untranslatable` (their panel titles carry `[REVIEW]` /
`[MANUAL]` suffixes too).

## Commands

| Command | What it does |
|---|---|
| `fetch` | Bulk-export dashboards from New Relic via NerdGraph. `--guid` to cherry-pick, `--region US\|EU`, `--out DIR`. |
| `list` | List all dashboards (guid, name, account) visible to the key. |
| `convert` | Convert NR dashboard JSON files/dirs → Grafana JSON + `migration-report.json`. `--config`, `--page-strategy rows\|split`, `--passthrough`. |
| `validate` | Statically validate Grafana dashboard JSON (schema requirements, unique panel ids, balanced query expressions, datasource variable wiring, grid bounds). |
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

Everything the converter had to guess is flagged `needs-review` in the
report, so the loop is: convert → import → fix the flagged panels → put the
fix into the config → reconvert. The config makes the second dashboard
cheaper than the first.

For per-dashboard fine-tuning with Claude, see the bundled skill in
`.claude/skills/nr-dashboard-tailor/` — it walks Claude through verifying a
converted dashboard against your live stack (label/metric existence, query
semantics) and fixing residual issues.

## Verifying against a live stack

`tools/check_queries.py` submits every generated query to your real
Mimir/Prometheus and Loki endpoints and reports parse/validity failures
(empty results are fine — it checks syntax, not data):

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
builder + validator), `nr2grafana/nerdgraph.py` (bulk export client),
`nr2grafana/cli.py`.
