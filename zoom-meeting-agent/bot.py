#!/usr/bin/env python3
"""Zoom-бот: входит во встречу своего Zoom-аккаунта (Meeting SDK, Web),
пишет звук кусками → stt.py расшифровывает whisper'ом, чат и события — в журнал,
умеет писать в чат встречи (say.py).

  bot.py --mn <номер> --pwd <пароль> [--name "AI-агент"] [--port 8791] [--headed]

Встреча должна принадлежать аккаунту, в котором создано SDK-приложение: во встречи
чужих аккаунтов Zoom с 02.03.2026 пускает SDK-ботов только по OBF-токену. Для чужих
встреч — webjoin.py (веб-клиент).

Сессия: sessions/<дата>-<mn>/ — events.jsonl, chunks/*.webm, transcript.md.
Ключи — .env рядом (ZOOM_SDK_CLIENT_ID / ZOOM_SDK_CLIENT_SECRET), см. .env.example.
"""
import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
sys.path.insert(0, str(HERE))
from env import load_env  # noqa: E402


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sdk_signature(client_id: str, secret: str, mn: str, role: int = 0) -> str:
    """JWT для Meeting SDK (HS256): https://developers.zoom.us/docs/meeting-sdk/auth/"""
    iat = int(time.time()) - 30
    exp = iat + 2 * 3600
    head = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64url(json.dumps({"appKey": client_id, "sdkKey": client_id, "mn": str(mn),
                              "role": role, "iat": iat, "exp": exp, "tokenExp": exp}).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64url(sig)}"


class State:
    def __init__(self, sess: Path, env: dict):
        self.sess = sess
        self.env = env
        self.outbox: list[str] = []
        self.lock = threading.Lock()
        self.left = threading.Event()
        (sess / "chunks").mkdir(parents=True, exist_ok=True)

    def event(self, rec: dict):
        rec.setdefault("ts", int(time.time() * 1000))
        with open(self.sess / "events.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        kind = rec.get("kind")
        if kind in ("joined", "join_error", "init_error", "chat", "chat_sent", "chat_error", "left"):
            print(f"[{kind}] {json.dumps(rec.get('data'), ensure_ascii=False)[:300]}", flush=True)


def make_handler(st: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/signature":
                mn = parse_qs(u.query)["mn"][0]
                sig = sdk_signature(st.env["ZOOM_SDK_CLIENT_ID"], st.env["ZOOM_SDK_CLIENT_SECRET"], mn)
                return self._send(200, json.dumps({"signature": sig}).encode())
            if u.path == "/outbox":
                with st.lock:
                    msgs, st.outbox = st.outbox, []
                return self._send(200, json.dumps(msgs, ensure_ascii=False).encode())
            if u.path == "/left":
                st.event({"kind": "left"})
                st.left.set()
                return self._send(200, b"left", "text/plain")
            if u.path in ("/", "/index.html"):
                u = u._replace(path="/static/index.html")
            if u.path.startswith("/static/"):
                p = (STATIC / u.path[len("/static/"):]).resolve()
                if STATIC in p.parents and p.is_file():
                    ctype = {".html": "text/html", ".js": "application/javascript", ".css": "text/css",
                             ".json": "application/json", ".wasm": "application/wasm"}.get(p.suffix, "application/octet-stream")
                    return self._send(200, p.read_bytes(), ctype)
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n)
            if u.path == "/audio":
                ts = parse_qs(u.query).get("ts", [str(int(time.time() * 1000))])[0]
                (st.sess / "chunks" / f"{ts}.webm").write_bytes(data)
                return self._send(200, b"{}")
            if u.path == "/event":
                try:
                    st.event(json.loads(data))
                except Exception:
                    pass
                return self._send(200, b"{}")
            if u.path == "/say":
                msg = json.loads(data)["message"]
                with st.lock:
                    st.outbox.append(msg)
                return self._send(200, b"{}")
            self._send(404, b"not found", "text/plain")

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mn", required=True, help="номер встречи, без пробелов")
    ap.add_argument("--pwd", default="", help="пароль встречи (pwd из ссылки тоже подходит)")
    ap.add_argument("--name", default=None, help="имя в встрече (по умолчанию AGENT_NAME или «AI-агент»)")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--no-stt", action="store_true")
    ap.add_argument("--no-agent", action="store_true", help="не отвечать в чате и не слать выжимку")
    ap.add_argument("--mic-file", help="wav, который бот «говорит» в микрофон (для теста), включает микрофон")
    ap.add_argument("--minutes", type=float, default=0, help="выйти через N минут (0 — до конца встречи)")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    deadline = time.time() + a.minutes * 60 if a.minutes else None
    mn = a.mn.replace(" ", "")
    a.name = a.name or load_env().get("AGENT_NAME", "AI-агент")

    sess = HERE / "sessions" / f"{dt.datetime.now():%Y-%m-%d_%H%M%S}-{mn}-{a.port}"
    st = State(sess, load_env())
    (HERE / "sessions" / "current").unlink(missing_ok=True)
    (HERE / "sessions" / "current").symlink_to(sess.name)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (HERE / "sessions" / "port").write_text(str(a.port))
    print(f"сессия: {sess}", flush=True)

    stt = None
    if not a.no_stt:
        stt = subprocess.Popen([str(HERE / ".venv/bin/python"), str(HERE / "stt.py"), str(sess)])
    agent = None
    if not a.no_agent:
        agent = subprocess.Popen([sys.executable, str(HERE / "agent.py"), str(sess)])

    from playwright.sync_api import sync_playwright
    from urllib.parse import urlencode
    with sync_playwright() as p:
        br = p.chromium.launch(headless=not a.headed, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            "--autoplay-policy=no-user-gesture-required"]
            + ([f"--use-file-for-fake-audio-capture={Path(a.mic_file).resolve()}"] if a.mic_file else []))
        ctx = br.new_context(permissions=["microphone", "camera"])
        ctx.add_init_script(path=str(STATIC / "audio-hook.js"))
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and st.event({"kind": "console", "data": m.text[:500]}))
        url = f"http://127.0.0.1:{a.port}/?" + urlencode({"mn": mn, "pwd": a.pwd, "name": a.name, "unmute": int(bool(a.mic_file))})
        page.goto(url)
        try:
            n = 0
            while not st.left.is_set() and not (deadline and time.time() > deadline):
                page.wait_for_timeout(5000)
                n += 1
                if n % 6 == 1:
                    page.screenshot(path=str(sess / "last.png"))
        except KeyboardInterrupt:
            pass
        finally:
            try:
                page.screenshot(path=str(sess / "last.png"))
            except Exception:
                pass
            br.close()
    if stt:
        time.sleep(25)  # дать последнему куску дописаться
        stt.terminate()
    if agent:
        agent.terminate()
        if not a.no_stt and (sess / "transcript.md").exists():
            subprocess.run([sys.executable, str(HERE / "agent.py"), str(sess), "--summary"], check=False)
    print(f"готово: {sess / 'transcript.md'}", flush=True)


if __name__ == "__main__":
    main()
