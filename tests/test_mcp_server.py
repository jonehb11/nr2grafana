"""Tests for nr2grafana.mcp_server (nr2grafana AS a stdio MCP server).

Two layers:

* in-process dispatch tests -- drive ``handle_call`` / ``_dispatch_method``
  directly against a temp Store (convert a pasted NR dashboard, then
  round-trip list_dashboards -> get_dashboard -> get_artifact), and check
  that a bad tool name / bad args raise a ToolError.
* an end-to-end stdio test -- spawn the real server in a subprocess and
  drive it with the project's own MCPClient (the "fake MCP server" test
  pattern), exercising initialize / tools/list / tools/call and a bad
  tool name returning a JSON-RPC error.
"""

import json
import os
import sys
import tempfile
import unittest

from nr2grafana import mcp_server
from nr2grafana.mcp_server import (Ctx, ToolError, TOOLS, handle_call,
                                   _dispatch_method)
from nr2grafana.store import Store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO_ROOT, "fixtures", "newrelic",
                       "sample-service-dashboard.json")

# Every tool the contract requires the server to expose.
EXPECTED_TOOLS = {
    "list_dashboards", "get_dashboard", "get_artifact", "convert",
    "fetch_newrelic", "validate", "parity", "compare", "samples",
    "diagnose", "deepdive", "cost_analyze", "tco", "cost_rca",
    "mitigate", "ai_context", "readiness",
    "add_datasource", "grafana_import", "grafana_test", "heal",
    "missing_datasources",
}


def _nr_fixture():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def _text(envelope):
    """The decoded JSON payload out of a tool-result envelope."""
    content = envelope["content"]
    return json.loads(content[0]["text"])


