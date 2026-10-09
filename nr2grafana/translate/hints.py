"""Live translation hints (SEAM-HINTS).

``collect_hints`` looks at the live Grafana/Mimir instance and at New
Relic (read-only) so the translators can make data-driven decisions
instead of guessing:

* ``metric_types``  {prom_name: counter|gauge|histogram|summary} from
  Mimir ``/api/v1/metadata`` (through the Grafana datasource proxy),
  backfilled from ``__name__`` existence (``x_total`` exists -> counter,
  ``x_bucket`` -> histogram, ``x_sum`` + ``x_count`` -> summary) when
  the remote-write pipeline shipped no metadata.
* ``metric_exists`` {prom_name: bool} for every candidate name the
  dashboard's ``FROM Metric`` queries could produce, so the
  ``_total``-vs-bare decision is driven by what the instance holds.
* ``entities``      {guid: {name, type, service_label[, env]}} resolved
  through NerdGraph ``actor { entity(guid) }`` for every
  ``entity.guid = '<GUID>'`` predicate (READ-ONLY).
* ``attr_values``   {attr: [values]} via read-only NRQL
  ``SELECT uniques(attr, N)`` for env/cluster-like attributes and any
  attribute compared to a ``{{var}}`` placeholder.
* ``label_values``  {label: [values]} from Mimir for the mapped labels
  of those attributes.
* ``notes``         every degradation, in plain words.

Nothing here raises: a missing client, a 404ing proxy or an NRQL error
degrades to an empty section plus a note. Nothing here mutates New
Relic; every NerdGraph request is a query (the client refuses
mutations outright).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from ..nrql.parser import (
    Attr, BoolOp, Cmp, FacetItem, Func, InList, Lit, NotOp, NrqlQuery,
    SelectItem, parse_nrql)
from .common import is_nr_variable, map_attr

_LOG = Callable[[str], None]

# Attributes whose values are worth knowing up front (environment and
# cluster scoping -- F1's `concat('p-', {{env}})` and F10's
# "svc (prod)" both hinge on them).
_ENV_ATTR_RE = re.compile(
    r"(?i)^(?:tags\.|attributes\.|resource\.)?"
    r"(?:[a-z0-9_]+\.)*"
    r"(?:env|environment|envname|cluster|clustername|cluster_name|"
    r"deployment\.environment|k8s\.clustername|k8s\.cluster\.name|"
    r"region|stage|tier)$")

# `entity.guid = '...'` spellings seen in real dashboards.
_GUID_ATTRS = ("entity.guid", "entityguid", "entity_guid", "guid",
               "nr.entityguid", "nr.entity.guid")
_GUID_VALUE_RE = re.compile(r"^[A-Za-z0-9+/=_-]{8,}$")

# "svc (prod)" -> ("svc", "prod"); F10.
_NAME_ENV_SUFFIX_RE = re.compile(r"^(.*\S)\s+\(([^()]+)\)\s*$")

_DEFAULT_LIMITS = {
    "hints_max_metrics": 400,
    "hints_max_entities": 50,
    "hints_max_attrs": 12,
    "hints_max_values": 100,
    "hints_max_labels": 12,
    "hints_since": "1 day ago",
}

_META_KIND = {
    "counter": "counter", "gauge": "gauge", "histogram": "histogram",
    "gaugehistogram": "histogram", "summary": "summary",
    "stateset": "gauge", "info": "gauge",
}


def empty_hints() -> Dict[str, Any]:
    return {"metric_types": {}, "metric_exists": {}, "entities": {},
            "attr_values": {}, "label_values": {}, "notes": []}


# ---------------------------------------------------------------------------
# Dashboard scan (pure; no I/O)
# ---------------------------------------------------------------------------

def _iter_nrql(dash: Any) -> Iterable[Tuple[str, Optional[int]]]:
    """Yield ``(nrql, account_id)`` for every query in an NRDashboard or
    a raw NR dashboard dict (NerdGraph read / UI export)."""
    if dash is None:
        return
    pages = getattr(dash, "pages", None)
    if pages is None and isinstance(dash, dict):
        if isinstance(dash.get("dashboard"), dict):
            dash = dash["dashboard"]
        pages = dash.get("pages") or []
    for page in pages or []:
        widgets = getattr(page, "widgets", None)
        if widgets is None and isinstance(page, dict):
            widgets = page.get("widgets") or []
        for w in widgets or []:
            raw = getattr(w, "raw_configuration", None)
            if raw is None and isinstance(w, dict):
                raw = w.get("rawConfiguration") or {}
            for nq in (raw or {}).get("nrqlQueries") or []:
                if not isinstance(nq, dict):
                    continue
                text = nq.get("query")
                if not isinstance(text, str) or not text.strip():
                    continue
                acct = nq.get("accountId")
                if acct is None:
                    ids = nq.get("accountIds") or []
                    acct = ids[0] if ids else None
                yield text, _int_or_none(acct)
    variables = getattr(dash, "variables", None)
    if variables is None and isinstance(dash, dict):
        variables = dash.get("variables") or []
    for v in variables or []:
        nrq = getattr(v, "nrql_query", None)
        if nrq is None and isinstance(v, dict):
            nrq = v.get("nrqlQuery")
        if isinstance(nrq, dict) and isinstance(nrq.get("query"), str):
            ids = nrq.get("accountIds") or []
            yield nrq["query"], _int_or_none(ids[0] if ids else None)


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _walk_cond(cond: Any) -> Iterable[Any]:
    if cond is None:
        return
    if isinstance(cond, BoolOp):
        for item in cond.items:
            for sub in _walk_cond(item):
                yield sub
    elif isinstance(cond, NotOp):
        for sub in _walk_cond(cond.item):
            yield sub
    else:
        yield cond


def _attr_name(node: Any) -> Optional[str]:
    if isinstance(node, Attr) and is_nr_variable(node) is None:
        return node.name
    return None


def _metric_names_in(q: NrqlQuery) -> List[str]:
    """NR metric names referenced by ``FROM Metric SELECT agg(name)``."""
    if not q.from_ or q.from_[0].lower() != "metric":
        return []
    out: List[str] = []

    def descend(node: Any) -> None:
        if isinstance(node, Func):
            for a in node.args:
                descend(a)
        elif isinstance(node, Attr) and is_nr_variable(node) is None:
            out.append(node.name)
        elif isinstance(node, Lit) and isinstance(node.value, str) \
                and "." in node.value and " " not in node.value:
            out.append(node.value)

    for item in q.select:
        if isinstance(item, SelectItem):
            descend(item.expr)
    # `WHERE metricName = 'x'` / `IN (...)` is the other way NR names a
    # metric family.
    for leaf in _walk_cond(q.where):
        left = getattr(leaf, "left", None)
        if _attr_name(left) not in ("metricName", "metric.name"):
            continue
        if isinstance(leaf, Cmp) and isinstance(leaf.right, Lit) \
                and isinstance(leaf.right.value, str):
            out.append(leaf.right.value)
        elif isinstance(leaf, InList):
            out.extend(v.value for v in leaf.values
                       if isinstance(v, Lit) and isinstance(v.value, str))
    return out


def prom_candidates(nr_name: str, cfg: Dict[str, Any]) -> List[str]:
    """Every Prometheus name the translator might emit for an NR metric:
    the explicit metric_map name, the normalized base, and its
    ``_total``/``_sum``/``_count``/``_bucket`` variants."""
    return _candidates(nr_name, cfg)[1]


def _candidates(nr_name: str, cfg: Dict[str, Any]) \
        -> Tuple[str, List[str]]:
    """``(normalized base, candidate names)``; see prom_candidates."""
    from .metrics import normalize_metric_name
    names: List[str] = []
    entry = (cfg.get("metric_map") or {}).get(nr_name)
    if isinstance(entry, str):
        names.append(entry)
    elif isinstance(entry, dict) and entry.get("name"):
        names.append(str(entry["name"]))
    base = normalize_metric_name(nr_name)
    names.append(base)
    for suffix in ("_total", "_sum", "_count", "_bucket"):
        if not base.endswith(suffix):
            names.append(base + suffix)
    seen: Set[str] = set()
    return base, [n for n in names if n and not (n in seen or seen.add(n))]


def scan_dashboard(dash: Any, cfg: Dict[str, Any]) \
        -> Dict[str, Any]:
    """Pure scan of the dashboard's NRQL for everything the live lookups
    need. Returns ``{metrics, guids, attrs, account_ids, parse_errors}``
    where ``attrs`` is an ordered list of ``(event_type, attr)``."""
    metrics: List[str] = []
    guids: List[str] = []
    attrs: List[Tuple[str, str]] = []
    account_ids: List[int] = []
    errors = 0
    seen_m: Set[str] = set()
    seen_g: Set[str] = set()
    seen_a: Set[Tuple[str, str]] = set()

    def add_attr(event: str, name: str) -> None:
        key = (event, name)
        if key not in seen_a:
            seen_a.add(key)
            attrs.append(key)

    for text, acct in _iter_nrql(dash):
        if acct is not None and acct not in account_ids:
            account_ids.append(acct)
        try:
            q = parse_nrql(text)
        except Exception:  # noqa: BLE001 - parser owns its errors
            errors += 1
            continue
        event = q.from_[0] if q.from_ else ""
        for m in _metric_names_in(q):
            if m not in seen_m:
                seen_m.add(m)
                metrics.append(m)
        for leaf in _walk_cond(q.where):
            left = getattr(leaf, "left", None)
            name = _attr_name(left)
            if not name:
                continue
            low = name.lower()
            if low in _GUID_ATTRS:
                vals: List[Any] = []
                if isinstance(leaf, Cmp) and leaf.op == "=":
                    vals = [leaf.right]
                elif isinstance(leaf, InList) and not leaf.negated:
                    vals = list(leaf.values)
                for v in vals:
                    if isinstance(v, Lit) and isinstance(v.value, str) \
                            and _GUID_VALUE_RE.match(v.value) \
                            and v.value not in seen_g:
                        seen_g.add(v.value)
                        guids.append(v.value)
                continue
            if event and (_ENV_ATTR_RE.match(name)
                          or _compares_to_var(leaf)):
                add_attr(event, name)
        if event:
            for f in q.facet:
                fname = _attr_name(f.expr) if isinstance(f, FacetItem) \
                    else None
                if fname and _ENV_ATTR_RE.match(fname):
                    add_attr(event, fname)
    return {"metrics": metrics, "guids": guids, "attrs": attrs,
            "account_ids": account_ids, "parse_errors": errors}


def _compares_to_var(leaf: Any) -> bool:
    """True when a predicate's value side is (or contains) a ``{{var}}``
    placeholder -- e.g. ``env = {{env}}`` or ``cluster =
    concat('p-', {{env}})``."""
    if isinstance(leaf, Cmp):
        return _has_var(leaf.right)
    if isinstance(leaf, InList):
        return any(_has_var(v) for v in leaf.values)
    return False


def _has_var(node: Any) -> bool:
    if isinstance(node, Func):
        return any(_has_var(a) for a in node.args)
    return is_nr_variable(node) is not None


def split_env_suffix(name: str) -> Tuple[str, Optional[str]]:
    """``"svc (prod)"`` -> ``("svc", "prod")``; otherwise ``(name,
    None)``."""
    m = _NAME_ENV_SUFFIX_RE.match(name or "")
    if m:
        return m.group(1), m.group(2).strip()
    return (name or "").strip(), None


# ---------------------------------------------------------------------------
# Live lookups
# ---------------------------------------------------------------------------

def _limit(cfg: Dict[str, Any], key: str) -> int:
    try:
        return max(0, int(cfg.get(key, _DEFAULT_LIMITS[key])))
    except (TypeError, ValueError):
        return int(_DEFAULT_LIMITS[key])


def _pick_prom_uid(grafana: Any, cfg: Dict[str, Any],
                   notes: List[str]) -> str:
    """Prometheus-type datasource uid to introspect: config pin, else
    the instance default, else the first one."""
    pinned = cfg.get("hints_prometheus_uid") or ""
    if not pinned:
        ds = (cfg.get("datasources") or {}).get("prometheus") or {}
        uid = ds.get("uid") or ""
        if uid and not uid.startswith("${"):
            pinned = uid
    if pinned:
        return str(pinned)
    try:
        dss = grafana.datasources() or []
    except Exception as e:  # noqa: BLE001 - degrade
        notes.append("grafana: cannot list datasources (%s); Mimir "
                     "metadata skipped" % e)
        return ""
    proms = [d for d in dss if isinstance(d, dict)
             and d.get("type") == "prometheus"]
    for d in proms:
        if d.get("isDefault") and d.get("uid"):
            return str(d["uid"])
    if proms and proms[0].get("uid"):
        return str(proms[0]["uid"])
    notes.append("grafana: no prometheus-type datasource on the "
                 "instance; add one (Mimir) or set hints_prometheus_uid")
    return ""


def _proxy_get(grafana: Any, uid: str, path: str,
               errors: List[str]) -> Any:
    """Proxied GET through the datasource proxy, preferring GrafanaLive's
    helper and falling back to the client's request method."""
    helper = getattr(grafana, "_proxy_get", None)
    if callable(helper):
        return helper(uid, path, errors)
    try:
        return grafana._req(
            "GET", "/api/datasources/proxy/uid/%s%s" % (uid, path))
    except Exception as e:  # noqa: BLE001 - degrade
        errors.append(str(e))
        return None


