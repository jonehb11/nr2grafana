# Translation notes: NRQL -> PromQL / LogQL / TraceQL

Honest support matrix for the nr2grafana 1.11 translation layer
(`nr2grafana/nrql/parser.py` + `nr2grafana/translate/`). Every translated
query carries a confidence level; this document says exactly what each
construct becomes and where the semantics drift.

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
  panel is emitted with the original NRQL preserved and a precise note.

## Event-type routing (FROM ...)

| FROM | Target | Status | Notes |
| --- | --- | --- | --- |
| `Metric` | PromQL | varies | name via `metric_map` config, else heuristics (`_total` -> counter, `_bucket` -> histogram, `histogram()`/`apdex()` arg -> histogram, `percentile()`/`median()` arg -> histogram only when the name looks like a duration/latency histogram else gauge, else gauge); heuristic hits are `needs-review` |
| `Transaction` | PromQL | approximate | OTel semconv `http_server_request_duration_seconds` histogram (legacy flavor and config overrides supported); requires OTel instrumentation |
| `TransactionError` | PromQL | needs-review | approximated as 5xx responses on the same histogram |
| `Span` (aggregations) | PromQL | needs-review | span metrics (`spanmetrics_flavor` config: otel / otel-seconds / tempo / legacy); metric names are deployment-specific |
| `Span` (raw / SELECT *) | TraceQL | approximate | trace search; results differ from raw span listings |
| `Log` | LogQL | varies | see LogQL section |
| `SystemSample` / `NetworkSample` / `StorageSample` | PromQL | exact / approximate | canonical node_exporter expressions (see INFRA_MAP); flagged needs-review because exporter presence is assumed |
| `K8s*Sample` | PromQL | exact-shaped | kube-state-metrics / cAdvisor expressions; same exporter caveat |
| `PageView` / `Browser*` / `JavaScriptError` / `AjaxRequest` | - | untranslatable | names Grafana Faro as the LGTM equivalent |
| `Mobile*` | - | untranslatable | names Grafana Faro |
| `Synthetic*` | - | untranslatable | names blackbox_exporter / Grafana Synthetic Monitoring |
| `NrConsumption` / `NrUsage` / `NrAuditEvent` | - | untranslatable | New Relic-only; needs the NR datasource plugin |
| multiple event types | first only | needs-review | only the first FROM entry is translated; noted |

## Aggregations -> PromQL

| Construct | Status | Translation / notes |
| --- | --- | --- |
| `count(*)` on counter | approximate | TIMESERIES: `sum by (...)(rate(m[$__rate_interval])) * $__interval_ms / 1000` (events per Grafana step); no TIMESERIES: `sum(increase(m[$__range]))`; on `FROM Metric` NR count() counts datapoints, not increase - noted |
| `count(*)` on histogram | exact | same idiom on `m_count` |
| `count(*)` on gauge | needs-review | `count(m)` counts series, not events |
| `sum(x)` counter | exact | same count idiom (`rate * step` / `increase` over the range) |
| `sum(x)` gauge | approximate | `sum(avg_over_time(m[W]))` (current-total semantics) |
| `average(x)` histogram | exact | `sum(rate(_sum)) / sum(rate(_count))` |
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
| `rate(agg, N unit)` | exact-shaped | `sum(rate(m[W])) * seconds(N unit)`; the parser normalizes `1 minute` etc. to seconds. Panel unit: requests/min or /sec (`reqpm` / `reqps`) on HTTP and span metrics, counts/min or /sec (`cpm` / `cps`) elsewhere, bytes/sec (`Bps`) for `rate(sum(<bytes>), 1 second)`, otherwise none. `rate(filter(count(*), WHERE c), …)` keeps the embedded WHERE (hoisted to `filter(rate(…))`) |
| `rate(x)` of anything but `count()` / `sum()` / `filter()` of those | untranslatable | refused with the reason (a rate of an average has no metric form) |
| `derivative(<per-second attribute>)` | untranslatable | a second derivative; PromQL has no sound form — plot the rate itself |
| `uniques(attr)` | approximate | `group by (label)(metric{...})` instant query rendered as a table (`panel-hint:table`); one row per label value seen in the range |
| `latest(<string attribute>)` on Transaction (`latest(host)`) | approximate | the label values seen in the range as a table (noted: every value, not only the latest); attributes with no label say so and point at Loki / span events |
| `predictLinear(average(duration), N unit)` on a histogram | approximate | `predict_linear((<average>)[$__range:], seconds)` — a subquery over the range |
| `bucketPercentile(x, p)` | approximate | `histogram_quantile(p/100, …)` like `percentile()` |
| `getCdfValue(x, v)` | approximate | share of observations ≤ v: `_bucket{le="v"}` over `_count` (`percentunit`) |
| `round`, `abs`, `floor`, `ceil`, `sqrt`, `exp`, `ln`/`log`, `log10`, `log2`, `clamp_max`, `clamp_min`, `pow`, `mod` around an aggregation | composes | the PromQL function around the translated expression (`round(expr, 0.01)` for two decimals; `pow` / `mod` as `^` / `%`) |
| `derivative(x, N unit)` counter | approximate | `sum(rate(m[W])) * N` |
| `derivative(x, N unit)` gauge | approximate | `deriv(m[W]) * N` (linear regression) |
| `derivative()` histogram | untranslatable | only _bucket/_sum/_count series exist |
| `predictLinear(x, N unit)` | approximate | `predict_linear(m[W], seconds)`; counter resets skew it (noted needs-review) |
| `stddev(x)` gauge | approximate | per-series `stddev_over_time` (NR computes over all events) |
| `stddev(x)` counter/histogram | untranslatable | needs raw values; no sum-of-squares series |
| `uniqueCount(attr)` | approximate | `count(count by (label)(...))` - distinct label values on series, not event-level uniqueness |
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
| `SELECT agg(alias) FROM (SELECT agg2(...) AS alias FROM X ... FACET a [, b]) [WHERE alias > n] [FACET a] [TIMESERIES]` (nested query) | approximate | the inner query is one vector per facet value; the outer aggregation folds it: `avg(sum by (instance)(...))`, `count((...) > 100)`, `quantile(0.95, ...)`, `sum(a) / sum(b)`; an outer FACET must name inner facets (`avg by (http_route)(...)`); LogQL likewise (no `percentile`); a nested query inside a nested query, `uniqueCount()` outer, or an inner query without FACET is refused with the reason |
| `WHERE a = / != v` | exact | label matcher (`=~ ${var:regex}` for dashboard variables) |
| `WHERE a IN (...)` / `NOT IN` | exact | anchored regex alternation; `IN ({{var}})` -> `=~ ${var:regex}` |
| `WHERE a LIKE p` | exact | `%`/`_` -> `.*`/`.`; case-insensitive `(?i)` to match NRQL LIKE |
| `WHERE a RLIKE p` | exact | passed through as regex matcher |
| `WHERE a IS [NOT] NULL` | exact | label absent / present matcher |
| `WHERE ... AND ...` | exact | matchers conjoin |
| `WHERE a=x OR a=y` (same attr) | exact | merged into one regex matcher `a=~"x|y"` |
| `WHERE` OR across attributes | needs-review | cannot be label matchers; clause DROPPED with an explicit note |
| `NOT (...)` | exact where flippable | matcher ops flip; negated AND (OR in disguise) is dropped with a note |
| numeric comparison on status code | exact | `>= 400` etc. -> status-class regex (`4..|5..`); other numeric label comparisons dropped, needs-review |
| attribute not in `label_map` | needs-review | sanitized name used; note says to verify the label exists |
| `FACET attr` | exact | `by (label)` grouping + legend |
| `FACET fn(...)` (except cases) | needs-review | no label equivalent; grouping dropped |
| `FACET ... LIMIT n` | approximate | `topk(n, ...)`; per-step evaluation on range queries noted |
| `FACET` without LIMIT | note | NR defaults to top 10; translation returns ALL groups (note suggests topk) |
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


