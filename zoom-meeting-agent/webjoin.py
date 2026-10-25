#!/usr/bin/env python3
"""Слушатель через веб-клиент Zoom (app.zoom.us/wc): годится для ЧУЖИХ встреч, куда
SDK-бота без OBF не пускают. Заходит гостем, без микрофона и камеры, в чат не пишет.
Звук — тот же static/audio-hook.js, но куски уходят в Python через expose_binding
(страница https://app.zoom.us не может слать на 127.0.0.1). Расшифровка — stt.py,
в конце — выжимка владельцу (agent.py --summary).

  webjoin.py --mn <номер> --pwd <pwd из ссылки> [--name "AI-агент"] [--minutes 120]
"""
import argparse
import base64
import datetime as dt
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env import need, set_current, venv_python  # noqa: E402

HERE = Path(__file__).resolve().parent
HOOK = (HERE / "static" / "audio-hook.js").read_text(encoding="utf-8").replace(
    "if (blob.size > 2000) fetch('/audio?ts=' + Date.now(), {method: 'POST', body: blob});",
    "if (blob.size > 2000) blob.arrayBuffer().then(b => { let s = ''; const u = new Uint8Array(b);"
    " for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));"
    " window.__zbAudio(Date.now(), btoa(s)); });")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mn", required=True)
    ap.add_argument("--pwd", default="")
    ap.add_argument("--name", default="AI-агент")
    ap.add_argument("--minutes", type=float, default=180)
    ap.add_argument("--no-summary", action="store_true")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    mn = a.mn.replace(" ", "")
    sess = HERE / "sessions" / f"{dt.datetime.now():%Y-%m-%d_%H%M%S}-{mn}-web"
    (sess / "chunks").mkdir(parents=True)
    set_current(sess)
    print(f"сессия: {sess}", flush=True)
    need("ffmpeg")
    stt = subprocess.Popen([venv_python(), str(HERE / "stt.py"), str(sess)])

    def ev(kind, data=None):
        with open(sess / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": kind, "data": data, "ts": int(time.time() * 1000)}, ensure_ascii=False) + "\n")

    def on_audio(_src, ts, b64):
        (sess / "chunks" / f"{int(ts)}.webm").write_bytes(base64.b64decode(b64))

    from playwright.sync_api import sync_playwright
    deadline = time.time() + a.minutes * 60
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        state = HERE / "storage_state.json"  # вход аккаунта бота: для встреч «только для залогиненных»
        ctx = br.new_context(viewport={"width": 1280, "height": 720},
                             storage_state=str(state) if state.exists() else None)
        ctx.expose_binding("__zbAudio", on_audio)
        ctx.add_init_script(HOOK)
        page = ctx.new_page()
        try:
            page.goto(f"https://app.zoom.us/wc/join/{mn}" + (f"?pwd={a.pwd}" if a.pwd else ""))
            page.wait_for_timeout(6000)
            for _ in range(4):
                c = page.get_by_role("button", name="Continue without microphone and camera")
                if not c.count():
                    c = page.get_by_role("button", name="Continue without microphone")
                if c.count():
                    c.first.click()
                    page.wait_for_timeout(1200)
            name = page.get_by_role("textbox", name="Your Name")
            if name.count():
                name.fill(a.name)
            if page.locator("button.preview-join-button").count():
                page.locator("button.preview-join-button").click()
            page.wait_for_timeout(12000)
            # «Join Audio by Computer», если спросят — иначе звука встречи не будет
            for label in ("Join Audio by Computer", "Join Audio", "Got it", "OK"):
                b = page.get_by_role("button", name=label)
                if b.count():
                    try:
                        b.first.click(timeout=2000)
                    except Exception:
                        pass
            ev("joined", {"title": page.title()})
            print(f"[joined] {page.title()}", flush=True)
            n = 0
            while time.time() < deadline:
                page.wait_for_timeout(10000)
                n += 1
                if n % 3 == 1:
                    page.screenshot(path=str(sess / "last.png"))
                body = page.locator("body").inner_text()
                if "meeting has been ended" in body.lower() or "this meeting has ended" in body.lower():
                    ev("left", {"reason": "ended"})
                    break
        except KeyboardInterrupt:
            pass
        finally:
            try:
                page.screenshot(path=str(sess / "last.png"))
            except Exception:
                pass
            br.close()
    time.sleep(25)
    stt.terminate()
    if not a.no_summary:
        subprocess.run([sys.executable, str(HERE / "agent.py"), str(sess), "--summary"], check=False)
    print(f"готово: {sess / 'transcript.md'}", flush=True)


if __name__ == "__main__":
    main()
