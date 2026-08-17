# NRQL → LGTM translation rules (as implemented)

Reference for what `nr2grafana` emits and why. Confidence levels:
**exact** (semantically equivalent) · **approximate** (bounded known drift)
· **needs-review** (correct only under a stated assumption) ·
**untranslatable** (no equivalent; placeholder emitted).

## Clause routing

| NRQL clause | Destination |
|---|---|
| `SELECT agg(x)` | query expression core; multiple aggregations → multiple targets (refId A, B, …) |
| `AS 'alias'` | legendFormat |
| `FROM <source>` | datasource routing: `Log`→Loki, `Span` search→Tempo, `Span` aggregation→span metrics in Mimir, everything else→Mimir |
| `WHERE` | label matchers / stream selectors / line filters |
| `FACET a, b` | `by (a, b)` + legend `{{a}} / {{b}}` |
| `FACET … LIMIT n` | `topk(n, …)` (per-step on range queries — approximate) |
| `TIMESERIES` | range query, window `$__rate_interval` (Loki: `$__auto`) |
| no `TIMESERIES` | instant query, window `$__range` (NR aggregates the whole SINCE window; a bare instant selector would not) |
| `SINCE t` | dashboard time range (most common SINCE across widgets wins) |
| `UNTIL` | flagged; Grafana panel overrides can't express non-now UNTIL |
| `COMPARE WITH t` | second PromQL target with `offset` (LogQL: dropped + flagged) |
| `SLIDE BY` | absorbed into TIMESERIES handling |
| `WITH TIMEZONE` / `EXTRAPOLATE` | dropped with an informational note |

## Aggregations → PromQL (type-aware)

Metric type comes from `metric_map` config, else heuristics (flagged
needs-review): `_total`→counter, `percentile/histogram/apdex` arg→histogram,
`rate/count`→counter, else gauge.

| NRQL | gauge | counter | histogram |
|---|---|---|---|
| `average(m)` | `avg by() (avg_over_time(m[W]))` | flagged | `sum(rate(m_sum[W])) / sum(rate(m_count[W]))` |
| `sum(m)` | `sum(avg_over_time(m[W]))` (approx) | `sum(increase(m[W]))` | `sum(increase(m_sum[W]))` |
| `max/min(m)` | `max(max_over_time(m[W]))` | — | `histogram_quantile(1/0, …_bucket)` (approx) |
| `count(*)` | `count(m)` (series, flagged) | `sum(increase(m[W]))` | `sum(increase(m_count[W]))` |
| `latest(m)` | `m` instant / `last_over_time(m[$__interval])` range | same | — |
| `percentile(m, p…)` | — | — | `histogram_quantile(p/100, sum by (le,BY)(rate(m_bucket[W])))`, one target per p |
| `rate(agg, 1 min)` | flagged | `sum(rate(m[W])) * 60` | `sum(rate(m_count[W])) * 60` |
| `derivative(m, 1 min)` | `deriv(m[W]) * 60` | `rate * 60` | — |
| `uniqueCount(attr)` | `count(count by (attr)(m))` (label-value approx) | | |
| `filter(agg, WHERE p)` | inner agg with p merged into selector | | |
| `agg(x) * k` (SELECT arithmetic) | multiplier preserved as `(expr) * k`; derived unit dropped + flagged | | |
| `stddev(m)` | `stddev_over_time(m[W])` per series (approx; NR is event-level) | | |
| `percentage(agg, WHERE p)` | `100 * (with p) / (without p)` | | |
| `apdex(m, t: T)` | — | — | `(rate(bucket{le=T}) + rate(bucket{le=4T})) / 2 / rate(count)` — needs buckets at T and 4T |
| `histogram(m, …)` | — | — | `sum by (le)(increase(m_bucket[$__interval]))` + heatmap panel (forced range query) |
| `funnel/earliest/keyset/eventType` | untranslatable | | |

## FROM sources

