"""MCP boundaries and lifecycle on synthetic files/mocked Zoom, never a live call."""
import asyncio
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_server as bridge

PROFILE = {"version": 1, "meeting": "123456789", "password": "synthetic-private-password",
           "name": "Synthetic test assistant", "minutes": 2}
SESSION = "a" * 32
CORE = "2026-10-27_120000-123456789-web-1234abcd"


@unittest.skipUnless(bridge.FILE_SUPPORT, "MCP private file boundary requires POSIX directory handles")
class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        # macOS /var is an OS symlink; use its canonical temporary root without
        # weakening the production rejection of links inside the private store.
        self.home = Path(self.temporary.name).resolve() / "home"
        self.manager = bridge.MeetingTools(self.home)
        self.store = self.manager.store

    def tearDown(self):
        self.temporary.cleanup()

    def profile(self):
        self.store.write_json(("meetings", "test.json"), PROFILE)

    def recording(self, *, state="stopped", complete=True):
        self.store.write_json(("runs", SESSION, "worker.json"),
                              {"state": state, "heartbeat": time.time(), "exit_code": 0})
        core = ("runs", SESSION, "sessions", CORE)
        self.store.write_json((*core, "status.json"), {"state": "complete" if complete else "recording",
                "recording_complete": complete, "incomplete": False, "problems": []})
        return core

    def test_construction_doctor_and_empty_lists_never_create_home(self):
        self.assertFalse(self.home.exists())
        with patch.object(bridge.subprocess, "Popen") as spawn:
            self.manager.doctor()
            self.assertEqual(self.manager.list_sessions()["sessions"], [])
            self.assertEqual(self.manager.list_meeting_profiles()["profiles"], [])
        spawn.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_profile_schema_has_no_commands_paths_or_optional_llm(self):
        for bad in ({**PROFILE, "command": "anything"}, {**PROFILE, "llm": "codex"},
                    {**PROFILE, "meeting": "../123456789"}, {**PROFILE, "minutes": float("nan")},
                    {**PROFILE, "minutes": True}, {**PROFILE, "minutes": 481},
                    {**PROFILE, "password": "a\nb"}, {**PROFILE, "name": "\x1bescape"}):
            with self.subTest(bad=list(bad)), self.assertRaises(bridge.BoundaryError):
                bridge.validate_profile(bad)
        self.assertEqual(bridge.validate_profile({"meeting": "123456789"})["minutes"], 180)

    def test_confirmation_is_required_before_any_write_or_spawn(self):
        self.profile()
        with patch("runtime.start_process") as spawn:
            result = self.manager.start_web("test")
        self.assertEqual(result["error"], "recording_confirmation_required")
        spawn.assert_not_called()
        self.assertFalse((self.home / "runs").exists())

    def test_start_is_secret_free_no_llm_and_creates_private_unique_runs(self):
        self.profile()
        process = Mock(poll=Mock(return_value=None))
        with patch("runtime.start_process", return_value=process) as spawn, \
                patch.dict(os.environ, {"OPENAI_API_KEY": "never-copy-this", "TELEGRAM_TOKEN": "never-copy-this",
                                       "PLAYWRIGHT_BROWSERS_PATH": "synthetic browser cache"}):
            first = self.manager.start_web("test", True)
            second = self.manager.start_web("test", True)
        self.assertTrue(first["ok"])
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["llm"], "none")
        self.assertFalse(first["recording_complete"])
        command = spawn.call_args.args[0]
        serialized = json.dumps(command)
        self.assertNotIn(PROFILE["password"], serialized)
        self.assertNotIn(PROFILE["meeting"], serialized)
        self.assertNotIn(PROFILE["name"], serialized)
        options = spawn.call_args.kwargs
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertNotIn("TELEGRAM_TOKEN", options["env"])
        self.assertEqual(options["env"]["PLAYWRIGHT_BROWSERS_PATH"], "synthetic browser cache")
        self.assertEqual(options["stdout"], bridge.subprocess.DEVNULL)
        self.assertEqual(options["stderr"], bridge.subprocess.DEVNULL)
        for parent in (self.home / "runs", self.home / "runs" / first["session_id"]):
            self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.home / "runs" / first["session_id"] / "meeting.json").stat().st_mode & 0o777, 0o600)

    def test_failed_spawn_is_listed_without_exposing_exception_or_password(self):
        self.profile()
        with patch("runtime.start_process", side_effect=OSError(PROFILE["password"])):
            result = self.manager.start_web("test", True)
        self.assertEqual(result["error"], "worker_start_failed")
        listed = self.manager.list_sessions()
        self.assertEqual(listed["sessions"][0]["worker_state"], "failed")
        self.assertNotIn(PROFILE["password"], json.dumps([result, listed]))
        self.assertFalse((self.home / "runs" / result["session_id"] / "meeting.json").exists())

    def test_two_active_limit_is_shared_across_managers_and_failures_release_slots(self):
        self.profile()
        other = bridge.MeetingTools(self.home)
        with patch("runtime.start_process", side_effect=OSError("synthetic start failure")):
            failed = self.manager.start_web("test", True)
        self.assertEqual(failed["error"], "worker_start_failed")
        with patch("runtime.start_process", return_value=Mock()) as spawn:
            first = self.manager.start_web("test", True)
            second = other.start_web("test", True)
            rejected = self.manager.start_web("test", True)
            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(rejected["error"], "active_recording_limit")
            self.assertEqual(spawn.call_count, 2)
            self.store.write_json(("runs", first["session_id"], "worker.json"),
                                  {"state": "stopped", "heartbeat": time.time(), "exit_code": 0})
            self.assertTrue(other.start_web("test", True)["ok"])
            self.assertEqual(spawn.call_count, 3)

    def test_unresponsive_workers_continue_to_consume_slots(self):
        self.profile()
        for session_id, state in (("a" * 32, "running"), ("b" * 32, "stopping")):
            self.store.write_json(("runs", session_id, "worker.json"),
                                  {"state": state, "heartbeat": time.time() - 10000})
        with patch("runtime.start_process") as spawn:
            self.assertEqual(self.manager.start_web("test", True)["error"], "active_recording_limit")
        spawn.assert_not_called()

    def test_unreadable_existing_worker_state_requires_manual_review(self):
        self.profile()
        self.store.write(("runs", SESSION, "unrelated.txt"), b"synthetic incomplete startup")
        with patch("runtime.start_process") as spawn:
            result = self.manager.start_web("test", True)
        self.assertEqual(result["error"], "session_state_requires_manual_review")
        spawn.assert_not_called()

    def test_run_preparation_failure_is_terminal_and_does_not_hold_slot(self):
        self.profile()
        original = self.store.write_json

        def fail_profile(parts, *args, **kwargs):
            if parts[-1] == "meeting.json":
                raise OSError("synthetic private file failure")
            return original(parts, *args, **kwargs)

        with patch.object(self.store, "write_json", side_effect=fail_profile), patch("runtime.start_process") as spawn:
            result = self.manager.start_web("test", True)
        self.assertEqual(result["error"], "worker_start_failed")
        self.assertEqual(self.manager.session_status(result["session_id"])["worker_state"], "failed")
        spawn.assert_not_called()
        with patch("runtime.start_process", return_value=Mock()):
            self.assertTrue(self.manager.start_web("test", True)["ok"])

    def test_cross_process_start_lock_prevents_competing_admission(self):
        self.profile()
        code = '''import sys
sys.path.insert(0, sys.argv[1])
from mcp_server import PrivateStore
with PrivateStore(sys.argv[2]).start_guard():
    print("locked", flush=True)
    sys.stdin.readline()
'''
        process = subprocess.Popen([sys.executable, "-c", code, str(bridge.HERE), str(self.home)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            import selectors
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=10), "synthetic lock child did not initialize")
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with patch("runtime.start_process") as spawn:
                self.assertEqual(self.manager.start_web("test", True)["error"], "start_in_progress")
            spawn.assert_not_called()
            process.communicate("release\n", timeout=5)
            self.assertEqual(process.returncode, 0)
            with patch("runtime.start_process", return_value=Mock()):
                self.assertTrue(self.manager.start_web("test", True)["ok"])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_ids_and_result_kind_cannot_be_paths(self):
        self.recording()
        for value in ("../", "/tmp", SESSION + "/..", "A" * 32, "", "\x00"):
            for operation in (self.manager.session_status, self.manager.stop_session, self.manager.read_result):
                self.assertFalse(operation(value)["ok"])
        self.assertFalse(self.manager.read_result(SESSION, "../../meeting")["ok"])
        self.assertFalse(self.manager.start_web("../test", True)["ok"])

    def test_symlink_profile_is_rejected_and_omitted_from_listing(self):
        self.profile()
        profile = self.home / "meetings" / "test.json"
        target = self.home / "private-copy"
        profile.rename(target)
        profile.symlink_to(target)
        self.assertFalse(self.manager.start_web("test", True)["ok"])
        self.assertEqual(self.manager.list_meeting_profiles()["profiles"], [])

    def test_symlink_parent_and_result_are_not_followed(self):
        core = self.recording()
        secret = Path(self.temporary.name) / "outside"
        secret.write_text("must not be read")
        secret.chmod(0o600)
        transcript = self.home.joinpath(*core, "transcript.md")
        transcript.symlink_to(secret)
        self.assertFalse(self.manager.read_result(SESSION)["ok"])
        transcript.unlink()
        sessions = self.home / "runs" / SESSION / "sessions"
        moved = self.home / "moved-sessions"
        sessions.rename(moved)
        sessions.symlink_to(moved, target_is_directory=True)
        self.assertFalse(self.manager.session_status(SESSION)["ok"])

    def test_hardlinks_and_world_readable_profiles_are_rejected(self):
        self.profile()
        profile = self.home / "meetings" / "test.json"
        profile.chmod(0o644)
        self.assertFalse(self.manager.start_web("test", True)["ok"])
        profile.chmod(0o600)
        os.link(profile, self.home / "extra-link")
        self.assertFalse(self.manager.start_web("test", True)["ok"])

    def test_fifos_are_rejected_without_blocking(self):
        self.recording()
        transcript = self.home / "runs" / SESSION / "sessions" / CORE / "transcript.md"
        os.mkfifo(transcript, 0o600)
        self.assertFalse(self.manager.read_result(SESSION)["ok"])

    def test_stop_after_reconnect_uses_request_file_not_pid_or_external_signal(self):
        self.recording(state="running", complete=False)
        # A malicious/old PID field is ignored rather than used for signalling.
        self.store.write_json(("runs", SESSION, "worker.json"),
                              {"state": "running", "heartbeat": time.time(), "pid": 1})
        reconnected = bridge.MeetingTools(self.home)
        with patch.object(os, "kill") as kill, patch.object(bridge.subprocess, "Popen") as spawn:
            result = reconnected.stop_session(SESSION)
        self.assertTrue(result["stop_requested"])
        self.assertFalse(result["stopped"])
        self.assertEqual(self.store.read(("runs", SESSION, "stop.request")), b"stop\n")
        kill.assert_not_called()
        spawn.assert_not_called()

    def test_stop_request_symlink_is_not_followed_or_replaced(self):
        self.recording(state="running", complete=False)
        victim = Path(self.temporary.name) / "victim"
        victim.write_text("unchanged")
        (self.home / "runs" / SESSION / "stop.request").symlink_to(victim)
        self.assertFalse(self.manager.stop_session(SESSION)["ok"])
        self.assertEqual(victim.read_text(), "unchanged")

    def test_stale_heartbeat_does_not_report_connected_or_complete(self):
        self.recording(state="running", complete=False)
        self.store.write_json(("runs", SESSION, "worker.json"),
                              {"state": "running", "heartbeat": time.time() - 60})
        result = self.manager.session_status(SESSION)
        self.assertEqual(result["worker_state"], "unresponsive")
        self.assertFalse(result["recording_complete"])

    def test_status_fields_are_allowlisted(self):
        core = self.recording()
        self.store.write_json((*core, "status.json"), {"state": "complete", "recording_complete": True,
                "incomplete": False, "password": PROFILE["password"], "reason": PROFILE["password"],
                "problems": [PROFILE["password"], "audio_not_received"]})
        result = self.manager.session_status(SESSION)
        self.assertNotIn(PROFILE["password"], json.dumps(result))
        self.assertEqual(result["problems"], ["audio_not_received"])

    def test_unicode_pagination_preserves_all_transcript_characters(self):
        core = self.recording()
        text = "Привет 🌍. " * 100
        self.store.write((*core, "transcript.md"), text.encode())
        parts, offset = [], 0
        while offset is not None:
            result = self.manager.read_result(SESSION, offset=offset, limit=17)
            self.assertTrue(result["ok"])
            self.assertLessEqual(len(result["text"]), 17)
            parts.append(result["text"])
            offset = result["next_offset"]
        self.assertEqual("".join(parts), text)
        self.assertEqual(result["artifact_state"], "complete")

    def test_partial_transcript_is_labelled_and_read_limits_enforced(self):
        core = self.recording(state="running", complete=False)
        self.store.write((*core, "transcript.md"), b"partial transcript")
        self.assertEqual(self.manager.read_result(SESSION)["artifact_state"], "partial")
        for params in ({"offset": -1}, {"limit": 12001}, {"limit": True}, {"offset": bridge.MAX_ARTIFACT + 1}):
            self.assertFalse(self.manager.read_result(SESSION, **params)["ok"])

    def test_summary_requires_current_digest_and_complete_minutes_status(self):
        core = self.recording()
        summary = b"synthetic verified summary"
        self.store.write((*core, "summary.md"), summary)
        result = self.manager.read_result(SESSION, "summary")
        self.assertEqual(result["artifact_state"], "stale")
        self.assertFalse(result["available"])
        self.store.write_json((*core, "minutes-status.json"), {"status": "complete",
                             "summary_md_sha256": hashlib.sha256(summary).hexdigest()})
        self.assertEqual(self.manager.read_result(SESSION, "summary")["text"], summary.decode())
        self.store.write((*core, "summary.md"), summary + b" changed")
        self.assertFalse(self.manager.read_result(SESSION, "summary")["available"])

    def test_worker_calls_existing_webjoin_with_llm_none_and_removes_copied_secrets(self):
        self.profile()
        with patch("runtime.start_process", return_value=Mock()):
            result = self.manager.start_web("test", True)
        run_id = result["session_id"]
        with patch("webjoin.main", return_value=0) as main, patch.dict(os.environ):
            old_mask = os.umask(0o077)
            try:
                self.assertEqual(bridge.run_worker(self.home, run_id), 0)
            finally:
                os.umask(old_mask)
        args = main.call_args.args[0]
        self.assertEqual(args[args.index("--llm") + 1], "none")
        self.assertNotIn("--pwd", args)
        self.assertNotIn(PROFILE["password"], args)
        self.assertEqual(self.manager.session_status(run_id)["worker_state"], "stopped")
        self.assertFalse((self.home / "runs" / run_id / ".meeting-password").exists())
        self.assertFalse((self.home / "runs" / run_id / "meeting.json").exists())

    def test_stop_is_delivered_inside_owned_worker_and_final_state_is_persisted(self):
        self.profile()
        with patch("runtime.start_process", return_value=Mock()):
            result = self.manager.start_web("test", True)
        run_id = result["session_id"]
        self.manager.stop_session(run_id)
        delivered = threading.Event()

        def capture(_):
            self.assertTrue(delivered.wait(3))
            return 0

        def stop(sig):
            self.assertEqual(sig, signal.SIGINT)
            delivered.set()

        with patch("webjoin.main", side_effect=capture), patch.object(signal, "raise_signal", side_effect=stop), \
                patch.dict(os.environ):
            old_mask = os.umask(0o077)
            try:
                self.assertEqual(bridge.run_worker(self.home, run_id), 0)
            finally:
                os.umask(old_mask)
        self.assertEqual(self.manager.session_status(run_id)["worker_state"], "stopped")


