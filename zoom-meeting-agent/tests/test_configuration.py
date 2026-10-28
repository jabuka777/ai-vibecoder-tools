import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import warnings
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
import configure
import env

spec = importlib.util.spec_from_file_location('skill_installer', CORE.parent / 'tools/install_zoom_skill.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_supported_link_is_parsed_without_fetching(self):
        self.assertEqual(configure.parse_meeting('https://us02web.zoom.us/j/12345678901?pwd=TEST%2BVALUE'),
                         ('12345678901', 'TEST+VALUE'))
        self.assertEqual(configure.parse_meeting('123 456 789'), ('123456789', ''))
        for value in ('https://zoom.us.attacker.invalid/j/123456789', 'http://zoom.us/j/123456789',
                      'https://user:pass@zoom.us/j/123456789', 'https://zoom.us/j/123456789?pwd=a&pwd=b',
                      'https://zoom.us:445/j/123456789', 'https://zoom.us/j/１２３４５６７８９'):
            with self.subTest(value=value), self.assertRaises(configure.ConfigurationError):
                configure.parse_meeting(value)

    def test_profiles_are_private_and_never_overwritten_implicitly(self):
        path = configure.profile_path(self.home, 'demo')
        data = configure.validate_profile({'meeting': '123456789', 'password': 'SYNTHETIC'})
        configure.private_write(path, json.dumps(data))
        self.assertEqual(configure.load_profile(self.home, 'demo'), data)
        with self.assertRaises(configure.ConfigurationError):
            configure.private_write(path, '{}')
        self.assertEqual(json.loads(path.read_text()), data)
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)
            with self.assertRaises(configure.ConfigurationError):
                configure.load_profile(self.home, 'demo')

    def test_traversal_symlink_unknown_fields_and_nonfinite_duration_fail(self):
        for name in ('../other', '/tmp/other', '', 'a.b'):
            with self.assertRaises(configure.ConfigurationError):
                configure.profile_path(self.home, name)
        for extra in ({'minutes': float('nan')}, {'minutes': True}, {'name': 'x\ny'},
                      {'password': 'a\nb'}, {'unknown': 'field'}, {'version': True}):
            with self.assertRaises(configure.ConfigurationError):
                configure.validate_profile({'meeting': '123456789', **extra})
        if os.name != 'nt':
            outside = self.home / 'outside'
            outside.mkdir()
            (self.home / 'meetings').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(configure.ConfigurationError):
                configure.profile_path(self.home, 'demo')

    def test_credentials_preserve_other_settings_without_echo(self):
        configure.private_write(self.home / '.env', 'AGENT_NAME=Demo\n')
        output = io.StringIO()
        with patch.object(configure, 'secret', return_value='SYNTHETIC_KEY'), contextlib.redirect_stdout(output):
            configure.credentials(self.home, 'deepseek')
        self.assertNotIn('SYNTHETIC_KEY', output.getvalue())
        self.assertIn('AGENT_NAME=Demo', (self.home / '.env').read_text())
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'ENVIRONMENT_KEY'}):
            self.assertEqual(env.load_env(self.home)['DEEPSEEK_API_KEY'], 'ENVIRONMENT_KEY')
        with self.assertRaises(configure.ConfigurationError):
            configure.credentials(self.home, 'deepseek')

    def test_noninteractive_secret_input_never_falls_back_to_echo(self):
        with patch.object(configure.sys.stdin, 'isatty', return_value=False), patch.object(configure.getpass, 'getpass') as prompt:
            with self.assertRaises(configure.ConfigurationError):
                configure.secret('key')
            prompt.assert_not_called()

    def test_terminal_echo_fallback_is_rejected(self):
        def fallback(*args):
            warnings.warn('no private terminal', configure.getpass.GetPassWarning)
            self.fail('Echo fallback must not be reached')
        with patch.object(configure.sys.stdin, 'isatty', return_value=True), patch.object(configure.getpass, 'getpass', side_effect=fallback):
            with self.assertRaises(configure.ConfigurationError):
                configure.secret('key')

    @unittest.skipUnless(os.name == 'posix', 'POSIX directory permissions')
    def test_existing_shared_profile_directory_fails_before_secret_prompt(self):
        (self.home / 'meetings').mkdir(mode=0o755)
        with self.assertRaises(configure.ConfigurationError):
            configure.profile_path(self.home, 'demo')

    def test_run_profile_passes_only_password_file_and_removes_temporary(self):
        path = configure.profile_path(self.home, 'demo')
        configure.private_write(path, json.dumps({'meeting': '123456789', 'password': 'SYNTHETIC_KEY'}))
        captured = []
        def run(args):
            self.assertNotIn('SYNTHETIC_KEY', ' '.join(args))
            password_file = Path(args[args.index('--password-file') + 1])
            captured.append(password_file)
            self.assertEqual(password_file.read_text(), 'SYNTHETIC_KEY')
            self.assertEqual(args[args.index('--llm') + 1], 'none')
            return 0
        with patch('importlib.import_module', return_value=types.SimpleNamespace(main=run)), patch.dict(os.environ):
            self.assertEqual(configure.run_profile(self.home, 'demo'), 0)
        self.assertFalse(captured[0].exists())


class SkillInstallerTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'symlink setup requires privileges on Windows')
    def test_symlink_ancestor_or_backup_is_never_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            outside = home / 'outside'
            outside.mkdir()
            (home / '.agents').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                installer.install('codex', target_home=home, app_home=home / 'data')
            self.assertEqual(list(outside.iterdir()), [])
            first = installer.install('claude', target_home=home, app_home=home / 'data')
            (Path(first['path']) / 'SKILL.md').write_text('custom')
            (home / '.jabuka-tools-skill-backups').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                installer.install('claude', target_home=home, app_home=home / 'data', replace=True)
            self.assertEqual((Path(first['path']) / 'SKILL.md').read_text(), 'custom')

    def test_install_repeat_update_preserves_previous_and_other_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for client in installer.CLIENTS:
                first = installer.install(client, target_home=home, app_home=home / 'data')
                self.assertEqual(first['status'], 'installed')
                self.assertEqual(installer.install(client, target_home=home, app_home=home / 'data')['status'], 'unchanged')
                installed = Path(first['path'])
                (installed / 'SKILL.md').write_text('user customization')
                with self.assertRaises(ValueError):
                    installer.install(client, target_home=home, app_home=home / 'data')
                replaced = installer.install(client, target_home=home, app_home=home / 'data', replace=True)
                self.assertEqual((Path(replaced['backup']) / 'SKILL.md').read_text(), 'user customization')
                self.assertTrue((installed / 'scripts/zoom.py').exists())

    def test_config_snippets_have_absolute_commands_and_no_secrets(self):
        home = Path(tempfile.gettempdir()).resolve() / 'synthetic home'
        snippet = installer.mcp_configuration('claude', home)
        server = json.loads(snippet)['mcpServers']['zoom-meeting-agent']
        self.assertTrue(Path(server['command']).is_absolute())
        self.assertIn('--home', server['args'])
        self.assertNotIn('env', server)
        self.assertTrue(installer.mcp_configuration('codex', home).startswith('[mcp_servers.zoom-meeting-agent]'))


if __name__ == '__main__':
    unittest.main()