class ToolSetTests(unittest.TestCase):
    def test_tools_cover_contract(self):
        names = {t["name"] for t in TOOLS}
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_every_tool_has_schema(self):
        for tool in TOOLS:
            self.assertTrue(tool.get("name"))
            self.assertTrue(tool.get("description"))
            schema = tool.get("inputSchema")
            self.assertIsInstance(schema, dict)
            self.assertEqual(schema.get("type"), "object")
            self.assertIsInstance(schema.get("properties"), dict)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="n2g-mcp-")
        self.store = Store(os.path.join(self.tmp, "n2g.db"))
        self.out_dir = os.path.join(self.tmp, "out")
        self.ctx = Ctx(self.store, lambda m: None)

    def tearDown(self):
        self.store.close()

    def test_initialize(self):
        res = _dispatch_method("initialize", {}, self.ctx)
        self.assertEqual(res["protocolVersion"],
                         mcp_server.PROTOCOL_VERSION)
        self.assertEqual(res["serverInfo"]["name"], "nr2grafana")
        self.assertIn("tools", res["capabilities"])

    def test_tools_list(self):
        res = _dispatch_method("tools/list", {}, self.ctx)
        names = {t["name"] for t in res["tools"]}
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_unknown_method_raises(self):
        with self.assertRaises(ToolError) as cm:
            _dispatch_method("frobnicate", {}, self.ctx)
        self.assertEqual(cm.exception.code,
                         mcp_server._ERR_METHOD_NOT_FOUND)

    def test_unknown_tool_raises(self):
        with self.assertRaises(ToolError) as cm:
            handle_call("no_such_tool", {}, self.ctx)
        self.assertIn("unknown tool", str(cm.exception))
        self.assertEqual(cm.exception.code,
                         mcp_server._ERR_INVALID_PARAMS)

    def test_bad_arguments_type_raises(self):
        with self.assertRaises(ToolError):
            handle_call("list_dashboards", ["not", "an", "object"],
                        self.ctx)

    def test_missing_required_arg_raises(self):
        with self.assertRaises(ToolError):
            handle_call("get_dashboard", {}, self.ctx)

    def test_convert_list_get_artifact_roundtrip(self):
        # convert (pasted nr_json) -> the artifact comes back
        env = handle_call("convert",
                          {"nr_json": _nr_fixture(),
                           "out_dir": self.out_dir}, self.ctx)
        conv = _text(env)
        self.assertTrue(conv["dashboards"])
        self.assertFalse(conv["failed"])
        slug = conv["dashboards"][0]["slug"]

        # list_dashboards sees it
        listed = _text(handle_call("list_dashboards", {}, self.ctx))
        slugs = [d["slug"] for d in listed["dashboards"]]
        self.assertIn(slug, slugs)

        # get_dashboard returns the Grafana JSON + widget report
        got = _text(handle_call("get_dashboard", {"slug": slug},
                                self.ctx))
        self.assertEqual(got["slug"], slug)
        self.assertIn("panels", got["dashboard"])

        # get_artifact round-trips the persisted widget-report
        art = _text(handle_call("get_artifact",
                                {"slug": slug,
                                 "kind": "widget-report"}, self.ctx))
        self.assertEqual(art["kind"], "widget-report")
        self.assertIn("widgets", art["artifact"])

    def test_get_artifact_missing_raises(self):
        with self.assertRaises(ToolError):
            handle_call("get_artifact",
                        {"slug": "nope", "kind": "parity"}, self.ctx)

    def test_validate_path_ok_and_problems(self):
        # A converted dashboard, then validate its stored slug.
        env = handle_call("convert",
                          {"nr_json": _nr_fixture(),
                           "out_dir": self.out_dir}, self.ctx)
        slug = _text(env)["dashboards"][0]["slug"]
        res = _text(handle_call("validate", {"slug": slug}, self.ctx))
        self.assertTrue(res["ok"])
        self.assertEqual(res["problems"], [])

    def test_validate_bad_path_raises(self):
        with self.assertRaises(ToolError):
            handle_call("validate",
                        {"path": os.path.join(self.tmp, "nope.json")},
                        self.ctx)

    def test_validate_needs_path_or_slug(self):
        with self.assertRaises(ToolError):
            handle_call("validate", {}, self.ctx)

    def test_readiness_unknown_slug_raises(self):
        with self.assertRaises(ToolError):
            handle_call("readiness", {"slug": "ghost"}, self.ctx)

    def test_parity_without_grafana_raises_actionable(self):
        # No Grafana configured -> an actionable ToolError, not a crash.
        env = handle_call("convert",
                          {"nr_json": _nr_fixture(),
                           "out_dir": self.out_dir}, self.ctx)
        slug = _text(env)["dashboards"][0]["slug"]
        with self.assertRaises(ToolError) as cm:
            handle_call("parity", {"slug": slug}, self.ctx)
        self.assertNotIn("Traceback", str(cm.exception))

    def test_ai_context_whole_workspace(self):
        handle_call("convert",
                    {"nr_json": _nr_fixture(),
                     "out_dir": self.out_dir}, self.ctx)
        ctx_bundle = _text(handle_call("ai_context", {}, self.ctx))
        self.assertIn("schema", ctx_bundle)
        md = _text(handle_call("ai_context", {"format": "markdown"},
                               self.ctx))
        self.assertEqual(md["format"], "markdown")
        self.assertIsInstance(md["markdown"], str)

    # -- 1.11 SEAM-REPORT / live / bind pass-through ---------------------

    def _convert(self, **extra):
        args = {"nr_json": _nr_fixture(), "out_dir": self.out_dir}
        args.update(extra)
        conv = _text(handle_call("convert", args, self.ctx))
        self.assertTrue(conv["dashboards"], conv)
        return conv

    def test_missing_datasources_tool_shape(self):
        slug = self._convert()["dashboards"][0]["slug"]
        res = _text(handle_call("missing_datasources", {"slug": slug},
                                self.ctx))
        self.assertEqual(res["slug"], slug)
        for key in ("missing_datasources", "datasources_to_add",
                    "manual_panels", "needs_review", "counts"):
            self.assertIn(key, res)
        # Offline (no Grafana configured) every ${var} ref is unbound,
        # so the converted dashboard's families are reported missing,
        # each with the exact add-datasource template.
        self.assertFalse(res["grafana_checked"])
        self.assertIn("prometheus", res["missing_datasources"])
        prom = next(d for d in res["datasources_to_add"]
                    if d["family"] == "prometheus")
        tpl = prom["template"]
        self.assertEqual(tpl["type"], "prometheus")
        self.assertEqual(tpl["api"]["path"], "/api/grafana/datasource")
        self.assertEqual(tpl["mcp"]["tool"], "add_datasource")
        self.assertIn("url", tpl["mcp"]["arguments"])
        self.assertIn("add-datasource", tpl["cli"])
        for row in res["manual_panels"]:
            self.assertIn("panel_id", row)
            self.assertIn("why", row)
            self.assertIn("closest_equivalent", row)

    def test_missing_datasources_unknown_slug_raises(self):
        with self.assertRaises(ToolError) as cm:
            handle_call("missing_datasources", {"slug": "ghost"},
                        self.ctx)
        self.assertIn("ghost", str(cm.exception))

    def test_missing_datasources_requires_slug(self):
        with self.assertRaises(ToolError):
            handle_call("missing_datasources", {}, self.ctx)

    def test_convert_accepts_live_env_bind_offline(self):
        # No NR key / Grafana URL: live hints and bind degrade to job
        # notes, env still pins the target env; the convert succeeds.
        conv = self._convert(live=True, env="prod", bind=True)
        entry = conv["dashboards"][0]
        self.assertEqual(entry["env"], "prod")
        self.assertFalse(entry["live_hints"])
        self.assertFalse(entry["bound"])
        self.assertIn("missing_datasources", entry)
        self.assertIsInstance(entry["manual_panels"], int)

    def test_get_dashboard_and_readiness_surface_missing(self):
        slug = self._convert()["dashboards"][0]["slug"]
        got = _text(handle_call("get_dashboard", {"slug": slug},
                                self.ctx))
        self.assertIn("missing", got)
        self.assertIn("missing_datasources", got["missing"])
        rd = _text(handle_call("readiness", {"slug": slug}, self.ctx))
        self.assertIn("missing_datasources", rd)
        self.assertIn("manual_panels", rd)

    def test_tool_schemas_advertise_live_env_bind(self):
        by_name = {t["name"]: t for t in TOOLS}
        props = by_name["convert"]["inputSchema"]["properties"]
        for key in ("live", "env", "bind"):
            self.assertIn(key, props)
        imp = by_name["grafana_import"]["inputSchema"]["properties"]
        self.assertIn("bind", imp)
        self.assertIn("env", imp)
        self.assertEqual(
            by_name["missing_datasources"]["inputSchema"]["required"],
            ["slug"])


