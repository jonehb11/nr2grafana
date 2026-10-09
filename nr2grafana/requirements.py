"""Datasource requirement analyzer.

Given a converted Grafana dashboard, its widget report, and (optionally)
the parsed New Relic dashboard, work out exactly what a Grafana instance
needs before the dashboard can be imported and show data:

- which datasources (and non-core plugins) must exist, and which panels
  use them;
- which New Relic data *domains* the original dashboard drew from
  (AWS/GCP/Azure integrations, infra samples, K8s, logs, traces, APM,
  RUM, NR account data) and what Grafana datasource or ingestion
  pipeline produces equivalent data;
- per translated panel, the concrete metric names / labels / Loki stream
  selectors the queries expect to find;
- widgets that are New Relic-native and have no LGTM equivalent as-is.

Output schema: "nr2grafana/requirements/v1" (see ARCHITECTURE-1.1.md).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from . import __version__
from .model import NRDashboard
from .nrql.parser import NrqlParseError, parse_nrql

SCHEMA = "nr2grafana/requirements/v1"
GENERATED_BY = "nr2grafana %s" % __version__

# Datasource family emitted for CloudWatch targets (translate/cloudwatch
# + grafana/builder) and its default ``${var}`` reference.
CLOUDWATCH_FAMILY = "cloudwatch"
_CLOUDWATCH_UID_REF = "${cloudwatch_datasource}"

# Default plugin type per family when the config has no entry.
_FAMILY_PLUGIN = {
    "prometheus": "prometheus", "loki": "loki", "tempo": "tempo",
    "cloudwatch": "cloudwatch",
    "newrelic": "nrgrafanaplugin-newrelic-datasource",
}


# ---------------------------------------------------------------------------
# Domain knowledge: NR event types / metric prefixes -> Grafana equivalents
# ---------------------------------------------------------------------------
# Entries are matched IN ORDER (most specific first). Each entry:
#   domain          stable identifier used in the output
#   event_types     exact NR event types (lowercased)
#   event_prefixes  NR event-type prefixes (lowercased)
#   metric_prefixes prefixes of dotted metric/attribute names in NRQL
#   options         Grafana equivalents:
#     {"kind": "datasource", "plugin_id": ..., "core": bool, "note": ...}
#     {"kind": "pipeline", "note": "<exporter/collector> -> <backend>; ..."}
# Extend/override via cfg["domain_map"] (a list of entries in the same
# shape; user entries are matched before the built-ins).

DOMAIN_MAP: List[Dict[str, Any]] = [
    {"domain": "aws-lambda",
     "event_prefixes": ["awslambda"],
     "metric_prefixes": ["aws.lambda."],
     "options": [
         {"kind": "datasource", "plugin_id": "cloudwatch", "core": True,
          "note": "CloudWatch datasource (AWS/Lambda namespace); needs "
                  "AWS credentials or an IAM role"},
         {"kind": "pipeline",
          "note": "YACE / prometheus cloudwatch-exporter or OTel "
                  "collector awscloudwatch receiver scraping AWS/Lambda "
                  "-> Mimir; metrics then appear as aws_lambda_* (e.g. "
                  "aws_lambda_duration_average, aws_lambda_errors_sum)"},
         {"kind": "pipeline",
          "note": "Lambda logs: CloudWatch Logs via the cloudwatch "
                  "datasource (Logs mode), or ship log groups with "
                  "lambda-promtail -> Loki"},
     ]},
    {"domain": "aws",
     "event_prefixes": ["aws"],
     "event_types": ["computesample", "queuesample", "datastoresample",
                     "loadbalancersample", "blockdevicesample"],
     "metric_prefixes": ["aws.", "provider."],
     "options": [
         {"kind": "datasource", "plugin_id": "cloudwatch", "core": True,
          "note": "CloudWatch datasource; needs AWS credentials or an "
                  "IAM role"},
         {"kind": "pipeline",
          "note": "YACE / cloudwatch-exporter or OTel collector "
                  "awscloudwatch receiver -> Mimir; metrics then appear "
                  "as aws_<namespace>_* (e.g. aws_sqs_*, aws_rds_*)"},
     ]},
    {"domain": "gcp",
     "event_prefixes": ["gcp"],
     "metric_prefixes": ["gcp."],
     "options": [
         {"kind": "datasource", "plugin_id": "stackdriver", "core": True,
          "note": "Google Cloud Monitoring (stackdriver) datasource; "
                  "needs a GCP service account key or workload identity"},
         {"kind": "pipeline",
          "note": "stackdriver-exporter or OTel collector "
                  "googlecloudmonitoring receiver -> Mimir; metrics then "
                  "appear as stackdriver_<resource>_*"},
     ]},
    {"domain": "azure",
     "event_prefixes": ["azure"],
     "metric_prefixes": ["azure."],
     "options": [
         {"kind": "datasource",
          "plugin_id": "grafana-azure-monitor-datasource", "core": True,
          "note": "Azure Monitor datasource; needs an app registration "
                  "(client id/secret) or managed identity"},
         {"kind": "pipeline",
          "note": "azure-metrics-exporter or OTel collector azuremonitor "
                  "receiver -> Mimir; metrics then appear as azure_*"},
     ]},
    {"domain": "k8s",
     "event_prefixes": ["k8s"],
     "metric_prefixes": ["k8s."],
     "options": [
         {"kind": "pipeline",
          "note": "kube-state-metrics + cAdvisor/kubelet (or OTel "
                  "k8scluster/kubeletstats receivers) -> Mimir; metrics "
                  "appear as kube_*, container_*, node_*"},
         {"kind": "datasource", "plugin_id": "prometheus", "core": True,
          "note": "queried through the Prometheus/Mimir datasource once "
                  "the cluster ships metrics"},
     ]},
    {"domain": "infra-host",
     "event_types": ["systemsample", "processsample", "networksample",
                     "storagesample", "containersample"],
     "options": [
         {"kind": "pipeline",
          "note": "node_exporter -> Mimir (node_cpu_seconds_total, "
                  "node_memory_*, node_filesystem_*, node_network_*) or "
                  "OTel collector hostmetrics receiver (system_cpu_*, "
                  "system_memory_*)"},
         {"kind": "datasource", "plugin_id": "prometheus", "core": True,
          "note": "queried through the Prometheus/Mimir datasource once "
                  "hosts ship metrics"},
     ]},
    {"domain": "logs",
     "event_types": ["log", "logextendedrecord"],
     "options": [
         {"kind": "datasource", "plugin_id": "loki", "core": True,
          "note": "Loki datasource pointed at your Loki/LGTM logs "
                  "backend"},
         {"kind": "pipeline",
          "note": "promtail / Grafana Alloy / OTel collector filelog "
                  "receiver -> Loki; for AWS Lambda sources use "
                  "CloudWatch Logs or lambda-promtail -> Loki"},
     ]},
    {"domain": "traces",
     "event_types": ["span", "distributedtrace",
                     "distributedtracesummary"],
     "options": [
         {"kind": "datasource", "plugin_id": "tempo", "core": True,
          "note": "Tempo datasource pointed at your traces backend"},
         {"kind": "pipeline",
          "note": "OTel traces -> Tempo; aggregated span queries need "
                  "span metrics in Mimir via Tempo metrics-generator "
                  "(traces_spanmetrics_*) or the OTel spanmetrics "
                  "connector (traces_span_metrics_*)"},
     ]},
    {"domain": "apm",
     "event_types": ["transaction", "transactionerror"],
     "options": [
         {"kind": "pipeline",
          "note": "OTel APM instrumentation -> Mimir; HTTP metrics "
                  "appear as http_server_request_duration_seconds_* and "
                  "span metrics as traces_span_metrics_* / "
                  "traces_spanmetrics_*"},
         {"kind": "datasource", "plugin_id": "prometheus", "core": True,
          "note": "queried through the Prometheus/Mimir datasource once "
                  "services are instrumented"},
     ]},
    {"domain": "synthetics",
     "event_types": ["syntheticcheck", "syntheticrequest"],
     "options": [
         {"kind": "pipeline",
          "note": "blackbox_exporter probes -> Mimir (probe_success, "
                  "probe_duration_seconds, probe_http_status_code)"},
         {"kind": "pipeline",
          "note": "Grafana Synthetic Monitoring (Grafana Cloud) provides "
                  "hosted checks with ready-made dashboards"},
     ]},
    {"domain": "browser-rum",
     "event_prefixes": ["pageview", "pageaction", "browser",
                        "javascripterror", "ajaxrequest"],
     "options": [
         {"kind": "pipeline",
          "note": "Grafana Faro Web SDK -> Faro collector (Alloy) -> "
                  "Loki/Mimir; RUM events, web vitals and JS errors"},
     ]},
    {"domain": "mobile",
     "event_prefixes": ["mobile"],
     "options": [
         {"kind": "pipeline",
          "note": "Grafana Faro mobile SDKs (or OTel mobile "
                  "instrumentation) -> Loki/Mimir"},
     ]},
    {"domain": "nr-account",
     "event_types": ["nrconsumption", "nrusage", "nrauditevent",
                     "nrdailyusage", "nrmtdconsumption",
                     "nrintegrationerror"],
     "metric_prefixes": ["newrelic."],
     "options": [
         {"kind": "datasource",
          "plugin_id": "nrgrafanaplugin-newrelic-datasource",
          "core": False,
          "note": "New Relic account/usage/audit data exists only inside "
                  "New Relic; keep these panels on the New Relic "
                  "datasource plugin (NRQL passthrough)"},
     ]},
]


# Plugin ids that ship with Grafana (no `grafana-cli plugins install`).
_CORE_PLUGINS = {
    "prometheus", "loki", "tempo", "cloudwatch", "stackdriver",
    "grafana-azure-monitor-datasource", "elasticsearch", "graphite",
    "influxdb", "jaeger", "zipkin", "mysql", "postgres", "mssql",
    "opentsdb", "testdata",
}

_FAMILY_PURPOSE = {
    "prometheus": "metrics (Mimir/Prometheus)",
    "loki": "logs (Loki)",
    "tempo": "traces (Tempo)",
    "cloudwatch": "AWS metrics (CloudWatch)",
    "newrelic": "NRQL passthrough (New Relic datasource plugin)",
}

# NR visualization id -> closest Grafana panel type (for `equivalent`
# suggestions on untranslated widgets).
_VIZ_EQUIV = {
    "viz.line": "timeseries", "viz.area": "timeseries",
    "viz.stacked-bar": "timeseries", "viz.bar": "bargauge",
    "viz.billboard": "stat", "viz.bullet": "gauge",
    "viz.pie": "piechart", "viz.table": "table",
    "viz.heatmap": "heatmap", "viz.histogram": "histogram",
    "viz.json": "table", "viz.event-feed": "table",
    "logger.log-table-widget": "logs", "viz.funnel": "bar gauge",
    "topology.service-map": "node graph",
}


# ---------------------------------------------------------------------------
# NRQL evidence extraction
# ---------------------------------------------------------------------------

_FROM_RE = re.compile(
    r"\bFROM\s+(`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)"
    r"((?:\s*,\s*(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_]*))*)", re.I)

# Backticked names and dotted identifiers (metric names, prefixed
# integration attributes like aws.requestId -- both are domain
# evidence).
_DOTTED_RE = re.compile(
    r"`([^`]+)`|(?<![\w.$])([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)")


def _nrql_event_types(nrql: str) -> List[str]:
    """Event types in a FROM clause, original casing preserved."""
    try:
        return list(parse_nrql(nrql).from_)
    except NrqlParseError:
        pass
    m = _FROM_RE.search(nrql or "")
    if not m:
        return []
    names = [m.group(1)] + re.split(r"\s*,\s*", m.group(2).strip(", \t"))
    return [n.strip("`") for n in names if n and n.strip("`")]


def _nrql_dotted_names(nrql: str) -> List[str]:
    out: List[str] = []
    for m in _DOTTED_RE.finditer(nrql or ""):
        name = m.group(1) or m.group(2)
        if name and name not in out:
            out.append(name)
    return out


def _domain_table(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Built-in DOMAIN_MAP with user entries (cfg['domain_map']) matched
    first, so site-specific rules win."""
    user = cfg.get("domain_map") or []
    table = [e for e in user if isinstance(e, dict) and e.get("domain")]
    return table + DOMAIN_MAP


