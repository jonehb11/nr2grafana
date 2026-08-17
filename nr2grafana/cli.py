"""nr2grafana CLI.

Commands:
  fetch     Bulk-export dashboards from New Relic (NerdGraph) to JSON files
  convert   Convert NR dashboard JSON file(s) to Grafana dashboard JSON
  validate  Statically validate Grafana dashboard JSON file(s)
  list      List dashboards visible to the API key
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

from .config import DEFAULT_CONFIG, load_config
from .grafana.builder import build_dashboards, slugify
from .grafana.validate import validate_dashboard
from .model import parse_nr_dashboard
from .nerdgraph import NerdGraphClient, NerdGraphError


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

    files = _collect_inputs(args.inputs)
    if not files:
        _err("no input files")
        return 2
    os.makedirs(args.out, exist_ok=True)

    all_reports: List[Dict[str, Any]] = []
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
            out_path = os.path.join(args.out, filename)
            _write_json(out_path, dash)
            written.append(out_path)
            counts: Dict[str, int] = {}
            for r in report:
                counts[r["confidence"]] = counts.get(r["confidence"], 0) + 1
            summary = ", ".join("%d %s" % (v, k)
                                for k, v in sorted(counts.items()))
            print("%s -> %s  (%s)" % (os.path.basename(path), out_path,
                                      summary or "no widgets"),
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
    total = sum(len(r["widgets"]) for r in all_reports)
    review = sum(1 for r in all_reports for w in r["widgets"]
                 if w["confidence"] in ("needs-review", "untranslatable"))
    failure_note = (", %d input file(s) FAILED (see failed_inputs in the "
                    "report)" % len(failed_inputs)) if failed_inputs else ""
    print("\n%d dashboards written, %d widgets converted "
          "(%d need review)%s. Report: %s"
          % (len(written), total, review, failure_note, report_path),
          file=sys.stderr)
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
    p_conv.set_defaults(func=cmd_convert)

    p_val = sub.add_parser(
        "validate", help="validate Grafana dashboard JSON file(s)")
    p_val.add_argument("inputs", nargs="+",
                       help="Grafana dashboard JSON files or directories")
    p_val.set_defaults(func=cmd_validate)

    p_cfg = sub.add_parser(
        "example-config", help="print the default mapping config as JSON")
    p_cfg.set_defaults(func=cmd_example_config)

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
