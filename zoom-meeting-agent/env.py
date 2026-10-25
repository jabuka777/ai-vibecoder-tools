"""Настройки: переменные окружения поверх файла .env рядом со скриптами."""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env() -> dict:
    out = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    out.update({k: v for k, v in os.environ.items() if k.startswith(("ZOOM_", "AGENT_", "TELEGRAM_"))})
    return out


# --- кроссплатформенные помощники (Windows / macOS / Linux) ---
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SESSIONS = HERE / "sessions"


def venv_python() -> str:
    """Python из .venv рядом (Windows: Scripts\\python.exe, остальные: bin/python), иначе текущий."""
    for p in (HERE / ".venv" / "Scripts" / "python.exe", HERE / ".venv" / "bin" / "python"):
        if p.exists():
            return str(p)
    return sys.executable


def set_current(sess: Path):
    """Запомнить текущую сессию файлом, а не symlink: на Windows symlink требует прав."""
    SESSIONS.mkdir(exist_ok=True)
    (SESSIONS / "current.txt").write_text(str(sess), encoding="utf-8")


def current() -> Path:
    return Path((SESSIONS / "current.txt").read_text(encoding="utf-8").strip())


def tmpdir() -> str:
    return tempfile.gettempdir()


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        sys.exit(f"Не найден «{tool}» в PATH — установите его (см. README, раздел «Установка»).")
    return path
