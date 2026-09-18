# nr2grafana programmatic API

nr2grafana 1.10 exposes every capability to AIs and scripts three ways.
All three drive the exact same read-only, propose-only library code the
web UI uses: New Relic and AWS are never mutated, the tool proposes
changes (configs, PromQL/LogQL, mitigations) and never executes them
against AWS/K8s, and the web server still defaults to loopback only.

- [1. MCP server (stdio)](#1-mcp-server-stdio) -- nr2grafana *as* an MCP
  server, so a local AI (Claude Desktop / Claude Code / Kiro) calls its
  operations as tools.
- [2. Token-authed HTTP API](#2-token-authed-http-api) -- the localhost
  web server run headless with a bearer token, for any HTTP client.
- [3. JSON CLI](#3-json-cli) -- a global `--json` flag makes every
  command print one structured JSON object to stdout.

---

## 1. MCP server (stdio)

Run nr2grafana as a stdio JSON-RPC 2.0 MCP server (newline-delimited
messages on stdin/stdout):

```console
$ nr2grafana mcp serve
```

It implements `initialize`, `tools/list` and `tools/call`. Each tool
result is the standard MCP envelope
`{"content": [{"type": "text", "text": "<json>"}]}`; the `text` is the
JSON artifact the operation produced. Errors come back as JSON-RPC error
objects with an actionable message -- never a traceback.

Secrets and configuration come from the process **environment** and the
local store, never from tool arguments (unless you deliberately pass
them). Relevant env vars:

| Variable | Purpose |
|----------|---------|
| `NEW_RELIC_API_KEY` | New Relic user key (read-only NerdGraph) |
| `GRAFANA_URL`, `GRAFANA_TOKEN` | Grafana / LGTM stack access |
| `ANTHROPIC_API_KEY` | optional, for AI-assisted tools |
| `N2G_DB` | store path override (defaults to `~/.nr2grafana`) |

### Tools

| Tool | Arguments | What it does |
|------|-----------|--------------|
| `list_dashboards` | -- | list stored converted dashboards |
| `get_dashboard` | `slug` | one stored dashboard's Grafana JSON |
| `get_artifact` | `slug`, `kind` | a stored artifact (widget-report, requirements, parity, ...) |
| `convert` | `input_dir` \| `nr_json`, `out_dir?`, `package?` | convert NR dashboards (paste JSON via `nr_json`) |
| `fetch_newrelic` | `guids?`, `out_dir?` | fetch NR dashboards to disk (needs NR key; read-only) |
| `validate` | `path` \| `slug` | validate a dashboard / conversion |
| `parity` | `slug`, `from?`, `to?` | NR-vs-Grafana numeric parity |
| `compare` | `slug` | side-by-side comparison model |
| `samples` | `slug`, `panel_id?` | raw sample rows per panel/side |
| `diagnose` | `slug` | diagnose no-data / mismatch panels |
| `deepdive` | `prom?`, `mimir?`, `loki?`, `kube?` | LGTM stack deep-dive |
| `cost_analyze` | `slug?` | cost estimate + safe savings recommendations |
| `tco` | `months?`, `profile?` | AWS TCO trend (read-only Cost Explorer) |
| `cost_rca` | `anomaly_report` \| `anomaly_id`, `profile?` | root-cause a cost anomaly |
| `mitigate` | `rca?` \| `slug` | reliability-safe mitigation plan (proposal only) |
| `ai_context` | `slug`, `format?` | compact AI context bundle |
| `readiness` | `slug` | migration-readiness grade |

Long operations run to completion synchronously and return the finished
artifact (there is no polling on the MCP transport).

### Wiring nr2grafana into an MCP client

`nr2grafana mcp config` generates a ready config that wires nr2grafana
itself (plus, optionally, the Grafana and AWS-cost MCP servers) into your
client. Secrets are referenced through env vars, never embedded.

**Claude Desktop / Claude Code** (`claude_desktop_config.json` /
`.mcp.json`):

```json
{
  "mcpServers": {
    "nr2grafana": {
      "command": "python3",
      "args": ["-m", "nr2grafana", "mcp", "serve"],
      "env": {
        "NEW_RELIC_API_KEY": "${NEW_RELIC_API_KEY}",
        "GRAFANA_URL": "${GRAFANA_URL}",
        "GRAFANA_TOKEN": "${GRAFANA_TOKEN}"
      }
    }
  }
}
```

**Kiro** (`.kiro/settings/mcp.json`) uses the same `mcpServers` shape.
Generate the exact file for your client with:

```console
$ nr2grafana mcp config --kind claude   # or: --kind kiro
```

---

## 2. Token-authed HTTP API

The localhost web server can run headless with a bearer token so
non-browser clients (scripts, AIs, CI) can drive the same JSON API the UI
uses:

```console
$ export N2G_API_TOKEN="$(openssl rand -hex 24)"
$ nr2grafana api serve            # 127.0.0.1:8765, headless
```

Flags: `--host` (default `127.0.0.1`), `--port` (default `8765`),
`--api-token T` (else `N2G_API_TOKEN`), `--no-browser`.

### Token model

- **No token configured** -> behavior is unchanged from earlier
  releases: the API answers browser same-origin requests on loopback
  only. A programmatic POST without a same-origin `Origin`/`Referer`
  header is `403`.
- **Valid `Authorization: Bearer <token>`** -> the request is a trusted
  non-browser client. It **bypasses the same-origin/CSRF check** (such
  clients send no `Origin`). The loopback DNS-rebinding guard still
  applies unless the server was explicitly bound to a non-loopback host.
- **Constant-time compare.** The token is read from `N2G_API_TOKEN` (or
  `--api-token`) at startup. It is **never logged, echoed in a response,
  or written to disk** -- it lives in process memory only.
- **Non-loopback bind.** `--host` other than loopback (e.g. `0.0.0.0`)
  is **refused unless a token is set**. When bound non-loopback, every
  request from a remote peer must present the token.
- **Unauthed helpers** on loopback: `GET /api/health` (liveness) and
  `GET /api/spec` (the machine-readable API description) need no token.

### Discovery

`GET /api/spec` returns a stable JSON description of the routes, methods
and summaries, plus the auth model, so an AI/script can discover the API:

```console
$ curl -s http://127.0.0.1:8765/api/spec | python3 -m json.tool
```

### curl example (bearer token)

Convert a pasted New Relic dashboard, then poll the job. Note there is no
`Origin` header -- the token is what authorizes the state-changing POST:

```console
$ TOKEN="$N2G_API_TOKEN"
$ curl -s -X POST http://127.0.0.1:8765/api/convert \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"nr_json": <your NR dashboard JSON>, "package": false}'
{"job": "3f9c1a2b7d4e"}

$ curl -s http://127.0.0.1:8765/api/jobs/3f9c1a2b7d4e \
    -H "Authorization: Bearer $TOKEN"
{"id": "3f9c1a2b7d4e", "status": "done", "result": {"dashboards": [ ... ]}}
```

The same POST **without** the `Authorization` header (and without a
same-origin `Origin`) returns `403`.

Long operations return `{"job": "<id>"}`; poll `GET /api/jobs/<id>` until
`status` is `done` or `error`. See `GET /api/spec` for the full route
list.

---

## 3. JSON CLI

Every CLI command accepts a global `--json` flag (placed before the
subcommand). With it, the command prints exactly one JSON object to
**stdout** -- `{"ok": bool, "command": str, "result": ..., "error"?}` --
while all human/progress text goes to stderr, so stdout stays clean JSON.
Exit codes are unchanged.

```console
$ nr2grafana --json convert ./newrelic-dashboards --out ./out
{"ok": true, "command": "convert", "result": {"dashboards": [ ... ]}}

$ nr2grafana --json list | jq '.result'
[ ... ]
```

This makes nr2grafana scriptable from any language without the HTTP
server or an MCP client -- pipe the JSON straight into `jq` or a program.

---

## Security invariants (all three modes)

- New Relic and AWS access is strictly **read-only**; the tool
  **proposes** config/mitigations and never executes AWS/K8s changes.
- The web server defaults to **loopback**; a non-loopback bind requires a
  token; the bearer token is the *only* thing that relaxes the
  same-origin check, and only for holders of the token.
- API keys and the API token stay in process **memory / environment**;
  they are never written to disk, logged, or echoed in a response.
