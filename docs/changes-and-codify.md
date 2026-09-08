# Change tracking and codifying fixes into config

The point of tracking changes is the converter's core loop: convert →
test → fix the flagged panels → **put the fix into the config** →
reconvert. 1.1 automates the last step: every fix you (or the AI)
make is recorded, and `changes suggest-config` turns the record into a
config overlay.

## Where changes live

A local sqlite database at `~/.nr2grafana/nr2grafana.db` (directory
created mode 0700). It holds converted dashboards, their artifacts
(requirements, reports, test results), run history, the change log,
and non-secret preferences. **Secrets are never stored** — the store
actively refuses credential-looking setting keys, so API keys and
tokens cannot end up on disk. Delete the directory any time; you lose
history, not functionality.

## What gets recorded

Every mutation made through the tool: query edits (from the web UI
panel editor or an applied AI fix), datasource assignments, panel
edits, imports, datasource creations, dashboard pushes. Each entry
carries action, target, before/after, why, a source
(`user` / `ai` / `auto`), and a timestamp.

## Reporting

```bash
python3 -m nr2grafana changes report                 # JSON, all dashboards
python3 -m nr2grafana changes report --slug checkout-service-overview --markdown
```

The Markdown form is one table per dashboard:

```
| When | Source | Action | Target | Change | Why |
| --- | --- | --- | --- | --- | --- |
| 2026-09-08T16:14:11Z | user | query-edit | `panel 5 / A` | `...service_name="checkout"...` -> `...service="checkout"...` | stack labels services as 'service' |
| 2026-09-08T16:14:11Z | user | datasource-set | `${datasource}` | `${datasource}` -> `{"type": "prometheus", "uid": "mimir-prod"}` | bound to prod Mimir |
```

## Codify: `changes suggest-config`

```bash
python3 -m nr2grafana changes suggest-config          # all dashboards
python3 -m nr2grafana changes suggest-config --slug checkout-service-overview
```

Inspects the recorded query edits and infers a mergeable config
overlay — label renames become `label_map` entries, metric renames
become `metric_map` entries, datasource bindings become
`datasources.<family>.uid` — each with a rationale and confidence:

```json
{
  "overlay": {
    "label_map": {"service_name": "service"},
    "datasources": {"prometheus": {"uid": "mimir-prod"}}
  },
  "rationale": [
    {"change_id": 1, "confidence": "high",
     "inference": "label rename 'service_name' -> 'service' in edited query"},
    {"change_id": 2, "confidence": "high",
     "inference": "datasource uid for 'prometheus' set to 'mimir-prod'"}
  ]
}
```

Merge the overlay into your mapping config (copy the keys in — the
overlay uses the same shape as `example-config`), then reconvert:
every future dashboard gets the fix automatically. Ambiguous edits are
skipped rather than guessed — check the rationale's confidence before
trusting a `medium` inference. The web UI's Changes page shows the
same overlay with a copy button.