def _mimir_hints(grafana: Any, scan: Dict[str, Any], cfg: Dict[str, Any],
                 out: Dict[str, Any], log: _LOG) -> None:
    notes = out["notes"]
    uid = _pick_prom_uid(grafana, cfg, notes)
    if not uid:
        return
    errors: List[str] = []
    if scan["metrics"]:
        _mimir_metric_hints(grafana, uid, scan, cfg, out, log)

    # label values for the mapped labels of env/cluster attrs
    labels: List[str] = []
    for _event, attr in scan["attrs"]:
        label, _mapped = map_attr(attr, cfg)
        if label and label not in labels:
            labels.append(label)
    max_l = _limit(cfg, "hints_max_labels")
    max_v = _limit(cfg, "hints_max_values")
    fetch_lv = getattr(grafana, "prom_label_values", None)
    for label in labels[:max_l]:
        lerr: List[str] = []
        if callable(fetch_lv):
            vals = fetch_lv(uid, label, "", lerr) or []
        else:
            resp = _proxy_get(grafana, uid,
                              "/api/v1/label/%s/values" % label, lerr)
            data = resp.get("data") if isinstance(resp, dict) else None
            vals = list(data) if isinstance(data, list) else []
        if lerr:
            notes.append("mimir: label %s values unavailable (%s)"
                         % (label, "; ".join(lerr)))
            continue
        out["label_values"][label] = [str(v) for v in vals][:max_v]
    if len(labels) > max_l:
        notes.append("mimir: label values fetched for the first %d of %d "
                     "labels (hints_max_labels)" % (max_l, len(labels)))


