#!/usr/bin/env python3
"""Вход в Zoom аккаунтом бота и сохранение сессии для webjoin.py (встречи «только для
залогиненных»). Открывает обычное окно браузера: войдите сами (пароль, 2FA, капча —
руками), потом нажмите Enter в терминале.

  python login.py
"""
from pathlib import Path
import os
import json
import tempfile

from playwright.sync_api import sync_playwright

from env import APP_HOME

OUT = APP_HOME / "storage_state.json"
with sync_playwright() as p:
    br = p.chromium.launch(headless=False)
    ctx = br.new_context()
    page = ctx.new_page()
    page.goto("https://zoom.us/signin")
    input("Войдите в Zoom в открывшемся окне, затем нажмите Enter здесь… ")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=OUT.parent, delete=False) as saved:
        os.chmod(saved.name, 0o600)
        json.dump(ctx.storage_state(), saved)
        saved.flush()
        os.fsync(saved.fileno())
    os.replace(saved.name, OUT)
    br.close()
print(f"сессия сохранена: {OUT} (не публикуйте этот файл — в нём вход в аккаунт)")
