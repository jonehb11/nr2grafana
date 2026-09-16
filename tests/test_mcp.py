"""Tests for nr2grafana.mcp (JSON-RPC MCP client + config generator)."""

import io
import json
import sys
import textwrap
import unittest
import urllib.error
from unittest import mock

from nr2grafana import mcp
from nr2grafana.mcp import (MCPClient, MCPError, GRAFANA_TOKEN_ENV,
                            GRAFANA_TOKEN_REF, AWS_PROFILE_ENV,
                            AWS_REGION_ENV, AWS_PROFILE_REF, AWS_REGION_REF,
                            AWS_COST_MCP_COMMAND, AWS_COST_MCP_PACKAGE,
                            generate_mcp_config, cost_via_mcp, probe)


# A tiny MCP server speaking newline-delimited JSON-RPC 2.0 over stdio.
# It answers initialize, tools/list and tools/call, ignores the
# initialized notification, and echoes tool arguments back.
_FAKE_SERVER = textwrap.dedent('''
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    # A stray non-JSON log line on stdout must be tolerated by clients.
    sys.stdout.write("starting fake mcp server\\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        rid = req.get("id")
        if method == "notifications/initialized":
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-grafana", "version": "9.9"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "search_dashboards", "description": "find dashes"},
                {"name": "query_prometheus", "description": "promql"}]}})
        elif method == "tools/call":
            params = req.get("params") or {}
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(params.get("arguments"))}],
                "isError": False}})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "method not found"}})
''')

# A server that returns a JSON-RPC error for tools/call.
_ERR_SERVER = textwrap.dedent('''
    import json, sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                "result": {"serverInfo": {"name": "err"}}}) + "\\n")
            sys.stdout.flush()
        elif method == "notifications/initialized":
            continue
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32000, "message": "boom"}}) + "\\n")
            sys.stdout.flush()
''')


# A fake AWS Cost Explorer MCP server: advertises a natural-language
# "ask_cost" tool (with an inputSchema exposing a "question" arg) plus a
# structured tool with no free-text arg, and echoes the question back.
_FAKE_COST_SERVER = textwrap.dedent('''
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        rid = req.get("id")
        if method == "notifications/initialized":
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "aws-cost-explorer",
                               "version": "1.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "get_cost_and_usage",
                 "description": "structured cost query",
                 "inputSchema": {"type": "object", "properties": {
                     "start": {"type": "string"},
                     "end": {"type": "string"}}}},
                {"name": "ask_cost",
                 "description": "natural language cost question",
                 "inputSchema": {"type": "object", "properties": {
                     "question": {"type": "string"}}}}]}})
        elif method == "tools/call":
            params = req.get("params") or {}
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"tool": params.get("name"),
                     "arguments": params.get("arguments")})}],
                "isError": False}})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "method not found"}})
''')

# A cost MCP server that advertises only structured tools (no free-text
# argument) so cost_via_mcp cannot answer a natural-language question.
_FAKE_COST_STRUCT_ONLY = textwrap.dedent('''
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        rid = req.get("id")
        if method == "notifications/initialized":
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "serverInfo": {"name": "cost-struct"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "get_cost_and_usage",
                 "inputSchema": {"type": "object", "properties": {
                     "start": {"type": "string"}}}}]}})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "nope"}})
''')


def server_cmd(code):
    return [sys.executable, "-c", code]


