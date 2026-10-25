"""Failure and capability-boundary tests; no real model or authentication."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm


class LlmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.metrics = Path(self.temp.name) / "metrics.jsonl"
        self.which = patch.object(llm.shutil, "which", return_value="/synthetic/cli")
        self.which.start()
        self.addCleanup(self.which.stop)

    def call(self, backend="claude", **kwargs):
        return llm.call("synthetic transcript", backend=backend, metrics_path=self.metrics, **kwargs)

    def result(self, output="", code=0):
        return subprocess.CompletedProcess(["synthetic"], code, output, "DO_NOT_LEAK_STDERR")

    def codex_runner(self, events, answer="answer", code=0):
        def run(cmd, **kwargs):
            if cmd[-1] == "--version":
                return self.result("codex-cli 0.164.0")
            if answer is not None:
                Path(cmd[cmd.index("--output-last-message") + 1]).write_text(answer, encoding="utf-8")
            return self.result("\n".join(json.dumps(e) for e in events), code)
        return run

    def test_no_implicit_cloud_backend_or_unknown_backend(self):
        with patch.object(llm.subprocess, "run") as run:
            for backend in (None, "none", "", "auto", "unknown"):
                with self.subTest(backend=backend), self.assertRaises(llm.ModelError):
                    self.call(backend=backend)
            run.assert_not_called()

    def test_claude_has_empty_tools_empty_mcp_and_ignores_hooks_and_settings(self):
        def run(cmd, **kwargs):
            self.assertEqual(cmd[cmd.index("--tools") + 1], "")
            self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
            self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "mcp__*")
            self.assertIn("--strict-mcp-config", cmd)
            self.assertIn("--disable-slash-commands", cmd)
            self.assertIn("--no-session-persistence", cmd)
            self.assertEqual(json.loads(cmd[cmd.index("--settings") + 1]), {"disableAllHooks": True})
            mcp = Path(cmd[cmd.index("--mcp-config") + 1])
            self.assertEqual(json.loads(mcp.read_text(encoding="utf-8")), {"mcpServers": {}})
            self.assertEqual(mcp.parent, kwargs["cwd"])
            self.assertEqual(kwargs["input"], "synthetic transcript")
            return self.result(json.dumps({"subtype": "success", "is_error": False,
                                           "result": "  answer  ", "usage": {"input_tokens": 3}}))

        with patch.object(llm.subprocess, "run", side_effect=run):
            self.assertEqual(self.call(), "answer")
        metric = json.loads(self.metrics.read_text(encoding="utf-8"))
        self.assertTrue(metric["success"])
        self.assertNotIn("synthetic transcript", self.metrics.read_text(encoding="utf-8"))

    def test_child_does_not_inherit_meeting_or_telegram_secrets(self):
        with patch.dict(os.environ, {"ZOOM_SDK_CLIENT_SECRET": "do-not-pass",
                                     "TELEGRAM_BOT_TOKEN": "do-not-pass", "AGENT_SECRET": "do-not-pass",
                                     "UNRELATED_TOKEN": "do-not-pass"}):
            child = llm.child_environment()
        for key in ("ZOOM_SDK_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "AGENT_SECRET", "UNRELATED_TOKEN"):
            self.assertNotIn(key, child)

    def test_nonzero_timeout_invalid_json_or_quota_are_not_success(self):
        responses = [self.result("DO_NOT_LEAK_STDOUT", 17), self.result("not json"),
                     self.result("null"), self.result("[]"), self.result("42"),
                     self.result(json.dumps({"subtype": "error_during_execution", "is_error": True,
                                             "result": "Quota exhausted: DO_NOT_LEAK_STDOUT"})),
                     self.result(json.dumps({"subtype": "success", "is_error": False, "result": "  "})),
                     self.result(json.dumps({"subtype": "success", "is_error": False, "result": {"text": "bad"}}))]
        for response in responses:
            with self.subTest(response=response.stdout[:20]), patch.object(llm.subprocess, "run", return_value=response):
                with self.assertRaises(llm.ModelError) as exc:
                    self.call()
                self.assertNotIn("DO_NOT_LEAK", str(exc.exception))
        timeout = subprocess.TimeoutExpired(["synthetic"], 1, output="DO_NOT_LEAK_STDOUT", stderr="DO_NOT_LEAK_STDERR")
        with patch.object(llm.subprocess, "run", side_effect=timeout), self.assertRaises(llm.ModelError) as exc:
            self.call()
        self.assertNotIn("DO_NOT_LEAK", str(exc.exception))
        for metric in self.metrics.read_text(encoding="utf-8").splitlines():
            self.assertFalse(json.loads(metric)["success"])

    def test_codex_rejects_tool_events_even_with_success_and_answer_file(self):
        for kind in ("command_execution", "mcp_tool_call", "web_search", "file_change", "collab_tool_call"):
            events = [{"type": "item.completed", "item": {"type": kind}}, {"type": "turn.completed"}]
            with self.subTest(kind=kind), patch.object(llm.subprocess, "run", side_effect=self.codex_runner(events)):
                with self.assertRaises(llm.ModelError):
                    self.call("codex")

    def test_codex_rejects_failed_missing_or_empty_results(self):
        cases = [([{"type": "error", "message": "DO_NOT_LEAK_QUOTA"}, {"type": "turn.completed"}], "answer", 0),
                 ([{"type": "turn.failed"}, {"type": "turn.completed"}], "answer", 0),
                 ([], "answer", 0), ([{"type": "turn.completed"}], None, 0),
                 ([{"type": "turn.completed"}], " ", 0), ([{"type": "turn.completed"}], "answer", 2)]
        for events, answer, code in cases:
            with self.subTest(events=events, answer=answer, code=code):
                with patch.object(llm.subprocess, "run", side_effect=self.codex_runner(events, answer, code)):
                    with self.assertRaises(llm.ModelError) as exc:
                        self.call("codex")
                    self.assertNotIn("DO_NOT_LEAK", str(exc.exception))

    def test_codex_malformed_event_shapes_fail_with_model_error(self):
        for malformed in (None, [], 42, {"type": "item.completed", "item": []},
                          {"type": "item.completed", "item": "not-an-object"}):
            with self.subTest(shape=type(malformed).__name__):
                events = [malformed, {"type": "turn.completed"}]
                with patch.object(llm.subprocess, "run", side_effect=self.codex_runner(events)):
                    with self.assertRaises(llm.ModelError):
                        self.call("codex")

    def test_codex_requires_verified_cli_version_before_request(self):
        for stdout, code in (("codex-cli 0.163.0", 0), ("codex-cli 0.164.1", 0), ("codex-cli 0.165.0", 0),
                             ("codex-cli unknown", 0), ("codex-cli 0.164.0", 1)):
            with patch.object(llm.subprocess, "run", return_value=self.result(stdout, code)) as run:
                with self.subTest(version=stdout), self.assertRaises(llm.ModelError):
                    self.call("codex")
                self.assertEqual(run.call_count, 1)

    def test_codex_accepts_only_completed_text_and_records_usage(self):
        events = [{"type": "thread.started", "thread_id": "synthetic"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
                  {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 1}}]
        with patch.object(llm.subprocess, "run", side_effect=self.codex_runner(events)):
            self.assertEqual(self.call("codex"), "answer")
        metric = json.loads(self.metrics.read_text(encoding="utf-8"))
        self.assertTrue(metric["success"])
        self.assertEqual(metric["usage"]["output_tokens"], 1)


if __name__ == "__main__":
    unittest.main()
