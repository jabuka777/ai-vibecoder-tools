"""Two live loopback endpoints must never share a destination or credential."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control import State, create_server
from say import say


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.states, self.servers, self.threads = [], [], []
        for name in ("A", "B"):
            state = State(self.root / name, {}, name)
            server = create_server(state)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.states.append(state)
            self.servers.append(server)
            self.threads.append(thread)

    def tearDown(self):
        for server, thread in zip(self.servers, self.threads):
            server.shutdown()
            server.server_close()
            thread.join()
        self.temporary.cleanup()

    def test_starting_second_session_does_not_redirect_first(self):
        first, second = self.states
        # A stale global pointer must have no effect on explicit session routing.
        (self.root / "port").write_text(str(second.port), encoding="utf-8")
        self.assertTrue(say(first.sess, "SYNTHETIC_FOR_A"))
        self.assertTrue(say(second.sess, "SYNTHETIC_FOR_B"))
        self.assertTrue(say(first.sess, "SECOND_SYNTHETIC_FOR_A"))
        self.assertEqual([item['message'] for item in first.outbox], ["SYNTHETIC_FOR_A", "SECOND_SYNTHETIC_FOR_A"])
        self.assertEqual([item['message'] for item in second.outbox], ["SYNTHETIC_FOR_B"])

    def test_other_session_port_with_original_token_fails_closed(self):
        first, second = self.states
        path = first.sess / "control.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["port"] = second.port
        path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(HTTPError) as error:
            say(first.sess, "MUST_NOT_REACH_B")
        self.assertEqual(error.exception.code, 401)
        self.assertEqual(first.outbox, [])
        self.assertEqual(second.outbox, [])


if __name__ == "__main__":
    unittest.main()