class StdioClientTests(unittest.TestCase):
    def test_initialize_list_and_call(self):
        with MCPClient(command=server_cmd(_FAKE_SERVER), timeout=10) as c:
            info = c.initialize()
            self.assertEqual(info.get("serverInfo", {}).get("name"),
                             "fake-grafana")
            tools = c.list_tools()
            names = [t["name"] for t in tools]
            self.assertIn("search_dashboards", names)
            self.assertIn("query_prometheus", names)
            result = c.call_tool("query_prometheus", {"expr": "up"})
            text = result["content"][0]["text"]
            self.assertEqual(json.loads(text), {"expr": "up"})

    def test_lazy_initialize_on_list(self):
        with MCPClient(command=server_cmd(_FAKE_SERVER), timeout=10) as c:
            tools = c.list_tools()
            self.assertTrue(tools)
            self.assertTrue(c._initialized)

    def test_string_command_is_split(self):
        cmd = "%s -c %s" % (sys.executable, json.dumps(_FAKE_SERVER))
        # shlex handles the quoting; just confirm it parses to argv.
        client = MCPClient(command=cmd, timeout=10)
        self.assertEqual(client._argv[0], sys.executable)
        client.close()

    def test_tool_error_becomes_mcperror(self):
        with MCPClient(command=server_cmd(_ERR_SERVER), timeout=10) as c:
            c.initialize()
            with self.assertRaises(MCPError) as ctx:
                c.call_tool("anything", {})
            self.assertIn("boom", str(ctx.exception))

    def test_context_manager_closes_process(self):
        c = MCPClient(command=server_cmd(_FAKE_SERVER), timeout=10)
        with c:
            c.initialize()
            proc = c._proc
        self.assertIsNone(c._proc)
        self.assertIsNotNone(proc.poll())  # reaped

    def test_close_is_idempotent(self):
        c = MCPClient(command=server_cmd(_FAKE_SERVER), timeout=10)
        c.initialize()
        c.close()
        c.close()  # no raise

    def test_missing_command_raises(self):
        c = MCPClient(command=["definitely-not-a-real-binary-xyz"],
                      timeout=5)
        with self.assertRaises(MCPError) as ctx:
            c.initialize()
        self.assertIn("not found", str(ctx.exception))
        c.close()

    def test_exited_server_raises_actionable(self):
        # Server exits immediately without replying.
        c = MCPClient(command=[sys.executable, "-c", "pass"], timeout=5)
        with self.assertRaises(MCPError) as ctx:
            c.initialize()
        self.assertIn("exited", str(ctx.exception))
        c.close()

    def test_call_tool_requires_name(self):
        c = MCPClient(command=server_cmd(_FAKE_SERVER), timeout=10)
        with self.assertRaises(MCPError):
            c.call_tool("", {})
        c.close()


class TransportGuardTests(unittest.TestCase):
    def test_needs_exactly_one_transport(self):
        with self.assertRaises(MCPError):
            MCPClient()
        with self.assertRaises(MCPError):
            MCPClient(command=["x"], url="http://x")

    def test_empty_command_raises(self):
        with self.assertRaises(MCPError):
            MCPClient(command=[])


