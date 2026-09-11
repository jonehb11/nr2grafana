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


def _field(name, label, path, required=False, secret=False,
           placeholder="", help_="", multiline=False):
    """Build one DS_TEMPLATES field spec dict."""
    spec = {"name": name, "label": label, "required": required,
            "secret": secret, "placeholder": placeholder, "help": help_,
            "path": path}
    if multiline:
        spec["multiline"] = True
    return spec


# Guided add-datasource form specs. Each field's "path" says where the
# value lands in the create/update payload: "url", "jsonData.X" or
# "secureJsonData.X" (folded by build_datasource_payload). Anything the
# HTTP API cannot express is called out in "notes".
DS_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "prometheus": {
        "label": "Prometheus / Mimir",
        "plugin_id": "prometheus",
        "core": True,
        "fields": [
            _field("url", "URL", "url", required=True,
                   placeholder="http://mimir:9009/prometheus",
                   help_="Base URL of the Prometheus-compatible API as "
                         "reachable FROM THE GRAFANA SERVER. For Mimir "
                         "include the /prometheus prefix."),
            _field("httpMethod", "HTTP method", "jsonData.httpMethod",
                   placeholder="POST",
                   help_="POST (default, handles long queries) or GET."),
            _field("timeInterval", "Scrape interval",
                   "jsonData.timeInterval", placeholder="60s",
                   help_="Lower bound for $__rate_interval; match your "
                         "scrape/remote-write interval."),
        ],
        "notes": "Works for Prometheus, Mimir and Thanos. Basic auth and "
                 "TLS client certificates cannot be set through this "
                 "form; add them in Grafana (Connections -> Data "
                 "sources) after creation if your endpoint needs them.",
    },
    "loki": {
        "label": "Loki",
        "plugin_id": "loki",
        "core": True,
        "fields": [
            _field("url", "URL", "url", required=True,
                   placeholder="http://loki:3100",
                   help_="Loki base URL as reachable from the Grafana "
                         "server (no /loki/api/v1 suffix)."),
            _field("maxLines", "Max lines", "jsonData.maxLines",
                   placeholder="1000",
                   help_="Line limit per log query (default 1000)."),
        ],
        "notes": "For multi-tenant Loki the X-Scope-OrgID header must be "
                 "added as a custom HTTP header in Grafana after "
                 "creation; it cannot be set through this form.",
    },
    "tempo": {
        "label": "Tempo",
        "plugin_id": "tempo",
        "core": True,
        "fields": [
            _field("url", "URL", "url", required=True,
                   placeholder="http://tempo:3200",
                   help_="Tempo base URL as reachable from the Grafana "
                         "server."),
        ],
        "notes": "Trace-to-logs / trace-to-metrics links reference other "
                 "datasource uids; wire those up in Grafana once the "
                 "Loki and Prometheus datasources exist.",
    },
    "cloudwatch": {
        "label": "Amazon CloudWatch",
        "plugin_id": "cloudwatch",
        "core": True,
        "fields": [
            _field("authType", "Auth type", "jsonData.authType",
                   required=True, placeholder="keys",
                   help_="'keys' (access/secret key below), 'default' "
                         "(instance profile / env credential chain on "
                         "the Grafana server), or 'credentials' (shared "
                         "credentials file on the Grafana server)."),
            _field("defaultRegion", "Default region",
                   "jsonData.defaultRegion", required=True,
                   placeholder="us-east-1",
                   help_="Region used when a query does not set one."),
            _field("accessKey", "Access key ID",
                   "secureJsonData.accessKey", secret=True,
                   placeholder="AKIA...",
                   help_="Required when Auth type is 'keys'."),
            _field("secretKey", "Secret access key",
                   "secureJsonData.secretKey", secret=True,
                   help_="Required when Auth type is 'keys'."),
            _field("assumeRoleArn", "Assume role ARN",
                   "jsonData.assumeRoleArn",
                   placeholder="arn:aws:iam::123456789012:role/grafana",
                   help_="Optional IAM role to assume for queries."),
        ],
        "notes": "Grafana queries CloudWatch from the Grafana server, so "
                 "the server (not your browser) needs network access to "
                 "AWS and, for auth types other than 'keys', the "
                 "matching credentials on that host. The IAM identity "
                 "needs cloudwatch:GetMetricData/ListMetrics (read "
                 "only).",
    },
    "stackdriver": {
        "label": "Google Cloud Monitoring",
        "plugin_id": "stackdriver",
        "core": True,
        "fields": [
            _field("authenticationType", "Authentication type",
                   "jsonData.authenticationType", required=True,
                   placeholder="jwt",
                   help_="'jwt' (service-account key, fields below) or "
                         "'gce' (GCE metadata server; leave the rest "
                         "empty)."),
            _field("defaultProject", "Default project",
                   "jsonData.defaultProject",
                   placeholder="my-gcp-project",
                   help_="project_id from the service-account JSON key "
                         "file."),
            _field("clientEmail", "Client email",
                   "jsonData.clientEmail",
                   placeholder="sa-name@project.iam.gserviceaccount.com",
                   help_="client_email from the service-account JSON "
                         "key file."),
            _field("tokenUri", "Token URI", "jsonData.tokenUri",
                   placeholder="https://oauth2.googleapis.com/token",
                   help_="token_uri from the service-account JSON key "
                         "file."),
            _field("privateKey", "Private key",
                   "secureJsonData.privateKey", secret=True,
                   multiline=True,
                   help_="private_key from the service-account JSON key "
                         "file - paste the full '-----BEGIN PRIVATE "
                         "KEY-----' block including newlines."),
        ],
        "notes": "The Grafana UI's 'upload service account key file' "
                 "button cannot be used through the HTTP API. Instead, "
                 "open the downloaded JSON key file and copy "
                 "client_email, token_uri, project_id and private_key "
                 "into the fields above with authentication type 'jwt'. "
                 "The service account needs the Monitoring Viewer role.",
    },
    "grafana-azure-monitor-datasource": {
        "label": "Azure Monitor",
        "plugin_id": "grafana-azure-monitor-datasource",
        "core": True,
        "fields": [
            _field("cloudName", "Azure cloud", "jsonData.cloudName",
                   placeholder="azuremonitor",
                   help_="'azuremonitor' (public), 'govazuremonitor' or "
                         "'chinaazuremonitor'."),
            _field("tenantId", "Directory (tenant) ID",
                   "jsonData.tenantId", required=True,
                   help_="From the App Registration overview page."),
            _field("clientId", "Application (client) ID",
                   "jsonData.clientId", required=True,
                   help_="From the App Registration overview page."),
            _field("clientSecret", "Client secret",
                   "secureJsonData.clientSecret", required=True,
                   secret=True,
                   help_="A client secret created under the App "
                         "Registration's 'Certificates & secrets'."),
            _field("subscriptionId", "Default subscription",
                   "jsonData.subscriptionId",
                   help_="Optional default subscription id for "
                         "queries."),
        ],
        "notes": "Create an App Registration in Microsoft Entra ID "
                 "(Azure AD), grant it the Monitoring Reader role on "
                 "the subscription, and use its tenant id, client id "
                 "and a client secret here.",
    },
    "nrgrafanaplugin-newrelic-datasource": {
        "label": "New Relic",
        "plugin_id": "nrgrafanaplugin-newrelic-datasource",
        "core": False,
        "fields": [
            _field("apiKey", "API key", "secureJsonData.apiKey",
                   required=True, secret=True, placeholder="NRAK-...",
                   help_="New Relic user API key. Only query access is "
                         "used; nr2grafana never mutates New Relic."),
            _field("accountId", "Account ID", "jsonData.accountId",
                   required=True, placeholder="1234567",
                   help_="Numeric New Relic account id to query."),
            _field("region", "Region", "jsonData.region",
                   placeholder="US", help_="US or EU."),
        ],
        "notes": "Community plugin - install it first: grafana-cli "
                 "plugins install nrgrafanaplugin-newrelic-datasource, "
                 "then restart Grafana. Used only for passthrough "
                 "panels that have no LGTM equivalent.",
    },
}


