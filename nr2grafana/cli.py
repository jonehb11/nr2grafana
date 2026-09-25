"""nr2grafana CLI.

Commands:
  fetch     Bulk-export dashboards from New Relic (NerdGraph) to JSON files
  convert   Convert NR dashboard JSON file(s) to Grafana dashboard JSON
            (--package adds per-dashboard requirement packages + INDEX.md)
  analyze   (Re)generate requirements/packages for converted output
  validate  Statically validate Grafana dashboard JSON file(s)
  list      List dashboards visible to the API key
  grafana   Live Grafana ops: check requirements, test data, parity,
            diagnose, heal, datasources, import
  changes   Change-log reports and config codification
  web       Localhost web UI for the whole workflow
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from .artifacts import package_dashboard, write_index
from .changelog import ChangeLog
from .config import DEFAULT_CONFIG, load_config
from .diagnose import diagnose
from .grafana.builder import build_dashboards, slugify
from .grafana.client import GrafanaError
from .grafana.live import (DS_TEMPLATES, GrafanaLive,
                           build_datasource_payload)
from .grafana.validate import validate_dashboard
from .model import parse_nr_dashboard
from .nerdgraph import NerdGraphClient, NerdGraphError
from .parity import readiness, run_parity
from .remediate import auto_heal
from .samples import collect_samples, review_summary
from .requirements import analyze_dashboard, summarize
from .store import Store, StoreError

# Non-dashboard JSON files that live next to dashboards in output and
# package directories; never treated as dashboards to analyze/import.
_ARTIFACT_NAMES = frozenset([
    "migration-report.json", "requirements.json", "widget-report.json",
    "datatest.json", "datatest-results.json", "samples.json",
])


def _err(msg: str) -> None:
    print("error: %s" % msg, file=sys.stderr)


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _collect_inputs(paths: List[str]) -> List[str]:
    files: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.endswith(".json"):
                    files.append(os.path.join(p, name))
        elif os.path.isfile(p):
            files.append(p)
        else:
            _err("no such file or directory: %s" % p)
    return files


def _api_key(args: argparse.Namespace) -> str:
    key = args.api_key or os.environ.get("NEW_RELIC_API_KEY", "")
    if not key:
        _err("a New Relic USER API key is required: pass --api-key or set "
             "NEW_RELIC_API_KEY")
        sys.exit(2)
    return key


def _db_path() -> str:
    """Store path override for tests/automation (N2G_DB env var);
    empty means the default ~/.nr2grafana/nr2grafana.db."""
    return os.environ.get("N2G_DB", "")


def _open_store_soft() -> Optional[Store]:
    """Open the local store, degrading to None (with a note) so a
    broken/locked db never kills a conversion run."""
    try:
        return Store(_db_path())
    except Exception as e:
        print("note: local store unavailable (%s) -- results will not "
              "be recorded" % e, file=sys.stderr)
        return None


def _persist_package(store: Optional[Store], slug: str, source: str,
                     nr, dash: Dict[str, Any],
                     report: List[Dict[str, Any]],
                     req: Dict[str, Any], pkg_dir: str) -> None:
    """Best-effort persistence matching the web UI's conventions."""
    if store is None:
        return
    try:
        guid = getattr(nr, "guid", "") or "" if nr is not None else ""
        store.upsert_dashboard(slug, dash.get("title") or slug, source,
                               guid, dash)
        store.save_artifact(slug, "widget-report", {"widgets": report})
        store.save_artifact(slug, "requirements", req)
        store.set_setting("package_dir." + slug, os.path.abspath(pkg_dir))
    except (StoreError, OSError, ValueError) as e:
        print("note: could not record %s in the local store (%s)"
              % (slug, e), file=sys.stderr)


def _grafana_live(args: argparse.Namespace) -> GrafanaLive:
    url = getattr(args, "grafana_url", "") \
        or os.environ.get("GRAFANA_URL", "")
    token = getattr(args, "grafana_token", "") \
        or os.environ.get("GRAFANA_TOKEN", "")
    if not url:
        _err("a Grafana URL is required: pass --url (or --grafana-url) "
             "or set GRAFANA_URL")
        sys.exit(2)
    return GrafanaLive(url, token=token,
                       insecure=getattr(args, "insecure", False))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    client = NerdGraphClient(_api_key(args), region=args.region)
    try:
        dashboards = client.list_dashboards()
    except NerdGraphError as e:
        _err(str(e))
        return 1
    for d in dashboards:
        print("%s\t%s\t(account %s)" % (d.get("guid"), d.get("name"),
                                        d.get("accountId")))
    print("\n%d dashboards" % len(dashboards), file=sys.stderr)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    client = NerdGraphClient(_api_key(args), region=args.region)
    exported = 0
    failed: List[str] = []
    try:
        if args.guid:
            entities = [{"guid": g} for g in args.guid]
        else:
            entities = client.list_dashboards()
            print("Found %d dashboards" % len(entities), file=sys.stderr)
        os.makedirs(args.out, exist_ok=True)
        seen_names: Dict[str, int] = {}
        for i, ent in enumerate(entities, 1):
            guid = ent["guid"]
            try:
                dash = client.get_dashboard(guid)
            except NerdGraphError as e:
                _err("[%d/%d] %s failed: %s" % (i, len(entities), guid, e))
                failed.append(guid)
                continue
            slug = slugify(dash.get("name", "dashboard"), 60)
            seen_names[slug] = seen_names.get(slug, 0) + 1
            if seen_names[slug] > 1:
                slug = "%s-%d" % (slug, seen_names[slug])
            path = os.path.join(args.out, slug + ".json")
            _write_json(path, dash)
            exported += 1
            print("[%d/%d] %s -> %s" % (i, len(entities),
                                        dash.get("name"), path),
                  file=sys.stderr)
    except NerdGraphError as e:
        _err(str(e))
        return 1
    print("\nExported %d dashboards to %s" % (exported, args.out),
          file=sys.stderr)
    if failed:
        _err("%d failed: %s" % (len(failed), ", ".join(failed)))
        return 1
    return 0


def _log_err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _print_cannot_migrate(result: Dict[str, Any]) -> None:
    """The exact list of widgets that did not become live panels."""
    rows = [(d["dashboard"], c) for d in result.get("dashboards", [])
            for c in d.get("cannot_migrate", [])]
    if not rows:
        return
    print("\nWidgets that cannot be migrated to Grafana (%d):" % len(rows),
          file=sys.stderr)
    for title, c in rows:
        print("  - %s / %s / %s (%s)" % (title, c.get("page"),
                                         c.get("widget"),
                                         c.get("visualization")),
              file=sys.stderr)
        for nrql in c.get("nrql") or []:
            print("      NRQL: %s" % nrql, file=sys.stderr)
        print("      why: %s" % c.get("reason"), file=sys.stderr)
        if c.get("equivalent"):
            print("      closest Grafana equivalent: %s" % c["equivalent"],
                  file=sys.stderr)
        print("      in the output: %s panel titled '... [MANUAL]'"
              % ("NRQL passthrough" if c.get("placeholder")
                 == "nrql-passthrough" else "text placeholder"),
              file=sys.stderr)


def cmd_convert(args: argparse.Namespace) -> int:
    from . import pipeline
    package = bool(getattr(args, "package", False))
    store: Optional[Store] = None
    run_id = 0
    if package:
        store = _open_store_soft()
        if store is not None:
            try:
                run_id = store.record_run("convert", {
                    "inputs": list(args.inputs), "out": args.out,
                    "package": True})
            except (StoreError, ValueError):
                run_id = 0
    try:
        result = pipeline.convert_dashboards(
            list(args.inputs), args.out, config_path=args.config,
            page_strategy=args.page_strategy or "",
            passthrough=bool(args.passthrough), package=package,
            report_path=getattr(args, "report", "") or "", store=store,
            log=_log_err)
    except pipeline.PipelineError as e:
        msg = str(e)
        if msg.startswith("no such file") or msg.startswith(
                "no New Relic dashboard JSON"):
            msg = "no input files (%s)" % msg
        _err(msg)
        if store is not None:
            store.close()
        return e.code
    for f in result["failed_inputs"]:
        _err("%s: %s" % (f["source"], f["error"]))
    for d in result["dashboards"]:
        for e in d["validation"]["errors"]:
            _err("%s produced invalid output: %s" % (d["output"], e))
    _print_cannot_migrate(result)
    t = result["totals"]
    if result.get("index"):
        print("Index: %s" % result["index"], file=sys.stderr)
    failure_note = (", %d input file(s) FAILED (see failed_inputs in the "
                    "report)" % t["failed_inputs"]) if t["failed_inputs"] \
        else ""
    print("\n%d dashboards written, %d widgets converted (%d need review, "
          "%d cannot be migrated)%s. Report: %s"
          % (t["dashboards"], t["widgets"], t["needs_review"],
             t["cannot_migrate"], failure_note, result["report"]),
          file=sys.stderr)
    had_error = bool(t["failed_inputs"] or t["validation_errors"])
    if store is not None:
        try:
            if run_id:
                store.finish_run(run_id, "error" if had_error else "ok",
                                 {"dashboards": t["dashboards"],
                                  "widgets": t["widgets"],
                                  "review": t["needs_review"],
                                  "failed_inputs": t["failed_inputs"]})
        except (StoreError, ValueError):
            pass
        store.close()
    if getattr(args, "json_out", False):
        # Machine-readable summary on stdout so an AI driving `--json`
        # gets the written dashboards, the cannot-migrate list and any
        # failures, not just the human recap on stderr.
        print(json.dumps({
            "out_dir": result["out_dir"],
            "report": result["report"],
            "dashboards": [
                {"slug": r["slug"], "output": r["output"],
                 "title": r["dashboard"], "uid": r["uid"],
                 "source": r["source"],
                 "source_dashboard": r["source_dashboard"],
                 "confidence": r["confidence"],
                 "datasources": r["datasources"],
                 "cannot_migrate": r["cannot_migrate"],
                 "needs_review": r["needs_review"],
                 "validation": r["validation"]}
                for r in result["dashboards"]],
            "failed_inputs": result["failed_inputs"],
            "totals": t,
        }, ensure_ascii=False))
    return 1 if had_error else 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """(Re)generate requirements + package dirs for converted Grafana
    output and/or raw NR dashboard JSON."""
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _err(str(e))
        return 2
    files = [f for f in _collect_dashboard_files(args.inputs)
             if os.path.basename(f) not in _ARTIFACT_NAMES]
    if not files:
        _err("no input files")
        return 2
    out = args.out
    if not out:
        first = args.inputs[0]
        out = first if os.path.isdir(first) \
            else (os.path.dirname(first) or ".")
        if os.path.isfile(os.path.join(out, "dashboard.json")):
            # a package dir itself: write next to it, not inside it
            out = os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(out, exist_ok=True)

    # Widget reports from migration-report.json next to the inputs (for
    # package dirs the report lives in the parent of the package dir).
    report_dirs = set()
    for f in files:
        d = os.path.dirname(os.path.abspath(f))
        report_dirs.add(d)
        if os.path.basename(f) == "dashboard.json":
            report_dirs.add(os.path.dirname(d))
    reports_by_name: Dict[str, List[Dict[str, Any]]] = {}
    for d in sorted(report_dirs):
        rp = os.path.join(d, "migration-report.json")
        if not os.path.isfile(rp):
            continue
        try:
            rep = _load_json(rp)
        except (json.JSONDecodeError, OSError):
            continue
        for entry in rep.get("reports", []):
            out_path = entry.get("output") or ""
            name = os.path.basename(out_path)
            if name == "dashboard.json":
                # packaged output: key by the package dir (slug)
                name = os.path.basename(os.path.dirname(out_path))
            if name:
                reports_by_name[name] = entry.get("widgets", [])

    store = _open_store_soft()
    entries: List[Dict[str, Any]] = []
    packaged = 0
    had_error = False
    for path in files:
        base = os.path.basename(path)
        try:
            data = _load_json(path)
        except (json.JSONDecodeError, OSError) as e:
            _err("%s: %s" % (path, e))
            had_error = True
            continue
        jobs: List[Tuple[str, Any, Dict[str, Any],
                         List[Dict[str, Any]]]] = []
        if isinstance(data, dict) and "panels" in data:
            if base == "dashboard.json":  # inside a package dir
                pkg_dir = os.path.dirname(os.path.abspath(path))
                slug = os.path.basename(pkg_dir) or "dashboard"
                report = reports_by_name.get(slug, [])
                if not report:
                    # fall back to the package's own widget-report.json
                    wr = os.path.join(pkg_dir, "widget-report.json")
                    if os.path.isfile(wr):
                        try:
                            loaded = _load_json(wr)
                            if isinstance(loaded, list):
                                report = loaded
                        except (json.JSONDecodeError, OSError):
                            pass
            else:
                slug = base[:-len(".json")] \
                    if base.endswith(".json") else base
                report = reports_by_name.get(base, [])
            jobs.append((slug, None, data, report))
        elif isinstance(data, dict) and "pages" in data:
            try:
                nr = parse_nr_dashboard(data)
                outputs = build_dashboards(nr, cfg)
            except Exception as e:
                _err("%s: conversion failed (%s: %s)"
                     % (path, type(e).__name__, e))
                had_error = True
                continue
            for filename, dash, report in outputs:
                stem = filename[:-len(".json")] \
                    if filename.endswith(".json") else filename
                jobs.append((stem, nr, dash, report))
        else:
            _err("%s: not a Grafana or New Relic dashboard JSON "
                 "(skipping)" % path)
            had_error = True
            continue
        for slug, nr, dash, report in jobs:
            try:
                req = analyze_dashboard(nr, dash, report, cfg)
                pkg = package_dashboard(out, slug, dash, report, req, cfg)
            except Exception as e:
                _err("%s: packaging failed (%s: %s)"
                     % (slug, type(e).__name__, e))
                had_error = True
                continue
            packaged += 1
            entries.append({
                "slug": slug, "title": dash.get("title"),
                "dir": pkg, "widget_report": report,
                "requirements": req,
            })
            print("%s -> %s  (%s)" % (base, pkg, summarize(req)),
                  file=sys.stderr)
            _persist_package(store, slug, path, nr, dash, report, req,
                             pkg)
    idx = write_index(out, entries)
    print("\n%d package(s) written. Index: %s" % (packaged, idx),
          file=sys.stderr)
    if store is not None:
        store.close()
    if not packaged:
        return 1
    return 1 if had_error else 0


def _grafana_conn(args: argparse.Namespace):
    url = getattr(args, "grafana_url", "") or os.environ.get("GRAFANA_URL", "")
    token = getattr(args, "grafana_token", "") \
        or os.environ.get("GRAFANA_TOKEN", "")
    return url, token, bool(getattr(args, "insecure", False))


def cmd_validate(args: argparse.Namespace) -> int:
    from . import pipeline
    url, token, insecure = _grafana_conn(args)
    live = bool(url or getattr(args, "test", False))
    try:
        result = pipeline.validate_dashboards(
            list(args.inputs), grafana_url=url, grafana_token=token,
            insecure=insecure, test=bool(getattr(args, "test", False)),
            datasource_overrides=list(getattr(args, "datasource", []) or []),
            log=_log_err if live else (lambda m: None))
    except pipeline.PipelineError as e:
        _err(str(e))
        return e.code
    for d in result["dashboards"]:
        errs = d["errors"]
        if len(errs) == 1 and errs[0].startswith("INVALID JSON"):
            print("%s: %s" % (d["file"], errs[0]))
            continue
        if errs:
            print("%s: %d problem(s)" % (d["file"], len(errs)))
            for e in errs:
                print("  - " + e)
        else:
            print("%s: OK" % d["file"])
        for w in d.get("warnings") or []:
            print("  ! " + w)
        if live and d.get("datasources"):
            for need in d["datasources"]:
                chosen = need.get("chosen") or {}
                print("  datasource %-10s %-8s %s" % (
                    need["type"], need.get("status", "?"),
                    ("-> %s (%s)" % (chosen.get("name"), chosen.get("uid")))
                    if chosen else need.get("fix", "")))
        if d.get("data_test"):
            print("  data test: " + ", ".join(
                "%d %s" % (v, k) for k, v in
                sorted(d["data_test"]["summary"].items())))
    t = result["totals"]
    print("\n%d dashboard(s) validated, %d with problems (%d error(s), %d "
          "warning(s))" % (t["dashboards"], t["failed"], t["errors"],
                           t["warnings"]), file=sys.stderr)
    if getattr(args, "json_out", False):
        print(json.dumps(result, ensure_ascii=False))
    return 1 if t["failed"] else 0


