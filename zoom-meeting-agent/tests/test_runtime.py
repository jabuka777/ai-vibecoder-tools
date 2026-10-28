"""Synthetic lifecycle regressions: no Zoom, LLM, STT model or credentials."""
import contextlib
import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime
import webjoin
import bot
import agent


class Locator:
    def __init__(self, text="", count=0, visible=False):
        self.text, self.number, self.visible = text, count, visible
        self.first = self

    def inner_text(self):
        return self.text

    def count(self):
        return self.number

    def is_visible(self):
        return self.visible


class Page:
    def __init__(self, text, controls=0, visible=False):
        self.body = Locator(text)
        self.controls = Locator(count=controls, visible=visible)

    def locator(self, selector):
        return self.body if selector == "body" else self.controls


class PageClassificationTests(unittest.TestCase):
    def test_bot_rejection_is_not_a_login_prompt_even_with_stale_controls(self):
        for apostrophe in ("'", '’'):
            text = (f'Automated bots aren{apostrophe}t allowed to join this meeting. '
                    'If this was a mistake and you are a human, sign in to join the meeting.')
            self.assertEqual(webjoin.classify_page(Page(text, 1, True)), 'automation_blocked')

    def test_bot_name_or_normal_signin_does_not_imply_bot_rejection(self):
        self.assertEqual(webjoin.classify_page(Page('Zoom Transcript Bot. Sign in to join')), 'login_required')

    def test_login_with_hidden_or_visible_stale_controls_is_not_joined(self):
        for visible in (False, True):
            self.assertEqual(webjoin.classify_page(Page("Sign in to join this meeting", 1, visible)), "login_required")

    def test_hidden_leave_button_does_not_verify_join(self):
        self.assertEqual(webjoin.classify_page(Page("Connecting", 1, False)), "connecting")

    def test_visible_meeting_control_verifies_join(self):
        self.assertEqual(webjoin.classify_page(Page("Meeting", 1, True)), "joined")

    def test_waiting_and_ended_are_distinct_from_joined(self):
        self.assertEqual(webjoin.classify_page(Page("The host will let you in")), "waiting_room")
        self.assertEqual(webjoin.classify_page(Page("This meeting has ended", 1, True)), "ended")


