"""Offline tests: no Zoom, model download, credentials, or real meeting data."""
import contextlib
import importlib.util
import io
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "stt.py"
spec = importlib.util.spec_from_file_location("meeting_stt", MODULE)
stt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stt)


class Model:
    def transcribe(self, value, **kwargs):
        return iter([types.SimpleNamespace(text=value)]), None


class STTTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="meeting-stt-test-")
        self.addCleanup(self.directory.cleanup)
        self.session = Path(self.directory.name)
        (self.session / "chunks").mkdir()

    def chunk(self, name, data=b"synthetic"):
        path = self.session / "chunks" / name
        path.write_bytes(data)
        return path

    def health(self):
        return json.loads((self.session / "stt-health.json").read_text())

    def run_worker(self, **kwargs):
        return stt.run_session(self.session, once=True, model_factory=Model,
                               decoder=lambda path: "recognized synthetic speech", **kwargs)

    def test_import_does_not_require_whisper_or_start_worker(self):
        code = (
            "import importlib.util,sys; "
            f"s=importlib.util.spec_from_file_location('isolated_stt', {str(MODULE)!r}); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "assert 'faster_whisper' not in sys.modules; assert 'numpy' not in sys.modules"
        )
        subprocess.run([sys.executable, "-I", "-c", code], check=True, timeout=10)

    def test_corrupt_chunk_quarantined_and_next_chunk_processed(self):
        bad = self.chunk("1000000000000.webm")
        good = self.chunk("1000000020000.webm")
        def decode(path):
            if path == bad:
                raise ValueError("SENSITIVE error text must never be logged")
            return "synthetic accepted decision"
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = stt.run_session(self.session, once=True, model_factory=Model, decoder=decode)
        self.assertEqual(result, 1)
        self.assertFalse(bad.exists())
        self.assertTrue((bad.parent / "quarantine" / bad.name).exists())
        self.assertTrue(good.exists())
        self.assertIn("synthetic accepted decision", (self.session / "transcript.md").read_text())
        self.assertEqual((self.session / ".stt_done").read_text().split(), [good.name])
        ledger = (self.session / "stt-errors.jsonl").read_text()
        self.assertNotIn("SENSITIVE", ledger + errors.getvalue())
        self.assertNotIn("synthetic accepted decision", ledger)
        self.assertEqual(json.loads(ledger)["error_type"], "ValueError")
        self.assertEqual(self.health()["error_count"], 1)

    def test_real_ffmpeg_bad_chunk_does_not_block_next_audio(self):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('ffmpeg unavailable')
        try:
            import numpy
        except ImportError:
            self.skipTest('numpy unavailable')
        bad = self.chunk('1000000000000.webm', b'not-a-media-file')
        good = self.session / 'chunks' / '1000000020000.webm'
        subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=600:duration=0.3', '-c:a', 'libopus', str(good)],
                       check=True, capture_output=True, timeout=10)
        class DecodedModel:
            def transcribe(self, value, **kwargs):
                if not isinstance(value, numpy.ndarray) or not value.size:
                    raise AssertionError('Expected real decoded audio samples')
                return iter([types.SimpleNamespace(text='synthetic decoded tone')]), None
        with contextlib.redirect_stderr(io.StringIO()):
            result = stt.run_session(self.session, once=True, model_factory=DecodedModel,
                                    decoder=lambda path: stt.load(path, ffmpeg))
        self.assertEqual(result, 1)
        self.assertFalse(bad.exists())
        self.assertIn('synthetic decoded tone', (self.session / 'transcript.md').read_text())
        self.assertEqual(self.health()['error_count'], 1)
        self.assertEqual(self.health()["state"], "stopped")
        self.assertIsInstance(self.health()["last_processed"], float)
        # A drain/restart must retain the incomplete status, not turn it green.
        self.assertEqual(self.run_worker(), 1)
        self.assertEqual(self.health()["error_count"], 1)

    def test_once_drains_fresh_chunk_and_does_not_duplicate_on_restart(self):
        self.chunk("1000000000000.webm")
        self.assertEqual(self.run_worker(), 0)
        first = (self.session / "transcript.md").read_text()
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual((self.session / "transcript.md").read_text(), first)
        self.assertEqual(self.health()["processed_count"], 1)

    def test_crash_after_transcript_before_done_does_not_duplicate(self):
        self.chunk('1000000000000.webm')
        original = stt.atomic_text
        done_writes = []
        def fail_done(path, text):
            if path.name == '.stt_done':
                done_writes.append(True)
                if len(done_writes) == 2:
                    raise OSError('synthetic persistence failure')
            return original(path, text)
        with patch.object(stt, 'atomic_text', side_effect=fail_done), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_worker(), 2)
        before = (self.session / 'transcript.md').read_text()
        with patch.object(Model, 'transcribe', side_effect=AssertionError('Committed chunk must not be decoded again')):
            self.assertEqual(self.run_worker(), 0)
        self.assertEqual((self.session / 'transcript.md').read_text(), before)
        self.assertEqual(before.count('recognized synthetic speech'), 1)

    def test_late_older_chunk_restores_chronological_order(self):
        self.chunk('1000000020000.webm')
        self.assertEqual(self.run_worker(), 0)
        self.chunk('1000000000000.webm')
        self.assertEqual(self.run_worker(), 0)
        lines = (self.session / 'transcript.md').read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertLess(lines[0][:10], lines[1][:10])

    def test_interrupted_drain_reports_pending_input(self):
        self.chunk("1000000000000.webm")
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.run_worker(stop_event=stop), 1)
        self.assertEqual(self.health()["pending_count"], 1)
        self.assertEqual(self.health()["processed_count"], 0)

    def test_silence_counts_as_processed_without_fabricating_transcript(self):
        self.chunk("1000000000000.webm")
        result = stt.run_session(self.session, once=True, model_factory=Model, decoder=lambda path: " ")
        self.assertEqual(result, 0)
        self.assertFalse((self.session / "transcript.md").exists())
        self.assertEqual(self.health()["processed_count"], 1)
        self.assertIsNotNone(self.health()["last_processed"])

    def test_startup_failure_is_nonzero_and_observable(self):
        def failed_model():
            raise RuntimeError("sensitive model initialization text")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = stt.run_session(self.session, once=True, model_factory=failed_model, decoder=lambda p: p)
        self.assertEqual(result, 2)
        self.assertEqual(self.health()["state"], "failed")
        self.assertEqual(self.health()["error_type"], "RuntimeError")
        self.assertIsNone(self.health()["last_processed"])
        self.assertNotIn("sensitive", output.getvalue())

    def test_heartbeat_remains_atomic_while_model_is_starting(self):
        health = stt.Health(self.session, interval=0.005)
        health.start()
        self.addCleanup(health.close)
        first = self.health()["ts"]
        deadline = time.monotonic() + 0.08
        samples = []
        while time.monotonic() < deadline:
            samples.append(self.health())
        self.assertTrue(all(item["state"] == "starting" for item in samples))
        self.assertGreater(samples[-1]["ts"], first)
        self.assertIsNone(health.failure)

    def test_persistence_failure_is_fatal_and_keeps_input(self):
        chunk = self.chunk("1000000000000.webm")
        (self.session / "transcript.md").mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.run_worker()
        self.assertEqual(result, 2)
        self.assertTrue(chunk.exists())
        self.assertFalse((self.session / "chunks" / "quarantine").exists())
        self.assertEqual(self.health()["state"], "failed")

    def test_lazy_segment_failure_is_quarantined(self):
        self.chunk("1000000000000.webm")
        class BrokenModel:
            def transcribe(self, value, **kwargs):
                def segments():
                    yield types.SimpleNamespace(text="must not be partially saved")
                    raise RuntimeError("decoder failed mid-stream")
                return segments(), None
        with contextlib.redirect_stderr(io.StringIO()):
            result = stt.run_session(self.session, once=True, model_factory=BrokenModel, decoder=lambda p: p)
        self.assertEqual(result, 1)
        self.assertFalse((self.session / "transcript.md").exists())
        self.assertEqual(self.health()["error_count"], 1)


if __name__ == "__main__":
    unittest.main()