def _mimir_metric_hints(grafana: Any, uid: str, scan: Dict[str, Any],
                        cfg: Dict[str, Any], out: Dict[str, Any],
                        log: _LOG) -> None:
    notes = out["notes"]
    errors: List[str] = []

    # 1. metadata -> metric_types
    resp = _proxy_get(grafana, uid, "/api/v1/metadata", errors)
    meta = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(meta, dict):
        for name, entries in meta.items():
            if not isinstance(entries, list):
                continue
            kinds = {_META_KIND.get(str((e or {}).get("type", "")).lower())
                     for e in entries if isinstance(e, dict)}
            kinds.discard(None)
            if len(kinds) == 1:
                out["metric_types"][str(name)] = kinds.pop()
        log("hints: Mimir metadata for %d metric families"
            % len(out["metric_types"]))
    else:
        notes.append("mimir: /api/v1/metadata unavailable via datasource "
                     "%s (%s); metric kinds inferred from names only"
                     % (uid, "; ".join(errors) or "unexpected response"))
        errors = []

    # 2. __name__ existence -> metric_exists (+ suffix-derived kinds)
    names: Set[str] = set()
    fetch = getattr(grafana, "prom_metric_names", None)
    if callable(fetch):
        names = set(fetch(uid, errors) or [])
    else:
        resp = _proxy_get(grafana, uid, "/api/v1/label/__name__/values",
                          errors)
        data = resp.get("data") if isinstance(resp, dict) else None
        names = set(str(v) for v in data) if isinstance(data, list) \
            else set()
    if not names:
        notes.append("mimir: __name__ values unavailable via datasource "
                     "%s (%s); metric existence unknown"
                     % (uid, "; ".join(errors) or "empty list"))
    else:
        max_m = _limit(cfg, "hints_max_metrics")
        for nr_name in scan["metrics"][:max_m]:
            base, cands = _candidates(nr_name, cfg)
            for cand in cands:
                out["metric_exists"][cand] = cand in names
            _derive_kind(base, names, out["metric_types"])
        if len(scan["metrics"]) > max_m:
            notes.append("mimir: existence checked for the first %d of %d "
                         "metrics (hints_max_metrics)"
                         % (max_m, len(scan["metrics"])))
        log("hints: %d candidate names checked against %d live metrics"
            % (len(out["metric_exists"]), len(names)))


