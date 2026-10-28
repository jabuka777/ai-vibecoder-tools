"""Offline Chromium checks with real synthetic audio and the real HTTP handler.

No Zoom, account configuration, saved browser state or model is accessed. All
non-loopback network requests are aborted or fulfilled locally by Playwright.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot
import control
import runtime

SOURCE = Path(__file__).resolve().parents[1]
SYNTHETIC_AUDIO = """async () => {
  const context = new AudioContext();
  const oscillator = context.createOscillator();
  const destination = context.createMediaStreamDestination();
  oscillator.frequency.value = 440;
  oscillator.connect(destination);
  const audio = document.createElement('audio');
  audio.muted = true;
  audio.srcObject = destination.stream;
  document.body.appendChild(audio);
  await context.resume();
  oscillator.start();
  window.__synthetic = {context, oscillator, stream: destination.stream, audio};
}"""


class BrowserCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright, Error
        except ImportError:
            raise unittest.SkipTest("Playwright is not installed")
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(
                headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        except Error as exc:
            cls.playwright.stop()
            if "Executable doesn't exist" in str(exc):
                raise unittest.SkipTest("Playwright Chromium is not installed")
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        static = self.root / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>Synthetic capture test</title><body>Offline audio test</body>", encoding="utf-8")
        self.static_patch = patch.object(control, "STATIC", static)
        self.static_patch.start()
        self.state = control.State(self.root / "session", {}, "123456789")
        self.server = control.create_server(self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.state.port}"
        self.context = self.browser.new_context(service_workers="block")
        self.unexpected_requests = []
        def deny_network(route):
            self.unexpected_requests.append(route.request.url)
            route.abort()
        self.context.route("**/*", deny_network)
        bot.bind_control(self.context, self.state)
        # Preserve production capture code; only accelerate its timers and expose
        # native MediaRecorder instances so tests can wait for actual chunk turns.
        prelude = """(() => {
          const originalTimeout = window.setTimeout.bind(window);
          window.setTimeout = (callback, delay, ...args) =>
            originalTimeout(callback, delay === 20000 ? 350 : (delay === 1000 ? 50 : delay), ...args);
          const OriginalRecorder = window.MediaRecorder;
          window.__recorders = [];
          window.MediaRecorder = class extends OriginalRecorder {
            constructor(...args) { super(...args); window.__recorders.push(this); }
          };
        })();\n"""
        self.context.add_init_script(script=prelude + (SOURCE / "static" / "audio-hook.js").read_text(encoding="utf-8"))
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.static_patch.stop()
        self.temporary.cleanup()

    def start_audio(self):
        response = self.page.goto(self.origin + "/")
        self.assertEqual(response.status, 200)
        self.page.wait_for_load_state("networkidle")
        self.page.evaluate(SYNTHETIC_AUDIO)

    def chunks(self):
        return sorted(self.state.chunks.glob("*.webm"))

    def wait_until(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("Timed out waiting for synthetic audio upload")
            self.page.wait_for_timeout(50)

    def assert_valid_audio(self, files):
        self.assertTrue(files)
        decoder = shutil.which("ffmpeg")
        for path in files:
            self.assertEqual(path.read_bytes()[:4], b"\x1a\x45\xdf\xa3", path.name)
            if decoder:
                result = subprocess.run([decoder, "-v", "error", "-i", str(path), "-f", "null", "-"],
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_actual_webm_and_final_chunk_survive_source_track_stop(self):
        self.start_audio()
        self.wait_until(lambda: len(self.chunks()) >= 1)
        self.page.wait_for_timeout(100)
        before = len(self.chunks())
        self.assertGreaterEqual(before, 1)
        # stop() sets readyState=ended before the recorder flushes its final blob.
        self.page.evaluate("""async () => {
          window.__synthetic.stream.getTracks().forEach(track => track.stop());
          await window.__zbStopCapture();
        }""")
        self.assertGreater(len(self.chunks()), before)
        self.assert_valid_audio(self.chunks())
        self.assertEqual(self.unexpected_requests, [])

    def test_earlier_upload_failure_stays_fatal_after_later_success(self):
        attempts = []
        def fail_first_upload(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                route.fulfill(status=500, content_type="application/json", body="{}")
            else:
                route.fallback()
        self.context.route(self.origin + "/audio?*", fail_first_upload)
        self.start_audio()
        self.wait_until(lambda: len(attempts) >= 2 and len(self.chunks()) >= 1)
        self.page.wait_for_timeout(100)
        self.assertGreaterEqual(len(self.chunks()), 1)
        with self.assertRaisesRegex(Exception, "One or more audio chunks failed to upload"):
            self.page.evaluate("async () => await window.__zbStopCapture()")
        self.assertGreaterEqual(len(attempts), 3)
        events = [json.loads(line) for line in (self.state.sess / "events.jsonl").read_text().splitlines()]
        self.assertIn("capture_error", [event["kind"] for event in events])
        self.assert_valid_audio(self.chunks())
        self.assertEqual(self.unexpected_requests, [])

    def test_control_credential_does_not_reach_external_origin(self):
        received = []
        def external_stub(route):
            received.append(route.request.headers)
            route.fulfill(status=200, content_type="text/plain", body="synthetic response")
        self.context.route("https://example.invalid/**", external_stub)
        response = self.page.goto(self.origin + "/")
        self.assertEqual(response.status, 200)
        self.page.wait_for_load_state("networkidle")
        self.page.evaluate("async () => { await fetch('https://example.invalid/probe', {mode: 'no-cors'}); }")
        self.assertEqual(len(received), 1)
        self.assertNotIn("authorization", {key.lower(): value for key, value in received[0].items()})
        self.assertNotIn(self.state.token, json.dumps(received))
        self.assertEqual(self.unexpected_requests, [])

    def test_navigation_before_flush_is_incomplete_not_fresh_hook_success(self):
        self.start_audio()
        monitor = runtime.Monitor(self.state.sess)
        guard = runtime.CaptureGuard(self.page, monitor)
        guard.check()
        self.wait_until(lambda: len(self.chunks()) >= 1)
        monitor.tick(True, self.state.last_audio, self.state.audio_source)
        self.page.wait_for_timeout(100)
        (self.state.sess / "transcript.md").write_text("Synthetic earlier speech", encoding="utf-8")
        self.page.goto(self.origin + "/?replacement=1")
        self.page.wait_for_load_state("networkidle")
        with self.assertRaisesRegex(RuntimeError, "capture_document_changed"):
            guard.flush()
        self.assertEqual(runtime.finish(self.state.sess, monitor, "none", None, drain=False), 1)
        status = json.loads((self.state.sess / "status.json").read_text())
        self.assertEqual(status["state"], "incomplete")
        self.assertFalse(status["recording_complete"])
        self.assertEqual(self.unexpected_requests, [])

    def test_fragment_leave_preserves_document_and_flushes_real_audio(self):
        self.start_audio()
        monitor = runtime.Monitor(self.state.sess)
        guard = runtime.CaptureGuard(self.page, monitor)
        original = guard.check()
        self.wait_until(lambda: len(self.chunks()) >= 1)
        monitor.tick(True, self.state.last_audio, self.state.audio_source)
        self.page.wait_for_timeout(100)
        before = len(self.chunks())
        self.page.evaluate("location.hash = '#meeting-left'")
        guard.flush()
        self.assertEqual(guard.generation, original)
        self.assertFalse(monitor.failed)
        self.assertGreater(len(self.chunks()), before)
        self.assert_valid_audio(self.chunks())
        self.assertEqual(self.unexpected_requests, [])

    def test_stop_immediately_after_encoder_restart_keeps_decodable_tail(self):
        self.start_audio()
        self.page.wait_for_function("""() => {
          const recorder = window.__recorders.at(-1);
          return recorder && recorder.state === 'recording' && recorder.__hadSource &&
            performance.now() - recorder.__startedAt < 50;
        }""")
        before = len(self.chunks())
        self.page.evaluate("async () => await window.__zbStopCapture()")
        self.assertGreater(len(self.chunks()), before)
        self.assert_valid_audio(self.chunks())
        self.assertEqual(self.unexpected_requests, [])


if __name__ == "__main__":
    unittest.main()