class StartupCleanupTests(unittest.TestCase):
    def test_stop_during_launch_closes_browser_without_joining(self):
        from playwright import sync_api
        for module in (webjoin, bot):
            with self.subTest(mode=module.__name__), tempfile.TemporaryDirectory() as folder:
                session = Path(folder)
                (session / 'chunks').mkdir()
                browser = Mock()
                handlers = {}
                def launch(**kwargs):
                    handlers[module.signal.SIGTERM](module.signal.SIGTERM, None)
                    return browser
                manager = MagicMock()
                manager.__enter__.return_value.chromium.launch.side_effect = launch
                with contextlib.ExitStack() as stack:
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    stack.enter_context(patch.object(sync_api, 'sync_playwright', return_value=manager))
                    stack.enter_context(patch.object(module, 'new_session', return_value=session))
                    stack.enter_context(patch.object(module, 'start_stt', return_value=None))
                    stack.enter_context(patch.object(module, 'need'))
                    stack.enter_context(patch.object(module.signal, 'signal',
                                                    side_effect=lambda sig, callback: handlers.update({sig: callback})))
                    if module is bot:
                        stack.enter_context(patch.object(bot, 'load_env', return_value={
                            'ZOOM_SDK_CLIENT_ID': 'synthetic', 'ZOOM_SDK_CLIENT_SECRET': 'synthetic'}))
                    self.assertEqual(module.main(['--mn', '123456789']), 1)
                browser.new_context.assert_not_called()
                browser.close.assert_called_once()
                status = json.loads((session / 'status.json').read_text())
                self.assertFalse(status['recording_complete'])
                self.assertIn('stopped_by_user', status['problems'])

    def test_actual_child_and_sdk_server_close_after_browser_launch_failure(self):
        from playwright import sync_api
        from control import create_server
        for module in (webjoin, bot):
            with self.subTest(mode=module.__name__), tempfile.TemporaryDirectory() as directory:
                session = Path(directory)
                (session / 'chunks').mkdir()
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
                servers = []
                def server_factory(*args, **kwargs):
                    server = create_server(*args, **kwargs)
                    servers.append(server)
                    return server
                manager = MagicMock()
                manager.__enter__.return_value.chromium.launch.side_effect = RuntimeError('synthetic browser launch failure')
                try:
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                        stack.enter_context(patch.object(sync_api, 'sync_playwright', return_value=manager))
                        stack.enter_context(patch.object(module, 'new_session', return_value=session))
                        stack.enter_context(patch.object(module, 'start_stt', return_value=child))
                        stack.enter_context(patch.object(module.signal, 'signal'))
                        stack.enter_context(patch.object(runtime, 'run_process', return_value=Mock(returncode=0)))
                        if module is bot:
                            stack.enter_context(patch.object(bot, 'load_env', return_value={
                                'ZOOM_SDK_CLIENT_ID': 'synthetic', 'ZOOM_SDK_CLIENT_SECRET': 'synthetic'}))
                            stack.enter_context(patch.object(bot, 'create_server', side_effect=server_factory))
                        self.assertEqual(module.main(['--mn', '123456789']), 1)
                    self.assertIsNotNone(child.poll())
                    self.assertTrue(all(server.socket.fileno() == -1 for server in servers))
                    self.assertTrue(json.loads((session / 'status.json').read_text())['incomplete'])
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.session = Path(self.temporary.name)
        self.monotonic = patch.object(runtime.time, "monotonic", return_value=1000.0)
        self.wall = patch.object(runtime.time, "time", return_value=10000.0)
        self.monotonic.start()
        self.wall.start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.wall.stop()
        self.monotonic.stop()
        self.temporary.cleanup()

    def health(self, **overrides):
        value = {"ts": 10000, "state": "idle", "state_since": 9999,
                 "last_processed": 9999, "error_count": 0}
        value.update(overrides)
        (self.session / "stt-health.json").write_text(json.dumps(value), encoding="utf-8")

    def status(self):
        return json.loads((self.session / "status.json").read_text(encoding="utf-8"))

    def test_exited_stt_and_agent_fail_even_with_recent_audio(self):
        for name in ("stt", "agent"):
            monitor = runtime.Monitor(self.session, **{name: Mock(poll=Mock(return_value=1))})
            with self.assertRaisesRegex(RuntimeError, name + "_exited"):
                monitor.tick(True, 999, True)
            self.assertTrue(self.status()["incomplete"])

    def test_low_disk_space_stops_with_explicit_incomplete_status(self):
        monitor = runtime.Monitor(self.session)
        with patch.object(runtime.shutil, 'disk_usage', return_value=Mock(free=1024)):
            with self.assertRaisesRegex(RuntimeError, 'disk_space_low'):
                monitor.tick(True, 999, True)
        self.assertTrue(self.status()['incomplete'])

    @unittest.skipIf(sys.platform == 'win32', 'POSIX signal escalation')
    def test_real_unresponsive_child_is_killed_and_reaped(self):
        process = subprocess.Popen([sys.executable, '-c',
            'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            runtime.stop_process(process, timeout=0.05)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()

    def test_stale_heartbeat_fails_while_process_is_alive(self):
        monitor = runtime.Monitor(self.session, stt=Mock(poll=Mock(return_value=None)))
        self.health(ts=9979)
        with self.assertRaisesRegex(RuntimeError, "stt_heartbeat_stale"):
            monitor.tick(True, 999, True)

    def test_missing_heartbeat_and_hung_operation_fail(self):
        monitor = runtime.Monitor(self.session, stt=Mock(poll=Mock(return_value=None)))
        monitor.started = 979
        with self.assertRaisesRegex(RuntimeError, "stt_heartbeat_missing"):
            monitor.tick(True, 999, True)
        monitor = runtime.Monitor(self.session, stt=Mock(poll=Mock(return_value=None)))
        self.health(state="processing", state_since=9699)
        with self.assertRaisesRegex(RuntimeError, "stt_operation_timeout"):
            monitor.tick(True, 999, True)

    def test_unverified_join_has_deadline(self):
        monitor = runtime.Monitor(self.session)
        monitor.started = 879
        with self.assertRaisesRegex(RuntimeError, "join_not_verified"):
            monitor.tick(False)

    def test_join_without_real_audio_does_not_become_recording(self):
        monitor = runtime.Monitor(self.session)
        monitor.tick(True)
        self.assertEqual(self.status()["state"], "joined_waiting_audio")
        monitor.joined_at = 909
        with self.assertRaisesRegex(RuntimeError, "audio_not_received"):
            monitor.tick(True, 999, False)

    def test_source_loss_after_first_audio_has_no_startup_grace(self):
        (self.session / "transcript.md").write_text("Synthetic earlier speech", encoding="utf-8")
        monitor = runtime.Monitor(self.session)
        monitor.tick(True, 999, True)
        # Still in the first second of the meeting, well inside the initial 90s.
        with self.assertRaisesRegex(RuntimeError, "audio_source_lost"):
            monitor.tick(True, 999, False)
        self.assertEqual(runtime.finish(self.session, monitor, "none", None, drain=False), 1)
        self.assertFalse(self.status()["recording_complete"])
        self.assertEqual(self.status()["state"], "incomplete")

    def test_finalization_cannot_erase_observed_source_loss(self):
        (self.session / "transcript.md").write_text("Synthetic earlier speech", encoding="utf-8")
        monitor = runtime.Monitor(self.session)
        monitor.tick(True, 999, True)
        monitor.audio_source = False
        self.assertEqual(runtime.finish(self.session, monitor, "none", None, drain=False), 1)
        self.assertIn("audio_source_lost", self.status()["problems"])

    def test_stalled_audio_and_lost_meeting_fail(self):
        monitor = runtime.Monitor(self.session)
        monitor.joined_at = 900
        with self.assertRaisesRegex(RuntimeError, "audio_stream_stalled"):
            monitor.tick(True, 939, True)
        monitor = runtime.Monitor(self.session)
        monitor.joined_at = 900
        with self.assertRaisesRegex(RuntimeError, "meeting_connection_lost"):
            monitor.tick(False, 999, True)

    def test_quarantined_chunk_never_reports_clean_recording(self):
        monitor = runtime.Monitor(self.session, stt=Mock(poll=Mock(return_value=None)))
        self.health(error_count=1)
        monitor.tick(True, 999, True)
        self.assertEqual(self.status()["state"], "degraded")
        self.assertTrue(self.status()["incomplete"])

    def test_recording_requires_healthy_worker_and_audio(self):
        monitor = runtime.Monitor(self.session, stt=Mock(poll=Mock(return_value=None)))
        self.health(last_processed=None)
        monitor.tick(True, 999, True)
        self.assertEqual(self.status()["state"], "recording_stt_starting")
        self.health()
        monitor.tick(True, 999, True)
        self.assertEqual(self.status()["state"], "recording")
        self.assertIsNone(self.status()["recording_complete"])

    def test_failed_final_drain_prevents_complete_and_summary(self):
        (self.session / "transcript.md").write_text("Synthetic earlier text", encoding="utf-8")
        monitor = runtime.Monitor(self.session, stt=Mock())
        agent = types.ModuleType("agent")
        agent.summary = Mock(return_value=True)
        with patch.object(runtime, "stop_process"), \
                patch.object(runtime, "run_process", return_value=Mock(returncode=1)), \
                patch.dict(sys.modules, {"agent": agent}):
            self.assertEqual(runtime.finish(self.session, monitor, "claude", None), 1)
        agent.summary.assert_not_called()
        self.assertEqual(self.status()["state"], "incomplete")
        self.assertIn("transcript_incomplete", self.status()["problems"])
        self.assertFalse(self.status()["recording_complete"])

    def test_summary_failure_preserves_recording_and_manual_retry_succeeds(self):
        (self.session / "transcript.md").write_text("Synthetic complete transcript", encoding="utf-8")
        monitor = runtime.Monitor(self.session)
        monitor.tick(True, 999, True)

        def failed_pipeline(*args, **kwargs):
            self.assertTrue(self.status()["recording_complete"])
            self.assertEqual(self.status()["state"], "summarizing")
            return {"status": "incomplete"}

        with patch.object(agent, "build_minutes", side_effect=failed_pipeline):
            self.assertEqual(runtime.finish(self.session, monitor, "codex", None, drain=False), 1)
        self.assertEqual(self.status()["state"], "summary_failed")
        self.assertFalse(self.status()["incomplete"])
        self.assertTrue(self.status()["recording_complete"])
        with patch.object(agent, "build_minutes", return_value={"status": "complete"}) as build:
            self.assertTrue(agent.summary(self.session, backend="codex"))
            build.assert_called_once()
        self.assertEqual(self.status()["state"], "complete")
        self.assertNotIn("summary_failed", self.status()["problems"])

    def test_manual_summary_rejects_active_incomplete_and_legacy_unverified_status(self):
        (self.session / "transcript.md").write_text("Synthetic transcript", encoding="utf-8")
        statuses = [
            {"state": "recording", "recording_complete": None, "incomplete": False},
            {"state": "connecting", "recording_complete": None, "incomplete": False},
            {"state": "incomplete", "recording_complete": False, "incomplete": True},
            {"state": "complete", "incomplete": False},
            {"state": "untranscribed", "recording_complete": False, "incomplete": False},
        ]
        with patch.object(agent, "build_minutes") as build:
            for status in statuses:
                (self.session / "status.json").write_text(json.dumps(status), encoding="utf-8")
                self.assertFalse(agent.summary(self.session, backend="codex"))
            build.assert_not_called()

    def test_manual_legacy_transcript_without_status_is_supported(self):
        (self.session / "transcript.md").write_text("Legacy transcript", encoding="utf-8")
        with patch.object(agent, "build_minutes", return_value={"status": "complete"}) as build:
            self.assertTrue(agent.summary(self.session, backend="codex"))
            build.assert_called_once()

    def test_existing_text_without_verified_audio_cannot_finish_as_complete(self):
        (self.session / "transcript.md").write_text("Synthetic earlier text", encoding="utf-8")
        monitor = runtime.Monitor(self.session)
        monitor.tick(True)
        with patch.object(agent, "summary") as summary:
            self.assertEqual(runtime.finish(self.session, monitor, "codex", None, drain=False), 1)
            summary.assert_not_called()
        self.assertFalse(self.status()["recording_complete"])
        self.assertIn("audio_not_received", self.status()["problems"])

    def test_short_recording_can_verify_first_chunk_during_final_drain(self):
        (self.session / "transcript.md").write_text("Short synthetic recording", encoding="utf-8")
        monitor = runtime.Monitor(self.session, stt=Mock(poll=Mock(return_value=None)))
        self.health(last_processed=None)
        monitor.tick(True, None, True)
        self.assertFalse(monitor.audio_received)

        def drain(*args, **kwargs):
            self.health(state="stopped", processed_count=1, pending_count=0, last_processed=10000)
            return Mock(returncode=0)

        with patch.object(runtime, "stop_process"), patch.object(runtime, "run_process", side_effect=drain):
            self.assertEqual(runtime.finish(self.session, monitor, "none", None), 0)
        self.assertTrue(self.status()["recording_complete"])
        self.assertEqual(self.status()["state"], "complete")

    def test_transmitter_without_stt_is_explicitly_untranscribed(self):
        monitor = runtime.Monitor(self.session, capture_required=False)
        monitor.tick(True)
        self.assertFalse(monitor.failed)
        self.assertEqual(runtime.finish(self.session, monitor, "none", None), 0)
        self.assertEqual(self.status()["state"], "untranscribed")
        self.assertFalse((self.session / "transcript.md").exists())

    def test_forced_stop_waits_for_killed_child(self):
        process = Mock(poll=Mock(return_value=None))
        process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 15), 0]
        runtime.stop_process(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)


