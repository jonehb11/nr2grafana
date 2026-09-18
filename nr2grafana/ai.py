"""Optional AI assistance for fixing broken panels (stdlib only).

Two interchangeable backends share one duck-typed surface
(``available``, ``suggest_fix``, ``chat``):

* :class:`AIAssist` talks to the Anthropic Messages API via urllib.
  The API key comes from the ``api_key`` argument or the
  ANTHROPIC_API_KEY environment variable and is kept in process memory
  only -- it is never persisted, logged, or included in error
  messages. The model defaults to ``claude-sonnet-5`` and can be
  overridden via the ``model`` argument or the N2G_AI_MODEL
  environment variable.
* :class:`LocalAgent` runs any console AI agent the user already has
  installed (``claude -p {prompt}``, ``kiro-cli``, ...) as a local
  subprocess. No API key is needed; panel/query/error text is sent to
  the local process only. The command string is not a secret and may
  be persisted in settings.

:func:`get_assistant` picks the backend: API key wins, then a local
command, else None.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 2048
TIMEOUT = 60

# Local console-agent defaults: generous timeout (agent CLIs can be
# slow to cold-start), captured output capped so a runaway process
# cannot exhaust memory, only a stderr tail lands in error messages.
LOCAL_TIMEOUT = 180
_LOCAL_MAX_OUTPUT = 200 * 1024
_LOCAL_STDERR_TAIL = 500

# ANSI escape sequences: CSI (colors/cursor), OSC (titles/links,
# BEL- or ST-terminated), and lone two-byte escapes.
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[@-Z\\-_])")

_CONFIDENCES = ("high", "medium", "low")

_FIX_SYSTEM = """\
You are helping migrate New Relic dashboards to Grafana backed by an LGTM
stack (Loki logs, Grafana, Tempo traces, Mimir/Prometheus metrics). A panel
query translated from NRQL is failing against the live Grafana instance.
Diagnose the failure and propose a corrected query for the panel's
datasource (PromQL for prometheus, LogQL for loki, TraceQL for tempo).
Prefer metric and label names present in the provided instance data; if the
required data does not exist yet, set fixed_expr to null and list the
manual steps instead.

Respond with STRICT JSON only -- a single object, no prose and no markdown
fences -- with exactly these keys:
  "explanation": short string: why the query fails and what the fix does
  "fixed_expr": corrected query string, or null if no query fix applies
  "confidence": "high" | "medium" | "low"
  "actions": array of strings: manual steps the user must take (may be [])
"""

_CONVERT_SYSTEM = """\
You are helping migrate New Relic dashboards to Grafana backed by an LGTM
stack (Loki logs, Grafana, Tempo traces, Mimir/Prometheus metrics). A panel
was auto-migrated from NRQL, but the converter marked it needs-review or
untranslatable for the reasons in translation_notes. Produce a
higher-fidelity (or from-scratch) query for the panel's datasource family
(ds_family) that reproduces the original_nrql's intent against an OTel-fed
LGTM stack: PromQL for prometheus, LogQL for loki, TraceQL for tempo. OTel
metric names often carry unit/total suffixes (e.g. _seconds, _total) and NRQL
FACET / WHERE dimensions usually map to labels; prefer metric and label names
present in the provided instance data. If the required data does not exist
yet, set fixed_expr to null and list the manual steps instead.

Respond with STRICT JSON only -- a single object, no prose and no markdown
fences -- with exactly these keys:
  "explanation": short string: how the query reproduces the NRQL intent
  "fixed_expr": the translated query string, or null if none applies
  "confidence": "high" | "medium" | "low"
  "actions": array of strings: manual steps the user must take (may be [])
"""

_RCA_SYSTEM = """\
You are an SRE/FinOps assistant investigating an AWS cost anomaly for an
observability stack (Grafana LGTM -- Loki, Grafana, Tempo, Mimir/Prometheus)
running on EKS. You are given the anomaly, the converged read-only evidence
(cost-explorer, cloudtrail, vpc-flow-logs, eks-control-plane, lgtm-self-
metrics), any flow-log byte attribution, and a candidate mitigation plan with
its reliability guardrails.

