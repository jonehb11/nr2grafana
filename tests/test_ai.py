"""Tests for nr2grafana.ai (Claude API + local console assistance)."""

import io
import json
import shlex
import socket
import sys
import unittest
import urllib.error
from unittest import mock

from nr2grafana.ai import (AIAssist, AIError, DEFAULT_MODEL, LocalAgent,
                           get_assistant, _parse_fix, _parse_fix_loose,
                           _render_prompt, _strip_ansi, _strip_echo,
                           _strip_fences)

FAKE_KEY = "sk-ant-test-key-do-not-log"

PY = shlex.quote(sys.executable)


def cli(code):
    """Command string running ``code`` as a fake console AI agent."""
    return "%s -c %s" % (PY, shlex.quote(code))


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

    def test_parse_fix_loose_extracts_json_from_prose(self):
        raw = "Sure! Here you go:\n%s\nHope that helps." \
            % json.dumps(FIX_JSON)
        self.assertEqual(_parse_fix_loose(raw), FIX_JSON)

    def test_parse_fix_loose_plain_prose_falls_back(self):
        out = _parse_fix_loose("no json here at all")
        self.assertEqual(out["explanation"], "no json here at all")
        self.assertEqual(out["confidence"], "low")

    def test_strip_ansi(self):
        self.assertEqual(
            _strip_ansi("\x1b[1;31mred\x1b[0m \x1b]0;title\x07ok"),
            "red ok")

    def test_strip_echo_whole_lines_only(self):
        self.assertEqual(_strip_echo("ping\nANSWER", "ping"),
                         "ANSWER")
        self.assertEqual(_strip_echo("ANSWER\nping", "ping"),
                         "ANSWER")
        # an echo embedded in a line is NOT trivially detectable
        self.assertEqual(_strip_echo("ARGV:two words", "two words"),
                         "ARGV:two words")

    def test_render_prompt_single_user_turn_verbatim(self):
        self.assertEqual(
            _render_prompt([{"role": "user", "content": "hi"}]),
            "hi")
        self.assertEqual(
            _render_prompt([{"role": "user", "content": "hi"}],
                           system="SYS"),
            "SYS\n\nhi")

    def test_render_prompt_labels_multi_turn(self):
        out = _render_prompt(
            [{"role": "user", "content": "q1"},
             {"role": "assistant", "content": "a1"},
             {"role": "user", "content": "q2"}])
        self.assertIn("User: q1", out)
        self.assertIn("Assistant: a1", out)
        self.assertIn("User: q2", out)
        self.assertTrue(out.endswith("Assistant:"))


class LocalAgentRunTests(unittest.TestCase):
    """LocalAgent subprocess plumbing against real tiny CLIs."""

    def test_available(self):
        self.assertTrue(LocalAgent("some-cli").available)
        self.assertFalse(LocalAgent("").available)
        self.assertFalse(LocalAgent("   ").available)

    def test_prompt_substituted_as_single_argv_element(self):
        cmd = cli("import sys; print('ARGV:' + sys.argv[1])") \
            + " {prompt}"
        out = LocalAgent(cmd, timeout=30).chat(
            [{"role": "user", "content": "two words"}])
        self.assertEqual(out, "ARGV:two words")

    def test_stdin_mode_without_placeholder(self):
        cmd = cli("import sys;"
                  " print('STDIN:' + sys.stdin.read().strip())")
        out = LocalAgent(cmd, timeout=30).chat(
            [{"role": "user", "content": "hello"}])
        self.assertEqual(out, "STDIN:hello")

    def test_system_prompt_reaches_command(self):
        cmd = cli("import sys; print(sys.stdin.read())")
        out = LocalAgent(cmd, timeout=30).chat(
            [{"role": "user", "content": "question"}],
            system="SYSTEM RULES")
        self.assertIn("SYSTEM RULES", out)
        self.assertIn("question", out)

    def test_ansi_stripped_from_output(self):
        cmd = cli("import sys;"
                  " sys.stdout.write('\\x1b[31manswer\\x1b[0m\\n')")
        out = LocalAgent(cmd, timeout=30).chat(
            [{"role": "user", "content": "q"}])
        self.assertEqual(out, "answer")

    def test_leading_prompt_echo_stripped(self):
        cmd = cli("import sys; d = sys.stdin.read().strip();"
                  " print(d); print('ANSWER')")
        out = LocalAgent(cmd, timeout=30).chat(
            [{"role": "user", "content": "ping"}])
        self.assertEqual(out, "ANSWER")

    def test_output_capped(self):
        cmd = cli("print('x' * 300000)")
        out = LocalAgent(cmd, timeout=30).chat(
            [{"role": "user", "content": "q"}])
        self.assertLessEqual(len(out), 200 * 1024)
        self.assertGreater(len(out), 100 * 1024)

    def test_nonzero_exit_raises_actionable_error(self):
        cmd = cli("import sys; sys.stderr.write('kaboom details');"
                  " sys.exit(3)")
        with self.assertRaises(AIError) as cm:
            LocalAgent(cmd, timeout=30).chat(
                [{"role": "user", "content": "q"}])
        msg = str(cm.exception)
        self.assertIn("code 3", msg)
        self.assertIn("kaboom details", msg)
        self.assertIn("PATH", msg)

    def test_missing_binary_raises_actionable_error(self):
        agent = LocalAgent("definitely-not-a-real-cli-98765")
        with self.assertRaises(AIError) as cm:
            agent.chat([{"role": "user", "content": "q"}])
        msg = str(cm.exception)
        self.assertIn("definitely-not-a-real-cli-98765", msg)
        self.assertIn("PATH", msg)

    def test_timeout_raises_actionable_error(self):
        cmd = cli("import time; time.sleep(10)")
        with self.assertRaises(AIError) as cm:
            LocalAgent(cmd, timeout=1).chat(
                [{"role": "user", "content": "q"}])
        msg = str(cm.exception)
        self.assertIn("timed out", msg)
        self.assertIn("1 second", msg)
        self.assertIn(sys.executable, msg)

    def test_empty_command_raises(self):
        with self.assertRaises(AIError) as cm:
            LocalAgent("").chat([{"role": "user", "content": "q"}])
        self.assertIn("command", str(cm.exception))