def _derive_kind(base: str, names: Set[str],
                 types: Dict[str, str]) -> None:
    """Backfill a kind from which family members exist when Mimir has no
    metadata for the family (typical for OTel remote-write)."""
    if base + "_bucket" in names:
        for n in (base, base + "_bucket"):
            types.setdefault(n, "histogram")
    elif base + "_sum" in names and base + "_count" in names:
        for n in (base, base + "_sum", base + "_count"):
            types.setdefault(n, "summary")
    elif base.endswith("_total") and base in names:
        types.setdefault(base, "counter")
    elif base + "_total" in names:
        types.setdefault(base + "_total", "counter")
        types.setdefault(base, "counter")
    elif base in names:
        types.setdefault(base, "gauge")


def _entity_hints(nr: Any, scan: Dict[str, Any], cfg: Dict[str, Any],
                  out: Dict[str, Any], log: _LOG) -> None:
    notes = out["notes"]
    max_e = _limit(cfg, "hints_max_entities")
    guids = scan["guids"]
    lookup = getattr(nr, "get_entity", None)
    if guids and not callable(lookup):
        notes.append("newrelic: client has no get_entity(); %d entity "
                     "guid(s) left unresolved" % len(guids))
        return
    for guid in guids[:max_e]:
        try:
            ent = lookup(guid) or {}
        except Exception as e:  # noqa: BLE001 - degrade
            notes.append("newrelic: entity %s unresolved: %s"
                         % (_short(guid), e))
            continue
        name = str(ent.get("name") or "")
        service, env = split_env_suffix(name)
        info: Dict[str, Any] = {
            "name": name,
            "type": str(ent.get("type") or ent.get("entityType") or ""),
            "service_label": service or name,
        }
        if env:
            info["env"] = env
        if ent.get("domain"):
            info["domain"] = str(ent["domain"])
        out["entities"][guid] = info
    if len(guids) > max_e:
        notes.append("newrelic: resolved the first %d of %d entity guids "
                     "(hints_max_entities)" % (max_e, len(guids)))
    if guids:
        log("hints: %d/%d entity guid(s) resolved"
            % (len(out["entities"]), len(guids)))


