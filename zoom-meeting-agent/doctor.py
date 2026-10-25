#!/usr/bin/env python3
"""Check dependencies locally; does not join Zoom or call a cloud model."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['none', 'claude', 'codex'], default='none')
    args = parser.parse_args(argv)
    checks = {'python_3_10': sys.version_info >= (3, 10), 'ffmpeg': bool(shutil.which('ffmpeg'))}
    for module in ('numpy', 'faster_whisper', 'playwright'):
        checks[module] = importlib.util.find_spec(module) is not None
    if checks['playwright']:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            checks['chromium_installed'] = Path(playwright.chromium.executable_path).exists()
    if args.backend != 'none':
        checks[args.backend] = bool(shutil.which(args.backend))
    if args.backend == 'codex' and checks['codex']:
        from llm import require_codex_version, ModelError
        try:
            require_codex_version(shutil.which('codex'))
            checks['codex_supported_version'] = True
        except ModelError:
            checks['codex_supported_version'] = False
    print(json.dumps({'ok': all(checks.values()), 'checks': checks,
                      'cloud_login_checked': False, 'zoom_join_checked': False}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
