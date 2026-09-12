# Cost & efficiency optimization

Since 1.5 nr2grafana can evaluate the cost of your LGTM stack from **real
traffic** and propose **safe, concrete ways to cut it** — with ready-to-
paste config. The core idea: the tool already knows which metrics, labels,
and log streams your migrated dashboards actually *use*, so anything you
ingest that nothing queries is provably safe to trim.

> All costs are **estimates based on pricing assumptions you control** —
> never exact bills. The savings are directional, meant to prioritize work.

## What it measures (traffic sampling)

Through your Grafana service-account token's datasource proxy:

- **Mimir / Prometheus** — active-series count, the biggest metrics by
  series (`/api/v1/status/tsdb`), and per-label value cardinality. Series
  cardinality is the dominant cost driver.
- **Loki** — stream count, bytes per stream over a window
  (`/loki/api/v1/index/volume`), and label value cardinality. Stream-label
  cardinality is the #1 Loki cost driver.
- **Tempo** — volume, best-effort.

## What it recommends (safe by construction)

Every recommendation is checked against the **usage set** (what your
dashboards need). A dimension that is expensive **and** unused becomes a
drop; expensive **and** used becomes a *review* note (e.g. "move to
structured metadata"), never an auto-drop.

**Loki**
- Drop or move to **structured metadata** any stream label your dashboards
  don't filter on — id-like/high-churn labels (`trace_id`, `pod`,
  `request_id`, `ip`) are always pushed to structured metadata, never kept
  as stream labels.
- A **recommended stream-label set** (only the low-cardinality labels you
  actually query).
- Volume hotspots → retention/drop-noisy-lines advice.

**Mimir / Prometheus**
- Drop metrics you ingest but never query (plus a **keep-list** built from
  the usage set — safer than a drop-list because new noise is excluded
  automatically).
- `labeldrop` high-cardinality labels nothing groups by.
- Histogram bucket-bloat flag.

Each recommendation carries evidence (cardinality, bytes, dashboards-using
count), an estimated saving (series/GB and dollars), and **paste-ready,
commented config** for the right layer — Promtail/Grafana Alloy
`relabel_configs`, OTel Collector processors, Loki `limits_config`,
Prometheus `metric_relabel_configs`. Applying at the agent saves *before*
ingest, which is the cheapest place to cut.

## Use it

Web UI — the **Cost** view:
1. **Sample traffic** → per-datasource cards with cardinality/volume charts.
2. **Cost breakdown** with an editable pricing panel and a current →
   projected savings figure.
3. **Recommendations** — ranked cards, each with a "safe — nothing your
   dashboards use" chip or an amber "review", the savings, and copyable
   config; **Download all config** bundles every snippet as a zip.

CLI:
```bash
export GRAFANA_URL=... GRAFANA_TOKEN=...
python3 -m nr2grafana cost analyze ./out/*/ --from now-24h
# writes cost-report.json + a config/ dir of paste-ready snippets
python3 -m nr2grafana cost pricing        # view/edit the cost assumptions
```

Pricing assumptions live in the local store (no secrets); tune them to your
contract and the estimates update.