def _short(guid: str) -> str:
    return guid if len(guid) <= 12 else guid[:8] + "..."


def _ident(name: str) -> str:
    """Backtick an NRQL attribute so dotted/odd names stay valid."""
    return "`%s`" % name.replace("`", "")


def uniques_nrql(event: str, attr: str, limit: int, since: str) -> str:
    """The read-only NRQL used for attribute value discovery."""
    return ("SELECT uniques(%s, %d) FROM %s SINCE %s"
            % (_ident(attr), max(1, limit), event, since))


def _uniques_from(results: Any) -> List[Any]:
    """Pull the value list out of a ``uniques()`` NRQL result."""
    for row in results or []:
        if not isinstance(row, dict):
            continue
        for key, val in row.items():
            if str(key).startswith("uniques") and isinstance(val, list):
                return val
        for val in row.values():
            if isinstance(val, list):
                return val
    return []


def _attr_hints(nr: Any, scan: Dict[str, Any], cfg: Dict[str, Any],
                out: Dict[str, Any], log: _LOG) -> None:
    notes = out["notes"]
    attrs = scan["attrs"]
    if not attrs:
        return
    run = getattr(nr, "run_nrql", None)
    if not callable(run):
        notes.append("newrelic: client has no run_nrql(); attribute "
                     "values not discovered")
        return
    aids: List[int] = list(scan["account_ids"])
    if cfg.get("account_id"):
        aid = _int_or_none(cfg.get("account_id"))
        if aid is not None and aid not in aids:
            aids.insert(0, aid)
    if not aids:
        lister = getattr(nr, "list_account_ids", None)
        try:
            aids = list(lister() or []) if callable(lister) else []
        except Exception as e:  # noqa: BLE001 - degrade
            notes.append("newrelic: cannot list accounts (%s)" % e)
    if not aids:
        notes.append("newrelic: no account id known (none in the "
                     "dashboard, none visible to the key); attribute "
                     "values not discovered")
        return
    max_a = _limit(cfg, "hints_max_attrs")
    max_v = _limit(cfg, "hints_max_values")
    since = str(cfg.get("hints_since") or _DEFAULT_LIMITS["hints_since"])
    done: Set[str] = set()
    for event, attr in attrs[:max_a]:
        if attr in done:
            continue
        nrql = uniques_nrql(event, attr, max_v, since)
        vals: List[Any] = []
        last_err = ""
        for aid in aids[:3]:
            try:
                got = run(int(aid), nrql) or {}
            except Exception as e:  # noqa: BLE001 - degrade
                last_err = str(e)
                continue
            vals = _uniques_from(got.get("results"))
            if vals:
                break
        if not vals:
            notes.append("newrelic: no values for %s on FROM %s%s"
                         % (attr, event,
                            (" (%s)" % last_err) if last_err else ""))
            continue
        done.add(attr)
        out["attr_values"][attr] = [str(v) for v in vals
                                    if v is not None][:max_v]
    if len(attrs) > max_a:
        notes.append("newrelic: values discovered for the first %d of %d "
                     "attributes (hints_max_attrs)" % (max_a, len(attrs)))
    log("hints: values discovered for %d attribute(s)"
        % len(out["attr_values"]))


