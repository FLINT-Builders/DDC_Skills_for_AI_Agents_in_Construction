"""Risk scoring must see clause language that the heading split currently swallows.

The contract clause analyzer in
``1_DDC_Toolkit/Document-Control/contract-clause-analyzer/SKILL.md`` is loaded
from that file and exercised through ``analyze_text``, ``get_high_risk_clauses``,
and ``export_analysis``. These tests fail while a numbered clause's opening
sentence is stored as an unscored title, while text before the first numbered
heading is dropped, and while the export cuts that title to 50 characters.
"""

from __future__ import annotations

import math
import re
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "1_DDC_Toolkit" / "Document-Control" / "contract-clause-analyzer" / "SKILL.md"

# The counterparty's clause, as a single numbered paragraph with no separate heading.
ATTACK = (
    "14.2 Subcontractor waives all claims for consequential damages and shall indemnify "
    "and hold harmless Owner at Owner's sole discretion. Notices are sent to the address above."
)

# Same liability sentence, but the remainder contains one medium keyword ("shall").
ATTACK_WITH_LOW_REMAINDER = (
    "14.2 Subcontractor waives all claims for consequential damages and shall indemnify "
    "and hold harmless Owner at Owner's sole discretion. The contractor shall send notices."
)

# Opening sentence also carries the amounts, deadlines, obligations, and quoted terms
# that the extractors read from the body only.
RICH_ATTACK = (
    "14.2 The Contractor shall indemnify and hold harmless Owner at Owner's sole discretion, "
    'waive consequential damages, and pay liquidated damages of $250,000 within 14 days '
    'under the "Delay Waiver". Notices are sent to the address above.'
)

ONE_SENTENCE = (
    "14.2 Subcontractor waives all claims for consequential damages and shall indemnify "
    "and hold harmless Owner at Owner's sole discretion."
)

NEWLINE_CLAUSE = (
    "14.2 Indemnity\n"
    "Subcontractor shall indemnify and hold harmless Owner at Owner's sole discretion.\n"
)

PREAMBLE = (
    "The Subcontractor shall indemnify and hold harmless the Owner at the Owner's sole discretion.\n"
    "1.1 General. Work includes site clearing."
)

NESTED_NUMBER = (
    "14.2.1 Subcontractor waives all claims for consequential damages and shall indemnify "
    "and hold harmless Owner at Owner's sole discretion. Notices are sent to the address above."
)

SEPARATED_HEADING = (
    "15.1 Indemnification. The Contractor shall indemnify and hold harmless the Owner "
    "at the Owner's sole discretion."
)

UNNUMBERED = (
    "The Contractor shall indemnify and hold harmless the Owner at the Owner's sole discretion."
)

NEIGHBORS = (
    "3.1 Site Access. The contractor shall provide a fence.\n\n" + ATTACK
)

def _load_skill():
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    implementation = next(
        block for block in blocks if "class ContractClauseAnalyzer" in block
    )
    module = types.ModuleType("contract_clause_analyzer_skill")
    module.__file__ = str(SKILL)
    sys.modules[module.__name__] = module
    exec(compile(implementation, str(SKILL), "exec"), module.__dict__)
    return module.__dict__


SKILL_NS = _load_skill()
ContractClauseAnalyzer = SKILL_NS["ContractClauseAnalyzer"]
RiskLevel = SKILL_NS["RiskLevel"]
ClauseType = SKILL_NS["ClauseType"]


def _analyze(contract_text: str):
    analyzer = ContractClauseAnalyzer()
    result = analyzer.analyze_text("Construction Contract", contract_text)
    return analyzer, result


def _combined(clause) -> str:
    return f"{clause.title}\n{clause.text}".lower()


def _clause_containing(result, marker: str):
    marker = marker.lower()
    matches = [clause for clause in result.clauses if marker in _combined(clause)]
    assert matches, (
        f"no analyzed clause kept {marker!r}; "
        f"got {[(clause.title, clause.text, clause.risk_level) for clause in result.clauses]!r}"
    )
    return matches[0]


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value)


def test_opening_sentence_of_numbered_clause_is_high_risk():
    _analyzer, result = _analyze(ATTACK)
    clause = _clause_containing(result, "sole discretion")
    assert clause.risk_level == RiskLevel.HIGH


def test_opening_sentence_followed_by_shall_is_not_downgraded_to_low():
    _analyzer, result = _analyze(ATTACK_WITH_LOW_REMAINDER)
    clause = _clause_containing(result, "sole discretion")
    assert clause.risk_level == RiskLevel.HIGH
    assert result.risk_summary["high"] >= 1


def test_opening_sentence_is_classified_as_indemnification():
    _analyzer, result = _analyze(ATTACK)
    clause = _clause_containing(result, "indemnify")
    assert clause.clause_type == ClauseType.INDEMNIFICATION


def test_opening_sentence_is_listed_in_high_risk_clauses():
    analyzer, _result = _analyze(ATTACK)
    high = analyzer.get_high_risk_clauses()
    assert any("sole discretion" in _combined(clause) for clause in high)


