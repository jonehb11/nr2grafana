"""HTTP server behind the nr2grafana web UI.

Design notes:

* Secrets (New Relic / Grafana / Anthropic keys) live ONLY in the
  module-level :class:`Session` object -- process memory. They are never
  written to the Store, to disk, or to logs. Non-secret preferences
  (urls, directories, region, model, local AI agent command) are
  mirrored into Store settings so they survive restarts.
* Sibling 1.1 modules (store, requirements, artifacts, grafana.live,
  changelog, ai) are imported lazily inside handlers so this module
  imports cleanly even mid-build; the contract guarantees their APIs.
* Long operations (nr list/fetch, convert, grafana test/import) run in
  background threads tracked in an in-memory jobs dict; POST returns
  ``{"job": id}`` and the UI polls ``GET /api/jobs/<id>``.
* Every handler error becomes a JSON body ``{"error": msg}`` -- a
  traceback must never kill a request without a JSON response.
"""

from __future__ import annotations

import copy
import importlib
import io
import json
import os
import re
import threading
import time
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

SECRET_KEYS = ("nr_api_key", "grafana_token", "anthropic_api_key")
PREF_KEYS = ("nr_region", "grafana_url", "ai_model", "ai_command",
             "input_dir", "out_dir", "config_path")
_MAX_JOBS = 50

# Metric-name autocomplete cache: ds uid -> (fetched_at, [names]).
_METRICS_TTL = 60.0
_METRICS_CACHE: Dict[str, Tuple[float, List[str]]] = {}
_METRICS_LOCK = threading.Lock()

# Slugs come from slugify(); anything else is not a store key and must
# never reach the filesystem (download routes).
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Artifact slug used for instance-wide cost data (traffic sampling and
# whole-instance cost/optimize runs are not tied to one dashboard). It
# deliberately fails _SLUG_RE so it can never be requested through the
# filesystem download routes.
_INSTANCE_SLUG = "__instance__"

# Grafana datasource plugin type -> LGTM family used for traffic
# sampling. Anything not in here (cloudwatch, graphite, ...) is not a
# cost-sampleable LGTM component and is skipped.
_DS_FAMILY = {"prometheus": "prometheus", "loki": "loki",
              "tempo": "tempo"}

# optimize.recommend config target -> file name in cost-config.zip.
_COST_CONFIG_FILES = {
    "promtail": "promtail.yaml",
    "alloy": "alloy.river",
    "otel-collector": "otel-collector.yaml",
    "loki-limits": "loki-limits.yaml",
    "prometheus-relabel": "prometheus-relabel.yaml",
    "mimir-limits": "mimir-limits.yaml",
}


def _lazy(name):
    """Import a sibling module at call time. importlib honors
    sys.modules, which keeps handlers testable and lets the server
    start even while sibling modules are still being built."""
    return importlib.import_module("nr2grafana." + name)


class ApiError(Exception):
    """Handler error carrying an HTTP status code."""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


class Session:
    """In-process session state. Secrets never leave this object."""

    def __init__(self) -> None:
        self.nr_api_key = os.environ.get("NEW_RELIC_API_KEY", "")
        self.nr_region = "US"
        self.grafana_url = os.environ.get("GRAFANA_URL", "")
        self.grafana_token = os.environ.get("GRAFANA_TOKEN", "")
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.ai_model = ""
        # Local console AI agent command (e.g. "claude -p {prompt}").
        # NOT a secret: it persists via Store settings like other
        # prefs. The Anthropic key wins over it when both are set.
        self.ai_command = ""
        self.input_dir = "./newrelic-dashboards"
        self.out_dir = "./grafana-dashboards"
        self.config_path = ""
        # Connection pills: unset | ok | error (per service).
        self.status = {"newrelic": "unset", "grafana": "unset",
                       "ai": "unset"}
        self.status_detail = {"newrelic": "", "grafana": "", "ai": ""}

    def ai_backend(self) -> str:
        """Which AI backend is active: "api" | "local" | "none"."""
        if self.anthropic_api_key:
            return "api"
        if self.ai_command:
            return "local"
        return "none"

    def public(self) -> Dict[str, Any]:
        """State safe to send to the browser -- no secret values."""
        return {
            "nr_key_set": bool(self.nr_api_key),
            "nr_region": self.nr_region,
            "grafana_url": self.grafana_url,
            "grafana_token_set": bool(self.grafana_token),
            "anthropic_key_set": bool(self.anthropic_api_key),
            "ai_model": self.ai_model,
            "ai_command": self.ai_command,  # non-secret by design
            "ai_command_set": bool(self.ai_command),
            "ai_backend": self.ai_backend(),
            "input_dir": self.input_dir,
            "out_dir": self.out_dir,
            "config_path": self.config_path,
        }


SESSION = Session()

_JOBS: Dict[str, "_Job"] = {}
_JOBS_LOCK = threading.Lock()


class _Job:
    def __init__(self, jid: str, kind: str):
        self.id = jid
        self.kind = kind
        self.status = "running"  # running | done | error
        self.error = ""
        self.result: Any = None
        self._log: List[str] = []
        self._lock = threading.Lock()

    def add(self, msg: Any) -> None:
        with self._lock:
            self._log.append(str(msg))

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {"id": self.id, "kind": self.kind,
                    "status": self.status, "log": list(self._log),
                    "result": self.result, "error": self.error}


def _start_job(kind: str, fn: Callable[["_Job"], Any]) -> str:
    jid = uuid.uuid4().hex[:12]
    job = _Job(jid, kind)
    with _JOBS_LOCK:
        _JOBS[jid] = job
        if len(_JOBS) > _MAX_JOBS:
            done = [j for j in _JOBS.values() if j.status != "running"]
            for old in done[:len(_JOBS) - _MAX_JOBS]:
                _JOBS.pop(old.id, None)

    def run() -> None:
        try:
            job.result = fn(job)
            job.status = "done"
        except Exception as e:  # job errors surface via polling, not 500s
            job.error = _errmsg(e)
            job.add("ERROR: " + job.error)
            job.status = "error"

    threading.Thread(target=run, daemon=True,
                     name="nr2grafana-job-" + kind).start()
    return jid


def _errmsg(e: Exception) -> str:
    name = type(e).__name__
    if name in ("ApiError", "GrafanaError", "NerdGraphError", "AIError",
                "ValueError", "RuntimeError", "FileNotFoundError"):
        return str(e)
    return "%s: %s" % (name, e)


# ---------------------------------------------------------------------------
# helpers shared by handlers
# ---------------------------------------------------------------------------

class _BoundAWS:
    """Thin read-only wrapper over the awscost module that pre-binds a
    profile and/or region into any function whose signature accepts them
    (get_cost_and_usage, run_aws, caller_identity, ...). Exception
    classes and non-function attributes pass straight through, so
    ``aws.AWSError`` still works. This is the "client" half of tco's
    ``aws_mod_or_client`` argument; the module itself is passed when no
    profile/region override is set."""

    def __init__(self, mod, profile="", region=""):
        self._mod = mod
        self._profile = profile
        self._region = region

    def __getattr__(self, name):
        import inspect
        attr = getattr(self._mod, name)
        if not inspect.isroutine(attr):
            return attr  # classes (AWSError), constants, etc.
        try:
            params = inspect.signature(attr).parameters
        except (TypeError, ValueError):
            return attr

        def wrapper(*args, **kwargs):
            if self._profile and "profile" in params \
                    and "profile" not in kwargs:
                kwargs["profile"] = self._profile
            if self._region and "region" in params \
                    and "region" not in kwargs:
                kwargs["region"] = self._region
            return attr(*args, **kwargs)

        return wrapper


def _aws_client(profile="", region=""):
    """The awscost module, or a profile/region-bound wrapper over it."""
    aws = _lazy("awscost")
    if profile or region:
        return _BoundAWS(aws, profile, region)
    return aws


def _awscost():
    """The awscost module when the aws CLI is present and configured,
    else an actionable ApiError. AWS access is OPTIONAL and strictly
    read-only -- the tool degrades cleanly when it is absent."""
    try:
        aws = _lazy("awscost")
    except Exception:
        raise ApiError("AWS cost analysis is unavailable "
                       "(nr2grafana.awscost not importable)", 400)
    try:
        ok = bool(aws.aws_available())
    except Exception:
        ok = False
    if not ok:
        raise ApiError("AWS CLI not found / not configured -- install "
                       "the aws CLI and configure read-only credentials "
                       "(aws configure / SSO) to analyze TCO", 400)
    return aws


def _optional_awscost(profile="", region=""):
    """The awscost module (or a profile/region-bound wrapper) when the
    aws CLI is present and read-only access is configured, else None. RCA
    is designed to DEGRADE cleanly when AWS is absent -- a pasted anomaly
    report still yields a (lower-confidence) analysis without any live
    AWS calls, so callers must tolerate a None here rather than error."""
    try:
        aws = _lazy("awscost")
    except Exception:
        return None
    try:
        if not aws.aws_available():
            return None
    except Exception:
        return None
    if profile or region:
        return _BoundAWS(aws, profile, region)
    return aws


def _rca_mod():
    """The rca engine, or an actionable ApiError when it is not yet
    importable (the module is developed alongside this one)."""
    try:
        return _lazy("rca")
    except Exception:
        raise ApiError("cost-anomaly RCA is unavailable "
                       "(nr2grafana.rca not importable)", 400)


def _mitigate_mod():
    """The mitigation planner, or an actionable ApiError."""
    try:
        return _lazy("mitigate")
    except Exception:
        raise ApiError("mitigation planning is unavailable "
                       "(nr2grafana.mitigate not importable)", 400)


def _flowlogs_mod():
    """The flow-logs attribution module, or None. RCA runs (degraded)
    without it, so its absence is never fatal."""
    try:
        return _lazy("flowlogs")
    except Exception:
        return None


def _call_filtered(fn, *args, **kwargs):
    """Call ``fn`` passing ``args`` positionally and only the ``kwargs``
    its signature actually accepts, so these routes stay robust against
    sibling API drift (RCA/flowlogs/mitigate evolve concurrently). A
    ``**kwargs`` function receives everything unchanged."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in params}
    return fn(*args, **filtered)


def _default_anomaly_window(days=90):
    """(start, end) ISO dates spanning the last ``days`` days -- the
    default Cost Explorer date interval for anomaly discovery."""
    import datetime as _dt
    end = _dt.date.today()
    start = end - _dt.timedelta(days=max(1, int(days)))
    return start.isoformat(), end.isoformat()


def _anomaly_window(body):
    """The caller's {start,end} date interval, else the last 90 days."""
    start = str((body or {}).get("start") or "")
    end = str((body or {}).get("end") or "")
    if start and end:
        return start, end
    return _default_anomaly_window()


def _rca_report_input(body):
    """The pasted anomaly report from a request body, accepting a few
    friendly key aliases (report / anomaly / text). Returns the raw
    value (str or dict) or None when none is present/non-empty."""
    for key in ("report", "anomaly", "text"):
        val = (body or {}).get(key)
        if isinstance(val, dict) and val:
            return val
        if isinstance(val, str) and val.strip():
            return val
    return None


# Generated mitigation-config filenames must be safe basenames -- a
# planner-supplied name can never escape the download zip.
_CONFIG_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_config_filename(name, default):
    base = os.path.basename(str(name or "").strip())
    base = _CONFIG_NAME_RE.sub("-", base).strip("-.")
    if not base:
        return default
    if "." not in base:
        base += ".yaml"
    return base


def _config_text(cfg):
    """The paste-ready config body out of a mitigation config entry,
    tolerating the several key names the planner might use."""
    for k in ("content", "snippet", "yaml", "text", "config", "body"):
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _iter_mitigation_configs(mit):
    """Normalize one mitigation's generated configs to a list of dicts,
    tolerating a list of config dicts, a {filename: text} mapping, a
    single config dict, or a bare string."""
    raw = mit.get("configs")
    if raw is None:
        raw = mit.get("config")
    items: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, dict):
                items.append(c)
            elif isinstance(c, str) and c.strip():
                items.append({"content": c})
    elif isinstance(raw, dict):
        text_keys = ("content", "snippet", "yaml", "text", "config",
                     "body", "filename", "name", "target")
        if any(k in raw for k in text_keys):
            items.append(raw)
        else:  # a {filename: text|dict} mapping
            for k, v in raw.items():
                if isinstance(v, str):
                    items.append({"filename": k, "content": v})
                elif isinstance(v, dict):
                    d = dict(v)
                    d.setdefault("filename", k)
                    items.append(d)
    elif isinstance(raw, str) and raw.strip():
        items.append({"content": raw})
    return items


_MIT_KEEP_FLAGS = ("keeps_availability", "keeps_durability",
                   "keeps_performance", "handles_current_traffic")


def _mitigation_config_files(mitigation):
    """Group every mitigation's generated config into paste-ready
    (filename, text) pairs plus a README index. All configs are GENERIC
    (placeholder) values the planner emitted; this only files them."""
    buckets: Dict[str, List[str]] = {}
    readme = ["# nr2grafana reliability-safe mitigation configs",
              "",
              "Generated, paste-ready configs to cut AWS cost WITHOUT",
              "reducing availability, durability, performance or the",
              "ability to serve current traffic. Values are GENERIC",
              "placeholders (<ANGLE_BRACKETS>) -- fill them from your own",
              "GitOps/IaC. The tool PROPOSES; it never applies changes.",
              ""]
    mits = (mitigation or {}).get("mitigations") or []
    for i, mit in enumerate(mits, 1):
        if not isinstance(mit, dict):
            continue
        title = mit.get("title") or mit.get("id") or ("mitigation-%d" % i)
        keeps = [k.replace("keeps_", "").replace("handles_", "")
                 for k in _MIT_KEEP_FLAGS if mit.get(k)]
        line = "- %s" % title
        if keeps:
            line += "  (keeps: %s)" % ", ".join(keeps)
        readme.append(line)
        for cfg in _iter_mitigation_configs(mit):
            text = _config_text(cfg)
            if not text.strip():
                continue
            fname = _safe_config_filename(
                cfg.get("filename") or cfg.get("name")
                or cfg.get("target"), "mitigation-%d.yaml" % i)
            note = cfg.get("note") or cfg.get("description") or ""
            header = "# --- %s ---" % title
            if note:
                header += "\n# %s" % note
            buckets.setdefault(fname, []).append(
                header + "\n" + text.rstrip() + "\n")
    files = [(fname, "\n".join(parts))
             for fname, parts in sorted(buckets.items())]
    files.append(("README.md", "\n".join(readme) + "\n"))
    return files


def _call_tco_analyze(tco, aws, **kwargs):
    """Call tco.analyze passing only the kwargs its signature accepts,
    so the route stays robust against sibling API drift (e.g. group_by
    is a route/CLI concept the engine may or may not take)."""
    import inspect
    try:
        params = inspect.signature(tco.analyze).parameters
    except (TypeError, ValueError):
        params = {}
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return tco.analyze(aws, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in params}
    return tco.analyze(aws, **filtered)


def _parse_buckets(raw):
    """S3 bucket list from a JSON array or a comma-separated string."""
    if isinstance(raw, list):
        names = [str(b).strip() for b in raw]
    elif isinstance(raw, str):
        names = [b.strip() for b in raw.split(",")]
    else:
        return None
    names = [n for n in names if n]
    return names or None


def _grafana_live():
    """Build a GrafanaLive client from the session or fail actionably."""
    if not SESSION.grafana_url:
        raise ApiError("Grafana URL is not configured -- set it in "
                       "Setup first", 400)
    live_mod = _lazy("grafana.live")
    return live_mod.GrafanaLive(SESSION.grafana_url,
                                token=SESSION.grafana_token)


def _nerdgraph():
    if not SESSION.nr_api_key:
        raise ApiError("New Relic API key is not configured -- set it "
                       "in Setup first", 400)
    from ..nerdgraph import NerdGraphClient
    return NerdGraphClient(SESSION.nr_api_key, region=SESSION.nr_region)


def _ai():
    """Resolve the AI backend: Anthropic API key wins, then a local
    console agent command, else an actionable 400."""
    ai_mod = _lazy("ai")
    get = getattr(ai_mod, "get_assistant", None)
    if get is not None:
        ai = get(api_key=SESSION.anthropic_api_key,
                 model=SESSION.ai_model,
                 command=SESSION.ai_command)
    else:  # older ai module mid-build: API-only fallback
        ai = ai_mod.AIAssist(SESSION.anthropic_api_key,
                             SESSION.ai_model)
    if ai is None or not ai.available:
        raise ApiError("no AI backend configured -- add an Anthropic "
                       "API key or a local console agent command in "
                       "Setup to use AI assistance", 400)
    return ai


def _dash_from_row(slug: str, row: Optional[Dict[str, Any]]) \
        -> Dict[str, Any]:
    """Extract the Grafana dashboard JSON from a Store row, tolerating
    either the dashboard stored directly as ``data`` or wrapped."""
    if not row:
        raise ApiError("no stored dashboard with slug %r -- run "
                       "Convert first" % slug, 404)
    data = row.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("dashboard"), dict):
            return data["dashboard"]
        if "panels" in data:
            return data
    if "panels" in row:
        return {k: v for k, v in row.items()}
    raise ApiError("stored record for %r contains no dashboard JSON"
                   % slug, 500)


def _iter_panels(dash: Dict[str, Any]):
    def walk(panels):
        for p in panels:
            yield p
            if p.get("type") == "row":
                for q in walk(p.get("panels") or []):
                    yield q
    return walk(dash.get("panels") or [])


def _find_panel(dash: Dict[str, Any], panel_id: Any) -> Dict[str, Any]:
    for p in _iter_panels(dash):
        if p.get("id") == panel_id:
            return p
    raise ApiError("panel id %r not found in dashboard" % panel_id, 404)


