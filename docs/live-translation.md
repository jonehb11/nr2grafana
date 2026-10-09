# Translation fidelity (1.11): metric kinds, K8s, CloudWatch, env, live, bind

nr2grafana 1.11 was taught by a real migration of a few hundred widgets
where a human had to remap most of the converter's output by hand. This
page documents the deterministic rules that came out of it, how to
override each one in the mapping config, and the two new steps that
make a converted dashboard show data on a specific Grafana: **live
translation** (`convert --live`) and **datasource binding at export**
(`export --bind-datasources`).

The contract behind all of it: every migratable panel either shows data
against your Mimir/Loki/Tempo/CloudWatch, or is an honest `[MANUAL]`
placeholder that says why and names the closest equivalent. **No silent
empties.** New Relic is only ever read.

Contents:

- [1. Metric rename and kind inference](#1-metric-rename-and-kind-inference)
- [2. Counter semantics](#2-counter-semantics)
- [3. Summary metrics](#3-summary-metrics)
- [4. Kubernetes integration metrics (K8s map)](#4-kubernetes-integration-metrics-k8s-map)
- [5. AWS integration metrics -> CloudWatch targets](#5-aws-integration-metrics---cloudwatch-targets)
- [6. concat(), `$env` and the target environment](#6-concat-env-and-the-target-environment)
- [7. Live translation (`convert --live`)](#7-live-translation-convert---live)
- [8. Datasource binding at export](#8-datasource-binding-at-export)
- [9. The `[MANUAL]` placeholder contract](#9-the-manual-placeholder-contract)
- [10. What is missing: `missing_datasources`](#10-what-is-missing-missing_datasources)
- [11. Config reference](#11-config-reference)

---

## 1. Metric rename and kind inference

Every `FROM Metric` name is turned into a Prometheus name and a **kind**
(`counter`, `gauge`, `histogram`, `summary`). Both are deterministic, so
the same NRQL always yields the same PromQL, and both are reported per
metric in `widget-report.json` (`metric_kind`).

### Rename

| Step | Example |
| --- | --- |
| dots -> underscores | `acme_backend.order.created` -> `acme_backend_order_created` |
| dashes -> underscores | `acme-backend.queue-depth` -> `acme_backend_queue_depth` |
| camelCase preserved | `acme_backend.cacheHitRatio` -> `acme_backend_cacheHitRatio` |
| counter suffix | `acme_backend_order_created` -> `acme_backend_order_created_total` |
| summary | `acme_backend.latency.upper` -> `acme_backend_latency_upper_sum` / `_count` |
| histogram | `acme_backend.request.duration` -> `acme_backend_request_duration_bucket` |

camelCase is only rewritten when a K8s map entry applies (section 4).
The counter suffix is governed by `metric_total_suffix` (default
`true`); set it to `false` if your pipeline ships counters without
`_total`. With `--live` the suffix decision is data-driven instead (the
tool checks which spelling actually exists in Mimir).

### Kind resolution order

The first rule that answers wins:

1. **`metric_map`** -- an explicit entry in your config is always
   honored (exact Prometheus name and type).
2. **`metric_kinds`** -- a lighter override: NR name -> kind only, the
   name still goes through the deterministic rename.
3. **Live hints** -- with `--live`, the kind reported by Mimir's
   `/api/v1/metadata`, backfilled from which series exist (`x_total`
   -> counter, `x_bucket` -> histogram, `x_sum` + `x_count` ->
   summary).
4. **Name rules** (last dotted segment of the NR name):
   - **gauge** when it matches one of the gauge words
     (case-insensitive):
     `percent percentage utilization ratio bytes cores count size free`
     `used available desired missing requested limit gauge temperature`
     `age lag depth visible inflight queued connections`
     -- *unless* the aggregation is `sum()` or `count()` on an app
     metric ending in an event word, which is a **counter**:
     `created dispensed executed processed failed succeeded received`
     `sent completed started triggered updated deleted signal(s)`
     `event(s) request(s) error(s) hit(s) call(s) message(s) task(s)`
     `retries timeouts status_NNN`;
   - **summary** when it ends in
     `mean|median|upper|lower|percentiles|summary|pNN|stddev`;
   - **histogram** when it ends in
     `bucket|duration|latency|seconds|milliseconds` *and* the
     aggregation is `percentile()`/`histogram()`.
5. **Aggregation rules** for anything still unknown:
   - `sum()` / `count()` -> **counter** (NR "sum of an event metric
     over the window" is a counter increase, not an average of a
     gauge -- the single most common manual fix in the teaching set);
   - `latest()` / `average()` / `max()` / `min()` -> **gauge**;
   - `percentile()` -> **histogram**, flagged `needs-review`; if the
     name gives no histogram hint it stays a gauge and becomes
     `quantile_over_time`.
6. **Default**: gauge.

Name/aggregation-rule hits (rules 4-5) are `approximate` since 1.11
(the rules are deterministic and the teaching corpus confirmed them;
the note still says how to verify); the `percentile()`-of-unknown case
and the default (rule 6) stay `needs-review`. `metric_map`,
`metric_kinds` and live hints are `exact`.

### Overriding

```json
{
  "metric_kinds": {
    "acme_backend.cache.size": "counter",
    "acme_backend.worker.lag": "gauge"
  },
  "metric_map": {
    "acme_backend.order.created": {
      "name": "acme_orders_created_total",
      "type": "counter"
    }
  }
}
```

Use `metric_kinds` when only the kind guess is wrong; use `metric_map`
when the Prometheus name differs from the deterministic rename.
`changes suggest-config` emits these entries for you from fixes made in
the web UI or by the AI.

---

## 2. Counter semantics

NR `sum(x)` on an event-shaped metric means "total over the window";
NR `TIMESERIES` means "total per bucket". PromQL expresses both with
`increase()`, never `avg_over_time()`:

| NRQL | PromQL |
| --- | --- |
| `SELECT sum(m)` (no TIMESERIES) | `sum(increase(m_total[$__range]))` -- instant query |
| `SELECT sum(m) TIMESERIES` | `sum(increase(m_total[$__interval]))` -- per bucket (`rate() * $__interval` is equivalent) |
| `SELECT rate(sum(m), 1 second)` | `sum(rate(m_total[$__rate_interval]))` (scaled by the unit for other units) |
| `SELECT count(m)` | `increase` of the counter (unchanged from earlier releases) |
| `SELECT latest(g)` (gauge) | `last_over_time(g[$__interval])` |
| `SELECT average(g)` (gauge) | `avg_over_time(g[...])` |
| `... FACET a, b` | `by (a, b)` on the outer aggregation |
| `... FACET a LIMIT n` | `topk(n, ...)` |

A counter can never be `average()`d meaningfully; if your NRQL does
that, the translation is `needs-review` with a note.

---

## 3. Summary metrics

New Relic summary-type metrics expose `.mean`, `.median`, `.upper`,
`.lower`, `.percentiles`, `.stddev` views of one metric. The Prometheus
summary equivalent is a `_sum`/`_count` pair:

| NRQL | PromQL |
| --- | --- |
| `average(acme_backend.latency.upper.percentiles)` | `sum(rate(acme_backend_latency_upper_percentiles_sum[$__rate_interval])) / sum(rate(acme_backend_latency_upper_percentiles_count[$__rate_interval]))` |
| `sum(x.median)` | `sum(rate(x_median_sum[...]))` (noted) |
| `percentile(x.upper, 95)` | `x_upper{quantile="0.95"}` when the pipeline ships quantiles, else `needs-review` |

If your pipeline converts NR summaries to Prometheus *histograms*
instead, map the metric in `metric_map` with `"type": "histogram"`.

---

## 4. Kubernetes integration metrics (K8s map)

NR Kubernetes-integration metrics (`k8s.container.*`,
`k8s.deployment.*`, `k8s.pod.*`, `k8s.node.*`, `K8sContainerSample`)
are mapped to kube-state-metrics / cAdvisor / node_exporter metrics
through a module-level `K8S_METRIC_MAP`:

| New Relic | Prometheus |
| --- | --- |
| `k8s.container.cpuRequestedCores` | `kube_pod_container_resource_requests{resource="cpu"}` |
| `k8s.container.cpuLimitCores` | `kube_pod_container_resource_limits{resource="cpu"}` |
| `k8s.container.memoryRequestedBytes` | `kube_pod_container_resource_requests{resource="memory"}` |
| `k8s.container.memoryLimitBytes` | `kube_pod_container_resource_limits{resource="memory"}` |
| `k8s.container.memoryWorkingSetBytes` | `container_memory_working_set_bytes{container!=""}` |
| `k8s.container.cpuUsedCores` | `rate(container_cpu_usage_seconds_total{container!=""}[$__rate_interval])` |
| `k8s.container.cpuCoresUtilization` | `100 * sum by (namespace,pod,container)(rate(container_cpu_usage_seconds_total{container!=""}[...])) / on(namespace,pod,container) max by (namespace,pod,container)(kube_pod_container_resource_limits{resource="cpu"})` |
| `k8s.container.memoryUtilization` | same shape with `container_memory_working_set_bytes` / `kube_pod_container_resource_limits{resource="memory"}` |
| `k8s.deployment.podsAvailable` | `kube_deployment_status_replicas_available` |
| `k8s.deployment.podsDesired` | `kube_deployment_spec_replicas` |
| `k8s.deployment.podsMissing` | `kube_deployment_spec_replicas - kube_deployment_status_replicas_available` |
| `k8s.pod.*` / `k8s.node.*` (common) | pod status / `kube_pod_container_status_restarts_total` / `node_*` |
| `containerCpuCfsThrottledPeriodsDelta / containerCpuCfsPeriodsDelta` | `rate(container_cpu_cfs_throttled_periods_total[...]) / rate(container_cpu_cfs_periods_total[...])` |

Gauges in this table are read with `last_over_time`. Each entry carries
its own kind and notes, so a mapped panel is `exact`/`approximate`
rather than `needs-review`.

**K8s attribute labels** follow one generic rule -- strip the `k8s.`
prefix, camelCase -> snake_case, apply `label_map`, then `<x>Name` ->
`<x>`:

| NR attribute | Label |
| --- | --- |
| `k8s.clusterName` | `cluster` |
| `k8s.namespaceName` | `namespace` |
| `k8s.deploymentName` | `deployment` |
| `k8s.podName` | `pod` |
| `k8s.containerName` | `container` |
| `k8s.nodeName` | `node` |

Extend or override the table per deployment with `k8s_metric_map`:

```json
{
  "k8s_metric_map": {
    "k8s.container.cpuUsedCores": {
      "expr": "rate(container_cpu_usage_seconds_total{image!=\"\"}[5m])",
      "kind": "gauge",
      "notes": "cgroup v2 cluster: exclude pause containers"
    }
  }
}
```

---

## 5. AWS integration metrics -> CloudWatch targets

`FROM Metric` queries whose metric names start with `aws.` (and AWS
integration sample events) used to become PromQL that had no data in
Mimir. They are now emitted as real **CloudWatch datasource targets**,
which means a `cloudwatch` datasource becomes **required** for that
dashboard: `requirements.json` lists it, `grafana check` fails without
it, and `missing_datasources` names it (section 10).

| NRQL | CloudWatch target |
| --- | --- |
| `SELECT average(aws.rds.CPUUtilization) FROM Metric WHERE aws.rds.DBInstanceIdentifier = 'acme-db-1'` | `{namespace: "AWS/RDS", metricName: "CPUUtilization", statistic: "Average", dimensions: {DBInstanceIdentifier: ["acme-db-1"]}}` |
| `... FACET aws.sqs.QueueName` | `dimensions: {QueueName: ["*"]}`, `dimension_keys: ["QueueName"]` |
| `percentile(aws.alb.TargetResponseTime, 95)` | `statistic: "p95"` |
| `filter(...)` / sums over several queues | Metric Insights `SEARCH(...)` expression (`metricEditorMode: 1`) |

Rules:

- **Namespace**: `rds` -> `AWS/RDS`, `sqs` -> `AWS/SQS`, `lambda` ->
  `AWS/Lambda`, `ec2` -> `AWS/EC2`, `ebs` -> `AWS/EBS`, `elb` ->
  `AWS/ELB`, `alb` -> `AWS/ApplicationELB`, `nlb` -> `AWS/NetworkELB`,
  `dynamodb` -> `AWS/DynamoDB`, `s3` -> `AWS/S3`, `kinesis` ->
  `AWS/Kinesis`, `sns` -> `AWS/SNS`, `ecs` -> `AWS/ECS`, `eks` ->
  `ContainerInsights`, `elasticache` -> `AWS/ElastiCache`,
  `apigateway` -> `AWS/ApiGateway`, `cloudfront` -> `AWS/CloudFront`.
  Extend with `cloudwatch_namespaces`.
- **metricName**: the NR segment after the service, case kept.
- **Statistic**: `average` -> `Average`, `max` -> `Maximum`, `min` ->
  `Minimum`, `sum` -> `Sum`, `count` -> `SampleCount`, `latest` ->
  `Average` (noted), `percentile(x, N)` -> `pN`.
- **Dimensions**: `WHERE aws.<svc>.<Dim> = v` -> `dimensions[Dim] =
  [v]` (`$env` and `concat()` values allowed, see section 6); `FACET
  aws.<svc>.<Dim>` -> `["*"]` + `dimension_keys`. Lower-camel NR
  spellings are case-fixed (`dbClusterIdentifier` ->
  `DBClusterIdentifier`).
- **Region** is `default` (the datasource's region); `queryMode` is
  `Metrics`.

Targets use the `${cloudwatch_datasource}` variable and carry confidence
`approximate`. Configure the datasource with:

```json
{"datasources": {"cloudwatch": {"type": "cloudwatch", "uid": "..."}}}
```

If you ship AWS metrics into Mimir instead (YACE / cloudwatch-exporter
/ OTel `awscloudwatch` receiver), map them in `metric_map` and they
stay PromQL.

---

## 6. concat(), `$env` and the target environment

Real dashboards scope almost every query with
`WHERE cluster = concat('acme-cluster-', {{env}})`. Earlier releases
leaked the parser's internal node into the label value; 1.11 renders
values with one shared rule for metrics, logs and CloudWatch:

| NRQL value | Rendered |
| --- | --- |
| `'literal'` | `"literal"` |
| `{{env}}` | `"$env"` |
| `concat('acme-cluster-', {{env}})` | `"acme-cluster-$env"` |
| `IN ('a', concat('b-', {{env}}))` | `=~"a\|b-$env"` (regex alternation of rendered values) |

A Python-looking fragment (`Func(`, `Lit(`, `Attr(`) in an emitted query
is a bug; the corpus test asserts there are none.

`widget-report.json` lists the variables each panel uses
(`render_vars`), and the dashboard gets a Grafana `env` variable (name
from `env_var`, default `env`).

**Target environment.** Pass `--env <name>` to `convert`, `export` or
`grafana import` to set that variable's current/default value. The
name goes through `env_map` first, so NR environment names can differ
from Grafana ones:

```json
{"env_var": "env", "env_map": {"production": "prod", "staging": "stg"}}
```

`--pin-env` (export/import) additionally rewrites every `$env` to the
concrete value and drops the variable: a single-environment export.
With `--live`, the variable's default comes from the values New Relic
actually holds for the attribute (section 7).

**`appName = 'svc (prod)'`** -- the NR convention of suffixing the
environment onto the application name is split: the label becomes
`job="svc"` (label via `label_map`, default `job`) and `prod` feeds the
env variable.

---

## 7. Live translation (`convert --live`)

Without a live stack the converter has to guess kinds, entity names
and environment values. `--live` replaces guesses with data:

```bash
export NEW_RELIC_API_KEY=NRAK-...
export GRAFANA_URL=https://grafana.example.com GRAFANA_TOKEN=glsa_...
python3 -m nr2grafana convert ./newrelic-dashboards -o ./out --package \
    --live --env prod
```

Either connection is optional; each only adds its part. Everything it
does is **read-only**:

| Source | What is read | What it changes |
| --- | --- | --- |
| Mimir (through the Grafana datasource proxy) | `/api/v1/metadata` types; `__name__` existence for every candidate name a `FROM Metric` query could produce; label values for mapped labels | metric kind (section 1 step 3); `_total` vs bare name decided by what exists; label values validated |
| New Relic NerdGraph | `actor { entity(guid) }` for every `entity.guid = '<GUID>'` predicate | `entity.guid = '...'` -> `service_name="<entity name>"` (label via `entity_label`, default `service_name`) instead of untranslatable |
| New Relic NRQL (read-only `SELECT uniques(attr)`) | values of env/cluster-like attributes and of any attribute compared to a `{{var}}` | the `env` variable's options and default |

The NerdGraph client refuses to send any GraphQL mutation; `--live`
runs queries only. Limits (`hints_max_metrics`, `hints_max_entities`,
`hints_max_attrs`, `hints_max_values`, `hints_max_labels`,
`hints_since`) bound the number of lookups.

Collected hints are stored as `live-hints.json` in each package and are
also accepted directly through the config key `live_hints`, so a
conversion can be replayed offline with the same answers. Every
degradation (proxy 404, NRQL error, missing key) becomes a note in the
widget report rather than an exception.

Web UI / API: **Convert** has a *Live hints* toggle; `POST
/api/convert` and the MCP `convert` tool take `live: true`.

---

## 8. Datasource binding at export

Converted dashboards are **portable**: every target references a
datasource template variable (`${datasource}`, `${loki_datasource}`,
`${tempo_datasource}`, `${cloudwatch_datasource}`), so one JSON works on
any Grafana. Left unbound, though, an imported dashboard renders
empty until someone picks every datasource by hand -- the teaching
set's most frequent manual step. Binding writes a second, instance-
specific file:

```bash
python3 -m nr2grafana export ./out/*/ --bind-datasources --env prod
# -> ./out/<slug>/dashboard.bound.json next to dashboard.json
```

- `--bind-datasources` resolves each variable to the instance's
  datasource of that type (the default one, else the first) through
  `GRAFANA_URL`/`GRAFANA_TOKEN`, rewrites every panel and target ref
  to a concrete `{"type", "uid"}`, and drops the picker variables
  (`--keep-vars` keeps them, preselected).
- `--ds VARIABLE=UID` binds one variable explicitly (repeatable, works
  offline, overrides the resolved pick).
- `--env` / `--pin-env` set or pin the env variable (section 6).
- An unresolved ref fails loudly with the family that is missing and
  the add-datasource template; it never writes a half-bound file.

`grafana import --bind-datasources [--env ... --pin-env]` does the same
right before pushing (and fails loudly on unresolved refs instead of
importing an empty dashboard). On the CLI, `convert` itself stays
offline-portable: run `export` for the bound file. In the web UI, API
and MCP, `convert` with `bind: true` writes `dashboard.bound.json`
directly whenever a Grafana connection exists; **Download** offers the
bound JSON (`?bind=1&env=`), the package zip includes both files, and
the MCP `grafana_import` tool takes `bind` and `env`. Flat (non-package)
output gets `<slug>.bound.json` next to `<slug>.json`.

`dashboard.json` stays portable; `dashboard.bound.json` is the one to
import on *that* Grafana.

---

## 9. The `[MANUAL]` placeholder contract

A widget the converter cannot translate soundly never becomes an empty
panel. It becomes a text panel titled `<original title> [MANUAL]` whose
body states:

1. **why** -- the precise reason (for example "FinanceSample is New
   Relic billing data; no LGTM equivalent");
2. the **closest equivalent** -- the datasource and an example query or
   CloudWatch target that gets you nearest (`closest_equivalent:
   {datasource, example_query | cw_target, note}`), e.g. AWS Cost
   Explorer / the `tco` feature for `FinanceSample`, Grafana
   annotations for `Deployment` events, the NR datasource plugin for
   `NrConsumption`;
3. the **original NRQL**, verbatim.

The same fields appear in `widget-report.json` (`manual: true`,
`closest_equivalent`, `metric_kind`, `missing_datasource`,
`render_vars`, `k8s_mapped`, `cloudwatch`), in the package README's
"[MANUAL] panels" section, in `ai_context`, and in the
`missing_datasources` report. `needs-review` panels carry a
`closest_equivalent` too when the converter had a plausible alternative.

`--passthrough` still turns these into live panels through the New
Relic Grafana datasource plugin if you prefer.

---

## 10. What is missing: `missing_datasources`

`requirements.json` gains a `missing_datasources` summary: the
datasource families the dashboard needs that have no uid bound
(`prometheus`, `loki`, `tempo`, `cloudwatch`, ...). When a Grafana
connection is configured, families are resolved against the instance
-- a family whose plugin type exists there is not missing; offline,
every unbound `${var}` counts.

The same answer is available wherever an AI or script might ask:

- `GET /api/dashboards/<slug>/missing` and the MCP tool
  `missing_datasources` (argument `slug`) return
  `{missing_datasources: [family], datasources_to_add: [{template}],
  manual_panels: [{panel_id, title, why, closest_equivalent}],
  needs_review: [...], grafana_checked: bool}` -- each missing family
  with the exact add-datasource template (API body, MCP
  `add_datasource` call, CLI command);
- `GET /api/dashboards/<slug>` and the MCP `get_dashboard` /
  `readiness` results embed it;
- `ai_context` carries a "What to add before this dashboard works"
  section plus per-panel `metric_kind` / `closest_equivalent` /
  `missing_datasource`;
- the package README's "Before you import" section lists the families.

See [api.md](api.md) for the request/response shapes.

---

## 11. Config reference

New or changed keys in 1.11 (all optional; `example-config` prints the
defaults):

| Key | Default | Purpose |
| --- | --- | --- |
| `metric_kinds` | `{}` | NR metric name -> `counter\|gauge\|histogram\|summary`; overrides the name/aggregation rules without renaming |
| `metric_map` | `{}` | NR metric name -> `{name, type}`; wins over everything |
| `metric_total_suffix` | `true` | append `_total` to inferred counters |
| `k8s_metric_map` | `{}` | extend/override the K8s map; `{nr_name: {expr, kind, notes, labels?}}` |
| `cloudwatch_namespaces` | `{}` | extra `aws.<svc>` -> CloudWatch namespace entries |
| `datasources.cloudwatch` | `{type: "cloudwatch"}` | the CloudWatch datasource family (`uid` to bind) |
| `env_var` | `"env"` | name of the environment dashboard variable |
| `target_env` | `""` | default target environment (same as `--env`) |
| `env_map` | `{}` | NR environment name -> Grafana value |
| `entity_label` | `"service_name"` | label used for resolved `entity.guid` filters |
| `loki_case_insensitive_levels` | `true` | `log_level = 'ERROR'` -> `log_level=~"(?i)ERROR"` |
| `live_hints` | `null` | a stored `live-hints.json` to translate with, offline |
| `hints_max_*`, `hints_since` | see above | bounds for `--live` lookups |
