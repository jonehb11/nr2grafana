# Requirements analysis and dashboard packages

A converted dashboard is only half the migration — it still needs the
right datasources, plugins, and (crucially) the right *data* on the
Grafana side. The requirements analyzer works out all three per
dashboard, and the packager turns each dashboard into a self-contained
directory an operator can act on without reading any converter docs.

## Generating packages

Packages are produced three ways; all use the same analyzer:

```bash
# during conversion: one directory per dashboard instead of flat files
python3 -m nr2grafana convert ./newrelic-dashboards -o ./out --package

# after the fact, for output you already converted (uses the
# migration-report.json written next to the files when present) --
# also accepts raw New Relic dashboard JSON
python3 -m nr2grafana analyze ./grafana-dashboards -o ./out

# or interactively: the n2g wizard and the web UI (Convert & Package)
```

Each dashboard becomes `<out>/<slug>/`:

| File | What it is |
|---|---|
| `dashboard.json` | the importable Grafana dashboard |
| `README.md` | human guide: datasources to create, plugin installs, import steps (UI + curl), troubleshooting |
| `requirements.json` | the machine-readable analysis (schema `nr2grafana/requirements/v1`) |
| `widget-report.json` | per-panel translation confidence + original NRQL |
| `datatest.json` | per-panel test queries (schema `nr2grafana/datatest/v1`) |
| `test.sh` | executable data smoke test — see [live-testing.md](live-testing.md) |

`<out>/INDEX.md` summarizes the whole run — one row per dashboard with
panel count, confidence counts (exact/approx/review/manual), required
datasources, and detected domains — plus the suggested per-dashboard
workflow.

## What the analyzer determines

**Datasources** — collected from what the panels actually reference,
not guessed from config. For a typical APM dashboard:

```
| Name       | Plugin id    | Core | Purpose                    | Referenced as        |
|------------|--------------|------|----------------------------|----------------------|
| Loki       | `loki`       | yes  | logs (Loki)                | `${loki_datasource}` |
| Prometheus | `prometheus` | yes  | metrics (Mimir/Prometheus) | `${datasource}`      |
| Tempo      | `tempo`      | yes  | traces (Tempo)             | `${tempo_datasource}`|
```

**Plugins** — non-core plugins the dashboard needs, with the exact
install command. Passthrough panels (from `convert --passthrough`) add
`grafana cli plugins install nrgrafanaplugin-newrelic-datasource`.

**Data domains** — the analyzer reads the *original NRQL* (event
types, metric names) and maps it to data domains, each with the
Grafana-side options that produce equivalent data. Built-in coverage:

| NR evidence | Domain | Grafana equivalents |
|---|---|---|
| `AwsLambda*`, `aws.lambda.*` | aws-lambda | CloudWatch datasource, or YACE/cloudwatch-exporter/OTel → Mimir (`aws_lambda_*`); logs via CloudWatch Logs or lambda-promtail → Loki |
| other `aws.*` | aws | CloudWatch datasource or exporter pipeline |
| `gcp.*` | gcp | Google Cloud Monitoring (stackdriver) |
| `azure.*` | azure | Azure Monitor datasource |
| `SystemSample`, `ProcessSample`, ... | infra-host | node_exporter or OTel hostmetrics → Mimir |
| `K8s*Sample` | k8s | kube-state-metrics + cAdvisor/kubelet → Mimir |
| `Log` | logs | Loki (promtail/Alloy/OTel filelog) |
| `Span`, `DistributedTrace*` | traces | Tempo + span metrics (metrics-generator or spanmetrics connector) |
| `Transaction`, `TransactionError` | apm | OTel APM instrumentation → Mimir |
| `SyntheticCheck` | synthetics | blackbox_exporter or Grafana Synthetic Monitoring |
| `PageView*`, `Browser*`, `JavaScriptError` | browser-rum | Grafana Faro |
| `Mobile*` | mobile | Grafana Faro |
| `NrConsumption`, `NrUsage`, `NrAuditEvent` | nr-account | New Relic-only; needs the NR plugin passthrough |

Extend or override the table with a `domain_map` list in your config
(same entry shape as the built-ins; your entries match first, most
specific first).

**Data expectations** — per translated panel, the concrete metric
names, labels, and Loki stream selectors the queries will look for.
This is what turns "No data" from a mystery into a checklist:

```
- panel 5 (prometheus): needs metrics `http_server_request_duration_seconds_count`; labels `http_route`, `service_name`
- panel 13 (loki): needs log stream `{service_name="checkout", level="error"}`; labels `level`, `service_name`
```

**NR-native widgets** — panels with no LGTM equivalent as-is (funnels,
NR account data, ...) are listed with *why* and the closest option:

```
- Panel 10 'Checkout funnel' (`viz.funnel`)
  - why: no metric mapping for FROM PageView
  - equivalent: bar gauge panel + Grafana Faro Web SDK -> Faro collector (Alloy) -> Loki/Mimir
```

## Reading requirements.json programmatically

Top-level keys: `schema`, `dashboard`, `uid`, `generated_by`,
`datasources`, `plugins`, `domains`, `nr_native`, `data_expectations`,
`import` (numbered steps + a ready `api_example` curl). The same file
drives `grafana check` (live requirement verification — see
[live-testing.md](live-testing.md)) and the web UI's "Install these
first" card.
