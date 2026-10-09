#!/usr/bin/env python3
"""Redacted golden corpus: build it from a real query map, run the current
converter over it, and score the result against the human-made targets.

The corpus (fixtures/corpus/*.json) was derived from a real 531-widget
migration in which a human remapped the converter's output panel by panel.
Every widget keeps three things: the NRQL (shape-preserving, identifiers
redacted), the converter's output AT THE TIME ("before"), and the targets
the human ended up with in Grafana ("expected"). docs/dev/ARCHITECTURE-1.11
section 0 lists the failure classes F1..F12 this corpus teaches.

Usage:
  python3 tools/corpus_tools.py build <query-map.json> [--out DIR]
  python3 tools/corpus_tools.py score [--corpus DIR] [--json]
      [--limit N] [--show F2] [--no-builder]

``build`` reads the (untrusted, never committed) source map, derives the
redaction map by scanning it for identifier-shaped tokens, rewrites every
string, and refuses to write anything if one of the original identifiers
survives. Originals exist only in memory while building.

``score`` converts every corpus widget with the CURRENT translator and
reports the confidence distribution, per-class counts F1..F12, and the
agreement with the human targets after expression normalization.
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import os
import re
import sys
from collections import Counter, OrderedDict
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEFAULT_CORPUS_DIR = os.path.join(REPO, "fixtures", "corpus")
SCHEMA = 1

# Placeholders (the only "names" the corpus may contain).
ORG = "acme"
CLUSTER_PREFIX = "acme-cluster-"
APP_DASH = "acme-backend"
APP_UNDERSCORE = "acme_backend"
HOST = "example.com"

# Python reprs leaking into a query (F1).
PY_REPR_RE = re.compile(r"\b(?:Func|Lit|Attr|InList|Cmp|BoolOp)\(")

# Contract thresholds (section 2 item 9).
NEEDS_REVIEW_MAX = 0.45
UNTRANSLATABLE_MAX = 0.08
AGREEMENT_FLOOR = 0.15      # loose target-level agreement with humans

FAILURE_CLASSES = OrderedDict([
    ("F1", "Python repr (Func(/Lit(/Attr() in an emitted query"),
    ("F2", "sum()/count() of an event metric not treated as a counter"),
    ("F3", "NR k8s.* metric not mapped to kube-state-metrics/cAdvisor"),
    ("F4", "aws.* metric emitted as PromQL instead of a CloudWatch target"),
    ("F5", "NR summary metric (.mean/.median/.percentiles) not _sum/_count"),
    ("F6", "NRQL parse failure"),
    ("F7", "Loki search/aparse/capture not translated"),
    ("F8", "${datasource} refs left unbound after binding"),
    ("F9", "entity.guid predicate not resolved to a service label"),
    ("F10", "appName 'svc (env)' kept verbatim in a label value"),
    ("F11", "FACET without LIMIT / fixed TIMESERIES bucket (notes only)"),
    ("F12", "NR-only event (FinanceSample/Deployment/...) without a "
            "closest equivalent"),
])

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_ENV_WORDS = ("dev", "prod", "production", "stage", "staging", "stg",
              "test", "qa", "uat", "sandbox", "sbx", "preprod", "nonprod",
              "np", "demo", "perf", "int", "integration")
_ENV_SUFFIX_RE = re.compile(
    r"^(?P<base>.*?)(?P<suffix>(?:-(?:%s))?(?:-[a-z]{2}-[a-z]+-\d)?)$"
    % "|".join(_ENV_WORDS), re.I)
REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d$")
# Multi-dash tokens that are generic vocabulary, not identifiers.
ALLOWED_DASH_TOKENS = frozenset([
    "kube-state-metrics", "node-exporter", "cloudwatch-exporter",
    "x-forwarded-for", "x-request-id", "x-amzn-trace-id",
    "read-only-replica", "out-of-memory", "end-to-end", "time-to-live",
    "per-bucket-count", "case-insensitive-match", "needs-review-share",
    "top-k-groups", "state-of-the-world", "point-in-time",
    "non-stream-label", "stream-label-only", "line-filter-only",
])
DASH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_$./])([A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+){2,})"
    r"(?![A-Za-z0-9_])")
LONG_DIGITS_RE = re.compile(r"(?<![A-Za-z0-9_.])\d{6,}(?![A-Za-z0-9_])")
# Round constants (unit scaling such as * 1000000) are not identifiers.
ROUND_NUMBER_RE = re.compile(r"^[1-9]\d?0{4,}$")


# Replacement numbers: same length, no leading zero (a NRQL numeric
# literal keeps its shape), obviously synthetic: 1, zeros, ordinal.
SYNTHETIC_NUMBER_RE = re.compile(r"^10{2,}\d{1,4}$")


def synthetic_number(ordinal: int, length: int) -> str:
    body = str(ordinal)
    return "1" + body.rjust(max(length - 1, len(body) + 2), "0")


def is_plain_number(num: str) -> bool:
    """Scaling constants and whole hours/minutes (604800 = 7d in seconds,
    3600000 = 1h in ms) keep query shapes; they are never identifiers."""
    if ROUND_NUMBER_RE.match(num):
        return True
    n = int(num)
    return n % 3600 == 0 or n % 60000 == 0
HOST_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:com|net|org|io|internal|local|cloud|aws|dev)\b", re.I)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I)
HEX_BLOB_RE = re.compile(r"\b[0-9a-f]{24,}\b", re.I)
# Base64-ish blobs (NR entity GUIDs, keys): 32+ chars, mixed case AND a
# digit, or any +/= padding. CamelCase metric names carry no digits.
B64_BLOB_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?=[A-Za-z0-9+/=]{32,}(?![A-Za-z0-9+/=_]))"
    r"(?:(?=[A-Za-z0-9+/=]*[+/=])|(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[a-z])"
    r"(?=[A-Za-z0-9]*[A-Z]))[A-Za-z0-9+/=]{32,}")
_GUID_CTX_RE = re.compile(
    r"(?:entity\.guid|entityGuid|\bguid)\s*(?:=|!=|IN|NOT\s+IN)\s*\(?\s*"
    r"('[^']*'(?:\s*,\s*'[^']*')*)", re.I)
ARN_RE = re.compile(r"arn:aws[a-z-]*:[^\s'\"]+")
REDACTED_MARKER_RE = re.compile(r"<REDACTED_[A-Z_]+>")
_CONCAT_ENV_RE = re.compile(
    r"concat\(\s*'([^']*)'\s*,\s*\{\{\{?\s*env\s*\}?\}\}")
_APP_ENV_RE = re.compile(
    r"concat\(\s*'([^'(]*?)\s*\('\s*,\s*\{\{\{?\s*env\s*\}?\}\}")
_CLUSTER_CTX_RE = re.compile(
    r"(?:k8s\.clusterName|clusterName|cluster_name|cluster|"
    r"instrumentation\.source)\s*(?:=|!=|IN|NOT\s+IN|LIKE|NOT\s+LIKE)\s*"
    r"\(?\s*(concat\([^)]*\)|'[^']*'(?:\s*,\s*'[^']*')*)", re.I)
_QUEUE_CTX_RE = re.compile(
    r"aws\.sqs\.QueueName\s*(?:=|!=|IN|NOT\s+IN|LIKE|NOT\s+LIKE)\s*\(?\s*"
    r"(concat\([^)]*\)|'[^']*'(?:\s*,\s*'[^']*')*)", re.I)
_ACCOUNT_CTX_RE = re.compile(
    r"(?:linkedAccountName|linkedAccountId|accountName|accountId|"
    r"aws\.accountId)\s*(?:=|!=|IN|NOT\s+IN)\s*\(?\s*"
    r"('[^']*'(?:\s*,\s*'[^']*')*)", re.I)


def iter_strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            for s in iter_strings(v):
                yield s
    elif isinstance(obj, list):
        for v in obj:
            for s in iter_strings(v):
                yield s


def mask_shape(text: str) -> str:
    """Letters -> a/A, digits -> 9: safe to print, never the value."""
    text = re.sub(r"[a-z]", "a", text)
    text = re.sub(r"[A-Z]", "A", text)
    return re.sub(r"[0-9]", "9", text)


def _sep_variants(segments: List[str]) -> List[str]:
    """Every way of joining the segments with '-', '_' or nothing."""
    if len(segments) <= 1:
        return list(segments)
    out = []
    for combo in itertools.product(("-", "_", ""), repeat=len(segments) - 1):
        text = segments[0]
        for sep, seg in zip(combo, segments[1:]):
            text += sep + seg
        out.append(text)
    return out


def _ci_pattern(literal: str) -> str:
    """Regex for ``literal`` ignoring letter case, bounded so it does not
    match inside a longer alphanumeric word."""
    body = "".join("[%s%s]" % (c.lower(), c.upper()) if c.isalpha()
                   else re.escape(c) for c in literal)
    return r"(?<![A-Za-z0-9])" + body + r"(?![a-z0-9])"


def _cased(words: List[str], matched: str) -> str:
    sep = "-" if "-" in matched else ("_" if "_" in matched else "")
    text = sep.join(words)
    if matched.isupper():
        return text.upper()
    if matched[:1].isupper():
        return text[:1].upper() + text[1:]
    return text


class RedactionMap:
    """Ordered regex rules + the raw identifiers they replace (in memory
    only, so the build can prove none survived)."""

    def __init__(self) -> None:
        self.rules: List[Tuple[Any, Any]] = []
        self.originals: List[str] = []
        self.summary: Dict[str, Any] = OrderedDict()

    def add_words(self, segments: List[str], words: List[str],
                  key: str) -> None:
        variants = sorted(set(_sep_variants(segments)), key=len,
                          reverse=True)
        for v in variants:
            if not v:
                continue
            self.originals.append(v)
            self.rules.append((re.compile(_ci_pattern(v)),
                               (lambda m, w=words: _cased(w, m.group(0)))))
        self.summary[key] = "-".join(words)

    def add_literal(self, literal: str, replacement: str) -> None:
        if not literal:
            return
        self.originals.append(literal)
        self.rules.append((re.compile(_ci_pattern(literal)),
                           (lambda m, r=replacement: r)))

    def apply(self, text: str) -> str:
        for rx, repl in self.rules:
            text = rx.sub(repl, text)
        return text


def _strip_env(token: str) -> Tuple[str, str]:
    m = _ENV_SUFFIX_RE.match(token)
    if not m:
        return token, ""
    return m.group("base"), m.group("suffix")


def _literals(group: str) -> List[str]:
    """'a', 'b' -> [a, b]; concat('x-', {{env}}, '-y') -> [x-, -y]."""
    return re.findall(r"'([^']*)'", group)


def derive_redaction_map(data: Any) -> RedactionMap:
    """Scan the raw query map for identifier-shaped tokens and build the
    rules. Nothing derived here is ever written to disk."""
    blob = "\n".join(iter_strings(data))
    rmap = RedactionMap()

    # The org/app: concat('app (', {{env}}, ')') -> appName literal.
    apps = Counter(_APP_ENV_RE.findall(blob))
    app = apps.most_common(1)[0][0] if apps else ""
    app_segs = [s for s in re.split(r"[-_]", app) if s]
    org_segs = app_segs[:-1] if len(app_segs) > 1 else app_segs
    core = set()
    for segs in (org_segs, app_segs):
        core.update(v.lower() for v in _sep_variants(segs))

    def is_core(lit: str) -> bool:
        return lit.lower().strip("-_") in core

    # Cluster prefix(es): literals compared with a cluster attribute,
    # most often concat('x-y-z-', {{env}}); env/region suffixes stripped.
    cluster_count: Counter = Counter()
    for group in _CLUSTER_CTX_RE.findall(blob):
        for lit in _literals(group):
            base, _ = _strip_env(lit.rstrip("-"))
            if base and base.count("-") >= 1 and not is_core(base):
                cluster_count[base] += 1
    clusters = [c for c, _ in cluster_count.most_common()]
    for c in clusters:
        core.update(v.lower() for v in _sep_variants(
            [s for s in c.split("-") if s]))

    queue_lits: Set[str] = set()
    for group in _QUEUE_CTX_RE.findall(blob):
        for lit in _literals(group):
            queue_lits.add(lit)
    account_lits: Set[str] = set()
    for group in _ACCOUNT_CTX_RE.findall(blob):
        for lit in _literals(group):
            account_lits.add(lit)

    # Rule order: the most specific literals first (queue/account names
    # may embed the org or app token), then clusters, then the org.
    n_queue = 0
    for lit in sorted(queue_lits):
        base, suffix = _strip_env(lit.rstrip("-"))
        if not base or is_core(base) or base.startswith("-"):
            continue
        n_queue += 1
        rmap.add_literal(base, "acme-queue-%d" % n_queue)
    rmap.summary["queues"] = n_queue
    n_acct = 0
    seen_acct: Dict[str, str] = {}
    for lit in sorted(account_lits):
        base, suffix = _strip_env(lit)
        if not base or is_core(base):
            continue
        if base not in seen_acct:
            n_acct += 1
            seen_acct[base] = "acme-account-%d" % n_acct
            rmap.add_literal(base, seen_acct[base])
    rmap.summary["accounts"] = n_acct
    for i, prefix in enumerate(clusters):
        segs = [x for x in prefix.split("-") if x]
        words = ["acme", "cluster"] if i == 0 else \
            ["acme", "cluster%d" % (i + 1)]
        rmap.add_words(segs, words, "cluster_prefix_%d" % (i + 1))
    rmap.summary["clusters"] = len(clusters)
    if org_segs:
        rmap.add_words(org_segs, ["acme"], "org")
        rmap.summary["app"] = APP_DASH

    # Numbers that could be account ids / product codes: same-length
    # zero-padded ordinals keep the query shape.
    numbers = sorted(set(n for n in LONG_DIGITS_RE.findall(blob)
                         if not is_plain_number(n)),
                     key=lambda x: (len(x), x))
    for i, num in enumerate(numbers):
        rmap.add_literal(num, synthetic_number(i + 1, len(num)))
    rmap.summary["numbers"] = len(numbers)

    hosts = sorted(set(HOST_RE.findall(blob)))
    hosts = [h for h in hosts if h.lower() != HOST]
    for h in hosts:
        rmap.add_literal(h, HOST)
    rmap.summary["hostnames"] = len(hosts)

    for group in _GUID_CTX_RE.findall(blob):
        for lit in _literals(group):
            if lit and not REDACTED_MARKER_RE.fullmatch(lit) \
                    and not UUID_RE.fullmatch(lit):
                rmap.add_literal(lit, "<REDACTED_BLOB>")
    for rx, marker in ((ARN_RE, "<REDACTED_ARN>"),
                       (UUID_RE, "<REDACTED_GUID>"),
                       (HEX_BLOB_RE, "<REDACTED_BLOB>"),
                       (B64_BLOB_RE, "<REDACTED_BLOB>")):
        for tok in sorted(set(rx.findall(blob))):
            rmap.add_literal(tok, marker)

    # Catch-all: any other multi-dash token that is not generic vocabulary
    # becomes acme-name-<n> (env/region suffixes kept for shape).
    pass_a = rmap.apply(blob)
    leftovers: Set[str] = set()
    for tok in DASH_TOKEN_RE.findall(pass_a):
        base, _ = _strip_env(tok)
        low = base.lower()
        if low.startswith(ORG) or low in ALLOWED_DASH_TOKENS \
                or REGION_RE.match(low) or base.count("-") == 0:
            continue
        leftovers.add(base)
    for i, base in enumerate(sorted(leftovers, key=lambda s: (-len(s), s))):
        rmap.add_literal(base, "acme-name-%d" % (i + 1))
    rmap.summary["other_names"] = len(leftovers)
    return rmap


def redact(obj: Any, rmap: RedactionMap) -> Any:
    if isinstance(obj, str):
        return rmap.apply(obj)
    if isinstance(obj, dict):
        return OrderedDict((k, redact(v, rmap)) for k, v in obj.items())
    if isinstance(obj, list):
        return [redact(v, rmap) for v in obj]
    return obj


def surviving_originals(obj: Any, rmap: RedactionMap) -> List[str]:
    """Masked shapes of originals still present after redaction."""
    blob = "\n".join(iter_strings(obj)).lower()
    bad = []
    for orig in rmap.originals:
        if len(orig) < 3:
            continue
        if orig.isdigit():
            # Digit runs are identifiers only at digit boundaries (the
            # same run inside a duration such as now-864000s is not one).
            if re.search(r"(?<![a-z0-9_.])%s(?![a-z0-9_])" % orig, blob):
                bad.append(mask_shape(orig))
        elif orig.lower() in blob:
            bad.append(mask_shape(orig))
    return sorted(set(bad))


def shape_violations(text: str) -> List[str]:
    """Identifier-shaped tokens a REDACTED text must not contain. Used by
    the corpus test (which cannot know the originals) and by build."""
    out = []
    for tok in DASH_TOKEN_RE.findall(text):
        base, _ = _strip_env(tok)
        low = base.lower()
        if low.startswith(ORG) or low in ALLOWED_DASH_TOKENS \
                or REGION_RE.match(low):
            continue
        out.append("dash-token:" + tok)
    for num in LONG_DIGITS_RE.findall(text):
        if not SYNTHETIC_NUMBER_RE.match(num) and not is_plain_number(num):
            out.append("number:" + num)
    for h in HOST_RE.findall(text):
        if h.lower() != HOST:
            out.append("host:" + h)
    for rx, kind in ((UUID_RE, "uuid"), (HEX_BLOB_RE, "hex"),
                     (B64_BLOB_RE, "blob"), (ARN_RE, "arn")):
        for tok in rx.findall(text):
            out.append(kind + ":" + tok)
    for lit in _CONCAT_ENV_RE.findall(text):
        if not lit.lower().startswith(ORG):
            out.append("concat:" + lit)
    for group in _GUID_CTX_RE.findall(text):
        for lit in _literals(group):
            if lit and not REDACTED_MARKER_RE.fullmatch(lit):
                out.append("guid:" + lit)
    return out


# ---------------------------------------------------------------------------
# Corpus build / load
# ---------------------------------------------------------------------------

_DS_FAMILY = {"mimir": "prometheus", "prometheus": "prometheus",
              "loki": "loki", "cloudwatch": "cloudwatch", "tempo": "tempo"}
_CW_KEYS = ("expression", "namespace", "metricName", "statistic",
            "dimensions", "dimension_keys", "queryMode", "region",
            "metricEditorMode")


def _expected_target(live: Dict[str, Any]) -> Dict[str, Any]:
    uid = str(live.get("datasource_uid") or "")
    out: Dict[str, Any] = OrderedDict()
    out["datasource"] = _DS_FAMILY.get(uid.lower(),
                                       uid.lower() or "prometheus")
    out["refId"] = live.get("refId") or "A"
    if live.get("expr") is not None:
        out["expr"] = live["expr"]
    for k in _CW_KEYS:
        if k in live:
            out[k] = live[k]
    return out


def build_corpus(source: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    """Derive + apply the redaction map, verify, write page files."""
    if not isinstance(source, dict) or not isinstance(
            source.get("widgets"), list):
        raise ValueError("source must be a query map with a 'widgets' list")
    rmap = derive_redaction_map(source)
    pages: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    raw_pages: List[str] = []
    for w in source["widgets"]:
        page = str(w.get("page") or "Page")
        if page not in pages:
            pages[page] = []
            raw_pages.append(page)
        pages[page].append(w)
    files = []
    before_conf: Counter = Counter()
    total = 0
    written: List[Tuple[str, Any]] = []
    for pi, page in enumerate(raw_pages):
        entries = []
        for wi, w in enumerate(pages[page]):
            total += 1
            before_conf[str(w.get("confidence") or "")] += 1
            entry: Dict[str, Any] = OrderedDict()
            entry["id"] = "p%02d-w%03d" % (pi + 1, wi + 1)
            entry["title"] = str(w.get("widget") or "")
            entry["visualization"] = str(w.get("visualization") or "")
            entry["grafana_type"] = str(w.get("grafana_type") or "")
            entry["nrql"] = [str(q) for q in (w.get("nrql") or [])]
            entry["before"] = OrderedDict([
                ("confidence", str(w.get("confidence") or "")),
                ("queries", [OrderedDict([
                    ("datasource", q.get("datasource")),
                    ("type", q.get("type")),
                    ("expr", q.get("expr"))])
                    for q in (w.get("nr2grafana_queries") or [])]),
                ("notes", [str(n) for n in (w.get("notes") or [])]),
            ])
            entry["expected"] = [_expected_target(t)
                                 for t in (w.get("live_targets") or [])]
            entries.append(entry)
        doc = OrderedDict([("schema", SCHEMA), ("page", page),
                           ("widgets", entries)])
        doc = redact(doc, rmap)
        name = "page-%02d.json" % (pi + 1)
        files.append(name)
        written.append((name, doc))
    index = OrderedDict([
        ("schema", SCHEMA),
        ("description", "Redacted golden corpus: NRQL shapes from a real "
                        "%d-widget migration, the converter's output at "
                        "the time (before) and the human-made Grafana "
                        "targets (expected). All identifiers replaced by "
                        "acme-* placeholders." % total),
        ("widgets", total),
        ("pages", len(files)),
        ("files", files),
        ("before_confidence", OrderedDict(sorted(before_conf.items()))),
        ("placeholders", OrderedDict([
            ("cluster_prefix", CLUSTER_PREFIX), ("org", ORG),
            ("app", APP_DASH), ("app_metric_prefix", APP_UNDERSCORE),
            ("queues", "acme-queue-<n>"), ("accounts", "acme-account-<n>"),
            ("other_names", "acme-name-<n>"), ("hostnames", HOST)])),
        ("redaction_counts", OrderedDict(
            (k, v) for k, v in rmap.summary.items()
            if isinstance(v, int))),
    ])
    written.append(("index.json", index))

    # Prove the redaction before touching the disk.
    survivors = surviving_originals([d for _, d in written], rmap)
    if survivors:
        raise RuntimeError(
            "redaction incomplete; original identifiers survive (masked "
            "shapes): %s" % ", ".join(survivors))
    shapes: List[str] = []
    for _, d in written:
        for s in iter_strings(d):
            shapes.extend(shape_violations(s))
    if shapes:
        raise RuntimeError(
            "identifier-shaped tokens remain after redaction: %s"
            % ", ".join(sorted(set(mask_shape(s) for s in shapes))[:20]))

    os.makedirs(out_dir, exist_ok=True)
    for name, d in written:
        with open(os.path.join(out_dir, name), "w", encoding="ascii") as fh:
            json.dump(d, fh, indent=1, ensure_ascii=True)
            fh.write("\n")
    return index


def load_corpus(corpus_dir: str = DEFAULT_CORPUS_DIR) -> List[Dict[str, Any]]:
    """All widgets, in page order, each tagged with its page name."""
    index_path = os.path.join(corpus_dir, "index.json")
    if not os.path.exists(index_path):
        raise FileNotFoundError(
            "corpus index not found at %s; run 'python3 tools/corpus_tools"
            ".py build <query-map.json>' first" % index_path)
    with open(index_path, encoding="ascii") as fh:
        index = json.load(fh)
    widgets: List[Dict[str, Any]] = []
    for name in index.get("files") or []:
        with open(os.path.join(corpus_dir, name), encoding="ascii") as fh:
            doc = json.load(fh)
        for w in doc.get("widgets") or []:
            w = dict(w)
            w["page"] = doc.get("page", "")
            widgets.append(w)
    return widgets


# ---------------------------------------------------------------------------
# Case predicates (shared by the scorer and tests)
# ---------------------------------------------------------------------------

_FROM_RE = re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
_SUMMARY_RE = re.compile(
    r"\b(?:average|sum|latest|max|min)\(\s*[A-Za-z_][\w.]*\."
    r"(?:mean|median|upper|lower|percentiles|stddev|p\d\d)\s*\)", re.I)
_SUMCOUNT_METRIC_RE = re.compile(
    r"\b(?:sum|count)\(\s*([A-Za-z_][\w.]*)\s*\)")
_AWS_METRIC_RE = re.compile(r"\baws\.[a-z0-9]+\.[A-Za-z0-9]+")
_LOG_SEARCH_RE = re.compile(r"\b(?:allColumnSearch|aparse|capture)\s*\(", re.I)
_APP_ENV_LIT_RE = re.compile(
    r"appName\s*=\s*(?:'[^']* \([^')]*\)'|concat\('[^']*\(',)", re.I)


def from_event(nrql: str) -> str:
    m = _FROM_RE.search(nrql or "")
    return m.group(1).lower() if m else ""


def is_counter_case(w: Dict[str, Any]) -> bool:
    """sum()/count() of an app event metric that the human turned into a
    _total counter with increase()/rate()."""
    if not any(from_event(q) == "metric" and _SUMCOUNT_METRIC_RE.search(q)
               for q in w.get("nrql") or []):
        return False
    return any("_total[" in (e.get("expr") or "") for e in w["expected"])


def is_summary_case(w: Dict[str, Any]) -> bool:
    return any(_SUMMARY_RE.search(q) for q in w.get("nrql") or [])


def is_k8s_case(w: Dict[str, Any]) -> bool:
    return any("k8s." in q or from_event(q).startswith("k8s")
               for q in w.get("nrql") or [])


def is_aws_case(w: Dict[str, Any]) -> bool:
    return any(from_event(q) == "metric" and _AWS_METRIC_RE.search(q)
               for q in w.get("nrql") or [])


def is_log_search_case(w: Dict[str, Any]) -> bool:
    return any(from_event(q) == "log" and _LOG_SEARCH_RE.search(q)
               for q in w.get("nrql") or [])


def is_entity_guid_case(w: Dict[str, Any]) -> bool:
    return any("entity.guid" in q for q in w.get("nrql") or [])


def is_app_env_case(w: Dict[str, Any]) -> bool:
    return any(_APP_ENV_LIT_RE.search(q) for q in w.get("nrql") or [])


def is_nr_only_case(w: Dict[str, Any]) -> bool:
    return any(from_event(q) in ("financesample", "deployment",
                                 "nrconsumption", "nrdailyusage")
               for q in w.get("nrql") or [])


def has_concat_env(w: Dict[str, Any]) -> bool:
    return any(_CONCAT_ENV_RE.search(q) for q in w.get("nrql") or [])


# ---------------------------------------------------------------------------
# Running the current converter
# ---------------------------------------------------------------------------

def default_cfg() -> Dict[str, Any]:
    from nr2grafana.config import load_config
    return load_config()


def _flatten(t: Any) -> List[Any]:
    out = [t]
    for x in getattr(t, "extra", None) or []:
        out.extend(_flatten(x))
    return out


def convert_widget(w: Dict[str, Any],
                   cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Translate every NRQL of a corpus widget with the CURRENT router.
    Returns one plain dict per emitted target."""
    from nr2grafana.translate.router import translate_query
    targets: List[Dict[str, Any]] = []
    for q in w.get("nrql") or []:
        t = translate_query(q, cfg)
        for i, part in enumerate(_flatten(t)):
            targets.append(OrderedDict([
                ("nrql", q),
                ("sibling", i > 0),
                ("datasource", getattr(part, "datasource", "")),
                ("type", getattr(part, "query_type", "")),
                ("expr", getattr(part, "expr", "") or ""),
                ("legend", getattr(part, "legend", "") or ""),
                ("confidence", getattr(part, "confidence", "")),
                ("notes", list(getattr(part, "notes", []) or [])),
                ("cw", getattr(part, "cw", None)),
                ("closest_equivalent",
                 getattr(part, "closest_equivalent", None)),
                ("metric_kind", getattr(part, "metric_kind", "")),
                ("k8s_mapped", bool(getattr(part, "k8s_mapped", False))),
                ("vars", list(getattr(part, "vars", []) or [])),
            ]))
    return targets


