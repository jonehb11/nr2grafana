"""Datasource binding + target environment for exported dashboards.

SEAM-BIND (ARCHITECTURE-1.11): converted dashboards are PORTABLE -- every
target references a datasource template variable (``${datasource}``,
``${loki_datasource}``, ``${tempo_datasource}``,
``${cloudwatch_datasource}``) so one JSON works on any Grafana. Real
migrations showed that an exported dashboard whose refs are left unbound
renders empty (failure class F8), so this module produces the BOUND
flavour on demand:

  bind_datasources(dash, ds_map)  -> every ``${var}`` datasource ref
      rewritten to a concrete ``{"type", "uid"}`` and the datasource
      template variables dropped (``keep_vars=True`` keeps them);
  set_target_env(dash, env)       -> the ``env`` variable's current /
      default value set (``pin=True`` additionally rewrites ``$env`` to
      the concrete value and drops the variable, for a pinned export).

``ds_map`` is ``GrafanaLive.resolve_ds_map`` output (``{name: uid,
"${name}": uid}``) or an explicit map; values may also be
``{"type": .., "uid": ..}`` dicts. Both functions return a NEW dashboard
and never mutate the input. Stdlib only.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

# ``${name}`` (optionally ``${name:format}``) and legacy ``$name`` refs.
_BRACE_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[A-Za-z]+)?\}$")
_DOLLAR_REF = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")

# Built-in Grafana pseudo datasources that are never bound.
_BUILTIN_UIDS = frozenset(["-- Mixed --", "-- Grafana --",
                           "-- Dashboard --", "grafana"])

# Datasource variable name -> family (mirrors builder._DS_VAR_NAMES; a
# ``<family>_datasource`` name is the generic rule).
_DS_FAMILY_TYPES = {"datasource": "prometheus"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _var_name(ref: Any) -> str:
    """The variable name a datasource uid ref points at, '' when the ref
    is concrete (or not a string)."""
    if not isinstance(ref, str):
        return ""
    m = _BRACE_REF.match(ref.strip())
    if m:
        return m.group(1)
    m = _DOLLAR_REF.match(ref.strip())
    if m:
        return m.group(1)
    return ""


def datasource_variables(dash: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The dashboard's ``type: datasource`` template variables."""
    out: List[Dict[str, Any]] = []
    for var in (dash.get("templating") or {}).get("list") or []:
        if isinstance(var, dict) and var.get("type") == "datasource":
            out.append(var)
    return out


def _var_types(dash: Dict[str, Any]) -> Dict[str, str]:
    """Variable name -> datasource plugin type, from the datasource
    template variables (``query`` holds the type) and, as a fallback,
    from the type carried by any ref that uses the variable."""
    types: Dict[str, str] = {}
    for var in datasource_variables(dash):
        name = var.get("name") or ""
        q = var.get("query")
        if name and isinstance(q, str) and q:
            types[name] = q
    for ref in _iter_ds_refs(dash):
        if not isinstance(ref, dict):
            continue
        name = _var_name(ref.get("uid"))
        t = ref.get("type")
        if name and name not in types and isinstance(t, str) and t \
                and t != "datasource":
            types[name] = t
    for name, fam in _DS_FAMILY_TYPES.items():
        types.setdefault(name, fam)
    return types


def _iter_ds_refs(node: Any):
    """Yield every ``datasource`` value (dict or string) anywhere in the
    tree: panel/target/variable/annotation level, rows included."""
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "datasource" and (isinstance(val, (dict, str))):
                yield val
            if isinstance(val, (dict, list)):
                for x in _iter_ds_refs(val):
                    yield x
    elif isinstance(node, list):
        for item in node:
            for x in _iter_ds_refs(item):
                yield x


def normalize_ds_map(ds_map: Optional[Dict[str, Any]],
                     dash: Optional[Dict[str, Any]] = None) \
        -> Dict[str, Dict[str, str]]:
    """``{var_name: {"type", "uid"}}`` from any accepted ds_map shape:
    ``{name: uid}``, ``{"${name}": uid}`` (resolve_ds_map emits both) or
    ``{name: {"type", "uid"}}``. The type comes from the map entry, else
    from the dashboard's datasource variable / refs, else ''."""
    types = _var_types(dash) if dash else dict(_DS_FAMILY_TYPES)
    out: Dict[str, Dict[str, str]] = {}
    for key, val in (ds_map or {}).items():
        if not isinstance(key, str):
            continue
        name = _var_name(key) or key.strip()
        if not name:
            continue
        uid = ""
        ds_type = ""
        if isinstance(val, dict):
            uid = str(val.get("uid") or "")
            ds_type = str(val.get("type") or "")
        elif isinstance(val, str):
            uid = val
        if not uid or _var_name(uid):
            continue  # unresolved / self-referencing entry
        entry = out.setdefault(name, {"type": "", "uid": ""})
        entry["uid"] = uid
        if ds_type:
            entry["type"] = ds_type
    for name, entry in out.items():
        if not entry["type"]:
            entry["type"] = types.get(name, "")
    return out


