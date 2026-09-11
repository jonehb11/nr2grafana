# Live testing against Grafana

Static validation proves the queries parse; live testing proves the
dashboard will actually show data on *your* Grafana — and since 1.2,
that it shows the *same* data New Relic did (`parity`), why it doesn't
(`diagnose`), and fixes what can safely be fixed (`heal`). Everything
here talks to Grafana through a service account token — nothing needs
direct Mimir/Loki access.

## Setup: service account token

In Grafana: **Administration → Users and access → Service accounts →
Add service account** → **Add service account token**. Role: Viewer
suffices for check/test/parity/diagnose; Editor for import and
applying query fixes; Admin only if you want nr2grafana to create
datasources for you (see
[datasource-management.md](datasource-management.md) — the web UI's
Connect step probes and reports what your token can do). Then:

```bash
export GRAFANA_URL=https://grafana.example.com
export GRAFANA_TOKEN=glsa_...
```

All `grafana` subcommands read these env vars, or take `--url` /
`--token` flags (`--grafana-url` / `--grafana-token` also work);
`--insecure` skips TLS verification for lab instances. The token
stays in process memory/env — it is never written to disk or logged.

## `grafana check` — are the requirements met?

Verifies a package's `requirements.json` against the live instance:
each required datasource family (by plugin id) and each required
plugin, with an actionable fix per problem.

```bash
python3 -m nr2grafana grafana check ./out/checkout-service-overview
```

