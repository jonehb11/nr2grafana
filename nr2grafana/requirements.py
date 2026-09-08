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

from .model import NRDashboard
from .nrql.parser import NrqlParseError, parse_nrql

SCHEMA = "nr2grafana/requirements/v1"
GENERATED_BY = "nr2grafana 1.1.0"


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
           "nrgrafanaplugin-newrelic-datasource": "newrelic"}
    for family, spec in (cfg.get("datasources") or {}).items():
        if isinstance(spec, dict) and spec.get("type"):
            out[spec["type"]] = family
    return out


def _collect_datasources(dash: Dict[str, Any],
                         cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    fam_of = _family_map(cfg)
    found: Dict[str, Dict[str, Any]] = {}
    for panel in _iter_panels(dash):
        for target in panel.get("targets") or []:
            ds = target.get("datasource") or {}
            ds_type = ds.get("type", "")
            if not ds_type or ds_type == "datasource":
                continue
            family = fam_of.get(ds_type, ds_type)
            entry = found.setdefault(family, {
                "family": family,
                "plugin_id": ds_type,
                "core": ds_type in _CORE_PLUGINS,
                "uid_ref": ds.get("uid", ""),
                "purpose": _FAMILY_PURPOSE.get(family, ds_type),
                "panel_ids": [],
                "required": True,
            })
            pid = panel.get("id")
            if pid is not None and pid not in entry["panel_ids"]:
                entry["panel_ids"].append(pid)
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
        out.append({
            "panel_id": report.get("panel_id"),
            "widget": viz or report.get("widget",
                                        report.get("widget_title", "")),
            "why": why,
            "equivalent": equivalent,
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
    return out


def _import_section(dash: Dict[str, Any],
                    datasources: List[Dict[str, Any]],
                    plugins: List[Dict[str, Any]]) -> Dict[str, Any]:
    steps: List[str] = []
    if plugins:
        steps.append("Install plugin(s): %s; then restart Grafana."
                     % "; ".join(p["grafana_cli"] for p in plugins))
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
                      cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze one converted dashboard.

    nr may be None when only converted output is available; the widget
    report carries the original NRQL either way. Returns the
    requirements dict (schema nr2grafana/requirements/v1).
    """
    cfg = cfg or {}
    datasources = _collect_datasources(dash, cfg)
    plugins = _collect_plugins(datasources)
    return {
        "schema": SCHEMA,
        "dashboard": dash.get("title", "") or
                     (nr.name if nr is not None else ""),
        "uid": dash.get("uid", ""),
        "generated_by": GENERATED_BY,
        "datasources": datasources,
        "plugins": plugins,
        "domains": _collect_domains(widget_report, nr, cfg),
        "nr_native": _collect_nr_native(widget_report, cfg),
        "data_expectations": _collect_expectations(dash, cfg),
        "import": _import_section(dash, datasources, plugins),
    }


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
    native = requirements.get("nr_native") or []
    if native:
        parts.append("%d NR-native panel%s need%s manual attention" % (
            len(native), "" if len(native) == 1 else "s",
            "s" if len(native) == 1 else ""))
    if not parts:
        return "no datasource requirements detected"
    return "; ".join(parts)