- **Metric**: dots→underscores; `metric_map` overrides name/type/unit.
- **Transaction**: `http_server_request_duration_seconds_*` (semconv) or
  `http_server_duration_milliseconds_*` (`http_metrics_flavor: legacy`).
  `appName` → `service_name`, `name` → `http_route` (transaction names are
  not route patterns — flagged), `error IS TRUE` → 5xx matcher (flagged),
  `httpResponseCode >= 500` → `http_response_status_code=~"5.."`.
- **TransactionError**: HTTP histogram `_count` + 5xx matcher (needs-review).
- **Span aggregations**: span-metrics per `spanmetrics_flavor`
  (`otel` → `traces_span_metrics_duration_milliseconds` / `…_calls_total`,
  `tempo` → `traces_spanmetrics_latency` / `…_calls_total`).
- **SystemSample / NetworkSample / StorageSample / K8s\*Sample**: fixed
  per-attribute templates onto node_exporter / kube-state-metrics / cAdvisor
  (all flagged needs-review — exporter-dependent).
- **PageView / Browser\* / Mobile\***: untranslatable (no RUM in LGTM unless
  Faro).

## WHERE operators

`=`/`!=` → exact matchers; `IN` → escaped alternation `=~"a|b"`;
`LIKE '%x%'` → `=~"(?i).*x.*"` (NRQL LIKE is case-insensitive; literals are
regex-escaped); `RLIKE` → `=~` pass-through for PromQL/Loki label matchers (both RE2, both anchored) but wrapped in `^(?:...)$` for TraceQL, whose regex matchers are unanchored;
`IS NULL`/`IS NOT NULL` → `=""` / `!=""`; `IS TRUE/FALSE` → `="true"/"false"`;
numeric compares on labels → only http status classes special-cased
(`>= 500` → `5..`), else dropped + flagged; OR across different labels →
flagged; `{{var}}` → `=~"${var:regex}"` (multi-value safe).

## Logs (FROM Log) → LogQL

WHERE splits three ways: attributes in `loki_stream_labels` → stream
selector; `message` predicates → line filters (`|=`, `|~"(?i)…"`); everything
else → `| json | field op "v" | __error__=""` pipeline (flagged). Empty
selector is never emitted (`{service_name=~".+"}` + flag).
`count(*)` → `count_over_time`; `rate` → `rate()*unit`; numeric aggregations
→ `unwrap` with `by (...)` grouping on the range function itself (empty
`by ()` when unfaceted) so results aggregate across streams like NR events,
not per stream; `percentile` → `quantile_over_time`. `SELECT *` → logs
panel, `LIMIT` → maxLines.

## Traces (FROM Span, search-shaped) → TraceQL

`service.name`→`resource.service.name`, `name`→`name`,
`duration.ms > 500`→`duration > 500ms`, `error IS TRUE`→`status = error`,
unknown attrs → scope-agnostic `.attr` (flagged). `LIMIT` → target limit.
All regex forms (IN/LIKE/RLIKE) are explicitly anchored (`^...$`) because
TraceQL regex matchers are unanchored, unlike PromQL.

## Panels

viz.line/area → timeseries · viz.stacked-bar → timeseries (bars, stacked) ·
viz.billboard → stat (alertSeverity thresholds → steps) · viz.bullet → gauge
(limit → max) · viz.bar → bargauge · viz.pie → piechart · viz.table → table ·
viz.markdown → text · viz.heatmap/histogram → heatmap/histogram ·
log widgets → logs panel. Layout: `x=(column-1)*2`, `w=width*2`,
`y=(row-1)*3`, `h=height*3` (24-col grid). Pages → collapsed rows
(`page_strategy: rows`) or one dashboard per page with a linked dropdown
(`split`). NR variables: NRQL `uniques(attr)` → `label_values(label)` query
variable; ENUM → custom; STRING → textbox. Datasource references are
`type: datasource` template variables by default (portable imports).