def _match_event(entry: Dict[str, Any], event_lower: str) -> bool:
    if event_lower in [e.lower() for e in entry.get("event_types") or []]:
        return True
    for prefix in entry.get("event_prefixes") or []:
        if event_lower.startswith(prefix.lower()):
            return True
    return False


def _match_metric(entry: Dict[str, Any], name_lower: str) -> bool:
    for prefix in entry.get("metric_prefixes") or []:
        if name_lower.startswith(prefix.lower()):
            return True
    return False


def _domains_for_nrql(nrql: str,
                      table: List[Dict[str, Any]]) \
        -> List[Tuple[Dict[str, Any], str]]:
    """[(domain entry, evidence string)] for one NRQL query. Each piece
    of evidence maps to the FIRST matching table entry."""
    found: List[Tuple[Dict[str, Any], str]] = []
    for ev in _nrql_event_types(nrql):
        low = ev.lower()
        for entry in table:
            if _match_event(entry, low):
                found.append((entry, "FROM %s" % ev))
                break
    for name in _nrql_dotted_names(nrql):
        low = name.lower()
        for entry in table:
            if _match_metric(entry, low):
                found.append((entry, "metric %s" % name))
                break
    return found


# ---------------------------------------------------------------------------
# PromQL / LogQL data expectations
# ---------------------------------------------------------------------------

