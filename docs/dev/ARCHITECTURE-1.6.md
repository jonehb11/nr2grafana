# nr2grafana 1.6 — deep LGTM optimization, AI-first, Grafana MCP

Three themes:
1. **Deep stack optimization** — go beyond 1.5's cardinality/cost: analyze
   capacity, churn, right-sizing (compute saved while keeping performance),
   node bin-packing/consolidation, durability/availability, and data-transfer
   cost — and propose configs with estimated savings, **never sacrificing
   durability, availability, or performance**.
2. **AI as a first-class tenant** — every artifact the program produces is
   packaged into a compact, LLM-optimized context bundle so an AI (local CLI
   agent or API) can troubleshoot the dashboards and the obs stack end to
   end, and find every optimization without risking the stack.
3. **Grafana MCP** — nr2grafana can connect to a Grafana MCP server and can
   emit a ready MCP config wiring the Grafana MCP server + nr2grafana context
   into the user's local AI (claude/kiro).

Builds on 1.1–1.5 (all contracts hold). Version 1.6.0. **Zero deps: Python
3.9+ stdlib only.** Secrets in memory only. New Relic strictly read-only.

## Domain source (READ, do not copy customer values into code)

Two reference files carry real, expert LGTM optimization intelligence. READ
them for domain grounding; the engine you build must be GENERIC — do NOT hard-
code any customer value from them (account IDs, bucket names, cluster names,
specific node counts). They are illustrative, not config.
- `/private/tmp/claude-503/-Users-jon-Nr2Graf/14c17ba7-5d4a-4fa8-b780-2d22fbc117df/scratchpad/obsstack/lgtm_deepdive.py`
- `/private/tmp/claude-503/-Users-jon-Nr2Graf/14c17ba7-5d4a-4fa8-b780-2d22fbc117df/scratchpad/obsstack/LGTM-OPTIMIZATION-PLAN.md`

### Guardrails the engine MUST encode (from the reference)
Every recommendation carries risk flags; cost cuts must never silently trade
away durability/availability/performance:
- **Right-size to peak, not average**: suggested request = observed 24h peak
  × 1.5 (CPU) / × 1.3 (mem); never below observed peak. Mark
  `keeps_performance: true` only when the suggestion stays above peak.
- **Never CPU-limit Mimir ingesters** (WAL replay/compaction throttling →
  OOO + OOM). Flag any CPU limit on ingesters as a durability FAIL.
- **Memory request == limit on ingesters.**
- **Do NOT disable zone-aware replication** to save nodes — it is what makes
  packing safe (a node holds one zone; a node loss drops one replica →
  quorum survives). Recommend zone-aware packing, not its removal.
- **RF=3 / retention / scrape intervals / exemplars / spanmetrics**: never
  recommend reducing these for cost without a loud durability/scope caveat.
- **PDB maxUnavailable ≥ 1** on every ingester (0 blocks drains AND
  consolidation). **priorityClassName** on Loki/Tempo ingesters, not just
  Mimir.
- **S3 lifecycle**: Mimir bucket must have NO object-expiry (compactor owns
  deletes); Loki backstop expiry must sit well past retention.
- **Series elimination (unused metrics) is the lever that scales down every
  other cost** — prefer it over infra tweaks; drop at remote_write
  write_relabel, not at scrape (keep local debug data).
- **Zero-request pods make packing math fiction** — flag missing requests
  first.
- Bin-pack constraints: Mimir pods of different zone groups never share a
  node (same-zone MAY — the packing win); Loki/Tempo ingesters one-per-node
  (RF=3). Σ(mem limits)/node capacity > 1.0 = a burst can OOM the node.

## 1. `nr2grafana/deepdive.py` — metric-driven deep analysis (owner: deepdive agent)

A PromQL client (reuse GrafanaLive.ds_query / proxy, or a direct
Prometheus/Mimir/Loki URL) that reads component self-metrics
(`cortex_*`, `loki_*`, `tempo_*`, `otelcol_*`, `prometheus_remote_storage_*`,
`container_memory_working_set_bytes`) and produces findings. Port the
metric-driven sections of `lgtm_deepdive.py`:

```python
class PromClient:  # .vector/.scalar/.by/.json over /api/v1 ; never raises
def analyze(prom=None, mimir=None, loki=None, grafana=None, cfg=None,
            log=None) -> Dict   # schema "nr2grafana/deepdive/v1"
```
Sections → findings (each: `severity` FAIL/WARN/INFO, `area`
capacity|cardinality|churn|network|loki|tempo|efficiency, `title`,
`evidence` dict, `rationale`, `config` [ {target, language, snippet, note} ],
`est_savings` {series|bytes_per_day|monthly_usd|compute}, risk flags
`keeps_performance`/`keeps_durability`/`keeps_availability`, `keeps_intact`):
- **capacity**: bytes/series from RSS, GOMEMLIMIT-based real series capacity
  vs configured `max_global_series` (the "10M limit is not capacity" finding).
