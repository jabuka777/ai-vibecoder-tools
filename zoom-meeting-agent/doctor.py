#!/usr/bin/env python3
"""Check local prerequisites without connecting to Zoom or a cloud model."""
import argparse
import ctypes
import importlib.util
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from env import APP_HOME, load_env
from runtime import run_process

HERE = Path(__file__).resolve().parent
GIB = 1024 ** 3
OFFLINE_ENV = dict(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                   HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1', DO_NOT_TRACK='1')


def run_probe(code, timeout=30):
    """Fixed local code only; stderr, exceptions and subprocess output stay private."""
    try:
        result = run_process([sys.executable, '-c', code], capture_output=True,
                             timeout=timeout, env=dict(os.environ, **OFFLINE_ENV))
        if result.returncode or len(result.stdout) > 32768:
            return {'ok': False, 'reason': 'local_probe_failed'}
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or not isinstance(value.get('ok'), bool):
            raise ValueError('Invalid local probe')
        return value
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return {'ok': False, 'reason': 'local_probe_failed'}


def module_present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def available_memory():
    """Available physical memory, not the installed RAM size."""
    try:
        if sys.platform.startswith('linux'):
            for line in Path('/proc/meminfo').read_text().splitlines():
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
        elif sys.platform == 'win32':
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in
                    ('total', 'available', 'total_page', 'available_page',
                     'total_virtual', 'available_virtual', 'extended')]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available)
        elif sys.platform == 'darwin':
            result = run_process(['vm_stat'], capture_output=True, text=True, timeout=5)
            if not result.returncode:
                import re
                page = re.search(r'page size of (\d+) bytes', result.stdout)
                counts = re.findall(r'Pages (?:free|inactive|speculative):\s+(\d+)', result.stdout)
                if page and counts:
                    return int(page.group(1)) * sum(map(int, counts))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, AttributeError):
        pass
    return None


def free_disk(home):
    path = Path(home)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def system_help(system=None):
    return {'Darwin': 'Установите ffmpeg через Homebrew: brew install ffmpeg.',
            'Windows': 'Установите ffmpeg: winget install Gyan.FFmpeg; затем откройте новый терминал.',
            'Linux': 'Установите ffmpeg пакетным менеджером ОС; Ubuntu/Debian: sudo apt install ffmpeg.'
            }.get(system or platform.system(), 'Установите ffmpeg для своей ОС и добавьте его в PATH.')


def ffmpeg_probe(binary):
    try:
        return run_process([binary, '-version'], timeout=10, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL).returncode == 0
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False


def sdk_present():
    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.sources = []

        def handle_starttag(self, tag, attrs):
            if tag == 'script':
                source = dict(attrs).get('src')
                if source:
                    self.sources.append(source)
    try:
        parser = Scripts()
        parser.feed((HERE / 'static' / 'index.html').read_text(encoding='utf-8'))
        sources = parser.sources
        if not any(source.startswith('/static/zoom-meeting-') for source in sources):
            return False
        if not sources or any(not source.startswith('/static/') or '..' in source.split('/') for source in sources):
            return False
        files = [HERE / source.lstrip('/') for source in sources]
        return (all(path.is_file() and path.stat().st_size > 0 for path in files) and
                any(path.is_file() and path.stat().st_size > 0
                    for path in (HERE / 'static' / 'lib' / 'av').rglob('*.wasm')))
    except (OSError, UnicodeError, ValueError):
        return False


def chromium_probe(launch=False, headed=False):
    code = '''import json
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    installed = Path(p.chromium.executable_path).is_file()
    result = {"ok": installed, "installed": installed, "launched": False}
'''
    if launch:
        code += '''    if installed:
        try:
            browser = p.chromium.launch(headless=HEADLESS, args=["--disable-background-networking"])
            try:
                context = browser.new_context(service_workers="block")
                context.route("**/*", lambda route: route.abort())
                context.new_page().goto("about:blank")
                result["launched"] = True
            finally:
                browser.close()
        except Exception:
            result["ok"] = False
            result["reason"] = "browser_launch_failed"
'''.replace('HEADLESS', str(not headed))
    return run_probe(code + 'print(json.dumps(result))\n', timeout=45)