Analyze the anomaly and the evidence, then propose how to CUT the cost without
reducing availability, durability, performance, or the ability to serve the
CURRENT traffic rate. Respect every reliability guardrail in the context.

Domain rules you MUST follow:
  * A "*DataTransfer-Regional-Bytes" (or *InterZone*) usage type is cross-AZ
    NETWORK transfer within one region, NOT storage -- treat it as cross-AZ
    network even when the service tag is EBS/EC2. "Regional-Bytes" == cross-AZ.
  * NEVER propose a change that lowers the replication factor (RF) or shortens
    retention, and NEVER CPU-limit ingesters (throttling breaks ingest).
  * Zone-aware ring migration is done one zone at a time; deploy across a
    number of zones >= RF; never blind-disable NLB cross-zone (confirm >=1
    healthy target per enabled AZ first, or an AZ black-holes).
  * Keep every generated config GENERIC and paste-ready -- placeholders in
    <ANGLE_BRACKETS>, no customer-specific ids, account numbers, or values.
  * If a mitigation cannot preserve availability/durability/performance or
    current traffic, say so loudly and set its keeps_* flags to false.

Respond with STRICT JSON only -- a single object, no prose and no markdown
fences -- with exactly these keys:
  "root_cause": short string: the dominant driver and why, with its % share
  "mitigations": array of objects, each with:
      "title": short name of the mitigation
      "saving": expected $/day or %% saved (string)
      "change": what to change (references a generic config)
      "preconditions": array of strings: what MUST hold or it breaks something
      "keeps_availability": boolean
      "keeps_durability": boolean
      "keeps_performance": boolean
  "config_notes": array of strings: notes on the generic paste-ready configs