_CONF_RANK = {"exact": 0, "approximate": 1, "needs-review": 2,
              "untranslatable": 3}


def has_parse_failure(targets: List[Dict[str, Any]]) -> bool:
    """A parse error, or a fragment DROPPED without the parser's own
    explanation (dateOf()/hourOf() drops say "no Grafana equivalent" and
    are the contract's intended handling)."""
    for t in targets:
        for n in t["notes"]:
            if n.startswith("NRQL could not be parsed"):
                return True
            if "DROPPED from the translation" in n and \
                    "no Grafana equivalent" not in n:
                return True
    return False


def widget_confidence(targets: List[Dict[str, Any]]) -> str:
    """Worst confidence over the widget's primary targets ('' if none)."""
    confs = [t["confidence"] for t in targets if not t["sibling"]]
    if not confs:
        return ""
    return max(confs, key=lambda c: _CONF_RANK.get(c, 9))


# ---------------------------------------------------------------------------
# Expression normalization + agreement
# ---------------------------------------------------------------------------

_WINDOW_RE = re.compile(r"\[\s*(?:\$__rate_interval|\$__interval|\$__range|"
                        r"\$__auto|\d+[smhdw])\s*\]")
_MS_VAR_RE = re.compile(r"\$__(?:interval|range)_ms")
# Env scoping the human expressed through datasource/env binding instead
# of a matcher (contract F1: cluster="p-$env" "or dropped").
_ENV_SCOPE_LABELS = ("cluster", "cluster_name", "env",
                     "deployment_environment", "k8s_cluster_name")


