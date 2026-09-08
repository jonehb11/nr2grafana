"""Live Grafana operations through a service-account token.

Extends the minimal :class:`GrafanaClient` with instance inventory
(datasources, plugins), requirement checking against a packaged
``requirements.json``, per-panel data-pull testing via ``POST
/api/ds/query``, and dashboard update pushes.

Nothing here persists or logs secrets; the token lives only in the
client's in-memory headers.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from ..livecheck import iter_targets, substitute
from .client import GrafanaClient, GrafanaError

# Relative time specs accepted by ds_query ("now", "now-30m", "now-2h").
_REL_TIME = re.compile(r"^now(?:-(\d+)([smhdw]))?$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_VAR_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

_FAMILY_LABEL = {
    "prometheus": "Prometheus", "loki": "Loki", "tempo": "Tempo",
    "cloudwatch": "CloudWatch", "stackdriver": "Google Cloud Monitoring",
}


def _epoch_ms(spec: Any, now: Optional[float] = None) -> int:
    """Convert "now-1h"-style or epoch-millisecond input to epoch ms."""
    if now is None:
        now = time.time()
    s = str(spec).strip()
    m = _REL_TIME.match(s)
    if m:
        delta = 0
        if m.group(1):
            delta = int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
        return int((now - delta) * 1000)
    if re.match(r"^\d+$", s):
        return int(s)
    raise GrafanaError(
        "unsupported time spec %r (use 'now', 'now-1h' or epoch ms)" % spec)


def _frame_points(frame: Dict[str, Any]) -> int:
    """Number of rows in one dataframe of a /api/ds/query response."""
    values = (frame.get("data") or {}).get("values") or []
    return max((len(col) for col in values if isinstance(col, list)),
               default=0)


def _result_error(res: Dict[str, Any]) -> str:
    """Extract an error message from one refId result, '' if none."""
    if res.get("error"):
        return str(res["error"])
    errs = res.get("errors") or []
    if errs:
        return "; ".join(str(e.get("message") or e) if isinstance(e, dict)
                         else str(e) for e in errs)
    status = res.get("status")
    if isinstance(status, int) and status >= 400:
        return "query returned HTTP %d" % status
    return ""


class GrafanaLive(GrafanaClient):
    """GrafanaClient plus live inventory, testing and push operations."""

    # -- inventory ---------------------------------------------------------

    def plugins(self) -> List[Dict[str, Any]]:
        return self._req("GET", "/api/plugins")

    def datasource_by_uid(self, uid: str) -> Optional[Dict[str, Any]]:
        try:
            return self._req("GET", "/api/datasources/uid/" + uid)
        except GrafanaError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def search_dashboards(self, query: str = "") -> List[Dict[str, Any]]:
        path = "/api/search?type=dash-db"
        if query:
            path += "&query=" + urllib.parse.quote(query)
        return self._req("GET", path)

    def get_dashboard_by_uid(self, uid: str) -> Dict[str, Any]:
        return self._req("GET", "/api/dashboards/uid/" + uid)

    def create_datasource(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._req("POST", "/api/datasources", payload)

    # -- datasource resolution --------------------------------------------

    def _pick_ds(self, dss: List[Dict[str, Any]], ds_type: str) -> str:
        """uid of the default datasource of ds_type, else first, else ''."""
        matches = [d for d in dss if d.get("type") == ds_type]
        for d in matches:
            if d.get("isDefault"):
                return d.get("uid", "")
        if matches:
            return matches[0].get("uid", "")
        return ""

    def resolve_ds_map(self, dash: Dict[str, Any],
                       preferred: Optional[Dict[str, str]] = None) \
            -> Dict[str, str]:
        """Map datasource template var names and ``${var}`` uid refs found
        in the dashboard to concrete datasource uids on this instance.

        Preference order per variable: ``preferred[var_name]``, else the
        instance default datasource of the variable's type, else the first
        datasource of that type. Unresolvable variables are omitted.
        """
        preferred = preferred or {}
        dss = self.datasources()
        out: Dict[str, str] = {}

        def put(name: str, ds_type: str) -> None:
            uid = preferred.get(name) or self._pick_ds(dss, ds_type)
            if uid:
                out[name] = uid
                out["${%s}" % name] = uid

        for var in (dash.get("templating") or {}).get("list") or []:
            if var.get("type") != "datasource":
                continue
            put(var.get("name", ""), var.get("query", ""))
        # Raw ${var} placeholders in targets that have no matching
        # templating entry: resolve via the target's datasource type.
        for _panel, tgt in iter_targets(dash):
            ds = tgt.get("datasource") or {}
            m = _VAR_REF.match(ds.get("uid") or "")
            if m and ds.get("uid") not in out:
                put(m.group(1), ds.get("type", ""))
        return out

    # -- requirements ------------------------------------------------------

    def check_requirements(self, requirements: Dict[str, Any]) \
            -> List[Dict[str, Any]]:
        """Check a requirements dict against this instance.

        Returns one row per required datasource/plugin:
        ``{"item", "status": "ok|missing|no-default|wrong-type",
        "detail", "fix"}``.
        """
        dss = self.datasources()
        results: List[Dict[str, Any]] = []

        for req in requirements.get("datasources") or []:
            family = req.get("family", "")
            plugin_id = req.get("plugin_id", family)
            label = _FAMILY_LABEL.get(family, family or plugin_id)
            item = "datasource:%s" % (family or plugin_id)
            matches = [d for d in dss if d.get("type") == plugin_id]
            uid_ref = req.get("uid_ref", "")
            concrete = uid_ref and not uid_ref.startswith("${")
            fix_add = ("Add a %s datasource: Connections -> Data sources "
                       "-> Add -> %s" % (label, label))
            if not req.get("core", True):
                fix_add = ("grafana-cli plugins install %s, then restart "
                           "Grafana and %s" % (plugin_id, fix_add[0].lower()
                                               + fix_add[1:]))
            if concrete:
                found = next((d for d in dss if d.get("uid") == uid_ref),
                             None)
                if found is not None and found.get("type") != plugin_id:
                    results.append({
                        "item": item, "status": "wrong-type",
                        "detail": "uid %r is a %s datasource, expected %s"
                                  % (uid_ref, found.get("type"), plugin_id),
                        "fix": "Point the dashboard at a %s datasource or "
                               "rename/recreate uid %r as type %s"
                               % (label, uid_ref, plugin_id)})
                    continue
                if found is None and matches:
                    results.append({
                        "item": item, "status": "ok",
                        "detail": "uid %r not found; %d %s datasource(s) "
                                  "available for remapping (e.g. %r)"
                                  % (uid_ref, len(matches), plugin_id,
                                     matches[0].get("uid", "")),
                        "fix": ""})
                    continue
                if found is None:
                    results.append({
                        "item": item, "status": "missing",
                        "detail": "no datasource of type %r (uid %r not "
                                  "found)" % (plugin_id, uid_ref),
                        "fix": fix_add})
                    continue
                results.append({"item": item, "status": "ok",
                                "detail": "uid %r (%s)"
                                          % (uid_ref, found.get("name", "")),
                                "fix": ""})
                continue
            if not matches:
                results.append({
                    "item": item, "status": "missing",
                    "detail": "no datasource of type %r on %s"
                              % (plugin_id, self.base),
                    "fix": fix_add})
                continue
            default = next((d for d in matches if d.get("isDefault")), None)
            chosen = default or matches[0]
            results.append({
                "item": item, "status": "ok",
                "detail": "%d datasource(s) of type %r; will use %r"
                          % (len(matches), plugin_id,
                             chosen.get("name", chosen.get("uid", ""))),
                "fix": ""})

        plugin_ids = None
        for req in requirements.get("plugins") or []:
            pid = req.get("id", "")
            if not pid:
                continue
            if plugin_ids is None:
                try:
                    plugin_ids = set(p.get("id", "")
                                     for p in self.plugins())
                except GrafanaError as e:
                    plugin_ids = set()
                    results.append({
                        "item": "plugins", "status": "missing",
                        "detail": "cannot list plugins: %s" % e,
                        "fix": "Use a service-account token with plugin "
                               "read access, or check plugins manually"})
            if pid in plugin_ids:
                results.append({"item": "plugin:%s" % pid, "status": "ok",
                                "detail": "installed", "fix": ""})
            else:
                results.append({
                    "item": "plugin:%s" % pid, "status": "missing",
                    "detail": req.get("reason", "not installed"),
                    "fix": req.get("grafana_cli")
                           or "grafana-cli plugins install %s" % pid})
        return results

    # -- data-pull testing -------------------------------------------------

    def ds_query(self, ds_uid: str, ds_type: str, target: Dict[str, Any],
                 frm: str = "now-1h", to: str = "now") -> Dict[str, Any]:
        """POST /api/ds/query for one target; returns the raw response."""
        q: Dict[str, Any] = dict(target)
        q["refId"] = target.get("refId") or "A"
        q["datasource"] = {"uid": ds_uid, "type": ds_type}
        q["intervalMs"] = 60000
        q["maxDataPoints"] = 300
        if ds_type == "prometheus":
            q["expr"] = target.get("expr", "")
            q["range"] = True
        elif ds_type == "loki":
            q["expr"] = target.get("expr", "")
            q["queryType"] = "range"
        # tempo/others: pass the target's own query fields through.
        body = {"queries": [q],
                "from": str(_epoch_ms(frm)),
                "to": str(_epoch_ms(to))}
        return self._req("POST", "/api/ds/query", body)

    def test_dashboard(self, dash: Dict[str, Any],
                       ds_map: Optional[Dict[str, str]] = None,
                       log: Optional[Callable[[str], None]] = None) \
            -> List[Dict[str, Any]]:
        """Run every panel target through /api/ds/query.

        Substitutes template variables via :func:`livecheck.substitute`
        and datasource refs via ``ds_map`` (``resolve_ds_map`` output by
        default). Never raises per panel; failures become
        ``status: "error"`` rows.
        """
        if ds_map is None:
            try:
                ds_map = self.resolve_ds_map(dash)
            except GrafanaError:
                ds_map = {}
        emit = log or (lambda m: None)
        results: List[Dict[str, Any]] = []
        for panel, tgt in iter_targets(dash):
            if panel.get("type") in ("row", "text"):
                continue
            ds = tgt.get("datasource") or {}
            uid = ds.get("uid") or ""
            ds_type = ds.get("type") or ""
            if uid in ds_map:
                uid = ds_map[uid]
            elif _VAR_REF.match(uid):
                uid = ds_map.get(uid[2:-1], uid)
            expr = tgt.get("expr") or tgt.get("query") or ""
            if isinstance(expr, str):
                expr = substitute(expr)
            row = {"panel_id": panel.get("id"),
                   "panel_title": panel.get("title") or "",
                   "refId": tgt.get("refId") or "",
                   "datasource": uid, "expr": expr,
                   "status": "error", "error": "",
                   "frames": 0, "points": 0}
            results.append(row)
            if not uid or _VAR_REF.match(uid):
                row["error"] = ("unresolved datasource ref %r (no %s "
                                "datasource on instance?)"
                                % (ds.get("uid"), ds_type or "matching"))
                emit("  ERROR %s: %s" % (row["panel_title"], row["error"]))
                continue
            target = dict(tgt)
            if isinstance(tgt.get("expr"), str):
                target["expr"] = substitute(tgt["expr"])
            if isinstance(tgt.get("query"), str):
                target["query"] = substitute(tgt["query"])
            try:
                resp = self.ds_query(uid, ds_type, target)
            except GrafanaError as e:
                row["error"] = str(e)
                emit("  ERROR %s: %s" % (row["panel_title"], row["error"]))
                continue
            res = (resp.get("results") or {}).get(row["refId"] or "A") or {}
            err = _result_error(res)
            if err:
                row["error"] = err
                emit("  ERROR %s: %s" % (row["panel_title"], err))
                continue
            frames = res.get("frames") or []
            row["frames"] = len(frames)
            row["points"] = sum(_frame_points(f) for f in frames
                                if isinstance(f, dict))
            row["status"] = "data" if row["points"] > 0 else "no-data"
            emit("  %-7s %s [%s] %d frame(s), %d point(s)"
                 % (row["status"], row["panel_title"], row["refId"],
                    row["frames"], row["points"]))
        return results

    # -- push --------------------------------------------------------------

    def update_dashboard(self, dash: Dict[str, Any], folder_uid: str = "",
                         message: str = "") -> Dict[str, Any]:
        """Push an updated dashboard (overwrite=True import)."""
        return self.import_dashboard(
            dash, folder_uid=folder_uid, overwrite=True,
            message=message or "Updated by nr2grafana")
