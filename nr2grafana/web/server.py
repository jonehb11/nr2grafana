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


def _job_convert(job: _Job, body: Dict[str, Any], store) \
        -> Dict[str, Any]:
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
    files = _collect_json_files(input_dir)
    os.makedirs(out_dir, exist_ok=True)
    job.add("Converting %d file(s) from %s" % (len(files), input_dir))

    run_id = None
    try:
        run_id = store.record_run("convert", {"input_dir": input_dir,
                                              "out_dir": out_dir,
                                              "package": package})
    except Exception:
        pass

    entries: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    seen_slugs: Dict[str, int] = {}
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            nr = parse_nr_dashboard(data)
            outputs = build_dashboards(nr, cfg)
        except Exception as e:  # one bad file must not kill the batch
            job.add("FAIL %s: %s" % (os.path.basename(path), _errmsg(e)))
            failed.append({"source": path, "error": _errmsg(e)})
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
                               path, getattr(nr, "guid", "") or "",
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
                    % (os.path.basename(path), pkg_dir or slug + ".json",
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

    def _dispatch(self, fn: Callable[[], None]) -> None:
        try:
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
            "/api/samples": self._post_samples,
            "/api/review": self._post_review,
            "/api/diagnose": self._post_diagnose,
            "/api/fix": self._post_fix,
            "/api/heal": self._post_heal,
            "/api/panel/update": self._post_panel_update,
            "/api/panel/test": self._post_panel_test,
            "/api/ai/suggest": self._post_ai_suggest,
            "/api/ai/chat": self._post_ai_chat,
            "/api/ai/test": self._post_ai_test,
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

    # -- downloads -------------------------------------------------------

    def _get_download(self, path: str) -> None:
        if path == "/download/all.zip":
            return self._download_all()
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

    def _download_dashboard(self, slug: str) -> None:
        row = self._known_slug(slug)
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
                                    {"results": merged,
                                     "summary": counts})
            except Exception:
                pass
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
        for key in ("panel", "expr", "error", "datasource", "nrql"):
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

    def _enrich_ai_context(self, ctx: Dict[str, Any], slug: str,
                           panel_id: Any, ref_id: str) -> None:
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
                    title = w.get("widget") or w.get("widget_title")
                    if title:
                        ctx.setdefault("panel", title)
                    if not ctx.get("datasource"):
                        for qq in w.get("queries", []):
                            ctx["datasource"] = qq.get("datasource")
                            break
                    break
        except Exception:
            pass
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
