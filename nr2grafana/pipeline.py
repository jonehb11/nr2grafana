"""The migration pipeline as four library operations.

    import   New Relic -> local NR dashboard JSON (NerdGraph or files)
    convert  NR dashboard JSON -> Grafana dashboard JSON + report
    validate Grafana dashboard JSON -> static problems, datasource needs
             vs. a live Grafana, optional per-panel data test
    export   Grafana dashboard JSON -> a dashboard on a live Grafana,
             verified after creation, with the New Relic source named

Every operation returns a plain dict (JSON-serialisable) so the CLI, the
MCP server and any script get the same structured result. Nothing here
prints; callers pass ``log`` for progress lines. New Relic is only ever
read.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from .config import load_config
from .grafana.builder import build_dashboards, slugify
from .grafana.client import GrafanaError
from .grafana.live import GrafanaLive
from .grafana.validate import validate_dashboard_full
from .model import parse_nr_dashboard

Log = Callable[[str], None]

# Exit codes shared by the CLI:
#   0 ok, 1 problems found (validation errors, untranslatable widgets when
#   --strict, failed imports), 2 bad usage / unreadable input,
#   3 cannot reach or authenticate with New Relic / Grafana.
EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, EXIT_CONNECT = 0, 1, 2, 3


class PipelineError(Exception):
    """A failure with an exit code and an actionable message."""

    def __init__(self, message: str, code: int = EXIT_USAGE):
        super().__init__(message)
        self.code = code


def _noop(_msg: str) -> None:
    pass


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise PipelineError("no such file: %s" % path)
    except json.JSONDecodeError as e:
        raise PipelineError("%s is not valid JSON: %s" % (path, e))
    except OSError as e:
        raise PipelineError("cannot read %s: %s" % (path, e))


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


_ARTIFACT_NAMES = frozenset([
    "migration-report.json", "requirements.json", "widget-report.json",
    "datatest.json", "datatest-results.json", "samples.json",
    "import-manifest.json", "validate-results.json", "export-results.json",
    "parity-results.json", "diagnosis.json",
])


_ARTIFACT_SUFFIXES = tuple("." + n for n in _ARTIFACT_NAMES)


def _is_artifact(name: str) -> bool:
    return name in _ARTIFACT_NAMES or name.endswith(_ARTIFACT_SUFFIXES)


def collect_json_files(paths: List[str], kind: str = "json") -> List[str]:
    """Files from explicit paths and directories (one level, sorted),
    skipping nr2grafana's own artifact files. Raises for a missing path."""
    files: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            direct = os.path.join(p, "dashboard.json")
            if kind == "grafana" and os.path.isfile(direct):
                files.append(direct)
                continue
            found_pkg = False
            if kind == "grafana":
                for name in sorted(os.listdir(p)):
                    sub = os.path.join(p, name, "dashboard.json")
                    if os.path.isfile(sub):
                        files.append(sub)
                        found_pkg = True
            if found_pkg:
                continue
            for name in sorted(os.listdir(p)):
                if name.endswith(".json") and not _is_artifact(name):
                    files.append(os.path.join(p, name))
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise PipelineError("no such file or directory: %s" % p)
    return files


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def _nr_summary(data: Dict[str, Any], path: str) -> Dict[str, Any]:
    nr = parse_nr_dashboard(data)
    return {
        "name": nr.name, "guid": nr.guid, "account_id": nr.account_id,
        "pages": len(nr.pages), "widgets": nr.widget_count(),
        "variables": len(nr.variables), "file": path,
    }


