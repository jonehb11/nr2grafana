# Live testing against Grafana

Static validation proves the queries parse; live testing proves the
dashboard will actually show data on *your* Grafana. Both the CLI and
each package's `test.sh` talk to Grafana through a service account
token — nothing here needs direct Mimir/Loki access.

## Setup: service account token

In Grafana: **Administration → Users and access → Service accounts →
Add service account** (role: Editor for import/test; Viewer suffices
for check/test only) → **Add service account token**. Then:

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

1. `grafana check` → create what's missing.
2. `grafana import`.
3. `grafana test` → for each `error`/`no-data` panel, fix the query
   (web UI panel editor, or by hand) — every fix is recorded and can
   be codified back into config; see
   [changes-and-codify.md](changes-and-codify.md).
