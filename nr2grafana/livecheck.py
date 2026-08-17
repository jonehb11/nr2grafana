"""Live query validation: submit generated queries to real engines.

Substitutes Grafana template variables with concrete values, then submits
each PromQL expr to Prometheus/Mimir (/api/v1/query) and each LogQL expr to
Loki (/loki/api/v1/query_range). A parse error surfaces as a failure; empty
results are fine (this validates syntax/labels, not data).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Tuple

SUBS = [
    (r"\$__rate_interval", "5m"),
    (r"\$__interval", "1m"),
    (r"\$__range", "1h"),
    (r"\$__auto", "5m"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*:regex\}", ".+"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "x"),
    (r"\$[A-Za-z_][A-Za-z0-9_]*", "x"),
]


def substitute(expr: str) -> str:
    for pat, rep in SUBS:
        expr = re.sub(pat, rep, expr)
    return expr


def _http_check(url: str, timeout: int = 15) -> str:
    """Returns '' on success, else an error description."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        if body.get("status") == "success":
            return ""
        return json.dumps(body)[:200]
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
            return str(detail.get("error") or detail.get("message") or e)
        except Exception:
            return "HTTP %d" % e.code
    except urllib.error.URLError as e:
        return "unreachable: %s" % e
    except Exception as e:  # noqa: BLE001 - report, don't crash the sweep
        return str(e)


def check_prom(base: str, expr: str) -> str:
    return _http_check(base.rstrip("/") + "/api/v1/query?query="
                       + urllib.parse.quote(expr))


def check_loki(base: str, expr: str) -> str:
    qs = urllib.parse.urlencode({"query": expr, "since": "1h", "limit": "1"})
    return _http_check(base.rstrip("/") + "/loki/api/v1/query_range?" + qs)


def iter_targets(dash: Dict[str, Any]):
    def walk(panels):
        for p in panels:
            for t in p.get("targets") or []:
                yield p, t
            if p.get("type") == "row":
                for x in walk(p.get("panels") or []):
                    yield x
    return walk(dash.get("panels") or [])


def collect_files(paths: List[str]) -> List[str]:
    files: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p))
                      if f.endswith(".json") and f != "migration-report.json"]
        elif os.path.isfile(p):
            files.append(p)
    return files


def check_files(paths: List[str], prom: str, loki: str,
                log: Callable[[str], None] = lambda m: None) \
        -> Tuple[int, int, int, List[Dict[str, str]]]:
    """Returns (passed, failed, skipped, failures)."""
    passed = failed = skipped = 0
    failures: List[Dict[str, str]] = []
    for path in collect_files(paths):
        with open(path) as f:
            dash = json.load(f)
        for panel, tgt in iter_targets(dash):
            ds_type = (tgt.get("datasource") or {}).get("type", "")
            expr = tgt.get("expr") or ""
            label = "%s panel %s %r [%s]" % (
                os.path.basename(path), panel.get("id"),
                (panel.get("title") or "")[:36], tgt.get("refId"))
            if ds_type == "prometheus" and expr:
                err = check_prom(prom, substitute(expr))
            elif ds_type == "loki" and expr:
                err = check_loki(loki, substitute(expr))
            else:
                skipped += 1
                log("  SKIP  %s (%s)" % (label, ds_type or "no expr"))
                continue
            if err:
                failed += 1
                failures.append({"target": label,
                                 "expr": substitute(expr), "error": err})
                log("  FAIL  %s\n        expr: %s\n        error: %s"
                    % (label, substitute(expr), err))
            else:
                passed += 1
                log("  ok    %s" % label)
    return passed, failed, skipped, failures
