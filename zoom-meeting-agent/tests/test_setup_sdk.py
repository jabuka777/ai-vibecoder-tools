"""Synthetic npm/tar transactions; never download SDK or change product assets."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
import setup_sdk as sdk


class SDKSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.static = self.home / 'product' / 'static'
        (self.static / 'lib').mkdir(parents=True)
        (self.static / 'lib' / 'old.asset').write_bytes(b'old SDK')
        (self.static / 'zoom-meeting-6.9.0.min.js').write_bytes(b'old JS')
        (self.static / 'index.html').write_text((CORE / 'static/index.html').read_text().replace('6.10.0','6.9.0'))
        (self.static / 'custom.txt').write_text('operator customization')

    def archive(self, entries=None):
        path = self.home / 'synthetic.tgz'
        if entries is None:
            entries = [('package/dist/zoom-meeting-6.10.0.min.js', b'new JS', tarfile.REGTYPE)]
            entries += [('package/dist/lib/vendor/' + name, b'vendor', tarfile.REGTYPE) for name in sdk.VENDOR_FILES]
            entries += [('package/dist/lib/av/synthetic.wasm', b'wasm', tarfile.REGTYPE)]
            entries += [('package/dist/ui/zoom-meetingsdk.css', b'styles', tarfile.REGTYPE),
                        ('package/dist/ui/lazy-view.js', b'lazy UI', tarfile.REGTYPE)]
        with tarfile.open(path, 'w:gz') as bundle:
            for name, content, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '../../sentinel'
                member.size = len(content) if kind == tarfile.REGTYPE else 0
                bundle.addfile(member, io.BytesIO(content) if member.size else None)
        return path

    def snapshot(self):
        return {str(path.relative_to(self.static)): path.read_bytes()
                for path in self.static.rglob('*') if path.is_file()}

    def test_import_has_no_download_or_install_side_effects(self):
        spec = importlib.util.spec_from_file_location('sdk_import_check', CORE / 'setup_sdk.py')
        module = importlib.util.module_from_spec(spec)
        with patch('runtime.run_process') as run, patch('shutil.which') as which:
            spec.loader.exec_module(module)
        run.assert_not_called()
        which.assert_not_called()

    def test_only_numeric_version_can_reach_downloader(self):
        before = self.snapshot()
        for value in ('latest', '6.10', '../6.10.0', '6.10.0;echo unsafe', '--registry=invalid', '06.10.0', ''):
            with self.subTest(value=value), patch.object(sdk, 'download_archive') as download:
                with self.assertRaises(sdk.SDKSetupError):
                    sdk.install_sdk(value, static=self.static)
                download.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_rejects_traversal_links_devices_and_windows_aliases(self):
        sentinel = self.home / 'sentinel'
        sentinel.write_bytes(b'unchanged')
        cases = [('../sentinel', tarfile.REGTYPE), ('/sentinel', tarfile.REGTYPE),
                 ('package/../../sentinel', tarfile.REGTYPE), ('other/file', tarfile.REGTYPE),
                 ('package\\sentinel', tarfile.REGTYPE), ('package/file:stream', tarfile.REGTYPE),
                 ('package/CON', tarfile.REGTYPE), ('package/file.', tarfile.REGTYPE),
                 ('package/link', tarfile.SYMTYPE), ('package/link', tarfile.LNKTYPE),
                 ('package/device', tarfile.CHRTYPE), ('package/pipe', tarfile.FIFOTYPE)]
        for index, (name, kind) in enumerate(cases):
            with self.subTest(name=name, kind=kind):
                archive = self.archive([(name, b'unsafe', kind)])
                with self.assertRaises(sdk.SDKSetupError):
                    sdk.unpack_archive(archive, self.home / f'unpacked-{index}')
                self.assertEqual(sentinel.read_bytes(), b'unchanged')

    def test_duplicate_file_and_resource_limits_are_rejected(self):
        entries = [('package/file', b'abc', tarfile.REGTYPE)]
        cases = [('duplicate', entries * 2, {}), ('count', entries * 2, {'MAX_MEMBERS': 1}),
                 ('file', entries, {'MAX_FILE': 2}), ('expanded', entries, {'MAX_EXPANDED': 2})]
        for name, content, limits in cases:
            with self.subTest(name=name):
                archive = self.archive(content)
                with patch.multiple(sdk, **limits) if limits else contextlib.nullcontext():
                    with self.assertRaises(sdk.SDKSetupError):
                        sdk.unpack_archive(archive, self.home / name)

    def test_raw_tar_stream_limit_includes_metadata_before_extraction(self):
        archive = self.archive()
        with patch.object(sdk, 'MAX_TAR_STREAM', 512), self.assertRaises(sdk.SDKSetupError):
            sdk.unpack_archive(archive, self.home / 'metadata-limit')

    def test_install_preserves_original_sdk_and_custom_files_in_backup(self):
        before = self.snapshot()
        with patch.object(sdk, 'download_archive', return_value=self.archive()):
            result = sdk.install_sdk(static=self.static)
        backup = Path(result['backup']) / 'static'
        self.assertEqual({str(path.relative_to(backup)): path.read_bytes()
                          for path in backup.rglob('*') if path.is_file()}, before)
        self.assertEqual((self.static / 'custom.txt').read_text(), 'operator customization')
        self.assertEqual((self.static / 'zoom-meeting-6.10.0.min.js').read_bytes(), b'new JS')
        self.assertIn('6.10.0', (self.static / 'index.html').read_text())
        manifest = json.loads((self.static / 'sdk-installation.json').read_text())
        self.assertEqual(manifest['version'], '6.10.0')
        self.assertEqual(len(manifest['archive_sha256']), 64)
        self.assertFalse(result['live_zoom_verified'])

    def test_publication_failure_or_interrupt_rolls_back_original_tree(self):
        before = self.snapshot()
        archive = self.archive()
        original = Path.rename
        for failure in (OSError('synthetic publication failure'), KeyboardInterrupt()):
            def fail_publish(path, target):
                if path.name == 'static' and path.parent.name.startswith('.sdk-stage-'):
                    raise failure
                return original(path, target)
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(sdk, 'download_archive', return_value=archive), \
                    patch.object(Path, 'rename', fail_publish):
                with self.assertRaises((sdk.SDKSetupError, KeyboardInterrupt)):
                    sdk.install_sdk(static=self.static)
            self.assertEqual(self.snapshot(), before)

    def test_missing_ui_is_rejected_and_complete_ui_replaces_old_version(self):
        (self.static / 'ui').mkdir()
        (self.static / 'ui/old-version.js').write_text('old UI')
        before = self.snapshot()
        complete = self.archive()
        with tarfile.open(complete) as bundle:
            entries = [(m.name, bundle.extractfile(m).read(), tarfile.REGTYPE)
                       for m in bundle.getmembers() if '/ui/' not in m.name]
        with patch.object(sdk, 'download_archive', return_value=self.archive(entries)), self.assertRaises(sdk.SDKSetupError):
            sdk.install_sdk(static=self.static)
        self.assertEqual(self.snapshot(), before)
        with patch.object(sdk, 'download_archive', return_value=self.archive()):
            result = sdk.install_sdk(static=self.static)
        self.assertEqual((self.static / 'ui/zoom-meetingsdk.css').read_bytes(), b'styles')
        self.assertEqual((self.static / 'ui/lazy-view.js').read_bytes(), b'lazy UI')
        self.assertFalse((self.static / 'ui/old-version.js').exists())
        self.assertEqual((Path(result['backup']) / 'static/ui/old-version.js').read_text(), 'old UI')
        self.assertIn('/static/lib/vendor/react-redux.min.js', (self.static / 'index.html').read_text())

    def test_missing_assets_leave_original_untouched(self):
        before = self.snapshot()
        archive = self.archive([('package/dist/file', b'incomplete', tarfile.REGTYPE)])
        with patch.object(sdk, 'download_archive', return_value=archive), self.assertRaises(sdk.SDKSetupError):
            sdk.install_sdk(static=self.static)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.static.parent / '.sdk-backups').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX symlinks')
    def test_symlink_destination_or_backup_never_moves_existing_assets(self):
        before = self.snapshot()
        alias = self.home / 'alias'
        alias.symlink_to(self.static.parent, target_is_directory=True)
        with patch.object(sdk, 'download_archive') as download, self.assertRaises(sdk.SDKSetupError):
            sdk.install_sdk(static=alias / 'static')
        download.assert_not_called()
        outside = self.home / 'outside'
        outside.mkdir()
        (self.static.parent / '.sdk-backups').symlink_to(outside, target_is_directory=True)
        with patch.object(sdk, 'download_archive', return_value=self.archive()), self.assertRaises(sdk.SDKSetupError):
            sdk.install_sdk(static=self.static)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_download_is_bounded_has_no_scripts_shell_or_private_npm_environment(self):
        folder = self.home / 'download'
        folder.mkdir()
        def downloaded(*args, **kwargs):
            (folder / 'zoom-meetingsdk-6.10.0.tgz').write_bytes(b'synthetic')
            return Mock(returncode=0)
        with patch.object(sdk, 'npm_command', return_value=['synthetic-npm']), \
                patch.dict(os.environ, {'NPM_TOKEN': 'SYNTHETIC_SECRET', 'ZOOM_SDK_CLIENT_SECRET': 'SYNTHETIC_SECRET'}), \
                patch.object(sdk, 'run_process', side_effect=downloaded) as run:
            sdk.download_archive('6.10.0', folder)
        args, kwargs = run.call_args
        self.assertIn('--ignore-scripts', args[0])
        self.assertEqual(kwargs['timeout'], 600)
        self.assertNotIn('shell', kwargs)
        self.assertNotIn('NPM_TOKEN', kwargs['env'])
        self.assertNotIn('ZOOM_SDK_CLIENT_SECRET', kwargs['env'])
        self.assertEqual(Path(kwargs['env']['npm_config_userconfig']).read_text(), '')

    def test_download_error_does_not_echo_npm_output(self):
        folder = self.home / 'download'
        folder.mkdir()
        with patch.object(sdk, 'npm_command', return_value=['synthetic-npm']), \
                patch.object(sdk, 'run_process', side_effect=subprocess.CalledProcessError(1, 'npm', stderr='SYNTHETIC_SECRET')):
            with self.assertRaises(sdk.SDKSetupError) as error:
                sdk.download_archive('6.10.0', folder)
        self.assertNotIn('SYNTHETIC_SECRET', str(error.exception))

    @unittest.skipIf(os.name == 'nt', 'POSIX lock behavior')
    def test_second_installer_cannot_enter_existing_transaction(self):
        with sdk.install_lock(self.static.parent):
            with self.assertRaises(sdk.SDKSetupError):
                with sdk.install_lock(self.static.parent):
                    self.fail('Concurrent SDK transaction was allowed')


if __name__ == '__main__':
    unittest.main()
