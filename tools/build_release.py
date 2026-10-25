#!/usr/bin/env python3
"""Build a source-only archive; never include sessions, auth, caches or SDK binaries."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP = {'README.md', 'LICENSE', 'llms.txt', 'CITATION.cff', 'CHANGELOG.md'}
SOURCE_EXTENSIONS = {'.py', '.md', '.js', '.html', '.json', '.jsonl', '.txt'}


def release_paths():
    # Only tracked source files. A filled .env or recording cannot enter by glob.
    files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    for name in files:
        path = Path(name)
        if not name:
            continue
        if name in TOP:
            yield path
            continue
        if path.parts[0] not in {'zoom-meeting-agent', 'zoom-meeting-agent-codex', 'tools'}:
            continue
        if any(part in {'sessions', '.venv', '__pycache__', '.minutes-history', '.minutes-cache'} for part in path.parts):
            raise RuntimeError(f'Private data path is tracked: {name}')
        if path.name == '.env.example' or path.name == '.gitignore':
            yield path
        elif path.name in {'.env', 'storage_state.json', 'control.json'} or '.min.js' in path.name:
            raise RuntimeError(f'Private/generated file is tracked: {name}')
        elif path.suffix in SOURCE_EXTENSIONS:
            yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'zoom-meeting-agent-codex.zip')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error('Output exists; choose a new filename')
    paths = sorted(release_paths())
    with zipfile.ZipFile(args.output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            if (ROOT / path).is_symlink():
                raise RuntimeError('Source symlinks are not included')
            archive.write(ROOT / path, Path('ai-vibecoder-tools') / path)
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    args.output.with_suffix('.zip.sha256').write_text(f'{digest}  {args.output.name}\n')
    print(f'{args.output}\n{len(paths)} source files\nSHA256 {digest}')


if __name__ == '__main__':
    main()
