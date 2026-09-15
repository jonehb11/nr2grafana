"""Minimal Model Context Protocol (MCP) integration (stdlib only).

Two pieces, no third-party deps:

* :class:`MCPClient` speaks JSON-RPC 2.0 to an MCP server over either a
  stdio subprocess (newline-delimited JSON-RPC, the transport the
  Grafana ``mcp-grafana`` server speaks) or an HTTP endpoint. It runs
  the ``initialize`` handshake and exposes ``list_tools`` / ``call_tool``.
  It is a context manager and never leaks the subprocess: :meth:`close`
  (also called on ``__exit__``) always terminates the child.
* :func:`generate_mcp_config` emits a ready-to-paste MCP servers config
  wiring the Grafana MCP server (plus an optional nr2grafana context
  entry) into a local AI client (claude / kiro / generic). The Grafana
  service-account token is ALWAYS referenced through the
  ``GRAFANA_SERVICE_ACCOUNT_TOKEN`` environment variable -- a secret is
  never written into the generated config.
* :func:`probe` opens a client, initializes, lists tools and returns a
  small ``{"ok", "tools", "error"?}`` dict without ever raising.

Network / subprocess failures surface as :class:`MCPError` with an
actionable hint, never a traceback.
"""

from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# MCP protocol revision this client negotiates. Servers echo their own
# supported revision in the initialize result; we do not hard-fail on a
# mismatch (servers are expected to be backward compatible).
PROTOCOL_VERSION = "2024-11-05"

# The canonical Grafana MCP server and the env vars it reads. The token
# var is referenced, never its value -- see generate_mcp_config.
GRAFANA_MCP_COMMAND = "mcp-grafana"
GRAFANA_URL_ENV = "GRAFANA_URL"
GRAFANA_TOKEN_ENV = "GRAFANA_SERVICE_ACCOUNT_TOKEN"
# The literal reference written into generated configs (env-var
# interpolation), NOT a secret.
GRAFANA_TOKEN_REF = "${GRAFANA_SERVICE_ACCOUNT_TOKEN}"

DEFAULT_TIMEOUT = 30
_STDERR_TAIL = 500
_MAX_MESSAGE = 8 * 1024 * 1024


class MCPError(Exception):
    """Raised when an MCP server cannot be reached or returns an error."""


def _client_info() -> Dict[str, str]:
    """Identify this client in the initialize handshake."""
    try:
        from . import __version__ as ver
    except Exception:
        ver = "0"
    return {"name": "nr2grafana", "version": str(ver)}