def _split_top(text: str, sep: str = ",") -> List[str]:
    parts, depth, cur, quote = [], 0, [], ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _sort_matchers(expr: str) -> str:
    def fix(m: Any) -> str:
        items = [p.strip() for p in _split_top(m.group(1)) if p.strip()]
        return "{" + ",".join(sorted(items)) + "}"
    return re.sub(r"\{([^{}]*)\}", fix, expr)


def _sort_by_clauses(expr: str) -> str:
    def fix(m: Any) -> str:
        items = sorted(p.strip() for p in m.group(2).split(",") if p.strip())
        return "%s(%s)" % (m.group(1), ",".join(items))
    return re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\s*"
                  r"\(([^()]*)\)", fix, expr)


def _canon_by_position(expr: str) -> str:
    """sum(x) by (l) -> sum by (l)(x) so both spellings compare equal."""
    pat = re.compile(r"\)\s*(by|without)\s*\(([^()]*)\)")
    while True:
        m = pat.search(expr)
        if not m:
            return expr
        close = m.start()
        depth, i = 0, close
        while i >= 0:
            if expr[i] == ")":
                depth += 1
            elif expr[i] == "(":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        if i < 0:
            return expr
        j = i
        while j > 0 and (expr[j - 1].isalnum() or expr[j - 1] == "_"):
            j -= 1
        fn = expr[j:i]
        inner = expr[i + 1:close]
        expr = "%s%s %s (%s)(%s)%s" % (expr[:j], fn, m.group(1),
                                       m.group(2), inner, expr[m.end():])