def import_dashboards(out_dir: str, api_key: str = "", region: str = "US",
                      guids: Optional[List[str]] = None,
                      name_filter: str = "", files: Optional[List[str]] = None,
                      log: Log = _noop) -> Dict[str, Any]:
    """Bring New Relic dashboards to disk as NR JSON files.

    Either from New Relic itself (``api_key``; all dashboards the key can
    see, or the given ``guids``, or names matching ``name_filter``) or
    from local export files (``files``: "Copy JSON" exports or NerdGraph
    reads), which are validated and normalised into ``out_dir``. Writes
    ``import-manifest.json``.
    """
    os.makedirs(out_dir, exist_ok=True)
    imported: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    seen: Dict[str, int] = {}

    def place(data: Dict[str, Any], origin: str) -> None:
        try:
            nr = parse_nr_dashboard(data)
        except ValueError as e:
            failed.append({"source": origin, "error": str(e)})
            log("  FAIL %s: %s" % (origin, e))
            return
        slug = slugify(nr.name or "dashboard", 60)
        seen[slug] = seen.get(slug, 0) + 1
        if seen[slug] > 1:
            slug = "%s-%d" % (slug, seen[slug])
        path = os.path.join(out_dir, slug + ".json")
        _write_json(path, data)
        entry = _nr_summary(data, path)
        entry["origin"] = origin
        imported.append(entry)
        log("  ok   %s  (%d pages, %d widgets) -> %s"
            % (nr.name, len(nr.pages), nr.widget_count(), path))

    if files:
        for path in collect_json_files(files):
            place(_read_json(path), path)
    else:
        if not api_key:
            raise PipelineError(
                "a New Relic USER API key is required to import from New "
                "Relic: pass --api-key or set NEW_RELIC_API_KEY (or give "
                "local export files instead)", EXIT_USAGE)
        from .nerdgraph import NerdGraphClient, NerdGraphError
        client = NerdGraphClient(api_key, region=region)
        try:
            if guids:
                entities = [{"guid": g} for g in guids]
            else:
                entities = client.list_dashboards()
                if name_filter:
                    low = name_filter.lower()
                    entities = [e for e in entities
                                if low in (e.get("name") or "").lower()]
                log("Found %d dashboard(s)%s" % (
                    len(entities), " matching %r" % name_filter
                    if name_filter else ""))
            for i, ent in enumerate(entities, 1):
                guid = ent["guid"]
                try:
                    data = client.get_dashboard(guid)
                except NerdGraphError as e:
                    failed.append({"source": guid, "error": str(e)})
                    log("  FAIL [%d/%d] %s: %s" % (i, len(entities), guid, e))
                    continue
                place(data, "newrelic:%s" % guid)
        except NerdGraphError as e:
            raise PipelineError(str(e), EXIT_CONNECT)
    manifest = {
        "schema": "nr2grafana/import-manifest/v1",
        "generated_by": "nr2grafana %s" % __version__,
        "out_dir": os.path.abspath(out_dir),
        "dashboards": imported,
        "failed": failed,
    }
    _write_json(os.path.join(out_dir, "import-manifest.json"), manifest)
    manifest["manifest"] = os.path.join(out_dir, "import-manifest.json")
    manifest["ok"] = not failed
    return manifest


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------

def _cannot_migrate(report: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"page": r.get("page"), "widget": r.get("widget"),
             "visualization": r.get("visualization"),
             "panel_id": r.get("panel_id"), "nrql": r.get("nrql") or [],
             "reason": r.get("reason") or "; ".join(r.get("notes") or []),
             "equivalent": r.get("equivalent", ""),
             "placeholder": r.get("fallback", "")}
            for r in report if r.get("confidence") == "untranslatable"]


def _needs_review(report: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"page": r.get("page"), "widget": r.get("widget"),
             "panel_id": r.get("panel_id"),
             "notes": [n for n in r.get("notes") or []
                       if ":" not in n or n.split(":", 1)[0] not in (
                           "unit", "timefrom", "timeshift", "maxlines",
                           "limit", "panel-hint")]}
            for r in report if r.get("confidence") == "needs-review"]


