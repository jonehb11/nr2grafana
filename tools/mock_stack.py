#!/usr/bin/env python3
"""Offline mock of Grafana + New Relic NerdGraph (stdlib only).

Runs two local HTTP servers so the whole nr2grafana product - fetch,
convert, datasource management, data testing, parity, diagnosis,
remediation, import - can be demoed and end-to-end tested without any
real Grafana or New Relic:

* fake Grafana: /api/health, /api/user, /api/org, in-memory datasource
  CRUD (+ /health per datasource), folders, dashboard import/read,
  /api/search, /api/plugins, deterministic /api/ds/query frames, and
  the Prometheus/Loki proxy introspection endpoints backed by a
  configurable metric/label inventory.
* fake NerdGraph: POST /graphql serving the dashboards under
  ``fixtures/newrelic/`` (entitySearch + entity reads), a deterministic
  ``actor.account.nrql`` whose numbers are consistent with the fake
  Grafana data (so parity finds matches), and ``actor.user``.

Determinism rules for query data:

* a PromQL expr returns a realistic multi-point timeseries (>= 30
  points) when it references the metric ``up`` or any metric in the
  inventory; unknown metrics return an empty frame; anything containing
  ``syntax_error`` returns a query error. ``by (<label>)`` grouping
  yields one series per configured facet value, each with a distinct
  phase so the lines differ (``histogram_quantile`` collapses back to
  one series).
* the timeseries values follow a gentle, deterministic curve centered
  on the metric's baseline value - a slow ~1h wave keyed on absolute
  time (identical on both fake backends, so a faithful translation
  compares as "match") plus a tiny per-expression wiggle so distinct
  metrics draw distinct shapes. Metrics listed in
  ``MockState.diverge_metrics`` instead follow a strong ramp that does
  *not* track the New Relic side, so the comparison view has believable
  value-mismatches to show next to the matches.
* a LogQL expr returns data when its stream-selector labels exist in
  the Loki inventory; aggregations yield a timeseries, bare selectors
  yield log lines. Loki-backed panels therefore read as gf-empty until
  a Loki datasource is created, then flow real log lines - the
  "add a datasource and watch it light up" loop.
* NRQL aggregates return the same baseline curve (per-facet /
  per-bucket TIMESERIES) so a faithful translation compares as "match";
  plain ``SELECT count(*)`` scalars return the flat baseline and
  ``SELECT *`` event queries return log-style rows.

Auth is enforced: the fake Grafana wants ``Authorization: Bearer
<token>`` (except on /api/health, which is public like the real one)
and the fake NerdGraph wants the ``API-Key`` header - wrong or missing
credentials get a 401, so auth diagnostics can be exercised. The mock
credentials below are not real secrets.

CLI::

    python3 tools/mock_stack.py --port 3000 --nr-port 3001

Importable::

    from mock_stack import start_mock
    stack = start_mock(port=0, nr_port=0)   # ephemeral ports
    ... stack.grafana_url, stack.nr_url, stack.state ...
    stack.stop()
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

DEFAULT_GRAFANA_TOKEN = "mock-token"
DEFAULT_NR_API_KEY = "NRAK-MOCK"
DEFAULT_VALUE = 42.0

_FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "fixtures",
    "newrelic")

# Metric names the shipped fixtures translate to (see
# fixtures/newrelic/*.json run through the converter), plus "up".
DEFAULT_METRICS = [
    "up",
    "http_server_request_duration_seconds_count",
    "http_server_request_duration_seconds_sum",
    "http_server_request_duration_seconds_bucket",
    "traces_span_metrics_duration_milliseconds_bucket",
    "node_cpu_seconds_total",
    "kube_pod_container_status_restarts_total",
    "checkout_orders_completed",
]

# Metrics whose fake Grafana data intentionally diverges from the fake
# New Relic side, giving the comparison view a couple of honest
# value-mismatches to draw. Chosen to hit non-critical demo panels
# (host CPU, container restarts) so the golden-signal panels still
# match.
DEFAULT_DIVERGE_METRICS = [
    "node_cpu_seconds_total",
    "kube_pod_container_status_restarts_total",
]

DEFAULT_PROM_LABELS = {
    "service_name": ["checkout", "payments", "alpha", "beta"],
    "instance": ["checkout-1:9100", "checkout-2:9100"],
    "le": ["0.5", "1", "2", "+Inf"],
    "http_route": ["/cart", "/pay"],
    "span_name": ["GET /cart", "POST /pay"],
    "level": ["error", "warn", "info"],
    "pod": ["checkout-abc", "checkout-def"],
    "namespace": ["prod", "staging"],
    "cluster": ["prod"],
    "mode": ["idle", "user", "system"],
    "deployment_environment": ["prod", "staging"],
    "http_response_status_code": ["200", "500"],
    "job": ["checkout/app"],
}

DEFAULT_LOKI_LABELS = {
    "service_name": ["checkout", "payments"],
    "level": ["error", "warn", "info"],
    "job": ["checkout/app"],
}

DEFAULT_PLUGINS = [
    "prometheus", "loki", "tempo", "cloudwatch", "stackdriver",
    "grafana-azure-monitor-datasource",
]

_TOKEN_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_BY_RE = re.compile(r"\bby\s*\(([^)]*)\)", re.IGNORECASE)
_SELECTOR_RE = re.compile(r"\{([^}]*)\}")
_MATCHER_RE = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')
_NRQL_AGG_RE = re.compile(r"\bSELECT\s+[A-Za-z_]+\s*\(", re.IGNORECASE)
_NRQL_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)
_FACET_RE = re.compile(r"\bFACET\b", re.IGNORECASE)
_TIMESERIES_RE = re.compile(r"\bTIMESERIES\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# shared in-memory state
# ---------------------------------------------------------------------------

class MockState(object):
    """In-memory state shared by both fake servers.

    Tests and demos may tweak the public attributes: the metric/label
    inventory (``prom_metrics``, ``prom_labels``, ``loki_labels``),
    ``facet_values`` (series emitted for grouped/faceted queries),
    ``default_value`` (the constant both fake backends agree on) and
    the credentials. Never put real secrets here.
    """

    def __init__(self, grafana_token=DEFAULT_GRAFANA_TOKEN,
                 nr_api_key=DEFAULT_NR_API_KEY,
                 fixtures=None):
        self.lock = threading.Lock()
        self.grafana_token = grafana_token
        self.nr_api_key = nr_api_key
        # metric name -> value override (None = default_value)
        self.prom_metrics: Dict[str, Optional[float]] = dict(
            (m, None) for m in DEFAULT_METRICS)
        self.prom_labels: Dict[str, List[str]] = dict(
            (k, list(v)) for k, v in DEFAULT_PROM_LABELS.items())
        self.loki_labels: Dict[str, List[str]] = dict(
            (k, list(v)) for k, v in DEFAULT_LOKI_LABELS.items())
        self.facet_values: List[str] = ["a", "b"]
        self.default_value: float = DEFAULT_VALUE
        # Metrics whose fake Grafana series intentionally DISAGREE with
        # the New Relic side (a strong ramp instead of the shared
        # baseline curve), so the comparison view shows real
        # value-mismatches alongside the matches. Tests may edit this.
        self.diverge_metrics = set(DEFAULT_DIVERGE_METRICS)
        self.plugins: List[str] = list(DEFAULT_PLUGINS)
        self.nr_accounts = set([1234567, 7654321])
        self.fixtures: List[Dict[str, Any]] = fixtures or []
        # fake Grafana instance state
        self.datasources: List[Dict[str, Any]] = []
        self.folders: List[Dict[str, Any]] = []
        self.dashboards: Dict[str, Dict[str, Any]] = {}
        self._next_id = 0

    def next_id(self) -> int:
        with self.lock:
            self._next_id += 1
            return self._next_id

    def add_metric(self, name: str, value: Optional[float] = None) -> None:
        self.prom_metrics[name] = value

    def metric_names(self) -> List[str]:
        return list(self.prom_metrics)

    def value_for(self, metric: str) -> float:
        v = self.prom_metrics.get(metric)
        return self.default_value if v is None else float(v)

    # -- datasources -------------------------------------------------------

    def add_datasource(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a datasource; returns the stored (secret-free) dict."""
        with self.lock:
            n = self._next_id = self._next_id + 1
            ds = {
                "id": n,
                "uid": str(payload.get("uid") or "mock-ds-%d" % n),
                "orgId": 1,
                "name": str(payload.get("name") or "datasource-%d" % n),
                "type": str(payload.get("type") or ""),
                "typeName": str(payload.get("type") or ""),
                "access": payload.get("access") or "proxy",
                "url": payload.get("url") or "",
                "isDefault": not self.datasources,
                "jsonData": dict(payload.get("jsonData") or {}),
                "readOnly": False,
                # secret VALUES are dropped; only field names are kept,
                # exactly like the real API's response.
                "secureJsonFields": dict(
                    (k, True)
                    for k in (payload.get("secureJsonData") or {})),
            }
            self.datasources.append(ds)
            return ds

    def find_datasource(self, uid: str) -> Optional[Dict[str, Any]]:
        for ds in self.datasources:
            if ds.get("uid") == uid:
                return ds
        return None