## 1.11 additions

### Parser

| Construct | Status | Notes |
| --- | --- | --- |
| arithmetic in SELECT: `(a / b) * 100`, `100 * a / b`, `a - b`, `average(x * 1000)` | composes | scale factors → multiplier (unit rescaled: s×1000 → ms, ratio×100 → percent, bytes/1024 → kbytes, ...); `agg/agg` → ratio; other arithmetic → PromQL/LogQL arithmetic between the translated aggregations |
| arithmetic in WHERE: `duration * 1000 > 500` | folds | `(duration, >, 0.5)` |
| `--` / `/* */` comments | exact | stripped outside strings |
| `WITH expr AS alias` | exact | substituted before translation |
| `apdex(x, t:0.3)` (no space) | exact | previously silently used t=0.5 |
| `WHERE error` (bare boolean) | exact | `error = true` |
| subqueries | untranslatable | precise message |

### WHERE

| Construct | Status | Notes |
| --- | --- | --- |
| OR across different attributes (PromQL) | approximate | DNF (≤ 8 branches); each range/instant selector becomes `(f(m{A}[W]) or f(m{B}[W]))` — series are deduplicated by label set before aggregation, so nothing is double counted |
| OR across different attributes (LogQL) | approximate / needs-review | shared stream labels stay in the selector, the rest becomes `\| json \| a="x" or b="y"`; an OR spanning stream labels uses the all-streams selector (noted); an OR mixing `message` predicates is dropped (noted) |
| `NOT (a AND b)` | approximate | De Morgan → union |
| `count(*)` / `filter(count(*))` / `percentage(count(*))` / `FACET cases()` with `duration > T` on a histogram source | needs-review | bucket arithmetic `_count − _bucket{le="T"}`, bands `_bucket{le=hi} − _bucket{le=lo}`; exact only with a bucket boundary at T |
| numeric comparison on other attributes | needs-review | dropped with a note (PromQL); LogQL: `\| json \| field > n` |
| `numeric(x)`, `string(x)`, `toLower(x)`, `toUpper(x)`, `cast(x, ...)` | exact | unwrapped; case functions → `(?i)` |
| `transactionType = 'Web'` | exact | implicit on HTTP server metrics, dropped |
| `transactionType = 'Other'` | untranslatable | non-web work has no HTTP metric |
| attribute = attribute | needs-review | dropped, noted |

### FACET

| Construct | Status | Notes |
| --- | --- | --- |
| `concat(a, ':', b)` | approximate | `by (a, b)`, legend `{{a}}:{{b}}` |
| `capture(attr, r'(?P<name>...)')` / `aparse(attr, 'a/*/b')` | approximate | `label_replace(expr, "name", "$N", label, regex)` + `by (name)` |
| `if(cond, 'a', 'b')` | approximate | two filtered targets (like `cases`) |
| `hourOf/dateOf/weekdayOf/...(timestamp)`, `toDatetime(timestamp, fmt)` | needs-review | grouping dropped; the note names the panel interval to use instead (`toDatetime`: the finest field of the format) |
| `string(x)` etc. | exact | unwrapped |
| `lower(x)` / `upper(x)` | approximate | grouped by the label; noted that label values keep their case |
| `cases(...) OR 'other'` | approximate | the cases become targets; the catch-all bucket is noted, not emitted |

### FROM Metric — built-in names

| Name | Translation |
| --- | --- |
| `apm.service.transaction.duration` | HTTP server histogram (count/sum/percentile) |
| `apm.service.error.count`, `apm.service.transaction.error.count` | HTTP server `_count` with 5xx matcher (needs-review) |
| `apm.service.datastore.operation.duration` / `apm.service.external.host.duration` | `db_client_operation_duration_seconds` / `http_client_request_duration_seconds` (needs-review) |
| `apm.service.cpu.usertime.utilization`, `apm.service.memory.physical`, `apm.service.instance.count`, `apm.service.gc.time`, `apm.service.memory.heap.*`, `apm.service.thread.count` | OTel process/JVM metrics (needs-review) |
| `apm.service.overview.web` / `.other` | untranslatable (segment breakdown) |
| `newrelic.goldenmetrics.apm.application.throughput` / `responseTimeMs` / `errorRate` | derived from the HTTP histogram (per minute / ms / percent) |
| `newrelic.goldenmetrics.infra.host.*` | node_exporter templates |
| `newrelic.timeslice.value` + `metricTimesliceName` | untranslatable unless `metric_map` has the timeslice name |
| `host.*`, `k8s.<entity>.*` | alias the SystemSample/StorageSample/NetworkSample/K8s*Sample tables |
| `aws.<namespace>.<Metric>` | YACE: `aws_<ns>_<snake_metric>_<statistic>` (statistic from the aggregation), dimensions → `dimension_<Name>`, `aws.accountId` → `account_id` (needs-review) |
| OTel semconv (`http.server.request.duration`, `db.client.operation.duration`, `system.cpu.utilization`, `jvm.*`, ...) | typed with units |
| `getField(m, count|sum|max|min|average)` | the corresponding aggregation |
| `WHERE metricName = 'x'` with `count(*)` | selects the metric |
| unmapped `sum(x)` | counter (`increase`) with `_total` per config, needs-review |

### Infra events (`SystemSample`, `NetworkSample`, `StorageSample`, `ProcessSample`, `ContainerSample`, `K8s*Sample`)