class LocalAgentSuggestFixTests(unittest.TestCase):
    CONTEXT = {"panel": "Error rate", "expr": "uup",
               "error": "unknown metric", "datasource": "prometheus"}

    def _agent_printing(self, text):
        code = "import sys; sys.stdin.read(); print(%r)" % text
        return LocalAgent(cli(code), timeout=30)

    def test_plain_json_reply(self):
        out = self._agent_printing(
            json.dumps(FIX_JSON)).suggest_fix(self.CONTEXT)
        self.assertEqual(out, FIX_JSON)

    def test_fenced_json_reply(self):
        text = "```json\n%s\n```" % json.dumps(FIX_JSON)
        out = self._agent_printing(text).suggest_fix(self.CONTEXT)
        self.assertEqual(out, FIX_JSON)

    def test_json_wrapped_in_prose(self):
        text = "Here is my analysis:\n%s\nGood luck!" \
            % json.dumps(FIX_JSON)
        out = self._agent_printing(text).suggest_fix(self.CONTEXT)
        self.assertEqual(out, FIX_JSON)

    def test_non_json_falls_back(self):
        text = "The metric name looks wrong to me."
        out = self._agent_printing(text).suggest_fix(self.CONTEXT)
        self.assertEqual(out["explanation"], text)
        self.assertIsNone(out["fixed_expr"])
        self.assertEqual(out["confidence"], "low")


class LocalAgentTestProbeTests(unittest.TestCase):
    def test_probe_ok(self):
        res = LocalAgent(cli("import sys; sys.stdin.read();"
                             " print('OK')"), timeout=30).test()
        self.assertTrue(res["ok"])
        self.assertEqual(res["reply_excerpt"], "OK")
        self.assertIsInstance(res["latency_ms"], int)
        self.assertGreaterEqual(res["latency_ms"], 0)
        self.assertNotIn("error", res)

    def test_probe_failure_never_raises(self):
        res = LocalAgent(cli("import sys; sys.exit(9)"),
                         timeout=30).test()
        self.assertFalse(res["ok"])
        self.assertIn("code 9", res["error"])
        self.assertEqual(res["reply_excerpt"], "")

    def test_probe_wrong_reply_not_ok(self):
        res = LocalAgent(cli("import sys; sys.stdin.read();"
                             " print('nope')"), timeout=30).test()
        self.assertFalse(res["ok"])
        self.assertEqual(res["reply_excerpt"], "nope")
        self.assertIn("stdout", res["error"])


class GetAssistantTests(unittest.TestCase):
    def test_none_when_nothing_configured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(get_assistant())

    def test_api_key_arg_wins(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            ai = get_assistant(api_key=FAKE_KEY, command="my-cli")
        self.assertIsInstance(ai, AIAssist)
        self.assertTrue(ai.available)

    def test_env_key_wins_over_command(self):
        with mock.patch.dict("os.environ",
                             {"ANTHROPIC_API_KEY": FAKE_KEY},
                             clear=True):
            ai = get_assistant(command="my-cli")
        self.assertIsInstance(ai, AIAssist)

    def test_command_only_gives_local_agent(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            ai = get_assistant(command="claude -p {prompt}")
        self.assertIsInstance(ai, LocalAgent)
        self.assertEqual(ai.command, "claude -p {prompt}")
        self.assertTrue(ai.available)

    def test_blank_command_gives_none(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(get_assistant(command="   "))

    def test_model_passed_through(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            ai = get_assistant(api_key=FAKE_KEY,
                               model="claude-haiku-4-5")
        self.assertEqual(ai.model, "claude-haiku-4-5")


if __name__ == "__main__":
    unittest.main()
