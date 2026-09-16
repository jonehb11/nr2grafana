# nr2grafana 1.7 — deep TCO trend analysis (AWS read-only + Cost MCP)

New capability: analyze **total cost of ownership over time**, tie cost
movements to the optimization changes the tool has made, forecast, and
discover spend via **AWS Cost Explorer** — using the user's **local AWS CLI
auth**, **strictly read-only**, and optionally the **AWS Cost Explorer MCP
server**.

Builds on 1.1–1.6 (all contracts hold). Version 1.7.0. **Zero deps: Python
3.9+ stdlib only.** Secrets never persisted/logged. New Relic read-only.
**AWS access is READ-ONLY and enforced in code** (see §1).

## 1. `nr2grafana/awscost.py` — read-only AWS discovery (owner: awscost agent)

Shell out to the local `aws` CLI (uses the user's existing auth: env,
profile, SSO, role). **A hard read-only guard is the whole point**: only
allow-listed read verbs may run; anything mutating is refused before exec.

```python
class AWSError(Exception): ...
# Allow-list: a command may run ONLY if it matches (service, verb) where the
# verb is read-only. Enforce with an explicit allow-set, not a deny-set.
READONLY_VERBS = ("get-", "list-", "describe-", "head-", "lookup-",
                  "search-", "batch-get-")  # prefix allow-list
ALLOWED = {  # service -> allowed subcommands (exact or prefix); READ ONLY
  "ce": {"get-cost-and-usage", "get-cost-and-usage-with-resources",
         "get-cost-forecast", "get-dimension-values", "get-tags",
         "get-anomalies", "get-anomaly-monitors", "get-cost-categories",
         "list-cost-allocation-tags", "get-usage-forecast",
         "get-savings-plans-utilization", "get-reservation-utilization"},
  "sts": {"get-caller-identity"},
  "cloudwatch": {"get-metric-statistics", "get-metric-data", "list-metrics"},
  "s3api": {"list-buckets", "get-bucket-location",
            "get-bucket-lifecycle-configuration", "get-bucket-tagging"},
  "ec2": {"describe-instances", "describe-instance-types",
          "describe-regions"},
  "pricing": {"get-products", "get-attribute-values", "describe-services"},
  "organizations": {"list-accounts", "describe-organization"},
}
def aws_available() -> bool          # `aws` on PATH
def caller_identity() -> Dict        # sts get-caller-identity (who am I)
def run_aws(service, subcommand, args=None, region="us-east-1",
            profile="", timeout=120) -> Any
    # REFUSE (raise AWSError) unless (service, subcommand) is in ALLOWED and
    # subcommand starts with a READONLY_VERB. shell=False, argv list, never
    # interpolate into a shell. --output json; parse stdout. Actionable
    # errors (not configured / access denied / throttling).
def get_cost_and_usage(start, end, granularity="MONTHLY",
                       group_by=None, metrics=None, filt=None,
                       region="us-east-1", profile="") -> Dict
def get_cost_forecast(start, end, metric="UNBLENDED_COST",
                      granularity="MONTHLY", ...) -> Dict
def get_anomalies(start, end, ...) -> List[Dict]
def s3_bucket_sizes(buckets, region="us-east-1", profile="") -> Dict
    # via cloudwatch get-metric-statistics (BucketSizeBytes/NumberOfObjects)
```
Never run without an explicit allow-list match; unit-test that a mutating
command (e.g. ("ce","create-anomaly-monitor"), ("ec2","terminate-instances"))
is REFUSED. Owns tests/test_awscost.py (stub `run_aws`/subprocess with a fake
aws returning canned CE JSON; test the guard with adversarial inputs).

## 2. `nr2grafana/tco.py` — TCO trend engine (owner: tco agent)

```python
def cost_series(aws_mod_or_client, months=6, group_by="SERVICE",
                profile="", region="us-east-1", log=None) -> Dict
    # monthly cost per group over N months (via awscost.get_cost_and_usage)
def trends(series) -> Dict
    # per-group + total: month-over-month deltas, % growth, CAGR, latest
    # run-rate, direction (up/down/flat), a simple linear projection
def attribute_observability(series, deepdive=None, traffic=None,
                            packing=None, buckets=None) -> Dict
    # estimate the observability share of spend: EC2 (obs nodepool via
    # packing pool cost), S3 (mimir/loki/tempo buckets), DataTransfer/TGW/
    # NAT (from deepdive remote_write wire volume). Clearly labeled estimate.
def correlate_changes(series, change_log, snapshots=None) -> Dict
    # align recorded optimization actions (changelog + optimize/deepdive
    # artifacts, with dates) to cost movements: "dropped metric X on D ->
    # ingest/EC2 line moved $Y after". Honest about correlation != causation.
def forecast(aws_mod_or_client, months=3, ...) -> Dict   # CE forecast + our linear
def snapshot(store, report) -> None       # persist a dated TCO snapshot
def trend_over_snapshots(store) -> Dict   # diff dated snapshots over time
def analyze(aws_mod_or_client, store=None, deepdive=None, traffic=None,
            packing=None, change_log=None, months=6, buckets=None,
            log=None) -> Dict   # schema "nr2grafana/tco/v1"
```
Report schema "nr2grafana/tco/v1": `{"schema","generated_at","currency",
"months","total":{"series":[[month,usd]...],"trend":{...},"forecast":{...}},
"by_service":[...], "observability_attribution":{...},
"anomalies":[...], "change_correlation":{...},
"recommendations":[...],  # ties to deepdive/optimize savings vs actual trend
"assumptions":[...]}`. Everything is an ESTIMATE from the user's own CE
data + labeled assumptions. Owns tests/test_tco.py (feed canned CE series;
assert trend math, attribution, change correlation, forecast, snapshots).

