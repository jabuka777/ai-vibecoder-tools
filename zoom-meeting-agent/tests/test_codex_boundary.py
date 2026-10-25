"""Real pinned Codex CLI → loopback mock Responses, with synthetic credentials.

This inspects the actual advertised model tools, rather than merely checking
command flags. It contacts no model. Missing/unsupported CLI is a skipped local
integration test; production llm.call rejects unsupported versions.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm


class CodexBoundaryIntegrationTests(unittest.TestCase):
    def test_actual_cli_exposes_no_filesystem_network_or_agent_tools(self):
        binary = shutil.which("codex")
        if not binary:
            self.skipTest("Codex CLI is not installed")
        # Preserve these standard paths if present; never replace HOME/CODEX_HOME.
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "HOME", "USER", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                          "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "CODEX_HOME", "LANG", "LC_ALL"}}
        env["MEETING_BOUNDARY_TEST_KEY"] = "synthetic-loopback-only"
        version = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                 timeout=10, env=env)
        if version.returncode or not re.search(r"\bcodex-cli 0\.164\.0\b", version.stdout):
            self.skipTest("Only Codex CLI 0.164.0 has a verified tool boundary")
        captured = []
        server_errors = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    if self.headers.get("Content-Encoding"):
                        raise ValueError("Request compression must be disabled for this probe")
                    payload = json.loads(raw)
                    captured.append({"authorization": self.headers.get("Authorization"),
                                     "path": self.path, "payload": payload})
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    item = {"id": "msg_synthetic", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "boundary-ok", "annotations": []}]}
                    events = [
                        {"type": "response.created", "response": {"id": "resp_synthetic", "status": "in_progress", "output": []}},
                        {"type": "response.output_item.added", "output_index": 0,
                         "item": {**item, "status": "in_progress", "content": []}},
                        {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                         "content_index": 0, "delta": "boundary-ok"},
                        {"type": "response.output_item.done", "output_index": 0, "item": item},
                        {"type": "response.completed", "response": {"id": "resp_synthetic", "status": "completed",
                         "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
                    ]
                    for event in events:
                        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    self.close_connection = True
                except Exception as exc:
                    # Never include a captured request or authentication material in failure output.
                    server_errors.append(type(exc).__name__)
                    self.send_error(400)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix="meeting-boundary-") as directory:
                folder = Path(directory)
                output = folder / "answer.txt"
                (folder / "instructions.txt").write_text(llm.TEXT_ROLE, encoding="utf-8")
                extra = {
                    "model_provider": "meeting_boundary_probe",
                    "model_providers.meeting_boundary_probe.name": "meeting-boundary-probe",
                    "model_providers.meeting_boundary_probe.base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "model_providers.meeting_boundary_probe.wire_api": "responses",
                    "model_providers.meeting_boundary_probe.env_key": "MEETING_BOUNDARY_TEST_KEY",
                    "model_providers.meeting_boundary_probe.requires_openai_auth": False,
                    "features.enable_request_compression": False,
                }
                cmd = llm.codex_command(binary, "gpt-6-luna", output, extra)
                completed = subprocess.run(cmd, input="Synthetic boundary test. Reply boundary-ok.",
                                           text=True, capture_output=True, cwd=folder, env=env, timeout=35)
                self.assertEqual(completed.returncode, 0,
                                 "CLI failed; stdout/stderr deliberately suppressed to protect local context")
                self.assertEqual(server_errors, [])
                self.assertEqual(len(captured), 1, "Expected exactly one request to the mock provider")
                self.assertTrue(output.is_file(), "CLI did not write its final message")
                self.assertEqual(output.read_text(encoding="utf-8").strip(), "boundary-ok")
                self.assertIn('"type":"turn.completed"', completed.stdout.replace(" ", ""))
                self.assertNotIn("Unknown config", completed.stderr)
                for request in captured:
                    # Comparing booleans prevents unittest from printing unexpected auth values.
                    self.assertTrue(request["authorization"] == "Bearer synthetic-loopback-only",
                                    "Provider authentication was not the synthetic test credential")
                    self.assertEqual(request["path"], "/v1/responses")
                    self.assertIn(llm.TEXT_ROLE, json.dumps(request["payload"], ensure_ascii=False))
                    tools = request["payload"].get("tools", [])
                    names = []
                    for tool in tools:
                        name = tool.get("name", (tool.get("function") or {}).get("name"))
                        self.assertIn(tool.get("type"), {"function", "custom"}, "Unexpected built-in model capability")
                        names.append(name)
                    self.assertLessEqual(set(names), {"request_user_input"},
                                         "Model advertised an unexpected capability")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