"""


class AIError(Exception):
    """Raised when the Claude API cannot be used or returns an error."""


def _strip_fences(text: str) -> str:
    """Remove a surrounding markdown code fence, if present."""
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    # Drop the opening fence line (``` or ```json).
    lines = lines[1:]
    # Drop a closing fence line if present.
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _fallback(raw_text: str) -> Dict[str, Any]:
    return {"explanation": raw_text, "fixed_expr": None,
            "confidence": "low", "actions": []}


def _parse_fix(raw_text: str) -> Dict[str, Any]:
    """Parse the model's reply into the suggest_fix result shape.

    Defensive: strips markdown fences, falls back to a low-confidence
    explanation-only result when the reply is not the expected JSON.
    """
    candidate = _strip_fences(raw_text)
    try:
        data = json.loads(candidate)
    except (ValueError, TypeError):
        return _fallback(raw_text)
    if not isinstance(data, dict):
        return _fallback(raw_text)
    explanation = data.get("explanation")
    if not isinstance(explanation, str):
        explanation = raw_text
    fixed = data.get("fixed_expr")
    if not isinstance(fixed, str) or not fixed.strip():
        fixed = None
    confidence = data.get("confidence")
    if confidence not in _CONFIDENCES:
        confidence = "low"
    actions_in = data.get("actions")
    actions: List[str] = []
    if isinstance(actions_in, list):
        actions = [str(a) for a in actions_in if a is not None]
    return {"explanation": explanation, "fixed_expr": fixed,
            "confidence": confidence, "actions": actions}


def _parse_fix_loose(raw_text: str) -> Dict[str, Any]:
    """_parse_fix, retrying on the outermost ``{...}`` block.

    Console agents often wrap the requested JSON in extra prose that
    the strict parse cannot swallow; a second attempt on the brace
    slice recovers it before giving up on the fallback shape.
    """
    out = _parse_fix(raw_text)
    if out != _fallback(raw_text):
        return out
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if 0 <= start < end:
        inner = raw_text[start:end + 1]
        parsed = _parse_fix(inner)
        if parsed != _fallback(inner):
            return parsed
    return out


def _norm_str_list(value: Any) -> List[str]:
    """Coerce a value into a list of strings (dropping Nones)."""
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _norm_mitigation(item: Any) -> Dict[str, Any]:
    """Normalize one RCA mitigation into the result shape.

    Guarantees title/saving/change strings, a preconditions list, and
    the three baseline ``keeps_*`` booleans (defaulting to false); any
    extra ``keeps_*`` keys the model returns are coerced and kept.
    """
    if not isinstance(item, dict):
        out = {"title": str(item), "saving": "", "change": "",
               "preconditions": []}
    else:
        out = {
            "title": str(item.get("title") or ""),
            "saving": str(item.get("saving") or ""),
            "change": str(item.get("change") or ""),
            "preconditions": _norm_str_list(item.get("preconditions")),
        }
        for key, val in item.items():
            if isinstance(key, str) and key.startswith("keeps_"):
                out[key] = bool(val)
    for key in ("keeps_availability", "keeps_durability",
                "keeps_performance"):
        out.setdefault(key, False)
    return out


def _rca_fallback(raw_text: str) -> Dict[str, Any]:
    return {"root_cause": raw_text, "mitigations": [], "config_notes": []}


def _parse_rca(raw_text: str) -> Dict[str, Any]:
    """Parse the model's reply into the RCA result shape.

    Defensive like :func:`_parse_fix`: strips markdown fences and falls
    back to a root_cause-only result when the reply is not the expected
    ``{root_cause, mitigations, config_notes}`` JSON object.
    """
    candidate = _strip_fences(raw_text)
    try:
        data = json.loads(candidate)
    except (ValueError, TypeError):
        return _rca_fallback(raw_text)
    if not isinstance(data, dict):
        return _rca_fallback(raw_text)
    root = data.get("root_cause")
    if not isinstance(root, str):
        root = raw_text if root is None else json.dumps(root, default=str)
    mits_in = data.get("mitigations")
    mitigations: List[Dict[str, Any]] = []
    if isinstance(mits_in, list):
        mitigations = [_norm_mitigation(m) for m in mits_in]
    return {"root_cause": root, "mitigations": mitigations,
            "config_notes": _norm_str_list(data.get("config_notes"))}


def _parse_rca_loose(raw_text: str) -> Dict[str, Any]:
    """_parse_rca, retrying on the outermost ``{...}`` block.

    Console agents wrap the JSON in prose; a second attempt on the brace
    slice recovers it before giving up on the fallback shape.
    """
    out = _parse_rca(raw_text)
    if out != _rca_fallback(raw_text):
        return out
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if 0 <= start < end:
        inner = raw_text[start:end + 1]
        parsed = _parse_rca(inner)
        if parsed != _rca_fallback(inner):
            return parsed
    return out


def _is_convert(context: Dict[str, Any]) -> bool:
    """True when the caller asked for conversion-mode framing."""
    return str(context.get("mode") or "").strip().lower() == "convert"


def _is_rca(context: Dict[str, Any]) -> bool:
    """True when the caller asked for cost-anomaly RCA framing."""
    return str(context.get("mode") or "").strip().lower() == "rca"


def _system_for(context: Dict[str, Any]) -> str:
    """Pick the system framing by ``context["mode"]`` (default fix)."""
    if _is_rca(context):
        return _RCA_SYSTEM
    return _CONVERT_SYSTEM if _is_convert(context) else _FIX_SYSTEM


def _fix_prompt(context: Dict[str, Any]) -> str:
    """Render the suggest_fix / convert user prompt from a context.

    Shared by fix mode and convert mode; ``context["mode"]`` selects the
    framing sentence. When present, the converter's migration hints
    (original_nrql, translation_notes, confidence, ds_family) are folded
    in alongside the existing panel/query/error/datasource, requirements
    excerpt and instance metric/label samples.
    """
    instance = context.get("instance") or {}
    convert = _is_convert(context)
    detail: Dict[str, Any] = {}
    for key in ("panel", "expr", "error", "datasource", "nrql"):
        if context.get(key):
            detail[key] = context[key]
    if context.get("original_nrql"):
        detail["original_nrql"] = context["original_nrql"]
    if context.get("translation_notes"):
        detail["translation_notes"] = context["translation_notes"]
    if context.get("confidence"):
        detail["converter_confidence"] = context["confidence"]
    if context.get("ds_family"):
        detail["ds_family"] = context["ds_family"]
    if context.get("requirements"):
        detail["requirements_excerpt"] = context["requirements"]
    if instance.get("datasources"):
        detail["instance_datasources"] = instance["datasources"]
    if instance.get("metrics_sample"):
        detail["sample_metric_names"] = instance["metrics_sample"]
    body = json.dumps(detail, indent=2, default=str, sort_keys=True)
    if convert:
        return ("Translate this auto-migrated New Relic panel into a "
                "higher-fidelity Grafana/LGTM query. Context:\n" + body)
    return "Fix this failing Grafana panel query. Context:\n" + body


def _rca_prompt(context: Dict[str, Any]) -> str:
    """Render the cost-anomaly RCA user prompt from a context.

    Folds in whatever RCA artifacts are present -- the framed incident,
    the converged cause/evidence, flow-log byte attribution, a candidate
    mitigation plan and its reliability guardrails -- plus the same
    instance metric/label samples the fix prompt carries.
    """
    instance = context.get("instance") or {}
    detail: Dict[str, Any] = {}
    for key in ("incident", "anomaly", "cause", "evidence_convergence",
                "confidence", "flowlogs", "mitigations", "mitigation",
                "reliability", "guardrails", "rca"):
        if context.get(key):
            detail[key] = context[key]
    if context.get("requirements"):
        detail["requirements_excerpt"] = context["requirements"]
    if instance.get("datasources"):
        detail["instance_datasources"] = instance["datasources"]
    if instance.get("metrics_sample"):
        detail["sample_metric_names"] = instance["metrics_sample"]
    body = json.dumps(detail, indent=2, default=str, sort_keys=True)
    return ("Analyze this AWS cost anomaly and the converged evidence, "
            "then propose reliability-safe cost mitigations. Context:\n"
            + body)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (colors, cursor moves, OSC)."""
    return _ANSI_RE.sub("", text)