- **cardinality/churn**: top metrics/labels by series, `samples_per_series/s`
  (low ⇒ churn/stale), series churn %/h, duplicate Prometheus replicas
  without HA tracker (2× ingest).
- **network**: remote_write wire GB/month → cost by egress path (cross-AZ,
  TGW, NAT, GWLB) as clearly-labeled per-GB assumptions.
- **loki**: chunk size p50 / flush reasons (small idle chunks ⇒ stream
  cardinality, not volume), failed flushes, WAL/PVC pressure, S3 non-2xx,
  stream-label cardinality.
- **tempo/otel**: spans/s, metrics-generator active series, exporter
  failures/queue.
Reuses `costmodel` for $ where possible. Owns tests/test_deepdive.py (stub
PromClient with canned self-metrics).

## 2. `nr2grafana/packing.py` — topology, bin-pack, right-sizing, durability (owner: packing agent)

Kubernetes-aware analysis, **all optional** (only runs when `kubectl` is on
PATH and `enabled=True`; degrades to a clear "kubectl not available" note
otherwise — the tool must stay usable without a cluster).

```python
def kubectl_available() -> bool
def collect_topology(namespaces, pool_selector, log=None) -> Dict
    # nodes (alloc/req/limits, instance type, zone, price), pods (requests,
    # zone_group, anti-affinity, dnd, priority, restarts/OOM), PVC→AZ pins
def bin_pack(pods, shape, daemon_cpu, daemon_mem, prices, shapes) -> List
    # first-fit-decreasing by mem with the reference's real constraints
    # (Mimir zone groups never mix; Loki/Tempo ingesters one-per-node);
    # returns bins with mem_util, cpu_util, Σlimits/capacity
def packing_sim(topo, prices, shapes, overrides=None) -> Dict
    # candidate shapes ranked by $/mo; current pool cost; the "floor"
def rightsizing(usage_by_component, requests_by_component) -> Dict
    # suggested requests (peak×1.5 cpu / ×1.3 mem, never below peak) +
    # compute (cores/GiB) and $ saved, keeps_performance flag
def durability_audit(topo, ring_health=None) -> List
    # PDBs allow 0, ingesters without priorityClass, CPU-limited ingesters,
    # PVC zone pins that prevent co-location, AZ hosting 2 zones, OOMs
def karpenter_analyze(topo, packing_sim, cfg=None, log=None) -> Dict
    # DEEP Karpenter analysis + a proposed optimized NodePool for the obs pool
def analyze(cfg=None, prices=None, log=None) -> Dict  # schema "nr2grafana/packing/v1"
```
Module-level default `PRICE_HR` / `SHAPES` tables (generic AWS on-demand,
clearly overridable via cfg/env `PRICES`). Findings carry the same risk
flags as §1. Owns tests/test_packing.py (feed canned topology dicts — do NOT
require a real cluster; stub `kubectl` via a fake command or inject topology).

### Deep Karpenter analysis (`karpenter_analyze`)
The observability workloads run on their own Karpenter NodePool; the nodepool
config is where cost and availability meet. Read NodePools + NodeClaims +
EC2NodeClass (via kubectl, OPTIONAL — degrade cleanly without a cluster) and
produce findings + a **proposed optimized NodePool YAML** for the obs pool.
Analyze and recommend across:
- **Instance requirements**: are `karpenter.k8s.aws/instance-category /
  family / size` scoped to what the workload needs? Memory-bound backends
  (Mimir/Loki ingesters) → `r`-class (memory/$); flag `m`-class on a
  memory-bound pool. Recommend the shape from the bin-pack floor (§packing_sim)
  as the primary size, with one larger size for burst. Offer a **Graviton
  (arm64, r7g/m7g)** option (~5–10% cheaper) gated on arm64 images being
  available — mark it as an opt-in, not automatic.
- **capacity-type (spot vs on-demand)**: stateful ingesters (Mimir/Loki/Tempo)
  MUST stay **on-demand** (spot reclaim = ring churn / WAL loss risk) —
  recommend on-demand for the ingester nodepool/requirement and **spot with
  on-demand fallback** only for stateless components (queriers, distributors,
  gateways). Never blanket-spot an ingester pool; flag it if present.
- **disruption**: `consolidationPolicy: WhenEmptyOrUnderutilized` +
  `consolidateAfter` (e.g. 30m–1m) to reclaim underutilized nodes is the main
  cost lever; **disruption budgets** must respect PDBs so consolidation never
  evicts more than one ingester at a time (propose a budget like
  `nodes: "1"` or a per-reason budget, and `schedule`/`duration` windows for
  ingester pools). Flag `consolidationPolicy: WhenEmpty` only (leaves
  underutilized nodes running) as a cost finding.