class MCPClient:
    """A tiny JSON-RPC 2.0 MCP client (stdio subprocess or HTTP).

    Exactly one transport is used. Pass ``command`` (a list of argv, or
    a string that is shlex-split) to launch and talk to a stdio server,
    or ``url`` to POST JSON-RPC to an HTTP MCP endpoint. ``headers`` add
    request headers for the HTTP transport (e.g. Authorization);
    secrets in headers stay in memory only.

    Use as a context manager so the subprocess is always reaped::

        with MCPClient(command=["mcp-grafana"]) as c:
            tools = c.list_tools()
    """

    def __init__(self, command=None, url: str = "",
                 headers: Optional[Dict[str, str]] = None,
                 timeout: int = DEFAULT_TIMEOUT):
        if bool(command) == bool(url):
            raise MCPError(
                "MCPClient needs exactly one transport: pass command=[...] "
                "for a stdio server or url=... for an HTTP MCP server")
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.url = url.rstrip("/") if url else ""
        self.headers = dict(headers or {})
        self.server_info: Dict[str, Any] = {}
        self._initialized = False
        self._id = 0
        self._proc: Optional[subprocess.Popen] = None
        self._buf = bytearray()
        if command:
            self._argv = self._to_argv(command)
        else:
            self._argv = []

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "MCPClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    @staticmethod
    def _to_argv(command) -> List[str]:
        if isinstance(command, (list, tuple)):
            argv = [str(a) for a in command]
        else:
            try:
                argv = shlex.split(str(command))
            except ValueError as e:
                raise MCPError("cannot parse MCP command %r: %s"
                               % (command, e))
        if not argv:
            raise MCPError("empty MCP command -- give the server argv, "
                           "e.g. command=[\"mcp-grafana\"]")
        return argv

    def _spawn(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
        except FileNotFoundError:
            raise MCPError(
                "MCP server command not found: %r -- is it installed and "
                "on PATH?" % self._argv[0])
        except OSError as e:
            raise MCPError("cannot start MCP server %r: %s"
                           % (self._argv[0], e))

    def close(self) -> None:
        """Terminate the subprocess (idempotent); never raises."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

    # -- public MCP surface ---------------------------------------------

    def initialize(self) -> Dict[str, Any]:
        """Run the MCP initialize handshake; returns the server result."""
        if self._initialized:
            return self.server_info
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _client_info(),
        })
        if not isinstance(result, dict):
            raise MCPError("MCP server returned a malformed initialize "
                           "result")
        self.server_info = result
        self._initialized = True
        # Per spec the client confirms with an initialized notification.
        self._notify("notifications/initialized", {})
        return result

    def list_tools(self) -> List[Dict[str, Any]]:
        """Return the server's advertised tools (name/description/schema)."""
        self._ensure_initialized()
        result = self._rpc("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return []
        return [t for t in tools if isinstance(t, dict)]

    def call_tool(self, name: str,
                  arguments: Optional[Dict[str, Any]] = None) \
            -> Dict[str, Any]:
        """Invoke a tool by name; returns the tool result envelope."""
        if not name:
            raise MCPError("call_tool needs a tool name")
        self._ensure_initialized()
        result = self._rpc("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })
        if not isinstance(result, dict):
            raise MCPError("MCP tool %r returned a malformed result" % name)
        return result

    # -- JSON-RPC plumbing ----------------------------------------------

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _rpc(self, method: str, params: Dict[str, Any]) -> Any:
        """Send a request and return its ``result`` (or raise MCPError)."""
        req_id = self._next_id()
        message = {"jsonrpc": "2.0", "id": req_id,
                   "method": method, "params": params}
        if self.url:
            payload = self._http_send(message, expect_reply=True)
        else:
            payload = self._stdio_send(message, req_id)
        if not isinstance(payload, dict):
            raise MCPError("MCP server returned a non-object response to "
                           "%s" % method)
        if "error" in payload and payload["error"] is not None:
            raise MCPError(self._rpc_error(method, payload["error"]))
        return payload.get("result", {})

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        """Send a fire-and-forget notification (no id, no reply)."""
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            if self.url:
                self._http_send(message, expect_reply=False)
            else:
                self._stdio_write(message)
        except MCPError:
            # A best-effort notification must not break the session.
            pass

    @staticmethod
    def _rpc_error(method: str, err: Any) -> str:
        if isinstance(err, dict):
            msg = err.get("message") or "unknown error"
            code = err.get("code")
            if code is not None:
                return ("MCP %s failed (%s): %s"
                        % (method, code, str(msg)[:300]))
            return "MCP %s failed: %s" % (method, str(msg)[:300])
        return "MCP %s failed: %s" % (method, str(err)[:300])

    # -- stdio transport ------------------------------------------------

    def _stdio_write(self, message: Dict[str, Any]) -> None:
        self._spawn()
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError("MCP server stdin is not available")
        data = (json.dumps(message) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            raise MCPError("the MCP server closed its input%s"
                           % self._stderr_hint())

    def _stdio_send(self, message: Dict[str, Any], req_id: int) -> Any:
        self._stdio_write(message)
        deadline = time.time() + self.timeout
        while True:
            line = self._read_line(deadline)
            try:
                obj = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                # Servers may interleave non-JSON log lines on stdout.
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("id") == req_id:
                return obj
            # Ignore notifications / responses to other ids.

    def _read_line(self, deadline: float) -> bytes:
        """Read one newline-delimited message, honoring the deadline."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise MCPError("MCP server stdout is not available")
        fd = proc.stdout.fileno()
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl])
                del self._buf[:nl + 1]
                if line.strip():
                    return line
                continue
            remaining = deadline - time.time()
            if remaining <= 0:
                raise MCPError(
                    "MCP server did not respond within %ds -- it may be "
                    "hung or not speaking MCP over stdio" % self.timeout)
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                raise MCPError("lost the connection to the MCP server%s"
                               % self._stderr_hint())
            if not ready:
                raise MCPError(
                    "MCP server did not respond within %ds -- it may be "
                    "hung or not speaking MCP over stdio" % self.timeout)
            try:
                chunk = os.read(fd, 65536)
            except OSError as e:
                raise MCPError("cannot read from the MCP server: %s" % e)
            if not chunk:
                raise MCPError("the MCP server exited before replying%s"
                               % self._stderr_hint())
            self._buf.extend(chunk)
            if len(self._buf) > _MAX_MESSAGE:
                raise MCPError("MCP server sent an oversized message "
                               "(>%d bytes)" % _MAX_MESSAGE)

    def _stderr_hint(self) -> str:
        """A short tail of the server's stderr for error messages."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ""
        try:
            data = proc.stderr.read() or b""
        except Exception:
            return ""
        tail = data.decode("utf-8", "replace").strip()
        if not tail:
            return ""
        return " (server said: %s)" % tail[-_STDERR_TAIL:]

    # -- http transport -------------------------------------------------

    def _http_send(self, message: Dict[str, Any],
                   expect_reply: bool) -> Any:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        headers.update(self.headers)
        req = urllib.request.Request(
            self.url, data=json.dumps(message).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise MCPError(self._http_error(e))
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            raise MCPError("cannot reach the MCP server at %s (%s) -- check "
                           "the URL and network" % (self.url, reason))
        except OSError as e:
            raise MCPError("cannot reach the MCP server at %s: %s"
                           % (self.url, e))
        if not expect_reply:
            return None
        return self._parse_http_body(raw, ctype)

    @staticmethod
    def _parse_http_body(raw: str, ctype: str) -> Any:
        text = raw.strip()
        if not text:
            raise MCPError("MCP server returned an empty HTTP response")
        if "text/event-stream" in ctype or text.startswith("data:"):
            # Minimal SSE: use the last JSON object carried in a data line.
            found = None
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    body = line[len("data:"):].strip()
                    if body and body != "[DONE]":
                        try:
                            found = json.loads(body)
                        except ValueError:
                            continue
            if found is None:
                raise MCPError("MCP server returned an unparseable "
                               "event-stream response")
            return found
        try:
            return json.loads(text)
        except ValueError:
            raise MCPError("MCP server returned an unparseable HTTP "
                           "response")

    def _http_error(self, e: "urllib.error.HTTPError") -> str:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if e.code in (401, 403):
            return ("the MCP server rejected the request (HTTP %d) -- check "
                    "the service-account token / permissions" % e.code)
        base = "MCP server HTTP error %d at %s" % (e.code, self.url)
        if detail:
            base += ": " + detail
        return base


def probe(command=None, url: str = "",
          headers: Optional[Dict[str, str]] = None,
          timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Connect, initialize and list tools. Never raises.

    Returns ``{"ok": bool, "tools": [names], "server": {...}}`` and, on
    failure, an additional ``"error"`` string with an actionable hint.
    """
    out: Dict[str, Any] = {"ok": False, "tools": []}
    try:
        client = MCPClient(command=command, url=url, headers=headers,
                           timeout=timeout)
    except MCPError as e:
        out["error"] = str(e)
        return out
    try:
        info = client.initialize()
        tools = client.list_tools()
        out["ok"] = True
        out["tools"] = [t.get("name", "") for t in tools if t.get("name")]
        if isinstance(info, dict) and info.get("serverInfo"):
            out["server"] = info["serverInfo"]
    except MCPError as e:
        out["error"] = str(e)
    except Exception as e:  # defensive: probe must never raise
        out["error"] = "unexpected MCP error: %s" % e
    finally:
        client.close()
    return out


# -- config generation --------------------------------------------------

_VALID_KINDS = ("claude", "kiro", "generic")


def _grafana_server_entry(grafana_url: str, kind: str) -> Dict[str, Any]:
    """The Grafana MCP server entry; token via env var, never embedded."""
    entry: Dict[str, Any] = {
        "command": GRAFANA_MCP_COMMAND,
        "args": [],
        "env": {
            GRAFANA_URL_ENV: grafana_url or "${%s}" % GRAFANA_URL_ENV,
            # Reference the token env var -- NEVER the token value.
            GRAFANA_TOKEN_ENV: GRAFANA_TOKEN_REF,
        },
    }
    if kind == "kiro":
        entry["disabled"] = False
        entry["autoApprove"] = []
    return entry


def _context_server_entry(context_path: str, kind: str) -> Dict[str, Any]:
    """A read-only filesystem server exposing the nr2grafana context.

    Uses the standard filesystem MCP server scoped to the directory that
    holds the exported nr2grafana AI context, so the assistant can read
    the migration/diagnosis/optimization bundle alongside live Grafana.
    """
    root = os.path.dirname(os.path.abspath(context_path)) or "."
    entry: Dict[str, Any] = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", root],
        "env": {},
    }
    if kind == "kiro":
        entry["disabled"] = False
        entry["autoApprove"] = []
    return entry


def generate_mcp_config(grafana_url: str = "", kind: str = "claude",
                        n2g_context_path: str = "",
                        include_grafana: bool = True) -> Dict[str, Any]:
    """Build an MCP servers config for a local AI client.

    ``kind`` is ``claude`` | ``kiro`` | ``generic`` (all currently share
    the ``mcpServers`` object shape; kiro entries add ``disabled`` /
    ``autoApprove`` fields). When ``include_grafana`` is true a Grafana
    ``mcp-grafana`` server is added, reading ``GRAFANA_URL`` and the
    ``GRAFANA_SERVICE_ACCOUNT_TOKEN`` env var -- the token value is
    NEVER written into the returned config, only the env reference
    ``${GRAFANA_SERVICE_ACCOUNT_TOKEN}``. When ``n2g_context_path`` is
    given a read-only filesystem server exposing that context is added.

    Returns a plain dict ready to ``json.dumps`` into the client's MCP
    config file.
    """
    if kind not in _VALID_KINDS:
        raise MCPError("unknown MCP config kind %r -- use one of: %s"
                       % (kind, ", ".join(_VALID_KINDS)))
    servers: Dict[str, Any] = {}
    if include_grafana:
        servers["grafana"] = _grafana_server_entry(grafana_url, kind)
    if n2g_context_path:
        servers["nr2grafana-context"] = _context_server_entry(
            n2g_context_path, kind)
    return {"mcpServers": servers}


def config_note(kind: str = "claude") -> str:
    """A short human note on where the generated config belongs."""
    if kind == "claude":
        where = ("Claude Desktop: merge into claude_desktop_config.json; "
                 "Claude Code: into ~/.claude.json or .mcp.json")
    elif kind == "kiro":
        where = "Kiro: merge into .kiro/settings/mcp.json"
    else:
        where = "merge into your client's MCP servers config"
    return ("%s. Export %s in your shell first (it is never written to "
            "the file)." % (where, GRAFANA_TOKEN_ENV))