try:
    SDK_AVAILABLE = importlib.metadata.version("mcp") == "2.0.0"
except importlib.metadata.PackageNotFoundError:
    SDK_AVAILABLE = False


@unittest.skipUnless(SDK_AVAILABLE, "install optional requirements-mcp.txt for official SDK integration")
class SDKTests(unittest.TestCase):
    def test_official_sdk_tools_and_annotations(self):
        async def exercise(home):
            server = bridge.create_server(home)
            tools = {tool.name: tool for tool in await server.list_tools()}
            self.assertEqual(set(tools), {"doctor", "list_meeting_profiles", "list_sessions", "session_status",
                                         "read_result", "start_web", "stop_session"})
            self.assertTrue(tools["read_result"].annotations.read_only_hint)
            self.assertFalse(tools["start_web"].annotations.read_only_hint)
            result = await server.call_tool("start_web", {"profile_id": "test"})
            encoded = result.model_dump_json()
            self.assertIn("recording_confirmation_required", encoded)

        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as stdout:
            home = Path(folder).resolve() / "not-created"
            asyncio.run(exercise(home))
            self.assertFalse(home.exists())
            self.assertEqual(stdout.getvalue(), "")

    def test_stdio_roundtrip_does_not_create_home_or_emit_non_protocol_stdout(self):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async def exercise(home):
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(bridge.HERE / "mcp_server.py"), "--home", str(home)])
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=10) as client:
                    await client.initialize()
                    result = await client.list_tools()
                    self.assertEqual(len(result.tools), 7)
                    result = await client.call_tool("doctor", {})
                    self.assertFalse(result.is_error)
                    self.assertFalse(json.loads(result.content[0].text)["zoom_join_checked"])

        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve() / "not-created"
            asyncio.run(exercise(home))
            self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
