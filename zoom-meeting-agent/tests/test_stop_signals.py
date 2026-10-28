"""Actual INT/TERM while Playwright pumps a synthetic recording, no cloud/STT."""
import json
import os
from pathlib import Path
import select
import signal
import sys
import tempfile
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import runtime

RUNNER = r"""
import contextlib,json,signal,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import control,webjoin
import playwright.sync_api as api
root=Path(sys.argv[2]); session=root/'session'; (session/'chunks').mkdir(parents=True)
old={s:signal.getsignal(s) for s in (signal.SIGINT,signal.SIGTERM)}
html='''<html><body><input aria-label="Your Name"><button class="preview-join-button">Join</button><button class="footer__leave-btn" hidden>Leave</button><script>
document.querySelector('.preview-join-button').onclick=async()=>{
 const c=new AudioContext(); await c.resume(); const o=c.createOscillator(); o.connect(c.destination); o.start();
 document.querySelector('.preview-join-button').remove(); document.querySelector('.footer__leave-btn').hidden=false;
};</script></body></html>'''
hook=(Path(sys.argv[1])/'static/audio-hook.js').read_text().replace('const CHUNK_MS = 20000;', 'const CHUNK_MS = 200;')
hook+=';(()=>{const stop=window.__zbStopCapture;window.__zbStopCapture=async()=>{await stop();await window.__zbEvent("test_flush",{});};})();'
original=api.sync_playwright; write=control.write_chunk; announced=False
def chunk(*args):
 global announced
 result=write(*args)
 if not announced: announced=True; print('READY',flush=True)
 return result
class Browser:
 def __init__(self,b):self.b=b
 def new_context(self,**kw):
  c=self.b.new_context(**kw)
  c.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=html) if r.request.url.startswith('https://app.zoom.us/wc/join/') else r.abort())
  add=c.add_init_script
  c.add_init_script=lambda *a,**kw:add(script=hook)
  return c
 def close(self):self.b.close()
@contextlib.contextmanager
def local():
 with original() as p:
  yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw:Browser(p.chromium.launch(**kw))))
result={}
def finish(folder,monitor,*args):
 events=[json.loads(x) for x in (folder/'events.jsonl').read_text().splitlines()]
 result.update(flushed=sum(x['kind']=='test_flush' for x in events),chunks=len(list((folder/'chunks').glob('*.webm'))),problems=sorted(monitor.problems))
 return 0
with patch.object(api,'sync_playwright',local),patch.object(control,'write_chunk',chunk),patch.object(webjoin,'new_session',return_value=session),patch.object(webjoin,'start_stt',return_value=None),patch.object(webjoin,'need'),patch.object(webjoin,'finish',side_effect=finish),patch.object(webjoin,'APP_HOME',root/'empty'):
 code=webjoin.main(['--mn','123456789','--minutes','2','--llm','none'])
result['handlers_restored']=all(signal.getsignal(s)==h for s,h in old.items())
print('RESULT '+json.dumps(result),flush=True)
raise SystemExit(code)
"""


@unittest.skipUnless(os.name == 'posix', 'POSIX signals; Windows console needs separate acceptance')
class RecordingStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed')
        with sync_playwright() as p:
            if not Path(p.chromium.executable_path).is_file():
                raise unittest.SkipTest('Playwright Chromium is not installed')
            # Complete the driver handshake before closing this probe; reading
            # executable_path alone can leave its initialization task pending.
            browser = p.chromium.launch(headless=True)
            browser.close()

    def exercise(self, sig):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'record.py'
            script.write_text(RUNNER, encoding='utf-8')
            process = runtime.start_process([sys.executable, str(script), str(SOURCE), str(root)],
                                            stdout=-1, stderr=-2, bufsize=0)
            output = []
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([process.stdout], [], [], .2)
                    if not ready:
                        self.assertIsNone(process.poll(), ''.join(output))
                        continue
                    line = process.stdout.readline().decode('utf-8', errors='replace')
                    output.append(line)
                    if line.strip() == 'READY':
                        break
                else:
                    self.fail('No synthetic recorded chunk before deadline: ' + ''.join(output))
                process.send_signal(sig)
                remaining, _ = process.communicate(timeout=15)
                output.append(remaining.decode('utf-8', errors='replace'))
                self.assertEqual(process.returncode, 0, ''.join(output))
                rows = [line[7:] for line in ''.join(output).splitlines() if line.startswith('RESULT ')]
                self.assertEqual(len(rows), 1, ''.join(output))
                result = json.loads(rows[0])
                self.assertEqual(result['flushed'], 1)
                self.assertGreaterEqual(result['chunks'], 2)
                self.assertIn('stopped_by_user', result['problems'])
                self.assertTrue(result['handlers_restored'])
            finally:
                runtime.stop_process(process, timeout=1)
                if process.stdout:
                    process.stdout.close()

    def test_int_during_real_browser_recording_flushes_and_restores_handlers(self):
        self.exercise(signal.SIGINT)

    def test_term_during_real_browser_recording_flushes_and_restores_handlers(self):
        self.exercise(signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
