#!/usr/bin/env python3
"""Contract checks for synthetic meeting-minutes fixtures.

Offline mode never calls an LLM. Explicit --backend/--model runs live requests.
This is NOT a judge of free-text semantic equivalence.
It checks copied project/owner/deadline fields, fact types, topic keywords, and
source evidence against human-authored synthetic references. Corrections remain
separate ledger entries; this checker does not resolve the final active task list.
Accepted corrections retain the old task, cancel its OLD values, and add the new
task. Rejected proposals cancel the proposal, not the still-valid original task.
Fixture aliases are explicit; optional nulls apply only to selected cancellation
fields not restated by that event. Task/decision types are not interchangeable.
Additional grounded policy decisions or alternative fact decompositions can be
strict-ledger false negatives: inspect mismatches before judging semantic quality.

  python tools/evaluate_minutes.py --offline
  python tools/evaluate_minutes.py --offline --results results.jsonl
  python tools/evaluate_minutes.py --backend codex --model gpt-6-luna
  python tools/evaluate_minutes.py --backend claude --model sonnet --output-dir /tmp/meeting-eval-new

Live mode sends these synthetic transcripts to the selected model. It writes
results.jsonl, metadata-only metrics.jsonl and report.json to a NEW output folder.
Each case uses an isolated temporary session. No meeting, browser or STT runs.

Each results.jsonl record is {"case_id":"...", "summary": <summary.json object>}.
Without --results only fixture integrity is checked, not model quality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import minutes  # noqa: E402
import llm  # noqa: E402
from env import APP_HOME  # noqa: E402

DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "meeting_cases.jsonl"
FIELDS = ("type", "text", "project", "owner", "due", "evidence")
SIGNIFICANT_TYPES = {"decision", "task", "proposal", "cancelled"}
LIMITATION = (
    "Checks structured fields, topic keywords and exact evidence, not free-text semantic equivalence. "
    "Corrections remain separate draft ledger entries. Extra grounded policy decisions or alternative "
    "fact decompositions can fail this strict ledger contract; manually inspect mismatches. "
    "No LLM quality claim without actual model results."
)


def read_jsonl(path):
    records = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(f"Invalid JSON on line {number}") from exc
    return records


def sources_for(case):
    return minutes._sources({"transcript.md": case["transcript"], "events.jsonl": ""})


def load_cases(path=DEFAULT_FIXTURES):
    cases = read_jsonl(path)
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("Each fixture needs an id")
        ident = case["id"]
        if not re.fullmatch(r"[a-z][a-z0-9_]+", ident) or ident in seen:
            raise ValueError(f"Invalid or duplicate fixture id: {ident}")
        seen.add(ident)
        if case.get("schema_version") != 1 or not isinstance(case.get("transcript"), str):
            raise ValueError(f"Invalid fixture schema: {ident}")
        facts = case.get("expected_facts")
        sources = sources_for(case)
        minutes._validate(json.dumps({"facts": facts}, ensure_ascii=False), sources)
        aliases = case.get("aliases", {})
        if not isinstance(aliases, dict) or set(aliases) - {"project", "due"}:
            raise ValueError(f"Invalid alias fields: {ident}")
        for field, groups in aliases.items():
            if not isinstance(groups, dict):
                raise ValueError(f"Invalid aliases: {ident}")
            used = set()
            for canonical, values in groups.items():
                if (not isinstance(canonical, str) or not isinstance(values, list)
                        or canonical not in values or not all(isinstance(value, str) and value for value in values)
                        or len(set(values)) != len(values) or used.intersection(values)):
                    raise ValueError(f"Ambiguous or malformed aliases: {ident}")
                used.update(values)
        checks = case.get("checks")
        if not isinstance(checks, list) or len(checks) != len(facts):
            raise ValueError(f"One matching rule is required per expected fact: {ident}")
        source_ids = {source["id"] for source in sources}
        for check in checks:
            if not isinstance(check, dict) or set(check) != {"text_contains", "required_evidence_source_ids", "allow_null_fields"}:
                raise ValueError(f"Invalid matching rule: {ident}")
            if not isinstance(check["text_contains"], list) or not all(isinstance(x, str) and x for x in check["text_contains"]):
                raise ValueError(f"Invalid topic keywords: {ident}")
            nullable = check["allow_null_fields"]
            if not isinstance(nullable, list) or not all(field in {"owner", "due"} for field in nullable):
                raise ValueError(f"Invalid nullable fields: {ident}")
            refs = check["required_evidence_source_ids"]
            if not isinstance(refs, list) or not all(isinstance(ref, str) and ref in source_ids for ref in refs):
                raise ValueError(f"Unknown expected source: {ident}")
        # References themselves must satisfy the stated contract.
        problems = evaluate_case(case, reference_summary(case))
        if problems:
            raise ValueError(f"Inconsistent reference {ident}: {'; '.join(problems)}")
    if not cases:
        raise ValueError("No fixtures")
    return cases


def reference_summary(case):
    """Reference adapter for offline tests only; this is not a model response."""
    sources = sources_for(case)
    return {
        "schema_version": 1, "status": "complete", "verification": "unconfirmed",
        "model_key": "offline-handwritten-reference", "sources": sources,
        "chunks": [{"index": 1, "source_ids": [source["id"] for source in sources]}],
        "facts": [dict(fact, id=f"F{index:06}", chunk=1, verification="unconfirmed")
                  for index, fact in enumerate(case["expected_facts"], 1)],
    }


def _matches(expected, actual, check, aliases):
    for field in ("type", "project", "owner", "due"):
        wanted, observed = expected[field], actual[field]
        if wanted == observed:
            continue
        # Explicit fixture choices, never fuzzy normalization or inferred dates.
        # Some cancellation utterances do not restate the old assignee/deadline.
        if observed is None and field in check["allow_null_fields"]:
            continue
        if observed not in aliases.get(field, {}).get(wanted, [wanted]):
            return False
    lower = actual["text"].casefold()
    if any(word.casefold() not in lower for word in check["text_contains"]):
        return False
    ids = {item["source_id"] for item in actual["evidence"]}
    return set(check["required_evidence_source_ids"]) <= ids


def evaluate_case(case, summary):
    """Return contract failures; exact fields deliberately avoid an LLM judge."""
    problems = []
    if not isinstance(summary, dict):
        return ["summary must be an object"]
    if summary.get("schema_version") != 1 or summary.get("status") != "complete":
        problems.append("summary must be complete with schema_version=1")
    if summary.get("verification") != "unconfirmed":
        problems.append("summary must retain the unconfirmed draft label")
    sources = sources_for(case)
    if summary.get("sources") != sources:
        problems.append("summary sources do not exactly cover this fixture")
    try:
        coverage = [source_id for chunk in summary["chunks"] for source_id in chunk["source_ids"]]
        if sorted(coverage) != sorted(source["id"] for source in sources):
            problems.append("chunk coverage omits or duplicates a source")
    except (KeyError, TypeError):
        problems.append("chunk coverage is missing or malformed")
    actual = summary.get("facts")
    if not isinstance(actual, list):
        return problems + ["facts must be a list"]
    try:
        plain = []
        for fact in actual:
            if fact.get("verification") != "unconfirmed":
                problems.append("a fact lost its unconfirmed label")
            plain.append({field: fact[field] for field in FIELDS})
        minutes._validate(json.dumps({"facts": plain}, ensure_ascii=False), sources)
    except (minutes.MinutesError, KeyError, TypeError, AttributeError) as exc:
        return problems + [f"invalid facts/evidence: {type(exc).__name__}"]
    expected = case["expected_facts"]
    graph = [[j for j, fact in enumerate(plain) if _matches(item, fact, case["checks"][i], case.get("aliases", {}))]
             for i, item in enumerate(expected)]
    matched = {}
    def assign(index, visited):
        for candidate in graph[index]:
            if candidate in visited:
                continue
            visited.add(candidate)
            if candidate not in matched or assign(matched[candidate], visited):
                matched[candidate] = index
                return True
        return False
    for i, item in enumerate(expected):
        if not assign(i, set()):
            problems.append(f"missing/misassigned reference fact {i + 1}: " +
                            json.dumps({field: item[field] for field in ("type", "project", "owner", "due")}, ensure_ascii=False))
    for i, fact in enumerate(plain):
        if i not in matched and fact["type"] in SIGNIFICANT_TYPES:
            problems.append(f"unexpected or duplicate actionable fact {i + 1}")
    return problems



def _append_json(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def _numeric_usage(value):
    """Only numeric provider usage metadata, never an arbitrary model payload."""
    if not isinstance(value, dict):
        return None
    clean = {}
    for key, item in value.items():
        if key not in {"input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
                       "cache_creation_input_tokens", "cache_read_input_tokens", "reasoning_tokens",
                       "cache_creation", "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"}:
            continue
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            clean[key] = item
        elif isinstance(item, dict):
            nested = _numeric_usage(item)
            if nested:
                clean[key] = nested
    return clean or None


def run_live(cases, *, backend, model, output_dir, timeout=300):
    """Explicit live benchmark; exception text and model output stay out of logs."""
    started = time.monotonic()
    output_dir = Path(output_dir).resolve()
    # Never truncate an earlier evaluation or silently reuse its cached answers.
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    results_path = output_dir / "results.jsonl"
    metrics_path = output_dir / "metrics.jsonl"
    results_path.touch()
    metrics_path.touch()
    evaluations = []
    totals = {"total_calls": 0, "cached_chunks": 0, "call_errors": 0, "case_errors": 0}
    cost_values = []
    for case in cases:
        case_started = time.monotonic()
        case_calls = 0
        last_error_type = None
        summary = None
        status = {}
        with tempfile.TemporaryDirectory(prefix=f"meeting-eval-{case['id']}-") as temporary:
            session = Path(temporary)
            (session / "transcript.md").write_text(case["transcript"], encoding="utf-8")
            def ask(prompt, timeout=300):
                nonlocal case_calls, last_error_type
                case_calls += 1
                totals["total_calls"] += 1
                call_started = time.monotonic()
                # Per-call files prevent accidental reuse of a previous metric.
                provider_metrics = session / f"call-{case_calls}.jsonl"
                metric = {"case_id": case["id"], "call": case_calls,
                          "backend": backend, "model": model, "success": False}
                try:
                    result = llm.call(prompt, backend=backend, model=model,
                                      timeout=timeout_seconds, metrics_path=provider_metrics)
                    metric["success"] = True
                    _append_json(output_dir / "raw-responses.jsonl", {"case_id": case["id"], "call": case_calls, "response": result})
                    return result
                except Exception as exc:
                    totals["call_errors"] += 1
                    last_error_type = type(exc).__name__
                    metric["error_type"] = last_error_type
                    raise
                finally:
                    metric["elapsed_seconds"] = round(time.monotonic() - call_started, 3)
                    if provider_metrics.exists():
                        try:
                            records = read_jsonl(provider_metrics)
                            provider = records[-1] if records else {}
                            usage = _numeric_usage(provider.get("usage"))
                            if usage:
                                metric["usage"] = usage
                            reported_cost = provider.get("cost_usd")
                            if isinstance(reported_cost, (int, float)) and not isinstance(reported_cost, bool):
                                metric["provider_cost_usd"] = reported_cost
                                cost_values.append(reported_cost)
                        except (OSError, ValueError, TypeError, AttributeError):
                            metric["provider_metrics_unreadable"] = True
                    _append_json(metrics_path, metric)
            timeout_seconds = timeout
            try:
                status = minutes.build_minutes(session, ask, f"eval:{backend}:{model}")
                if status.get("status") == "complete":
                    summary = json.loads((session / "summary.json").read_text(encoding="utf-8"))
                    failures = evaluate_case(case, summary)
                else:
                    totals["case_errors"] += 1
                    failures = [f"model_or_extraction_failed:{last_error_type or 'MinutesIncomplete'}"]
            except Exception as exc:
                totals["case_errors"] += 1
                failures = [f"case_failed:{type(exc).__name__}"]
            cached = max(0, status.get("completed_chunks", 0) - case_calls)
            totals["cached_chunks"] += cached
            item = {"case_id": case["id"], "passed": not failures, "failures": failures,
                    "calls": case_calls, "cached_chunks": cached,
                    "elapsed_seconds": round(time.monotonic() - case_started, 3)}
            evaluations.append(item)
            _append_json(results_path, {"case_id": case["id"], "summary": summary,
                                       "evaluation": item})
    fixture_digest = hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    prompt_digest = hashlib.sha256(minutes.INSTRUCTIONS.encode()).hexdigest()
    report = {"mode": "live_results_contract_check", "backend": backend, "model": model,
              "fixture_set_sha256": fixture_digest, "extraction_instructions_sha256": prompt_digest,
              "passed": sum(item["passed"] for item in evaluations), "total": len(cases),
              **totals, "elapsed_seconds": round(time.monotonic() - started, 3),
              "call_count_definition": "Attempts to invoke llm.call; not independently verified provider request count.",
              "cases": evaluations, "results_path": str(results_path),
              "metrics_path": str(metrics_path), "limitation": LIMITATION}
    if cost_values:
        report["provider_reported_cost_usd"] = round(sum(cost_values), 8)
        report["cost_calls_reported"] = len(cost_values)
        report["cost_complete"] = len(cost_values) == totals["total_calls"]
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true", help="never call a model")
    mode.add_argument("--backend", choices=("claude", "codex", "deepseek"), help="explicit opt-in to live cloud requests")
    parser.add_argument("--model", help="required explicit model for live evaluation")
    parser.add_argument("--output-dir", type=Path, help="NEW live-results folder; defaults to sessions/evaluations/<unique>")
    parser.add_argument("--timeout", type=float, default=300, help="per-model-call timeout in seconds")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--results", type=Path, help="JSONL containing case_id and a summary.json object")
    args = parser.parse_args(argv)
    if args.backend and (not args.model or not args.model.strip()):
        parser.error("--backend requires an explicit --model")
    if args.backend and args.results:
        parser.error("--results is an offline input; use --output-dir for a live run")
    if args.offline and (args.model or args.output_dir):
        parser.error("--model and --output-dir require --backend")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        cases = load_cases(args.fixtures)
        if args.backend:
            destination = args.output_dir or APP_HOME / "sessions" / "evaluations" / (
                time.strftime("%Y%m%d-%H%M%S") + "-" + args.backend + "-" + uuid.uuid4().hex[:8])
            report = run_live(cases, backend=args.backend, model=args.model,
                              output_dir=destination, timeout=args.timeout)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["passed"] == report["total"] else 1
        if args.results is None:
            report = {"mode": "fixture_integrity_only", "fixtures": len(cases),
                      "model_evaluated": False, "limitation": LIMITATION}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        records = read_jsonl(args.results)
        results = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("case_id"), str) or "summary" not in record:
                raise ValueError("Each result needs case_id and summary")
            if record["case_id"] in results:
                raise ValueError(f"Duplicate case result: {record['case_id']}")
            results[record["case_id"]] = record["summary"]
        known = {case["id"] for case in cases}
        if set(results) - known:
            raise ValueError("Results contain unknown fixture IDs")
        evaluations = []
        for case in cases:
            failures = (["result missing"] if case["id"] not in results else
                        evaluate_case(case, results[case["id"]]))
            evaluations.append({"case_id": case["id"], "passed": not failures, "failures": failures})
        passed = sum(result["passed"] for result in evaluations)
        report = {"mode": "supplied_results_contract_check", "passed": passed,
                  "total": len(cases), "cases": evaluations, "limitation": LIMITATION}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if passed == len(cases) else 1
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc), "model_evaluated": False}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
