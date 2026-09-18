# nr2grafana 1.9 — AWS cost anomaly RCA + reliability-safe mitigation

> ENRICHED CONTRACT (Fable-5 ARCHITECT). This file is the single source of
> truth for the 12 build agents. It keeps the coordinator's module map, seams,
> and ownership verbatim, and adds the web-researched domain specifics: the RCA
> methodology, exact paste-ready GENERIC config templates, reliability
> guardrails, and the exact read-only AWS calls per module. All external facts
> carry an inline doc-URL citation (verified 2025-2026). Build agents: implement
> against THIS version. Nothing here overrides Cross-cutting rules at the end.

New capability: point nr2grafana at an AWS **cost spike** (from AWS Cost
Anomaly Detection, a Cost Explorer usage-type jump, or a pasted anomaly
report like the reference example), and it performs a **root-cause analysis**
by converging read-only evidence across AWS + the LGTM stack, then proposes a
**reliability-safe mitigation plan** with generated, paste-ready configs
(Karpenter, Mimir/Loki, Kubernetes Services, NLB) that cut cost **without
breaking availability, durability, or the ability to handle current
traffic**. AI can analyze the artifacts and produce the plan accounting for
current workload.

Builds on 1.1–1.8 (awscost.py read-only guard, tco.py, deepdive.py,
packing.py Karpenter, mcp.py, aicontext.py, ai.py). Version 1.9.0. **Zero
deps: Python 3.9+ stdlib only.** Secrets in memory only. **ALL AWS access is
READ-ONLY, enforced by the awscost allow-list.** NR read-only.

---

## 0. Domain facts the whole design rests on (researched, cited)

These are the load-bearing external facts. Modules encode them; tests assert
them. Citations are the authority — do not "improve" the numbers from memory.

### 0.1 Cross-AZ transfer billing & the "DataTransfer-Regional-Bytes" artifact
- Cross-AZ (inter-Availability-Zone, **same region**) traffic bills at
  **$0.01/GB in EACH direction** — sender pays $0.01 *and* receiver pays
  $0.01, so a round-tripped GB effectively costs **$0.02/GB**. Same-AZ traffic
  over **private** IPs is free; using public/EIP addresses even within one AZ
  incurs the regional charge.
  https://www.cloudzero.com/blog/reduce-data-transfer-costs/
  https://docs.aws.amazon.com/cur/latest/userguide/cur-data-transfers-charges.html
- **CRITICAL, verified:** the CUR usage-type code for cross-AZ within-region
  transfer is literally **`<Region>-DataTransfer-Regional-Bytes`**. "Regional"
  here means **cross-AZ inside one region**, NOT cross-region. You see **two**
  `DataTransfer-Regional-Bytes` line items per flow (one in, one out).
  https://www.nops.io/blog/aws-data-transfer-cost-usage-type/
  https://docs.aws.amazon.com/cur/latest/userguide/cur-data-transfers-charges.html
  So when the anomaly is tagged "EBS DataTransfer-Regional-Bytes", the **EBS
  service tag is a classification/attribution artifact**; the dollars are
  **cross-AZ network transfer**, not EBS storage. The RCA engine MUST treat a
  `*DataTransfer-Regional-Bytes` usage-type on ANY service (EBS, EC2, etc.) as
  a cross-AZ network hypothesis first, and rule storage in/out separately.
  (One-liner mnemonic for rca.py: `Regional-Bytes == cross-AZ network`.)

### 0.2 Grafana Mimir zone-aware replication
- Enable per component: write path on distributors & rulers, read path on
  queriers, plus the ring on ingesters:
  `-distributor.zone-awareness-enabled=true`,
  `-ingester.ring.zone-awareness-enabled=true`,
  `-ingester.ring.instance-availability-zone=<zone>` (Alertmanager uses
  `-alertmanager.sharding-ring.zone-awareness-enabled=true`).
  https://grafana.com/docs/mimir/latest/configure/configure-zone-aware-replication/
- Rule: **deploy across a number of zones >= replication factor**; default
  RF=3 needs >= 3 zones. Cluster needs `floor(RF/2)+1` **healthy** zones to
  serve (RF=3 -> tolerates 1 zone down). Deploying to **fewer zones than RF
  can miss writes or fail writes outright (data-loss risk).**
- Live-ring migration is done zone-by-zone with the **rollout-operator**;
  rolling updates must touch **only one zone at a time**. Helm migration
  sequence: create zone-aware ingester StatefulSets, keep the distributor/
  ruler/querier write flag **false** while zone ingesters backfill, add zone
  ingesters in bounded batches (<=21 at a time — series briefly double-count
  toward limits), then flip the write flag and **wait `-querier.query-store-
  after` (~12h)** before decommissioning old ingesters.
  https://grafana.com/docs/helm-charts/mimir-distributed/latest/migration-guides/migrate-from-single-zone-with-helm/

