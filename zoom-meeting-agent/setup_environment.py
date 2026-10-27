#!/usr/bin/env python3
"""Plan a local setup; installation and downloads require explicit flags.

No arguments: read-only plan. --install: isolated .venv, Python packages, Chromium.
--install-mcp: optional MCP packages. --download-model: explicit Whisper download.
No operating-system packages, accounts, config files or cloud models are changed.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid

from env import APP_HOME
from doctor import GIB, OFFLINE_ENV, available_memory, free_disk, system_help
from runtime import run_process

HERE = Path(__file__).resolve().parent
STATE_NAME = '.setup-state.json'
LOCK_NAME = '.setup.lock'
REQUIREMENTS = HERE / 'requirements.txt'
MCP_REQUIREMENTS = HERE / 'requirements-mcp.txt'


class SetupError(Exception):
    def __init__(self, code, remedy, step=None):
        super().__init__(code)
        self.code, self.remedy, self.step = code, remedy, step


def interpreter(home, system=None):
    folder = Path(home) / '.venv'
    return folder / ('Scripts/python.exe' if (system or platform.system()) == 'Windows' else 'bin/python')


def mcp_supported():
    return (os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and
            os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd)


def read_state(home):
    path = Path(home) / STATE_NAME
    if path.is_symlink():
        raise SetupError('unsafe_state_file', 'Файл состояния является ссылкой; выберите отдельный каталог --home.')
    if not path.exists():
        return {'version': 1, 'steps': {}}
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('oversized state')
        state = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('version') != 1 or
                not isinstance(state.get('steps'), dict) or
                any(not isinstance(item, dict) or not isinstance(item.get('status'), str)
                    for item in state['steps'].values())):
            raise ValueError('invalid state')
        return state
    except (OSError, ValueError, UnicodeError):
        raise SetupError('state_unreadable', 'Не удалось прочитать состояние; сохраните его копию и выберите новый --home.') from None


def write_state(home, state):
    path = Path(home) / STATE_NAME
    if path.is_symlink():
        raise SetupError('unsafe_state_file', 'Файл состояния является ссылкой; выберите отдельный --home.')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    data = json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8')
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def setup_lock(home):
    """OS releases this advisory lock after a crash, so a retry can resume."""
    path = Path(home) / LOCK_NAME
    if path.is_symlink():
        raise SetupError('unsafe_lock_file', 'Файл блокировки является ссылкой; выберите отдельный --home.')
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags, 0o600)
    stream = os.fdopen(descriptor, 'r+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if not path.stat().st_size:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        stream.close()
        raise SetupError('setup_in_progress', 'Другой установщик работает в этом --home. Дождитесь его завершения.') from None
    try:
        yield
    finally:
        stream.close()


def command(argv, *, timeout=1200, json_output=False, offline=False):
    environment = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1',
                       HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1', DO_NOT_TRACK='1')
    if offline:
        environment.update(OFFLINE_ENV)
    try:
        result = run_process([str(item) for item in argv], timeout=timeout, env=environment,
                             stdout=subprocess.PIPE if json_output else subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        if result.returncode:
            raise SetupError('command_failed', 'Локальная команда завершилась с ошибкой. Проверьте доступ к пакетам, место и Python; повторите ту же команду установки.')
        if json_output:
            if len(result.stdout) > 1024 * 1024:
                raise ValueError('oversized output')
            return json.loads(result.stdout)
        return None
    except SetupError:
        raise
    except subprocess.TimeoutExpired:
        raise SetupError('command_timeout', 'Истекло время шага. Его процессы остановлены; проверьте сеть и повторите ту же команду.') from None
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise SetupError('command_unavailable', 'Не удалось выполнить шаг. Проверьте Python, права на каталог и доступ к зависимостям.') from None


def inventory(python):
    value = command([python, '-m', 'pip', 'list', '--format=json', '--disable-pip-version-check'],
                    timeout=30, json_output=True, offline=True)
    if not isinstance(value, list) or any(not isinstance(item, dict) or
            not isinstance(item.get('name'), str) or not isinstance(item.get('version'), str) for item in value):
        raise SetupError('invalid_package_inventory', 'Проверьте pip в .venv и повторите установку.')
    return {item['name'].lower().replace('_', '-'): item['version'] for item in value}


def dependencies_valid(python):
    try:
        command([python, '-m', 'pip', 'check'], timeout=30, offline=True)
        command([python, '-c', 'import numpy, faster_whisper, playwright.sync_api'], timeout=45, offline=True)
        return True
    except SetupError:
        return False


def pip_available(python):
    try:
        command([python, '-m', 'pip', '--version'], timeout=30, offline=True)
        return True
    except SetupError:
        return False


def receipt_matches(receipt, actual):
    """Additional packages are harmless; removed or changed recorded ones are not."""
    expected = receipt.get('packages')
    return (isinstance(expected, dict) and bool(expected) and
            all(actual.get(name) == version for name, version in expected.items()))


def browser_present(python):
    try:
        return command([python, '-c', 'import json; from pathlib import Path; from playwright.sync_api import sync_playwright\nwith sync_playwright() as p: print(json.dumps(Path(p.chromium.executable_path).is_file()))'],
                       timeout=30, json_output=True, offline=True) is True
    except SetupError:
        return False


def model_present(python):
    code = '''import json
from pathlib import Path
from faster_whisper.utils import download_model
try:
    p = Path(download_model("small", local_files_only=True, use_auth_token=False))
    found = all((p / name).is_file() and (p / name).stat().st_size > 0 for name in ("model.bin", "config.json", "tokenizer.json"))
except Exception:
    found = False
print(json.dumps(found))
'''
    try:
        return command([python, '-c', code], timeout=45, json_output=True, offline=True) is True
    except SetupError:
        return False


def model_usable(python):
    try:
        command([python, '-c', 'from faster_whisper import WhisperModel; WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True)'],
                timeout=120, offline=True)
        return True
    except SetupError:
        return False


def plan(home, system=None):
    home = Path(home).resolve()
    system = system or platform.system()
    python = interpreter(home, system)
    system_commands = {
        'Linux': [['sudo', 'apt', 'install', 'python3-venv', 'ffmpeg'],
                  [str(python), '-m', 'playwright', 'install-deps', 'chromium']],
        'Darwin': [['brew', 'install', 'python', 'ffmpeg']],
        'Windows': [['winget', 'install', 'Python.Python.3.12'], ['winget', 'install', 'Gyan.FFmpeg']],
    }.get(system, [])
    memory, disk = available_memory(), free_disk(home)
    return dict(mode='plan', read_only=True, platform=system, home=str(home),
                python_compatible=sys.version_info >= (3, 10),
                available_memory_bytes=memory, free_disk_bytes=disk,
                venv_python=str(python), venv_exists=python.is_file(),
                mcp_filesystem_supported=system != 'Windows' and mcp_supported(),
                system_dependencies=dict(automatic=False, explanation=system_help(system),
                                         commands_for_manual_review=system_commands),
                steps=[
                    dict(name='venv', permission='--install', action='Создать или проверить .venv в выбранном --home; неизвестное незавершённое окружение не затирается.'),
                    dict(name='dependencies', permission='--install', action='Установить Python-зависимости из requirements.txt в .venv.'),
                    dict(name='chromium', permission='--install', action='Загрузить браузер выбранной версии Playwright; системные библиотеки не устанавливаются.'),
                    dict(name='mcp', permission='--install-mcp', action='Установить необязательные зависимости MCP из requirements-mcp.txt.'),
                    dict(name='model', permission='--download-model', action='Загрузить Whisper small в локальный кеш Hugging Face; записи не передаются.')],
                next_commands=[['python', 'setup_environment.py', '--home', str(home), '--install'],
                               ['python', 'setup_environment.py', '--home', str(home), '--download-model'],
                               [str(python), str(HERE / 'doctor.py'), '--home', str(home), '--check-browser', '--check-model']],
                notes=['Без флагов установки файлы и окружение не меняются.',
                       'Для новых зависимостей и модели оставьте хотя бы 2 ГиБ свободного места; записи требуют дополнительного места.',
                       'Браузер и модель используют кеши текущего пользователя; --home не переносит эти кеши.',
                       'Аккаунты, .env, встречи, cookie и системные пакеты этот установщик не меняет.',
                       'Успешная установка не означает успешный вход в Zoom или качество распознавания.'])


def install(home, *, install_core=False, install_mcp=False, download_model=False, repair_model=False):
    home = Path(home).resolve()
    if sys.version_info < (3, 10):
        raise SetupError('python_too_old', 'Установите Python 3.10+ и запустите команду новым интерпретатором.')
    if platform.system() not in ('Linux', 'Darwin', 'Windows'):
        raise SetupError('unsupported_platform', 'Используйте Linux, macOS или Windows.')
    if install_mcp and not mcp_supported():
        raise SetupError('mcp_platform_unsupported', 'Защищённый MCP-сервер пока требует POSIX/Linux; Windows не поддерживается. Для CLI повторите без --install-mcp.')
    if repair_model and not download_model:
        raise SetupError('repair_requires_download', 'Для повторной загрузки модели явно укажите --download-model --repair-model.')
    memory = available_memory()
    if memory is not None and memory < 768 * 1024 ** 2:
        raise SetupError('available_memory_low', 'Доступно менее 768 МиБ памяти. Закройте тяжёлые приложения перед установкой; ориентир для работы — 4 ГиБ.')
    if not install_core and not interpreter(home).is_file():
        raise SetupError('venv_missing', 'Сначала выполните python setup_environment.py --install с тем же --home.')
    disk = free_disk(home)
    if disk is None or disk < 256 * 1024 ** 2:
        raise SetupError('disk_space_low', 'До установки освободите место; для новой установки рекомендуется минимум 2 ГиБ.')
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (home / '.venv').is_symlink():
        raise SetupError('unsafe_venv', '.venv является ссылкой; выберите отдельный --home, чтобы не менять чужое окружение.')
    with setup_lock(home):
        state = read_state(home)
        python = interpreter(home)
        performed, skipped = [], []

        def step(name, work, receipt=None):
            state['steps'][name] = dict(status='running', started_at=dt.datetime.now(dt.timezone.utc).isoformat())
            write_state(home, state)
            try:
                work()
            except (SetupError, KeyboardInterrupt) as exc:
                state['steps'][name] = dict(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                                            reason='interrupted' if isinstance(exc, KeyboardInterrupt) else exc.code)
                write_state(home, state)
                if isinstance(exc, SetupError):
                    exc.step = name
                raise
            state['steps'][name] = dict(status='complete', **(receipt or {}))
            write_state(home, state)
            performed.append(name)

        if not python.is_file():
            if (home / '.venv').exists() and not state['steps'].get('venv'):
                raise SetupError('unmanaged_incomplete_venv', 'Существующее .venv не содержит Python и не создано этим установщиком. Выберите новый --home.')
            step('venv', lambda: command([sys.executable, '-m', 'venv', home / '.venv'], timeout=180))
        else:
            if not (home / '.venv' / 'pyvenv.cfg').is_file():
                raise SetupError('invalid_venv', 'У .venv нет pyvenv.cfg. Выберите отдельный --home.')
            command([python, '-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) and sys.prefix != sys.base_prefix else 1)'], timeout=15, offline=True)
            skipped.append('venv')
        if not pip_available(python):
            if not state['steps'].get('venv'):
                raise SetupError('unmanaged_venv_without_pip', 'У существующего .venv нет рабочего pip. Выберите новый --home; чужое окружение автоматически не перестраивается.')

            def repair_pip():
                command([python, '-m', 'ensurepip', '--upgrade'], timeout=180, offline=True)
                if not pip_available(python):
                    raise SetupError('pip_bootstrap_failed', 'Python не смог восстановить pip. Для Debian/Ubuntu проверьте пакет python3-venv; затем повторите установку.')
            step('venv', repair_pip)
            if 'venv' in skipped:
                skipped.remove('venv')

        def packages(name, requirements):
            if not requirements.is_file():
                raise SetupError('requirements_missing', 'В копии продукта нет нужного requirements-файла. Скачайте полный выпуск.', name)
            digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
            previous = state['steps'].get(name, {})
            try:
                same = (previous.get('status') == 'complete' and previous.get('sha256') == digest and
                        receipt_matches(previous, inventory(python)) and dependencies_valid(python))
                if same and name == 'mcp':
                    command([python, '-c', 'from mcp.server import MCPServer'], timeout=30, offline=True)
            except SetupError:
                same = False
            if same:
                skipped.append(name)
                return

            receipt = {'sha256': digest}

            def apply():
                command([python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', requirements])
                command([python, '-m', 'pip', 'check'], timeout=30, offline=True)
                if name == 'dependencies' and not dependencies_valid(python):
                    raise SetupError('dependency_import_failed', 'Пакеты установлены, но не загружаются. Проверьте архитектуру CPU и поддерживаемый Python.')
                if name == 'mcp':
                    command([python, '-c', 'from mcp.server import MCPServer'], timeout=30, offline=True)
                receipt['packages'] = inventory(python)
            step(name, apply, receipt)

        if install_core:
            packages('dependencies', REQUIREMENTS)
            if browser_present(python):
                skipped.append('chromium')
            else:
                def browser():
                    command([python, '-m', 'playwright', 'install', 'chromium'])
                    if not browser_present(python):
                        raise SetupError('chromium_missing_after_install', 'Chromium не найден после загрузки. Проверьте место и права на кеш браузера.')
                step('chromium', browser)
        if install_mcp:
            packages('mcp', MCP_REQUIREMENTS)
        if download_model:
            if not dependencies_valid(python):
                raise SetupError('dependencies_missing', 'Сначала выполните setup_environment.py --install с тем же --home.', 'model')
            if model_present(python) and not repair_model:
                if not model_usable(python):
                    raise SetupError('model_cache_unusable', 'Кеш найден, но модель не загрузилась. Проверьте память и CPU; для повторной загрузки файлов явно используйте --download-model --repair-model.', 'model')
                skipped.append('model')
            else:
                def model():
                    code = ('from huggingface_hub import snapshot_download; snapshot_download("Systran/faster-whisper-small", force_download=True, token=False, allow_patterns=["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"])'
                            if repair_model else 'from faster_whisper.utils import download_model; download_model("small", use_auth_token=False)')
                    command([python, '-c', code], timeout=1800)
                    if not model_present(python) or not model_usable(python):
                        raise SetupError('model_unusable_after_download', 'Модель после загрузки не готова. Проверьте память, CPU и место; для восстановления файлов есть явный --download-model --repair-model.')
                step('model', model)
        return dict(mode='install', requested_steps_complete=True, performed=performed, skipped=skipped,
                    venv_python=str(python), state_file=str(home / STATE_NAME),
                    cloud_called=False, zoom_join_checked=False, end_to_end_checked=False,
                    next_command=[str(python), str(HERE / 'doctor.py'), '--home', str(home), '--check-browser', '--check-model'],
                    notes=['Запрошенные шаги установки завершены; готовность проверьте командой doctor.',
                           'ffmpeg и системные библиотеки не устанавливались. Аккаунты и конфиги не менялись.'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=APP_HOME, help='private data directory; .venv is created here')
    parser.add_argument('--install', action='store_true', help='install Python requirements into .venv and Chromium into the Playwright cache')
    parser.add_argument('--install-mcp', action='store_true', help='install optional MCP dependencies; needs --install or an existing .venv')
    parser.add_argument('--download-model', action='store_true', help='explicitly download Whisper small; never uploads meeting data')
    parser.add_argument('--repair-model', action='store_true', help='with --download-model, explicitly fetch the model files again; never deletes cache')
    parser.add_argument('--json', action='store_true', help='machine-readable result')
    args = parser.parse_args(argv)
    try:
        result = install(args.home, install_core=args.install, install_mcp=args.install_mcp,
                         download_model=args.download_model, repair_model=args.repair_model) if any((args.install, args.install_mcp, args.download_model, args.repair_model)) else plan(args.home)
        code = 0
    except SetupError as exc:
        result = dict(mode='error', code=exc.code, step=exc.step, remedy=exc.remedy)
        code = 1
    except KeyboardInterrupt:
        result = dict(mode='error', code='interrupted', remedy='Повторите ту же команду: завершённые шаги будут проверены и использованы.')
        code = 130
    except (OSError, ValueError, RuntimeError, UnicodeError):
        result = dict(mode='error', code='local_files_unavailable', remedy='Проверьте права, место и формат состояния; конфиги не перезаписывались.')
        code = 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result['mode'] == 'plan':
        print(f"План установки для {result['platform']}. Ничего не изменено.")
        print(f"Окружение: {result['venv_python']}")
        for item in result['steps']:
            print(f"  {item['permission']}: {item['action']}")
        print(result['system_dependencies']['explanation'])
        print('\n'.join(result['notes']))
    elif result['mode'] == 'error':
        print(f"Не завершено: {result['code']}" + (f" ({result.get('step')})" if result.get('step') else ''))
        print(result['remedy'])
    else:
        print('Запрошенные шаги установки завершены: ' + (', '.join(result['performed']) or 'изменений не потребовалось'))
        print('Уже проверено: ' + (', '.join(result['skipped']) or '—'))
        print('\n'.join(result['notes']))
        print('Команда проверки (аргументы): ' + json.dumps(result['next_command'], ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
