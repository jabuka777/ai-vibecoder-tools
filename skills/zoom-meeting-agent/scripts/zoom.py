#!/usr/bin/env python3
"""Locate the installed product and run its commands without shell interpolation."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

SKILL = Path(__file__).resolve().parents[1]


def installation():
    receipt = SKILL / 'installation.json'
    if receipt.is_file():
        config = json.loads(receipt.read_text(encoding='utf-8'))
        root = Path(config['repository'])
        home = Path(config['home'])
    else:
        root = SKILL.parents[1]
        home = root / 'zoom-meeting-agent'
    core = root / 'zoom-meeting-agent'
    if not (core / 'doctor.py').is_file() or not (core / 'configure.py').is_file():
        raise ValueError('Product files moved or are missing. Reinstall the skill from the complete repository.')
    return root.resolve(), core.resolve(), home.resolve()


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['paths', 'setup', 'doctor', 'configure', 'login', 'web',
                                           'sdk', 'summarize', 'mcp', 'evaluate', 'setup-sdk'])
    args = parser.parse_args(arguments[:1])
    rest = arguments[1:]
    try:
        root, core, home = installation()
    except (ValueError, OSError, KeyError):
        print('Skill cannot locate product files. Run tools/install_zoom_skill.py again with --replace.', file=sys.stderr)
        return 1
    python = home / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    interpreter = str(python) if python.is_file() else sys.executable
    if args.command == 'paths':
        print(json.dumps({'repository': str(root), 'core': str(core), 'home': str(home),
                          'python': interpreter, 'environment_installed': python.is_file()}, indent=2))
        return 0
    scripts = {'setup': 'setup_environment.py', 'doctor': 'doctor.py', 'configure': 'configure.py',
               'login': 'login.py', 'web': 'webjoin.py', 'sdk': 'bot.py', 'summarize': 'agent.py',
               'mcp': 'mcp_server.py', 'evaluate': 'tools/evaluate_minutes.py', 'setup-sdk': 'setup_sdk.py'}
    if args.command == 'summarize':
        rest += ['--summary']
    env = dict(os.environ, ZOOM_AGENT_HOME=str(home))
    try:
        os.chdir(core)
        if os.name == 'nt':
            # Windows execvpe does not preserve quoting for an argument with
            # spaces. Popen builds the native command line from the argument list.
            # The engine shares this console and receives Ctrl+C itself; let it
            # finish its final audio/STT drain before the wrapper returns.
            process = subprocess.Popen([interpreter, str(core / scripts[args.command]), *rest], env=env)
            while True:
                try:
                    return process.wait()
                except KeyboardInterrupt:
                    continue
        # Replace the wrapper: the engine retains the terminal and receives
        # Ctrl+C directly, so its own final flush/drain is not cut short.
        os.execvpe(interpreter, [interpreter, str(core / scripts[args.command]), *rest], env)
    except OSError:
        print('Command unavailable. Run setup and doctor before trying again.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
