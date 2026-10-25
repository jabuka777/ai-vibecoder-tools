import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import minutes


def records(prompt):
    return json.loads(prompt[len(minutes.INSTRUCTIONS):])


def fact(source, text=None, **fields):
    return {"type": "decision", "text": text or source["text"], "project": None,
            "owner": None, "due": None,
            "evidence": [{"source_id": source["id"], "quote": source["text"]}], **fields}


class MinutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sess = Path(self.temp.name)

    def transcript(self, text):
        (self.sess / "transcript.md").write_text(text, encoding="utf-8")

    def test_entire_long_meeting_and_chat_are_included(self):
        self.transcript("EARLY_DECISION\n" + "neutral filler\n" * 6000 + "LATE_DECISION\n")
        events = [{"kind": "chat", "data": {"sender": "A", "content": {"text": f"CHAT_{n}"}}}
                  for n in range(205)]
        (self.sess / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        seen = []

        def ask(prompt, timeout):
            chunk = records(prompt)
            self.assertLessEqual(len(minutes._json(chunk)), minutes.CHUNK_CHAR_LIMIT)
            seen.extend(chunk)
            return json.dumps({"facts": [fact(s) for s in chunk if "DECISION" in s["text"]]})

        result = minutes.build_minutes(self.sess, ask, "stub:v1")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(seen), 6002 + 205)
        self.assertEqual(len({s["id"] for s in seen}), len(seen))
        text = (self.sess / "summary.md").read_text(encoding="utf-8")
        self.assertIn("EARLY\\_DECISION", text)
        self.assertIn("LATE\\_DECISION", text)
        self.assertIn("Требует проверки человеком", text)

    def test_long_single_line_is_split_without_lost_characters(self):
        text = ('"\\' * 20000) + "END"
        self.transcript(text)
        seen = []

        def ask(prompt, timeout):
            seen.extend(records(prompt))
            return '{"facts":[]}'

        self.assertEqual(minutes.build_minutes(self.sess, ask, "stub")["status"], "complete")
        self.assertEqual("".join(s["text"] for s in seen), text)
        self.assertGreater(len(seen), 1)

    def test_fabricated_evidence_or_inferred_fields_fail_closed(self):
        self.transcript("Alex will check ALPHA on Monday.")
        for mutation in (lambda f: f["evidence"][0].update(source_id="T999999"),
                         lambda f: f["evidence"][0].update(quote="fabricated quote"),
                         lambda f: f.update(owner="Casey"),
                         lambda f: f.update(due="2026-10-26"),
                         lambda f: f.pop("project")):
            def ask(prompt, timeout):
                f = fact(records(prompt)[0])
                mutation(f)
                return json.dumps({"facts": [f]})

            result = minutes.build_minutes(self.sess, ask, str(id(mutation)))
            self.assertEqual(result["status"], "incomplete")
            self.assertFalse((self.sess / "summary.md").exists())
            self.assertFalse((self.sess / "summary.json").exists())

    def test_unknown_fields_must_be_null_and_explicit_fields_are_preserved(self):
        self.transcript("Alex will check ALPHA on Monday.")

        def ask(prompt, timeout):
            return json.dumps({"facts": [fact(records(prompt)[0], owner="Alex", due="Monday", project="ALPHA")]})

        result = minutes.build_minutes(self.sess, ask, "stub")
        self.assertEqual(result["status"], "complete")
        ledger = json.loads((self.sess / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["facts"][0]["due"], "Monday")
        self.assertEqual(ledger["facts"][0]["verification"], "unconfirmed")

    def test_cache_reuse_model_invalidation_and_retry_after_partial_failure(self):
        self.transcript("neutral filler\n" * 3000)
        calls = []

        def failing(prompt, timeout):
            calls.append(prompt)
            return '{"facts":[]}' if len(calls) == 1 else None

        first = minutes.build_minutes(self.sess, failing, "stub:v1")
        self.assertEqual(first["status"], "incomplete")
        self.assertEqual(first["completed_chunks"], 1)

        def success(prompt, timeout):
            calls.append(prompt)
            return '{"facts":[]}'

        second = minutes.build_minutes(self.sess, success, "stub:v1")
        self.assertEqual(second["status"], "complete")
        self.assertEqual(second["model_calls"], second["total_chunks"] - 1)
        third = minutes.build_minutes(self.sess, success, "stub:v1")
        self.assertEqual(third["model_calls"], 0)
        changed = minutes.build_minutes(self.sess, success, "stub:v2")
        self.assertEqual(changed["model_calls"], changed["total_chunks"])

    def test_cancellation_and_proposal_do_not_erase_earlier_task(self):
        self.transcript("FIRST: do task\n" + "filler\n" * 3000 + "LAST: cancel task\nPROPOSAL: maybe next year\n")

        def ask(prompt, timeout):
            extracted = []
            for source in records(prompt):
                for marker, kind in (("FIRST:", "task"), ("LAST:", "cancelled"), ("PROPOSAL:", "proposal")):
                    if marker in source["text"]:
                        extracted.append(fact(source, type=kind))
            return json.dumps({"facts": extracted})

        self.assertEqual(minutes.build_minutes(self.sess, ask, "stub")["status"], "complete")
        ledger = json.loads((self.sess / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual([f["type"] for f in ledger["facts"]], ["task", "cancelled", "proposal"])

    def test_source_changes_or_bad_events_prevent_complete_status(self):
        self.transcript("original")

        def ask(prompt, timeout):
            self.transcript("original\nnew decision")
            return '{"facts":[]}'

        self.assertEqual(minutes.build_minutes(self.sess, ask, "stub")["status"], "incomplete")
        (self.sess / "events.jsonl").write_text("{broken", encoding="utf-8")
        result = minutes.build_minutes(self.sess, lambda *a, **k: self.fail("must not call model"), "stub")
        self.assertEqual(result["status"], "incomplete")

    def test_call_budget_fails_without_truncation_or_model_calls(self):
        self.transcript("x" * 40000)
        with patch.object(minutes, "MAX_CHUNKS", 1):
            result = minutes.build_minutes(self.sess, lambda *a, **k: self.fail("must not call model"), "stub")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["model_calls"], 0)
        self.assertGreater(result["total_chunks"], 1)

    def test_failed_rerun_marks_old_report_stale_and_keeps_original_history(self):
        self.transcript("ORIGINAL_DECISION")

        def ask(prompt, timeout):
            return json.dumps({"facts": [fact(source) for source in records(prompt)]})

        self.assertEqual(minutes.build_minutes(self.sess, ask, "stub")["status"], "complete")
        original_md = (self.sess / "summary.md").read_text(encoding="utf-8")
        original_json = (self.sess / "summary.json").read_text(encoding="utf-8")
        self.transcript("ORIGINAL_DECISION\nNEW_DECISION")
        failed = minutes.build_minutes(self.sess, lambda *a, **k: None, "stub")
        self.assertEqual(failed["status"], "incomplete")
        current_md = (self.sess / "summary.md").read_text(encoding="utf-8")
        self.assertTrue(current_md.startswith(minutes.STALE_BANNER))
        stale = json.loads((self.sess / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(stale["status"], "stale")
        history = self.sess / stale["previous_artifacts"]
        self.assertEqual((history / "summary.md").read_text(encoding="utf-8"), original_md)
        self.assertEqual((history / "summary.json").read_text(encoding="utf-8"), original_json)
        # Repeated failure keeps a single visible banner and the same original copy.
        minutes.build_minutes(self.sess, lambda *a, **k: None, "stub")
        self.assertEqual((self.sess / "summary.md").read_text(encoding="utf-8"), current_md)
        complete = minutes.build_minutes(self.sess, ask, "stub")
        self.assertEqual(complete["status"], "complete")
        self.assertFalse((self.sess / "summary.md").read_text(encoding="utf-8").startswith(minutes.STALE_BANNER))
        self.assertEqual(json.loads((self.sess / "summary.json").read_text(encoding="utf-8"))["status"], "complete")
        self.assertEqual((history / "summary.md").read_text(encoding="utf-8"), original_md)


if __name__ == "__main__":
    unittest.main()