### 0.3 Grafana Loki zone-aware replication
- Enable with `distributor.zone-awareness-enabled` (jsonnet:
  `multi_zone_ingester_enabled: true`). Three per-zone StatefulSets; each
  ingester carries an `availability-zone` label (`zone-a|zone-b|zone-c`);
  RF = number of zones (3). Managed by rollout-operator (`rollout-group:
  ingester`, one StatefulSet at a time).
  https://grafana.com/docs/loki/latest/operations/zone-ingesters/
- Migration safety: set the existing ingester **PDB `maxUnavailable: 0`**
  during reshuffle; **double the max-series/stream limits** first (zone
  distribution multiplies streams); enable write-path awareness, **wait
  `-querier.query-ingesters-within` (~3h)**, THEN enable read-path awareness;
  watch rule-eval + ingestion metrics stay flat throughout.

### 0.4 Kubernetes topology-aware routing (`trafficDistribution`)
- `spec.trafficDistribution: PreferClose` on a Service prefers topologically
  close (same-zone) endpoints. Field history: **alpha 1.30, beta/on-by-default
  1.31, GA 1.33**. In 1.33+ `PreferClose` is aliased to the clearer
  `PreferSameZone`; `PreferClose` still works. 1.34/1.35 add `PreferSameNode`/
  `PreferSameZone` variants.
  https://www.kubernetes.dev/resources/keps/4444/
  https://kubernetes.io/docs/concepts/services-networking/service/
- It is a **hint, not a guarantee** — behavior depends on the proxy
  (iptables/IPVS/eBPF). Unlike the older Topology Aware Hints, `PreferClose`
  has **no built-in overload safeguard**: if endpoints are NOT evenly spread
  across zones, same-zone preference **hotspots** the zone with few replicas.
  Guardrail: only recommend `PreferClose` when there are **>= replicas per
  zone to serve current per-zone traffic**, and the workload is spread across
  all serving zones (see reliability.py). Older mechanism for context:
  `topologyKeys` (removed) / Topology Aware Hints (`service.kubernetes.io/
  topology-mode: Auto`).

### 0.5 Karpenter v1 NodePool + EC2NodeClass
- `NodePool` (`karpenter.sh/v1`) = scheduling policy (instance types,
  capacity-type, zone requirements, limits, disruption). `EC2NodeClass`
  (`karpenter.k8s.aws/v1`) = AWS infra (subnets, SGs, AMI, role).
  https://karpenter.sh/docs/concepts/nodeclasses/
  https://karpenter.sh/docs/concepts/nodepools/
- **Subnet discovery** via `subnetSelectorTerms` (tags or ids). Karpenter can
  only place a node in an AZ for which a **matching discovery subnet exists**;
  if multiple match one AZ it picks the one with most free IPs.
  **AZ imbalance root cause:** if discovery subnets are tagged in only 2 of 3
  AZs (e.g. Private-A/Private-B, none in us-east-1c), Karpenter **cannot**
  launch in the third AZ even if the NodePool lists it in
  `topology.kubernetes.io/zone` — so the ring/pods collapse into 2 AZs and
  cross-AZ replication traffic (and its bill) concentrates there.
- Disruption: `consolidationPolicy: WhenEmpty | WhenEmptyOrUnderutilized`,
  `consolidateAfter`, and `budgets` (`nodes:` "20%"|"5", `reasons:`
  Drifted/Underutilized/Empty, optional `schedule`+`duration`).
  https://karpenter.sh/docs/concepts/disruption/

### 0.6 NLB cross-zone load balancing (cost vs black-hole)
- NLB defaults cross-zone **OFF**; enabling it routes each node's traffic to
  targets in **all** AZs and thus incurs cross-AZ charges. Disabling it removes
  that cost but each NLB node then serves **only same-AZ targets**.
  https://aws.amazon.com/blogs/networking-and-content-delivery/optimizing-data-transfer-costs-when-using-aws-network-load-balancer/
- **Black-hole risk:** with cross-zone OFF, if an AZ has **zero/one healthy
  target**, clients resolving to that zonal node get **dropped**. Never
  blind-disable.
- Safety valves (target-group attributes) that must gate a disable:
  - `target_group_health.dns_failover.minimum_healthy_targets.count` (default
    1): below it, the zone's node IP is marked **unhealthy in DNS** so new
    clients resolve elsewhere.
  - `target_group_health.unhealthy_state_routing.minimum_healthy_targets.count`
    (default 1): below it, the node **fails open** to all targets.
  - `dns_record.client_routing_policy=availability_zone_affinity` gives
    same-AZ client affinity as a lower-risk alternative to fully disabling
    cross-zone.
  https://docs.aws.amazon.com/elasticloadbalancing/latest/network/target-group-health.html
- Via the **AWS Load Balancer Controller**, cross-zone is set with
  `service.beta.kubernetes.io/aws-load-balancer-attributes:
  load_balancing.cross_zone.enabled=false` (the old
  `...-cross-zone-load-balancing-enabled` annotation is deprecated since
  v2.3.0). Only listed attributes are updated.
  https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/service/annotations/

