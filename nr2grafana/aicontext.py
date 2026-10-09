"""AI-first context bundle (stdlib only).

Package every artifact nr2grafana produces -- the converted dashboard
summary plus requirements, diagnosis, parity, samples, cost, optimize,
deep-dive and packing analyses -- into ONE compact, LLM-optimized
context bundle so an AI agent (local console CLI or the Anthropic API)
becomes a first-class tenant that can troubleshoot the migrated
dashboards and the whole LGTM observability stack.

Design goals:

- **Compact**: summaries and top-N slices, never raw dumps. The bundle
  is meant to fit comfortably in a prompt; long snippets are truncated
  and lists are capped.
- **Stable**: deterministic ordering and no wall-clock timestamp, so
  the same store produces byte-identical bundles (cache/diff friendly).
- **Self-describing**: a ``legend`` explains every field and a
  ``preamble`` tells the AI its task and the hard safety rules (never
  trade away durability / availability / performance for cost).
- **Safe**: ``redact=True`` (default) defensively strips any
  secret-looking values, even though artifacts should never carry
  secrets in the first place. New Relic is strictly read-only.

Public surface::

    build_context(store, slug, include, grafana, deepdive, redact=True,
                  missing=None)
        -> dict   # schema "nr2grafana/ai-context/v1"
    missing_report(requirements, widgets, templates=None, bound_refs=None,
                   instance_types=None, cfg=None) -> dict
        # SEAM-REPORT: what is still missing before the dashboard works
        # (unbound datasource families + the exact add-datasource
        # template, [MANUAL] panels with WHY + closest equivalent,
        # needs-review panels)
    to_markdown(context) -> str            # section per artifact
    to_prompt(context, question="") -> str # ready single-string prompt
    troubleshoot(assistant, context, question="") -> dict
        # {"answer", "backend"} ; never raises
    analyze_cost(assistant, context, question="") -> dict
        # RCA/mitigation AI flow over the bundle;
        # {"answer", "backend"[, "plan"]} ; never raises
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

SCHEMA = "nr2grafana/ai-context/v1"

# Artifact kinds folded into the bundle, in a fixed presentation order.
# The cost-anomaly trio (flowlogs -> rca -> mitigation) trails the stack
# analyses: flow-log evidence, then the root-cause diagnosis it feeds, then
# the reliability-safe mitigation plan derived from that diagnosis.
ARTIFACT_ORDER = ("requirements", "diagnosis", "parity", "samples",
                  "cost", "optimize", "deepdive", "packing",
                  "flowlogs", "rca", "mitigation")

# Compactness knobs: at most this many rows per list, strings capped.
TOP = 8
_MAX_STR = 400
_MAX_LIST = 24

# Converter confidence ordering: worst (most in need of AI conversion)
# first, so the per-panel translations list surfaces the hard panels.
_CONF_RANK = {
    "untranslatable": 0, "needs-review": 1,
    "approximate": 2, "exact": 3,
}

# Severity ranking shared across artifact vocabularies (diagnosis uses
# blocker/warn/info; optimize/deepdive use high/medium/low and
# FAIL/WARN/INFO). Lower sorts first (most severe).
_SEV_RANK = {
    "fail": 0, "blocker": 0, "critical": 0, "high": 0,
    "warn": 1, "warning": 1, "medium": 1,
    "info": 2, "low": 2, "ok": 3,
}

# Keys whose values are always masked when redacting.
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[-_]?key|apikey|credential|"
    r"authorization|bearer|cookie|session)",
    re.IGNORECASE)

# Values that look like credentials regardless of their key name.
_SECRET_VALUE_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{16,}"          # Anthropic / OpenAI style
    r"|gh[pousr]_[A-Za-z0-9]{16,}"        # GitHub tokens
    r"|glsa_[A-Za-z0-9_]{16,}"            # Grafana service-account
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"  # JWT
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]{12,})")

REDACTED = "[REDACTED]"

PREAMBLE = (
    "You are an SRE assistant troubleshooting a migration from New "
    "Relic to Grafana on an LGTM stack (Loki logs, Grafana dashboards, "
    "Tempo traces, Mimir/Prometheus metrics), and the health and cost "
    "of that observability stack. The bundle below is everything "
    "nr2grafana knows: a converted-dashboard summary plus each "
    "analysis artifact, compacted to summaries and top findings. Use "
    "it to (1) explain and fix no-data or wrong-value panels, (2) "
    "confirm data parity between New Relic and Grafana, and (3) "
    "propose SAFE optimizations. Hard rules you must honor: never "
    "recommend cutting replication factor, retention, zone-aware "
    "replication, scrape interval, exemplars or span-metrics for cost "
    "without a loud durability/availability caveat; right-size to the "
    "observed peak (never below it), not the average; never CPU-limit "
    "Mimir ingesters; keep stateful ingesters on-demand. New Relic is "
    "strictly read-only. Prefer eliminating unused metric series -- it "
    "scales down every other cost. When a recommendation carries "
    "keeps_performance / keeps_durability / keeps_availability flags, "
    "surface them; default to the option that keeps them true.")

LEGEND = {
    "dashboard": "The converted Grafana dashboard under study: slug, "
                 "title, New Relic source, datasource families, panel "
                 "count.",
    "requirements": "What the dashboard needs to run: datasource "
                    "families, plugins to install, data domains, and "
                    "New-Relic-native features with no Grafana "
                    "equivalent.",
    "diagnosis": "Findings from checking the dashboard against the "
                 "live Grafana instance; severity blocker/warn/info, "
                 "area auth/datasource/panel/data/config, each with a "
                 "fix.",
    "parity": "Per-panel comparison of New Relic vs Grafana query "
              "results; score 0-100 and per-verdict counts (match / "
              "mismatch / errors). Low-scoring panels are listed.",
    "samples": "Side-by-side sample query results (New Relic vs "
               "Grafana) per panel; used to spot empty or divergent "
               "panels.",
    "cost": "Estimated monthly LGTM cost by component (Mimir/Loki/"
            "Tempo), from your pricing inputs -- an estimate, not a "
            "bill.",
    "optimize": "Ranked cost/cardinality recommendations; each has an "
                "estimated monthly saving and keeps_intact / "
                "needs_review flags (safe vs review-first).",
    "deepdive": "Metric-driven deep analysis of the stack (capacity, "
                "cardinality, churn, network, Loki, Tempo); findings "
                "carry evidence, a config snippet, estimated savings "
                "and keeps_performance/durability/availability flags.",
    "packing": "Kubernetes topology, bin-packing / right-sizing, "
               "durability audit and Karpenter analysis; a proposed "
               "node layout and savings that keep availability.",
    "est_savings": "An ESTIMATE with stated assumptions: monthly_usd, "
                   "and native units (series, bytes_per_day, "
                   "compute).",
    "keeps_flags": "keeps_performance/durability/availability = the "
                   "recommendation does NOT sacrifice that property; "
                   "false means it trades it and must be caveated.",
    "translations": "Per-panel migration hints for the panels the "
                    "converter could not translate cleanly: the "
                    "original_nrql, the converter confidence "
                    "(exact/approximate/needs-review/untranslatable), "
                    "the translation_notes explaining what needs review "
                    "or a from-scratch conversion, plus metric_kind "
                    "(counter/gauge/histogram/summary per metric), "
                    "closest_equivalent (datasource + example query/"
                    "target for a [MANUAL] or needs-review panel) and "
                    "missing_datasource (the family this panel needs but "
                    "that is not bound yet).",
    "missing": "What to add before this dashboard works: "
               "missing_datasources (families with no bound uid) with "
               "the exact add-datasource template (API body, MCP tool "
               "call, CLI command), manual_panels ([MANUAL] placeholders "
               "with WHY + closest_equivalent) and needs_review panels.",
    "flowlogs": "VPC Flow Logs cross-AZ byte attribution: the dominant "
                "destination port (e.g. 9095 = Mimir/Loki gRPC), GB/day, "
                "per-driver %-of-cross-AZ share, top talker flows and the "
                "step-change date. A DataTransfer-Regional-Bytes usage "
                "type means cross-AZ NETWORK, not storage.",
    "rca": "Cost-anomaly root-cause analysis: the incident (usage_type, "
           "service, account, region, $/day, GB/day, onset/step-change), "
           "the dominant driver with its % share + evidence, ranked "
           "secondary drivers, an explicit ruled-out list, which "
           "independent evidence sources converged, and a confidence.",
    "mitigation": "Reliability-safe mitigation plan: ranked proposals "
                  "(PROPOSAL only, GitOps/IaC-owned, never executed) with "
                  "expected saving, reliability_guardrails (preconditions "
                  "that MUST hold), keeps_availability/durability/"
                  "performance + handles_current_traffic flags, and the "
                  "generated paste-ready config targets.",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _trunc(value: Any, limit: int = _MAX_STR) -> Any:
    """Truncate an overlong string, leaving other types untouched."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit].rstrip() + " ..."
    return value


