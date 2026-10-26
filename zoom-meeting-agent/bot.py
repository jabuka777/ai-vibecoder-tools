#!/usr/bin/env python3
"""Zoom SDK listener: session-bound controls, local STT, optional cloud LLM."""
import argparse
import math
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

from control import State, create_server
from env import load_env, need, meeting_password
from runtime import CaptureGuard, Monitor, finish, new_session, start_stt

HERE = Path(__file__).resolve().parent


def bind_control(context, state):
    """Attach credentials to the session origin only, including static requests."""
    context.route(f'http://127.0.0.1:{state.port}/**', lambda route:
                  route.continue_(headers={**route.request.headers, 'Authorization': 'Bearer ' + state.token}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mn', required=True)
    parser.add_argument('--pwd', help=argparse.SUPPRESS)
    parser.add_argument('--password-file', type=Path)
    parser.add_argument('--name', default=None)
    parser.add_argument('--port', type=int, default=0, help='0: allocate a separate free port')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--no-stt', action='store_true')
    parser.add_argument('--no-agent', action='store_true', help='alias for --llm none')
    parser.add_argument('--llm', choices=['none', 'claude', 'codex'], default='none')
    parser.add_argument('--model')
    parser.add_argument('--mic-file', type=Path)
    parser.add_argument('--minutes', type=float, default=0)
    parser.add_argument('--max-replies', type=int, default=100)
    parser.add_argument('--replies-per-minute', type=int, default=6)
    args = parser.parse_args(argv)
    password = meeting_password(args, parser)
    if not math.isfinite(args.minutes) or args.minutes < 0 or not 0 <= args.port <= 65535:
        parser.error('Invalid minutes or port')
    if args.max_replies < 1 or not 1 <= args.replies_per_minute <= 60:
        parser.error('Invalid reply limits')
    if args.no_agent:
        args.llm = 'none'
    if args.no_stt and args.llm != 'none':
        parser.error('--no-stt requires --llm none')
    settings = load_env()
    if not all(settings.get(key) for key in ('ZOOM_SDK_CLIENT_ID', 'ZOOM_SDK_CLIENT_SECRET')):
        parser.error('Configure SDK credentials in .env; see REGISTRATION.md')
    if not args.no_stt:
        need('ffmpeg')
    if args.mic_file and not args.mic_file.is_file():
        parser.error('--mic-file does not exist')
    meeting = args.mn.replace(' ', '')
    session = new_session(meeting, 'sdk', args.llm, args.model)
    state = State(session, settings, meeting)
    server = create_server(state, args.port)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monitor = Monitor(session, capture_required=not args.no_stt)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f'Session: {session}; LLM: {args.llm}', flush=True)
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    try:
        from playwright.sync_api import sync_playwright
        if not args.no_stt:
            monitor.stt = start_stt(session)
        if args.llm != 'none':
            command = [sys.executable, str(HERE / 'agent.py'), str(session), '--backend', args.llm,
                       '--name', args.name or settings.get('AGENT_NAME', 'AI-агент'),
                       '--max-replies', str(args.max_replies), '--replies-per-minute', str(args.replies_per_minute)]
            if args.model:
                command += ['--model', args.model]
            monitor.agent = subprocess.Popen(command)
        with sync_playwright() as playwright:
            flags = ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
                     '--autoplay-policy=no-user-gesture-required']
            if args.mic_file:
                flags.append(f'--use-file-for-fake-audio-capture={args.mic_file.resolve()}')
            browser = playwright.chromium.launch(headless=not args.headed, args=flags)
            try:
                context = browser.new_context(permissions=['microphone', 'camera'])
                bind_control(context, state)
                context.add_init_script(path=str(HERE / 'static' / 'audio-hook.js'))
                page = context.new_page()
                capture = CaptureGuard(page, monitor)
                try:
                    page.goto(f'http://127.0.0.1:{state.port}/?' + urlencode({'mn': meeting, 'pwd': password,
                              'name': args.name or settings.get('AGENT_NAME', 'AI-агент'),
                              'unmute': int(bool(args.mic_file))}))
                    capture.check()
                    while not state.left.is_set() and not (deadline and time.monotonic() >= deadline):
                        capture.check()
                        if state.capture_failed:
                            monitor.failed = True
                            monitor.problems.add('capture_error')
                        monitor.tick(state.joined, state.last_audio, state.audio_source)
                        page.wait_for_timeout(1000)
                finally:
                    try:
                        capture.flush()
                    except Exception:
                        monitor.failed = True
                        monitor.problems.add('capture_flush_failed')
            finally:
                browser.close()
    except KeyboardInterrupt:
        monitor.problems.add('stopped_by_user')
    except Exception as exc:
        monitor.failed = True
        monitor.problems.add(type(exc).__name__)
        monitor.write('failed', type(exc).__name__)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        if monitor.joined_at is None:
            monitor.failed = True
            monitor.problems.add('join_not_verified')
    return finish(session, monitor, args.llm, args.model)


if __name__ == '__main__':
    raise SystemExit(main())
