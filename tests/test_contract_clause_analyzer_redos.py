"""Failing tests for the contract-clause-analyzer section-heading split.

The skill implementation in SKILL.md splits every contract with a heading
regular expression and then analyzes the sections. A long run of digits and
periods, with no whitespace and capital letter after it, makes that split
scan the run once per digit. These tests call the published analyze_text
path on that input.

They are expected to fail while the unbounded heading pattern is still in
use, and to pass once a contract of this size is analyzed within the budget
below. The budget applies to analyze_text itself, after the skill module
has been loaded.
"""

from __future__ import annotations

import multiprocessing
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "Document-Control"
    / "contract-clause-analyzer"
    / "SKILL.md"
)

# Wall-clock cap so a hang fails the test instead of stalling the runner.
# The assertion that matters is ANALYZE_BUDGET_SECONDS, measured inside the
# child around analyze_text only.
PARENT_JOIN_SECONDS = 8
ANALYZE_BUDGET_SECONDS = 2.0

# "1." * PAIRS is a few hundred thousand characters: long enough that the
# quadratic split cannot finish inside the budget, and short enough that a
# line-anchored, bounded heading pattern still finishes comfortably.
DIGIT_PERIOD_PAIRS = 200_000

LEGITIMATE_CONTRACT = """AGREEMENT
This Subcontract is made between the Owner and the Contractor.

1.1 Payment
The contractor shall pay within 30 days. Retainage of 10% applies.

2.1 Indemnification
The contractor shall indemnify and hold harmless the owner from consequential damages. Unlimited liability applies.

3.2 Schedule
Time is of the essence. Completion within 45 calendar days.

10.2.1 Liquidated Damages
The contractor shall pay liquidated damages of $1000 per day.
"""


def _load_analyzer(skill_path: str):
    source = Path(skill_path).read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", source, re.DOTALL)
    if match is None:
        raise AssertionError(f"no Python implementation block in {skill_path}")
    namespace: dict = {}
    exec(compile(match.group(1), skill_path, "exec"), namespace)
    try:
        return namespace["ContractClauseAnalyzer"]
    except KeyError as exc:
        raise AssertionError("ContractClauseAnalyzer is not defined") from exc


def _run_analyze(skill_path: str, contract_name: str, text: str, conn) -> None:
    """Execute the skill's analyze_text in a child so the parent can stop a hang."""
    import time

    started = time.perf_counter()
    try:
        analyzer_cls = _load_analyzer(skill_path)
        result = analyzer_cls().analyze_text(contract_name, text)
    except Exception as exc:
        conn.send(
            {
                "status": "error",
                "elapsed": time.perf_counter() - started,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        conn.close()
        return

    clauses = []
    for clause in result.clauses:
        clauses.append(
            {
                "title": clause.title[:8000],
                "text": clause.text[:8000],
            }
        )
    conn.send(
        {
            "status": "ok",
            "elapsed": time.perf_counter() - started,
            "total": result.total_clauses,
            "clauses": clauses,
        }
    )
    conn.close()


def run_analyze(text: str, contract_name: str = "Construction Contract") -> dict:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_run_analyze,
        args=(str(SKILL_MD), contract_name, text, child),
    )
    process.start()
    child.close()
    returned = parent.poll(PARENT_JOIN_SECONDS)
    if not returned:
        process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)
        raise AssertionError(
            "analyze_text did not return within "
            f"{PARENT_JOIN_SECONDS:.0f}s for a {len(text)}-character contract"
        )
    message = parent.recv()
    process.join(5)
    return message


def clause_blob(message: dict) -> str:
    parts = []
    for clause in message["clauses"]:
        parts.append(clause["title"])
        parts.append(clause["text"])
    return "\n".join(parts).lower()


def assert_finished_within_budget(test: unittest.TestCase, message: dict) -> None:
    test.assertLess(
        message["elapsed"],
        ANALYZE_BUDGET_SECONDS,
        "analyze_text took "
        f"{message['elapsed']:.2f}s; budget is {ANALYZE_BUDGET_SECONDS:.1f}s "
        f"(status={message['status']})",
    )


def assert_legitimate_clauses(test: unittest.TestCase, message: dict) -> None:
    test.assertEqual(message["status"], "ok", message)
    test.assertGreaterEqual(message["total"], 4, message["clauses"])
    blob = clause_blob(message)
    for phrase in (
        "30 days",
        "indemnify",
        "calendar days",
        "liquidated damages",
    ):
        test.assertIn(phrase, blob, msg=f"missing {phrase!r} in analyzed clauses")


class ContractClauseAnalyzerRedosTest(unittest.TestCase):
    def test_legitimate_numbered_clauses_are_analyzed(self) -> None:
        message = run_analyze(LEGITIMATE_CONTRACT)
        assert_finished_within_budget(self, message)
        assert_legitimate_clauses(self, message)

    def test_digit_period_run_at_end_of_text_finishes(self) -> None:
        # Heading pattern needs whitespace and a capital letter. End of text
        # supplies neither, so every digit retries the digits-and-periods repeat.
        text = "1." * DIGIT_PERIOD_PAIRS
        message = run_analyze(text)
        assert_finished_within_budget(self, message)

    def test_digit_period_run_before_lowercase_finishes(self) -> None:
        text = ("1." * DIGIT_PERIOD_PAIRS) + " a"
        message = run_analyze(text)
        assert_finished_within_budget(self, message)

    def test_digit_period_run_does_not_block_preceding_clauses(self) -> None:
        # A real contract, then the run hidden after the clauses. Earlier
        # clauses must still come back, and the call must stay inside the budget.
        text = LEGITIMATE_CONTRACT + "\n" + ("1." * DIGIT_PERIOD_PAIRS)
        message = run_analyze(text)
        assert_finished_within_budget(self, message)
        assert_legitimate_clauses(self, message)


if __name__ == "__main__":
    unittest.main()
