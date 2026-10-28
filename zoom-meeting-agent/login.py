#!/usr/bin/env python3
"""Save a personal or assistant Zoom login locally; passwords/MFA stay in the browser."""
import argparse
from pathlib import Path
import json
import sys
from accounts import AccountError, list_accounts, read_state_file, save_state, state_path
from env import APP_HOME


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', choices=['personal', 'assistant', 'default'], default='default')
    parser.add_argument('--list', action='store_true', help='List saved profile IDs without cookie values')
    parser.add_argument('--replace', action='store_true', help='Renew this login with a private backup')
    parser.add_argument('--import-state', type=Path, help='Explicitly import an existing local browser login')
    args = parser.parse_args(argv)
    try:
        if args.list:
            print(json.dumps({'accounts': list_accounts(APP_HOME)}, ensure_ascii=False, indent=2))
            return 0
        out = state_path(APP_HOME, args.account)
        if out.exists() and not args.replace:
            raise AccountError('Login already exists; use --replace to renew this account only')
        if args.import_state is not None:
            save_state(APP_HOME, args.account, read_state_file(args.import_state), replace=args.replace)
        else:
            if not sys.stdin.isatty():
                raise AccountError('Open a local interactive terminal for browser login')
            from playwright.sync_api import sync_playwright
            print('Selected Zoom account profile: ' + args.account)
            print('Sign in to the intended account in the browser; passwords, MFA and CAPTCHA stay there.')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=False)
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    page.goto('https://zoom.us/signin')
                    input('After checking the account in Zoom, press Enter here: ')
                    save_state(APP_HOME, args.account, context.storage_state(), replace=args.replace)
                finally:
                    browser.close()
        print('Browser login saved for ' + args.account + '. Validity and meeting entry have not been verified.')
        return 0
    except AccountError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print('Login did not complete (' + type(exc).__name__ + '). Check browser, private file access and doctor.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