def _num(value: Any) -> float:
    """Best-effort float; 0.0 on anything non-numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sev_rank(sev: Any) -> int:
    return _SEV_RANK.get(str(sev or "").strip().lower(), 2)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rank_findings(rows: List[Dict[str, Any]],
                   savings_key: str = "est_savings") -> List[Dict[str, Any]]:
    """Deterministically order findings: severity, then $ saving desc,
    then title. Input rows are dicts; unknown fields are tolerated."""
    def key(row: Dict[str, Any]):
        sev = _sev_rank(row.get("severity"))
        usd = -_num(_as_dict(row.get(savings_key)).get("monthly_usd"))
        title = str(row.get("title") or row.get("problem") or "")
        return (sev, usd, title)
    return sorted([r for r in rows if isinstance(r, dict)], key=key)


def _compact_savings(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compact est_savings: keep only present numeric-ish fields."""
    src = _as_dict(row.get("est_savings"))
    if not src:
        return None
    out: Dict[str, Any] = {}
    for k in ("monthly_usd", "series", "streams", "bytes_per_day",
              "gb_per_day", "compute", "nodes"):
        if k in src and src[k] is not None:
            out[k] = src[k]
    return out or None


def _keeps(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Collect the keeps_* / needs_review / keeps_intact risk flags."""
    out: Dict[str, Any] = {}
    for k in ("keeps_performance", "keeps_durability",
              "keeps_availability", "keeps_intact", "needs_review"):
        if k in row:
            out[k] = bool(row[k])
    return out or None


def _pick(src: Dict[str, Any], keys, strlimit: int = 120) -> Dict[str, Any]:
    """Copy the present, non-empty ``keys`` from ``src``, truncating
    string values. Tolerant of missing keys and non-dict input."""
    out: Dict[str, Any] = {}
    src = _as_dict(src)
    for k in keys:
        if k in src:
            v = src[k]
            if v in (None, "", [], {}):
                continue
            out[k] = _trunc(v, strlimit) if isinstance(v, str) else v
    return out


# Numeric-ish saving fields, spanning the monthly (optimize/deepdive) and
# per-day (RCA/mitigation) vocabularies plus percentage shares.
_SAVINGS_KEYS = ("monthly_usd", "usd_per_day", "dollars_per_day",
                 "per_day_usd", "gb_per_day", "series", "streams",
                 "bytes_per_day", "compute", "nodes", "pct_saved",
                 "percent_saved", "pct", "share")


def _mitigation_savings(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compact a mitigation's expected saving; tolerant of it living
    under est_savings/savings or directly on the mitigation row."""
    src = _as_dict(row.get("est_savings")) or _as_dict(row.get("savings"))
    out: Dict[str, Any] = {}
    for k in _SAVINGS_KEYS:
        if k in src and src[k] is not None:
            out[k] = src[k]
    if out:
        return out
    return _pick(row, _SAVINGS_KEYS, 40) or None


def _config_targets(row: Dict[str, Any]) -> List[str]:
    """Just the config targets/languages, never the full snippet."""
    out: List[str] = []
    for c in _as_list(row.get("config")):
        if not isinstance(c, dict):
            continue
        tgt = c.get("target") or ""
        lang = c.get("language") or ""
        label = tgt if not lang else "%s (%s)" % (tgt, lang)
        if label:
            out.append(label)
    return out[:TOP]


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

def _redact_str(text: str) -> str:
    return _SECRET_VALUE_RE.sub(REDACTED, text)


def redact(value: Any, key: str = "") -> Any:
    """Recursively strip secret-looking values from a structure.

    A value is masked when its key looks like a credential name, or
    when a string value matches a known token pattern. Non-secret data
    is returned unchanged. Pure and defensive -- artifacts should never
    carry secrets, but the bundle is meant to be pasted into an AI, so
    this is a belt-and-suspenders scrub.
    """
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k) \
                    and isinstance(v, (str, int, float)) and v != "":
                out[k] = REDACTED
            else:
                out[k] = redact(v, k if isinstance(k, str) else "")
        return out
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, str):
        if _SECRET_KEY_RE.search(key) and value:
            return REDACTED
        return _redact_str(value)
    return value


# ---------------------------------------------------------------------------
# per-artifact summarizers  (compact: summaries + top-N)
# ---------------------------------------------------------------------------

def _sum_requirements(art: Dict[str, Any]) -> Dict[str, Any]:
    ds = []
    for d in _as_list(art.get("datasources"))[:_MAX_LIST]:
        if isinstance(d, dict):
            ds.append({"family": d.get("family") or d.get("type") or "",
                       "uid": d.get("uid") or ""})
    plugins = [p.get("id") or p.get("type") or ""
               for p in _as_list(art.get("plugins"))
               if isinstance(p, dict)][:_MAX_LIST]
    domains = [d.get("domain") or ""
               for d in _as_list(art.get("domains"))
               if isinstance(d, dict)][:_MAX_LIST]
    nr_native = []
    for n in _as_list(art.get("nr_native"))[:TOP]:
        if isinstance(n, dict):
            nr_native.append(_trunc(n.get("feature") or n.get("note")
                                    or str(n), 120))
        else:
            nr_native.append(_trunc(str(n), 120))
    out: Dict[str, Any] = {"datasources": ds}
    if plugins:
        out["install_plugins"] = plugins
    if domains:
        out["data_domains"] = domains
    if nr_native:
        out["nr_native_features"] = nr_native
    return out


def _sum_diagnosis(art: Dict[str, Any]) -> Dict[str, Any]:
    summary = _as_dict(art.get("summary"))
    findings = _rank_findings(_as_list(art.get("findings")))
    top = []
    for f in findings[:TOP]:
        top.append({
            "severity": f.get("severity") or "",
            "area": f.get("area") or "",
            "problem": _trunc(f.get("problem") or f.get("title") or ""),
            "fix": _trunc(f.get("fix") or ""),
            "panel_id": f.get("panel_id"),
        })
    return {
        "counts": {k: summary.get(k) for k in
                   ("findings", "blocker", "warn", "info")
                   if k in summary},
        "by_area": _as_dict(summary.get("by_area")),
        "top_findings": top,
    }


def _sum_parity(art: Dict[str, Any]) -> Dict[str, Any]:
    panels = _as_list(art.get("panels"))
    worst = []
    for r in panels:
        if not isinstance(r, dict):
            continue
        verdict = str(r.get("verdict") or "")
        if verdict in ("match", "ok", ""):
            continue
        worst.append(r)
    # Order: worst verdicts first, then by panel title, deterministic.
    worst.sort(key=lambda r: (_sev_rank(r.get("verdict")),
                              str(r.get("panel_title") or "")))
    rows = []
    for r in worst[:TOP]:
        rows.append({
            "panel": _trunc(r.get("panel_title") or "", 120),
            "refId": r.get("refId") or "",
            "verdict": r.get("verdict") or "",
            "detail": _trunc(r.get("detail") or ""),
        })
    return {
        "score": art.get("score"),
        "verdicts": _as_dict(art.get("summary")),
        "panels": len(panels),
        "worst_panels": rows,
    }


def _sum_samples(art: Dict[str, Any]) -> Dict[str, Any]:
    panels = _as_list(art.get("panels"))
    notable = []
    for r in panels:
        if not isinstance(r, dict):
            continue
        nr_kind = str(_as_dict(r.get("nr")).get("kind") or "")
        gf_kind = str(_as_dict(r.get("grafana")).get("kind") or "")
        divergent = (nr_kind != gf_kind
                     or nr_kind in ("error", "empty")
                     or gf_kind in ("error", "empty"))
        if divergent:
            notable.append({
                "panel": _trunc(r.get("panel_title") or "", 120),
                "refId": r.get("refId") or "",
                "nr": nr_kind,
                "grafana": gf_kind,
            })
    notable.sort(key=lambda x: (x["panel"], x["refId"]))
    return {"panels": len(panels), "divergent_panels": notable[:TOP]}


def _sum_cost(art: Dict[str, Any]) -> Dict[str, Any]:
    comps = []
    for c in _as_list(art.get("components")):
        if not isinstance(c, dict):
            continue
        comps.append({
            "component": c.get("name") or c.get("family")
            or c.get("component") or "",
            "family": c.get("family") or "",
            "monthly_usd": c.get("monthly_cost"),
        })
    comps.sort(key=lambda x: -_num(x.get("monthly_usd")))
    return {
        "monthly_total_usd": art.get("monthly_total"),
        "components": comps[:_MAX_LIST],
        "resources": _as_dict(art.get("resources")),
    }