def cmd_import(args: argparse.Namespace) -> int:
    from . import pipeline
    files = list(getattr(args, "files", []) or [])
    key = ""
    if not files:
        key = args.api_key or os.environ.get("NEW_RELIC_API_KEY", "")
    try:
        result = pipeline.import_dashboards(
            args.out, api_key=key, region=(args.region or "US").upper(),
            guids=list(args.guid or []), name_filter=args.name or "",
            files=files, log=_log_err)
    except pipeline.PipelineError as e:
        _err(str(e))
        return e.code
    n = len(result["dashboards"])
    print("\n%d dashboard(s) imported to %s%s. Manifest: %s"
          % (n, result["out_dir"],
             (", %d failed" % len(result["failed"])) if result["failed"]
             else "", result["manifest"]), file=sys.stderr)
    print("Next: nr2grafana convert %s -o ./grafana-dashboards"
          % result["out_dir"], file=sys.stderr)
    if getattr(args, "json_out", False):
        print(json.dumps(result, ensure_ascii=False))
    if result["failed"]:
        return 1
    if n == 0:
        _err("nothing imported")
        return 1
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from . import pipeline
    url, token, insecure = _grafana_conn(args)
    try:
        result = pipeline.export_dashboards(
            list(args.inputs), grafana_url=url, grafana_token=token,
            insecure=insecure, folder=args.folder or "",
            overwrite=bool(args.overwrite),
            datasource_overrides=list(args.datasource or []),
            test=bool(args.test), allow_missing=bool(args.allow_missing),
            log=_log_err)
    except pipeline.PipelineError as e:
        _err(str(e))
        return e.code
    print("", file=sys.stderr)
    for d in result["dashboards"]:
        src = d.get("source") or {}
        if d.get("url") or d.get("verified"):
            state = "created" if d["ok"] else "created with problems"
            print("%s: %r  %s  (uid %s, folder %s)" % (
                state, d.get("title"), d.get("url") or "", d.get("uid"),
                d.get("folder") or "General"))
            print("    sourced from New Relic dashboard %r%s" % (
                src.get("name") or "?",
                (" (guid %s)" % src["guid"]) if src.get("guid") else ""))
            v = d.get("verified") or {}
            if v:
                print("    verified: %s (%d panels, version %s)" % (
                    "yes" if v.get("ok") else "NO",
                    v.get("panels") or 0, v.get("version")))
            if d.get("data_test"):
                print("    data test: " + ", ".join(
                    "%d %s" % (n, k) for k, n in
                    sorted(d["data_test"]["summary"].items())))
        else:
            print("not exported: %r (%s)" % (d.get("title") or d["file"],
                                             "; ".join(d["problems"])[:300]))
        for p in d.get("problems") or []:
            print("    problem: %s" % p)
    t = result["totals"]
    print("\n%d dashboard(s) created on %s, %d failed"
          % (t["created"], result["grafana_url"], t["failed"]),
          file=sys.stderr)
    if getattr(args, "json_out", False):
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def cmd_inspect(args: argparse.Namespace) -> int:
    from . import pipeline
    from .inspect import render_inspection_text
    try:
        models = pipeline.inspect_inputs(list(args.inputs),
                                         config_path=args.config or "")
    except pipeline.PipelineError as e:
        _err(str(e))
        return e.code
    if getattr(args, "json_out", False):
        print(json.dumps(models if len(models) > 1 else models[0],
                         ensure_ascii=False))
        return 0
    for m in models:
        if m.get("error"):
            print("%s: %s" % (m.get("source"), m["error"]))
            continue
        print(render_inspection_text(m))
        print()
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    from . import pipeline
    from .inspect import render_explanation_text
    nrql = " ".join(args.nrql).strip()
    if not nrql:
        _err("give the NRQL to explain, e.g. explain \"SELECT count(*) "
             "FROM Transaction TIMESERIES\"")
        return 2
    try:
        model = pipeline.explain(nrql, config_path=args.config or "")
    except pipeline.PipelineError as e:
        _err(str(e))
        return e.code
    if getattr(args, "json_out", False):
        print(json.dumps(model, ensure_ascii=False))
    else:
        print(render_explanation_text(model))
    return 0 if not model.get("parse_error") else 1


def cmd_example_config(args: argparse.Namespace) -> int:
    print(json.dumps(DEFAULT_CONFIG, indent=2))
    return 0


# ---------------------------------------------------------------------------
# grafana subcommands (live instance via service-account token)
# ---------------------------------------------------------------------------

def _collect_requirements_files(paths: List[str]) -> List[str]:
    files: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            direct = os.path.join(p, "requirements.json")
            if os.path.isfile(direct):
                files.append(direct)
                continue
            for name in sorted(os.listdir(p)):
                sub = os.path.join(p, name, "requirements.json")
                if os.path.isfile(sub):
                    files.append(sub)
        elif os.path.isfile(p):
            files.append(p)
        else:
            _err("no such file or directory: %s" % p)
    return files


def _collect_dashboard_files(paths: List[str]) -> List[str]:
    """Dashboard JSON files from package dirs, package parents, flat
    converted dirs, or explicit files."""
    files: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            direct = os.path.join(p, "dashboard.json")
            if os.path.isfile(direct):
                files.append(direct)
                continue
            found = False
            for name in sorted(os.listdir(p)):
                sub = os.path.join(p, name, "dashboard.json")
                if os.path.isfile(sub):
                    files.append(sub)
                    found = True
            if found:
                continue
            for name in sorted(os.listdir(p)):
                if name.endswith(".json") and name not in _ARTIFACT_NAMES:
                    files.append(os.path.join(p, name))
        elif os.path.isfile(p):
            files.append(p)
        else:
            _err("no such file or directory: %s" % p)
    return files


def _dashboard_slug(path: str) -> str:
    base = os.path.basename(path)
    if base == "dashboard.json":
        return os.path.basename(os.path.dirname(os.path.abspath(path)))
    return base[:-len(".json")] if base.endswith(".json") else base


def cmd_grafana_check(args: argparse.Namespace) -> int:
    files = _collect_requirements_files(args.inputs)
    if not files:
        _err("no requirements.json found -- run 'convert --package' or "
             "'analyze' first")
        return 2
    client = _grafana_live(args)
    bad = 0
    for path in files:
        try:
            req = _load_json(path)
        except (json.JSONDecodeError, OSError) as e:
            _err("%s: %s" % (path, e))
            bad += 1
            continue
        print("%s  (%s)" % (req.get("dashboard") or "(untitled)", path))
        try:
            rows = client.check_requirements(req)
        except GrafanaError as e:
            _err(str(e))
            return 1
        for row in rows:
            status = row.get("status", "?")
            print("  %-10s %s  %s" % (status, row.get("item", ""),
                                      row.get("detail", "")))
            if status != "ok" and row.get("fix"):
                print("             fix: %s" % row["fix"])
            if status in ("missing", "wrong-type"):
                bad += 1
    print()
    if bad:
        print("%d requirement(s) not met (see fixes above)" % bad,
              file=sys.stderr)
    else:
        print("all requirements met", file=sys.stderr)
    return 1 if bad else 0


def cmd_grafana_test(args: argparse.Namespace) -> int:
    files = _collect_dashboard_files(args.inputs)
    if not files:
        _err("no dashboard JSON files found")
        return 2
    client = _grafana_live(args)
    store = _open_store_soft()
    errors = nodata = data = 0
    for path in files:
        try:
            dash = _load_json(path)
        except (json.JSONDecodeError, OSError) as e:
            _err("%s: %s" % (path, e))
            errors += 1
            continue
        print("%s:" % (dash.get("title") or path), file=sys.stderr)
        try:
            results = client.test_dashboard(
                dash, log=lambda m: print(m, file=sys.stderr))
        except GrafanaError as e:
            _err(str(e))
            if store is not None:
                store.close()
            return 1
        counts: Dict[str, int] = {}
        for r in results:
            counts[r.get("status", "?")] = \
                counts.get(r.get("status", "?"), 0) + 1
        errors += counts.get("error", 0)
        nodata += counts.get("no-data", 0)
        data += counts.get("data", 0)
        payload = {"results": results, "summary": counts}
        base = os.path.basename(path)
        res_dir = os.path.dirname(os.path.abspath(path))
        if base == "dashboard.json":
            res_path = os.path.join(res_dir, "datatest-results.json")
        else:
            stem = base[:-len(".json")] if base.endswith(".json") else base
            res_path = os.path.join(res_dir,
                                    "%s.datatest-results.json" % stem)
        _write_json(res_path, payload)
        print("  results -> %s" % res_path, file=sys.stderr)
        if store is not None:
            try:
                store.save_artifact(_dashboard_slug(path), "datatest",
                                    payload)
            except (StoreError, ValueError):
                pass
    print("\n%d target(s) returned data, %d no-data (warning), %d "
          "error(s)" % (data, nodata, errors), file=sys.stderr)
    if store is not None:
        store.close()
    return 1 if errors else 0


def cmd_grafana_import(args: argparse.Namespace) -> int:
    files = _collect_dashboard_files(args.inputs)
    if not files:
        _err("no dashboard JSON files found")
        return 2
    client = _grafana_live(args)
    try:
        info = client.health()
        print("connected -- Grafana %s" % info.get("version", "?"),
              file=sys.stderr)
    except GrafanaError as e:
        _err("cannot connect: %s" % e)
        return 1
    folder_uid = ""
    if args.folder:
        try:
            folder_uid = client.find_or_create_folder(args.folder)
        except GrafanaError as e:
            _err("folder error: %s" % e)
            return 1
    ok = 0
    problems: List[str] = []
    for path in files:
        try:
            dash = _load_json(path)
        except (json.JSONDecodeError, OSError) as e:
            _err("%s: %s" % (path, e))
            problems.append(path)
            continue
        if not isinstance(dash, dict) or "panels" not in dash:
            print("  skip %s (not a dashboard)" % path, file=sys.stderr)
            continue
        name = dash.get("title") or os.path.basename(path)
        try:
            res = client.import_dashboard(dash, folder_uid=folder_uid,
                                          overwrite=args.overwrite)
            ok += 1
            print("  ok   %s  %s" % (name, res.get("url", "")))
        except GrafanaError as e:
            msg = str(e)
            hint = ""
            if "name-exists" in msg or "version-mismatch" in msg:
                hint = " (already exists -- re-run with --overwrite to " \
                       "replace)"
            problems.append(name)
            print("  FAIL %s: %s%s" % (name, msg, hint))
    print("\n%d imported, %d failed" % (ok, len(problems)),
          file=sys.stderr)
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# grafana parity / diagnose / heal / datasources
# ---------------------------------------------------------------------------

def _sidecar_path(dash_path: str, package_name: str, flat_suffix: str) \
        -> str:
    """Path for a per-dashboard result file: <pkg>/<package_name> for
    package dirs, <stem>.<flat_suffix> next to flat dashboard files."""
    base = os.path.basename(dash_path)
    d = os.path.dirname(os.path.abspath(dash_path))
    if base == "dashboard.json":
        return os.path.join(d, package_name)
    stem = base[:-len(".json")] if base.endswith(".json") else base
    return os.path.join(d, "%s.%s" % (stem, flat_suffix))


def _load_json_soft(path: str) -> Any:
    """Load JSON, returning None when absent or unreadable."""
    if not os.path.isfile(path):
        return None
    try:
        return _load_json(path)
    except (json.JSONDecodeError, OSError):
        return None


def _package_dir_of(dash_path: str) -> str:
    """The package dir of a dashboard.json path, '' for flat files."""
    if os.path.basename(dash_path) == "dashboard.json":
        return os.path.dirname(os.path.abspath(dash_path))
    return ""


def _widget_report_for(dash_path: str) -> List[Dict[str, Any]]:
    """Best-effort widget report for a dashboard file: the package's
    widget-report.json, else the migration-report.json entry next to
    the file (or next to the package dir)."""
    base = os.path.basename(dash_path)
    d = os.path.dirname(os.path.abspath(dash_path))
    if base == "dashboard.json":
        loaded = _load_json_soft(os.path.join(d, "widget-report.json"))
        if isinstance(loaded, list):
            return loaded
        rep_dir, key = os.path.dirname(d), os.path.basename(d)
    else:
        rep_dir, key = d, base
    rep = _load_json_soft(os.path.join(rep_dir,
                                       "migration-report.json"))
    if isinstance(rep, dict):
        for entry in rep.get("reports") or []:
            out = entry.get("output") or ""
            name = os.path.basename(out)
            if name == "dashboard.json":
                name = os.path.basename(os.path.dirname(out))
            if name == key:
                widgets = entry.get("widgets")
                if isinstance(widgets, list):
                    return widgets
    return []


def _nr_account_ids(args: argparse.Namespace,
                    widget_report: List[Dict[str, Any]]) -> List[int]:
    """NR account ids for parity: --account-id flags first, then the
    NEW_RELIC_ACCOUNT_ID env var (comma-separated), then any ids
    recorded in the widget report."""
    raw: List[str] = list(getattr(args, "account_id", []) or [])
    if not raw:
        env = os.environ.get("NEW_RELIC_ACCOUNT_ID", "")
        raw = [x for x in env.split(",") if x.strip()]
    ids: List[int] = []
    for v in raw:
        try:
            ids.append(int(str(v).strip()))
        except ValueError:
            _err("ignoring non-numeric account id %r" % v)
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


def _save_artifact_soft(store: Optional[Store], slug: str, kind: str,
                        data: Dict[str, Any]) -> None:
    if store is None:
        return
    try:
        store.save_artifact(slug, kind, data)
    except (StoreError, ValueError):
        pass


def cmd_grafana_parity(args: argparse.Namespace) -> int:
    """Compare real data: original NRQL via NerdGraph vs the translated
    query via Grafana, per panel target. Exit 1 only on gf-error
    panels (the translated query itself failed)."""
    files = _collect_dashboard_files(args.inputs)
    if not files:
        _err("no dashboard JSON files found")
        return 2
    client = _grafana_live(args)
    nr = NerdGraphClient(_api_key(args), region=args.region)
    store = _open_store_soft()
    gf_errors = 0
    for path in files:
        try:
            dash = _load_json(path)
        except (json.JSONDecodeError, OSError) as e:
            _err("%s: %s" % (path, e))
            gf_errors += 1
            continue
        report_widgets = _widget_report_for(path)
        aids = _nr_account_ids(args, report_widgets)
        if not aids:
            try:
                aids = nr.list_account_ids()
            except NerdGraphError:
                aids = []
            if aids:
                print("note: no account id recorded; trying the "
                      "key's %d visible account(s): %s"
                      % (len(aids), ", ".join(map(str, aids))),
                      file=sys.stderr)
        if not aids:
            print("note: no New Relic account id known -- pass "
                  "--account-id or set NEW_RELIC_ACCOUNT_ID (NR side "
                  "will be skipped)", file=sys.stderr)
        print("%s:" % (dash.get("title") or path), file=sys.stderr)
        try:
            par = run_parity(nr, aids, client, dash, report_widgets,
                             frm=args.frm, to=args.to,
                             log=lambda m: print(m, file=sys.stderr))
        except GrafanaError as e:
            _err(str(e))
            if store is not None:
                store.close()
            return 1
        res_path = _sidecar_path(path, "parity-results.json",
                                 "parity-results.json")
        _write_json(res_path, par)
        print("  results -> %s" % res_path, file=sys.stderr)
        _save_artifact_soft(store, _dashboard_slug(path), "parity", par)

        for row in par.get("panels") or []:
            print("  %-15s [%s] %-32s %s"
                  % (row.get("verdict", "?"), row.get("refId", "?"),
                     (row.get("panel_title") or "")[:32],
                     row.get("detail", "")))
        summary = par.get("summary") or {}
        print("\nscore %s/100  (%s)"
              % (par.get("score"),
                 ", ".join("%d %s" % (v, k)
                           for k, v in sorted(summary.items()))
                 or "no panels compared"))

        pkg_dir = _package_dir_of(path)
        check_rows = None
        req = _load_json_soft(os.path.join(pkg_dir,
                                           "requirements.json")) \
            if pkg_dir else None
        if isinstance(req, dict):
            try:
                check_rows = client.check_requirements(req)
            except GrafanaError:
                check_rows = None
        tests = _load_json_soft(_sidecar_path(
            path, "datatest-results.json", "datatest-results.json"))
        test_rows = tests.get("results") if isinstance(tests, dict) \
            else None
        ready = readiness(par, check_rows=check_rows,
                          test_rows=test_rows)
        print("readiness: %s (%s/100)" % (ready.get("grade"),
                                          ready.get("score")))
        for reason in ready.get("reasons") or []:
            print("  - %s" % reason)
        gf_errors += int(summary.get("gf-error") or 0)
    if store is not None:
        store.close()
    return 1 if gf_errors else 0