def normalize_expr(expr: str, loose: bool = False) -> str:
    """Whitespace-free, label-order-insensitive form of a PromQL/LogQL
    expression. ``loose`` also unifies range windows and $__*_ms vars."""
    text = (expr or "").strip()
    text = _canon_by_position(text)
    text = _sort_matchers(text)
    text = _sort_by_clauses(text)
    if loose:
        text = _WINDOW_RE.sub("[W]", text)
        text = re.sub(r"\$\{(\w+)(?::\w+)?\}", r"$\1", text)
        text = _drop_env_scope(text)
        text = re.sub(r"\s+", "", text)
        # SEAM-COUNTER-SEMANTICS: rate(x[w]) * $__interval_ms / 1000 is
        # the per-bucket count, i.e. increase(x[$__interval]).
        if "*$__interval_ms/1000" in text:
            text = text.replace("*$__interval_ms/1000", "")
            text = text.replace("rate(", "increase(")
        text = _MS_VAR_RE.sub("W_MS", text)
    return re.sub(r"\s+", "", text)


def _drop_env_scope(expr: str) -> str:
    def fix(m: Any) -> str:
        keep = []
        for item in _split_top(m.group(1)):
            item = item.strip()
            if not item:
                continue
            lm = re.match(r"(\w+)\s*(=~|!~|!=|=)\s*\"(.*)\"$", item)
            if lm and lm.group(1) in _ENV_SCOPE_LABELS and (
                    "$env" in lm.group(3) or "${env" in lm.group(3)
                    or lm.group(3).startswith(CLUSTER_PREFIX)):
                continue
            keep.append(item)
        return "{" + ",".join(keep) + "}"
    return re.sub(r"\{([^{}]*)\}", fix, expr)