- **expireAfter**: `Never` → AMI/patch drift (security/compliance FAIL on
  regulated clusters). Recommend a finite `expireAfter` PLUS a paced rotation
  (drift + disruption budget) rather than never; note this both patches and
  re-packs. Do NOT recommend aggressive expiry that churns ingesters.
- **limits**: NodePool `limits.cpu/memory` sized to the projected footprint
  (bin-pack floor + Blue/growth headroom) so a runaway can't scale the pool
  unbounded; flag missing limits and over-limit usage.
- **do-not-disrupt**: pods/nodes with `karpenter.sh/do-not-disrupt` that block
  consolidation → list them and explain the tradeoff (ingesters may want DND
  during a window, but permanent DND stacks nodes).
- **weight / multiple nodepools**: if the obs pool coexists with a general
  pool, recommend `weight` + a `nodeSelector`/taint so obs backends land on
  the obs pool and general pods don't squat memory-optimized nodes.
- **AZ / topology**: nodepool `topology.kubernetes.io/zone` requirement must
  cover the AZs the zone-aware ingesters need; flag a pool that can't place a
  zone's PVC.
Output (inside the packing schema): `{"karpenter": {"nodepools":[...current
summary...], "findings":[...], "proposed_nodepool_yaml": "...generic,
commented, paste-ready...", "proposed_ec2nodeclass_note": "...",
"est_savings":{"monthly_usd":..,"nodes":..,"keeps_availability":true}}}`.
The proposed YAML must be GENERIC (placeholders for cluster/AMI/role/subnet
names, never customer values) and every cost lever must keep availability
(on-demand ingesters, budgets respecting PDBs). Cover Karpenter in
tests/test_packing.py with a canned nodepool/nodeclaim dict (no real cluster).

## 3. `nr2grafana/aicontext.py` — AI-first context bundle (owner: aicontext agent)

Make every artifact trivially consumable by an AI.

```python
def build_context(store, slug="", include=None, grafana=None,
                  deepdive=None, redact=True) -> Dict
    # schema "nr2grafana/ai-context/v1": assembles dashboard summary +
    # requirements + diagnosis + parity + samples + cost + optimize +
    # deepdive + packing into ONE structured bundle, with a legend of what
    # each field means and a task preamble ("you are troubleshooting a NR->
    # Grafana migration and the LGTM stack; here is everything known").
    # redact=True strips any secret-looking values defensively.
def to_markdown(context) -> str      # compact, LLM-optimized, section per artifact
def to_prompt(context, question="") -> str   # ready single-string prompt
def troubleshoot(assistant, context, question="") -> Dict
    # feed to an AIAssist/LocalAgent (duck-typed .chat); return
    # {"answer", "backend"} ; never raise (AI errors -> actionable text)
```
The bundle must be COMPACT (summaries + top-N, not raw dumps) and STABLE
(deterministic ordering). Owns tests/test_aicontext.py.

## 4. `nr2grafana/mcp.py` — Grafana MCP integration (owner: mcp agent)

A minimal stdlib MCP (JSON-RPC 2.0) client + config generator.

```python
class MCPError(Exception): ...
class MCPClient:
    # transport: stdio subprocess (command list) OR http/SSE base url
    def __init__(self, command=None, url=None, headers=None, timeout=30)
    def initialize(self) -> Dict           # MCP initialize handshake
    def list_tools(self) -> List[Dict]
    def call_tool(self, name, arguments) -> Dict
    def close(self) -> None
    # context-manager; never leak the subprocess; errors -> MCPError w/ hint
def generate_mcp_config(grafana_url, kind="claude", n2g_context_path="",
                        include_grafana=True) -> Dict
    # emit a ready MCP servers config: the Grafana MCP server
    # (mcp-grafana / "grafana" server, GRAFANA_URL + GRAFANA_SERVICE_ACCOUNT
    # _TOKEN from env, token NEVER written into the file — reference the env
    # var) plus an optional nr2grafana context entry. kind: claude|kiro|generic
def probe(command=None, url=None) -> Dict  # {"ok","tools":[...],"error"?}
```
Token safety: the generated config references `${GRAFANA_SERVICE_ACCOUNT_TOKEN}`
/ env, never embeds a secret. Owns tests/test_mcp.py — test against a tiny
in-test fake MCP server (a Python subprocess speaking JSON-RPC over stdio)
for initialize/list_tools/call_tool, and config generation shape + no-secret
guarantee.

## 5. Web + CLI + Store (owner: server-cli agent)

