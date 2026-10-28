#!/usr/bin/env python3
"""Actual OS installation from a source ZIP; no Zoom accounts or paid models.

pack is run in a clean checkout. run needs ffmpeg and a new work directory.
It explicitly downloads dependencies, Chromium and Whisper small into isolated
caches. Later component checks and installation resume prohibit downloads.
This is partial N24/N25 evidence, never independent human N26 or real Zoom.
"""
import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def pack(args):
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if revision != args.revision or subprocess.check_output(['git', 'status', '--porcelain'], cwd=root):
        raise ValueError('Packaging requires the exact clean commit, including all fixture files')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    archive = output / 'zoom-meeting-agent.zip'
    subprocess.run([sys.executable, str(root / 'tools/build_release.py'), '--output', str(archive)], check=True)
    bootstrap = output / 'portable_acceptance.py'
    shutil.copyfile(__file__, bootstrap)
    save(output / 'manifest.json', {'schema': 1, 'revision': revision,
         'archive': archive.name, 'archive_sha256': digest(archive),
         'bootstrap_sha256': digest(bootstrap), 'source_is_clean': True})
    return 0


def extract(package, work, revision):
    manifest = json.loads((package / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('revision') != revision or manifest.get('source_is_clean') is not True:
        raise ValueError('Source commit does not match the requested clean revision')
    archive = package / 'zoom-meeting-agent.zip'
    if digest(archive) != manifest['archive_sha256'] or digest(__file__) != manifest['bootstrap_sha256']:
        raise ValueError('Source package checksum mismatch')
    destination = work / 'Source archive Кириллица'
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 2000 or sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
            raise ValueError('Source archive exceeds acceptance limits')
        names = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename or
                    not path.parts or path.parts[0] != 'jabuka-tools' or ':' in entry.filename or
                    entry.filename in names or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Unsafe source archive member')
            names.add(entry.filename)
        bundle.extractall(destination)
    root = destination / 'jabuka-tools'
    if digest(root / 'tools/portable_acceptance.py') != manifest['bootstrap_sha256']:
        raise ValueError('Bootstrap and archived harness differ')
    return root, manifest


def child_environment(work):
    # Preserve OS paths; never reuse HOME/CODEX_HOME as a scratch directory.
    allowed = {'PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP',
               'TMPDIR', 'LANG', 'LC_ALL', 'APPDATA', 'LOCALAPPDATA', 'LD_LIBRARY_PATH',
               'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'DISPLAY', 'WAYLAND_DISPLAY'}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(HF_HOME=str(work / 'Whisper cache'),
               PLAYWRIGHT_BROWSERS_PATH=str(work / 'Browser cache'),
               PIP_CACHE_DIR=str(work / 'Package cache'), PIP_CONFIG_FILE=os.devnull,
               HF_HUB_DISABLE_IMPLICIT_TOKEN='1', HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1',
               PYTHONNOUSERSITE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               NO_PROXY='127.0.0.1,localhost', HF_TOKEN='')
    return env


def offline_environment(env):
    return dict(env, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PIP_NO_INDEX='1',
                HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9',
                ALL_PROXY='http://127.0.0.1:9', PLAYWRIGHT_DOWNLOAD_HOST='http://127.0.0.1:9')


def run_acceptance(args):
    report = {'schema': 1, 'scope': 'Fresh source ZIP installation and local components; not real Zoom or independent novice acceptance',
              'revision': args.revision, 'success': False, 'steps': [],
              'platform': {'system': platform.system(), 'release': platform.release(),
                           'version': platform.version(), 'machine': platform.machine(),
                           'description': platform.platform(),
                           'windows_edition': platform.win32_edition() if os.name == 'nt' else None,
                           'macos_version': platform.mac_ver()[0] if sys.platform == 'darwin' else None,
                           'python': platform.python_version(),
                           'runner_label': os.environ.get('ACCEPTANCE_RUNNER_LABEL'),
                           'image_os': os.environ.get('ImageOS'), 'image_version': os.environ.get('ImageVersion')},
              'not_verified': ['real Zoom F01/F02/F13', 'Windows 11 unless actual OS says Windows 11',
                               'independent human N26', 'recognition quality corpus', 'cloud LLM', 'Windows MCP'],
              'downloads': 'Explicit setup downloads from Python package index, Playwright and Hugging Face; no meeting data is sent'}
    started = time.monotonic()
    work, evidence = args.work.resolve(), args.report.resolve().parent
    evidence.mkdir(parents=True, exist_ok=True)

    def publish():
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        def clean(value):
            if isinstance(value, str):
                return value.replace(str(work), '<WORK>')
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value
        save(args.report, clean(report))

    def execute(name, command, env, timeout=300, expected=0, json_result=False):
        began = time.monotonic()
        item = {'name': name, 'expected_exit_code': expected}
        report['steps'].append(item)
        publish()
        print('Running ' + name, flush=True)
        result = process_runner([str(value) for value in command], env=env, cwd=work,
                                capture_output=True, timeout=timeout)
        item.update(exit_code=result.returncode, elapsed_seconds=round(time.monotonic() - began, 3))
        # Only fixed synthetic jobs run here, with a secret-free environment.
        stdout, stderr = result.stdout.decode('utf-8', errors='replace'), result.stderr.decode('utf-8', errors='replace')
        item['stdout_tail'] = stdout[-12000:]
        item['stderr_tail'] = stderr[-6000:]
        publish()
        if result.returncode != expected:
            raise RuntimeError('Unexpected exit code for ' + name)
        if json_result:
            value = json.loads(stdout)
            item['result'] = value
            return value
        return result

    try:
        if work.exists():
            raise ValueError('Use a new work directory; acceptance never deletes or reuses a previous run')
        work.mkdir(parents=True)
        root, manifest = extract(args.package.resolve(), work, args.revision)
        report['package'] = manifest
        # Use the product's owned process-tree cleanup for bounded commands.
        # Suppress bytecode only while importing the harness helper; child setup
        # retains its own normal policy, measured by the readonly-plan snapshot.
        before_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(root / 'zoom-meeting-agent'))
        try:
            from runtime import run_process as process_runner
        finally:
            sys.dont_write_bytecode = before_bytecode
        env = child_environment(work)
        home, client_home = work / 'App data Записи', work / 'Client profile'
        installer = root / 'tools/install_zoom_skill.py'
        execute('install_skills', [sys.executable, installer, '--client', 'all', '--target-home', client_home, '--home', home], env)
        wrapper = client_home / '.agents/skills/zoom-meeting-agent/scripts/zoom.py'
        source_before = {str(p.relative_to(root)): digest(p) for p in root.rglob('*') if p.is_file()}
        plan = execute('readonly_plan', [sys.executable, wrapper, 'setup', '--json'], env, json_result=True)
        if not plan['read_only'] or home.exists():
            raise AssertionError('Readonly plan created application data')
        source_after = {str(p.relative_to(root)): digest(p) for p in root.rglob('*') if p.is_file()}
        report['readonly_source_changes'] = sorted(name for name in set(source_after) | set(source_before)
                                                  if source_before.get(name) != source_after.get(name))
        home.mkdir(mode=0o700)
        sentinel = home / '.env'
        sentinel.write_text('AGENT_NAME=Synthetic acceptance assistant\n', encoding='utf-8')
        sentinel_before = digest(sentinel)
        flags = ['--install', '--download-model'] + (['--install-mcp'] if os.name == 'posix' else [])
        install = execute('fresh_install', [sys.executable, wrapper, 'setup', *flags, '--json'], env,
                          timeout=2400, json_result=True)
        if not install['requested_steps_complete'] or not {'venv', 'dependencies', 'chromium', 'model'} <= set(install['performed']):
            raise AssertionError('Fresh installation silently reused a required environment/cache step')
        if digest(sentinel) != sentinel_before:
            raise AssertionError('Installation changed an existing config')
        python = home / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        offline = offline_environment(env)
        doctor = execute('doctor_runtime', [python, wrapper, 'doctor', '--check-browser', '--check-model'], offline,
                         timeout=240, json_result=True)
        if not doctor['ok'] or not doctor['checks']['chromium_runtime'] or not doctor['checks']['whisper_runtime']:
            raise AssertionError('Browser or local model is not operational')
        repeated = execute('offline_resume', [python, wrapper, 'setup', *flags, '--json'], offline,
                           timeout=240, json_result=True)
        if repeated['performed'] or digest(sentinel) != sentinel_before:
            raise AssertionError('Completed setup was not repeatable')
        execute('repeat_skill_install', [python, installer, '--client', 'all', '--target-home', client_home, '--home', home], offline)
        if os.name == 'nt':
            unsupported_home = work / 'Unsupported MCP'
            unsupported = execute('windows_mcp_refused', [python, root / 'zoom-meeting-agent/setup_environment.py',
                '--home', unsupported_home, '--install', '--install-mcp', '--json'], offline, expected=1, json_result=True)
            if unsupported['code'] != 'mcp_platform_unsupported' or unsupported_home.exists():
                raise AssertionError('Unsupported Windows MCP was not refused before changes')
        execute('portable_component_tests', [python, root / 'tools/portable_acceptance.py', 'suite',
                    '--root', root, '--output', evidence / 'tests.json'], offline, timeout=420)
        execute('real_local_speech', [python, root / 'tools/portable_acceptance.py', 'speech',
                    '--root', root, '--session', work / 'Synthetic speech', '--output', evidence / 'speech.json'],
                    offline, timeout=420)
        packages = execute('package_inventory', [python, '-m', 'pip', 'list', '--format=json', '--disable-pip-version-check'],
                           offline, json_result=True)
        report['packages'] = packages
        report['success'] = True
    except Exception as exc:
        report['failure'] = {'type': type(exc).__name__, 'message': str(exc)[:1500]}
    finally:
        publish()
    print('Acceptance ' + ('passed' if report['success'] else 'failed') + '; see report.json', flush=True)
    return 0 if report['success'] else 1


def suite(args):
    sys.path.insert(0, str(args.root / 'zoom-meeting-agent'))
    tests = args.root / 'zoom-meeting-agent/tests'
    patterns = ['test_setup_environment.py', 'test_doctor.py', 'test_configuration.py',
                'test_skill_entry.py', 'test_setup_sdk.py', 'test_mcp_server.py', 'test_browser_capture.py', 'test_stop_signals.py']
    results = []
    for pattern in patterns:
        # MCP's subprocess stderr needs fileno(); StringIO is not a terminal/file.
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                result = unittest.TextTestRunner(stream=log, verbosity=2).run(unittest.defaultTestLoader.discover(str(tests), pattern=pattern))
            log.flush()
            log.seek(0)
            output = log.read()
        results.append({'suite': pattern, 'tests_run': result.testsRun,
                        'passed': result.wasSuccessful(), 'skipped': [(test.id(), reason) for test, reason in result.skipped],
                        'log': output[-16000:]})
    mandatory = next(item for item in results if item['suite'] == 'test_browser_capture.py')
    ok = all(item['passed'] and item['tests_run'] for item in results) and not mandatory['skipped']
    if os.name == 'posix':
        mcp = next(item for item in results if item['suite'] == 'test_mcp_server.py')
        ok = ok and not mcp['skipped']
    save(args.output, {'success': ok, 'results': results})
    return 0 if ok else 1


def speech(args):
    sys.path.insert(0, str(args.root / 'zoom-meeting-agent'))
    import stt
    fixture_path = args.root / 'tools/acceptance_portable/speech.json'
    if fixture_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Speech fixture exceeds size limit')
    fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
    audio = base64.b64decode(fixture['flac_base64'], validate=True)
    if hashlib.sha256(audio).hexdigest() != fixture['flac_sha256']:
        raise ValueError('Speech fixture checksum mismatch')
    args.session.mkdir(parents=True, exist_ok=False)
    chunks = args.session / 'chunks'
    chunks.mkdir()
    flac = args.session / 'synthetic.flac'
    flac.write_bytes(audio)
    ffmpeg = shutil.which('ffmpeg')
    subprocess.run([ffmpeg, '-v', 'error', '-i', str(flac), '-c:a', 'libopus',
                    '-ar', '16000', str(chunks / '1700000020000.webm')], check=True, capture_output=True, timeout=30)
    began = time.monotonic()
    code = stt.run_session(args.session, once=True)
    transcript = (args.session / 'transcript.md').read_text(encoding='utf-8') if (args.session / 'transcript.md').exists() else ''
    words = set(re.findall(r'[а-яё]+', transcript.lower()))
    found = {word: word in words for word in fixture['expected_keywords']}
    health = json.loads((args.session / 'stt-health.json').read_text(encoding='utf-8'))
    from faster_whisper.utils import download_model
    model = Path(download_model('small', local_files_only=True, use_auth_token=False))
    result = {'success': code == 0 and bool(transcript.strip()) and
                       health['processed_count'] == 1 and health['error_count'] == 0 and health['pending_count'] == 0,
              'exit_code': code, 'elapsed_seconds': round(time.monotonic() - began, 3),
              'fixture_sha256': fixture['flac_sha256'], 'reference_text': fixture['text'],
              'keywords': found,
              'keyword_check': {'passed': all(found.values()), 'matched': sum(found.values()),
                                'total': len(found), 'gates_installation': False},
              'recognition_quality_accepted': False, 'transcript': transcript, 'health': health,
              'model_snapshot': model.name, 'model_files_sha256': {p.name: digest(p) for p in model.iterdir() if p.is_file()},
              'scope': 'One synthetic Russian phrase through real ffmpeg and Whisper small; not a quality benchmark or Zoom'}
    save(args.output, result)
    return 0 if result['success'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('pack')
    build.add_argument('--output', type=Path, required=True)
    build.add_argument('--revision', required=True)
    run = commands.add_parser('run')
    run.add_argument('--package', type=Path, required=True)
    run.add_argument('--work', type=Path, required=True)
    run.add_argument('--report', type=Path, required=True)
    run.add_argument('--revision', required=True)
    tests = commands.add_parser('suite')
    tests.add_argument('--root', type=Path, required=True)
    tests.add_argument('--output', type=Path, required=True)
    audio = commands.add_parser('speech')
    audio.add_argument('--root', type=Path, required=True)
    audio.add_argument('--session', type=Path, required=True)
    audio.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    return {'pack': pack, 'run': run_acceptance, 'suite': suite, 'speech': speech}[args.command](args)


if __name__ == '__main__':
    raise SystemExit(main())
