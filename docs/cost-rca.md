# AWS cost-anomaly RCA + reliability-safe mitigation

Since 1.9 nr2grafana can investigate an AWS **cost spike** the way a senior
SRE would: converge read-only evidence to a root cause, then propose fixes
that cut the bill **without breaking availability, durability, performance,
or the ability to serve current traffic**. It reproduces investigations like
the classic "EBS DataTransfer-Regional-Bytes" spike that is really cross-AZ
network transfer from a non-zone-aware observability stack.

> **AWS is strictly read-only, and the tool only PROPOSES — it never
> executes.** It shells out to your local `aws` CLI (your aws-vault / SSO
> profile), an allow-list makes any mutating command impossible, and every
> generated config is GitOps-owned for you to apply. Costs are estimates.

## Connecting AWS

Uses the local credential chain wherever nr2grafana runs. Pick a profile
(read from `~/.aws/config`); it runs `aws --profile <name> …`, or wraps with
`aws-vault exec` when `N2G_AWS_WRAP` is set. Have the profile's session
active first (`aws sso login --profile X`, or an unlocked aws-vault) — in the
web UI the `aws` process runs server-side, so any MFA/SSO prompt appears on
the host, not the browser; an expired session yields a clear "run aws sso
login" message.

## Root-cause analysis

Feed it an AWS Cost Anomaly Detection anomaly, a Cost Explorer usage-type
jump, or **paste an anomaly report**. It converges evidence across Cost
Explorer, CloudTrail, VPC Flow Logs (CloudWatch Logs Insights), the EKS
control plane, and the LGTM self-metrics to a **dominant driver with a %
share**, a secondary, and a ruled-out list — each with its disproving
evidence. Key domain insight it encodes: a `*DataTransfer-Regional-Bytes`
usage type on *any* service (EBS, EC2, …) is a **cross-AZ network** charge
first — the service tag is a CUR attribution artifact, not storage.

## Reliability-safe mitigation

For each root cause it proposes ranked mitigations with estimated $/day
saved and, critically, **reliability preconditions** and keeps_availability
/durability/performance + handles-current-traffic flags. For the reference
cross-AZ case:

- **Make the stack zone-aware** (Mimir/Loki zone-aware replication + zone
  labels) — flagged as a careful *live-ring* migration (rollout-operator,
  one zone at a time, deploy ≥ RF zones, wait the query-store/ingesters
  window before decommissioning).
- **Topology-aware routing** (`trafficDistribution: PreferClose`) — only
  where endpoints are spread across serving zones, because it has no
  built-in overload safeguard and would hotspot an under-replicated zone.
- **Add the missing-AZ Karpenter discovery subnet** so the ring balances
  across 3 AZs instead of collapsing into 2.
- **Disable NLB cross-zone** — *only* after confirming healthy targets in
  every AZ; a blind disable black-holes a single-target AZ.

Each ships a generic, paste-ready config (Karpenter NodePool/EC2NodeClass,
Mimir/Loki values, Kubernetes Service, NLB annotations). "Ask AI to analyze"
runs the whole artifact bundle through your AI backend for a written plan.

## Use it

```bash
export GRAFANA_URL=... GRAFANA_TOKEN=...     # for the LGTM-side evidence
python3 -m nr2grafana aws profiles           # list local AWS profiles
python3 -m nr2grafana cost rca --paste       # paste an anomaly report, or:
python3 -m nr2grafana cost rca --anomaly-id <id> --profile prod \
    --flow-logs-group /vpc/flowlogs -o rca.json
python3 -m nr2grafana cost mitigate rca.json # ranked, gated mitigations + config/
```

Or the web UI **Cost RCA** view: paste/pick an anomaly, run RCA, run
mitigation, and copy or download the configs.