def _sum_optimize(art: Dict[str, Any]) -> Dict[str, Any]:
    recs = _rank_findings(_as_list(art.get("recommendations")))
    top = []
    for r in recs[:TOP]:
        entry: Dict[str, Any] = {
            "family": r.get("family") or "",
            "kind": r.get("kind") or "",
            "severity": r.get("severity") or "",
            "title": _trunc(r.get("title") or ""),
        }
        sav = _compact_savings(r)
        if sav:
            entry["est_savings"] = sav
        keeps = _keeps(r)
        if keeps:
            entry["risk"] = keeps
        targets = _config_targets(r)
        if targets:
            entry["config_targets"] = targets
        top.append(entry)
    return {"summary": _as_dict(art.get("summary")),
            "top_recommendations": top}


def _sum_findings_generic(art: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-dive style: a list of findings with evidence + risk flags."""
    findings = _rank_findings(_as_list(art.get("findings")))
    top = []
    for f in findings[:TOP]:
        entry: Dict[str, Any] = {
            "severity": f.get("severity") or "",
            "area": f.get("area") or "",
            "title": _trunc(f.get("title") or ""),
            "rationale": _trunc(f.get("rationale") or "", 240),
        }
        ev = _as_dict(f.get("evidence"))
        if ev:
            # Keep evidence tiny: first few scalar entries only.
            small: Dict[str, Any] = {}
            for k in sorted(ev.keys())[:6]:
                v = ev[k]
                if isinstance(v, (str, int, float, bool)):
                    small[k] = _trunc(v, 80)
            if small:
                entry["evidence"] = small
        sav = _compact_savings(f)
        if sav:
            entry["est_savings"] = sav
        keeps = _keeps(f)
        if keeps:
            entry["risk"] = keeps
        targets = _config_targets(f)
        if targets:
            entry["config_targets"] = targets
        top.append(entry)
    out: Dict[str, Any] = {"top_findings": top}
    if isinstance(art.get("summary"), dict):
        out["summary"] = art["summary"]
    return out


def _sum_packing(art: Dict[str, Any]) -> Dict[str, Any]:
    """Packing: bin-pack floor, right-sizing, durability, Karpenter.

    The packing schema is defensively summarized -- only well-known
    keys are pulled and everything is optional, so a partial or
    evolving shape degrades to a shallow summary instead of raising.
    """
    out: Dict[str, Any] = {}
    if art.get("kubectl") is False or art.get("available") is False:
        out["note"] = ("Kubernetes not analyzed (kubectl unavailable "
                       "or disabled); stack usable without a cluster.")
    sim = _as_dict(art.get("packing_sim")) or _as_dict(art.get("sim"))
    if sim:
        floor = sim.get("floor") or sim.get("recommended")
        candidates = []
        for c in _as_list(sim.get("candidates"))[:TOP]:
            if isinstance(c, dict):
                candidates.append({
                    "shape": c.get("shape") or c.get("instance") or "",
                    "nodes": c.get("nodes"),
                    "monthly_usd": c.get("monthly_usd") or c.get("cost"),
                    "mem_util": c.get("mem_util"),
                })
        out["packing"] = {"floor": floor, "candidates": candidates}
    rs = _as_dict(art.get("rightsizing"))
    if rs:
        out["rightsizing"] = {
            k: rs[k] for k in
            ("compute_saved", "monthly_usd", "keeps_performance",
             "cores_saved", "mem_gib_saved")
            if k in rs}
    dur = _as_list(art.get("durability"))
    if dur:
        rows = []
        for d in _rank_findings(dur)[:TOP]:
            rows.append({
                "severity": d.get("severity") or "",
                "title": _trunc(d.get("title") or d.get("problem") or ""),
            })
        out["durability_audit"] = rows
    karp = _as_dict(art.get("karpenter"))
    if karp:
        kfind = []
        for f in _rank_findings(_as_list(karp.get("findings")))[:TOP]:
            kfind.append({
                "severity": f.get("severity") or "",
                "title": _trunc(f.get("title") or ""),
            })
        ks: Dict[str, Any] = {"findings": kfind,
                              "est_savings": _as_dict(karp.get(
                                  "est_savings")) or None}
        if karp.get("proposed_nodepool_yaml"):
            ks["has_proposed_nodepool"] = True
        out["karpenter"] = ks
    if not out:
        # Unknown shape -- fall back to a shallow key listing.
        out["keys"] = sorted(k for k in art.keys() if k != "schema")
    return out


def _sum_flowlogs(art: Dict[str, Any]) -> Dict[str, Any]:
    """Flow-log cross-AZ attribution: dominant port, GB/day, per-driver
    %-share, top talker flows, step-change date. Degrades to a note when
    no flow logs are configured; tolerant of evolving field names."""
    out: Dict[str, Any] = {}
    note = art.get("note")
    if note:
        out["note"] = _trunc(str(note), 240)
    out.update(_pick(art, (
        "dominant_port", "gb_per_day", "cross_az_gb_per_day",
        "cross_az_pct", "step_change_date", "step_change", "onset",
        "window"), strlimit=120))
    drivers = []
    for d in _as_list(art.get("drivers"))[:TOP]:
        if isinstance(d, dict):
            drivers.append(_pick(d, (
                "driver", "workload", "port", "dstport", "dstPort",
                "gb_per_day", "gb", "pct_of_cross_az", "pct", "share"),
                80))
    if drivers:
        out["drivers"] = [d for d in drivers if d]
    flows = []
    src_flows = _as_list(art.get("top_flows")) or _as_list(art.get("flows"))
    for f in src_flows[:TOP]:
        if isinstance(f, dict):
            flows.append(_pick(f, (
                "src", "dst", "srcaddr", "dstaddr", "srcAddr", "dstAddr",
                "az_pair", "dstport", "dstPort", "port", "workload",
                "gb", "gb_per_day", "bytes"), 60))
    if flows:
        out["top_flows"] = [f for f in flows if f]
    if not out:
        out["keys"] = sorted(k for k in art.keys() if k != "schema")
    return out


def _sum_rca(art: Dict[str, Any]) -> Dict[str, Any]:
    """Root-cause analysis: incident frame, dominant driver (+share and
    evidence), ranked secondaries, ruled-out list, which evidence sources
    converged, and confidence. Defensive against key-name drift."""
    out: Dict[str, Any] = {}
    inc = _pick(_as_dict(art.get("incident")), (
        "usage_type", "service", "account", "region", "hypothesis_class",
        "class", "usd_per_day", "dollars_per_day", "$/day", "per_day_usd",
        "gb_per_day", "GB/day", "onset", "onset_date", "step_change_date",
        "step_change", "score"), strlimit=160)
    if inc:
        out["incident"] = inc
    cause = _as_dict(art.get("cause"))
    dom = _as_dict(cause.get("dominant"))
    if dom:
        d = _pick(dom, ("share", "pct", "driver", "port", "summary"),
                  strlimit=_MAX_STR)
        ev = dom.get("evidence")
        if isinstance(ev, list):
            evl = [_trunc(str(e), 160) for e in ev[:TOP] if e]
            if evl:
                d["evidence"] = evl
        elif isinstance(ev, dict):
            de = _pick(ev, sorted(ev.keys())[:6], 120)
            if de:
                d["evidence"] = de
        elif isinstance(ev, str) and ev:
            d["evidence"] = _trunc(ev, _MAX_STR)
        out["dominant"] = d
    sec = []
    for s in _as_list(cause.get("secondary"))[:TOP]:
        if isinstance(s, dict):
            sec.append(_pick(s, ("share", "pct", "driver", "port",
                                 "summary"), _MAX_STR))
        elif s:
            sec.append(_trunc(str(s), _MAX_STR))
    sec = [s for s in sec if s]
    if sec:
        out["secondary"] = sec
    ruled = []
    for r in _as_list(cause.get("ruled_out"))[:_MAX_LIST]:
        if isinstance(r, dict):
            ruled.append(_pick(r, ("hypothesis", "cause", "reason",
                                   "evidence", "summary"), 240))
        elif r:
            ruled.append(_trunc(str(r), 240))
    ruled = [r for r in ruled if r]
    if ruled:
        out["ruled_out"] = ruled
    conv = art.get("evidence_convergence")
    if isinstance(conv, list):
        cl = [str(c) for c in conv if c][:_MAX_LIST]
        if cl:
            out["evidence_convergence"] = cl
    elif isinstance(conv, dict):
        cd = _pick(conv, sorted(conv.keys())[:_MAX_LIST], 80)
        if cd:
            out["evidence_convergence"] = cd
    if art.get("confidence") not in (None, ""):
        out["confidence"] = art.get("confidence")
    if not out:
        out["keys"] = sorted(k for k in art.keys() if k != "schema")
    return out


def _sum_mitigation(art: Dict[str, Any]) -> Dict[str, Any]:
    """Mitigation plan: keep the planner's ranking, cap to top-N, and per
    proposal carry title/change/owner, compact saving, keeps_* +
    handles_current_traffic risk flags, reliability guardrails and the
    config TARGET labels only (never the full paste-ready snippet)."""
    rows = []
    for m in _as_list(art.get("mitigations"))[:TOP]:
        if not isinstance(m, dict):
            continue
        entry = _pick(m, ("title", "change", "owner"), _MAX_STR)
        sav = _mitigation_savings(m)
        if sav:
            entry["est_savings"] = sav
        keeps = _keeps(m)
        if keeps:
            entry["risk"] = keeps
        if "handles_current_traffic" in m:
            entry["handles_current_traffic"] = \
                bool(m["handles_current_traffic"])
        guards = m.get("reliability_guardrails") or m.get("guardrails")
        if isinstance(guards, list):
            g = [_trunc(str(x), 200) for x in guards[:TOP] if x]
            if g:
                entry["reliability_guardrails"] = g
        elif isinstance(guards, str) and guards:
            entry["reliability_guardrails"] = [_trunc(guards, _MAX_STR)]
        targets = _config_targets(m)
        if targets:
            entry["config_targets"] = targets
        rows.append(entry)
    out: Dict[str, Any] = {"mitigations": rows}
    if isinstance(art.get("summary"), dict):
        out["summary"] = art["summary"]
    for k in ("total_savings", "total_est_savings"):
        if art.get(k) not in (None, "", [], {}):
            out[k] = art[k]
            break
    return out


_SUMMARIZERS = {
    "requirements": _sum_requirements,
    "diagnosis": _sum_diagnosis,
    "parity": _sum_parity,
    "samples": _sum_samples,
    "cost": _sum_cost,
    "optimize": _sum_optimize,
    "deepdive": _sum_findings_generic,
    "packing": _sum_packing,
    "flowlogs": _sum_flowlogs,
    "rca": _sum_rca,
    "mitigation": _sum_mitigation,
}


def summarize_artifact(kind: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Compact one raw artifact into its bundle summary (never raises)."""
    fn = _SUMMARIZERS.get(kind)
    data = _as_dict(data)
    if fn is None:
        return {"keys": sorted(k for k in data.keys() if k != "schema")}
    try:
        return fn(data)
    except Exception:  # noqa: BLE001 - degrade, never raise
        return {"note": "could not summarize %s artifact" % kind,
                "keys": sorted(k for k in data.keys() if k != "schema")}


# ---------------------------------------------------------------------------
# dashboard summary
# ---------------------------------------------------------------------------

def _dashboard_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    """Compact summary of a stored dashboard row."""
    data = _as_dict(row.get("data"))
    panels = _as_list(data.get("panels"))
    families = sorted({
        str(_as_dict(t.get("datasource")).get("type") or "")
        for p in panels if isinstance(p, dict)
        for t in _as_list(p.get("targets"))
        if isinstance(t, dict) and _as_dict(t.get("datasource")).get("type")
    })
    return {
        "slug": row.get("slug") or "",
        "title": row.get("title") or data.get("title") or "",
        "nr_source": row.get("source") or "",
        "nr_guid": row.get("nr_guid") or "",
        "panel_count": len(panels),
        "datasource_families": families,
        "updated_at": row.get("updated_at") or "",
    }


def _conf_rank(conf: Any) -> int:
    return _CONF_RANK.get(str(conf or "").strip().lower(), 2)


def _widget_title(w: Dict[str, Any]) -> str:
    return _trunc(w.get("widget") or w.get("panel_title")
                  or w.get("title") or "", 120)


def _closest_equivalent(w: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The SEAM-REPORT closest_equivalent of a widget row, compacted to
    {datasource, example_query|cw_target, note}; None when absent."""
    ce = w.get("closest_equivalent")
    if isinstance(ce, str) and ce.strip():
        return {"note": _trunc(ce, _MAX_STR)}
    if not isinstance(ce, dict) or not ce:
        return None
    out: Dict[str, Any] = {}
    for k in ("datasource", "example_query", "note"):
        v = ce.get(k)
        if v not in (None, "", [], {}):
            out[k] = _trunc(v, _MAX_STR) if isinstance(v, str) else v
    if isinstance(ce.get("cw_target"), dict) and ce["cw_target"]:
        out["cw_target"] = ce["cw_target"]
    return out or None


def _metric_kind(w: Dict[str, Any]) -> Any:
    """metric_kind per SEAM-REPORT: a {metric: kind} map or a bare kind
    string; anything else is dropped."""
    mk = w.get("metric_kind")
    if isinstance(mk, dict) and mk:
        return {str(k): str(v) for k, v in sorted(mk.items())}
    if isinstance(mk, str) and mk:
        return mk
    return None


def _missing_family(w: Dict[str, Any]) -> str:
    md = w.get("missing_datasource")
    return str(md) if isinstance(md, str) and md else ""


def _is_manual(w: Dict[str, Any]) -> bool:
    conf = str(w.get("confidence") or "").strip().lower()
    return bool(w.get("manual")) or conf == "untranslatable"


def _panel_translations(widgets: Any) -> List[Dict[str, Any]]:
    """Compact per-panel migration hints from the widget-report.

    Surfaces only the panels that need attention -- a non-exact
    converter confidence, any translation notes, or a missing
    datasource -- carrying their original_nrql and translation_notes
    plus the SEAM-REPORT fields (metric_kind, closest_equivalent,
    missing_datasource, manual) so the AI copilot can translate or
    improve them. Deterministic: worst confidence first, then
    panel_id, then title; capped and truncated for compactness.
    """
    rows: List[Dict[str, Any]] = []
    for w in _as_list(widgets):
        if not isinstance(w, dict):
            continue
        conf = str(w.get("confidence") or "").strip()
        notes = [_trunc(str(n), 200)
                 for n in _as_list(w.get("notes")) if n]
        nrql = [_trunc(str(q), _MAX_STR)
                for q in _as_list(w.get("nrql")) if q]
        missing = _missing_family(w)
        if conf.lower() in ("exact", "") and not notes and not missing:
            continue
        row: Dict[str, Any] = {
            "panel_id": w.get("panel_id"),
            "panel": _widget_title(w),
            "confidence": conf,
            "original_nrql": nrql,
            "translation_notes": notes,
        }
        mk = _metric_kind(w)
        if mk is not None:
            row["metric_kind"] = mk
        ce = _closest_equivalent(w)
        if ce:
            row["closest_equivalent"] = ce
        if missing:
            row["missing_datasource"] = missing
        if _is_manual(w):
            row["manual"] = True
        rv = [str(v) for v in _as_list(w.get("render_vars")) if v]
        if rv:
            row["render_vars"] = rv[:TOP]
        for flag in ("k8s_mapped", "cloudwatch"):
            if w.get(flag):
                row[flag] = True
        rows.append(row)
    rows.sort(key=lambda r: (_conf_rank(r.get("confidence")),
                             str(r.get("panel_id")), str(r.get("panel"))))
    return rows[:_MAX_LIST]


# ---------------------------------------------------------------------------
# SEAM-REPORT: what is missing before the dashboard works
# ---------------------------------------------------------------------------

_VAR_REF_RE = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$")

# Datasource family -> Grafana plugin type when the requirements entry
# carries none (cloudwatch is the 1.11 family for aws.* metrics).
_FAMILY_PLUGIN = {
    "prometheus": "prometheus", "mimir": "prometheus", "loki": "loki",
    "tempo": "tempo", "cloudwatch": "cloudwatch",
    "newrelic": "nrgrafanaplugin-newrelic-datasource",
}


def _default_templates() -> Dict[str, Any]:
    """DS_TEMPLATES from grafana.live, or {} when not importable."""
    try:
        from .grafana.live import DS_TEMPLATES
        return dict(DS_TEMPLATES)
    except Exception:  # noqa: BLE001 - sibling mid-build
        return {}


def _is_var_ref(uid: Any) -> bool:
    return not uid or bool(_VAR_REF_RE.match(str(uid)))


def add_datasource_template(family: str, plugin_id: str = "",
                            templates: Optional[Dict[str, Any]] = None) \
        -> Dict[str, Any]:
    """The exact add-datasource template for one missing family: the
    fields to fill (placeholders only, never values), the HTTP API body,
    the MCP ``add_datasource`` call and the CLI command. Secret fields
    are listed by name and rendered as ``<SECRET>``."""
    plugin_id = plugin_id or _FAMILY_PLUGIN.get(family, family)
    templates = templates if templates is not None else _default_templates()
    tpl = _as_dict(templates.get(plugin_id))
    name = str(tpl.get("label") or family).split("/")[0].strip() or family
    fields: List[Dict[str, Any]] = []
    values: Dict[str, Any] = {}
    for f in _as_list(tpl.get("fields")):
        if not isinstance(f, dict) or not f.get("name"):
            continue
        fname = str(f["name"])
        secret = bool(f.get("secret"))
        placeholder = "" if secret else str(f.get("placeholder") or "")
        fields.append({"name": fname,
                       "label": str(f.get("label") or fname),
                       "required": bool(f.get("required")),
                       "secret": secret,
                       "placeholder": placeholder})
        if f.get("required") or fname == "url":
            values[fname] = "<SECRET>" if secret else (
                placeholder or "<%s>" % fname.upper())
    if not fields:  # unknown plugin: the url is the universal field
        fields.append({"name": "url", "label": "URL", "required": True,
                       "secret": False, "placeholder": ""})
        values["url"] = "<URL>"
    cli = ("nr2grafana grafana add-datasource --type %s --name %s"
           % (plugin_id, name))
    for k, v in values.items():
        cli += " --set %s=%s" % (k, v)
    out: Dict[str, Any] = {
        "family": family,
        "type": plugin_id,
        "name": name,
        "fields": fields,
        "api": {"method": "POST", "path": "/api/grafana/datasource",
                "body": {"type": plugin_id, "name": name,
                         "values": dict(values)}},
        "mcp": {"tool": "add_datasource",
                "arguments": dict({"type": plugin_id, "name": name},
                                  **values)},
        "cli": cli,
    }
    if tpl.get("notes"):
        out["notes"] = _trunc(str(tpl["notes"]), 240)
    return out


def missing_report(requirements: Any, widgets: Any,
                   templates: Optional[Dict[str, Any]] = None,
                   bound_refs: Any = None,
                   instance_types: Any = None,
                   cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """SEAM-REPORT: exactly what is still missing before a converted
    dashboard shows data, so an AI knows what to add.

    ``requirements`` is the requirements artifact (its ``datasources``
    rows and, when the requirements agent produced one, its
    ``missing_datasources`` summary); ``widgets`` the widget-report
    rows. ``bound_refs`` is the set of ``${var}`` refs / variable names
    a live Grafana resolved (GrafanaLive.resolve_ds_map keys) and
    ``instance_types`` the set of datasource plugin types present on
    that instance; both None when no Grafana connection exists, in
    which case every ``${var}`` reference counts as unbound (nothing
    has been bound yet -- F8). Returns::

        {"missing_datasources": [family, ...],
         "datasources_to_add": [{family, plugin_id, panel_ids, uid_ref,
                                 reason, template}],
         "manual_panels": [{panel_id, title, why, closest_equivalent,
                            original_nrql}],
         "needs_review": [{panel_id, title, why, closest_equivalent,
                           metric_kind, missing_datasource}],
         "counts": {...}, "ready": bool}

    Pure and deterministic; never raises on odd shapes.
    """
    reqs = _as_dict(requirements)
    cfg = _as_dict(cfg)
    bound = set(str(b) for b in bound_refs) if bound_refs else set()
    types = (set(str(t) for t in instance_types)
             if instance_types is not None else None)
    fam_plugin = dict(_FAMILY_PLUGIN)
    for family, spec in _as_dict(cfg.get("datasources")).items():
        if isinstance(spec, dict) and spec.get("type"):
            fam_plugin[str(family)] = str(spec["type"])

    to_add: Dict[str, Dict[str, Any]] = {}

    def need(family: str, plugin_id: str, panel_ids: List[Any],
             uid_ref: str, reason: str) -> None:
        entry = to_add.setdefault(family, {
            "family": family, "plugin_id": plugin_id,
            "panel_ids": [], "uid_ref": uid_ref, "reason": reason})
        for pid in panel_ids:
            if pid not in entry["panel_ids"]:
                entry["panel_ids"].append(pid)

    for d in _as_list(reqs.get("datasources")):
        if not isinstance(d, dict):
            continue
        family = str(d.get("family") or d.get("type") or "")
        if not family or d.get("required") is False:
            continue
        plugin_id = str(d.get("plugin_id") or fam_plugin.get(family)
                        or family)
        uid_ref = str(d.get("uid_ref") or d.get("uid") or "")
        pids = [p for p in _as_list(d.get("panel_ids"))]
        if types is not None:
            if plugin_id in types or (uid_ref and uid_ref in bound):
                continue
            need(family, plugin_id, pids, uid_ref,
                 "no %s datasource exists on the Grafana instance"
                 % plugin_id)
        elif _is_var_ref(uid_ref) and uid_ref not in bound:
            need(family, plugin_id, pids, uid_ref,
                 "datasource ref %s is not bound to a concrete uid "
                 "(no Grafana connection to resolve it)"
                 % (uid_ref or "(none)"))

    # The requirements agent's own summary (SEAM-REPORT) wins by union.
    for m in _as_list(reqs.get("missing_datasources")):
        if isinstance(m, dict):
            family = str(m.get("family") or "")
            plugin_id = str(m.get("plugin_id") or fam_plugin.get(family)
                            or family)
            reason = str(m.get("reason") or m.get("note") or
                         "required but no uid bound")
            pids = _as_list(m.get("panel_ids"))
        else:
            family = str(m or "")
            plugin_id = fam_plugin.get(family, family)
            reason = "required but no uid bound"
            pids = []
        if family and (types is None or plugin_id not in types):
            need(family, plugin_id, pids, "", reason)

    manual: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []
    for w in _as_list(widgets):
        if not isinstance(w, dict):
            continue
        fam = _missing_family(w)
        if fam and (types is None or fam_plugin.get(fam, fam) not in types):
            plugin_id = fam_plugin.get(fam, fam)
            need(fam, plugin_id, [w.get("panel_id")], "",
                 "no %s datasource exists on the Grafana instance "
                 "(panel needs it)" % plugin_id if types is not None
                 else "panel needs the %s datasource (no uid bound)"
                 % fam)
        notes = [str(n) for n in _as_list(w.get("notes")) if n]
        why = _trunc("; ".join(notes), _MAX_STR) if notes else ""
        conf = str(w.get("confidence") or "").strip().lower()
        if _is_manual(w):
            manual.append({
                "panel_id": w.get("panel_id"),
                "title": _widget_title(w),
                "why": why or "no LGTM translation for this widget",
                "closest_equivalent": _closest_equivalent(w),
                "original_nrql": [_trunc(str(q), _MAX_STR)
                                  for q in _as_list(w.get("nrql")) if q],
            })
        elif conf == "needs-review":
            row: Dict[str, Any] = {
                "panel_id": w.get("panel_id"),
                "title": _widget_title(w),
                "why": why or "approximate translation; verify against "
                              "live data",
                "closest_equivalent": _closest_equivalent(w),
            }
            mk = _metric_kind(w)
            if mk is not None:
                row["metric_kind"] = mk
            if fam:
                row["missing_datasource"] = fam
            review.append(row)

    def _pkey(r: Dict[str, Any]):
        return (str(r.get("panel_id")), r.get("title") or "")
    manual.sort(key=_pkey)
    review.sort(key=_pkey)
    rows = []
    for family in sorted(to_add):
        entry = to_add[family]
        entry["panel_ids"] = sorted(
            (p for p in entry["panel_ids"] if p is not None), key=str)
        entry["template"] = add_datasource_template(
            family, entry["plugin_id"], templates)
        rows.append(entry)
    families = [r["family"] for r in rows]
    return {
        "missing_datasources": families,
        "datasources_to_add": rows,
        "manual_panels": manual,
        "needs_review": review,
        "counts": {"missing_datasources": len(families),
                   "manual_panels": len(manual),
                   "needs_review": len(review)},
        "ready": not families and not manual,
    }


def _grafana_note(grafana: Any) -> Optional[Dict[str, Any]]:
    """Non-secret note about the live Grafana target, if provided."""
    if grafana is None:
        return None
    base = getattr(grafana, "base", None) or getattr(grafana, "url", None)
    if not base:
        return None
    # Strip any userinfo (user:pass@) from the URL defensively.
    safe = re.sub(r"://[^/@]*@", "://", str(base))
    return {"base_url": safe}


# ---------------------------------------------------------------------------
# build_context
# ---------------------------------------------------------------------------

def _compact_missing(missing: Any) -> Optional[Dict[str, Any]]:
    """Cap the SEAM-REPORT lists for the bundle (the API route returns
    them in full); keep the exact add-datasource templates."""
    src = _as_dict(missing)
    if not src:
        return None
    out: Dict[str, Any] = {
        "missing_datasources": [str(f) for f in
                                _as_list(src.get("missing_datasources"))],
        "datasources_to_add": [
            d for d in _as_list(src.get("datasources_to_add"))
            if isinstance(d, dict)][:_MAX_LIST],
        "manual_panels": [
            m for m in _as_list(src.get("manual_panels"))
            if isinstance(m, dict)][:_MAX_LIST],
        "needs_review": [
            r for r in _as_list(src.get("needs_review"))
            if isinstance(r, dict)][:_MAX_LIST],
    }
    counts = _as_dict(src.get("counts"))
    out["counts"] = counts or {
        "missing_datasources": len(out["missing_datasources"]),
        "manual_panels": len(_as_list(src.get("manual_panels"))),
        "needs_review": len(_as_list(src.get("needs_review")))}
    out["ready"] = bool(src.get("ready", not out["missing_datasources"]
                                and not out["manual_panels"]))
    return out


def build_context(store, slug: str = "", include: Optional[List[str]] = None,
                  grafana: Any = None, deepdive: Any = None,
                  redact: bool = True,
                  missing: Any = None) -> Dict[str, Any]:
    """Assemble the AI context bundle (schema ``nr2grafana/ai-context/v1``).

    ``store`` is a :class:`~nr2grafana.store.Store` (or None). ``slug``
    selects the dashboard; when empty, the first stored dashboard is
    used if any. ``include`` optionally restricts which artifact kinds
    are folded in (default: all available). ``grafana`` is an optional
    live client used only for a non-secret target note. ``deepdive`` is
    an optional pre-computed deep-dive artifact (and may carry a nested
    ``packing`` result); when omitted, both are read from the store.
    ``missing`` is an optional pre-computed :func:`missing_report`
    (the web/MCP layer passes one resolved against the live Grafana);
    when omitted it is derived offline from the stored requirements +
    widget-report. ``redact`` scrubs secret-looking values from the
    whole bundle.

    The bundle is compact (summaries + top-N) and stable (deterministic
    ordering, no wall-clock timestamp) so identical inputs yield an
    identical bundle.
    """
    include_set = None
    if include is not None:
        include_set = set(include)

    # Resolve the dashboard row.
    dash_row = None
    if store is not None:
        try:
            if slug:
                dash_row = store.get_dashboard(slug)
            else:
                listed = store.list_dashboards() or []
                if listed:
                    slug = listed[0].get("slug") or ""
                    if slug:
                        dash_row = store.get_dashboard(slug)
        except Exception:  # noqa: BLE001 - store errors never crash us
            dash_row = None

    dashboard = _dashboard_summary(dash_row) if dash_row else None

    # Gather raw artifacts (explicit args win over the store).
    raw: Dict[str, Any] = {}
    explicit_deep = _as_dict(deepdive) if isinstance(deepdive, dict) else {}
    if explicit_deep:
        # A deepdive arg may bundle a nested packing result.
        packing_nested = explicit_deep.pop("packing", None) \
            if "packing" in explicit_deep else None
        raw["deepdive"] = explicit_deep
        if isinstance(packing_nested, dict):
            raw["packing"] = packing_nested

    if store is not None and slug:
        for kind in ARTIFACT_ORDER:
            if kind in raw:
                continue
            try:
                art = store.get_artifact(slug, kind)
            except Exception:  # noqa: BLE001
                art = None
            if isinstance(art, dict) and art:
                raw[kind] = art

    # Summarize in fixed order, honoring the include filter.
    artifacts: Dict[str, Any] = {}
    for kind in ARTIFACT_ORDER:
        if kind not in raw:
            continue
        if include_set is not None and kind not in include_set:
            continue
        artifacts[kind] = summarize_artifact(kind, raw[kind])

    available = [k for k in ARTIFACT_ORDER if k in artifacts]
    missing_kinds = [k for k in ARTIFACT_ORDER if k not in artifacts]

    # Per-panel migration hints (original_nrql + translation_notes) for
    # the panels the converter flagged, read from the widget-report.
    translations: List[Dict[str, Any]] = []
    widgets: List[Any] = []
    if store is not None and slug:
        try:
            wr = store.get_artifact(slug, "widget-report")
        except Exception:  # noqa: BLE001 - store errors never crash us
            wr = None
        if isinstance(wr, dict):
            widgets = _as_list(wr.get("widgets"))
            translations = _panel_translations(widgets)

    # SEAM-REPORT "what to add": the caller's live-resolved report wins;
    # otherwise derive it offline (every ${var} ref counts as unbound).
    what_to_add = _compact_missing(missing)
    if what_to_add is None and dash_row is not None and (
            raw.get("requirements") or widgets):
        try:
            what_to_add = _compact_missing(missing_report(
                raw.get("requirements"), widgets))
        except Exception:  # noqa: BLE001 - never break the bundle
            what_to_add = None

    context: Dict[str, Any] = {
        "schema": SCHEMA,
        "preamble": PREAMBLE,
        "legend": dict(LEGEND),
        "dashboard": dashboard,
        "available_artifacts": available,
        "missing_artifacts": missing_kinds,
        "artifacts": artifacts,
    }
    if translations:
        context["translations"] = translations
    if what_to_add is not None:
        context["missing"] = what_to_add
    gnote = _grafana_note(grafana)
    if gnote:
        context["grafana"] = gnote

    if redact:
        context = redact_context(context)
    return context


def redact_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the bundle with secret-looking values stripped."""
    return redact(context)


# ---------------------------------------------------------------------------
# markdown rendering
# ---------------------------------------------------------------------------

def _kv_lines(prefix: str, data: Dict[str, Any]) -> List[str]:
    """Render a flat dict as ``- key: value`` bullet lines."""
    out = []
    for k in data:
        v = data[k]
        if v is None or v == "" or v == [] or v == {}:
            continue
        out.append("%s- %s: %s" % (prefix, k, _fmt_scalar(v)))
    return out


def _fmt_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float, str)):
        return str(v)
    if isinstance(v, list):
        parts = []
        for item in v[:_MAX_LIST]:
            if isinstance(item, dict):
                parts.append("{" + _fmt_scalar(item) + "}")
            else:
                parts.append(_fmt_scalar(item))
        return ", ".join(parts)
    if isinstance(v, dict):
        return ", ".join("%s=%s" % (ik, _fmt_scalar(v[ik])) for ik in v
                         if v[ik] not in (None, "", [], {}))
    return str(v)


