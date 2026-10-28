#!/usr/bin/env python3
"""Local setup: enter meeting links and credentials in your own terminal, never in chat."""
import argparse
import getpass
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import warnings
from urllib.parse import parse_qs, urlsplit
from accounts import AccountError, validate_account

HERE = Path(__file__).resolve().parent


class ConfigurationError(ValueError):
    pass


def profile_path(home, profile_id):
    if not isinstance(profile_id, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', profile_id):
        raise ConfigurationError('Use a profile ID with letters, digits, - or _, up to 64 characters')
    folder = Path(home) / 'meetings'
    if folder.is_symlink():
        raise ConfigurationError('Meeting directory must not be a symbolic link')
    if folder.exists():
        info = folder.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ConfigurationError('Meeting directory is not a directory')
        if os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ConfigurationError('Restrict the meetings directory to its owner (mode 0700) before continuing')
    return folder / (profile_id + '.json')


def validate_profile(data):
    if not isinstance(data, dict) or set(data) - {'version', 'meeting', 'password', 'name', 'minutes', 'account'}:
        raise ConfigurationError('Invalid meeting profile')
    if type(data.get('version', 1)) is not int or data.get('version', 1) != 1:
        raise ConfigurationError('Unsupported meeting profile version')
    meeting = data.get('meeting')
    if not isinstance(meeting, str) or not re.fullmatch(r'[0-9]{9,11}', meeting):
        raise ConfigurationError('Meeting number must have 9 to 11 digits')
    password, name, minutes = data.get('password', ''), data.get('name', 'AI meeting assistant'), data.get('minutes', 180)
    if not isinstance(password, str) or len(password) > 4096 or any(c in password for c in '\r\n\x00'):
        raise ConfigurationError('Invalid meeting passcode')
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ConfigurationError('Invalid assistant name')
    if type(minutes) not in (int, float) or not math.isfinite(minutes) or not 1 <= minutes <= 480:
        raise ConfigurationError('Duration must be between 1 and 480 minutes')
    try:
        account = validate_account(data.get('account', 'default'))
    except AccountError:
        raise ConfigurationError('Choose personal, assistant, guest, or default for the Zoom account') from None
    return {'version': 1, 'meeting': meeting, 'password': password, 'name': name, 'minutes': minutes,
            'account': account}


def parse_meeting(value):
    value = value.strip()
    digits = value.replace(' ', '').replace('-', '')
    if re.fullmatch(r'[0-9]{9,11}', digits):
        return digits, ''
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
        if host != 'zoom.us' and not host.endswith('.zoom.us'):
            raise ValueError
        match = re.fullmatch(r'/(?:j|wc/join)/([0-9]{9,11})/?', parsed.path)
        if not match:
            raise ValueError
        passwords = parse_qs(parsed.query).get('pwd', [''])
        if len(passwords) != 1:
            raise ValueError
        validate_profile({'meeting': match[1], 'password': passwords[0]})
        return match[1], passwords[0]
    except ValueError:
        raise ConfigurationError('Enter a Zoom meeting number or an https://…zoom.us/j/… link') from None


def check_private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ConfigurationError('Settings must be a regular file, not a link')
    if os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ConfigurationError('Settings must belong to you and have mode 0600')


def private_write(path, text, *, replace=False):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ConfigurationError('Settings path must not be a symbolic link')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        check_private_file(path)
        if not replace:
            raise ConfigurationError('Settings already exist; choose a new ID or explicitly use --replace')
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if replace:
            os.replace(temporary, path)
        else:
            # Publish only a complete file; never overwrite a concurrent creator.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_profile(home, profile_id):
    path = profile_path(home, profile_id)
    check_private_file(path)
    if path.stat().st_size > 16384:
        raise ConfigurationError('Meeting profile exceeds size limit')
    return validate_profile(json.loads(path.read_text(encoding='utf-8')))


def secret(prompt):
    if not sys.stdin.isatty():
        raise ConfigurationError('Run this command in your local terminal to enter private values')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning:
        raise ConfigurationError('Hidden input is unavailable. Use a local terminal; no value was read') from None


def credentials(home, service, replace=False):
    keys = {'sdk': ('ZOOM_SDK_CLIENT_ID', 'ZOOM_SDK_CLIENT_SECRET'),
            'deepseek': ('DEEPSEEK_API_KEY',)}[service]
    path = Path(home) / '.env'
    text = ''
    if path.exists() or path.is_symlink():
        check_private_file(path)
        if path.stat().st_size > 65536:
            raise ConfigurationError('Settings exceed size limit')
        text = path.read_text(encoding='utf-8')
    lines = text.splitlines()
    existing = {line.split('=', 1)[0].strip() for line in lines if '=' in line and not line.lstrip().startswith('#')}
    if existing.intersection(keys) and not replace:
        raise ConfigurationError('Credentials already configured; use --replace only to change them')
    values = {}
    for key in keys:
        value = secret(key + ' (hidden): ').strip()
        if not value or any(ord(c) < 33 for c in value) or any(c in value for c in '\"\''):
            raise ConfigurationError('Invalid credential; value was not saved')
        values[key] = value
    retained = [line for line in lines if not ('=' in line and line.split('=', 1)[0].strip() in keys)]
    private_write(path, '\n'.join(retained + [k + '=' + v for k, v in values.items()]) + '\n', replace=path.exists())
    print('Credentials saved locally. Authentication has not been tested.')


def run_profile(home, profile_id, *, mode='web', backend='none', model=None, account=None):
    data = load_profile(home, profile_id)
    selected = validate_account(account if account is not None else data['account'])
    if mode != 'web' and selected not in ('default', 'guest'):
        raise ConfigurationError('Named browser logins apply to Web mode; SDK uses separate application authorization')
    os.environ['ZOOM_AGENT_HOME'] = str(Path(home).resolve())
    # Imports happen after choosing the private home. No password is put in argv.
    from importlib import import_module
    engine = import_module('webjoin' if mode == 'web' else 'bot')
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=profile_path(home, profile_id).parent,
                                     prefix='.passcode-', delete=False) as stream:
        password_file = Path(stream.name)
        os.chmod(password_file, 0o600)
        stream.write(data['password'])
    try:
        args = ['--mn', data['meeting'], '--password-file', str(password_file), '--name', data['name'],
                '--minutes', str(data['minutes']), '--llm', backend]
        if model:
            args += ['--model', model]
        if mode == 'web':
            args += ['--account', selected]
        return engine.main(args)
    finally:
        password_file.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('ZOOM_AGENT_HOME', HERE)))
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('meeting', help='Enter a link privately and save a local meeting profile')
    create.add_argument('profile_id')
    create.add_argument('--name', default='AI meeting assistant')
    create.add_argument('--minutes', type=float, default=60)
    create.add_argument('--replace', action='store_true')
    create.add_argument('--account', choices=['personal', 'assistant', 'guest', 'default'])
    for service in ('sdk', 'deepseek'):
        child = commands.add_parser(service, help='Save credentials through hidden terminal prompts')
        child.add_argument('--replace', action='store_true')
    join = commands.add_parser('run', help='Join a saved meeting; recording begins when admitted')
    join.add_argument('profile_id')
    join.add_argument('--mode', choices=['web', 'sdk'], default='web')
    join.add_argument('--llm', choices=['none', 'claude', 'codex', 'deepseek'], default='none')
    join.add_argument('--model')
    join.add_argument('--account', choices=['personal', 'assistant', 'guest', 'default'],
                      help='Explicit override for this run; the saved meeting profile is unchanged')
    args = parser.parse_args(argv)
    try:
        if args.command == 'meeting':
            path = profile_path(args.home, args.profile_id)
            if path.exists() and not args.replace:
                raise ConfigurationError('Profile already exists; choose another ID or use --replace')
            account = args.account
            if account is None:
                if not sys.stdin.isatty():
                    raise ConfigurationError('Choose --account personal, assistant, or guest in a local terminal')
                choice = input('Zoom account: 1 personal, 2 separate assistant, 3 guest: ').strip()
                account = {'1': 'personal', '2': 'assistant', '3': 'guest'}.get(choice)
                if account is None:
                    raise ConfigurationError('Choose 1, 2, or 3; nothing was saved')
            meeting, password = parse_meeting(secret('Zoom link or meeting number (hidden): '))
            if not password:
                password = secret('Passcode (hidden; Enter if none): ')
            data = validate_profile({'meeting': meeting, 'password': password, 'name': args.name,
                                     'minutes': args.minutes, 'account': account})
            private_write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n', replace=args.replace)
            print('Meeting profile saved. It has not joined or recorded anything.')
        elif args.command == 'run':
            return run_profile(args.home, args.profile_id, mode=args.mode, backend=args.llm, model=args.model,
                               account=args.account)
        else:
            credentials(args.home, args.command, args.replace)
    except (ConfigurationError, AccountError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print('Local settings could not be read or saved. Check file access and format.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
