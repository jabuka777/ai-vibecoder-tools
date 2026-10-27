#!/usr/bin/env python3
"""Install the Zoom skill for a local AI client, without changing its other settings."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'skills' / 'zoom-meeting-agent'
CLIENTS = {'codex': '.agents/skills', 'claude': '.claude/skills', 'cursor': '.cursor/skills'}


def no_link_ancestors(path, boundary):
    relative = path.relative_to(boundary)
    current = boundary
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Installation directories must not be symbolic links')


def expected_files(home):
    files = {}
    for path in SOURCE.rglob('*'):
        if path.is_symlink():
            raise ValueError('Skill source contains a symbolic link')
        if path.is_file() and path.suffix in {'.md', '.py', '.yaml'} and '__pycache__' not in path.parts:
            files[str(path.relative_to(SOURCE))] = path.read_bytes()
    if 'SKILL.md' not in files:
        raise ValueError('Download the complete repository, including skills/')
    files['installation.json'] = (json.dumps({'version': 1, 'repository': str(ROOT), 'home': str(home)}, indent=2) + '\n').encode()
    return files


def install(client, *, target_home, app_home, replace=False):
    destination = target_home / CLIENTS[client] / 'zoom-meeting-agent'
    no_link_ancestors(destination, target_home)
    files = expected_files(app_home)
    if destination.is_symlink():
        raise ValueError('Existing skill is a symbolic link; choose its owner-managed installation')
    if destination.exists():
        actual = {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob('*')
                  if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts}
        if actual == files and not any(p.is_symlink() for p in destination.rglob('*')):
            return {'client': client, 'status': 'unchanged', 'path': str(destination)}
        if not replace:
            raise ValueError('An existing skill differs. Use --replace to archive it before updating')
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    with tempfile.TemporaryDirectory(prefix='.zoom-install-', dir=destination.parent) as staging:
        prepared = Path(staging) / 'zoom-meeting-agent'
        prepared.mkdir()
        for name, contents in files.items():
            path = prepared / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        if destination.exists():
            backups = target_home / '.jabuka-tools-skill-backups'
            no_link_ancestors(backups, target_home)
            backups.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = backups / (client + '-' + uuid.uuid4().hex)
            destination.rename(backup)
        try:
            prepared.rename(destination)
        except OSError:
            if backup is not None and not destination.exists():
                backup.rename(destination)
            raise
    return {'client': client, 'status': 'installed', 'path': str(destination),
            'backup': str(backup) if backup else None}


def mcp_configuration(client, app_home):
    python = app_home / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    entry = {'command': str(python), 'args': [str(ROOT / 'zoom-meeting-agent' / 'mcp_server.py'),
                                           '--home', str(app_home)]}
    if client == 'codex':
        return ('[mcp_servers.zoom-meeting-agent]\ncommand = ' + json.dumps(entry['command']) +
                '\nargs = ' + json.dumps(entry['args']) + '\n')
    return json.dumps({'mcpServers': {'zoom-meeting-agent': entry}}, indent=2) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', choices=[*CLIENTS, 'all'], required=True)
    parser.add_argument('--target-home', type=Path, default=Path.home(), help='OS user home; tests may use a temporary home')
    parser.add_argument('--home', type=Path, default=ROOT / 'zoom-meeting-agent', help='Product data and venv directory')
    parser.add_argument('--replace', action='store_true', help='Archive an existing different skill and install this version')
    parser.add_argument('--print-mcp', action='store_true', help='Only print a config snippet; do not install or change config')
    args = parser.parse_args(argv)
    app_home, target_home = args.home.expanduser().resolve(), args.target_home.expanduser().resolve()
    if args.print_mcp:
        if args.client == 'all':
            parser.error('--print-mcp needs one client')
        print(mcp_configuration(args.client, app_home), end='')
        return 0
    try:
        for client in CLIENTS if args.client == 'all' else [args.client]:
            print(json.dumps(install(client, target_home=target_home, app_home=app_home, replace=args.replace)))
    except (ValueError, OSError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else 'Skill installation failed; existing settings were preserved.', file=sys.stderr)
        return 1
    print('Open the client in the downloaded repository and invoke zoom-meeting-agent. MCP is optional and configured separately.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
