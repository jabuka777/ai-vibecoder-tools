#!/usr/bin/env python3
"""Написать в чат встречи от бота: say.py "текст". Бот должен быть запущен (bot.py)."""
import json
import sys
import urllib.request
from pathlib import Path

port = (Path(__file__).resolve().parent / "sessions" / "port").read_text().strip()
req = urllib.request.Request(f"http://127.0.0.1:{port}/say", method="POST",
                             data=json.dumps({"message": " ".join(sys.argv[1:])}).encode())
urllib.request.urlopen(req, timeout=5)
print("в очереди")
