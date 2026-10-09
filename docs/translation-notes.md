# Translation notes: NRQL -> PromQL / LogQL / TraceQL

Honest support matrix for the nr2grafana translation layer
(`nr2grafana/nrql/parser.py` + `nr2grafana/translate/`), current as of
1.11. Every translated query carries a confidence level; this document
says exactly what each construct becomes and where the semantics drift.
The 1.11 fidelity rules (metric kind inference, counter semantics,
summaries, the K8s map, CloudWatch targets, `concat()`/`$env`, live
hints, datasource binding, `[MANUAL]` placeholders) are documented in
depth in [live-translation.md](live-translation.md); the tables below
only summarize them.

Confidence taxonomy (worst wins across a query):

| Level | Meaning |
| --- | --- |
| `exact` | semantically equivalent output |
| `approximate` | close; minor, documented semantic drift |
| `needs-review` | translated, but a human must verify an assumption |
| `untranslatable` | no sound mapping; the note says why and what to do |

General invariants:

- `TIMESERIES` -> Grafana range query with window `$__rate_interval`
  (PromQL) / `$__auto` (LogQL); no `TIMESERIES` -> instant query with
  window `$__range`, matching NR's whole-window aggregation.
- `SINCE`/`UNTIL` map to the panel/dashboard time range (via a
  `timefrom:` hint the builder consumes), never into the query text.
- Units are never numerically rescaled; a `unit:` hint sets the panel
  unit instead. Count-shaped aggregations always hint `unit:short`.
- Untranslatable or unparsable queries never abort a conversion: the
  panel becomes a `[MANUAL]` text panel (WHY + closest equivalent +
  original NRQL) and the widget report carries `closest_equivalent`.
  There are no silent empty panels.
- Label values are rendered by one shared rule (metrics, logs,
  CloudWatch): literals verbatim, `{{var}}` -> `$var`, `concat('p-',
  {{env}})` -> `p-$env`, IN/OR lists -> regex alternation of the
  rendered values. A Python-looking node repr in an emitted query is a
  bug.

## Event-type routing (FROM ...)

