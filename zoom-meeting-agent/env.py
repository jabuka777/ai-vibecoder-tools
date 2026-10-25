"""Настройки: переменные окружения поверх файла .env рядом со скриптами."""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env() -> dict:
    out = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    out.update({k: v for k, v in os.environ.items() if k.startswith(("ZOOM_", "AGENT_", "TELEGRAM_"))})
    return out