def load_fixtures(directory: str = "") -> List[Dict[str, Any]]:
    """Load every parseable NR dashboard JSON under a fixtures dir."""
    directory = directory or _FIXTURES_DIR
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(directory):
        return out
    n = 0
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, fname),
                      encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages \
                or not all(isinstance(p, dict) for p in pages):
            continue
        n += 1
        entity = dict(data)
        entity.setdefault("guid", "MOCK-DASH-%d" % n)
        entity.setdefault("name", fname)
        entity["_accountId"] = 1234567
        out.append(entity)
    return out


# ---------------------------------------------------------------------------
# deterministic frames
# ---------------------------------------------------------------------------

def _hash01(*parts: Any) -> float:
    """Deterministic float in ``[0, 1)`` from the given parts.

    Uses hashlib rather than the builtin ``hash`` (which is salted per
    process for strings) so the fake data is byte-for-byte reproducible
    across runs and processes - a hard requirement for the two fake
    backends to agree.
    """
    raw = "|".join(str(p) for p in parts).encode()
    return int(hashlib.md5(raw).hexdigest()[:8], 16) / float(0x100000000)


def _facet_phase(facet_value: str) -> float:
    """Stable phase offset (radians) for one series of a grouped query.

    Keyed on the facet *value* string, which survives translation
    unchanged, so the fake Grafana and fake NerdGraph give matching
    series a matching phase (still "match") while distinct facets draw
    visibly distinct lines.
    """
    if not facet_value:
        return 0.0
    return _hash01("facet", facet_value) * 2.0 * math.pi