| FROM | Target | Status | Notes |
| --- | --- | --- | --- |
| `Metric` (app metrics) | PromQL | varies | deterministic rename (dots/dashes -> `_`, camelCase kept, counters get `_total`) + kind inference: `metric_map` > `metric_kinds` > live Mimir metadata (`--live`) > name rules (gauge words / event words / summary / histogram suffixes) > aggregation rules (`sum()`/`count()` -> counter, `latest()`/`average()`/`max()`/`min()` -> gauge, `percentile()` -> histogram) > gauge; name/aggregation-rule hits are `approximate` (the default and `percentile()` of an unknown name are `needs-review`). See [live-translation.md section 1](live-translation.md#1-metric-rename-and-kind-inference) |
| `Metric` (`aws.*`) | CloudWatch | approximate | real CloudWatch datasource target `{namespace, metricName, statistic, dimensions}` (or a Metric Insights `SEARCH` expression for `filter()`/multi-queue sums); the `cloudwatch` datasource becomes REQUIRED |
| `Metric` (`k8s.*`) | PromQL | exact / approximate | `K8S_METRIC_MAP` -> kube-state-metrics / cAdvisor / node_exporter expressions; `k8s.<x>Name` attributes -> `<x>` labels; extensible via `k8s_metric_map` |
| `Metric` (summary views `.mean/.median/.upper/.percentiles/...`) | PromQL | approximate | NR summary -> Prometheus summary `_sum`/`_count` (`sum(rate(_sum))/sum(rate(_count))`) |
| `Transaction` | PromQL | approximate | OTel semconv `http_server_request_duration_seconds` histogram (legacy flavor and config overrides supported); requires OTel instrumentation |
| `TransactionError` | PromQL | needs-review | approximated as 5xx responses on the same histogram |
| `Span` (aggregations) | PromQL | needs-review | span metrics (`spanmetrics_flavor` config: otel / otel-seconds / tempo / legacy); metric names are deployment-specific |
| `Span` (raw / SELECT *) | TraceQL | approximate | trace search; results differ from raw span listings |
| `Log` | LogQL | varies | see LogQL section |
| `SystemSample` / `NetworkSample` / `StorageSample` | PromQL | exact / approximate | canonical node_exporter expressions (see INFRA_MAP); flagged needs-review because exporter presence is assumed |
| `K8s*Sample` | PromQL | exact-shaped | kube-state-metrics / cAdvisor expressions; same exporter caveat; `containerCpuCfsThrottledPeriodsDelta / containerCpuCfsPeriodsDelta` -> `rate(..._throttled_periods_total)/rate(..._periods_total)` |
| AWS integration samples (`AwsRds*`, `QueueSample`, ...) | CloudWatch | approximate | routed like `aws.*` metrics |
| `PageView` / `Browser*` / `JavaScriptError` / `AjaxRequest` | - | untranslatable | names Grafana Faro as the LGTM equivalent |
| `Mobile*` | - | untranslatable | names Grafana Faro |
| `Synthetic*` | - | untranslatable | names blackbox_exporter / Grafana Synthetic Monitoring |
| `NrConsumption` / `NrUsage` / `NrAuditEvent` | - | untranslatable | New Relic-only; `[MANUAL]` with closest equivalent = the NR datasource plugin (`--passthrough`) |
| `FinanceSample` | - | untranslatable | `[MANUAL]`; closest equivalent = AWS Cost Explorer (`tco analyze`) |
| `Deployment` | - | untranslatable | `[MANUAL]`; closest equivalent = Grafana annotations |
| multiple event types (`FROM Log, Log_dev`) | first only | needs-review | only the first FROM entry is translated; noted |

## Aggregations -> PromQL

| Construct | Status | Translation / notes |
| --- | --- | --- |
| `count(*)` on counter | approximate | `sum by (...)(increase(m[W]))`; on `FROM Metric` NR count() counts datapoints, not increase - noted |
| `count(*)` on histogram | exact | `sum(increase(m_count[W]))` |
| `count(*)` on gauge | needs-review | `count(m)` counts series, not events |
| `sum(x)` counter | exact | instant: `sum(increase(m_total[$__range]))` (total over the window); `TIMESERIES`: `sum(increase(m_total[$__interval]))` (per bucket) |
| `sum(x)` / `count(x)` on an unknown app metric | approximate | inferred counter (AGG-RULES) -> the counter shape above, never `avg_over_time`; `metric_kinds` to override |
| `rate(sum(x), 1 second)` counter | exact | `sum(rate(m_total[$__rate_interval]))` (scaled for other units) |
| `sum(x)` gauge | approximate | `sum(avg_over_time(m[W]))` (current-total semantics) |
| `average(x)` histogram | exact | `sum(rate(_sum)) / sum(rate(_count))` |
| `average(x.mean)` / `average(x.upper.percentiles)` summary | approximate | `sum(rate(x_mean_sum[W])) / sum(rate(x_mean_count[W]))` |
| `average(x)` gauge | exact | `avg(avg_over_time(m[W]))` |
| `max(x)` / `min(x)` gauge | exact | `max(max_over_time)` / `min(min_over_time)` |
| `max(x)` histogram | approximate | `histogram_quantile(1, ...)` = top bucket bound (overestimate) |
| `min(x)` histogram | needs-review | p0 = lowest bucket bound |
| `latest(x)` | exact | instant vector (instant) / `last_over_time(m[$__interval])` (range); histogram -> recent average, approximate |
| `earliest(x)` | untranslatable | PromQL has no `first_over_time` (LogQL does - see below) |
| `percentile(x, p...)` histogram | approximate | `histogram_quantile(p/100, sum by (le, ...)(rate(_bucket[W])))`; bucket interpolation vs NR event data noted; multiple p values -> one target each |
| `percentile(x, p)` gauge (or unmapped non-histogram name) | needs-review | `quantile_over_time(p/100, m[W])` per series (avg across series); NR computes percentiles over all raw events. An unmapped `FROM Metric` name with no `_bucket`/duration/latency hint resolves as a gauge and takes this path — it is NEVER emitted as `histogram_quantile` over a nonexistent `m_bucket` family (which would return no data) |
| `percentile(x, p)` counter | untranslatable | a counter has neither a `_bucket` series nor rankable raw samples; note says to map the metric to a histogram in `metric_map` |
| `median(x)` | as percentile | p50 |
| `apdex(x, t: T)` histogram | needs-review | `(sum(rate(b{le=T})) + sum(rate(b{le=4T}))) / 2 / sum(rate(_count))`; algebraically `(satisfied + tolerating/2) / total` because buckets are cumulative. Requires bucket bounds at exactly T and 4T (noted). Thresholds scale x1000 for millisecond histograms. Integral bounds match both `le="2"` and `le="2.0"` spellings via regex |
| `apdex()` non-histogram | untranslatable | needs a histogram metric |
| `histogram(x, ...)` | approximate | `sum by (le)(increase(_bucket[$__interval]))` + `panel-hint:heatmap`; Prometheus bucket bounds, not NR's requested buckets |
| `rate(agg, N unit)` | exact-shaped | `sum(rate(m[W])) * seconds(N unit)`; the parser normalizes `1 minute` etc. to seconds |
| `derivative(x, N unit)` counter | approximate | `sum(rate(m[W])) * N` |
| `derivative(x, N unit)` gauge | approximate | `deriv(m[W]) * N` (linear regression) |
| `derivative()` histogram | untranslatable | only _bucket/_sum/_count series exist |
| `predictLinear(x, N unit)` | approximate | `predict_linear(m[W], seconds)`; counter resets skew it (noted needs-review) |
| `stddev(x)` gauge | approximate | per-series `stddev_over_time` (NR computes over all events) |
| `stddev(x)` counter/histogram | untranslatable | needs raw values; no sum-of-squares series |
| `uniqueCount(attr)` | approximate | `count(count by (label)(...))` - distinct label values on series, not event-level uniqueness |
| `agg(x) * N`, `24*7*max(x)` (constants both sides) | preserved | arithmetic between aggregates and constants kept in the expr; `10e8`-style scientific notation parses |
| `agg(x) + agg(y)`, `agg(x) - agg(y)` | approximate | both operands translated in the same context and combined as `(left) + (right)`; a series present in only one operand drops out (note says to add `or vector(0)`). Over `aws.*` metrics this (and `agg(x) / agg(y)`) becomes a CloudWatch Metric Math target: hidden operand queries `m1`, `m2`, ... plus an expression query `m1 + m2` |
| `sum(0)` / `latest(N)` (constant) | exact | `vector(N)` (placeholder widgets) |
| `filter(count(*), WHERE ...) * 100 / count(*)` on `FROM Log` | approximate | LogQL ratio `100 * (left) / (right)` (unit percent) |
| `filter(agg, WHERE c)` | composes | embedded WHERE becomes extra label matchers on that aggregation only |
| `percentage(agg, WHERE c)` | composes | `100 * filtered / unfiltered`; `unit:percent` hint |
| `agg(if(cond, x))` | approximate | rewritten to a filtered aggregation (sound: NRQL aggregations skip NULL). `count(if(c, 1))` and `sum(if(c, 1, 0))` -> filtered `count(*)`; `ELSE 0` accepted for `sum` only |
| `agg(if(cond, x, y))` non-trivial ELSE | untranslatable | the ELSE value enters every non-matching row; split into filtered queries (precise note) |
| bare `if()` in SELECT | untranslatable | wrap in an aggregation |
| `FACET cases(WHERE c1 AS a, ...)` | approximate | one filtered query per case (legend = alias); NR's implicit "Other" bucket is not emitted; degrades to a dropped-grouping note when a case cannot become matchers |
| `funnel(...)` | untranslatable | event-sequence analysis; no metric equivalent (same message for every event type) |
| `eventType()` / `keyset()` / `aggregationEndTime()` | untranslatable | NRDB introspection |
| `agg(x) / agg(y)` | approximate | ratio of two aggregations (classic error-rate shape): each operand is translated in the SAME context (so both carry the shared `FACET` grouping and outer `WHERE`) and divided as `(left) / (right)`; PromQL matches the two vectors on the group labels (a series present in only one operand drops out; the denominator must be non-zero). `count/count`, `rate/rate`, `uniqueCount/...` (count-shaped over count-shaped) hint `unit:percentunit`; other ratios (e.g. `sum/sum`) force no unit. Chained `a/b/c` nests left as `(a/b)/c`. The operands' own unit hints do not carry to the quotient |
| `agg(x) * N`, `N * agg(x)`, `agg(x) / N` | preserved | multiplier kept in the expr; derived unit hint dropped with a note |
| multiple SELECT items | composes | one target per aggregation; non-aggregated items dropped with a note |

## Query clauses

| Clause | Status | Notes |
| --- | --- | --- |
| `WHERE a = / != v` | exact | label matcher (`=~ ${var:regex}` for dashboard variables) |
| `WHERE a = concat('p-', {{env}})` | exact | `a="p-$env"` (shared value renderer); in IN/OR lists -> `=~"p-$env\|q-$env"` |
| `WHERE flag` (bare boolean attr) / `a IS TRUE/FALSE` | exact | `flag="true"` / `="false"` |
| `WHERE appName = 'svc (prod)'` | approximate | `job="svc"` (label via `label_map`); the `(env)` suffix feeds the env variable |
| `WHERE entity.guid = '<GUID>'` | needs-review / exact with `--live` | offline: `[MANUAL]`-style note; `--live` resolves the GUID through NerdGraph (read-only) to `service_name="<entity name>"` (`entity_label`) |
| `` WHERE `backticked.attr` = v `` | exact | backticks accepted everywhere |
| `-- comments` | exact | stripped anywhere in the NRQL |
| two `SELECT`s in one NRQL string | composes | split; the second becomes an extra target |
| `WITH METRIC_FORMAT`, `dateOf/hourOf/weekOf` | note | dropped with a note |
| `WHERE a IN (...)` / `NOT IN` | exact | anchored regex alternation; `IN ({{var}})` -> `=~ ${var:regex}` |
| `WHERE a LIKE p` | exact | `%`/`_` -> `.*`/`.`; case-insensitive `(?i)` to match NRQL LIKE |
| `WHERE a RLIKE p` | exact | passed through as regex matcher |
| `WHERE a IS [NOT] NULL` | exact | label absent / present matcher |
| `WHERE ... AND ...` | exact | matchers conjoin |
| `WHERE a=x OR a=y` (same attr) | exact | merged into one regex matcher `a=~"x|y"` |
| `WHERE` OR across attributes | needs-review | cannot be label matchers; clause DROPPED with an explicit note |
| `NOT (...)` | exact where flippable | matcher ops flip; negated AND (OR in disguise) is dropped with a note |
| numeric comparison on status code | exact | `>= 400` etc. -> status-class regex (`4..|5..`); other numeric label comparisons dropped, needs-review |
| attribute not in `label_map` | approximate | sanitized name used (custom metric dimensions keep their name through OTel/Prom); note says to verify the label exists |
| `WHERE a ... FACET x WHERE b` (two WHERE clauses) | exact | NRQL allows several `WHERE` clauses; they are ANDed (1.11 - earlier versions silently kept only the last) |
| `FACET attr` | exact | `by (label)` grouping + legend |
| `FACET fn(...)` (except cases) | needs-review | no label equivalent; grouping dropped |
| `FACET ... LIMIT n` | approximate | `topk(n, ...)`; per-step evaluation on range queries noted |
| `FACET` without LIMIT | note | NR defaults to top 10; translation returns ALL groups (note suggests topk) |
| `FACET k8s.namespaceName, k8s.deploymentName` | exact | `by (namespace, deployment)` (K8s attribute rule) |
| `FACET aws.<svc>.<Dim>` | approximate | CloudWatch `dimensions[Dim] = ["*"]` + `dimension_keys` |
| `TIMESERIES [AUTO/MAX]` | exact | range query, Grafana picks the step |
| `TIMESERIES <fixed>` | note | Grafana buckets by query interval; note says to set panel Min interval |
| `SLIDE BY n` | approximate | no sliding-window equivalent; honest note (series look more stepped) |
| `SINCE <rel>` | exact | `timefrom:` hint (`now-30m`, `now/d`, ...) consumed by the builder |
| `UNTIL` | needs-review | not expressible per panel; noted |
| `COMPARE WITH <rel>` | approximate | second identically-translated target with PromQL `offset <d>` / LogQL range `offset` (Loki 2.3+, noted needs-review), legend `(<d> earlier)`; month approximated as 30d; dropped with a note for trace search and log-stream panels |
| `LIMIT n` | context | topk (faceted metrics), `maxlines:` hint (log panels), `limit:` hint (trace search) |
| `ORDER BY` | note | not preserved in the query; use panel sorting |
| `WITH TIMEZONE` | note | dropped; set the dashboard timezone |
| `EXTRAPOLATE` | note | dropped (not applicable to metric data) |
| unrecognized tail clauses | needs-review | captured verbatim and reported as DROPPED |
| `SELECT *` (metrics route) | untranslatable | raw event listing; route to Loki/Tempo |

## FROM Log -> LogQL

WHERE predicates split three ways: stream-selector labels (config
`loki_stream_labels`), line filters (predicates on `message`), and
pipeline label filters - structured-metadata labels (config
`loki_metadata_labels`) go before the parser stage, everything else
after `| json` / `| logfmt` (config `loki_parser`) with an
`| __error__=""` guard.

| Construct | Status | Notes |
| --- | --- | --- |
| `SELECT *` / plain attrs | exact | log-stream query + `panel-hint:logs`; column projection not supported (full line shown, approximate); `LIMIT` -> `maxlines:` |
| no stream-label filter | needs-review | emits `{service_name=~".+"}` (scans all streams) with a warning note |
| `message = / LIKE / RLIKE ...` | approximate/exact | line filters `|=`, `|~ "(?i)..."`, `!~`; equality degrades to substring `|=` (noted) |
| `allColumnSearch('t', insensitive: true)` | approximate | `|~ "(?i)t"` (line filter; NR searches all attributes) |
| `log_level = 'ERROR'` (level-like labels) | exact | `log_level=~"(?i)ERROR"` when `loki_case_insensitive_levels` is true (default) |
| stream label `= concat('p-', {{env}})` | exact | `{cluster="p-$env"}` (shared value renderer) |
| `SELECT f1, f2 FROM Log` (projection) | approximate | logs panel with `| json | line_format "{{.f1}} {{.f2}}"` |
| `bytecountestimate()` | approximate | `bytes_over_time` |
| `count(*)` | exact | `sum by (...)(count_over_time(stream [W]))` |
| `rate(count(*), N unit)` | exact | `sum(rate(stream [W])) * N-seconds` |
| `average/sum/max/min(attr)` | approximate | `*_over_time(stream | unwrap field [W]) by (...)`; unwrap assumes a numeric parsed field (noted); explicit `by ()` keeps NR's single-series event-level semantics |
| `percentile/median(attr, p...)` | approximate | `quantile_over_time`; one target per p |
| `latest/earliest(attr)` | approximate | `last_over_time` / `first_over_time` over unwrap |
| `uniqueCount(attr)` | needs-review | `count(sum by (label)(count_over_time(...)))`; cardinality cost noted |
| `percentage(count(*), WHERE c)` | exact-shaped | `100 * filtered / total`; only count(*) supported |
| `filter(agg, WHERE c)` | composes | embedded WHERE merged BEFORE the selector is built, so it can contribute stream labels |
| `agg(if(cond, x))` | approximate | same trivially-a-filter rewrite as metrics |
| `FACET` on non-stream label | needs-review | requires the parser stage; field names must be verified |
| `FACET aparse(message, '%[TOPIC:*]%')` / `capture(message, r'...')` | approximate | `| regexp "(?P<topic>...)"` (NR `%`/`*` wildcards -> regex) + `sum by (topic)` |
| `COMPARE WITH` | needs-review | `offset` on the range vector (Loki 2.3+); dropped for log-stream panels |
| other aggregations | untranslatable | precise per-function message |

## FROM Span (raw) -> TraceQL

| Construct | Status | Notes |
| --- | --- | --- |
| known attrs (`service.name`, `name`, `http.statusCode`, ...) | exact | mapped to TraceQL intrinsics / scoped fields |
| unknown attrs | needs-review | scope-agnostic `.attr`; verify span./resource. scope |
| `error` / `otel.status_code` | exact | `status = error` / `status != error` |
| `duration` comparisons | exact | `duration > 100ms` (ms vs s inferred from the attr name) |
| `LIKE` / `RLIKE` / `IN` | exact | regexes explicitly anchored (`^...$`) because TraceQL regex is UNANCHORED, unlike PromQL |
| `IS [NOT] NULL` | exact | `field = nil` / `!= nil` |
| `FACET` / `COMPARE WITH` | needs-review | not applicable to a trace-search panel; dropped with notes |
| `LIMIT n` | hint | `limit:` note consumed by the builder |

## Output contract: `[MANUAL]` placeholders and the widget report

An `untranslatable` widget becomes a text panel titled
`<title> [MANUAL]` whose body is WHY + the closest equivalent (datasource
and an example query / CloudWatch target) + the original NRQL. Each
`widget-report.json` entry carries `metric_kind`, `missing_datasource`,
`closest_equivalent`, `manual`, `render_vars`, `k8s_mapped` and
`cloudwatch`; `requirements.json` carries `missing_datasources`. See
[live-translation.md sections 9-10](live-translation.md#9-the-manual-placeholder-contract).

## Changed test expectations in the 1.2 fidelity pass

Existing tests were only changed where they asserted objectively wrong
output:

1. `tests/test_translate_metrics.py::test_apdex` - previously asserted
   the 4t bucket matcher as `le="2"` (exact match). Prometheus text
   format renders integral bounds as `le="2"` but OpenMetrics renders
   `le="2.0"`; an exact match silently returns no data on half the
   stacks. The translator now emits `le=~"2|2\.0"` for integral bounds
   and the test asserts the robust form.
2. `tests/test_translate_metrics.py::test_compare_with_noted_as_dropped`
   - removed. It asserted that `COMPARE WITH` was dropped with a note;
   `COMPARE WITH` is now translated as a second identically-translated
   target with a PromQL/LogQL `offset`, which is a sound mapping.
   Replaced by the `COMPARE WITH` tests asserting the offset target.
