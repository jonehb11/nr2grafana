"""Optional Claude AI assistance for fixing broken panels (stdlib only).

Talks to the Anthropic Messages API via urllib. The API key comes from the
``api_key`` argument or the ANTHROPIC_API_KEY environment variable and is
kept in process memory only -- it is never persisted, logged, or included
in error messages. The model defaults to ``claude-sonnet-5`` and can be
overridden via the ``model`` argument or the N2G_AI_MODEL environment
variable.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 2048
TIMEOUT = 60

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
        "datasources" and "metrics_sample" lists), "nrql".

        Returns {"explanation", "fixed_expr", "confidence", "actions"}.
        """
        user = self._fix_prompt(context)
        raw = self.chat([{"role": "user", "content": user}],
                        system=_FIX_SYSTEM)
        return _parse_fix(raw)

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

    def _fix_prompt(self, context: Dict[str, Any]) -> str:
        instance = context.get("instance") or {}
        detail: Dict[str, Any] = {}
        for key in ("panel", "expr", "error", "datasource", "nrql"):
            if context.get(key):
                detail[key] = context[key]
        if context.get("requirements"):
            detail["requirements_excerpt"] = context["requirements"]
        if instance.get("datasources"):
            detail["instance_datasources"] = instance["datasources"]
        if instance.get("metrics_sample"):
            detail["sample_metric_names"] = instance["metrics_sample"]
        return ("Fix this failing Grafana panel query. Context:\n"
                + json.dumps(detail, indent=2, default=str, sort_keys=True))

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
