"""Root-cause diagnostics: explain WHY a panel shows no data, and how
to fix it.

Given a live Grafana connection and whatever artifacts exist (the
NerdGraph client, converted dashboard, requirements analysis, per-panel
test results, NR-vs-Grafana parity report), run layered checks and
produce findings that name the root cause and, where possible, carry a
machine-applicable fix consumed by ``nr2grafana.remediate.apply_fix``:

1. auth        -- are the Grafana token and NR key usable, and does the
                  token's role suffice (Editor+ for dashboards, Admin
                  for datasource creation)?
2. datasource  -- do required datasources/plugins exist, and do the
                  existing ones pass a health check?
3. panel       -- per no-data panel: missing metric (did-you-mean via
                  difflib, ``_total`` suffix flip), offending label
                  matcher located by matcher elimination (drop one
                  matcher at a time and instant-query), actual label
                  values listed; Loki stream-label mismatches and
                  json-vs-logfmt parser-stage mismatches.
4. data        -- New Relic has data but the metric family is entirely
                  absent from Grafana: name the missing ingestion
                  pipeline or datasource.
5. config      -- recurring renames consolidated into one mergeable
                  converter config overlay.

Output schema ``nr2grafana/diagnosis/v1``; persisted by callers as
Store artifact kind ``"diagnosis"``. Every check degrades gracefully
when a client or artifact is missing, and :func:`diagnose` never raises
on network failures. All Grafana/New Relic access is read-only.
"""

from __future__ import annotations

import difflib
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .grafana.client import GrafanaError
from .grafana.live import DS_TEMPLATES

SCHEMA = "nr2grafana/diagnosis/v1"
ARTIFACT_KIND = "diagnosis"

# Matcher-elimination probe budget: cheap instant queries used to find
# the label matcher that empties a selector. Hard caps keep diagnosis
# fast even on large dashboards.
MAX_PROBES_PER_PANEL = 8
MAX_PROBES_TOTAL = 60

_SEV_ORDER = {"blocker": 0, "warn": 1, "info": 2}

# difflib similarity at/above which a suggestion is high confidence
# (auto_heal may apply it on its own).
_HIGH_CONFIDENCE = 0.8
_CLOSE_CUTOFF = 0.6

_PROM_KEYWORDS = {
    "by", "without", "on", "ignoring", "group_left", "group_right",
    "bool", "offset", "and", "or", "unless", "atan2", "inf", "nan",
}

_GROUP_RE = re.compile(
    r"\b(by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)",
    re.I)
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`|\'(?:[^\'\\]|\\.)*\'')
_RANGE_RE = re.compile(r"\[[^\]]*\]")
_IDENT_RE = re.compile(r"(?<![0-9a-zA-Z_.:$])[a-zA-Z_:][a-zA-Z0-9_:]*")
_METRIC_TAIL = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*$")
_MATCHER_RE = re.compile(
    r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*'
    r'("(?:[^"\\]|\\.)*"|`[^`]*`|\'(?:[^\'\\]|\\.)*\')')
_PARSER_STAGE_RE = re.compile(r"\|\s*(json|logfmt)\b")
_LOKI_HINT_RE = re.compile(r"\|=|\|~|\|\s*(json|logfmt|pattern|unpack)")


# ---------------------------------------------------------------------------
# PromQL/LogQL selector scanning (quote-aware; local so this module does
# not depend on private helpers of requirements.py)
# ---------------------------------------------------------------------------

def _skip_string(s: str, i: int) -> int:
    """Index just past the string literal starting at ``s[i]``."""
    quote = s[i]
    j = i + 1
    n = len(s)
    while j < n:
        c = s[j]
        if quote != "`" and c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        j += 1
    return n


def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'", "`"):
        body = tok[1:-1]
        if tok[0] != "`":
            body = body.replace("\\" + tok[0], tok[0])
            body = body.replace("\\\\", "\\")
        return body
    return tok


def _scan_selectors(expr: str) -> List[Dict[str, Any]]:
    """Every ``metric{matchers}`` selector in an expression.

    Returns dicts ``{"metric", "selector", "matchers", "start", "end"}``
    where ``matchers`` are ``{"label", "op", "value", "text"}`` (text is
    the exact matcher substring, usable for surgical replacement) and
    start/end span the ``{...}`` part. Quote-aware character scan, so
    braces inside matcher values do not confuse it.
    """
    out: List[Dict[str, Any]] = []
    i, n = 0, len(expr or "")
    while i < n:
        ch = expr[i]
        if ch in ('"', "'", "`"):
            i = _skip_string(expr, i)
            continue
        if ch == "{":
            j, depth = i + 1, 1
            while j < n and depth:
                c = expr[j]
                if c in ('"', "'", "`"):
                    j = _skip_string(expr, j)
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                j += 1
            body = expr[i:j]
            m = _METRIC_TAIL.search(expr[:i].rstrip())
            metric = m.group(0) if m else ""
            if metric.lower() in _PROM_KEYWORDS:
                metric = ""
            matchers = []
            for mm in _MATCHER_RE.finditer(body):
                matchers.append({"label": mm.group(1),
                                 "op": mm.group(2),
                                 "value": _unquote(mm.group(3)),
                                 "text": mm.group(0)})
            out.append({"metric": metric, "selector": body,
                        "matchers": matchers, "start": i, "end": j})
            i = j
            continue
        i += 1
    return out


def _prom_metrics(expr: str) -> List[str]:
    """Metric names a PromQL expression expects to exist."""
    sels = _scan_selectors(expr or "")
    metrics: List[str] = []
    for sel in sels:
        name = sel["metric"]
        if not name:
            for m in sel["matchers"]:
                if m["label"] == "__name__" and m["op"] in ("=", "=~"):
                    name = m["value"]
        if name and name not in metrics:
            metrics.append(name)
    # Blank out the selector spans, then look for bare metric idents.
    chars = list(expr or "")
    for sel in sels:
        for k in range(sel["start"], sel["end"]):
            chars[k] = " "
    cleaned = _GROUP_RE.sub(" ", "".join(chars))
    cleaned = _STRING_RE.sub('""', cleaned)
    cleaned = _RANGE_RE.sub(" ", cleaned)
    for m in _IDENT_RE.finditer(cleaned):
        tok = m.group(0)
        if cleaned[m.end():].lstrip().startswith("("):
            continue  # function call
        if tok.lower() in _PROM_KEYWORDS or tok in metrics:
            continue
        metrics.append(tok)
    return metrics