class StdioServerTests(unittest.TestCase):
    """Drive the real server over stdio via the project's MCPClient."""

    def setUp(self):
        from nr2grafana.mcp import MCPClient
        self._MCPClient = MCPClient
        self.tmp = tempfile.mkdtemp(prefix="n2g-mcp-stdio-")
        self._old_db = os.environ.get("N2G_DB")
        self._old_pp = os.environ.get("PYTHONPATH")
        os.environ["N2G_DB"] = os.path.join(self.tmp, "n2g.db")
        # Ensure the child can import the package regardless of cwd.
        os.environ["PYTHONPATH"] = (
            REPO_ROOT + os.pathsep + (self._old_pp or "")).rstrip(
                os.pathsep)

    def tearDown(self):
        for name, old in (("N2G_DB", self._old_db),
                          ("PYTHONPATH", self._old_pp)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    def _client(self):
        code = ("from nr2grafana.mcp_server import serve_stdio; "
                "serve_stdio()")
        return self._MCPClient(command=[sys.executable, "-c", code],
                               timeout=30)

    def test_full_stdio_flow(self):
        out_dir = os.path.join(self.tmp, "out")
        with self._client() as client:
            info = client.initialize()
            self.assertEqual(
                info.get("serverInfo", {}).get("name"), "nr2grafana")

            tools = client.list_tools()
            names = {t["name"] for t in tools}
            self.assertEqual(names, EXPECTED_TOOLS)

            # convert a pasted NR dashboard
            res = client.call_tool(
                "convert", {"nr_json": _nr_fixture(),
                            "out_dir": out_dir})
            conv = json.loads(res["content"][0]["text"])
            self.assertTrue(conv["dashboards"])
            slug = conv["dashboards"][0]["slug"]

            # list_dashboards sees it (same in-child store)
            listed = json.loads(client.call_tool(
                "list_dashboards", {})["content"][0]["text"])
            self.assertIn(slug,
                          [d["slug"] for d in listed["dashboards"]])

            # get_artifact round-trips
            art = json.loads(client.call_tool(
                "get_artifact",
                {"slug": slug, "kind": "widget-report"})
                ["content"][0]["text"])
            self.assertIn("widgets", art["artifact"])

    def test_bad_tool_name_is_mcp_error(self):
        from nr2grafana.mcp import MCPError
        with self._client() as client:
            client.initialize()
            with self.assertRaises(MCPError):
                client.call_tool("definitely_not_a_tool", {})


class McpLiveLoopTests(unittest.TestCase):
    """Close the whole migration loop through the MCP tools against the
    offline mock stack: convert -> add_datasource (prom/loki/tempo) ->
    grafana_import -> grafana_test -> parity -> readiness. Proves an AI
    driving only the MCP surface can take a New Relic dashboard all the
    way to a data-backed, scored Grafana migration.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
        try:
            import mock_stack
        except Exception as exc:  # pragma: no cover - environment guard
            self.skipTest("mock_stack unavailable: %s" % exc)
        self.stack = mock_stack.start_mock()
        self.addCleanup(self.stack.stop)
        self.tmp = tempfile.mkdtemp(prefix="n2g-mcp-loop-")
        self.store = Store(os.path.join(self.tmp, "n2g.db"))
        self.ctx = Ctx(self.store, lambda m: None)
        # Point the web SESSION (reused by every tool) at the mock stack.
        from nr2grafana.web import server as web
        self._web = web
        self._old_session = web.SESSION
        self._old_ng = os.environ.get("N2G_NERDGRAPH_URL")
        os.environ["N2G_NERDGRAPH_URL"] = self.stack.nr_url + "/graphql"
        sess = web.Session()
        sess.grafana_url = self.stack.grafana_url
        sess.grafana_token = self.stack.grafana_token
        sess.nr_api_key = self.stack.nr_api_key
        web.SESSION = sess
        self.addCleanup(self._restore)

    def _restore(self):
        self._web.SESSION = self._old_session
        if self._old_ng is None:
            os.environ.pop("N2G_NERDGRAPH_URL", None)
        else:
            os.environ["N2G_NERDGRAPH_URL"] = self._old_ng
        self.store.close()

    def _tool(self, name, args):
        return json.loads(
            handle_call(name, args, self.ctx)["content"][0]["text"])

    def test_convert_provision_import_parity(self):
        with open(FIXTURE, encoding="utf-8") as handle:
            nr = json.load(handle)
        conv = self._tool("convert", {"nr_json": nr})
        slug = conv["dashboards"][0]["slug"]

        # Before provisioning, diagnose flags the missing datasources.
        diag = self._tool("diagnose", {"slug": slug})
        areas = diag["summary"]["by_area"]
        self.assertGreaterEqual(areas.get("datasource", 0), 1)

        # Provision the three LGTM datasources through the MCP tool.
        for ds_type, url in (("prometheus", "http://mimir:9009/prometheus"),
                             ("loki", "http://loki:3100"),
                             ("tempo", "http://tempo:3200")):
            res = self._tool("add_datasource",
                             {"type": ds_type, "name": ds_type,
                              "url": url})
            self.assertTrue(res["created"]["uid"])
            self.assertIn(str(res["health"].get("status")).lower(),
                          ("ok",))

        # Import the converted dashboard into the (mock) Grafana.
        imp = self._tool("grafana_import", {"slug": slug})
        self.assertEqual(imp["ok"], 1)
        self.assertEqual(imp["total"], 1)

        # Per-panel data test: panels now return data, none error.
        test = self._tool("grafana_test", {"slug": slug})
        self.assertEqual(test["summary"].get("error", 0), 0)
        self.assertGreater(test["summary"].get("data", 0), 5)

        # Parity now finds real matches instead of gf-error everywhere.
        par = self._tool("parity", {"slug": slug})
        self.assertGreaterEqual(par["summary"].get("match", 0), 5)
        self.assertNotIn("gf-error", par["summary"])

        # Readiness reflects the data-backed migration (no longer blocked).
        rd = self._tool("readiness", {"slug": slug})
        self.assertNotEqual(rd.get("grade"), "blocked")
        self.assertGreater(rd.get("score", 0), 0)

    def test_add_datasource_missing_url_is_actionable(self):
        with self.assertRaises(ToolError) as cm:
            handle_call("add_datasource",
                        {"type": "prometheus", "name": "p"}, self.ctx)
        self.assertIn("url", str(cm.exception).lower())

    def test_add_datasource_unknown_type_is_actionable(self):
        with self.assertRaises(ToolError) as cm:
            handle_call("add_datasource",
                        {"type": "no-such-ds", "name": "x",
                         "url": "http://x"}, self.ctx)
        self.assertIn("unknown datasource type", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
