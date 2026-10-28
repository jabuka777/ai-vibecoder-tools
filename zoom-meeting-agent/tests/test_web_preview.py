"""Real local browser regressions for Zoom's guest passcode/name preview."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from runtime import Monitor
from webjoin import prepare_preview


class PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = sync_playwright().start()
        cls.browser = cls.manager.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.manager.stop()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.monitor = Monitor(Path(self.temporary.name))
        self.page = self.browser.new_page()
        self.page.set_content('''<input id="input-for-pwd"><input id="input-for-name">
          <button hidden>Continue without microphone and camera</button>
          <button class="preview-join-button" onclick="window.submitted=true">Join</button>''')

    def tearDown(self):
        self.page.close()
        self.temporary.cleanup()

    def test_missing_passcode_fails_before_submit_and_keeps_recording_incomplete(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(
                RuntimeError, 'meeting_passcode_required_use_local_configure'):
            prepare_preview(self.page, 'Synthetic assistant', '', self.monitor)
        self.assertTrue(self.monitor.failed)
        self.assertFalse(self.page.evaluate('!!window.submitted'))
        self.assertEqual(self.page.locator('#input-for-name').input_value(), '')

    def test_configured_passcode_and_name_reach_form_without_logging_secret(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            prepare_preview(self.page, 'Synthetic assistant', 'synthetic-only', self.monitor)
        self.assertTrue(self.page.evaluate('!!window.submitted'))
        self.assertEqual(self.page.locator('#input-for-pwd').input_value(), 'synthetic-only')
        self.assertEqual(self.page.locator('#input-for-name').input_value(), 'Synthetic assistant')
        self.assertNotIn('synthetic-only', output.getvalue())
        self.assertFalse(self.monitor.failed)

    def test_hidden_old_passcode_and_join_controls_do_not_block_waiting_room(self):
        self.page.set_content('''The host will let you in
          <input id="input-for-pwd" hidden><input id="input-for-name" hidden>
          <button hidden class="preview-join-button">Join</button>''')
        prepare_preview(self.page, 'Synthetic assistant', '', self.monitor)
        self.assertFalse(self.monitor.failed)

    def test_rejected_passcode_is_not_submitted_again(self):
        for message in ('Wrong passcode. Please try again.', 'Incorrect Password'):
            with self.subTest(message=message):
                self.page.locator('body').evaluate('''(el,text)=>{
                    document.querySelector('#join-error')?.remove();
                    const error=document.createElement('div');error.id='join-error';
                    error.textContent=text;el.append(error)}''', message)
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(
                        RuntimeError, 'meeting_passcode_rejected'):
                    prepare_preview(self.page, 'Synthetic assistant', 'synthetic-only', self.monitor)
                self.assertFalse(self.page.evaluate('!!window.submitted'))
                self.assertEqual(self.page.locator('#input-for-pwd').input_value(), '')

    def test_late_media_prompt_is_dismissed_before_retrying_join(self):
        self.page.set_content('''<style>
          .overlay {position:fixed;inset:0;background:white;z-index:100}
          .moving {animation:move .25s linear}
          @keyframes move {from {transform:translateX(100px)} to {transform:translateX(0)}}
          </style><input id="input-for-name" oninput="showPrompt()">
          <button class="preview-join-button" onclick="window.submitted=true">Join</button>
          <div class="overlay ReactModal__Content" hidden><a href="#" onclick="event.preventDefault();this.parentNode.remove()">
          Continue without microphone and camera</a></div>
          <script>let shown=false;function showPrompt(){if(shown)return;shown=true;
          document.querySelector('.preview-join-button').classList.add('moving');
          setTimeout(()=>document.querySelector('.overlay').hidden=false,30);}</script>''')
        prepare_preview(self.page, 'Synthetic assistant', '', self.monitor)
        self.assertEqual(self.page.locator('.overlay').count(), 0)
        if not self.page.evaluate('!!window.submitted'):
            prepare_preview(self.page, 'Synthetic assistant', '', self.monitor)
        self.assertTrue(self.page.evaluate('!!window.submitted'))
        self.assertFalse(self.monitor.failed)

    def test_existing_media_prompt_link_is_dismissed_without_requesting_devices(self):
        self.page.set_content('''<input id="input-for-name">
          <button class="preview-join-button" onclick="window.submitted=true">Join</button>
          <div class="ReactModal__Content" style="position:fixed;inset:0;background:white">
          <button onclick="window.devicesRequested=true">Use microphone and camera</button>
          <a href="#" onclick="event.preventDefault();this.parentNode.remove()">Continue without microphone and camera</a></div>''')
        prepare_preview(self.page, 'Synthetic assistant', '', self.monitor)
        self.assertTrue(self.page.evaluate('!!window.submitted'))
        self.assertFalse(self.page.evaluate('!!window.devicesRequested'))

    def test_join_waits_for_async_preview_validation(self):
        self.page.set_content('''<input id="input-for-name">
          <button disabled class="preview-join-button" onclick="window.submitted=true">Join</button>''')
        prepare_preview(self.page, 'Synthetic assistant', '', self.monitor)
        self.assertFalse(self.page.evaluate('!!window.submitted'))
        self.page.locator('button.preview-join-button').evaluate('el=>el.disabled=false')
        prepare_preview(self.page, 'Synthetic assistant', '', self.monitor)
        self.assertTrue(self.page.evaluate('!!window.submitted'))

    def test_unknown_overlay_is_not_dismissed_or_bypassed(self):
        self.page.set_content('''<input id="input-for-name">
          <button class="preview-join-button" onclick="window.submitted=true">Join</button>
          <div style="position:fixed;inset:0;background:white;z-index:100">Unknown prompt</div>''')
        from playwright.sync_api import TimeoutError
        with self.assertRaises(TimeoutError):
            prepare_preview(self.page, 'Synthetic assistant', '', self.monitor)
        self.assertFalse(self.page.evaluate('!!window.submitted'))


if __name__ == '__main__':
    unittest.main()
