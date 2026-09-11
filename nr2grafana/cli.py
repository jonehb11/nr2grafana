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


def cmd_convert(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _err(str(e))
        return 2
    if args.page_strategy:
        cfg["page_strategy"] = args.page_strategy
    if args.passthrough:
        cfg["passthrough_fallback"] = True
    package = bool(getattr(args, "package", False))

    files = _collect_inputs(args.inputs)
    if not files:
        _err("no input files")
        return 2
    os.makedirs(args.out, exist_ok=True)

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

    all_reports: List[Dict[str, Any]] = []
    index_entries: List[Dict[str, Any]] = []
    failed_inputs: List[Dict[str, str]] = []
    written: List[str] = []
    seen_files: Dict[str, int] = {}
    seen_uids: Dict[str, int] = {}
    seen_titles: Dict[str, int] = {}
    had_error = False
    for path in files:
        try:
            data = _load_json(path)
            nr = parse_nr_dashboard(data)
            outputs = build_dashboards(nr, cfg)
        except (json.JSONDecodeError, ValueError) as e:
            _err("%s: %s" % (path, e))
            failed_inputs.append({"source": path, "error": str(e)})
            had_error = True
            continue
        except Exception as e:  # a bad file must never kill a batch run
            _err("%s: conversion failed (%s: %s)"
                 % (path, type(e).__name__, e))
            failed_inputs.append({"source": path, "error": "%s: %s"
                                  % (type(e).__name__, e)})
            had_error = True
            continue
        for filename, dash, report in outputs:
            # Dedupe filenames, uids AND titles across the batch. Titles
            # matter because Grafana's overwrite:true import matches by
            # title within a folder — two dashboards named "Team Dashboard"
            # would silently overwrite each other even with distinct uids.
            seen_files[filename] = seen_files.get(filename, 0) + 1
            if seen_files[filename] > 1:
                stem = filename[:-len(".json")]
                filename = "%s-%d.json" % (stem, seen_files[filename])
            uid = dash.get("uid") or ""
            seen_uids[uid] = seen_uids.get(uid, 0) + 1
            if seen_uids[uid] > 1:
                suffix = "-%d" % seen_uids[uid]
                dash["uid"] = uid[:40 - len(suffix)] + suffix
            title = dash.get("title") or ""
            seen_titles[title] = seen_titles.get(title, 0) + 1
            if seen_titles[title] > 1:
                dash["title"] = "%s (%d)" % (title, seen_titles[title])
                print("note: duplicate dashboard name %r renamed to %r so "
                      "Grafana imports don't overwrite each other"
                      % (title, dash["title"]), file=sys.stderr)
            problems = validate_dashboard(dash)
            if problems:
                had_error = True
                _err("%s -> %s produced invalid output:" % (path, filename))
                for p in problems:
                    _err("  " + p)
            slug = filename[:-len(".json")] \
                if filename.endswith(".json") else filename
            out_path = ""
            extra = ""
            if package:
                try:
                    req = analyze_dashboard(nr, dash, report, cfg)
                    pkg = package_dashboard(args.out, slug, dash, report,
                                            req, cfg)
                except Exception as e:
                    had_error = True
                    _err("%s: packaging failed (%s: %s); writing flat "
                         "file instead" % (slug, type(e).__name__, e))
                else:
                    out_path = os.path.join(pkg, "dashboard.json")
                    extra = "; " + summarize(req)
                    index_entries.append({
                        "slug": slug, "title": dash.get("title"),
                        "dir": pkg, "widget_report": report,
                        "requirements": req,
                    })
                    _persist_package(store, slug, path, nr, dash, report,
                                     req, pkg)
            if not out_path:
                out_path = os.path.join(args.out, filename)
                _write_json(out_path, dash)
            written.append(out_path)
            counts: Dict[str, int] = {}
            for r in report:
                counts[r["confidence"]] = counts.get(r["confidence"], 0) + 1
            summary = ", ".join("%d %s" % (v, k)
                                for k, v in sorted(counts.items()))
            print("%s -> %s  (%s%s)" % (os.path.basename(path), out_path,
                                        summary or "no widgets", extra),
                  file=sys.stderr)
            all_reports.append({
                "source": path,
                "output": out_path,
                "dashboard": dash.get("title"),
                "widgets": report,
                "summary": counts,
            })

    report_path = args.report or os.path.join(args.out,
                                              "migration-report.json")
    _write_json(report_path, {"reports": all_reports,
                              "failed_inputs": failed_inputs})
    if package:
        idx = write_index(args.out, index_entries)
        print("Index: %s" % idx, file=sys.stderr)
    total = sum(len(r["widgets"]) for r in all_reports)
    review = sum(1 for r in all_reports for w in r["widgets"]
                 if w["confidence"] in ("needs-review", "untranslatable"))
    failure_note = (", %d input file(s) FAILED (see failed_inputs in the "
                    "report)" % len(failed_inputs)) if failed_inputs else ""
    print("\n%d dashboards written, %d widgets converted "
          "(%d need review)%s. Report: %s"
          % (len(written), total, review, failure_note, report_path),
          file=sys.stderr)
    if store is not None:
        try:
            if run_id:
                store.finish_run(run_id,
                                 "error" if had_error else "ok",
                                 {"dashboards": len(written),
                                  "widgets": total, "review": review,
                                  "failed_inputs": len(failed_inputs)})
        except (StoreError, ValueError):
            pass
        store.close()
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


def cmd_validate(args: argparse.Namespace) -> int:
    files = _collect_inputs(args.inputs)
    if not files:
        _err("no input files")
        return 2
    bad = 0
    for path in files:
        if os.path.basename(path) == "migration-report.json":
            continue
        try:
            dash = _load_json(path)
        except json.JSONDecodeError as e:
            print("%s: INVALID JSON: %s" % (path, e))
            bad += 1
            continue
        problems = validate_dashboard(dash)
        if problems:
            bad += 1
            print("%s: %d problem(s)" % (path, len(problems)))
            for p in problems:
                print("  - " + p)
        else:
            print("%s: OK" % path)
    return 1 if bad else 0


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

def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="nr2grafana",
        description="Convert New Relic dashboards to Grafana (LGTM stack) "
                    "dashboards.")
    sub = ap.add_subparsers(dest="command")

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
        help="convert NR dashboard JSON file(s)/dir(s) to Grafana JSON")
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
        "validate", help="validate Grafana dashboard JSON file(s)")
    p_val.add_argument("inputs", nargs="+",
                       help="Grafana dashboard JSON files or directories")
    p_val.set_defaults(func=cmd_validate)

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

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        # No subcommand: interactive wizard in a terminal, help otherwise.
        if sys.stdin.isatty() and sys.stdout.isatty():
            return cmd_interactive(args)
        ap.print_help()
        return 2
    if getattr(args, "region", None):
        args.region = args.region.upper()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
