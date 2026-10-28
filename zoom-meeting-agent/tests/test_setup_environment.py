"""Installer recovery and boundaries; no downloads or system changes."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_environment as setup


class FakeInstallation:
    def __init__(self, root):
        self.root = root
        self.home = root / 'user data'
        self.requirements = root / 'requirements.txt'
        self.requirements.write_text('numpy==2.0\n')
        self.mcp = root / 'requirements-mcp.txt'
        self.mcp.write_text('mcp==2.0.0\n')
        self.browser = False
        self.model = False
        self.packages = {'numpy': '2.0'}
        self.calls = []
        self.fail_pip_once = False

    def execute(self, args, **kwargs):
        command = [str(arg) for arg in args]
        self.calls.append(command)
        if command[1:3] == ['-m', 'venv']:
            python = setup.interpreter(self.home)
            python.parent.mkdir(parents=True)
            python.touch()
            (self.home / '.venv' / 'pyvenv.cfg').write_text('home = test\n')
        if 'pip' in command and 'install' in command:
            if self.fail_pip_once:
                self.fail_pip_once = False
                raise setup.SetupError('command_failed', 'repeat setup')
            if str(self.mcp) in command:
                self.packages['mcp'] = '2.0.0'
        if 'playwright' in command and 'install' in command:
            self.browser = True
        if any('download_model("small", use_auth_token=False)' in arg for arg in command):
            self.model = True
        if any('snapshot_download(' in arg and 'force_download=True' in arg for arg in command):
            self.model = True

    def activate(self, stack):
        # Exercise receipt/recovery algorithms independently of the host OS.
        # The real unsupported-platform boundary has its own negative test.
        stack.enter_context(patch.object(setup, 'mcp_supported', return_value=True))
        stack.enter_context(patch.object(setup, 'REQUIREMENTS', self.requirements))
        stack.enter_context(patch.object(setup, 'MCP_REQUIREMENTS', self.mcp))
        stack.enter_context(patch.object(setup, 'command', side_effect=self.execute))
        stack.enter_context(patch.object(setup, 'inventory', side_effect=lambda _: dict(self.packages)))
        stack.enter_context(patch.object(setup, 'dependencies_valid', return_value=True))
        stack.enter_context(patch.object(setup, 'browser_present', side_effect=lambda _: self.browser))
        stack.enter_context(patch.object(setup, 'model_present', side_effect=lambda _: self.model))
        stack.enter_context(patch.object(setup, 'model_usable', side_effect=lambda _: self.model))
        stack.enter_context(patch.object(setup, 'free_disk', return_value=8 * setup.GIB))
        stack.enter_context(patch.object(setup, 'available_memory', return_value=8 * setup.GIB))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fake(self, stack):
        fake = FakeInstallation(self.root)
        fake.activate(stack)
        return fake

    def test_default_is_read_only_even_for_missing_home(self):
        home = self.root / 'not created'
        with patch.object(setup, 'run_process') as command, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(setup.main(['--home', str(home), '--json']), 0)
        self.assertTrue(json.loads(output.getvalue())['read_only'])
        self.assertFalse(home.exists())
        command.assert_not_called()

    def test_plan_uses_platform_paths_and_never_runs_system_commands(self):
        with patch.object(setup, 'run_process') as run:
            for system in ('Linux', 'Darwin', 'Windows'):
                result = setup.plan(self.root, system)
                self.assertFalse(result['system_dependencies']['automatic'])
                self.assertEqual('Scripts/python.exe' in Path(result['venv_python']).as_posix(), system == 'Windows')
                self.assertTrue(all(isinstance(cmd, list) for cmd in result['system_dependencies']['commands_for_manual_review']))
        run.assert_not_called()

    def test_repeated_install_validates_then_skips_without_touching_config(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            fake.home.mkdir()
            config = fake.home / '.env'
            config.write_text('PRIVATE_SETTING=private-test-value\n')
            first = setup.install(fake.home, install_core=True)
            self.assertEqual(first['performed'], ['venv', 'dependencies', 'chromium'])
            fake.calls.clear()
            second = setup.install(fake.home, install_core=True)
            self.assertEqual(second['performed'], [])
            self.assertFalse(any('install' in call for call in fake.calls))
            self.assertEqual(config.read_text(), 'PRIVATE_SETTING=private-test-value\n')
            self.assertNotIn('private-test-value', (fake.home / setup.STATE_NAME).read_text())

    def test_failed_step_resumes_and_keeps_completed_environment(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            fake.fail_pip_once = True
            with self.assertRaises(setup.SetupError):
                setup.install(fake.home, install_core=True)
            state = setup.read_state(fake.home)
            self.assertEqual(state['steps']['dependencies']['status'], 'failed')
            fake.calls.clear()
            result = setup.install(fake.home, install_core=True)
            self.assertNotIn('venv', result['performed'])
            self.assertEqual(result['performed'], ['dependencies', 'chromium'])

    def test_changed_packages_cannot_hide_behind_complete_receipt(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            setup.install(fake.home, install_core=True)
            fake.packages['numpy'] = 'unexpected-version'
            fake.calls.clear()
            result = setup.install(fake.home, install_core=True)
            self.assertIn('dependencies', result['performed'])

    def test_changed_requirements_reinstalls(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            setup.install(fake.home, install_core=True)
            fake.requirements.write_text('numpy==2.1\n')
            self.assertIn('dependencies', setup.install(fake.home, install_core=True)['performed'])

    def test_core_reinstall_cannot_overwrite_mcp_receipt(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            setup.install(fake.home, install_core=True, install_mcp=True)
            for value in (None, '2.0.1'):
                with self.subTest(mcp_version=value):
                    if value is None:
                        fake.packages.pop('mcp')
                    else:
                        fake.packages['mcp'] = value
                    fake.requirements.write_text(fake.requirements.read_text() + '# changed\n')
                    result = setup.install(fake.home, install_core=True, install_mcp=True)
                    self.assertIn('dependencies', result['performed'])
                    self.assertIn('mcp', result['performed'])
                    self.assertEqual(fake.packages['mcp'], '2.0.0')

    def test_partial_venv_without_pip_is_repaired_and_resumed(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            base_execute = fake.execute

            def incomplete_venv(args, **kwargs):
                base_execute(args, **kwargs)
                if '-m' in args and 'venv' in args:
                    raise setup.SetupError('command_failed', 'ensurepip failed')
            with patch.object(setup, 'command', side_effect=incomplete_venv):
                with self.assertRaises(setup.SetupError):
                    setup.install(fake.home, install_core=True)
            self.assertTrue(setup.interpreter(fake.home).exists())
            self.assertEqual(setup.read_state(fake.home)['steps']['venv']['status'], 'failed')
            fake.calls.clear()
            with patch.object(setup, 'pip_available', side_effect=[False, True]):
                result = setup.install(fake.home, install_core=True)
            self.assertTrue(any('ensurepip' in call for call in fake.calls))
            self.assertTrue(result['requested_steps_complete'])

    def test_model_download_requires_explicit_flag(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            setup.install(fake.home, install_core=True)
            self.assertFalse(fake.model)
            result = setup.install(fake.home, download_model=True)
            self.assertEqual(result['performed'], ['model'])
            self.assertTrue(fake.model)
            self.assertEqual(setup.install(fake.home, download_model=True)['performed'], [])

    def test_corrupt_cached_model_is_not_reported_ready(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            setup.install(fake.home, install_core=True)
            fake.model = True
            with patch.object(setup, 'model_usable', return_value=False):
                with self.assertRaisesRegex(setup.SetupError, 'model_cache_unusable'):
                    setup.install(fake.home, download_model=True)
            result = setup.install(fake.home, download_model=True, repair_model=True)
            self.assertIn('model', result['performed'])
            self.assertTrue(any('force_download=True' in arg for call in fake.calls for arg in call))

    def test_repair_requires_explicit_download_and_unsupported_mcp_does_no_work(self):
        with patch.object(setup, 'command') as command:
            with self.assertRaisesRegex(setup.SetupError, 'repair_requires_download'):
                setup.install(self.root / 'missing', repair_model=True)
            with patch.object(setup, 'mcp_supported', return_value=False):
                with self.assertRaisesRegex(setup.SetupError, 'mcp_platform_unsupported'):
                    setup.install(self.root / 'missing', install_core=True, install_mcp=True)
            command.assert_not_called()
        self.assertFalse((self.root / 'missing').exists())

    def test_optional_actions_need_existing_venv(self):
        for kwargs in ({'download_model': True}, {'install_mcp': True}):
            with self.subTest(kwargs=kwargs), patch.object(setup, 'command') as command:
                with self.assertRaisesRegex(setup.SetupError, 'venv_missing'):
                    setup.install(self.root / 'missing', **kwargs)
                command.assert_not_called()
                self.assertFalse((self.root / 'missing').exists())

    def test_critical_memory_blocks_changes(self):
        with patch.object(setup, 'available_memory', return_value=128 * 1024 ** 2), patch.object(setup, 'command') as command:
            with self.assertRaisesRegex(setup.SetupError, 'available_memory_low'):
                setup.install(self.root / 'missing', install_core=True)
            command.assert_not_called()
        self.assertFalse((self.root / 'missing').exists())

    def test_mcp_install_has_own_receipt_and_is_repeatable(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            first = setup.install(fake.home, install_core=True, install_mcp=True)
            self.assertIn('mcp', first['performed'])
            self.assertEqual(setup.install(fake.home, install_core=True, install_mcp=True)['performed'], [])

    def test_missing_browser_is_repaired_despite_previous_success(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            setup.install(fake.home, install_core=True)
            fake.browser = False
            result = setup.install(fake.home, install_core=True)
            self.assertEqual(result['performed'], ['chromium'])

    def test_unmanaged_incomplete_venv_is_not_overwritten(self):
        home = self.root / 'home'
        (home / '.venv').mkdir(parents=True)
        marker = home / '.venv' / 'do-not-overwrite'
        marker.write_text('existing')
        with patch.object(setup, 'command') as command:
            with self.assertRaisesRegex(setup.SetupError, 'unmanaged_incomplete_venv'):
                setup.install(home, install_core=True)
            command.assert_not_called()
        self.assertEqual(marker.read_text(), 'existing')

    def test_invalid_state_is_preserved(self):
        home = self.root
        path = home / setup.STATE_NAME
        for text in ('not-json', '{"version": 1, "steps": {"venv": "wrong"}}'):
            path.write_text(text)
            with self.assertRaisesRegex(setup.SetupError, 'state_unreadable'):
                setup.read_state(home)
            self.assertEqual(path.read_text(), text)

    @unittest.skipIf(sys.platform == 'win32', 'symlink creation needs Windows-specific privileges')
    def test_state_symlink_does_not_overwrite_target(self):
        target = self.root / 'keep.txt'
        target.write_text('do-not-change')
        (self.root / setup.STATE_NAME).symlink_to(target)
        with self.assertRaisesRegex(setup.SetupError, 'unsafe_state_file'):
            setup.write_state(self.root, {'version': 1, 'steps': {}})
        self.assertEqual(target.read_text(), 'do-not-change')

    @unittest.skipIf(sys.platform == 'win32', 'symlink creation needs Windows-specific privileges')
    def test_venv_symlink_rejected(self):
        other = self.root / 'other'
        other.mkdir()
        (self.root / '.venv').symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(setup.SetupError, 'unsafe_venv'):
            setup.install(self.root, install_core=True)
        self.assertEqual(list(other.iterdir()), [])

    def test_lock_releases_on_failure_and_blocks_concurrent_setup(self):
        with self.assertRaisesRegex(RuntimeError, 'test interrupted'):
            with setup.setup_lock(self.root):
                with self.assertRaisesRegex(setup.SetupError, 'setup_in_progress'):
                    with setup.setup_lock(self.root):
                        self.fail('second installer entered lock')
                raise RuntimeError('test interrupted')
        with setup.setup_lock(self.root):
            pass

    def test_command_failure_never_echoes_credentials(self):
        result = subprocess.CompletedProcess(['fake'], 1, stdout=b'secret-token', stderr=b'secret-password')
        with patch.object(setup, 'run_process', return_value=result):
            with self.assertRaises(setup.SetupError) as raised:
                setup.command(['fake'])
        self.assertNotIn('secret', str(raised.exception))
        self.assertNotIn('secret', raised.exception.remedy)

    def test_installer_commands_are_argv_and_never_os_package_manager(self):
        with ExitStack() as stack:
            fake = self.fake(stack)
            setup.install(fake.home, install_core=True, install_mcp=True, download_model=True)
            self.assertTrue(all(isinstance(command, list) for command in fake.calls))
            self.assertFalse(any(command[0] in ('sudo', 'apt', 'brew', 'winget', 'sh', 'bash') for command in fake.calls))


if __name__ == '__main__':
    unittest.main()
