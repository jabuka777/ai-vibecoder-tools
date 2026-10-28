"""Personal/assistant boundaries on synthetic files and real local Chromium contexts."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import configure
import login
import webjoin


def state(value):
    return {'cookies': [{'name': 'account_canary', 'value': value, 'domain': '127.0.0.1',
            'path': '/', 'expires': -1, 'httpOnly': False, 'secure': False, 'sameSite': 'Lax'}], 'origins': []}


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'data'

    def test_two_logins_are_private_and_selection_never_changes_the_other(self):
        personal = accounts.save_state(self.home, 'personal', state('personal-only'))
        assistant = accounts.save_state(self.home, 'assistant', state('assistant-only'))
        self.assertEqual(accounts.select_state(self.home, 'personal'), personal)
        self.assertEqual(accounts.select_state(self.home, 'assistant'), assistant)
        self.assertIsNone(accounts.select_state(self.home, 'guest'))
        self.assertIsNone(accounts.select_state(self.home, 'default'))
        self.assertEqual(json.loads(personal.read_text()), state('personal-only'))
        self.assertEqual(json.loads(assistant.read_text()), state('assistant-only'))
        if os.name == 'posix':
            for p in (personal, assistant):
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
                self.assertEqual(p.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(p.parent.parent.stat().st_mode & 0o777, 0o700)

    def test_missing_personal_never_launches_as_assistant_legacy_or_guest(self):
        accounts.save_state(self.home, 'assistant', state('assistant-only'))
        accounts.save_state(self.home, 'default', state('legacy-only'))
        with patch.object(webjoin, 'APP_HOME', self.home), patch.object(webjoin, 'new_session') as session, \
                patch.object(webjoin, 'start_stt') as stt, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                webjoin.main(['--mn', '123456789', '--account', 'personal'])
        self.assertEqual(result.exception.code, 2)
        session.assert_not_called()
        stt.assert_not_called()

    def test_explicit_renewal_preserves_old_login_and_peer(self):
        p = accounts.save_state(self.home, 'personal', state('old-personal'))
        a = accounts.save_state(self.home, 'assistant', state('assistant-only'))
        with self.assertRaises(accounts.AccountError):
            accounts.save_state(self.home, 'personal', state('new-personal'))
        self.assertEqual(json.loads(p.read_text()), state('old-personal'))
        accounts.save_state(self.home, 'personal', state('new-personal'), replace=True)
        backups = list((p.parent / 'login-backups').glob('*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), state('old-personal'))
        self.assertEqual(json.loads(a.read_text()), state('assistant-only'))

    def test_listing_does_not_create_home_or_expose_cookie_values(self):
        self.assertFalse(self.home.exists())
        self.assertTrue(all(not row['saved_login_present'] for row in accounts.list_accounts(self.home)))
        self.assertFalse(self.home.exists())
        accounts.save_state(self.home, 'personal', state('private-canary'))
        output = io.StringIO()
        with patch.object(login, 'APP_HOME', self.home), contextlib.redirect_stdout(output):
            self.assertEqual(login.main(['--list']), 0)
        self.assertNotIn('private-canary', output.getvalue())
        self.assertTrue(json.loads(output.getvalue())['accounts'][0]['saved_login_present'])

    def test_import_is_explicit_local_and_does_not_launch_browser(self):
        source = Path(self.temp.name) / 'old-login.json'
        configure.private_write(source, json.dumps(state('import-canary')))
        with patch.object(login, 'APP_HOME', self.home), patch('playwright.sync_api.sync_playwright') as browser, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(login.main(['--account', 'assistant', '--import-state', str(source)]), 0)
        browser.assert_not_called()
        self.assertNotIn('import-canary', output.getvalue())
        self.assertEqual(json.loads(accounts.select_state(self.home, 'assistant').read_text()), state('import-canary'))
        self.assertTrue(source.exists())

    def test_account_identifiers_and_state_shape_are_closed(self):
        for account in ('../personal', '/tmp/state', '', None, True, 'someone@example.com'):
            with self.subTest(account=account), self.assertRaises(accounts.AccountError):
                accounts.state_path(self.home, account)
        for value in ([], {'cookies': 'wrong'}, {'origins': {}}):
            with self.assertRaises(accounts.AccountError):
                accounts.save_state(self.home, 'assistant', value)

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership and symlinks')
    def test_shared_or_symlinked_account_paths_fail_closed(self):
        self.home.mkdir()
        (self.home / 'accounts').mkdir(mode=0o755)
        with self.assertRaises(accounts.AccountError):
            accounts.save_state(self.home, 'personal', state('must-not-save'))
        (self.home / 'accounts').chmod(0o700)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir(mode=0o700)
        (self.home / 'accounts' / 'personal').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(accounts.AccountError):
            accounts.save_state(self.home, 'personal', state('must-not-save'))
        self.assertEqual(list(outside.iterdir()), [])

    def test_explicit_run_override_does_not_change_saved_meeting_account(self):
        p = configure.profile_path(self.home, 'test')
        configure.private_write(p, json.dumps({'meeting': '123456789', 'account': 'personal'}))
        calls = []
        engine = types.SimpleNamespace(main=lambda args: calls.append(args) or 0)
        with patch('importlib.import_module', return_value=engine), patch.dict(os.environ):
            self.assertEqual(configure.run_profile(self.home, 'test', account='assistant'), 0)
        self.assertEqual(calls[0][calls[0].index('--account') + 1], 'assistant')
        self.assertEqual(configure.load_profile(self.home, 'test')['account'], 'personal')

    def test_meeting_wizard_asks_account_before_private_link(self):
        with patch.object(configure.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', return_value='1'), \
                patch.object(configure, 'secret', side_effect=['123456789', 'synthetic-passcode']), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(configure.main(['--home', str(self.home), 'meeting', 'test']), 0)
        self.assertEqual(configure.load_profile(self.home, 'test')['account'], 'personal')


class BrowserAccountTests(unittest.TestCase):
    def test_real_browser_contexts_receive_only_selected_login_and_guest_receives_none(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as folder, sync_playwright() as pw:
            home = Path(folder)
            accounts.save_state(home, 'personal', state('personal-browser-canary'))
            accounts.save_state(home, 'assistant', state('assistant-browser-canary'))
            browser = pw.chromium.launch(headless=True)
            try:
                for role in ('personal', 'assistant', 'guest'):
                    path = accounts.select_state(home, role)
                    context = browser.new_context(storage_state=str(path) if path else None)
                    try:
                        values = [item['value'] for item in context.cookies('http://127.0.0.1/')]
                        self.assertEqual(values, [] if role == 'guest' else [role + '-browser-canary'])
                    finally:
                        context.close()
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