def _replace_metric(expr: str, old: str, new: str) -> str:
    return re.sub(r"(?<![A-Za-z0-9_:])%s(?![A-Za-z0-9_:])"
                  % re.escape(old), new, expr)


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _confidence(a: str, b: str) -> str:
    return "high" if _similarity(a, b) >= _HIGH_CONFIDENCE else "medium"


# ---------------------------------------------------------------------------
# Finding construction
# ---------------------------------------------------------------------------

def _add_finding(findings: List[Dict[str, Any]], used_ids: set,
                 fid: str, severity: str, area: str, problem: str,
                 evidence: str, fix_desc: str, fix_kind: str = "none",
                 action: Optional[Dict[str, Any]] = None,
                 panel_id: Optional[Any] = None,
                 confidence: str = "") -> Dict[str, Any]:
    base, n = fid, 2
    while fid in used_ids:
        fid = "%s-%d" % (base, n)
        n += 1
    used_ids.add(fid)
    finding: Dict[str, Any] = {
        "id": fid, "severity": severity, "area": area,
        "problem": problem, "evidence": evidence,
        "fix": {"description": fix_desc, "kind": fix_kind},
    }
    if action is not None:
        finding["fix"]["action"] = action
    if panel_id is not None:
        finding["panel_id"] = panel_id
    if confidence:
        finding["confidence"] = confidence
    findings.append(finding)
    return finding


def _ds_template_action(plugin_id: str,
                        name: str = "") -> Optional[Dict[str, Any]]:
    """Ready create_datasource payload template for a plugin type.

    Fields the user must supply (required or secret ones) are listed in
    ``needs_input``; remediate.apply_fix refuses to auto-create until
    they are filled in, so no placeholder secrets ever hit the wire.
    """
    tpl = DS_TEMPLATES.get(plugin_id)
    if tpl is None:
        return None
    action: Dict[str, Any] = {"name": name or tpl["label"],
                              "type": tpl["plugin_id"],
                              "access": "proxy"}
    needs: List[str] = []
    for field in tpl.get("fields") or []:
        if field.get("path") == "url":
            action["url"] = ""
        if field.get("required") or field.get("secret"):
            if field["name"] not in needs:
                needs.append(field["name"])
    action["needs_input"] = needs
    return action


def _template_inputs(plugin_id: str) -> str:
    """Human list of the inputs a datasource template needs."""
    tpl = DS_TEMPLATES.get(plugin_id)
    if tpl is None:
        return ""
    parts = []
    for field in tpl.get("fields") or []:
        if field.get("required") or field.get("secret"):
            parts.append("%s (%s)" % (field["name"], field["label"]))
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# ds_query response inspection + probe machinery
# ---------------------------------------------------------------------------

def _resp_points(resp: Any, ref_id: str = "A") -> Optional[int]:
    """Data points in a /api/ds/query response; None if it errored."""
    if not isinstance(resp, dict):
        return None
    res = (resp.get("results") or {}).get(ref_id) or {}
    if res.get("error") or res.get("errors"):
        return None
    status = res.get("status")
    if isinstance(status, int) and status >= 400:
        return None
    total = 0
    for frame in res.get("frames") or []:
        if not isinstance(frame, dict):
            continue
        values = (frame.get("data") or {}).get("values") or []
        total += max((len(c) for c in values if isinstance(c, list)),
                     default=0)
    return total


def _probe(grafana: Any, uid: str, ds_type: str, expr: str,
           budget: Dict[str, Any]) -> Optional[bool]:
    """One capped probe query; True/False = data/empty, None = unknown."""
    if budget["panel"] <= 0 or budget["total"] <= 0:
        budget["capped"] = True
        return None
    budget["panel"] -= 1
    budget["total"] -= 1
    try:
        resp = grafana.ds_query(uid, ds_type,
                                {"refId": "A", "expr": expr},
                                frm="now-1h")
    except Exception:
        return None
    pts = _resp_points(resp)
    if pts is None:
        return None
    return pts > 0


def _selector_text(metric: str,
                   matchers: List[Dict[str, Any]]) -> str:
    if matchers:
        return "%s{%s}" % (metric, ",".join(m["text"] for m in matchers))
    return metric


def _eliminate_matchers(grafana: Any, uid: str, sel: Dict[str, Any],
                        budget: Dict[str, Any]) \
        -> Tuple[str, Optional[Dict[str, Any]]]:
    """Find the matcher whose removal makes the selector return data.

    Returns ("outer", None) when the full selector already has data,
    ("matcher", m) with the offending matcher, or ("none", None) when
    no single matcher restores data (or the probe budget ran out).
    """
    metric = sel["metric"]
    droppable = [m for m in sel["matchers"] if m["label"] != "__name__"]
    pinned = [m for m in sel["matchers"] if m["label"] == "__name__"]
    full = _selector_text(metric, pinned + droppable)
    if not full:
        return "none", None
    if _probe(grafana, uid, "prometheus", "count(%s)" % full, budget):
        return "outer", None
    for i, matcher in enumerate(droppable):
        kept = pinned + droppable[:i] + droppable[i + 1:]
        text = _selector_text(metric, kept)
        if not text:
            continue
        if _probe(grafana, uid, "prometheus", "count(%s)" % text,
                  budget):
            return "matcher", matcher
    return "none", None


# ---------------------------------------------------------------------------
# Layer 1: auth
# ---------------------------------------------------------------------------

def _check_auth(findings: List[Dict[str, Any]], ids: set,
                grafana: Any, nr: Any,
                emit: Callable[[str], None]) -> None:
    if grafana is not None:
        _check_grafana_auth(findings, ids, grafana, emit)
    if nr is not None:
        _check_nr_auth(findings, ids, nr, emit)


