# nr2grafana 1.11 — translation fidelity v2 (taught by real migrations)

Goal: an agent one-shot migration — import -> convert -> validate -> export —
where every migratable panel either (a) shows data against live Mimir/Loki/
Tempo/CloudWatch or (b) is an honest [MANUAL] placeholder with WHY + the
closest equivalent. **No silent empties.** Never mutate New Relic. No secrets
or customer identifiers in code, fixtures, docs, logs, or PRs (placeholders
only: <CLUSTER_PREFIX>-<env>, acme_backend.*, <NR_DASHBOARD_GUID>).

This contract is derived from a real 531-widget migration where a human had to
remap the converter's output. Every rule below cites the failure it fixes.
Builds on 1.1–1.10. Version 1.11.0. Zero deps (Python 3.9+ stdlib).

## 0. Observed failure classes (what humans changed) — the teaching set

| # | Converter did | Human changed it to | Count |
|---|---|---|---|
| F1 | `cluster="Func(name='concat', args=[Lit('p-'), Attr('{{env}}')]...)"` (Python repr of the concat node in a label value) | `cluster="p-$env"` or dropped | 254 |
| F2 | `avg_over_time(acme_backend_order_created[...])` ("assumed gauge") | `increase(acme_backend_order_created_total[$__range])` / `rate(..._total[$__rate_interval])` | ~210 |
| F3 | `k8s_container_cpuRequestedCores{k8s_namespaceName=..,k8s_deploymentName=..}` | `kube_pod_container_resource_requests{namespace=..,deployment=..,resource="cpu"}` | ~150 |
| F4 | PromQL for `aws.rds.CPUUtilization` / `aws.sqs.*` (no data in Mimir) | CloudWatch datasource target `{namespace:"AWS/RDS", metricName:"CPUUtilization", statistic:"Average"|"p95", dimensions:{DBInstanceIdentifier:["*"]}}` | 39 |
| F5 | `average(x.upper.percentiles)` / `sum(x.median)` untranslatable | `sum(rate(x_upper_percentiles_sum[..]))/sum(rate(x_upper_percentiles_count[..]))` (NR summary -> Prom summary) | ~15 |
| F6 | parse failure on bare boolean predicate (`... AND should_publish`) and `FROM Log, Log_dev` | handled | 14 |
| F7 | Loki: `allColumnSearch('t', insensitive: true)`, `FACET aparse(message,'%[TOPIC:*]%')`, `capture()` untranslatable | `|~ "(?i)t"`, `| regexp ".*\[TOPIC:(?P<topic>.*)\].*"` + `sum by (topic)`, case-insensitive `log_level=~"(?i)ERROR"` | ~25 |
| F8 | `${datasource}` / `${loki_datasource}` left unbound in exported JSON | bound to concrete uids (e.g. the instance's mimir/loki) | all |
| F9 | `entity.guid = '<GUID>'` untranslatable | `service_name_instance="<entity-name>"` (needs live NR GUID->entity resolution) | ~20 |
| F10 | `appName = 'svc (prod)'` kept as-is | `job="svc"` (strip " (env)" suffix; env from suffix) | ~10 |
| F11 | `FACET` without LIMIT returns ALL groups; `TIMESERIES 1 hour` | notes only (keep) | — |
| F12 | FinanceSample / Deployment / NrConsumption | untranslatable — but should be honest [MANUAL] + closest equivalent (AWS Cost Explorer/TCO feature; Grafana annotations) | ~10 |

Target after this release on the same corpus: F1/F6/F8 = 0 occurrences; F2/F3/
F4/F5/F7 handled deterministically; needs-review share materially down;
every untranslatable carries `closest_equivalent`.

## 1. Shared seams (agents code against these; no cross-file edits)

- **SEAM-RENDER** (`translate/common.py`, owner: metrics agent):
  `render_value(node, cfg) -> (text, kind)` renders a NRQL value node — Lit,
  Attr, `{{var}}` placeholder, `concat(...)` of those — to a Grafana-ready
  string: `concat('p-', {{env}})` -> `"p-$env"`; `{{env}}` alone -> `"$env"`;
  Lit -> literal. `kind` in {"literal","var","mixed"}. Used by metrics AND
  logs translators (logs agent imports it; falls back to a local copy only if
  missing at import time). IN/OR lists of such values -> regex alternation of
  rendered values (`=~"p-$env|q-$env"`), never Python reprs. A Python repr
  (`Func(`, `Lit(`, `Attr(`) appearing in any emitted query is a BUG; the corpus
  test asserts zero.
- **SEAM-KIND** (`translate/metrics.py`, owner: metrics agent):
  `infer_metric_kind(nr_name, agg, cfg, hints=None) -> MetricSource` with
  `mtype` in {counter, gauge, histogram, summary} and the deterministic Prom
  name. Deterministic rename: dots -> underscores, dashes -> underscores,
  camelCase preserved unless a K8S_METRIC_MAP entry applies; counters get
  `_total` (config `metric_total_suffix`, default True); summary ->
  `<name>_sum`/`<name>_count`; histogram -> `<name>_bucket`. Resolution order:
  explicit `cfg["metric_map"]` entry > `hints` (live Mimir metadata, see
  SEAM-HINTS) > NAME-RULES > AGG-RULES > default. NAME-RULES: gauge if the
  last segment matches (?i)(percent|percentage|utilization|ratio|bytes|cores|
  count$|size|free|used|available|desired|missing|requested|limit|gauge|
  temperature|age|lag|depth|visible|inflight|queued|connections) **unless**
  the agg is `sum()`/`count()` on an app metric ending in a past-tense/event
  word ((?i)(created|dispensed|executed|processed|failed|succeeded|received|
  sent|completed|started|triggered|updated|deleted|signals?|events?|requests?|
  errors?|hits?|calls?|messages?|tasks?|retries|timeouts|status_\d{3})) ->
  counter; summary if the name ends in (mean|median|upper|lower|percentiles|
  summary|p\d\d|stddev); histogram if name ends in (bucket|duration|latency|
  seconds|milliseconds) AND agg is percentile/histogram. AGG-RULES: `sum()`/
  `count()` of an otherwise-unknown app metric -> counter; `latest()`/
  `average()`/`max()`/`min()` of unknown -> gauge; `percentile()` of unknown
  -> histogram (needs-review) else quantile_over_time on a gauge.
- **SEAM-COUNTER-SEMANTICS** (metrics agent): counter + `sum()` with no
  TIMESERIES -> `sum(increase(m_total[$__range]))` (instant, "total over the
  window" = NR sum); counter + `sum()` + TIMESERIES -> `sum(increase(m_total[$__interval]))`
  (count per bucket, matches NR per-bucket sum; note that `rate()*interval` is
  equivalent); `rate(sum(m), 1 second)` -> `sum(rate(m_total[$__rate_interval]))`
  (scale for other units); `count(m)` on a counter -> increase (existing);
  `average(summary.mean)` -> `sum(rate(_sum))/sum(rate(_count))`; `latest(g)`
  -> `last_over_time(g[$__interval])`; `average(g)` -> `avg_over_time`; FACET
  -> `by (labels)` on the outer agg; FACET+LIMIT -> topk.
- **SEAM-K8S** (metrics agent): module-level `K8S_METRIC_MAP` (extensible via
  cfg["k8s_metric_map"]) from NR Kubernetes-integration metrics to kube-state-
  metrics/cAdvisor, each entry = (prom_expr_template, labels, kind, notes):
  `k8s.container.cpuRequestedCores` -> `kube_pod_container_resource_requests{resource="cpu"}` (gauge, last_over_time);
  `k8s.container.cpuLimitCores` -> `kube_pod_container_resource_limits{resource="cpu"}`;
  `k8s.container.memoryRequestedBytes` -> `kube_pod_container_resource_requests{resource="memory"}`;
  `k8s.container.memoryLimitBytes` -> `kube_pod_container_resource_limits{resource="memory"}`;
  `k8s.container.memoryWorkingSetBytes` -> `container_memory_working_set_bytes{container!=""}`;
  `k8s.container.cpuUsedCores` -> `rate(container_cpu_usage_seconds_total{container!=""}[$__rate_interval])`;
  `k8s.container.cpuCoresUtilization` -> `100 * sum by (namespace,pod,container)(rate(container_cpu_usage_seconds_total{container!=""}[..])) / on(namespace,pod,container) max by (namespace,pod,container)(kube_pod_container_resource_limits{resource="cpu"})`;
  `k8s.container.memoryUtilization` analog with working_set / limits{resource="memory"};
  `k8s.deployment.podsAvailable` -> `kube_deployment_status_replicas_available`;
  `k8s.deployment.podsDesired` -> `kube_deployment_spec_replicas`;
  `k8s.deployment.podsMissing` -> `kube_deployment_spec_replicas - kube_deployment_status_replicas_available`;
  `k8s.pod.*`/`k8s.node.*` common ones (status, restarts -> kube_pod_container_status_restarts_total, node cpu/mem via node_* );
  `K8sContainerSample containerCpuCfsThrottledPeriodsDelta/containerCpuCfsPeriodsDelta` ratio -> `rate(container_cpu_cfs_throttled_periods_total[..])/rate(container_cpu_cfs_periods_total[..])`.
  K8s attribute labels: `k8s.clusterName`->cluster, `k8s.namespaceName`->
  namespace, `k8s.deploymentName`->deployment, `k8s.podName`->pod, `k8s.
  containerName`->container, `k8s.nodeName`->node (generic rule: strip `k8s.`
  prefix, camelCase -> snake, then apply label_map, then `<x>Name`->`<x>`).
- **SEAM-CW** (`translate/cloudwatch.py`, owner: cloudwatch agent):
  `translate_to_cloudwatch(q, cfg) -> Translation` for `FROM Metric` queries
  whose metric names start with `aws.` (and AWS integration sample events).
  Emits `Translation.datasource="cloudwatch"` with `cw` payload (not expr):
  `{namespace, metricName, statistic, dimensions:{Name:[values|"*"]},
  dimension_keys, region:"default", queryMode:"Metrics", metricEditorMode:0}`
  or a Metric Insights/SEARCH `expression` (metricEditorMode:1) for
  `filter()`/multi-queue sums. Namespace map: rds->AWS/RDS, sqs->AWS/SQS,
  lambda->AWS/Lambda, ec2->AWS/EC2, ebs->AWS/EBS, elb->AWS/ELB, alb->
  AWS/ApplicationELB, nlb->AWS/NetworkELB, dynamodb->AWS/DynamoDB, s3->AWS/S3,
  kinesis->AWS/Kinesis, sns->AWS/SNS, ecs->AWS/ECS, eks->ContainerInsights,
  elasticache->AWS/ElastiCache, apigateway->AWS/ApiGateway, cloudfront->
  AWS/CloudFront, (extensible cfg["cloudwatch_namespaces"]). metricName = NR
  segment after the service (keep case). Statistic: average->Average,
  max->Maximum, min->Minimum, sum->Sum, count->SampleCount, latest->Average
  (note), percentile(x,N)->pN. Dimensions: WHERE `aws.<svc>.<Dim> = v` ->
  dimensions[Dim]=[v] (rendered via SEAM-RENDER, `$env` allowed; concat ->
  value with var), FACET `aws.<svc>.<Dim>` -> dimensions[Dim]=["*"] +
  dimension_keys; `dbClusterIdentifier`->DBClusterIdentifier etc. (case-fix
  table). Builder (same agent) emits a real CloudWatch target from `cw` with
  datasource family "cloudwatch" (`${cloudwatch_datasource}` var; config
  `datasources.cloudwatch` {type:"cloudwatch"}), confidence approximate.
  requirements.py (requirements agent) lists the cloudwatch datasource as
  REQUIRED when any CW target exists.
- **SEAM-HINTS** (`translate/hints.py`, owner: live agent):
  `collect_hints(nr=None, grafana=None, dash=None, cfg=None, log=None) -> dict`
  with: `metric_types` {prom_name: counter|gauge|histogram|summary} from Mimir
  `/api/v1/metadata` (via GrafanaLive proxy) + existence from `__name__`
  values (so `_total` vs bare is DATA-DRIVEN when a Grafana URL is given);
  `entities` {guid: {name, type, service_label}} via NerdGraph entity lookup
  (READ-ONLY); `attr_values` {attr: [values]} via read-only NRQL
  `SELECT uniques(attr)` for env/cluster attrs; `label_values` from Mimir.
  Translators accept `cfg["live_hints"]` and prefer them: kind from
  metric_types; `entity.guid='X'` -> `service_name="<entity name>"` (label
  via cfg `entity_label`, default service_name); `{{env}}` default value from
  attr_values. `convert --live` / `--grafana-url` wire this in. Never mutates.
- **SEAM-BIND** (`nr2grafana/bind.py`, owner: bind agent):
  `bind_datasources(dash, ds_map, keep_vars=False) -> dash` rewrites every
  target/panel datasource ref `${datasource}`-style (and the `${var}` uid
  refs) to concrete `{type, uid}` from ds_map (from GrafanaLive.resolve_ds_map
  or an explicit map), removes the datasource template variables unless
  keep_vars; `set_target_env(dash, env, env_map=None)` sets the `env` variable
  current/default value and (optionally) rewrites `$env` to a concrete value
  for a pinned export; `--env`/`--bind-datasources` on export/import/download
  and MCP grafana_import; package writes `dashboard.json` (portable, vars) +
  `dashboard.bound.json` when a Grafana connection exists.
- **SEAM-REPORT** (builder/requirements/artifacts): every widget-report entry
  gains: `metric_kind` (per metric), `missing_datasource` (family or null),
  `closest_equivalent` {datasource, example_query|cw_target, note} for
  needs-review/untranslatable, `manual` bool, `render_vars` (vars used, e.g.
  ["env"]), `k8s_mapped`/`cloudwatch` flags. Untranslatable panels become
  `[MANUAL] <title>` text panels whose body = WHY + closest equivalent +
  original NRQL (builder agent). `requirements.json` gains `missing_datasources`
  summary (families required but with no uid bound) so an AI knows exactly
  what to add; `/api/dashboards/<slug>`, MCP `get_dashboard`/`readiness`, and
  `ai_context` surface it.

## 2. Ownership (disjoint files)

1. **parser**: nrql/parser.py, tests/test_parser.py — bare boolean attr
   predicate (`AND flag` == `flag = true`), backticked attrs everywhere,
   `IS TRUE/FALSE/NULL/NOT NULL`, multi-event `FROM Log, Log_dev` (-> first
   event, note), `allColumnSearch(text, insensitive: true)`, `aparse(field,
   pattern)`, `capture(field, r'...')`, `tuple()`, `WITH METRIC_FORMAT`,
   `dateOf/hourOf/weekOf` (-> note), comments (`--`) stripped anywhere, two
   SELECTs in one NRQL string (split -> extras). Keep AST node names stable.
2. **metrics**: translate/metrics.py, translate/common.py, tests/test_translate_metrics.py
   — SEAM-RENDER, SEAM-KIND, counter semantics, summary metrics, K8S map,
   k8s attr normalization, appName "svc (env)" -> job/service label + env,
   `uniqueCount(x)` -> `count(count by (x)(...))`, arithmetic between
   aggregates (ratios, `24*7*max()`), `COMPARE WITH` kept, `live_hints`.
3. **logs**: translate/logs.py, tests/test_translate_logs.py — concat/$env in
   stream labels via SEAM-RENDER; `allColumnSearch` -> `|~ "(?i)..."`;
   `aparse`/`capture` FACET -> `| regexp "(?P<name>..)"` + `by (name)` (convert
   NR `%`/`*` wildcards to regex); `log_level='X'` -> case-insensitive option
   (cfg `loki_case_insensitive_levels` default True); raw `SELECT f1,f2 FROM
   Log` -> logs panel (`| json | line_format "{{.f1}} {{.f2}}"`); `bytecountestimate`
   -> `bytes_over_time`; `uniqueCount` -> `count(count by)`; multi-event FROM.
4. **cloudwatch+builder**: translate/cloudwatch.py (new), translate/router.py,
   grafana/builder.py, config.py, tests/test_translate_cloudwatch.py,
   tests/test_builder.py — SEAM-CW + routing (aws.* metrics -> cloudwatch;
   `filter()`/multi-queue -> SEARCH expression), CW target emission, new
   `cloudwatch` datasource family + `${cloudwatch_datasource}` var, DEFAULT_CONFIG
   keys for ALL new options (`metric_kinds`, `k8s_metric_map`, `cloudwatch_
   namespaces`, `env_var` (default "env"), `target_env`, `env_map`,
   `entity_label`, `loki_case_insensitive_levels`, `live_hints`), [MANUAL]
   placeholder enrichment (SEAM-REPORT fields in the widget report), `job`
   label for appName.
5. **live**: translate/hints.py (new), nerdgraph.py (read-only entity lookup +
   `SELECT uniques()` NRQL via run_nrql; keep the mutation guard), tests/
   test_hints.py, tests/test_nerdgraph.py — SEAM-HINTS.
6. **bind+cli**: bind.py (new), cli.py, interactive.py, tests/test_bind.py,
   tests/test_cli.py — SEAM-BIND; `convert --live --grafana-url/--token --env
   <name>`; `export --bind-datasources --env`; wizard entries.
7. **api**: web/server.py, mcp_server.py, aicontext.py, tests/test_web.py,
   tests/test_mcp_server.py, tests/test_aicontext.py — expose SEAM-REPORT +
   `missing_datasources` (GET /api/dashboards/<slug>/missing, MCP tool
   `missing_datasources`), `convert` route/tool accept `live`, `env`,
   `bind`; import/download bind datasources when a Grafana connection exists;
   ai_context carries per-panel metric_kind/closest_equivalent/missing_datasource
   + a "what to add" section.
8. **requirements+artifacts**: requirements.py, artifacts.py, tests/
   test_requirements.py, tests/test_artifacts.py — cloudwatch family detection
   from CW targets, `missing_datasources` summary, README "Before you import"
   lists them, [MANUAL] panels section with closest equivalents.
9. **corpus**: fixtures/corpus/ (new), tests/test_corpus.py, tools/corpus_tools.py
   — a REDACTED golden corpus derived from the real query map (coordinator
   supplies the scratch path; it must NOT be committed): generic names only
   (`<CLUSTER_PREFIX>-` -> `acme-cluster-`, app -> `acme_backend`/`acme-backend`,
   org -> `acme`, queues -> `acme-queue-*`, hostnames -> example.com, keep
   `<REDACTED_*>`), preserving NRQL shapes + human live targets as expected
   outputs. Tests: zero Python reprs in output; concat -> `$env`; counters get
   `_total` + increase/rate; summary -> _sum/_count; K8S map applied; CW
   targets emitted with correct namespace/metricName/statistic; untranslatable
   -> [MANUAL] with closest_equivalent; confidence distribution thresholds
   (needs-review <= 45% of widgets, untranslatable <= 8%); a normalized-expr
   comparison vs human live targets reporting agreement % (informational +
   a floor).
10. **docs**: README.md, docs/translation-notes.md, docs/live-translation.md
    (new), docs/api.md — the rules above as user docs; support matrix update.

## 3. Verification + iteration loop
- **corpus verifier (Fable)**: run the converter over fixtures/corpus,
  measure before/after (confidence distribution, agreement vs human targets,
  per-failure-class counts F1..F12), fix what it can, and return the
  remaining gap classes with concrete examples.
- **secrets+safety verifier**: grep repo + diff for real identifiers from
  the source map (cluster prefix, app/org names, account ids, GUID-like
  blobs, hostnames) — must be ZERO in committed files; NR mutation guard
  intact; AWS read-only intact; no secrets in docs.
- **iteration**: up to 2 rounds of gap-closer agents driven by the corpus
  verifier's remaining list, each followed by re-verification. Exit when the
  corpus thresholds hold and the verifier reports no high-severity class.

## Cross-cutting
stdlib only, py3.9, ASCII/LF/4-space/79-col; keep all existing tests green
(fix a test only if it asserted objectively wrong output and say so); no
customer identifiers anywhere in committed files; never mutate New Relic.
