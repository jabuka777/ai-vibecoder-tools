"""Session lifecycle, process supervision and explicit recording state."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from env import SESSIONS, venv_python

HERE = Path(__file__).resolve().parent


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def new_session(meeting, mode, backend, model=None):
    if not meeting.isascii() or not meeting.isdigit() or not 9 <= len(meeting) <= 11:
        raise ValueError('Meeting number must contain 9 to 11 digits')
    SESSIONS.mkdir(parents=True, exist_ok=True, mode=0o700)
    session = SESSIONS / f'{dt.datetime.now():%Y-%m-%d_%H%M%S}-{meeting}-{mode}-{uuid.uuid4().hex[:8]}'
    session.mkdir(mode=0o700)
    (session / 'chunks').mkdir(mode=0o700)
    atomic_json(session / 'session.json', {'version': 2, 'meeting': meeting, 'mode': mode,
                'backend': backend, 'model': model, 'created_at': dt.datetime.now().isoformat()})
    return session


class Monitor:
    def __init__(self, session, stt=None, agent=None, capture_required=True):
        self.session, self.stt, self.agent = session, stt, agent
        self.capture_required = capture_required
        self.started = time.monotonic()
        self.joined_at = None
        self.last_audio = None
        self.audio_source = False
        self.audio_source_seen = False
        self.audio_received = False
        self.last_state = None
        self.problems = set()
        self.failed = False
        self.recording_complete = None
        self.summary_state = None

    def write(self, state, reason=None):
        atomic_json(self.session / 'status.json', {'state': state, 'reason': reason,
                    'ts': time.time(), 'incomplete': self.failed,
                    'recording_complete': self.recording_complete,
                    'summary_state': self.summary_state, 'problems': sorted(self.problems)})
        if (state, reason) != self.last_state:
            print(f'[{state}]' + (f' {reason}' if reason else ''), flush=True)
            self.last_state = (state, reason)

    def fail(self, reason):
        self.problems.add(reason)
        self.failed = True
        self.write('failed', reason)
        raise RuntimeError(reason)

    def tick(self, joined=False, last_audio=None, audio_source=False):
        now = time.monotonic()
        self.last_audio, self.audio_source = last_audio, audio_source
        if audio_source:
            self.audio_source_seen = True
        if last_audio is not None and audio_source:
            self.audio_received = True
        if self.capture_required and self.audio_received and not audio_source:
            # The initial grace period is for starting capture, never for losing
            # a source which has already supplied audio.
            self.fail('audio_source_lost')
        if not joined and self.joined_at is not None:
            self.fail('meeting_connection_lost')
        if joined and self.joined_at is None:
            self.joined_at = now
        for name, process in [('stt', self.stt), ('agent', self.agent)]:
            if process is not None and process.poll() is not None:
                self.fail(f'{name}_exited')
        health_path = self.session / 'stt-health.json'
        health = {}
        if self.stt:
            if health_path.exists():
                try:
                    health = json.loads(health_path.read_text(encoding='utf-8'))
                except (ValueError, OSError):
                    self.fail('stt_health_unreadable')
                if time.time() - health.get('ts', 0) > 20:
                    self.fail('stt_heartbeat_stale')
                if health.get('state') == 'failed':
                    self.fail('stt_failed')
                if health.get('state') in ('starting', 'processing') and time.time() - health.get('state_since', 0) > 300:
                    self.fail('stt_operation_timeout')
                if health.get('error_count', 0):
                    self.problems.add('audio_chunks_quarantined')
                    self.failed = True
            elif now - self.started > 20:
                self.fail('stt_heartbeat_missing')
        if self.joined_at is None:
            if now - self.started > 120:
                self.fail('join_not_verified')
            self.write('connecting')
        elif not self.capture_required:
            self.write('joined_no_transcription')
        elif last_audio is None or not audio_source:
            if now - self.joined_at > 90:
                self.fail('audio_not_received')
            self.write('joined_waiting_audio')
        elif now - last_audio > 60:
            self.fail('audio_stream_stalled')
        elif self.failed:
            self.write('degraded', ','.join(sorted(self.problems)))
        elif self.stt and not health.get('last_processed'):
            self.write('recording_stt_starting')
        else:
            self.write('recording')


class CaptureGuard:
    """Bind final flush to the same browser document that captured the meeting.

    A reload or cross-document leave destroys MediaRecorder's pending chunk.
    A fresh hook in the replacement document cannot certify that old recording.
    Navigation while still connecting is allowed; once joined or capturing,
    losing the document makes the recording incomplete.
    """
    def __init__(self, page, monitor):
        self.page = page
        self.monitor = monitor
        self.generation = None

    def check(self):
        if not self.monitor.capture_required:
            return None
        snapshot = self.page.evaluate("""() => ({
            generation: window.__zbCaptureGeneration || null,
            ready: typeof window.__zbStopCapture === 'function'
        })""")
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('generation'), str)
                or not snapshot['generation'] or snapshot.get('ready') is not True):
            self.monitor.fail('capture_hook_missing')
        generation = snapshot['generation']
        if self.generation is not None and generation != self.generation:
            if (self.monitor.joined_at is not None or self.monitor.audio_source_seen
                    or self.monitor.audio_received):
                self.monitor.fail('capture_document_changed')
        self.generation = generation
        return generation

    def flush(self):
        expected = self.check()
        if expected is None:
            return
        result = self.page.evaluate("""async expected => {
            if (window.__zbCaptureGeneration !== expected ||
                    typeof window.__zbStopCapture !== 'function')
                throw Error('Capture document changed before flush');
            await window.__zbStopCapture();
            if (window.__zbCaptureGeneration !== expected)
                throw Error('Capture document changed during flush');
            return expected;
        }""", expected)
        if result != expected:
            self.monitor.fail('capture_flush_unverified')


def start_stt(session):
    return subprocess.Popen([venv_python(), str(HERE / 'stt.py'), str(session)])


def stop_process(process, timeout=15):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def finish(session, monitor, backend, model, drain=True):
    stop_process(monitor.agent)
    stop_process(monitor.stt)
    if drain and monitor.stt:
        try:
            result = subprocess.run([venv_python(), str(HERE / 'stt.py'), str(session), '--once'], timeout=300)
            if result.returncode:
                monitor.failed = True
                monitor.problems.add('transcript_incomplete')
        except (OSError, subprocess.TimeoutExpired):
            monitor.failed = True
            monitor.problems.add('stt_drain_failed')
        # A short recording can produce its first chunk only during final flush.
        # Accept it only with an observed source and confirmed completed STT work.
        if monitor.audio_source_seen and not monitor.audio_received:
            try:
                final_health = json.loads((session / 'stt-health.json').read_text(encoding='utf-8'))
                if (final_health.get('state') == 'stopped' and final_health.get('processed_count', 0) > 0
                        and final_health.get('last_processed') and not final_health.get('error_count', 0)
                        and not final_health.get('pending_count', 0)):
                    monitor.audio_received = True
            except (OSError, ValueError, TypeError, AttributeError):
                pass
    transcript = session / 'transcript.md'
    if monitor.joined_at is None:
        monitor.failed = True
        monitor.problems.add('join_not_verified')
    if monitor.capture_required and not monitor.audio_received:
        monitor.failed = True
        monitor.problems.add('audio_not_received')
    if monitor.capture_required and monitor.audio_received and not monitor.audio_source:
        monitor.failed = True
        monitor.problems.add('audio_source_lost')
    if monitor.capture_required and (not transcript.exists() or not transcript.read_text(encoding='utf-8').strip()):
        monitor.failed = True
        monitor.problems.add('no_transcript')
    monitor.recording_complete = bool(monitor.capture_required and not monitor.failed)
    # Commit recording state before any model call. A model failure cannot make
    # a complete recording ineligible for a later, cached summary retry.
    monitor.write('incomplete' if monitor.failed else ('recorded' if monitor.capture_required else 'untranscribed'),
                  ','.join(sorted(monitor.problems)) or None)
    summary_failed = False
    if backend != 'none' and monitor.recording_complete:
        monitor.summary_state = 'processing'
        monitor.write('summarizing')
        try:
            from agent import summary
            summary_failed = not summary(session, backend=backend, model=model)
        except Exception as exc:
            print(f'[summary] failed: {type(exc).__name__}', flush=True)
            summary_failed = True
        monitor.summary_state = 'failed' if summary_failed else 'complete'
        if summary_failed:
            monitor.problems.add('summary_failed')
        else:
            monitor.problems.discard('summary_failed')
    state = ('incomplete' if monitor.failed else 'summary_failed' if summary_failed else
             'complete' if monitor.capture_required else 'untranscribed')
    monitor.write(state, ','.join(sorted(monitor.problems)) or None)
    print(f'Session: {session}', flush=True)
    return 1 if monitor.failed or summary_failed else 0