def _fold_value(payload: Dict[str, Any], path: str, value: Any) -> None:
    """Fold one value into a datasource payload per its template path."""
    if path == "url":
        payload["url"] = str(value)
    elif path.startswith("jsonData."):
        payload.setdefault("jsonData", {})[path[len("jsonData."):]] = value
    elif path.startswith("secureJsonData."):
        key = path[len("secureJsonData."):]
        payload.setdefault("secureJsonData", {})[key] = value
    else:
        raise GrafanaError("unsupported datasource field path %r" % path)


def build_datasource_payload(ds_type: str, name: str,
                             values: Dict[str, str]) -> Dict[str, Any]:
    """Build a create/update datasource payload from template values.

    ``values`` is keyed by field name from ``DS_TEMPLATES[ds_type]``
    (raw ``url`` / ``jsonData.X`` / ``secureJsonData.X`` path keys are
    accepted too). Raises :class:`GrafanaError` for an unknown type or
    missing required fields; never logs secret values.
    """
    tpl = DS_TEMPLATES.get(ds_type)
    if tpl is None:
        raise GrafanaError(
            "unknown datasource type %r (known: %s)"
            % (ds_type, ", ".join(sorted(DS_TEMPLATES))))
    values = values or {}
    payload: Dict[str, Any] = {"name": name, "type": tpl["plugin_id"],
                               "access": "proxy"}
    used = set()
    missing = []
    for field in tpl["fields"]:
        val = values.get(field["name"])
        key = field["name"]
        if val is None:
            val = values.get(field["path"])
            key = field["path"]
        if val is not None:
            used.add(key)
        if val is None or str(val).strip() == "":
            if field.get("required"):
                missing.append(field["name"])
            continue
        _fold_value(payload, field["path"], val)
    if missing:
        raise GrafanaError(
            "missing required field(s) for %s datasource: %s"
            % (ds_type, ", ".join(missing)))
    for key, val in values.items():
        if key in used or val is None or str(val).strip() == "":
            continue
        if key == "url" or key.startswith("jsonData.") \
                or key.startswith("secureJsonData."):
            _fold_value(payload, key, val)
    return payload


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

    def update_datasource(self, uid: str,
                          payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._req("PUT", "/api/datasources/uid/" + uid, payload)

    def delete_datasource(self, uid: str) -> None:
        self._req("DELETE", "/api/datasources/uid/" + uid)

    # -- datasource health -------------------------------------------------

    # Cheap per-type probe targets for datasources whose plugin does not
    # implement the /health endpoint (older Grafana or older plugins).
    _HEALTH_PROBES = {
        "prometheus": {"refId": "A", "expr": "vector(1)"},
        "loki": {"refId": "A",
                 "expr": 'sum(count_over_time({job=~".+"}[1m]))'},
        "tempo": {"refId": "A", "queryType": "traceql", "query": "{}",
                  "limit": 1},
    }

    def datasource_health(self, uid: str) -> Dict[str, Any]:
        """Health of one datasource; never raises.

        Tries ``GET /api/datasources/uid/<uid>/health`` first; when that
        endpoint is missing or errors, falls back to a cheap probe query
        through ``/api/ds/query``. Always returns ``{"status", "message"}``
        with status ``ok``, ``error`` or ``unknown`` (no probe possible).
        """
        health_err = ""
        try:
            resp = self._req("GET",
                             "/api/datasources/uid/%s/health" % uid)
            if isinstance(resp, dict) and resp.get("status"):
                status = str(resp.get("status", "")).lower()
                status = "ok" if status in ("ok", "success") else "error"
                return {"status": status,
                        "message": str(resp.get("message", ""))}
            health_err = "health endpoint returned no status"
        except GrafanaError as e:
            health_err = str(e)
        return self._health_probe(uid, health_err)

    def _health_probe(self, uid: str, health_err: str) -> Dict[str, Any]:
        """Probe a datasource with a minimal read query; never raises."""
        try:
            ds = self.datasource_by_uid(uid)
        except GrafanaError as e:
            return {"status": "error",
                    "message": "cannot look up datasource uid %r: %s"
                               % (uid, e)}
        if ds is None:
            return {"status": "error",
                    "message": "datasource uid %r not found" % uid}
        ds_type = ds.get("type", "")
        target = self._HEALTH_PROBES.get(ds_type)
        if target is None:
            return {"status": "unknown",
                    "message": "no /health endpoint (%s) and no probe "
                               "query known for type %r; check the "
                               "datasource in Grafana directly"
                               % (health_err, ds_type)}
        try:
            resp = self.ds_query(uid, ds_type, dict(target),
                                 frm="now-5m")
        except GrafanaError as e:
            return {"status": "error",
                    "message": "probe query failed: %s" % e}
        res = (resp.get("results") or {}).get("A") or {}
        err = _result_error(res)
        if err:
            return {"status": "error",
                    "message": "probe query failed: %s" % err}
        return {"status": "ok",
                "message": "probe query via /api/ds/query succeeded "
                           "(no /health endpoint for this datasource)"}

    # -- datasource proxy introspection ------------------------------------

    def _proxy_get(self, uid: str, path: str,
                   errors: Optional[List[str]] = None) -> Any:
        """GET through the datasource proxy; None + stashed error on
        failure."""
        try:
            return self._req(
                "GET", "/api/datasources/proxy/uid/%s%s" % (uid, path))
        except GrafanaError as e:
            if errors is not None:
                errors.append(str(e))
            return None

    @staticmethod
    def _data_list(resp: Any, errors: Optional[List[str]] = None,
                   what: str = "") -> List[Any]:
        """Extract the "data" list from a Prom/Loki-style response."""
        if isinstance(resp, dict) and isinstance(resp.get("data"), list):
            return resp["data"]
        if resp is not None and errors is not None:
            errors.append("unexpected %s response shape: %.120r"
                          % (what or "proxy", resp))
        return []

    def prom_metric_names(self, uid: str,
                          errors: Optional[List[str]] = None) \
            -> List[str]:
        """All metric names known to a Prometheus-type datasource.

        Returns ``[]`` on any failure (proxy 404, auth, network); pass a
        list as ``errors`` to receive the error text.
        """
        resp = self._proxy_get(uid, "/api/v1/label/__name__/values",
                               errors)
        return [str(v) for v in self._data_list(resp, errors,
                                                "metric names")]

    def prom_labels(self, uid: str,
                    errors: Optional[List[str]] = None) -> List[str]:
        """All label names known to a Prometheus-type datasource."""
        resp = self._proxy_get(uid, "/api/v1/labels", errors)
        return [str(v) for v in self._data_list(resp, errors,
                                                "label names")]

    def prom_label_values(self, uid: str, label: str, match: str = "",
                          errors: Optional[List[str]] = None) \
            -> List[str]:
        """Values of one label, optionally restricted to a series
        matcher."""
        path = ("/api/v1/label/%s/values"
                % urllib.parse.quote(label, safe=""))
        if match:
            path += "?" + urllib.parse.urlencode({"match[]": match})
        resp = self._proxy_get(uid, path, errors)
        return [str(v) for v in self._data_list(resp, errors,
                                                "label values")]

    def prom_series(self, uid: str, match: str, frm: str = "now-1h",
                    errors: Optional[List[str]] = None) \
            -> List[Dict[str, Any]]:
        """Series (label sets) matching a selector over a recent
        window."""
        try:
            start = _epoch_ms(frm) // 1000
            end = _epoch_ms("now") // 1000
        except GrafanaError as e:
            if errors is not None:
                errors.append(str(e))
            return []
        path = "/api/v1/series?" + urllib.parse.urlencode(
            [("match[]", match), ("start", start), ("end", end)])
        resp = self._proxy_get(uid, path, errors)
        return [s for s in self._data_list(resp, errors, "series")
                if isinstance(s, dict)]

    def loki_labels(self, uid: str,
                    errors: Optional[List[str]] = None) -> List[str]:
        """All stream label names known to a Loki datasource."""
        resp = self._proxy_get(uid, "/loki/api/v1/labels", errors)
        return [str(v) for v in self._data_list(resp, errors,
                                                "loki labels")]

    def loki_label_values(self, uid: str, label: str,
                          errors: Optional[List[str]] = None) \
            -> List[str]:
        """Values of one Loki stream label."""
        path = ("/loki/api/v1/label/%s/values"
                % urllib.parse.quote(label, safe=""))
        resp = self._proxy_get(uid, path, errors)
        return [str(v) for v in self._data_list(resp, errors,
                                                "loki label values")]

    # -- token capabilities ------------------------------------------------

    def permissions_report(self) -> Dict[str, Any]:
        """What this token can do, probed with GETs only; never raises.

        Returns ``{"user", "role", "can_admin_datasources",
        "can_edit_dashboards", "detail"}``. Probes ``/api/user``,
        ``/api/org``, ``/api/datasources`` and (when available)
        ``/api/access-control/user/permissions`` - nothing destructive.
        """
        detail: List[str] = []
        user = ""
        can_read_ds = False
        try:
            u = self._req("GET", "/api/user")
            if isinstance(u, dict):
                user = str(u.get("login") or u.get("email")
                           or u.get("name") or "")
            detail.append("authenticated as %r" % (user or "unknown"))
        except GrafanaError as e:
            s = str(e)
            if "HTTP 401" in s:
                detail.append("token rejected (401): check the "
                              "service-account token")
            elif "HTTP 403" in s:
                detail.append("/api/user denied (403)")
            else:
                detail.append("/api/user failed: %s" % s)
        try:
            org = self._req("GET", "/api/org")
            if isinstance(org, dict) and org.get("name"):
                detail.append("org %r" % org["name"])
        except GrafanaError:
            pass  # org read is informational only
        try:
            self.datasources()
            can_read_ds = True
            detail.append("can list datasources")
        except GrafanaError as e:
            detail.append("cannot list datasources: %s" % e)
        perms = None
        try:
            p = self._req("GET", "/api/access-control/user/permissions")
            if isinstance(p, dict):
                perms = p
        except GrafanaError:
            detail.append("access-control API unavailable; "
                          "capabilities inferred from read probes")
        if perms is not None:
            can_admin = any(k in perms for k in
                            ("datasources:create", "datasources:write",
                             "datasources:delete"))
            can_edit = any(k in perms for k in
                           ("dashboards:create", "dashboards:write"))
            if can_admin:
                role = "Admin"
            elif can_edit:
                role = "Editor"
            elif user or can_read_ds:
                role = "Viewer"
            else:
                role = ""
            if not can_admin:
                detail.append("no datasources:create permission - "
                              "Admin role needed to create datasources")
            if not can_edit:
                detail.append("no dashboards:write permission - "
                              "Editor role needed to import dashboards")
        else:
            can_admin = False
            can_edit = bool(user or can_read_ds)
            role = ""
            detail.append("datasource-admin rights could not be "
                          "verified without the access-control API")
        return {"user": user, "role": role,
                "can_admin_datasources": can_admin,
                "can_edit_dashboards": can_edit,
                "detail": "; ".join(detail)}

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
            fix_add = ("Add a %s datasource without leaving nr2grafana: "
                       "web UI Datasources -> Add datasource, or "
                       "`nr2grafana grafana add-datasource --type %s`"
                       % (label, plugin_id))
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