def _check_grafana_auth(findings: List[Dict[str, Any]], ids: set,
                        grafana: Any,
                        emit: Callable[[str], None]) -> None:
    req = getattr(grafana, "_req", None)
    if not callable(req):
        return
    err = ""
    try:
        req("GET", "/api/user")
    except Exception as e:
        err = str(e)
    if err and "401" in err:
        _add_finding(
            findings, ids, "grafana-auth", "blocker", "auth",
            "Grafana rejected the service-account token (HTTP 401).",
            "GET /api/user: %s" % err,
            "Create a new service-account token in Grafana "
            "(Administration -> Service accounts) and enter it in "
            "settings. Editor role is enough to import dashboards; "
            "Admin is needed to create datasources.",
            "credentials")
        return
    if err and "403" in err:
        # Some setups deny /api/user to service accounts; /api/org
        # still answering means the token itself works.
        try:
            req("GET", "/api/org")
            err = ""
        except Exception as e:
            err = str(e)
        if err:
            _add_finding(
                findings, ids, "grafana-role", "blocker", "auth",
                "The Grafana token authenticates but its role is "
                "insufficient (HTTP 403).",
                "GET /api/user and /api/org both denied: %s" % err,
                "Grant the service account a higher role: Editor or "
                "above to import/update dashboards, Admin to create "
                "datasources.",
                "credentials")
            return
    if err:
        _add_finding(
            findings, ids, "grafana-unreachable", "blocker", "auth",
            "Grafana could not be reached.",
            "GET /api/user: %s" % err,
            "Check the Grafana URL (scheme, host, port) and that it "
            "is reachable from this machine; for self-signed TLS use "
            "the insecure option.",
            "credentials")
        return
    # Token works; check what the role actually allows.
    perms_fn = getattr(grafana, "permissions_report", None)
    if not callable(perms_fn):
        return
    try:
        report = perms_fn()
    except Exception:
        return
    if not isinstance(report, dict):
        return
    if not report.get("can_edit_dashboards", True):
        _add_finding(
            findings, ids, "grafana-role-editor", "blocker", "auth",
            "The token cannot edit dashboards.",
            report.get("detail", ""),
            "Grant the service account the Editor role (or higher) so "
            "dashboards can be imported and updated.",
            "credentials")
    elif not report.get("can_admin_datasources", True):
        _add_finding(
            findings, ids, "grafana-role-admin", "info", "auth",
            "The token cannot create datasources (Editor-level).",
            report.get("detail", ""),
            "Dashboard import will work; to let nr2grafana create "
            "missing datasources, grant the service account the "
            "Admin role.",
            "credentials")
    emit("auth: grafana token ok (%s)"
         % (report.get("user") or "service account"))


def _check_nr_auth(findings: List[Dict[str, Any]], ids: set, nr: Any,
                   emit: Callable[[str], None]) -> None:
    post = getattr(nr, "_post", None)
    if not callable(post):
        return
    try:
        data = post("{ actor { user { email } } }")
    except Exception as e:
        _add_finding(
            findings, ids, "nr-auth", "blocker", "auth",
            "The New Relic API key was rejected or NerdGraph is "
            "unreachable.",
            "NerdGraph {actor{user{email}}}: %s" % e,
            "Use a USER API key (NRAK-...) with access to the "
            "account, and make sure the region (US/EU) matches where "
            "the account lives.",
            "credentials")
        return
    email = ""
    if isinstance(data, dict):
        email = str(((data.get("actor") or {}).get("user") or {})
                    .get("email") or "")
    emit("auth: new relic key ok (%s)" % (email or "user"))


# ---------------------------------------------------------------------------
# Layer 2: datasources
# ---------------------------------------------------------------------------

def _check_datasources(findings: List[Dict[str, Any]], ids: set,
                       grafana: Any,
                       requirements: Optional[Dict[str, Any]],
                       emit: Callable[[str], None]) -> None:
    if grafana is None or not requirements:
        return
    plugin_by_family: Dict[str, str] = {}
    for ds in requirements.get("datasources") or []:
        fam = ds.get("family", "")
        plugin_by_family[fam] = ds.get("plugin_id", fam)
    try:
        rows = grafana.check_requirements(requirements)
    except Exception as e:
        _add_finding(
            findings, ids, "datasource-check-failed", "warn",
            "datasource",
            "Datasource requirements could not be checked.",
            str(e),
            "Check the Grafana URL/token, then re-run the check.",
            "none")
        return
    for row in rows or []:
        item = str(row.get("item", ""))
        status = row.get("status", "")
        if status == "ok":
            continue
        if item.startswith("datasource:"):
            family = item.split(":", 1)[1]
            plugin = plugin_by_family.get(family, family)
            if status == "missing":
                action = _ds_template_action(plugin)
                inputs = _template_inputs(plugin)
                desc = ("Create a %s datasource (type %s)." % (family,
                                                               plugin))
                if inputs:
                    desc += " Input needed: %s." % inputs
                notes = (DS_TEMPLATES.get(plugin) or {}).get("notes", "")
                if notes:
                    desc += " " + notes
                _add_finding(
                    findings, ids, "ds-%s-missing" % family, "blocker",
                    "datasource",
                    "Required %s datasource is missing; every panel "
                    "using it will show no data." % family,
                    row.get("detail", ""),
                    desc, "add-datasource", action)
            elif status == "wrong-type":
                _add_finding(
                    findings, ids, "ds-%s-wrong-type" % family,
                    "blocker", "datasource",
                    "A datasource with the expected uid exists but "
                    "has the wrong type.",
                    row.get("detail", ""),
                    row.get("fix", "") or
                    "Repoint the dashboard at a %s datasource."
                    % family,
                    "none")
            else:
                _add_finding(
                    findings, ids, "ds-%s-%s" % (family, status),
                    "warn", "datasource",
                    "Datasource %s: %s." % (family, status),
                    row.get("detail", ""), row.get("fix", ""), "none")
        elif item.startswith("plugin:"):
            pid = item.split(":", 1)[1]
            _add_finding(
                findings, ids, "plugin-%s-missing" % pid, "blocker",
                "datasource",
                "Required Grafana plugin %s is not installed." % pid,
                row.get("detail", ""),
                row.get("fix", "")
                or "grafana-cli plugins install %s, then restart "
                   "Grafana." % pid,
                "install-plugin")
        else:
            _add_finding(
                findings, ids, "requirement-%s" % (item or "unknown"),
                "warn", "datasource",
                "Requirement %r is not satisfied (%s)." % (item, status),
                row.get("detail", ""), row.get("fix", ""), "none")
    _check_ds_health(findings, ids, grafana, plugin_by_family, emit)


