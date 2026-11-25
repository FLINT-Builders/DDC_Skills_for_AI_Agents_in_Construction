"""Deadline tests for quadratic contract-clause patterns.

The implementation under test is the Python block in
4_DDC_Curated/Contract-Legal/contract-clause-extractor/SKILL.md.
These tests fail while that block is vulnerable: a contract section that
mentions payment, or a whole contract passed to generate_summary, can make
dollar, time-period, contract-sum, and party matching take time that grows
with the square of one token. They pass once those runs are bounded (or the
call is otherwise stopped) and ordinary clauses still extract.

Run from the repository root:

    python3 -m unittest tests.test_contract_clause_extractor_redos -v
"""

import multiprocessing
import unittest
from pathlib import Path

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "4_DDC_Curated"
    / "Contract-Legal"
    / "contract-clause-extractor"
    / "SKILL.md"
)

# Vulnerable inputs below were measured above roughly 10 seconds on
# CPython 3.12. A bounded quantifier finishes the same input in well under
# a tenth of a second, so a two-second kill is late for a fixed build and
# early for the backtracking one.
DEADLINE_SECONDS = 2.0
_FORK = multiprocessing.get_context("fork")


def load_extractor(skill_path=None):
    """Execute the skill's implementation fence and return the class."""
    path = Path(skill_path) if skill_path is not None else SKILL_PATH
    text = path.read_text(encoding="utf-8")
    fence = "```python\n"
    search_from = 0
    while True:
        start = text.find(fence, search_from)
        if start < 0:
            raise AssertionError(f"no Python implementation fence in {path}")
        code_start = start + len(fence)
        end = text.find("\n```", code_start)
        if end < 0:
            raise AssertionError(f"unterminated Python fence in {path}")
        code = text[code_start:end]
        if "class ContractClauseExtractor:" in code:
            namespace = {}
            exec(compile(code, str(path), "exec"), namespace)
            return namespace["ContractClauseExtractor"]
        search_from = end + 4


def _invoke(method_name, contract_text, result_pipe):
    try:
        extractor = load_extractor()()
        getattr(extractor, method_name)(contract_text)
    except BaseException as exc:
        result_pipe.send(f"{type(exc).__name__}: {exc}")
        return
    result_pipe.send(None)


def assert_finishes(method_name, contract_text):
    parent, child = _FORK.Pipe(duplex=False)
    process = _FORK.Process(
        target=_invoke,
        args=(method_name, contract_text, child),
    )
    process.start()
    child.close()
    process.join(DEADLINE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join()
        raise AssertionError(
            f"{method_name} was still running after {DEADLINE_SECONDS:.0f}s "
            f"on a {len(contract_text)}-character contract"
        )
    if not parent.poll(1):
        raise AssertionError(
            f"{method_name} exited {process.exitcode} without reporting a result"
        )
    failure = parent.recv()
    if failure:
        raise AssertionError(f"{method_name} failed: {failure}")


class ExtractorBehaviorTests(unittest.TestCase):
    """Ordinary contracts still yield amounts, periods, value, and parties."""

    @classmethod
    def setUpClass(cls):
        cls.extractor_cls = load_extractor()

    def test_payment_clause_amounts_and_periods(self):
        contract = """
ARTICLE 5 PAYMENT

Payment terms require 25000 dollars and a fee of $5,000.00 within 30 days.
Final payment follows 14 business days after acceptance.
"""
        clauses = self.extractor_cls().extract_clauses(contract)
        payment = [clause for clause in clauses if clause.category.value == "payment"]
        self.assertEqual(len(payment), 1)
        self.assertEqual(payment[0].dollar_amounts, [25000.0, 5000.0])
        self.assertEqual(payment[0].time_periods, ["30 days", "14 business days"])

    def test_contract_sum_and_written_amount(self):
        summed = self.extractor_cls().generate_summary(
            "The contract sum: $1,250.50 is agreed in full.\n"
        )
        self.assertEqual(summed.contract_value, 1250.5)

        written = self.extractor_cls().generate_summary(
            "The stipulated amount is $2,500.00 and no/100.\n"
        )
        self.assertEqual(written.contract_value, 2500.0)

    def test_labeled_parties_and_parenthetical_parties(self):
        labeled = self.extractor_cls().generate_summary(
            "Owner: Northwind Builders, a corporation\n"
            "Contractor: Apex Civil, an LLC\n"
        )
        self.assertEqual(
            labeled.parties,
            {"owner": "Northwind Builders", "contractor": "Apex Civil"},
        )

        parenthetical = self.extractor_cls().generate_summary(
            'Agreement between Northwind Builders ("Owner") '
            'and Apex Civil ("Contractor").\n'
        )
        self.assertEqual(
            parenthetical.parties,
            {"owner": "Northwind Builders", "contractor": "Apex Civil"},
        )


class ExtractorDeadlineTests(unittest.TestCase):
    """Attacker-controlled contract text must not stall clause extraction."""

    def test_extract_clauses_digit_run_in_payment_section_finishes(self):
        # Category gate matches "payment terms", then dollar and time patterns
        # scan a digit run that never reaches "dollars" or a unit word.
        contract = "payment terms\n" + ("9" * 30000)
        assert_finishes("extract_clauses", contract)

    def test_generate_summary_contract_value_digit_run_finishes(self):
        # No "contract sum" / "total contract" phrase, so the third value
        # pattern scans the whole digit run.
        assert_finishes("generate_summary", "9" * 30000)

    def test_generate_summary_repeated_owner_finishes(self):
        assert_finishes("generate_summary", "Owner " * 20000)

    def test_generate_summary_repeated_contractor_finishes(self):
        assert_finishes("generate_summary", "Contractor " * 15000)

    def test_generate_summary_repeated_between_owner_finishes(self):
        assert_finishes("generate_summary", "between A " * 15000)

    def test_generate_summary_repeated_and_contractor_finishes(self):
        assert_finishes("generate_summary", "and A " * 20000)


if __name__ == "__main__":
    unittest.main()
