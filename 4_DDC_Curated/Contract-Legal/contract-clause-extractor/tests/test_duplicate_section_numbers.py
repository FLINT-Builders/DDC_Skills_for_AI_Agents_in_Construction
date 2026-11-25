"""Spec for contract-clause-extractor section retention.

These tests fail while the skill is vulnerable. `_split_into_sections` stores
sections in a dict keyed only by the heading number, so a later ARTICLE or
SECTION with the same number replaces the earlier text before classification.
`extract_clauses`, `generate_summary`, and `generate_report` then omit the
replaced clause from the clauses, the risk assessment, the action items, and
the report.

They load the implementation from SKILL.md. A fix should keep every section
(an ordered list of number/text pairs, or a key that is unique even when the
number repeats), record a warning in the report when a number repeats, and
ignore an unmatched optional title group from `re.split` instead of crashing.
"""

import re
import unittest
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"

INDEMNITY_MARKER = "tower crane collapse claim"
NOTICES_MARKER = "certified mail to the project office"
PAYMENT_MARKER = "curtain wall package"
DISPUTE_MARKER = "project county arbitration seat"

# Counterparty contract: high-risk Article 12, then an exhibit that restarts
# numbering at Section 12 with text that matches no clause category.
ATTACK_CONTRACT = f"""
This Agreement is between Harbor Owner LLC ("Owner")
and Northwind Builders Inc ("Contractor").

ARTICLE 12: INDEMNIFICATION

Contractor shall indemnify and hold harmless the Owner from and against
all claims arising out of the work, including consequential damages,
and waive any limitation. This broad form indemnification covers the
{INDEMNITY_MARKER}.

EXHIBIT A

SECTION 12: Notices

All notices under this exhibit shall be sent by {NOTICES_MARKER}.
"""

# Three categorized sections that share one number. Keeping only the first
# or only the last still hides a clause.
REPEATED_CATEGORIES = f"""
ARTICLE 12: INDEMNIFICATION

Contractor shall indemnify and hold harmless the Owner, including consequential damages,
and waive claims related to the {INDEMNITY_MARKER}.

SECTION 12: PAYMENT

Progress payments shall be made within 30 days. Retainage of ten percent shall
be withheld from the {PAYMENT_MARKER}.

SECTION 12: DISPUTE RESOLUTION

Dispute resolution shall proceed by binding arbitration at the {DISPUTE_MARKER}.
"""

# A heading with no capitalized title makes the optional title group miss.
# re.split then yields None, and the current loop calls None.strip().
UNTITLED_HEADING = f"""
ARTICLE 5: PAYMENT

Progress payments shall be made within 30 days based on the schedule of values.

SECTION 12
12.1 Contractor shall indemnify and hold harmless the Owner, including consequential damages,
and waive all claims for the {INDEMNITY_MARKER}.
"""

CLEAN_CONTRACT = f"""
ARTICLE 12: INDEMNIFICATION

Contractor shall indemnify and hold harmless the Owner, including consequential damages,
and waive claims related to the {INDEMNITY_MARKER}.

ARTICLE 5: PAYMENT

Progress payments shall be made within 30 days. Retainage of ten percent shall
be withheld from the {PAYMENT_MARKER}.
"""

DUPLICATE_WARNING = re.compile(
    r"(duplicate|repeated|more than once|reused|reuse|collision|conflict)"
    r".{0,240}\b12\b"
    r"|\b12\b.{0,240}"
    r"(duplicate|repeated|more than once|reused|reuse|collision|conflict)",
    re.IGNORECASE | re.DOTALL,
)


def load_extractor_namespace(skill_path=None):
    """Execute the skill implementation fence and return its namespace."""
    path = Path(skill_path) if skill_path else SKILL_PATH
    text = path.read_text(encoding="utf-8")
    fences = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    implementation = next(
        block for block in fences if "class ContractClauseExtractor" in block
    )
    namespace = {}
    exec(compile(implementation, str(path), "exec"), namespace)
    return namespace


def _clauses(extractor, contract_text):
    try:
        return extractor.extract_clauses(contract_text)
    except Exception as exc:
        raise AssertionError(
            f"extract_clauses raised {type(exc).__name__}: {exc}"
        ) from exc


def _summary(extractor, contract_text):
    try:
        return extractor.generate_summary(contract_text)
    except Exception as exc:
        raise AssertionError(
            f"generate_summary raised {type(exc).__name__}: {exc}"
        ) from exc


def _report(extractor, summary):
    try:
        return extractor.generate_report(summary)
    except Exception as exc:
        raise AssertionError(
            f"generate_report raised {type(exc).__name__}: {exc}"
        ) from exc


def _category(clause):
    category = clause.category
    return category.value if hasattr(category, "value") else str(category)


def _risk(clause):
    risk = clause.risk_level
    return risk.value if hasattr(risk, "value") else str(risk)


def _find(clauses, category, marker):
    matches = [
        clause
        for clause in clauses
        if _category(clause) == category and marker in clause.full_text
    ]
    return matches


class DuplicateSectionNumberTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_extractor_namespace()
        cls.extractor_cls = cls.ns["ContractClauseExtractor"]

    def setUp(self):
        self.extractor = self.extractor_cls()

    def test_extract_clauses_keeps_high_risk_text_when_later_heading_reuses_number(self):
        clauses = _clauses(self.extractor, ATTACK_CONTRACT)
        matches = _find(clauses, "indemnification", INDEMNITY_MARKER)
        self.assertTrue(
            matches,
            "Article 12 indemnification was dropped after Exhibit A reused section 12",
        )
        self.assertEqual(matches[0].article_number, "12")
        self.assertEqual(_risk(matches[0]), "high")
        self.assertNotIn(NOTICES_MARKER, matches[0].full_text)

    def test_extract_clauses_keeps_every_repeated_section(self):
        clauses = _clauses(self.extractor, REPEATED_CATEGORIES)
        expected = (
            ("indemnification", INDEMNITY_MARKER),
            ("payment", PAYMENT_MARKER),
            ("dispute", DISPUTE_MARKER),
        )
        for category, marker in expected:
            matches = _find(clauses, category, marker)
            self.assertEqual(
                len(matches),
                1,
                f"expected one {category} clause containing {marker!r}, got {len(matches)}",
            )
            self.assertEqual(matches[0].article_number, "12")
            for other in (INDEMNITY_MARKER, PAYMENT_MARKER, DISPUTE_MARKER):
                if other != marker:
                    self.assertNotIn(other, matches[0].full_text)

        indemnity = _find(clauses, "indemnification", INDEMNITY_MARKER)[0]
        self.assertEqual(_risk(indemnity), "high")

    def test_generate_summary_flags_clause_hidden_by_reused_number(self):
        summary = _summary(self.extractor, ATTACK_CONTRACT)
        grouped = summary.clauses_by_category.get("indemnification", [])
        self.assertTrue(
            any(INDEMNITY_MARKER in clause.full_text for clause in grouped),
            "summary clauses omit the indemnification body",
        )
        risk_values = " ".join(str(value) for value in summary.risk_assessment.values())
        self.assertRegex(risk_values, r"(?i)high risk")
        self.assertRegex(risk_values, r"(?i)indemnification")
        actions = "\n".join(summary.action_items)
        self.assertRegex(actions, r"(?i)review")
        self.assertRegex(actions, r"(?i)indemnification")
        self.assertRegex(actions, r"\b12\b")

    def test_generate_summary_flags_each_repeated_section(self):
        summary = _summary(self.extractor, REPEATED_CATEGORIES)
        for category, marker in (
            ("indemnification", INDEMNITY_MARKER),
            ("payment", PAYMENT_MARKER),
            ("dispute", DISPUTE_MARKER),
        ):
            grouped = summary.clauses_by_category.get(category, [])
            self.assertTrue(
                any(marker in clause.full_text for clause in grouped),
                f"summary is missing {category}",
            )
        actions = "\n".join(summary.action_items)
        self.assertRegex(actions, r"(?i)indemnification")
        risk_values = " ".join(str(value) for value in summary.risk_assessment.values())
        self.assertRegex(risk_values, r"(?i)high risk")

    def test_generate_report_lists_hidden_clause_and_warns_on_repeated_number(self):
        summary = _summary(self.extractor, ATTACK_CONTRACT)
        report = _report(self.extractor, summary)
        self.assertRegex(report, r"(?i)indemnification")
        self.assertRegex(report, r"(?i)high risk")
        self.assertRegex(report, r"(?i)review")
        self.assertRegex(report, r"\b12\b")
        self.assertRegex(
            report,
            DUPLICATE_WARNING,
            "report must warn that section 12 occurred more than once",
        )
        # The notices exhibit must not replace the indemnification body.
        indemnity = summary.clauses_by_category.get("indemnification", [])
        self.assertTrue(any(INDEMNITY_MARKER in clause.full_text for clause in indemnity))
        self.assertTrue(
            all(NOTICES_MARKER not in clause.full_text for clause in indemnity)
        )

    def test_generate_report_lists_every_repeated_section_and_warns(self):
        summary = _summary(self.extractor, REPEATED_CATEGORIES)
        report = _report(self.extractor, summary)
        for label in ("indemnification", "payment", "dispute"):
            self.assertRegex(report, rf"(?i){label}")
        self.assertRegex(report, r"(?i)high risk")
        self.assertRegex(report, DUPLICATE_WARNING)

    def test_untitled_heading_does_not_abort_and_keeps_surrounding_clauses(self):
        clauses = _clauses(self.extractor, UNTITLED_HEADING)
        self.assertTrue(
            _find(clauses, "payment", "Progress payments"),
            "payment clause before the untitled heading was lost",
        )
        indemnity = _find(clauses, "indemnification", INDEMNITY_MARKER)
        self.assertEqual(len(indemnity), 1)
        self.assertEqual(indemnity[0].article_number, "12")
        self.assertEqual(_risk(indemnity[0]), "high")

        summary = _summary(self.extractor, UNTITLED_HEADING)
        risk_values = " ".join(str(value) for value in summary.risk_assessment.values())
        self.assertRegex(risk_values, r"(?i)indemnification")
        report = _report(self.extractor, summary)
        self.assertRegex(report, r"(?i)indemnification")
        self.assertRegex(report, r"(?i)high risk")

    def test_distinct_section_numbers_still_extract_and_report(self):
        clauses = _clauses(self.extractor, CLEAN_CONTRACT)
        self.assertEqual(len(_find(clauses, "indemnification", INDEMNITY_MARKER)), 1)
        self.assertEqual(len(_find(clauses, "payment", PAYMENT_MARKER)), 1)
        indemnity = _find(clauses, "indemnification", INDEMNITY_MARKER)[0]
        self.assertEqual(_risk(indemnity), "high")
        self.assertEqual(indemnity.article_number, "12")

        summary = _summary(self.extractor, CLEAN_CONTRACT)
        report = _report(self.extractor, summary)
        self.assertRegex(report, r"(?i)indemnification")
        self.assertRegex(report, r"(?i)payment")
        self.assertRegex(report, r"(?i)high risk")
        self.assertIsNone(
            DUPLICATE_WARNING.search(report),
            "distinct article numbers must not be reported as duplicates",
        )


if __name__ == "__main__":
    unittest.main()
