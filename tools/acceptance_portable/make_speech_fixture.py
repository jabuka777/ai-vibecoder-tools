#!/usr/bin/env python3
"""Generate the public synthetic fixture using eSpeak NG, never a neural voice.

Needs ffmpeg and a locally installed eSpeak NG shared library/data directory.
Example: python make_speech_fixture.py --library /path/libespeak-ng.so
         --data-parent /usr/share --output speech.json
The library may also be the eSpeak bridge distributed by Piper; no Piper voice
model is loaded. Only the generated FLAC and its provenance are distributed.
"""
import argparse
import base64
import ctypes
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave

TEXT = 'Проверка записи. Программа работает. Это тестовая запись. Проверка завершена.'
# Declared before the first Whisper pilot; this is a smoke check, not WER scoring.
KEYWORDS = ['проверка', 'работает', 'запись']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--data-parent', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    engine = ctypes.CDLL(str(args.library.resolve()))
    engine.espeak_Info.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
    engine.espeak_Info.restype = ctypes.c_char_p
    version = engine.espeak_Info(None).decode('ascii')
    engine.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    engine.espeak_Initialize.restype = ctypes.c_int
    rate = engine.espeak_Initialize(2, 0, str(args.data_parent.resolve()).encode(), 0)
    if rate <= 0:
        raise RuntimeError('eSpeak NG could not initialize')
    blocks = []
    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)

    @callback_type
    def collect(samples, count, events):
        if count:
            blocks.append(ctypes.string_at(samples, count * 2))
        return 0

    engine.espeak_SetSynthCallback.argtypes = [callback_type]
    engine.espeak_SetSynthCallback(collect)
    engine.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
    if engine.espeak_SetVoiceByName(b'ru'):
        raise RuntimeError('eSpeak NG Russian voice is unavailable')
    engine.espeak_SetParameter.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    if engine.espeak_SetParameter(1, 145, 0):
        raise RuntimeError('eSpeak NG rate could not be set')
    engine.espeak_Synth.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint,
        ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
    encoded = ctypes.create_string_buffer(TEXT.encode('utf-8') + b'\0')
    if engine.espeak_Synth(encoded, len(encoded), 0, 1, 0, 1, None, None):
        raise RuntimeError('eSpeak NG synthesis failed')
    if engine.espeak_Synchronize():
        raise RuntimeError('eSpeak NG synthesis did not complete')
    pcm = b''.join(blocks)
    if not pcm or len(pcm) > rate * 2 * 30:
        raise RuntimeError('Generated speech is empty or exceeds 30 seconds')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required')
    with tempfile.TemporaryDirectory() as directory:
        wav, flac = Path(directory) / 'speech.wav', Path(directory) / 'speech.flac'
        with wave.open(str(wav), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(pcm)
        subprocess.run([ffmpeg, '-v', 'error', '-i', str(wav), '-ar', '16000',
                        '-compression_level', '8', str(flac)], check=True, capture_output=True)
        audio = flac.read_bytes()
    result = {'schema': 1, 'synthetic': True, 'language': 'ru', 'text': TEXT,
              'expected_keywords': KEYWORDS, 'duration_seconds': len(pcm) / rate / 2,
              'codec': 'FLAC', 'sample_rate': 16000, 'channels': 1,
              'fixture_license': 'CC0-1.0',
              'provenance': {'engine': 'eSpeak NG', 'engine_version': version,
                  'engine_license': 'GPL-3.0-or-later', 'engine_source': 'https://github.com/espeak-ng/espeak-ng',
                  'voice': 'ru, rule-based synthesis; no neural voice or human recording',
                  'voice_rate_words_per_minute': 145,
                  'generator': 'tools/acceptance_portable/make_speech_fixture.py',
                  'command_template': ['python', 'make_speech_fixture.py', '--library', '<espeak-ng shared library>',
                                       '--data-parent', '<directory containing espeak-ng-data>', '--output', 'speech.json'],
                  'note': 'Original test text and generated fixture dedicated to CC0. Engine is a separate GPL program; no engine binary or voice model is included.'},
              'flac_sha256': hashlib.sha256(audio).hexdigest(),
              'flac_base64': base64.b64encode(audio).decode('ascii')}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('duration_seconds', 'flac_sha256', 'expected_keywords')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