def _wave(t_s: float, phase: float = 0.0) -> float:
    """Gentle deterministic multiplier centered on 1.0.

    A pure function of absolute time (epoch seconds) and a per-series
    phase, so both fake backends sample the *same* underlying curve and
    a faithful translation lands on "match". The ~1h period keeps it
    slowly varying, so the coarser NRQL TIMESERIES buckets still line up
    with the finer Grafana frames when parity aligns them by timestamp.
    """
    x = t_s / 600.0
    return (1.0 + 0.06 * math.sin(x + phase)
            + 0.02 * math.sin(2.7 * x + 1.3 * phase))


def _jitter(expr: str, i: int) -> float:
    """Tiny per-expression wiggle so distinct metrics draw distinct
    shapes on the Grafana side.

    Index-seeded (a hash of the expression and the bucket index), never
    a live RNG, so repeated queries return identical data. Kept small
    (+/-2%) so a metric that should match the New Relic side - which
    carries no jitter - stays inside parity's match tolerance.
    """
    return (_hash01(expr, i) - 0.5) * 2.0 * 0.02


def _bucket_times(frm_ms: int, to_ms: int, cap: int = 200) \
        -> List[int]:
    """Epoch-ms tick marks for a frame: >= 30 points over a 1h range."""
    step = max(15000, (to_ms - frm_ms) // 45)
    times: List[int] = []
    t = frm_ms
    while t <= to_ms and len(times) < cap:
        times.append(int(t))
        t += step
    return times


def _time_frame(ref_id: str, labels: Dict[str, str], frm_ms: int,
                to_ms: int, value: float, phase: float = 0.0,
                expr: str = "", diverge: bool = False) -> Dict[str, Any]:
    """A realistic multi-point timeseries frame.

    ``value`` is the series baseline; the shape is the shared time wave
    (so it matches the New Relic side) plus a small per-expr wiggle,
    unless ``diverge`` is set, in which case the series follows a strong
    ramp that deliberately does not track New Relic.
    """
    times = _bucket_times(frm_ms, to_ms)
    n = len(times)
    vals: List[float] = []
    for i, tm in enumerate(times):
        t_s = tm / 1000.0
        if diverge:
            frac = (i / float(n - 1)) if n > 1 else 0.5
            v = value * (0.35 + 1.7 * frac) * _wave(t_s, phase)
        else:
            v = value * _wave(t_s, phase) * (1.0 + _jitter(expr, i))
        vals.append(round(float(v), 4))
    return {"schema": {"refId": ref_id,
                       "fields": [
                           {"name": "Time", "type": "time"},
                           {"name": "Value", "type": "number",
                            "labels": labels or {}}]},
            "data": {"values": [times, vals]}}


def _empty_frame(ref_id: str) -> Dict[str, Any]:
    return {"schema": {"refId": ref_id,
                       "fields": [
                           {"name": "Time", "type": "time"},
                           {"name": "Value", "type": "number"}]},
            "data": {"values": [[], []]}}


def _log_frame(ref_id: str, frm_ms: int, to_ms: int) -> Dict[str, Any]:
    # Payment-error lines are interleaved so a "mock payment failed"
    # line leads whether a consumer takes the first N lines or the most
    # recent N (``samples._log_lines`` surfaces ``entries[-limit:]``):
    # the log panel then lines up with the NR ``SELECT *`` side, whose
    # first row is also a payment failure. The warn/info lines add
    # believable variety for the chart.
    lines = [
        "level=error msg=\"mock payment failed\" attempt=1",
        "level=info msg=\"checkout completed\" order=1005 amount=42.00",
        "level=error msg=\"mock payment failed\" attempt=2 order=1002",
        "level=warn msg=\"retrying charge\" gateway=stripe order=1003",
        "level=error msg=\"mock payment failed\" attempt=3 order=1004",
        "level=warn msg=\"slow downstream\" dep=inventory latency_ms=812",
    ]
    step = max(1, (to_ms - frm_ms) // (len(lines) + 1))
    times = [int(frm_ms + step * (i + 1)) for i in range(len(lines))]
    return {"schema": {"refId": ref_id,
                       "fields": [
                           {"name": "Time", "type": "time"},
                           {"name": "Line", "type": "string"}]},
            "data": {"values": [times, lines]}}


def _err_result(msg: str, status: int = 400) -> Dict[str, Any]:
    return {"error": msg, "errors": [{"message": msg}], "status": status}


def _group_label(expr: str) -> str:
    """First by(...) label that isn't "le"; '' when ungrouped."""
    if "histogram_quantile" in expr:
        return ""
    for m in _BY_RE.finditer(expr):
        for part in m.group(1).split(","):
            name = part.strip()
            if name and name != "le":
                return name
    return ""


def _prom_result(state: MockState, ref_id: str, expr: str,
                 frm_ms: int, to_ms: int) -> Dict[str, Any]:
    if "syntax_error" in expr:
        return _err_result(
            "parse error at char 1: unexpected identifier "
            "\"syntax_error\"")
    tokens = set(_TOKEN_RE.findall(expr))
    known = [m for m in state.metric_names() if m in tokens]
    if not known:
        return {"status": 200, "frames": [_empty_frame(ref_id)]}
    metric = known[0]
    value = state.value_for(metric)
    diverge = metric in state.diverge_metrics
    group = _group_label(expr)
    if group:
        frames = [_time_frame(ref_id, {group: fv}, frm_ms, to_ms, value,
                              _facet_phase(fv), expr, diverge)
                  for fv in state.facet_values]
    else:
        frames = [_time_frame(ref_id, {}, frm_ms, to_ms, value, 0.0,
                              expr, diverge)]
    return {"status": 200, "frames": frames}


def _loki_selector_known(state: MockState, expr: str) -> bool:
    m = _SELECTOR_RE.search(expr)
    if not m:
        return False
    matchers = _MATCHER_RE.findall(m.group(1))
    for label, op, val in matchers:
        values = state.loki_labels.get(label)
        if values is None:
            return False
        if op == "=" and values and val not in values:
            return False
    return True


def _loki_result(state: MockState, ref_id: str, expr: str,
                 frm_ms: int, to_ms: int) -> Dict[str, Any]:
    if "syntax_error" in expr:
        return _err_result(
            "parse error at line 1, col 1: syntax error: "
            "unexpected IDENTIFIER")
    if not _loki_selector_known(state, expr):
        return {"status": 200, "frames": [_empty_frame(ref_id)]}
    if expr.lstrip().startswith("{"):
        return {"status": 200,
                "frames": [_log_frame(ref_id, frm_ms, to_ms)]}
    value = state.default_value
    group = _group_label(expr)
    if group:
        frames = [_time_frame(ref_id, {group: fv}, frm_ms, to_ms, value,
                              _facet_phase(fv), expr)
                  for fv in state.facet_values]
    else:
        frames = [_time_frame(ref_id, {}, frm_ms, to_ms, value, 0.0,
                              expr)]
    return {"status": 200, "frames": frames}


def _nrql_rows(state: MockState, nrql: str,
               now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Deterministic NRQL results consistent with the fake Grafana."""
    if now is None:
        now = time.time()
    now = int(now)
    if not _NRQL_AGG_RE.search(nrql):
        # SELECT * style event queries: rows without numeric values,
        # honoring an explicit LIMIT (nr2grafana's sample pulls use
        # "SELECT * ... LIMIT n"). Message text matches the fake
        # Loki log lines so side-by-side sample review lines up.
        n = 3
        m = _NRQL_LIMIT_RE.search(nrql)
        if m:
            n = max(1, min(int(m.group(1)), 100))
        return [{"timestamp": (now - 60 * i) * 1000,
                 "message": "mock payment failed attempt=%d" % i,
                 "level": "error"} for i in range(1, n + 1)]
    value = state.default_value
    facets = state.facet_values if _FACET_RE.search(nrql) else [None]
    rows: List[Dict[str, Any]] = []
    if _TIMESERIES_RE.search(nrql):
        # Per-bucket values follow the SAME shared time wave the fake
        # Grafana samples (keyed on absolute time, per-facet phase), so
        # the coarse NRQL buckets line up bucket-for-bucket with the
        # finer Grafana frames and a faithful translation charts as a
        # believable "match" - not a dead-flat line next to a wavy one.
        for i in range(6):
            begin = now - 3600 + i * 600
            mid = begin + 300
            for fv in facets:
                phase = _facet_phase(fv) if fv is not None else 0.0
                bucket_val = round(value * _wave(mid, phase), 4)
                row: Dict[str, Any] = {"beginTimeSeconds": begin,
                                       "endTimeSeconds": begin + 600,
                                       "result": bucket_val}
                if fv is not None:
                    row["facet"] = fv
                rows.append(row)
    else:
        for fv in facets:
            row = {"result": value}
            if fv is not None:
                row["facet"] = fv
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class _JSONHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-stack/1.2"

    def log_message(self, fmt, *args):  # noqa: D102 - silence stderr
        if getattr(self.server, "verbose", False):
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    @property
    def state(self) -> MockState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Any:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self.route(method)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 - keep the server alive
            try:
                self._send(500, {"message": "mock server error: %s" % e})
            except Exception:  # noqa: BLE001
                pass

    def route(self, method: str) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# fake Grafana
# ---------------------------------------------------------------------------

class GrafanaHandler(_JSONHandler):
    """Fake Grafana HTTP API, honest to the real endpoint shapes."""

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        return auth == "Bearer " + self.state.grafana_token

    def route(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)

        if parts == ["api", "health"] and method == "GET":
            self._send(200, {"database": "ok", "commit": "mock",
                             "version": "11.0.0-mock"})
            return
        if not self._authed():
            self._send(401, {"message": "Unauthorized",
                             "traceID": ""})
            return

        if method == "GET" and parts == ["api", "user"]:
            self._send(200, {"id": 1, "login": "mock-sa",
                             "email": "mock-sa@example.com",
                             "name": "Mock Service Account",
                             "orgId": 1})
            return
        if method == "GET" and parts == ["api", "org"]:
            self._send(200, {"id": 1, "name": "Mock Org"})
            return
        if method == "GET" and parts == ["api", "access-control",
                                         "user", "permissions"]:
            self._send(200, {
                "datasources:create": ["datasources:*"],
                "datasources:write": ["datasources:*"],
                "datasources:delete": ["datasources:*"],
                "datasources:read": ["datasources:*"],
                "dashboards:create": ["folders:*"],
                "dashboards:write": ["dashboards:*"],
                "dashboards:read": ["dashboards:*"],
            })
            return
        if parts == ["api", "datasources"]:
            self._datasources(method)
            return
        if len(parts) >= 4 and parts[:3] == ["api", "datasources",
                                             "uid"]:
            self._datasource_by_uid(method, parts[3:])
            return
        if len(parts) >= 5 and parts[:4] == ["api", "datasources",
                                             "proxy", "uid"]:
            self._proxy(method, parts[4], parts[5:], query)
            return
        if parts == ["api", "folders"]:
            self._folders(method)
            return
        if parts == ["api", "dashboards", "db"] and method == "POST":
            self._import_dashboard()
            return
        if len(parts) == 4 and parts[:3] == ["api", "dashboards",
                                             "uid"] and method == "GET":
            self._dashboard_by_uid(parts[3])
            return
        if parts == ["api", "search"] and method == "GET":
            self._search(query)
            return
        if parts == ["api", "plugins"] and method == "GET":
            self._send(200, [{"id": pid, "name": pid,
                              "type": "datasource", "enabled": True}
                             for pid in self.state.plugins])
            return
        if parts == ["api", "ds", "query"] and method == "POST":
            self._ds_query()
            return
        self._send(404, {"message": "not found: %s %s"
                                    % (method, parsed.path)})

    # -- datasource CRUD ---------------------------------------------------

    def _datasources(self, method: str) -> None:
        if method == "GET":
            self._send(200, list(self.state.datasources))
            return
        if method == "POST":
            payload = self._body()
            if not isinstance(payload, dict) or not payload.get("name") \
                    or not payload.get("type"):
                self._send(400, {"message": "bad request data: name "
                                            "and type are required"})
                return
            for ds in self.state.datasources:
                if ds.get("name") == payload["name"]:
                    self._send(409, {"message": "data source with the "
                                                "same name already "
                                                "exists"})
                    return
            ds = self.state.add_datasource(payload)
            self._send(200, {"datasource": ds, "id": ds["id"],
                             "message": "Datasource added",
                             "name": ds["name"]})
            return
        self._send(405, {"message": "method not allowed"})

    def _datasource_by_uid(self, method: str, rest: List[str]) -> None:
        uid = rest[0]
        ds = self.state.find_datasource(uid)
        if len(rest) == 2 and rest[1] == "health" and method == "GET":
            if ds is None:
                self._send(404, {"message": "data source not found"})
            elif ds.get("type") in ("prometheus", "loki", "tempo"):
                self._send(200, {"status": "OK",
                                 "message": "mock datasource is "
                                            "working"})
            else:
                # Like plugins without a /health resource.
                self._send(404, {"message": "Health check not "
                                            "implemented"})
            return
        if len(rest) != 1:
            self._send(404, {"message": "not found"})
            return
        if ds is None:
            self._send(404, {"message": "data source not found"})
            return
        if method == "GET":
            self._send(200, ds)
            return
        if method == "PUT":
            payload = self._body()
            if not isinstance(payload, dict):
                self._send(400, {"message": "bad request data"})
                return
            with self.state.lock:
                for key in ("name", "type", "url", "access"):
                    if payload.get(key) is not None:
                        ds[key] = payload[key]
                if isinstance(payload.get("jsonData"), dict):
                    ds["jsonData"] = dict(payload["jsonData"])
                secure = payload.get("secureJsonData") or {}
                for k in secure:
                    ds["secureJsonFields"][k] = True
            self._send(200, {"datasource": ds,
                             "message": "Datasource updated"})
            return
        if method == "DELETE":
            with self.state.lock:
                self.state.datasources.remove(ds)
            self._send(200, {"message": "Data source deleted"})
            return
        self._send(405, {"message": "method not allowed"})

    # -- datasource proxy (Prom / Loki introspection) ----------------------

    def _proxy(self, method: str, uid: str, rest: List[str],
               query: Dict[str, List[str]]) -> None:
        ds = self.state.find_datasource(uid)
        if method != "GET" or ds is None:
            self._send(404, {"message": "data source not found"})
            return
        ds_type = ds.get("type")
        state = self.state
        if ds_type == "prometheus":
            if rest == ["api", "v1", "label", "__name__", "values"]:
                self._send(200, {"status": "success",
                                 "data": state.metric_names()})
                return
            if rest == ["api", "v1", "labels"]:
                self._send(200, {"status": "success",
                                 "data": sorted(state.prom_labels)})
                return
            if len(rest) == 5 and rest[:3] == ["api", "v1", "label"] \
                    and rest[4] == "values":
                label = urllib.parse.unquote(rest[3])
                self._send(200, {"status": "success",
                                 "data": state.prom_labels.get(label,
                                                               [])})
                return
            if rest == ["api", "v1", "series"]:
                match = (query.get("match[]") or [""])[0]
                m = _TOKEN_RE.search(match)
                metric = m.group(0) if m else ""
                if metric in state.prom_metrics:
                    self._send(200, {"status": "success", "data": [
                        {"__name__": metric,
                         "service_name": "checkout"}]})
                else:
                    self._send(200, {"status": "success", "data": []})
                return
        elif ds_type == "loki":
            if rest == ["loki", "api", "v1", "labels"]:
                self._send(200, {"status": "success",
                                 "data": list(state.loki_labels)})
                return
            if len(rest) == 6 and rest[:4] == ["loki", "api", "v1",
                                               "label"] \
                    and rest[5] == "values":
                label = urllib.parse.unquote(rest[4])
                self._send(200, {"status": "success",
                                 "data": state.loki_labels.get(label,
                                                               [])})
                return
        self._send(404, {"message": "no proxy route for %s"
                                    % "/".join(rest)})

    # -- folders / dashboards ----------------------------------------------

    def _folders(self, method: str) -> None:
        if method == "GET":
            self._send(200, list(self.state.folders))
            return
        if method == "POST":
            payload = self._body()
            title = (payload or {}).get("title") or ""
            if not title:
                self._send(400, {"message": "folder title cannot be "
                                            "empty"})
                return
            n = self.state.next_id()
            folder = {"id": n, "uid": "mock-folder-%d" % n,
                      "title": title}
            with self.state.lock:
                self.state.folders.append(folder)
            self._send(200, folder)
            return
        self._send(405, {"message": "method not allowed"})

    def _import_dashboard(self) -> None:
        body = self._body()
        dash = (body or {}).get("dashboard")
        if not isinstance(dash, dict):
            self._send(400, {"message": "bad request data: missing "
                                        "dashboard"})
            return
        uid = dash.get("uid") or "mock-dash-%d" % self.state.next_id()
        overwrite = bool(body.get("overwrite"))
        with self.state.lock:
            existing = self.state.dashboards.get(uid)
            if existing is not None and not overwrite:
                self._send(412, {"message": "A dashboard with the same "
                                            "uid already exists",
                                 "status": "name-exists"})
                return
            version = (existing["version"] + 1) if existing else 1
            slug = re.sub(r"[^a-z0-9]+", "-",
                          (dash.get("title") or uid).lower()).strip("-")
            stored = dict(dash)
            stored["uid"] = uid
            stored["version"] = version
            self.state.dashboards[uid] = {
                "dashboard": stored, "version": version,
                "folderUid": body.get("folderUid") or "",
                "slug": slug,
            }
        self._send(200, {"id": self.state.next_id(), "uid": uid,
                         "url": "/d/%s/%s" % (uid, slug),
                         "status": "success", "version": version,
                         "slug": slug})

    def _dashboard_by_uid(self, uid: str) -> None:
        entry = self.state.dashboards.get(uid)
        if entry is None:
            self._send(404, {"message": "Dashboard not found"})
            return
        self._send(200, {"dashboard": entry["dashboard"],
                         "meta": {"uid": uid,
                                  "slug": entry["slug"],
                                  "folderUid": entry["folderUid"],
                                  "version": entry["version"],
                                  "url": "/d/%s/%s"
                                         % (uid, entry["slug"])}})

    def _search(self, query: Dict[str, List[str]]) -> None:
        want = (query.get("query") or [""])[0].lower()
        out = []
        for uid, entry in self.state.dashboards.items():
            title = entry["dashboard"].get("title") or uid
            if want and want not in title.lower():
                continue
            out.append({"id": 1, "uid": uid, "title": title,
                        "uri": "db/%s" % entry["slug"],
                        "url": "/d/%s/%s" % (uid, entry["slug"]),
                        "type": "dash-db", "tags": [],
                        "folderUid": entry["folderUid"]})
        self._send(200, out)

    # -- /api/ds/query -----------------------------------------------------

    def _ds_query(self) -> None:
        body = self._body()
        queries = (body or {}).get("queries") or []
        try:
            frm_ms = int(body.get("from"))
            to_ms = int(body.get("to"))
        except (TypeError, ValueError):
            to_ms = int(time.time() * 1000)
            frm_ms = to_ms - 3600 * 1000
        results: Dict[str, Any] = {}
        for q in queries:
            if not isinstance(q, dict):
                continue
            ref = q.get("refId") or "A"
            ds_ref = q.get("datasource") or {}
            uid = ds_ref.get("uid") if isinstance(ds_ref, dict) else ""
            ds = self.state.find_datasource(uid or "")
            if ds is None:
                results[ref] = _err_result(
                    "datasource %r not found" % uid, 404)
                continue
            ds_type = ds.get("type") or ""
            expr = q.get("expr") or q.get("query") or ""
            if not isinstance(expr, str):
                expr = ""
            if ds_type == "prometheus":
                results[ref] = _prom_result(self.state, ref, expr,
                                            frm_ms, to_ms)
            elif ds_type == "loki":
                results[ref] = _loki_result(self.state, ref, expr,
                                            frm_ms, to_ms)
            else:
                # tempo / cloudwatch / passthrough: deterministic empty.
                results[ref] = {"status": 200,
                                "frames": [_empty_frame(ref)]}
        self._send(200, {"results": results})


# ---------------------------------------------------------------------------
# fake NerdGraph
# ---------------------------------------------------------------------------

class NerdGraphHandler(_JSONHandler):
    """Fake NerdGraph: POST /graphql, read-only, fixture-backed."""

    def route(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if method != "POST" or path != "/graphql":
            self._send(404, {"errors": [{"message": "not found"}]})
            return
        if (self.headers.get("API-Key") or "") != self.state.nr_api_key:
            self._send(401, {"errors": [{"message":
                                         "invalid or missing API key"}]})
            return
        body = self._body()
        query = (body or {}).get("query") or ""
        variables = (body or {}).get("variables") or {}
        if "entitySearch" in query:
            self._entity_search()
        elif "nrql" in query and "q" in variables:
            self._nrql(variables)
        elif "entity" in query and "guid" in variables:
            self._entity(variables)
        elif "user" in query:
            actor = {"user": {"email": "mock-user@example.com",
                              "name": "Mock User", "id": 1}}
            if "accounts" in query:
                actor["accounts"] = [
                    {"id": a, "name": "Mock account %d" % a}
                    for a in sorted(self.state.nr_accounts)]
            self._send(200, {"data": {"actor": actor}})
        else:
            self._send(200, {"errors": [
                {"message": "mock NerdGraph does not implement this "
                            "query"}]})

    def _entity_search(self) -> None:
        entities = [{"guid": fx["guid"], "name": fx.get("name", ""),
                     "accountId": fx.get("_accountId", 1234567),
                     "dashboardParentGuid": None}
                    for fx in self.state.fixtures]
        self._send(200, {"data": {"actor": {"entitySearch": {
            "results": {"entities": entities, "nextCursor": None}}}}})

    def _entity(self, variables: Dict[str, Any]) -> None:
        guid = variables.get("guid")
        for fx in self.state.fixtures:
            if fx["guid"] == guid:
                entity = dict((k, v) for k, v in fx.items()
                              if not k.startswith("_"))
                self._send(200, {"data": {"actor": {"entity": entity}}})
                return
        self._send(200, {"data": {"actor": {"entity": None}}})

    def _nrql(self, variables: Dict[str, Any]) -> None:
        try:
            account = int(variables.get("id"))
        except (TypeError, ValueError):
            account = -1
        nrql = str(variables.get("q") or "")
        if account not in self.state.nr_accounts:
            self._send(200, {"errors": [
                {"message": "Account %s not found or not authorized "
                            "for this API key" % account}]})
            return
        if "syntax_error" in nrql:
            self._send(200, {"errors": [
                {"message": "NRQL Syntax Error: Error at line 1: "
                            "unexpected token 'syntax_error'"}]})
            return
        now = time.time()
        rows = _nrql_rows(self.state, nrql, now)
        facets = ["facet"] if _FACET_RE.search(nrql) else []
        self._send(200, {"data": {"actor": {"account": {"nrql": {
            "results": rows,
            "metadata": {"facets": facets,
                         "timeWindow": {
                             "begin": int((now - 3600) * 1000),
                             "end": int(now * 1000)}}}}}}})


# ---------------------------------------------------------------------------
# server lifecycle
# ---------------------------------------------------------------------------

class MockStack(object):
    """Handles for a running mock stack; stop() shuts both servers."""

    def __init__(self, grafana_server, nr_server, state: MockState):
        self._servers = [grafana_server, nr_server]
        self.state = state
        self.grafana_url = "http://127.0.0.1:%d" \
            % grafana_server.server_address[1]
        self.nr_url = "http://127.0.0.1:%d" \
            % nr_server.server_address[1]
        self._threads: List[threading.Thread] = []

    @property
    def grafana_token(self) -> str:
        return self.state.grafana_token

    @property
    def nr_api_key(self) -> str:
        return self.state.nr_api_key

    def _start(self) -> None:
        for srv in self._servers:
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        for srv in self._servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:  # noqa: BLE001 - already down is fine
                pass
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []

    def __enter__(self) -> "MockStack":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


def start_mock(port: int = 0, nr_port: int = 0,
               state: Optional[MockState] = None,
               fixtures_dir: str = "",
               verbose: bool = False) -> MockStack:
    """Start the fake Grafana and fake NerdGraph servers.

    ``port``/``nr_port`` 0 binds ephemeral ports (read the actual URLs
    from the returned handles). Pass a prepared :class:`MockState` to
    customize inventory/credentials; otherwise fixtures are loaded from
    ``fixtures/newrelic/`` (or ``fixtures_dir``).
    """
    if state is None:
        state = MockState(fixtures=load_fixtures(fixtures_dir))
    elif not state.fixtures:
        state.fixtures = load_fixtures(fixtures_dir)
    grafana_server = ThreadingHTTPServer(("127.0.0.1", port),
                                         GrafanaHandler)
    nr_server = ThreadingHTTPServer(("127.0.0.1", nr_port),
                                    NerdGraphHandler)
    for srv in (grafana_server, nr_server):
        srv.daemon_threads = True
        srv.state = state  # type: ignore[attr-defined]
        srv.verbose = verbose  # type: ignore[attr-defined]
    stack = MockStack(grafana_server, nr_server, state)
    stack._start()
    return stack


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Offline mock Grafana + NerdGraph for nr2grafana "
                    "demos and e2e tests")
    ap.add_argument("--port", type=int, default=3000,
                    help="fake Grafana port (default 3000)")
    ap.add_argument("--nr-port", type=int, default=3001,
                    help="fake NerdGraph port (default 3001)")
    ap.add_argument("--token", default=DEFAULT_GRAFANA_TOKEN,
                    help="Grafana bearer token the mock accepts")
    ap.add_argument("--nr-key", default=DEFAULT_NR_API_KEY,
                    help="New Relic API key the mock accepts")
    ap.add_argument("--fixtures", default="",
                    help="directory of NR dashboard JSON to serve "
                         "(default: fixtures/newrelic/)")
    ap.add_argument("--verbose", action="store_true",
                    help="log every request to stderr")
    args = ap.parse_args(argv)

    state = MockState(grafana_token=args.token, nr_api_key=args.nr_key,
                      fixtures=load_fixtures(args.fixtures))
    try:
        stack = start_mock(port=args.port, nr_port=args.nr_port,
                           state=state, verbose=args.verbose)
    except OSError as e:
        print("cannot bind mock servers: %s (ports %d/%d in use?)"
              % (e, args.port, args.nr_port), file=sys.stderr)
        return 1
    print("mock Grafana:   %s   (token: %s)"
          % (stack.grafana_url, state.grafana_token))
    print("mock NerdGraph: %s/graphql   (API key: %s)"
          % (stack.nr_url, state.nr_api_key))
    print("serving %d fixture dashboard(s); Ctrl-C to stop"
          % len(state.fixtures))
    print("point nr2grafana at the mock NR side with: "
          "export N2G_NERDGRAPH_URL=%s/graphql" % stack.nr_url)
    sys.stdout.flush()  # banner must appear even when redirected
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopping mock stack")
        stack.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