### 0.7 AWS Cost Anomaly Detection — GetAnomalies shape (read-only)
Request: `DateInterval{StartDate,EndDate}` (required), `MonitorArn`,
`Feedback` (YES|NO|PLANNED_ACTIVITY), `MaxResults`, `NextPageToken`,
`TotalImpact{NumericOperator,StartValue,EndValue}`.
Response `Anomalies[]`: `AnomalyId`, `AnomalyStartDate`, `AnomalyEndDate`,
`DimensionValue`, `MonitorArn`, `AnomalyScore{CurrentScore,MaxScore}`,
`Impact{MaxImpact,TotalActualSpend,TotalExpectedSpend,TotalImpact,
TotalImpactPercentage}`, `RootCauses[]{Service,Region,LinkedAccount,
LinkedAccountName,UsageType,Impact{Contribution}}`, `Feedback`.
https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetAnomalies.html
parse_anomaly_report MUST accept this JSON directly AND a pasted human report.

### 0.8 CloudWatch Logs Insights read-only semantics (allow-list justification)
- `StartQuery` only **initiates** a query over existing log DATA; it creates no
  log group/stream, writes nothing to S3, mutates no infrastructure.
  `GetQueryResults` only **retrieves** an already-run query's rows;
  `StopQuery` only cancels. IAM actions: `logs:StartQuery`,
  `logs:GetQueryResults`, `logs:StopQuery`, scoped to log-group resources.
  https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_StartQuery.html
  https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_GetQueryResults.html
  https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazoncloudwatchlogs.html
  These are the ONLY non-`get-/list-/describe-` verbs allowed, whitelisted as
  proven read-of-data exceptions (see awscost.py contract 1.1).

### 0.9 VPC Flow Logs fields for cross-AZ attribution
- Default (v2) fields: `version account-id interface-id srcaddr dstaddr
  srcport dstport protocol packets bytes start end action log-status`.
  Custom format can add `az-id, subnet-id, instance-id, vpc-id, region,
  pkt-srcaddr, pkt-dstaddr, flow-direction, traffic-path, tcp-flags`.
  https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs.html
- Cross-AZ attribution: group `bytes` by the AZ of each ENI. Preferred path
  when `az-id` is in the custom log format (attribute per flow directly);
  fallback maps `interface-id`/`srcaddr`/`dstaddr` -> ENI -> subnet -> AZ via
  `ec2 describe-network-interfaces` + `describe-subnets`. On EKS the VPC CNI
  attaches ENIs to nodes and assigns pod IPs from them, so
  `describe-network-interfaces` `Attachment`/`PrivateIpAddresses`/`Description`
  yields **ENI -> node/pod IP** attribution (best-effort; degrade gracefully).
  Dominant-port logic keys on `dstport` (e.g. 9095 = Mimir/Loki gRPC).
  https://repost.aws/knowledge-center/vpc-find-traffic-contributors

---

## Reference worked example (the target output shape)
A real investigation the feature should be able to reproduce/assist:
- **Anomaly**: `EBS DataTransfer-Regional-Bytes` cost spike, acct
  348342704569 / us-east-1. EBS *storage* is negligible; the label is a
  **classification artifact** — the charge is entirely **cross-AZ network
  transfer** (~16,470 GB/day, ~$164/day), true step-change 2026-08-31.
  (Consistent with 0.1: Regional-Bytes = cross-AZ; $164/day / $0.02 per
  round-tripped GB ~= 8,235 GB/day each way ~= 16,470 GB/day total two-way.)
- **Root cause (~91%)**: the Grafana observability stack (Mimir/Loki/Tempo)
  on EKS runs a **non-zone-aware** distributed hash-ring — RF=3 replication +
  query fan-out over gRPC **port 9095** — confined to 2 imbalanced AZs
  (Karpenter discovery subnets only in Private-A/Private-B; none in
  us-east-1c). VPC Flow Logs prove port 9095 is ~91% of cross-AZ pod bytes,
  stepping up with a Karpenter scale-up. **Secondary (~8%)**: a cross-zone-
  enabled Mimir NLB. **Ruled out**: EBS volume/snapshot growth, RDS cross-AZ
  replica.
- **Mitigation (primary ~91%)**: make the stack **zone-aware** (Mimir/Loki
  `-distributor.zone-awareness-enabled` + zone labels) and **topology-aware
  routing** (`trafficDistribution: PreferClose`) so RF=3 quorum + query
  fan-out prefer same-AZ. **Structural**: add a us-east-1c Karpenter
  discovery subnet so the ring balances across 3 AZs. **Secondary (~8%)**:
  disable cross-zone on the Mimir NLB — but ONLY after confirming healthy
  targets exist in every enabled AZ (a blind disable black-holes the 1b
  node). Durable changes are GitOps/IaC-owned; the tool PROPOSES, never
  executes.