def _check_ds_health(findings: List[Dict[str, Any]], ids: set,
                     grafana: Any, plugin_by_family: Dict[str, str],
                     emit: Callable[[str], None]) -> None:
    health_fn = getattr(grafana, "datasource_health", None)
    if not callable(health_fn):
        return
    try:
        dss = grafana.datasources()
    except Exception:
        return
    checked = set()
    for family, plugin in sorted(plugin_by_family.items()):
        matches = [d for d in dss or [] if d.get("type") == plugin]
        chosen = next((d for d in matches if d.get("isDefault")), None) \
            or (matches[0] if matches else None)
        if chosen is None:
            continue
        uid = chosen.get("uid", "")
        if not uid or uid in checked:
            continue
        checked.add(uid)
        try:
            health = health_fn(uid)
        except Exception as e:
            health = {"status": "error", "message": str(e)}
        status = (health or {}).get("status", "unknown")
        message = (health or {}).get("message", "")
        if status == "ok":
            emit("datasource %s (%s): healthy" % (family, uid))
            continue
        if status == "error":
            _add_finding(
                findings, ids, "ds-%s-health" % family, "blocker",
                "datasource",
                "The %s datasource %r exists but fails its health "
                "check, so its panels get no data."
                % (family, chosen.get("name", uid)),
                "health: %s" % (message or "error"),
                "Open the datasource settings and fix the failing "
                "part. Likely causes: the URL is not reachable FROM "
                "THE GRAFANA SERVER (not your browser), wrong or "
                "expired credentials, or an untrusted TLS "
                "certificate.",
                "none", None, None)
        else:
            _add_finding(
                findings, ids, "ds-%s-health" % family, "info",
                "datasource",
                "Health of the %s datasource %r could not be "
                "determined." % (family, chosen.get("name", uid)),
                message,
                "Open the datasource in Grafana and press 'Save & "
                "test' to verify it manually.",
                "none")


# ---------------------------------------------------------------------------
# Layer 3: per-panel root cause
# ---------------------------------------------------------------------------

def _problem_rows(test_results: Optional[List[Dict[str, Any]]],
                  parity: Optional[Dict[str, Any]]) \
        -> List[Dict[str, Any]]:
    """Merge test/parity rows into per-target problem rows."""
    rows: List[Dict[str, Any]] = []
    seen = set()
    for row in test_results or []:
        status = row.get("status", "")
        if status not in ("no-data", "error"):
            continue
        key = (row.get("panel_id"), row.get("refId") or "")
        seen.add(key)
        rows.append({"panel_id": row.get("panel_id"),
                     "refId": row.get("refId") or "",
                     "expr": row.get("expr") or "",
                     "uid": row.get("datasource") or "",
                     "status": status,
                     "error": row.get("error") or ""})
    for row in (parity or {}).get("panels") or []:
        verdict = row.get("verdict", "")
        if verdict not in ("gf-empty", "gf-error"):
            continue
        key = (row.get("panel_id"), row.get("refId") or "")
        if key in seen:
            continue
        seen.add(key)
        rows.append({"panel_id": row.get("panel_id"),
                     "refId": row.get("refId") or "",
                     "expr": row.get("expr") or "",
                     "uid": row.get("datasource") or "",
                     "status": "no-data" if verdict == "gf-empty"
                               else "error",
                     "error": row.get("detail") or ""})
    return rows


def _iter_panels(dash: Optional[Dict[str, Any]]):
    for panel in (dash or {}).get("panels") or []:
        yield panel
        for child in panel.get("panels") or []:
            yield child


def _target_types(dash: Optional[Dict[str, Any]],
                  requirements: Optional[Dict[str, Any]]) \
        -> Tuple[Dict[Tuple[Any, str], str], Dict[Any, str]]:
    by_target: Dict[Tuple[Any, str], str] = {}
    for panel in _iter_panels(dash):
        for tgt in panel.get("targets") or []:
            ds = tgt.get("datasource") or {}
            by_target[(panel.get("id"), tgt.get("refId") or "")] = \
                ds.get("type", "")
    by_panel: Dict[Any, str] = {}
    for exp in (requirements or {}).get("data_expectations") or []:
        by_panel.setdefault(exp.get("panel_id"),
                            exp.get("datasource", ""))
    return by_target, by_panel


def _guess_type(expr: str) -> str:
    if expr.lstrip().startswith("{") or _LOKI_HINT_RE.search(expr or ""):
        return "loki"
    return "prometheus"