Statuses per item: `ok`, `missing` (fix: "Add a Loki datasource:
Connections -> Data sources -> Add -> Loki", or a `grafana cli plugins
install ...` line), or `wrong-type` (a concrete uid points at a
different plugin type). Exit code is 1 if anything is missing — usable
as a CI gate before import. Accepts package dirs or bare
`requirements.json` paths.

## `grafana test` — does data actually flow?

Runs every panel query through `POST /api/ds/query` and classifies
each target:

```bash
python3 -m nr2grafana grafana test ./out/checkout-service-overview
```

- `data` — query ran and returned frames with points
- `no-data` — query ran, nothing matched (a *warning*: the metric or
  labels don't exist in your stack yet; the package README's
  data-expectations list says exactly what was expected)
- `error` — Grafana rejected the query; the actual error text is
  reported per panel

Results are written to `datatest-results.json` in the package dir.
Exit code is 1 only on `error` panels; `no-data` never fails the run.
Template variables are substituted with match-alls and datasource
variables resolved to the first datasource of the matching type (the
web UI lets you pick explicitly). Accepts package dirs or bare
`dashboard.json` files.

## `grafana import`

```bash
python3 -m nr2grafana grafana import ./out --folder "Migrated from NR"
# re-importing updated versions of the same dashboards:
python3 -m nr2grafana grafana import ./out --folder "Migrated from NR" --overwrite
```

Keep `--overwrite` off for first imports: Grafana matches by title
within a folder, so overwrite can silently replace a same-named
dashboard; without it, collisions fail loudly instead.

## `grafana parity` — is the data the *same*?

`test` proves data flows; `parity` proves the numbers agree with New
Relic. Per panel target it runs the original NRQL through NerdGraph
(read-only) and the translated query through `/api/ds/query`, over
the same time window, and compares:

```bash
export NEW_RELIC_API_KEY=NRAK-...     # both sides needed
python3 -m nr2grafana grafana parity ./out/checkout-service-overview \
    --account-id 1234567 --from now-1h --to now
```

```
Checkout Service Overview:
  results -> out/checkout-service-overview/parity-results.json
  match           [A] Requests per second              values agree (NR mean 42.58 vs Grafana mean 42.58)
  close           [A] Latency p95                      consistent constant ratio ~0.001 (Grafana/New Relic) - Grafana values ~1/1000 of New Relic: likely s vs ms
  gf-empty        [A] Orders processed                 New Relic has 6 point(s) but the Grafana query returned none
  both-empty      [A] Recent errors                    no data on either side for this range

score 64/100  (1 both-empty, 1 close, 1 gf-empty, 1 match)
readiness: almost (64/100)
  - 1 panel(s) return no data in Grafana while New Relic has data
  - 1 panel(s) had no New Relic data to compare against
```

Account ids come from `--account-id` (repeatable),
`NEW_RELIC_ACCOUNT_ID` (comma-separated), or the ids recorded in the
package's widget report. Results go to `parity-results.json` in the
package dir; exit code is 1 only on `gf-error` panels (the translated
query itself failed). Verdicts, unit-ratio hints, and the scoring
model: [parity-and-diagnostics.md](parity-and-diagnostics.md).

## `grafana diagnose` — *why* is a panel empty or wrong?

Layered root-cause analysis: token/role problems, missing or
unhealthy datasources, renamed metrics ("did you mean"), offending
label matchers (found by dropping matchers one at a time and probing),
Loki stream-label and json/logfmt parser mismatches, and truly-missing
ingestion pipelines. Uses whatever artifacts exist next to the
dashboard (`datatest-results.json`, `parity-results.json`,
`requirements.json`) — run test and parity first for the sharpest
answers.

```bash
python3 -m nr2grafana grafana diagnose ./out/checkout-service-overview
```

```
Checkout Service Overview:
  diagnosis -> out/checkout-service-overview/diagnosis.json
  warn     panel       panel 3: Panel 3: metric 'checkout_orders' does not exist, but 'checkout_orders_total' does - your pipeline appends the Prometheus '_total' counter suffix.
           fix (edit-query): Use 'checkout_orders_total' in the query (set config metric_total_suffix to True so future conversions match).
  info     config      Recurring rename patterns can be codified so the next conversion produces working queries directly.
           fix (config-overlay): Merge this overlay into your converter config (config-overlay.json) and re-run convert with it.
```

Findings are written to `diagnosis.json` with machine-applicable fix
payloads (applied one-click in the web UI, or automatically by
`heal`). Exit code is 1 on `blocker` findings.

## `grafana heal` — apply the safe fixes automatically

```bash
python3 -m nr2grafana grafana heal ./out/checkout-service-overview
# also push the fixed dashboard to Grafana:
python3 -m nr2grafana grafana heal ./out/... --push --max-rounds 3
```

```
Checkout Service Overview:
round 1: 1 data, 1 error, 2 no-data; 3 finding(s), 1 fixed
round 2: 2 data, 1 error, 1 no-data; 2 finding(s), 0 fixed
1 fix(es) applied, 2 finding(s) remaining (converged)
```

Loops test → diagnose → apply → re-test until converged or
`--max-rounds`. Only *safe* fixes are applied: config overlays and
query edits the engine marked high-confidence. It never creates
datasources, never touches credentials, and never pushes to Grafana
without `--push`; everything else stays in the diagnosis for you.
Fixes update the package's `dashboard.json`/`datatest.json` (or the
flat file) and are change-logged with source `auto`, so
`changes suggest-config` folds them into your mapping config.

## `grafana datasources` / `add-datasource`

List the instance's datasources with a live health badge, and create
missing ones from guided templates (Prometheus/Mimir, Loki, Tempo,
CloudWatch, Google Cloud Monitoring, Azure Monitor, the New Relic
passthrough plugin) — secrets prompted, health-checked immediately:

```bash
python3 -m nr2grafana grafana datasources
python3 -m nr2grafana grafana add-datasource --type loki --name Loki \
    --set url=http://loki.monitoring.svc:3100
```

Details, per-cloud credential requirements, and token-role notes:
[datasource-management.md](datasource-management.md).

## `test.sh` — the zero-install variant

Every package ships an executable `test.sh` that does what
`grafana test` does using only `curl` + `python3` (no jq, no
nr2grafana install) — hand the package dir to any operator:

```bash
GRAFANA_URL=https://grafana.example.com GRAFANA_TOKEN=glsa_... \
  sh ./out/checkout-service-overview/test.sh
```

One line per query — `PASS`, `NO-DATA` (warning), or `FAIL` with the
error detail. Exit 1 only on FAILs; exit 2 with a usage message if the
env vars or tools are missing:

```
error: set GRAFANA_URL and GRAFANA_TOKEN first, e.g.
  GRAFANA_URL=https://grafana.example.com \
  GRAFANA_TOKEN=glsa_xxx sh test.sh
(GRAFANA_TOKEN is a Grafana service account token)
```

`datatest.json` in the same dir is the machine-readable manifest both
runners share: per-panel raw queries, datasource family, and whether
data is expected (`data` for exact/approximate panels, `any`
otherwise).

## The loop

1. `grafana check` → `grafana add-datasource` for what's missing.
2. `grafana test` → `grafana heal` fixes the safe cases itself.
3. `grafana diagnose` → apply the remaining fixes (web UI one-click,
   or by hand) — every fix is recorded and can be codified back into
   config; see [changes-and-codify.md](changes-and-codify.md).
4. `grafana import`, then `grafana parity` to prove the imported
   dashboards show the same data New Relic did.
