#!/usr/bin/env python3
"""Install a pinned Zoom Meeting SDK, preserving the previous static assets.

  python setup_sdk.py           # 6.10.0
  python setup_sdk.py 6.10.0

This command downloads the public npm package; importing it does nothing.
Stop SDK recordings first. Old assets remain in .sdk-backups/ next to static/.
Local installation transactions are tested on Linux, not Windows/macOS.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

from runtime import run_process

DEFAULT_VERSION = '6.10.0'
HERE = Path(__file__).resolve().parent
STATIC = HERE / 'static'
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
MAX_MEMBERS = 50000
MAX_TAR_STREAM = 640 * 1024 * 1024
SDK_SCRIPT = re.compile(r'zoom-meeting-[0-9]+\.[0-9]+\.[0-9]+\.min\.js')
VENDOR_FILES = ('react.min.js', 'react-dom.min.js', 'redux.min.js', 'react-redux.min.js', 'redux-thunk.min.js', 'lodash.min.js')


class SDKSetupError(ValueError):
    pass


def validate_version(version):
    if not isinstance(version, str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}', version):
        raise SDKSetupError('Use a numeric SDK version: MAJOR.MINOR.PATCH')
    return version


def no_symlink_ancestors(path):
    path = Path(os.path.abspath(path))
    for entry in (path, *path.parents):
        if entry.is_symlink():
            raise SDKSetupError('SDK installation paths must not contain symbolic links')
    return path


@contextmanager
def install_lock(parent):
    path = parent / '.sdk-setup.lock'
    no_symlink_ancestors(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    stream = os.fdopen(fd, 'r+b')
    try:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SDKSetupError('SDK lock must be a regular private file')
        if os.name == 'nt':
            import msvcrt
            if not info.st_size:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise SDKSetupError('Another SDK installation is running; wait for it to finish') from None
    finally:
        stream.close()


def npm_command():
    npm = shutil.which('npm')
    if not npm:
        raise SDKSetupError('Install Node.js with npm, then reopen your terminal')
    if Path(npm).suffix.lower() in {'.cmd', '.bat'}:
        node = shutil.which('node')
        cli = Path(npm).parent / 'node_modules' / 'npm' / 'bin' / 'npm-cli.js'
        if not node or not cli.is_file():
            raise SDKSetupError('Cannot locate npm-cli.js; repair the Node.js installation')
        return [node, str(cli)]
    return [npm]


def download_archive(version, folder):
    """No lifecycle scripts, shell, user npm config, npm tokens or raw diagnostics."""
    allowed = {'PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP',
               'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'LANG', 'LC_ALL', 'HTTP_PROXY',
               'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    for name in ('user.npmrc', 'global.npmrc'):
        (folder / name).write_text('', encoding='utf-8')
    environment.update(npm_config_userconfig=str(folder / 'user.npmrc'),
                       npm_config_globalconfig=str(folder / 'global.npmrc'),
                       npm_config_cache=str(folder / 'npm-cache'),
                       npm_config_ignore_scripts='true', npm_config_audit='false',
                       npm_config_fund='false', npm_config_update_notifier='false')
    try:
        run_process([*npm_command(), 'pack', '@zoom/meetingsdk@' + version, '--ignore-scripts',
                     '--registry=https://registry.npmjs.org', '--pack-destination', str(folder), '--silent'],
                    cwd=folder, env=environment, check=True, timeout=600,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        raise SDKSetupError('SDK download failed or timed out; previous SDK assets were not changed') from None
    archive = folder / f'zoom-meetingsdk-{version}.tgz'
    try:
        info = archive.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_ARCHIVE:
            raise SDKSetupError('SDK archive is not a regular file within the size limit')
    except OSError:
        raise SDKSetupError('npm did not produce the expected SDK archive') from None
    return archive


class BoundedTarStream:
    """Also bound extended/PAX metadata before tarfile allocates its contents."""
    def __init__(self, stream):
        self.stream, self.remaining = stream, MAX_TAR_STREAM

    def read(self, size=-1):
        if size < 0 or size > self.remaining:
            raise SDKSetupError('SDK archive exceeds the decompressed stream limit')
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data


def unpack_archive(archive, destination):
    """Bounded manual extraction, including Python versions before tar filters."""
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    total, names = 0, set()
    try:
        with gzip.open(archive, 'rb') as uncompressed, \
                tarfile.open(fileobj=BoundedTarStream(uncompressed), mode='r|') as bundle:
            for index, member in enumerate(bundle):
                name = member.name
                parts = PurePosixPath(name).parts
                if (index >= MAX_MEMBERS or len(name) > 1024 or not parts or parts[0] != 'package'
                        or name.startswith('/') or '\\' in name or '\x00' in name
                        or any(part in ('.', '..') or ':' in part or part.rstrip(' .') != part for part in parts)
                        or any(re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I) for part in parts)
                        or name in names or not (member.isdir() or member.isreg())
                        or member.sparse is not None or any(key.startswith('GNU.sparse') for key in member.pax_headers)
                        or member.size < 0 or member.size > MAX_FILE):
                    raise SDKSetupError('SDK archive contains an unsafe or oversized member')
                names.add(name)
                total += member.size
                if total > MAX_EXPANDED:
                    raise SDKSetupError('SDK archive exceeds the expanded size limit')
                target = destination.joinpath(*parts)
                if member.isdir():
                    if member.size:
                        raise SDKSetupError('SDK archive contains an invalid directory')
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise SDKSetupError('SDK archive member has no content')
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with source, os.fdopen(fd, 'wb') as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise SDKSetupError('SDK archive is truncated')
                        output.write(chunk)
                        remaining -= len(chunk)
    except (OSError, tarfile.TarError, EOFError):
        raise SDKSetupError('SDK archive could not be safely extracted') from None
    return destination / 'package' / 'dist'


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def prepare_assets(static, dist, version, staging, archive):
    if not static.is_dir() or not (static / 'index.html').is_file():
        raise SDKSetupError('Download the complete product, including static/index.html')
    for path in static.rglob('*'):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise SDKSetupError('Existing SDK assets contain links or special files; installation stopped')
    script = dist / f'zoom-meeting-{version}.min.js'
    required = [script, dist / 'ui' / 'zoom-meetingsdk.css',
                *(dist / 'lib' / 'vendor' / name for name in VENDOR_FILES)]
    if not (dist / 'lib' / 'av').is_dir() or any(not path.is_file() or not path.stat().st_size for path in required):
        raise SDKSetupError('SDK archive lacks the assets required by this product')
    original = (static / 'index.html').read_text(encoding='utf-8')
    index, count = SDK_SCRIPT.subn(f'zoom-meeting-{version}.min.js', original)
    if count != 1:
        raise SDKSetupError('Expected one SDK script reference in index.html; previous files were preserved')
    prepared = staging / 'static'
    shutil.copytree(static, prepared,
                    ignore=lambda folder, names: [name for name in names if name in {'lib', 'ui'} or SDK_SCRIPT.fullmatch(name)]
                    if Path(folder) == static else [])
    shutil.copytree(dist / 'lib', prepared / 'lib')
    shutil.copytree(dist / 'ui', prepared / 'ui')
    shutil.copy2(script, prepared / script.name)
    (prepared / 'index.html').write_text(index, encoding='utf-8')
    (prepared / 'sdk-installation.json').write_text(json.dumps({
        'version': version, 'package': '@zoom/meetingsdk', 'archive_sha256': digest(archive),
        'script_sha256': digest(script), 'installed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'live_zoom_verified': False,
    }, indent=2) + '\n', encoding='utf-8')
    return prepared


def install_sdk(version=DEFAULT_VERSION, *, static=STATIC):
    version = validate_version(version)
    static = no_symlink_ancestors(static)
    if not static.parent.is_dir():
        raise SDKSetupError('Product directory is missing')
    with install_lock(static.parent), tempfile.TemporaryDirectory(prefix='.sdk-stage-', dir=static.parent) as folder:
        staging = Path(folder)
        archive = download_archive(version, staging)
        dist = unpack_archive(archive, staging / 'unpacked')
        prepared = prepare_assets(static, dist, version, staging, archive)
        # Keep backup beside static so both renames stay on one filesystem.
        backups = no_symlink_ancestors(static.parent / '.sdk-backups')
        backups.mkdir(mode=0o700, exist_ok=True)
        info = backups.stat()
        if (info.st_dev != static.parent.stat().st_dev or
                (os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
            raise SDKSetupError('SDK backup directory must be private and on the product filesystem')
        backup = backups / uuid.uuid4().hex
        backup.mkdir(mode=0o700)
        try:
            static.rename(backup / 'static')
            prepared.rename(static)
        except BaseException as exc:
            if (backup / 'static').exists() and not static.exists():
                (backup / 'static').rename(static)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise SDKSetupError('SDK publication failed; check the retained backup before retrying') from None
    return {'version': version, 'static': str(static), 'backup': str(backup), 'live_zoom_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', nargs='?', default=DEFAULT_VERSION)
    args = parser.parse_args(argv)
    try:
        result = install_sdk(args.version)
    except SDKSetupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError):
        print('SDK setup failed. Inspect .sdk-backups before retrying; no backup is removed.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('SDK setup interrupted. Inspect .sdk-backups before retrying; no backup is removed.', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print('Assets installed. Run a separate entry/audio smoke test on your own Zoom meeting.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