## 3. `nr2grafana/mcp.py` additions — AWS Cost MCP (owner: mcp agent)

Extend `generate_mcp_config` and add a helper so the tool can use the **AWS
Cost Explorer MCP server** (`awslabs.cost-explorer-mcp-server`, run via
`uvx awslabs.cost-explorer-mcp-server@latest`) for discovery, alongside the
Grafana MCP server.

```python
# generate_mcp_config gains include_aws_cost=False. When true, add an
# "aws-cost-explorer" server entry: command uvx awslabs.cost-explorer-mcp-
# server@latest, env AWS_PROFILE / AWS_REGION referenced via ${...} (NEVER
# embed AWS keys — rely on the local credential chain / profile).
def cost_via_mcp(client, question="") -> Dict
    # optional: call the AWS Cost MCP server's tools through MCPClient
    # (list_tools/call_tool) for natural-language cost discovery; never raise.
```
Extend tests/test_mcp.py: aws-cost server appears in the config with no
embedded secret; the fake MCP server test still passes.

## 4. Web + CLI + Store (owner: server-cli agent)

server.py: `POST /api/tco {months?, group_by?, profile?, buckets?}` -> job
(runs tco.analyze via awscost using local auth; persists "tco" artifact +
a dated snapshot); `GET /api/tco?slug=`/latest; `GET /api/aws/identity`
(caller_identity — shows which account/role, read-only); `GET /download/
tco-report.json`. /api/mcp/config gains an `aws_cost` toggle. Store
ARTIFACT_KINDS += "tco","tco-snapshot". /api/state features += "tco",
"aws". CLI: `tco analyze [--months N] [--group-by SERVICE|USAGE_TYPE]
[--profile P] [--region R] [--buckets b1,b2] [-o OUT]` (prints trend table
+ run-rate + projection + observability attribution + change correlation;
writes tco-report.json), `tco identity` (print caller identity), `tco
trend` (diff stored snapshots). Wizard: "Analyze AWS TCO trends". Owns
server.py, cli.py, interactive.py, store.py, tests/test_web.py,
tests/test_cli.py. Lazily import siblings; degrade cleanly when `aws` is
absent (clear "AWS CLI not found / not configured" message, never a crash).

## 5. Web UI — TCO view (owner: ui agent, ui.py ONLY)

New **TCO** nav item: an identity banner (account/role, read-only badge), a
"Run TCO analysis" form (months, group-by, profile), then: a **total-cost
trend chart** (monthly series via chart()) with run-rate + growth headline
and a forecast overlay; a **by-service** breakdown (bar/donut); an
**observability attribution** card (estimated obs share: EC2/S3/transfer)
tied to the deep-dive; a **change-correlation timeline** (recorded
optimization actions plotted against the cost line — "did our changes move
the bill?"); an **anomalies** list; and an **AWS Cost MCP** panel (generate/
copy config, referencing the local profile, no secrets). Plain-language
tooltips (run-rate, CAGR, attribution is an estimate). Reuse chart()/
errorCard/console/jsonDetails/copy. esc() discipline; keep test hooks;
single module string; no external assets; < 520KB.

## 6. Mock + e2e (owner: mock agent)

Add a **fake `aws` CLI** (tools/fake_aws.py — a python script that emits
canned Cost Explorer JSON for get-cost-and-usage/get-cost-forecast/
get-anomalies/sts get-caller-identity, with a believable multi-month upward
trend and one anomaly) and point awscost at it via an env override
(`N2G_AWS_BIN`) for tests. Add a fake AWS Cost MCP mode to
tools/fake_mcp_server.py (or reuse it). Extend tests/test_e2e_mock.py: drive
tco.analyze end to end against the fake aws (assert trend detected, forecast
present, attribution computed, and a mutating command is refused).

## Cross-cutting
1. stdlib only, py3.9, ASCII/LF/4-space/79-col.
2. **AWS is READ-ONLY, enforced by an explicit allow-list**; a mutating
   command must be impossible to run. AWS credentials are NEVER read, logged,
   or written — the tool only shells out to `aws`, which uses the local
   credential chain; MCP config references profile/env, never embeds keys.
3. Everything is an estimate from the user's own Cost Explorer data with
   labeled assumptions; correlation is presented as correlation, not proof.
4. `aws` CLI / AWS access is OPTIONAL; the tool stays usable without it.
5. Every module tested; full suite stays green.
6. Coordinator bumps version to 1.7.0 and writes docs + README after build.