# Words that survive selector/grouping stripping but are never metrics.
_PROM_KEYWORDS = {
    "by", "without", "on", "ignoring", "group_left", "group_right",
    "bool", "offset", "and", "or", "unless", "atan2", "inf", "nan",
}

_GROUP_RE = re.compile(
    r"\b(by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)",
    re.I)
_LABEL_MATCH_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=~|!~|!=|=)")
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`')
_RANGE_RE = re.compile(r"\[[^\]]*\]")
_IDENT_RE = re.compile(r"(?<![0-9a-zA-Z_.:$])[a-zA-Z_:][a-zA-Z0-9_:]*")


def _split_selectors(expr: str) -> Tuple[List[str], str]:
    """(selector texts incl. braces, expr with selectors removed).

    A character scan rather than a regex: matcher values may contain
    braces themselves (Grafana `${var:regex}` interpolations), so
    quoted strings must be honored while looking for the closing brace.
    """
    selectors: List[str] = []
    out: List[str] = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch in ('"', "`"):
            j = i + 1
            while j < n and expr[j] != ch:
                j += 2 if ch == '"' and expr[j] == "\\" else 1
            out.append(expr[i:j + 1])
            i = j + 1
            continue
        if ch == "{":
            j, depth, quote = i + 1, 1, ""
            while j < n and depth:
                c = expr[j]
                if quote:
                    if quote == '"' and c == "\\":
                        j += 2
                        continue
                    if c == quote:
                        quote = ""
                elif c in ('"', "`"):
                    quote = c
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                j += 1
            selectors.append(expr[i:j])
            out.append(" ")
            i = j
            continue
        out.append(ch)
        i += 1
    return selectors, "".join(out)


