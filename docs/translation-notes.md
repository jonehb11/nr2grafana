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
| `rate(agg, N unit)` | exact-shaped | `sum(rate(m[W])) * seconds(N unit)`; the parser normalizes `1 minute` etc. to seconds |
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
| `hourOf/dateOf/weekdayOf/...(timestamp)` | needs-review | note suggests the interval to use |
| `string(x)` etc. | exact | unwrapped |

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
(exact); `SINCE last week` → `now-1w/w`, `last month` → `now-1M/M`.

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