def _strip_echo(text: str, prompt: str) -> str:
    """Drop a whole-line echo of the prompt at either end.

    Some console agents repeat the input before (or after) the
    answer; only an echo occupying its own leading/trailing lines is
    trivially detectable, so nothing else is touched.
    """
    s = text.strip()
    p = prompt.strip()
    if not p or s == p:
        return s
    if s.startswith(p + "\n"):
        s = s[len(p):].lstrip("\n")
    if s.endswith("\n" + p):
        s = s[:-(len(p) + 1)].rstrip("\n")
    return s


def _render_prompt(messages: List[Dict[str, str]],
                   system: str = "") -> str:
    """Flatten a Messages-style conversation into one console prompt.

    A single user turn is passed through verbatim (after the system
    text); longer conversations get User:/Assistant: labels and a
    trailing "Assistant:" cue.
    """
    parts: List[str] = []
    if system:
        parts.append(system.strip())
    convo = [m for m in messages or [] if m.get("content")]
    if len(convo) == 1 and convo[0].get("role", "user") == "user":
        parts.append(str(convo[0]["content"]))
    else:
        for m in convo:
            label = "Assistant" if m.get("role") == "assistant" \
                else "User"
            parts.append("%s: %s" % (label, m["content"]))
        parts.append("Assistant:")
    return "\n\n".join(p for p in parts if p)


