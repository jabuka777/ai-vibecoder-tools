"""CLI validation before credentials, browsers, processes or external calls."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import runpy
import tempfile
import types
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot
import env
import webjoin


class CliInputTests(unittest.TestCase):
    def test_legacy_password_argument_is_rejected_without_echo(self):
        for module in (bot, webjoin):
            error = io.StringIO()
            with contextlib.redirect_stderr(error), patch.object(module, 'new_session') as create:
                with self.assertRaises(SystemExit) as result:
                    module.main(['--mn', '123456789', '--pwd', 'FALSE_CANARY_PASSWORD'])
                self.assertEqual(result.exception.code, 2)
                self.assertNotIn('FALSE_CANARY_PASSWORD', error.getvalue())
                create.assert_not_called()

    def test_password_file_is_read_without_process_argument_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'password.txt'
            path.write_text('FALSE_CANARY_PASSWORD\n')
            args = types.SimpleNamespace(pwd=None, password_file=path)
            self.assertEqual(env.meeting_password(args, argparse.ArgumentParser()), 'FALSE_CANARY_PASSWORD')

    def test_nonfinite_duration_is_rejected_before_any_session(self):
        for module in (bot, webjoin):
            for value in ('nan', 'inf', '-inf'):
                with contextlib.redirect_stderr(io.StringIO()), patch.object(module, 'new_session') as create:
                    with self.assertRaises(SystemExit):
                        module.main(['--mn', '123456789', '--minutes=' + value])
                    create.assert_not_called()

    def test_login_state_is_private_and_cookie_is_not_printed(self):
        from playwright import sync_api
        import login
        with tempfile.TemporaryDirectory() as directory:
            manager = MagicMock()
            context = manager.__enter__.return_value.chromium.launch.return_value.new_context.return_value
            context.storage_state.return_value = {'cookies': [{'value': 'FALSE_CANARY_COOKIE'}]}
            output = io.StringIO()
            with patch.object(login, 'APP_HOME', Path(directory)), \
                    patch.object(sync_api, 'sync_playwright', return_value=manager), \
                    patch.object(login.sys.stdin, 'isatty', return_value=True), \
                    patch('builtins.input', return_value=''), contextlib.redirect_stdout(output):
                self.assertEqual(login.main(['--account', 'personal']), 0)
            path = Path(directory) / 'accounts' / 'personal' / 'storage_state.json'
            self.assertEqual(json.loads(path.read_text())['cookies'][0]['value'], 'FALSE_CANARY_COOKIE')
            self.assertNotIn('FALSE_CANARY_COOKIE', output.getvalue())
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