def _md_findings(rows: List[Dict[str, Any]]) -> List[str]:
    """Render a list-of-dict findings block as compact bullets."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            out.append("- %s" % r)
            continue
        head_bits = [str(r[k]) for k in ("severity", "verdict", "area",
                                         "family", "kind")
                     if r.get(k)]
        title = (r.get("title") or r.get("problem") or r.get("panel")
                 or r.get("detail") or "")
        head = " ".join(head_bits)
        line = "- "
        if head:
            line += "[%s] " % head
        line += str(title)
        out.append(line.rstrip())
        detail_bits = []
        for k in ("detail", "fix", "rationale", "refId", "nr",
                  "grafana", "panel_id"):
            if r.get(k) and k not in ("title", "problem", "panel"):
                detail_bits.append("%s: %s" % (k, r[k]))
        if r.get("est_savings"):
            detail_bits.append("est_savings: %s"
                               % _fmt_scalar(r["est_savings"]))
        if r.get("risk"):
            detail_bits.append("risk: %s" % _fmt_scalar(r["risk"]))
        if r.get("config_targets"):
            detail_bits.append("config: %s"
                               % _fmt_scalar(r["config_targets"]))
        if r.get("evidence"):
            detail_bits.append("evidence: %s"
                               % _fmt_scalar(r["evidence"]))
        for bit in detail_bits:
            out.append("  - %s" % bit)
    return out


def _md_artifact(kind: str, summary: Dict[str, Any]) -> List[str]:
    """Render one artifact summary as a markdown section body."""
    lines: List[str] = []
    # Sections that are primarily a findings/rows list.
    list_keys = {
        "diagnosis": "top_findings",
        "optimize": "top_recommendations",
        "deepdive": "top_findings",
    }
    if kind in list_keys:
        # Emit the scalar summary first, then the findings list.
        for k in summary:
            if k == list_keys[kind]:
                continue
            v = summary[k]
            if isinstance(v, dict) and v:
                lines.append("- %s: %s" % (k, _fmt_scalar(v)))
            elif v not in (None, "", [], {}):
                lines.append("- %s: %s" % (k, _fmt_scalar(v)))
        rows = _as_list(summary.get(list_keys[kind]))
        if rows:
            lines.extend(_md_findings(rows))
        return lines
    if kind == "parity":
        lines.extend(_kv_lines("", {
            "score": summary.get("score"),
            "panels": summary.get("panels"),
            "verdicts": summary.get("verdicts"),
        }))
        rows = _as_list(summary.get("worst_panels"))
        if rows:
            lines.append("- worst panels:")
            lines.extend("  " + ln for ln in _md_findings(rows))
        return lines
    if kind == "samples":
        lines.append("- panels: %s" % summary.get("panels"))
        rows = _as_list(summary.get("divergent_panels"))
        if rows:
            lines.append("- divergent panels:")
            lines.extend("  " + ln for ln in _md_findings(rows))
        return lines
    if kind == "cost":
        lines.append("- monthly_total_usd: %s"
                     % summary.get("monthly_total_usd"))
        for c in _as_list(summary.get("components")):
            lines.append("- %s: $%s/mo"
                         % (c.get("component") or c.get("family"),
                            c.get("monthly_usd")))
        res = _as_dict(summary.get("resources"))
        if res:
            lines.append("- resources: %s" % _fmt_scalar(res))
        return lines
    if kind == "packing":
        return _kv_lines("", summary)
    if kind == "flowlogs":
        for k in summary:
            if k in ("drivers", "top_flows"):
                continue
            v = summary[k]
            if v not in (None, "", [], {}):
                lines.append("- %s: %s" % (k, _fmt_scalar(v)))
        for d in _as_list(summary.get("drivers")):
            lines.append("- driver: %s" % _fmt_scalar(d))
        for f in _as_list(summary.get("top_flows")):
            lines.append("- flow: %s" % _fmt_scalar(f))
        return lines
    if kind == "rca":
        for k in ("incident", "confidence", "evidence_convergence"):
            if summary.get(k) not in (None, "", [], {}):
                lines.append("- %s: %s" % (k, _fmt_scalar(summary[k])))
        dom = _as_dict(summary.get("dominant"))
        if dom:
            lines.append("- dominant cause: %s" % _fmt_scalar(dom))
        for s in _as_list(summary.get("secondary")):
            lines.append("- secondary: %s" % _fmt_scalar(s))
        for r in _as_list(summary.get("ruled_out")):
            lines.append("- ruled_out: %s" % _fmt_scalar(r))
        return lines
    if kind == "mitigation":
        if isinstance(summary.get("summary"), dict) and summary["summary"]:
            lines.append("- summary: %s" % _fmt_scalar(summary["summary"]))
        for k in ("total_savings", "total_est_savings"):
            if summary.get(k) not in (None, "", [], {}):
                lines.append("- %s: %s" % (k, _fmt_scalar(summary[k])))
        for m in _as_list(summary.get("mitigations")):
            lines.append("- mitigation: %s" % _fmt_scalar(m))
        return lines
    # requirements + any generic summary: flat bullets.
    return _kv_lines("", summary)


WHAT_TO_ADD_HEADING = "## What to add before this dashboard works"


def _md_what_to_add(what: Dict[str, Any]) -> List[str]:
    """Render the SEAM-REPORT section: missing datasources with their
    exact add-datasource template, [MANUAL] panels with WHY + closest
    equivalent, and the needs-review panels."""
    out: List[str] = [WHAT_TO_ADD_HEADING]
    families = _as_list(what.get("missing_datasources"))
    rows = _as_list(what.get("datasources_to_add"))
    if not families and not rows:
        out.append("- datasources: all required families are bound")
    else:
        out.append("- missing datasources: %s"
                   % ", ".join(str(f) for f in families))
    for d in rows:
        if not isinstance(d, dict):
            continue
        tpl = _as_dict(d.get("template"))
        line = "- add datasource %s (type %s)" % (
            d.get("family"), d.get("plugin_id") or tpl.get("type"))
        if d.get("reason"):
            line += ": %s" % d["reason"]
        out.append(line)
        pids = _as_list(d.get("panel_ids"))
        if pids:
            out.append("  - panels: %s"
                       % ", ".join(str(p) for p in pids[:_MAX_LIST]))
        api = _as_dict(tpl.get("api"))
        if api:
            out.append("  - api: %s %s %s" % (
                api.get("method"), api.get("path"),
                json.dumps(api.get("body"), sort_keys=True)))
        mcp = _as_dict(tpl.get("mcp"))
        if mcp:
            out.append("  - mcp: %s %s" % (
                mcp.get("tool"),
                json.dumps(mcp.get("arguments"), sort_keys=True)))
        if tpl.get("cli"):
            out.append("  - cli: %s" % tpl["cli"])
        fields = [f for f in _as_list(tpl.get("fields"))
                  if isinstance(f, dict)]
        if fields:
            out.append("  - fields: %s" % ", ".join(
                "%s%s%s" % (f.get("name"),
                            " (required)" if f.get("required") else "",
                            " (secret)" if f.get("secret") else "")
                for f in fields))
        if tpl.get("notes"):
            out.append("  - note: %s" % tpl["notes"])
    manual = _as_list(what.get("manual_panels"))
    counts = _as_dict(what.get("counts"))
    if manual:
        out.append("- manual panels (%s): honest [MANUAL] placeholders; "
                   "build each by hand from the closest equivalent"
                   % (counts.get("manual_panels") or len(manual)))
        for m in manual:
            if not isinstance(m, dict):
                continue
            out.append("  - panel %s %s: %s"
                       % (m.get("panel_id"), m.get("title") or "",
                          m.get("why") or ""))
            if m.get("closest_equivalent"):
                out.append("    - closest_equivalent: %s"
                           % _fmt_scalar(m["closest_equivalent"]))
    review = _as_list(what.get("needs_review"))
    if review:
        out.append("- needs review (%s): verify against live data"
                   % (counts.get("needs_review") or len(review)))
        for r in review:
            if not isinstance(r, dict):
                continue
            line = "  - panel %s %s: %s" % (
                r.get("panel_id"), r.get("title") or "", r.get("why") or "")
            out.append(line)
            if r.get("closest_equivalent"):
                out.append("    - closest_equivalent: %s"
                           % _fmt_scalar(r["closest_equivalent"]))
    return out


def to_markdown(context: Dict[str, Any]) -> str:
    """Render the bundle as compact, LLM-optimized markdown.

    One section per artifact, preceded by the task preamble, the
    dashboard summary and a short field legend. Deterministic order.
    """
    context = _as_dict(context)
    out: List[str] = []
    out.append("# nr2grafana AI context")
    out.append("")
    out.append(str(context.get("preamble") or PREAMBLE))
    out.append("")

    dash = _as_dict(context.get("dashboard"))
    out.append("## Dashboard")
    if dash:
        out.extend(_kv_lines("", dash))
    else:
        out.append("- (no converted dashboard in scope)")
    gnote = _as_dict(context.get("grafana"))
    if gnote:
        out.append("- grafana_target: %s" % gnote.get("base_url"))
    out.append("")

    what = _as_dict(context.get("missing"))
    if what:
        out.extend(_md_what_to_add(what))
        out.append("")

    translations = _as_list(context.get("translations"))
    if translations:
        out.append("## Panel translations to review")
        for t in translations:
            if not isinstance(t, dict):
                continue
            head = "- panel %s %s [%s]" % (
                t.get("panel_id"), t.get("panel") or "",
                t.get("confidence") or "")
            if t.get("manual"):
                head += " MANUAL"
            out.append(head.rstrip())
            for q in _as_list(t.get("original_nrql")):
                out.append("  - original_nrql: %s" % q)
            for n in _as_list(t.get("translation_notes")):
                out.append("  - note: %s" % n)
            if t.get("metric_kind"):
                out.append("  - metric_kind: %s"
                           % _fmt_scalar(t["metric_kind"]))
            if t.get("missing_datasource"):
                out.append("  - missing_datasource: %s"
                           % t["missing_datasource"])
            if t.get("closest_equivalent"):
                out.append("  - closest_equivalent: %s"
                           % _fmt_scalar(t["closest_equivalent"]))
        out.append("")

    avail = _as_list(context.get("available_artifacts"))
    out.append("## Artifacts available")
    out.append("- present: %s" % (", ".join(avail) or "none"))
    missing = _as_list(context.get("missing_artifacts"))
    if missing:
        out.append("- missing: %s" % ", ".join(missing))
    out.append("")

    legend = _as_dict(context.get("legend"))
    if legend:
        out.append("## Legend")
        for k in legend:
            out.append("- **%s**: %s" % (k, legend[k]))
        out.append("")

    artifacts = _as_dict(context.get("artifacts"))
    for kind in ARTIFACT_ORDER:
        if kind not in artifacts:
            continue
        out.append("## %s" % kind)
        body = _md_artifact(kind, _as_dict(artifacts[kind]))
        if body:
            out.extend(body)
        else:
            out.append("- (empty)")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# prompt + troubleshoot
# ---------------------------------------------------------------------------

_TROUBLESHOOT_SYSTEM = (
    "You are an expert SRE troubleshooting a New Relic -> Grafana/LGTM "
    "migration and the LGTM observability stack. Answer using ONLY the "
    "provided context; if something is not in it, say so and name the "
    "artifact that would answer it. Every optimization you suggest must "
    "state its risk to durability, availability and performance and "
    "default to the safe option -- never trade RF, retention, "
    "zone-awareness or scrape interval for cost without a loud caveat.")

_DEFAULT_QUESTION = (
    "Given this context, what are the highest-priority issues to fix "
    "for a successful migration and a healthy, cost-efficient stack, "
    "and what is the safe next step for each?")


def to_prompt(context: Dict[str, Any], question: str = "") -> str:
    """Render a single ready-to-send prompt string from the bundle."""
    body = to_markdown(context)
    q = (question or "").strip() or _DEFAULT_QUESTION
    return "%s\n\n## Question\n%s\n" % (body, q)


def _backend_name(assistant: Any) -> str:
    """Human-readable backend label for a duck-typed assistant."""
    if assistant is None:
        return "none"
    cls = type(assistant).__name__
    mapping = {"AIAssist": "anthropic-api", "LocalAgent": "local-agent"}
    return mapping.get(cls, cls or "unknown")


def troubleshoot(assistant: Any, context: Dict[str, Any],
                 question: str = "") -> Dict[str, Any]:
    """Feed the bundle + question to a duck-typed AI backend.

    ``assistant`` is any object exposing ``.chat(messages, system)`` and
    ``.available`` (AIAssist / LocalAgent). Returns
    ``{"answer", "backend"}`` and NEVER raises: a missing or failing
    backend becomes an actionable answer string instead of an
    exception.
    """
    backend = _backend_name(assistant)
    if assistant is None or not getattr(assistant, "available", False):
        return {
            "answer": ("No AI backend is configured. Set "
                       "ANTHROPIC_API_KEY for the Claude API, or "
                       "configure a local console agent command (e.g. "
                       "\"claude -p {prompt}\"), then retry. The "
                       "context bundle is ready to paste into any AI "
                       "manually in the meantime."),
            "backend": "none",
        }
    prompt = to_prompt(context, question)
    try:
        reply = assistant.chat([{"role": "user", "content": prompt}],
                               system=_TROUBLESHOOT_SYSTEM)
    except Exception as e:  # noqa: BLE001 - AI errors -> actionable text
        return {
            "answer": ("The AI backend (%s) could not answer: %s. The "
                       "context bundle is intact -- retry, switch "
                       "backend, or paste it into an AI manually."
                       % (backend, e)),
            "backend": backend,
        }
    text = (reply or "").strip()
    if not text:
        return {
            "answer": ("The AI backend (%s) returned an empty reply; "
                       "retry or switch backend." % backend),
            "backend": backend,
        }
    return {"answer": text, "backend": backend}


# ---------------------------------------------------------------------------
# cost-anomaly RCA + mitigation AI flow
# ---------------------------------------------------------------------------

_RCA_SYSTEM = (
    "You are an SRE + FinOps engineer performing ROOT-CAUSE ANALYSIS of an "
    "AWS cost anomaly and proposing a RELIABILITY-SAFE mitigation, using the "
    "converged read-only evidence in the bundle (cost-explorer, VPC flow "
    "logs, EKS control plane, LGTM self-metrics, CloudTrail). Rules you MUST "
    "honor: treat a *DataTransfer-Regional-Bytes (or *InterZone*) usage type "
    "as CROSS-AZ NETWORK transfer, NOT storage, regardless of the service "
    "tag (EBS/EC2/...). Converge multiple independent sources before "
    "asserting a cause; give the dominant driver a % share and list "
    "secondaries and an explicit ruled-out set. Every mitigation must CUT "
    "cost WITHOUT reducing availability, durability, performance or the "
    "ability to serve the CURRENT traffic rate; state its reliability "
    "PRECONDITIONS (what must hold or it breaks something) and set keeps_* "
    "false with a loud caveat when it cannot. Never propose dropping the "
    "replication factor or retention, never CPU-limit Mimir/Loki ingesters, "
    "and never blind-disable NLB cross-zone (confirm >=1 healthy target in "
    "EVERY enabled AZ first). Keep all generated configs GENERIC and "
    "paste-ready (placeholders, no customer values); you PROPOSE changes, "
    "you never execute them (GitOps/IaC-owned). AWS access is strictly "
    "read-only. Respond with STRICT JSON only -- a single object, no prose "
    "and no markdown fences -- with exactly these keys: "
    "\"root_cause\": string, \"mitigations\": array of objects "
    "{\"title\", \"saving\", \"change\", \"preconditions\", "
    "\"keeps_availability\", \"keeps_durability\", \"keeps_performance\"}, "
    "\"config_notes\": string.")

_RCA_QUESTION = (
    "Analyze this AWS cost anomaly and the converged evidence: identify the "
    "dominant driver with its % share, the ranked secondary drivers and the "
    "ruled-out hypotheses, then propose a ranked, reliability-safe "
    "mitigation plan that cuts the cost WITHOUT reducing availability, "
    "durability, performance or the ability to serve the current traffic "
    "rate. State each mitigation's reliability preconditions and its "
    "keeps_availability/durability/performance flags.")


def _parse_rca_reply(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of the strict-JSON RCA plan from a reply.

    Strips a surrounding markdown fence, then retries on the outermost
    ``{...}`` slice. Returns the parsed object only when it carries a
    ``root_cause`` or ``mitigations`` field; otherwise None. Never raises.
    """
    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and (
                "root_cause" in data or "mitigations" in data):
            return data
    return None