server.py routes (existing job/Session/artifact conventions):
- `POST /api/deepdive {prom?, mimir?, loki?, kube?, pricing?}` -> job;
  runs deepdive.analyze (+ packing.analyze when kube requested & available);
  persists "deepdive". `GET /api/deepdive?slug=`.
- `GET /api/ai/context?slug=` -> aicontext.build_context; `?format=markdown`
  returns text/markdown. `POST /api/ai/troubleshoot {slug, question}` ->
  aicontext.troubleshoot via the configured backend (job).
- `GET/POST /api/mcp/config` (generate config; POST persists non-secret
  prefs), `POST /api/mcp/probe` (probe a Grafana MCP server).
- `GET /download/ai-context.md?slug=` and `/download/ai-context.json?slug=`.
- Store ARTIFACT_KINDS += "deepdive","packing","ai-context". /api/state
  features += "deepdive","ai_context","mcp".
cli.py: `deepdive [--prom URL --mimir URL --loki URL] [--kube] [--pricing F]
[-o OUT]` (prints findings ranked by severity with est savings + risk flags,
writes deepdive-report.json + config/ snippets); `ai-context [slug] [--markdown]
[-o FILE]`; `ai troubleshoot [slug] --question Q` (uses the configured AI
backend); `mcp config [--kind claude|kiro] [-o FILE]`, `mcp probe [--url URL |
--command "..."]`. Wizard: "Deep-dive the LGTM stack", "Export AI context",
"Set up Grafana MCP". Owns: server.py, cli.py, interactive.py, store.py,
tests/test_web.py, tests/test_cli.py. Lazily import sibling modules.

## 6. Web UI (owner: ui agent, ui.py ONLY)

- **Stack** nav item (deep-dive): run button (prom/mimir/loki URLs + a
  "include Kubernetes (needs kubectl)" toggle), findings grouped by area
  (Capacity / Cardinality / Efficiency / Durability / Cost / Network /
  Karpenter) each a card with severity chip, evidence numbers, the config
  snippet (copyable, target selector), estimated saving, and clear **risk
  chips** ("safe — keeps performance/durability/availability" green, or amber
  caveat). A packing table (candidate shapes → nodes → $/mo → mem util →
  Σlimits/capacity) and a **Karpenter** card showing the current nodepool
  summary plus the proposed optimized NodePool YAML (copyable) with its
  availability/cost rationale. A headline "estimated $X/mo and N cores
  saveable without reducing durability/availability/performance".
- **AI** view: show the context bundle (collapsible per-artifact sections),
  a "Copy AI context" + "Download .md/.json", a big **Troubleshoot with AI**
  box (question input → runs /api/ai/troubleshoot → rendered answer), and an
  **MCP** panel (generate/copy the Grafana MCP config for claude/kiro, a
  "Probe MCP server" button). Reuse the existing chart()/errorCard/console/
  jsonDetails/copy helpers and the AI backend picker.
- Friendliness: plain-language explanations + tooltips for every stack term
  (active series, bytes/series, bin-pack, zone-aware). esc() discipline; keep
  test hooks; single module string; no external assets; < 480KB.

## 7. Mock stack + e2e (owner: mock agent)

Extend tools/mock_stack.py so deepdive + ai-context + mcp are demoable
offline:
- Fake Prometheus/Mimir self-metrics: `cortex_distributor_received_samples_total`,
  `cortex_ingester_memory_series`, `container_memory_working_set_bytes`
  (ingester RSS), `cortex_ingester_memory_series_created_total`,
  `prometheus_remote_storage_bytes_total`, plus cardinality API responses —
  with an obvious churn/duplicate-replica/high-cardinality scenario.
- Fake Loki self-metrics (chunk size small, a failed-flush scenario).
- A tiny **fake MCP server** script (stdio JSON-RPC) the mcp tests/e2e use.
- Extend tests/test_e2e_mock.py: drive deepdive.analyze end to end (asserts
  capacity + cardinality + a churn finding with config + savings), build an
  ai-context bundle over a converted dashboard, and an MCP probe against the
  fake server.

## Cross-cutting
1. stdlib only, py3.9, ASCII/LF/4-space/79-col, docstrings.
2. Every cost/compute saving is an ESTIMATE with clear assumptions; every
   recommendation states its risk to durability/availability/performance and
   defaults to the SAFE option. Never recommend cutting RF/retention/zone-
   awareness/scrape-interval for cost without a loud caveat + `keeps_*` false.
3. kubectl/cluster access is OPTIONAL; the tool stays fully usable without it.
4. Secrets never written to config files or artifacts (MCP config references
   env vars; ai-context redacts).
5. Every module tested; full suite stays green
   (`N2G_DB=$(mktemp) python3 -m unittest discover -s tests`).
6. Coordinator bumps version to 1.6.0 and writes docs (deep-dive, ai-context,
   mcp) + README after build.
