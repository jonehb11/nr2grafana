---
inclusion: always
---

# nr2grafana steering

Follow `AGENTS.md` at the repository root. Summary:

- Workflow: `python3 -m nr2grafana import|convert|validate|export`, always
  with `--json` before the command when the result is consumed by an agent.
- Understand a New Relic dashboard first: `python3 -m nr2grafana inspect
  <nr.json>`; one query: `python3 -m nr2grafana explain "<NRQL>"`.
- Fix flagged (`[REVIEW]`) panels by editing the mapping config
  (`label_map`, `metric_map`, `loki_stream_labels`, `event_map`, …) and
  re-running `convert` — not by patching dashboard JSON.
- New Relic is read-only. Never print or store `NEW_RELIC_API_KEY`,
  `GRAFANA_TOKEN`.
- Tests: `python3 -m unittest discover -s tests`.
