# Deep LGTM optimization, AI-first troubleshooting, Grafana MCP

Since 1.6 nr2grafana goes beyond migrating dashboards: it analyzes the whole
LGTM stack for cost, efficiency, durability, availability, and scalability —
and packages everything so an AI can troubleshoot it for you.

> Every cost/compute saving is an **estimate** with clearly labeled
> assumptions, and every recommendation states its risk to **durability,
> availability, and performance** and defaults to the safe option. Nothing
> here trades away RF, retention, zone-awareness, scrape intervals, or puts
> stateful ingesters on spot to save money.

## Deep-dive (`deepdive` / web **Stack** view)

Point it at your Prometheus/Mimir/Loki (and optionally your cluster) and it
produces findings grouped by area:

- **Capacity** — real bytes/series from ingester RSS and the GOMEMLIMIT-based
  *actual* series ceiling versus the configured `max_global_series` (the
  "the limit is not capacity" finding), so you plan growth on real numbers.
- **Cardinality & churn** — top metrics/labels by series, a low
  samples-per-series ratio (churn/stale series), and duplicate Prometheus
  replicas ingesting everything twice.
- **Efficiency / right-sizing** — CPU/memory requests versus 24h peak usage,
  with a suggested request (peak × 1.5 CPU / × 1.3 memory, never below peak)
  and the compute saved. Performance is preserved by construction.
- **Cost / packing** — a first-fit-decreasing bin-pack of your observability
  pods into candidate instance shapes (honoring Mimir zone-aware packing and
  Loki/Tempo one-ingester-per-node), giving the node-count floor and $/mo,
  with an OOM-risk check (Σ memory limits vs node capacity).
- **Durability** — PDBs that allow zero disruptions, ingesters without a
  priority class, CPU-limited Mimir ingesters, PVC zone pins that prevent
  co-location, an AZ hosting two logical zones, recent OOMs.
- **Network** — remote_write wire GB/month and its cost across egress paths
  (cross-AZ, TGW, NAT, GWLB).
- **Karpenter** — a deep analysis of the observability NodePool with a
  **proposed, generic, paste-ready NodePool YAML**: r-class for the
  memory-bound backends, the bin-pack-floor shape plus a burst size, a
  Graviton opt-in, **on-demand for stateful ingesters** (spot-with-fallback
  only for stateless), `WhenEmptyOrUnderutilized` consolidation with
  **disruption budgets that respect PDBs** (never evict more than one
  ingester), a finite `expireAfter` plus paced rotation (for patching,
  instead of `Never`), pool limits sized to the floor plus growth headroom,
  and AZ coverage for the zone-aware ingesters.

Kubernetes/Karpenter analysis is optional — it runs only when `kubectl` is
available and you opt in; the metric-driven analysis works without a cluster.

```bash
python3 -m nr2grafana deepdive --prom http://localhost:9090 \
    --mimir http://localhost:8080 --loki http://localhost:3100 --kube
# writes deepdive-report.json + a config/ dir of paste-ready snippets
```

## AI as a first-class tenant (`ai-context`, `ai troubleshoot`, web **AI** view)

The program bundles everything it knows about a dashboard and the stack —
requirements, diagnosis, parity, samples, cost, optimization, deep-dive, and
packing findings — into one compact, self-explaining **AI context bundle**
(markdown or JSON, secrets redacted, with a legend and a safety-aware task
preamble). Hand that bundle to any AI and it can pinpoint why a panel is
empty, which cost cuts are safe, and what the durability risks are.

```bash
python3 -m nr2grafana ai-context <slug> --markdown -o context.md
python3 -m nr2grafana ai troubleshoot <slug> --question "why is panel 3 empty?"
```

The `ai troubleshoot` flow uses whatever AI backend you configured — an
Anthropic API key or a **local CLI agent** (`claude -p {prompt}`, `kiro`,
etc.), so it runs against your own local authentication.

## Grafana MCP (`mcp config`, `mcp probe`, web AI view **MCP** panel)

Generate a ready MCP configuration that wires the Grafana MCP server
(`mcp-grafana`) plus the nr2grafana context into your local AI (Claude or
Kiro), then let the AI operate Grafana and read nr2grafana's findings
together. The token is always referenced via the
`${GRAFANA_SERVICE_ACCOUNT_TOKEN}` environment variable — never written into
the config file.

```bash
python3 -m nr2grafana mcp config --kind claude -o mcp.json
python3 -m nr2grafana mcp probe --command "mcp-grafana"   # list its tools
```