def whisper_probe(load=False):
    code = '''import json
from pathlib import Path
from faster_whisper.utils import download_model
try:
    model = Path(download_model("small", local_files_only=True, use_auth_token=False))
    cached = all((model / name).is_file() and (model / name).stat().st_size > 0 for name in ("model.bin", "config.json", "tokenizer.json"))
except Exception:
    cached = False
result = {"ok": cached, "cached": cached, "loaded": False}
'''
    if load:
        code += '''if cached:
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(str(model), device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True)
        result["loaded"] = True
    except Exception:
        result["ok"] = False
        result["reason"] = "model_load_failed"
'''
    return run_probe(code + 'print(json.dumps(result))\n', timeout=120 if load else 30)


def collect_checks(backend='none', mode='web', headed=False,
                   check_browser=False, check_model=False, home=None):
    home = Path(home or APP_HOME).resolve()
    details = {}

    def add(name, ok, message, fix='', required=True, status=None):
        details[name] = dict(ok=bool(ok), status=status or ('ok' if ok else 'missing'),
                             required=required, message=message, fix=fix)

    add('python_3_10', sys.version_info >= (3, 10), 'Нужен Python 3.10 или новее.', 'Установите Python 3.10+ и повторите настройку.')
    add('supported_os', platform.system() in ('Linux', 'Darwin', 'Windows'), 'Linux, macOS или Windows; это не подтверждение испытаний на каждой ОС.')
    ffmpeg = shutil.which('ffmpeg')
    add('ffmpeg', bool(ffmpeg), 'ffmpeg нужен для декодирования аудио.', system_help())
    add('ffmpeg_runtime', bool(ffmpeg) and ffmpeg_probe(ffmpeg), 'Локальная проверка запуска ffmpeg; декодирование речи здесь не выполняется.', system_help())
    for module in ('numpy', 'faster_whisper', 'playwright'):
        add(module, module_present(module), f'Python-пакет {module}.', 'Запустите setup_environment.py --install и используйте Python из .venv.')
    browser = chromium_probe(check_browser, headed) if details['playwright']['ok'] else {}
    add('chromium_installed', browser.get('installed', False), 'Наличие Chromium выбранной версии Playwright.', 'Запустите setup_environment.py --install; при ошибке запуска проверьте системные библиотеки браузера.')
    if check_browser:
        add('chromium_runtime', browser.get('launched', False), 'Запуск локальной пустой страницы, без подключения к Zoom.', 'Для Linux проверьте системные библиотеки Playwright; для окна нужен рабочий графический сеанс.')
    model = whisper_probe(check_model) if details['faster_whisper']['ok'] else {}
    add('whisper_model_cached', model.get('cached', False), 'Наличие модели small в локальном кеше.', 'Для явной загрузки: python setup_environment.py --download-model. Doctor ничего не скачивает.')
    if check_model:
        add('whisper_runtime', model.get('loaded', False), 'Локальная загрузка Whisper small, CPU int8; качество речи не проверяется.', 'Проверьте память и CPU. Для явной повторной загрузки файлов: setup_environment.py --download-model --repair-model.')
    memory = available_memory()
    if memory is None:
        add('available_memory', False, 'Доступную память определить не удалось.', 'Проверьте память средствами ОС; ориентир 4 ГиБ.', required=False, status='not_checked')
    else:
        enough = memory >= 768 * 1024 ** 2
        add('available_memory', enough, f'Доступно {memory / GIB:.2f} ГиБ памяти; ориентир 4 ГиБ, остановка ниже 768 МиБ.', 'Закройте тяжёлые приложения.', status='ok' if memory >= 4 * GIB else 'warning' if enough else 'missing')
    disk = free_disk(home)
    add('disk_space', disk is not None and disk >= 256 * 1024 ** 2,
        'Свободное место неизвестно.' if disk is None else f'Свободно {disk / GIB:.2f} ГиБ; предварительный минимум 256 МиБ.',
        'Освободите место или выберите --home на другом диске.', status='not_checked' if disk is None else 'ok' if disk >= 2 * GIB else 'warning' if disk >= 256 * 1024 ** 2 else 'missing')
    display = platform.system() != 'Linux' or bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))
    add('gui_session', display, 'Графический сеанс нужен для login.py и запуска с окном; наличие переменной ещё не проверяет окно.',
        'Для проверки окна: --headed --check-browser на компьютере с графическим сеансом.', required=headed, status='ok' if display else 'missing' if headed else 'not_required')
    sdk = sdk_present()
    add('sdk_assets', sdk, 'Файлы скриптов из index.html и локальные WASM; SDK нужен только для режима SDK.', 'Установите Node.js/npm, выполните setup_sdk.py и настройте приложение по REGISTRATION.md.', required=mode == 'sdk', status='ok' if sdk else 'missing' if mode == 'sdk' else 'not_required')
    if mode == 'sdk':
        try:
            config = load_env(home=home)
            credentials = all(config.get(key, '').strip() for key in ('ZOOM_SDK_CLIENT_ID', 'ZOOM_SDK_CLIENT_SECRET'))
        except (OSError, UnicodeError):
            credentials = False
        add('sdk_credentials_configured', credentials, 'Только наличие ID и секрета приложения; значения не выводятся, вход не проверяется.', 'Сохраните SDK-настройки через configure.py по REGISTRATION.md.')
    if backend in ('claude', 'codex'):
        binary = shutil.which(backend)
        add(backend, bool(binary), f'Наличие {backend} CLI; вход и лимиты не проверяются.', f'Установите {backend} CLI и выполните вход.')
        if backend == 'codex' and binary:
            from llm import require_codex_version, ModelError
            try:
                require_codex_version(binary)
                supported = True
            except (ModelError, OSError, subprocess.SubprocessError):
                supported = False
            add('codex_supported_version', supported, 'Версия Codex должна соответствовать проверенной границе инструментов.', 'Используйте версию Codex, указанную в README.')
    elif backend == 'deepseek':
        try:
            configured = bool(load_env(home=home).get('DEEPSEEK_API_KEY', '').strip())
        except (OSError, UnicodeError):
            configured = False
        add('deepseek_key_configured', configured, 'Только наличие DEEPSEEK_API_KEY; ключ не выводится, запросов нет.', 'Сохраните ключ через configure.py; действительность ключа и баланс не проверены.')
    return dict(ok=all(item['ok'] for item in details.values() if item['required']),
                checks={name: item['ok'] for name, item in details.items()}, details=details,
                backend=backend, mode=mode, browser_runtime_checked=check_browser,
                model_runtime_checked=check_model, cloud_login_checked=False, zoom_join_checked=False,
                stt_accuracy_checked=False, end_to_end_checked=False, sdk_authentication_checked=False,
                notes=['Это проверка локальных условий, а не успешная запись Zoom.',
                       'Пороги памяти и диска — предварительные ограничения, не гарантия производительности.'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['none', 'claude', 'codex', 'deepseek'], default='none')
    parser.add_argument('--mode', choices=['web', 'sdk'], default='web')
    parser.add_argument('--home', type=Path, default=APP_HOME)
    parser.add_argument('--headed', action='store_true', help='require a graphical session')
    parser.add_argument('--check-browser', action='store_true', help='launch a local blank page; no Zoom')
    parser.add_argument('--check-model', action='store_true', help='load cached Whisper offline; no download')
    parser.add_argument('--text', action='store_true', help='human-readable output instead of JSON')
    args = parser.parse_args(argv)
    result = collect_checks(**{key: value for key, value in vars(args).items() if key != 'text'})
    if args.text:
        for name, check in result['details'].items():
            print(f"[{check['status']}] {name}: {check['message']}")
            if check['status'] not in ('ok', 'not_required') and check['fix']:
                print('  ' + check['fix'])
        print('\n' + '\n'.join(result['notes']))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
