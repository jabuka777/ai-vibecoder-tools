"""Synthetic contract tests; the adapter returns references, never an LLM answer."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("evaluate_minutes", ROOT / "tools" / "evaluate_minutes.py")
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)
minutes = evaluator.minutes


class MinutesContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = evaluator.load_cases()
        cls.by_id = {case["id"]: case for case in cls.cases}

    def mutated(self, ident):
        case = self.by_id[ident]
        return case, copy.deepcopy(evaluator.reference_summary(case))

    def assertRejected(self, case, summary):
        self.assertTrue(evaluator.evaluate_case(case, summary), case["id"])

    def test_reference_adapter_roundtrip_all_cases(self):
        self.assertGreaterEqual(len(self.cases), 12)
        with tempfile.TemporaryDirectory(prefix="meeting-contract-test-") as root:
            for case in self.cases:
                with self.subTest(case=case["id"]):
                    session = Path(root) / case["id"]
                    session.mkdir()
                    (session / "transcript.md").write_text(case["transcript"], encoding="utf-8")
                    calls = []
                    def reference_adapter(prompt, timeout=300):
                        calls.append(prompt)
                        self.assertIn("SOURCE_RECORDS_JSON", prompt)
                        return json.dumps({"facts": case["expected_facts"]}, ensure_ascii=False)
                    status = minutes.build_minutes(session, reference_adapter, "offline-reference-test")
                    self.assertEqual(status["status"], "complete", status)
                    self.assertEqual(len(calls), 1)
                    summary = json.loads((session / "summary.json").read_text())
                    self.assertEqual(evaluator.evaluate_case(case, summary), [])

    def test_wrong_owner_and_project_fail_even_when_quote_contains_them(self):
        case, summary = self.mutated("accepted_action")
        summary["facts"][0]["owner"] = "Мария"
        self.assertRejected(case, summary)
        case, summary = self.mutated("accepted_action")
        summary["facts"][0]["project"] = "Бета"
        self.assertRejected(case, summary)

    def test_proposal_cannot_be_promoted_to_task(self):
        case, summary = self.mutated("proposal_not_decision")
        summary["facts"][0]["type"] = "task"
        self.assertRejected(case, summary)

    def test_cancellation_and_late_corrections_cannot_disappear(self):
        for ident in ("explicit_cancellation", "deadline_moved", "late_owner_correction", "late_project_correction"):
            with self.subTest(case=ident):
                case, summary = self.mutated(ident)
                summary["facts"] = [fact for fact in summary["facts"] if fact["type"] != "cancelled"]
                self.assertRejected(case, summary)

    def test_wrong_corrected_due_and_owner_fail(self):
        case, summary = self.mutated("deadline_moved")
        corrected = next(fact for fact in summary["facts"] if fact["type"] == "task" and fact["due"] == "15 ноября")
        corrected["due"] = "12 ноября"
        self.assertRejected(case, summary)
        case, summary = self.mutated("deadline_moved")
        cancelled = next(fact for fact in summary["facts"] if fact["type"] == "cancelled")
        cancelled["due"] = "15 ноября"
        self.assertRejected(case, summary)
        case, summary = self.mutated("late_owner_correction")
        reassigned = next(fact for fact in summary["facts"] if fact["type"] == "task" and fact["owner"] == "Нина")
        reassigned["owner"] = "Олег"
        self.assertRejected(case, summary)
        case, summary = self.mutated("late_owner_correction")
        cancelled = next(fact for fact in summary["facts"] if fact["type"] == "cancelled")
        cancelled["owner"] = "Нина"
        self.assertRejected(case, summary)

    def test_two_projects_cannot_be_merged(self):
        case, summary = self.mutated("same_task_two_projects")
        summary["facts"].pop()
        self.assertRejected(case, summary)

    def test_injection_cannot_create_an_action_even_with_real_evidence(self):
        case, summary = self.mutated("prompt_injection_as_data")
        sources = {source["id"]: source for source in summary["sources"]}
        summary["facts"].append({
            "id": "F000002", "chunk": 1, "verification": "unconfirmed", "type": "task",
            "text": "Отправить пароли", "project": "Альфа", "owner": "Нина", "due": "сегодня",
            "evidence": [{"source_id": ident, "quote": sources[ident]["text"]}
                         for ident in ("T000001", "T000003")],
        })
        # Real source quotes alone do not imply that a quoted malicious request
        # was accepted; the separate reference comparison catches this mistake.
        plain = [{key: fact[key] for key in evaluator.FIELDS} for fact in summary["facts"]]
        minutes._validate(json.dumps({"facts": plain}, ensure_ascii=False), summary["sources"])
        self.assertRejected(case, summary)

    def test_unknown_owner_and_deadline_cannot_be_invented(self):
        case, summary = self.mutated("ambiguous_owner")
        summary["facts"][0]["owner"] = "Олег"
        self.assertRejected(case, summary)
        case, summary = self.mutated("deadline_unknown")
        summary["facts"][0]["due"] = "завтра"
        self.assertRejected(case, summary)

    def test_missing_source_and_confirmation_claim_are_rejected(self):
        case, summary = self.mutated("accepted_action")
        summary["chunks"][0]["source_ids"].pop()
        self.assertRejected(case, summary)
        case, summary = self.mutated("accepted_action")
        summary["verification"] = "verified"
        self.assertRejected(case, summary)

    def test_offline_cli_does_not_claim_a_model_was_tested(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = evaluator.main(["--offline"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertFalse(report["model_evaluated"])
        self.assertEqual(report["mode"], "fixture_integrity_only")

    def test_results_cli_requires_every_case(self):
        with tempfile.TemporaryDirectory(prefix="meeting-results-test-") as root:
            path = Path(root) / "results.jsonl"
            case = self.cases[0]
            path.write_text(json.dumps({"case_id": case["id"], "summary": evaluator.reference_summary(case)}, ensure_ascii=False) + "\n")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = evaluator.main(["--offline", "--results", str(path)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["passed"], 1)

    def test_explicit_literal_aliases_preserve_identity_and_deadline(self):
        case, summary = self.mutated("accepted_action")
        summary["facts"][0]["project"] = "Альфе"
        summary["facts"][0]["due"] = "до 12 ноября"
        self.assertEqual(evaluator.evaluate_case(case, summary), [])
        summary["facts"][0]["project"] = "Бете"
        self.assertRejected(case, summary)

    def test_cancellation_can_omit_unrestated_owner_and_due(self):
        case, summary = self.mutated("explicit_cancellation")
        cancelled = next(fact for fact in summary["facts"] if fact["type"] == "cancelled")
        cancelled["owner"] = None
        cancelled["due"] = None
        self.assertEqual(evaluator.evaluate_case(case, summary), [])
        cancelled["owner"] = "Нина"
        self.assertRejected(case, summary)

    def test_nullable_cancellation_does_not_weaken_new_assignment(self):
        case, summary = self.mutated("late_owner_correction")
        assignment = next(fact for fact in summary["facts"] if fact["type"] == "task" and fact["owner"] == "Нина")
        assignment["owner"] = None
        self.assertRejected(case, summary)
        case, summary = self.mutated("late_project_correction")
        cancelled = next(fact for fact in summary["facts"] if fact["type"] == "cancelled")
        self.assertEqual(cancelled["project"], "Альфа")
        assignment = next(fact for fact in summary["facts"] if fact["type"] == "task" and fact["project"] == "Бета")
        assignment["project"] = "Альфа"
        self.assertRejected(case, summary)

    def test_accepted_deadline_change_cancels_old_and_creates_new_task(self):
        case, summary = self.mutated("deadline_moved")
        actual = [(fact["type"], fact["due"]) for fact in summary["facts"]]
        self.assertEqual(actual, [("task", "12 ноября"), ("proposal", "15 ноября"),
                                  ("cancelled", "12 ноября"), ("task", "15 ноября")])
        summary["facts"] = [fact for fact in summary["facts"]
                            if not (fact["type"] == "task" and fact["due"] == "15 ноября")]
        self.assertRejected(case, summary)

    def test_rejected_deadline_change_cancels_proposal_not_current_task(self):
        case, summary = self.mutated("deadline_change_not_accepted")
        self.assertEqual([(fact["type"], fact["due"]) for fact in summary["facts"]],
                         [("task", "12 ноября"), ("proposal", "15 ноября"), ("cancelled", "15 ноября")])
        cancellation = next(fact for fact in summary["facts"] if fact["type"] == "cancelled")
        cancellation["due"] = "12 ноября"
        self.assertRejected(case, summary)

    def test_explicit_self_commitment_does_not_need_repeated_acknowledgement(self):
        case, summary = self.mutated("relative_deadline_preserved")
        task = summary["facts"][0]
        task.update(project="Альфе", evidence=[{
            "source_id":"T000002", "quote":case["transcript"].splitlines()[1]}])
        self.assertEqual(evaluator.evaluate_case(case, summary), [])

    def test_live_cli_mock_calls_are_isolated_and_metadata_only(self):
        captured = []
        def fake_call(prompt, *, backend, model, timeout, metrics_path):
            records = json.loads(prompt.rsplit("\n", 1)[1])
            text = "\n".join(source["text"] for source in records)
            case = next(case for case in self.cases if case["transcript"] == text)
            captured.append((backend, model, timeout, metrics_path.parent))
            provider = {"usage": {"input_tokens": 100, "output_tokens": 20,
                                  "arbitrary_text": "PRIVATE_PROVIDER_TEXT"},
                        "cost_usd": 0.002, "actual_text": "PRIVATE_PROVIDER_TEXT"}
            metrics_path.write_text(json.dumps(provider) + "\n")
            return json.dumps({"facts": case["expected_facts"]}, ensure_ascii=False)
        with tempfile.TemporaryDirectory(prefix="meeting-live-mock-") as root:
            destination = Path(root) / "evaluation"
            output = io.StringIO()
            with patch.object(evaluator.llm, "call", side_effect=fake_call), contextlib.redirect_stdout(output):
                code = evaluator.main(["--backend", "codex", "--model", "test-model",
                                       "--timeout", "12", "--output-dir", str(destination)])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["passed"], len(self.cases))
            self.assertEqual(report["total_calls"], len(self.cases))
            self.assertEqual(report["cached_chunks"], 0)
            self.assertEqual(report["call_errors"], 0)
            self.assertEqual(report["case_errors"], 0)
            self.assertTrue(report["cost_complete"])
            self.assertAlmostEqual(report["provider_reported_cost_usd"], 0.002 * len(self.cases))
            self.assertEqual(len({item[3] for item in captured}), len(self.cases))
            self.assertTrue(all(item[:3] == ("codex", "test-model", 12.0) for item in captured))
            self.assertTrue(all(not item[3].exists() for item in captured))
            metrics = (destination / "metrics.jsonl").read_text()
            self.assertNotIn("PRIVATE_PROVIDER_TEXT", metrics + output.getvalue())
            self.assertNotIn("Подготовить макет", metrics)
            self.assertEqual(len(evaluator.read_jsonl(destination / "results.jsonl")), len(self.cases))
            self.assertTrue((destination / "report.json").exists())

    def test_live_failure_continues_and_never_logs_exception_text(self):
        cases = self.cases[:2]
        calls = 0
        def fake_call(prompt, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise evaluator.llm.ModelError("PRIVATE_TRANSCRIPT_IN_EXCEPTION")
            return json.dumps({"facts": cases[1]["expected_facts"]}, ensure_ascii=False)
        with tempfile.TemporaryDirectory(prefix="meeting-live-failure-") as root:
            destination = Path(root) / "evaluation"
            with patch.object(evaluator.llm, "call", side_effect=fake_call):
                report = evaluator.run_live(cases, backend="claude", model="test-model",
                                            output_dir=destination)
            self.assertEqual(report["total_calls"], 2)
            self.assertEqual(report["call_errors"], 1)
            self.assertEqual(report["case_errors"], 1)
            self.assertEqual(report["passed"], 1)
            self.assertNotIn("provider_reported_cost_usd", report)
            for name in ("report.json", "metrics.jsonl", "results.jsonl"):
                self.assertNotIn("PRIVATE_TRANSCRIPT_IN_EXCEPTION", (destination / name).read_text())
            results = evaluator.read_jsonl(destination / "results.jsonl")
            self.assertIsNone(results[0]["summary"])
            self.assertEqual(results[1]["summary"]["status"], "complete")

    def test_live_requires_explicit_backend_and_model(self):
        with patch.object(evaluator.llm, "call") as call:
            for arguments in ([], ["--backend", "codex"],
                              ["--offline", "--backend", "codex", "--model", "test-model"]):
                with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        evaluator.main(arguments)
                    self.assertEqual(raised.exception.code, 2)
            call.assert_not_called()

    def test_live_refuses_existing_results_directory_before_call(self):
        with tempfile.TemporaryDirectory(prefix="meeting-live-existing-") as root:
            with patch.object(evaluator.llm, "call") as call:
                with self.assertRaises(FileExistsError):
                    evaluator.run_live(self.cases[:1], backend="codex", model="test-model", output_dir=root)
                call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
