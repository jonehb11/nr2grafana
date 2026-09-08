"""Tests for nr2grafana.ai (Claude API assistance)."""

import io
import json
import socket
import unittest
import urllib.error
from unittest import mock

from nr2grafana.ai import (AIAssist, AIError, DEFAULT_MODEL, _parse_fix,
                           _strip_fences)

FAKE_KEY = "sk-ant-test-key-do-not-log"


def api_response(text):
    """Build a fake urlopen context manager returning a Messages reply."""
    body = json.dumps({
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }).encode()
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def http_error(code, message="boom"):
    body = json.dumps({"type": "error",
                       "error": {"type": "err", "message": message}})
    return urllib.error.HTTPError(
        "https://api.anthropic.com/v1/messages", code, "err",
        {}, io.BytesIO(body.encode()))


FIX_JSON = {
    "explanation": "metric renamed",
    "fixed_expr": "sum(rate(http_requests_total[5m]))",
    "confidence": "high",
    "actions": ["none"],
}


class AvailableTests(unittest.TestCase):
    def test_no_key_not_available(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(AIAssist().available)

    def test_key_arg_available(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(AIAssist(api_key=FAKE_KEY).available)

    def test_key_from_env(self):
        with mock.patch.dict("os.environ",
                             {"ANTHROPIC_API_KEY": FAKE_KEY}, clear=True):
            self.assertTrue(AIAssist().available)

    def test_default_model(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(AIAssist().model, DEFAULT_MODEL)

    def test_model_env_override(self):
        with mock.patch.dict("os.environ",
                             {"N2G_AI_MODEL": "claude-opus-5"},
                             clear=True):
            self.assertEqual(AIAssist().model, "claude-opus-5")

    def test_model_arg_beats_env(self):
        with mock.patch.dict("os.environ",
                             {"N2G_AI_MODEL": "claude-opus-5"},
                             clear=True):
            self.assertEqual(AIAssist(model="claude-haiku-4-5").model,
                             "claude-haiku-4-5")

    def test_no_key_raises_actionable_error(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AIError) as cm:
                AIAssist().chat([{"role": "user", "content": "hi"}])
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))


class SuggestFixTests(unittest.TestCase):
    def setUp(self):
        self.ai = AIAssist(api_key=FAKE_KEY)
        self.context = {
            "panel": "Error rate",
            "expr": "sum(rate(http_request_total[5m]))",
            "error": "no data",
            "datasource": "prometheus",
            "requirements": {"metrics": ["http_requests_total"]},
            "instance": {"datasources": [{"type": "prometheus"}],
                         "metrics_sample": ["http_requests_total"]},
            "nrql": "SELECT count(*) FROM Transaction",
        }

    def test_happy_path_plain_json(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=api_response(json.dumps(FIX_JSON))):
            out = self.ai.suggest_fix(self.context)
        self.assertEqual(out, FIX_JSON)

    def test_fenced_json(self):
        text = "```json\n%s\n```" % json.dumps(FIX_JSON)
        with mock.patch("urllib.request.urlopen",
                        return_value=api_response(text)):
            out = self.ai.suggest_fix(self.context)
        self.assertEqual(out, FIX_JSON)

    def test_bare_fence(self):
        text = "```\n%s\n```" % json.dumps(FIX_JSON)
        with mock.patch("urllib.request.urlopen",
                        return_value=api_response(text)):
            out = self.ai.suggest_fix(self.context)
        self.assertEqual(out, FIX_JSON)

    def test_non_json_falls_back(self):
        text = "I think the metric name is wrong."
        with mock.patch("urllib.request.urlopen",
                        return_value=api_response(text)):
            out = self.ai.suggest_fix(self.context)
        self.assertEqual(out, {"explanation": text, "fixed_expr": None,
                               "confidence": "low", "actions": []})

    def test_null_fixed_expr_and_bad_confidence_normalized(self):
        reply = {"explanation": "install exporter", "fixed_expr": None,
                 "confidence": "certain", "actions": ["run YACE"]}
        with mock.patch("urllib.request.urlopen",
                        return_value=api_response(json.dumps(reply))):
            out = self.ai.suggest_fix(self.context)
        self.assertIsNone(out["fixed_expr"])
        self.assertEqual(out["confidence"], "low")
        self.assertEqual(out["actions"], ["run YACE"])

    def test_json_array_falls_back(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=api_response("[1, 2]")):
            out = self.ai.suggest_fix(self.context)
        self.assertEqual(out["explanation"], "[1, 2]")
        self.assertEqual(out["confidence"], "low")

    def test_request_shape(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=api_response(
                            json.dumps(FIX_JSON))) as m:
            self.ai.suggest_fix(self.context)
        req = m.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url,
                         "https://api.anthropic.com/v1/messages")
        self.assertEqual(req.get_header("X-api-key"), FAKE_KEY)
        self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")
        body = json.loads(req.data.decode())
        self.assertEqual(body["model"], DEFAULT_MODEL)
        self.assertEqual(body["max_tokens"], 2048)
        self.assertIn("STRICT JSON", body["system"])
        user = body["messages"][0]["content"]
        self.assertIn("http_request_total", user)
        self.assertIn("SELECT count(*)", user)
        self.assertIn("metrics_sample", json.dumps(self.context))


