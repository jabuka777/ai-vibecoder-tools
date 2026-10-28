"""A real TERM during decoding must preserve valid input for the final drain."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from runtime import start_process, stop_process, run_process


@unittest.skipUnless(os.name == 'posix' and shutil.which('ffmpeg'), 'requires POSIX signals and ffmpeg')
class STTShutdownTests(unittest.TestCase):
    def test_signal_during_decoder_keeps_chunk_for_actual_ffmpeg_drain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / 'session'
            chunks = session / 'chunks'
            chunks.mkdir(parents=True)
            chunk = chunks / '1700000000000.webm'
            actual_ffmpeg = shutil.which('ffmpeg')
            subprocess.run([actual_ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi',
                '-i', 'sine=frequency=440:duration=0.2', '-c:a', 'libopus', str(chunk)],
                check=True, timeout=10)
            os.utime(chunk, (time.time()-5, time.time()-5))
            wrapper = root / 'ffmpeg'
            wrapper.write_text('#!' + sys.executable + '\n' +
                'import os, pathlib, sys, time\n' +
                f'root=pathlib.Path({str(root)!r})\n' +
                'if not (root/"release").exists():\n' +
                '    (root/"decoder-ready").write_text("ready")\n' +
                '    time.sleep(60)\n' +
                f'os.execv({actual_ffmpeg!r}, [{actual_ffmpeg!r}, *sys.argv[1:]])\n')
            wrapper.chmod(0o700)
            worker = root / 'worker.py'
            worker.write_text('import sys\nfrom types import SimpleNamespace\n' +
                f'sys.path.insert(0, {str(HERE)!r})\nimport stt\n' +
                'class Model:\n' +
                '    def transcribe(self, audio, **kwargs):\n' +
                '        assert len(audio)>0\n' +
                '        return [SimpleNamespace(text="saved tail")], None\n' +
                'stt.make_model=Model\nraise SystemExit(stt.main())\n')
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get('PATH', ''))
            process = start_process([sys.executable, str(worker), str(session)],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic()+15
                while not (root/'decoder-ready').exists() and process.poll() is None and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertTrue((root/'decoder-ready').exists(), 'actual decoder subprocess did not start')
                stop_process(process)
                self.assertEqual(process.returncode, 0)
                self.assertTrue(chunk.is_file())
                self.assertFalse((chunks/'quarantine').exists())
                health = json.loads((session/'stt-health.json').read_text())
                self.assertEqual(health['pending_count'], 1)
                self.assertEqual(health['error_count'], 0)
                (root/'release').touch()
                result = run_process([sys.executable, str(worker), str(session), '--once'],
                                     env=env, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((session/'transcript.md').read_text().count('saved tail'), 1)
                self.assertFalse((chunks/'quarantine').exists())
                health = json.loads((session/'stt-health.json').read_text())
                self.assertEqual(health['pending_count'], 0)
                self.assertEqual(health['processed_count'], 1)
            finally:
                stop_process(process)


if __name__ == '__main__':
    unittest.main()