The architect must encode this class of reasoning generically (not the
customer's specific IDs).

---

## RCA METHODOLOGY (rca.py encodes this; deterministic, evidence-first)

Goal: converge multiple **independent, read-only** evidence sources onto ONE
dominant driver with a **% share**, plus ranked secondaries and an explicit
**ruled-out** list. Never assert a cause from a single source.

### Step A — Frame the incident (Cost Explorer / anomaly report)
From GetAnomalies (0.7) or a pasted report, extract: usage_type, service,
account, region, `$/day` (TotalImpact / days), onset & step-change date,
score. **Classify the usage_type**: if it matches `*DataTransfer-Regional-
Bytes` (or `*-DataTransfer-*` / `*InterZone*`) -> hypothesis class
**CROSS_AZ_NETWORK** (per 0.1), regardless of the service tag (EBS/EC2/...).
Convert `$/day` -> `GB/day` using $0.02 per two-way GB (state the assumption).

### Step B — Localize the bytes (VPC Flow Logs, flowlogs.py)
Run bounded Logs Insights queries (0.8/0.9) to answer:
1. What share of total cross-AZ bytes is cross-AZ vs same-AZ? (rule in 0.1)
2. Which **dstport** dominates cross-AZ bytes, and its %-share? (e.g. 9095)
3. Which **AZ pair** carries it, and is it **imbalanced** (2 AZs only)?
4. When did it **step up** (align to the cost step-change date)?
Output the dominant port, %-of-cross-AZ per driver, GB/day, step-change date.

### Step C — Identify the workload (EKS control plane + LGTM self-metrics)
- `eks describe-cluster`/`list-nodegroups` + `ec2 describe-network-interfaces`
  map the dominant ENIs/IPs to nodes/pods -> name the workload (Mimir/Loki/
  Tempo ingesters, distributors, queriers).
- LGTM self-metrics (via existing deepdive/Mimir query): confirm RF (usually
  3), ring membership per zone, and that the ring is **non-zone-aware**
  (replicas of a series/stream land in arbitrary zones). gRPC 9095 = Mimir/
  Loki inter-component (ingest replication + query fan-out).

### Step D — Explain the imbalance (Karpenter / EC2 topology)
`ec2 describe-subnets` + Karpenter EC2NodeClass discovery tags: if discovery
subnets exist in fewer AZs than the region offers (0.5), the ring is forced
into those AZs -> every RF=3 write crosses a zone boundary -> cross-AZ bytes.
`cloudtrail lookup-events` correlates a Karpenter scale-up / config change to
the step-change date (Step B4).

### Step E — Rule-out logic (mandatory, each with its disproving evidence)
- **EBS storage growth**: `ce` storage usage-type flat + `ec2 describe-
  volumes/snapshots` count/size flat across the step-change -> RULED OUT.
- **Cross-region transfer**: confirm both endpoints in the SAME region (flow
  logs `region`/subnet AZ) -> it's cross-AZ, not cross-region.
- **RDS cross-AZ replica**: no Multi-AZ replica / no matching RDS ENIs in the
  dominant flows -> RULED OUT.
- **NAT/Internet egress**: dominant flows are private-IP to private-IP intra-
  VPC (not via NAT GW / not public) -> not egress.
- **Same-AZ over public IP** (0.1 edge case): check flows are private-IP so the
  charge is genuinely cross-AZ, not EIP-in-one-AZ.

### Step F — Converge & score
`evidence_convergence` lists which of {cost-explorer, cloudtrail,
vpc-flow-logs, eks-control-plane, lgtm-self-metrics} AGREED. The dominant
driver's **% share** comes from Step B's flow-log byte share (the measured
quantity), corroborated by C/D. `confidence` rises with the number of
independent agreeing sources; a single-source claim caps confidence LOW.
Secondary drivers (e.g. cross-zone NLB ~8%) get their own share from the flow
logs. If flow logs are absent, degrade to a clearly-flagged hypothesis
(confidence LOW) — never fabricate a %.

---

## Module map (build agents, disjoint files)

### 1. `awscost.py` — extend read-only discovery (owner: aws-discovery agent)
Extend `ALLOWED` (keeping the read-only guard airtight) with the services RCA
needs, all read-only: `cloudtrail` (lookup-events), `logs` (**start-query /
get-query-results / stop-query** for CloudWatch Logs Insights over VPC Flow
Logs — these read log DATA, create no infra, write nothing to S3; allow-list
them EXPLICITLY as justified read exceptions to the get-/list-/describe-
prefix rule, and add tests that NO other non-prefix verb is allowed),
`eks` (describe-*/list-*), `ec2` (describe-network-interfaces/subnets/route-
tables/nat-gateways/availability-zones), `elbv2` (describe-load-balancers/
target-groups/target-health/listeners), `ce` (get-anomalies already).
aws-vault / SSO profiles: add `list_profiles()` (parse ~/.aws/config) and an
optional `aws-vault exec <profile> --` wrapper (detected or via env
`N2G_AWS_WRAP`), still read-only. Add helpers: cloudtrail_lookup,
logs_insights_query (start->poll get-results, bounded), eks_describe,
ec2_network_topology, elbv2_describe. Owns tests/test_awscost.py.

**Exact read-only calls this module must permit (and ONLY these):**
- `ce`: `get-anomalies`, `get-anomaly-monitors`, `get-anomaly-subscriptions`,
  `get-cost-and-usage`, `get-cost-and-usage-with-resources`,
  `get-dimension-values` (all read; existing prefix rule covers `get-`).
- `cloudtrail`: `lookup-events` (read management events; correlates the
  step-change to a Karpenter/config action). `lookup-events` is a read verb
  but non-prefix -> allow-list explicitly like logs.
- `logs`: `start-query`, `get-query-results`, `stop-query` — the ONLY
  non-`get/list/describe` verbs, justified per 0.8. Also
  `describe-log-groups`, `describe-queries` (prefix-covered).
- `eks`: `describe-cluster`, `list-clusters`, `list-nodegroups`,
  `describe-nodegroup`, `list-fargate-profiles` (prefix-covered).
- `ec2`: `describe-network-interfaces`, `describe-subnets`,
  `describe-route-tables`, `describe-nat-gateways`,
  `describe-availability-zones`, `describe-instances`, `describe-volumes`,
  `describe-snapshots` (prefix-covered; used for topology + storage rule-out).
- `elbv2`: `describe-load-balancers`, `describe-target-groups`,
  `describe-target-health`, `describe-listeners`,
  `describe-target-group-attributes` (prefix-covered; NLB cross-zone + per-AZ
  health).
Test matrix MUST assert: (a) each allow-listed non-prefix verb (`lookup-
events`, `start-query`, `get-query-results`, `stop-query`) is permitted;
(b) a representative WRITE/mutate verb per service (`ec2 run-instances`,
`eks create-cluster`, `logs put-log-events`/`create-log-group`/
`delete-log-group`, `elbv2 modify-target-group-attributes`,
`cloudtrail delete-trail`) is REJECTED; (c) no wildcard slips through.

### 2. `flowlogs.py` — cross-AZ byte attribution (owner: flowlogs agent)
Analyze VPC Flow Logs (via awscost.logs_insights_query) to attribute cross-AZ
bytes by srcaddr/dstaddr AZ, port, and (via ENI->pod when available) workload.
Produce: top cross-AZ flows, the dominant port (e.g. 9095), %-of-cross-AZ per
driver, GB/day, and a step-change date. schema "nr2grafana/flowlogs/v1".
Never raise; degrade to a clear "no flow logs configured" note. Owns
tests/test_flowlogs.py (canned Logs Insights results).

**Logs Insights query templates (GENERIC, placeholders; per 0.9).** The module
builds these strings and runs them bounded via awscost.logs_insights_query.
Prefer `az_id` when present in the log format; else fall back to ENI mapping.

Bytes by AZ (when custom format has az-id):
```
fields @timestamp, srcAddr, dstAddr, dstPort, bytes, azId
| filter action = 'ACCEPT'
| stats sum(bytes)/1073741824 as gb by azId
| sort gb desc
```
Cross-AZ bytes by destination port (dominant-driver discovery):
```
parse @message "* * * * * * * * * * * * * * *" as version, acct, eni,
  srcAddr, dstAddr, srcPort, dstPort, protocol, packets, bytes, start,
  end, action, logStatus, azId
| filter action = 'ACCEPT' and srcAddr like /^10\./ and dstAddr like /^10\./
| stats sum(bytes)/1073741824 as gb by dstPort
| sort gb desc
| limit 20
```
Top cross-AZ talker pairs (for ENI->workload attribution):
```
| filter action = 'ACCEPT'
| stats sum(bytes) as b by srcAddr, dstAddr, dstPort
| sort b desc
| limit 50
```
Step-change detection: same query bucketed `by bin(1d)` over a window that
straddles the suspected onset; the module finds the day where daily GB jumps.
Placeholders the caller injects: `<FLOW_LOG_GROUP>`, `<START_EPOCH>`,
`<END_EPOCH>`, `<CIDR>` (defaults to RFC1918 private ranges). Bound results
(limit, time window, poll cap). Compute `%-of-cross-AZ` = driver_gb /
sum(all cross-AZ gb). ENI->pod: resolve `srcAddr`/`dstAddr` to the ENI whose
`PrivateIpAddresses` contains it, then its `Description`/`Attachment` names
the node; degrade to "IP only" if unmapped.

### 3. `rca.py` — cost-anomaly root-cause engine (owner: rca agent)
```python
def parse_anomaly_report(text_or_json) -> Dict   # accept a pasted report OR a CE anomaly
def analyze(anomaly, aws=None, flowlogs=None, deepdive=None, packing=None,
            tco=None, k8s=None, cfg=None, log=None) -> Dict
```
schema "nr2grafana/rca/v1": incident (usage_type, service, account, region,
$/day, GB/day, onset/step-change), cause {dominant:{share,summary,evidence},
secondary:[...], ruled_out:[...]}, evidence_convergence (which sources
agreed: cost-explorer, cloudtrail, vpc-flow-logs, eks-control-plane,
lgtm-self-metrics), confidence. Encode the domain knowledge the architect
documents: DataTransfer-Regional-Bytes = cross-AZ classification artifact;
cross-AZ from non-zone-aware ring replication/query fan-out; AZ imbalance from
Karpenter subnet gaps; NLB cross-zone; and the RULE-OUT logic (EBS storage
flat, RDS replica none). Owns tests/test_rca.py.

**Implement Steps A–F from RCA METHODOLOGY above.** `parse_anomaly_report`
handles BOTH the GetAnomalies JSON (0.7 field names exactly) and a pasted
human report (regex the usage_type, `$/day`, GB/day, step-change date, account,
region). Deterministic mapping of usage_type -> hypothesis class:
`*DataTransfer-Regional-Bytes`/`*InterZone*` -> CROSS_AZ_NETWORK;
`*DataTransfer-Out-Bytes` -> INTERNET_EGRESS; storage usage-types ->
STORAGE_GROWTH. `share` for the dominant/secondary comes from flowlogs byte %.
`confidence` = f(count of agreeing evidence_convergence sources); flowlogs
absent -> LOW + explicit note. `ruled_out[]` each carries the disproving
evidence string (Step E).

### 4. `mitigate.py` — reliability-safe mitigation planner (owner: mitigate agent)
```python
def plan(rca, deepdive=None, packing=None, capacity=None, cfg=None) -> Dict
```
schema "nr2grafana/mitigation/v1": ranked mitigations, each: title, expected
%/$ saved, the change, **reliability guardrails** (preconditions that MUST
hold or it breaks something — e.g. "confirm healthy NLB targets in EVERY
enabled AZ before disabling cross-zone, else the single-target AZ black-
holes"), keeps_availability/keeps_durability/keeps_performance flags,
handles_current_traffic (validated against deepdive capacity/traffic so the
change still serves the current rate), owner ("GitOps/IaC — proposal only,
never executed"), and generated **paste-ready configs** (Mimir/Loki zone-
aware values + zone labels; Kubernetes Service `trafficDistribution:
PreferClose`; Karpenter NodePool/EC2NodeClass with 3-AZ discovery subnets;
NLB cross-zone annotation with the target-health safety gate). All configs
GENERIC (placeholders, no customer values). Owns tests/test_mitigate.py.

Ranking = by expected $/day saved, but any mitigation whose reliability.check
returns `safe=false` is DEMOTED and its `keeps_*` set false with a loud
caveat. Each mitigation calls reliability.check (module 5) and copies its
`required_preconditions` into `reliability_guardrails`. `handles_current_
traffic` cross-checks deepdive's current per-zone request/ingest rate against
post-change per-zone capacity (e.g. after PreferClose, does each zone still
have >= replicas to serve its share?).

**Paste-ready GENERIC config templates the planner emits** (placeholders in
`<ANGLE_BRACKETS>`; NO customer values). Each references its 0.x source.

M1 — Mimir zone-aware (primary; per 0.2). Helm/values fragment + flags:
```yaml
# Mimir: enable zone-aware replication (write+read path). RF must be <= #zones.
# Migrate LIVE ring zone-by-zone via rollout-operator; one zone at a time.
mimir:
  structuredConfig:
    ingester:
      ring:
        zone_awareness_enabled: true          # -ingester.ring.zone-awareness-enabled
        replication_factor: <RF_DEFAULT_3>
    distributor:
      ring: {}
# Flags (set on the right components):
#   distributors, rulers:  -distributor.zone-awareness-enabled=true   # write path
#   queriers:              -distributor.zone-awareness-enabled=true   # read path
#   each ingester:         -ingester.ring.instance-availability-zone=<ZONE_A|ZONE_B|ZONE_C>
rollout_operator: { enabled: true }           # one zone at a time
ingester:
  zoneAwareReplication: { enabled: true }
  # zones -> your 3 AZs; DEPLOY ACROSS >= RF ZONES (RF=3 -> 3 AZs)
  # <ZONE_A>=<AZ_1>  <ZONE_B>=<AZ_2>  <ZONE_C>=<AZ_3>
```

M2 — Loki zone-aware (primary; per 0.3):
```yaml
loki:
  config:
    distributor: { zone_awareness_enabled: true }   # write path
    querier:     { zone_awareness_enabled: true }   # read path
    ingester:
      lifecycler:
        ring: { replication_factor: <RF_3>, zone_awareness_enabled: true }
# Deploy 3 per-zone ingester StatefulSets (zone-a/zone-b/zone-c), each pod
# labeled availability-zone=<ZONE>. Managed by rollout-operator
# (rollout-group: ingester). Migration: existing ingester PDB maxUnavailable:0,
# DOUBLE max-series/stream limits first, enable write path, WAIT
# query_ingesters_within (~3h), THEN enable read path.
```

M3 — Kubernetes topology-aware routing (primary; per 0.4). Requires k8s >=1.31
(beta) / >=1.33 (GA). Apply to the intra-stack Services (e.g. distributor,
querier, gateway):
```yaml
apiVersion: v1
kind: Service
metadata:
  name: <SERVICE_NAME>
spec:
  trafficDistribution: PreferClose     # >=1.33 alias: PreferSameZone
  selector: { <APP_SELECTOR> }
  ports: [ { port: <PORT>, targetPort: <TARGET_PORT> } ]
# GUARDRAIL: only when endpoints are spread across ALL serving zones with
# >= replicas/zone to carry current per-zone traffic (PreferClose has NO
# overload safeguard; imbalance -> hotspot).
```

M4 — Karpenter 3-AZ discovery (structural fix for imbalance; per 0.5):
```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata: { name: <NODECLASS_NAME> }
spec:
  role: "<KARPENTER_NODE_ROLE>"
  amiSelectorTerms: [ { alias: <AMI_ALIAS_e.g._al2023@latest> } ]
  subnetSelectorTerms:                 # MUST resolve a subnet in EACH of 3 AZs
    - tags: { karpenter.sh/discovery: "<CLUSTER_NAME>" }
  securityGroupSelectorTerms:
    - tags: { karpenter.sh/discovery: "<CLUSTER_NAME>" }
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata: { name: <NODEPOOL_NAME> }
spec:
  template:
    spec:
      nodeClassRef: { group: karpenter.k8s.aws, kind: EC2NodeClass, name: <NODECLASS_NAME> }
      requirements:
        - key: topology.kubernetes.io/zone
          operator: In
          values: ["<AZ_1>", "<AZ_2>", "<AZ_3>"]   # all 3 AZs
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]                      # or ["spot","on-demand"]
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: <e.g._1m>
    budgets:
      - nodes: "10%"                                  # rate-limit voluntary disruption
  limits: { cpu: "<CPU_LIMIT>" }
# ACTION for the imbalance root cause: tag a subnet in the MISSING AZ (e.g.
# us-east-1c) with karpenter.sh/discovery=<CLUSTER_NAME> so the ring can
# balance across 3 AZs. Discovery subnet gap -> nodes cannot launch there.
```

M5 — NLB cross-zone (secondary; per 0.6) — GATED, never blind-disable:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: <NLB_SERVICE_NAME>
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: "external"
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: "ip"
    # SECONDARY saving: turn OFF cross-zone (removes cross-AZ LB charge) ...
    service.beta.kubernetes.io/aws-load-balancer-attributes: >-
      load_balancing.cross_zone.enabled=false
    # ... ONLY WITH these health gates so a thin AZ never black-holes:
    service.beta.kubernetes.io/aws-load-balancer-target-group-attributes: >-
      target_group_health.dns_failover.minimum_healthy_targets.count=1,
      target_group_health.unhealthy_state_routing.minimum_healthy_targets.count=1
    # Lower-risk alternative to disabling: same-AZ affinity, keep cross-zone on
    #   dns_record.client_routing_policy=availability_zone_affinity
spec:
  type: LoadBalancer
  selector: { <APP_SELECTOR> }
  ports: [ { port: <PORT>, targetPort: <TARGET_PORT> } ]
# GUARDRAIL: confirm >=1 (ideally >=2) HEALTHY target in EVERY enabled AZ via
# elbv2 describe-target-health BEFORE disabling cross-zone. keeps_availability
# = false unless every enabled AZ passes.
```

### 5. `reliability.py` — guardrail library (owner: reliability agent)
Encodes the safety rules every mitigation is checked against (RF/quorum math,
per-AZ target health, PDB, zone-aware ring migration safety, "never blind-
disable cross-zone", never CPU-limit ingesters, keep RF/retention). `check(
mitigation, context) -> {safe, violations, required_preconditions}`. Owns
tests/test_reliability.py. mitigate.py consumes it.

**Rule set (each returns violations + required_preconditions):**
- QUORUM/RF (0.2/0.3): number of zones after change >= RF; healthy zones >=
  `floor(RF/2)+1`; RF and retention unchanged by the mitigation.
- ZONE-AWARE MIGRATION SAFETY (0.2/0.3): rollout touches ONE zone at a time;
  existing ingester PDB `maxUnavailable:0` during reshuffle; max-series/stream
  limits doubled first; write-path enabled and `query_ingesters_within`/
  `query_store_after` wait observed BEFORE read-path; never deploy to fewer
  zones than RF (data-loss caveat).
- PREFERCLOSE OVERLOAD (0.4): endpoints spread across all serving zones AND
  per-zone replicas >= needed for current per-zone traffic (from deepdive);
  else violation "hotspot risk — PreferClose has no overload safeguard".
- NLB TARGET HEALTH (0.6): for every ENABLED AZ, `describe-target-health`
  shows >=1 healthy target; require the two `minimum_healthy_targets.count`
  gates set; else "black-hole risk — do not disable cross-zone".
- PDB present for any StatefulSet being rolled; consolidation budgets in place
  for Karpenter changes (0.5) so node churn is rate-limited.
- NEVER CPU-limit ingesters (throttling breaks ingest); keep requests.
- HANDLES-CURRENT-TRAFFIC: post-change per-zone capacity >= current per-zone
  rate (deepdive) — else keeps_performance=false.

### 6. `ai.py` — RCA/mitigation AI framing (owner: ai agent)
Add `_RCA_SYSTEM`: "Analyze this AWS cost anomaly and the converged evidence;
propose how to cut the cost WITHOUT reducing availability/durability/
performance or the ability to serve the CURRENT traffic rate; respect the
reliability guardrails; output strict JSON {root_cause, mitigations:[{title,
saving, change, preconditions, keeps_*}], config_notes}." suggest_fix/analyze
picks it by context["mode"]=="rca". Owns ai.py + tests/test_ai.py.
The system prompt MUST instruct the model to treat `*DataTransfer-Regional-
Bytes` as cross-AZ network (not storage), to never propose a change that drops
RF/retention or CPU-limits ingesters, and to keep configs GENERIC. (Anthropic
Claude Messages API is the LLM per repo convention — no new deps; ai.py stays
transport-agnostic as in 1.x.)

### 7. `aicontext.py` — RCA artifacts in the bundle (owner: aicontext agent)
build_context includes rca/mitigation/flowlogs artifacts; add
`analyze_cost(assistant, context)` that runs the RCA/mitigation AI flow over
the bundle. Owns aicontext.py + tests/test_aicontext.py.

### 8. `mcp.py` — AWS anomaly/CloudWatch MCP (owner: mcp agent)
Extend generate_mcp_config with the AWS Cost Anomaly / CloudWatch MCP servers
(env/profile refs, no secrets). Owns mcp.py + tests/test_mcp.py.
Reference the AWS Cost Explorer / CloudWatch read-only MCP servers by env/
profile (`AWS_PROFILE`, region), never inline creds; keep read-only.

### 9. `web/server.py` + `store.py` (owner: server agent)
Routes: POST /api/rca (accept a pasted anomaly report OR {anomaly_id} from CE;
runs discovery + flowlogs + rca; job; persist "rca"), POST /api/mitigate
(mitigate.plan; persist "mitigation"), POST /api/rca/analyze (AI over the
bundle; job), GET /api/aws/profiles (awscost.list_profiles), GET /api/aws/
anomalies (ce get-anomalies), GET /download/mitigation-configs.zip. Store
ARTIFACT_KINDS += rca/mitigation/flowlogs. /api/state features += rca. Owns
server.py, store.py, tests/test_web.py, tests/test_e2e_mock.py.

### 10. `cli.py` + `interactive.py` (owner: cli agent)
`cost rca [--anomaly-file F | --anomaly-id ID | --paste] [--profile P]
[--flow-logs-group G]`, `cost mitigate [rca.json]`, `aws profiles`. Wizard:
"Investigate a cost anomaly (RCA + mitigation)". Owns cli.py, interactive.py,
tests/test_cli.py.

### 11. `web/ui.py` (owner: ui agent)
A **Cost RCA** view: paste a cost-anomaly report (textarea) OR pick from AWS
anomalies + an aws-vault/SSO profile selector; run RCA; render the root-cause
breakdown (driver %, evidence-convergence chips, ruled-out), the ranked
mitigation plan with per-item reliability guardrails as amber "precondition"
chips + green keeps_* chips + copyable configs (Karpenter/Mimir/Loki/Service/
NLB) with a target selector, an "estimated $/day saved without reducing
reliability" headline, an "Ask AI to analyze" button, and Download configs.
esc() discipline; keep test hooks; single module string; no external assets;
< 620KB. Owns ui.py ONLY.

### 12. `tools/fake_aws.py` + e2e (owner: mock agent)
Extend fake_aws for cloudtrail lookup-events, logs start-query/get-query-
results (VPC Flow Logs with a port-9095 cross-AZ scenario matching the
reference), eks describe, ec2 describe-network/subnets (2-AZ imbalance,
missing 1c), elbv2 target-health (one AZ with a single target), ce
get-anomalies (an EBS DataTransfer-Regional-Bytes anomaly). Deterministic.
Extend tests/test_e2e_mock.py: drive rca.analyze end to end against the fake
AWS + reproduce the cross-AZ RCA (dominant driver = cross-AZ ring on port
9095, secondary = NLB, ruled-out EBS storage) and mitigate.plan (zone-aware +
trafficDistribution + 1c subnet, NLB gated on target health) with the
reliability guardrails present. (mock agent coordinates test_e2e_mock with
the server agent — server owns it; mock adds fake_aws + drives via a helper.)
Fake data MUST make the numbers self-consistent with 0.1 (e.g. daily GB that,
at $0.02/two-way-GB, reproduces the ~$164/day headline) and the GetAnomalies
JSON shape from 0.7 exactly (RootCauses[].UsageType = "<Region>-DataTransfer-
Regional-Bytes"). The elbv2 fixture MUST include one AZ with a single healthy
target so the NLB guardrail (keeps_availability=false unless gated) is exercised.

## Cross-cutting
1. stdlib only, py3.9, ASCII/LF/4-space/79-col.
2. **AWS strictly READ-ONLY** (allow-list; logs start/get-query-results are
   the only justified non-prefix reads and must be proven unable to mutate
   infra). aws-vault/SSO respected. Creds never read/logged/embedded.
3. The tool PROPOSES; it NEVER executes AWS/K8s changes (no EKS write, no
   apply). Every mitigation states its reliability preconditions and defaults
   to the safe option; NO cost cut may reduce availability/durability/
   performance or the ability to serve current traffic without a loud caveat
   and keeps_*=false.
4. All generated configs are GENERIC and paste-ready (GitOps-owned).
5. Every module tested; full suite stays green. Coordinator bumps to 1.9.0
   and writes docs/README after build.
