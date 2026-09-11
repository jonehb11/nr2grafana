"""Apply diagnosis fixes and auto-heal converted dashboards.

:func:`apply_fix` executes one fix produced by the root-cause engine
(``nr2grafana.diagnose``): edit a panel query in the packaged dashboard
(and its ``datatest.json``), create a datasource from a ready payload,
or fold a config overlay into ``config-overlay.json`` next to the
package directory. Every applied change is recorded through the
:class:`~nr2grafana.changelog.ChangeLog`.

:func:`auto_heal` runs the closed loop: test the dashboard against a
live Grafana, diagnose the failures, apply only the SAFE fixes
(high-confidence query edits and config overlays -- it never creates
datasources and never pushes to Grafana unless explicitly asked),
re-test, and repeat until nothing new can be fixed.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .grafana.client import GrafanaError

try:  # reuse the config deep-merge when available
    from .config import _merge
except ImportError:  # pragma: no cover - config always ships with us
    def _merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> None:
        for k, v in overlay.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                _merge(base[k], v)
            else:
                base[k] = v

# Fix kinds apply_fix can execute; everything else is advice-only.
ACTIONABLE_KINDS = ("edit-query", "add-datasource", "config-overlay")
ADVICE_KINDS = ("install-plugin", "credentials", "pipeline", "none")

# Query expression keys per datasource family (grafana/builder targets):
# prometheus/loki use "expr", tempo uses "query" (TraceQL), the New
# Relic passthrough plugin uses "queryText".
_EXPR_KEYS = ("expr", "query", "queryText")

_OVERLAY_FILE = "config-overlay.json"

# Keys stripped from an add-datasource action before POSTing it as the
# create_datasource payload (metadata for the UI/user, not for Grafana).
_ACTION_META_KEYS = ("needs_input", "confidence", "note", "notes")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _iter_panels(panels: Optional[List[Dict[str, Any]]]) \
        -> Iterator[Dict[str, Any]]:
    """Yield every panel, recursing into row panels."""
    for p in panels or []:
        yield p
        if p.get("type") == "row":
            for sub in _iter_panels(p.get("panels")):
                yield sub


def _find_panel(dash: Dict[str, Any],
                panel_id: Any) -> Optional[Dict[str, Any]]:
    for p in _iter_panels(dash.get("panels")):
        if p.get("id") == panel_id or str(p.get("id")) == str(panel_id):
            return p
    return None


def _find_target(panel: Dict[str, Any],
                 ref_id: str) -> Optional[Dict[str, Any]]:
    want = ref_id or "A"
    for tgt in panel.get("targets") or []:
        if (tgt.get("refId") or "A") == want:
            return tgt
    return None


def _expr_key(ds_type: str) -> str:
    """Expression key for a target's datasource plugin type."""
    t = (ds_type or "").lower()
    if "tempo" in t:
        return "query"
    if "newrelic" in t:
        return "queryText"
    return "expr"


def _target_expr_key(tgt: Dict[str, Any]) -> str:
    """Key holding this target's query: the family key when present,
    else whichever known key currently carries a string."""
    ds = tgt.get("datasource") or {}
    key = _expr_key(ds.get("type", ""))
    if isinstance(tgt.get(key), str):
        return key
    for k in _EXPR_KEYS:
        if isinstance(tgt.get(k), str) and tgt[k].strip():
            return k
    return key


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)
        f.write("\n")


def _normalize_fix(fix: Dict[str, Any]) -> Dict[str, Any]:
    """Accept either a diagnosis finding (with a nested "fix") or the
    fix dict itself; return a flat fix dict carrying "why"."""
    if "kind" not in fix and isinstance(fix.get("fix"), dict):
        inner = dict(fix["fix"])
        if not inner.get("why"):
            inner["why"] = fix.get("problem", "")
        action = inner.get("action")
        if isinstance(action, dict) and "panel_id" not in action \
                and fix.get("panel_id") is not None:
            action = dict(action)
            action["panel_id"] = fix["panel_id"]
            inner["action"] = action
        if "confidence" not in inner and fix.get("confidence"):
            inner["confidence"] = fix["confidence"]
        return inner
    return fix