def unbound_refs(dash: Dict[str, Any]) -> List[str]:
    """Sorted variable names still referenced by a ``${var}``-style
    datasource ref anywhere in the dashboard (empty = fully bound)."""
    names = set()
    for ref in _iter_ds_refs(dash):
        uid = ref.get("uid") if isinstance(ref, dict) else ref
        name = _var_name(uid)
        if name:
            names.add(name)
    return sorted(names)


# ---------------------------------------------------------------------------
# bind_datasources
# ---------------------------------------------------------------------------

def _bind_ref(ref: Any, bound: Dict[str, Dict[str, str]]) -> Any:
    """Rewrite one datasource ref (dict or legacy string) when it points
    at a bound variable; anything else is returned unchanged."""
    if isinstance(ref, str):
        name = _var_name(ref)
        if name and name in bound:
            return {"type": bound[name]["type"] or "",
                    "uid": bound[name]["uid"]}
        return ref
    if not isinstance(ref, dict):
        return ref
    uid = ref.get("uid")
    if isinstance(uid, str) and uid in _BUILTIN_UIDS:
        return ref
    name = _var_name(uid)
    if not name or name not in bound:
        return ref
    new = dict(ref)
    new["uid"] = bound[name]["uid"]
    # Prefer the instance's real plugin type; keep the existing one when
    # the map carries none.
    if bound[name]["type"]:
        new["type"] = bound[name]["type"]
    return new


def _walk_bind(node: Any, bound: Dict[str, Dict[str, str]]) -> None:
    if isinstance(node, dict):
        for key in list(node.keys()):
            val = node[key]
            if key == "datasource":
                node[key] = _bind_ref(val, bound)
                val = node[key]
            if isinstance(val, (dict, list)):
                _walk_bind(val, bound)
    elif isinstance(node, list):
        for item in node:
            _walk_bind(item, bound)


def bind_datasources(dash: Dict[str, Any], ds_map: Dict[str, Any],
                     keep_vars: bool = False) -> Dict[str, Any]:
    """Return a copy of ``dash`` with every ``${var}`` datasource ref
    (targets, panels, rows, query variables, annotations) rewritten to
    the concrete ``{"type", "uid"}`` from ``ds_map``. Datasource template
    variables that were bound are dropped unless ``keep_vars``; variables
    the map cannot resolve are kept (and their refs stay symbolic) so
    ``unbound_refs()`` can report exactly what is missing."""
    if not isinstance(dash, dict):
        raise ValueError("bind_datasources expects a dashboard dict")
    out = copy.deepcopy(dash)
    bound = normalize_ds_map(ds_map, out)
    if not bound:
        return out
    _walk_bind(out.get("panels"), bound)
    _walk_bind(out.get("annotations"), bound)
    tvars = (out.get("templating") or {}).get("list")
    if isinstance(tvars, list):
        kept: List[Dict[str, Any]] = []
        for var in tvars:
            if isinstance(var, dict) and var.get("type") == "datasource" \
                    and var.get("name") in bound:
                if keep_vars:
                    # keep the picker but preselect the bound datasource
                    var = dict(var)
                    var["current"] = {
                        "selected": True,
                        "text": bound[var["name"]]["uid"],
                        "value": bound[var["name"]]["uid"]}
                    kept.append(var)
                continue
            if isinstance(var, dict):
                _walk_bind(var, bound)
            kept.append(var)
        out.setdefault("templating", {})["list"] = kept
    return out


# ---------------------------------------------------------------------------
# set_target_env
# ---------------------------------------------------------------------------

def find_variable(dash: Dict[str, Any], name: str) \
        -> Optional[Dict[str, Any]]:
    for var in (dash.get("templating") or {}).get("list") or []:
        if isinstance(var, dict) and var.get("name") == name:
            return var
    return None


def _env_pattern(var_name: str) -> "re.Pattern[str]":
    n = re.escape(var_name)
    return re.compile(r"\$\{%s(?::[^}]*)?\}|\$%s\b|\[\[%s(?::[^\]]*)?\]\]"
                      % (n, n, n))


def env_refs(dash: Dict[str, Any], var_name: str = "env") -> int:
    """Number of ``$env``-style interpolations across the dashboard
    (panels, annotations, other variables, links)."""
    pat = _env_pattern(var_name)
    count = 0
    for text in _iter_strings(_env_scope(dash, var_name)):
        count += len(pat.findall(text))
    return count


def _env_scope(dash: Dict[str, Any], var_name: str) -> List[Any]:
    """The subtrees where ``$env`` may be interpolated: everything except
    the env variable's own definition."""
    scope: List[Any] = [dash.get("panels"), dash.get("annotations"),
                        dash.get("links"), dash.get("title")]
    for var in (dash.get("templating") or {}).get("list") or []:
        if isinstance(var, dict) and var.get("name") != var_name:
            scope.append(var)
    return scope