def cmd_grafana_samples(args: argparse.Namespace) -> int:
    """Pull raw samples from both sides for human review: NR events/
    rows via NerdGraph vs Loki log lines / Prometheus datapoints via
    /api/ds/query. Writes samples.json next to each dashboard."""
    files = _collect_dashboard_files(args.inputs)
    if not files:
        _err("no dashboard JSON files found")
        return 2
    client = _grafana_live(args)
    nr = _optional_nerdgraph(args)
    if nr is None:
        print("note: no New Relic API key -- the NR side of each "
              "sample will be skipped (pass --api-key or set "
              "NEW_RELIC_API_KEY)", file=sys.stderr)
    store = _open_store_soft()
    rc = 0
    for path in files:
        try:
            dash = _load_json(path)
        except (json.JSONDecodeError, OSError) as e:
            _err("%s: %s" % (path, e))
            rc = 1
            continue
        report_widgets = _widget_report_for(path)
        aids = _nr_account_ids(args, report_widgets)
        if nr is not None and not aids:
            try:
                aids = nr.list_account_ids()
            except NerdGraphError:
                aids = []
        print("%s:" % (dash.get("title") or path), file=sys.stderr)
        try:
            rep = collect_samples(nr, aids, client, dash,
                                  report_widgets, frm=args.frm,
                                  to=args.to, limit=args.limit,
                                  log=lambda m: print(m,
                                                      file=sys.stderr))
        except GrafanaError as e:
            _err(str(e))
            if store is not None:
                store.close()
            return 1
        res_path = _sidecar_path(path, "samples.json", "samples.json")
        _write_json(res_path, rep)
        print("  samples -> %s" % res_path, file=sys.stderr)
        slug = _dashboard_slug(path)
        _save_artifact_soft(store, slug, "samples", rep)

        for row in rep.get("panels") or []:
            nrs = row.get("nr") or {}
            gfs = row.get("grafana") or {}
            print("  %-32s [%s]  NR:%s(%d)  Grafana:%s(%d)"
                  % ((row.get("panel_title") or "(untitled)")[:32],
                     row.get("refId") or "?",
                     nrs.get("kind", "?"),
                     len(nrs.get("samples") or []),
                     gfs.get("kind", "?"),
                     len(gfs.get("samples") or [])))
            preview = _sample_preview(nrs)
            if preview:
                print("      nr> %s" % preview)
            preview = _sample_preview(gfs)
            if preview:
                print("      gf> %s" % preview)
        if store is not None:
            summ = review_summary(store, slug)
            reviewed = (summ.get("confirmed", 0)
                        + summ.get("rejected", 0)
                        + summ.get("unsure", 0))
            if reviewed:
                print("review: %d confirmed, %d rejected, %d unsure, "
                      "%s unreviewed"
                      % (summ.get("confirmed", 0),
                         summ.get("rejected", 0),
                         summ.get("unsure", 0),
                         summ.get("unreviewed", "?")))
            else:
                print("review: none yet -- confirm or reject each "
                      "panel in the web UI (Panels tab) so readiness "
                      "can report human sign-off", file=sys.stderr)
    if store is not None:
        store.close()
    return rc


def _sample_preview(side: Dict[str, Any]) -> str:
    """One-line preview of a sample side for terminal output."""
    kind = side.get("kind", "")
    samples = side.get("samples") or []
    if kind == "error":
        return "ERROR: %s" % (side.get("error") or "")[:90]
    if not samples:
        return ""
    first = samples[0]
    if kind == "logs":
        return str(first.get("line", ""))[:90]
    if kind == "events":
        msg = first.get("message")
        if msg is not None:
            return str(msg)[:90]
        return ", ".join("%s=%s" % (k, v)
                         for k, v in list(first.items())[:4])[:90]
    if kind == "points":
        pts = first.get("points") or []
        return "%d point(s), last %s" % (
            len(pts), pts[-1][1] if pts else "?")
    return str(first)[:90]


def _optional_nerdgraph(args: argparse.Namespace) \
        -> Optional[NerdGraphClient]:
    """A NerdGraph client when a key is available, else None (the
    NR-side checks simply degrade)."""
    key = getattr(args, "api_key", "") \
        or os.environ.get("NEW_RELIC_API_KEY", "")
    if not key:
        return None
    return NerdGraphClient(key, region=getattr(args, "region", "US"))


def cmd_grafana_diagnose(args: argparse.Namespace) -> int:
    """Root-cause every failure layer by layer; exit 1 on blockers."""
    files = _collect_dashboard_files(args.inputs)
    if not files:
        _err("no dashboard JSON files found")
        return 2
    client = _grafana_live(args)
    nr = _optional_nerdgraph(args)
    cfg: Dict[str, Any] = {}
    try:
        cfg = load_config(getattr(args, "config", "") or "")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _err(str(e))
        return 2
    store = _open_store_soft()
    blockers = 0
    for path in files:
        try:
            dash = _load_json(path)
        except (json.JSONDecodeError, OSError) as e:
            _err("%s: %s" % (path, e))
            blockers += 1
            continue
        pkg_dir = _package_dir_of(path)
        req = _load_json_soft(os.path.join(
            pkg_dir, "requirements.json")) if pkg_dir else None
        tests = _load_json_soft(_sidecar_path(
            path, "datatest-results.json", "datatest-results.json"))
        test_rows = tests.get("results") if isinstance(tests, dict) \
            else None
        par = _load_json_soft(_sidecar_path(
            path, "parity-results.json", "parity-results.json"))
        print("%s:" % (dash.get("title") or path), file=sys.stderr)
        diag = diagnose(client, nr=nr, dash=dash, requirements=req,
                        test_results=test_rows, parity=par, cfg=cfg,
                        log=lambda m: print(m, file=sys.stderr))
        res_path = _sidecar_path(path, "diagnosis.json",
                                 "diagnosis.json")
        _write_json(res_path, diag)
        print("  diagnosis -> %s" % res_path, file=sys.stderr)
        _save_artifact_soft(store, _dashboard_slug(path), "diagnosis",
                            diag)

        findings = diag.get("findings") or []
        if not findings:
            print("no problems found")
        for f in findings:
            where = ""
            if f.get("panel_id") is not None:
                where = " panel %s:" % f["panel_id"]
            print("  %-8s %-11s%s %s"
                  % (f.get("severity", "?"), f.get("area", ""),
                     where, f.get("problem", "")))
            fix = f.get("fix") or {}
            if fix.get("description"):
                print("           fix (%s): %s"
                      % (fix.get("kind", "none"), fix["description"]))
        blockers += int((diag.get("summary") or {}).get("blocker")
                        or 0)
    if store is not None:
        store.close()
    if blockers:
        print("\n%d blocker(s) found (see fixes above)" % blockers,
              file=sys.stderr)
    return 1 if blockers else 0


def cmd_grafana_heal(args: argparse.Namespace) -> int:
    """Auto-heal: test -> diagnose -> apply safe fixes -> re-test."""
    files = _collect_dashboard_files(args.inputs)
    if not files:
        _err("no dashboard JSON files found")
        return 2
    client = _grafana_live(args)
    nr = _optional_nerdgraph(args)
    store = _open_store_soft()
    clog = ChangeLog(store) if store is not None else None
    rc = 0
    for path in files:
        try:
            dash = _load_json(path)
        except (json.JSONDecodeError, OSError) as e:
            _err("%s: %s" % (path, e))
            rc = 1
            continue
        pkg_dir = _package_dir_of(path)
        req = _load_json_soft(os.path.join(
            pkg_dir, "requirements.json")) if pkg_dir else None
        slug = _dashboard_slug(path)
        print("%s:" % (dash.get("title") or path), file=sys.stderr)
        result = auto_heal(client, nr, dash, _widget_report_for(path),
                           req or {}, slug, pkg_dir, changelog=clog,
                           max_rounds=getattr(args, "max_rounds", 3),
                           log=lambda m: print(m, file=sys.stderr),
                           push=bool(getattr(args, "push", False)))
        for rnd in result.get("rounds") or []:
            tests_str = ", ".join(
                "%d %s" % (v, k)
                for k, v in sorted((rnd.get("tests") or {}).items())) \
                or "no targets"
            print("round %d: %s; %d finding(s), %d fixed"
                  % (rnd.get("round", 0), tests_str,
                     rnd.get("findings", 0), rnd.get("fixed", 0)))
        print("%d fix(es) applied, %d finding(s) remaining%s"
              % (result.get("fixed", 0),
                 len(result.get("remaining_findings") or []),
                 " (converged)" if result.get("converged") else ""))
        if result.get("fixed") and not pkg_dir:
            # flat file: apply_fix only edited the in-memory dash
            _write_json(path, dash)
            print("  updated %s" % path, file=sys.stderr)
        _save_artifact_soft(store, slug, "heal", result)
        if result.get("error"):
            _err(result["error"])
            rc = 1
    if store is not None:
        store.close()
    return rc


def cmd_grafana_datasources(args: argparse.Namespace) -> int:
    """List the instance's datasources with a live health check."""
    client = _grafana_live(args)
    try:
        dss = client.datasources()
    except GrafanaError as e:
        _err(str(e))
        return 1
    if not dss:
        print("no datasources configured", file=sys.stderr)
        return 0
    print("%-10s %-24s %-28s %-8s %s"
          % ("HEALTH", "NAME", "TYPE", "DEFAULT", "UID"))
    for ds in dss:
        uid = ds.get("uid") or ""
        health = {"status": "unknown", "message": ""}
        if uid:
            health = client.datasource_health(uid)
        print("%-10s %-24s %-28s %-8s %s"
              % (health.get("status", "?"), ds.get("name", ""),
                 ds.get("type", ""),
                 "yes" if ds.get("isDefault") else "",
                 uid))
        if health.get("status") not in ("ok", "unknown") \
                and health.get("message"):
            print("           %s" % health["message"])
    return 0


def cmd_grafana_add_datasource(args: argparse.Namespace) -> int:
    """Create a datasource from a DS_TEMPLATES form spec."""
    tpl = DS_TEMPLATES.get(args.type)
    if tpl is None:
        _err("unknown datasource type %r (known: %s)"
             % (args.type, ", ".join(sorted(DS_TEMPLATES))))
        return 2
    values: Dict[str, str] = {}
    for spec in getattr(args, "set_values", []) or []:
        if "=" not in spec:
            _err("--set expects field=value, got %r" % spec)
            return 2
        key, val = spec.split("=", 1)
        values[key.strip()] = val
    if sys.stdin.isatty() and not getattr(args, "no_prompt", False):
        import getpass
        for field in tpl["fields"]:
            if field.get("secret") and not values.get(field["name"]):
                val = getpass.getpass(
                    "%s (%s, blank to skip): "
                    % (field.get("label", field["name"]),
                       field["name"]))
                if val:
                    values[field["name"]] = val
    try:
        payload = build_datasource_payload(args.type, args.name,
                                           values)
    except GrafanaError as e:
        _err(str(e))
        print("fields for %s:" % args.type, file=sys.stderr)
        for field in tpl["fields"]:
            print("  --set %s=...  %s%s" % (
                field["name"], field.get("label", ""),
                " (required)" if field.get("required") else ""),
                file=sys.stderr)
        if tpl.get("notes"):
            print("note: %s" % tpl["notes"], file=sys.stderr)
        return 2
    client = _grafana_live(args)
    try:
        resp = client.create_datasource(payload)
    except GrafanaError as e:
        _err("creating datasource failed: %s (an Admin service-"
             "account token is required)" % e)
        return 1
    created = resp.get("datasource") if isinstance(resp, dict) else None
    if not isinstance(created, dict):
        created = resp if isinstance(resp, dict) else {}
    uid = created.get("uid") or ""
    health = {"status": "unknown", "message": "no uid returned"}
    if uid:
        health = client.datasource_health(uid)
    print("created datasource %r (type %s, uid %s)"
          % (args.name, tpl["plugin_id"], uid or "?"))
    print("health: %s%s"
          % (health.get("status", "?"),
             " -- " + health["message"] if health.get("message")
             else ""))
    if tpl.get("notes"):
        print("note: %s" % tpl["notes"], file=sys.stderr)
    return 1 if health.get("status") == "error" else 0


# ---------------------------------------------------------------------------
# changes subcommands
# ---------------------------------------------------------------------------

def cmd_changes_report(args: argparse.Namespace) -> int:
    try:
        store = Store(_db_path())
    except Exception as e:
        _err("cannot open local store: %s" % e)
        return 1
    with store:
        log = ChangeLog(store)
        if args.markdown:
            print(log.report_markdown(args.slug))
        else:
            print(json.dumps(log.report(args.slug), indent=2))
    return 0


