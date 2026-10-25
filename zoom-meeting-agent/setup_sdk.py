#!/usr/bin/env python3
"""Скачивает Zoom Meeting SDK Web (npm-пакет @zoom/meetingsdk) и кладёт его в static/.
Работает на Windows, macOS и Linux; нужен Node.js (npm в PATH). Около 160 МБ, в git не хранится.

  python setup_sdk.py            # версия по умолчанию
  python setup_sdk.py 6.10.0     # конкретная версия
"""
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

VER = sys.argv[1] if len(sys.argv) > 1 else "6.10.0"
HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

npm = shutil.which("npm")
if not npm:
    sys.exit("Нужен Node.js (npm в PATH): https://nodejs.org")
with tempfile.TemporaryDirectory() as tmp:
    subprocess.run([npm, "pack", f"@zoom/meetingsdk@{VER}", "-q"], cwd=tmp, check=True,
                   stdout=subprocess.DEVNULL)
    tgz = next(Path(tmp).glob("zoom-meetingsdk-*.tgz"))
    with tarfile.open(tgz) as t:
        t.extractall(tmp)
    dist = Path(tmp) / "package" / "dist"
    shutil.rmtree(STATIC / "lib", ignore_errors=True)
    shutil.copytree(dist / "lib", STATIC / "lib")
    for old in STATIC.glob("zoom-meeting-*.min.js"):
        old.unlink()
    shutil.copy(dist / f"zoom-meeting-{VER}.min.js", STATIC)
idx = STATIC / "index.html"
idx.write_text(re.sub(r"zoom-meeting-[0-9.]+\.min\.js", f"zoom-meeting-{VER}.min.js",
                      idx.read_text(encoding="utf-8")), encoding="utf-8")
print(f"Zoom Meeting SDK {VER} → {STATIC}")
