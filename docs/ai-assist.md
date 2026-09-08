# AI assistance (optional)

nr2grafana can ask Claude to diagnose and fix broken panel queries.
It is entirely optional: without an API key every other feature works
unchanged, and nothing is ever sent anywhere unless you explicitly
trigger an AI action.

## Setup

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Or paste the key on the web UI's Setup page. The key lives in process
memory/env only — it is never stored on disk, logged, or included in
error messages. The model defaults to `claude-sonnet-5`; override with
the `N2G_AI_MODEL` env var.

## What it does

**Fix a failing panel** — the main use. When `grafana test` (or the
web UI) marks a panel `error` or `no-data`, "Ask AI" sends Claude the
panel context: the translated query, the actual Grafana error, the
datasource type, the original NRQL, the dashboard's requirements
excerpt, and a sample of what actually exists on your instance
(datasource list, metric names). Claude answers with structured JSON:

```json
{
  "explanation": "the stack labels services as 'service', not 'service_name'",
  "fixed_expr": "sum(rate(http_server_request_duration_seconds_count{service=\"checkout\"}[$__rate_interval]))",
  "confidence": "high",
  "actions": []
}
```

In the web UI the fix is one click to apply, then **Test** to verify
and **Save & Push** to ship it. If no query fix applies (the data
simply doesn't exist yet), `fixed_expr` is `null` and `actions` lists
the manual steps instead. AI-applied edits are recorded in the change
log with `source: "ai"`, so they show up in `changes report` and feed
`changes suggest-config` like any manual fix.

**Chat** — the web UI's AI Assistant page is a free-form conversation
for anything the structured fix flow doesn't cover ("why does this
NRQL have no PromQL equivalent?", "what exporter emits kube_pod_*?").

## What gets sent where

When (and only when) you use an AI action, the panel queries, error
messages, and instance metadata described above are sent to the
Anthropic API (`api.anthropic.com`) over HTTPS. Your Grafana and New
Relic credentials are never included. If that's not acceptable for
your environment, don't set the key — nothing else changes.

## Errors

Failures come back as one actionable line, never a traceback: a bad or
revoked key (401), rate limiting (429), API overload (529), or an
unreachable network each get a specific message. A missing key simply
reports that `ANTHROPIC_API_KEY` is not set.