class _PanelChecker(object):
    """State shared across per-panel checks: inventories, budget,
    collected renames for the config layer and truly-absent metrics
    for the pipeline layer."""

    def __init__(self, grafana, findings, ids, cfg, emit):
        self.grafana = grafana
        self.findings = findings
        self.ids = ids
        self.cfg = cfg or {}
        self.emit = emit
        self.budget = {"total": MAX_PROBES_TOTAL, "panel": 0,
                       "capped": False}
        self.metric_map: Dict[str, str] = {}
        self.label_map: Dict[str, str] = {}
        self.flags: Dict[str, Any] = {}
        self.absent: Dict[Any, List[str]] = {}
        self._metrics_cache: Dict[str, Optional[List[str]]] = {}
        self._loki_labels_cache: Dict[str, Optional[List[str]]] = {}

    # -- inventories -------------------------------------------------------

    def metric_names(self, uid: str) -> Optional[List[str]]:
        if uid not in self._metrics_cache:
            names: Optional[List[str]] = None
            try:
                got = self.grafana.prom_metric_names(uid)
                if got:
                    names = [str(v) for v in got]
            except Exception:
                names = None
            self._metrics_cache[uid] = names
        return self._metrics_cache[uid]

    def loki_label_names(self, uid: str) -> Optional[List[str]]:
        if uid not in self._loki_labels_cache:
            names = None
            try:
                got = self.grafana.loki_labels(uid)
                if got:
                    names = [str(v) for v in got]
            except Exception:
                names = None
            self._loki_labels_cache[uid] = names
        return self._loki_labels_cache[uid]

    def label_values(self, uid: str, label: str,
                     match: str = "") -> List[str]:
        try:
            return [str(v) for v in
                    self.grafana.prom_label_values(uid, label, match)]
        except Exception:
            return []

    def loki_values(self, uid: str, label: str) -> List[str]:
        try:
            return [str(v) for v in
                    self.grafana.loki_label_values(uid, label)]
        except Exception:
            return []

    def series_labels(self, uid: str, match: str) -> List[str]:
        names: set = set()
        try:
            for s in self.grafana.prom_series(uid, match) or []:
                if isinstance(s, dict):
                    names.update(k for k in s if k != "__name__")
        except Exception:
            pass
        return sorted(names)

    # -- error rows --------------------------------------------------------

    def check_error_row(self, row: Dict[str, Any]) -> None:
        pid, ref = row["panel_id"], row["refId"]
        err = row["error"]
        low = err.lower()
        if "401" in err or "unauthorized" in low:
            _add_finding(
                self.findings, self.ids, "panel-%s-auth" % pid,
                "blocker", "auth",
                "Panel %s query was rejected as unauthorized." % pid,
                err,
                "The datasource credentials are invalid; fix them in "
                "the datasource settings (Grafana proxies the query, "
                "so the datasource's own auth is what failed).",
                "credentials", None, pid)
        elif "unresolved datasource" in low:
            _add_finding(
                self.findings, self.ids,
                "panel-%s-datasource-unresolved" % pid, "blocker",
                "datasource",
                "Panel %s references a datasource that does not "
                "exist on this Grafana instance." % pid,
                err,
                "Create the missing datasource (see the datasource "
                "findings) or bind the dashboard variable to an "
                "existing one.",
                "none", None, pid)
        else:
            _add_finding(
                self.findings, self.ids,
                "panel-%s-query-error" % pid, "warn", "panel",
                "Panel %s query [%s] fails to execute."
                % (pid, ref or "A"),
                err,
                "Fix the query; the datasource returned the error "
                "above verbatim.",
                "none", None, pid)

    # -- prometheus --------------------------------------------------------

    def check_prom_panel(self, row: Dict[str, Any]) -> None:
        pid, ref, expr, uid = (row["panel_id"], row["refId"],
                               row["expr"], row["uid"])
        inventory = self.metric_names(uid)
        if inventory is None:
            _add_finding(
                self.findings, self.ids,
                "metrics-unavailable-%s" % uid, "info", "data",
                "The metric inventory of datasource %r could not be "
                "read, so metric-name checks were skipped." % uid,
                "GET /api/datasources/proxy/uid/%s/api/v1/label/"
                "__name__/values returned nothing" % uid,
                "Check that the datasource proxy works and the "
                "backend answers the Prometheus label-values API.",
                "none")
            return
        invset = set(inventory)
        metrics = _prom_metrics(expr)
        missing = [m for m in metrics if m not in invset]
        for metric in missing:
            self._missing_metric(pid, ref, expr, metric, inventory,
                                 invset)
        if missing or row["status"] != "no-data":
            return
        self.budget["panel"] = MAX_PROBES_PER_PANEL
        probed = False
        for sel in _scan_selectors(expr):
            if not sel["matchers"]:
                continue
            probed = True
            self._eliminate(pid, ref, expr, uid, sel)
            break
        if not probed and metrics:
            _add_finding(
                self.findings, self.ids, "panel-%s-no-points" % pid,
                "info", "panel",
                "Panel %s: metric %r exists but returned no points "
                "in the tested window." % (pid, metrics[0]),
                "expr: %s" % expr,
                "Widen the dashboard time range or confirm the "
                "metric is currently being written.",
                "none", None, pid)

    def _missing_metric(self, pid: Any, ref: str, expr: str,
                        metric: str, inventory: List[str],
                        invset: set) -> None:
        # _total suffix flip first: it is the most specific signal.
        flipped = ""
        if metric.endswith("_total") and metric[:-6] in invset:
            flipped = metric[:-6]
            suffix_setting = False
        elif metric + "_total" in invset:
            flipped = metric + "_total"
            suffix_setting = True
        if flipped:
            new_expr = _replace_metric(expr, metric, flipped)
            self.flags["metric_total_suffix"] = suffix_setting
            _add_finding(
                self.findings, self.ids,
                "panel-%s-metric-total-suffix" % pid, "warn", "panel",
                "Panel %s: metric %r does not exist, but %r does - "
                "your pipeline %s the Prometheus '_total' counter "
                "suffix." % (pid, metric, flipped,
                             "appends" if suffix_setting
                             else "does not append"),
                "datasource has %r; query asks for %r"
                % (flipped, metric),
                "Use %r in the query (set config metric_total_suffix "
                "to %s so future conversions match)."
                % (flipped, suffix_setting),
                "edit-query",
                {"panel_id": pid, "refId": ref or "A",
                 "new_expr": new_expr},
                pid, "high")
            self.emit("panel %s: _total suffix flip %r -> %r"
                      % (pid, metric, flipped))
            return
        close = difflib.get_close_matches(metric, inventory, n=3,
                                          cutoff=_CLOSE_CUTOFF)
        if close:
            best = close[0]
            new_expr = _replace_metric(expr, metric, best)
            conf = _confidence(metric, best)
            self.metric_map[metric] = best
            _add_finding(
                self.findings, self.ids,
                "panel-%s-metric-missing" % pid, "warn", "panel",
                "Panel %s: metric %r does not exist in the "
                "datasource - did you mean %r?" % (pid, metric, best),
                "no exact match among %d metrics; closest: %s"
                % (len(inventory), ", ".join(close)),
                "Replace %r with %r in the query." % (metric, best),
                "edit-query",
                {"panel_id": pid, "refId": ref or "A",
                 "new_expr": new_expr},
                pid, conf)
            self.emit("panel %s: did you mean %r for %r"
                      % (pid, best, metric))
            return
        self.absent.setdefault(pid, []).append(metric)
        _add_finding(
            self.findings, self.ids,
            "panel-%s-metric-missing" % pid, "warn", "panel",
            "Panel %s: metric %r does not exist and nothing similar "
            "is being ingested." % (pid, metric),
            "no metric close to %r among %d metrics in the "
            "datasource" % (metric, len(inventory)),
            "This data is not flowing into your metrics backend at "
            "all; see the pipeline findings for what to deploy.",
            "pipeline", None, pid)

    def _eliminate(self, pid: Any, ref: str, expr: str, uid: str,
                   sel: Dict[str, Any]) -> None:
        verdict, matcher = _eliminate_matchers(self.grafana, uid, sel,
                                               self.budget)
        if verdict == "outer":
            _add_finding(
                self.findings, self.ids,
                "panel-%s-selector-ok" % pid, "info", "panel",
                "Panel %s: the series selector matches data, so the "
                "emptiness comes from the surrounding expression."
                % pid,
                "count(%s) returned data; full expr did not: %s"
                % (_selector_text(sel["metric"], sel["matchers"]),
                   expr),
                "Check range windows and functions: a rate()/"
                "increase() window shorter than the scrape interval "
                "yields nothing - use $__rate_interval.",
                "none", None, pid)
            return
        if verdict != "matcher" or matcher is None:
            reason = ("probe budget exhausted"
                      if self.budget["capped"]
                      else "no single matcher restores data - several "
                           "matchers may be wrong at once")
            _add_finding(
                self.findings, self.ids,
                "panel-%s-selector-empty" % pid, "warn", "panel",
                "Panel %s: the series selector matches nothing."
                % pid,
                "%s; matchers: %s"
                % (reason,
                   ", ".join(m["text"] for m in sel["matchers"])),
                "Check each label matcher against the datasource's "
                "actual labels (use the metric explorer).",
                "none", None, pid)
            return
        self._offending_matcher(pid, ref, expr, uid, sel, matcher)

    def _offending_matcher(self, pid: Any, ref: str, expr: str,
                           uid: str, sel: Dict[str, Any],
                           matcher: Dict[str, Any]) -> None:
        label, value = matcher["label"], matcher["value"]
        metric = sel["metric"]
        values = self.label_values(uid, label, metric)
        if not values:
            # The label itself does not exist on these series.
            names = self.series_labels(uid, metric) if metric else []
            close = difflib.get_close_matches(label, names, n=1,
                                              cutoff=_CLOSE_CUTOFF) \
                if names else []
            if close:
                new_text = matcher["text"].replace(label, close[0], 1)
                new_expr = expr.replace(matcher["text"], new_text, 1)
                self.label_map[label] = close[0]
                _add_finding(
                    self.findings, self.ids,
                    "panel-%s-label-missing" % pid, "warn", "panel",
                    "Panel %s: label %r does not exist on %r - did "
                    "you mean %r?" % (pid, label, metric or "series",
                                      close[0]),
                    "labels on those series: %s" % ", ".join(names),
                    "Rename label %r to %r in the query."
                    % (label, close[0]),
                    "edit-query",
                    {"panel_id": pid, "refId": ref or "A",
                     "new_expr": new_expr},
                    pid, _confidence(label, close[0]))
            else:
                _add_finding(
                    self.findings, self.ids,
                    "panel-%s-label-missing" % pid, "warn", "panel",
                    "Panel %s: matcher on label %r empties the "
                    "selector and that label has no values."
                    % (pid, label),
                    "dropping %s restores data; labels present: %s"
                    % (matcher["text"], ", ".join(names) or "unknown"),
                    "Remove the matcher or filter on a label that "
                    "exists on these series.",
                    "none", None, pid)
            return
        close = difflib.get_close_matches(value, values, n=1,
                                          cutoff=_CLOSE_CUTOFF)
        shown = ", ".join(sorted(values)[:10])
        if close and matcher["op"] in ("=", "=~"):
            new_text = "%s%s\"%s\"" % (label, matcher["op"], close[0])
            new_expr = expr.replace(matcher["text"], new_text, 1)
            _add_finding(
                self.findings, self.ids,
                "panel-%s-label-value" % pid, "warn", "panel",
                "Panel %s: no series has %s=%r; the actual value is "
                "%r." % (pid, label, value, close[0]),
                "dropping %s restores data; %s values: %s"
                % (matcher["text"], label, shown),
                "Use %s=\"%s\" in the query." % (label, close[0]),
                "edit-query",
                {"panel_id": pid, "refId": ref or "A",
                 "new_expr": new_expr},
                pid, _confidence(value, close[0]))
            self.emit("panel %s: label %s value %r -> %r"
                      % (pid, label, value, close[0]))
        else:
            _add_finding(
                self.findings, self.ids,
                "panel-%s-label-value" % pid, "warn", "panel",
                "Panel %s: the matcher %s matches no series."
                % (pid, matcher["text"]),
                "dropping it restores data; actual %s values: %s"
                % (label, shown),
                "Pick one of the actual values above for label %r."
                % label,
                "none", None, pid)

    # -- loki --------------------------------------------------------------

    def check_loki_panel(self, row: Dict[str, Any]) -> None:
        pid, ref, expr, uid = (row["panel_id"], row["refId"],
                               row["expr"], row["uid"])
        sels = _scan_selectors(expr)
        if not sels:
            return
        sel = sels[0]
        labels = self.loki_label_names(uid)
        if labels is None:
            _add_finding(
                self.findings, self.ids,
                "loki-labels-unavailable-%s" % uid, "info", "data",
                "Loki stream labels of datasource %r could not be "
                "read; stream-selector checks were skipped." % uid,
                "GET /api/datasources/proxy/uid/%s/loki/api/v1/"
                "labels returned nothing" % uid,
                "Check the datasource proxy and that Loki answers "
                "its labels API.",
                "none")
            return
        found_problem = False
        for matcher in sel["matchers"]:
            label, value = matcher["label"], matcher["value"]
            if label not in labels:
                found_problem = True
                close = difflib.get_close_matches(
                    label, labels, n=1, cutoff=_CLOSE_CUTOFF)
                if close:
                    new_text = matcher["text"].replace(label,
                                                       close[0], 1)
                    new_expr = expr.replace(matcher["text"],
                                            new_text, 1)
                    self.label_map[label] = close[0]
                    _add_finding(
                        self.findings, self.ids,
                        "panel-%s-loki-label-missing" % pid, "warn",
                        "panel",
                        "Panel %s: %r is not a Loki stream label - "
                        "did you mean %r?" % (pid, label, close[0]),
                        "stream labels: %s" % ", ".join(labels),
                        "Rename label %r to %r in the stream "
                        "selector." % (label, close[0]),
                        "edit-query",
                        {"panel_id": pid, "refId": ref or "A",
                         "new_expr": new_expr},
                        pid, _confidence(label, close[0]))
                else:
                    _add_finding(
                        self.findings, self.ids,
                        "panel-%s-loki-label-missing" % pid, "warn",
                        "panel",
                        "Panel %s: %r is not a Loki stream label."
                        % (pid, label),
                        "stream labels: %s" % ", ".join(labels),
                        "Select streams by one of the labels above, "
                        "or add %r as a stream label in your log "
                        "shipper." % label,
                        "none", None, pid)
                continue
            if matcher["op"] != "=":
                continue
            values = self.loki_values(uid, label)
            if values and value not in values:
                found_problem = True
                close = difflib.get_close_matches(
                    value, values, n=1, cutoff=_CLOSE_CUTOFF)
                shown = ", ".join(sorted(values)[:10])
                if close:
                    new_text = "%s=\"%s\"" % (label, close[0])
                    new_expr = expr.replace(matcher["text"],
                                            new_text, 1)
                    _add_finding(
                        self.findings, self.ids,
                        "panel-%s-loki-label-value" % pid, "warn",
                        "panel",
                        "Panel %s: no log stream has %s=%r; the "
                        "actual value is %r."
                        % (pid, label, value, close[0]),
                        "%s values: %s" % (label, shown),
                        "Use %s=\"%s\" in the stream selector."
                        % (label, close[0]),
                        "edit-query",
                        {"panel_id": pid, "refId": ref or "A",
                         "new_expr": new_expr},
                        pid, _confidence(value, close[0]))
                else:
                    _add_finding(
                        self.findings, self.ids,
                        "panel-%s-loki-label-value" % pid, "warn",
                        "panel",
                        "Panel %s: no log stream has %s=%r."
                        % (pid, label, value),
                        "%s values: %s" % (label, shown),
                        "Pick one of the actual values above for "
                        "label %r." % label,
                        "none", None, pid)
        if found_problem:
            return
        self._loki_parser_check(pid, ref, expr, uid, sel)

    def _loki_parser_check(self, pid: Any, ref: str, expr: str,
                           uid: str, sel: Dict[str, Any]) -> None:
        m = _PARSER_STAGE_RE.search(expr)
        if not m:
            return
        parser = m.group(1)
        other = "logfmt" if parser == "json" else "json"
        self.budget["panel"] = MAX_PROBES_PER_PANEL
        probe_expr = "sum(count_over_time(%s[5m]))" % sel["selector"]
        got = _probe(self.grafana, uid, "loki", probe_expr,
                     self.budget)
        if not got:
            return
        new_expr = expr[:m.start()] + expr[m.start():].replace(
            parser, other, 1)
        self.flags["loki_parser"] = other
        _add_finding(
            self.findings, self.ids,
            "panel-%s-loki-parser" % pid, "warn", "panel",
            "Panel %s: the log streams exist, but the '| %s' parser "
            "stage yields nothing - the logs are probably %s-"
            "formatted." % (pid, parser, other),
            "%s returned data while the full query did not"
            % probe_expr,
            "Replace '| %s' with '| %s' (set config loki_parser to "
            "\"%s\" so future conversions match)."
            % (parser, other, other),
            "edit-query",
            {"panel_id": pid, "refId": ref or "A",
             "new_expr": new_expr},
            pid, "medium")
        self.emit("panel %s: loki parser mismatch %s -> %s"
                  % (pid, parser, other))

    # -- other datasource types -------------------------------------------

    def check_other_panel(self, row: Dict[str, Any],
                          ds_type: str) -> None:
        """No-data panel on a datasource we cannot introspect
        (tempo, cloud plugins, ...): still tell the user what to
        check instead of staying silent."""
        pid, ref, expr = row["panel_id"], row["refId"], row["expr"]
        if ds_type == "tempo":
            desc = ("Tempo found no traces matching this TraceQL "
                    "query in the tested time range. Check that "
                    "traces are being sent to Tempo at all, widen "
                    "the time range, and verify the span attribute "
                    "names/values in the selector (attribute names "
                    "are case-sensitive; New Relic attribute names "
                    "often differ from OTel semantic conventions).")
        else:
            desc = ("The %s datasource returned no data for this "
                    "query. Verify the datasource points at the "
                    "right backend, the query matches how that "
                    "backend names things, and the backend has data "
                    "in the tested time range."
                    % (ds_type or "configured"))
        _add_finding(
            self.findings, self.ids,
            "panel-%s-no-data" % pid, "warn", "panel",
            "Panel %s query [%s] returns no data (datasource type "
            "%s)." % (pid, ref or "A", ds_type or "unknown"),
            expr, desc, "none", None, pid)
        self.emit("panel %s: no data on %s datasource"
                  % (pid, ds_type or "unknown"))


