"""Static validation of generated Grafana dashboard JSON.

Catches everything that would make an import fail or produce broken panels:
schema-level requirements, duplicate ids, malformed gridPos, empty or
unbalanced query expressions, bad datasource refs.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

_UID_RE = re.compile(r"^[a-zA-Z0-9\-_]{1,40}$")
_VAR_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")

_PAIRS = {"(": ")", "{": "}", "[": "]"}
_CLOSERS = {v: k for k, v in _PAIRS.items()}


def _balanced(expr: str) -> bool:
    stack: List[str] = []
    in_str = ""
    prev = ""
    for ch in expr:
        if in_str:
            if ch == in_str and prev != "\\":
                in_str = ""
            prev = "" if prev == "\\" else ch
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            prev = ""
            continue
        if ch in _PAIRS:
            stack.append(ch)
        elif ch in _CLOSERS:
            if not stack or stack.pop() != _CLOSERS[ch]:
                return False
        prev = ch
    return not stack and not in_str


def _iter_panels(dash: Dict[str, Any]):
    for p in dash.get("panels") or []:
        yield p
        if p.get("type") == "row":
            for child in p.get("panels") or []:
                yield child


def validate_dashboard(dash: Dict[str, Any]) -> List[str]:
    """Returns a list of problems; empty list = valid."""
    errs: List[str] = []
    if not isinstance(dash, dict):
        return ["dashboard is not a JSON object"]
    if not (dash.get("title") or "").strip():
        errs.append("dashboard title is empty")
    uid = dash.get("uid")
    if uid is not None and not _UID_RE.match(str(uid)):
        errs.append("uid %r invalid (allowed: [a-zA-Z0-9_-], max 40 chars)"
                    % uid)
    if dash.get("id") not in (None,):
        errs.append("dashboard 'id' must be null for import")
    if not isinstance(dash.get("schemaVersion"), int):
        errs.append("schemaVersion missing")
    if not isinstance(dash.get("panels"), list):
        errs.append("panels must be a list")
        return errs

    try:
        json.dumps(dash)
    except (TypeError, ValueError) as e:
        errs.append("dashboard not JSON-serializable: %s" % e)

    seen_ids = set()
    for p in _iter_panels(dash):
        pid = p.get("id")
        where = "panel id=%s title=%r" % (pid, p.get("title", ""))
        if not isinstance(pid, int):
            errs.append("%s: id missing or not an int" % where)
        elif pid in seen_ids:
            errs.append("%s: duplicate panel id" % where)
        else:
            seen_ids.add(pid)
        if not p.get("type"):
            errs.append("%s: missing panel type" % where)
        gp = p.get("gridPos") or {}
        for k in ("h", "w", "x", "y"):
            if not isinstance(gp.get(k), int):
                errs.append("%s: gridPos.%s missing/not int" % (where, k))
        if isinstance(gp.get("x"), int) and isinstance(gp.get("w"), int):
            if gp["x"] < 0 or gp["w"] < 1 or gp["x"] + gp["w"] > 24:
                errs.append("%s: gridPos out of 24-column bounds (x=%s w=%s)"
                            % (where, gp["x"], gp["w"]))
        if isinstance(gp.get("h"), int) and gp["h"] < 1:
            errs.append("%s: gridPos.h < 1" % where)

        if p.get("type") in ("text", "row"):
            continue
        refids = set()
        for t in p.get("targets") or []:
            rid = t.get("refId")
            if not rid:
                errs.append("%s: target missing refId" % where)
            elif rid in refids:
                errs.append("%s: duplicate refId %s" % (where, rid))
            refids.add(rid)
            ds = t.get("datasource")
            if not (isinstance(ds, dict) and ds.get("type")
                    and ds.get("uid")):
                errs.append("%s: target %s datasource ref malformed"
                            % (where, rid))
            expr = t.get("expr") or t.get("query") or t.get("queryText")
            if not expr:
                errs.append("%s: target %s has no expr/query" % (where, rid))
            elif not _balanced(expr):
                errs.append("%s: target %s expression has unbalanced "
                            "brackets/quotes: %s" % (where, rid, expr))
            if isinstance(ds, dict) and ds.get("type") == "loki":
                e = t.get("expr", "")
                if "{}" in e.replace(" ", ""):
                    errs.append("%s: Loki query has an empty stream "
                                "selector {}" % where)

    names = set()
    for v in (dash.get("templating") or {}).get("list") or []:
        n = v.get("name", "")
        if not _VAR_NAME_RE.match(n or ""):
            errs.append("template variable name %r invalid" % n)
        if n in names:
            errs.append("duplicate template variable %r" % n)
        names.add(n)
        if not v.get("type"):
            errs.append("template variable %r missing type" % n)

    # Every ${var} datasource uid must have a matching datasource variable.
    ds_vars = {v.get("name") for v in
               (dash.get("templating") or {}).get("list") or []
               if v.get("type") == "datasource"}
    for p in _iter_panels(dash):
        for t in (p.get("targets") or []):
            ds = t.get("datasource") or {}
            uid = str(ds.get("uid", ""))
            m = re.fullmatch(r"\$\{([A-Za-z0-9_]+)\}", uid)
            if m and m.group(1) not in ds_vars:
                errs.append("panel id=%s references datasource variable "
                            "${%s} but no such datasource variable exists"
                            % (p.get("id"), m.group(1)))
    for v in (dash.get("templating") or {}).get("list") or []:
        ds = v.get("datasource") or {}
        if isinstance(ds, dict):
            uid = str(ds.get("uid", ""))
            m = re.fullmatch(r"\$\{([A-Za-z0-9_]+)\}", uid)
            if m and m.group(1) not in ds_vars:
                errs.append("template variable %r references datasource "
                            "variable ${%s} but no such datasource variable "
                            "exists" % (v.get("name"), m.group(1)))
    return errs