def _promql_needs(expr: str) -> Tuple[List[str], List[str]]:
    """(metric names, label names) an expression expects to exist."""
    labels: Set[str] = set()

    def grab_group(m: "re.Match[str]") -> str:
        for name in m.group(2).split(","):
            name = name.strip()
            if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
                labels.add(name)
        return " "

    cleaned = _GROUP_RE.sub(grab_group, expr or "")
    selectors, cleaned = _split_selectors(cleaned)
    for sel in selectors:
        labels.update(
            _LABEL_MATCH_RE.findall(_STRING_RE.sub('""', sel[1:-1])))
    cleaned = _STRING_RE.sub('""', cleaned)
    cleaned = _RANGE_RE.sub(" ", cleaned)

    metrics: List[str] = []
    for m in _IDENT_RE.finditer(cleaned):
        tok = m.group(0)
        if cleaned[m.end():].lstrip().startswith("("):
            continue  # function call
        if tok.lower() in _PROM_KEYWORDS or tok in metrics:
            continue
        metrics.append(tok)
    return metrics, sorted(labels)


def _logql_needs(expr: str) -> Tuple[str, List[str]]:
    """(stream selector, label names) for a LogQL expression."""
    labels: Set[str] = set()
    for grp in _GROUP_RE.finditer(expr or ""):
        for name in grp.group(2).split(","):
            name = name.strip()
            if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
                labels.add(name)
    selectors, _ = _split_selectors(expr or "")
    selector = selectors[0] if selectors else ""
    if selector:
        labels.update(
            _LABEL_MATCH_RE.findall(_STRING_RE.sub('""', selector[1:-1])))
    return selector, sorted(labels)


# ---------------------------------------------------------------------------
# Dashboard walking
# ---------------------------------------------------------------------------

def _iter_panels(dash: Dict[str, Any]):
    for panel in dash.get("panels") or []:
        yield panel
        for child in panel.get("panels") or []:
            yield child