# Keys that may carry a target's query text: prometheus/loki targets
# use "expr", tempo (TraceQL) uses "query", the New Relic passthrough
# plugin uses "queryText".
_QUERY_KEYS = ("expr", "query", "queryText")


def _target_query_key(target: Dict[str, Any]) -> str:
    """Key holding this target's query text. Prefers the family key for
    the target's datasource type, falling back to whichever known key
    currently carries a non-empty string, then to "expr"."""
    ds_type = ((target.get("datasource") or {}).get("type") or "").lower()
    if "tempo" in ds_type:
        key = "query"
    elif "newrelic" in ds_type:
        key = "queryText"
    else:
        key = "expr"
    if isinstance(target.get(key), str):
        return key
    for k in _QUERY_KEYS:
        if isinstance(target.get(k), str) and target[k].strip():
            return k
    return key


def _find_target(panel: Dict[str, Any], ref_id: str) -> Dict[str, Any]:
    targets = panel.get("targets") or []
    if not targets:
        raise ApiError("panel %r has no query targets"
                       % panel.get("title"), 400)
    if ref_id:
        for t in targets:
            if t.get("refId") == ref_id:
                return t
        raise ApiError("no target with refId %r on panel %r"
                       % (ref_id, panel.get("title")), 404)
    return targets[0]


def _package_dir(store, slug: str) -> str:
    try:
        p = store.get_setting("package_dir." + slug, "")
    except Exception:
        p = ""
    if p and os.path.isdir(p):
        return p
    guess = os.path.join(SESSION.out_dir, slug)
    if os.path.isfile(os.path.join(guess, "dashboard.json")):
        return guess
    return ""


