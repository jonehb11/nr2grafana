"""Metric-driven deep analysis of the LGTM stack (schema
"nr2grafana/deepdive/v1").

Where :mod:`nr2grafana.traffic` / :mod:`nr2grafana.optimize` reason about a
*dashboard's* series/stream cost, this module reads the LGTM components'
own self-metrics -- ``cortex_*`` (Mimir), ``loki_*``, ``tempo_*``,
``otelcol_*`` and ``prometheus_remote_storage_*`` -- plus container RSS
(``container_memory_working_set_bytes``) and turns them into the deeper
capacity / churn / network / Loki-chunk / Tempo-OTel findings the 1.6
contract asks for. No Kubernetes access is required (that is
:mod:`nr2grafana.packing`); everything here comes from PromQL and the
Mimir/Loki HTTP APIs.

:class:`PromClient` is a tiny read-only PromQL/HTTP client over ``/api/v1``
that NEVER raises: every failure is collected into ``.errors`` and the
missing datum simply drops the finding that needed it. It reads directly
from a Prometheus / Mimir / Loki URL, or -- when given a GrafanaLive client
-- through the Grafana datasource proxy.

:func:`analyze` runs the sections and returns ranked findings. Every
finding carries ``severity`` (FAIL/WARN/INFO), ``area``
(capacity|cardinality|churn|network|loki|tempo|efficiency), a ``title``,
an ``evidence`` dict of the numbers behind it, a ``rationale``, optional
paste-ready ``config`` snippets, an ``est_savings`` ESTIMATE, and the risk
flags ``keeps_performance`` / ``keeps_durability`` / ``keeps_availability``
(plus ``keeps_intact`` / ``needs_review`` where a drop must be verified
unused first). SAFETY: no recommendation here reduces replication factor,
retention, scrape interval or zone-awareness for cost; series-drop
recommendations default to the safe option and are marked
``keeps_intact: false`` until the caller proves the dimension unused.

New Relic is never touched; nothing here persists or logs secrets.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

SCHEMA = "nr2grafana/deepdive/v1"
GENERATED_BY = "nr2grafana 1.6.0"

GIB = 2 ** 30
BYTES_PER_GB = 1_000_000_000.0   # decimal GB (egress/storage are billed so)
SECONDS_PER_MONTH = 86400.0 * 30.0

# ---------------------------------------------------------------------------
# Tunable thresholds. Every number that turns a measurement into a finding
# lives here; override any subset via cfg["deepdive"]["thresholds"].
# ---------------------------------------------------------------------------
DEFAULT_T: Dict[str, float] = {
    "series_capacity_margin": 0.85,   # usable fraction of GOMEMLIMIT for TSDB
    "bytes_per_series_default": 4300,  # only if RSS/series cannot be measured
    "gomemlimit_frac_of_limit": 0.85,  # GOMEMLIMIT estimate when unset
    "samples_per_series_low": 0.015,  # <1 sample/66s per series => churn/stale
    "churn_warn_frac_per_h": 0.05,    # series created/h over in-memory series
    "rw_lag_warn_s": 60,
    "chunk_p50_warn_bytes": 200_000,  # Loki chunks flushing this small = card.
    "stream_label_values_warn": 1000,  # a single Loki stream label > this
    "pvc_used_warn": 0.70,
    "queue_util_warn": 0.80,
    "tempo_unused_spans_s": 1.0,
    "top_metric_share": 0.03,         # a metric over this fraction of series
    "top_metrics_n": 10,              # top metrics to list as drop candidates
}

# What 1 GB of spoke->hub remote_write costs on each hop a byte might
# traverse. GENERIC AWS on-demand assumptions (USD/GB) -- clearly labeled,
# not a quote; multiply by the hops YOUR path actually crosses. Override
# via cfg["deepdive"]["egress_usd_per_gb"].
DEFAULT_EGRESS_USD_PER_GB: Dict[str, float] = {
    "cross-AZ (in+out)": 0.02,
    "Transit Gateway (per attachment hop)": 0.02,
    "NAT gateway": 0.045,
    "GWLB / inspection": 0.004,
}

# Default component namespaces used only in the container-RSS / object-store
# label filters. Override via cfg["deepdive"]["namespaces"].
DEFAULT_NS = {"mimir": "mimir", "loki": "loki", "tempo": "tempo"}

_SEV_ORDER = {"FAIL": 0, "WARN": 1, "INFO": 2}


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

def _num(value: Any, default: float = 0.0) -> float:
    """Coerce to float, tolerating None / junk / NaN."""
    try:
        if value is None:
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:        # NaN
        return default
    return out


def _gib(nbytes: Any) -> float:
    return round(_num(nbytes) / GIB, 2)


def _pct(a: Any, b: Any) -> Optional[float]:
    a = _num(a)
    b = _num(b)
    return None if b == 0 else a / b


def _fmt_count(v: Any) -> str:
    """Human-readable magnitude, e.g. 4.0M / 12.3k / 950."""
    v = _num(v)
    if abs(v) >= 1e9:
        return "%.2fG" % (v / 1e9)
    if abs(v) >= 1e6:
        return "%.2fM" % (v / 1e6)
    if abs(v) >= 1e3:
        return "%.1fk" % (v / 1e3)
    return "%.0f" % v


# ---------------------------------------------------------------------------
# PromClient -- read-only PromQL/HTTP over /api/v1; never raises.
# ---------------------------------------------------------------------------

class PromClient(object):
    """Tiny PromQL/HTTP client. NEVER raises.

    ``base`` is a Prometheus base URL, a Mimir Prometheus-API base (include
    the ``/prometheus`` prefix for Mimir), or a Loki gateway base. When
    ``proxy=(grafana, uid)`` is given instead, requests route through
    ``GrafanaLive``'s datasource proxy for datasource ``uid``. Any request
    failure appends ``(path, message)`` to :attr:`errors` and yields an
    empty result, so a missing metric or an unreachable endpoint quietly
    drops only the findings that depend on it.
    """

    def __init__(self, base: Optional[str] = None, headers: Optional[
            Dict[str, str]] = None, timeout: float = 30.0,
            proxy: Optional[Tuple[Any, str]] = None) -> None:
        self.base = base.rstrip("/") if base else None
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._proxy = proxy
        self.errors: List[Tuple[str, str]] = []

    @property
    def available(self) -> bool:
        return self.base is not None or self._proxy is not None

    # -- the single network primitive (subclassed/overridden in tests) -----

    def _request(self, path: str,
                 params: Optional[Dict[str, Any]] = None) -> Any:
        """One GET; parsed JSON or None. Never raises."""
        if not self.available:
            return None
        qs = urllib.parse.urlencode(params, doseq=True) if params else ""
        try:
            if self._proxy is not None:
                grafana, uid = self._proxy
                full = path + (("?" + qs) if qs else "")
                # GrafanaLive._proxy_get already swallows errors -> None.
                return grafana._proxy_get(uid, full, self.errors)
            url = self.base + path + (("?" + qs) if qs else "")
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:   # noqa: BLE001 - client must never raise
            self.errors.append((path, str(exc)[:200]))
            return None

    # -- PromQL ------------------------------------------------------------

    def vector(self, expr: str) -> List[Tuple[Dict[str, str], float]]:
        """Instant-vector result as ``[(labels, value), ...]``."""
        data = self._request("/api/v1/query", {"query": expr})
        if not isinstance(data, dict) or data.get("status") != "success":
            if data is not None:
                self.errors.append(("query", "non-success for %.80s" % expr))
            return []
        out: List[Tuple[Dict[str, str], float]] = []
        for m in (data.get("data") or {}).get("result") or []:
            try:
                out.append((m.get("metric") or {}, float(m["value"][1])))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return out

    def scalar(self, expr: str) -> Optional[float]:
        """First value of an instant vector, or None."""
        vec = self.vector(expr)
        return vec[0][1] if vec else None

    def by(self, expr: str, *labels: str) -> Dict[Any, float]:
        """Instant vector keyed by the given label(s).

        With one label the key is its value; with several it is a tuple.
        Later duplicates win (matching Prometheus' own last-wins).
        """
        out: Dict[Any, float] = {}
        for metric, val in self.vector(expr):
            if len(labels) == 1:
                key: Any = metric.get(labels[0], "")
            else:
                key = tuple(metric.get(lbl, "") for lbl in labels)
            out[key] = val
        return out

    def json(self, path: str,
             params: Optional[Dict[str, Any]] = None) -> Any:
        """Raw GET of an arbitrary API path (cardinality / series APIs)."""
        return self._request(path, params or {})


# ---------------------------------------------------------------------------
# Finding construction
# ---------------------------------------------------------------------------

def _finding(severity: str, area: str, title: str, rationale: str,
             evidence: Optional[Dict[str, Any]] = None,
             config: Optional[List[Dict[str, str]]] = None,
             est_savings: Optional[Dict[str, Any]] = None,
             keeps_performance: bool = True, keeps_durability: bool = True,
             keeps_availability: bool = True,
             keeps_intact: Optional[bool] = None,
             needs_review: bool = False) -> Dict[str, Any]:
    """Build one finding dict in the shape the 1.6 contract mandates."""
    out: Dict[str, Any] = {
        "severity": severity,
        "area": area,
        "title": title,
        "rationale": rationale,
        "evidence": evidence or {},
        "keeps_performance": bool(keeps_performance),
        "keeps_durability": bool(keeps_durability),
        "keeps_availability": bool(keeps_availability),
    }
    if keeps_intact is not None:
        out["keeps_intact"] = bool(keeps_intact)
    if needs_review:
        out["needs_review"] = True
    if config:
        out["config"] = config
    if est_savings:
        out["est_savings"] = {k: v for k, v in est_savings.items()
                              if v is not None}
    return out


def _cfg_pricing(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Effective pricing: costmodel defaults deep-merged with cfg overrides."""
    try:
        from .costmodel import effective_pricing
        return effective_pricing(cfg.get("pricing"))
    except Exception:  # noqa: BLE001 - costmodel optional; fall back
        base = {"mimir": {"usd_per_1k_series_month": 0.60}}
        over = cfg.get("pricing")
        if isinstance(over, dict):
            mimir = over.get("mimir")
            if isinstance(mimir, dict):
                base["mimir"].update(mimir)
        return base


def _series_usd(series: float, pricing: Dict[str, Any]) -> float:
    """Estimated monthly $ for ``series`` active series (ESTIMATE)."""
    per_1k = _num((pricing.get("mimir") or {}).get(
        "usd_per_1k_series_month"), 0.60)
    return round(_num(series) / 1000.0 * per_1k, 2)


# ===========================================================================
# Sections
# ===========================================================================

def section_capacity(prom: PromClient, cfg: Dict[str, Any], t: Dict[
        str, float], pricing: Dict[str, Any]) -> Tuple[
        Dict[str, Any], List[Dict[str, Any]]]:
    """Real per-ingester series capacity from measured bytes/series x
    GOMEMLIMIT, vs the *configured* ``max_global_series`` -- the "the limit
    is not capacity" finding."""
    dcfg = cfg.get("deepdive") or {}
    ns = (dcfg.get("namespaces") or DEFAULT_NS).get("mimir", "mimir")
    rf = int(_num(dcfg.get("replication_factor"), 3)) or 3
    s: Dict[str, Any] = {}
    findings: List[Dict[str, Any]] = []

    s["samples_s"] = prom.scalar(
        "sum(rate(cortex_distributor_received_samples_total[5m]))")
    s["mem_series_total"] = prom.scalar("sum(cortex_ingester_memory_series)")
    series_by_pod = prom.by("cortex_ingester_memory_series", "pod")
    s["ingesters"] = len(series_by_pod) or (prom.scalar(
        "count(cortex_ingester_memory_series)") or 0)
    s["unique_series_est"] = _num(s["mem_series_total"]) / rf if rf else None
    s["created_per_h"] = prom.scalar(
        "sum(increase(cortex_ingester_memory_series_created_total[1h]))")

    rss = prom.by(
        'max by (pod) (container_memory_working_set_bytes'
        '{namespace="%s", container="ingester"})' % ns, "pod")
    s["ingester_rss_gib"] = {k: _gib(v) for k, v in rss.items()}
    bps: List[float] = []
    for pod, series in series_by_pod.items():
        if pod in rss and _num(series) > 0:
            bps.append(_num(rss[pod]) / _num(series))
    measured = (sum(bps) / len(bps)) if bps else None
    s["bytes_per_series"] = round(measured, 1) if measured else None
    bpsv = measured or t["bytes_per_series_default"]

    # GOMEMLIMIT: explicit cfg override, else an estimate from the ingester
    # container memory limit (cAdvisor), else unknown.
    gomem = _num(dcfg.get("gomemlimit_bytes")) or None
    mem_limit = prom.scalar(
        'max(container_spec_memory_limit_bytes'
        '{namespace="%s", container="ingester"})' % ns)
    if gomem is None and mem_limit:
        gomem = _num(mem_limit) * t["gomemlimit_frac_of_limit"]
    s["gomemlimit_bytes"] = round(gomem) if gomem else None
    s["ingester_mem_limit_bytes"] = round(_num(mem_limit)) or None

    budget = _num(gomem) * t["series_capacity_margin"] if gomem else 0.0
    cap_per_ing = (budget / bpsv) if budget else None
    s["series_capacity_per_ingester"] = round(cap_per_ing) if cap_per_ing \
        else None
    global_cap = None
    if cap_per_ing and s["ingesters"]:
        # Each unique series lives on RF ingesters, so unique capacity is
        # the summed per-ingester capacity divided by the replication factor.
        global_cap = cap_per_ing * _num(s["ingesters"]) / rf
    s["global_series_capacity_est"] = round(global_cap) if global_cap \
        else None

    limits = prom.by(
        'max by (limit_name) (cortex_limits_overrides'
        '{limit_name="max_global_series_per_user"}) or '
        'max by (limit_name) (cortex_limits_defaults'
        '{limit_name="max_global_series_per_user"})', "limit_name")
    configured = limits.get("max_global_series_per_user")
    s["configured_max_global_series"] = round(_num(configured)) if \
        configured else None

    if global_cap and configured and global_cap < _num(configured):
        findings.append(_finding(
            "WARN", "capacity",
            "Real series capacity (%s) is below the configured limit (%s)"
            % (_fmt_count(global_cap), _fmt_count(configured)),
            "The configured max_global_series is a guard, not headroom. "
            "At the measured %s B/series x %.0f%% of GOMEMLIMIT %.1fGi over "
            "%d ingester(s) / RF %d, the ring can actually hold ~%s unique "
            "series before ingesters OOM. Plan more ingesters or memory "
            "BEFORE onboarding growth -- do NOT lower RF or retention to "
            "make the number fit." % (
                _fmt_count(bpsv), t["series_capacity_margin"] * 100.0,
                _num(gomem) / GIB, int(_num(s["ingesters"])), rf,
                _fmt_count(global_cap)),
            evidence={
                "bytes_per_series": s["bytes_per_series"] or round(bpsv, 1),
                "bytes_per_series_measured": measured is not None,
                "gomemlimit_gib": _gib(gomem) if gomem else None,
                "ingesters": int(_num(s["ingesters"])),
                "replication_factor": rf,
                "series_capacity_per_ingester":
                    s["series_capacity_per_ingester"],
                "global_series_capacity_est":
                    s["global_series_capacity_est"],
                "configured_max_global_series":
                    s["configured_max_global_series"]},
            config=[{
                "target": "mimir runtime overrides (limits)",
                "language": "yaml",
                "note": "Keep the limit as a GUARD; add ingesters or memory "
                        "to raise real capacity. Never lower RF/retention "
                        "for cost.",
                "snippet":
                    "# The 'limit' is a safety guard, not capacity. Real\n"
                    "# ceiling = bytes/series x usable-GOMEMLIMIT x\n"
                    "# ingesters / RF. To grow: add ingesters or memory\n"
                    "# (memory request == limit; NEVER CPU-limit ingesters).\n"
                    "overrides:\n"
                    "  <tenant>:\n"
                    "    max_global_series_per_user: <keep-as-guard>\n"}],
            keeps_performance=True, keeps_durability=True,
            keeps_availability=True))

    # samples/series: a low ratio means the head is full of stale/churned
    # series (short-lived pods, CI runners, duplicate replicas).
    sps = _pct(s["samples_s"], s["unique_series_est"])
    s["samples_per_series_per_s"] = round(sps, 5) if sps is not None else None
    return s, findings


def section_churn(prom: PromClient, capacity: Dict[str, Any],
                  t: Dict[str, float], pricing: Dict[str, Any]) -> Tuple[
                  Dict[str, Any], List[Dict[str, Any]]]:
    """Series churn + duplicate Prometheus replicas (2x ingest)."""
    s: Dict[str, Any] = {}
    findings: List[Dict[str, Any]] = []

    sps = capacity.get("samples_per_series_per_s")
    if sps is not None and sps < t["samples_per_series_low"]:
        interval = (1.0 / sps) if sps else 0.0
        findings.append(_finding(
            "WARN", "churn",
            "Low samples/series ratio (%.4f/s, ~1 sample per %.0fs)"
            % (sps, interval),
            "In-memory series far exceed what scrape intervals explain -- "
            "the head is full of high-churn or stale series (short-lived "
            "pods, CI runners, or duplicated replicas). Each churned series "
            "still costs ingester memory and a block index entry for hours "
            "after it dies. Find the label driving it (pod/uid/container "
            "id) and drop it at remote_write, keeping local scrape data.",
            evidence={"samples_per_series_per_s": sps,
                      "unique_series_est":
                          capacity.get("unique_series_est")},
            config=[_relabel_drop_snippet("<high_churn_label>", "labeldrop")],
            keeps_performance=True, keeps_intact=False, needs_review=True))

    created = _num(capacity.get("created_per_h"))
    mem_total = _num(capacity.get("mem_series_total"))
    churn_frac = _pct(created, mem_total)
    s["series_churn_frac_per_h"] = round(churn_frac, 4) if churn_frac \
        is not None else None
    if churn_frac is not None and churn_frac > t["churn_warn_frac_per_h"]:
        findings.append(_finding(
            "WARN", "churn",
            "Series churn is %.1f%%/h of in-memory series" % (
                churn_frac * 100.0),
            "%s series are created per hour against %s in memory. Each "
            "churned series costs ingester RAM plus a block index entry for "
            "hours after it dies; identify the unbounded label (pod, uid, "
            "container id) and relabel it away at remote_write." % (
                _fmt_count(created), _fmt_count(mem_total)),
            evidence={"created_per_h": round(created),
                      "mem_series_total": round(mem_total),
                      "churn_frac_per_h": s["series_churn_frac_per_h"]},
            config=[_relabel_drop_snippet("<high_churn_label>", "labeldrop")],
            keeps_performance=True, keeps_intact=False, needs_review=True))
    return s, findings


def section_cardinality(prom: PromClient, mimir: PromClient,
                        capacity: Dict[str, Any], cfg: Dict[str, Any],
                        t: Dict[str, float], pricing: Dict[str, Any]) \
        -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Top metrics/labels by series (drop candidates) and duplicate
    Prometheus replicas without an HA tracker (2x ingest)."""
    dcfg = cfg.get("deepdive") or {}
    cluster_label = dcfg.get("cluster_label", "cluster")
    replica_label = dcfg.get("replica_label", "prometheus_replica")
    s: Dict[str, Any] = {}
    findings: List[Dict[str, Any]] = []

    ha_present = bool(prom.vector(
        "count(cortex_ha_tracker_elected_replica_changes_total)"))
    s["ha_tracker_present"] = ha_present

    # Duplicate Prometheus HA replicas: replicas per cluster > 1 with no HA
    # tracker means every series is ingested (and billed over the wire)
    # twice.
    dup = prom.by(
        "count by (%s) (count by (%s, %s) (prometheus_build_info))"
        % (cluster_label, cluster_label, replica_label), cluster_label)
    s["prometheus_replicas_by_cluster"] = {k: int(_num(v))
                                           for k, v in dup.items()}
    head_by_cluster = prom.by(
        "max by (%s) (sum by (%s, %s) (prometheus_tsdb_head_series))"
        % (cluster_label, cluster_label, replica_label), cluster_label)
    for cluster, count in dup.items():
        if _num(count) > 1 and not ha_present:
            dup_series = _num(head_by_cluster.get(cluster))
            findings.append(_finding(
                "FAIL", "cardinality",
                "Cluster %r runs %d Prometheus replicas with no Mimir HA "
                "tracker" % (cluster or "?", int(_num(count))),
                "Every series is ingested %d times and sent over the "
                "network %d times. Enable the Mimir HA tracker (dedup keeps "
                "RF, retention and availability fully intact) or run a "
                "single replica per source." % (
                    int(_num(count)), int(_num(count))),
                evidence={"cluster": cluster,
                          "replicas": int(_num(count)),
                          "ha_tracker_present": ha_present,
                          "head_series_per_replica": round(dup_series)
                          if dup_series else None},
                config=[_ha_tracker_snippet(cluster_label, replica_label)],
                est_savings={
                    "series": round(dup_series) if dup_series else None,
                    "monthly_usd": _series_usd(dup_series, pricing)
                    if dup_series else None},
                keeps_performance=True, keeps_durability=True,
                keeps_availability=True, keeps_intact=True))

    # Top metrics / labels by series via the Mimir cardinality API.
    top_metrics: Dict[str, int] = {}
    if mimir is not None and mimir.available:
        data = mimir.json("/api/v1/cardinality/label_values",
                          {"label_names[]": "__name__", "limit": 40})
        if isinstance(data, dict) and data.get("labels"):
            lab = data["labels"][0]
            for c in lab.get("cardinality") or []:
                name = c.get("label_value")
                if name is not None:
                    top_metrics[str(name)] = int(_num(c.get("series_count")))
            s["metric_names_total"] = lab.get("label_values_count")
        names = mimir.json("/api/v1/cardinality/label_names", {"limit": 30})
        if isinstance(names, dict) and names.get("cardinality"):
            s["top_labels_by_values"] = {
                str(c.get("label_name")): int(_num(c.get(
                    "label_values_count")))
                for c in names["cardinality"]
                if c.get("label_name") is not None}
    s["top_metrics_by_series"] = dict(sorted(
        top_metrics.items(), key=lambda kv: -kv[1])[:25])

    if top_metrics:
        total = _num(capacity.get("unique_series_est")) or sum(
            top_metrics.values())
        hot = [(k, v) for k, v in sorted(
            top_metrics.items(), key=lambda kv: -kv[1])
            if total and v / total > t["top_metric_share"]]
        hot = hot[:int(t["top_metrics_n"])]
        if hot:
            listed = ", ".join("%s (%s, %.0f%%)" % (
                k, _fmt_count(v), (v / total * 100.0) if total else 0.0)
                for k, v in hot)
            saved = sum(v for _k, v in hot)
            findings.append(_finding(
                "INFO", "cardinality",
                "%d metric(s) each own >%.0f%% of active series"
                % (len(hot), t["top_metric_share"] * 100.0),
                "Top series owners: %s. Any that NO dashboard or rule "
                "references can be dropped at the spoke remote_write "
                "(never at scrape -- keep local debug data). Verify each is "
                "unused (e.g. mimirtool analyze grafana/ruler/prometheus) "
                "before dropping; histograms with per-pod/le labels are the "
                "usual offenders." % listed,
                evidence={"top_metrics": dict(hot),
                          "unique_series_est":
                              capacity.get("unique_series_est")},
                config=[_relabel_drop_snippet(
                    "<unused_metric_name>", "drop")],
                est_savings={"series": round(saved)},
                keeps_performance=True, keeps_intact=False,
                needs_review=True))
    return s, findings


def section_network(prom: PromClient, cfg: Dict[str, Any],
                    t: Dict[str, float]) -> Tuple[
                    Dict[str, Any], List[Dict[str, Any]]]:
    """remote_write health + wire GB/month priced by egress path."""
    dcfg = cfg.get("deepdive") or {}
    egress = dict(DEFAULT_EGRESS_USD_PER_GB)
    if isinstance(dcfg.get("egress_usd_per_gb"), dict):
        egress.update(dcfg["egress_usd_per_gb"])
    s: Dict[str, Any] = {}
    findings: List[Dict[str, Any]] = []

    wire_by = prom.by(
        "sum by (instance) (rate(prometheus_remote_storage_bytes_total"
        "[1h]))", "instance")
    s["wire_bytes_s_by_instance"] = {k: round(_num(v), 1)
                                     for k, v in wire_by.items()}
    wire_s = sum(_num(v) for v in wire_by.values())
    s["wire_bytes_s_total"] = round(wire_s, 1)
    gb_month = wire_s * SECONDS_PER_MONTH / BYTES_PER_GB
    s["wire_gb_month"] = round(gb_month, 1)
    s["wire_cost_by_path_mo"] = {k: round(gb_month * v, 2)
                                 for k, v in egress.items()}

    dropped = prom.by(
        "sum by (instance) (increase("
        "prometheus_remote_storage_samples_dropped_total[1h])) > 0",
        "instance")
    failed = prom.by(
        "sum by (instance) (increase("
        "prometheus_remote_storage_samples_failed_total[1h])) > 0",
        "instance")
    s["dropped_per_h"] = {k: round(_num(v)) for k, v in dropped.items()}
    s["failed_per_h"] = {k: round(_num(v)) for k, v in failed.items()}

    hi = prom.by("max by (instance, url) ("
                 "prometheus_remote_storage_highest_timestamp_in_seconds)",
                 "instance", "url")
    sent = prom.by("max by (instance, url) ("
                   "prometheus_remote_storage_queue_highest_sent_timestamp_"
                   "seconds)", "instance", "url")
    lag = {}
    for key in hi:
        lag["%s -> %s" % (key[0], key[1])] = round(
            _num(hi[key]) - _num(sent.get(key, hi[key])), 1)
    s["lag_s"] = lag

    for inst, v in dropped.items():
        findings.append(_finding(
            "FAIL", "network",
            "remote_write is DROPPING samples on %r (%s/h)" % (
                inst, _fmt_count(v)),
            "Dropped samples are permanent data loss on the write path. "
            "Inspect the queue (max_shards/capacity), the target's health "
            "and any 4xx from Mimir; this is never a cost trade.",
            evidence={"instance": inst, "dropped_per_h": round(_num(v))},
            keeps_performance=True, keeps_durability=True,
            keeps_availability=True))
    if failed:
        findings.append(_finding(
            "WARN", "network", "remote_write is retrying/failing samples",
            "Failed sends mean the queue is fighting the target. Raise "
            "maxShards/capacity or fix the slow/erroring endpoint before it "
            "backs up into dropped samples.",
            evidence={"failed_per_h": s["failed_per_h"]}))
    for path, v in lag.items():
        if _num(v) > t["rw_lag_warn_s"]:
            findings.append(_finding(
                "WARN", "network", "remote_write lag on %s is %ss" % (
                    path, v),
                "The queue is not keeping up. Raise maxShards / capacity, or "
                "the target is slow -- investigate before shards saturate.",
                evidence={"path": path, "lag_s": v}))

    if wire_s > 0:
        findings.append(_finding(
            "INFO", "network",
            "remote_write wire volume is ~%s GB/month (compressed)"
            % _fmt_count(gb_month),
            "Cost depends entirely on which hops your spoke->hub bytes "
            "cross. Per-GB ASSUMPTIONS (not a quote): %s. Compare with your "
            "cloud bill before optimizing transfer -- and fix any duplicate "
            "Prometheus replicas first, which halves this volume." % (
                ", ".join("%s $%.3f/GB" % (k, v)
                          for k, v in egress.items())),
            evidence={"wire_gb_month": s["wire_gb_month"],
                      "wire_bytes_s_total": s["wire_bytes_s_total"],
                      "cost_by_path_mo": s["wire_cost_by_path_mo"],
                      "assumptions_usd_per_gb": egress}))
    return s, findings


def section_loki(prom: PromClient, loki: PromClient, cfg: Dict[str, Any],
                 t: Dict[str, float]) -> Tuple[
                 Dict[str, Any], List[Dict[str, Any]]]:
    """Loki chunk size / flush reasons / failed flushes / S3 errors / WAL
    pressure / stream-label cardinality."""
    dcfg = cfg.get("deepdive") or {}
    ns = (dcfg.get("namespaces") or DEFAULT_NS).get("loki", "loki")
    s: Dict[str, Any] = {}
    findings: List[Dict[str, Any]] = []

    s["lines_s"] = prom.scalar(
        "sum(rate(loki_distributor_lines_received_total[5m]))")
    s["bytes_s"] = prom.scalar(
        "sum(rate(loki_distributor_bytes_received_total[5m]))")
    s["streams_created_per_h"] = prom.scalar(
        "sum(increase(loki_ingester_streams_created_total[1h]))")
    s["chunk_size_p50"] = prom.scalar(
        "histogram_quantile(0.5, sum by (le) (rate("
        "loki_ingester_chunk_size_bytes_bucket[1h])))")
    s["chunk_utilization_p50"] = prom.scalar(
        "histogram_quantile(0.5, sum by (le) (rate("
        "loki_ingester_chunk_utilization_bucket[1h])))")
    flushed = prom.by(
        "sum by (reason) (increase("
        "loki_ingester_chunks_flushed_total[1h])) > 0", "reason")
    s["flushed_by_reason_per_h"] = {k: round(_num(v))
                                    for k, v in flushed.items()}
    s["failed_flushes_per_h"] = prom.scalar(
        "sum(increase(loki_ingester_failed_flushes_total[1h]))")
    s["wal_disk_full_failures_per_h"] = prom.scalar(
        "sum(increase(loki_ingester_wal_disk_full_failures_total[1h]))")
    s3 = prom.by(
        "sum by (operation, status_code) (increase("
        "loki_s3_request_duration_seconds_count[1h])) > 0",
        "operation", "status_code")
    s["s3_requests_per_h"] = {"%s/%s" % (k[0], k[1]): round(_num(v))
                              for k, v in s3.items()}
    pvc = prom.by(
        "max by (persistentvolumeclaim) (kubelet_volume_stats_used_bytes"
        '{namespace="%s"} / kubelet_volume_stats_capacity_bytes'
        '{namespace="%s"})' % (ns, ns), "persistentvolumeclaim")
    s["pvc_used_frac"] = {k: round(_num(v), 3) for k, v in pvc.items()}

    if _num(s["failed_flushes_per_h"]) > 0:
        findings.append(_finding(
            "FAIL", "loki",
            "%s Loki chunk flushes/h are FAILING" % _fmt_count(
                s["failed_flushes_per_h"]),
            "Chunks that cannot flush stay in memory and the WAL grows "
            "until the PVC fills -- the classic 'disk filled overnight' "
            "outage. Check S3/object-store permissions (Pod Identity / "
            "bucket policy) and the non-2xx responses below; this is a "
            "durability problem, not a tuning one.",
            evidence={"failed_flushes_per_h": round(_num(
                s["failed_flushes_per_h"])),
                "s3_requests_per_h": s["s3_requests_per_h"]},
            keeps_performance=True, keeps_durability=True,
            keeps_availability=True))
    bad_s3 = {k: v for k, v in s["s3_requests_per_h"].items()
              if not str(k.split("/")[-1]).startswith("2")}
    if bad_s3:
        findings.append(_finding(
            "FAIL", "loki", "Loki object-store returned non-2xx responses",
            "Failing object-store operations (%s) break the flush path and "
            "back logs up into the WAL/PVC. Fix credentials / bucket policy "
            "before the disk fills." % ", ".join(
                "%s=%s/h" % (k, _fmt_count(v)) for k, v in bad_s3.items()),
            evidence={"non_2xx": bad_s3}))
    for pvc_name, frac in s["pvc_used_frac"].items():
        if frac > t["pvc_used_warn"]:
            findings.append(_finding(
                "FAIL" if frac >= 0.9 else "WARN", "loki",
                "Loki PVC %r is %.0f%% full" % (pvc_name, frac * 100.0),
                "WAL/chunks are not draining. Check failed flushes, S3 "
                "errors and chunk_idle/max_chunk_age, and whether the "
                "ingester is actually shipping. Growing the PVC is cheap "
                "insurance but does not fix a broken flush path.",
                evidence={"pvc": pvc_name, "used_frac": frac}))

    chunk_p50 = _num(s["chunk_size_p50"])
    if chunk_p50 and chunk_p50 < t["chunk_p50_warn_bytes"]:
        findings.append(_finding(
            "WARN", "loki",
            "Median flushed chunk is only %s" % _fmt_count(chunk_p50),
            "Small chunks flushing mostly on idle/max_age (flush reasons: "
            "%s) mean too many LOW-VOLUME streams -- i.e. stream-label "
            "cardinality, not log volume. Tiny chunks bloat the index and "
            "object-store PUT/LIST cost. Keep stream labels to a small "
            "low-cardinality set (service, namespace, level) and move "
            "id-like labels to structured metadata." % (
                s["flushed_by_reason_per_h"] or "unknown"),
            evidence={"chunk_size_p50_bytes": round(chunk_p50),
                      "chunk_utilization_p50": s["chunk_utilization_p50"],
                      "flushed_by_reason_per_h":
                          s["flushed_by_reason_per_h"]},
            config=[_loki_metadata_snippet()],
            keeps_performance=True, keeps_intact=False, needs_review=True))

    # Stream-label cardinality via the Loki series API.
    if loki is not None and loki.available:
        sel = dcfg.get("loki_series_match", '{namespace=~".+"}')
        end = int(time.time())
        start = end - 3600
        data = loki.json("/loki/api/v1/series", {
            "match[]": sel, "start": str(start * 10 ** 9),
            "end": str(end * 10 ** 9)})
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            streams = data["data"]
            s["streams_last_1h"] = len(streams)
            values: Dict[str, set] = {}
            for st in streams:
                if not isinstance(st, dict):
                    continue
                for k, v in st.items():
                    values.setdefault(k, set()).add(v)
            counts = dict(sorted(((k, len(v)) for k, v in values.items()),
                                 key=lambda kv: -kv[1]))
            s["stream_label_values"] = counts
            for label, n in counts.items():
                if n > t["stream_label_values_warn"]:
                    findings.append(_finding(
                        "WARN", "loki",
                        "Loki stream label %r has %d distinct values"
                        % (label, n),
                        "High-cardinality index labels (pod, uid, request "
                        "ids) across %d streams make tiny chunks and a huge "
                        "index. Move %r to structured metadata (still "
                        "queryable) and keep the stream-label set small; "
                        "rewrite selectors that filtered on it."
                        % (len(streams), label),
                        evidence={"label": label, "distinct_values": n,
                                  "streams": len(streams)},
                        config=[_loki_metadata_snippet()],
                        keeps_performance=True, keeps_intact=False,
                        needs_review=True))
        elif data is not None:
            s["series_api_note"] = (
                "Loki series API returned nothing for %s; set "
                "cfg.deepdive.loki_series_match to a selector matching your "
                "index labels" % sel)
    return s, findings


def section_tempo_otel(prom: PromClient, cfg: Dict[str, Any],
                       t: Dict[str, float]) -> Tuple[
                       Dict[str, Any], List[Dict[str, Any]]]:
    """Tempo spans + metrics-generator series; OTel exporter failures/queue."""
    dcfg = cfg.get("deepdive") or {}
    ns = (dcfg.get("namespaces") or DEFAULT_NS).get("tempo", "tempo")
    s: Dict[str, Any] = {}
    findings: List[Dict[str, Any]] = []

    s["spans_s"] = prom.scalar(
        "sum(rate(tempo_distributor_spans_received_total[5m]))")
    s["metrics_generator_active_series"] = prom.scalar(
        "sum(tempo_metrics_generator_registry_active_series)")
    s["ingester_rss_gib"] = {k: _gib(v) for k, v in prom.by(
        "max by (pod) (container_memory_working_set_bytes"
        '{namespace="%s", container="ingester"})' % ns, "pod").items()}

    spans = s["spans_s"]
    if spans is not None and _num(spans) < t["tempo_unused_spans_s"]:
        findings.append(_finding(
            "INFO", "tempo",
            "Tempo receives only %s spans/s -- effectively unused"
            % _fmt_count(spans),
            "Its cost is what its pods pin (nodes via anti-affinity, the "
            "ingester PVCs), not throughput -- an adoption gap, not a "
            "platform one. Pre-set metrics_generator max_active_series "
            "BEFORE real traces arrive so a first burst cannot flood Mimir "
            "with span-metric series.",
            evidence={"spans_s": round(_num(spans), 3),
                      "metrics_generator_active_series":
                          s["metrics_generator_active_series"]},
            keeps_performance=True))

    send_failed = prom.by(
        "sum by (exporter) (increase("
        "otelcol_exporter_send_failed_spans_total[1h])) > 0", "exporter")
    send_failed.update(prom.by(
        "sum by (exporter) (increase("
        "otelcol_exporter_send_failed_metric_points_total[1h])) > 0",
        "exporter"))
    s["exporter_send_failed_per_h"] = {k: round(_num(v))
                                       for k, v in send_failed.items()}
    queue = prom.by(
        "max by (exporter) (otelcol_exporter_queue_size / "
        "otelcol_exporter_queue_capacity)", "exporter")
    s["exporter_queue_util"] = {k: round(_num(v), 3)
                                for k, v in queue.items()}

    if send_failed:
        findings.append(_finding(
            "FAIL", "tempo", "OTel collector is failing to export",
            "Exporters are dropping data (%s). Check retry_on_failure and "
            "sending_queue settings and the backend's 4xx/5xx; enlarge the "
            "queue rather than shrinking it." % ", ".join(
                "%s=%s/h" % (k, _fmt_count(v))
                for k, v in send_failed.items()),
            evidence={"send_failed_per_h": s["exporter_send_failed_per_h"]},
            config=[_otel_queue_snippet()],
            keeps_performance=True, keeps_durability=True,
            keeps_availability=True))
    for exporter, util in queue.items():
        if _num(util) > t["queue_util_warn"]:
            findings.append(_finding(
                "WARN", "tempo",
                "OTel exporter %r queue is %.0f%% full" % (
                    exporter, _num(util) * 100.0),
                "The exporter is about to drop data -- the backend is slow "
                "or the batch is too small. Enlarge the queue / batch; do "
                "not reduce it for memory.",
                evidence={"exporter": exporter, "queue_util": round(
                    _num(util), 3)},
                config=[_otel_queue_snippet()]))
    return s, findings


# ---------------------------------------------------------------------------
# Config-snippet builders (generic, commented, paste-ready)
# ---------------------------------------------------------------------------

def _relabel_drop_snippet(placeholder: str, action: str) -> Dict[str, str]:
    if action == "drop":
        body = (
            "      - source_labels: [__name__]\n"
            "        regex: \"%s\"       # the UNUSED metric name\n"
            "        action: drop" % placeholder)
        note = ("Drops the whole metric. Confirm NO dashboard/rule uses it "
                "(mimirtool analyze) first -- dropping a used metric breaks "
                "panels.")
    else:
        body = (
            "      - regex: \"%s\"       # the high-churn label name\n"
            "        action: labeldrop" % placeholder)
        note = ("Drops one label. Confirm it is not used in "
                "queries/group-by first; dropping a queried label breaks "
                "panels.")
    snippet = (
        "# Drop at the SPOKE remote_write, NOT at scrape -- keep full-\n"
        "# resolution local debug data. Reduces series, blocks, S3 and\n"
        "# wire bytes proportionally, without touching scrape intervals,\n"
        "# RF or retention.\n"
        "remote_write:\n"
        "  - url: https://<mimir-gateway>/api/v1/push\n"
        "    write_relabel_configs:\n" + body + "\n")
    return {"target": "spoke Prometheus/agent remote_write",
            "language": "yaml", "snippet": snippet, "note": note}


def _ha_tracker_snippet(cluster_label: str,
                        replica_label: str) -> Dict[str, str]:
    snippet = (
        "# Mimir HA tracker: dedupe two Prometheus HA replicas so each\n"
        "# series is ingested ONCE. Set matching external labels on the\n"
        "# replicas, then enable the tracker. Keeps RF, retention and\n"
        "# availability fully intact -- it only removes the DUPLICATE.\n"
        "limits:\n"
        "  accept_ha_samples: true\n"
        "  ha_cluster_label: %s\n"
        "  ha_replica_label: %s\n"
        "distributor:\n"
        "  ha_tracker:\n"
        "    enable_ha_tracker: true\n"
        "    kvstore:\n"
        "      store: memberlist\n" % (cluster_label, replica_label))
    return {"target": "mimir config (limits + distributor.ha_tracker)",
            "language": "yaml", "snippet": snippet,
            "note": "Alternative: run a single Prometheus replica per "
                    "source. Either way keeps durability/availability."}


def _loki_metadata_snippet() -> Dict[str, str]:
    snippet = (
        "# Keep the Loki STREAM-label set tiny and low-cardinality; move\n"
        "# id-like / per-pod labels to structured metadata so they stay\n"
        "# queryable WITHOUT exploding streams. Rewrite selectors that\n"
        "# filtered on a moved label. (Alloy/OTel pipeline example.)\n"
        "stage.structured_metadata {\n"
        "  values = { \"<high_card_label>\" = \"\" }   # was a stream label\n"
        "}\n"
        "stage.labels {\n"
        "  values = { service_name = \"\", namespace = \"\", level = \"\" }\n"
        "}\n")
    return {"target": "log collector (Alloy / OTel) pipeline",
            "language": "hcl", "snippet": snippet,
            "note": "Moving a label to structured metadata keeps it "
                    "searchable; selectors using it as a stream label must "
                    "be rewritten. Does not reduce retention."}


def _otel_queue_snippet() -> Dict[str, str]:
    snippet = (
        "# OTel Collector exporter backpressure: enlarge the queue and\n"
        "# enable retry so transient backend 5xx do not DROP data. This\n"
        "# increases durability; it does not trade it away.\n"
        "exporters:\n"
        "  otlp:\n"
        "    retry_on_failure:\n"
        "      enabled: true\n"
        "    sending_queue:\n"
        "      enabled: true\n"
        "      queue_size: 10000        # size to your burst, not down\n")
    return {"target": "otel-collector exporters", "language": "yaml",
            "snippet": snippet,
            "note": "Sizing the queue UP protects against drops; never "
                    "shrink it for memory without a loud caveat."}


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------

def _as_client(spec: Any, cfg_headers: Dict[str, str],
               timeout: float) -> Optional[PromClient]:
    """Turn a URL string / pre-built client into a PromClient (or None).

    A pre-built object (anything exposing ``.vector``) is returned as-is so
    tests can inject a canned client.
    """
    if spec is None:
        return None
    if hasattr(spec, "vector"):
        return spec
    if isinstance(spec, str) and spec.strip():
        return PromClient(spec.strip(), headers=cfg_headers, timeout=timeout)
    return None


def _proxy_client(grafana: Any, ds_type: str,
                  timeout: float) -> Optional[PromClient]:
    """Best-effort PromClient over the Grafana datasource proxy."""
    if grafana is None:
        return None
    try:
        dss = grafana.datasources()
    except Exception:  # noqa: BLE001 - grafana optional
        return None
    uid = ""
    matches = [d for d in dss if d.get("type") == ds_type]
    for d in matches:
        if d.get("isDefault"):
            uid = d.get("uid", "")
            break
    if not uid and matches:
        uid = matches[0].get("uid", "")
    if not uid:
        return None
    return PromClient(None, proxy=(grafana, uid), timeout=timeout)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _rank(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deterministic order: severity, then $ saving desc, then title."""
    def key(f: Dict[str, Any]) -> Tuple[int, float, str]:
        sev = _SEV_ORDER.get(f.get("severity"), 3)
        usd = _num((f.get("est_savings") or {}).get("monthly_usd"))
        return (sev, -usd, f.get("title", ""))
    return sorted(findings, key=key)


def analyze(prom: Any = None, mimir: Any = None, loki: Any = None,
            grafana: Any = None, cfg: Optional[Dict[str, Any]] = None,
            log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Run the metric-driven deep-dive and return schema
    "nr2grafana/deepdive/v1".

    ``prom`` / ``mimir`` / ``loki`` are base URLs (or pre-built PromClient
    objects, for testing). Component self-metrics are read from ``prom`` if
    given, else ``mimir``, else the Grafana Prometheus datasource proxy; the
    Mimir cardinality API is read from ``mimir`` (or the proxy); Loki stream
    cardinality from ``loki`` (or the proxy). Every section degrades
    independently -- a missing endpoint drops only its findings. Never
    raises.
    """
    cfg = cfg or {}
    dcfg = cfg.get("deepdive") or {}
    emit = log or (lambda m: None)
    t = dict(DEFAULT_T)
    if isinstance(dcfg.get("thresholds"), dict):
        t.update({k: v for k, v in dcfg["thresholds"].items()
                  if isinstance(v, (int, float))})
    pricing = _cfg_pricing(cfg)
    timeout = _num(dcfg.get("timeout"), 30.0) or 30.0
    headers: Dict[str, str] = {}
    org = dcfg.get("org_id", "anonymous")
    if org:
        headers["X-Scope-OrgID"] = str(org)

    prom_c = _as_client(prom, headers, timeout)
    mimir_c = _as_client(mimir, headers, timeout)
    loki_c = _as_client(loki, headers, timeout)

    # Self-metrics come from Prometheus if given, else Mimir, else proxy.
    self_c = prom_c or mimir_c or _proxy_client(grafana, "prometheus",
                                                timeout)
    card_c = mimir_c or _proxy_client(grafana, "prometheus", timeout)
    stream_c = loki_c or _proxy_client(grafana, "loki", timeout)

    findings: List[Dict[str, Any]] = []
    sections: Dict[str, Any] = {}

    if self_c is None:
        note = ("no Prometheus/Mimir endpoint reachable: pass a prom= or "
                "mimir= URL, or a Grafana client with a Prometheus "
                "datasource. The deep-dive needs component self-metrics.")
        emit(note)
        return {
            "schema": SCHEMA, "generated_by": GENERATED_BY,
            "generated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "available": False, "note": note, "sections": {},
            "findings": [], "query_errors": [],
            "summary": {"findings": 0, "fail": 0, "warn": 0, "info": 0,
                        "total_est_monthly_usd": 0.0}}

    steps = [
        ("capacity", lambda: section_capacity(self_c, cfg, t, pricing)),
    ]
    cap_section: Dict[str, Any] = {}
    for name, fn in steps:
        try:
            emit("deep-dive: analyzing %s ..." % name)
            cap_section, fs = fn()
            sections[name] = cap_section
            findings.extend(fs)
        except Exception as exc:  # noqa: BLE001 - one section must not sink
            emit("deep-dive: %s section error: %s" % (name, exc))
            sections[name] = {"error": str(exc)[:200]}

    later = [
        ("churn", lambda: section_churn(self_c, cap_section, t, pricing)),
        ("cardinality", lambda: section_cardinality(
            self_c, card_c, cap_section, cfg, t, pricing)),
        ("network", lambda: section_network(self_c, cfg, t)),
        ("loki", lambda: section_loki(self_c, stream_c, cfg, t)),
        ("tempo", lambda: section_tempo_otel(self_c, cfg, t)),
    ]
    for name, fn in later:
        try:
            emit("deep-dive: analyzing %s ..." % name)
            sec, fs = fn()
            sections[name] = sec
            findings.extend(fs)
        except Exception as exc:  # noqa: BLE001
            emit("deep-dive: %s section error: %s" % (name, exc))
            sections[name] = {"error": str(exc)[:200]}

    findings = _rank(findings)
    counts = {"FAIL": 0, "WARN": 0, "INFO": 0}
    total_usd = 0.0
    for f in findings:
        counts[f.get("severity", "INFO")] = counts.get(
            f.get("severity", "INFO"), 0) + 1
        total_usd += _num((f.get("est_savings") or {}).get("monthly_usd"))

    query_errors: List[str] = []
    for client in (self_c, card_c, stream_c):
        if client is not None and getattr(client, "errors", None):
            for path, msg in client.errors[:40]:
                query_errors.append("%s: %s" % (path, msg))

    emit("deep-dive: %d finding(s) (%d FAIL / %d WARN / %d INFO)" % (
        len(findings), counts["FAIL"], counts["WARN"], counts["INFO"]))
    return {
        "schema": SCHEMA,
        "generated_by": GENERATED_BY,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "available": True,
        "sections": sections,
        "findings": findings,
        "summary": {
            "findings": len(findings),
            "fail": counts["FAIL"], "warn": counts["WARN"],
            "info": counts["INFO"],
            "total_est_monthly_usd": round(total_usd, 2)},
        "assumptions": {
            "replication_factor": int(_num(
                dcfg.get("replication_factor"), 3)) or 3,
            "egress_usd_per_gb": {
                **DEFAULT_EGRESS_USD_PER_GB,
                **(dcfg.get("egress_usd_per_gb")
                   if isinstance(dcfg.get("egress_usd_per_gb"), dict)
                   else {})},
            "note": ("All $ figures are ESTIMATES from the stated per-GB / "
                     "per-1k-series pricing assumptions, not a bill.")},
        "query_errors": query_errors,
        "disclaimer": ("Metric-driven estimate. Every cost recommendation "
                       "states its risk and defaults to the safe option; "
                       "series drops are keeps_intact:false until you prove "
                       "the dimension unused."),
    }
