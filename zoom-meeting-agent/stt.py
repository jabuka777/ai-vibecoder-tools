#!/usr/bin/env python3
"""Расшифровка кусков звука встречи: следит за <сессия>/chunks/*.webm, гонит через
faster-whisper (small, CPU, int8), дописывает transcript.md. Запускает bot.py сам.

  python stt.py sessions/<сессия>   (запускается из bot.py / webjoin.py сам)
"""
import datetime as dt
import sys
import time
from pathlib import Path

import subprocess

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env import need  # noqa: E402

FFMPEG = need("ffmpeg")
from faster_whisper import WhisperModel


def load(path):
    # системный ffmpeg вместо av из колеса: у av бывает несовместимая сборка
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                          "-ar", "16000", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0

sess = Path(sys.argv[1])
chunks = sess / "chunks"
out = sess / "transcript.md"
done = sess / ".stt_done"
model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=3)
seen = set(done.read_text(encoding="utf-8").split()) if done.exists() else set()

while True:
    for f in sorted(chunks.glob("*.webm")):
        if f.name in seen or time.time() - f.stat().st_mtime < 1:
            continue
        segs, _ = model.transcribe(load(f), language="ru", vad_filter=True, beam_size=1)
        text = " ".join(s.text.strip() for s in segs).strip()
        if text:
            t = dt.datetime.fromtimestamp(int(f.stem) / 1000 - 20).strftime("%H:%M:%S")
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(f"[{t}] {text}\n")
            print(f"[{t}] {text}", flush=True)
        seen.add(f.name)
        with open(done, "a", encoding="utf-8") as fh:
            fh.write(f.name + "\n")
    time.sleep(2)