class WebFlushTests(unittest.TestCase):
    def run_web(self, *, flush_error, saved_login=False, account='default'):
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder)
            from accounts import save_state, state_path
            if saved_login:
                save_state(session, account if account != 'guest' else 'default', {'cookies': [], 'origins': []})
            (session / "transcript.md").write_text("Synthetic earlier speech", encoding="utf-8")
            page = Mock()
            page.get_by_role.return_value = Locator()
            page.wait_for_timeout.side_effect = KeyboardInterrupt()
            def evaluate(script, expected=None):
                if expected is None:
                    return {"generation": "synthetic-document", "ready": True}
                if flush_error:
                    raise RuntimeError("Synthetic flush failure")
                return expected
            page.evaluate.side_effect = evaluate
            context = Mock()
            context.new_page.return_value = page

            def joined_with_audio(_):
                callbacks = {call.args[0]: call.args[1] for call in context.expose_binding.call_args_list}
                callbacks["__zbEvent"](None, "audio_source", {"active": True})
                callbacks["__zbAudio"](None, 1700000000000, base64.b64encode(b"synthetic-chunk").decode())
                return "joined"

            browser = Mock()
            browser.new_context.return_value = context
            manager = MagicMock()
            manager.__enter__.return_value.chromium.launch.return_value = browser
            api = types.ModuleType("playwright.sync_api")
            api.sync_playwright = lambda: manager
            package = types.ModuleType("playwright")
            package.sync_api = api
            with patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": api}), \
                    patch.object(webjoin, "new_session", return_value=session), \
                    patch.object(webjoin, "APP_HOME", session), \
                    patch.object(webjoin, "need"), \
                    patch.object(webjoin, "start_stt", return_value=None), \
                    patch.object(webjoin, "classify_page", side_effect=joined_with_audio), \
                    patch.object(webjoin.signal, "signal"), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = webjoin.main(["--mn", "123456789", "--llm", "none", "--account", account])
            self.assertEqual(sum(len(call.args) == 2 for call in page.evaluate.call_args_list), 1)
            browser.close.assert_called_once()
            auth = [json.loads(line)['data'] for line in (session / 'events.jsonl').read_text().splitlines()
                    if json.loads(line)['kind'] == 'browser_auth']
            supplied = saved_login and account != 'guest'
            self.assertEqual(auth, [{'account': account, 'stored_login_supplied': supplied, 'login_verified': False}])
            self.assertEqual(browser.new_context.call_args.kwargs['storage_state'],
                             str(state_path(session, account)) if supplied else None)
            return code, json.loads((session / "status.json").read_text(encoding="utf-8"))

    def test_interrupt_still_flushes_and_closes_browser(self):
        code, status = self.run_web(flush_error=False)
        self.assertEqual(code, 0)
        self.assertEqual(status["state"], "complete")

    def test_flush_failure_keeps_existing_transcript_incomplete(self):
        code, status = self.run_web(flush_error=True)
        self.assertEqual(code, 1)
        self.assertEqual(status["state"], "incomplete")
        self.assertIn("capture_flush_failed", status["problems"])

    def test_saved_login_is_disclosed_without_claiming_verified_identity(self):
        code, status = self.run_web(flush_error=False, saved_login=True)
        self.assertEqual(code, 0)
        self.assertTrue(status['recording_complete'])

    def test_each_named_login_reaches_only_its_browser_context(self):
        for account in ('personal', 'assistant'):
            with self.subTest(account=account):
                code, status = self.run_web(flush_error=False, saved_login=True, account=account)
                self.assertEqual(code, 0)

    def test_explicit_guest_does_not_receive_existing_login(self):
        code, status = self.run_web(flush_error=False, saved_login=True, account='guest')
        self.assertEqual(code, 0)


class CaptureGuardTests(unittest.TestCase):
    def test_missing_hook_is_not_a_successful_empty_flush(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            monitor = runtime.Monitor(Path(folder))
            page = Mock(evaluate=Mock(return_value={"generation": None, "ready": False}))
            with self.assertRaisesRegex(RuntimeError, "capture_hook_missing"):
                runtime.CaptureGuard(page, monitor).flush()
            self.assertTrue(monitor.failed)

    def test_redirect_before_join_can_rebind_but_after_join_fails(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            monitor = runtime.Monitor(Path(folder))
            page = Mock()
            page.evaluate.side_effect = [{"generation": name, "ready": True} for name in ("login", "meeting", "replacement")]
            guard = runtime.CaptureGuard(page, monitor)
            self.assertEqual(guard.check(), "login")
            self.assertEqual(guard.check(), "meeting")
            monitor.tick(True, runtime.time.monotonic(), True)
            with self.assertRaisesRegex(RuntimeError, "capture_document_changed"):
                guard.flush()
            self.assertTrue(monitor.failed)


if __name__ == "__main__":
    unittest.main()