def _check_panels(findings: List[Dict[str, Any]], ids: set,
                  grafana: Any, dash: Optional[Dict[str, Any]],
                  requirements: Optional[Dict[str, Any]],
                  test_results: Optional[List[Dict[str, Any]]],
                  parity: Optional[Dict[str, Any]],
                  cfg: Optional[Dict[str, Any]],
                  emit: Callable[[str], None]) -> _PanelChecker:
    checker = _PanelChecker(grafana, findings, ids, cfg, emit)
    rows = _problem_rows(test_results, parity)
    if not rows:
        return checker
    by_target, by_panel = _target_types(dash, requirements)
    for row in rows:
        if row["status"] == "error":
            checker.check_error_row(row)
            continue
        if grafana is None:
            continue
        uid = row["uid"]
        if not uid or uid.startswith("${"):
            continue  # covered by the datasource layer
        ds_type = by_target.get((row["panel_id"], row["refId"])) \
            or by_panel.get(row["panel_id"]) \
            or _guess_type(row["expr"])
        if ds_type == "prometheus":
            checker.check_prom_panel(row)
        elif ds_type == "loki":
            checker.check_loki_panel(row)
        else:
            checker.check_other_panel(row, ds_type)
    return checker


# ---------------------------------------------------------------------------
# Layer 4: data pipeline
# ---------------------------------------------------------------------------

