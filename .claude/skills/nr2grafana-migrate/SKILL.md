---
name: nr2grafana-migrate
description: Migrate New Relic dashboards to Grafana end to end with nr2grafana — import from New Relic, convert to Grafana JSON, validate against the live Grafana (datasource types, per-panel data), export and verify, then fix flagged panels through the mapping config. Use when the user asks to migrate/convert/move a New Relic dashboard to Grafana, to check what a migrated dashboard needs, or why a panel was flagged.
---

# Migrating New Relic dashboards to Grafana

You drive `python3 -m nr2grafana` (or `n2g`). Put `--json` before every
command and read the single JSON object on stdout; human text is on stderr.
Exit codes: 0 ok, 1 problems, 2 bad input, 3 cannot reach/authenticate.

## 1. Get the dashboards

- From New Relic (needs `NEW_RELIC_API_KEY`, a USER key; read-only):
  `python3 -m nr2grafana --json import -o ./nr --name "<part of the name>"`
  (or `--guid <GUID>`, or nothing for all dashboards the key can see).
- From a file the user pasted/exported ("Copy JSON" or a NerdGraph read):
  `python3 -m nr2grafana --json import -o ./nr <file.json>`.
- Read `import-manifest.json`: name, guid, account, pages, widgets per file.

## 2. Understand before converting

`python3 -m nr2grafana --json inspect ./nr/<file>.json` gives, per widget,
the parsed NRQL clauses, attributes/variables, the translation plan and
confidence, the datasource types needed, and `cannot_migrate` with a reason
and the closest Grafana equivalent. Tell the user up front which widgets will
not migrate and why (funnels, service maps, custom visualizations, RUM,
synthetics, Nr* account data, subqueries).

For one query: `python3 -m nr2grafana --json explain "<NRQL>"`.

## 3. Convert

`python3 -m nr2grafana --json convert ./nr -o ./grafana [-c mappings.json]`

Read `result.dashboards[*]`: `output`, `uid`, `datasources`,
`cannot_migrate`, `needs_review` (each with the assumptions to verify),
`validation.errors` (must be empty). Report the cannot-migrate list verbatim.

## 4. Validate against the user's Grafana

`python3 -m nr2grafana --json validate ./grafana --grafana-url $GRAFANA_URL --test`

- `datasources[*].status == "missing"` → tell the user exactly which
  datasource type to add (the `fix` field) or, for the New Relic passthrough,
  which plugin to install. Nothing else will make those panels work.
- `data_test.summary`: `error` panels are wrong queries or labels; `no-data`
  panels mean the metric/labels do not exist in the stack yet.
- Verify assumptions through the Grafana datasource proxy
  (`/api/datasources/proxy/uid/<uid>/api/v1/label/__name__/values`,
  `/api/v1/label/<label>/values`, Loki `/loki/api/v1/labels`) rather than
  guessing.

## 5. Fix through the config, then reconvert

Put every fix in the mapping config (`python3 -m nr2grafana example-config`
shows all keys): `label_map`, `metric_map` (name/type/unit, `expr`
templates, timeslice names), `loki_stream_labels` / `loki_parser`,
`spanmetrics_flavor`, `http_metrics_flavor`, `metric_total_suffix`,
`event_map`, `span_aggregations`. Re-run `convert` and `validate` until
`validation.errors` is empty and the data test has no `error` rows. Only
edit dashboard JSON by hand for one-off cosmetics.

## 6. Export and verify

`python3 -m nr2grafana --json export ./grafana --grafana-url $GRAFANA_URL --folder "Migrated from New Relic" --test`

For each `result.dashboards[*]` report: `title`, `url`, `uid`, `folder`,
`source.name` / `source.guid` (the New Relic dashboard it came from),
`verified.ok`, `data_test.summary`, and `problems`. `--overwrite` replaces an
existing dashboard with the same uid/title.

## Rules

- New Relic is never modified. Never echo API keys or tokens.
- Do not invent metric names: the converter flags guesses as `needs-review`
  and says what to verify; verify, then codify.
- A panel is done only when its query returns data on the user's Grafana.