About 200 attributes are mapped onto node_exporter, process-exporter,
cAdvisor, kube-state-metrics and kubelet metrics (see
`translate/nrmetrics.py`); every SELECT item is translated (one target
each); `count(*)` / `uniqueCount(<entity>)` become the entity population
(`count(kube_pod_info)`, `count(node_uname_info)`, ...); pod `status`
filters/facets select `kube_pod_status_phase{phase=...}`; `latest(status)
FACET podName` → `max by (pod, phase)(kube_pod_status_phase == 1)`;
`cpuCoresUtilization` / `memoryWorkingSetUtilization` join usage with
limits `on (namespace, pod, container)`. Unknown attributes are
untranslatable with the exact `metric_map` key to add. Control-plane
samples, `K8sEvent`, `InfrastructureEvent`, `Deployment`, `NrAiIncident`
name their LGTM equivalent (control-plane metrics, Loki, annotations,
Grafana Alerting).

### Transaction attributes

`duration`/`totalTime`/`webDuration` → HTTP server histogram;
`databaseDuration` → DB client histogram; `externalDuration` → HTTP
client histogram; `databaseCallCount`/`externalCallCount` → their
`_count`; `latest(timestamp)` → `max(timestamp(_count)) * 1000`
(dateTimeAsIso); anything else is untranslatable with the reason (it used
to become the HTTP histogram silently).

### Logs / traces

`latest(message)` → logs panel limited to one line; several aggregations →
one target each; `agg/agg` and `agg ± agg` preserved as LogQL arithmetic;
`event_map` custom events → Loki with stream labels.
`otel.status_code = 'ERROR'` → `status = error` (was inverted);
`uniqueCount(trace.id)` → TraceQL metrics root-span count;
`span_aggregations: "traceql"` → TraceQL metrics for every aggregation.

### Time

`SINCE a UNTIL b` (relative) → `timeFrom = a − b`, `timeShift = b`
(exact); `SINCE last week` → `now-1w/w`, `last month` → `now-1M/M`;
`SINCE yesterday UNTIL today` (and `last week UNTIL this week`, `last
month UNTIL this month`, `last year UNTIL this year`) → `timeFrom now/d` +
`timeShift 1d/d` (the whole previous unit, exact); `SINCE {{n}} minutes
ago` → a per-panel `timeFrom now-${n}m`.

### Iteration 2 — semantic quirks found by auditing the emitted queries

Every emitted PromQL / LogQL / TraceQL expression of a 256-query corpus
and the fixture dashboards is now parsed and type-checked by the real
engines (`promtool check rules`, `logcli fmt`, a local Tempo) before a
release; the pass found no syntax problems but the semantic review found
these, all fixed:

| Construct | Before | Now |
| --- | --- | --- |
| `count(*)` / `sum(counter)` with TIMESERIES | `sum(increase(m[$__rate_interval]))` — over-counts by `rate_interval / interval` (25 % at a 15 s scrape and a 1 m step) | `sum(rate(m[$__rate_interval])) * $__interval_ms / 1000` = events per Grafana step, NR's per-bucket count; the factor cancels in `percentage()`, `filter()/count()` and `sum()/count()` ratios; instant queries keep `increase(m[$__range])` |
| `TIMESERIES 5 minutes` | note only | panel `interval` = `5m` (min interval), so buckets match NR |
| `host LIKE '%{{host}}%'`, `appName = 'prod-{{svc}}'`, `IN ('a-{{env}}', 'b')`, `RLIKE 'x-{{v}}.*'` | the braces were regex-escaped and matched literally (no data) | `=~".*${host:regex}.*"`, `=~"prod-${svc:regex}"`, ... (the variable stays a variable; multi-value selections work) |
| `FACET {{attr}}` | `by (__attr__)` | `by ($attr)`, legend `{{$attr}}` |
| TraceQL `http.statusCode = '500'`, `IN (500, 502)`, `LIKE '5%'` | string / regex comparisons that never match an int attribute | `= 500`, `(f = 500 \|\| f = 502)`, `(f >= 500 && f < 600)`; other int attributes with LIKE are dropped with a note |
| span metrics `span.kind = 'server'` | `span_kind="server"` | `span_kind="SPAN_KIND_SERVER"` (the enum name both the OTel connector and Tempo emit) |
| Loki `level = 'ERROR'` (also `severity`, `detected_level`, ...) | case-sensitive | `level=~"(?i)ERROR"`, noted |
| `K8sContainerSample` / `K8sNodeSample` utilization joins | `... / on (...) kube_pod_container_resource_limits{...}` — fails with *many-to-many matching* when kube-state-metrics runs two replicas | right side reduced with `max by (namespace, pod, container)(...)` / `max by (node)(...)` |
| `viz.line` / `viz.area` / `viz.stacked-bar` / `viz.sparkline` / `viz.scatter` whose NRQL lacks `TIMESERIES` | instant query → a single point | translated as a range query (approximate, noted); a `viz.billboard` with `TIMESERIES` gets a stat sparkline |
| `viz.heatmap` with `FACET` (no `histogram()`) | targets forced to `format: heatmap` with a `{{le}}` legend (empty) | series kept as rows (Grafana's time-series-buckets layout); only `histogram()` targets use `le` buckets |
| `SINCE today` / `yesterday` / `this week` on one widget | no panel override (only `now-<n>` forms) | `timeFrom: now/d`, `now-1d/d`, `now/w` (Grafana's own relative syntax) |
| `yAxisLeft.zero`, `facet.showOtherSeries`, `viz.billboard-comparison` | ignored | `min: 0`; note that topk has no 'Other' bucket; note on the percent-change badge |
| NRQL dashboard variables | always `label_values(label)` on Prometheus | routed by FROM: Prometheus `label_values(<metric>{<WHERE>}, label)` (HTTP histogram `_count`, span-metrics calls, the `metricName`, the infra entity metric, `aws_<ns>_info`), Loki `label_values({<stream>}, label)`, Tempo tag values (`resource.service.name`); `SELECT count(*) ... FACET attr` variables work too |
| `ComputeSample` / `DatastoreSample` / `QueueSample` / `LoadBalancerSample` / `BlockDeviceSample` / `ServerlessSample` / `StreamSample` / `CdnSample` / `DnsSample` / `ApiGatewaySample` (API-polling AWS integrations) | untranslatable | `provider.<Metric>.<Statistic>` + `WHERE provider = '<type>'` → YACE `aws_<namespace>_<metric>_<statistic>` (needs-review: NR camel-cases the CloudWatch name; pin with `metric_map` key `"<Event>.provider.<Metric>.<Stat>"`), dimensions → `dimension_<Name>`, `awsRegion` → `region`, `label.<Tag>` → `tag_<Tag>`, `count(*)` / `uniqueCount(<id>)` → `aws_<ns>_info` series; without `provider` the message lists the known types |

### Iteration 3 — a second corpus (339 queries) and the produced dashboards

| Construct | Before | Now |
| --- | --- | --- |
| `round(x, n)`, `abs`, `floor`, `ceil`, `sqrt`, `exp`, `ln`/`log`, `log10`, `log2`, `clamp_max`, `clamp_min`, `pow`, `mod` around an aggregation (or arithmetic over aggregations) | untranslatable | the PromQL function around the translated expression (`round(expr, 0.01)` for two decimals; `pow`/`mod` as `^`/`%`); LogQL drops `round()` with a note (no rounding function) and refuses the others |
| `uniques(attr)` | untranslatable | `group by (label)(metric{...})` instant query, rendered as a table (one row per value) |
| `latest(<string attribute>)` on Transaction | empty reason | says the attribute is a string with no metric and points at Loki / span events |
| `percentile(x, {{pct}})`, `LIMIT {{limit}}`, `WHERE metricName = '{{metric}}'`, `average({{metric}})`, `SINCE {{since}}`, `TIMESERIES {{interval}}` | silently p95 / parse error / dropped / `_metric_` | `histogram_quantile($pct / 100, ...)`, `topk($limit, ...)`, `$metric` as the metric name (needs-review: the variable must hold Prometheus names), panel `timeFrom: $since`, panel `interval: $interval` (each noted with what the variable must contain) |
| `duration > {{threshold}}` | "not numeric; dropped" + a misleading label note | one note explaining that a bucket cannot be chosen from a variable |
| `FACET if(error, 'a', 'b')` (bare boolean) | grouping dropped | two filtered targets like `if(error IS TRUE, ...)` |
| `FACET cases(...), name` | grouping dropped | one filtered target per case, each grouped by the other FACET attributes (legend `case {{label}}`) |
| `rate(sum(<histogram>), 1 minute)` | rate of `_count` | rate of `_sum` (time spent per minute) |
| unknown `*.count` / `*.sum` metric names (`process.runtime.jvm.threads.count`, `custom.count`) | treated as a histogram's `_count`/`_sum` | a gauge keeping its name unless the stem looks like a duration histogram; the older `process.runtime.jvm.*` names are known |
| `WHERE metricName = 'x'` next to `SELECT agg(x)`, `WHERE x IS NOT NULL` | leaked as label matchers | consumed (a different metricName is noted); `uniqueCount(metricName) WHERE metricName LIKE 'custom.%'` → `count(count by (__name__)({__name__=~"custom_.*"}))` |
| `sum()` of a CloudWatch `Sum`/`SampleCount` statistic (YACE `*_sum`, legacy `provider.*.Sum`) | `sum(avg_over_time(...))` (per-period average) | `sum(sum_over_time(m[$__interval]))` — adds the datapoints, as NR does |
| span metrics `otel.status_code = 'ERROR'` inside `percentage()`/`filter()` | `status_code="ERROR"` | `status_code="STATUS_CODE_ERROR"` |
| `count(*) / 60`, `sum(bytes) / 1024 / 1024` | `* 0.016667`, `* 0.000001` (rounded) | `/ 60`, `/ 1048576` (exact) |
| LogQL `sum(x)` | `sum_over_time(...) by (...)` — Loki rejects grouping on `sum_over_time` | `sum by (...)(sum_over_time(...))` |
| LogQL OR mixing `message` predicates with attribute filters | the whole WHERE dropped (all streams) | the shared stream selector is kept; only the OR is dropped (noted) |
| LogQL `FACET capture(message, r'(?P<x>...)')` | `label_replace` note + `\| json` | `\| regexp "..."` parser stage extracting the label |
| LogQL `FACET cases(...)` / `if(...)` | grouping dropped | one filtered target per case |
| `WHERE timestamp > n` (logs) | `\| json \| timestamp > n` | dropped: the time picker selects the period |
| TraceQL `name LIKE '%{{op}}%'`, `service.name = 'prod-{{svc}}'` | braces escaped literally | `${op:regex}` / `$svc` inside the pattern |
| TraceQL `parentId IS NULL`, `nr.entryPoint IS TRUE` | `.parentId = nil`, `.nr.entryPoint = true` (no such attributes) | `nestedSetParent < 0` (root spans); `IS NOT NULL` → `>= 0` |
| `FROM DistributedTraceSummary` aggregations | span metrics with a `root_entity_name` label that does not exist | TraceQL metrics over root spans (`{ nestedSetParent < 0 && resource.service.name = "x" } \| count_over_time()`), `avg_over_time`/`min`/`max`/`sum` noted with the Tempo version they need |
| `K8sJobSample`, `K8sCronjobSample`, `K8sNamespaceSample` (cpu/memory/pods), `K8sNodeSample.runningPods`, `ContainerSample.memoryLimitBytes` | untranslatable | kube-state-metrics (`kube_job_*`, `kube_cronjob_*`, `kube_pod_container_resource_*` summed per namespace), kubelet (`kubelet_running_pods`), cAdvisor |
| on-host integration samples (`NginxSample`, `ApacheSample`, `MysqlSample`, `PostgresqlDatabaseSample`/`InstanceSample`, `RedisSample`/`RedisKeyspaceSample`, `KafkaOffsetSample`/`TopicSample`, `ElasticsearchClusterSample`/`NodeSample`, `RabbitmqQueueSample`/`NodeSample`, plus Mongo, Memcached, HAProxy, Cassandra, Consul, MSSQL, Oracle, JMX, Flex, ...) | "custom or unknown event type" | the common attributes map to the matching exporter's metrics (nginx-prometheus-exporter, apache_exporter, mysqld_exporter, postgres_exporter, redis_exporter, kafka_exporter, elasticsearch_exporter, the RabbitMQ prometheus plugin); every other attribute names the exporter to rebuild on |

The produced dashboards were audited as JSON (grid overlaps, panel
datasources, `$variables` used vs. defined, legends on grouped queries,
`interval`/`timeFrom` syntax, units on duration panels): no findings
beyond the translations above.

### Iteration 4 — a third corpus (205 shapes) and every widget-configuration knob

| Construct | Before | Now |
| --- | --- | --- |
| `percentile(x * 1000, 50, 95)` | only the first percentile target was scaled | every target carries the SELECT multiplier |
| `predictLinear(average(duration), 1 hour)` on a histogram | untranslatable | `predict_linear((<average>)[$__range:], 3600)` (a subquery over the range) |
| `bucketPercentile(x, 95)`, `getCdfValue(x, 0.5)` | unsupported | `histogram_quantile(0.95, …)`; share of observations ≤ 0.5 (`_bucket{le}` / `_count`, percentunit) |
| `rate(uniqueCount(x), 1 minute)` | rendered as the request rate | refused with the reason (only `rate(count())` / `rate(sum())` map) |
| `derivative(<per-second attribute>)` | rendered as the rate itself | refused (a second derivative has no sound PromQL form) |
| `FACET … ORDER BY x ASC LIMIT n` | `topk` | `bottomk` |
| `FACET cases(…) … COMPARE WITH` | a comparison target without the case filter was still emitted | not emitted (the note already said so) |
| `FACET if(c1, 'a', if(c2, 'b', 'c'))` | second target labelled `false` | one target per branch: `c1`, `!c1 ∧ c2`, `!c1 ∧ !c2` (PromQL and LogQL) |
| `host IS NOT NULL`, `duration IS NOT NULL`, `error IS NOT NULL` on Transaction | `duration!=""`, `error!=""` label matchers | dropped (every request carries them); duplicate matchers collapse |
| `uniqueCount(name)` / `uniques(name)` on Transaction | `span_name` | `http_route` (consistent with FACET name) |
| `latest(host)` and other label attributes on Transaction | untranslatable | the label values seen in the range (a table; noted as every value, not only the latest) |
| `LIKE '%\\_%'`, `LIKE '50\\%'` | the escape became a regex `\\` | `\\_` / `\\%` are literal `_` / `%` |
| `WHERE true`, `AND false`, `1 = 1` | parse error / matcher on a label named `true` | no filter (a constant false predicate is noted) |
| `AS "Total"`, `name = "quoted"` | parse error | double-quoted strings accepted |
| `bytecountestimate()` | generic "unsupported" | NRDB introspection, refused with the reason |
| `FACET status` on `K8sPodSample` | `kube_pod_info` grouped by a `phase` label it does not have | `sum by (phase)(kube_pod_status_phase == 1)` |
| `restartCount` on container samples | an expression template (rate/COMPARE WITH impossible) | a counter: `latest()` is the count, `rate(sum(restartCount), 1 hour)` → restarts per hour, COMPARE WITH offsets |
| COMPARE WITH on derived infra expressions without a range selector | the comparison target repeated the current values | instant selectors get the `offset` (or the note says it could not be applied) |
| `rate(sum(x), 1 second)` on infra events | "needs an attribute argument" | nested aggregations descend to the attribute |
| `AwsLambdaInvocation` / `AwsLambdaInvocationError` (the Lambda layer) | unknown event | `aws_lambda_invocations_sum` / `aws_lambda_errors_sum` (`sum_over_time` per step), `duration` → `aws_lambda_duration_average` |
| `NrIntegrationError` | "custom or unknown" | named as New Relic ingest data |
| LogQL `message != ''` / `message IS NOT NULL` | `!= ""` (which excludes every line) | dropped with a note |
| LogQL `FACET aparse(message, 'user=* %')` | `\| regexp "user=(.*) .*"` (Loki requires a named group) | `\| regexp "user=(?P<aparse>.*) .*"` |
| `kubernetes.pod_name` / `namespace_name` / `container_name` / `node_name` / `cluster_name` log attributes | parsed fields | the `pod` / `namespace` / `container` / `node` / `cluster` stream labels |
| `viz.table` / `viz.pie` / `viz.bar` / `viz.bullet` with `TIMESERIES` | a range query behind a pie or table (one slice or row per bucket) | instant over the range, noted |
| widget `nullValues` | ignored | `preserve` → `spanNulls`; `zero` → a note (PromQL returns no sample; `or vector(0)`) |
| widget `colors.seriesOverrides`, `yAxisRight.series` | ignored | fixed-colour and right-axis field overrides by series name |
| widget `linkedEntityGuids`, `refreshInterval` | ignored | a panel link to the New Relic entity; the smallest interval becomes the dashboard refresh |
| widget `platformOptions.ignoreTimeRange` | the SINCE only voted for the dashboard range | the SINCE is always a panel time override (Grafana's picker does not affect it) |

### Iteration 5 — New Relic's own quickstart shapes and alternative configurations

A fourth corpus modelled on New Relic's APM / Kubernetes / Infra / Logs /
AWS quickstart dashboards, plus the whole harness re-run under alternative
configurations (`http_metrics_flavor: legacy`, `spanmetrics_flavor: tempo`,
`loki_parser: logfmt`, `span_aggregations: traceql`, `event_map`,
`metric_map` templates). All parse; the semantic findings:

| Construct | Before | Now |
| --- | --- | --- |
| `response.status >= '500'` (a numeric comparison spelled as a string) | dropped | the 5xx class matcher, like `>= 500` |
| `error.expected IS FALSE` on `TransactionError` | `error_expected="false"` label | dropped with a note (every 5xx counts) |
| `average(databaseCallCount)` / `average(externalCallCount)` | DB calls per second | DB (or HTTP client) operations divided by HTTP server requests: calls per request |
| `count(<gauge metric>)` on `FROM Metric` | `count(m)` (the number of series) | `sum(count_over_time(m[$__interval]))` — datapoints per step |
| `metric_map` string entries ending in `_total` | treated as gauges | counters |
| `collector.name`, `instrumentation.provider`, `newrelic.source`, ... in a metric WHERE | label matchers that match nothing | dropped with a note (New Relic ingest metadata) |
| `tags.<Key>` on `aws.*` metrics | `tags_Key` | YACE's `tag_Key` |
| `uniqueCount(aws.ec2.InstanceId)` | a metric named `aws_ec2_instance_id_average` | `count(count by (dimension_InstanceId)(aws_ec2_info{...}))` |
| `FROM Metric SELECT uniqueCount(k8s.podName) WHERE k8s.pod.status = 'Pending'`, `latest(k8s.pod.status)`, `uniqueCount(k8s.nodeName)`, ... | garbage metric names | the same translations as the sample events (`kube_pod_status_phase`, `kube_node_info`, ...) |
| `WHERE isReady = 0` / `isScheduled = 0` (metric-valued attributes) on infra events | `isReady="0"` label matchers | dropped with a note naming the metric to join on |
| `status = 'Waiting' AND reason = 'CrashLoopBackOff'` on containers; `latest(reason)` | `kube_pod_container_status_waiting{reason=...}` (no such label) / untranslatable | `kube_pod_container_status_waiting_reason{reason=...}`; `latest(reason)` lists the active waiting/terminated reason per container |
| `rate(sum(<derived per-second attribute>), 1 minute)`, `rate(sum(restartCount), 1 hour)` on pods | unscaled / untranslatable | scaled by the unit; counters inside templates get `rate()` applied in place |
| `span_aggregations: "traceql"` with an aggregation TraceQL metrics cannot express (`percentage()`) | untranslatable | span metrics in Mimir with a note |
| `entityGuid`, `containerId` / `container.id` | unmapped | default label map entries |

### Iteration 6 — clause, time, alias and unit edge shapes

A fifth corpus (214 queries: time keywords and `UNTIL` pairs, FACET
functions, aliases next to FACET, `rate()` periods, `IS NULL` checks,
quoting, every place a `{{variable}}` can appear). All parse; the semantic
findings:

| Construct | Before | Now |
| --- | --- | --- |
| `rate(count(*), 1 minute)` / `1 second` | panel unit `short` | `reqpm` / `reqps` on HTTP and span metrics, `cpm` / `cps` on other counters and logs; `rate(sum(<bytes>), 1 second)` → `Bps` |
| `rate(filter(count(*), WHERE error IS TRUE), 1 minute)` | the embedded WHERE silently dropped (PromQL and LogQL) | hoisted to `filter(rate(count(*)), WHERE …)`: the 5xx matcher (or Loki filter) is kept |
| `rate(sum(bytes), 1 second)` on logs | the line rate | `sum(rate({…} \| unwrap bytes [$__auto]))` (bytes per second) |
| `SELECT average(duration) AS 'Avg', percentile(duration, 95) AS 'p95' … FACET name` | every target legend `{{http_route}}` (indistinguishable series) | `{{http_route}} Avg`, `{{http_route}} p95`; without aliases the NRQL expression names the target (`average(totalTime)`) |
| `SINCE yesterday UNTIL today` (and the week / month / year pairs) | `timeFrom now-1d/d` (yesterday **and** today so far) plus a "cannot be expressed" note | `timeFrom now/d` + `timeShift 1d/d`: exactly the whole previous day |
| `SINCE {{n}} minutes ago` | "could not be mapped" | per-panel `timeFrom now-${n}m` (the validator accepts the form) |
| `FACET toDatetime(timestamp, 'yyyy-MM-dd')` | grouped by a `timestamp` label | a time-bucketing FACET like `dateOf()`: dropped with the interval to use |
| `FACET lower(name)` | silent | noted that Prometheus label values keep their case |
| `FACET cases(…) OR 'slow'` | `OR 'slow'` reported as not understood | parsed; the catch-all bucket is noted as not emitted |
| `average(duration) FACET cases(WHERE duration < 1 AS 'fast', WHERE duration >= 1 AS 'slow')` | two identical unfiltered targets labelled fast / slow | grouping dropped with the reason (only `count(*)` can be split at a bucket boundary) |
| `httpResponseCode >= 400 AND httpResponseCode < 500` | two overlapping regex matchers (`4..\|5..` and `[1234]..`) | one intersected band matcher (`4..`; `>= 300 AND < 500` → `[34]..`) |
| `count(*) / uniqueCount(host)`, `uniqueCount(a) / uniqueCount(b)` | `percentunit` | no unit (a per-entity number); `filter(count(*), …) / count(*)` and `errors / requests` stay proportions |
| `transactionType = 'Web'`, `error.expected IS FALSE`, `FACET transactionType` | a "not in label_map" review note next to the note that explains the drop | only the explaining note (default label-map entries) |
| `duration IS NOT NULL`, `name IS NOT NULL`, `error IS NOT NULL` on Transaction | a "not in label_map" review note | dropped as always true (approximate); `databaseDuration IS NOT NULL` is refused with the reason (the histogram cannot tell transactions with DB calls apart) |
| `WHERE appName = 'x' AND {{filter}}` (a variable standing for a whole condition) | `$filter="true"` label matcher | dropped with a note |
| `earliest(cpuPercent)` on infra samples | silently the current value | refused (no `first_over_time` in PromQL), like `FROM Metric` |

### Iteration 7 — the export path against a real Grafana 12.1 stack

The converted fixtures were exported to a local Grafana 12.1 with
Prometheus 2.53, Loki 3.1 and Tempo 2.7 datasources (synthetic OTel / node /
kube-state metrics and JSON logs), validated with `--test`, read back, and
every dashboard rendered in headless Chromium. All eleven dashboards were
created, verified and rendered with no panel errors. What the live run
changed:

| Construct | Before | Now |
| --- | --- | --- |
| `validate --test` / `export --test` on a TraceQL search panel | `error` — Grafana's Tempo backend refuses TraceQL searches on `/api/ds/query` ("backend TraceQL search queries are not supported"; the browser runs them) | the search runs through Tempo's `/api/search` via the datasource proxy and is classified `data` / `no-data` by the traces returned |
| a single un-aliased aggregation without FACET (`SELECT count(*) …`) | Grafana's `__auto` legend, which shows the whole PromQL / LogQL for label-less results | the series is named the way New Relic did: the NRQL expression (`count(*)`, `rate(count(*), 1 minute)`, `average(duration)`) or the alias; COMPARE WITH targets become `count(*) (1w earlier)` |
| value columns of a table built from instant queries | `Value`, `Value #A`, `Value #B` | the alias or the aggregation (`Avg`, `p95`, `count(*)`) through the `organize` transformation |
| `export` on a uid that already exists | Grafana's misleading "The dashboard has been changed by someone else" (HTTP 412) | "a dashboard with uid … / title … already exists in Grafana folder …; re-run with --overwrite" |

Verified unchanged on the live stack: datasource variables are pinned to the
chosen instances on export, the `Migrated from New Relic dashboard …`
provenance survives in the description and tag (Grafana 12 drops unknown
top-level keys, so the `nr2grafana` block is only kept in the local JSON and
`*.export-results.json`), collapsed rows, stat / gauge / bargauge / pie /
table / heatmap / logs / text panels, and panel time overrides (`Last 1 day`
etc.) all render. The `PromQL info: input to histogram_quantile needed to
be fixed for monotonicity` notice Prometheus attaches to a
`histogram_quantile` over non-monotonic synthetic buckets appears as a
panel warning; it comes from the data, not the query.

### Iteration 8 — a crafted edge dashboard on the live stack, nested queries

A dashboard exercising every time/variable/mixed-datasource shape was
exported to the local Grafana 12.1 and rendered; the NRQL parser and
translator gained nested queries. Findings:

| Construct | Before | Now |
| --- | --- | --- |
| `TIMESERIES {{interval}}` with a New Relic ENUM/STRING variable whose values are `1 minute` / `5 minutes` | the panel failed in Grafana ("Invalid interval string") | the variable's values are rewritten to Grafana spans (`1m`, `5m`) when the variable feeds a panel interval; `SINCE {{since}}` values (`1 hour ago`, `today`) become `now-1h` / `now/d`; plain numbers (`now-${n}m`) stay |
| `SINCE yesterday UNTIL today` in a dashboard with other ranges | the `now/d` half could win the dashboard default range, leaving the panel with only `timeShift 1d/d` (correct only for a sub-day dashboard range) | the panel always carries both `timeFrom now/d` and `timeShift 1d/d`; calendar ranges no longer vote for the dashboard default |
| a table mixing units (`average(duration)`, `count(*)`) | the count column showed seconds: `byFrameRefID` unit overrides do not survive the merge transformation | unit overrides match the renamed column (`byName`) |
| `FACET name AS 'Endpoint'` in a table | column `http_route` | column `Endpoint` |
| `SELECT average(c) FROM (SELECT count(*) AS c FROM Transaction FACET host)` and the other nested shapes (`count(*) ... WHERE c > 100`, `percentile(c, 95)`, `sum(errors) / sum(total)`, outer `FACET`, `LIMIT`) | "nested subquery has no equivalent" | translated (see the clause table); refused with the reason when the shape has no vector-aggregation form |
| a trailing `;` | parse error | tolerated |

Verified on the live stack: `now-${n}m` and `$interval` overrides, the
`-- Mixed --` panel datasource for a widget with a Loki and a Prometheus
query, `FACET cases(...)` stacked bars, billboard thresholds and
percent-change comparisons, logs / pie / gauge / bargauge / heatmap /
markdown panels, and the query-variable `label_values(...)` definitions
all render without panel errors.

### Iteration 9 — histogram components, cumulative counters, Micrometer names

A ninth batch of realistic dashboard queries (APM tables, infra ratios,
Kubernetes reasons, Kafka/AWS/JVM metrics, Loki fields, span metrics):

| Construct | Before | Now |
| --- | --- | --- |
| `sum(x.count)`, `rate(sum(x.count), 1 minute)`, `sum(x.sum) / sum(x.count)` on a histogram known by its stem (`http.server.request.duration.count`, `http.server.requests.count`) | the `_sum` series for every `sum()` (the ratio was always 1) | the series the name says: `_count` (a count, unit `reqpm`/`short`) or `_sum` |
| Micrometer / Spring Boot names (`jvm.memory.max`, `jvm.gc.pause`, `jvm.threads.live`, `process.cpu.usage`, `http.server.requests`, `hikaricp.connections.*`, `tomcat.*`, `logback.events`, `cache.*`, ...) | name-normalised guesses (`jvm_gc_pause` as a gauge) | the Prometheus-registry names (`jvm_gc_pause_seconds` timer, `jvm_memory_max_bytes`, `http_server_requests_seconds` with `uri`/`method`/`status` labels); timers note that `_bucket` needs percentiles-histogram |
| `max(restartCount)`, `average(restartCount)`, `sum(restartCount)` on container samples | the per-step increase (`max` of a rate) | the sampled cumulative value (`max_over_time`, `avg_over_time`, `sum by (pod)(last_over_time(...))`); `rate()` and `COMPARE WITH` still work on increases |
| `latest(reason)` / `WHERE reason = 'Evicted'` on `K8sPodSample` | untranslatable / `kube_pod_info{reason=...}` (no such label) | `kube_pod_status_reason{reason=...} == 1` (grouped by `reason`); a `status` filter next to it is implied |
| `average(numeric(duration_ms))` legend | `average(numeric(duration_ms))` | `average(duration_ms)` |
| the "panel unit set to percent (was )" note | an empty "(was )" | "(was s)" only when there was a unit |

### Iteration 10 — numeric WHERE on infra populations, ratio units

A tenth batch (Kubernetes / infra quickstart shapes and alert-style
counts):

| Construct | Before | Now |
| --- | --- | --- |
| `count(*)` / `uniqueCount(<entity>)` on a sample event `WHERE <metric-valued attribute> > n` (`podsMissing > 0`, `restartCount > 5`, `cpuPercent > 90`, `isReady = 0`) | the comparison vanished silently (the population was counted unfiltered) | the population is the attribute's own series filtered by the comparison: `count((expr) > n)`; several comparisons `and` together; a status/phase filter next to them is intersected `on (<entity labels>)`; the note shows the filter |
| an average / max / latest on a derived infra expression with a numeric WHERE it cannot take | silent | "cannot become a label matcher for this derived expression; dropped" |
| `average(loadAverageOneMinute) / latest(coreCount)` and other ratios of plain numbers | `percentunit` | no unit; only counts over counts (and sums of counters) are proportions |
| `allocatableCpuCoresUtilization`, `allocatableMemoryUtilization` on `K8sNodeSample` | unknown | used / allocatable per node (cAdvisor root cgroup over kube-state-metrics allocatable) |

### Iteration 14 — mapping-config variations, every `--json` command, browser render

The same generator ran under sixteen mapping configurations (HTTP and
span-metrics flavors and overrides, Loki parser / stream-label / metadata
settings, `metric_total_suffix`, `event_map` routing to Loki and to
metrics, `label_map` and `metric_map` overrides, `span_aggregations:
traceql`, `page_strategy: split` with `passthrough_fallback`): 2 900
emitted queries parsed, no crash, no static validation error. Every primary
command (`import`, `convert`, `inspect`, `explain`, `validate`, `export`,
including error paths) returned a proper `--json` envelope; exported fuzz
dashboards rendered in headless Chromium with no panel error. One gap:

| Construct | Before | Now |
| --- | --- | --- |
| `metric_map` keyed `"<Event>.<attribute>"` for an attribute the tool already maps (`"SystemSample.cpuPercent"`, `"ProcessSample.cpuPercent"`) | ignored — only unknown attributes and legacy AWS samples consulted it, although the review notes tell you to add exactly that key | overrides the built-in exporter mapping (name/type/unit, `matchers`, or a full `expr` template); `"<Event>.__count__"` overrides the population metric behind `count(*)` |

### Iteration 13 — wider sweep through the builder and a live Grafana

The generator grew to 40 event types (infra, on-host integrations, legacy
AWS, Lambda, browser/synthetics, custom events), 60 aggregation shapes,
nested and `WITH` queries, and random widget configurations, variables and
layouts; every generated dashboard was built, statically validated and run
through `validate --test` on a local Grafana/Prometheus/Loki/Tempo. Fixed:

| Construct | Before | Now |
| --- | --- | --- |
| `earliest(x)` without `TIMESERIES` | refused | the value at the start of the time range: `x @ ${__from:date:seconds}` (approximate); derived infra expressions use `last_over_time((expr)[W:] @ …)`; `latest(x) - earliest(x)` therefore works. With `TIMESERIES` still refused (no first-in-bucket function) |
| `percentile` / `median` of a derived infra expression (`memoryUsedPercent`, `diskUsedPercent`) | refused | `quantile_over_time(q, (expr)[W:])` (needs-review; the first percentile only, with a note for the others) |
| `count(<label attribute>)` on infra samples (`count(jobName)`, `count(interfaceName)`) | "no known exporter metric for attribute" | the entity population (NR counts samples carrying the attribute) |
| `latest(timestamp)` / `max(timestamp)` / `min(timestamp)` on infra samples | refused | `max(timestamp(<population metric>)) * 1000` (the last sample), `min(min_over_time(timestamp(…)[$__range:])) * 1000` (the first), unit `dateTimeAsIso` |
| `latest(<label attribute>)` (`latest(deploymentName)`) | "add it to metric_map" | refused as a label, pointing at `FACET` / `uniques()` |
| every SELECT item failing for one reason (`count(*), average(duration) FROM PageView`) | the reason repeated per item | one reason naming the items |
| `dimensions()` | "not supported" | NRDB introspection, like `keyset()` / `eventType()` |
| `WHERE a = 1 OR a = 2` on `DistributedTraceSummary` (TraceQL metrics) | `nestedSetParent < 0 && a = 1 \|\| a = 2` — `&&` binds tighter, the OR escaped the root filter | `nestedSetParent < 0 && (a = 1 \|\| a = 2)` |
| `trace.id = 500` (a number against a string field) | `trace:id = 500` (Tempo: "binary operations must operate on the same type") | `trace:id = "500"` |
| `spanCount` / `entityCount` in a `DistributedTraceSummary` WHERE | `.spanCount > 5` (no such attribute) | dropped with a note (a trace-level count has no TraceQL field); `root.span.name` maps to the root span's `name` |
| `round(percentile(duration, 99), 1)` as TraceQL metrics | refused | the wrapper is dropped with a note (use the panel's decimals); `numeric(duration)` casts are unwrapped; arithmetic is refused in plain words instead of naming the internal `_arith` |
| a widget layout outside New Relic's 12-column grid | a Grafana `gridPos` outside 24 columns (validation error, export refused) | clamped to the grid with a note in the report |
| `{{var}}` used by a widget but not defined among the dashboard's variables | "references variable $var which is not defined" (export refused) | a textbox variable labelled "var (undefined in the New Relic dashboard)" is added |
| two New Relic variables with one name | duplicate Grafana variables (validation error) | the first wins, its description says so |
| `--json validate` | `"result": null` on stdout, the summary JSON on stderr (the human per-file lines broke the envelope) | the envelope carries the summary; human lines go to stderr |

### Iteration 12 — randomised sweep (every emitted query must parse)

A randomised generator (event × aggregation × WHERE × FACET × time clause,
several thousand queries over four seeds) ran every emitted query through
promtool, Loki and Tempo 2.7. What it caught:

| Construct | Before | Now |
| --- | --- | --- |
| `count(*)` on a sample whose population metric carries fixed matchers (`StorageSample`, `ContainerSample`, …) | `count(node_filesystem_size_bytes{fstype!~"…"}{mountpoint="/"})` — two selector blocks, rejected | one selector with both |
| `COMPARE WITH` on a derived infra expression whose matcher contains `}` (`LIKE '%{{proc}}%'`) or several selectors | the offset landed inside the regex, or on the range selectors only; a bare metric name got none and a note | every vector selector (and nothing inside a quoted string) is offset; the comparison target is re-translated from the original WHERE, so a phase filter (`status = 'Running'`) or a numeric population filter (`podsMissing > 0`) is not lost in it |
| `uniqueCount(<attr>)` on `ProcessSample` (an aggregated population, `sum(namedprocess_namegroup_num_procs)`) | `avg by (…)(namedprocess_namegroup_num_procs)` | distinct label values among the aggregated series: `count(count by (groupname)(namedprocess_namegroup_num_procs))`; `uniqueCount(processId)` is the process count `sum(…num_procs)`; `count(*) WHERE cpuPercent > 50` counts the process groups whose CPU series exceeds 50 |
| `predictLinear` / `derivative` / `stddev` of a derived infra expression (`diskUsedPercent`, `cpuPercent`) | refused | a PromQL subquery: `predict_linear((expr)[$__range:], s)`, `deriv((expr)[W:]) * 60`, `stddev_over_time((expr)[W:])` (approximate); `predict_linear` of a plain gauge regresses over `$__range` too (New Relic regresses over the query window, not over a few scrapes) |
| `status IN ('Waiting', 'Terminated')` / `status LIKE 'Wait%'` on `K8sContainerSample` | `kube_pod_container_status_a\|b` (rejected) | the states the regex matches among running/waiting/terminated: `{__name__=~"kube_pod_container_status_(waiting\|terminated)"}`; no match → dropped with the reason. A consumed `status`/`reason` filter no longer leaves a "not in label_map" note |
| `rate(count(*), 1 minute)` on infra samples | "needs an attribute argument" | refused as New Relic's sampling rate (nothing to translate) |
| Loki selector made only of empty-compatible matchers (`NOT level = 'x'`, `level IS NULL`, `service_name NOT LIKE 'x%'`) | `{level!~"(?i)x"}` — Loki: "queries require at least one regexp or equality matcher that does not have an empty-compatible value" | `{service_name=~".+", level!~"(?i)x"}` with a needs-review note asking for a positive label filter |
| `FACET {{var}}` on TraceQL metrics | `by (.{{var}})` (rejected) | grouping dropped with a note |
| `FACET root.entity.name` on TraceQL metrics | `by (.root.entity.name)` | the WHERE field resolution: `by (resource.service.name)` |
| `sum(duration)` as TraceQL metrics | `sum_over_time` noted as Tempo 2.7+ | needs-review: Tempo 2.8+ (2.7 rejects it) |
| `<attr> IS NULL` on spans | `= nil`, silently | `= nil` with a needs-review note: Tempo 2.7 rejects it (`{.a = nil} not yet supported`) and cannot select spans that lack an attribute at all (`!(x != nil)` matches nothing — verified on 2.7.2) |
| function spelling in legends, notes and reports | the parser's lowercase (`uniquecount(host)`, `predictlinear(x, 3600)`) | New Relic's (`uniqueCount(host)`, `predictLinear(x, 1 hour)`) |
| `count(*)` on template-backed samples | two notes saying the count is the exporter's series | one |

### Iteration 11 — Loki and Tempo shapes

| Construct | Before | Now |
| --- | --- | --- |
| `parentId IS NULL` / `nr.entryPoint IS TRUE` on an aggregated `FROM Span` (span metrics) | a `parentId=""` label matcher (no such label: no data) | `span_kind=~"SPAN_KIND_SERVER\|SPAN_KIND_CONSUMER"` (entry spans) with a note; `span_aggregations: "traceql"` keeps the exact `nestedSetParent < 0` |
| `errorCount > 0` on `DistributedTraceSummary` | `.errorCount > 0` (no such span attribute) | the trace-level condition `{ root filters } && { status = error }` for searches and TraceQL metrics; `errorCount = 0` is refused with the reason |
| `newrelic.source`, `plugin.type`, `plugin.version`, ... in a log WHERE | parsed-field filters that never match | dropped with a note (New Relic ingest metadata) |
| `filter(count(*), WHERE ...) / count(*)` on logs | legend `count(*)` | the NRQL expression |
| `latest(message) ... FACET host` | the FACET vanished silently | noted as not expressible on a logs panel |
