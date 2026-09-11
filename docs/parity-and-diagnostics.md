# Data parity & root-cause diagnostics

`grafana test` tells you *whether* a panel returns data. 1.2 adds the
two questions that actually decide whether a migration is done:

- **Parity** — does the migrated panel show the *same numbers* New
  Relic shows? For every panel target the original NRQL runs through
  NerdGraph (strictly read-only) and the translated query runs through
  Grafana's `/api/ds/query`; both results are normalized into series
  of `[timestamp, value]` points and compared.
- **Diagnostics** — when a panel is empty or wrong, *why*? A layered
  root-cause engine names the exact cause (bad token, missing
  datasource, renamed metric, wrong label value, absent ingestion
  pipeline) and, wherever possible, attaches a machine-applicable fix.

Both are available from the CLI, the web UI (Validate / Diagnostics
steps), and the JSON API.

## Running a parity check

Needs both sides: a Grafana service-account token and a New Relic USER
key, plus the account id(s) to run NRQL against (`--account-id`,
`NEW_RELIC_ACCOUNT_ID` comma-separated, or the ids recorded in the
package's widget report).

```bash
export GRAFANA_URL=https://grafana.example.com GRAFANA_TOKEN=glsa_...
export NEW_RELIC_API_KEY=NRAK-...
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

The Grafana time range is mirrored on the NR side: when the NRQL has
no `SINCE`/`UNTIL` of its own, `--from now-1h` becomes `SINCE 1 hour
AGO`, so both sides aggregate the same window. Faceted NRQL results
become one series per facet (labeled with the real facet attribute
names parsed from the query), `TIMESERIES` buckets become points, and
scalar aggregates become one-point series — mirroring how Grafana
dataframes are flattened on the other side. Exit code is 1 only when a
translated query itself errored (`gf-error`); mismatches and empty
panels are findings, not failures.

Results land in `parity-results.json` in the package dir (or
`<name>.parity-results.json` next to a flat file), are persisted to
the local store, and use schema `nr2grafana/parity/v1`. A panel row:

```json
{
  "panel_id": 2,
  "panel_title": "Latency p95",
  "refId": "A",
  "nrql": "SELECT percentile(duration, 95) FROM Transaction WHERE appName = 'checkout' TIMESERIES",
  "expr": "histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name=\"checkout\"}[5m])))",
  "datasource": "mimir",
  "verdict": "close",
  "detail": "consistent constant ratio ~0.001 (Grafana/New Relic) - Grafana values ~1/1000 of New Relic: likely s vs ms",
  "ratio": 0.001,
  "nr_summary": {"series": 1, "points": 6, "mean": 224.0,
                 "min": 208.0, "max": 251.0, "last": 219.0},
  "gf_summary": {"series": 1, "points": 6, "mean": 0.224,
                 "min": 0.208, "max": 0.251, "last": 0.219}
}
```

## Verdicts

Series are paired by matching label values, timestamps aligned to the
coarser side's buckets, and values compared per pair:

| Verdict | Meaning |
|---|---|
| `match` | Values agree: series means within 2%, aligned points within 5%. |
| `close` | Within tolerance (15% by default) — **or** the values differ by a *consistent constant ratio* (median ratio across points, with a median-absolute-deviation check so noise doesn't fake consistency). The ratio is reported. |
| `value-mismatch` | Both sides have data; the numbers genuinely differ. |
| `shape-mismatch` | The series don't line up (e.g. 1 NR series vs 7 Grafana series) — usually a faceting/grouping translation problem. |
| `nr-empty` | Grafana has data, New Relic returned none — nothing to verify against (data may have aged out of NR, or the NRQL window is off). |
| `gf-empty` | New Relic has data, the Grafana query returned none — the classic "metric/label doesn't exist here yet" case; run diagnose. |
| `both-empty` | No data on either side for this range. |
| `nr-error` / `gf-error` | That side's query failed; the error text is in `detail`. Per-panel errors never abort the run. |

### Unit hints on constant ratios

A consistent ratio near a known constant gets an explicit hint in
`detail`, because these are almost always unit mismatches, not data
problems:

| Ratio (Grafana/NR) | Hint |
|---|---|
| ~1000 | likely ms vs s |
| ~0.001 | likely s vs ms |
| ~60 | likely per-min vs per-s |
| ~1/60 | likely per-s vs per-min |

`histogram_quantile` returning seconds while NR's `percentile()`
reported milliseconds is the canonical `~0.001` case: the shapes agree
perfectly and the panel just needs its unit set (or the query a
`* 1000`) — hence `close`, not a mismatch.

## Score and readiness

The parity score is a weighted average over panel targets, 0–100:

| Verdict | Weight |
|---|---|
| `match` | 1.0 |
| `close` | 0.8 |
| Grafana data without an NR comparison (`nr-empty`, or `nr-error` when Grafana returned points) | 0.6 |
| `both-empty` | 0.5 |
| `value-mismatch`, `shape-mismatch`, `gf-empty` | 0.25 |
| `nr-error` (no Grafana data either), `gf-error` | 0.0 |

`readiness` folds in the requirement-check and data-test artifacts:
missing required datasources/plugins **always** grade `blocked` and
subtract 15 points each; otherwise ≥85 is `ready`, ≥60 `almost`, below
that `blocked`, each with plain-language reasons. Without a parity run
yet, the score falls back to data-test coverage (and says so). The web
UI's readiness ring and the download buttons are driven by exactly
this (`GET /api/readiness?slug=`).

## Human sign-off: `grafana samples`

Parity proves the *numbers* agree; sample review proves the *data* is
what you think it is. For every panel target it pulls a handful of
actual raw samples from each source and puts them side by side so a
person can confirm them — e.g. the CloudWatch logs you used to read
through New Relic vs the same log lines now flowing into Loki:

- **New Relic side** (read-only, via NerdGraph): for log/event panels
  the original NRQL is stripped of aggregates/`TIMESERIES`/`FACET`
  and re-run as `SELECT * FROM <event> WHERE <same where> LIMIT n`,
  yielding real event rows (`kind: "events"`); metric aggregates run
  as-is and show the raw result rows (`kind: "rows"`).
- **Grafana side**: Loki panels run just the stream selector as a raw
  log query and show the actual log lines with timestamps
  (`kind: "logs"`); Prometheus panels show the last datapoints per
  series (`kind: "points"`); tempo/passthrough panels show whatever
  rows the datasource returns (`kind: "rows"`).

Payloads are bounded (default 5 samples per side, strings truncated
at ~500 chars) and per-side failures become `kind: "error"` entries —
a sample pull never aborts. Results use schema
`nr2grafana/samples/v1`, land in `samples.json` in the package dir
and in the store as artifact `samples`.

```bash
python3 -m nr2grafana grafana samples ./out/checkout-service-overview \
    --account-id 1234567 --from now-1h --limit 5
```

```
Checkout Service Overview:
  nr:events  gf:logs    Error logs [A]
      nr> mock payment failed attempt=1
      gf> level=error msg="mock payment failed" attempt=1
  nr:rows    gf:points  Throughput [A]
  samples -> out/checkout-service-overview/samples.json
```

In the web UI, every panel's expandable detail has a **Samples**
section: a "Pull samples" button, side-by-side "New Relic" vs
"Grafana" cards with the raw lines/datapoints, and a sign-off bar —
*"Is this what you expect?"* with **Confirm** / **Reject** /
**Not sure** plus an optional note. Verdicts are stored per panel
target in the `review` artifact (`POST /api/review {slug, panel_id,
refId, verdict, note?}`, read back via `GET /api/review?slug=`;
`POST /api/samples {slug, panel_id?, limit?}` runs the pull as a
job) and every verdict lands in the change log as `panel-review`.

Verdicts feed `readiness` directly:

- any **rejected** panel forces grade `blocked`, with the rejected
  panels listed in the reasons;
- when **every** panel is confirmed, the readiness score is floored
  at 90 and the reasons gain a `human-verified` line — a validated
  dashboard someone actually vouched for, not just one whose numbers
  matched.

The rejection note is also handed to "Ask AI" as extra context when
you ask Claude to fix that panel.

## Diagnostics: `grafana diagnose`

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

It reads whatever artifacts exist next to the dashboard
(`requirements.json`, `datatest-results.json`, `parity-results.json`)
— run test and parity first for the sharpest diagnosis, but every
layer degrades gracefully when an input is missing. Exit code is 1 on
`blocker` findings. Output schema is `nr2grafana/diagnosis/v1`,
written to `diagnosis.json` and persisted to the store.

The layers, in order:

1. **Auth** — is the Grafana token valid, and does its role suffice
   (Editor+ to import/edit dashboards, Admin to create datasources)?
   Is the NR key usable? Bad credentials become findings with the
   exact role/permission named, not stack traces.
2. **Datasources** — required datasources/plugins missing or
   wrong-type (each missing one carries a ready-to-post
   `create_datasource` payload template), plus a live health check on
   the existing ones: a failing datasource reports Grafana's health
   message and the likely causes (URL unreachable *from the Grafana
   server*, auth, TLS).
3. **Per-panel root cause** — for each no-data/error panel: fetch the
   instance's actual metric names once; a missing metric gets a
   "did you mean" via fuzzy match (the `_total` counter-suffix flip is
   checked first and is high-confidence); a metric that exists but
   matches nothing gets *matcher elimination* — matchers are dropped
   one at a time with cheap instant queries until the offending label
   is found, then that label's real values are listed. Loki panels get
   the same treatment for stream-selector labels/values, plus
   json-vs-logfmt parser-stage detection.
4. **Data pipeline** — New Relic still has the data but the metric
   family is entirely absent from your stack: the finding names the
   missing ingestion pipeline or datasource (e.g. "no `aws_lambda_*`
   metrics in Mimir: add a CloudWatch datasource OR deploy
   cloudwatch-exporter"), sourced from the requirements analysis's
   domain table.
5. **Config** — every rename discovered above is consolidated into
   *one* mergeable config overlay (`metric_map`, `label_map`,
   `metric_total_suffix`, `loki_parser`), so the next `convert` run
   produces working queries directly.

Each finding:

```json
{
  "id": "panel-3-metric-total-suffix",
  "severity": "warn",
  "area": "panel",
  "panel_id": 3,
  "problem": "Panel 3: metric 'checkout_orders' does not exist, but 'checkout_orders_total' does - your pipeline appends the Prometheus '_total' counter suffix.",
  "evidence": "datasource has 'checkout_orders_total'; query asks for 'checkout_orders'",
  "confidence": "high",
  "fix": {
    "kind": "edit-query",
    "description": "Use 'checkout_orders_total' in the query (set config metric_total_suffix to True so future conversions match).",
    "action": {"panel_id": 3, "refId": "A",
               "new_expr": "sum(rate(checkout_orders_total{service_name=\"checkout\"}[$__rate_interval]))"}
  }
}
```

Severity is `blocker` / `warn` / `info`; area is `auth` / `datasource`
/ `panel` / `data` / `config`. Fix kinds:

| Kind | What applying it does |
|---|---|
| `edit-query` | Rewrites the panel target's expression (dashboard.json + datatest.json in the package; optional live push). |
| `config-overlay` | Deep-merges the payload into `config-overlay.json` next to the package. |
| `add-datasource` | Posts the prepared `create_datasource` payload — but only after you fill in the `needs_input` fields (URLs, credentials); it is refused until then. |
| `install-plugin` / `credentials` / `pipeline` | Manual: the finding carries the exact instructions (e.g. the `grafana-cli plugins install ...` line); nothing is applied. |
| `none` | Informational only. |

In the web UI every finding has a one-click **Fix** button that shows
exactly what will change before applying (`POST /api/fix
{slug, finding_id, push?}`); every applied fix is recorded in the
change log with before/after.

## Auto-heal: `grafana heal`

```bash
python3 -m nr2grafana grafana heal ./out/checkout-service-overview
# apply fixes AND push the healed dashboard to Grafana:
python3 -m nr2grafana grafana heal ./out/checkout-service-overview --push
```

```
Checkout Service Overview:
round 1: 1 data, 1 error, 2 no-data; 3 finding(s), 1 fixed
round 2: 2 data, 1 error, 1 no-data; 2 finding(s), 0 fixed
1 fix(es) applied, 2 finding(s) remaining (converged)
```

The loop is test → diagnose → apply **safe** fixes → re-test, up to
`--max-rounds` (default 3) or until it converges. Safe means:

- `config-overlay` merges — always reversible, never touch Grafana;
- `edit-query` fixes explicitly marked `"confidence": "high"` (the
  `_total` flip, near-exact did-you-mean matches).

Auto-heal will **never**: create datasources, enter credentials,
install plugins, apply low-confidence query guesses, or push anything
to Grafana without `--push`. Everything below the safety bar is left
in `remaining_findings` for you to apply one click at a time. Each
applied fix is change-logged with source `auto`.

## Scripting via the web API

The web server exposes the same engines as background jobs
(`POST` returns `{"job": id}`; poll `GET /api/jobs/<id>`):

- `POST /api/parity {"slug", "from?", "to?"}` — persisted as artifact
  `parity`
- `POST /api/diagnose {"slug"}` — persisted as `diagnosis`
- `POST /api/heal {"slug", "push?"}` — persisted as `heal`
- `POST /api/fix {"slug", "finding_id", "push?"}` — apply one finding
- `GET /api/readiness?slug=` — the readiness grade above

See [web-ui.md](web-ui.md) for the full API surface, and
[mock-stack.md](mock-stack.md) to try all of this offline against the
bundled fake Grafana + NerdGraph.
