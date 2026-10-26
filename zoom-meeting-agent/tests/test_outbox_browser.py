"""Actual Chromium and local HTTP, with a synthetic SDK send callback."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control import State, create_server
from bot import bind_control


class OutboxBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Playwright not installed')
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.playwright.stop()
            raise unittest.SkipTest('Chromium unavailable')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name), {}, '123456789')
        self.server = create_server(self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.context = self.browser.new_context()
        bind_control(self.context, self.state)
        self.page = self.context.new_page()
        self.page.goto(self.origin + '/static/outbox.js')
        self.page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / 'static/outbox.js'))
        self.state.enqueue('SYNTHETIC_MESSAGE')

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def configure(self, outcome='success'):
        self.page.evaluate('''outcome => {
          window.calls = 0;
          window.poll = window.__zbCreateOutbox({sendChat: options => {
            window.calls++;
            if (outcome !== 'timeout') setTimeout(() => options[outcome](), 10);
          }}, () => Promise.resolve(), 50);
        }''', outcome)

    def test_lost_ack_and_overlapping_polls_send_only_once(self):
        attempts = []
        def fail_first(route):
            attempts.append(True)
            if len(attempts) == 1:
                route.fulfill(status=500, body='{}')
            else:
                route.fallback()
        self.context.route(self.origin + '/outbox/ack', fail_first)
        self.configure()
        self.page.evaluate('async () => { await Promise.all([poll(), poll(), poll()]); await poll(); }')
        self.assertEqual(self.page.evaluate('calls'), 1)
        self.assertEqual(self.state.outbox[0]['state'], 'sent')
        self.assertEqual(len(attempts), 2)

    def test_failed_send_is_retained_and_never_repeated(self):
        self.configure('error')
        self.page.evaluate('async () => { await poll(); await poll(); }')
        self.assertEqual(self.page.evaluate('calls'), 1)
        self.assertEqual(self.state.outbox[0]['state'], 'failed')

    def test_missing_callback_is_uncertain_without_retry(self):
        self.configure('timeout')
        self.page.evaluate('async () => { await poll(); await poll(); }')
        self.assertEqual(self.page.evaluate('calls'), 1)
        self.assertEqual(self.state.outbox[0]['state'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