def cmd_changes_suggest(args: argparse.Namespace) -> int:
    try:
        store = Store(_db_path())
    except Exception as e:
        _err("cannot open local store: %s" % e)
        return 1
    with store:
        suggestion = ChangeLog(store).suggest_config(args.slug)
    print(json.dumps(suggestion, indent=2))
    if not suggestion.get("overlay"):
        print("no codifiable changes recorded yet (edit queries via the "
              "web UI or record changes first)", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# cost & efficiency optimization (1.5)
# ---------------------------------------------------------------------------

# optimize.recommend config target -> file name written under config/.
_COST_CONFIG_FILES = {
    "promtail": "promtail.yaml",
    "alloy": "alloy.river",
    "otel-collector": "otel-collector.yaml",
    "loki-limits": "loki-limits.yaml",
    "prometheus-relabel": "prometheus-relabel.yaml",
    "mimir-limits": "mimir-limits.yaml",
}

# mitigate.plan config target -> file name written under config/. Each
# reliability-safe mitigation carries GENERIC, paste-ready snippets
# (Mimir/Loki zone-aware, Service trafficDistribution, Karpenter, NLB).
_MITIGATION_CONFIG_FILES = {
    "mimir": "mimir-zone-aware.yaml",
    "loki": "loki-zone-aware.yaml",
    "k8s-service": "service-trafficdistribution.yaml",
    "service": "service-trafficdistribution.yaml",
    "trafficdistribution": "service-trafficdistribution.yaml",
    "karpenter": "karpenter-nodepool.yaml",
    "nlb": "nlb-crosszone.yaml",
}


def _cost_ds_list(client: GrafanaLive) -> List[Dict[str, Any]]:
    """[{"family","uid","type"}] for every prometheus/loki/tempo
    datasource in the instance (input to traffic.sample_traffic)."""
    fam = {"prometheus": "prometheus", "loki": "loki", "tempo": "tempo"}
    out: List[Dict[str, Any]] = []
    for ds in client.datasources() or []:
        uid = ds.get("uid") or ""
        family = fam.get((ds.get("type") or "").lower())
        if uid and family:
            out.append({"family": family, "uid": uid,
                        "type": ds.get("type") or family})
    return out


def _cost_usage_inputs(args: argparse.Namespace) \
        -> Tuple[List[Dict[str, Any]], List[List[Dict[str, Any]]]]:
    """Collect (dashboards, widget_reports) for usage.collect_usage.
    Uses the converted Grafana dashboards named on the command line;
    with no inputs, falls back to every dashboard in the local store."""
    dashboards: List[Dict[str, Any]] = []
    reports: List[List[Dict[str, Any]]] = []
    inputs = list(getattr(args, "inputs", []) or [])
    if inputs:
        from .model import parse_nr_dashboard
        for path in _collect_dashboard_files(inputs):
            if os.path.basename(path) in _ARTIFACT_NAMES:
                continue
            data = _load_json_soft(path)
            if isinstance(data, dict) and "panels" in data:
                dashboards.append(data)
                reports.append(_widget_report_for(path))
            elif isinstance(data, dict) and "pages" in data:
                try:
                    nr = parse_nr_dashboard(data)
                    cfg = load_config("")
                    for _fn, dash, report in build_dashboards(nr, cfg):
                        dashboards.append(dash)
                        reports.append(report)
                except Exception as e:
                    _err("%s: could not convert for usage (%s)"
                         % (path, e))
        return dashboards, reports
    store = _open_store_soft()
    if store is not None:
        with store:
            for row in store.list_dashboards():
                slug = row.get("slug", "")
                full = store.get_dashboard(slug) if slug else None
                data = (full or {}).get("data") if full else None
                if isinstance(data, dict) and "panels" in data:
                    dashboards.append(data)
                    wr = store.get_artifact(slug, "widget-report") or {}
                    reports.append(wr.get("widgets", []))
    return dashboards, reports


def _load_pricing(args: argparse.Namespace, costmodel) -> Dict[str, Any]:
    """Effective pricing: costmodel defaults overlaid by a --pricing
    JSON file when given."""
    pricing = dict(getattr(costmodel, "DEFAULT_PRICING", {}) or {})
    path = getattr(args, "pricing", "") or ""
    if path:
        loaded = _load_json(path)
        if isinstance(loaded, dict):
            pricing.update(loaded)
    return pricing


def _write_cost_config(out_dir: str, optimize: Dict[str, Any]) -> str:
    """Write each recommendation's config snippet into out_dir/config/
    grouped by target, plus a README. Returns the config dir path."""
    cfg_dir = os.path.join(out_dir, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    buckets: Dict[str, List[str]] = {}
    readme = ["# nr2grafana cost-optimization config", "",
              "Paste-ready snippets to cut LGTM-stack cost. Every one is",
              "safe: nothing a migrated dashboard uses is ever dropped.",
              "Apply at the collector/agent where possible (cheapest).",
              ""]
    for rec in optimize.get("recommendations") or []:
        title = rec.get("title") or rec.get("id") or "recommendation"
        safe = "safe" if rec.get("keeps_intact") else "REVIEW"
        readme.append("- [%s] %s (%s)"
                      % (rec.get("severity", "?"), title, safe))
        for cfg in rec.get("config") or []:
            snippet = cfg.get("snippet") or ""
            if not snippet.strip():
                continue
            target = cfg.get("target") or "other"
            fname = _COST_CONFIG_FILES.get(target, "%s.yaml" % target)
            header = "# --- %s ---" % title
            if cfg.get("note"):
                header += "\n# %s" % cfg["note"]
            buckets.setdefault(fname, []).append(
                header + "\n" + snippet.rstrip() + "\n")
    for fname, parts in sorted(buckets.items()):
        with open(os.path.join(cfg_dir, fname), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(parts))
    with open(os.path.join(cfg_dir, "README.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(readme) + "\n")
    return cfg_dir


def cmd_cost_analyze(args: argparse.Namespace) -> int:
    """Sample datasource traffic, subtract what the migrated dashboards
    need, and print safe, dollar-estimated savings recommendations."""
    client = _grafana_live(args)
    try:
        import importlib
        traffic_mod = importlib.import_module("nr2grafana.traffic")
        usage_mod = importlib.import_module("nr2grafana.usage")
        costmodel_mod = importlib.import_module("nr2grafana.costmodel")
        optimize_mod = importlib.import_module("nr2grafana.optimize")
    except Exception as e:  # pragma: no cover - built by sibling agents
        _err("cost analysis is unavailable: %s" % e)
        return 2
    ds_list = _cost_ds_list(client)
    if not ds_list:
        _err("no Loki/Prometheus/Tempo datasources found to sample")
        return 1
    log = lambda m: print(m, file=sys.stderr)
    print("sampling traffic from %d datasource(s) (%s .. %s)..."
          % (len(ds_list), args.frm, args.to), file=sys.stderr)
    try:
        traffic = traffic_mod.sample_traffic(client, ds_list,
                                             frm=args.frm, to=args.to,
                                             log=log)
    except GrafanaError as e:
        _err(str(e))
        return 1
    dashboards, reports = _cost_usage_inputs(args)
    print("analyzing what %d dashboard(s) need..." % len(dashboards),
          file=sys.stderr)
    usage = usage_mod.collect_usage(dashboards, reports)
    try:
        pricing = _load_pricing(args, costmodel_mod)
    except (json.JSONDecodeError, OSError) as e:
        _err("pricing file: %s" % e)
        return 2
    cost = costmodel_mod.estimate_costs(traffic, pricing)
    optimize = optimize_mod.recommend(traffic, usage, cost=cost,
                                      pricing=pricing, log=log)
    recs = optimize.get("recommendations") or []
    savings = costmodel_mod.apply_savings(cost, recs)

    print()
    print("%-40s %-11s %-14s %s"
          % ("RECOMMENDATION", "FAMILY", "EST $/MO SAVED", "SAFE?"))
    for rec in recs:
        est = rec.get("est_savings") or {}
        usd = est.get("monthly_usd")
        usd_str = ("$%.2f" % usd) if isinstance(usd, (int, float)) \
            else "-"
        safe = "safe" if rec.get("keeps_intact") else "review"
        title = (rec.get("title") or rec.get("id") or "")[:40]
        print("%-40s %-11s %-14s %s"
              % (title, rec.get("family", "?"), usd_str, safe))
    if not recs:
        print("no savings recommendations -- your usage matches what "
              "your dashboards need")
    print()
    print("estimated monthly cost (based on your pricing inputs):")
    print("  current:   $%.2f" % cost.get("monthly_total", 0.0))
    print("  projected: $%.2f" % savings.get("projected_total", 0.0))
    print("  saved:     $%.2f  (%s%%)"
          % (savings.get("saved_total", 0.0),
             savings.get("saved_pct", 0)))
    print(dim_note())

    out_dir = args.out or "."
    os.makedirs(out_dir, exist_ok=True)
    report = {"schema": "nr2grafana/cost-report/v1",
              "traffic": traffic, "usage": usage, "cost": cost,
              "optimize": optimize, "savings": savings}
    report_path = os.path.join(out_dir, "cost-report.json")
    _write_json(report_path, report)
    cfg_dir = _write_cost_config(out_dir, optimize)
    print("report -> %s" % report_path, file=sys.stderr)
    print("config -> %s" % cfg_dir, file=sys.stderr)
    return 0


def dim_note() -> str:
    return ("note: all costs are ESTIMATES based on your pricing "
            "inputs, not exact bills.")


def cmd_cost_pricing(args: argparse.Namespace) -> int:
    """Print (or edit via --set key=value) the pricing assumptions.
    Overrides persist in the local store as the non-secret setting
    web.pricing, shared with the web UI."""
    try:
        import importlib
        costmodel_mod = importlib.import_module("nr2grafana.costmodel")
    except Exception as e:  # pragma: no cover - built by sibling agents
        _err("cost model unavailable: %s" % e)
        return 2
    pricing = dict(getattr(costmodel_mod, "DEFAULT_PRICING", {}) or {})
    store = _open_store_soft()
    if store is not None:
        saved = store.get_setting("web.pricing", None)
        if isinstance(saved, dict):
            pricing.update(saved)
    sets = getattr(args, "set_values", []) or []
    if sets:
        overrides: Dict[str, Any] = {}
        for spec in sets:
            if "=" not in spec:
                _err("--set expects key=value, got %r" % spec)
                if store is not None:
                    store.close()
                return 2
            key, val = spec.split("=", 1)
            key = key.strip()
            try:
                num: Any = int(val)
            except ValueError:
                try:
                    num = float(val)
                except ValueError:
                    num = val.strip()
            overrides[key] = num
        pricing.update(overrides)
        if store is not None:
            merged = {}
            existing = store.get_setting("web.pricing", None)
            if isinstance(existing, dict):
                merged.update(existing)
            merged.update(overrides)
            try:
                store.set_setting("web.pricing", merged)
                print("updated %d assumption(s) in the local store"
                      % len(overrides), file=sys.stderr)
            except (StoreError, ValueError) as e:
                _err("could not persist pricing: %s" % e)
    print(json.dumps(pricing, indent=2, sort_keys=True))
    print(dim_note(), file=sys.stderr)
    if store is not None:
        store.close()
    return 0


# ---------------------------------------------------------------------------
# cost-anomaly root-cause analysis + reliability-safe mitigation (1.9)
# ---------------------------------------------------------------------------
# ALL AWS access here is strictly READ-ONLY (awscost allow-list). The tool
# PROPOSES mitigations only -- it never applies AWS/K8s changes.

def _call_filtered(fn, *args, **kwargs):
    """Call ``fn`` passing only the kwargs its signature accepts (so a
    sibling module built in parallel with a slightly narrower signature
    still works). A **kwargs sink means everything is forwarded."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in params}
    return fn(*args, **filtered)


def _pct(value: Any) -> str:
    """Format a share as a percentage. Accepts 0..1 fractions or 0..100
    percentages (a value <= 1 is treated as a fraction)."""
    if not isinstance(value, (int, float)):
        return "-"
    pct = value * 100.0 if 0 < value <= 1 else float(value)
    return "%.0f%%" % pct


def _num(d: Dict[str, Any], *keys: str) -> Optional[float]:
    """First numeric value among ``keys`` in ``d`` (nested est_savings
    dicts are searched too when a key is given as 'est_savings.usd')."""
    for key in keys:
        cur: Any = d
        ok = True
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and isinstance(cur, (int, float)):
            return float(cur)
    return None


def _parse_report(rca_mod, raw: Any) -> Dict[str, Any]:
    """rca.parse_anomaly_report accepts a CE GetAnomalies dict OR a pasted
    human report string. Parse JSON when the raw text is JSON, else pass
    the string through."""
    parse = getattr(rca_mod, "parse_anomaly_report", None)
    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            payload = raw
    if parse is None:
        return payload if isinstance(payload, dict) else {"raw": raw}
    return parse(payload)


def _rca_anomaly(args: argparse.Namespace, rca_mod, aws) \
        -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve the anomaly to analyze from exactly one source: an
    anomaly file, a pasted report (stdin), or an AWS Cost Anomaly
    Detection AnomalyId (read-only ce get-anomalies). Returns
    (anomaly, error_message)."""
    chosen = [bool(getattr(args, "anomaly_file", "")),
              bool(getattr(args, "anomaly_id", "")),
              bool(getattr(args, "paste", False))]
    if sum(chosen) == 0:
        return None, ("choose an anomaly source: --anomaly-file F, "
                      "--anomaly-id ID, or --paste")
    if getattr(args, "anomaly_file", ""):
        try:
            with open(args.anomaly_file, encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            return None, "cannot read anomaly file: %s" % e
        return _parse_report(rca_mod, raw), None
    if getattr(args, "paste", False):
        if sys.stdin.isatty():
            print("Paste the anomaly report, then press Ctrl-D:",
                  file=sys.stderr)
        raw = sys.stdin.read()
        if not raw.strip():
            return None, "no anomaly report provided on stdin"
        return _parse_report(rca_mod, raw), None
    # --anomaly-id: needs read-only AWS access
    if aws is None:
        return None, ("--anomaly-id needs AWS access (ce get-anomalies) "
                      "but the aws CLI is not available -- use "
                      "--anomaly-file or --paste instead")
    import datetime
    days = max(1, int(getattr(args, "days", 60) or 60))
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    try:
        anomalies = aws.get_anomalies(start.isoformat(), end.isoformat())
    except Exception as e:
        return None, ("ce get-anomalies failed: %s -- check your read-only "
                      "AWS credentials" % e)
    for a in anomalies or []:
        if str(a.get("AnomalyId")) == str(args.anomaly_id):
            return _parse_report(rca_mod, a), None
    return None, ("anomaly id %r not found in the last %d day(s) of Cost "
                  "Anomaly Detection results" % (args.anomaly_id, days))


def _print_rca(rca: Dict[str, Any]) -> None:
    """Terminal breakdown of an rca.json: incident, dominant/secondary
    cause with %, evidence, ruled-out, evidence convergence, confidence."""
    inc = rca.get("incident") or {}
    cause = rca.get("cause") or {}
    dom = cause.get("dominant") or {}
    print()
    print("== Cost anomaly root-cause analysis ==")
    loc = []
    if inc.get("account"):
        loc.append("account %s" % inc["account"])
    if inc.get("region"):
        loc.append(str(inc["region"]))
    print("incident: %s on %s%s"
          % (inc.get("usage_type") or "?", inc.get("service") or "?",
             (" (" + " / ".join(loc) + ")") if loc else ""))
    usd = _num(inc, "usd_per_day", "dollars_per_day", "per_day_usd")
    gb = _num(inc, "gb_per_day", "gb_day")
    metrics = []
    if usd is not None:
        metrics.append("$%.2f/day" % usd)
    if gb is not None:
        metrics.append("~%.0f GB/day cross-AZ" % gb)
    onset = inc.get("onset") or inc.get("onset_date")
    step = inc.get("step_change") or inc.get("step_change_date")
    if onset:
        metrics.append("onset %s" % onset)
    if step and step != onset:
        metrics.append("step-change %s" % step)
    elif step:
        metrics.append("step-change %s" % step)
    if metrics:
        print("  " + "   ".join(metrics))

    print()
    print("dominant cause (%s): %s"
          % (_pct(dom.get("share")), dom.get("summary") or "?"))
    for ev in dom.get("evidence") or []:
        print("  evidence: %s" % ev)
    secondary = cause.get("secondary") or []
    if secondary:
        print()
        print("secondary cause(s):")
        for s in secondary:
            print("  - (%s) %s"
                  % (_pct(s.get("share")), s.get("summary") or "?"))
            for ev in s.get("evidence") or []:
                print("      evidence: %s" % ev)
    ruled = cause.get("ruled_out") or []
    if ruled:
        print()
        print("ruled out:")
        for r in ruled:
            if isinstance(r, dict):
                label = r.get("cause") or r.get("title") or "?"
                why = r.get("evidence") or r.get("why") or ""
                print("  - %s%s" % (label, (": " + why) if why else ""))
            else:
                print("  - %s" % r)

    conv = rca.get("evidence_convergence")
    if isinstance(conv, dict):
        agreed = [k for k, v in conv.items() if v]
    elif isinstance(conv, list):
        agreed = [str(x) for x in conv]
    else:
        agreed = []
    print()
    print("evidence convergence: %s"
          % (", ".join(agreed) if agreed else "(single source)"))
    print("confidence: %s" % (rca.get("confidence") or "?"))


def cmd_cost_rca(args: argparse.Namespace) -> int:
    """Root-cause an AWS cost anomaly by converging READ-ONLY evidence
    (Cost Explorer + VPC Flow Logs + EKS/NLB topology + LGTM self-
    metrics). Prints the breakdown and writes rca.json. Degrades cleanly
    without AWS: it still runs from a pasted/file anomaly report, but the
    live evidence is unavailable and confidence drops to LOW."""
    rca_mod = _import_soft("rca")
    if rca_mod is None or not hasattr(rca_mod, "analyze"):
        _err("cost RCA is unavailable (nr2grafana.rca not importable)")
        return 2
    awscost = _import_soft("awscost")
    profile = getattr(args, "profile", "") or ""
    region = getattr(args, "region", "") or "us-east-1"
    aws = None
    if awscost is not None and hasattr(awscost, "aws_available"):
        if awscost.aws_available():
            aws = _bound_aws(awscost, profile, region)
        else:
            print("note: AWS CLI not found / not configured -- RCA will "
                  "run from the anomaly report alone; live evidence (VPC "
                  "flow logs, EKS/NLB topology) is unavailable and "
                  "confidence will be LOW", file=sys.stderr)
    anomaly, err = _rca_anomaly(args, rca_mod, aws)
    if err is not None:
        _err(err)
        # usage-style errors (no/invalid source) -> 2; runtime -> 1
        usage = ("choose an anomaly source" in err
                 or "needs AWS access" in err)
        return 2 if usage else 1
    try:
        cfg = load_config(getattr(args, "config", "") or "")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _err(str(e))
        return 2
    cfg = dict(cfg)
    if getattr(args, "flow_logs_group", ""):
        cfg["flow_logs_group"] = args.flow_logs_group
    if profile:
        cfg["aws_profile"] = profile
    if region:
        cfg["aws_region"] = region
    flowlogs_mod = _import_soft("flowlogs")
    deepdive_mod = _import_soft("deepdive")
    packing_mod = _import_soft("packing")
    tco_mod = _import_soft("tco")
    log = lambda m: print(m, file=sys.stderr)
    print("converging read-only evidence for the anomaly...",
          file=sys.stderr)
    try:
        rca = _call_filtered(
            rca_mod.analyze, anomaly, aws=aws, flowlogs=flowlogs_mod,
            deepdive=deepdive_mod, packing=packing_mod, tco=tco_mod,
            cfg=cfg, log=log)
    except Exception as e:
        _err("RCA failed: %s" % e)
        return 1
    if not isinstance(rca, dict):
        _err("RCA produced no result")
        return 1
    _print_rca(rca)

    out_dir = getattr(args, "out", "") or "."
    os.makedirs(out_dir, exist_ok=True)
    rca_path = os.path.join(out_dir, "rca.json")
    _write_json(rca_path, rca)
    print("report -> %s" % rca_path, file=sys.stderr)
    store = _open_store_soft()
    if store is not None:
        try:
            store.save_artifact(_INSTANCE_SLUG, "rca", rca)
        except Exception as e:
            print("note: could not record RCA in the local store (%s)"
                  % e, file=sys.stderr)
        store.close()
    print("next: nr2grafana cost mitigate %s" % rca_path, file=sys.stderr)
    return 0


def _write_mitigation_config(out_dir: str,
                             plan: Dict[str, Any]) -> str:
    """Write each mitigation's paste-ready config snippets into
    out_dir/config/ grouped by target, plus a README that carries the
    reliability preconditions. Returns the config dir path ('' when
    there is nothing to write). All snippets are GENERIC (placeholders,
    no customer values) and GitOps/IaC-owned -- proposals only."""
    buckets: Dict[str, List[str]] = {}
    readme = ["# nr2grafana reliability-safe cost mitigation configs", "",
              "GENERIC, paste-ready snippets that cut cross-AZ / cost "
              "WITHOUT",
              "reducing availability, durability, performance or the "
              "ability to",
              "serve current traffic. The tool PROPOSES these; it never "
              "applies",
              "them (GitOps/IaC-owned). Read each mitigation's "
              "preconditions",
              "before applying -- some break something if a precondition "
              "does not",
              "hold (e.g. never blind-disable NLB cross-zone).", ""]
    for mit in plan.get("mitigations") or []:
        title = mit.get("title") or "mitigation"
        keeps = []
        for key, label in (("keeps_availability", "availability"),
                           ("keeps_durability", "durability"),
                           ("keeps_performance", "performance")):
            if mit.get(key) is False:
                keeps.append("MAY REDUCE " + label)
        flag = (" [" + "; ".join(keeps) + "]") if keeps else " [safe]"
        readme.append("## %s%s" % (title, flag))
        for pc in mit.get("reliability_guardrails") \
                or mit.get("preconditions") or []:
            readme.append("- precondition: %s" % pc)
        readme.append("")
        cfgs = mit.get("configs") or mit.get("config") or []
        for cfg in cfgs:
            snippet = cfg.get("snippet") or ""
            if not snippet.strip():
                continue
            target = (cfg.get("target") or "other").lower()
            fname = _MITIGATION_CONFIG_FILES.get(target, "%s.yaml" % target)
            header = "# --- %s ---" % title
            if cfg.get("note"):
                header += "\n# %s" % cfg["note"]
            for pc in mit.get("reliability_guardrails") \
                    or mit.get("preconditions") or []:
                header += "\n# PRECONDITION: %s" % pc
            buckets.setdefault(fname, []).append(
                header + "\n" + snippet.rstrip() + "\n")
    if not buckets:
        return ""
    cfg_dir = os.path.join(out_dir, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    for fname, parts in sorted(buckets.items()):
        with open(os.path.join(cfg_dir, fname), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(parts))
    with open(os.path.join(cfg_dir, "README.md"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(readme) + "\n")
    return cfg_dir


def _print_mitigation(plan: Dict[str, Any]) -> None:
    """Ranked mitigations with estimated $/day saved, keeps_* flags,
    handles-current-traffic, reliability preconditions and loud caveats
    for anything that could reduce reliability."""
    mits = plan.get("mitigations") or []
    print()
    print("== Reliability-safe mitigation plan ==")
    if not mits:
        print("  (no mitigations proposed)")
        return
    print("%-3s %-40s %-14s %-11s %s"
          % ("#", "MITIGATION", "EST $/DAY SAVED", "KEEPS A/D/P",
             "TRAFFIC"))
    safe_usd = 0.0
    for i, mit in enumerate(mits, 1):
        usd = _num(mit, "usd_per_day_saved", "est_savings.usd_per_day",
                   "est_savings.daily_usd", "saved_usd_per_day")
        usd_str = ("$%.2f" % usd) if usd is not None else "-"
        adp = "".join(
            "y" if mit.get(k) is not False else "n"
            for k in ("keeps_availability", "keeps_durability",
                      "keeps_performance"))
        adp = "/".join(list(adp))
        traffic = "yes" if mit.get("handles_current_traffic") \
            is not False else "NO"
        title = (mit.get("title") or "")[:40]
        print("%-3d %-40s %-14s %-11s %s"
              % (i, title, usd_str, adp, traffic))
        reduces = [label for k, label in (
            ("keeps_availability", "availability"),
            ("keeps_durability", "durability"),
            ("keeps_performance", "performance"))
            if mit.get(k) is False]
        if reduces or mit.get("handles_current_traffic") is False:
            extra = list(reduces)
            if mit.get("handles_current_traffic") is False:
                extra.append("cannot serve current traffic")
            print("    CAUTION: may reduce %s -- demoted; apply only if the "
                  "preconditions below are met" % "/".join(extra))
        elif usd is not None:
            safe_usd += usd
        for pc in mit.get("reliability_guardrails") \
                or mit.get("preconditions") or []:
            print("    precondition: %s" % pc)
        owner = mit.get("owner")
        if owner:
            print("    owner: %s" % owner)
    if safe_usd:
        print()
        print("estimated $%.2f/day saveable without reducing availability, "
              "durability or performance" % safe_usd)
    print(dim_note())


def cmd_cost_mitigate(args: argparse.Namespace) -> int:
    """Turn an rca.json into a ranked, reliability-safe mitigation plan
    with paste-ready GENERIC configs. Prints the ranked mitigations (with
    $ saved, reliability preconditions and keeps_* flags) and writes
    mitigation.json + a config/ dir. The tool PROPOSES only -- it never
    applies AWS/K8s changes."""
    mitigate_mod = _import_soft("mitigate")
    if mitigate_mod is None or not hasattr(mitigate_mod, "plan"):
        _err("mitigation planning is unavailable (nr2grafana.mitigate not "
             "importable)")
        return 2
    rca_path = getattr(args, "rca", "") or "rca.json"
    rca = _load_json_soft(rca_path)
    if not isinstance(rca, dict):
        _err("could not read RCA file %r -- run 'cost rca' first (it "
             "writes rca.json)" % rca_path)
        return 1
    try:
        cfg = load_config(getattr(args, "config", "") or "")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _err(str(e))
        return 2
    store = _open_store_soft()
    deepdive = packing = None
    if store is not None:
        try:
            deepdive = store.get_artifact(_INSTANCE_SLUG, "deepdive")
            packing = store.get_artifact(_INSTANCE_SLUG, "packing")
        except Exception:
            deepdive = packing = None
    try:
        plan = _call_filtered(mitigate_mod.plan, rca, deepdive=deepdive,
                              packing=packing, capacity=None, cfg=cfg)
    except Exception as e:
        _err("mitigation planning failed: %s" % e)
        if store is not None:
            store.close()
        return 1
    if not isinstance(plan, dict):
        _err("mitigation planning produced no result")
        if store is not None:
            store.close()
        return 1
    _print_mitigation(plan)

    out_dir = getattr(args, "out", "") or "."
    os.makedirs(out_dir, exist_ok=True)
    plan_path = os.path.join(out_dir, "mitigation.json")
    _write_json(plan_path, plan)
    cfg_dir = _write_mitigation_config(out_dir, plan)
    print("plan -> %s" % plan_path, file=sys.stderr)
    if cfg_dir:
        print("config -> %s" % cfg_dir, file=sys.stderr)
    if store is not None:
        try:
            store.save_artifact(_INSTANCE_SLUG, "mitigation", plan)
        except Exception as e:
            print("note: could not record the mitigation plan in the "
                  "local store (%s)" % e, file=sys.stderr)
        store.close()
    return 0


def cmd_aws_profiles(args: argparse.Namespace) -> int:
    """List AWS profile names from the local ~/.aws config (names only;
    credentials are never read). Read-only."""
    awscost = _import_soft("awscost")
    if awscost is None or not hasattr(awscost, "list_profiles"):
        _err("AWS profile listing is unavailable (nr2grafana.awscost not "
             "importable)")
        return 2
    try:
        profiles = awscost.list_profiles()
    except Exception as e:
        _err("could not list AWS profiles: %s" % e)
        return 1
    if not profiles:
        print("no AWS profiles found in ~/.aws/config or "
              "~/.aws/credentials", file=sys.stderr)
        return 0
    for name in profiles:
        print(name)
    print("\n%d profile(s); only section names are read, never credential "
          "values" % len(profiles), file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# deep-dive / AI context / Grafana MCP (1.6)
# ---------------------------------------------------------------------------

# Store slug for instance-wide artifacts (deep-dive/packing/cost are not
# tied to one dashboard). Mirrors web/server.py's _INSTANCE_SLUG.
_INSTANCE_SLUG = "__instance__"

# Deep-dive/packing severity ordering for ranked output.
_DD_SEV_RANK = {"FAIL": 0, "WARN": 1, "INFO": 2}


def _import_soft(name: str):
    """Import a sibling module, returning None on failure (deepdive /
    packing are built by sibling agents and may be absent mid-build)."""
    import importlib
    try:
        return importlib.import_module("nr2grafana." + name)
    except Exception:  # pragma: no cover - only when a sibling is absent
        return None


def _dd_risk(finding: Dict[str, Any]) -> str:
    """One-line risk flag for a finding: 'safe' when it keeps
    performance/durability/availability, else a loud caution listing
    what it would affect."""
    labels = (("keeps_performance", "performance"),
              ("keeps_durability", "durability"),
              ("keeps_availability", "availability"))
    bad = [label for key, label in labels if finding.get(key) is False]
    if bad:
        return "CAUTION: affects " + "/".join(bad)
    if any(key in finding for key, _ in labels):
        return "safe (keeps perf/durability/availability)"
    return ""


def _dd_rank(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        findings or [],
        key=lambda f: (_DD_SEV_RANK.get(str(f.get("severity", "")).upper(),
                                        3),
                       str(f.get("area", "")), str(f.get("title", ""))))


def _print_findings(title: str, findings: List[Dict[str, Any]]) -> None:
    ranked = _dd_rank(findings)
    print()
    print(title)
    if not ranked:
        print("  (no findings)")
        return
    print("  %-6s %-12s %-38s %-13s %s"
          % ("SEV", "AREA", "FINDING", "EST $/MO", "RISK"))
    for f in ranked:
        est = f.get("est_savings") or {}
        usd = est.get("monthly_usd")
        usd_str = ("$%.2f" % usd) if isinstance(usd, (int, float)) else "-"
        print("  %-6s %-12s %-38s %-13s %s"
              % (str(f.get("severity", "?")).upper()[:6],
                 str(f.get("area", ""))[:12],
                 str(f.get("title", ""))[:38], usd_str, _dd_risk(f)))


def _dd_headline(findings: List[Dict[str, Any]]) -> None:
    """Headline of estimated safe monthly savings (findings that do not
    reduce durability/availability/performance)."""
    usd = 0.0
    cores = 0.0
    for f in findings or []:
        if f.get("keeps_durability") is False \
                or f.get("keeps_availability") is False \
                or f.get("keeps_performance") is False:
            continue
        est = f.get("est_savings") or {}
        v = est.get("monthly_usd")
        if isinstance(v, (int, float)):
            usd += v
        compute = est.get("compute") or {}
        if isinstance(compute, dict):
            c = compute.get("cores")
            if isinstance(c, (int, float)):
                cores += c
    if usd or cores:
        print()
        print("estimated $%.2f/mo and %.1f core(s) saveable without "
              "reducing durability, availability or performance"
              % (usd, cores))


def _write_deepdive_config(out_dir: str, deepdive: Dict[str, Any],
                           packing: Optional[Dict[str, Any]]) -> str:
    """Write each finding's config snippet into out_dir/config/ grouped
    by target, plus the proposed Karpenter NodePool YAML. Returns the
    config dir path, or '' when there is nothing to write."""
    buckets: Dict[str, List[str]] = {}

    def add(title: str, cfgs: Any) -> None:
        for cfg in cfgs or []:
            snippet = cfg.get("snippet") or ""
            if not snippet.strip():
                continue
            target = cfg.get("target") or "other"
            fname = _COST_CONFIG_FILES.get(target, "%s.yaml" % target)
            header = "# --- %s ---" % title
            if cfg.get("note"):
                header += "\n# %s" % cfg["note"]
            buckets.setdefault(fname, []).append(
                header + "\n" + snippet.rstrip() + "\n")

    for f in deepdive.get("findings") or []:
        add(f.get("title") or f.get("area") or "finding", f.get("config"))
    if isinstance(packing, dict):
        for f in packing.get("findings") or []:
            add(f.get("title") or "finding", f.get("config"))
        karpenter = packing.get("karpenter") or {}
        yaml_text = karpenter.get("proposed_nodepool_yaml")
        if isinstance(yaml_text, str) and yaml_text.strip():
            buckets["karpenter-nodepool.yaml"] = [yaml_text.rstrip() + "\n"]
    if not buckets:
        return ""
    cfg_dir = os.path.join(out_dir, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    for fname, parts in sorted(buckets.items()):
        with open(os.path.join(cfg_dir, fname), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(parts))
    return cfg_dir


def cmd_deepdive(args: argparse.Namespace) -> int:
    """Deep-dive the LGTM stack from component self-metrics and, when
    --kube is given and kubectl is on PATH, Kubernetes topology / bin-
    packing / Karpenter. Prints findings ranked by severity with
    estimated savings and risk flags; writes deepdive-report.json plus
    config/ snippets. Exit 1 if any FAIL-severity finding is present."""
    deepdive_mod = _import_soft("deepdive")
    if deepdive_mod is None or not hasattr(deepdive_mod, "analyze"):
        _err("deep-dive is unavailable (nr2grafana.deepdive not "
             "importable)")
        return 2
    try:
        cfg = load_config(getattr(args, "config", "") or "")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _err(str(e))
        return 2
    if getattr(args, "pricing", ""):
        try:
            loaded = _load_json(args.pricing)
        except (json.JSONDecodeError, OSError) as e:
            _err("pricing file: %s" % e)
            return 2
        if isinstance(loaded, dict):
            cfg = dict(cfg)
            cfg["pricing"] = loaded
    log = lambda m: print(m, file=sys.stderr)
    grafana = None
    if getattr(args, "grafana_url", "") or os.environ.get("GRAFANA_URL"):
        try:
            grafana = _grafana_live(args)
        except SystemExit:  # missing url -> just skip the live client
            grafana = None
    deepdive = deepdive_mod.analyze(
        prom=getattr(args, "prom", "") or None,
        mimir=getattr(args, "mimir", "") or None,
        loki=getattr(args, "loki", "") or None,
        grafana=grafana, cfg=cfg, log=log)
    findings = deepdive.get("findings") or []
    packing: Optional[Dict[str, Any]] = None
    if getattr(args, "kube", False):
        packing_mod = _import_soft("packing")
        if packing_mod is None or not hasattr(packing_mod, "analyze"):
            print("note: Kubernetes packing analysis unavailable "
                  "(nr2grafana.packing not importable)", file=sys.stderr)
        elif packing_mod.kubectl_available():
            prices = cfg.get("pricing") \
                if isinstance(cfg.get("pricing"), dict) else None
            packing = packing_mod.analyze(cfg=cfg, prices=prices, log=log)
        else:
            print("note: kubectl not available -- skipping Kubernetes "
                  "topology / packing / Karpenter analysis (the metric "
                  "deep-dive below is unaffected)", file=sys.stderr)

    _print_findings("== LGTM stack findings ==", findings)
    if isinstance(packing, dict) and packing.get("findings"):
        _print_findings("== Kubernetes / bin-pack / Karpenter ==",
                        packing.get("findings"))
    _print_headline_all(findings, packing)

    out_dir = args.out or "."
    os.makedirs(out_dir, exist_ok=True)
    report: Dict[str, Any] = {
        "schema": "nr2grafana/deepdive-report/v1",
        "deepdive": deepdive}
    if packing is not None:
        report["packing"] = packing
    report_path = os.path.join(out_dir, "deepdive-report.json")
    _write_json(report_path, report)
    cfg_dir = _write_deepdive_config(out_dir, deepdive, packing)
    print("report -> %s" % report_path, file=sys.stderr)
    if cfg_dir:
        print("config -> %s" % cfg_dir, file=sys.stderr)
    print(dim_note(), file=sys.stderr)
    fails = sum(1 for f in findings
                if str(f.get("severity", "")).upper() == "FAIL")
    if isinstance(packing, dict):
        fails += sum(1 for f in packing.get("findings") or []
                     if str(f.get("severity", "")).upper() == "FAIL")
    return 1 if fails else 0


def _print_headline_all(findings: List[Dict[str, Any]],
                        packing: Optional[Dict[str, Any]]) -> None:
    combined = list(findings or [])
    if isinstance(packing, dict):
        combined += list(packing.get("findings") or [])
    _dd_headline(combined)


def _ai_assistant(args: argparse.Namespace):
    """Resolve an AI backend: an Anthropic API key (arg or env) wins,
    else a local console agent command. Returns a duck-typed assistant
    (with .available / .chat) or None when nothing is configured."""
    ai_mod = _import_soft("ai")
    if ai_mod is None:
        return None
    api_key = getattr(args, "anthropic_key", "") \
        or os.environ.get("ANTHROPIC_API_KEY", "")
    command = getattr(args, "command", "") \
        or os.environ.get("N2G_AI_COMMAND", "")
    model = getattr(args, "model", "") or os.environ.get("N2G_AI_MODEL", "")
    get = getattr(ai_mod, "get_assistant", None)
    if get is not None:
        return get(api_key=api_key, model=model, command=command)
    if api_key and hasattr(ai_mod, "AIAssist"):
        return ai_mod.AIAssist(api_key, model)
    return None


def _ai_context_bundle(store, aicontext_mod, slug: str) -> Dict[str, Any]:
    """Build the AI context bundle for ``slug`` (empty = whole
    workspace), threading in the instance-wide deep-dive when present."""
    deepdive = None
    try:
        deepdive = store.get_artifact(slug or _INSTANCE_SLUG, "deepdive")
    except Exception:
        deepdive = None
    return aicontext_mod.build_context(store, slug=slug, deepdive=deepdive,
                                       redact=True)


def cmd_ai_context(args: argparse.Namespace) -> int:
    """Export the compact, LLM-optimized AI context bundle (JSON or, with
    --markdown, Markdown) to stdout or a file."""
    aicontext_mod = _import_soft("aicontext")
    if aicontext_mod is None or not hasattr(aicontext_mod,
                                            "build_context"):
        _err("ai-context is unavailable (nr2grafana.aicontext not "
             "importable)")
        return 2
    store = _open_store_soft()
    if store is None:
        _err("the local store is required for ai-context -- run "
             "'convert --package' first")
        return 1
    try:
        slug = getattr(args, "slug", "") or ""
        context = _ai_context_bundle(store, aicontext_mod, slug)
        if getattr(args, "markdown", False):
            text = aicontext_mod.to_markdown(context)
        else:
            text = json.dumps(context, indent=2, ensure_ascii=False)
        out = getattr(args, "out", "") or ""
        if out:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print("ai-context -> %s" % out, file=sys.stderr)
        else:
            print(text)
    finally:
        store.close()
    return 0


def cmd_ai_troubleshoot(args: argparse.Namespace) -> int:
    """Ask the configured AI backend a question with the full context
    bundle attached. Never crashes on AI errors -- they surface as the
    answer text (aicontext.troubleshoot never raises)."""
    aicontext_mod = _import_soft("aicontext")
    if aicontext_mod is None or not hasattr(aicontext_mod,
                                            "build_context"):
        _err("AI troubleshooting is unavailable (nr2grafana.aicontext "
             "not importable)")
        return 2
    assistant = _ai_assistant(args)
    if assistant is None or not getattr(assistant, "available", False):
        _err("no AI backend configured -- set ANTHROPIC_API_KEY or pass "
             "--command for a local console agent (e.g. --command "
             "'claude -p {prompt}')")
        return 2
    store = _open_store_soft()
    if store is None:
        _err("the local store is required -- run 'convert --package' "
             "first")
        return 1
    try:
        slug = getattr(args, "slug", "") or ""
        context = _ai_context_bundle(store, aicontext_mod, slug)
        result = aicontext_mod.troubleshoot(
            assistant, context, getattr(args, "question", "") or "")
    finally:
        store.close()
    print(result.get("answer") or "(no answer returned)")
    backend = result.get("backend") or ""
    if backend:
        print("\n[%s backend]" % backend, file=sys.stderr)
    return 0


def cmd_mcp_config(args: argparse.Namespace) -> int:
    """Emit a ready MCP servers config wiring the Grafana MCP server (and
    optionally the nr2grafana context) into a local AI client. The
    Grafana token is referenced via env, never written to the file."""
    mcp_mod = _import_soft("mcp")
    if mcp_mod is None or not hasattr(mcp_mod, "generate_mcp_config"):
        _err("MCP integration is unavailable (nr2grafana.mcp not "
             "importable)")
        return 2
    grafana_url = getattr(args, "grafana_url", "") \
        or os.environ.get("GRAFANA_URL", "")
    try:
        cfg = mcp_mod.generate_mcp_config(
            grafana_url, kind=args.kind,
            n2g_context_path=getattr(args, "context", "") or "",
            include_grafana=not getattr(args, "no_grafana", False),
            include_aws_cost=bool(getattr(args, "aws_cost", False)),
            include_aws_cloudwatch=bool(
                getattr(args, "aws_cloudwatch", False)),
            include_n2g=not getattr(args, "no_n2g", False))
    except Exception as e:
        _err(str(e))
        return 2
    text = json.dumps(cfg, indent=2)
    out = getattr(args, "out", "") or ""
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("mcp config -> %s" % out, file=sys.stderr)
    else:
        print(text)
    note = getattr(mcp_mod, "config_note", None)
    if callable(note):
        print("note: %s" % note(args.kind), file=sys.stderr)
    return 0


def cmd_mcp_probe(args: argparse.Namespace) -> int:
    """Probe a Grafana MCP server (initialize + list tools) over stdio
    (--command) or HTTP/SSE (--url). probe() never raises."""
    mcp_mod = _import_soft("mcp")
    if mcp_mod is None or not hasattr(mcp_mod, "probe"):
        _err("MCP integration is unavailable (nr2grafana.mcp not "
             "importable)")
        return 2
    url = getattr(args, "url", "") or ""
    command = getattr(args, "command", "") or ""
    cmd_list = None
    if command.strip():
        import shlex
        cmd_list = shlex.split(command)
    if not url and not cmd_list:
        _err("provide --url (http/SSE) or --command (stdio) to probe")
        return 2
    result = mcp_mod.probe(command=cmd_list, url=url)
    if result.get("ok"):
        tools = result.get("tools") or []
        server = result.get("server") or {}
        name = server.get("name") if isinstance(server, dict) else ""
        print("ok -- MCP server reachable%s, %d tool(s)"
              % ((" (%s)" % name) if name else "", len(tools)))
        for t in tools:
            print("  - %s" % t)
        return 0
    _err("MCP probe failed: %s" % (result.get("error")
                                   or "unknown error"))
    return 1


# ---------------------------------------------------------------------------
# AWS TCO trend analysis (1.7) -- read-only Cost Explorer
# ---------------------------------------------------------------------------

class _BoundAWS:
    """Read-only wrapper over the awscost module pre-binding a profile
    and/or region into any function whose signature accepts them. The
    module is passed straight through when neither override is set."""

    def __init__(self, mod, profile="", region=""):
        self._mod = mod
        self._profile = profile
        self._region = region

    def __getattr__(self, name):
        import inspect
        attr = getattr(self._mod, name)
        if not inspect.isroutine(attr):
            return attr
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


def _bound_aws(awscost, profile="", region=""):
    if profile or region:
        return _BoundAWS(awscost, profile, region)
    return awscost


def _call_tco_analyze(tco, aws, **kwargs):
    """Call tco.analyze with only the kwargs its signature accepts."""
    import inspect
    try:
        params = inspect.signature(tco.analyze).parameters
    except (TypeError, ValueError):
        params = {}
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return tco.analyze(aws, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in params}
    return tco.analyze(aws, **filtered)


def _tco_artifacts_soft(store):
    """(deepdive, traffic, packing, change_log) from the store for the
    instance-wide slug; each degrades to None when absent."""
    if store is None:
        return None, None, None, None
    dd = tr = pk = cl = None
    try:
        dd = store.get_artifact(_INSTANCE_SLUG, "deepdive")
        tr = store.get_artifact(_INSTANCE_SLUG, "traffic")
        pk = store.get_artifact(_INSTANCE_SLUG, "packing")
        cl = store.list_changes()
    except Exception:
        pass
    return dd, tr, pk, cl


def _print_tco(report: Dict[str, Any]) -> None:
    """Terminal summary: per-service trend table, run-rate + growth
    headline, projection, observability attribution and change
    correlation. All figures are ESTIMATES from the user's own CE
    data."""
    total = report.get("total") or {}
    trend = total.get("trend") or {}
    series = total.get("series") or []
    print()
    print("%-32s %-14s %-10s %s"
          % ("SERVICE", "LATEST $/MO", "GROWTH %", "DIRECTION"))
    for svc in report.get("by_service") or []:
        s_series = svc.get("series") or []
        latest = s_series[-1][1] if s_series else svc.get("latest")
        s_trend = svc.get("trend") or {}
        latest_str = ("$%.2f" % latest) \
            if isinstance(latest, (int, float)) else "-"
        growth = s_trend.get("pct_growth")
        growth_str = ("%+.1f%%" % growth) \
            if isinstance(growth, (int, float)) else "-"
        print("%-32s %-14s %-10s %s"
              % (str(svc.get("service", "?"))[:32], latest_str,
                 growth_str, s_trend.get("direction", "")))
    run_rate = trend.get("run_rate")
    growth = trend.get("pct_growth")
    print()
    print("total monthly run-rate: %s   growth: %s   direction: %s"
          % (("$%.2f" % run_rate)
             if isinstance(run_rate, (int, float)) else "-",
             ("%+.1f%%" % growth)
             if isinstance(growth, (int, float)) else "-",
             trend.get("direction", "n/a")))
    if series:
        first_m, first_v = series[0]
        last_m, last_v = series[-1]
        print("  %s: $%.2f  ->  %s: $%.2f  (%d month(s))"
              % (first_m, float(first_v), last_m, float(last_v),
                 len(series)))
    forecast = total.get("forecast") or {}
    fseries = forecast.get("series") or forecast.get("next") or []
    if fseries:
        nxt = fseries[0]
        try:
            print("  projected next month (%s): $%.2f"
                  % (nxt[0], float(nxt[1])))
        except (TypeError, ValueError, IndexError):
            pass
    attr = report.get("observability_attribution") or {}
    usd = attr.get("monthly_usd")
    if isinstance(usd, (int, float)):
        print()
        print("estimated observability share: $%.2f/mo (ESTIMATE)" % usd)
    corr = report.get("change_correlation") or {}
    events = corr.get("events") or corr.get("correlations") or []
    if events:
        print()
        print("change correlation (correlation, not proof):")
        for ev in events[:8]:
            print("  - %s" % (ev.get("summary")
                              or ev.get("title") or ev))
    anomalies = report.get("anomalies") or []
    if anomalies:
        print()
        print("%d cost anomaly(ies) detected" % len(anomalies))


def cmd_tco_analyze(args: argparse.Namespace) -> int:
    """Analyze AWS total cost of ownership trends over time via Cost
    Explorer (read-only, local aws CLI auth). Prints a trend table,
    run-rate + projection, observability attribution and change
    correlation; writes tco-report.json. All figures are ESTIMATES."""
    awscost = _import_soft("awscost")
    tco = _import_soft("tco")
    if awscost is None or not hasattr(awscost, "aws_available"):
        _err("AWS TCO analysis is unavailable (nr2grafana.awscost not "
             "importable)")
        return 2
    if tco is None or not hasattr(tco, "analyze"):
        _err("AWS TCO analysis is unavailable (nr2grafana.tco not "
             "importable)")
        return 2
    if not awscost.aws_available():
        _err("AWS CLI not found / not configured -- install the aws CLI "
             "and configure read-only credentials (aws configure / SSO) "
             "to analyze TCO")
        return 2
    profile = getattr(args, "profile", "") or ""
    region = getattr(args, "region", "") or ""
    months = max(1, min(int(getattr(args, "months", 6) or 6), 36))
    group_by = getattr(args, "group_by", "SERVICE") or "SERVICE"
    raw_buckets = getattr(args, "buckets", "") or ""
    buckets = [b.strip() for b in raw_buckets.split(",") if b.strip()] \
        or None
    log = lambda m: print(m, file=sys.stderr)
    client = _bound_aws(awscost, profile, region)
    store = _open_store_soft()
    dd, tr, pk, cl = _tco_artifacts_soft(store)
    print("discovering AWS cost over %d month(s) via Cost Explorer "
          "(read-only, local auth)..." % months, file=sys.stderr)
    try:
        report = _call_tco_analyze(
            tco, client, store=store, deepdive=dd, traffic=tr,
            packing=pk, change_log=cl, months=months, group_by=group_by,
            buckets=buckets, profile=profile, region=region, log=log)
    except Exception as e:
        _err("TCO analysis failed: %s" % e)
        if store is not None:
            store.close()
        return 1
    _print_tco(report)
    print(dim_note(), file=sys.stderr)
    if store is not None:
        try:
            store.save_artifact(_INSTANCE_SLUG, "tco", report)
            snap = getattr(tco, "snapshot", None)
            if callable(snap):
                snap(store, report)
            else:
                store.save_artifact(_INSTANCE_SLUG, "tco-snapshot",
                                    report)
        except Exception as e:
            print("note: could not record TCO in the local store (%s)"
                  % e, file=sys.stderr)
        store.close()
    out_dir = getattr(args, "out", "") or "."
    os.makedirs(out_dir, exist_ok=True)
    report_path = os.path.join(out_dir, "tco-report.json")
    _write_json(report_path, report)
    print("report -> %s" % report_path, file=sys.stderr)
    return 0


def cmd_tco_identity(args: argparse.Namespace) -> int:
    """Print the AWS caller identity (which account/role the read-only
    analysis runs as). Never mutates anything."""
    awscost = _import_soft("awscost")
    if awscost is None or not hasattr(awscost, "aws_available"):
        _err("AWS access is unavailable (nr2grafana.awscost not "
             "importable)")
        return 2
    if not awscost.aws_available():
        _err("AWS CLI not found / not configured -- install the aws CLI "
             "and configure read-only credentials (aws configure / SSO)")
        return 2
    client = _bound_aws(awscost, getattr(args, "profile", "") or "",
                        getattr(args, "region", "") or "")
    try:
        identity = client.caller_identity()
    except Exception as e:
        _err("could not read AWS identity: %s -- check your aws "
             "credentials (read-only)" % e)
        return 1
    print(json.dumps(identity, indent=2, sort_keys=True))
    print("read-only: nr2grafana only ever issues get-/list-/describe- "
          "AWS calls", file=sys.stderr)
    return 0


def cmd_tco_trend(args: argparse.Namespace) -> int:
    """Diff the dated TCO snapshots recorded over time."""
    tco = _import_soft("tco")
    if tco is None or not hasattr(tco, "trend_over_snapshots"):
        _err("TCO trend is unavailable (nr2grafana.tco not importable)")
        return 2
    store = _open_store_soft()
    if store is None:
        _err("the local store is required for 'tco trend' -- run "
             "'tco analyze' first")
        return 1
    try:
        trend = tco.trend_over_snapshots(store)
    except Exception as e:
        _err("could not compute snapshot trend: %s" % e)
        return 1
    finally:
        store.close()
    print(json.dumps(trend, indent=2, sort_keys=True))
    print(dim_note(), file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------

def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve
    try:
        store = Store(_db_path())
    except Exception as e:
        _err("cannot open local store: %s" % e)
        return 1
    try:
        return serve(host=args.host, port=args.port,
                     open_browser=not args.no_browser, store=store)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# programmatic API surface (1.10): stdio MCP server + headless token API
# ---------------------------------------------------------------------------

def _is_loopback(host: str) -> bool:
    """True when ``host`` is a loopback bind address (or unset). Anything
    else (0.0.0.0, a LAN/public address) is treated as non-loopback and
    may only be bound with an API token set."""
    h = (host or "").strip().lower()
    if h in ("", "127.0.0.1", "::1", "localhost", "[::1]"):
        return True
    return h.startswith("127.")


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    """Run nr2grafana AS a stdio JSON-RPC MCP server so a local AI
    (Claude/Kiro) can call its operations as tools. Blocks reading stdin
    until the client disconnects. Config/secrets come from the process
    environment, never from the transport."""
    import importlib
    try:
        mcp_server = importlib.import_module("nr2grafana.mcp_server")
    except Exception as e:
        _err("MCP server is unavailable (nr2grafana.mcp_server not "
             "importable): %s" % e)
        return 2
    if not hasattr(mcp_server, "serve_stdio"):
        _err("MCP server is unavailable (serve_stdio entry point missing)")
        return 2
    store = _open_store_soft()
    log = lambda m: print(m, file=sys.stderr)
    try:
        mcp_server.serve_stdio(store=store, log=log)
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        _err("MCP server error: %s" % e)
        return 1
    finally:
        if store is not None:
            store.close()
    return 0


def cmd_api_serve(args: argparse.Namespace) -> int:
    """Launch the localhost web app as a headless, token-authed HTTP API
    so non-browser clients (scripts / AI) can drive it with a bearer
    token. The token is read from --api-token or N2G_API_TOKEN and is
    NEVER printed, echoed or logged. A non-loopback --host is refused
    unless a token is set (so a remotely reachable API always requires
    authentication)."""
    from .web import serve
    host = getattr(args, "host", "") or "127.0.0.1"
    token = getattr(args, "api_token", "") \
        or os.environ.get("N2G_API_TOKEN", "")
    if not _is_loopback(host) and not token:
        _err("refusing to bind non-loopback host %r without an API token: "
             "pass --api-token or set N2G_API_TOKEN so remote clients must "
             "authenticate" % host)
        return 2
    try:
        store = Store(_db_path())
    except Exception as e:
        _err("cannot open local store: %s" % e)
        return 1
    if token:
        print("API token configured: programmatic requests must send the "
              "'Authorization: Bearer <token>' header (the token value is "
              "never printed or logged).", file=sys.stderr)
    else:
        print("no API token set: the API stays browser same-origin only on "
              "loopback; set --api-token or N2G_API_TOKEN to allow "
              "programmatic (non-browser) clients.", file=sys.stderr)
    try:
        return serve(host=host, port=args.port,
                     open_browser=not getattr(args, "no_browser", False),
                     store=store, api_token=token, headless=True)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# global --json wrapper: one JSON object on stdout, human/log text on stderr
# ---------------------------------------------------------------------------

_SUBCOMMAND_DESTS = (
    "grafana_command", "changes_command", "cost_command", "mcp_command",
    "tco_command", "aws_command", "ai_command", "api_command")


def _command_name(args: argparse.Namespace) -> str:
    """Full dotted command path (e.g. 'grafana parity') for the JSON
    envelope's ``command`` field."""
    parts: List[str] = []
    top = getattr(args, "command", None)
    if top:
        parts.append(str(top))
    for dest in _SUBCOMMAND_DESTS:
        val = getattr(args, dest, None)
        if val:
            parts.append(str(val))
            break
    return " ".join(parts)


def _json_error_line(err_text: str) -> str:
    """Best-effort short error message from captured stderr: the last
    'error: ...' line, else the last non-empty line."""
    last = ""
    for line in err_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        last = stripped
        if stripped.startswith("error:"):
            return stripped[len("error:"):].strip()
    return last


def _emit_json(command: str, ok: bool, result: Any, error: Optional[str],
               stream) -> None:
    obj: Dict[str, Any] = {"ok": ok, "command": command, "result": result}
    if error:
        obj["error"] = error
    stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
    stream.flush()


def _run_json(command: str, func, args: argparse.Namespace) -> int:
    """Run ``func`` with stdout/stderr captured, then print a single JSON
    envelope to the real stdout. Human/log text (anything the command
    wrote to stderr, plus any non-JSON stdout) is forwarded to the real
    stderr so stdout stays pure JSON. Exit codes are unchanged."""
    import io as _io
    real_stdout, real_stderr = sys.stdout, sys.stderr
    buf_out, buf_err = _io.StringIO(), _io.StringIO()
    rc = 1
    exc_error: Optional[str] = None
    try:
        sys.stdout, sys.stderr = buf_out, buf_err
        try:
            rc = func(args)
        except SystemExit as e:
            code = e.code
            rc = code if isinstance(code, int) else \
                (0 if code is None else 1)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            rc = 1
            exc_error = "%s: %s" % (type(e).__name__, e)
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
    err_text = buf_err.getvalue()
    if err_text:
        real_stderr.write(err_text)
    out_text = buf_out.getvalue()
    result: Any = None
    if out_text.strip():
        try:
            result = json.loads(out_text)
        except (json.JSONDecodeError, ValueError):
            # Not JSON: it is human text -> stderr, keep stdout clean.
            real_stderr.write(out_text)
    real_stderr.flush()
    if not isinstance(rc, int):
        rc = 0 if rc is None else 1
    ok = rc == 0
    error = exc_error
    if not ok and error is None:
        error = _json_error_line(err_text) or "command failed"
    _emit_json(command, ok, result, error, real_stdout)
    return rc


# ---------------------------------------------------------------------------

_PRIMARY_COMMANDS = ("import", "convert", "validate", "export", "inspect",
                     "explain")

_EPILOG = """\
workflow:
  nr2grafana import  -o ./nr            # New Relic -> NR dashboard JSON (or: import -o ./nr file.json)
  nr2grafana convert ./nr -o ./grafana  # NR JSON -> Grafana JSON + migration-report.json
  nr2grafana validate ./grafana --grafana-url $GRAFANA_URL --test
  nr2grafana export   ./grafana --grafana-url $GRAFANA_URL --folder "Migrated"
  nr2grafana inspect  ./nr/x.json       # deep model of an NR dashboard (for humans and AI agents)
  nr2grafana explain  "SELECT ... FROM ..."

exit codes: 0 ok, 1 problems found, 2 bad input/usage, 3 cannot reach or
authenticate with New Relic / Grafana. Add --json before the command for a
single JSON result on stdout.

advanced (hidden) commands: analyze, fetch, list, grafana (check, test,
parity, samples, diagnose, heal, datasources, add-datasource, import),
changes, cost, deepdive, ai-context, ai, mcp, tco, aws, api, web,
example-config, interactive. Run `nr2grafana <command> -h` for their help.
"""


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="nr2grafana",
        description="Migrate New Relic dashboards to Grafana (LGTM stack): "
                    "import, convert, validate, export. New Relic is only "
                    "ever read.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", dest="json_out",
                    help="emit one JSON object result to stdout and route "
                         "all human/log text to stderr (for scripts and "
                         "AI); place before the subcommand")
    sub = ap.add_subparsers(dest="command", metavar="<command>")

    def add_nr_args(p):
        p.add_argument("--api-key", "-k", default="",
                       help="New Relic USER API key (NRAK-...); or set "
                            "NEW_RELIC_API_KEY")
        p.add_argument("--region", default="US", choices=["US", "EU",
                                                          "us", "eu"],
                       help="New Relic region (default US)")

    def add_grafana_args(p):
        p.add_argument("--url", "--grafana-url", dest="grafana_url",
                       default="",
                       help="Grafana base URL (e.g. "
                            "http://localhost:3000); or set GRAFANA_URL")
        p.add_argument("--token", "--grafana-token", dest="grafana_token",
                       default="",
                       help="Grafana service-account token; or set "
                            "GRAFANA_TOKEN")
        p.add_argument("--insecure", action="store_true",
                       help="skip TLS certificate verification")

    def _need_sub(parser):
        def f(_args: argparse.Namespace) -> int:
            parser.print_help()
            return 2
        return f

    # ---- the four steps + the two understanding commands -----------------
    p_imp = sub.add_parser(
        "import",
        help="step 1: bring New Relic dashboards to disk as NR JSON "
             "(from New Relic via NerdGraph, or from exported files)")
    add_nr_args(p_imp)
    p_imp.add_argument("files", nargs="*",
                       help="local NR dashboard JSON exports (UI 'Copy "
                            "JSON' or NerdGraph reads) to validate and "
                            "normalise instead of fetching from New Relic")
    p_imp.add_argument("--out", "-o", default="./newrelic-dashboards",
                       help="output directory (default: %(default)s)")
    p_imp.add_argument("--guid", "-g", action="append", default=[],
                       help="import only this dashboard GUID (repeatable)")
    p_imp.add_argument("--name", default="",
                       help="import only dashboards whose name contains "
                            "this text (case-insensitive)")
    p_imp.set_defaults(func=cmd_import)

    p_list = sub.add_parser("list", help="list dashboards in the account")
    add_nr_args(p_list)
    p_list.set_defaults(func=cmd_list)

    p_fetch = sub.add_parser(
        "fetch", help="bulk-export New Relic dashboards to JSON files")
    add_nr_args(p_fetch)
    p_fetch.add_argument("--guid", "-g", action="append", default=[],
                         help="export only this dashboard GUID (repeatable); "
                              "default: all dashboards")
    p_fetch.add_argument("--out", "-o", default="./newrelic-dashboards",
                         help="output directory (default: %(default)s)")
    p_fetch.set_defaults(func=cmd_fetch)

    p_conv = sub.add_parser(
        "convert",
        help="step 2: convert NR dashboard JSON file(s)/dir(s) to Grafana "
             "JSON + migration-report.json (names every widget that cannot "
             "be migrated and why)")
    p_conv.add_argument("inputs", nargs="+",
                        help="NR dashboard JSON files or directories")
    p_conv.add_argument("--out", "-o", default="./grafana-dashboards",
                        help="output directory (default: %(default)s); note: "
                             "migration-report.json there is replaced on "
                             "every run")
    p_conv.add_argument("--config", "-c", default="",
                        help="mapping config JSON (see 'example-config')")
    p_conv.add_argument("--report", default="",
                        help="migration report path (default: "
                             "<out>/migration-report.json)")
    p_conv.add_argument("--page-strategy", choices=["rows", "split"],
                        default="",
                        help="multi-page dashboards: one dashboard with "
                             "collapsible rows (rows) or one dashboard per "
                             "page (split)")
    p_conv.add_argument("--passthrough", action="store_true",
                        help="untranslatable widgets query New Relic via "
                             "the official NR Grafana datasource plugin "
                             "instead of becoming text placeholders")
    p_conv.add_argument("--package", action="store_true",
                        help="write one package directory per dashboard "
                             "(dashboard.json, requirements.json, "
                             "README.md, test.sh, datatest.json) plus "
                             "INDEX.md, instead of flat files")
    p_conv.set_defaults(func=cmd_convert)

    p_ana = sub.add_parser(
        "analyze",
        help="(re)generate requirements + package dirs for converted "
             "Grafana output or NR dashboard JSON")
    p_ana.add_argument("inputs", nargs="+",
                       help="converted Grafana JSON, NR dashboard JSON, "
                            "or directories of either (uses "
                            "migration-report.json next to converted "
                            "files when present)")
    p_ana.add_argument("--out", "-o", default="",
                       help="package output directory (default: alongside "
                            "the inputs)")
    p_ana.add_argument("--config", "-c", default="",
                       help="mapping config JSON (see 'example-config')")
    p_ana.set_defaults(func=cmd_analyze)

    p_val = sub.add_parser(
        "validate",
        help="step 3: validate converted dashboards (static checks; with "
             "--grafana-url also the datasource types your Grafana has vs. "
             "needs, and with --test every panel query)")
    p_val.add_argument("inputs", nargs="+",
                       help="Grafana dashboard JSON files or directories")
    add_grafana_args(p_val)
    p_val.add_argument("--test", action="store_true",
                       help="run every panel query through Grafana "
                            "(/api/ds/query) and report data / no-data / "
                            "error per panel (needs --grafana-url)")
    p_val.add_argument("--datasource", action="append", default=[],
                       metavar="TYPE=UID",
                       help="bind a datasource type to a specific "
                            "datasource uid/name (e.g. prometheus=mimir); "
                            "repeatable")
    p_val.set_defaults(func=cmd_validate)

    p_exp = sub.add_parser(
        "export",
        help="step 4: create the converted dashboards on a Grafana "
             "instance, verify each one after creation, and name the New "
             "Relic dashboard it came from")
    p_exp.add_argument("inputs", nargs="+",
                       help="Grafana dashboard JSON files or directories "
                            "(convert output)")
    add_grafana_args(p_exp)
    p_exp.add_argument("--folder", "-f", default="",
                       help="Grafana folder title (created if missing)")
    p_exp.add_argument("--overwrite", action="store_true",
                       help="replace a dashboard with the same uid/title")
    p_exp.add_argument("--datasource", action="append", default=[],
                       metavar="TYPE=UID",
                       help="bind a datasource type to a specific "
                            "datasource uid/name; repeatable")
    p_exp.add_argument("--test", action="store_true",
                       help="after creating, run every panel query and "
                            "report data / no-data / error")
    p_exp.add_argument("--allow-missing", action="store_true",
                       dest="allow_missing",
                       help="export even when Grafana lacks a datasource "
                            "type the dashboard needs")
    p_exp.set_defaults(func=cmd_export)

    p_ins = sub.add_parser(
        "inspect",
        help="deep, structured understanding of a New Relic dashboard: "
             "every widget and NRQL clause, attributes, variables, the "
             "translation plan, datasources needed, what cannot migrate")
    p_ins.add_argument("inputs", nargs="+",
                       help="NR dashboard JSON files or directories")
    p_ins.add_argument("--config", "-c", default="",
                       help="mapping config JSON (see 'example-config')")
    p_ins.set_defaults(func=cmd_inspect)

    p_expl = sub.add_parser(
        "explain",
        help="parse and translate one NRQL query, showing every clause, "
             "the emitted PromQL/LogQL/TraceQL and every assumption")
    p_expl.add_argument("nrql", nargs="+", help="the NRQL query (quote it)")
    p_expl.add_argument("--config", "-c", default="",
                        help="mapping config JSON (see 'example-config')")
    p_expl.set_defaults(func=cmd_explain)

    p_cfg = sub.add_parser(
        "example-config", help="print the default mapping config as JSON")
    p_cfg.set_defaults(func=cmd_example_config)

    p_graf = sub.add_parser(
        "grafana",
        help="live Grafana operations: check requirements, test panel "
             "data, import dashboards")
    p_graf.set_defaults(func=_need_sub(p_graf))
    gsub = p_graf.add_subparsers(dest="grafana_command")

    g_check = gsub.add_parser(
        "check",
        help="check datasource/plugin requirements against a live "
             "Grafana instance (exit 1 on missing)")
    g_check.add_argument("inputs", nargs="+",
                         help="package dir(s) or requirements.json "
                              "file(s)")
    add_grafana_args(g_check)
    g_check.set_defaults(func=cmd_grafana_check)

    g_test = gsub.add_parser(
        "test",
        help="run every panel query through /api/ds/query and report "
             "data/no-data/error (exit 1 on errors; no-data is a "
             "warning)")
    g_test.add_argument("inputs", nargs="+",
                        help="package dir(s) or dashboard JSON file(s)")
    add_grafana_args(g_test)
    g_test.set_defaults(func=cmd_grafana_test)

    g_par = gsub.add_parser(
        "parity",
        help="compare real data New Relic vs Grafana per panel "
             "(original NRQL via NerdGraph vs translated query via "
             "/api/ds/query); exit 1 only on Grafana query errors",
        epilog="Tip: 'grafana samples' pulls raw rows/log lines from "
               "both sides so a human can sign off panel by panel.")
    g_par.add_argument("inputs", nargs="+",
                       help="package dir(s) or dashboard JSON file(s)")
    add_grafana_args(g_par)
    add_nr_args(g_par)
    g_par.add_argument("--account-id", "-a", action="append",
                       default=[], dest="account_id",
                       help="New Relic account id to run NRQL against "
                            "(repeatable); or set "
                            "NEW_RELIC_ACCOUNT_ID (comma-separated)")
    g_par.add_argument("--from", dest="frm", default="now-1h",
                       help="range start (default: %(default)s)")
    g_par.add_argument("--to", dest="to", default="now",
                       help="range end (default: %(default)s)")
    g_par.set_defaults(func=cmd_grafana_parity)

    g_smp = gsub.add_parser(
        "samples",
        help="pull raw data samples from BOTH sides (New Relic "
             "events/rows via NerdGraph, Grafana log lines/"
             "datapoints) for human side-by-side sign-off; writes "
             "samples.json next to each dashboard")
    g_smp.add_argument("inputs", nargs="+",
                       help="package dir(s) or dashboard JSON "
                            "file(s)")
    add_grafana_args(g_smp)
    add_nr_args(g_smp)
    g_smp.add_argument("--account-id", "-a", action="append",
                       default=[], dest="account_id",
                       help="New Relic account id to run NRQL "
                            "against (repeatable); or set "
                            "NEW_RELIC_ACCOUNT_ID (comma-separated)")
    g_smp.add_argument("--from", dest="frm", default="now-1h",
                       help="range start (default: %(default)s)")
    g_smp.add_argument("--to", dest="to", default="now",
                       help="range end (default: %(default)s)")
    g_smp.add_argument("--limit", type=int, default=5,
                       help="max samples per side per panel "
                            "(default: %(default)s)")
    g_smp.set_defaults(func=cmd_grafana_samples)

    g_diag = gsub.add_parser(
        "diagnose",
        help="root-cause failing/empty panels (auth, datasources, "
             "metric/label names, pipeline, config); exit 1 on "
             "blockers")
    g_diag.add_argument("inputs", nargs="+",
                        help="package dir(s) or dashboard JSON "
                             "file(s)")
    add_grafana_args(g_diag)
    add_nr_args(g_diag)
    g_diag.add_argument("--config", "-c", default="",
                        help="mapping config JSON used for the "
                             "conversion (improves suggestions)")
    g_diag.set_defaults(func=cmd_grafana_diagnose)

    g_heal = gsub.add_parser(
        "heal",
        help="auto-heal loop: test -> diagnose -> apply safe fixes "
             "(high-confidence query edits, config overlays) -> "
             "re-test")
    g_heal.add_argument("inputs", nargs="+",
                        help="package dir(s) or dashboard JSON "
                             "file(s)")
    add_grafana_args(g_heal)
    add_nr_args(g_heal)
    g_heal.add_argument("--push", action="store_true",
                        help="push fixed dashboards to Grafana "
                             "(default: local files only)")
    g_heal.add_argument("--max-rounds", type=int, default=3,
                        dest="max_rounds",
                        help="test/fix rounds (default: %(default)s)")
    g_heal.set_defaults(func=cmd_grafana_heal)

    g_ds = gsub.add_parser(
        "datasources",
        help="list the instance's datasources with live health")
    add_grafana_args(g_ds)
    g_ds.set_defaults(func=cmd_grafana_datasources)

    g_add = gsub.add_parser(
        "add-datasource",
        help="create a datasource from a guided template "
             "(prometheus, loki, tempo, cloudwatch, stackdriver, "
             "azure monitor, new relic)")
    g_add.add_argument("--type", "-t", required=True,
                       help="datasource type (one of: %s)"
                            % ", ".join(sorted(DS_TEMPLATES)))
    g_add.add_argument("--name", "-n", required=True,
                       help="datasource name in Grafana")
    g_add.add_argument("--set", action="append", default=[],
                       dest="set_values", metavar="FIELD=VALUE",
                       help="template field value (repeatable); "
                            "omitted secret fields are prompted for "
                            "on a terminal")
    add_grafana_args(g_add)
    g_add.set_defaults(func=cmd_grafana_add_datasource)

    g_imp = gsub.add_parser(
        "import", help="import dashboards into a live Grafana instance")
    g_imp.add_argument("inputs", nargs="+",
                       help="package dir(s), converted output dir(s) or "
                            "dashboard JSON file(s)")
    g_imp.add_argument("--folder", "-f", default="",
                       help="folder title to import into (created if "
                            "missing; blank = General)")
    g_imp.add_argument("--overwrite", action="store_true",
                       help="replace existing dashboards instead of "
                            "failing on collisions")
    add_grafana_args(g_imp)
    g_imp.set_defaults(func=cmd_grafana_import)

    p_ch = sub.add_parser(
        "changes",
        help="change-log reports and config codification")
    p_ch.set_defaults(func=_need_sub(p_ch))
    csub = p_ch.add_subparsers(dest="changes_command")

    c_rep = csub.add_parser(
        "report", help="print the recorded change log")
    c_rep.add_argument("--slug", default="",
                       help="only changes for this dashboard slug")
    c_rep.add_argument("--markdown", action="store_true",
                       help="Markdown tables instead of JSON")
    c_rep.set_defaults(func=cmd_changes_report)

    c_sug = csub.add_parser(
        "suggest-config",
        help="infer a mapping-config overlay from recorded query/"
             "datasource edits")
    c_sug.add_argument("--slug", default="",
                       help="only changes for this dashboard slug")
    c_sug.set_defaults(func=cmd_changes_suggest)

    p_cost = sub.add_parser(
        "cost",
        help="estimate LGTM-stack cost from real traffic and propose "
             "safe, dollar-estimated ways to cut it")
    p_cost.set_defaults(func=_need_sub(p_cost))
    costsub = p_cost.add_subparsers(dest="cost_command")

    co_an = costsub.add_parser(
        "analyze",
        help="sample datasource traffic, subtract what the migrated "
             "dashboards use, and print ranked safe savings + "
             "estimated $ (writes cost-report.json + config/ snippets)")
    co_an.add_argument("inputs", nargs="*",
                       help="converted package dir(s) or dashboard "
                            "JSON (default: all dashboards in the local "
                            "store)")
    add_grafana_args(co_an)
    co_an.add_argument("--from", dest="frm", default="now-24h",
                       help="traffic sample range start "
                            "(default: %(default)s)")
    co_an.add_argument("--to", dest="to", default="now",
                       help="traffic sample range end "
                            "(default: %(default)s)")
    co_an.add_argument("--pricing", default="",
                       help="pricing assumptions JSON file (overrides "
                            "the built-in defaults)")
    co_an.add_argument("--out", "-o", default=".",
                       help="output directory for cost-report.json and "
                            "config/ (default: current directory)")
    co_an.set_defaults(func=cmd_cost_analyze)

    co_pr = costsub.add_parser(
        "pricing",
        help="print the pricing assumptions (or edit them with "
             "--set key=value; overrides persist locally)")
    co_pr.add_argument("--set", action="append", default=[],
                       dest="set_values", metavar="KEY=VALUE",
                       help="override a pricing assumption (repeatable)")
    co_pr.set_defaults(func=cmd_cost_pricing)

    co_rca = costsub.add_parser(
        "rca",
        help="root-cause a specific AWS cost anomaly (e.g. a cross-AZ "
             "DataTransfer-Regional-Bytes spike) by converging READ-ONLY "
             "evidence (Cost Explorer + VPC Flow Logs + EKS/NLB topology); "
             "prints the dominant/secondary cause with %% share, evidence "
             "convergence and ruled-out list, and writes rca.json")
    rca_src = co_rca.add_mutually_exclusive_group()
    rca_src.add_argument("--anomaly-file", dest="anomaly_file", default="",
                         help="path to a CE GetAnomalies JSON or a pasted "
                              "human anomaly report")
    rca_src.add_argument("--anomaly-id", dest="anomaly_id", default="",
                         help="AWS Cost Anomaly Detection AnomalyId to "
                              "fetch via ce get-anomalies (read-only)")
    rca_src.add_argument("--paste", action="store_true",
                         help="read the anomaly report from stdin")
    co_rca.add_argument("--profile", default="",
                        help="aws CLI profile (blank = default credential "
                             "chain; keys are never read or written)")
    co_rca.add_argument("--region", default="us-east-1",
                        help="AWS region (default: %(default)s)")
    co_rca.add_argument("--flow-logs-group", dest="flow_logs_group",
                        default="",
                        help="CloudWatch Logs group holding VPC Flow Logs "
                             "(enables cross-AZ byte attribution via "
                             "read-only Logs Insights)")
    co_rca.add_argument("--days", type=int, default=60,
                        help="lookback window in days for --anomaly-id "
                             "(default: %(default)s)")
    co_rca.add_argument("--config", "-c", default="",
                        help="mapping config JSON")
    co_rca.add_argument("--out", "-o", default=".",
                        help="output dir for rca.json (default: current "
                             "directory)")
    co_rca.set_defaults(func=cmd_cost_rca)

    co_mit = costsub.add_parser(
        "mitigate",
        help="turn an rca.json into a ranked, reliability-safe mitigation "
             "plan with paste-ready GENERIC configs (Mimir/Loki zone-aware, "
             "Service trafficDistribution, Karpenter, NLB); prints $ saved "
             "+ reliability preconditions + keeps_* flags and writes "
             "mitigation.json + config/. PROPOSES only -- never applied")
    co_mit.add_argument("rca", nargs="?", default="",
                        help="path to rca.json (default: ./rca.json)")
    co_mit.add_argument("--config", "-c", default="",
                        help="mapping config JSON")
    co_mit.add_argument("--out", "-o", default=".",
                        help="output dir for mitigation.json + config/ "
                             "(default: current directory)")
    co_mit.set_defaults(func=cmd_cost_mitigate)

    # -- deep-dive / AI context / Grafana MCP (1.6) ---------------------

    p_dd = sub.add_parser(
        "deepdive",
        help="deep-dive the LGTM stack from component self-metrics "
             "(capacity, cardinality, churn, network, Loki, Tempo) and, "
             "with --kube, Kubernetes topology / bin-pack / Karpenter; "
             "prints safe, dollar-estimated findings and writes "
             "deepdive-report.json + config/ snippets")
    p_dd.add_argument("--prom", default="",
                      help="Prometheus query URL (component self-metrics)")
    p_dd.add_argument("--mimir", default="",
                      help="Mimir query URL (component self-metrics)")
    p_dd.add_argument("--loki", default="", help="Loki query URL")
    p_dd.add_argument("--kube", action="store_true",
                      help="also analyze Kubernetes topology, bin-pack "
                           "and Karpenter (needs kubectl on PATH; degrades "
                           "cleanly without a cluster)")
    p_dd.add_argument("--pricing", default="",
                      help="pricing assumptions JSON file (overrides the "
                           "built-in defaults)")
    p_dd.add_argument("--config", "-c", default="",
                      help="mapping config JSON")
    p_dd.add_argument("--out", "-o", default=".",
                      help="output dir for deepdive-report.json + config/ "
                           "(default: current directory)")
    add_grafana_args(p_dd)
    p_dd.set_defaults(func=cmd_deepdive)

    p_aic = sub.add_parser(
        "ai-context",
        help="export a compact, LLM-optimized context bundle of every "
             "artifact (dashboard, requirements, diagnosis, parity, "
             "cost, deep-dive) so an AI can troubleshoot the migration "
             "and the LGTM stack")
    p_aic.add_argument("slug", nargs="?", default="",
                       help="dashboard slug (default: whole workspace)")
    p_aic.add_argument("--markdown", "-m", action="store_true",
                       help="emit Markdown instead of JSON")
    p_aic.add_argument("--out", "-o", default="",
                       help="write to FILE instead of stdout")
    p_aic.set_defaults(func=cmd_ai_context)

    p_ai = sub.add_parser(
        "ai",
        help="AI troubleshooting over the exported context bundle")
    p_ai.set_defaults(func=_need_sub(p_ai))
    aisub = p_ai.add_subparsers(dest="ai_command")
    ai_ts = aisub.add_parser(
        "troubleshoot",
        help="ask the configured AI backend a question with the full "
             "context bundle attached")
    ai_ts.add_argument("slug", nargs="?", default="",
                       help="dashboard slug (default: whole workspace)")
    ai_ts.add_argument("--question", "-q", default="",
                       help="the troubleshooting question")
    ai_ts.add_argument("--command", default="",
                       help="local console AI agent command (e.g. "
                            "'claude -p {prompt}'); or set N2G_AI_COMMAND")
    ai_ts.add_argument("--anthropic-key", dest="anthropic_key",
                       default="",
                       help="Anthropic API key; or set ANTHROPIC_API_KEY")
    ai_ts.add_argument("--model", default="", help="AI model override")
    ai_ts.set_defaults(func=cmd_ai_troubleshoot)

    p_mcp = sub.add_parser(
        "mcp",
        help="Grafana MCP: generate a local-AI config or probe a server")
    p_mcp.set_defaults(func=_need_sub(p_mcp))
    mcpsub = p_mcp.add_subparsers(dest="mcp_command")
    mc_cfg = mcpsub.add_parser(
        "config",
        help="emit a ready MCP servers config wiring the Grafana MCP "
             "server (+ optional nr2grafana context) into your local AI; "
             "the token is referenced via env, never written to the file")
    mc_cfg.add_argument("--kind", default="claude",
                        choices=["claude", "kiro", "generic"],
                        help="target AI client (default: %(default)s)")
    mc_cfg.add_argument("--context", default="",
                        help="path to an exported nr2grafana AI context so "
                             "the assistant can read it alongside Grafana")
    mc_cfg.add_argument("--no-grafana", action="store_true",
                        help="omit the Grafana MCP server entry")
    mc_cfg.add_argument("--no-n2g", action="store_true",
                        help="omit the nr2grafana MCP server entry")
    mc_cfg.add_argument("--aws-cost", action="store_true",
                        help="also register the awslabs AWS Cost Explorer "
                             "MCP server (uvx, read-only) for cost / anomaly "
                             "work; AWS auth via AWS_PROFILE/AWS_REGION, no "
                             "keys embedded")
    mc_cfg.add_argument("--aws-cloudwatch", action="store_true",
                        help="also register the awslabs CloudWatch MCP "
                             "server (uvx, read-only) for metrics + Logs "
                             "Insights over VPC Flow Logs")
    mc_cfg.add_argument("--out", "-o", default="",
                        help="write to FILE instead of stdout")
    add_grafana_args(mc_cfg)
    mc_cfg.set_defaults(func=cmd_mcp_config)
    mc_pr = mcpsub.add_parser(
        "probe",
        help="probe a Grafana MCP server (initialize + list tools)")
    mc_pr.add_argument("--url", default="",
                       help="http/SSE MCP endpoint")
    mc_pr.add_argument("--command", default="",
                       help="stdio MCP server command (e.g. 'mcp-grafana')")
    mc_pr.set_defaults(func=cmd_mcp_probe)
    mc_srv = mcpsub.add_parser(
        "serve",
        help="run nr2grafana AS a stdio JSON-RPC MCP server so a local AI "
             "(Claude/Kiro) can call every operation as a tool")
    mc_srv.set_defaults(func=cmd_mcp_serve)

    # -- AWS TCO trend analysis (1.7) -----------------------------------

    p_tco = sub.add_parser(
        "tco",
        help="analyze AWS total cost of ownership trends over time via "
             "Cost Explorer (strictly READ-ONLY, using your local aws "
             "CLI auth); ties cost movements to the optimizations this "
             "tool has made and forecasts. All figures are ESTIMATES.")
    p_tco.set_defaults(func=_need_sub(p_tco))
    tcosub = p_tco.add_subparsers(dest="tco_command")

    t_an = tcosub.add_parser(
        "analyze",
        help="discover monthly AWS spend, attribute the observability "
             "share, correlate with recorded optimizations and forecast; "
             "writes tco-report.json (ESTIMATES from your own CE data)")
    t_an.add_argument("--months", type=int, default=6,
                      help="months of history to analyze "
                           "(default: %(default)s)")
    t_an.add_argument("--group-by", dest="group_by", default="SERVICE",
                      choices=["SERVICE", "USAGE_TYPE"],
                      help="Cost Explorer grouping (default: %(default)s)")
    t_an.add_argument("--profile", default="",
                      help="aws CLI profile (blank = default credential "
                           "chain; keys are never read or written)")
    t_an.add_argument("--region", default="us-east-1",
                      help="AWS region for Cost Explorer "
                           "(default: %(default)s)")
    t_an.add_argument("--buckets", default="",
                      help="comma-separated S3 bucket names to attribute "
                           "to observability (mimir/loki/tempo)")
    t_an.add_argument("--out", "-o", default=".",
                      help="output dir for tco-report.json "
                           "(default: current directory)")
    t_an.set_defaults(func=cmd_tco_analyze)

    t_id = tcosub.add_parser(
        "identity",
        help="print the AWS caller identity (which account/role the "
             "read-only analysis runs as)")
    t_id.add_argument("--profile", default="",
                      help="aws CLI profile (blank = default chain)")
    t_id.add_argument("--region", default="us-east-1",
                      help="AWS region (default: %(default)s)")
    t_id.set_defaults(func=cmd_tco_identity)

    t_tr = tcosub.add_parser(
        "trend",
        help="diff the dated TCO snapshots recorded over time")
    t_tr.set_defaults(func=cmd_tco_trend)

    p_aws = sub.add_parser(
        "aws",
        help="AWS helpers (strictly READ-ONLY): list local aws profiles")
    p_aws.set_defaults(func=_need_sub(p_aws))
    awssub = p_aws.add_subparsers(dest="aws_command")
    a_pr = awssub.add_parser(
        "profiles",
        help="list AWS profile names from your local ~/.aws config (names "
             "only; no credential values are ever read)")
    a_pr.set_defaults(func=cmd_aws_profiles)

    p_api = sub.add_parser(
        "api",
        help="run nr2grafana's localhost web app as a headless, token-"
             "authed HTTP API for scripts and AI clients")
    p_api.set_defaults(func=_need_sub(p_api))
    apisub = p_api.add_subparsers(dest="api_command")
    a_srv = apisub.add_parser(
        "serve",
        help="launch the headless token-authed HTTP API (bearer token from "
             "--api-token or N2G_API_TOKEN; a non-loopback host is refused "
             "without a token)")
    a_srv.add_argument("--host", default="127.0.0.1",
                       help="bind address (default: %(default)s; a non-"
                            "loopback host REQUIRES an API token)")
    a_srv.add_argument("--port", type=int, default=8765,
                       help="port to listen on (default: %(default)s)")
    a_srv.add_argument("--api-token", dest="api_token", default="",
                       help="bearer token that programmatic clients must "
                            "present; or set N2G_API_TOKEN (the value is "
                            "never printed or logged)")
    a_srv.add_argument("--no-browser", action="store_true",
                       help="do not open a browser (the headless API is "
                            "non-interactive)")
    a_srv.set_defaults(func=cmd_api_serve)

    p_web = sub.add_parser(
        "web", help="launch the localhost web UI")
    p_web.add_argument("--port", type=int, default=8765,
                       help="port to listen on (default: %(default)s)")
    p_web.add_argument("--host", default="127.0.0.1",
                       help="bind address (default: %(default)s; keep it "
                            "localhost unless you know what you're doing)")
    p_web.add_argument("--no-browser", action="store_true",
                       help="don't open the browser automatically")
    p_web.set_defaults(func=cmd_web)

    def cmd_interactive(_args: argparse.Namespace) -> int:
        from .interactive import run_wizard
        return run_wizard()

    p_int = sub.add_parser(
        "interactive", aliases=["wizard", "run"],
        help="guided interactive mode (default when run with no arguments "
             "in a terminal)")
    p_int.set_defaults(func=cmd_interactive)

    # Only the workflow commands are listed in --help; everything else stays
    # available (see the epilog) but does not clutter the surface.
    if hasattr(sub, "_choices_actions"):
        sub._choices_actions = [a for a in sub._choices_actions
                                if a.dest in _PRIMARY_COMMANDS]

    args = ap.parse_args(argv)
    if getattr(args, "region", None):
        args.region = args.region.upper()
    json_out = bool(getattr(args, "json_out", False))
    func = getattr(args, "func", None)
    if not func:
        # No subcommand: interactive wizard in a terminal, help otherwise.
        if json_out:
            _emit_json(_command_name(args), False, None,
                       "no command given", sys.stdout)
            return 2
        if sys.stdin.isatty() and sys.stdout.isatty():
            return cmd_interactive(args)
        ap.print_help()
        return 2
    if json_out:
        return _run_json(_command_name(args), func, args)
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