def _family_map(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Datasource plugin type -> config family name."""
    out = {"prometheus": "prometheus", "loki": "loki", "tempo": "tempo",
           "cloudwatch": "cloudwatch",
           "nrgrafanaplugin-newrelic-datasource": "newrelic"}
    for family, spec in (cfg.get("datasources") or {}).items():
        if isinstance(spec, dict) and spec.get("type"):
            out[spec["type"]] = family
    return out


def _family_ref(cfg: Dict[str, Any], family: str) -> Tuple[str, str]:
    """(plugin type, uid ref) the builder would emit for ``family``."""
    spec = (cfg.get("datasources") or {}).get(family) or {}
    ds_type = spec.get("type") or _FAMILY_PLUGIN.get(family, family)
    uid = spec.get("uid") or ""
    if not uid:
        uid = (_CLOUDWATCH_UID_REF if family == CLOUDWATCH_FAMILY
               else "${%s_datasource}" % family)
    return ds_type, uid


def _report_wants_cloudwatch(entry: Dict[str, Any]) -> bool:
    """True when a widget-report entry needs the CloudWatch family:
    the builder flagged it (``cloudwatch: true``), it reports the family
    as missing, or its closest equivalent is a CloudWatch target."""
    if entry.get("cloudwatch") is True:
        return True
    if entry.get("missing_datasource") == CLOUDWATCH_FAMILY:
        return True
    ce = entry.get("closest_equivalent")
    if isinstance(ce, dict) and (ce.get("datasource") == CLOUDWATCH_FAMILY
                                 or ce.get("cw_target")):
        return True
    for q in entry.get("queries") or []:
        if isinstance(q, dict) and q.get("datasource") == CLOUDWATCH_FAMILY:
            return True
    return False


def _collect_datasources(dash: Dict[str, Any],
                         cfg: Dict[str, Any],
                         widget_report: Optional[List[Dict[str, Any]]] = None
                         ) -> List[Dict[str, Any]]:
    fam_of = _family_map(cfg)
    found: Dict[str, Dict[str, Any]] = {}

    def add(family: str, ds_type: str, uid_ref: str,
            pid: Any) -> None:
        entry = found.setdefault(family, {
            "family": family,
            "plugin_id": ds_type,
            "core": ds_type in _CORE_PLUGINS,
            "uid_ref": uid_ref,
            "purpose": _FAMILY_PURPOSE.get(family, ds_type),
            "panel_ids": [],
            "required": True,
        })
        if pid is not None and pid not in entry["panel_ids"]:
            entry["panel_ids"].append(pid)

    for panel in _iter_panels(dash):
        for target in panel.get("targets") or []:
            ds = target.get("datasource") or {}
            ds_type = ds.get("type", "")
            if not ds_type or ds_type == "datasource":
                continue
            family = fam_of.get(ds_type, ds_type)
            add(family, ds_type, ds.get("uid", ""), panel.get("id"))
    # CloudWatch is REQUIRED as soon as the widget report says a panel
    # needs it, even when that panel is a [MANUAL] placeholder (text
    # panel, no target) whose closest equivalent is a CloudWatch query.
    for entry in widget_report or []:
        if _report_wants_cloudwatch(entry):
            ds_type, uid_ref = _family_ref(cfg, CLOUDWATCH_FAMILY)
            add(CLOUDWATCH_FAMILY, ds_type, uid_ref, entry.get("panel_id"))
    for entry in found.values():
        entry["panel_ids"].sort(key=lambda p: (isinstance(p, str), p))
    return sorted(found.values(), key=lambda e: e["family"])


def _collect_plugins(datasources: List[Dict[str, Any]]) \
        -> List[Dict[str, Any]]:
    plugins = []
    for ds in datasources:
        if ds["core"]:
            continue
        plugins.append({
            "id": ds["plugin_id"],
            "reason": "%d panel(s) use the %s datasource (%s)"
                      % (len(ds["panel_ids"]), ds["family"],
                         ds["purpose"]),
            "grafana_cli":
                "grafana-cli plugins install %s" % ds["plugin_id"],
        })
    return plugins


def _collect_domains(widget_report: List[Dict[str, Any]],
                     nr: Optional[NRDashboard],
                     cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    table = _domain_table(cfg)
    out: Dict[str, Dict[str, Any]] = {}

    def add(entry: Dict[str, Any], evidence: str,
            panel_id: Optional[int]) -> None:
        dom = out.setdefault(entry["domain"], {
            "domain": entry["domain"], "evidence": [], "panel_ids": [],
            "options": [dict(o) for o in entry.get("options") or []],
        })
        if evidence not in dom["evidence"]:
            dom["evidence"].append(evidence)
        if panel_id is not None and panel_id not in dom["panel_ids"]:
            dom["panel_ids"].append(panel_id)

    for report in widget_report or []:
        pid = report.get("panel_id")
        for nrql in report.get("nrql") or []:
            for entry, evidence in _domains_for_nrql(nrql, table):
                add(entry, evidence, pid)
    if nr is not None:
        for var in nr.variables:
            nrql = (var.nrql_query or {}).get("query", "")
            if not nrql:
                continue
            for entry, evidence in _domains_for_nrql(nrql, table):
                add(entry, "%s (variable %s)" % (evidence, var.name), None)
    for dom in out.values():
        dom["panel_ids"].sort()
    return sorted(out.values(), key=lambda d: d["domain"])


def _collect_nr_native(widget_report: List[Dict[str, Any]],
                       cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    table = _domain_table(cfg)
    out: List[Dict[str, Any]] = []
    for report in widget_report or []:
        if report.get("confidence") != "untranslatable":
            continue
        viz = report.get("visualization") or ""
        notes = report.get("notes") or []
        why = "; ".join(notes[:3]) or "query could not be translated"
        ptype = _VIZ_EQUIV.get(viz, "timeseries")
        matched = []
        for nrql in report.get("nrql") or []:
            matched.extend(_domains_for_nrql(nrql, table))
        if matched:
            note = (matched[0][0].get("options") or
                    [{"note": ""}])[0].get("note", "")
            equivalent = "%s panel + %s" % (ptype, note) if note \
                else "%s panel (see domain %r options)" \
                % (ptype, matched[0][0]["domain"])
        else:
            equivalent = ("%s panel; recreate the query against your "
                          "LGTM stack (see widget report notes)" % ptype)
        if report.get("fallback") == "nrql-passthrough":
            equivalent = ("currently NRQL passthrough via the New Relic "
                          "datasource plugin; alternative: " + equivalent)
        ce = _closest_equivalent(report)
        if ce:
            equivalent = _equivalent_text(ce) or equivalent
        out.append({
            "panel_id": report.get("panel_id"),
            "widget": viz or report.get("widget",
                                        report.get("widget_title", "")),
            "why": why,
            "equivalent": equivalent,
            "missing_datasource": report.get("missing_datasource") or None,
            "closest_equivalent": ce,
        })
    return out


def _closest_equivalent(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The builder's ``closest_equivalent`` dict for a report entry
    (``{datasource, example_query | cw_target, note}``), or None."""
    ce = entry.get("closest_equivalent")
    if isinstance(ce, dict) and ce:
        return dict(ce)
    if isinstance(ce, str) and ce.strip():
        return {"datasource": "", "note": ce.strip()}
    return None


def _equivalent_text(ce: Dict[str, Any]) -> str:
    """One-line rendering of a closest_equivalent dict."""
    parts: List[str] = []
    ds = ce.get("datasource") or ""
    query = ce.get("example_query") or ce.get("expr") or ""
    cw = ce.get("cw_target")
    if query:
        parts.append("%s: %s" % (ds, query) if ds else str(query))
    elif isinstance(cw, dict) and cw:
        desc = "%s %s" % (cw.get("namespace", ""), cw.get("metricName", ""))
        if cw.get("statistic"):
            desc += " (%s)" % cw["statistic"]
        dims = cw.get("dimensions")
        if isinstance(dims, dict) and dims:
            desc += " by " + ", ".join(sorted(dims))
        parts.append("%s target: %s" % (ds or CLOUDWATCH_FAMILY,
                                        desc.strip()))
    elif ds:
        parts.append("%s datasource" % ds)
    note = ce.get("note") or ""
    if note:
        parts.append(str(note))
    return " -- ".join(parts)


def _is_manual(entry: Dict[str, Any]) -> bool:
    return bool(entry.get("manual")) or \
        entry.get("confidence") == "untranslatable"


def _collect_manual_panels(widget_report: List[Dict[str, Any]]) \
        -> List[Dict[str, Any]]:
    """Per-panel [MANUAL] surface: every panel the builder could not
    translate (or flagged ``manual``), with WHY, the datasource family
    it is missing (if any) and the closest equivalent query/target."""
    out: List[Dict[str, Any]] = []
    for entry in widget_report or []:
        if not _is_manual(entry):
            continue
        notes = [str(n) for n in entry.get("notes") or [] if n]
        ce = _closest_equivalent(entry)
        nrql = [q for q in entry.get("nrql") or [] if q]
        out.append({
            "panel_id": entry.get("panel_id"),
            "title": entry.get("widget_title") or entry.get("widget")
            or "(untitled)",
            "page": entry.get("page") or "",
            "visualization": entry.get("visualization") or "",
            "confidence": entry.get("confidence") or "",
            "why": "; ".join(notes[:3]) or "query could not be translated",
            "nrql": nrql[0] if nrql else "",
            "missing_datasource": entry.get("missing_datasource") or None,
            "closest_equivalent": ce,
            "equivalent": _equivalent_text(ce) if ce else "",
        })
    return out


def _collect_expectations(dash: Dict[str, Any],
                          cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    fam_of = _family_map(cfg)
    out: List[Dict[str, Any]] = []
    for panel in _iter_panels(dash):
        for target in panel.get("targets") or []:
            ds_type = (target.get("datasource") or {}).get("type", "")
            family = fam_of.get(ds_type, ds_type)
            if family == "prometheus" and target.get("expr"):
                metrics, labels = _promql_needs(target["expr"])
                out.append({"panel_id": panel.get("id"),
                            "datasource": "prometheus",
                            "needs": {"metrics": metrics,
                                      "labels": labels}})
            elif family == "loki" and target.get("expr"):
                selector, labels = _logql_needs(target["expr"])
                out.append({"panel_id": panel.get("id"),
                            "datasource": "loki",
                            "needs": {"stream_selector": selector,
                                      "labels": labels}})
            elif family == "tempo":
                query = target.get("query") or target.get("expr") or ""
                out.append({"panel_id": panel.get("id"),
                            "datasource": "tempo",
                            "needs": {"traceql": query}})
            elif family == CLOUDWATCH_FAMILY:
                out.append({"panel_id": panel.get("id"),
                            "datasource": CLOUDWATCH_FAMILY,
                            "needs": _cloudwatch_needs(target)})
    return out


def _cloudwatch_needs(target: Dict[str, Any]) -> Dict[str, Any]:
    """What a CloudWatch target expects to find in the AWS account."""
    dims = target.get("dimensions")
    if not isinstance(dims, dict):
        dims = {}
    needs: Dict[str, Any] = {
        "namespace": target.get("namespace") or "",
        "metricName": target.get("metricName") or "",
        "statistic": target.get("statistic") or "",
        "dimensions": sorted(str(k) for k in dims),
        "region": target.get("region") or "default",
    }
    if target.get("expression"):
        needs["expression"] = target["expression"]
    return needs


# ---------------------------------------------------------------------------
# Missing datasources (what to add before import)
# ---------------------------------------------------------------------------

# Fallback add-datasource field specs when grafana.live.DS_TEMPLATES is
# unavailable; same shape as that table's "fields" (name/path/required/
# placeholder).
_FALLBACK_FIELDS: Dict[str, List[Dict[str, Any]]] = {
    "prometheus": [
        {"name": "url", "path": "url", "required": True,
         "placeholder": "http://mimir:9009/prometheus"},
        {"name": "httpMethod", "path": "jsonData.httpMethod",
         "required": False, "placeholder": "POST"},
    ],
    "loki": [
        {"name": "url", "path": "url", "required": True,
         "placeholder": "http://loki:3100"},
    ],
    "tempo": [
        {"name": "url", "path": "url", "required": True,
         "placeholder": "http://tempo:3200"},
    ],
    "cloudwatch": [
        {"name": "authType", "path": "jsonData.authType", "required": True,
         "placeholder": "keys"},
        {"name": "defaultRegion", "path": "jsonData.defaultRegion",
         "required": True, "placeholder": "us-east-1"},
        {"name": "accessKey", "path": "secureJsonData.accessKey",
         "required": False, "placeholder": "AKIA...", "secret": True},
        {"name": "secretKey", "path": "secureJsonData.secretKey",
         "required": False, "placeholder": "", "secret": True},
    ],
}


def _ds_template_spec(plugin_id: str) -> Tuple[List[Dict[str, Any]], str]:
    """(field specs, notes) for ``plugin_id`` from grafana.live's guided
    add-datasource forms, falling back to a built-in table."""
    try:
        from .grafana.live import DS_TEMPLATES
    except Exception:  # noqa: BLE001 - optional, never fatal
        DS_TEMPLATES = {}
    spec = DS_TEMPLATES.get(plugin_id) if isinstance(DS_TEMPLATES, dict) \
        else None
    if isinstance(spec, dict) and spec.get("fields"):
        return list(spec["fields"]), str(spec.get("notes") or "")
    return list(_FALLBACK_FIELDS.get(plugin_id) or []), ""


def _set_path(payload: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = payload
    for key in parts[:-1]:
        cur = cur.setdefault(key, {})
    cur[parts[-1]] = value


def add_datasource_template(family: str, plugin_id: str = "",
                            name: str = "") -> Dict[str, Any]:
    """The exact payload/commands that add a datasource of ``family``.

    Returns ``{"cli", "ui", "api", "payload", "required_fields",
    "notes"}`` where ``payload`` is a ready-to-edit body for
    ``POST /api/datasources`` with ``<...>`` placeholders for values the
    operator must fill in (secrets are never pre-filled).
    """
    plugin_id = plugin_id or _FAMILY_PLUGIN.get(family, family)
    fields, notes = _ds_template_spec(plugin_id)
    payload: Dict[str, Any] = {
        "name": name or family, "type": plugin_id, "access": "proxy",
    }
    required: List[str] = []
    for field in fields:
        fname = field.get("name", "")
        path = field.get("path") or fname
        if not fname or not path:
            continue
        if field.get("secret"):
            value = "<%s>" % fname
        else:
            value = field.get("placeholder") or "<%s>" % fname
        _set_path(payload, path, value)
        if field.get("required"):
            required.append(fname)
    return {
        "cli": "nr2grafana grafana add-datasource --type %s --name %s"
               % (plugin_id, name or family),
        "ui": "Connections -> Data sources -> Add new data source -> %s"
              % plugin_id,
        "api": 'curl -sS -X POST "$GRAFANA_URL/api/datasources" '
               '-H "Authorization: Bearer $GRAFANA_TOKEN" '
               '-H "Content-Type: application/json" -d @datasource.json',
        "payload": payload,
        "required_fields": required,
        "notes": notes,
    }


def _check_status_by_family(check_rows: Optional[List[Dict[str, Any]]]) \
        -> Optional[Dict[str, Dict[str, Any]]]:
    """GrafanaLive.check_requirements rows -> {family: row}; None when no
    check ran."""
    if check_rows is None:
        return None
    out: Dict[str, Dict[str, Any]] = {}
    for row in check_rows or []:
        item = str(row.get("item") or "")
        if item.startswith("datasource:"):
            out[item.split(":", 1)[1]] = row
    return out


def _collect_missing(datasources: List[Dict[str, Any]],
                     widget_report: List[Dict[str, Any]],
                     check_rows: Optional[List[Dict[str, Any]]] = None) \
        -> List[Dict[str, Any]]:
    """Families the dashboard needs but that nothing binds yet.

    Without a live check a family is missing when its uid reference is
    still a ``${var}`` template variable (nothing bound it to a concrete
    datasource). With ``check_rows`` (GrafanaLive.check_requirements),
    the instance decides: ``missing``/``wrong-type`` rows are missing,
    ``ok`` rows are not even if the reference is unbound (the instance
    has a datasource of that type to bind at import time).
    """
    status = _check_status_by_family(check_rows)
    out: List[Dict[str, Any]] = []
    for ds in datasources:
        if not ds.get("required", True):
            continue
        family = ds.get("family", "")
        uid_ref = ds.get("uid_ref") or ""
        unbound = (not uid_ref) or uid_ref.startswith("${")
        reason, detail = "", ""
        if status is not None:
            row = status.get(family)
            if row is None:
                if unbound:
                    reason = "unbound"
                    detail = ("%s not covered by the instance check; uid "
                              "reference %r is unbound" % (family, uid_ref))
            elif row.get("status") in ("missing", "wrong-type"):
                reason = "absent" if row.get("status") == "missing" \
                    else "wrong-type"
                detail = str(row.get("detail") or "")
        elif unbound:
            reason = "unbound"
            detail = ("no concrete datasource uid bound; the dashboard "
                      "references %r" % (uid_ref or "(none)"))
        if not reason:
            continue
        panel_ids = list(ds.get("panel_ids") or [])
        for entry in widget_report or []:
            if entry.get("missing_datasource") == family:
                pid = entry.get("panel_id")
                if pid is not None and pid not in panel_ids:
                    panel_ids.append(pid)
        panel_ids.sort(key=lambda p: (isinstance(p, str), p))
        plugin_id = ds.get("plugin_id") or _FAMILY_PLUGIN.get(family, family)
        tpl = add_datasource_template(family, plugin_id)
        fix = ("Add a %s datasource (%s), then bind %s to it at import "
               "time or re-export with --bind-datasources"
               % (family, tpl["cli"], uid_ref or "the panels"))
        if status is not None and status.get(family) and \
                status[family].get("fix"):
            fix = str(status[family]["fix"])
        out.append({
            "family": family,
            "plugin_id": plugin_id,
            "core": bool(ds.get("core", plugin_id in _CORE_PLUGINS)),
            "uid_ref": uid_ref,
            "reason": reason,
            "detail": detail,
            "panel_ids": panel_ids,
            "purpose": ds.get("purpose") or _FAMILY_PURPOSE.get(family,
                                                                family),
            "fix": fix,
            "add_datasource": tpl,
        })
    return out


def _import_section(dash: Dict[str, Any],
                    datasources: List[Dict[str, Any]],
                    plugins: List[Dict[str, Any]],
                    missing: Optional[List[Dict[str, Any]]] = None) \
        -> Dict[str, Any]:
    steps: List[str] = []
    if plugins:
        steps.append("Install plugin(s): %s; then restart Grafana."
                     % "; ".join(p["grafana_cli"] for p in plugins))
    if missing:
        steps.append("Add missing datasource(s): %s." % "; ".join(
            "%s (type %s, %s): %s"
            % (m["family"], m["plugin_id"], m["reason"],
               m["add_datasource"]["cli"]) for m in missing))
    if datasources:
        steps.append("Create/verify datasources: %s." % ", ".join(
            "%s (type %s, referenced as %s)"
            % (d["family"], d["plugin_id"], d["uid_ref"] or "default")
            for d in datasources))
    steps.append("UI import: Dashboards > New > Import, paste "
                 "dashboard.json, and bind each datasource variable "
                 "when prompted.")
    steps.append("API import: POST /api/dashboards/db with a service "
                 "account token (see api_example).")
    steps = ["%d. %s" % (i + 1, s) for i, s in enumerate(steps)]
    api_example = (
        'curl -s -X POST -H "Authorization: Bearer $GRAFANA_TOKEN" '
        '-H "Content-Type: application/json" '
        '"$GRAFANA_URL/api/dashboards/db" '
        '-d "{\\"dashboard\\": $(cat dashboard.json), '
        '\\"overwrite\\": true, \\"folderUid\\": \\"\\"}"')
    return {"steps": steps, "api_example": api_example}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_dashboard(nr: Optional[NRDashboard], dash: Dict[str, Any],
                      widget_report: List[Dict[str, Any]],
                      cfg: Dict[str, Any],
                      check_rows: Optional[List[Dict[str, Any]]] = None) \
        -> Dict[str, Any]:
    """Analyze one converted dashboard.

    nr may be None when only converted output is available; the widget
    report carries the original NRQL either way. ``check_rows`` is an
    optional GrafanaLive.check_requirements result for the target
    instance; with it ``missing_datasources`` reflects what that
    instance actually lacks instead of what the JSON leaves unbound.
    Returns the requirements dict (schema nr2grafana/requirements/v1).
    """
    cfg = cfg or {}
    widget_report = widget_report or []
    datasources = _collect_datasources(dash, cfg, widget_report)
    plugins = _collect_plugins(datasources)
    missing = _collect_missing(datasources, widget_report, check_rows)
    return {
        "schema": SCHEMA,
        "dashboard": dash.get("title", "") or
                     (nr.name if nr is not None else ""),
        "uid": dash.get("uid", ""),
        "generated_by": GENERATED_BY,
        "datasources": datasources,
        "missing_datasources": missing,
        "plugins": plugins,
        "domains": _collect_domains(widget_report, nr, cfg),
        "nr_native": _collect_nr_native(widget_report, cfg),
        "manual_panels": _collect_manual_panels(widget_report),
        "data_expectations": _collect_expectations(dash, cfg),
        "import": _import_section(dash, datasources, plugins, missing),
    }


def missing_datasources(requirements: Dict[str, Any],
                        check_rows: Optional[List[Dict[str, Any]]] = None) \
        -> List[Dict[str, Any]]:
    """``missing_datasources`` for an existing requirements dict,
    recomputed against ``check_rows`` when given (so the API/MCP layer
    can refresh the summary after an instance check without
    re-analyzing the dashboard)."""
    if check_rows is None and "missing_datasources" in requirements:
        return list(requirements.get("missing_datasources") or [])
    return _collect_missing(requirements.get("datasources") or [],
                            requirements.get("manual_panels") or [],
                            check_rows)


def summarize(requirements: Dict[str, Any]) -> str:
    """One-line human summary for the CLI and package index."""
    parts: List[str] = []
    ds = requirements.get("datasources") or []
    if ds:
        parts.append("%d datasource%s (%s)" % (
            len(ds), "" if len(ds) == 1 else "s",
            ", ".join(d["family"] for d in ds)))
    plugins = requirements.get("plugins") or []
    if plugins:
        parts.append("install plugin%s %s" % (
            "" if len(plugins) == 1 else "s",
            ", ".join(p["id"] for p in plugins)))
    domains = requirements.get("domains") or []
    if domains:
        parts.append("data domains: %s" % ", ".join(
            d["domain"] for d in domains))
    missing = requirements.get("missing_datasources") or []
    if missing:
        parts.append("missing datasource%s: %s" % (
            "" if len(missing) == 1 else "s",
            ", ".join("%s (%s)" % (m.get("family", "?"),
                                   m.get("reason", "unbound"))
                      for m in missing)))
    native = requirements.get("nr_native") or []
    if native:
        parts.append("%d NR-native panel%s need%s manual attention" % (
            len(native), "" if len(native) == 1 else "s",
            "s" if len(native) == 1 else ""))
    manual = requirements.get("manual_panels") or []
    extra_manual = [m for m in manual if m.get("confidence")
                    != "untranslatable"]
    if extra_manual:
        parts.append("%d other [MANUAL] panel%s" % (
            len(extra_manual), "" if len(extra_manual) == 1 else "s"))
    if not parts:
        return "no datasource requirements detected"
    return "; ".join(parts)
