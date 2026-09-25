# nr2grafana 1.11 — conversion depth, a four-command surface, agent-native

1.11 is a fidelity release for the core: the NRQL parser and the three
translators were stress-tested against ~250 real-world query shapes and
rewritten where they were wrong or silent, the CLI was reduced to the four
steps a migration actually has, and every step now returns a structured
result that a local AI agent (Claude Code, Cursor, Kiro, any MCP client) can
drive. Builds on 1.1–1.10; every earlier command still exists (hidden from
`--help`, listed in the epilog). Version 1.11.0, stdlib only.

## A. NRQL parser (`nrql/parser.py`)

- Full arithmetic in SELECT and WHERE with precedence and parentheses:
  `(a / b) * 100`, `100 * a / b`, `count(*) - filter(...)`,
  `duration * 1000 > 500`. Normalisation folds scale factors into
  `SelectItem.multiplier`, agg/agg into `_ratio`, everything else into
  `_arith(op, a, b)`. A scale factor on the argument of a linear aggregation
  (`average(duration * 1000)`) is lifted into the multiplier.
- `--` line and `/* */` block comments (outside strings).
- `WITH <expr> AS <alias>` computed attributes (leading, after FROM, or
  trailing), substituted into SELECT/WHERE/FACET before normalisation.
- Named arguments glued to their value (`apdex(duration, t:0.3)` used to
  silently fall back to t=0.5 — now parsed).
- Bare boolean predicates (`WHERE error AND ...`), `r'...'` raw strings in
  `capture()`, `WITH METRIC_FORMAT`.
- Subqueries (`FROM (SELECT ...)`, `IN (SELECT ...)`) are rejected with a
  message that says so instead of a generic token error.
- `-` and `/` are operators; hyphenated names need backticks (as in NRQL).

## B. Shared matcher layer (`translate/common.py`)

- WHERE → disjunctive normal form (`cond_to_branches`): an OR across
  different attributes is no longer dropped (which silently returned data
  for *every* service); it becomes a list of matcher conjunctions (max 8).
  Same-attribute ORs still merge into one regex matcher first.
- Numeric comparisons are captured as `NumericPred`s instead of dropped;
  translators that can express them consume them, the router reports the
  rest.
- `numeric()/string()/toLower()/toUpper()/cast()` wrappers are unwrapped
  (case functions force `(?i)`); attribute-to-attribute and
  expression comparisons are reported, never rendered as literals.
- FACET functions: `concat()` → group by every attribute with a legend
  template, `capture()`/`aparse()` → `label_replace(...)` specs, time
  bucketing functions → precise note, `buckets()` → note.

## C. PromQL translator (`translate/metrics.py`, `translate/nrmetrics.py`)

- `_Ctx.rf()` renders every range/instant selector, unioned with PromQL
  `or` across WHERE branches (`sum(increase(m{A}[W]) or increase(m{B}[W]))`
  — sound because series are deduplicated by label set before
  aggregation) and wrapped in `label_replace` for capture facets.
- Histogram bucket arithmetic for `count(*)`/`filter()`/`percentage()`/
  `cases()` with a numeric threshold on the source's own value attribute
  (`WHERE duration > 1` → `_count − _bucket{le="1"}`, bands →
  `_bucket{le=hi} − _bucket{le=lo}`), flagged needs-review because a bucket
  boundary must exist there.
- `nrmetrics.py`: built-in knowledge of New Relic's own metric names —
  `apm.service.*` (HTTP histogram, 5xx count, DB/HTTP client histograms,
  process CPU/memory, instance count), `newrelic.goldenmetrics.apm.*`
  (throughput/min, responseTimeMs, errorRate as derived templates),
  `newrelic.timeslice.value` (needs `metric_map` keyed by timeslice name),
  OTel semantic-convention names as ingested by NR (typed, with units),
  `host.*`/`k8s.*` agent dimensional metrics aliasing the sample-event
  tables, and `aws.<ns>.<Metric>` under the YACE naming convention
  (`aws_ec2_cpuutilization_average`, `dimension_InstanceId` labels).
- Infra sample events: `INFRA` table with ~200 (event, attribute) specs of
  kind gauge / counter / rate / histogram / expr (templates with `<AGG>`,
  `<AGGINV>`, `<BY>`, `<SEL>`, `<W>`, `<HTTP>`) / count (entity population
  for `count(*)` and `uniqueCount(<entity attr>)`) / none (precise reason
  plus LGTM equivalent), for SystemSample, NetworkSample, StorageSample,
  ProcessSample (process-exporter), ContainerSample (cAdvisor) and every
  K8s*Sample (kube-state-metrics, cAdvisor, kubelet). `EVENT_LABELS`
  overlays per-event attribute→label conventions (`nodeName`→`node`,
  `interfaceName`→`device`, `name`→`name` on containers, ...). Pod phase
  filters/facets select `kube_pod_status_phase`; cAdvisor-implicit filters
  (`state = 'running'`) are dropped with a note.