def _nr_data_panels(parity: Optional[Dict[str, Any]]) -> set:
    """Panel ids where New Relic returned data points."""
    out = set()
    for row in (parity or {}).get("panels") or []:
        if ((row.get("nr_summary") or {}).get("points") or 0) > 0:
            out.add(row.get("panel_id"))
    return out


def _check_pipeline(findings: List[Dict[str, Any]], ids: set,
                    requirements: Optional[Dict[str, Any]],
                    parity: Optional[Dict[str, Any]],
                    absent: Dict[Any, List[str]],
                    emit: Callable[[str], None]) -> None:
    if not requirements or not absent:
        return
    nr_has = _nr_data_panels(parity)
    for domain in requirements.get("domains") or []:
        panel_ids = sorted(set(domain.get("panel_ids") or [])
                           & set(absent))
        if not panel_ids:
            continue
        name = domain.get("domain", "unknown")
        metrics = sorted({m for pid in panel_ids
                          for m in absent.get(pid, [])})
        options = domain.get("options") or []
        ds_opt = next((o for o in options
                       if o.get("kind") == "datasource"
                       and o.get("plugin_id") in DS_TEMPLATES), None)
        notes = "; ".join(str(o.get("note", "")) for o in options
                          if o.get("note"))
        with_nr = sorted(set(panel_ids) & nr_has)
        problem = ("Panels %s expect %s data (%s) that is not being "
                   "ingested into Grafana at all."
                   % (", ".join(str(p) for p in panel_ids), name,
                      ", ".join(metrics[:5]) or "see queries"))
        if with_nr:
            problem += (" New Relic still has this data (panel%s %s "
                        "returned points), so only the Grafana-side "
                        "pipeline is missing."
                        % ("" if len(with_nr) == 1 else "s",
                           ", ".join(str(p) for p in with_nr)))
        severity = "blocker" if with_nr else "warn"
        if ds_opt is not None:
            plugin = ds_opt["plugin_id"]
            action = _ds_template_action(plugin)
            inputs = _template_inputs(plugin)
            desc = ("Either add a %s datasource (input needed: %s) "
                    "and repoint these panels at it, or deploy an "
                    "exporter pipeline%s"
                    % (plugin, inputs or "none",
                       ": " + notes if notes else "."))
            _add_finding(findings, ids, "pipeline-%s" % name,
                         severity, "data", problem,
                         "domain evidence: %s"
                         % "; ".join(domain.get("evidence") or []),
                         desc, "add-datasource", action)
        else:
            _add_finding(findings, ids, "pipeline-%s" % name,
                         severity, "data", problem,
                         "domain evidence: %s"
                         % "; ".join(domain.get("evidence") or []),
                         notes or "Deploy an ingestion pipeline for "
                                  "this data domain.",
                         "pipeline")
        emit("pipeline: %s data missing for panels %s"
             % (name, panel_ids))