def _cw_key(cw: Dict[str, Any]) -> Tuple[str, str, str]:
    if cw.get("expression"):
        return ("expression", normalize_expr(str(cw["expression"]), True),
                "")
    return (str(cw.get("namespace") or ""), str(cw.get("metricName") or ""),
            str(cw.get("statistic") or ""))


def compare_targets(w: Dict[str, Any],
                    targets: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Match each human target against the emitted ones."""
    strict = set(normalize_expr(t["expr"]) for t in targets if t["expr"])
    loose = set(normalize_expr(t["expr"], True) for t in targets
                if t["expr"])
    cw_keys = set(_cw_key(t["cw"]) for t in targets if t.get("cw"))
    res = {"expected": 0, "strict": 0, "loose": 0, "misses": []}
    for e in w.get("expected") or []:
        res["expected"] += 1
        if e.get("datasource") == "cloudwatch":
            hit = _cw_key(e) in cw_keys
            res["strict"] += int(hit)
            res["loose"] += int(hit)
            if not hit:
                res["misses"].append(e)
            continue
        ex = e.get("expr") or ""
        if normalize_expr(ex) in strict:
            res["strict"] += 1
            res["loose"] += 1
        elif normalize_expr(ex, True) in loose:
            res["loose"] += 1
        else:
            res["misses"].append(e)
    return res


# ---------------------------------------------------------------------------
# Failure classes
# ---------------------------------------------------------------------------

def _exprs(targets: List[Dict[str, Any]]) -> List[str]:
    out = []
    for t in targets:
        out.append(t.get("expr") or "")
        out.append(t.get("legend") or "")
        cw = t.get("cw") or {}
        out.append(json.dumps(cw, sort_keys=True) if cw else "")
    return out


def failure_classes(w: Dict[str, Any],
                    targets: List[Dict[str, Any]]) -> Set[str]:
    """Which contract failure classes the CURRENT output still shows."""
    out: Set[str] = set()
    exprs = _exprs(targets)
    notes = [n for t in targets for n in t["notes"]]
    conf = widget_confidence(targets)
    joined = "\n".join(exprs)
    if any(PY_REPR_RE.search(e) for e in exprs):
        out.add("F1")
    if is_counter_case(w) and not re.search(
            r"\b(?:increase|rate)\([^\n]*_total", joined):
        out.add("F2")
    if is_k8s_case(w):
        exp = "\n".join(e.get("expr") or "" for e in w.get("expected") or [])
        wants_kube = re.search(r"\b(?:kube_|container_|node_)", exp)
        has_kube = re.search(r"\b(?:kube_|container_|node_)", joined)
        raw_k8s = re.search(r"\bk8s_", joined)
        if wants_kube and (raw_k8s or not has_kube):
            out.add("F3")
        elif not exp and raw_k8s:
            out.add("F3")
    if is_aws_case(w) and not any(t.get("cw") for t in targets):
        out.add("F4")
    if is_summary_case(w) and not ("_sum" in joined and "_count" in joined):
        out.add("F5")
    if has_parse_failure(targets):
        out.add("F6")
    if is_log_search_case(w):
        if conf == "untranslatable" or not re.search(
                r"\|~|\bregexp\b|\(\?i\)", joined):
            out.add("F7")
    if is_entity_guid_case(w) and conf == "untranslatable":
        out.add("F9")
    if is_app_env_case(w) and re.search(r'="[^"]* \([^"]*"', joined):
        out.add("F10")
    if any(n.startswith("FACET without LIMIT") or n.startswith("TIMESERIES ")
           for n in notes):
        out.add("F11")
    if is_nr_only_case(w) and conf == "untranslatable" and not any(
            t.get("closest_equivalent") for t in targets):
        out.add("F12")
    return out


# ---------------------------------------------------------------------------
# Optional end-to-end checks through the dashboard builder
# ---------------------------------------------------------------------------

def corpus_as_nr_dashboard(widgets: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Re-assemble the corpus widgets as a New Relic dashboard export so
    the real builder (and bind) can run over them."""
    pages: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for w in widgets:
        page = pages.setdefault(w.get("page") or "Page",
                                {"name": w.get("page") or "Page",
                                 "widgets": []})
        raw: Dict[str, Any] = {}
        if w.get("nrql"):
            raw["nrqlQueries"] = [{"accountId": 0, "query": q}
                                  for q in w["nrql"]]
        else:
            raw["text"] = w.get("title") or ""
        n = len(page["widgets"])
        page["widgets"].append({
            "title": w.get("title") or "",
            "visualization": {"id": w.get("visualization") or "viz.line"},
            "layout": {"column": 1 + (n % 3) * 4, "row": 1 + (n // 3) * 3,
                       "width": 4, "height": 3},
            "rawConfiguration": raw,
        })
    return {"name": "acme-backend corpus", "description": "",
            "pages": list(pages.values()),
            "variables": [{"name": "env", "title": "env", "type": "ENUM",
                           "items": [{"title": e, "value": e}
                                     for e in ("dev", "stage", "prod")],
                           "defaultValues": [{"value": {"string": "prod"}}],
                           "isMultiSelection": False,
                           "replacementStrategy": "STRING"}]}


def builder_checks(widgets: List[Dict[str, Any]],
                   cfg: Dict[str, Any]) -> Dict[str, Any]:
    """[MANUAL] placeholders + unbound datasource refs (F8). Returns
    {'available': False} when the builder/bind seams are missing."""
    try:
        from nr2grafana.model import parse_nr_dashboard
        from nr2grafana.grafana.builder import build_dashboards
    except Exception as e:  # pragma: no cover - sibling mid-edit
        return {"available": False, "reason": "builder import: %s" % e}
    cfg = copy.deepcopy(cfg)
    cfg["page_strategy"] = "rows"
    cfg["passthrough_fallback"] = False
    nr = parse_nr_dashboard(corpus_as_nr_dashboard(widgets))
    results = build_dashboards(nr, cfg)
    dash, report = results[0][1], results[0][2]
    untrans = [r for r in report
               if r.get("confidence") == "untranslatable"]
    manual_ok = [r for r in untrans
                 if r.get("manual") is True or "[MANUAL]" in
                 str(r.get("title") or r.get("widget") or "")]
    with_ce = [r for r in untrans if r.get("closest_equivalent")]
    panels = []
    for p in dash.get("panels") or []:
        panels.append(p)
        panels.extend(p.get("panels") or [])
    manual_panels = [p for p in panels
                     if "[MANUAL]" in str(p.get("title") or "")]
    out: Dict[str, Any] = OrderedDict([
        ("available", True),
        ("report_entries", len(report)),
        ("untranslatable_entries", len(untrans)),
        ("untranslatable_marked_manual", len(manual_ok)),
        ("untranslatable_with_closest_equivalent", len(with_ce)),
        ("manual_panels", len(manual_panels)),
        ("missing_datasource_entries",
         sum(1 for r in report if r.get("missing_datasource"))),
    ])
    try:
        from nr2grafana.bind import bind_datasources, unbound_refs
    except Exception as e:  # pragma: no cover - sibling mid-edit
        out["bind_available"] = False
        out["bind_reason"] = str(e)
        return out
    ds_map = {"datasource": {"type": "prometheus", "uid": "mimir"},
              "loki_datasource": {"type": "loki", "uid": "loki"},
              "tempo_datasource": {"type": "tempo", "uid": "tempo"},
              "cloudwatch_datasource": {"type": "cloudwatch",
                                        "uid": "cloudwatch"},
              "newrelic_datasource": {
                  "type": "nrgrafanaplugin-newrelic-datasource",
                  "uid": "newrelic"}}
    bound = bind_datasources(dash, ds_map)
    out["bind_available"] = True
    out["unbound_before_bind"] = unbound_refs(dash)
    out["unbound_after_bind"] = unbound_refs(bound)
    out["F8"] = len(out["unbound_after_bind"])
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score(widgets: List[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None,
          with_builder: bool = True) -> Dict[str, Any]:
    cfg = cfg or default_cfg()
    conf_now: Counter = Counter()
    conf_before: Counter = Counter()
    classes: Counter = Counter()
    examples: Dict[str, List[Dict[str, Any]]] = {k: [] for k in
                                                 FAILURE_CLASSES}
    agree = {"expected": 0, "strict": 0, "loose": 0, "widgets": 0,
             "widgets_full": 0}
    repr_hits = 0
    queried = 0
    no_closest = 0
    per_widget: List[Dict[str, Any]] = []
    for w in widgets:
        if not w.get("nrql"):
            continue
        queried += 1
        targets = convert_widget(w, cfg)
        conf = widget_confidence(targets)
        conf_now[conf] += 1
        conf_before[(w.get("before") or {}).get("confidence", "")] += 1
        fcs = failure_classes(w, targets)
        for fc in fcs:
            classes[fc] += 1
            if len(examples[fc]) < 3:
                examples[fc].append({
                    "id": w["id"], "nrql": w["nrql"][0][:160],
                    "output": (targets[0]["expr"] if targets else "")[:160],
                    "expected": ((w.get("expected") or [{}])[0].get("expr")
                                 or "")[:160]})
        repr_hits += sum(1 for e in _exprs(targets) if PY_REPR_RE.search(e))
        if conf == "untranslatable" and not any(
                t.get("closest_equivalent") for t in targets):
            no_closest += 1
        cmp_ = compare_targets(w, targets)
        if cmp_["expected"]:
            agree["widgets"] += 1
            agree["expected"] += cmp_["expected"]
            agree["strict"] += cmp_["strict"]
            agree["loose"] += cmp_["loose"]
            if cmp_["loose"] == cmp_["expected"]:
                agree["widgets_full"] += 1
        per_widget.append({"id": w["id"], "confidence": conf,
                           "classes": sorted(fcs),
                           "matched": cmp_["loose"],
                           "expected": cmp_["expected"]})

    def share(n: int) -> float:
        return round(n / float(queried), 4) if queried else 0.0

    def share_t(n: int) -> float:
        return round(n / float(agree["expected"]), 4) \
            if agree["expected"] else 0.0

    result: Dict[str, Any] = OrderedDict([
        ("widgets_total", len(widgets)),
        ("widgets_queried", queried),
        ("confidence", OrderedDict(
            (k, {"count": conf_now.get(k, 0),
                 "share": share(conf_now.get(k, 0))})
            for k in ("exact", "approximate", "needs-review",
                      "untranslatable"))),
        ("confidence_before", OrderedDict(
            (k, {"count": conf_before.get(k, 0),
                 "share": share(conf_before.get(k, 0))})
            for k in ("exact", "approximate", "needs-review",
                      "untranslatable"))),
        ("thresholds", OrderedDict([
            ("needs_review_max", NEEDS_REVIEW_MAX),
            ("untranslatable_max", UNTRANSLATABLE_MAX),
            ("agreement_floor", AGREEMENT_FLOOR),
            ("needs_review_ok",
             share(conf_now.get("needs-review", 0)) <= NEEDS_REVIEW_MAX),
            ("untranslatable_ok",
             share(conf_now.get("untranslatable", 0)) <= UNTRANSLATABLE_MAX),
        ])),
        ("python_repr_hits", repr_hits),
        ("untranslatable_without_closest_equivalent", no_closest),
        ("failure_classes", OrderedDict(
            (k, {"count": classes.get(k, 0), "what": v})
            for k, v in FAILURE_CLASSES.items())),
        ("agreement", OrderedDict([
            ("human_targets", agree["expected"]),
            ("strict_matches", agree["strict"]),
            ("strict_share", share_t(agree["strict"])),
            ("loose_matches", agree["loose"]),
            ("loose_share", share_t(agree["loose"])),
            ("widgets_compared", agree["widgets"]),
            ("widgets_fully_matched", agree["widgets_full"]),
            ("widgets_share", round(agree["widgets_full"] /
                                    float(agree["widgets"]), 4)
             if agree["widgets"] else 0.0),
        ])),
        ("examples", examples),
        ("per_widget", per_widget),
    ])
    result["thresholds"]["agreement_ok"] = \
        result["agreement"]["loose_share"] >= AGREEMENT_FLOOR
    if with_builder:
        bc = builder_checks(widgets, cfg)
        result["builder"] = bc
        if bc.get("available") and "F8" in bc:
            result["failure_classes"]["F8"]["count"] = bc["F8"]
    return result


def format_report(res: Dict[str, Any], show: str = "",
                  limit: int = 10) -> str:
    lines = []
    lines.append("corpus: %d widgets, %d with NRQL"
                 % (res["widgets_total"], res["widgets_queried"]))
    lines.append("confidence (now vs before):")
    for k, v in res["confidence"].items():
        b = res["confidence_before"][k]
        lines.append("  %-15s %4d (%5.1f%%)   before %4d (%5.1f%%)"
                     % (k, v["count"], v["share"] * 100, b["count"],
                        b["share"] * 100))
    th = res["thresholds"]
    lines.append("thresholds: needs-review <= %d%% [%s], untranslatable "
                 "<= %d%% [%s], agreement >= %d%% [%s]"
                 % (th["needs_review_max"] * 100,
                    "ok" if th["needs_review_ok"] else "FAIL",
                    th["untranslatable_max"] * 100,
                    "ok" if th["untranslatable_ok"] else "FAIL",
                    th["agreement_floor"] * 100,
                    "ok" if th["agreement_ok"] else "FAIL"))
    lines.append("python reprs in output: %d; untranslatable without "
                 "closest_equivalent: %d"
                 % (res["python_repr_hits"],
                    res["untranslatable_without_closest_equivalent"]))
    lines.append("failure classes still present:")
    for k, v in res["failure_classes"].items():
        lines.append("  %-4s %4d  %s" % (k, v["count"], v["what"]))
    ag = res["agreement"]
    lines.append("agreement vs human targets: strict %d/%d (%.1f%%), "
                 "loose %d/%d (%.1f%%); widgets fully matched %d/%d "
                 "(%.1f%%)"
                 % (ag["strict_matches"], ag["human_targets"],
                    ag["strict_share"] * 100, ag["loose_matches"],
                    ag["human_targets"], ag["loose_share"] * 100,
                    ag["widgets_fully_matched"], ag["widgets_compared"],
                    ag["widgets_share"] * 100))
    bc = res.get("builder") or {}
    if bc.get("available"):
        lines.append("builder: %d untranslatable entries, %d marked "
                     "[MANUAL], %d with closest_equivalent; unbound after "
                     "bind: %s"
                     % (bc["untranslatable_entries"],
                        bc["untranslatable_marked_manual"],
                        bc["untranslatable_with_closest_equivalent"],
                        bc.get("unbound_after_bind", "n/a")))
    if show:
        lines.append("examples for %s:" % show)
        for ex in (res["examples"].get(show) or [])[:limit]:
            lines.append("  %s" % ex["id"])
            lines.append("    nrql:     %s" % ex["nrql"])
            lines.append("    output:   %s" % ex["output"])
            lines.append("    expected: %s" % ex["expected"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="build the redacted corpus")
    b.add_argument("source", help="query map JSON (never committed)")
    b.add_argument("--out", default=DEFAULT_CORPUS_DIR)
    s = sub.add_parser("score", help="run + score the current converter")
    s.add_argument("--corpus", default=DEFAULT_CORPUS_DIR)
    s.add_argument("--json", action="store_true", dest="as_json")
    s.add_argument("--show", default="", help="print examples for a class")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--no-builder", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        with open(args.source, encoding="utf-8") as fh:
            source = json.load(fh)
        index = build_corpus(source, args.out)
        print("wrote %d widgets over %d page files to %s"
              % (index["widgets"], index["pages"], args.out))
        print("redaction counts: %s"
              % json.dumps(index["redaction_counts"]))
        return 0
    if args.cmd == "score":
        widgets = load_corpus(args.corpus)
        res = score(widgets, with_builder=not args.no_builder)
        if args.as_json:
            res = dict(res)
            res.pop("per_widget", None)
            print(json.dumps(res, indent=1))
        else:
            print(format_report(res, args.show, args.limit))
        th = res["thresholds"]
        return 0 if (th["needs_review_ok"] and th["untranslatable_ok"]
                     and res["python_repr_hits"] == 0) else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