class AIAssist:
    """Small Claude API client for panel-fix suggestions and chat."""

    def __init__(self, api_key: str = "", model: str = ""):
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = (model or os.environ.get("N2G_AI_MODEL", "")
                      or DEFAULT_MODEL)

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    # -- public API ------------------------------------------------------

    def suggest_fix(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Ask Claude to fix a broken panel query.

        context keys (all optional): "panel", "expr", "error",
        "datasource", "requirements", "instance" (dict with
        "datasources" and "metrics_sample" lists), "nrql". Conversion
        mode adds "mode" ("fix"|"convert"), "original_nrql",
        "translation_notes", "confidence" and "ds_family"; when
        mode == "convert" the conversion system framing is used.

        Returns {"explanation", "fixed_expr", "confidence", "actions"}.
        """
        user = _fix_prompt(context)
        raw = self.chat([{"role": "user", "content": user}],
                        system=_system_for(context))
        return _parse_fix(raw)

    def analyze(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Ask Claude to analyze an AWS cost anomaly (RCA mode).

        Uses the RCA system framing (picked by ``context["mode"] ==
        "rca"``) and folds in the converged evidence, flow-log byte
        attribution and candidate mitigation plan present in ``context``.

        Returns {"root_cause", "mitigations", "config_notes"} where each
        mitigation carries title/saving/change/preconditions and the
        keeps_availability/keeps_durability/keeps_performance flags.
        """
        ctx = dict(context)
        ctx["mode"] = "rca"
        user = _rca_prompt(ctx)
        raw = self.chat([{"role": "user", "content": user}],
                        system=_system_for(ctx))
        return _parse_rca(raw)

    def chat(self, messages: List[Dict[str, str]],
             system: str = "") -> str:
        """Send a Messages API request; return the assistant's text."""
        payload = self._request({
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": messages,
        })
        parts = []
        for block in payload.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)

    # -- internals -------------------------------------------------------

    def _request(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if not self._api_key:
            raise AIError(
                "no Anthropic API key configured; set ANTHROPIC_API_KEY "
                "or provide a key to enable AI assistance")
        if not body.get("system"):
            body = {k: v for k, v in body.items() if k != "system"}
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(body).encode(),
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise AIError(self._http_message(e))
        except socket.timeout:
            raise AIError("the Claude API request timed out after %d "
                          "seconds; try again" % TIMEOUT)
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            raise AIError("cannot reach the Claude API (%s); check your "
                          "network connection and any proxy settings"
                          % reason)
        try:
            payload = json.loads(raw)
        except ValueError:
            raise AIError("the Claude API returned an unreadable response")
        if not isinstance(payload, dict):
            raise AIError("the Claude API returned an unexpected response")
        return payload

    @staticmethod
    def _http_message(e: "urllib.error.HTTPError") -> str:
        detail = ""
        try:
            data = json.loads(e.read().decode())
            detail = data.get("error", {}).get("message", "")
        except Exception:
            pass
        if e.code == 401:
            return ("the Anthropic API key was rejected (401); check that "
                    "the key is valid and not revoked")
        if e.code == 403:
            return ("the Anthropic API key lacks permission for this "
                    "request (403)")
        if e.code == 429:
            return ("rate limited by the Claude API (429); wait a moment "
                    "and try again")
        if e.code == 529:
            return ("the Claude API is temporarily overloaded (529); "
                    "try again shortly")
        msg = "Claude API error (HTTP %d)" % e.code
        if detail:
            msg += ": " + detail[:300]
        return msg


class LocalAgent:
    """AI assistance through a local console agent (no API key).

    ``command`` is any CLI AI agent already installed by the user,
    e.g. ``claude -p {prompt}`` (Claude Code print mode) or
    ``kiro-cli``. The command is shlex-split and run WITHOUT a shell:
    if ``{prompt}`` appears it is substituted inside its argv element
    (never shell-interpolated); otherwise the prompt is written to the
    process's stdin.

    The command string is not a secret and may be persisted in Store
    settings. Privacy: local mode sends panel/query/error text to the
    local process only -- nothing leaves the machine unless the
    configured CLI itself talks to a remote service. The command runs
    with the user's own permissions.

    Duck-types :class:`AIAssist`: ``available``, ``suggest_fix``,
    ``chat``; plus :meth:`test` for a cheap connectivity probe.
    """

    def __init__(self, command: str, timeout: int = LOCAL_TIMEOUT):
        self.command = (command or "").strip()
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.command)

    # -- public API ------------------------------------------------------

    def suggest_fix(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Ask the local agent to fix a broken panel query.

        Same context and return shape as AIAssist.suggest_fix,
        including conversion mode via ``context["mode"]``.
        """
        user = _fix_prompt(context)
        raw = self.chat([{"role": "user", "content": user}],
                        system=_system_for(context))
        return _parse_fix_loose(raw)

    def analyze(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Ask the local agent to analyze a cost anomaly (RCA mode).

        Same context and return shape as AIAssist.analyze; the loose
        parse recovers the RCA JSON from any surrounding prose.
        """
        ctx = dict(context)
        ctx["mode"] = "rca"
        user = _rca_prompt(ctx)
        raw = self.chat([{"role": "user", "content": user}],
                        system=_system_for(ctx))
        return _parse_rca_loose(raw)

    def chat(self, messages: List[Dict[str, str]],
             system: str = "") -> str:
        """Run the agent once over the flattened conversation."""
        return self._run(_render_prompt(messages, system))

    def test(self) -> Dict[str, Any]:
        """Probe the command with a trivial prompt. Never raises.

        Returns {"ok", "reply_excerpt", "latency_ms"} plus "error"
        when the probe failed.
        """
        start = time.time()
        try:
            reply = self.chat([{"role": "user",
                                "content": "Reply with exactly: OK"}])
        except Exception as e:
            return {"ok": False, "reply_excerpt": "",
                    "latency_ms": int((time.time() - start) * 1000),
                    "error": str(e)}
        latency = int((time.time() - start) * 1000)
        out: Dict[str, Any] = {"ok": "OK" in reply,
                               "reply_excerpt": reply.strip()[:200],
                               "latency_ms": latency}
        if not out["ok"]:
            if reply.strip():
                out["error"] = ("the command ran but did not reply "
                                "OK -- check that it accepts a "
                                "prompt and prints the answer to "
                                "stdout")
            else:
                out["error"] = ("the command produced no output on "
                                "stdout")
        return out

    # -- internals -------------------------------------------------------

    def _argv(self, prompt: str):
        """Build (argv, use_stdin) for one invocation."""
        try:
            argv = shlex.split(self.command)
        except ValueError as e:
            raise AIError("cannot parse the local agent command %r: "
                          "%s" % (self.command, e))
        if not argv:
            raise AIError("no local agent command configured -- set "
                          "one (e.g. \"claude -p {prompt}\") to use "
                          "local AI assistance")
        use_stdin = not any("{prompt}" in a for a in argv)
        if not use_stdin:
            argv = [a.replace("{prompt}", prompt) for a in argv]
        return argv, use_stdin

    def _run(self, prompt: str) -> str:
        argv, use_stdin = self._argv(prompt)
        env = dict(os.environ)
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"
        hint = (" -- is the CLI installed and on PATH? try running "
                "it in a terminal")
        try:
            proc = subprocess.run(
                argv,
                input=prompt.encode("utf-8") if use_stdin else None,
                stdin=None if use_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, env=env, timeout=self.timeout)
        except FileNotFoundError:
            raise AIError("local agent command not found: %r%s"
                          % (argv[0], hint))
        except subprocess.TimeoutExpired:
            raise AIError("local agent command %r timed out after "
                          "%d seconds%s"
                          % (self.command, self.timeout, hint))
        except OSError as e:
            raise AIError("cannot run local agent command %r: %s%s"
                          % (self.command, e, hint))
        if proc.returncode != 0:
            tail = _strip_ansi(
                proc.stderr[-4096:].decode("utf-8", "replace"))
            tail = tail.strip()[-_LOCAL_STDERR_TAIL:]
            msg = ("local agent command %r exited with code %d"
                   % (self.command, proc.returncode))
            if tail:
                msg += ": %s" % tail
            raise AIError(msg + hint)
        out = proc.stdout[:_LOCAL_MAX_OUTPUT].decode("utf-8",
                                                     "replace")
        return _strip_echo(_strip_ansi(out), prompt)


def get_assistant(api_key: str = "", model: str = "",
                  command: str = "") -> Optional[Any]:
    """Pick the configured AI backend.

    Precedence: the Anthropic API wins when a key is configured
    (``api_key`` argument or ANTHROPIC_API_KEY env), then a local
    console agent when ``command`` is set, else None. Both returned
    objects share the AIAssist duck type. Local mode sends
    panel/query/error text to the local process only -- nothing is
    sent to any remote API.
    """
    if api_key or os.environ.get("ANTHROPIC_API_KEY", ""):
        return AIAssist(api_key=api_key, model=model)
    if command and command.strip():
        return LocalAgent(command)
    return None
