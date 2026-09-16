# TCO trend analysis (AWS Cost Explorer, read-only)

Since 1.7 nr2grafana can analyze your **total cost of ownership over time**
— pulling real spend from AWS Cost Explorer, spotting trends, forecasting,
and tying cost movements to the optimization changes the tool has made.

> **AWS access is strictly read-only.** The tool shells out to your local
> `aws` CLI (your existing profile / SSO / role) and an explicit allow-list
> makes a mutating command *impossible to run* — it is refused before exec,
> with no shell interpolation. Your AWS credentials are never read, logged,
> or written; only read verbs (`get-`/`list-`/`describe-`) on Cost Explorer,
> CloudWatch, S3, STS, and pricing are permitted. Every dollar figure is an
> estimate derived from your own Cost Explorer data, with labeled
> assumptions.

## What it produces

- **Trend** — monthly cost per service over N months, with month-over-month
  %, CAGR, the current run-rate, direction, and a linear projection.
- **Forecast** — AWS Cost Explorer's own forecast plus the tool's linear
  extrapolation.
- **Anomalies** — from AWS Cost Anomaly Detection.
- **Observability attribution** — an estimate of how much of the bill is
  your LGTM stack: EC2 from the observability nodepool's packing cost, S3
  from the Mimir/Loki/Tempo buckets, and data transfer from the deep-dive's
  remote_write wire volume.
- **Change correlation** — it aligns the optimization actions the tool
  recorded (dropped metrics, right-sizing, datasource changes) against the
  cost line: *did our changes move the bill?* Presented honestly as
  correlation, not proof — a bill that kept rising is reported as
  "increase / not aligned", never spun as a win.
- **Snapshots over time** — each run is snapshotted and dated, so you can
  watch the trend evolve across runs.

## Use it

```bash
# Uses your local aws auth (AWS_PROFILE / SSO / role). Read-only.
python3 -m nr2grafana tco identity                 # which account/role am I?
python3 -m nr2grafana tco analyze --months 6 --group-by SERVICE \
    --profile my-sso-profile --region us-east-1 \
    --buckets prod-mimir,prod-loki,prod-tempo -o tco-report.json
python3 -m nr2grafana tco trend                    # diff dated snapshots
```

In the web UI, the **TCO** view shows the identity banner (with a read-only
badge), the trend chart with a forecast overlay and run-rate headline, the
by-service breakdown, the observability-attribution card, the
change-correlation timeline, the anomalies list, and the AWS Cost MCP panel.

## AWS Cost Explorer MCP

Generate a config that wires the AWS Cost Explorer MCP server
(`awslabs.cost-explorer-mcp-server`, run via `uvx`) into your local AI, so
you can ask cost questions in natural language. The config references your
AWS profile/region via environment variables — it never embeds credentials.

```bash
python3 -m nr2grafana mcp config --kind claude --aws-cost -o mcp.json
```