def _with_defaults(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A partial cfg (tests, API callers) still maps attributes the way
    the converter does: borrow label_map from DEFAULT_CONFIG."""
    cfg = dict(cfg or {})
    if "label_map" not in cfg:
        try:
            from ..config import DEFAULT_CONFIG
            cfg["label_map"] = dict(DEFAULT_CONFIG.get("label_map") or {})
        except Exception:  # noqa: BLE001 - config is optional here
            cfg["label_map"] = {}
    return cfg


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def collect_hints(nr: Any = None, grafana: Any = None, dash: Any = None,
                  cfg: Optional[Dict[str, Any]] = None,
                  log: Optional[_LOG] = None) -> Dict[str, Any]:
    """Collect live translation hints. Never raises; every failure lands
    in ``notes``. ``nr`` is a :class:`NerdGraphClient` (or None), ``grafana``
    a :class:`GrafanaLive` (or None), ``dash`` an NRDashboard or raw NR
    dashboard dict."""
    cfg = _with_defaults(cfg)
    log = log or (lambda msg: None)
    out = empty_hints()
    notes = out["notes"]
    try:
        scan = scan_dashboard(dash, cfg)
    except Exception as e:  # noqa: BLE001 - degrade
        notes.append("hints: dashboard scan failed (%s); no hints" % e)
        return out
    if scan["parse_errors"]:
        notes.append("hints: %d NRQL query(ies) did not parse and were "
                     "skipped" % scan["parse_errors"])
    if grafana is None:
        notes.append("mimir: no Grafana connection (pass --grafana-url); "
                     "metric kinds inferred from names only")
    else:
        try:
            _mimir_hints(grafana, scan, cfg, out, log)
        except Exception as e:  # noqa: BLE001 - never raise
            notes.append("mimir: hint collection failed (%s)" % e)
    if nr is None:
        if scan["guids"]:
            notes.append("newrelic: no NerdGraph connection (pass "
                         "--api-key); %d entity guid(s) unresolved"
                         % len(scan["guids"]))
        if scan["attrs"]:
            notes.append("newrelic: no NerdGraph connection; attribute "
                         "values not discovered")
    else:
        try:
            _entity_hints(nr, scan, cfg, out, log)
        except Exception as e:  # noqa: BLE001 - never raise
            notes.append("newrelic: entity lookup failed (%s)" % e)
        try:
            _attr_hints(nr, scan, cfg, out, log)
        except Exception as e:  # noqa: BLE001 - never raise
            notes.append("newrelic: attribute discovery failed (%s)" % e)
    return out