class ErrorMappingTests(unittest.TestCase):
    def setUp(self):
        self.ai = AIAssist(api_key=FAKE_KEY)
        self.msgs = [{"role": "user", "content": "hi"}]

    def test_401_maps_to_bad_key(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=http_error(401)):
            with self.assertRaises(AIError) as cm:
                self.ai.chat(self.msgs)
        msg = str(cm.exception)
        self.assertIn("401", msg)
        self.assertIn("key", msg.lower())
        self.assertNotIn(FAKE_KEY, msg)

    def test_429_maps_to_rate_limited(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=http_error(429)):
            with self.assertRaises(AIError) as cm:
                self.ai.chat(self.msgs)
        self.assertIn("rate limited", str(cm.exception).lower())

    def test_500_includes_detail(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=http_error(500, "internal")):
            with self.assertRaises(AIError) as cm:
                self.ai.chat(self.msgs)
        msg = str(cm.exception)
        self.assertIn("500", msg)
        self.assertIn("internal", msg)

    def test_network_unreachable(self):
        err = urllib.error.URLError(OSError("connection refused"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(AIError) as cm:
                self.ai.chat(self.msgs)
        self.assertIn("cannot reach", str(cm.exception))

    def test_timeout(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=socket.timeout()):
            with self.assertRaises(AIError) as cm:
                self.ai.chat(self.msgs)
        self.assertIn("timed out", str(cm.exception))


class ChatTests(unittest.TestCase):
    def test_returns_concatenated_text(self):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps({
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "tool_use", "name": "x"},
                {"type": "text", "text": "world"},
            ]}).encode()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        ai = AIAssist(api_key=FAKE_KEY)
        with mock.patch("urllib.request.urlopen", return_value=resp):
            out = ai.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "Hello world")

    def test_system_omitted_when_empty(self):
        ai = AIAssist(api_key=FAKE_KEY)
        with mock.patch("urllib.request.urlopen",
                        return_value=api_response("ok")) as m:
            ai.chat([{"role": "user", "content": "hi"}])
        body = json.loads(m.call_args[0][0].data.decode())
        self.assertNotIn("system", body)

    def test_system_included(self):
        ai = AIAssist(api_key=FAKE_KEY)
        with mock.patch("urllib.request.urlopen",
                        return_value=api_response("ok")) as m:
            ai.chat([{"role": "user", "content": "hi"}], system="be terse")
        body = json.loads(m.call_args[0][0].data.decode())
        self.assertEqual(body["system"], "be terse")


class HelperTests(unittest.TestCase):
    def test_strip_fences_plain(self):
        self.assertEqual(_strip_fences('{"a": 1}'), '{"a": 1}')

    def test_strip_fences_json_fence(self):
        self.assertEqual(_strip_fences('```json\n{"a": 1}\n```'),
                         '{"a": 1}')

    def test_parse_fix_missing_keys(self):
        out = _parse_fix('{"explanation": "hm"}')
        self.assertEqual(out, {"explanation": "hm", "fixed_expr": None,
                               "confidence": "low", "actions": []})

    def test_parse_fix_actions_coerced(self):
        out = _parse_fix(json.dumps({"explanation": "x",
                                     "fixed_expr": "up",
                                     "confidence": "medium",
                                     "actions": "not-a-list"}))
        self.assertEqual(out["actions"], [])
        self.assertEqual(out["confidence"], "medium")
        self.assertEqual(out["fixed_expr"], "up")


if __name__ == "__main__":
    unittest.main()