def convert_dashboards(inputs: List[str], out_dir: str,
                       config_path: str = "", page_strategy: str = "",
                       passthrough: bool = False, package: bool = False,
                       report_path: str = "", store: Any = None,
                       log: Log = _noop) -> Dict[str, Any]:
    """NR dashboard JSON files/dirs -> Grafana dashboard JSON files plus
    ``migration-report.json``. Every output is validated; the result lists
    exactly which widgets could not be migrated and why. With ``package``
    each dashboard gets a package directory (README, requirements, smoke
    test) plus INDEX.md, and — when a ``store`` is given — is recorded in
    the local store."""
    try:
        cfg = load_config(config_path)
    except FileNotFoundError as e:
        raise PipelineError(str(e))
    except json.JSONDecodeError as e:
        raise PipelineError("config %s is not valid JSON: %s"
                            % (config_path, e))
    if page_strategy:
        cfg["page_strategy"] = page_strategy
    if passthrough:
        cfg["passthrough_fallback"] = True
    files = collect_json_files(inputs)
    if not files:
        raise PipelineError("no New Relic dashboard JSON files found in: %s"
                            % ", ".join(inputs))
    os.makedirs(out_dir, exist_ok=True)

    results: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    index_entries: List[Dict[str, Any]] = []
    seen_files: Dict[str, int] = {}
    seen_uids: Dict[str, int] = {}
    seen_titles: Dict[str, int] = {}
    for path in files:
        try:
            data = _read_json(path)
            nr = parse_nr_dashboard(data)
            outputs = build_dashboards(nr, cfg, source_file=path)
        except PipelineError as e:
            failed.append({"source": path, "error": str(e)})
            log("  FAIL %s: %s" % (path, e))
            continue
        except ValueError as e:
            failed.append({"source": path, "error": "not a New Relic "
                                                    "dashboard: %s" % e})
            log("  FAIL %s: not a New Relic dashboard: %s" % (path, e))
            continue
        except Exception as e:  # one bad file must never kill the batch
            failed.append({"source": path, "error": "%s: %s"
                           % (type(e).__name__, e)})
            log("  FAIL %s: conversion failed (%s: %s)"
                % (path, type(e).__name__, e))
            continue
        for filename, dash, report in outputs:
            seen_files[filename] = seen_files.get(filename, 0) + 1
            if seen_files[filename] > 1:
                filename = "%s-%d.json" % (filename[:-5], seen_files[filename])
            uid = dash.get("uid") or ""
            seen_uids[uid] = seen_uids.get(uid, 0) + 1
            if seen_uids[uid] > 1:
                suffix = "-%d" % seen_uids[uid]
                dash["uid"] = uid[:40 - len(suffix)] + suffix
            title = dash.get("title") or ""
            seen_titles[title] = seen_titles.get(title, 0) + 1
            if seen_titles[title] > 1:
                dash["title"] = "%s (%d)" % (title, seen_titles[title])
                log("  note: duplicate dashboard name %r renamed to %r"
                    % (title, dash["title"]))
            slug = filename[:-5]
            validation = validate_dashboard_full(dash)
            out_path = os.path.join(out_dir, filename)
            if package:
                from .artifacts import package_dashboard
                from .requirements import analyze_dashboard, summarize
                try:
                    req = analyze_dashboard(nr, dash, report, cfg)
                    pkg = package_dashboard(out_dir, slug, dash, report, req,
                                            cfg)
                except Exception as e:  # packaging must not lose the output
                    log("  %s: packaging failed (%s: %s); writing flat file"
                        % (slug, type(e).__name__, e))
                    failed.append({"source": path, "error": "packaging "
                                   "failed: %s: %s" % (type(e).__name__, e)})
                    _write_json(out_path, dash)
                else:
                    out_path = os.path.join(pkg, "dashboard.json")
                    index_entries.append({
                        "slug": slug, "title": dash.get("title"),
                        "dir": pkg, "widget_report": report,
                        "requirements": req})
                    log("      package: %s" % summarize(req))
                    _persist(store, slug, path, nr, dash, report, req, pkg,
                             log)
            else:
                _write_json(out_path, dash)
            counts: Dict[str, int] = {}
            for r in report:
                counts[r["confidence"]] = counts.get(r["confidence"], 0) + 1
            from .requirements import datasource_needs
            entry = {
                "source": path,
                "source_dashboard": {"name": nr.name, "guid": nr.guid,
                                     "account_id": nr.account_id},
                "output": out_path,
                "slug": slug,
                "dashboard": dash.get("title"),
                "uid": dash.get("uid"),
                "panels": len([r for r in report]),
                "summary": counts,
                "confidence": counts,
                "datasources": [{"type": n["type"], "variable": n["variable"],
                                 "panels": len(n["panels"])}
                                for n in datasource_needs(dash)],
                "cannot_migrate": _cannot_migrate(report),
                "needs_review": _needs_review(report),
                "validation": validation,
                "widgets": report,
            }
            results.append(entry)
            summary = ", ".join("%d %s" % (v, k)
                                for k, v in sorted(counts.items()))
            log("  %s -> %s  (%s)" % (os.path.basename(path), out_path,
                                      summary or "no widgets"))
            for c in entry["cannot_migrate"]:
                log("      cannot migrate: %s / %s (%s): %s"
                    % (c["page"], c["widget"], c["visualization"],
                       c["reason"]))
            for e in validation["errors"]:
                log("      VALIDATION ERROR: %s" % e)
    report_path = report_path or os.path.join(out_dir,
                                              "migration-report.json")
    _write_json(report_path, {"reports": results, "failed_inputs": failed})
    index_path = ""
    if package:
        from .artifacts import write_index
        index_path = write_index(out_dir, index_entries)
    totals = {
        "dashboards": len(results),
        "widgets": sum(r["panels"] for r in results),
        "cannot_migrate": sum(len(r["cannot_migrate"]) for r in results),
        "needs_review": sum(len(r["needs_review"]) for r in results),
        "validation_errors": sum(len(r["validation"]["errors"])
                                 for r in results),
        "failed_inputs": len(failed),
    }
    return {"ok": not failed and not totals["validation_errors"],
            "out_dir": os.path.abspath(out_dir), "report": report_path,
            "index": index_path,
            "dashboards": results, "failed_inputs": failed,
            "totals": totals}