def _iter_strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for val in node.values():
            for x in _iter_strings(val):
                yield x
    elif isinstance(node, list):
        for item in node:
            for x in _iter_strings(item):
                yield x


def _rewrite_strings(node: Any, pat: "re.Pattern[str]", value: str) -> Any:
    if isinstance(node, str):
        return pat.sub(lambda _m: value, node)
    if isinstance(node, dict):
        for key in list(node.keys()):
            node[key] = _rewrite_strings(node[key], pat, value)
        return node
    if isinstance(node, list):
        return [_rewrite_strings(item, pat, value) for item in node]
    return node


def resolve_env(env: str, env_map: Optional[Dict[str, Any]] = None) \
        -> Tuple[str, str]:
    """``(value, text)`` for a requested env name: ``env_map`` maps the
    name given on the command line to the concrete label value used by
    the target stack (``{"prod": "production"}``); a dict entry may carry
    ``{"value", "text"}``. Unmapped names are used verbatim."""
    env = (env or "").strip()
    mapped: Any = (env_map or {}).get(env) if isinstance(env_map, dict) \
        else None
    if isinstance(mapped, dict):
        value = str(mapped.get("value") or env)
        text = str(mapped.get("text") or env)
        return value, text
    if isinstance(mapped, str) and mapped:
        return mapped, env
    return env, env


def set_target_env(dash: Dict[str, Any], env: str,
                   env_map: Optional[Dict[str, Any]] = None,
                   var_name: str = "env", pin: bool = False) \
        -> Dict[str, Any]:
    """Return a copy of ``dash`` whose ``env`` variable (``var_name``)
    has ``env`` as its current/default value: a custom variable gains the
    option when missing and selects it, a textbox gets it as its text, a
    query variable gets it preselected. When the dashboard interpolates
    ``$env`` but defines no such variable, a one-option custom variable
    is added so the refs resolve. With ``pin=True`` every ``$env`` /
    ``${env}`` / ``[[env]]`` interpolation is rewritten to the concrete
    value and the variable is dropped (a pinned, single-environment
    export)."""
    if not isinstance(dash, dict):
        raise ValueError("set_target_env expects a dashboard dict")
    env = (env or "").strip()
    if not env:
        raise ValueError("an environment name is required (e.g. prod)")
    var_name = var_name or "env"
    out = copy.deepcopy(dash)
    value, text = resolve_env(env, env_map)
    tvars = out.setdefault("templating", {}).setdefault("list", [])
    if not isinstance(tvars, list):
        tvars = []
        out["templating"]["list"] = tvars
    var = find_variable(out, var_name)
    if var is None:
        if env_refs(out, var_name) or not pin:
            var = {"name": var_name, "label": var_name.capitalize(),
                   "type": "custom", "hide": 0, "skipUrlSync": False,
                   "multi": False, "includeAll": False,
                   "query": "%s : %s" % (text, value),
                   "options": [], "current": {}}
            tvars.append(var)
    if var is not None:
        _select_value(var, value, text)
    if pin:
        pat = _env_pattern(var_name)
        for key in ("panels", "annotations", "links"):
            if key in out:
                out[key] = _rewrite_strings(out[key], pat, value)
        if isinstance(out.get("title"), str):
            out["title"] = pat.sub(lambda _m: value, out["title"])
        kept: List[Any] = []
        for v in tvars:
            if isinstance(v, dict) and v.get("name") == var_name:
                continue
            kept.append(_rewrite_strings(v, pat, value))
        out["templating"]["list"] = kept
    return out


def _select_value(var: Dict[str, Any], value: str, text: str) -> None:
    vtype = var.get("type") or "custom"
    if vtype == "custom":
        options = var.get("options")
        if not isinstance(options, list):
            options = []
        found = False
        for opt in options:
            if not isinstance(opt, dict):
                continue
            hit = str(opt.get("value")) == value
            opt["selected"] = hit
            if hit:
                found = True
                text = str(opt.get("text") or text)
        if not found:
            options.append({"selected": True, "text": text,
                            "value": value})
            var["query"] = ", ".join(
                "%s : %s" % (o.get("text"), o.get("value"))
                for o in options if isinstance(o, dict))
        var["options"] = options
        var["current"] = {"selected": True, "text": text, "value": value}
        return
    if vtype == "textbox":
        var["query"] = value
        var["current"] = {"selected": False, "text": value, "value": value}
        var["options"] = []
        return
    # query / constant / interval / anything else: preselect the value
    if var.get("multi"):
        var["current"] = {"selected": True, "text": [text],
                          "value": [value]}
    else:
        var["current"] = {"selected": True, "text": text, "value": value}
    if vtype == "constant":
        var["query"] = value
