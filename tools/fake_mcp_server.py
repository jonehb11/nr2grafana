#!/usr/bin/env python3
"""A tiny fake MCP server speaking JSON-RPC 2.0 over stdio (stdlib only).

It exists so ``nr2grafana.mcp`` (MCPClient / probe) can be exercised end
to end -- ``initialize`` handshake, ``tools/list`` and ``tools/call`` --
without installing the real Grafana ``mcp-grafana`` binary. The transport
matches what :class:`nr2grafana.mcp.MCPClient` speaks: newline-delimited
JSON, one JSON-RPC message per line, on stdin/stdout. Anything that is
not a well-formed request is ignored; nothing but JSON-RPC is ever
written to stdout (diagnostics, if any, go to stderr) so the client's
line reader never trips over stray output.

The advertised tools mimic a couple of Grafana MCP tools (list
datasources, search dashboards, query Prometheus). ``tools/call`` does
not touch any real system: it echoes the tool name and arguments back as
text content, which is all the tests need to prove the round trip.

Run standalone (it will sit reading stdin)::

    python3 tools/fake_mcp_server.py

It is normally launched as a subprocess by MCPClient(command=[...]).
"""

import json
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "fake-grafana-mcp", "version": "1.6.0"}

# A couple of grafana-like tool definitions (name / description / schema).
TOOLS = [
    {
        "name": "list_datasources",
        "description": "List the Grafana datasources.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_dashboards",
        "description": "Search Grafana dashboards by a query string.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "search text"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "query_prometheus",
        "description": "Run an instant PromQL query against a datasource.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasourceUid": {"type": "string"},
                "expr": {"type": "string"},
            },
            "required": ["datasourceUid", "expr"],
        },
    },
]

_TOOLS_BY_NAME = dict((t["name"], t) for t in TOOLS)

# JSON-RPC 2.0 error codes.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602


def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _handle(msg):
    """Handle one parsed request; return a response dict or None.

    ``None`` means "no reply" -- either a notification (no ``id``) or a
    message we deliberately stay silent on.
    """
    if not isinstance(msg, dict):
        return _error(None, _INVALID_REQUEST, "expected a JSON object")
    method = msg.get("method")
    req_id = msg.get("id")
    is_notification = "id" not in msg
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })
    if method == "notifications/initialized" or is_notification:
        # Fire-and-forget notifications get no response.
        return None
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in _TOOLS_BY_NAME:
            return _error(req_id, _INVALID_PARAMS,
                          "unknown tool: %r" % name)
        # Echo the call back as text content (no real side effects).
        text = "called %s with %s" % (
            name, json.dumps(arguments, sort_keys=True))
        return _result(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        })
    # Unknown method: only requests (with an id) deserve an error reply.
    if is_notification:
        return None
    return _error(req_id, _METHOD_NOT_FOUND,
                  "method not found: %r" % method)


def _write(out, response):
    out.write((json.dumps(response) + "\n").encode("utf-8"))
    out.flush()


def serve(stdin=None, stdout=None):
    """Read newline-delimited JSON-RPC from ``stdin``, reply on ``stdout``.

    Loops until EOF. Malformed lines get a parse-error reply (with a null
    id, per JSON-RPC); blank lines are skipped.
    """
    rd = stdin if stdin is not None else sys.stdin.buffer
    wr = stdout if stdout is not None else sys.stdout.buffer
    while True:
        line = rd.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except ValueError:
            _write(wr, _error(None, _PARSE_ERROR, "invalid JSON"))
            continue
        try:
            response = _handle(msg)
        except Exception as e:  # never crash the loop
            rid = msg.get("id") if isinstance(msg, dict) else None
            _write(wr, _error(rid, _INVALID_REQUEST,
                              "server error: %s" % e))
            continue
        if response is not None:
            _write(wr, response)
    return 0


def main():
    try:
        return serve()
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
