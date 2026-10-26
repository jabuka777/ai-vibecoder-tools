"""Synthetic damaged journals, partial UTF-8 writes and reply-budget checks."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent
from llm import ModelError


class AgentEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = Path(self.temp.name)
        self.events = self.session / 'events.jsonl'

    def event(self, message='бот, ответь', sender='Synthetic speaker'):
        return {'kind': 'chat', 'data': {'sender': sender, 'content': {'text': message}}}

    def test_partial_utf8_and_json_are_not_lost(self):
        tail = agent.LineTail(self.events)
        raw = (json.dumps(self.event(), ensure_ascii=False) + '\n').encode()
        split = raw.index('б'.encode()) + 1
        self.events.write_bytes(raw[:split])
        self.assertEqual(tail.read(), [])
        with self.events.open('ab') as stream:
            stream.write(raw[split:])
        self.assertEqual(json.loads(tail.read()[0]), self.event())
        self.assertEqual(tail.read(), [])

    def test_rewriting_consumed_text_does_not_replay_questions(self):
        tail = agent.LineTail(self.events)
        self.events.write_text('original question\n')
        self.assertEqual(tail.read(), ['original question\n'])
        self.events.write_text('different question\nnew line\n')
        with self.assertRaises(ValueError):
            tail.read()

    def test_overlong_record_is_skipped_without_consuming_next(self):
        tail = agent.LineTail(self.events)
        self.events.write_bytes(b'x' * (agent.MAX_LINE_BYTES + 1) + b'\n' + b'{"kind":"left"}\n')
        self.assertEqual([json.loads(line) for line in tail.read()], [{'kind': 'left'}])
        self.assertEqual(tail.errors, 1)

    def test_damaged_shapes_do_not_poison_context(self):
        invalid = [[], {'kind': 'chat', 'data': None}, {'kind': 'chat', 'data': {'content': 'bad'}},
                   {'kind': 'chat_sent', 'data': []}]
        self.events.write_text('\n'.join(json.dumps(item) for item in invalid + [self.event()]) + '\n')
        self.assertIn('ответь', agent.context(self.session))

    def test_live_reader_survives_bad_records_and_model_failure(self):
        def append(_):
            payloads = ['[]', '{broken', json.dumps({'kind': 'chat', 'data': {'content': None}}),
                        json.dumps(self.event()), json.dumps(self.event('бот, ещё вопрос')),
                        json.dumps({'kind': 'left'})]
            self.events.write_text('\n'.join(payloads) + '\n')
        with patch.object(agent.time, 'sleep', side_effect=append), \
                patch.object(agent, 'answer', side_effect=[ModelError('synthetic'), None]) as answer:
            agent.listen(self.session, backend='codex')
        self.assertEqual(answer.call_count, 2)
        health = json.loads((self.session / 'agent-health.json').read_text())
        self.assertEqual(health['invalid_records'], 3)
        self.assertEqual(health['reply_failures'], 1)

    def test_burst_of_fifty_questions_has_explicit_attempt_limit(self):
        def append(_):
            self.events.write_text('\n'.join(json.dumps(self.event()) for _ in range(50)) +
                                   '\n' + json.dumps({'kind': 'left'}) + '\n')
        with patch.object(agent.time, 'sleep', side_effect=append), patch.object(agent, 'answer') as answer:
            agent.listen(self.session, backend='codex')
        self.assertEqual(answer.call_count, 6)
        health = json.loads((self.session / 'agent-health.json').read_text())
        self.assertEqual(health['rate_limited'], 44)

    def test_session_budget_does_not_reset_after_window(self):
        budget = agent.ReplyBudget(maximum=2, per_minute=1)
        with patch.object(agent.time, 'monotonic', side_effect=[0, 1, 61, 122]):
            self.assertEqual([budget.allow() for _ in range(4)], [True, False, True, False])

    def test_mentions_and_own_messages_do_not_trigger_replies(self):
        def append(_):
            payloads = [self.event('наш бот обновлён'), self.event('бот, я сам', agent.BOT_NAME),
                        self.event('? вопрос'), {'kind': 'left'}]
            self.events.write_text('\n'.join(json.dumps(item) for item in payloads) + '\n')
        with patch.object(agent.time, 'sleep', side_effect=append), patch.object(agent, 'answer') as answer:
            agent.listen(self.session, backend='codex')
        self.assertEqual(answer.call_count, 1)


if __name__ == '__main__':
    unittest.main()
