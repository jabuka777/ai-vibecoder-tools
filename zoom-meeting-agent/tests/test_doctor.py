"""Doctor distinguishes local prerequisites and verification, without cloud calls."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import doctor
REAL_WHISPER_PROBE = doctor.whisper_probe


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(doctor, 'module_present', return_value=True))
        self.stack.enter_context(patch.object(doctor, 'available_memory', return_value=8 * doctor.GIB))
        self.stack.enter_context(patch.object(doctor, 'free_disk', return_value=8 * doctor.GIB))
        self.browser = self.stack.enter_context(patch.object(doctor, 'chromium_probe', return_value={'installed': True, 'launched': True, 'ok': True}))
        self.model = self.stack.enter_context(patch.object(doctor, 'whisper_probe', return_value={'cached': True, 'loaded': True, 'ok': True}))
        self.stack.enter_context(patch.object(doctor.shutil, 'which', return_value='/usr/bin/test'))
        self.stack.enter_context(patch.object(doctor, 'ffmpeg_probe', return_value=True))
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(doctor, 'HERE', Path(self.directory)))

    def test_web_does_not_require_sdk_or_gui(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(doctor.platform, 'system', return_value='Linux'):
            result = doctor.collect_checks()
        self.assertTrue(result['ok'])
        self.assertFalse(result['details']['sdk_assets']['required'])
        self.assertFalse(result['details']['gui_session']['required'])
        self.assertFalse(result['zoom_join_checked'])
        self.assertFalse(result['end_to_end_checked'])
        self.assertFalse(result['browser_runtime_checked'])
        self.assertFalse(result['model_runtime_checked'])
        self.browser.assert_called_once_with(False, False)
        self.model.assert_called_once_with(False)

    def test_sdk_requires_installed_assets(self):
        result = doctor.collect_checks(mode='sdk')
        self.assertFalse(result['ok'])
        self.assertTrue(result['details']['sdk_assets']['required'])

    def test_sdk_requires_exact_referenced_assets_and_credentials(self):
        root = Path(self.directory)
        (root / 'static' / 'lib' / 'av').mkdir(parents=True)
        (root / 'static' / 'index.html').write_text('<script src="/static/zoom-meeting-6.10.0.min.js"></script>')
        (root / 'static' / 'zoom-meeting-6.9.0.min.js').write_text('old version')
        (root / 'static' / 'lib' / 'av' / 'audio.wasm').write_bytes(b'wasm')
        self.assertFalse(doctor.sdk_present())
        (root / 'static' / 'zoom-meeting-6.10.0.min.js').write_text('current version')
        self.assertTrue(doctor.sdk_present())
        with patch.object(doctor, 'load_env', return_value={}):
            self.assertFalse(doctor.collect_checks(mode='sdk')['ok'])
        with patch.object(doctor, 'load_env', return_value={'ZOOM_SDK_CLIENT_ID': 'private-id', 'ZOOM_SDK_CLIENT_SECRET': 'private-secret'}):
            result = doctor.collect_checks(mode='sdk')
        self.assertTrue(result['ok'])
        self.assertNotIn('private-', json.dumps(result))
        self.assertFalse(result['sdk_authentication_checked'])

    def test_existing_but_broken_ffmpeg_fails_readiness(self):
        with patch.object(doctor, 'ffmpeg_probe', return_value=False):
            result = doctor.collect_checks()
        self.assertTrue(result['checks']['ffmpeg'])
        self.assertFalse(result['checks']['ffmpeg_runtime'])
        self.assertFalse(result['ok'])

    def test_missing_model_is_explicit_not_downloaded(self):
        self.model.return_value = {'cached': False, 'ok': False}
        result = doctor.collect_checks()
        self.assertFalse(result['ok'])
        self.assertIn('--download-model', result['details']['whisper_model_cached']['fix'])
        self.assertNotIn('whisper_runtime', result['checks'])

    def test_runtime_checks_remain_separate_from_installed_files(self):
        self.browser.return_value = {'installed': True, 'launched': False, 'ok': False}
        self.model.return_value = {'cached': True, 'loaded': False, 'ok': False}
        result = doctor.collect_checks(check_browser=True, check_model=True)
        self.assertTrue(result['checks']['chromium_installed'])
        self.assertTrue(result['checks']['whisper_model_cached'])
        self.assertFalse(result['checks']['chromium_runtime'])
        self.assertFalse(result['checks']['whisper_runtime'])
        self.assertFalse(result['ok'])

    def test_deepseek_only_checks_key_presence_without_leaking_it(self):
        with patch.object(doctor, 'load_env', return_value={'DEEPSEEK_API_KEY': 'secret-test-key'}), patch.object(doctor, 'run_process') as run:
            result = doctor.collect_checks(backend='deepseek')
        self.assertTrue(result['checks']['deepseek_key_configured'])
        self.assertNotIn('secret-test-key', json.dumps(result))
        self.assertFalse(result['cloud_login_checked'])
        run.assert_not_called()

    def test_critical_memory_and_disk_are_errors(self):
        with patch.object(doctor, 'available_memory', return_value=256 * 1024 ** 2), patch.object(doctor, 'free_disk', return_value=32 * 1024 ** 2):
            result = doctor.collect_checks()
        self.assertFalse(result['ok'])
        self.assertFalse(result['checks']['available_memory'])
        self.assertFalse(result['checks']['disk_space'])

    def test_unknown_memory_is_reported_as_unchecked(self):
        with patch.object(doctor, 'available_memory', return_value=None):
            result = doctor.collect_checks()
        self.assertTrue(result['ok'])
        self.assertEqual(result['details']['available_memory']['status'], 'not_checked')

    def test_headed_requires_display_on_linux(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(doctor.platform, 'system', return_value='Linux'):
            result = doctor.collect_checks(headed=True)
        self.assertFalse(result['ok'])
        self.assertFalse(result['checks']['gui_session'])

    def test_backend_argument_keeps_json_and_exit_contract(self):
        with redirect_stdout(io.StringIO()) as output:
            code = doctor.main(['--backend', 'none'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(all(isinstance(value, bool) for value in result['checks'].values()))
        self.assertFalse(result['cloud_login_checked'])
        self.assertFalse(result['zoom_join_checked'])

    def test_probe_forces_offline_and_redacts_failures(self):
        result = subprocess.CompletedProcess(['fake'], 1, stdout=b'secret-test-key', stderr=b'password')
        with patch.object(doctor, 'run_process', return_value=result) as run:
            value = doctor.run_probe('print("test")')
        self.assertFalse(value['ok'])
        self.assertNotIn('secret', json.dumps(value))
        self.assertEqual(run.call_args.kwargs['env']['HF_HUB_OFFLINE'], '1')
        self.assertIsInstance(run.call_args.args[0], list)

    def test_probe_rejects_malformed_response_and_timeout(self):
        for response in (b'[]', b'{"ok": "true"}', b'not-json'):
            with self.subTest(response=response), patch.object(doctor, 'run_process', return_value=subprocess.CompletedProcess(['test'], 0, stdout=response)):
                self.assertFalse(doctor.run_probe('pass')['ok'])
        with patch.object(doctor, 'run_process', side_effect=subprocess.TimeoutExpired('private command', 1)):
            self.assertFalse(doctor.run_probe('pass')['ok'])

    def test_whisper_probe_cannot_implicitly_download(self):
        # Inspect the fixed subprocess program, rather than invoke the model.
        with patch.object(doctor, 'run_probe', return_value={'ok': True}) as run:
            REAL_WHISPER_PROBE(load=True)
        program = run.call_args.args[0]
        self.assertIn('local_files_only=True', program)
        self.assertIn('use_auth_token=False', program)
        self.assertNotIn('local_files_only=False', program)

    def test_recording_model_requires_local_cache(self):
        import types
        import stt
        from unittest.mock import Mock
        constructor = Mock()
        with patch.dict(sys.modules, {'faster_whisper': types.SimpleNamespace(WhisperModel=constructor)}):
            stt.make_model()
        self.assertTrue(constructor.call_args.kwargs['local_files_only'])


if __name__ == '__main__':
    unittest.main()