def _result(kind: str, applied: bool, detail: str,
            verify: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = {"applied": applied, "kind": kind, "detail": detail}
    if verify is not None:
        out["verify"] = verify
    return out


def _record(changelog: Any, slug: str, action: str, target: str,
            before: Any, after: Any, why: str, source: str) -> None:
    if changelog is None:
        return
    changelog.record(slug, action, target, before, after,
                     why=why, source=source)


# ---------------------------------------------------------------------------
# apply_fix
# ---------------------------------------------------------------------------

def apply_fix(fix: Dict[str, Any], grafana: Any = None,
              dash: Optional[Dict[str, Any]] = None,
              package_dir: str = "", changelog: Any = None,
              slug: str = "", push: bool = False,
              source: str = "user") -> Dict[str, Any]:
    """Apply one diagnosis fix; returns
    ``{"applied": bool, "kind", "detail", "verify": optional}``.

    ``fix`` may be a whole diagnosis finding or its inner ``fix`` dict.
    Dispatches on ``fix["kind"]``:

    - ``edit-query``: patch the panel query (located by ``panel_id`` +
      ``refId`` in the action payload) in ``dash`` and rewrite the
      package's ``dashboard.json`` and ``datatest.json``; push live via
      ``update_dashboard`` when ``push`` is true.
    - ``add-datasource``: ``create_datasource`` with the action payload
      (refused with instructions while it still has unfilled
      ``needs_input``), then health-check the new datasource.
    - ``config-overlay``: deep-merge the action payload into
      ``config-overlay.json`` next to the package directory.
    - advice-only kinds (install-plugin/credentials/pipeline/none):
      no-op, instructions returned in ``detail``.

    Applied changes are recorded via ``changelog`` (``source`` is
    "auto" when called from :func:`auto_heal`).
    """
    fix = _normalize_fix(fix or {})
    kind = fix.get("kind") or "none"
    why = fix.get("why") or fix.get("description") or ""

    if kind == "edit-query":
        return _apply_edit_query(fix, grafana, dash, package_dir,
                                 changelog, slug, push, source, why)
    if kind == "add-datasource":
        return _apply_add_datasource(fix, grafana, changelog, slug,
                                     source, why)
    if kind == "config-overlay":
        return _apply_config_overlay(fix, package_dir, changelog, slug,
                                     source, why)
    if kind in ADVICE_KINDS:
        detail = fix.get("description") or (
            "manual step required (%s)" % kind)
        action = fix.get("action")
        if action:
            detail += " -- details: %s" % json.dumps(
                action, sort_keys=True)
        return _result(kind, False, detail)
    return _result(kind, False,
                   "unknown fix kind %r; nothing applied" % kind)


# -- edit-query -------------------------------------------------------------

def _apply_edit_query(fix: Dict[str, Any], grafana: Any,
                      dash: Optional[Dict[str, Any]], package_dir: str,
                      changelog: Any, slug: str, push: bool,
                      source: str, why: str) -> Dict[str, Any]:
    action = fix.get("action") or {}
    panel_id = action.get("panel_id")
    ref_id = action.get("refId") or "A"
    new_expr = action.get("new_expr") or action.get("expr") or ""
    if panel_id is None or not new_expr:
        return _result("edit-query", False,
                       "fix action needs panel_id and new_expr; got %s"
                       % json.dumps(action, sort_keys=True))

    dash_path = os.path.join(package_dir, "dashboard.json") \
        if package_dir else ""
    if dash is None and dash_path and os.path.exists(dash_path):
        try:
            dash = _read_json(dash_path)
        except (OSError, ValueError) as e:
            return _result("edit-query", False,
                           "cannot read %s: %s" % (dash_path, e))
    if dash is None:
        return _result("edit-query", False,
                       "no dashboard to edit (pass dash= or a "
                       "package_dir containing dashboard.json)")

    panel = _find_panel(dash, panel_id)
    if panel is None:
        return _result("edit-query", False,
                       "panel id %s not found in dashboard %r"
                       % (panel_id, dash.get("title", "")))
    tgt = _find_target(panel, ref_id)
    if tgt is None:
        return _result("edit-query", False,
                       "panel %s has no target with refId %r"
                       % (panel_id, ref_id))

    key = _target_expr_key(tgt)
    before = tgt.get(key) if isinstance(tgt.get(key), str) else ""
    if before == new_expr:
        return _result("edit-query", False,
                       "panel %s [%s] already has this query"
                       % (panel_id, ref_id))
    tgt[key] = new_expr

    if package_dir:
        if dash_path:
            _write_json(dash_path, dash)
        _patch_datatest(package_dir, panel_id, ref_id, new_expr)

    _record(changelog, slug, "query-edit",
            "panel %s [%s]" % (panel_id, ref_id), before, new_expr,
            why, source)

    detail = "panel %s [%s]: %s updated" % (panel_id, ref_id, key)
    verify = None
    if push:
        if grafana is None:
            detail += "; not pushed (no Grafana connection)"
        else:
            try:
                grafana.update_dashboard(
                    dash, message="nr2grafana fix: %s" % (why or "query"))
                verify = {"pushed": True}
                detail += "; pushed to Grafana"
                _record(changelog, slug, "dashboard-updated",
                        dash.get("uid", "") or dash.get("title", ""),
                        None, {"panel_id": panel_id, "refId": ref_id},
                        why, source)
            except GrafanaError as e:
                verify = {"pushed": False, "error": str(e)}
                detail += "; local edit saved but push failed: %s" % e
    return _result("edit-query", True, detail, verify)


def _patch_datatest(package_dir: str, panel_id: Any, ref_id: str,
                    new_expr: str) -> None:
    """Keep datatest.json consistent with the edited dashboard."""
    path = os.path.join(package_dir, "datatest.json")
    if not os.path.exists(path):
        return
    try:
        manifest = _read_json(path)
    except (OSError, ValueError):
        return
    changed = False
    for tgt in manifest.get("targets") or []:
        same_panel = (tgt.get("panel_id") == panel_id
                      or str(tgt.get("panel_id")) == str(panel_id))
        if same_panel and (tgt.get("refId") or "A") == (ref_id or "A"):
            if tgt.get("expr") != new_expr:
                tgt["expr"] = new_expr
                changed = True
    if changed:
        _write_json(path, manifest)


# -- add-datasource ---------------------------------------------------------

def _apply_add_datasource(fix: Dict[str, Any], grafana: Any,
                          changelog: Any, slug: str, source: str,
                          why: str) -> Dict[str, Any]:
    action = fix.get("action")
    if not isinstance(action, dict) or not action:
        return _result("add-datasource", False,
                       "fix has no datasource payload; create the "
                       "datasource by hand: %s"
                       % (fix.get("description") or "see diagnosis"))

    needs = action.get("needs_input")
    if needs:
        fields = ", ".join(str(n) for n in needs)
        return _result(
            "add-datasource", False,
            "cannot create datasource automatically: fill in %s first "
            "(e.g. via the web UI datasource form), then retry" % fields)

    if grafana is None:
        return _result("add-datasource", False,
                       "no Grafana connection; connect with a service "
                       "account token (Admin role) to create datasources")

    payload = {k: v for k, v in action.items()
               if k not in _ACTION_META_KEYS}
    name = payload.get("name") or payload.get("type") or "datasource"
    try:
        resp = grafana.create_datasource(payload)
    except GrafanaError as e:
        return _result("add-datasource", False,
                       "creating datasource %r failed: %s (an Admin "
                       "service-account token is required)" % (name, e))

    created = resp.get("datasource") if isinstance(resp, dict) else None
    if not isinstance(created, dict):
        created = resp if isinstance(resp, dict) else {}
    uid = created.get("uid") or ""

    verify = {"status": "unknown",
              "message": "health check unavailable"}
    health_fn = getattr(grafana, "datasource_health", None)
    if uid and callable(health_fn):
        try:
            verify = health_fn(uid)
        except Exception as e:  # contract says it never raises; be safe
            verify = {"status": "error", "message": str(e)}

    _record(changelog, slug, "datasource-created", name, None,
            {"uid": uid, "type": payload.get("type", ""), "name": name},
            why, source)

    detail = "created datasource %r (uid %s); health: %s" % (
        name, uid or "?", verify.get("status", "unknown"))
    if verify.get("status") not in ("ok", "OK"):
        msg = verify.get("message", "")
        if msg:
            detail += " -- %s" % msg
    return _result("add-datasource", True, detail, verify)


# -- config-overlay ---------------------------------------------------------

def overlay_path(package_dir: str) -> str:
    """Path of config-overlay.json next to (i.e. beside) a package dir."""
    parent = os.path.dirname(os.path.abspath(package_dir.rstrip("/")))
    return os.path.join(parent, _OVERLAY_FILE)


def _apply_config_overlay(fix: Dict[str, Any], package_dir: str,
                          changelog: Any, slug: str, source: str,
                          why: str) -> Dict[str, Any]:
    action = fix.get("action") or {}
    overlay = action.get("overlay") if isinstance(
        action.get("overlay"), dict) else action
    if not isinstance(overlay, dict) or not overlay:
        return _result("config-overlay", False,
                       "fix has no overlay payload to merge")
    if not package_dir:
        return _result(
            "config-overlay", False,
            "no package directory; merge this into your converter "
            "config yourself: %s" % json.dumps(overlay, sort_keys=True))

    path = overlay_path(package_dir)
    current: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            data = _read_json(path)
            if isinstance(data, dict):
                current = data
        except (OSError, ValueError) as e:
            return _result("config-overlay", False,
                           "cannot read existing %s: %s" % (path, e))
    before = copy.deepcopy(current)
    _merge(current, overlay)
    if current == before:
        return _result("config-overlay", False,
                       "overlay already present in %s" % path)
    try:
        _write_json(path, current)
    except OSError as e:
        return _result("config-overlay", False,
                       "cannot write %s: %s" % (path, e))

    _record(changelog, slug, "config-overlay", _OVERLAY_FILE,
            before, current, why, source)
    return _result("config-overlay", True,
                   "merged %s into %s (use it with `nr2grafana convert "
                   "-c %s` on the next conversion)"
                   % (json.dumps(overlay, sort_keys=True), path, path))


# ---------------------------------------------------------------------------
# auto_heal
# ---------------------------------------------------------------------------

def _load_diagnose() -> Callable[..., Dict[str, Any]]:
    """Late import so tests can stub the root-cause engine."""
    from nr2grafana.diagnose import diagnose
    return diagnose


def _is_safe(finding: Dict[str, Any]) -> bool:
    """True for fixes auto_heal may apply on its own.

    Safe: config overlays, and query edits explicitly marked
    ``"confidence": "high"`` on the finding (or its fix). Creating
    datasources, credentials, plugin installs etc. are never safe.
    """
    fx = finding.get("fix") or {}
    kind = fx.get("kind", "")
    action = fx.get("action")
    if not isinstance(action, dict) or not action:
        return False
    if kind == "config-overlay":
        return True
    if kind == "edit-query":
        conf = (finding.get("confidence") or fx.get("confidence")
                or action.get("confidence"))
        has_expr = bool(action.get("new_expr") or action.get("expr"))
        return conf == "high" and has_expr
    return False


def _fix_signature(finding: Dict[str, Any]) -> str:
    fx = finding.get("fix") or {}
    return json.dumps({"kind": fx.get("kind"),
                       "action": fx.get("action")}, sort_keys=True)


def _status_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        status = row.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
    return counts


def auto_heal(grafana: Any, nr: Any, dash: Dict[str, Any],
              widget_report: List[Dict[str, Any]],
              requirements: Dict[str, Any], slug: str,
              package_dir: str, changelog: Any = None,
              max_rounds: int = 3,
              log: Optional[Callable[[str], None]] = None,
              push: bool = False) -> Dict[str, Any]:
    """Test -> diagnose -> fix loop applying only SAFE fixes.

    Each round runs ``grafana.test_dashboard``, feeds the results to
    the root-cause engine, and applies the safe subset of its fixes
    (see :func:`_is_safe`) through :func:`apply_fix` with source
    "auto". It NEVER creates datasources, and never pushes edited
    dashboards to Grafana unless ``push`` is true. Stops when a round
    yields no new safe fixes or after ``max_rounds`` rounds.

    Returns ``{"rounds": [...], "fixed": n,
    "remaining_findings": [...]}`` (plus "converged" and, on a hard
    connection failure, an "error" message).
    """
    emit = log or (lambda m: None)
    diagnose = _load_diagnose()

    rounds: List[Dict[str, Any]] = []
    remaining: List[Dict[str, Any]] = []
    applied_sigs = set()  # type: set
    fixed_total = 0
    converged = False

    for rnd in range(1, max(1, max_rounds) + 1):
        emit("auto-heal round %d: testing dashboard ..." % rnd)
        try:
            tests = grafana.test_dashboard(dash, log=log)
        except GrafanaError as e:
            msg = ("cannot test dashboard against Grafana: %s -- check "
                   "the URL and service-account token" % e)
            emit(msg)
            return {"rounds": rounds, "fixed": fixed_total,
                    "remaining_findings": remaining,
                    "converged": False, "error": msg}

        diagnosis = diagnose(grafana, nr=nr, dash=dash,
                             requirements=requirements,
                             test_results=tests, log=log)
        findings = (diagnosis or {}).get("findings") or []

        entry: Dict[str, Any] = {
            "round": rnd,
            "tests": _status_counts(tests),
            "findings": len(findings),
            "applied": [],
            "fixed": 0,
        }
        rounds.append(entry)

        safe = [f for f in findings
                if _is_safe(f) and _fix_signature(f) not in applied_sigs]
        emit("auto-heal round %d: %d finding(s), %d safe fix(es)"
             % (rnd, len(findings), len(safe)))
        if not safe:
            remaining = findings
            converged = True
            emit("auto-heal: converged (no new safe fixes)")
            break

        fixed_sigs = set()  # type: set
        for finding in safe:
            sig = _fix_signature(finding)
            applied_sigs.add(sig)  # never retry, even on failure
            res = apply_fix(finding, grafana=grafana, dash=dash,
                            package_dir=package_dir,
                            changelog=changelog, slug=slug,
                            push=push, source="auto")
            res["finding_id"] = finding.get("id")
            entry["applied"].append(res)
            emit("auto-heal:   %s %s" % (
                "FIXED " if res.get("applied") else "SKIP  ",
                res.get("detail", "")))
            if res.get("applied"):
                entry["fixed"] += 1
                fixed_total += 1
                fixed_sigs.add(sig)

        remaining = [f for f in findings
                     if _fix_signature(f) not in fixed_sigs]
        if entry["fixed"] == 0:
            converged = True
            emit("auto-heal: converged (safe fixes did not apply)")
            break

    if not converged:
        emit("auto-heal: stopped after %d round(s) (max reached)"
             % max_rounds)
    emit("auto-heal: %d fix(es) applied, %d finding(s) remaining"
         % (fixed_total, len(remaining)))
    return {"rounds": rounds, "fixed": fixed_total,
            "remaining_findings": remaining, "converged": converged}
