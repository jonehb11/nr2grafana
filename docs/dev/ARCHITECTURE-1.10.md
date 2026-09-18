# nr2grafana 1.10 — programmatic API surface (MCP server + token API + JSON CLI)

Make every nr2grafana capability drivable by an AI or a script, three ways:
1. **nr2grafana AS an MCP server** — a stdio JSON-RPC MCP server exposing the
   operations as tools, so a local AI (Claude/Kiro) can call them directly.
2. **Token-authed headless HTTP API** — the existing localhost web server,
   runnable headless with a bearer token so non-browser clients can call it.
3. **Machine-readable CLI** — a global `--json` flag: every command emits a
   structured JSON result to stdout.

Builds on 1.1–1.9 (all contracts hold). Version 1.10.0. Zero deps (Python
3.9+ stdlib). Secrets in memory/env only. AWS + New Relic strictly read-only;
the tool proposes, never executes AWS/K8s changes. **The API surface must not
weaken any existing guard** (read-only, propose-only, localhost default).

## A. `nr2grafana/mcp_server.py` — nr2grafana as an MCP server (owner: mcp-server agent)
A stdlib stdio JSON-RPC 2.0 MCP server (newline-delimited, the transport the
existing MCPClient speaks). Implements `initialize`, `tools/list`,
`tools/call`. Reuse framing knowledge from mcp.py but this is the SERVER side.
```python
def serve_stdio(store=None, log=None) -> None   # blocking read/dispatch/write loop
TOOLS: list of {name, description, inputSchema}  # JSON Schemas
def handle_call(name, arguments, ctx) -> dict     # dispatch -> library call
```
Expose these tools (each maps to existing library/route logic; NEVER shells to
mutate; long ops run to completion synchronously and return the artifact):
- `list_dashboards`, `get_dashboard {slug}`, `get_artifact {slug, kind}`
- `convert {input_dir|nr_json, out_dir?, package?}` (paste path via nr_json)
- `fetch_newrelic {guids?, out_dir?}` (needs NR key in env; read-only)
- `validate {path|slug}`
- `parity {slug, from?, to?}`, `compare {slug}`, `samples {slug, panel_id?}`
- `diagnose {slug}`, `deepdive {prom?,mimir?,loki?,kube?}`
- `cost_analyze {slug?}`, `tco {months?,profile?}`
- `cost_rca {anomaly_report|anomaly_id, profile?}`, `mitigate {rca?|slug}`
- `ai_context {slug, format?}` (returns the compact bundle for the AI itself)
- `readiness {slug}`
Config/secrets come from env + Store settings (never as tool args unless the
user passes them). Each tool result is `{content:[{type:"text", text:<json>}]}`
per MCP. Errors return an MCP error object, never a traceback. Owns
mcp_server.py + tests/test_mcp_server.py (drive it via stdio in-test like the
fake MCP server tests do).

## B. `web/server.py` — token auth + headless (owner: server agent)
- **Bearer token**: read `N2G_API_TOKEN` (env) at startup. When a request
  carries `Authorization: Bearer <token>` matching it, the request is a
  trusted programmatic client: it BYPASSES the 1.7.1 same-origin/CSRF check
  (non-browser clients send no Origin) — but the loopback-Host check still
  applies unless the server was explicitly bound non-loopback (see below).
  Constant-time compare; the token is never logged or echoed in any response.
  No token configured -> the API behaves exactly as today (browser same-origin
  only); programmatic POSTs without a token still 403.
- **Non-loopback bind**: `serve(host=..., api_token=...)` may bind a
  non-loopback host ONLY when an api_token is set; then the loopback-Host
  guard is relaxed to allow that host, and EVERY state-changing request must
  present the bearer token (GET stays open on loopback, token-required off
  loopback). Binding 0.0.0.0 without a token is refused with a clear error.
- **Discovery**: `GET /api/spec` returns a stable JSON description of the API
  (routes, methods, request/response summaries) so an AI/script can discover
  it. `GET /api/health` (unauthed, loopback) for readiness.
- serve() signature gains `api_token=""`, `headless=False` (no browser open;
  print the base URL + "API token required for programmatic POST" note).
  Owns server.py + tests/test_web.py (token bypass works, wrong/no token still
  403 for off-origin POST, token never in responses, /api/spec shape, 0.0.0.0
  -without-token refused).

## C. `cli.py` + `interactive.py` — JSON CLI + serve commands (owner: cli agent)
- Global `--json` flag (top-level, before the subcommand): every command
  prints a single JSON object to stdout with the command's structured result
  (`{"ok":bool,"command":str,"result":...,"error":?}`); human text goes to
  stderr so stdout stays clean JSON. Exit codes unchanged.
- `n2g mcp serve` — launch the stdio MCP server (mcp_server.serve_stdio).
- `n2g api serve [--host 127.0.0.1] [--port 8765] [--api-token T | env
  N2G_API_TOKEN] [--no-browser]` — launch the headless token-authed API
  (web.serve with api_token + headless). Refuse non-loopback host without a
  token. Print the token requirement, never the token value.
- Wizard: an "Expose to AI (MCP server / API)" menu entry that prints the
  ready `mcp config` snippet and the api-serve command. Owns cli.py,
  interactive.py, tests/test_cli.py.

## D. `mcp.py` — register nr2grafana's own server (owner: mcp-config agent)
- `generate_mcp_config` gains `include_n2g=True`: add an `nr2grafana` MCP
  server entry (command: the python module `python3 -m nr2grafana mcp serve`,
  env referencing the same non-secret prefs; secrets via the user's env, never
  embedded) so `n2g mcp config` wires nr2grafana itself + Grafana + AWS-cost
  MCP servers into Claude/Kiro in one file. Owns mcp.py + tests/test_mcp.py.

## E. docs + e2e (owner: server agent, shared)
docs/api.md: the three access modes, the tool list, the token model, example
Claude/Kiro MCP config, and a curl example with the bearer token. Extend
tests/test_e2e_mock.py: drive the MCP server over stdio through a full
op (e.g. convert nr_json -> list_dashboards -> parity) and the token API
(POST with token succeeds, without token 403). (Coordinate test_e2e_mock with
the mcp-server agent: server agent owns the file.)

## Cross-cutting
1. stdlib only, py3.9, ASCII/LF/4-space/79-col.
2. No guard weakened: read-only AWS/NR, propose-only, localhost default; the
   token is the ONLY thing that relaxes the same-origin check, and only for
   holders of the token; non-loopback bind REQUIRES a token.
3. The token is never logged, echoed, or written to disk (env/arg only).
4. Every module tested; full suite stays green. Coordinator bumps to 1.10.0
   and updates README after build.
