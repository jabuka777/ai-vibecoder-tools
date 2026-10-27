"""Late STT chunks must not crash replies, replay chat or alter summary input."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent
import minutes
import stt


class TranscriptTailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = Path(self.temp.name)
        self.transcript = self.session / 'transcript.md'

    def test_late_chunk_reorders_real_stt_view_without_replaying_existing_speech(self):
        stt.recover_transcript(self.session, set())
        tail = agent.TranscriptTail(self.transcript)
        records = self.session / '.stt-records'
        late = {'chunk': '1700000040000.webm', 'stamp': '12:00:20', 'text': 'бот, поздний вопрос'}
        early = {'chunk': '1700000020000.webm', 'stamp': '12:00:00', 'text': 'бот, ранний вопрос'}
        (records / '1700000040000.webm.json').write_text(json.dumps(late), encoding='utf-8')
        stt.recover_transcript(self.session, set())
        self.assertEqual(tail.read(), ['[12:00:20] бот, поздний вопрос\n'])
        (records / '1700000020000.webm.json').write_text(json.dumps(early), encoding='utf-8')
        stt.recover_transcript(self.session, set())
        self.assertEqual(tail.read(), ['[12:00:00] бот, ранний вопрос\n'])
        self.assertEqual(tail.read(), [])
        # Full minutes still consume the full chronological view, independently
        # from the listener's digest history and any listener restart.
        restarted = agent.TranscriptTail(self.transcript)
        self.assertEqual(restarted.read(), [])
        source = minutes._sources(minutes._read_inputs(self.session))
        self.assertEqual([item['text'] for item in source],
                         ['[12:00:00] бот, ранний вопрос', '[12:00:20] бот, поздний вопрос'])

    def test_occurrence_counts_preserve_two_identical_lines_once_each(self):
        tail = agent.TranscriptTail(self.transcript)
        self.transcript.write_text('identical\n', encoding='utf-8')
        self.assertEqual(tail.read(), ['identical\n'])
        self.transcript.write_text('inserted\nidentical\nidentical\n', encoding='utf-8')
        self.assertEqual(tail.read(), ['inserted\n', 'identical\n'])
        self.transcript.write_text('identical\nidentical\ninserted\n', encoding='utf-8')
        self.assertEqual(tail.read(), [])

    def test_partial_utf8_bad_line_and_long_line_do_not_hide_next_speech(self):
        tail = agent.TranscriptTail(self.transcript)
        raw = 'бот, вопрос\n'.encode('utf-8')
        self.transcript.write_bytes(raw[:1])
        self.assertEqual(tail.read(), [])
        self.transcript.write_bytes(raw)
        self.assertEqual(tail.read(), ['бот, вопрос\n'])
        self.transcript.write_bytes(raw + b'\xff\n' + b'x' * (agent.MAX_LINE_BYTES + 1) + b'\nvalid\n')
        self.assertEqual(tail.read(), ['valid\n'])
        self.assertEqual(tail.errors, 2)

    def test_listener_survives_reordered_speech_without_replaying_chat(self):
        events = self.session / 'events.jsonl'
        tick = 0
        clock = 0
        def advance(_):
            nonlocal tick, clock
            tick += 1
            clock = tick * 100
            if tick == 1:
                events.write_text(json.dumps({'kind': 'chat', 'data': {
                    'sender': 'Synthetic participant', 'content': {'text': '? chat question'}}}) + '\n')
                self.transcript.write_text('[12:00:20] бот, поздний вопрос\n')
            elif tick == 2:
                self.transcript.write_text('[12:00:00] бот, ранний вопрос\n[12:00:20] бот, поздний вопрос\n')
            elif tick == 3:
                with events.open('a') as stream:
                    stream.write(json.dumps({'kind': 'left'}) + '\n')
        with patch.object(agent.time, 'sleep', side_effect=advance), \
                patch.object(agent.time, 'time', side_effect=lambda: clock), \
                patch.object(agent, 'answer') as answer:
            agent.listen(self.session, backend='codex')
        questions = [item.args[2].strip() for item in answer.call_args_list]
        self.assertEqual(questions, ['? chat question', 'бот, поздний вопрос', 'бот, ранний вопрос'])
        self.assertEqual(json.loads((self.session / 'agent-health.json').read_text())['invalid_records'], 0)


if __name__ == '__main__':
    unittest.main()
