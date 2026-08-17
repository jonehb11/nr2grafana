---
name: nr-dashboard-tailor
description: Tailor a converted New Relic→Grafana dashboard to the local LGTM stack — verify metric/label names against live Mimir/Loki/Tempo, fix panels flagged needs-review/untranslatable in the migration report, and fold fixes back into the converter config. Use after running `nr2grafana convert`, when a converted dashboard shows no-data panels, or when the user asks to "fix/tailor/finish" a migrated dashboard.
---

# Tailoring a converted dashboard to this LGTM stack

You are finishing the migration of one (or more) Grafana dashboards produced
by `nr2grafana convert`. The converter is deliberately honest: everything it
had to guess is tagged. Your job is to turn guesses into verified facts.

## Inputs to gather first

1. The converted dashboard JSON file(s) and `migration-report.json` from the
   same output directory. The report lists, per widget: original NRQL,
   emitted queries, confidence, and notes explaining every assumption.
2. Stack endpoints (ask the user if unknown, or read from Grafana
   datasource provisioning):
   - Mimir/Prometheus query URL (e.g. `http://mimir:9009/prometheus`)
   - Loki URL, Tempo URL
   - A Grafana URL + token is even better: you can query *through* Grafana
     datasource proxies (`/api/datasources/proxy/uid/<uid>/...`) so you don't
     need direct network access to each backend.

## Workflow

### 1. Triage from the report
Read `migration-report.json`. Work the widgets in this order:
`untranslatable` → `needs-review` → `approximate`. Ignore `exact` unless the
user reports it broken.

### 2. Verify assumptions against the live stack (never guess twice)
For each flagged panel, check what the note says was assumed, then verify:

- **Metric exists / type**:
  `GET <mimir>/api/v1/metadata?metric=<name>` and
  `GET <mimir>/api/v1/label/__name__/values?match[]=<candidate>` — try the
  candidates listed in the panel note (e.g. `http_server_request_duration_seconds`
  vs `http_server_duration_milliseconds`; `traces_span_metrics_*` vs
  `traces_spanmetrics_*`; `_ratio`/`_total` suffix variants).
- **Label exists / values**:
  `GET <mimir>/api/v1/labels` and `/api/v1/label/<label>/values` (e.g. is it
  `service_name`, `job`, or `service`? `cluster` or `k8s_cluster_name`?).
- **Loki stream labels**: `GET <loki>/loki/api/v1/labels` — anything not in
  this list must be a pipeline filter (`| json | field="v"`), not a stream
  selector.
- **Actually run the query**: `GET <mimir>/api/v1/query?query=<expr>` (or the
  Grafana proxy equivalent) and confirm it returns series. Empty result ≠
  valid migration.

### 3. Fix the dashboard JSON
Edit the panel targets in place. Rules:
- Keep the original NRQL and the confidence note in the panel `description`;
  append a line `Tailored: <what you changed and verified>`.
- Remove the ` [REVIEW]` suffix from the panel title only after the query
  verifiably returns data.
- For `untranslatable` placeholders, decide with the user: rebuild with a
  different approach (recording rule, different metric), keep it as NRQL
  passthrough via the New Relic datasource plugin, or drop it.
- Preserve panel ids, gridPos, and refIds. Run
  `python3 -m nr2grafana validate <file>` after editing.

### 4. Fold fixes back into the config (make the next dashboard free)
Every fix is one of these — put it where it belongs in the mapping config
(the JSON passed to `--config`):
- wrong label name → `label_map`
- wrong metric name/type/unit → `metric_map`
- wrong Loki indexing assumption → `loki_stream_labels` / `loki_parser`
- wrong span-metrics naming → `spanmetrics_flavor` or `span_metrics`
- wrong HTTP semconv generation → `http_metrics_flavor`

Then offer to re-run `python3 -m nr2grafana convert` on the remaining
dashboards with the updated config, and diff the results.

### 5. Verify end-to-end
If Grafana API access exists: POST the dashboard
(`/api/dashboards/db`, body `{"dashboard": <json with id:null>, "overwrite": true}`),
then query each panel's expr via the datasource proxy and report which panels
return data and which are still empty (with the reason: missing metric,
missing label, no data in range).

## Known semantic gaps to communicate honestly

- Percentiles via `histogram_quantile` are bucket-interpolated; small
  numeric drift vs New Relic is expected and normal.
- `topk` on range queries is evaluated per step (series may flicker); it is
  the correct translation of `FACET ... LIMIT`.
- `uniqueCount` over metrics counts label values on series, not raw events.
- NR `count(*)` per-bucket vs Prometheus `rate()` differ by a unit factor —
  the converter uses `increase()`/`rate()*60` to match NR semantics; verify
  the panel unit rather than "fixing" the numbers.
- Funnels, service maps, RUM (PageView) widgets have no LGTM equivalent
  unless Faro/RUM is deployed — say so instead of inventing a lookalike.