class HttpClientTests(unittest.TestCase):
    def _fake_urlopen(self, payload, ctype="application/json"):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.headers = {"Content-Type": ctype}
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_http_initialize_and_list(self):
        replies = [
            {"jsonrpc": "2.0", "id": 1,
             "result": {"serverInfo": {"name": "http-mcp"}}},
            {"jsonrpc": "2.0", "id": 2,
             "result": {"tools": [{"name": "t1"}]}},
        ]
        calls = {"n": 0}

        def fake_open(req, timeout=None):
            i = calls["n"]
            calls["n"] += 1
            # index 0 = initialize, 1 = initialized notify, 2 = list
            if i == 1:
                return self._fake_urlopen({})  # notify ack
            payload = replies.pop(0)
            return self._fake_urlopen(payload)

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            c = MCPClient(url="http://mcp.example/rpc")
            info = c.initialize()
            self.assertEqual(info["serverInfo"]["name"], "http-mcp")
            tools = c.list_tools()
            self.assertEqual(tools[0]["name"], "t1")

    def test_http_error_actionable(self):
        err = urllib.error.HTTPError(
            "http://mcp.example/rpc", 401, "no", {},
            io.BytesIO(b"denied"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            c = MCPClient(url="http://mcp.example/rpc")
            with self.assertRaises(MCPError) as ctx:
                c.initialize()
            self.assertIn("token", str(ctx.exception))

    def test_http_sse_body_parsed(self):
        body = ("event: message\n"
                "data: " + json.dumps(
                    {"jsonrpc": "2.0", "id": 1,
                     "result": {"serverInfo": {"name": "sse"}}}) + "\n\n")
        parsed = MCPClient._parse_http_body(body, "text/event-stream")
        self.assertEqual(parsed["result"]["serverInfo"]["name"], "sse")


class ProbeTests(unittest.TestCase):
    def test_probe_ok(self):
        out = probe(command=server_cmd(_FAKE_SERVER), timeout=10)
        self.assertTrue(out["ok"])
        self.assertIn("search_dashboards", out["tools"])
        self.assertEqual(out["server"]["name"], "fake-grafana")
        self.assertNotIn("error", out)

    def test_probe_missing_binary_never_raises(self):
        out = probe(command=["nope-not-real-xyz"], timeout=5)
        self.assertFalse(out["ok"])
        self.assertIn("error", out)
        self.assertEqual(out["tools"], [])

    def test_probe_bad_transport_never_raises(self):
        out = probe()  # no command, no url
        self.assertFalse(out["ok"])
        self.assertIn("error", out)


class ConfigTests(unittest.TestCase):
    def test_claude_shape(self):
        cfg = generate_mcp_config(grafana_url="https://g.example",
                                  kind="claude")
        self.assertIn("mcpServers", cfg)
        graf = cfg["mcpServers"]["grafana"]
        self.assertEqual(graf["command"], "mcp-grafana")
        self.assertEqual(graf["env"]["GRAFANA_URL"], "https://g.example")
        self.assertEqual(graf["env"][GRAFANA_TOKEN_ENV], GRAFANA_TOKEN_REF)
        # claude entries do not carry kiro-only fields
        self.assertNotIn("disabled", graf)

    def test_kiro_shape_has_extra_fields(self):
        cfg = generate_mcp_config(grafana_url="https://g.example",
                                  kind="kiro")
        graf = cfg["mcpServers"]["grafana"]
        self.assertIn("disabled", graf)
        self.assertEqual(graf["disabled"], False)
        self.assertEqual(graf["autoApprove"], [])

    def test_context_entry_added(self):
        cfg = generate_mcp_config(
            grafana_url="https://g.example", kind="claude",
            n2g_context_path="/tmp/out/ai-context.md")
        self.assertIn("nr2grafana-context", cfg["mcpServers"])
        entry = cfg["mcpServers"]["nr2grafana-context"]
        self.assertIn("/tmp/out", entry["args"][-1])

    def test_include_grafana_false(self):
        cfg = generate_mcp_config(
            grafana_url="https://g.example", include_grafana=False,
            n2g_context_path="/tmp/x/ai-context.md")
        self.assertNotIn("grafana", cfg["mcpServers"])
        self.assertIn("nr2grafana-context", cfg["mcpServers"])

    def test_unknown_kind_raises(self):
        with self.assertRaises(MCPError):
            generate_mcp_config(grafana_url="https://g", kind="emacs")

    def test_no_secret_embedded(self):
        # Even if a real-looking token is present in the environment, the
        # generated config must reference the env var, never the value.
        secret = "glsa_SUPER_SECRET_TOKEN_value_1234567890"
        with mock.patch.dict("os.environ",
                             {GRAFANA_TOKEN_ENV: secret}, clear=False):
            cfg = generate_mcp_config(grafana_url="https://g.example",
                                      kind="claude")
        blob = json.dumps(cfg)
        self.assertNotIn(secret, blob)
        self.assertIn(GRAFANA_TOKEN_REF, blob)

    def test_config_note_mentions_env_var(self):
        note = mcp.config_note("claude")
        self.assertIn(GRAFANA_TOKEN_ENV, note)


class AwsCostConfigTests(unittest.TestCase):
    def test_aws_cost_off_by_default(self):
        cfg = generate_mcp_config(grafana_url="https://g.example")
        self.assertNotIn("aws-cost-explorer", cfg["mcpServers"])

    def test_aws_cost_server_added(self):
        cfg = generate_mcp_config(grafana_url="https://g.example",
                                  include_aws_cost=True)
        self.assertIn("aws-cost-explorer", cfg["mcpServers"])
        entry = cfg["mcpServers"]["aws-cost-explorer"]
        self.assertEqual(entry["command"], AWS_COST_MCP_COMMAND)
        self.assertEqual(entry["command"], "uvx")
        self.assertIn(AWS_COST_MCP_PACKAGE, entry["args"])
        self.assertEqual(entry["env"][AWS_PROFILE_ENV], AWS_PROFILE_REF)
        self.assertEqual(entry["env"][AWS_REGION_ENV], AWS_REGION_REF)

    def test_aws_cost_kiro_extra_fields(self):
        cfg = generate_mcp_config(kind="kiro", include_grafana=False,
                                  include_aws_cost=True)
        entry = cfg["mcpServers"]["aws-cost-explorer"]
        self.assertEqual(entry["disabled"], False)
        self.assertEqual(entry["autoApprove"], [])

    def test_aws_cost_no_secret_embedded(self):
        # A real-looking AWS key in the environment must never leak into
        # the generated config -- only the ${AWS_PROFILE} reference.
        akid = "AKIAIOSFODNN7EXAMPLE"
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        with mock.patch.dict("os.environ",
                             {"AWS_ACCESS_KEY_ID": akid,
                              "AWS_SECRET_ACCESS_KEY": secret,
                              AWS_PROFILE_ENV: "prod"}, clear=False):
            cfg = generate_mcp_config(grafana_url="https://g.example",
                                      include_aws_cost=True)
        blob = json.dumps(cfg)
        self.assertNotIn(akid, blob)
        self.assertNotIn(secret, blob)
        self.assertNotIn("prod", blob)
        self.assertIn(AWS_PROFILE_REF, blob)
        self.assertIn(AWS_REGION_REF, blob)

    def test_grafana_still_present_alongside_aws(self):
        cfg = generate_mcp_config(grafana_url="https://g.example",
                                  include_aws_cost=True)
        self.assertIn("grafana", cfg["mcpServers"])
        self.assertIn("aws-cost-explorer", cfg["mcpServers"])


class CostViaMcpTests(unittest.TestCase):
    def test_lists_tools_without_question(self):
        with MCPClient(command=server_cmd(_FAKE_COST_SERVER),
                       timeout=10) as c:
            out = cost_via_mcp(c)
        self.assertTrue(out["ok"])
        self.assertIn("ask_cost", out["tools"])
        self.assertIn("get_cost_and_usage", out["tools"])
        self.assertNotIn("result", out)

    def test_answers_question_via_nl_tool(self):
        with MCPClient(command=server_cmd(_FAKE_COST_SERVER),
                       timeout=10) as c:
            out = cost_via_mcp(c, question="what did S3 cost last month?")
        self.assertTrue(out["ok"])
        self.assertEqual(out["tool"], "ask_cost")
        text = out["result"]["content"][0]["text"]
        echoed = json.loads(text)
        self.assertEqual(echoed["tool"], "ask_cost")
        self.assertEqual(echoed["arguments"],
                         {"question": "what did S3 cost last month?"})

    def test_no_nl_tool_returns_error_not_raise(self):
        with MCPClient(command=server_cmd(_FAKE_COST_STRUCT_ONLY),
                       timeout=10) as c:
            out = cost_via_mcp(c, question="how much?")
        self.assertFalse(out["ok"])
        self.assertIn("error", out)
        self.assertIn("get_cost_and_usage", out["tools"])

    def test_none_client_never_raises(self):
        out = cost_via_mcp(None, question="anything")
        self.assertFalse(out["ok"])
        self.assertIn("error", out)

    def test_dead_server_never_raises(self):
        # Server exits immediately without speaking MCP.
        c = MCPClient(command=[sys.executable, "-c", "pass"], timeout=5)
        out = cost_via_mcp(c, question="hi")
        self.assertFalse(out["ok"])
        self.assertIn("error", out)
        c.close()

    def test_tool_error_reported_not_raised(self):
        # ask_cost exists but the call comes back as a JSON-RPC error.
        c = MCPClient(command=server_cmd(_FAKE_COST_SERVER), timeout=10)
        c.initialize()
        with mock.patch.object(c, "call_tool",
                               side_effect=MCPError("boom")):
            out = cost_via_mcp(c, question="q")
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["error"])
        c.close()


if __name__ == "__main__":
    unittest.main()
