#!/usr/bin/env python3
"""Separate Codex edition, using the audited shared meeting engine."""
import argparse
import os
from pathlib import Path
import runpy
import sys

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / 'zoom-meeting-agent'


def option_values(arguments, option):
    for index, argument in enumerate(arguments):
        if argument.startswith(option + '='):
            yield argument.split('=', 1)[1]
        elif argument == option and index + 1 < len(arguments):
            yield arguments[index + 1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['web', 'sdk', 'summarize', 'login', 'setup-sdk', 'doctor', 'evaluate'])
    args, rest = parser.parse_known_args()
    if not (CORE / 'llm.py').is_file():
        parser.error('Download the whole repository or release ZIP; the shared engine is required')
    os.environ['ZOOM_AGENT_HOME'] = str(HERE)
    sys.path.insert(0, str(CORE))
    if args.command == 'summarize':
        if any(arg == '--backend' or arg.startswith('--backend=') for arg in rest):
            parser.error('Codex edition uses --backend codex')
        script = CORE / 'agent.py'
        rest += ['--summary', '--backend', 'codex']
    elif args.command == 'evaluate':
        script = CORE / 'tools' / 'evaluate_minutes.py'
        backends = list(option_values(rest, '--backend'))
        if any(backend != 'codex' for backend in backends):
            parser.error('Codex edition uses --backend codex')
        if '--offline' not in rest and not backends:
            rest += ['--backend', 'codex']
    else:
        script = CORE / {'web': 'webjoin.py', 'sdk': 'bot.py', 'login': 'login.py',
                         'setup-sdk': 'setup_sdk.py', 'doctor': 'doctor.py'}[args.command]
        if args.command in ('web', 'sdk'):
            if any(backend not in ('codex', 'none') for backend in option_values(rest, '--llm')):
                parser.error('Use --llm codex or --llm none in the Codex edition')
    sys.argv = [str(script), *rest]
    runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    main()