def _write_package_dashboard(store, slug: str,
                             dash: Dict[str, Any]) -> str:
    pkg = _package_dir(store, slug)
    if not pkg:
        return ""
    path = os.path.join(pkg, "dashboard.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dash, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def _family_ds_ref(cfg: Dict[str, Any], family: str) -> Dict[str, str]:
    """Datasource reference for one LGTM family from the config -- the
    same {type, uid} shape grafana.builder._Build.ds_ref emits, so a
    converted panel points at exactly what the auto-converter would."""
    ds = (cfg.get("datasources") or {}).get(family, {})
    return {"type": ds.get("type", family), "uid": ds.get("uid", "")}


def _make_convert_target(ds_family: str, ref_id: str, expr: str,
                         ds_ref: Dict[str, str]) -> Dict[str, Any]:
    """A real Grafana query target for ``ds_family`` carrying ``expr``.
    The query text lives under the key Grafana reads for that family:
    "query" (TraceQL) for tempo, "expr" for prometheus/loki."""
    tgt: Dict[str, Any] = {"refId": ref_id, "datasource": dict(ds_ref)}
    if ds_family == "tempo":
        tgt.update({"query": expr, "queryType": "traceql",
                    "tableType": "traces", "filters": [], "limit": 20})
    elif ds_family == "loki":
        tgt.update({"expr": expr, "queryType": "range",
                    "legendFormat": "", "editorMode": "code"})
    else:
        tgt.update({"expr": expr, "legendFormat": "__auto",
                    "editorMode": "code", "range": True,
                    "instant": False, "format": "time_series"})
    return tgt


_FAMILY_DEFAULT_VIZ = {"prometheus": "timeseries", "loki": "logs",
                       "tempo": "table"}


def _placeholder_viz(store, slug: str, panel: Dict[str, Any],
                     ds_family: str) -> str:
    """The Grafana panel type a converted text-placeholder should
    become: the original NR visualization when recognized, else a
    sensible per-family default. "" when the panel is not a text
    placeholder (its existing type is kept)."""
    if panel.get("type") != "text":
        return ""
    from ..grafana.builder import _panel_type_for
    viz = ""
    wr = (_artifact(store, slug, "widget-report") or {}).get(
        "widgets", [])
    for w in wr:
        if w.get("panel_id") == panel.get("id"):
            viz = _panel_type_for(w.get("visualization") or "")
            break
    return viz or _FAMILY_DEFAULT_VIZ.get(ds_family, "timeseries")


def _apply_placeholder_viz(panel: Dict[str, Any], ptype: str) -> None:
    """Turn a text placeholder into a real ``ptype`` panel: proper
    options/fieldConfig, no leftover markdown content."""
    from ..grafana.builder import (_base_thresholds, _legend,
                                    _timeseries_custom)
    panel["type"] = ptype
    panel["transparent"] = False
    fc: Dict[str, Any] = {"defaults": {}, "overrides": []}
    if ptype == "timeseries":
        fc["defaults"] = {"color": {"mode": "palette-classic"},
                          "thresholds": _base_thresholds(),
                          "mappings": [], "custom": _timeseries_custom()}
        panel["options"] = {"legend": _legend(),
                            "tooltip": {"mode": "multi", "sort": "desc"}}
    elif ptype == "logs":
        panel["options"] = {"showTime": True, "showLabels": False,
                            "showCommonLabels": False,
                            "wrapLogMessage": True,
                            "prettifyLogMessage": False,
                            "enableLogDetails": True,
                            "dedupStrategy": "none",
                            "sortOrder": "Descending"}
    elif ptype == "table":
        panel["options"] = {"showHeader": True, "cellHeight": "sm",
                            "footer": {"show": False,
                                       "reducer": ["sum"],
                                       "countRows": False,
                                       "fields": ""},
                            "sortBy": []}
    else:
        panel["options"] = {}
    panel["fieldConfig"] = fc


def _write_package_datatest(store, slug: str,
                            dash: Dict[str, Any]) -> str:
    """Regenerate <package>/datatest.json from the (edited) dashboard so
    the bundled smoke test stays in step with a converted panel."""
    pkg = _package_dir(store, slug)
    if not pkg:
        return ""
    artifacts = _lazy("artifacts")
    build = getattr(artifacts, "build_datatest", None)
    if build is None:  # sibling mid-build: skip, never break the edit
        return ""
    wr = (_artifact(store, slug, "widget-report") or {}).get(
        "widgets", [])
    path = os.path.join(pkg, "datatest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build(dash, wr), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def _merge_datatest(store, slug: str,
                    results: List[Dict[str, Any]]) -> None:
    """Fold single-target test ``results`` into the stored datatest
    artifact (replace matching panel_id/refId rows, else append)."""
    try:
        dt = store.get_artifact(slug, "datatest") or {}
        merged = dt.get("results", [])
        for r in results:
            for i, old in enumerate(merged):
                if (old.get("panel_id") == r.get("panel_id")
                        and old.get("refId") == r.get("refId")):
                    merged[i] = r
                    break
            else:
                merged.append(r)
        counts: Dict[str, int] = {}
        for r in merged:
            s = r.get("status", "?")
            counts[s] = counts.get(s, 0) + 1
        store.save_artifact(slug, "datatest",
                            {"results": merged, "summary": counts})
    except Exception:
        pass


def _widget_nrql(w: Dict[str, Any]) -> str:
    """The widget's raw NRQL as one string (the report stores a list)."""
    raw = w.get("nrql")
    if isinstance(raw, list):
        return "; ".join(str(q) for q in raw if q)
    return str(raw) if raw else ""


def _widget_family(w: Dict[str, Any], panel: Dict[str, Any]) -> str:
    """Best LGTM family for a flagged widget: the converter's own
    target datasource when one exists, else the panel's target
    datasource type, else prometheus (metrics are the common case)."""
    for qq in w.get("queries") or []:
        fam = qq.get("datasource")
        if fam in ("prometheus", "loki", "tempo"):
            return fam
        if fam == "newrelic":
            return "prometheus"
    for t in panel.get("targets") or []:
        typ = ((t.get("datasource") or {}).get("type") or "").lower()
        if "loki" in typ:
            return "loki"
        if "tempo" in typ:
            return "tempo"
        if "prometheus" in typ:
            return "prometheus"
    return "prometheus"


def _first_ref_id(panel: Dict[str, Any]) -> str:
    for t in panel.get("targets") or []:
        if t.get("refId"):
            return t["refId"]
    return "A"


def _first_expr(panel: Dict[str, Any]) -> str:
    for t in panel.get("targets") or []:
        for k in _QUERY_KEYS:
            v = t.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def _flagged_convert_context(store, slug: str, w: Dict[str, Any],
                             panel: Dict[str, Any],
                             instance: Optional[List[Dict[str, Any]]]) \
        -> Tuple[Dict[str, Any], str, str]:
    """Build the conversion-mode AI context (SEAM-1 keys) for one
    flagged widget-report row. Returns (context, ds_family, ref_id)."""
    ds_family = _widget_family(w, panel)
    ref_id = _first_ref_id(panel)
    ctx: Dict[str, Any] = {"mode": "convert", "ds_family": ds_family}
    notes = w.get("notes")
    if isinstance(notes, list) and notes:
        ctx["translation_notes"] = notes
    raw = _widget_nrql(w)
    if raw:
        ctx["original_nrql"] = raw
    if w.get("confidence"):
        ctx["confidence"] = w["confidence"]
    title = w.get("widget") or w.get("widget_title")
    if title:
        ctx["panel"] = title
    expr = _first_expr(panel)
    if expr:
        ctx["expr"] = expr
    if instance:
        ctx["instance"] = {"datasources": instance}
    return ctx, ds_family, ref_id


def _normalize_nr_json(nr_json: Any) -> List[Dict[str, Any]]:
    """Validate a pasted ``nr_json`` (a NR dashboard object OR a list of
    them) and return the list of dashboard dicts, raising a clear 400
    when anything is not a New Relic dashboard."""
    from ..model import parse_nr_dashboard
    is_list = isinstance(nr_json, list)
    if isinstance(nr_json, dict):
        items = [nr_json]
    elif is_list:
        items = nr_json
    else:
        raise ApiError("nr_json must be a New Relic dashboard object or "
                       "a list of dashboards", 400)
    if not items:
        raise ApiError("nr_json is empty -- paste at least one New "
                       "Relic dashboard", 400)
    out: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        where = "[%d]" % i if is_list else ""
        if not isinstance(item, dict):
            raise ApiError("nr_json%s is not a dashboard object" % where,
                           400)
        try:
            parse_nr_dashboard(item)
        except ValueError as e:
            raise ApiError("nr_json%s is not a valid New Relic "
                           "dashboard: %s" % (where, e), 400)
        out.append(item)
    return out


def _collect_json_files(input_dir: str) -> List[str]:
    if not os.path.isdir(input_dir):
        raise ApiError("input directory not found: %s" % input_dir, 400)
    files = []
    for name in sorted(os.listdir(input_dir)):
        if name.endswith(".json") and name != "migration-report.json":
            files.append(os.path.join(input_dir, name))
    if not files:
        raise ApiError("no .json dashboard files in %s -- fetch from "
                       "New Relic first" % input_dir, 400)
    return files


def _persist_dashboard(store, slug: str, title: str, source: str,
                       nr_guid: str, dash: Dict[str, Any],
                       report: List[Dict[str, Any]],
                       reqs: Dict[str, Any], pkg_dir: str,
                       nr_raw: Optional[Dict[str, Any]] = None) -> None:
    store.upsert_dashboard(slug, title, source, nr_guid, dash)
    store.save_artifact(slug, "widget-report", {"widgets": report})
    store.save_artifact(slug, "requirements", reqs)
    if isinstance(nr_raw, dict) and nr_raw:
        # The original New Relic dashboard json, kept so Compare can
        # render the NR side offline (best-effort: never break convert).
        try:
            store.save_artifact(slug, "nr-source", nr_raw)
        except Exception:
            pass
    if pkg_dir:
        try:
            store.set_setting("package_dir." + slug, pkg_dir)
        except Exception:
            # e.g. a slug that trips the store's secret-key guard --
            # the package dir is then re-guessed from out_dir instead.
            pass


def _confidence_counts(report: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for w in report or []:
        c = w.get("confidence", "unknown")
        counts[c] = counts.get(c, 0) + 1
    return counts


def _single_target_test(live, dash: Dict[str, Any],
                        panel: Dict[str, Any], target: Dict[str, Any],
                        expr: str) -> List[Dict[str, Any]]:
    """Run test_dashboard against a one-panel copy carrying ``expr``."""
    p = copy.deepcopy(panel)
    p.pop("panels", None)
    t = copy.deepcopy(target)
    t[_target_query_key(t)] = expr
    p["targets"] = [t]
    mini = {"title": dash.get("title", ""), "uid": dash.get("uid", ""),
            "templating": copy.deepcopy(dash.get("templating") or {}),
            "panels": [p]}
    return live.test_dashboard(mini)


def _load_cfg() -> Dict[str, Any]:
    """Session config (or defaults) for parity/diagnose/heal calls."""
    from ..config import load_config
    try:
        return load_config(SESSION.config_path)
    except Exception:
        return {}


def _account_ids(body: Dict[str, Any],
                 widget_report: List[Dict[str, Any]]) -> List[int]:
    """Fallback NR account ids for parity: request body first, then
    any ids recorded in the widget report."""
    ids: List[int] = []
    for v in body.get("account_ids") or []:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            pass
    if ids:
        return ids
    seen = set()
    for w in widget_report or []:
        for v in (w.get("account_ids") or w.get("accountIds") or []):
            try:
                seen.add(int(v))
            except (TypeError, ValueError):
                pass
    return sorted(seen)


def _cached_metric_names(live, uid: str) -> List[str]:
    """prom_metric_names(uid) with a 60s in-memory cache per ds uid."""
    now = time.time()
    with _METRICS_LOCK:
        entry = _METRICS_CACHE.get(uid)
        if entry and now - entry[0] < _METRICS_TTL:
            return entry[1]
    names = list(live.prom_metric_names(uid) or [])
    with _METRICS_LOCK:
        _METRICS_CACHE[uid] = (now, names)
    return names


def _artifact(store, slug: str, kind: str) -> Optional[Dict[str, Any]]:
    try:
        return store.get_artifact(slug, kind)
    except Exception:
        return None


def _reupsert_dashboard(store, slug: str, row: Dict[str, Any],
                        dash: Dict[str, Any]) -> None:
    """Persist an in-place-edited dashboard back to the Store."""
    try:
        store.upsert_dashboard(slug,
                               row.get("title", dash.get("title", slug)),
                               row.get("source", ""),
                               row.get("nr_guid", ""), dash)
    except Exception:
        pass


def _comparison_inputs(store, slug: str):
    """Resolve the inputs compare.build_comparison needs for ``slug``:
    the converted dashboard, its widget report, the stored New Relic
    source json (or None -> NR side is best-effort), a GrafanaLive
    client, and a NerdGraphClient when a key is set (else None).
    Raises ApiError(404) when the slug is unknown."""
    dash = _dash_from_row(slug, store.get_dashboard(slug))
    wr = (_artifact(store, slug, "widget-report") or {}).get(
        "widgets", [])
    nr_src = _artifact(store, slug, "nr-source")
    nr_raw = nr_src if isinstance(nr_src, dict) else None
    live = _grafana_live()
    nr = None
    if SESSION.nr_api_key:
        try:
            nr = _nerdgraph()
        except ApiError:
            nr = None
    return dash, wr, nr_raw, live, nr


# ---------------------------------------------------------------------------
# cost / efficiency helpers (section 6)
# ---------------------------------------------------------------------------

def _traffic_ds_list(live, uids=None):
    """Build the traffic.sample_traffic ds_list from the instance's
    datasources: [{"family","uid","type"}] for every prometheus/loki/
    tempo datasource, optionally filtered to ``uids``."""
    want = set(uids) if uids else None
    out: List[Dict[str, Any]] = []
    for ds in live.datasources() or []:
        uid = ds.get("uid") or ""
        family = _DS_FAMILY.get((ds.get("type") or "").lower())
        if not uid or not family:
            continue
        if want is not None and uid not in want:
            continue
        out.append({"family": family, "uid": uid,
                    "type": ds.get("type") or family})
    return out


def _default_pricing() -> Dict[str, Any]:
    """costmodel.DEFAULT_PRICING, tolerating a not-yet-built sibling."""
    try:
        return dict(_lazy("costmodel").DEFAULT_PRICING)
    except Exception:
        return {}


def _effective_pricing(store, override=None) -> Dict[str, Any]:
    """Merge, in precedence order, DEFAULT_PRICING < persisted
    web.pricing < a per-request override. Pricing is plain numbers --
    never a secret -- so it lives in Store settings."""
    pricing = _default_pricing()
    try:
        saved = store.get_setting("web.pricing", None)
        if isinstance(saved, dict):
            pricing.update(saved)
    except Exception:
        pass
    if isinstance(override, dict):
        pricing.update(override)
    return pricing


def _cost_slug(body_slug):
    """Store slug for a cost artifact: the dashboard slug when given,
    else the instance-wide slug."""
    return body_slug if body_slug else _INSTANCE_SLUG


def _usage_inputs(store, slug=""):
    """Collect (dashboards, widget_reports) for usage.collect_usage:
    one stored dashboard when ``slug`` is given, else every stored
    dashboard. Missing/broken rows are skipped."""
    dashboards: List[Dict[str, Any]] = []
    widget_reports: List[Any] = []
    if slug:
        slugs = [slug]
    else:
        slugs = [r.get("slug", "") for r in store.list_dashboards()]
    for s in slugs:
        if not s:
            continue
        row = store.get_dashboard(s)
        try:
            dash = _dash_from_row(s, row)
        except ApiError:
            continue
        dashboards.append(dash)
        widget_reports.append(
            (_artifact(store, s, "widget-report") or {}).get(
                "widgets", []))
    return dashboards, widget_reports


def _cost_config_files(optimize) -> List[Tuple[str, str]]:
    """Group every recommendation's config snippet by its target into
    (filename, text) pairs, plus a README index. Returns paste-ready,
    commented files for cost-config.zip."""
    buckets: Dict[str, List[str]] = {}
    readme = ["# nr2grafana cost-optimization config",
              "",
              "Generated snippets to cut LGTM-stack cost. Each is safe:",
              "the tool never drops a metric, label, or stream that a",
              "migrated dashboard uses. Review before applying; apply at",
              "the collector/agent where possible to save before ingest.",
              ""]
    recs = (optimize or {}).get("recommendations") or []
    for rec in recs:
        title = rec.get("title") or rec.get("id") or "recommendation"
        safe = "safe" if rec.get("keeps_intact") else "REVIEW"
        readme.append("- [%s] %s (%s)"
                      % (rec.get("severity", "?"), title, safe))
        for cfg in rec.get("config") or []:
            target = cfg.get("target") or "other"
            fname = _COST_CONFIG_FILES.get(target, "%s.yaml" % target)
            snippet = cfg.get("snippet") or ""
            if not snippet.strip():
                continue
            note = cfg.get("note") or ""
            header = "# --- %s ---" % title
            if note:
                header += "\n# %s" % note
            buckets.setdefault(fname, []).append(
                header + "\n" + snippet.rstrip() + "\n")
    files = [(fname, "\n".join(parts))
             for fname, parts in sorted(buckets.items())]
    files.append(("README.md", "\n".join(readme) + "\n"))
    return files


# ---------------------------------------------------------------------------
# job bodies
# ---------------------------------------------------------------------------

def _job_nr_list(job: _Job) -> Dict[str, Any]:
    client = _nerdgraph()
    job.add("Listing dashboards from New Relic (%s)..."
            % SESSION.nr_region)
    try:
        dashboards = client.list_dashboards()
    except Exception as e:
        SESSION.status["newrelic"] = "error"
        SESSION.status_detail["newrelic"] = _errmsg(e)
        raise
    SESSION.status["newrelic"] = "ok"
    SESSION.status_detail["newrelic"] = ("%d dashboards visible"
                                         % len(dashboards))
    job.add("Found %d dashboards" % len(dashboards))
    return {"dashboards": dashboards, "count": len(dashboards)}


def _job_nr_fetch(job: _Job, body: Dict[str, Any]) -> Dict[str, Any]:
    from ..grafana.builder import slugify
    client = _nerdgraph()
    out = body.get("out") or SESSION.input_dir
    guids = body.get("guids") or []
    if guids:
        entities = [{"guid": g} for g in guids]
    else:
        job.add("Listing dashboards...")
        entities = client.list_dashboards()
        job.add("Found %d dashboards" % len(entities))
    os.makedirs(out, exist_ok=True)
    seen: Dict[str, int] = {}
    written: List[str] = []
    failed: List[Dict[str, str]] = []
    for i, ent in enumerate(entities, 1):
        guid = ent.get("guid", "")
        try:
            dash = client.get_dashboard(guid)
        except Exception as e:
            job.add("[%d/%d] %s FAILED: %s"
                    % (i, len(entities), guid, _errmsg(e)))
            failed.append({"guid": guid, "error": _errmsg(e)})
            continue
        slug = slugify(dash.get("name", "dashboard"), 60)
        seen[slug] = seen.get(slug, 0) + 1
        if seen[slug] > 1:
            slug = "%s-%d" % (slug, seen[slug])
        path = os.path.join(out, slug + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dash, f, indent=2, ensure_ascii=False)
            f.write("\n")
        written.append(path)
        job.add("[%d/%d] %s -> %s"
                % (i, len(entities), dash.get("name"), path))
    SESSION.status["newrelic"] = "error" if failed and not written \
        else "ok"
    job.add("Exported %d dashboards to %s" % (len(written), out))
    return {"written": written, "failed": failed, "out": out}


def _job_convert(job: _Job, body: Dict[str, Any], store,
                 pasted: Optional[List[Dict[str, Any]]] = None) \
        -> Dict[str, Any]:
    """Convert one or more NR dashboards. Reads .json files from an
    input directory by default; when ``pasted`` is given (the SEAM-2
    ``nr_json`` branch) it converts those in-memory dashboards instead
    and never touches the filesystem for input. Output packaging and
    persistence are identical either way."""
    artifacts = _lazy("artifacts")
    reqmod = _lazy("requirements")
    from ..config import load_config
    from ..grafana.builder import build_dashboards
    from ..model import parse_nr_dashboard

    input_dir = body.get("input_dir") or SESSION.input_dir
    out_dir = body.get("out_dir") or SESSION.out_dir
    config_path = body.get("config_path")
    if config_path is None:
        config_path = SESSION.config_path
    package = bool(body.get("package", True))

    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise ApiError("config: %s" % e, 400)

    # inputs: (source label, zero-arg loader returning the NR json).
    inputs: List[Tuple[str, Callable[[], Any]]] = []
    if pasted is not None:
        for i, obj in enumerate(pasted):
            inputs.append(("pasted[%d]" % i, (lambda o=obj: o)))
        run_meta: Dict[str, Any] = {"pasted": len(pasted),
                                    "out_dir": out_dir,
                                    "package": package}
        job.add("Converting %d pasted dashboard(s)" % len(inputs))
    else:
        for path in _collect_json_files(input_dir):
            def _load(p=path):
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            inputs.append((path, _load))
        run_meta = {"input_dir": input_dir, "out_dir": out_dir,
                    "package": package}
        job.add("Converting %d file(s) from %s"
                % (len(inputs), input_dir))
    os.makedirs(out_dir, exist_ok=True)

    run_id = None
    try:
        run_id = store.record_run("convert", run_meta)
    except Exception:
        pass

    entries: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    seen_slugs: Dict[str, int] = {}
    for source, loader in inputs:
        label = os.path.basename(source)
        try:
            data = loader()
            nr = parse_nr_dashboard(data)
            outputs = build_dashboards(nr, cfg)
        except Exception as e:  # one bad input must not kill the batch
            job.add("FAIL %s: %s" % (label, _errmsg(e)))
            failed.append({"source": source, "error": _errmsg(e)})
            continue
        for filename, dash, report in outputs:
            slug = filename[:-5] if filename.endswith(".json") \
                else filename
            seen_slugs[slug] = seen_slugs.get(slug, 0) + 1
            if seen_slugs[slug] > 1:
                slug = "%s-%d" % (slug, seen_slugs[slug])
            reqs = reqmod.analyze_dashboard(nr, dash, report, cfg)
            pkg_dir = ""
            if package:
                pkg_dir = artifacts.package_dashboard(
                    out_dir, slug, dash, report, reqs, cfg)
            else:
                flat = os.path.join(out_dir, slug + ".json")
                with open(flat, "w", encoding="utf-8") as f:
                    json.dump(dash, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            _persist_dashboard(store, slug, dash.get("title", slug),
                               source, getattr(nr, "guid", "") or "",
                               dash, report, reqs, pkg_dir,
                               nr_raw=data if isinstance(data, dict)
                               else None)
            counts = _confidence_counts(report)
            families = [d.get("family", "")
                        for d in reqs.get("datasources", [])]
            entry = {"slug": slug, "title": dash.get("title", slug),
                     "panels": len(report), "confidence": counts,
                     "datasources": families,
                     "domains": [d.get("domain", "")
                                 for d in reqs.get("domains", [])],
                     "package_dir": pkg_dir}
            entries.append(entry)
            results.append(entry)
            job.add("%s -> %s  (%s)"
                    % (label, pkg_dir or slug + ".json",
                       ", ".join("%d %s" % (v, k)
                                 for k, v in sorted(counts.items()))
                       or "no widgets"))
    if package and entries:
        try:
            idx = artifacts.write_index(out_dir, entries)
            job.add("Index: %s" % idx)
        except Exception as e:
            job.add("index generation failed: %s" % _errmsg(e))
    summary = {"dashboards": len(results), "failed": len(failed)}
    if run_id is not None:
        try:
            store.finish_run(run_id,
                             "error" if failed and not results
                             else "done", summary)
        except Exception:
            pass
    job.add("Done: %d dashboard(s), %d failed input(s)"
            % (len(results), len(failed)))
    return {"dashboards": results, "failed": failed,
            "out_dir": out_dir, "packaged": package}


def _job_grafana_test(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    slug = body.get("slug") or ""
    if not slug:
        raise ApiError("missing 'slug'", 400)
    live = _grafana_live()
    dash = _dash_from_row(slug, store.get_dashboard(slug))
    job.add("Testing %r against %s" % (slug, SESSION.grafana_url))
    try:
        results = live.test_dashboard(dash, log=job.add)
    except Exception as e:
        SESSION.status["grafana"] = "error"
        SESSION.status_detail["grafana"] = _errmsg(e)
        raise
    SESSION.status["grafana"] = "ok"
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.get("status", "?")] = counts.get(
            r.get("status", "?"), 0) + 1
    store.save_artifact(slug, "datatest", {"results": results,
                                           "summary": counts})
    pkg = _package_dir(store, slug)
    if pkg:
        try:
            with open(os.path.join(pkg, "datatest-results.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"results": results, "summary": counts}, f,
                          indent=2)
                f.write("\n")
        except OSError as e:
            job.add("could not write datatest-results.json: %s" % e)
    job.add("Tested %d target(s): %s"
            % (len(results),
               ", ".join("%d %s" % (v, k)
                         for k, v in sorted(counts.items())) or "none"))
    return {"slug": slug, "results": results, "summary": counts}


def _job_grafana_import(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    slugs = body.get("slugs") or ([body["slug"]]
                                  if body.get("slug") else [])
    if not slugs:
        raise ApiError("missing 'slug' or 'slugs'", 400)
    folder = body.get("folder") or ""
    overwrite = bool(body.get("overwrite"))
    live = _grafana_live()
    clog = _lazy("changelog").ChangeLog(store)
    folder_uid = ""
    if folder:
        folder_uid = live.find_or_create_folder(folder)
        job.add("Folder %r -> uid %s" % (folder, folder_uid))
    out: List[Dict[str, Any]] = []
    ok = 0
    for slug in slugs:
        try:
            dash = _dash_from_row(slug, store.get_dashboard(slug))
            res = live.import_dashboard(
                dash, folder_uid=folder_uid, overwrite=overwrite,
                message="Imported by nr2grafana web")
            url = res.get("url", "")
            # Grafana returns a root-relative url ("/d/<uid>/..."); make
            # it absolute so the UI's "Open in Grafana" link works when
            # the page is served from a different origin (localhost).
            if url.startswith("/") and SESSION.grafana_url:
                url = SESSION.grafana_url.rstrip("/") + url
            out.append({"slug": slug, "status": "ok", "url": url,
                        "uid": res.get("uid", "")})
            ok += 1
            job.add("ok    %s -> %s" % (slug, url or "imported"))
            try:
                clog.record(slug, "import", "grafana:%s"
                            % SESSION.grafana_url, "", url or "imported",
                            why="web import", source="user")
            except Exception:
                pass
        except Exception as e:
            msg = _errmsg(e)
            out.append({"slug": slug, "status": "error", "error": msg})
            job.add("FAIL  %s: %s" % (slug, msg))
    SESSION.status["grafana"] = "ok" if ok else "error"
    job.add("Imported %d/%d dashboard(s)" % (ok, len(slugs)))
    return {"results": out, "ok": ok, "total": len(slugs)}


def _job_parity(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    slug = body.get("slug") or ""
    if not slug:
        raise ApiError("missing 'slug'", 400)
    row = store.get_dashboard(slug)
    dash = _dash_from_row(slug, row)
    wr = (_artifact(store, slug, "widget-report") or {}).get(
        "widgets", [])
    live = _grafana_live()
    nr = _nerdgraph()
    parity_mod = _lazy("parity")
    frm = body.get("from") or "now-1h"
    to = body.get("to") or "now"
    aids = _account_ids(body, wr)
    if not aids:
        # Neither the request nor the widget report knows the NR
        # account -- fall back to every account the key can see so
        # the user never has to look ids up in New Relic.
        try:
            aids = nr.list_account_ids()
        except Exception as e:
            job.add("could not list NR accounts: %s" % _errmsg(e))
        if aids:
            job.add("No account id recorded; trying the key's %d "
                    "visible account(s): %s"
                    % (len(aids), ", ".join(map(str, aids))))
    job.add("Comparing NR vs Grafana data for %r (%s .. %s)"
            % (slug, frm, to))
    report = parity_mod.run_parity(
        nr, aids, live, dash, wr,
        ds_map=body.get("ds_map"), frm=frm, to=to, log=job.add)
    store.save_artifact(slug, "parity", report)
    SESSION.status["grafana"] = "ok"
    SESSION.status["newrelic"] = "ok"
    job.add("Parity score %s -- %s"
            % (report.get("score"),
               ", ".join("%d %s" % (v, k) for k, v in
                         sorted((report.get("summary") or {}).items()))
               or "no panels compared"))
    return report


def _job_compare(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    slug = body.get("slug") or ""
    if not slug:
        raise ApiError("missing 'slug'", 400)
    dash, wr, nr_raw, live, nr = _comparison_inputs(store, slug)
    compare_mod = _lazy("compare")
    frm = body.get("from") or "now-1h"
    to = body.get("to") or "now"
    aids = _account_ids(body, wr)
    if not aids and nr is not None:
        try:
            aids = nr.list_account_ids()
        except Exception as e:
            job.add("could not list NR accounts: %s" % _errmsg(e))
    job.add("Building side-by-side comparison for %r (%s .. %s)"
            % (slug, frm, to))
    report = compare_mod.build_comparison(
        nr, aids, live, nr_raw, dash, wr,
        ds_map=body.get("ds_map"), frm=frm, to=to, log=job.add)
    store.save_artifact(slug, "comparison", report)
    SESSION.status["grafana"] = "ok"
    if nr is not None:
        SESSION.status["newrelic"] = "ok"
    job.add("Comparison score %s -- %s"
            % (report.get("score"),
               ", ".join("%d %s" % (v, k) for k, v in
                         sorted((report.get("summary") or {}).items()))
               or "no panels compared"))
    return report


def _job_samples(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    slug = body.get("slug") or ""
    if not slug:
        raise ApiError("missing 'slug'", 400)
    dash = _dash_from_row(slug, store.get_dashboard(slug))
    wr = (_artifact(store, slug, "widget-report") or {}).get(
        "widgets", [])
    live = _grafana_live()
    nr = None
    if SESSION.nr_api_key:
        try:
            nr = _nerdgraph()
        except ApiError:
            nr = None
    samples_mod = _lazy("samples")
    frm = body.get("from") or "now-1h"
    to = body.get("to") or "now"
    try:
        limit = int(body.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    panel_id = body.get("panel_id")
    aids = _account_ids(body, wr)
    if not aids and nr is not None:
        try:
            aids = nr.list_account_ids()
        except Exception as e:
            job.add("could not list NR accounts: %s" % _errmsg(e))
    job.add("Pulling raw samples for %r (%s .. %s, %d per side%s)"
            % (slug, frm, to, limit,
               ", panel %s" % panel_id if panel_id is not None
               else ""))
    report = samples_mod.collect_samples(
        nr, aids, live, dash, wr, frm=frm, to=to, limit=limit,
        panel_id=panel_id, log=job.add)
    pulled = len(report.get("panels") or [])
    if panel_id is not None:
        report = samples_mod.merge_samples(
            _artifact(store, slug, "samples"), report)
    store.save_artifact(slug, "samples", report)
    SESSION.status["grafana"] = "ok"
    job.add("Sampled %d target(s); review them side by side and "
            "confirm or reject each panel" % pulled)
    return report


def _job_diagnose(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    slug = body.get("slug") or ""
    if not slug:
        raise ApiError("missing 'slug'", 400)
    dash = _dash_from_row(slug, store.get_dashboard(slug))
    live = _grafana_live()
    nr = None
    if SESSION.nr_api_key:
        try:
            nr = _nerdgraph()
        except ApiError:
            nr = None
    job.add("Diagnosing %r against %s" % (slug, SESSION.grafana_url))
    diag = _lazy("diagnose").diagnose(
        live, nr=nr, dash=dash,
        requirements=_artifact(store, slug, "requirements"),
        test_results=(_artifact(store, slug, "datatest")
                      or {}).get("results"),
        parity=_artifact(store, slug, "parity"),
        cfg=_load_cfg(), log=job.add)
    store.save_artifact(slug, "diagnosis", diag)
    findings = diag.get("findings") or []
    job.add("Diagnosis: %d finding(s) -- %s"
            % (len(findings),
               ", ".join("%d %s" % (v, k) for k, v in
                         sorted((diag.get("summary") or {}).items())
                         if isinstance(v, int))
               or "all clear"))
    return diag


def _job_heal(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    slug = body.get("slug") or ""
    if not slug:
        raise ApiError("missing 'slug'", 400)
    row = store.get_dashboard(slug)
    dash = _dash_from_row(slug, row)
    live = _grafana_live()
    nr = None
    if SESSION.nr_api_key:
        try:
            nr = _nerdgraph()
        except ApiError:
            nr = None
    wr = (_artifact(store, slug, "widget-report") or {}).get(
        "widgets", [])
    reqs = _artifact(store, slug, "requirements") or {}
    clog = _lazy("changelog").ChangeLog(store)
    job.add("Auto-heal starting for %r" % slug)
    result = _lazy("remediate").auto_heal(
        live, nr, dash, wr, reqs, slug,
        _package_dir(store, slug), changelog=clog, log=job.add)
    if result.get("fixed"):
        _reupsert_dashboard(store, slug, row, dash)
    try:
        store.save_artifact(slug, "heal", result)
    except Exception:
        pass  # heal summary persistence is best-effort
    if body.get("push") and result.get("fixed"):
        job.add("Pushing healed dashboard to Grafana...")
        res = live.update_dashboard(
            dash, message="nr2grafana: auto-heal (%d fix(es))"
            % result.get("fixed", 0))
        try:
            clog.record(slug, "dashboard-updated",
                        "grafana:%s" % SESSION.grafana_url, "",
                        res.get("url", "updated"),
                        why="pushed auto-heal fixes", source="auto")
        except Exception:
            pass
        result["push"] = res
    job.add("Auto-heal done: %d fix(es), %d finding(s) remaining"
            % (result.get("fixed", 0),
               len(result.get("remaining_findings") or [])))
    return result


def _job_traffic(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    """Sample real datasource traffic (Loki volume, Mimir tsdb status)
    and persist it as the instance-wide "traffic" artifact."""
    live = _grafana_live()
    frm = body.get("from") or "now-24h"
    to = body.get("to") or "now"
    ds_uids = body.get("ds_uids") or None
    ds_list = _traffic_ds_list(live, ds_uids)
    if not ds_list:
        raise ApiError("no Loki/Prometheus/Tempo datasources found to "
                       "sample -- add one in Setup first", 400)
    job.add("Sampling traffic from %d datasource(s) (%s .. %s)"
            % (len(ds_list), frm, to))
    traffic = _lazy("traffic").sample_traffic(
        live, ds_list, frm=frm, to=to, log=job.add)
    store.save_artifact(_INSTANCE_SLUG, "traffic", traffic)
    SESSION.status["grafana"] = "ok"
    job.add("Sampled %d datasource(s)"
            % len(traffic.get("datasources") or []))
    return traffic


def _job_cost(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    """Cross-reference sampled traffic against what the migrated
    dashboards actually need, estimate cost, and emit safe savings
    recommendations. Persists "cost" and "optimize"."""
    slug = body.get("slug") or ""
    if slug and not store.get_dashboard(slug):
        raise ApiError("no dashboard with slug %r -- run Convert first"
                       % slug, 404)
    frm = body.get("from") or "now-24h"
    to = body.get("to") or "now"
    traffic = _artifact(store, _INSTANCE_SLUG, "traffic")
    if not traffic:
        job.add("No cached traffic sample; sampling fresh...")
        live = _grafana_live()
        ds_list = _traffic_ds_list(live, body.get("ds_uids") or None)
        if not ds_list:
            raise ApiError("no Loki/Prometheus/Tempo datasources found "
                           "to sample -- add one in Setup first", 400)
        traffic = _lazy("traffic").sample_traffic(
            live, ds_list, frm=frm, to=to, log=job.add)
        store.save_artifact(_INSTANCE_SLUG, "traffic", traffic)
    else:
        job.add("Using cached traffic sample")
    dashboards, widget_reports = _usage_inputs(store, slug)
    job.add("Computing what %d dashboard(s) need..." % len(dashboards))
    usage = _lazy("usage").collect_usage(dashboards, widget_reports)
    pricing = _effective_pricing(store, body.get("pricing"))
    costmodel = _lazy("costmodel")
    cost = costmodel.estimate_costs(traffic, pricing)
    optimize = _lazy("optimize").recommend(
        traffic, usage, cost=cost, pricing=pricing,
        cfg=_load_cfg(), log=job.add)
    recs = optimize.get("recommendations") or []
    savings = costmodel.apply_savings(cost, recs)
    store_slug = _cost_slug(slug)
    store.save_artifact(store_slug, "cost", cost)
    store.save_artifact(store_slug, "optimize", optimize)
    SESSION.status["grafana"] = "ok"
    job.add("Estimated $%.2f/mo current; %d recommendation(s), "
            "est %s%% saved"
            % (cost.get("monthly_total", 0.0), len(recs),
               savings.get("saved_pct", 0)))
    return {"slug": slug, "cost": cost, "optimize": optimize,
            "savings": savings, "usage": usage}


def _optional_grafana_live():
    """A GrafanaLive client when a URL is configured, else None. Unlike
    :func:`_grafana_live` this never raises -- deep-dive and AI context
    stay usable when no Grafana instance is wired up (direct
    Prometheus/Mimir/Loki URLs, or store artifacts only)."""
    if not SESSION.grafana_url:
        return None
    try:
        return _grafana_live()
    except ApiError:
        return None


def _job_deepdive(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    """Deep LGTM stack analysis: run deepdive.analyze against the
    component self-metrics and, when Kubernetes is requested AND kubectl
    is on PATH, packing.analyze for topology/right-sizing/Karpenter.
    Persists "deepdive" and (when it runs) "packing"."""
    prom = body.get("prom") or ""
    mimir = body.get("mimir") or ""
    loki = body.get("loki") or ""
    grafana = _optional_grafana_live()
    cfg = _load_cfg()
    pricing = body.get("pricing")
    if isinstance(pricing, dict) and pricing:
        cfg = dict(cfg)
        cfg["pricing"] = _effective_pricing(store, pricing)
    job.add("Analyzing the LGTM stack from component self-metrics...")
    deepdive = _lazy("deepdive").analyze(
        prom=prom or None, mimir=mimir or None, loki=loki or None,
        grafana=grafana, cfg=cfg, log=job.add)
    store_slug = _cost_slug(body.get("slug") or "")
    store.save_artifact(store_slug, "deepdive", deepdive)
    out: Dict[str, Any] = {"slug": body.get("slug") or "",
                           "deepdive": deepdive}
    findings = deepdive.get("findings") or []
    job.add("Deep-dive: %d finding(s)" % len(findings))
    if body.get("kube"):
        packing_mod = _lazy("packing")
        if packing_mod.kubectl_available():
            job.add("kubectl found; analyzing node topology, bin-pack "
                    "and Karpenter...")
            prices = pricing if isinstance(pricing, dict) else None
            packing = packing_mod.analyze(
                cfg=cfg, prices=prices, log=job.add)
            store.save_artifact(store_slug, "packing", packing)
            out["packing"] = packing
        else:
            note = ("kubectl not available -- skipping Kubernetes "
                    "topology / packing / Karpenter analysis (the "
                    "metric-driven deep-dive above is unaffected)")
            job.add(note)
            out["packing"] = {"schema": "nr2grafana/packing/v1",
                              "available": False, "note": note,
                              "findings": []}
    return out


def _job_troubleshoot(job: _Job, body: Dict[str, Any], store,
                      assistant) -> Dict[str, Any]:
    """Assemble the AI context bundle and ask the configured AI backend
    the user's troubleshooting question. Never raises: AI errors are
    returned as actionable text by aicontext.troubleshoot."""
    slug = body.get("slug") or ""
    question = body.get("question") or ""
    grafana = _optional_grafana_live()
    deepdive = _artifact(store, _cost_slug(slug), "deepdive")
    aicontext = _lazy("aicontext")
    job.add("Assembling the AI context bundle...")
    context = aicontext.build_context(
        store, slug=slug, grafana=grafana, deepdive=deepdive,
        redact=True)
    job.add("Asking the %s AI backend..." % SESSION.ai_backend())
    result = aicontext.troubleshoot(assistant, context, question)
    SESSION.status["ai"] = "ok"
    job.add("Answer received (%s backend)"
            % result.get("backend", SESSION.ai_backend()))
    return result


def _job_ai_convert_panels(job: _Job, body: Dict[str, Any], store,
                           assistant) -> Dict[str, Any]:
    """Iterate the dashboard's needs-review/untranslatable panels and
    ask the AI backend, in conversion mode, for a higher-fidelity (or
    from-scratch) query per panel. Returns PROPOSALS only -- nothing is
    applied to the dashboard; the UI reviews then applies each via
    /api/panel/convert or /api/panel/update."""
    slug = body.get("slug") or ""
    if not slug:
        raise ApiError("missing 'slug'", 400)
    dash = _dash_from_row(slug, store.get_dashboard(slug))
    wr = (_artifact(store, slug, "widget-report") or {}).get(
        "widgets", [])
    instance: Optional[List[Dict[str, Any]]] = None
    if SESSION.grafana_url:
        try:
            live = _grafana_live()
            instance = [{"name": d.get("name"), "type": d.get("type"),
                         "uid": d.get("uid")}
                        for d in live.datasources()]
        except Exception:
            instance = None  # AI conversion works without a live stack
    flagged = [w for w in wr
               if w.get("confidence") in ("needs-review",
                                          "untranslatable")]
    job.add("Asking the %s AI backend to convert %d flagged panel(s)"
            % (SESSION.ai_backend(), len(flagged)))
    proposals: List[Dict[str, Any]] = []
    for w in flagged:
        pid = w.get("panel_id")
        try:
            panel = _find_panel(dash, pid)
        except ApiError:
            continue  # report row without a matching panel -- skip
        ctx, ds_family, ref_id = _flagged_convert_context(
            store, slug, w, panel, instance)
        title = w.get("widget") or w.get("widget_title") or ""
        proposal: Dict[str, Any] = {
            "panel_id": pid, "refId": ref_id, "ds_family": ds_family,
            "panel_title": title,
            "original_nrql": ctx.get("original_nrql", ""),
            "notes": ctx.get("translation_notes", []),
            "converter_confidence": w.get("confidence"),
        }
        try:
            res = assistant.suggest_fix(ctx)
        except Exception as e:
            proposal.update({"proposed_expr": None, "explanation": "",
                             "confidence": "", "actions": [],
                             "error": _errmsg(e)})
            job.add("panel %s: AI error: %s" % (pid, _errmsg(e)))
            proposals.append(proposal)
            continue
        proposal.update({
            "proposed_expr": res.get("fixed_expr"),
            "explanation": res.get("explanation", ""),
            "confidence": res.get("confidence", ""),
            "actions": res.get("actions") or [],
        })
        proposals.append(proposal)
        job.add("panel %s (%s): %s"
                % (pid, w.get("confidence"),
                   res.get("fixed_expr") or "manual steps only"))
    SESSION.status["ai"] = "ok"
    job.add("Prepared %d proposal(s) -- review and apply each"
            % len(proposals))
    return {"slug": slug, "proposals": proposals,
            "count": len(proposals)}


def _job_tco(job: _Job, body: Dict[str, Any], store) -> Dict[str, Any]:
    """Discover AWS spend over time via Cost Explorer (read-only, local
    auth), attribute the observability share, correlate it with the
    optimizations this tool recorded, and forecast. Persists the "tco"
    artifact and a dated "tco-snapshot". Everything is an ESTIMATE from
    the user's own Cost Explorer data."""
    aws_mod = _awscost()  # re-check inside the job; clean error if absent
    tco = _lazy("tco")
    profile = str(body.get("profile") or "")
    region = str(body.get("region") or "")
    try:
        months = int(body.get("months") or 6)
    except (TypeError, ValueError):
        months = 6
    months = max(1, min(months, 36))
    group_by = body.get("group_by") or "SERVICE"
    buckets = _parse_buckets(body.get("buckets"))
    client = _aws_client(profile, region) if (profile or region) \
        else aws_mod
    # Resolve bucket NAMES to sizes so S3 observability cost can actually
    # be attributed. The form only collects names; CloudWatch
    # BucketSizeBytes (read-only) turns them into the {name: {bytes}}
    # shape tco.attribute_observability prices. Best-effort: on any
    # failure we fall back to the bare name list (tco degrades to a note).
    if buckets and hasattr(aws_mod, "s3_bucket_sizes"):
        try:
            sizes = aws_mod.s3_bucket_sizes(
                buckets, region=region or "us-east-1", profile=profile)
            if isinstance(sizes, dict) and sizes:
                priced = {n: i for n, i in sizes.items()
                          if isinstance(i, dict) and i.get("bytes")}
                job.add("Resolved %d/%d S3 bucket size(s) via CloudWatch "
                        "for S3 attribution." % (len(priced), len(buckets)))
                if priced:
                    buckets = sizes
        except Exception as e:  # best-effort; keep names on failure
            job.add("note: could not resolve S3 bucket sizes (%s); "
                    "S3 attribution will be unpriced." % _errmsg(e))
    deepdive = _artifact(store, _INSTANCE_SLUG, "deepdive")
    traffic = _artifact(store, _INSTANCE_SLUG, "traffic")
    packing = _artifact(store, _INSTANCE_SLUG, "packing")
    try:
        change_log = store.list_changes()
    except Exception:
        change_log = None
    job.add("Discovering AWS cost over %d month(s) via Cost Explorer "
            "(read-only, local auth)..." % months)
    report = _call_tco_analyze(
        tco, client, store=store, deepdive=deepdive, traffic=traffic,
        packing=packing, change_log=change_log, months=months,
        group_by=group_by, buckets=buckets, profile=profile,
        region=region, log=job.add)
    store.save_artifact(_INSTANCE_SLUG, "tco", report)
    try:
        snap = getattr(tco, "snapshot", None)
        if callable(snap):
            snap(store, report)
        else:
            store.save_artifact(_INSTANCE_SLUG, "tco-snapshot", report)
    except Exception as e:  # snapshot persistence is best-effort
        job.add("note: could not persist TCO snapshot (%s)" % _errmsg(e))
    trend = (report.get("total") or {}).get("trend") or {}
    job.add("TCO analysis complete (direction: %s)"
            % (trend.get("direction") or "n/a"))
    return report


# ---------------------------------------------------------------------------
# cost-anomaly RCA / mitigation job bodies (section 9)
# ---------------------------------------------------------------------------

def _job_rca(job: _Job, body: Dict[str, Any], store) -> Dict[str, Any]:
    """Frame a cost anomaly (a pasted report OR a Cost Explorer anomaly
    by id), converge read-only evidence (discovery + VPC flow logs) and
    run the root-cause engine. Persists "rca" (and "flowlogs" when flow
    logs were attributed). AWS access is OPTIONAL and strictly read-only;
    without it the analysis degrades to a lower-confidence hypothesis."""
    rca = _rca_mod()
    profile = str(body.get("profile") or "")
    region = str(body.get("region") or "")
    slug = body.get("slug") or ""
    store_slug = _cost_slug(slug)
    aws = _optional_awscost(profile, region)

    # -- Step A: frame the incident -------------------------------------
    anomaly_id = str(body.get("anomaly_id") or "")
    if anomaly_id:
        if aws is None:
            raise ApiError("AWS is required to fetch anomaly %r from "
                           "Cost Explorer -- configure read-only aws "
                           "credentials, or paste the anomaly report "
                           "instead" % anomaly_id, 400)
        start, end = _anomaly_window(body)
        job.add("Fetching anomaly %s from Cost Explorer (read-only, "
                "%s..%s)..." % (anomaly_id, start, end))
        anomalies = aws.get_anomalies(start, end)
        match = None
        for a in anomalies or []:
            if str(a.get("AnomalyId")) == anomaly_id:
                match = a
                break
        if match is None:
            raise ApiError("no anomaly with id %r in Cost Explorer for "
                           "%s..%s" % (anomaly_id, start, end), 404)
        anomaly = rca.parse_anomaly_report(match)
    else:
        report = _rca_report_input(body)
        if report is None:
            raise ApiError("provide a pasted anomaly 'report' (text or "
                           "JSON) or an 'anomaly_id'", 400)
        job.add("Parsing the pasted anomaly report...")
        anomaly = rca.parse_anomaly_report(report)

    # -- Step B: localize the bytes (VPC flow logs) ---------------------
    flowlogs_res = None
    flow_group = (body.get("flow_logs_group")
                  or body.get("flow_log_group")
                  or body.get("flow_logs") or "")
    if flow_group and aws is None:
        job.add("note: a flow-logs group was given but AWS is not "
                "available (read-only) -- skipping flow-log attribution; "
                "the RCA will be a lower-confidence hypothesis")
    elif flow_group:
        fl = _flowlogs_mod()
        if fl is not None and hasattr(fl, "analyze"):
            start, end = _anomaly_window(body)
            job.add("Attributing cross-AZ bytes from VPC Flow Logs "
                    "group %r..." % flow_group)
            try:
                flowlogs_res = _call_filtered(
                    fl.analyze, aws, log_group=flow_group,
                    region=region, profile=profile, start=start,
                    end=end,
                    onset=anomaly.get("step_change") or anomaly.get(
                        "onset"),
                    cfg=_load_cfg(), log=job.add)
                if isinstance(flowlogs_res, dict) and flowlogs_res:
                    store.save_artifact(store_slug, "flowlogs",
                                        flowlogs_res)
            except Exception as e:
                flowlogs_res = None
                job.add("note: VPC Flow Logs attribution unavailable "
                        "(%s) -- the RCA will degrade to a lower-"
                        "confidence hypothesis" % _errmsg(e))

    # -- Steps C-F: converge evidence into a root cause -----------------
    deepdive = _artifact(store, store_slug, "deepdive")
    packing = _artifact(store, store_slug, "packing")
    tco = _artifact(store, store_slug, "tco")
    job.add("Converging evidence into a root-cause analysis...")
    rca_res = _call_filtered(
        rca.analyze, anomaly, aws=aws, flowlogs=flowlogs_res,
        deepdive=deepdive, packing=packing, tco=tco,
        cfg=_load_cfg(), log=job.add)
    store.save_artifact(store_slug, "rca", rca_res)
    dominant = ((rca_res.get("cause") or {}).get("dominant") or {}) \
        if isinstance(rca_res, dict) else {}
    job.add("RCA complete (confidence: %s; dominant driver: %s)"
            % (rca_res.get("confidence", "n/a")
               if isinstance(rca_res, dict) else "n/a",
               dominant.get("summary") or dominant.get("share")
               or "see report"))
    return {"slug": slug, "rca": rca_res, "flowlogs": flowlogs_res,
            "anomaly": anomaly}


def _job_mitigate(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
    """Turn a stored (or supplied) RCA into a ranked, reliability-safe
    mitigation plan and persist "mitigation". Every mitigation carries
    its reliability preconditions and keeps_* flags; the tool PROPOSES
    only -- nothing is ever executed against AWS/K8s."""
    mitigate = _mitigate_mod()
    slug = body.get("slug") or ""
    store_slug = _cost_slug(slug)
    rca_res = body.get("rca")
    if not (isinstance(rca_res, dict) and rca_res):
        rca_res = _artifact(store, store_slug, "rca")
    if not rca_res:
        raise ApiError("no RCA available -- run an RCA first or pass an "
                       "'rca' object", 404)
    deepdive = _artifact(store, store_slug, "deepdive")
    packing = _artifact(store, store_slug, "packing")
    job.add("Planning reliability-safe mitigations...")
    plan = _call_filtered(
        mitigate.plan, rca_res, deepdive=deepdive, packing=packing,
        cfg=_load_cfg(), log=job.add)
    store.save_artifact(store_slug, "mitigation", plan)
    mits = (plan.get("mitigations") or []) if isinstance(plan, dict) \
        else []
    job.add("Planned %d mitigation(s) -- proposals only, review the "
            "reliability preconditions before applying" % len(mits))
    return {"slug": slug, "mitigation": plan}


def _job_rca_analyze(job: _Job, body: Dict[str, Any], store,
                     assistant) -> Dict[str, Any]:
    """Assemble the AI context bundle (which includes the rca / mitigation
    / flowlogs artifacts) and ask the configured AI backend to analyze
    the anomaly and propose reliability-safe mitigations. Never raises:
    AI errors are returned as actionable text by aicontext."""
    slug = body.get("slug") or ""
    grafana = _optional_grafana_live()
    store_slug = _cost_slug(slug)
    deepdive = _artifact(store, store_slug, "deepdive")
    aicontext = _lazy("aicontext")
    job.add("Assembling the RCA / mitigation AI context bundle...")
    context = aicontext.build_context(
        store, slug=slug, grafana=grafana, deepdive=deepdive,
        redact=True)
    if isinstance(context, dict):
        context["mode"] = "rca"
    job.add("Asking the %s AI backend to analyze the anomaly..."
            % SESSION.ai_backend())
    fn = getattr(aicontext, "analyze_cost", None)
    if callable(fn):
        result = _call_filtered(fn, assistant, context)
    else:  # aicontext mid-build: fall back to the generic Q&A flow
        question = body.get("question") or (
            "Analyze this AWS cost anomaly and the converged evidence, "
            "then propose how to cut the cost WITHOUT reducing "
            "availability, durability, performance or the ability to "
            "serve the current traffic rate.")
        result = aicontext.troubleshoot(assistant, context, question)
    SESSION.status["ai"] = "ok"
    job.add("Analysis received (%s backend)"
            % (result.get("backend", SESSION.ai_backend())
               if isinstance(result, dict) else SESSION.ai_backend()))
    return result


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "nr2grafana"
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # keep the terminal quiet; jobs carry their own logs

    @property
    def store(self):
        return self.server.store  # type: ignore[attr-defined]

    def _json(self, obj: Any, code: int = 200) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type",
                         "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, text: str) -> None:
        raw = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _markdown(self, text: str) -> None:
        raw = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _bytes(self, raw: bytes, ctype: str, filename: str) -> None:
        """Stream a download with a Content-Disposition attachment."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % filename)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_body_bytes(self) -> bytes:
        """Read and cache the request body. Runs at the top of every
        do_* method -- BEFORE the response is written -- because with
        HTTP/1.1 keep-alive an unread body stays in the socket and
        corrupts the next request on that connection (browsers then
        see bogus 501s for requests prefixed with the stray bytes).
        NOTE: BaseHTTPRequestHandler reuses one handler instance for
        all requests on a connection, so this must re-read on every
        call, never trust a cached value from the previous request."""
        length = int(self.headers.get("Content-Length") or 0)
        self._raw_body = self.rfile.read(length) if length > 0 else b""
        return self._raw_body

    def _body(self) -> Dict[str, Any]:
        raw = getattr(self, "_raw_body", b"")
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError("request body is not valid JSON", 400)
        if not isinstance(body, dict):
            raise ApiError("request body must be a JSON object", 400)
        return body

    # Hostnames a browser may legitimately use to reach this localhost
    # server. Any other Host header means the request was aimed at us
    # via a rebound DNS name (DNS-rebinding), and is refused.
    _ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")

    def _host_allowed(self, netloc: str) -> bool:
        """True when ``netloc`` (a Host/Origin/Referer host[:port]) names
        this local server: an allowed loopback hostname and, when a port
        is present, this server's own port."""
        if not netloc:
            return False
        hostname = netloc
        port = ""
        if netloc.startswith("["):  # bracketed IPv6, e.g. [::1]:8765
            rb = netloc.find("]")
            if rb == -1:
                return False
            hostname = netloc[1:rb]
            rest = netloc[rb + 1:]
            if rest.startswith(":"):
                port = rest[1:]
        elif netloc.count(":") == 1:
            hostname, port = netloc.rsplit(":", 1)
        if hostname not in self._ALLOWED_HOSTS:
            return False
        if port and port != str(self.server.server_address[1]):
            return False
        return True

    def _security_guard(self) -> None:
        """Refuse DNS-rebinding and cross-site requests BEFORE dispatch.

        (i) Every request must carry a loopback Host header, else a
        rebound DNS name is pointing a victim's browser at this server.
        (ii) State-changing methods (POST/PUT/DELETE) must additionally
        carry a same-origin Origin (or, absent that, Referer) so a
        malicious page cannot drive this API from the user's browser.
        GET/HEAD are exempt from (ii) so the page and its data load."""
        if not self._host_allowed(self.headers.get("Host", "")):
            raise ApiError("forbidden: unexpected Host header %r -- this "
                           "server only answers loopback requests"
                           % self.headers.get("Host", ""), 403)
        if self.command not in ("POST", "PUT", "DELETE"):
            return
        origin = self.headers.get("Origin")
        if origin is not None:
            if self._host_allowed(urlsplit(origin).netloc):
                return
            raise ApiError("forbidden: cross-origin request blocked "
                           "(Origin %s)" % origin, 403)
        referer = self.headers.get("Referer")
        if referer:
            if self._host_allowed(urlsplit(referer).netloc):
                return
            raise ApiError("forbidden: cross-origin request blocked "
                           "(Referer %s)" % referer, 403)
        raise ApiError("forbidden: state-changing request needs a "
                       "same-origin Origin or Referer header", 403)

    def _dispatch(self, fn: Callable[[], None]) -> None:
        try:
            self._security_guard()
            fn()
        except ApiError as e:
            self._json({"error": str(e)}, e.code)
        except Exception as e:
            name = type(e).__name__
            code = 502 if name in ("GrafanaError", "NerdGraphError",
                                   "AIError") else 500
            self._json({"error": _errmsg(e)}, code)

    # -- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        self._read_body_bytes()
        self._dispatch(self._get)

    def do_POST(self) -> None:  # noqa: N802
        self._read_body_bytes()  # drain even if the handler ignores it
        self._dispatch(self._post)

    def do_PUT(self) -> None:  # noqa: N802
        self._read_body_bytes()
        self._dispatch(self._put)

    def do_DELETE(self) -> None:  # noqa: N802
        self._read_body_bytes()
        self._dispatch(self._delete)

    def _get(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        q = parse_qs(parts.query)
        slug = (q.get("slug") or [""])[0]
        if path in ("/", "/index.html"):
            from . import ui
            self._html(ui.PAGE)
        elif path == "/api/state":
            self._get_state()
        elif path == "/api/dashboards":
            self._get_dashboards()
        elif path.startswith("/api/dashboards/"):
            self._get_dashboard(path[len("/api/dashboards/"):])
        elif path.startswith("/api/jobs/"):
            self._get_job(path[len("/api/jobs/"):])
        elif path == "/api/changes/suggest-config":
            clog = _lazy("changelog").ChangeLog(self.store)
            self._json(clog.suggest_config(slug))
        elif path == "/api/changes":
            self._json({"changes": self.store.list_changes(slug)})
        elif path == "/api/grafana/ds-templates":
            self._json(_lazy("grafana.live").DS_TEMPLATES)
        elif path == "/api/metrics":
            self._get_metrics(q)
        elif path == "/api/labels":
            self._get_labels(q)
        elif path == "/api/review":
            self._get_review(slug)
        elif path == "/api/panel-data":
            self._get_panel_data(q)
        elif path == "/api/readiness":
            self._get_readiness(slug)
        elif path == "/api/pricing":
            self._get_pricing()
        elif path == "/api/deepdive":
            self._get_deepdive(slug)
        elif path == "/api/ai/context":
            self._get_ai_context(q)
        elif path == "/api/mcp/config":
            self._get_mcp_config(q)
        elif path == "/api/tco":
            self._get_tco(slug)
        elif path == "/api/aws/identity":
            self._get_aws_identity(q)
        elif path == "/api/aws/profiles":
            self._get_aws_profiles()
        elif path == "/api/aws/anomalies":
            self._get_aws_anomalies(q)
        elif path.startswith("/download/"):
            self._get_download(path)
        else:
            raise ApiError("not found: %s" % path, 404)

    def _put(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        prefix = "/api/grafana/datasource/"
        if path.startswith(prefix):
            uid = path[len(prefix):]
            if uid and "/" not in uid:
                body = self._body()
                live = _grafana_live()
                self._json(live.update_datasource(uid, body))
                return
        raise ApiError("not found: %s" % path, 404)

    def _delete(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        prefix = "/api/grafana/datasource/"
        if path.startswith(prefix):
            uid = path[len(prefix):]
            if uid and "/" not in uid:
                live = _grafana_live()
                live.delete_datasource(uid)
                self._json({"ok": True, "uid": uid})
                return
        raise ApiError("not found: %s" % path, 404)

    def _post(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        ds_prefix = "/api/grafana/datasource/"
        if path.startswith(ds_prefix) and path.endswith("/health"):
            uid = path[len(ds_prefix):-len("/health")]
            if uid and "/" not in uid:
                live = _grafana_live()
                self._json(live.datasource_health(uid))
                return
        vf_prefix = "/api/datasource/"
        if path.startswith(vf_prefix) and path.endswith("/verify-flow"):
            uid = path[len(vf_prefix):-len("/verify-flow")]
            if uid and "/" not in uid:
                self._post_verify_flow(uid)
                return
        routes = {
            "/api/compare": self._post_compare,
            "/api/settings": self._post_settings,
            "/api/nr/list": self._post_nr_list,
            "/api/nr/fetch": self._post_nr_fetch,
            "/api/nr/test-key": self._post_nr_test_key,
            "/api/convert": self._post_convert,
            "/api/grafana/health": self._post_grafana_health,
            "/api/grafana/test-token": self._post_grafana_test_token,
            "/api/grafana/datasources": self._post_grafana_datasources,
            "/api/grafana/datasource": self._post_grafana_datasource,
            "/api/grafana/plugins": self._post_grafana_plugins,
            "/api/grafana/check": self._post_grafana_check,
            "/api/grafana/test": self._post_grafana_test,
            "/api/grafana/import": self._post_grafana_import,
            "/api/parity": self._post_parity,
            "/api/traffic": self._post_traffic,
            "/api/cost": self._post_cost,
            "/api/pricing": self._post_pricing,
            "/api/samples": self._post_samples,
            "/api/review": self._post_review,
            "/api/diagnose": self._post_diagnose,
            "/api/fix": self._post_fix,
            "/api/heal": self._post_heal,
            "/api/panel/update": self._post_panel_update,
            "/api/panel/convert": self._post_panel_convert,
            "/api/panel/test": self._post_panel_test,
            "/api/ai/suggest": self._post_ai_suggest,
            "/api/ai/convert-panels": self._post_ai_convert_panels,
            "/api/ai/chat": self._post_ai_chat,
            "/api/ai/test": self._post_ai_test,
            "/api/deepdive": self._post_deepdive,
            "/api/ai/troubleshoot": self._post_ai_troubleshoot,
            "/api/mcp/config": self._post_mcp_config,
            "/api/mcp/probe": self._post_mcp_probe,
            "/api/tco": self._post_tco,
            "/api/rca": self._post_rca,
            "/api/rca/analyze": self._post_rca_analyze,
            "/api/mitigate": self._post_mitigate,
        }
        fn = routes.get(path)
        if not fn:
            raise ApiError("not found: %s" % path, 404)
        fn()

    # -- GET handlers ----------------------------------------------------

    def _get_state(self) -> None:
        db = {"dashboards": 0, "changes": 0, "runs": 0, "path": ""}
        try:
            db["dashboards"] = len(self.store.list_dashboards())
            db["changes"] = len(self.store.list_changes())
            db["runs"] = len(self.store.list_runs())
            db["path"] = getattr(self.store, "path", "")
        except Exception:
            pass
        try:
            from .. import __version__
            ver = str(__version__)
        except Exception:
            ver = "1.2.0"
        features: Dict[str, bool] = {}
        for name in ("parity", "diagnose", "remediate", "samples",
                     "compare"):
            try:
                _lazy(name)
                features[name] = True
            except Exception:
                features[name] = False
        # "cost" is on once the whole 1.5 optimization pipeline is
        # importable (traffic sampling + usage + cost model + engine).
        try:
            for name in ("traffic", "usage", "costmodel", "optimize"):
                _lazy(name)
            features["cost"] = True
        except Exception:
            features["cost"] = False
        try:
            features["ds_templates"] = isinstance(
                getattr(_lazy("grafana.live"), "DS_TEMPLATES", None),
                dict)
        except Exception:
            features["ds_templates"] = False
        try:
            features["ai_local"] = hasattr(_lazy("ai"), "LocalAgent")
        except Exception:
            features["ai_local"] = False
        # 1.6 deep-dive / AI-context / MCP capabilities.
        try:
            features["deepdive"] = hasattr(_lazy("deepdive"), "analyze")
        except Exception:
            features["deepdive"] = False
        try:
            features["packing"] = hasattr(_lazy("packing"), "analyze")
        except Exception:
            features["packing"] = False
        try:
            features["ai_context"] = hasattr(_lazy("aicontext"),
                                             "build_context")
        except Exception:
            features["ai_context"] = False
        try:
            features["mcp"] = hasattr(_lazy("mcp"),
                                      "generate_mcp_config")
        except Exception:
            features["mcp"] = False
        # 1.7 TCO / AWS discovery. "tco" is on when the engine is
        # importable; "aws" is on when the aws CLI is present so a run
        # can actually reach Cost Explorer (read-only).
        try:
            features["tco"] = hasattr(_lazy("tco"), "analyze")
        except Exception:
            features["tco"] = False
        try:
            features["aws"] = bool(_lazy("awscost").aws_available())
        except Exception:
            features["aws"] = False
        # 1.9 cost-anomaly RCA + reliability-safe mitigation. "rca" is on
        # once the whole engine (rca + mitigate + reliability + flowlogs)
        # is importable; the routes still degrade cleanly when AWS is
        # absent (a pasted report yields a lower-confidence analysis).
        try:
            for name in ("rca", "mitigate", "reliability", "flowlogs"):
                _lazy(name)
            features["rca"] = True
        except Exception:
            features["rca"] = False
        self._json({"app": "nr2grafana",
                    "version": ver,
                    "session": SESSION.public(),
                    "status": SESSION.status,
                    "status_detail": SESSION.status_detail,
                    "features": features,
                    "db": db})

    def _get_dashboards(self) -> None:
        rows = self.store.list_dashboards()
        out = []
        for row in rows:
            slug = row.get("slug", "")
            item = {"slug": slug, "title": row.get("title", slug),
                    "source": row.get("source", ""),
                    "nr_guid": row.get("nr_guid", ""),
                    "updated": row.get("updated",
                                       row.get("updated_at", ""))}
            try:
                wr = self.store.get_artifact(slug, "widget-report") or {}
                widgets = wr.get("widgets", [])
                item["panels"] = len(widgets)
                item["confidence"] = _confidence_counts(widgets)
            except Exception:
                item["panels"] = 0
                item["confidence"] = {}
            try:
                reqs = self.store.get_artifact(slug, "requirements") or {}
                item["datasources"] = sorted(set(
                    d.get("family", "")
                    for d in reqs.get("datasources", [])))
                item["domains"] = [d.get("domain", "")
                                   for d in reqs.get("domains", [])]
            except Exception:
                item["datasources"] = []
                item["domains"] = []
            try:
                dt = self.store.get_artifact(slug, "datatest") or {}
                item["datatest_summary"] = dt.get("summary", {})
            except Exception:
                item["datatest_summary"] = {}
            par = _artifact(self.store, slug, "parity") or {}
            item["parity_score"] = par.get("score")
            item["parity_summary"] = par.get("summary", {})
            diag = _artifact(self.store, slug, "diagnosis") or {}
            item["findings_summary"] = diag.get("summary", {})
            try:
                item["review_summary"] = _lazy(
                    "samples").review_summary(self.store, slug)
            except Exception:
                item["review_summary"] = {}
            out.append(item)
        self._json({"dashboards": out})

    def _get_dashboard(self, slug: str) -> None:
        row = self.store.get_dashboard(slug)
        if not row:
            raise ApiError("no dashboard with slug %r" % slug, 404)
        dash = _dash_from_row(slug, row)

        def art(kind: str) -> Any:
            try:
                return self.store.get_artifact(slug, kind)
            except Exception:
                return None

        self._json({
            "slug": slug,
            "title": row.get("title", dash.get("title", slug)),
            "source": row.get("source", ""),
            "nr_guid": row.get("nr_guid", ""),
            "dashboard": dash,
            "requirements": art("requirements"),
            "widget_report": (art("widget-report") or {}).get("widgets",
                                                              []),
            "datatest": art("datatest"),
            "check": art("check"),
            "parity": art("parity"),
            "diagnosis": art("diagnosis"),
            "samples": art("samples"),
            "review": art("review"),
            "changes": self.store.list_changes(slug),
            "package_dir": _package_dir(self.store, slug),
        })

    def _get_job(self, jid: str) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(jid)
        if not job:
            raise ApiError("no such job: %s" % jid, 404)
        self._json(job.to_dict())

    def _get_metrics(self, q: Dict[str, List[str]]) -> None:
        """Metric-name autocomplete: up to 200 names matching ?q=."""
        uid = (q.get("uid") or [""])[0]
        if not uid:
            raise ApiError("missing 'uid' query parameter", 400)
        query = (q.get("q") or [""])[0].strip().lower()
        live = _grafana_live()
        # A uid that names no datasource on the instance would otherwise
        # surface as an opaque empty list or a 500; verify it up front
        # (skipping when we already have cached names for it) and return
        # an actionable 404 instead.
        with _METRICS_LOCK:
            entry = _METRICS_CACHE.get(uid)
            cached = bool(entry and time.time() - entry[0] < _METRICS_TTL)
        if not cached:
            known = set()
            try:
                known = set(d.get("uid") for d in (live.datasources()
                                                   or []) if d.get("uid"))
            except Exception:
                known = set()
            if known and uid not in known:
                raise ApiError("no datasource with uid %r on this "
                               "Grafana instance; pick one from "
                               "Datasources" % uid, 404)
        names = _cached_metric_names(live, uid)
        if query:
            names = [n for n in names if query in n.lower()]
        self._json({"uid": uid, "total": len(names),
                    "metrics": names[:200]})

    def _get_labels(self, q: Dict[str, List[str]]) -> None:
        """Label names (or ?label= values) for a prometheus/loki ds."""
        uid = (q.get("uid") or [""])[0]
        if not uid:
            raise ApiError("missing 'uid' query parameter", 400)
        ds_type = (q.get("type") or ["prometheus"])[0] or "prometheus"
        label = (q.get("label") or [""])[0]
        live = _grafana_live()
        if ds_type == "loki":
            if label:
                self._json({"uid": uid, "label": label,
                            "values": live.loki_label_values(uid,
                                                             label)})
            else:
                self._json({"uid": uid, "labels": live.loki_labels(uid)})
        elif ds_type == "prometheus":
            if label:
                self._json({"uid": uid, "label": label,
                            "values": live.prom_label_values(uid,
                                                             label)})
            else:
                fn = getattr(live, "prom_labels", None)
                self._json({"uid": uid,
                            "labels": list(fn(uid)) if fn else []})
        else:
            raise ApiError("type must be prometheus or loki", 400)

    def _get_readiness(self, slug: str) -> None:
        if not slug:
            raise ApiError("missing 'slug' query parameter", 400)
        if not self.store.get_dashboard(slug):
            raise ApiError("no dashboard with slug %r" % slug, 404)
        parity_mod = _lazy("parity")
        res = parity_mod.readiness(
            _artifact(self.store, slug, "parity"),
            check_rows=(_artifact(self.store, slug, "check")
                        or {}).get("items"),
            test_rows=(_artifact(self.store, slug, "datatest")
                       or {}).get("results"),
            review=_artifact(self.store, slug, "review"))
        try:
            res["review"] = _lazy("samples").review_summary(
                self.store, slug)
        except Exception:
            pass
        self._json(res)

    def _get_review(self, slug: str) -> None:
        if not slug:
            raise ApiError("missing 'slug' query parameter", 400)
        if not self.store.get_dashboard(slug):
            raise ApiError("no dashboard with slug %r" % slug, 404)
        samples_mod = _lazy("samples")
        art = _artifact(self.store, slug, "review") or {}
        self._json({"slug": slug,
                    "reviews": art.get("reviews") or {},
                    "summary": samples_mod.review_summary(self.store,
                                                          slug)})

    def _get_panel_data(self, q: Dict[str, List[str]]) -> None:
        """Re-fetch one panel's render data for one side (nr|grafana)
        without a full compare job -- per-panel refresh in the Compare
        view. Reuses compare.build_comparison and returns just the
        requested panel/side."""
        slug = (q.get("slug") or [""])[0]
        if not slug:
            raise ApiError("missing 'slug' query parameter", 400)
        side = (q.get("side") or ["grafana"])[0] or "grafana"
        if side not in ("nr", "grafana"):
            raise ApiError("side must be 'nr' or 'grafana'", 400)
        pid_raw = (q.get("panel_id") or [""])[0]
        if pid_raw == "":
            raise ApiError("missing 'panel_id' query parameter", 400)
        dash, wr, nr_raw, live, nr = _comparison_inputs(self.store, slug)
        aids = _account_ids({}, wr)
        if not aids and nr is not None:
            try:
                aids = nr.list_account_ids()
            except Exception:
                aids = []
        frm = (q.get("from") or ["now-1h"])[0] or "now-1h"
        to = (q.get("to") or ["now"])[0] or "now"
        report = _lazy("compare").build_comparison(
            nr, aids, live, nr_raw, dash, wr, frm=frm, to=to, log=None)
        match = None
        for p in report.get("panels") or []:
            if str(p.get("panel_id")) == str(pid_raw):
                match = p
                break
        if match is None:
            raise ApiError("panel id %r not in comparison for %r"
                           % (pid_raw, slug), 404)
        self._json({"slug": slug, "panel_id": match.get("panel_id"),
                    "side": side, "title": match.get("title"),
                    "viz": match.get("viz"),
                    "verdict": match.get("verdict"),
                    "range": {"from": frm, "to": to},
                    "data": match.get(side)})

    # -- deep-dive / AI-context / MCP (1.6) ------------------------------

    def _get_deepdive(self, slug: str) -> None:
        """Return the stored deep-dive analysis (and packing, when a
        Kubernetes run produced one). ``slug`` is optional -- the stack
        deep-dive is instance-wide by default."""
        store_slug = _cost_slug(slug)
        deepdive = _artifact(self.store, store_slug, "deepdive")
        if not deepdive:
            raise ApiError("no deep-dive analysis yet -- run one from "
                           "the Stack view first", 404)
        self._json({"slug": slug, "deepdive": deepdive,
                    "packing": _artifact(self.store, store_slug,
                                         "packing")})

    # -- TCO / AWS discovery (1.7) ---------------------------------------

    def _get_tco(self, slug: str) -> None:
        """Return the stored TCO report (instance-wide by default) plus,
        when snapshots exist, the trend across them."""
        store_slug = _cost_slug(slug)
        report = _artifact(self.store, store_slug, "tco")
        if not report:
            raise ApiError("no TCO analysis yet -- run one from the TCO "
                           "view first", 404)
        out: Dict[str, Any] = {"slug": slug, "tco": report}
        try:
            tco = _lazy("tco")
            fn = getattr(tco, "trend_over_snapshots", None)
            if callable(fn):
                out["snapshot_trend"] = fn(self.store)
        except Exception:
            pass  # snapshot trend is a bonus; never fail the read
        self._json(out)

    def _get_aws_identity(self, q: Dict[str, List[str]]) -> None:
        """Who the local aws CLI authenticates as (sts get-caller-
        identity) -- shows which account/role the read-only analysis
        would run against. Never mutates anything."""
        aws = _awscost()  # actionable 400 when the aws CLI is absent
        profile = (q.get("profile") or [""])[0]
        region = (q.get("region") or [""])[0]
        client = _aws_client(profile, region) if (profile or region) \
            else aws
        try:
            identity = client.caller_identity()
        except Exception as e:
            raise ApiError("could not read AWS identity: %s -- check "
                           "your aws credentials (read-only)"
                           % _errmsg(e), 502)
        self._json({"identity": identity, "read_only": True,
                    "profile": profile, "region": region})

    def _get_aws_profiles(self) -> None:
        """List named AWS profiles from ~/.aws/config (aws-vault / SSO
        friendly) so the UI can offer a profile picker. Never needs the
        aws CLI itself (profiles are read from config), never reads or
        returns any credential material."""
        try:
            aws = _lazy("awscost")
        except Exception:
            raise ApiError("AWS support is unavailable "
                           "(nr2grafana.awscost not importable)", 400)
        profiles: List[Any] = []
        fn = getattr(aws, "list_profiles", None)
        if callable(fn):
            try:
                profiles = list(fn() or [])
            except Exception as e:
                raise ApiError("could not read AWS profiles: %s"
                               % _errmsg(e), 400)
        try:
            available = bool(aws.aws_available())
        except Exception:
            available = False
        self._json({"profiles": profiles, "aws_available": available,
                    "read_only": True})

    def _get_aws_anomalies(self, q: Dict[str, List[str]]) -> None:
        """Cost Explorer anomalies over a date interval (read-only) so
        the UI can offer an anomaly picker to feed the RCA. Defaults to
        the last 90 days when no interval is given."""
        aws = _awscost()  # actionable 400 when the aws CLI is absent
        profile = (q.get("profile") or [""])[0]
        region = (q.get("region") or [""])[0]
        start = (q.get("start") or [""])[0]
        end = (q.get("end") or [""])[0]
        if not (start and end):
            start, end = _default_anomaly_window()
        client = _aws_client(profile, region) if (profile or region) \
            else aws
        try:
            anomalies = client.get_anomalies(start, end)
        except Exception as e:
            raise ApiError("could not read cost anomalies: %s -- check "
                           "your aws credentials (read-only)"
                           % _errmsg(e), 502)
        self._json({"anomalies": anomalies or [], "start": start,
                    "end": end, "read_only": True, "profile": profile,
                    "region": region})

    def _ai_context(self, slug: str) -> Dict[str, Any]:
        """Build the AI context bundle for ``slug`` (empty = the whole
        workspace), threading in a live Grafana client and the stored
        deep-dive when present. redact=True strips secret-looking
        values defensively."""
        if slug:
            if not _SLUG_RE.match(slug) or not self.store.get_dashboard(
                    slug):
                raise ApiError("no dashboard with slug %r" % slug, 404)
        grafana = _optional_grafana_live()
        deepdive = _artifact(self.store, _cost_slug(slug), "deepdive")
        return _lazy("aicontext").build_context(
            self.store, slug=slug, grafana=grafana, deepdive=deepdive,
            redact=True)

    def _get_ai_context(self, q: Dict[str, List[str]]) -> None:
        slug = (q.get("slug") or [""])[0]
        fmt = (q.get("format") or [""])[0].lower()
        context = self._ai_context(slug)
        if fmt in ("markdown", "md"):
            self._markdown(_lazy("aicontext").to_markdown(context))
        else:
            self._json(context)

    def _mcp_config(self, kind: str, grafana_url: str,
                    include_grafana: bool, n2g_context_path: str,
                    include_aws_cost: bool = False) -> Dict[str, Any]:
        gen = _lazy("mcp").generate_mcp_config
        kwargs: Dict[str, Any] = {"kind": kind,
                                  "n2g_context_path": n2g_context_path,
                                  "include_grafana": include_grafana}
        import inspect
        try:
            params = inspect.signature(gen).parameters
        except (TypeError, ValueError):
            params = {}
        if "include_aws_cost" in params or any(
                p.kind == p.VAR_KEYWORD for p in params.values()):
            kwargs["include_aws_cost"] = include_aws_cost
        cfg = gen(grafana_url, **kwargs)
        return {"kind": kind, "grafana_url": grafana_url,
                "include_grafana": include_grafana,
                "include_aws_cost": include_aws_cost,
                "n2g_context_path": n2g_context_path, "config": cfg}

    def _get_mcp_config(self, q: Dict[str, List[str]]) -> None:
        """Generate an MCP config from persisted prefs + query
        overrides. The Grafana token is NEVER embedded -- the config
        references the GRAFANA_SERVICE_ACCOUNT_TOKEN env var. The AWS
        Cost MCP entry references AWS_PROFILE/AWS_REGION, never keys."""
        kind = (q.get("kind") or [""])[0] \
            or self.store.get_setting("web.mcp_kind", "claude")
        grafana_url = (q.get("grafana_url") or [""])[0] \
            or SESSION.grafana_url
        ctx_path = (q.get("n2g_context_path") or [""])[0] \
            or self.store.get_setting("web.mcp_context_path", "")
        inc_raw = (q.get("include_grafana") or [""])[0]
        include = inc_raw.lower() not in ("0", "false", "no") \
            if inc_raw else True
        aws_raw = (q.get("aws_cost") or [""])[0]
        aws_cost = aws_raw.lower() in ("1", "true", "yes") \
            if aws_raw else bool(
                self.store.get_setting("web.mcp_aws_cost", False))
        self._json(self._mcp_config(kind, grafana_url, include,
                                    ctx_path, aws_cost))

    # -- downloads -------------------------------------------------------

    def _download_ai_context(self, path: str, slug: str) -> None:
        context = self._ai_context(slug)
        aicontext = _lazy("aicontext")
        if path.endswith(".md"):
            raw = (aicontext.to_markdown(context) + "\n").encode("utf-8")
            self._bytes(raw, "text/markdown; charset=utf-8",
                        "ai-context.md")
        else:
            raw = (json.dumps(context, indent=2, ensure_ascii=False)
                   + "\n").encode("utf-8")
            self._bytes(raw, "application/json; charset=utf-8",
                        "ai-context.json")

    def _download_tco_report(self, slug: str) -> None:
        store_slug = _cost_slug(slug)
        report = _artifact(self.store, store_slug, "tco")
        if not report:
            raise ApiError("no TCO analysis yet -- run one first", 404)
        raw = (json.dumps(report, indent=2, ensure_ascii=False)
               + "\n").encode("utf-8")
        self._bytes(raw, "application/json; charset=utf-8",
                    "tco-report.json")

    def _get_download(self, path: str) -> None:
        if path == "/download/all.zip":
            return self._download_all()
        if path == "/download/cost-config.zip":
            q = parse_qs(urlsplit(self.path).query)
            return self._download_cost_config(
                (q.get("slug") or [""])[0])
        if path in ("/download/ai-context.md", "/download/ai-context.json"):
            q = parse_qs(urlsplit(self.path).query)
            return self._download_ai_context(
                path, (q.get("slug") or [""])[0])
        if path == "/download/tco-report.json":
            q = parse_qs(urlsplit(self.path).query)
            return self._download_tco_report((q.get("slug") or [""])[0])
        if path == "/download/mitigation-configs.zip":
            q = parse_qs(urlsplit(self.path).query)
            return self._download_mitigation_configs(
                (q.get("slug") or [""])[0])
        m = re.match(r"^/download/dashboard/([^/]+)\.json$", path)
        if m:
            return self._download_dashboard(m.group(1))
        m = re.match(r"^/download/package/([^/]+)\.zip$", path)
        if m:
            return self._download_package(m.group(1))
        raise ApiError("not found: %s" % path, 404)

    def _known_slug(self, slug: str) -> Dict[str, Any]:
        """Validate a download slug against the store -- anything not
        a stored slug is a 404, so raw paths can never be requested."""
        row = None
        if _SLUG_RE.match(slug) and slug not in (".", ".."):
            row = self.store.get_dashboard(slug)
        if not row:
            raise ApiError("no stored dashboard with slug %r" % slug,
                           404)
        return row

    def _force_download(self) -> bool:
        """True when the request carries ?force=1 / ?force=true, letting
        the caller download past the readiness gate deliberately."""
        q = parse_qs(urlsplit(self.path).query)
        return (q.get("force") or [""])[0].lower() in ("1", "true", "yes")

    def _download_block_reason(self, slug: str) -> str:
        """Actionable reason a dashboard must not be downloaded, or ""
        when it is clear to ship. A dashboard is blocked when a panel
        was rejected in human review, or when parity.readiness grades it
        "blocked" after a verification run. A freshly converted
        dashboard with no parity/check/test/review artifacts has nothing
        to block on and downloads freely."""
        review = _artifact(self.store, slug, "review")
        rows = []
        if isinstance(review, dict):
            rows = list((review.get("reviews") or {}).values())
        rejected = [r for r in rows if r.get("verdict") == "rejected"]
        if rejected:
            return ("%d panel(s) were rejected in human review -- fix "
                    "or re-review them, or add ?force=1 to download "
                    "anyway" % len(rejected))
        parity = _artifact(self.store, slug, "parity")
        check = _artifact(self.store, slug, "check")
        test = _artifact(self.store, slug, "datatest")
        if not (parity or check or test):
            return ""  # nothing verified yet -- nothing to block on
        try:
            res = _lazy("parity").readiness(
                parity, check_rows=(check or {}).get("items"),
                test_rows=(test or {}).get("results"), review=review)
        except Exception:
            return ""  # never let a readiness hiccup block a download
        if res.get("grade") == "blocked":
            reasons = "; ".join(res.get("reasons") or []) \
                or "migration not ready"
            return ("migration readiness is blocked (score %s): %s -- "
                    "resolve the issues or add ?force=1 to download "
                    "anyway" % (res.get("score"), reasons))
        return ""

    def _gate_download(self, slug: str) -> None:
        """Raise 409 when ``slug`` is not ready to download, unless the
        request forced it with ?force=1."""
        if self._force_download():
            return
        reason = self._download_block_reason(slug)
        if reason:
            raise ApiError(reason, 409)

    def _download_dashboard(self, slug: str) -> None:
        row = self._known_slug(slug)
        self._gate_download(slug)
        dash = _dash_from_row(slug, row)
        raw = (json.dumps(dash, indent=2, ensure_ascii=False)
               + "\n").encode("utf-8")
        self._bytes(raw, "application/json; charset=utf-8",
                    slug + ".json")

    @staticmethod
    def _zip_dir(zf: "zipfile.ZipFile", root: str, prefix: str) -> None:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                try:
                    zf.write(full, prefix + "/" + rel.replace(os.sep,
                                                              "/"))
                except OSError:
                    pass  # unreadable file must not kill the download

    def _download_package(self, slug: str) -> None:
        self._known_slug(slug)
        self._gate_download(slug)
        pkg = _package_dir(self.store, slug)
        if not pkg:
            raise ApiError("no package directory for %r -- run Convert "
                           "with packaging first" % slug, 404)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            self._zip_dir(zf, pkg, slug)
        self._bytes(buf.getvalue(), "application/zip", slug + ".zip")

    def _download_all(self) -> None:
        rows = self.store.list_dashboards()
        if not rows:
            raise ApiError("no dashboards stored yet -- run Convert "
                           "first", 404)
        if not self._force_download():
            blocked = []
            for row in rows:
                slug = row.get("slug", "")
                if not slug or not _SLUG_RE.match(slug):
                    continue
                if self._download_block_reason(slug):
                    blocked.append(slug)
            if blocked:
                raise ApiError(
                    "%d dashboard(s) are not ready to download (%s) -- "
                    "resolve them or add ?force=1 to download the whole "
                    "set anyway" % (len(blocked),
                                    ", ".join(sorted(blocked)[:10])),
                    409)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for row in rows:
                slug = row.get("slug", "")
                if not slug or not _SLUG_RE.match(slug):
                    continue
                pkg = _package_dir(self.store, slug)
                if pkg and os.path.isdir(pkg):
                    self._zip_dir(zf, pkg, slug)
                    continue
                try:
                    dash = _dash_from_row(
                        slug, self.store.get_dashboard(slug))
                except ApiError:
                    continue
                zf.writestr(slug + "/dashboard.json",
                            json.dumps(dash, indent=2,
                                       ensure_ascii=False) + "\n")
        self._bytes(buf.getvalue(), "application/zip",
                    "nr2grafana-dashboards.zip")

    # -- POST handlers ---------------------------------------------------

    def _post_settings(self) -> None:
        body = self._body()
        for key in SECRET_KEYS:
            if key in body:
                setattr(SESSION, key, str(body[key] or ""))
        for key in PREF_KEYS:
            if key in body:
                val = str(body[key] or "")
                if key == "nr_region":
                    val = (val or "US").upper()
                    if val not in ("US", "EU"):
                        raise ApiError("nr_region must be US or EU", 400)
                setattr(SESSION, key, val)
                try:
                    self.store.set_setting("web." + key, val)
                except Exception:
                    pass  # prefs persistence is best-effort
        # Reflect key changes in the pills without claiming "tested".
        if "nr_api_key" in body and not SESSION.nr_api_key:
            SESSION.status["newrelic"] = "unset"
        if ("grafana_token" in body or "grafana_url" in body) \
                and not SESSION.grafana_url:
            SESSION.status["grafana"] = "unset"
        if "anthropic_api_key" in body or "ai_command" in body:
            SESSION.status["ai"] = "unset" \
                if SESSION.ai_backend() == "none" else "ok"
        self._json({"ok": True, "session": SESSION.public()})

    def _post_nr_list(self) -> None:
        _nerdgraph()  # fail fast with 400 if key missing
        self._json({"job": _start_job("nr-list", _job_nr_list)})

    def _post_nr_fetch(self) -> None:
        body = self._body()
        _nerdgraph()
        self._json({"job": _start_job(
            "nr-fetch", lambda job: _job_nr_fetch(job, body))})

    def _post_convert(self) -> None:
        body = self._body()
        store = self.store
        nr_json = body.get("nr_json")
        if nr_json is not None:
            # SEAM-2: convert pasted NR json directly (no filesystem
            # input). Validate synchronously so a non-dashboard is a
            # clear 400 rather than a job that quietly errors.
            pasted = _normalize_nr_json(nr_json)
            self._json({"job": _start_job(
                "convert",
                lambda job: _job_convert(job, body, store,
                                         pasted=pasted))})
            return
        self._json({"job": _start_job(
            "convert", lambda job: _job_convert(job, body, store))})

    def _post_grafana_health(self) -> None:
        live = _grafana_live()
        try:
            health = live.health()
        except Exception as e:
            SESSION.status["grafana"] = "error"
            SESSION.status_detail["grafana"] = _errmsg(e)
            raise
        SESSION.status["grafana"] = "ok"
        SESSION.status_detail["grafana"] = (
            "Grafana %s" % health.get("version", "reachable"))
        self._json(health)

    def _post_grafana_datasources(self) -> None:
        live = _grafana_live()
        self._json({"datasources": live.datasources()})

    def _post_grafana_plugins(self) -> None:
        live = _grafana_live()
        self._json({"plugins": live.plugins()})

    def _post_grafana_check(self) -> None:
        body = self._body()
        slug = body.get("slug") or ""
        if not slug:
            raise ApiError("missing 'slug'", 400)
        reqs = self.store.get_artifact(slug, "requirements")
        if not reqs:
            raise ApiError("no requirements recorded for %r -- run "
                           "Convert with packaging first" % slug, 404)
        live = _grafana_live()
        items = live.check_requirements(reqs)
        self.store.save_artifact(slug, "check", {"items": items})
        SESSION.status["grafana"] = "ok"
        self._json({"slug": slug, "items": items})

    def _post_grafana_test(self) -> None:
        body = self._body()
        store = self.store
        self._json({"job": _start_job(
            "grafana-test",
            lambda job: _job_grafana_test(job, body, store))})

    def _post_grafana_import(self) -> None:
        body = self._body()
        store = self.store
        self._json({"job": _start_job(
            "grafana-import",
            lambda job: _job_grafana_import(job, body, store))})

    def _post_nr_test_key(self) -> None:
        """Minimal read-only NerdGraph actor query to prove the key."""
        client = _nerdgraph()
        query = "{ actor { user { name email } accounts { id name } } }"
        try:
            data = client._post(query)
        except Exception as e:
            SESSION.status["newrelic"] = "error"
            SESSION.status_detail["newrelic"] = _errmsg(e)
            raise
        actor = (data or {}).get("actor") or {}
        user = actor.get("user") or {}
        accounts = actor.get("accounts") or []
        SESSION.status["newrelic"] = "ok"
        SESSION.status_detail["newrelic"] = (
            "key valid for %s (%d account(s))"
            % (user.get("email") or "user", len(accounts)))
        self._json({"ok": True, "user": user, "accounts": accounts})

    def _post_grafana_test_token(self) -> None:
        """permissions_report + health for the configured token."""
        live = _grafana_live()
        try:
            report = live.permissions_report()
        except Exception as e:
            SESSION.status["grafana"] = "error"
            SESSION.status_detail["grafana"] = _errmsg(e)
            raise
        try:
            health = live.health()
        except Exception as e:
            health = {"error": _errmsg(e)}
        ok = "error" not in health
        SESSION.status["grafana"] = "ok" if ok else "error"
        SESSION.status_detail["grafana"] = (
            report.get("detail") or report.get("role") or "")
        self._json({"ok": ok, "permissions": report, "health": health})

    def _post_grafana_datasource(self) -> None:
        """Create a datasource from a DS_TEMPLATES form and health-
        check it immediately. values may hold secrets -- never logged."""
        body = self._body()
        ds_type = body.get("type") or ""
        name = body.get("name") or ""
        values = body.get("values") or {}
        if not ds_type or not name:
            raise ApiError("missing 'type' or 'name'", 400)
        live_mod = _lazy("grafana.live")
        try:
            payload = live_mod.build_datasource_payload(ds_type, name,
                                                        values)
        except (KeyError, ValueError) as e:
            raise ApiError("cannot build datasource payload: %s"
                           % _errmsg(e), 400)
        live = _grafana_live()
        created = live.create_datasource(payload)
        uid = ""
        if isinstance(created, dict):
            ds = created.get("datasource")
            if isinstance(ds, dict):
                uid = ds.get("uid", "")
            uid = uid or created.get("uid", "")
        if uid:
            health = live.datasource_health(uid)
        else:
            health = {"status": "unknown",
                      "message": "create returned no uid to "
                                 "health-check"}
        try:
            _lazy("changelog").ChangeLog(self.store).record(
                "", "datasource-created", "%s %s" % (ds_type, name),
                "", uid or name, why="created from web UI",
                source="user")
        except Exception:
            pass
        SESSION.status["grafana"] = "ok"
        resp: Dict[str, Any] = {"ok": health.get("status") == "ok",
                                "uid": uid, "datasource": created,
                                "health": health}
        # When the request names the currently-open dashboard, probe
        # whether data now flows through the new datasource so the UI
        # can show it lighting up the instant it is created.
        slug = body.get("slug") or ""
        if slug and uid:
            try:
                row = self.store.get_dashboard(slug)
                if row:
                    dash = _dash_from_row(slug, row)
                    wr = (_artifact(self.store, slug, "widget-report")
                          or {}).get("widgets", [])
                    reqs = _artifact(self.store, slug,
                                     "requirements") or {}
                    resp["flow"] = _lazy("compare").datasource_flow(
                        live, reqs, dash, wr, ds_uid=uid, ds_map=None)
            except Exception as e:  # flow is a bonus; never fail create
                resp["flow_error"] = _errmsg(e)
        self._json(resp)

    def _post_compare(self) -> None:
        """Build the side-by-side comparison as a job; persists the
        "comparison" artifact and returns build_comparison's output.
        The NR key is optional (NR side falls back to best-effort)."""
        body = self._body()
        _grafana_live()  # fail fast with 400 before starting the job
        store = self.store
        self._json({"job": _start_job(
            "compare", lambda job: _job_compare(job, body, store))})

    def _post_verify_flow(self, uid: str) -> None:
        """Fast (non-job) datasource-flow probe for one ds uid against
        the currently-open dashboard -- powers "re-check flow"."""
        body = self._body()
        slug = body.get("slug") or ""
        if not slug:
            raise ApiError("missing 'slug'", 400)
        dash = _dash_from_row(slug, self.store.get_dashboard(slug))
        wr = (_artifact(self.store, slug, "widget-report") or {}).get(
            "widgets", [])
        reqs = _artifact(self.store, slug, "requirements") or {}
        live = _grafana_live()
        flow = _lazy("compare").datasource_flow(
            live, reqs, dash, wr, ds_uid=uid, ds_map=body.get("ds_map"))
        self._json({"slug": slug, "uid": uid, "flow": flow})

    def _post_parity(self) -> None:
        body = self._body()
        _grafana_live()  # fail fast with 400 before starting the job
        _nerdgraph()
        store = self.store
        self._json({"job": _start_job(
            "parity", lambda job: _job_parity(job, body, store))})

    def _post_traffic(self) -> None:
        body = self._body()
        _grafana_live()  # fail fast with 400 before starting the job
        store = self.store
        self._json({"job": _start_job(
            "traffic", lambda job: _job_traffic(job, body, store))})

    def _post_cost(self) -> None:
        body = self._body()
        _grafana_live()  # fail fast with 400 before starting the job
        store = self.store
        self._json({"job": _start_job(
            "cost", lambda job: _job_cost(job, body, store))})

    def _get_pricing(self) -> None:
        """Effective pricing assumptions (defaults + any persisted
        overrides) and the shipped defaults, for the editable panel."""
        self._json({"pricing": _effective_pricing(self.store),
                    "defaults": _default_pricing()})

    def _post_pricing(self) -> None:
        """Persist non-secret pricing assumptions in Store settings and
        return the effective set. Body: {"pricing": {...}} or a bare
        object of pricing keys."""
        body = self._body()
        pricing = body.get("pricing")
        if not isinstance(pricing, dict):
            pricing = {k: v for k, v in body.items() if k != "pricing"}
        if not isinstance(pricing, dict) or not pricing:
            raise ApiError("missing 'pricing' object", 400)
        try:
            self.store.set_setting("web.pricing", pricing)
        except Exception as e:
            raise ApiError("could not persist pricing: %s" % _errmsg(e),
                           400)
        self._json({"ok": True,
                    "pricing": _effective_pricing(self.store),
                    "defaults": _default_pricing()})

    def _download_cost_config(self, slug: str) -> None:
        """Zip every recommendation's config snippet as paste-ready
        files (promtail.yaml, prometheus-relabel.yaml, ...). slug picks
        a per-dashboard optimize run; omitted = the instance-wide run."""
        if slug:
            self._known_slug(slug)
        store_slug = _cost_slug(slug)
        optimize = _artifact(self.store, store_slug, "optimize")
        if not optimize:
            raise ApiError("no cost recommendations yet -- run a cost "
                           "analysis first", 404)
        files = _cost_config_files(optimize)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, text in files:
                zf.writestr(name, text)
        self._bytes(buf.getvalue(), "application/zip",
                    "cost-config.zip")

    def _download_mitigation_configs(self, slug: str) -> None:
        """Zip every generated mitigation config as paste-ready files
        (Mimir/Loki zone-aware, Service trafficDistribution, Karpenter
        3-AZ discovery, gated NLB cross-zone) plus a README index. slug
        picks a per-dashboard run; omitted = the instance-wide run. All
        configs are GENERIC placeholders; filenames are sanitized to safe
        basenames so nothing can escape the archive."""
        if slug:
            self._known_slug(slug)
        store_slug = _cost_slug(slug)
        mitigation = _artifact(self.store, store_slug, "mitigation")
        if not mitigation:
            raise ApiError("no mitigation plan yet -- run a mitigation "
                           "first", 404)
        files = _mitigation_config_files(mitigation)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, text in files:
                zf.writestr(name, text)
        self._bytes(buf.getvalue(), "application/zip",
                    "mitigation-configs.zip")

    def _post_samples(self) -> None:
        body = self._body()
        _grafana_live()  # fail fast with 400 before starting the job
        store = self.store
        self._json({"job": _start_job(
            "samples", lambda job: _job_samples(job, body, store))})

    def _post_review(self) -> None:
        """Record a human confirm/reject/unsure verdict for a panel
        target and return the updated review summary."""
        body = self._body()
        slug = body.get("slug") or ""
        panel_id = body.get("panel_id")
        verdict = body.get("verdict") or ""
        if not slug or panel_id in (None, "") or not verdict:
            raise ApiError("missing 'slug', 'panel_id' or 'verdict'",
                           400)
        if not self.store.get_dashboard(slug):
            raise ApiError("no dashboard with slug %r" % slug, 404)
        samples_mod = _lazy("samples")
        try:
            entry = samples_mod.record_review(
                self.store, slug, panel_id, body.get("refId") or "",
                verdict, note=body.get("note") or "",
                source=body.get("source") or "user")
        except ValueError as e:
            raise ApiError(str(e), 400)
        self._json({"ok": True, "slug": slug, "review": entry,
                    "summary": samples_mod.review_summary(self.store,
                                                          slug)})

    def _post_diagnose(self) -> None:
        body = self._body()
        _grafana_live()
        store = self.store
        self._json({"job": _start_job(
            "diagnose", lambda job: _job_diagnose(job, body, store))})

    def _post_heal(self) -> None:
        body = self._body()
        _grafana_live()
        store = self.store
        self._json({"job": _start_job(
            "heal", lambda job: _job_heal(job, body, store))})

    def _post_fix(self) -> None:
        """Apply one fix from the stored diagnosis, by finding id."""
        body = self._body()
        slug = body.get("slug") or ""
        fid = body.get("finding_id")
        if not slug or fid in (None, ""):
            raise ApiError("missing 'slug' or 'finding_id'", 400)
        diag = _artifact(self.store, slug, "diagnosis")
        if not diag:
            raise ApiError("no diagnosis recorded for %r -- run "
                           "Diagnose first" % slug, 404)
        finding = None
        for f in diag.get("findings") or []:
            if str(f.get("id")) == str(fid):
                finding = f
                break
        if not finding:
            raise ApiError("no finding %r in the stored diagnosis for "
                           "%r -- re-run Diagnose" % (fid, slug), 404)
        fix = finding.get("fix")
        if not isinstance(fix, dict):
            raise ApiError("finding %r carries no fix" % fid, 400)
        values = body.get("values")
        if fix.get("kind") == "add-datasource" \
                and isinstance(values, dict) and values:
            # The diagnosis action is a template with unfilled
            # needs_input fields; the UI collects them inline and we
            # fold them into a real create payload here so the user
            # never has to leave the Diagnostics view.
            action = fix.get("action") or {}
            ds_type = action.get("type") or ""
            live_mod = _lazy("grafana.live")
            try:
                payload = live_mod.build_datasource_payload(
                    ds_type, action.get("name") or ds_type, values)
            except Exception as e:
                raise ApiError("cannot build datasource payload: %s"
                               % _errmsg(e), 400)
            fix = dict(fix)
            fix["action"] = payload
        row = self.store.get_dashboard(slug)
        dash = _dash_from_row(slug, row)
        live = None
        if SESSION.grafana_url:
            try:
                live = _grafana_live()
            except ApiError:
                live = None
        clog = _lazy("changelog").ChangeLog(self.store)
        result = _lazy("remediate").apply_fix(
            fix, grafana=live, dash=dash,
            package_dir=_package_dir(self.store, slug),
            changelog=clog, slug=slug, push=bool(body.get("push")))
        if result.get("applied"):
            _reupsert_dashboard(self.store, slug, row, dash)
        result.setdefault("finding_id", finding.get("id"))
        self._json(result)

    def _post_panel_update(self) -> None:
        body = self._body()
        slug = body.get("slug") or ""
        expr = body.get("expr")
        if not slug or expr is None:
            raise ApiError("missing 'slug' or 'expr'", 400)
        store = self.store
        row = store.get_dashboard(slug)
        dash = _dash_from_row(slug, row)
        panel = _find_panel(dash, body.get("panel_id"))
        target = _find_target(panel, body.get("refId") or "")
        qkey = _target_query_key(target)
        before = target.get(qkey) \
            if isinstance(target.get(qkey), str) else ""
        target[qkey] = expr
        store.upsert_dashboard(slug, row.get("title",
                                             dash.get("title", slug)),
                               row.get("source", ""),
                               row.get("nr_guid", ""), dash)
        pkg_path = _write_package_dashboard(store, slug, dash)
        clog = _lazy("changelog").ChangeLog(store)
        clog.record(slug, "query-edit",
                    "panel %s [%s]" % (body.get("panel_id"),
                                       target.get("refId", "A")),
                    before, expr, why=body.get("why", ""),
                    source=body.get("source", "user"))
        resp: Dict[str, Any] = {"ok": True, "slug": slug,
                                "before": before, "after": expr,
                                "package_file": pkg_path}
        if body.get("retest"):
            live = _grafana_live()
            results = _single_target_test(live, dash, panel, target,
                                          expr)
            resp["test"] = results
            _merge_datatest(store, slug, results)
        if body.get("push"):
            live = _grafana_live()
            res = live.update_dashboard(
                dash, message="nr2grafana: query edit on panel %s"
                % body.get("panel_id"))
            clog.record(slug, "dashboard-updated",
                        "grafana:%s" % SESSION.grafana_url, "",
                        res.get("url", "updated"),
                        why="pushed edited query",
                        source=body.get("source", "user"))
            resp["push"] = res
        self._json(resp)

    def _post_panel_convert(self) -> None:
        """SEAM-3: give a target-less (untranslatable) panel a real
        query. Attaches a family datasource ref + expr, sets the panel
        datasource, flips a text placeholder to the proper viz where
        known, and rewrites the stored + package dashboard.json +
        datatest.json. Returns the updated panel. AI conversions are
        proposals -- this route only runs on an explicit apply."""
        body = self._body()
        slug = body.get("slug") or ""
        expr = body.get("expr")
        ds_family = str(body.get("ds_family") or "").strip().lower()
        if not slug or expr is None or not ds_family:
            raise ApiError("missing 'slug', 'expr' or 'ds_family'", 400)
        if ds_family not in ("prometheus", "loki", "tempo"):
            raise ApiError("ds_family must be prometheus, loki or tempo",
                           400)
        store = self.store
        row = store.get_dashboard(slug)
        dash = _dash_from_row(slug, row)
        panel = _find_panel(dash, body.get("panel_id"))
        cfg = _load_cfg()
        ds_ref = _family_ds_ref(cfg, ds_family)
        ref_id = body.get("refId") or "A"
        target = _make_convert_target(ds_family, ref_id, expr, ds_ref)
        was_type = panel.get("type")
        viz = _placeholder_viz(store, slug, panel, ds_family)
        if viz:
            _apply_placeholder_viz(panel, viz)
            title = panel.get("title", "")
            for marker in (" [MANUAL]", " [NRQL PASSTHROUGH]",
                           " [REVIEW]"):
                title = title.replace(marker, "")
            panel["title"] = title
        panel["targets"] = [target]
        panel["datasource"] = dict(ds_ref)
        store.upsert_dashboard(slug,
                               row.get("title", dash.get("title", slug)),
                               row.get("source", ""),
                               row.get("nr_guid", ""), dash)
        pkg_dash = _write_package_dashboard(store, slug, dash)
        pkg_dt = _write_package_datatest(store, slug, dash)
        clog = _lazy("changelog").ChangeLog(store)
        clog.record(slug, "query-edit",
                    "panel %s [%s] convert->%s"
                    % (body.get("panel_id"), ref_id, ds_family),
                    "", expr, why=body.get("why", "AI conversion"),
                    source=body.get("source", "user"))
        resp: Dict[str, Any] = {
            "ok": True, "slug": slug, "panel_id": panel.get("id"),
            "panel": panel, "ds_family": ds_family, "refId": ref_id,
            "was_type": was_type, "type": panel.get("type"),
            "after": expr, "package_file": pkg_dash,
            "datatest_file": pkg_dt}
        if body.get("retest"):
            live = _grafana_live()
            results = _single_target_test(live, dash, panel, target,
                                          expr)
            resp["test"] = results
            _merge_datatest(store, slug, results)
        if body.get("push"):
            live = _grafana_live()
            res = live.update_dashboard(
                dash, message="nr2grafana: converted panel %s"
                % body.get("panel_id"))
            clog.record(slug, "dashboard-updated",
                        "grafana:%s" % SESSION.grafana_url, "",
                        res.get("url", "updated"),
                        why="pushed converted panel",
                        source=body.get("source", "user"))
            resp["push"] = res
        self._json(resp)

    def _post_panel_test(self) -> None:
        """Test a candidate expr for one panel WITHOUT saving it."""
        body = self._body()
        slug = body.get("slug") or ""
        expr = body.get("expr")
        if not slug or expr is None:
            raise ApiError("missing 'slug' or 'expr'", 400)
        dash = _dash_from_row(slug, self.store.get_dashboard(slug))
        panel = _find_panel(dash, body.get("panel_id"))
        target = _find_target(panel, body.get("refId") or "")
        live = _grafana_live()
        results = _single_target_test(live, dash, panel, target, expr)
        self._json({"results": results})

    def _post_ai_suggest(self) -> None:
        body = self._body()
        ai = _ai()
        ctx: Dict[str, Any] = dict(body.get("context") or {})
        for key in ("panel", "expr", "error", "datasource", "nrql",
                    "mode", "ds_family", "original_nrql",
                    "translation_notes", "confidence"):
            if body.get(key) is not None:
                ctx[key] = body[key]
        slug = body.get("slug") or ""
        if slug:
            self._enrich_ai_context(ctx, slug, body.get("panel_id"),
                                    body.get("refId") or "")
        if SESSION.grafana_url:
            try:
                live = _grafana_live()
                ctx.setdefault("instance", {})["datasources"] = [
                    {"name": d.get("name"), "type": d.get("type"),
                     "uid": d.get("uid"),
                     "isDefault": d.get("isDefault", False)}
                    for d in live.datasources()]
            except Exception:
                pass  # AI help must work without a live instance
        try:
            res = ai.suggest_fix(ctx)
        except Exception as e:
            SESSION.status["ai"] = "error"
            SESSION.status_detail["ai"] = _errmsg(e)
            raise
        SESSION.status["ai"] = "ok"
        self._json(res)

    def _post_ai_convert_panels(self) -> None:
        """Batch conversion-mode AI over a dashboard's flagged panels.
        Returns a job whose result is a list of PROPOSALS -- nothing is
        applied until the user applies each via panel/convert or
        panel/update."""
        body = self._body()
        ai = _ai()  # 400 fast when no AI backend is configured
        store = self.store
        self._json({"job": _start_job(
            "ai-convert-panels",
            lambda job: _job_ai_convert_panels(job, body, store, ai))})

    def _enrich_ai_context(self, ctx: Dict[str, Any], slug: str,
                           panel_id: Any, ref_id: str) -> None:
        """Fold the stored converter output for one panel into the AI
        context. Beyond the fix-mode fields (requirements, nrql, panel
        title, datasource, error, expr) this fills the SEAM-1
        conversion keys the ai module consumes -- mode (default "fix"),
        translation_notes, original_nrql, ds_family and confidence --
        from the widget-report row so convert-mode has the reasons the
        auto-converter flagged the panel."""
        try:
            reqs = self.store.get_artifact(slug, "requirements")
            if reqs:
                ctx.setdefault("requirements", reqs)
        except Exception:
            pass
        try:
            wr = self.store.get_artifact(slug, "widget-report") or {}
            for w in wr.get("widgets", []):
                if w.get("panel_id") == panel_id:
                    if w.get("nrql"):
                        ctx.setdefault("nrql", w.get("nrql"))
                    raw = _widget_nrql(w)
                    if raw:
                        ctx.setdefault("original_nrql", raw)
                    notes = w.get("notes")
                    if isinstance(notes, list) and notes:
                        ctx.setdefault("translation_notes", notes)
                    if w.get("confidence"):
                        ctx.setdefault("confidence", w.get("confidence"))
                    title = w.get("widget") or w.get("widget_title")
                    if title:
                        ctx.setdefault("panel", title)
                    fam = None
                    for qq in w.get("queries", []):
                        fam = qq.get("datasource")
                        break
                    if fam:
                        if not ctx.get("datasource"):
                            ctx["datasource"] = fam
                        ctx.setdefault(
                            "ds_family",
                            "prometheus" if fam == "newrelic" else fam)
                    break
        except Exception:
            pass
        # mode defaults to "fix"; the UI sends "convert" for the
        # needs-review / untranslatable panels it wants re-translated.
        ctx.setdefault("mode", "fix")
        try:
            dt = self.store.get_artifact(slug, "datatest") or {}
            for r in dt.get("results", []):
                if (r.get("panel_id") == panel_id
                        and (not ref_id or r.get("refId") == ref_id)):
                    ctx.setdefault("error", r.get("error"))
                    ctx.setdefault("expr", r.get("expr"))
                    break
        except Exception:
            pass

    def _post_ai_chat(self) -> None:
        body = self._body()
        ai = _ai()
        messages = body.get("messages") or []
        if not messages:
            raise ApiError("missing 'messages'", 400)
        system = body.get("system") or ""
        base = ("You are the assistant inside nr2grafana, a tool that "
                "migrates New Relic dashboards to Grafana (LGTM stack: "
                "Mimir/Prometheus, Loki, Tempo). Help the user fix "
                "translated PromQL/LogQL/TraceQL queries, choose "
                "datasources, and troubleshoot no-data panels. Be "
                "concise and concrete.")
        try:
            names = [r.get("title", r.get("slug", ""))
                     for r in self.store.list_dashboards()][:20]
            if names:
                base += ("\nDashboards in the local workspace: "
                         + ", ".join(n for n in names if n))
        except Exception:
            pass
        try:
            reply = ai.chat(messages,
                            system=(system + "\n" + base).strip())
        except Exception as e:
            SESSION.status["ai"] = "error"
            SESSION.status_detail["ai"] = _errmsg(e)
            raise
        SESSION.status["ai"] = "ok"
        self._json({"reply": reply})

    def _post_ai_test(self) -> None:
        """Probe the configured AI backend.

        Local mode runs LocalAgent.test() (a trivial "reply OK"
        prompt through the user's console agent); API mode sends the
        same cheap one-line prompt through the Messages API. Returns
        {"ok", "backend", "reply_excerpt", "latency_ms"[, "error"]}
        and never 500s on a failing probe -- the failure is the
        result.
        """
        backend = SESSION.ai_backend()
        ai = _ai()  # 400 with an actionable message when backend none
        if backend == "local" and hasattr(ai, "test"):
            res = ai.test()
        else:
            start = time.time()
            try:
                reply = ai.chat([{"role": "user",
                                  "content": "Reply with exactly: "
                                             "OK"}])
                res = {"ok": True,
                       "reply_excerpt": reply.strip()[:200],
                       "latency_ms": int((time.time() - start)
                                         * 1000)}
            except Exception as e:
                res = {"ok": False, "reply_excerpt": "",
                       "latency_ms": int((time.time() - start)
                                         * 1000),
                       "error": _errmsg(e)}
        res["backend"] = backend
        if res.get("ok"):
            SESSION.status["ai"] = "ok"
            SESSION.status_detail["ai"] = (
                "%s backend ok (%d ms)"
                % (backend, res.get("latency_ms", 0)))
        else:
            SESSION.status["ai"] = "error"
            SESSION.status_detail["ai"] = res.get("error", "")
        self._json(res)

    # -- deep-dive / AI-context / MCP (1.6) ------------------------------

    def _post_deepdive(self) -> None:
        body = self._body()
        store = self.store
        self._json({"job": _start_job(
            "deepdive",
            lambda job: _job_deepdive(job, body, store))})

    def _post_ai_troubleshoot(self) -> None:
        body = self._body()
        ai = _ai()  # 400 fast when no AI backend is configured
        store = self.store
        self._json({"job": _start_job(
            "ai-troubleshoot",
            lambda job: _job_troubleshoot(job, body, store, ai))})

    def _post_mcp_config(self) -> None:
        """Generate an MCP config and persist the non-secret prefs
        (kind, include-grafana, context path). No token is written."""
        body = self._body()
        kind = body.get("kind") \
            or self.store.get_setting("web.mcp_kind", "claude")
        grafana_url = body.get("grafana_url") or SESSION.grafana_url
        ctx_path = body.get("n2g_context_path")
        if ctx_path is None:
            ctx_path = self.store.get_setting("web.mcp_context_path", "")
        include = body.get("include_grafana")
        if include is None:
            include = True
        include = bool(include)
        aws_cost = body.get("aws_cost")
        if aws_cost is None:
            aws_cost = bool(
                self.store.get_setting("web.mcp_aws_cost", False))
        aws_cost = bool(aws_cost)
        resp = self._mcp_config(kind, grafana_url, include, ctx_path,
                                aws_cost)
        for key, val in (("web.mcp_kind", kind),
                         ("web.mcp_context_path", ctx_path or ""),
                         ("web.mcp_include_grafana", include),
                         ("web.mcp_aws_cost", aws_cost)):
            try:
                self.store.set_setting(key, val)
            except Exception:
                pass  # pref persistence is best-effort
        resp["ok"] = True
        self._json(resp)

    def _post_mcp_probe(self) -> None:
        """Probe a Grafana MCP server (stdio command or http/SSE url).
        probe() never raises -- a failed probe returns {"ok": False,
        "error": ...}, so this is a fast non-job route."""
        body = self._body()
        url = body.get("url") or ""
        command = body.get("command")
        if isinstance(command, str) and command.strip():
            import shlex
            command = shlex.split(command)
        elif not isinstance(command, list):
            command = None
        if not url and not command:
            raise ApiError("provide a 'url' (http/SSE) or 'command' "
                           "(stdio) to probe", 400)
        self._json(_lazy("mcp").probe(command=command,
                                      url=url or None))

    # -- TCO / AWS discovery (1.7) ---------------------------------------

    def _post_tco(self) -> None:
        """Run tco.analyze as a job against AWS Cost Explorer (read-only,
        local aws CLI auth). Fails fast with a clear 400 when the aws CLI
        is absent so the UI never sees a crash."""
        body = self._body()
        _awscost()  # fail fast (400) before starting the job
        store = self.store
        self._json({"job": _start_job(
            "tco", lambda job: _job_tco(job, body, store))})

    # -- cost-anomaly RCA / mitigation (1.9) -----------------------------

    def _post_rca(self) -> None:
        """Root-cause a cost anomaly as a job: accept a pasted anomaly
        report OR a Cost Explorer {anomaly_id}, converge read-only
        evidence (discovery + VPC flow logs) and persist "rca" (+
        "flowlogs"). AWS is optional and strictly read-only."""
        body = self._body()
        anomaly_id = str(body.get("anomaly_id") or "")
        if not anomaly_id and _rca_report_input(body) is None:
            raise ApiError("provide a pasted anomaly 'report' (text or "
                           "JSON) or an 'anomaly_id' from Cost Explorer",
                           400)
        _rca_mod()  # fail fast (400) when the RCA engine isn't available
        if anomaly_id:
            _awscost()  # need the aws CLI to fetch the anomaly by id
        store = self.store
        self._json({"job": _start_job(
            "rca", lambda job: _job_rca(job, body, store))})

    def _post_mitigate(self) -> None:
        """Plan reliability-safe mitigations from a stored (or supplied)
        RCA as a job; persists "mitigation". Proposal only -- nothing is
        executed against AWS/K8s."""
        body = self._body()
        _mitigate_mod()  # fail fast (400) when the planner isn't ready
        slug = body.get("slug") or ""
        rca_res = body.get("rca")
        if not (isinstance(rca_res, dict) and rca_res) \
                and not _artifact(self.store, _cost_slug(slug), "rca"):
            raise ApiError("no RCA available -- run an RCA first or pass "
                           "an 'rca' object", 404)
        store = self.store
        self._json({"job": _start_job(
            "mitigate", lambda job: _job_mitigate(job, body, store))})

    def _post_rca_analyze(self) -> None:
        """Ask the AI backend to analyze the anomaly bundle and propose
        reliability-safe mitigations (job)."""
        body = self._body()
        ai = _ai()  # 400 fast when no AI backend is configured
        store = self.store
        self._json({"job": _start_job(
            "rca-analyze",
            lambda job: _job_rca_analyze(job, body, store, ai))})


# ---------------------------------------------------------------------------
# entrypoints
# ---------------------------------------------------------------------------

def _load_prefs(store) -> None:
    """Restore non-secret preferences from the Store into the session."""
    for key in PREF_KEYS:
        try:
            val = store.get_setting("web." + key, "")
        except Exception:
            return
        if val:
            setattr(SESSION, key, val)
    if SESSION.ai_backend() != "none":
        SESSION.status["ai"] = "ok"


def create_server(host: str = "127.0.0.1", port: int = 8765,
                  store=None) -> ThreadingHTTPServer:
    """Build the HTTP server (bound, not yet serving). Used by serve()
    and by tests, which pass port=0 and their own Store."""
    if store is None:
        store = _lazy("store").Store()
    _load_prefs(store)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True  # type: ignore[attr-defined]
    httpd.store = store  # type: ignore[attr-defined]
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True, store=None) -> int:
    """Run the web UI until interrupted. Returns an exit code."""
    try:
        httpd = create_server(host, port, store)
    except OSError as e:
        print("error: cannot bind %s:%d (%s) -- is another nr2grafana "
              "web instance running? Try --port." % (host, port, e))
        return 1
    real_port = httpd.server_address[1]
    url = "http://%s:%d/" % (host or "127.0.0.1", real_port)
    print("nr2grafana web UI: %s  (Ctrl-C to stop)" % url)
    print("API keys entered in the UI stay in this process's memory "
          "only.")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0
