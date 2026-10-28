#!/usr/bin/env python3
"""Zoom web listener. Local transcription by default; opt in with --llm codex|claude."""
import argparse
import base64
import json
import math
from pathlib import Path
import re
import signal
import time
from urllib.parse import urlencode

from env import APP_HOME, need, meeting_password
from runtime import CaptureGuard, Monitor, atomic_json, cooperative_stop, finish, new_session, start_stt

HERE = Path(__file__).resolve().parent
JOINED_SELECTOR = 'button.footer__leave-btn, #wc-footer button[aria-label*="Leave"], #wc-footer button[aria-label*="Выйти"]'


def prepare_preview(page, name, password, monitor):
    """Fill a visible guest form without clicking hidden/stale controls."""
    passcode = page.locator('#input-for-pwd')
    if passcode.count() and passcode.first.is_visible():
        body = page.locator('body').inner_text().lower()
        if any(text in body for text in ('wrong passcode', 'incorrect passcode',
                                        'incorrect meeting passcode', 'неверный код доступа')):
            monitor.fail('meeting_passcode_rejected')
        if not password:
            monitor.fail('meeting_passcode_required_use_local_configure')
        passcode.first.fill(password)
    for label in ('Continue without microphone and camera', 'Continue without microphone'):
        button = page.get_by_role('button', name=label, exact=True)
        if button.count() and button.first.is_visible():
            button.first.click(timeout=2000)
    field = page.locator('#input-for-name')
    if not field.count() or not field.first.is_visible():
        field = page.get_by_role('textbox', name='Your Name')
    if field.count() and field.first.is_visible():
        field.first.fill(name)
    button = page.locator('button.preview-join-button')
    if button.count() and button.first.is_visible():
        button.first.click(timeout=2000)


def classify_page(page):
    body = page.locator('body').inner_text().lower()
    if any(text in body for text in ('meeting has been ended', 'this meeting has ended', 'конференция завершена')):
        return 'ended'
    if any(text in body for text in ('sign in to join', 'sign in to zoom', 'войдите в zoom')):
        return 'login_required'
    controls = page.locator(JOINED_SELECTOR)
    if controls.count() and controls.first.is_visible():
        return 'joined'
    if any(text in body for text in ('waiting room', 'host will let you in', 'зал ожидания')):
        return 'waiting_room'
    return 'connecting'


@cooperative_stop
def main(argv=None, *, _stop):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mn', required=True)
    parser.add_argument('--pwd', help=argparse.SUPPRESS)
    parser.add_argument('--password-file', type=Path)
    parser.add_argument('--name', default='AI meeting assistant')
    parser.add_argument('--minutes', type=float, default=180)
    parser.add_argument('--llm', choices=['none', 'claude', 'codex', 'deepseek'], default='none')
    parser.add_argument('--model')
    parser.add_argument('--no-summary', action='store_true', help='alias for --llm none')
    args = parser.parse_args(argv)
    password = meeting_password(args, parser)
    if not math.isfinite(args.minutes) or args.minutes <= 0:
        parser.error('--minutes must be positive')
    if args.no_summary:
        args.llm = 'none'
    need('ffmpeg')
    session = new_session(args.mn.replace(' ', ''), 'web', args.llm, args.model)
    monitor = Monitor(session)
    print(f'Session: {session}; LLM: {args.llm}', flush=True)
    started, last_audio, audio_source = time.monotonic(), None, False

    def event(kind, data=None):
        nonlocal audio_source
        if kind == 'capture_error':
            monitor.failed = True
            monitor.problems.add('audio_upload_failed')
        if kind == 'audio_source':
            audio_source = bool((data or {}).get('active'))
        with (session / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'kind': kind, 'data': data, 'ts': int(time.time() * 1000)}) + '\n')

    def on_audio(_source, stamp, encoded):
        nonlocal last_audio
        from control import write_chunk, MAX_AUDIO_BYTES
        if not isinstance(encoded, str) or len(encoded) > MAX_AUDIO_BYTES * 4 // 3 + 4:
            raise ValueError('Audio too large')
        data = base64.b64decode(encoded, validate=True)
        write_chunk(session, str(stamp), data)
        last_audio = time.monotonic()

    try:
        from playwright.sync_api import sync_playwright
        monitor.stt = start_stt(session)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=['--autoplay-policy=no-user-gesture-required'])
            try:
                if _stop.is_set():
                    raise KeyboardInterrupt()
                state = APP_HOME / 'storage_state.json'
                context = browser.new_context(viewport={'width': 1280, 'height': 720},
                            storage_state=str(state) if state.exists() else None)
                context.expose_binding('__zbAudio', on_audio)
                context.expose_binding('__zbEvent', lambda src, kind, data: event(kind, data))
                context.add_init_script(path=str(HERE / 'static' / 'audio-hook.js'))
                page = context.new_page()
                capture = CaptureGuard(page, monitor)
                if _stop.is_set():
                    raise KeyboardInterrupt()
                page.goto('https://app.zoom.us/wc/join/' + args.mn.replace(' ', '') + '?' + urlencode({'pwd': password}))
                capture.check()
                joined = False
                while not _stop.is_set() and time.monotonic() - started < args.minutes * 60:
                    capture.check()
                    state = classify_page(page)
                    if state == 'ended':
                        event('left', {'reason': 'ended'})
                        if not joined:
                            monitor.fail('meeting_ended_before_join')
                        break
                    if state == 'login_required':
                        monitor.fail('login_required_run_login_py')
                    if state == 'joined' and not joined:
                        joined = True
                        event('joined')
                    if not joined:
                        prepare_preview(page, args.name, password, monitor)
                    for label in ('Join Audio by Computer', 'Join Audio'):
                        button = page.get_by_role('button', name=label, exact=True)
                        if button.count() and button.first.is_visible():
                            button.first.click(timeout=2000)
                    monitor.tick(state == 'joined', last_audio, audio_source)
                    page.wait_for_timeout(1000)
            finally:
                try:
                    if 'capture' in locals():
                        capture.flush()
                except Exception:
                    monitor.problems.add('capture_flush_failed')
                    monitor.failed = True
                browser.close()
    except KeyboardInterrupt:
        monitor.problems.add('stopped_by_user')
    except Exception as exc:
        monitor.failed = True
        monitor.problems.add(type(exc).__name__)
        monitor.write('failed', type(exc).__name__)
    finally:
        if monitor.joined_at is None:
            monitor.failed = True
            monitor.problems.add('join_not_verified')
    if _stop.is_set():
        monitor.problems.add('stopped_by_user')
    return finish(session, monitor, args.llm, args.model)


if __name__ == '__main__':
    raise SystemExit(main())