def test_risk_summary_counts_the_opening_sentence():
    _analyzer, result = _analyze(ATTACK)
    assert result.risk_summary["high"] >= 1


def test_single_sentence_numbered_clause_is_scored_from_its_own_words():
    _analyzer, result = _analyze(ONE_SENTENCE)
    clause = _clause_containing(result, "indemnify")
    assert clause.risk_level == RiskLevel.HIGH
    assert clause.clause_type == ClauseType.INDEMNIFICATION


def test_nested_clause_number_opening_sentence_is_high_risk():
    analyzer, result = _analyze(NESTED_NUMBER)
    clause = _clause_containing(result, "sole discretion")
    assert clause.risk_level == RiskLevel.HIGH
    assert any(clause.risk_level == RiskLevel.HIGH for clause in analyzer.get_high_risk_clauses())


def test_body_on_the_next_line_is_scored_with_the_heading():
    _analyzer, result = _analyze(NEWLINE_CLAUSE)
    clause = _clause_containing(result, "indemnify")
    assert clause.risk_level == RiskLevel.HIGH
    assert clause.clause_type == ClauseType.INDEMNIFICATION


def test_text_before_the_first_numbered_heading_is_kept_and_scored():
    _analyzer, result = _analyze(PREAMBLE)
    clause = _clause_containing(result, "indemnify and hold harmless")
    assert clause.risk_level == RiskLevel.HIGH
    assert result.risk_summary["high"] >= 1


@pytest.mark.parametrize(
    "phrase",
    [
        "waive",
        "forfeit",
        "no limit",
        "unlimited liability",
        "liquidated damages",
        "consequential",
        "sole discretion",
        "hold harmless",
        "indemnify",
    ],
)
def test_single_high_risk_phrase_in_the_opening_sentence_is_not_informational(phrase):
    contract = (
        f"9.4 The Contractor agrees to {phrase} as written. "
        "Notices are sent to the address above."
    )
    _analyzer, result = _analyze(contract)
    clause = _clause_containing(result, phrase)
    assert clause.risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH}


def test_amounts_in_the_opening_sentence_are_extracted():
    _analyzer, result = _analyze(RICH_ATTACK)
    clause = _clause_containing(result, "250,000")
    assert any("250,000" in amount for amount in clause.amounts)


def test_deadlines_in_the_opening_sentence_are_extracted():
    _analyzer, result = _analyze(RICH_ATTACK)
    clause = _clause_containing(result, "within 14 days")
    assert any("within 14 days" in deadline.lower() for deadline in clause.deadlines)


def test_obligations_in_the_opening_sentence_are_extracted():
    _analyzer, result = _analyze(RICH_ATTACK)
    clause = _clause_containing(result, "indemnify")
    assert any("indemnify" in obligation.lower() for obligation in clause.obligations)


def test_quoted_key_terms_in_the_opening_sentence_are_extracted():
    _analyzer, result = _analyze(RICH_ATTACK)
    clause = _clause_containing(result, "Delay Waiver")
    assert "Delay Waiver" in clause.key_terms


def test_neighboring_ordinary_clause_stays_low():
    analyzer, result = _analyze(NEIGHBORS)
    ordinary = _clause_containing(result, "fence")
    risky = _clause_containing(result, "sole discretion")
    assert ordinary.risk_level == RiskLevel.LOW
    assert risky.risk_level == RiskLevel.HIGH
    assert len(analyzer.get_high_risk_clauses()) == 1


def test_export_reports_the_opening_sentence_as_high_risk(tmp_path):
    analyzer, result = _analyze(RICH_ATTACK)
    output = tmp_path / "analysis.xlsx"
    analyzer.export_analysis(result, str(output))

    summary = pd.read_excel(output, sheet_name="Summary")
    clauses = pd.read_excel(output, sheet_name="Clauses")
    assert int(summary.loc[0, "High Risk"]) >= 1

    row = clauses.iloc[0]
    assert _cell(row["Risk"]) == "high"
    assert _cell(row["Type"]) == ClauseType.INDEMNIFICATION.value
    assert "250,000" in _cell(row["Amounts"])
    assert "within 14 days" in _cell(row["Dates"]).lower()

    clause = result.clauses[0]
    exported_title = _cell(row["Title"])
    # The spreadsheet writes title[:50]. Any tail cut off there has to remain in
    # the clause body; otherwise the opening sentence is shortened to 50 characters
    # and nowhere else in the export.
    if exported_title != clause.title:
        omitted = clause.title[len(exported_title):]
        assert omitted.lower() in clause.text.lower()


def test_separated_heading_still_flags_the_body():
    analyzer, result = _analyze(SEPARATED_HEADING)
    clause = _clause_containing(result, "indemnify")
    assert clause.risk_level == RiskLevel.HIGH
    assert clause.clause_type == ClauseType.INDEMNIFICATION
    assert analyzer.get_high_risk_clauses()


def test_unnumbered_text_is_still_scored():
    _analyzer, result = _analyze(UNNUMBERED)
    clause = _clause_containing(result, "indemnify")
    assert clause.risk_level == RiskLevel.HIGH
    assert clause.clause_type == ClauseType.INDEMNIFICATION
