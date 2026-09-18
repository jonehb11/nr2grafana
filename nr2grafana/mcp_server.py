"""nr2grafana AS a stdio MCP server (stdlib only).

This is the SERVER side of the Model Context Protocol: a
newline-delimited JSON-RPC 2.0 server over stdio (the same transport the
client in :mod:`nr2grafana.mcp` speaks). A local AI (Claude / Kiro) can
launch ``python3 -m nr2grafana mcp serve`` and call every nr2grafana
operation as a tool -- convert a New Relic dashboard, inspect the
converted artifacts, run parity / compare / diagnose against a live
Grafana, analyze LGTM-stack and AWS cost, root-cause a cost anomaly, and
export the compact AI-context bundle.

Design notes / guards (kept in step with the rest of the tool):

* Each tool maps to the SAME library / job logic the web server uses --
  we import :mod:`nr2grafana.web.server` and call its ``_job_*`` helpers
  with a lightweight in-memory job, so a tool call runs the operation
  synchronously and returns the finished artifact. Nothing is shelled
  out; New Relic and AWS stay strictly READ-ONLY; the tool PROPOSES,
  never executes AWS/K8s changes.
* Secrets (New Relic / Grafana / Anthropic keys) come from the
  environment (via the web ``Session``) and never from tool arguments
  unless the caller passes them; they are never logged and never echoed
  in a tool result. Non-secret preferences are hydrated from the Store.
* stdout carries ONLY JSON-RPC messages -- every log line goes to
  stderr (or the caller's ``log``), so the protocol channel stays clean.
* Errors surface as JSON-RPC error objects with an actionable message,
  never a traceback.

Public surface::

    def serve_stdio(store=None, log=None) -> None
    TOOLS: list of {name, description, inputSchema}
    def handle_call(name, arguments, ctx) -> dict
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"

# JSON-RPC error codes we emit.
_ERR_METHOD_NOT_FOUND = -32601
_ERR_INVALID_PARAMS = -32602
_ERR_INTERNAL = -32603
_ERR_TOOL = -32000

_WEB = None  # cached nr2grafana.web.server module (imported lazily)


class ToolError(Exception):
    """A tool / method failure carrying a JSON-RPC error code.

    Its message is actionable and safe to return to the caller; it never
    contains a traceback or a secret.
    """

    def __init__(self, message: str, code: int = _ERR_TOOL) -> None:
        super().__init__(message)
        self.code = code


class Ctx:
    """Per-server context passed to every tool: the Store and a logger."""

    def __init__(self, store: Any, log: Callable[[str], None]) -> None:
        self.store = store
        self.log = log


def _version() -> str:
    try:
        from . import __version__ as ver
        return str(ver)
    except Exception:
        return "0"


def _web():
    """The web.server module (job / library call logic), imported lazily.

    Raises an actionable :class:`ToolError` when it is not importable, so
    a tool call degrades cleanly instead of crashing the server.
    """
    global _WEB
    if _WEB is None:
        try:
            _WEB = importlib.import_module("nr2grafana.web.server")
        except Exception as exc:
            raise ToolError("nr2grafana web/library layer is not "
                            "importable: %s" % exc, _ERR_INTERNAL)
    return _WEB


def _errmsg(exc: Exception) -> str:
    """Actionable one-line message for ``exc`` (never a traceback)."""
    try:
        return _web()._errmsg(exc)
    except Exception:
        return "%s: %s" % (type(exc).__name__, exc)


def _job(kind: str):
    """A lightweight in-memory job for the reused web ``_job_*`` helpers.

    Its ``add`` collects progress lines in memory (never on stdout); the
    helper returns the finished artifact directly.
    """
    return _web()._Job("mcp", kind)


def _sub(arguments: Dict[str, Any], keys) -> Dict[str, Any]:
    """A request body from the tool arguments, keeping only ``keys`` that
    were actually supplied (so unset args fall back to env / defaults)."""
    body: Dict[str, Any] = {}
    for key in keys:
        if arguments.get(key) is not None:
            body[key] = arguments[key]
    return body


def _req_str(arguments: Dict[str, Any], name: str) -> str:
    val = arguments.get(name)
    if val is None or (isinstance(val, str) and not val.strip()):
        raise ToolError("missing required argument %r" % name,
                        _ERR_INVALID_PARAMS)
    return str(val)


# ---------------------------------------------------------------------------
# tool implementations (each maps to the same library / job logic the web
# server uses)
# ---------------------------------------------------------------------------

def _t_list_dashboards(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    return {"dashboards": ctx.store.list_dashboards()}


def _t_get_dashboard(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    slug = _req_str(arguments, "slug")
    row = ctx.store.get_dashboard(slug)
    if not row:
        raise ToolError("no dashboard with slug %r -- run convert first"
                        % slug)
    dash = web._dash_from_row(slug, row)

    def art(kind: str) -> Any:
        return web._artifact(ctx.store, slug, kind)

    return {
        "slug": slug,
        "title": row.get("title", dash.get("title", slug)),
        "source": row.get("source", ""),
        "nr_guid": row.get("nr_guid", ""),
        "dashboard": dash,
        "requirements": art("requirements"),
        "widget_report": (art("widget-report") or {}).get("widgets", []),
        "datatest": art("datatest"),
        "check": art("check"),
        "parity": art("parity"),
        "diagnosis": art("diagnosis"),
        "samples": art("samples"),
        "review": art("review"),
        "changes": ctx.store.list_changes(slug),
        "package_dir": web._package_dir(ctx.store, slug),
    }


def _t_get_artifact(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    slug = _req_str(arguments, "slug")
    kind = _req_str(arguments, "kind")
    art = ctx.store.get_artifact(slug, kind)
    if art is None:
        raise ToolError("no %r artifact for slug %r" % (kind, slug))
    return {"slug": slug, "kind": kind, "artifact": art}


def _t_convert(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("input_dir", "out_dir", "config_path"))
    if arguments.get("package") is not None:
        body["package"] = bool(arguments["package"])
    nr_json = arguments.get("nr_json")
    if nr_json is not None:
        pasted = web._normalize_nr_json(nr_json)
        return web._job_convert(_job("convert"), body, ctx.store,
                                pasted=pasted)
    if not body.get("input_dir") and not getattr(
            web.SESSION, "input_dir", ""):
        raise ToolError("convert needs 'nr_json' (a pasted New Relic "
                        "dashboard) or 'input_dir' (a directory of "
                        ".json dashboards)", _ERR_INVALID_PARAMS)
    return web._job_convert(_job("convert"), body, ctx.store)


def _t_fetch_newrelic(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    web._nerdgraph()  # fail fast (400) when no NR key is configured
    body: Dict[str, Any] = {}
    if arguments.get("guids") is not None:
        body["guids"] = arguments["guids"]
    if arguments.get("out_dir") is not None:
        body["out"] = arguments["out_dir"]
    return web._job_nr_fetch(_job("nr-fetch"), body)


def _t_validate(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    try:
        from .grafana.validate import validate_dashboard
    except Exception as exc:
        raise ToolError("dashboard validation is unavailable: %s" % exc,
                        _ERR_INTERNAL)
    path = arguments.get("path")
    slug = arguments.get("slug")
    if path:
        try:
            with open(str(path), encoding="utf-8") as handle:
                dash = json.load(handle)
        except FileNotFoundError:
            raise ToolError("no such file: %s" % path,
                            _ERR_INVALID_PARAMS)
        except (OSError, ValueError) as exc:
            raise ToolError("cannot read dashboard JSON %s: %s"
                            % (path, exc), _ERR_INVALID_PARAMS)
        problems = validate_dashboard(dash)
        return {"path": str(path), "ok": not problems,
                "problems": problems}
    if slug:
        row = ctx.store.get_dashboard(str(slug))
        if not row:
            raise ToolError("no dashboard with slug %r" % slug)
        dash = web._dash_from_row(str(slug), row)
        problems = validate_dashboard(dash)
        return {"slug": str(slug), "ok": not problems,
                "problems": problems}
    raise ToolError("validate needs a 'path' or a 'slug'",
                    _ERR_INVALID_PARAMS)


def _t_parity(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("slug", "from", "to", "ds_map",
                            "account_ids"))
    return web._job_parity(_job("parity"), body, ctx.store)


def _t_compare(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("slug", "from", "to", "ds_map",
                            "account_ids"))
    return web._job_compare(_job("compare"), body, ctx.store)


def _t_samples(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("slug", "panel_id", "from", "to", "limit",
                            "account_ids"))
    return web._job_samples(_job("samples"), body, ctx.store)


def _t_diagnose(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("slug",))
    return web._job_diagnose(_job("diagnose"), body, ctx.store)


def _t_deepdive(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("prom", "mimir", "loki", "slug", "pricing"))
    if arguments.get("kube") is not None:
        body["kube"] = bool(arguments["kube"])
    return web._job_deepdive(_job("deepdive"), body, ctx.store)


def _t_cost_analyze(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("slug", "from", "to", "pricing", "ds_uids"))
    return web._job_cost(_job("cost"), body, ctx.store)


def _t_tco(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("months", "profile", "region", "group_by",
                            "buckets"))
    return web._job_tco(_job("tco"), body, ctx.store)


def _t_cost_rca(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("anomaly_id", "profile", "region", "slug",
                            "start", "end", "flow_logs_group"))
    report = arguments.get("anomaly_report")
    if report is None:
        report = arguments.get("report")
    if report is not None:
        body["report"] = report
    return web._job_rca(_job("rca"), body, ctx.store)


def _t_mitigate(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("slug", "rca"))
    return web._job_mitigate(_job("mitigate"), body, ctx.store)


def _t_ai_context(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    aicontext = web._lazy("aicontext")
    slug = str(arguments.get("slug") or "")
    fmt = str(arguments.get("format") or "").lower()
    grafana = web._optional_grafana_live()
    deepdive = web._artifact(ctx.store, web._cost_slug(slug), "deepdive")
    context = aicontext.build_context(
        ctx.store, slug=slug, grafana=grafana, deepdive=deepdive,
        redact=True)
    if fmt in ("markdown", "md"):
        return {"slug": slug, "format": "markdown",
                "markdown": aicontext.to_markdown(context)}
    return context


def _t_add_datasource(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    ds_type = _req_str(arguments, "type")
    name = _req_str(arguments, "name")
    live_mod = importlib.import_module("nr2grafana.grafana.live")
    values: Dict[str, Any] = dict(arguments.get("settings") or {})
    if arguments.get("url") is not None:
        values.setdefault("url", arguments["url"])
    try:
        payload = live_mod.build_datasource_payload(ds_type, name, values)
    except Exception as exc:  # unknown type / missing required field
        raise ToolError(_errmsg(exc), _ERR_INVALID_PARAMS)
    live = web._grafana_live()  # actionable ApiError when unconfigured
    resp = live.create_datasource(payload)
    created = resp.get("datasource") if isinstance(resp, dict) else None
    if not isinstance(created, dict):
        created = resp if isinstance(resp, dict) else {}
    uid = created.get("uid") or ""
    health: Dict[str, Any] = {"status": "unknown",
                              "message": "no uid returned"}
    health_fn = getattr(live, "datasource_health", None)
    if uid and callable(health_fn):
        try:
            health = health_fn(uid)
        except Exception as exc:  # health probe must not fail the call
            health = {"status": "error", "message": _errmsg(exc)}
    try:
        web._lazy("changelog").ChangeLog(ctx.store).record(
            "", "datasource-created", name, "",
            {"uid": uid, "type": created.get("type", ""), "name": name},
            why="mcp add_datasource", source="user")
    except Exception:
        pass  # change-log recording is best-effort
    return {"created": {"uid": uid,
                        "name": created.get("name", name),
                        "type": created.get("type",
                                            payload.get("type", ds_type))},
            "health": health}


def _t_grafana_import(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("slug", "slugs", "folder"))
    if arguments.get("overwrite") is not None:
        body["overwrite"] = bool(arguments["overwrite"])
    if not body.get("slug") and not body.get("slugs"):
        raise ToolError("grafana_import needs a 'slug' (or 'slugs' list) "
                        "of a converted dashboard", _ERR_INVALID_PARAMS)
    return web._job_grafana_import(_job("import"), body, ctx.store)


def _t_grafana_test(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    slug = _req_str(arguments, "slug")
    return web._job_grafana_test(_job("test"), {"slug": slug}, ctx.store)


def _t_heal(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    body = _sub(arguments, ("slug",))
    if arguments.get("push") is not None:
        body["push"] = bool(arguments["push"])
    if not body.get("slug"):
        raise ToolError("heal needs a 'slug' of a converted dashboard",
                        _ERR_INVALID_PARAMS)
    return web._job_heal(_job("heal"), body, ctx.store)


def _t_readiness(arguments: Dict[str, Any], ctx: Ctx) -> Any:
    web = _web()
    slug = _req_str(arguments, "slug")
    if not ctx.store.get_dashboard(slug):
        raise ToolError("no dashboard with slug %r" % slug)
    parity_mod = web._lazy("parity")
    res = parity_mod.readiness(
        web._artifact(ctx.store, slug, "parity"),
        check_rows=(web._artifact(ctx.store, slug, "check")
                    or {}).get("items"),
        test_rows=(web._artifact(ctx.store, slug, "datatest")
                   or {}).get("results"),
        review=web._artifact(ctx.store, slug, "review"))
    try:
        res["review"] = web._lazy("samples").review_summary(
            ctx.store, slug)
    except Exception:
        pass
    return res


# name -> (implementation, description, inputSchema)
_TOOL_SPECS = [
    ("list_dashboards", _t_list_dashboards,
     "List every converted dashboard in the local store (slug, title, "
     "source, and which artifacts exist for each).",
     {"type": "object", "properties": {}}),
    ("get_dashboard", _t_get_dashboard,
     "Get one converted dashboard's Grafana JSON plus its stored "
     "artifacts (requirements, widget report, tests, parity, "
     "diagnosis, samples, review, change log).",
     {"type": "object",
      "properties": {"slug": {"type": "string",
                              "description": "dashboard slug"}},
      "required": ["slug"]}),
    ("get_artifact", _t_get_artifact,
     "Get a single stored artifact for a dashboard by kind (e.g. "
     "requirements, widget-report, parity, diagnosis, cost, tco, rca).",
     {"type": "object",
      "properties": {"slug": {"type": "string"},
                     "kind": {"type": "string",
                              "description": "artifact kind"}},
      "required": ["slug", "kind"]}),
    ("convert", _t_convert,
     "Convert New Relic dashboard(s) to Grafana. Provide 'nr_json' (a "
     "pasted New Relic dashboard object or a list of them) OR "
     "'input_dir' (a directory of .json dashboards). Persists the "
     "converted dashboards and artifacts and returns the result.",
     {"type": "object",
      "properties": {
          "nr_json": {"description": "a New Relic dashboard object or a "
                                     "list of them (pasted)"},
          "input_dir": {"type": "string",
                        "description": "directory of .json dashboards"},
          "out_dir": {"type": "string",
                      "description": "output directory (packaging)"},
          "config_path": {"type": "string"},
          "package": {"type": "boolean",
                      "description": "write a package directory "
                                     "(default true)"}}}),
    ("fetch_newrelic", _t_fetch_newrelic,
     "Fetch dashboards FROM New Relic via NerdGraph (read-only). Needs "
     "a New Relic API key in the environment. Optionally limit to "
     "'guids'; writes .json files into 'out_dir'.",
     {"type": "object",
      "properties": {
          "guids": {"type": "array", "items": {"type": "string"},
                    "description": "dashboard GUIDs (default: all)"},
          "out_dir": {"type": "string"}}}),
    ("validate", _t_validate,
     "Statically validate Grafana dashboard JSON. Provide 'path' (a "
     "file) or 'slug' (a stored dashboard). Returns the list of "
     "problems (empty when valid).",
     {"type": "object",
      "properties": {"path": {"type": "string"},
                     "slug": {"type": "string"}}}),
    ("parity", _t_parity,
     "Compare New Relic vs Grafana data for a dashboard and score the "
     "migration parity. Needs a live Grafana and a New Relic key.",
     {"type": "object",
      "properties": {"slug": {"type": "string"},
                     "from": {"type": "string",
                              "description": "range start "
                                             "(default now-1h)"},
                     "to": {"type": "string",
                            "description": "range end (default now)"}},
      "required": ["slug"]}),
    ("compare", _t_compare,
     "Build a side-by-side New Relic vs Grafana comparison for a "
     "dashboard (New Relic side is best-effort when no key is set).",
     {"type": "object",
      "properties": {"slug": {"type": "string"},
                     "from": {"type": "string"},
                     "to": {"type": "string"}},
      "required": ["slug"]}),
    ("samples", _t_samples,
     "Pull raw side-by-side samples for a dashboard (optionally a "
     "single 'panel_id') so each panel can be confirmed or rejected.",
     {"type": "object",
      "properties": {"slug": {"type": "string"},
                     "panel_id": {"description": "one panel id "
                                                 "(optional)"},
                     "from": {"type": "string"},
                     "to": {"type": "string"},
                     "limit": {"type": "integer"}},
      "required": ["slug"]}),
    ("diagnose", _t_diagnose,
     "Diagnose a converted dashboard against a live Grafana (missing "
     "datasources/plugins, no-data panels, ...) and return findings.",
     {"type": "object",
      "properties": {"slug": {"type": "string"}},
      "required": ["slug"]}),
    ("deepdive", _t_deepdive,
     "Deep LGTM-stack analysis from component self-metrics; pass "
     "direct 'prom'/'mimir'/'loki' URLs and/or 'kube' true for "
     "Kubernetes topology / right-sizing (read-only).",
     {"type": "object",
      "properties": {"prom": {"type": "string"},
                     "mimir": {"type": "string"},
                     "loki": {"type": "string"},
                     "kube": {"type": "boolean"},
                     "slug": {"type": "string"}}}),
    ("cost_analyze", _t_cost_analyze,
     "Estimate LGTM-stack cost from sampled traffic vs what the "
     "migrated dashboards need, and propose safe savings. 'slug' "
     "scopes it to one dashboard; omit for the whole instance.",
     {"type": "object",
      "properties": {"slug": {"type": "string"},
                     "from": {"type": "string"},
                     "to": {"type": "string"}}}),
    ("tco", _t_tco,
     "Discover AWS spend over time via Cost Explorer (read-only, local "
     "credentials), attribute the observability share and forecast. "
     "Optional 'months' and AWS 'profile'/'region'.",
     {"type": "object",
      "properties": {"months": {"type": "integer"},
                     "profile": {"type": "string"},
                     "region": {"type": "string"},
                     "group_by": {"type": "string"}}}),
    ("cost_rca", _t_cost_rca,
     "Root-cause an AWS cost anomaly. Provide 'anomaly_report' (pasted "
     "text or JSON) OR 'anomaly_id' (from Cost Explorer). AWS is "
     "optional and strictly read-only; the analysis degrades cleanly "
     "without it.",
     {"type": "object",
      "properties": {
          "anomaly_report": {"description": "pasted anomaly report "
                                            "(text or JSON)"},
          "anomaly_id": {"type": "string"},
          "profile": {"type": "string"},
          "region": {"type": "string"},
          "slug": {"type": "string"}}}),
    ("mitigate", _t_mitigate,
     "Turn a stored (or supplied) RCA into a ranked, reliability-safe "
     "mitigation plan. Proposal only -- nothing is executed against "
     "AWS/K8s. Pass 'slug' or an 'rca' object.",
     {"type": "object",
      "properties": {"slug": {"type": "string"},
                     "rca": {"type": "object",
                             "description": "an RCA result (optional)"}}}),
    ("ai_context", _t_ai_context,
     "Return the compact, redacted AI-context bundle for a dashboard "
     "(or the whole workspace when 'slug' is empty). format 'markdown' "
     "returns the rendered brief instead of JSON.",
     {"type": "object",
      "properties": {"slug": {"type": "string"},
                     "format": {"type": "string",
                                "enum": ["json", "markdown", "md"]}}}),
    ("readiness", _t_readiness,
     "Overall migration readiness for a dashboard (score / grade / "
     "reasons) from its parity, check, test and human-review "
     "artifacts.",
     {"type": "object",
      "properties": {"slug": {"type": "string"}},
      "required": ["slug"]}),
    ("add_datasource", _t_add_datasource,
     "Create a Grafana datasource on the live instance so converted "
     "dashboards resolve and light up. Pass 'type' (prometheus, loki, "
     "tempo, cloudwatch, ...), a 'name', and the connection 'url' "
     "(and/or extra 'settings' keyed by field name). Returns the "
     "created datasource uid and a live health check.",
     {"type": "object",
      "properties": {
          "type": {"type": "string",
                   "description": "datasource type / plugin id "
                                  "(prometheus, loki, tempo, ...)"},
          "name": {"type": "string"},
          "url": {"type": "string",
                  "description": "base URL as reachable FROM the "
                                 "Grafana server"},
          "settings": {"type": "object",
                       "description": "extra field values (e.g. "
                                      "jsonData.httpMethod), keyed by "
                                      "the datasource template field "
                                      "name"}},
      "required": ["type", "name"]}),
    ("grafana_import", _t_grafana_import,
     "Import a converted dashboard into the live Grafana instance. Pass "
     "'slug' (or 'slugs' for several), an optional 'folder', and "
     "'overwrite' true to replace an existing one. Returns each "
     "dashboard's Grafana URL / uid.",
     {"type": "object",
      "properties": {
          "slug": {"type": "string"},
          "slugs": {"type": "array", "items": {"type": "string"}},
          "folder": {"type": "string",
                     "description": "Grafana folder (created if absent)"},
          "overwrite": {"type": "boolean"}}}),
    ("grafana_test", _t_grafana_test,
     "Run each panel's query against the live Grafana and record which "
     "targets return data / no-data / error (the datatest artifact that "
     "feeds readiness). Needs the datasources to exist.",
     {"type": "object",
      "properties": {"slug": {"type": "string"}},
      "required": ["slug"]}),
    ("heal", _t_heal,
     "Auto-heal a converted dashboard against the live Grafana: apply "
     "the safe fixes from its diagnosis (create datasources that have "
     "enough info, correct datasource refs, ...) and re-persist. Pass "
     "'push' true to also push the fixed dashboard back to Grafana. "
     "Returns the fixes applied and any findings still needing input.",
     {"type": "object",
      "properties": {"slug": {"type": "string"},
                     "push": {"type": "boolean"}},
      "required": ["slug"]}),
]

TOOLS: List[Dict[str, Any]] = [
    {"name": name, "description": desc, "inputSchema": schema}
    for name, _fn, desc, schema in _TOOL_SPECS
]

_TOOL_FUNCS: Dict[str, Callable[[Dict[str, Any], Ctx], Any]] = {
    name: fn for name, fn, _desc, _schema in _TOOL_SPECS
}


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------

def handle_call(name: str, arguments: Any, ctx: Ctx) -> Dict[str, Any]:
    """Dispatch a ``tools/call`` to the library and wrap the result.

    Returns the MCP tool-result envelope
    ``{"content": [{"type": "text", "text": <json-string>}]}``. Raises
    :class:`ToolError` for an unknown tool, bad arguments, or any library
    failure (the caller turns it into a JSON-RPC error object -- never a
    traceback).
    """
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        raise ToolError("unknown tool: %s" % name, _ERR_INVALID_PARAMS)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError("tool arguments must be a JSON object",
                        _ERR_INVALID_PARAMS)
    try:
        result = fn(arguments, ctx)
    except ToolError:
        raise
    except Exception as exc:  # any library error -> actionable message
        raise ToolError(_errmsg(exc))
    text = json.dumps(result, default=str, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _dispatch_method(method: str, params: Dict[str, Any],
                     ctx: Ctx) -> Dict[str, Any]:
    if method == "initialize":
        return {"protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "nr2grafana",
                               "version": _version()}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        if not isinstance(params, dict):
            raise ToolError("tools/call params must be an object",
                            _ERR_INVALID_PARAMS)
        name = params.get("name")
        if not name:
            raise ToolError("tools/call needs a tool 'name'",
                            _ERR_INVALID_PARAMS)
        return handle_call(name, params.get("arguments") or {}, ctx)
    raise ToolError("method not found: %s" % method,
                    _ERR_METHOD_NOT_FOUND)


def _error_obj(rid: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def _write_message(out, obj: Dict[str, Any]) -> None:
    data = (json.dumps(obj, default=str, ensure_ascii=False)
            + "\n").encode("utf-8")
    out.write(data)
    out.flush()


def _default_log(message: str) -> None:
    try:
        sys.stderr.write("[nr2grafana-mcp] %s\n" % message)
        sys.stderr.flush()
    except Exception:
        pass


def serve_stdio(store: Any = None,
                log: Optional[Callable[[str], None]] = None) -> None:
    """Run the stdio MCP server loop until stdin closes (blocking).

    Reads newline-delimited JSON-RPC 2.0 requests from stdin and writes
    responses to stdout; all logging goes to stderr / ``log`` so stdout
    stays a clean protocol channel. ``store`` defaults to a Store at
    ``$N2G_DB`` (or the user default). Never raises to the caller: a
    per-message failure becomes a JSON-RPC error object.
    """
    if log is None:
        log = _default_log
    close_store = False
    if store is None:
        store_mod = importlib.import_module("nr2grafana.store")
        store = store_mod.Store(os.environ.get("N2G_DB", ""))
        close_store = True
    # Hydrate non-secret prefs from the store into the web session so
    # tools that reach Grafana / New Relic use the saved URLs / region.
    try:
        _web()._load_prefs(store)
    except Exception:
        pass
    ctx = Ctx(store, log)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    log("nr2grafana MCP server ready (%d tools)" % len(TOOLS))
    try:
        while True:
            raw = stdin.readline()
            if not raw:
                break  # stdin closed -> shut down
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue  # tolerate stray non-JSON lines
            if not isinstance(msg, dict):
                continue
            method = msg.get("method")
            rid = msg.get("id")
            is_request = "id" in msg
            # Notifications (no id), e.g. notifications/initialized, get
            # no reply.
            if not is_request:
                continue
            if not isinstance(method, str) or not method:
                _write_message(stdout, _error_obj(
                    rid, _ERR_INVALID_PARAMS, "missing method"))
                continue
            params = msg.get("params") or {}
            try:
                result = _dispatch_method(method, params, ctx)
            except ToolError as exc:
                _write_message(stdout, _error_obj(
                    rid, exc.code, str(exc)))
                continue
            except Exception as exc:  # defensive: never crash the loop
                _write_message(stdout, _error_obj(
                    rid, _ERR_INTERNAL,
                    "internal error: %s" % _errmsg(exc)))
                continue
            _write_message(stdout, {"jsonrpc": "2.0", "id": rid,
                                    "result": result})
    finally:
        if close_store:
            try:
                store.close()
            except Exception:
                pass
        log("nr2grafana MCP server stopped")
