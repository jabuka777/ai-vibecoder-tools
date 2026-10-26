#!/usr/bin/env python3
"""Local speech recognition with per-chunk isolation and observable health.

  python stt.py sessions/<session>
  python stt.py sessions/<session> --once

Stop the recorder and the previous worker before using --once: it drains every
remaining completed .webm file, including files younger than one second. Writers
must publish chunks by atomic rename. A failed chunk is retained in quarantine;
its error log contains metadata only. Exit codes: 0 clean stop/drain, 1 incomplete
transcription (quarantined chunks), 2 startup or worker failure.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid


def load(path, ffmpeg):
    """Decode through system ffmpeg; import numerical dependencies only at runtime."""
    import numpy as np

    raw = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
         "-ar", "16000", "-"], capture_output=True, check=True, timeout=120,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def make_model():
    from faster_whisper import WhisperModel

    return WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=3)


class Health:
    """Publish atomic snapshots, even while the model is loading or decoding."""

    def __init__(self, session, error_count=0, processed_count=0, interval=2):
        self.path = session / "stt-health.json"
        self.interval = interval
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.failure = None
        self.thread = None
        now = time.time()
        self.data = {
            "ts": now, "state": "starting", "state_since": now,
            "error_count": error_count, "last_processed": None,
            "last_chunk": None, "error_type": None, "pid": os.getpid(),
            "processed_count": processed_count, "pending_count": 0,
        }

    def _write(self):
        self.data["ts"] = time.time()
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def update(self, **values):
        with self.lock:
            if "state" in values and values["state"] != self.data["state"]:
                values["state_since"] = time.time()
            self.data.update(values)
            self._write()

    def start(self):
        self.update()
        self.thread = threading.Thread(target=self._beat, daemon=True)
        self.thread.start()

    def _beat(self):
        while not self.stop.wait(self.interval):
            try:
                self.update()
            except Exception as exc:
                self.failure = exc
                self.stop.set()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()


def quarantine(session, chunk, exc):
    """Retain failed input; never put recognition output or exception text in logs."""
    folder = session / "chunks" / "quarantine"
    folder.mkdir(exist_ok=True)
    size = chunk.stat().st_size
    destination = folder / chunk.name
    if destination.exists():
        destination = folder / f"{chunk.stem}-{uuid.uuid4().hex}.webm"
    os.replace(chunk, destination)
    record = {
        "ts": time.time(), "chunk": chunk.name, "quarantined_as": destination.name,
        "bytes": size, "error_type": type(exc).__name__,
    }
    with (session / "stt-errors.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def atomic_text(path, text):
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                     prefix='.stt-', delete=False) as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, path)


def recover_transcript(session, seen):
    """Committed per-chunk records are authoritative across interrupted writes."""
    folder = session / '.stt-records'
    folder.mkdir(mode=0o700, exist_ok=True)
    base = session / '.stt-base.md'
    transcript = session / 'transcript.md'
    if not base.exists():
        atomic_text(base, transcript.read_text(encoding='utf-8') if transcript.exists() else '')
    records = [json.loads(path.read_text(encoding='utf-8')) for path in folder.glob('*.json')]
    records.sort(key=lambda record: int(Path(record['chunk']).stem))
    for record in records:
        seen.add(record['chunk'])
    text = base.read_text(encoding='utf-8')
    text += ''.join(f"[{record['stamp']}] {record['text']}\n" for record in records if record['text'])
    if text or transcript.exists():
        atomic_text(transcript, text)
    atomic_text(session / '.stt_done', ''.join(name + '\n' for name in sorted(seen)))


def run_session(session, *, once=False, model_factory=None, decoder=None,
                stop_event=None, poll_interval=2, heartbeat_interval=2):
    """Run one session; injectable decoding/model boundaries keep tests offline."""
    session = Path(session)
    stop_event = stop_event if stop_event is not None else threading.Event()
    health = None
    try:
        chunks = session / "chunks"
        if not chunks.is_dir():
            raise FileNotFoundError("Session chunks directory is missing")
        done = session / ".stt_done"
        seen = set(done.read_text(encoding="utf-8").split()) if done.exists() else set()
        # Quarantined files remain evidence of an incomplete transcript across
        # worker restarts and the final --once drain. Do not silently reset this.
        quarantine_dir = chunks / "quarantine"
        errors = len(list(quarantine_dir.glob("*.webm"))) if quarantine_dir.exists() else 0
        health = Health(session, errors, len(seen), heartbeat_interval)
        # Preserve progress when a stopped worker is replaced by the final drain.
        previous_health = session / "stt-health.json"
        if previous_health.exists():
            try:
                previous = json.loads(previous_health.read_text(encoding="utf-8"))
                last_processed = previous.get("last_processed")
                if isinstance(last_processed, (int, float)):
                    health.data["last_processed"] = last_processed
            except (OSError, ValueError, TypeError):
                pass
        health.start()
        recover_transcript(session, seen)
        health.update(processed_count=len(seen),
                      last_processed=(health.data['last_processed'] or time.time()) if seen else None)
        if decoder is None:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise FileNotFoundError("ffmpeg is unavailable")
            decoder = lambda path: load(path, ffmpeg)
        model = (model_factory or make_model)()
        while True:
            if health.failure:
                raise RuntimeError("Cannot publish STT heartbeat") from health.failure
            health.update(state="degraded" if errors else "idle")
            for chunk in sorted(chunks.glob("*.webm"), key=lambda path: int(path.stem) if path.stem.isdigit() else -1):
                if stop_event.is_set():
                    break
                if chunk.name in seen:
                    continue
                if not once and time.time() - chunk.stat().st_mtime < 1:
                    continue
                health.update(state="processing", last_chunk=chunk.name)
                try:
                    # Validate the timestamp before emitting any transcript text.
                    stamp = dt.datetime.fromtimestamp(int(chunk.stem) / 1000 - 20).strftime("%H:%M:%S")
                    segments, _ = model.transcribe(
                        decoder(chunk), language="ru", vad_filter=True, beam_size=1,
                    )
                    text = " ".join(segment.text.strip() for segment in segments).strip()
                except Exception as exc:
                    quarantine(session, chunk, exc)
                    errors += 1
                    health.update(state="degraded", error_count=errors,
                                  error_type=type(exc).__name__)
                    print(f"[stt] quarantined {chunk.name}: {type(exc).__name__}", file=sys.stderr, flush=True)
                    continue
                # Persistence failures are fatal, not decoding failures: preserve
                # the source chunk for recovery and let the parent see failed.
                # Commit the recognized chunk before materializing derived text
                # and .stt_done. A crash between these writes is replayable.
                atomic_text(session / '.stt-records' / (chunk.name + '.json'),
                            json.dumps({'chunk': chunk.name, 'stamp': stamp, 'text': text}, ensure_ascii=False))
                recover_transcript(session, seen)
                health.update(last_processed=time.time(), processed_count=len(seen),
                              state="degraded" if errors else "idle")
            if once or stop_event.wait(poll_interval):
                break
        if health.failure:
            raise RuntimeError("Cannot publish STT heartbeat") from health.failure
        pending = sum(chunk.name not in seen for chunk in chunks.glob("*.webm"))
        health.update(state="stopped", error_count=errors, pending_count=pending)
        return 1 if errors or (once and pending) else 0
    except Exception as exc:
        # No exception text: decoder/model errors can contain input or local data.
        print(f"[stt] worker failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        if health:
            try:
                health.update(state="failed", error_type=type(exc).__name__)
            except Exception:
                pass
        return 2
    finally:
        if health:
            health.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--once", action="store_true", help="drain completed chunks and exit")
    args = parser.parse_args(argv)
    stop_event = threading.Event()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda *_: stop_event.set())
    try:
        return run_session(args.session, once=args.once, stop_event=stop_event)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
