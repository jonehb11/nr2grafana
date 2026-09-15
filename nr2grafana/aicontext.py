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

    build_context(store, slug, include, grafana, deepdive, redact=True)
        -> dict   # schema "nr2grafana/ai-context/v1"
    to_markdown(context) -> str            # section per artifact
    to_prompt(context, question="") -> str # ready single-string prompt
    troubleshoot(assistant, context, question="") -> dict
        # {"answer", "backend"} ; never raises
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

SCHEMA = "nr2grafana/ai-context/v1"

# Artifact kinds folded into the bundle, in a fixed presentation order.
ARTIFACT_ORDER = ("requirements", "diagnosis", "parity", "samples",
                  "cost", "optimize", "deepdive", "packing")

# Compactness knobs: at most this many rows per list, strings capped.
TOP = 8
_MAX_STR = 400
_MAX_LIST = 24

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


_SUMMARIZERS = {
    "requirements": _sum_requirements,
    "diagnosis": _sum_diagnosis,
    "parity": _sum_parity,
    "samples": _sum_samples,
    "cost": _sum_cost,
    "optimize": _sum_optimize,
    "deepdive": _sum_findings_generic,
    "packing": _sum_packing,
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

def build_context(store, slug: str = "", include: Optional[List[str]] = None,
                  grafana: Any = None, deepdive: Any = None,
                  redact: bool = True) -> Dict[str, Any]:
    """Assemble the AI context bundle (schema ``nr2grafana/ai-context/v1``).

    ``store`` is a :class:`~nr2grafana.store.Store` (or None). ``slug``
    selects the dashboard; when empty, the first stored dashboard is
    used if any. ``include`` optionally restricts which artifact kinds
    are folded in (default: all available). ``grafana`` is an optional
    live client used only for a non-secret target note. ``deepdive`` is
    an optional pre-computed deep-dive artifact (and may carry a nested
    ``packing`` result); when omitted, both are read from the store.
    ``redact`` scrubs secret-looking values from the whole bundle.

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
    missing = [k for k in ARTIFACT_ORDER if k not in artifacts]

    context: Dict[str, Any] = {
        "schema": SCHEMA,
        "preamble": PREAMBLE,
        "legend": dict(LEGEND),
        "dashboard": dashboard,
        "available_artifacts": available,
        "missing_artifacts": missing,
        "artifacts": artifacts,
    }
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
    # requirements + any generic summary: flat bullets.
    return _kv_lines("", summary)


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
