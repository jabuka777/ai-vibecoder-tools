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
        self.page.locator('body').evaluate("el => el.append('Wrong passcode. Please try again.')")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(
                RuntimeError, 'meeting_passcode_rejected'):
            prepare_preview(self.page, 'Synthetic assistant', 'synthetic-only', self.monitor)
        self.assertFalse(self.page.evaluate('!!window.submitted'))
        self.assertEqual(self.page.locator('#input-for-pwd').input_value(), '')


if __name__ == '__main__':
    unittest.main()