def _persist(store: Any, slug: str, source: str, nr: Any,
             dash: Dict[str, Any], report: List[Dict[str, Any]],
             req: Dict[str, Any], pkg_dir: str, log: Log) -> None:
    """Best-effort persistence into the local store (never fatal)."""
    if store is None:
        return
    try:
        store.upsert_dashboard(slug, dash.get("title") or slug, source,
                               getattr(nr, "guid", "") or "", dash)
        store.save_artifact(slug, "widget-report", {"widgets": report})
        store.save_artifact(slug, "requirements", req)
        store.set_setting("package_dir." + slug, os.path.abspath(pkg_dir))
    except Exception as e:
        log("  note: could not record %s in the local store (%s)"
            % (slug, e))


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def _source_of(dash: Dict[str, Any]) -> Dict[str, Any]:
    meta = dash.get("nr2grafana") or {}
    src = dict(meta.get("source") or {})
    if not src:
        m = re.search(r"Migrated from New Relic dashboard '([^']*)'"
                      r"(?: \(guid ([^)]*)\))?", dash.get("description") or "")
        if m:
            src = {"name": m.group(1), "guid": m.group(2) or ""}
    return src


def _connect(grafana_url: str, token: str, insecure: bool) -> GrafanaLive:
    if not grafana_url:
        raise PipelineError("a Grafana URL is required: pass --grafana-url "
                            "or set GRAFANA_URL", EXIT_USAGE)
    live = GrafanaLive(grafana_url, token=token, insecure=insecure)
    try:
        health = live.health()
    except GrafanaError as e:
        raise PipelineError("cannot reach Grafana at %s: %s"
                            % (grafana_url, e), EXIT_CONNECT)
    try:
        live.datasources()
    except GrafanaError as e:
        msg = str(e)
        if "HTTP 401" in msg or "HTTP 403" in msg:
            raise PipelineError(
                "Grafana at %s rejected the token (%s). Use a service-"
                "account token with the Editor role (Admin to create "
                "folders); pass --grafana-token or set GRAFANA_TOKEN"
                % (grafana_url, msg.split(":")[0]), EXIT_CONNECT)
        raise PipelineError("cannot list datasources on %s: %s"
                            % (grafana_url, e), EXIT_CONNECT)
    live.version = str(health.get("version", "")) if isinstance(
        health, dict) else ""
    return live


