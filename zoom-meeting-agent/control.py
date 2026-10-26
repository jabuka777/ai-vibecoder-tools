"""Session-bound, authenticated loopback control server for the SDK browser."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

STATIC = Path(__file__).resolve().parent / "static"
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
MAX_QUEUE = 100
MAX_MESSAGE_CHARS = 4000
REQUEST_TIMEOUT = 10
MAX_CONNECTIONS = 16


def write_chunk(session, ts, data):
    """Publish one complete audio chunk, without ever replacing another file.

    The private temporary file and its final hard link are on the same filesystem.
    link() atomically creates the final name or fails if it already exists, even
    across processes. Filesystems without hard links fail closed.
    """
    if not isinstance(ts, str) or not re.fullmatch(r"[0-9]{1,20}", ts):
        raise ValueError("audio timestamp must contain 1 to 20 ASCII digits")
    if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= MAX_AUDIO_BYTES:
        raise ValueError("empty, invalid or oversized audio chunk")
    session = Path(session).resolve()
    chunks = session / "chunks"
    if chunks.is_symlink() or chunks.resolve().parent != session:
        raise ValueError("chunks must be a real session directory")
    chunks.mkdir(exist_ok=True)
    target = chunks / (ts + ".webm")
    fd, temporary = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=chunks)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
    return target


class State:
    def __init__(self, sess: Path, env: dict, mn: str):
        self.sess = Path(sess).resolve()
        self.env = dict(env)
        self.mn = str(mn)
        self.token = secrets.token_urlsafe(32)
        self.port = None
        self.outbox = []
        self.lock = threading.Lock()
        self.audio_lock = threading.Lock()
        self.left = threading.Event()
        self.joined = False
        self.last_audio = None
        self.audio_source = False
        self.last_audio_source = None
        self.capture_failed = False
        self.sess.mkdir(parents=True, exist_ok=True)
        self.chunks = self.sess / "chunks"
        if self.chunks.is_symlink():
            raise ValueError("chunks must be a real session directory")
        self.chunks.mkdir(exist_ok=True)

    def save_outbox(self):
        # Called under self.lock. Retain a recoverable, private delivery ledger.
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.sess,
                                         prefix='.outbox-', delete=False) as stream:
            json.dump(self.outbox, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = stream.name
        os.replace(temporary, self.sess / 'outbox.json')

    def enqueue(self, message):
        with self.lock:
            active = [item for item in self.outbox if item['state'] in ('queued', 'sending')]
            if len(active) >= MAX_QUEUE:
                return None
            # Bound memory and snapshot size while keeping all unconfirmed work.
            history = [item for item in self.outbox if item['state'] not in ('queued', 'sending')]
            self.outbox = (history + active)[-(MAX_QUEUE - 1):] if len(active) < MAX_QUEUE - 1 else active
            item = {'id': uuid.uuid4().hex, 'message': message, 'state': 'queued'}
            self.outbox.append(item)
            try:
                self.save_outbox()
            except OSError:
                self.outbox.remove(item)
                raise
            return item['id']

    def delivery(self, message_id, action):
        with self.lock:
            item = next((item for item in self.outbox if item['id'] == message_id), None)
            if item is None:
                return 404
            old = item['state']
            if action == 'claim':
                if old != 'queued':
                    return 409
                new = 'sending'
            else:
                if action not in ('sent', 'failed', 'uncertain'):
                    return 400
                if old == action:
                    return 200  # A repeated acknowledgement must not re-send.
                if old != 'sending':
                    return 409
                new = action
            item['state'] = new
            try:
                self.save_outbox()
            except OSError:
                item['state'] = old
                raise
            return 200

    def event(self, rec: dict):
        """Keep a bounded JSON event with an authoritative receipt timestamp."""
        clean = _event(rec)
        now = time.monotonic()
        with self.lock:
            with (self.sess / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(clean, ensure_ascii=False) + "\n")
            if clean["kind"] == "capture_error":
                self.capture_failed = True
            if clean["kind"] == "joined":
                self.joined = True
            elif clean["kind"] in ("left", "join_error", "init_error"):
                self.joined = False
            elif clean["kind"] == "audio_source":
                data = clean.get("data")
                self.audio_source = not (isinstance(data, dict) and data.get("active") is False)
                self.last_audio_source = now
        if clean["kind"] in ("joined", "join_error", "init_error", "left"):
            print(f"[{clean['kind']}]", flush=True)


def _event(rec):
    if not isinstance(rec, dict) or not isinstance(rec.get("kind"), str):
        raise ValueError("event must have a kind")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", rec["kind"]):
        raise ValueError("invalid event kind")
    # Drop caller-supplied top-level fields and timestamps. JSON encoding escapes
    # newlines, so untrusted messages cannot inject separate journal records.
    clean = {"kind": rec["kind"], "data": rec.get("data"), "ts": int(time.time() * 1000)}
    if clean["kind"] == "chat":
        data = clean["data"]
        if not isinstance(data, dict) or not isinstance(data.get("sender"), str):
            raise ValueError("invalid chat sender")
        content = data.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("text"), str):
            raise ValueError("invalid chat content")
        clean["data"] = {"sender": data["sender"], "content": {"text": content["text"]}}
    elif clean["kind"] == "chat_sent":
        data = clean["data"]
        if not isinstance(data, dict) or not isinstance(data.get("m"), str):
            raise ValueError("invalid sent chat message")
        clean["data"] = {"m": data["m"]}
    elif clean['kind'].endswith('_error'):
        # SDK errors may echo join parameters, credentials or URLs. Retain only
        # a numeric diagnostic code; never serialize arbitrary error objects.
        data = clean['data']
        code = data.get('errorCode') if isinstance(data, dict) else None
        clean['data'] = {'error_code': code if type(code) is int else None}
    if len(json.dumps(clean, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("event is too large")
    return clean


def _signature(st):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
    iat = int(time.time()) - 30
    head = encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = {"appKey": st.env["ZOOM_SDK_CLIENT_ID"], "sdkKey": st.env["ZOOM_SDK_CLIENT_ID"],
               "mn": st.mn, "role": 0, "iat": iat, "exp": iat + 7200, "tokenExp": iat + 7200}
    body = encode(json.dumps(payload).encode())
    digest = hmac.new(st.env["ZOOM_SDK_CLIENT_SECRET"].encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{encode(digest)}"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(REQUEST_TIMEOUT)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_handler(st):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body=b"{}", ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def _authorized(self):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Host", []) != [host]:
                self._send(403)
                return False
            origins = self.headers.get_all("Origin", [])
            if origins and origins != [f"http://{host}"]:
                self._send(403)
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self._send(403)
                return False
            auth = self.headers.get_all("Authorization", [])
            expected = ("Bearer " + st.token).encode("utf-8")
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode("utf-8"), expected):
                self._send(401)
                return False
            return True

        def do_GET(self):
            if not self._authorized():
                return
            u = urlparse(self.path)
            if u.path == "/signature":
                if parse_qs(u.query) != {"mn": [st.mn]}:
                    return self._send(400)
                try:
                    body = json.dumps({"signature": _signature(st)}).encode()
                except KeyError:
                    return self._send(503)
                return self._send(200, body)
            if u.path == "/outbox":
                with st.lock:
                    messages = [dict(item) for item in st.outbox]
                return self._send(200, json.dumps(messages, ensure_ascii=False).encode())
            if u.path == "/left":
                return self._send(405)
            route = "/static/index.html" if u.path in ("/", "/index.html") else unquote(u.path)
            if route.startswith("/static/") and "\\" not in route and "\x00" not in route:
                static = STATIC.resolve()
                path = (static / route[len("/static/"):]).resolve()
                if static in path.parents and path.is_file():
                    ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                             ".css": "text/css", ".json": "application/json", ".wasm": "application/wasm"}.get(path.suffix, "application/octet-stream")
                    return self._send(200, path.read_bytes(), ctype)
            self._send(404)

        def do_POST(self):
            if not self._authorized():
                return
            u = urlparse(self.path)
            if u.path not in ("/audio", "/event", "/say", "/left", "/outbox/claim", "/outbox/ack"):
                return self._send(404)
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                return self._send(400)
            size = int(lengths[0])
            if size > (MAX_AUDIO_BYTES if u.path == "/audio" else MAX_JSON_BYTES):
                return self._send(413)
            try:
                data = self.rfile.read(size)
            except TimeoutError:
                return self._send(408)
            if len(data) != size:
                return self._send(400)
            if u.path == "/audio":
                return self._audio(u, data)
            if u.path == "/left":
                st.event({"kind": "left"})
                st.left.set()
                return self._send(200)
            try:
                value = json.loads(data)
                if u.path == "/event":
                    st.event(value)
                elif u.path.startswith('/outbox/'):
                    if not isinstance(value, dict) or not isinstance(value.get('id'), str):
                        return self._send(400)
                    action = 'claim' if u.path.endswith('/claim') else value.get('state')
                    return self._send(st.delivery(value['id'], action))
                else:
                    message = value.get("message") if isinstance(value, dict) else None
                    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
                        return self._send(400)
                    message_id = st.enqueue(message)
                    if message_id is None:
                        return self._send(429)
                    return self._send(200, json.dumps({'id': message_id, 'state': 'queued'}).encode())
            except (ValueError, TypeError, RecursionError):
                return self._send(400)
            except OSError:
                return self._send(500)
            self._send(200)

        def _audio(self, u, data):
            query = parse_qs(u.query)
            values = query.get("ts", [])
            if set(query) != {"ts"} or len(values) != 1 or not re.fullmatch(r"[0-9]{1,20}", values[0]) or not data:
                return self._send(400)
            with st.audio_lock:
                try:
                    write_chunk(st.sess, values[0], data)
                except ValueError:
                    return self._send(400)
                except FileExistsError:
                    return self._send(409)
                except OSError:
                    return self._send(500)
                st.last_audio = time.monotonic()
            return self._send(200)

    return Handler


def create_server(st: State, port=0):
    """Bind first, then exclusively persist this session's endpoint and token.

    The caller starts serve_forever. Existing control metadata is never replaced.
    """
    if st.port is not None:
        raise ValueError("session control server already created")
    server = _Server(("127.0.0.1", port), make_handler(st))
    control = st.sess / "control.json"
    try:
        fd = os.open(control, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "host": "127.0.0.1", "port": server.server_port,
                       "token": st.token, "meeting": st.mn}, stream)
        st.port = server.server_port
    except BaseException:
        server.server_close()
        raise
    return server