- Transaction attributes are mapped explicitly (`duration`/`totalTime` →
  HTTP server histogram, `databaseDuration` → DB client histogram,
  `externalDuration` → HTTP client histogram, `latest(timestamp)` →
  `max(timestamp(...)) * 1000`); unknown attributes are untranslatable
  with the reason instead of silently becoming the HTTP histogram.
  `transactionType = 'Web'` is dropped as implicit; `'Other'` is
  untranslatable.
- Multi-item SELECTs survive one untranslatable item (dropped with a
  note); infra multi-item SELECTs translate every item.
- `getField(m, count|sum|max|min|average)`, `FROM Metric WHERE metricName =
  'x'`, `FACET if(cond, a, b)` (two cases), `_arith` between aggregations,
  multiplier-aware units (`s×1000 → ms`, ratio×100 → percent, bytes/1024 →
  kbytes, ...), `sum()` of an unmapped metric resolves as a counter.
- Config: `metric_map` entries accept `matchers` and full `expr`
  templates; `event_map` routes custom event types to Loki (stream
  labels) or to a metric; `span_aggregations: "traceql"`.

## D. LogQL translator (`translate/logs.py`)

Numeric predicates → `| json | field > 500`; `_ratio`/`_arith` between log
aggregations; multipliers with unit scaling; one target per aggregation
instead of dropping the rest; `latest(message)` → a one-line logs panel;
OR across attributes → shared stream selector + pipeline `or` (or the
all-streams selector with a note when the OR spans stream labels; dropped
with a note only when it mixes message predicates); `event_map` stream
labels; `numeric()` unwrapping.

## E. TraceQL (`translate/traces.py`)

`otel.status_code = 'ERROR'` is `status = error` (it was inverted); `OK` /
`UNSET` map too. TraceQL metrics (`{ ... } | rate() by (...)`,
`quantile_over_time(duration, q)`, `count_over_time()`, ...) for aggregated
Span queries when `span_aggregations: "traceql"`, and always for
`uniqueCount(trace.id)` (root-span count: `nestedSetParent < 0`). Targets
carry `metricsQueryType: "range"`.

## F. Router / builder / validator

- `SINCE a UNTIL b` (both relative) → panel `timeFrom` = a−b and
  `timeShift` = b (exact); `SINCE last week/last month/this year/monday`.
- Builder: `timeShift`, TraceQL-metrics targets, `viz.scatter`
  (points), `viz.sparkline` (stat + area), `viz.traffic-light`,
  `viz.billboard-comparison`; explicit `NO_PANEL_WIDGETS` (funnel, service
  map, inventory, geo map, custom nerdpack ids) with reason + closest
  Grafana equivalent; report entries carry `panel_title`, `reason`,
  `equivalent`; every dashboard gets a `nr2grafana.source` provenance
  block, the source named in the description, and a link back to the New
  Relic entity.
- `validate_dashboard_full()` adds query-language sanity (leaked NRQL,
  leftover template placeholders, stray commas, empty Loki selectors,
  unwrap-less range aggregations, TraceQL shape), undefined `$variables`,
  `timeFrom`/`timeShift` format, row structure, panel-type and duplicate
  title warnings.

## G. Pipeline + CLI (`pipeline.py`, `cli.py`, `inspect.py`)

Four library operations returning plain dicts, used by the CLI and the
MCP server alike:

| step | what it does | writes |
|---|---|---|
| `import` | NerdGraph (all / `--guid` / `--name`) or local export files, validated and normalised | `<out>/*.json`, `import-manifest.json` |
| `convert` | build + deep-validate; names every widget that cannot migrate (reason, equivalent), panels needing review, datasource types per dashboard | dashboards, `migration-report.json` (+ packages/INDEX.md with `--package`) |
| `validate` | static; `--grafana-url`: datasource *types* needed vs. present with the exact fix and the uid each binds to; `--test`: every panel through `/api/ds/query` | `*.validate-results.json` |
| `export` | binds datasource variables to the instance, creates (folder, overwrite), reads back and verifies title/panel count, optional data test; prints created title/url/uid/folder and the New Relic source | `*.export-results.json` |

`inspect` (deep semantic model of an NR dashboard: pages, widgets, parsed
NRQL clauses, attributes, variables, translation plan, datasources needed,
cannot-migrate list) and `explain` (one NRQL). `--json` before any command
yields one JSON object on stdout. Exit codes: 0 / 1 problems / 2 usage /
3 connectivity. Legacy commands remain, hidden from `--help`.

MCP server tools added: `inspect`, `explain`, `import`, `convert_files`,
`validate_dashboards`, `export` (pipeline-backed, no web layer).

## H. Agent guidance

`AGENTS.md` (workflow, output contracts, how to fix flagged panels through
the config, the NRQL semantics the converter guarantees), `CLAUDE.md`
(includes it), `.cursor/rules/nr2grafana.mdc`, `.kiro/steering/
nr2grafana.md`, and the `nr2grafana-migrate` skill (end-to-end runbook)
next to the existing `nr-dashboard-tailor` skill.