def _parse_overrides(pairs: Optional[List[str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise PipelineError("--datasource expects type=uid (e.g. "
                                "prometheus=mimir), got %r" % item)
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def validate_dashboards(inputs: List[str], grafana_url: str = "",
                        grafana_token: str = "", insecure: bool = False,
                        test: bool = False,
                        datasource_overrides: Optional[List[str]] = None,
                        write_results: bool = True,
                        log: Log = _noop) -> Dict[str, Any]:
    """Static validation of converted dashboards; with a Grafana URL also
    checks the datasource types they need against the instance and, with
    ``test``, runs every panel query through Grafana."""
    files = collect_json_files(inputs, kind="grafana")
    if not files:
        raise PipelineError("no Grafana dashboard JSON files found in: %s"
                            % ", ".join(inputs))
    live: Optional[GrafanaLive] = None
    if grafana_url or test:
        live = _connect(grafana_url, grafana_token, insecure)
        log("connected to Grafana %s (%s)" % (grafana_url,
                                              live.version or "?"))
    overrides = _parse_overrides(datasource_overrides)
    results: List[Dict[str, Any]] = []
    for path in files:
        try:
            dash = _read_json(path)
        except PipelineError as e:
            results.append({"file": path, "ok": False,
                            "errors": ["INVALID JSON: %s" % e],
                            "warnings": []})
            log("  FAIL %s: INVALID JSON: %s" % (path, e))
            continue
        if not isinstance(dash, dict) or "panels" not in dash:
            results.append({"file": path, "ok": False,
                            "errors": ["not a Grafana dashboard (no "
                                       "'panels')"], "warnings": []})
            log("  FAIL %s: not a Grafana dashboard" % path)
            continue
        static = validate_dashboard_full(dash)
        entry: Dict[str, Any] = {
            "file": path, "title": dash.get("title"), "uid": dash.get("uid"),
            "source": _source_of(dash),
            "errors": list(static["errors"]),
            "warnings": list(static["warnings"]),
        }
        if live is not None:
            try:
                check = live.datasource_check(dash, overrides)
            except GrafanaError as e:
                raise PipelineError("datasource check failed: %s" % e,
                                    EXIT_CONNECT)
            entry["datasources"] = check["needs"]
            for need in check["missing"]:
                entry["errors"].append(
                    "Grafana has no %s datasource (needed by %d panel(s): "
                    "%s); fix: %s" % (need["type"], len(need["panels"]),
                                      need["purpose"], need["fix"]))
            for need in check["needs"]:
                if need.get("note"):
                    entry["warnings"].append(need["note"])
            if test:
                rows = live.test_dashboard(dash, ds_map=check["ds_map"],
                                           log=lambda m: log("   " + m))
                counts: Dict[str, int] = {}
                for r in rows:
                    counts[r["status"]] = counts.get(r["status"], 0) + 1
                entry["data_test"] = {"summary": counts, "panels": rows}
                for r in rows:
                    if r["status"] == "error":
                        entry["errors"].append(
                            "panel %r (%s): query error: %s"
                            % (r["panel_title"], r["refId"], r["error"]))
                    elif r["status"] == "no-data":
                        entry["warnings"].append(
                            "panel %r (%s) returned no data — the metric/"
                            "labels may not exist in your stack yet: %s"
                            % (r["panel_title"], r["refId"], r["expr"]))
        entry["ok"] = not entry["errors"]
        results.append(entry)
        state = "OK" if entry["ok"] else "FAIL"
        log("  %-4s %s  (%d error(s), %d warning(s))" % (
            state, entry.get("title") or path, len(entry["errors"]),
            len(entry["warnings"])))
        for e in entry["errors"]:
            log("       ERROR   %s" % e)
        for w in entry["warnings"]:
            log("       warning %s" % w)
        if write_results:
            out_path = _sidecar(path, "validate-results.json")
            _write_json(out_path, entry)
    return {"ok": all(r["ok"] for r in results), "dashboards": results,
            "totals": {"dashboards": len(results),
                       "failed": sum(1 for r in results if not r["ok"]),
                       "errors": sum(len(r["errors"]) for r in results),
                       "warnings": sum(len(r["warnings"]) for r in results)}}


def _sidecar(dash_path: str, name: str) -> str:
    base = os.path.basename(dash_path)
    d = os.path.dirname(os.path.abspath(dash_path))
    if base == "dashboard.json":
        return os.path.join(d, name)
    stem = base[:-5] if base.endswith(".json") else base
    return os.path.join(d, "%s.%s" % (stem, name))


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _bind_datasources(dash: Dict[str, Any], check: Dict[str, Any]) \
        -> Dict[str, Any]:
    """Pin the dashboard's datasource variables to the chosen instances
    so the imported dashboard works without a manual pick (the variables
    stay, so the dashboard remains portable)."""
    out = json.loads(json.dumps(dash))
    chosen = {n["variable"]: n["chosen"] for n in check["needs"]
              if n.get("chosen")}
    for v in (out.get("templating") or {}).get("list") or []:
        if v.get("type") == "datasource" and v.get("name") in chosen:
            c = chosen[v["name"]]
            v["current"] = {"selected": True, "text": c["name"],
                            "value": c["uid"]}
    return out


def export_dashboards(inputs: List[str], grafana_url: str,
                      grafana_token: str = "", insecure: bool = False,
                      folder: str = "", overwrite: bool = False,
                      datasource_overrides: Optional[List[str]] = None,
                      test: bool = False, allow_missing: bool = False,
                      log: Log = _noop) -> Dict[str, Any]:
    """Create the converted dashboards on a Grafana instance and verify
    each one after creation. Refuses to export a dashboard with static
    validation errors or missing datasource types (unless
    ``allow_missing``)."""
    files = collect_json_files(inputs, kind="grafana")
    if not files:
        raise PipelineError("no Grafana dashboard JSON files found in: %s"
                            % ", ".join(inputs))
    live = _connect(grafana_url, grafana_token, insecure)
    log("connected to Grafana %s (%s)" % (grafana_url, live.version or "?"))
    overrides = _parse_overrides(datasource_overrides)
    folder_uid = ""
    if folder:
        try:
            folder_uid = live.find_or_create_folder(folder)
        except GrafanaError as e:
            raise PipelineError(
                "cannot use folder %r: %s (the token needs permission to "
                "create folders, or create it in Grafana first)"
                % (folder, e), EXIT_CONNECT)
    results: List[Dict[str, Any]] = []
    for path in files:
        dash = _read_json(path)
        entry: Dict[str, Any] = {"file": path, "title": dash.get("title"),
                                 "uid": dash.get("uid"),
                                 "source": _source_of(dash), "ok": False,
                                 "problems": []}
        results.append(entry)
        if not isinstance(dash, dict) or "panels" not in dash:
            entry["problems"].append("not a Grafana dashboard (no 'panels')")
            log("  SKIP %s: not a Grafana dashboard" % path)
            continue
        static = validate_dashboard_full(dash)
        if static["errors"]:
            entry["problems"].extend("validation: " + e
                                     for e in static["errors"])
            log("  SKIP %s: %d validation error(s); run `validate` first"
                % (dash.get("title") or path, len(static["errors"])))
            for e in static["errors"]:
                log("       %s" % e)
            continue
        try:
            check = live.datasource_check(dash, overrides)
        except GrafanaError as e:
            raise PipelineError("datasource check failed: %s" % e,
                                EXIT_CONNECT)
        entry["datasources"] = [
            {"type": n["type"], "status": n["status"],
             "chosen": n.get("chosen"), "panels": len(n["panels"]),
             "fix": n.get("fix", "")} for n in check["needs"]]
        if check["missing"] and not allow_missing:
            for need in check["missing"]:
                entry["problems"].append(
                    "Grafana has no %s datasource (needed by %d panel(s)); "
                    "%s" % (need["type"], len(need["panels"]), need["fix"]))
            log("  SKIP %s: missing datasource(s): %s (use --allow-missing "
                "to export anyway)" % (dash.get("title") or path,
                                       ", ".join(n["type"]
                                                 for n in check["missing"])))
            continue
        bound = _bind_datasources(dash, check)
        bound.pop("nr2grafana", None)
        try:
            res = live.import_dashboard(bound, folder_uid=folder_uid,
                                        overwrite=overwrite,
                                        message="nr2grafana export of New "
                                                "Relic dashboard %r"
                                                % (entry["source"].get(
                                                    "name") or ""))
        except GrafanaError as e:
            msg = str(e)
            hint = ""
            if "name-exists" in msg or "version-mismatch" in msg \
                    or "HTTP 412" in msg:
                hint = (" — a dashboard with this title/uid already exists "
                        "in the folder; re-run with --overwrite to replace "
                        "it")
            elif "HTTP 403" in msg:
                hint = " — the token cannot create dashboards here (needs " \
                       "the Editor role on the folder)"
            entry["problems"].append("import failed: %s%s" % (msg, hint))
            log("  FAIL %s: %s%s" % (dash.get("title") or path, msg, hint))
            continue
        uid = res.get("uid") or dash.get("uid") or ""
        url = res.get("url") or ""
        if url and not url.startswith("http"):
            url = grafana_url.rstrip("/") + url
        entry.update({"uid": uid, "url": url, "id": res.get("id"),
                      "version": res.get("version"),
                      "folder": folder or "General"})
        try:
            verify = live.verify_import(uid, bound)
        except GrafanaError as e:
            verify = {"ok": False, "problems": ["read-back failed: %s" % e]}
        entry["verified"] = verify
        if not verify.get("ok"):
            entry["problems"].extend(verify.get("problems") or [])
        if test:
            rows = live.test_dashboard(bound, ds_map=check["ds_map"],
                                       log=lambda m: log("   " + m))
            counts: Dict[str, int] = {}
            for r in rows:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            entry["data_test"] = {"summary": counts, "panels": rows}
            for r in rows:
                if r["status"] == "error":
                    entry["problems"].append(
                        "panel %r (%s) query error: %s"
                        % (r["panel_title"], r["refId"], r["error"]))
        entry["ok"] = not entry["problems"]
        src = entry["source"]
        log("  %s %s -> %s  (uid %s%s)" % (
            "ok  " if entry["ok"] else "WARN", dash.get("title"),
            url or "(no url)", uid,
            ", folder %s" % folder if folder else ""))
        if src.get("name"):
            log("       sourced from New Relic dashboard %r%s" % (
                src["name"], " (guid %s)" % src["guid"] if src.get("guid")
                else ""))
        for p in entry["problems"]:
            log("       problem: %s" % p)
        _write_json(_sidecar(path, "export-results.json"), entry)
    created = [r for r in results if r.get("url") or r.get("uid")
               and r.get("verified")]
    return {"ok": all(r["ok"] for r in results), "grafana_url": grafana_url,
            "folder": folder, "dashboards": results,
            "totals": {"dashboards": len(results),
                       "created": len(created),
                       "failed": sum(1 for r in results if not r["ok"])}}


# ---------------------------------------------------------------------------
# inspect / explain
# ---------------------------------------------------------------------------

def inspect_inputs(inputs: List[str], config_path: str = "") \
        -> List[Dict[str, Any]]:
    from .inspect import inspect_dashboard
    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise PipelineError(str(e))
    out = []
    for path in collect_json_files(inputs):
        data = _read_json(path)
        try:
            out.append(inspect_dashboard(data, cfg, source=path))
        except ValueError as e:
            out.append({"source": path, "error": "not a New Relic "
                                                 "dashboard: %s" % e})
    if not out:
        raise PipelineError("no New Relic dashboard JSON files found in: %s"
                            % ", ".join(inputs))
    return out


def explain(nrql: str, config_path: str = "") -> Dict[str, Any]:
    from .inspect import explain_nrql
    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise PipelineError(str(e))
    return explain_nrql(nrql, cfg)
