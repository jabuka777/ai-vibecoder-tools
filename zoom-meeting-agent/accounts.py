"""Select private Zoom browser logins without falling back to another account."""
import json
import os
from pathlib import Path
import stat
import uuid

ACCOUNTS = ('personal', 'assistant', 'guest', 'default')
# Browser storage can exceed the small metadata limit (for example Zoom localStorage).
MAX_STATE = 4 * 1024 * 1024


class AccountError(ValueError):
    pass


def validate_account(value):
    if not isinstance(value, str) or value not in ACCOUNTS:
        raise AccountError('Choose personal, assistant, guest, or default')
    return value


def state_parts(account):
    validate_account(account)
    if account == 'guest':
        return None
    return ('storage_state.json',) if account == 'default' else ('accounts', account, 'storage_state.json')


def private_directory(path, *, create=False):
    if path.is_symlink():
        raise AccountError('Account directory must not be a symbolic link')
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise AccountError('Invalid account directory')
        if os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise AccountError('Restrict account directories to their owner (mode 0700)')


def state_path(home, account):
    parts = state_parts(account)
    if parts is None:
        return None
    home = Path(home)
    for index in range(1, len(parts)):
        private_directory(home.joinpath(*parts[:index]))
    path = home.joinpath(*parts)
    if path.exists() or path.is_symlink():
        from configure import check_private_file
        try:
            check_private_file(path)
        except (OSError, ValueError):
            raise AccountError('Browser login must be an owner-only regular file (mode 0600)') from None
        if path.stat().st_size > MAX_STATE:
            raise AccountError('Browser login exceeds the supported size limit')
    return path


def select_state(home, account):
    path = state_path(home, account)
    if path is None:
        return None
    if path.exists():
        return path
    if account == 'default':
        return None  # Legacy guest behavior is explicitly announced by the caller.
    raise AccountError('account_login_required: run login.py --account ' + account)


def validate_state(data):
    if (not isinstance(data, dict) or not isinstance(data.get('cookies', []), list)
            or not isinstance(data.get('origins', []), list)):
        raise AccountError('Invalid browser state format')
    return data


def read_state_file(path):
    from configure import check_private_file
    path = Path(path)
    try:
        check_private_file(path)
        if path.stat().st_size > MAX_STATE:
            raise ValueError()
        return validate_state(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        raise AccountError('Cannot import browser login; check private file permissions and format') from None


def save_state(home, account, data, *, replace=False):
    from configure import private_write
    path = state_path(home, account)
    if path is None:
        raise AccountError('Guest mode does not store a login')
    raw = json.dumps(validate_state(data), ensure_ascii=False)
    if len(raw.encode('utf-8')) > MAX_STATE:
        raise AccountError('Browser login exceeds the supported size limit')
    home = Path(home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    parts = state_parts(account)
    for index in range(1, len(parts)):
        private_directory(home.joinpath(*parts[:index]), create=True)
    if path.exists():
        if not replace:
            raise AccountError('Login already exists; use --replace to renew this account only')
        backup_dir = path.parent / 'login-backups'
        private_directory(backup_dir, create=True)
        private_write(backup_dir / (uuid.uuid4().hex + '.json'), path.read_text(encoding='utf-8'))
    private_write(path, raw, replace=replace)
    return path


def list_accounts(home):
    result = []
    for account in ('personal', 'assistant', 'default'):
        row = {'account': account, 'saved_login_present': False, 'login_verified': False}
        try:
            row['saved_login_present'] = state_path(home, account).exists()
        except (AccountError, OSError):
            row['error'] = 'invalid_private_login'
        result.append(row)
    return result
