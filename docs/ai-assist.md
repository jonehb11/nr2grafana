# AI assistance (optional)

nr2grafana can ask an AI assistant to diagnose and fix broken panel
queries. It is entirely optional: without a backend every other
feature works unchanged, and nothing is ever sent anywhere unless you
explicitly trigger an AI action. Two backends are supported:

* **Anthropic API** — set an API key; requests go to Claude over
  HTTPS.
* **Local console AI** — no key needed; point nr2grafana at any AI
  CLI already on your machine (see below). If both are configured,
  the API key wins.

## Setup (API backend)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Or paste the key on the web UI's Setup page. The key lives in process
memory/env only — it is never stored on disk, logged, or included in
error messages. The model defaults to `claude-sonnet-5`; override with
the `N2G_AI_MODEL` env var.

## Local console AI (no API key)

If you already have a console AI agent installed — Claude Code,
kiro-cli, anything that takes a prompt and prints an answer — you can
route all of nr2grafana's AI assistance through it instead of an API
key. Enter the command on the web UI's Setup page (or
`POST /api/settings {"ai_command": "..."}`).

**How the command is run**

* The command string is split with `shlex` and executed directly —
  **no shell**, so pipes/redirection/`$VAR` expansion do not work.
* If `{prompt}` appears in the command, the prompt is substituted
  into that argv element as a single argument (spaces, quotes, and
  newlines in the prompt are safe — nothing is shell-interpolated).
  Without `{prompt}`, the prompt is written to the process's stdin.
* The process runs from the app's working directory with your
  environment plus `TERM=dumb` and `NO_COLOR=1`; ANSI color codes are
  stripped from the output anyway, captured output is capped at
  ~200KB, and the call times out after 180 seconds.
* Chat conversations are flattened into one prompt (`User:` /
  `Assistant:` turns); the reply is whatever the command prints to
  stdout. For panel fixes the agent is asked for the same strict
  JSON as the API backend, with defensive parsing (fenced blocks and
  surrounding prose are tolerated).

**Examples**

```text
claude -p {prompt}      # Claude Code print mode (argv substitution)
kiro-cli                # any CLI that reads the prompt from stdin
llm                     # e.g. Simon Willison's llm tool, stdin mode
```

Use the Setup page's **Test** button (`POST /api/ai/test`) to verify
the wiring: it sends a trivial "reply OK" prompt and reports the
reply excerpt and latency, or the exact failure (command not found,
non-zero exit with the stderr tail, timeout).

**What data goes where.** Local mode sends the same panel context as
the API backend — queries, error text, requirement excerpts, metric
name samples — but only to the local process. nr2grafana itself sends
nothing to any remote API in local mode; whether the CLI you
configured talks to a remote service is between you and that CLI.

**Security notes.** The command runs locally with your user
permissions — configure only a CLI you trust, exactly as you would
type it in a terminal. Unlike API keys, the command string is not a
secret: it persists in `~/.nr2grafana` settings and survives
restarts. Your Grafana/New Relic credentials are never part of the
prompt.

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
Anthropic API (`api.anthropic.com`) over HTTPS — or, in local mode,
to your local command's process and nowhere else. Your Grafana and
New Relic credentials are never included. If neither is acceptable
for your environment, configure no backend — nothing else changes.

## Errors

Failures come back as one actionable line, never a traceback: a bad or
revoked key (401), rate limiting (429), API overload (529), or an
unreachable network each get a specific message. A missing backend
simply reports that no key or local command is configured. Local-mode
failures name the command and include the exit code and the tail of
its stderr, plus a reminder to check that the CLI is installed and on
PATH.
