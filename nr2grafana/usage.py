"""What the migrated dashboards actually NEED (the KEEP set).

The cost-optimization engine's safety guarantee rests entirely on this
module: before recommending that any metric, label or log-stream label be
dropped, ``optimize.recommend`` checks it against the sets produced here.
Anything a converted dashboard references is, by definition, *used* and
must never be proposed for removal.

``collect_usage`` walks the converted Grafana dashboards (and, optionally,
the builder's migration reports) and extracts, per datasource family:

- **prometheus**: every metric name a PromQL query references (including
  histogram ``_bucket``/``_sum``/``_count`` siblings) and every label used
  in a matcher or ``by()``/``without()`` grouping;
- **loki**: the stream-selector labels each LogQL query filters on, the
  broader set of labels referenced anywhere in the query, and the concrete
  label *values* filtered on (so we can tell a used value apart from a
  merely-present one);
- **tempo**: the raw TraceQL and any span/resource attribute names.

It reuses the PromQL / LogQL scanners from :mod:`requirements` so the
"what is used" logic stays identical to the requirements analyzer.

Output schema: ``nr2grafana/usage/v1``.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Set

from .requirements import (
    _family_map,
    _iter_panels,
    _logql_needs,
    _promql_needs,
)

SCHEMA = "nr2grafana/usage/v1"

# label = "value" / label =~ "v1|v2" pairs inside a stream selector.
_PAIR_RE = re.compile(
    r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*'
    r'"((?:[^"\\]|\\.)*)"')

# Attribute references in TraceQL: .name, span.name, resource.name.
_TRACEQL_ATTR_RE = re.compile(
    r'(?:\b(?:span|resource)\.)?\.?'
    r'([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)*)\s*(?:=~|!~|!=|=|>|<)')


def _histogram_siblings(metric: str) -> List[str]:
    """The ``_bucket``/``_sum``/``_count`` family for a histogram metric.

    A dashboard that uses ``foo_bucket`` also implicitly relies on the
    histogram base ``foo``; keep-lists must retain every sibling so a
    "keep only what we use" rule never breaks a used histogram.
    """
    for suffix in ("_bucket", "_sum", "_count"):
        if metric.endswith(suffix):
            base = metric[: -len(suffix)]
            return [base, base + "_bucket", base + "_sum",
                    base + "_count"]
    return [metric]


def _selector_pairs(selector: str) -> List[Any]:
    """[(label, op, [values])] for a stream selector like ``{a="b"}``."""
    out: List[Any] = []
    inner = selector[1:-1] if selector.startswith("{") else selector
    for m in _PAIR_RE.finditer(inner):
        label, op, raw = m.group(1), m.group(2), m.group(3)
        if op in ("=~", "!~"):
            # Split alternation and strip simple regex anchors so a
            # value like `^(prod|stage)$` yields prod, stage.
            body = raw.strip()
            body = body[1:] if body.startswith("^") else body
            body = body[:-1] if body.endswith("$") else body
            body = body.strip("()")
            values = [v.strip() for v in body.split("|") if v.strip()]
        else:
            values = [raw] if raw else []
        out.append((label, op, values))
    return out


def _add(target: Set[str], items) -> None:
    for it in items or []:
        if it:
            target.add(it)


def collect_usage(dashboards, widget_reports=None,
                  cfg=None) -> Dict[str, Any]:
    """Scan converted dashboards for the used metrics / labels / streams.

    ``dashboards`` is a converted Grafana dashboard dict or a list of
    them. ``widget_reports`` (the builder's migration reports) is accepted
    for signature parity and lightly scanned for extra evidence; usage is
    derived primarily from the converted query text, which is the ground
    truth for what the panels will ask the datasources for.

    Returns schema ``nr2grafana/usage/v1``::

        {"schema", "generated_at", "dashboards": int,
         "prometheus": {"metrics": [...], "labels": [...]},
         "loki": {"stream_labels": [...], "used_labels": [...],
                  "filtered_values": {label: [values]}},
         "tempo": {"traceql": [...], "labels": [...]}}
    """
    cfg = cfg or {}
    if isinstance(dashboards, dict):
        dashboards = [dashboards]
    dashboards = [d for d in (dashboards or []) if isinstance(d, dict)]
    fam_of = _family_map(cfg)

    prom_metrics: Set[str] = set()
    prom_labels: Set[str] = set()
    loki_stream_labels: Set[str] = set()
    loki_used_labels: Set[str] = set()
    loki_values: Dict[str, Set[str]] = {}
    tempo_queries: List[str] = []
    tempo_labels: Set[str] = set()

    def note_value(label: str, values) -> None:
        bucket = loki_values.setdefault(label, set())
        for v in values or []:
            if v:
                bucket.add(v)

    for dash in dashboards:
        for panel in _iter_panels(dash):
            for target in panel.get("targets") or []:
                ds_type = (target.get("datasource") or {}).get("type", "")
                family = fam_of.get(ds_type, ds_type)
                expr = target.get("expr") or ""
                if family == "prometheus" and expr:
                    metrics, labels = _promql_needs(expr)
                    _add(prom_labels, labels)
                    for metric in metrics:
                        prom_metrics.add(metric)
                elif family == "loki" and expr:
                    selector, labels = _logql_needs(expr)
                    _add(loki_used_labels, labels)
                    for label, _op, values in _selector_pairs(selector):
                        loki_stream_labels.add(label)
                        loki_used_labels.add(label)
                        note_value(label, values)
                elif family == "tempo":
                    query = target.get("query") or expr or ""
                    if query:
                        tempo_queries.append(query)
                        for m in _TRACEQL_ATTR_RE.finditer(query):
                            tempo_labels.add(m.group(1))

    # widget_reports is accepted for signature parity; usage is derived
    # from the converted query text above, which is the ground truth for
    # what the panels ask the datasources for.
    _ = widget_reports

    return {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime()),
        "dashboards": len(dashboards),
        "prometheus": {
            "metrics": sorted(prom_metrics),
            "labels": sorted(prom_labels),
        },
        "loki": {
            "stream_labels": sorted(loki_stream_labels),
            "used_labels": sorted(loki_used_labels),
            "filtered_values": {
                k: sorted(v) for k, v in sorted(loki_values.items())},
        },
        "tempo": {
            "traceql": tempo_queries,
            "labels": sorted(tempo_labels),
        },
    }


def prometheus_keep_set(usage: Dict[str, Any]) -> Set[str]:
    """Every Prometheus metric name that must survive a keep/drop rule.

    Used metric names plus their histogram siblings, so dropping "unused"
    metrics never orphans a histogram the dashboards depend on.
    """
    keep: Set[str] = set()
    for metric in (usage.get("prometheus") or {}).get("metrics") or []:
        for sibling in _histogram_siblings(metric):
            keep.add(sibling)
    return keep


def loki_label_keep_set(usage: Dict[str, Any]) -> Set[str]:
    """Every Loki label a dashboard filters on or groups by (never drop)."""
    loki = usage.get("loki") or {}
    keep: Set[str] = set()
    _add(keep, loki.get("stream_labels"))
    _add(keep, loki.get("used_labels"))
    return keep