def _json_candidates(text: str) -> List[str]:
    """Yield parse candidates for a possibly-fenced JSON reply."""
    out: List[str] = []
    s = (text or "").strip()
    if not s:
        return out
    if s.startswith("```"):
        lines = s.splitlines()[1:]
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    out.append(s)
    start = s.find("{")
    end = s.rfind("}")
    if 0 <= start < end:
        inner = s[start:end + 1]
        if inner != s:
            out.append(inner)
    return out


def analyze_cost(assistant: Any, context: Dict[str, Any],
                 question: str = "") -> Dict[str, Any]:
    """Run the RCA/mitigation AI flow over the context bundle.

    Frames the bundle for cost-anomaly root-cause analysis and a
    reliability-safe mitigation proposal (``_RCA_SYSTEM``) and sends it to
    a duck-typed backend (AIAssist / LocalAgent, exposing ``.chat`` and
    ``.available``). Returns ``{"answer", "backend"}`` -- plus ``"plan"``
    when the reply parses as the strict-JSON RCA object -- and NEVER
    raises: a missing or failing backend becomes actionable text.
    """
    backend = _backend_name(assistant)
    if assistant is None or not getattr(assistant, "available", False):
        return {
            "answer": ("No AI backend is configured for cost RCA. Set "
                       "ANTHROPIC_API_KEY for the Claude API, or configure "
                       "a local console agent command (e.g. "
                       "\"claude -p {prompt}\"), then retry. The RCA "
                       "context bundle is ready to paste into any AI "
                       "manually in the meantime."),
            "backend": "none",
        }
    prompt = to_prompt(context, question or _RCA_QUESTION)
    try:
        reply = assistant.chat([{"role": "user", "content": prompt}],
                               system=_RCA_SYSTEM)
    except Exception as e:  # noqa: BLE001 - AI errors -> actionable text
        return {
            "answer": ("The AI backend (%s) could not analyze the cost "
                       "anomaly: %s. The RCA context bundle is intact -- "
                       "retry, switch backend, or paste it into an AI "
                       "manually." % (backend, e)),
            "backend": backend,
        }
    text = (reply or "").strip()
    if not text:
        return {
            "answer": ("The AI backend (%s) returned an empty reply; "
                       "retry or switch backend." % backend),
            "backend": backend,
        }
    out: Dict[str, Any] = {"answer": text, "backend": backend}
    plan = _parse_rca_reply(text)
    if plan is not None:
        out["plan"] = plan
    return out