# ---------------------------------------------------------------------------
# Layer 5: config consolidation
# ---------------------------------------------------------------------------

def _check_config(findings: List[Dict[str, Any]], ids: set,
                  checker: _PanelChecker) -> None:
    overlay: Dict[str, Any] = {}
    if checker.metric_map:
        overlay["metric_map"] = dict(sorted(checker.metric_map.items()))
    if checker.label_map:
        overlay["label_map"] = dict(sorted(checker.label_map.items()))
    overlay.update(checker.flags)
    if not overlay:
        return
    parts = []
    if checker.metric_map:
        parts.append("%d metric rename(s): %s" % (
            len(checker.metric_map),
            ", ".join("%s -> %s" % kv
                      for kv in sorted(checker.metric_map.items()))))
    if checker.label_map:
        parts.append("%d label rename(s): %s" % (
            len(checker.label_map),
            ", ".join("%s -> %s" % kv
                      for kv in sorted(checker.label_map.items()))))
    for key, val in sorted(checker.flags.items()):
        parts.append("%s = %r" % (key, val))
    _add_finding(
        findings, ids, "config-renames", "info", "config",
        "Recurring rename patterns can be codified so the next "
        "conversion produces working queries directly.",
        "; ".join(parts),
        "Merge this overlay into your converter config "
        "(config-overlay.json) and re-run convert with it.",
        "config-overlay", overlay)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def diagnose(grafana: Any, nr: Any = None,
             dash: Optional[Dict[str, Any]] = None,
             requirements: Optional[Dict[str, Any]] = None,
             test_results: Optional[List[Dict[str, Any]]] = None,
             parity: Optional[Dict[str, Any]] = None,
             cfg: Optional[Dict[str, Any]] = None,
             log: Optional[Callable[[str], None]] = None) \
        -> Dict[str, Any]:
    """Run all diagnostic layers and return the diagnosis report.

    Every argument except ``grafana`` is optional and each layer
    degrades gracefully when its inputs are missing; the function never
    raises on network failures. Returns schema
    ``nr2grafana/diagnosis/v1``: ``{"schema", "generated_at",
    "findings": [...], "summary": {...}}`` where each finding carries a
    stable unique id, a severity (blocker|warn|info), an area
    (auth|datasource|panel|data|config), the problem, its evidence and
    a fix with an optional machine ``action`` payload for
    ``remediate.apply_fix``.
    """
    emit = log or (lambda m: None)
    findings: List[Dict[str, Any]] = []
    ids: set = set()

    _check_auth(findings, ids, grafana, nr, emit)
    _check_datasources(findings, ids, grafana, requirements, emit)
    checker = _check_panels(findings, ids, grafana, dash, requirements,
                            test_results, parity, cfg, emit)
    _check_pipeline(findings, ids, requirements, parity,
                    checker.absent, emit)
    _check_config(findings, ids, checker)

    findings.sort(key=lambda f: _SEV_ORDER.get(f.get("severity"), 3))
    summary: Dict[str, Any] = {"findings": len(findings),
                               "blocker": 0, "warn": 0, "info": 0,
                               "by_area": {}, "panels": []}
    panels = set()
    for f in findings:
        sev = f.get("severity", "info")
        if sev in summary:
            summary[sev] += 1
        area = f.get("area", "")
        summary["by_area"][area] = summary["by_area"].get(area, 0) + 1
        if f.get("panel_id") is not None:
            panels.add(f["panel_id"])
    summary["panels"] = sorted(panels, key=lambda p: (str(type(p)),
                                                      str(p)))
    emit("diagnosis: %d finding(s) (%d blocker, %d warn, %d info)"
         % (len(findings), summary["blocker"], summary["warn"],
            summary["info"]))
    return {"schema": SCHEMA,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime()),
            "findings": findings,
            "summary": summary}
