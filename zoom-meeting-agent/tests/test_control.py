"""Control-plane regressions with synthetic files and loopback only."""
import contextlib
import base64
import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = control.State(self.root / "session", {
            "ZOOM_SDK_CLIENT_ID": "synthetic", "ZOOM_SDK_CLIENT_SECRET": "synthetic"
        }, "123456789")
        self.server = control.create_server(self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method, route, body=None, *, token=True, headers=None):
        request_headers = {"Authorization": "Bearer " + self.state.token} if token else {}
        request_headers.update(headers or {})
        if body is None and method == "POST":
            body = b""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, route, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_all_routes_require_session_token(self):
        for method, route in (("GET", "/"), ("GET", "/signature?mn=123456789"),
                              ("GET", "/outbox"), ("POST", "/audio?ts=1"),
                              ("POST", "/outbox/claim"), ("POST", "/outbox/ack"),
                              ("POST", "/event"), ("POST", "/say"), ("POST", "/left")):
            with self.subTest(route=route):
                self.assertEqual(self.request(method, route, b"{}" if method == "POST" else None, token=False)[0], 401)
        self.assertEqual(self.request("GET", "/outbox", headers={"Authorization": "Bearer wrong-session"})[0], 401)
        self.assertFalse(self.state.left.is_set())
        self.assertEqual(self.state.outbox, [])

    def test_host_and_origin_are_pinned(self):
        for headers in ({"Host": "untrusted.example"}, {"Origin": "https://untrusted.example"},
                        {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("GET", "/outbox", headers=headers)[0], 403)
        self.assertEqual(self.request("GET", "/outbox", headers={"Origin": f"http://127.0.0.1:{self.server.server_port}"})[0], 200)

    def test_session_config_is_unique_and_cannot_be_overwritten(self):
        first = json.loads((self.state.sess / "control.json").read_text())
        second_state = control.State(self.root / "second", {}, "987654321")
        second_server = control.create_server(second_state)
        try:
            second = json.loads((second_state.sess / "control.json").read_text())
            self.assertNotEqual(first["port"], second["port"])
            self.assertNotEqual(first["token"], second["token"])
            self.assertEqual(first["meeting"], "123456789")
            self.assertEqual(first["host"], "127.0.0.1")
            with self.assertRaises(ValueError):
                control.create_server(self.state)
            replacement = control.State(self.state.sess, {}, "999")
            with self.assertRaises(FileExistsError):
                control.create_server(replacement)
            self.assertEqual(first, json.loads((self.state.sess / "control.json").read_text()))
            if os.name != "nt":
                self.assertEqual((self.state.sess / "control.json").stat().st_mode & 0o777, 0o600)
        finally:
            second_server.server_close()

    def test_traversal_absolute_and_duplicate_names_are_rejected(self):
        outside = self.root / "outside.webm"
        outside.write_bytes(b"original")
        for name in ("../../outside", str(outside)[:-5], "../outside", "1/2", "１", "1.webm", "1\\2", '-1', '9' * 21):
            self.assertEqual(self.request("POST", "/audio?" + urlencode({"ts": name}), b"audio")[0], 400)
        self.assertEqual(outside.read_bytes(), b"original")
        self.assertEqual(self.request("POST", "/audio?ts=1&ts=2", b"audio")[0], 400)
        self.assertEqual(self.request("POST", "/audio?ts=1", b"audio")[0], 200)
        self.assertEqual(self.request("POST", "/audio?ts=1", b"overwrite")[0], 409)
        self.assertEqual((self.state.chunks / "1.webm").read_bytes(), b"audio")
        self.assertIsNotNone(self.state.last_audio)
        self.assertEqual([p.name for p in self.state.chunks.iterdir()], ["1.webm"])

    def test_symlink_target_cannot_be_written(self):
        outside = self.root / "outside.webm"
        outside.write_bytes(b"original")
        try:
            (self.state.chunks / "123.webm").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.request("POST", "/audio?ts=123", b"overwrite")[0], 409)
        self.assertEqual(outside.read_bytes(), b"original")

    def test_write_chunk_helper_rejects_invalid_input(self):
        for stamp, data in (("../escape", b"x"), (1, b"x"), ("1", b""),
                            ("1", "not-bytes"), ("1", b"x" * (control.MAX_AUDIO_BYTES + 1))):
            with self.subTest(stamp=stamp, size=len(data)):
                with self.assertRaises(ValueError):
                    control.write_chunk(self.state.sess, stamp, data)
        self.assertEqual(list(self.state.chunks.iterdir()), [])

    def test_write_chunk_parallel_duplicate_has_exactly_one_winner(self):
        barrier = threading.Barrier(2)
        def upload(data):
            barrier.wait(timeout=2)
            try:
                control.write_chunk(self.state.sess, "777", data)
                return data
            except FileExistsError:
                return None
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(upload, [b"first", b"second"]))
        winners = [data for data in results if data is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual((self.state.chunks / "777.webm").read_bytes(), winners[0])
        self.assertEqual([p.name for p in self.state.chunks.iterdir()], ["777.webm"])

    def test_oversized_body_rejected_before_reading(self):
        for route, limit in (("/audio?ts=1", control.MAX_AUDIO_BYTES), ("/say", control.MAX_JSON_BYTES)):
            self.assertEqual(self.request("POST", route, b"", headers={"Content-Length": str(limit + 1)})[0], 413)
        self.assertEqual(self.request("POST", "/say", b"", headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.request("POST", "/say", b"", headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_say_queue_bound_and_acknowledgement(self):
        for value in ({"message": 123}, {"message": ""}, {"message": "x" * (control.MAX_MESSAGE_CHARS + 1)}, []):
            self.assertEqual(self.request("POST", "/say", json.dumps(value).encode())[0], 400)
        self.assertEqual(self.request("POST", "/say", b'{"message":"synthetic"}')[0], 200)
        first = json.loads(self.request("GET", "/outbox")[1])
        self.assertEqual(first[0]['message'], 'synthetic')
        self.assertEqual(json.loads(self.request("GET", "/outbox")[1]), first)
        payload = json.dumps({'id': first[0]['id']}).encode()
        self.assertEqual(self.request('POST', '/outbox/claim', payload)[0], 200)
        self.assertEqual(self.request('POST', '/outbox/claim', payload)[0], 409)
        ack = json.dumps({'id': first[0]['id'], 'state': 'sent'}).encode()
        self.assertEqual(self.request('POST', '/outbox/ack', ack)[0], 200)
        self.assertEqual(self.request('POST', '/outbox/ack', ack)[0], 200)
        self.assertEqual(json.loads((self.state.sess / 'outbox.json').read_text())[0]['state'], 'sent')
        if os.name != 'nt':
            self.assertEqual((self.state.sess / 'outbox.json').stat().st_mode & 0o777, 0o600)
        self.state.outbox[:] = [{'id': str(i), 'message': 'synthetic', 'state': 'queued'}
                               for i in range(control.MAX_QUEUE)]
        self.assertEqual(self.request("POST", "/say", b'{"message":"overflow"}')[0], 429)
        self.assertEqual(len(self.state.outbox), control.MAX_QUEUE)

    def test_failed_or_uncertain_delivery_is_retained_without_automatic_retry(self):
        for outcome in ('failed', 'uncertain'):
            message_id = self.state.enqueue('synthetic')
            payload = json.dumps({'id': message_id}).encode()
            self.assertEqual(self.request('POST', '/outbox/claim', payload)[0], 200)
            ack = json.dumps({'id': message_id, 'state': outcome}).encode()
            self.assertEqual(self.request('POST', '/outbox/ack', ack)[0], 200)
            self.assertEqual(self.request('POST', '/outbox/claim', payload)[0], 409)
            ledger = json.loads(self.request('GET', '/outbox')[1])
            self.assertEqual(next(item['state'] for item in ledger if item['id'] == message_id), outcome)

    def test_slow_body_times_out_and_server_continues(self):
        with patch.object(control, 'REQUEST_TIMEOUT', 0.05):
            self.assertEqual(self.request('POST', '/say', b'x', headers={'Content-Length': '100'})[0], 408)
        self.assertEqual(self.request('GET', '/outbox')[0], 200)

    def test_sdk_error_journal_does_not_echo_credentials(self):
        payload = {'kind': 'join_error', 'data': {'errorCode': 300, 'password': 'CANARY_PASSWORD',
                   'signature': 'CANARY_SIGNATURE', 'nested': {'url': 'CANARY_URL'}}}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.request('POST', '/event', json.dumps(payload).encode())[0], 200)
        journal = (self.state.sess / 'events.jsonl').read_text()
        self.assertNotIn('CANARY', journal)
        self.assertEqual(json.loads(journal)['data'], {'error_code': 300})

    def test_signature_only_for_bound_meeting(self):
        self.assertEqual(self.request("GET", "/signature?mn=other")[0], 400)
        self.assertEqual(self.request('GET', '/signature?mn=')[0], 400)
        self.assertEqual(self.request("GET", "/signature?mn=123456789&mn=other")[0], 400)
        status, body = self.request("GET", "/signature?mn=123456789")
        self.assertEqual(status, 200)
        head, payload, signature = json.loads(body)['signature'].split('.')
        decode = lambda value: base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
        claims = json.loads(decode(payload))
        self.assertEqual(claims['mn'], '123456789')
        self.assertEqual(claims['role'], 0)
        self.assertEqual(claims['exp'] - claims['iat'], 7200)
        expected = hmac.new(b'synthetic', (head + '.' + payload).encode(), hashlib.sha256).digest()
        self.assertEqual(decode(signature), expected)

    def test_outbox_history_remains_bounded_without_dropping_active_work(self):
        first_id = self.state.enqueue('must remain queued')
        for _ in range(control.MAX_QUEUE + 5):
            message_id = self.state.enqueue('synthetic delivered')
            self.assertEqual(self.state.delivery(message_id, 'claim'), 200)
            self.assertEqual(self.state.delivery(message_id, 'sent'), 200)
        self.assertLessEqual(len(self.state.outbox), control.MAX_QUEUE)
        self.assertTrue(any(item['id'] == first_id and item['state'] == 'queued' for item in self.state.outbox))

    def test_events_have_server_timestamp_and_health_state(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.request("POST", "/event", b'{"kind":"joined","ts":1}')[0], 200)
        self.assertTrue(self.state.joined)
        self.assertEqual(self.request("POST", "/event", b'{"kind":"audio_source","data":{"type":"synthetic"}}')[0], 200)
        self.assertTrue(self.state.audio_source)
        self.assertIsNotNone(self.state.last_audio_source)
        events = [json.loads(line) for line in (self.state.sess / "events.jsonl").read_text().splitlines()]
        self.assertGreater(events[0]["ts"], 1)
        self.assertNotIn("injected", events[0])
        for payload in (b'[]', b'{"kind":"joined\\nforged"}', b'{"kind":"test","data":NaN}'):
            self.assertEqual(self.request("POST", "/event", payload)[0], 400)

    def test_unknown_control_fields_do_not_change_state(self):
        payload = {'message': 'synthetic', 'unexpected': True}
        self.assertEqual(self.request('POST', '/say', json.dumps(payload).encode())[0], 400)
        self.assertEqual(self.state.outbox, [])
        self.assertEqual(self.request('POST', '/event', b'{"kind":"joined","unexpected":true}')[0], 400)
        self.assertFalse(self.state.joined)
        self.assertFalse((self.state.sess / 'events.jsonl').exists())
        message_id = self.state.enqueue('synthetic')
        claim = {'id': message_id, 'unexpected': True}
        self.assertEqual(self.request('POST', '/outbox/claim', json.dumps(claim).encode())[0], 400)
        self.assertEqual(self.state.outbox[0]['state'], 'queued')
        self.assertEqual(self.request('POST', '/outbox/claim', json.dumps({'id': message_id}).encode())[0], 200)
        ack = {'id': message_id, 'state': 'sent', 'unexpected': True}
        self.assertEqual(self.request('POST', '/outbox/ack', json.dumps(ack).encode())[0], 400)
        self.assertEqual(self.state.outbox[0]['state'], 'sending')
        del ack['unexpected']
        self.assertEqual(self.request('POST', '/outbox/ack', json.dumps(ack).encode())[0], 200)
        self.assertEqual(self.state.outbox[0]['state'], 'sent')

    def test_malformed_chat_cannot_poison_agent_event_reader(self):
        for payload in ({"kind": "chat", "data": []},
                        {"kind": "chat", "data": {"sender": "synthetic", "content": []}},
                        {"kind": "chat_sent", "data": {"m": []}}):
            self.assertEqual(self.request("POST", "/event", json.dumps(payload).encode())[0], 400)
        valid = {"kind": "chat", "data": {"sender": "synthetic", "content": {"text": "hello\nworld"}, "extra": "drop"}}
        self.assertEqual(self.request("POST", "/event", json.dumps(valid).encode())[0], 200)
        lines = (self.state.sess / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["data"], {"sender": "synthetic", "content": {"text": "hello\nworld"}})

    def test_left_requires_authenticated_post(self):
        self.assertEqual(self.request("GET", "/left")[0], 405)
        self.assertFalse(self.state.left.is_set())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.request("POST", "/left")[0], 200)
        self.assertTrue(self.state.left.is_set())

    def test_static_requires_auth_and_confines_paths(self):
        self.assertEqual(self.request("GET", "/")[0], 200)
        for route in ("/static/../../README.md", "/static/%2e%2e/README.md", "/static/%00"):
            self.assertEqual(self.request("GET", route)[0], 404)


if __name__ == "__main__":
    unittest.main()
