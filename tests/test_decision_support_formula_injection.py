"""Spreadsheet formula injection in the decision-support Excel export.

These tests exercise DecisionSupportSystem.export_to_excel from the skill
implementation. They fail while caller-supplied names that start with "=" are
written into the workbook as formulas, and they pass once those values are
forced to text before write.

Run from the repository root:

    python3 -m unittest discover -s tests -p "test_decision_support_formula_injection.py" -v
"""

from __future__ import annotations

import html
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import openpyxl

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "2_DDC_Book"
    / "4.1-Analytics-KPI-Dashboard"
    / "decision-support"
    / "SKILL.md"
)

# Distinct arithmetic payloads. A spreadsheet that stores them as formulas
# evaluates them (LibreOffice Calc turns "=200+7" into 207). The body after
# the leading "=" is what OOXML stores inside <f>.
PROJECT_NAME = "=100+7"
WINNING_ALTERNATIVE = "=200+7"
LOSING_ALTERNATIVE = "=300+7"
CRITERION_NAME = "=400+7"
CRITERION_UNIT = "=500+7"
# Column headers are the criterion name plus " (Raw)" / " (Norm)". Closing
# the trailing quote keeps both headers a formula the sheet will evaluate.
HEADER_CRITERION_NAME = '=600+7&"'
HEADER_CRITERION_UNIT = "score"

SAFE_PROJECT = "Office Building A"
SAFE_WINNER = "Contractor A"
SAFE_LOSER = "Contractor B"


def load_skill_namespace():
    """Execute the implementation block published in the skill."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(text[start:end], str(SKILL_PATH), "exec"), namespace)
    return namespace


def formula_bodies(xlsx_path: Path) -> list[str]:
    """Return every formula body stored in the workbook XML."""
    bodies = []
    with zipfile.ZipFile(xlsx_path) as workbook:
        for name in workbook.namelist():
            if not name.endswith(".xml"):
                continue
            xml = workbook.read(name).decode("utf-8")
            for raw in re.findall(r"<f(?:\s[^>]*)?>(.*?)</f>", xml):
                bodies.append(html.unescape(raw))
    return bodies


class DecisionExportFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = load_skill_namespace()
        cls.DecisionSupportSystem = ns["DecisionSupportSystem"]
        cls.Alternative = ns["Alternative"]
        cls.Criterion = ns["Criterion"]
        cls.CriterionType = ns["CriterionType"]

    def export(self, system) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "decision_analysis.xlsx"
        system.export_to_excel(str(path))
        return path

    def build_system(self, project, winner, loser, criterion, unit, header_criterion, header_unit):
        system = self.DecisionSupportSystem(project)
        system.add_criterion(
            self.Criterion(
                "cost",
                criterion,
                self.CriterionType.COST,
                0.5,
                False,
                unit,
            )
        )
        system.add_criterion(
            self.Criterion(
                "quality",
                header_criterion,
                self.CriterionType.QUALITY,
                0.5,
                True,
                header_unit,
            )
        )
        system.add_alternative(
            self.Alternative(
                "win",
                winner,
                "low cost, high quality bid",
                {"cost": 1, "quality": 9},
            )
        )
        system.add_alternative(
            self.Alternative(
                "lose",
                loser,
                "high cost, low quality bid",
                {"cost": 9, "quality": 1},
            )
        )
        return system

    def assert_no_formula_elements(self, path: Path):
        bodies = formula_bodies(path)
        self.assertEqual(
            bodies,
            [],
            f"workbook contains formula cells that a spreadsheet will evaluate: {bodies}",
        )

    def assert_payload_is_text(self, worksheet, payload: str):
        matches = []
        for row in worksheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and payload in cell.value:
                    matches.append(cell)
        self.assertTrue(
            matches,
            f"{payload!r} was not written to sheet {worksheet.title!r}",
        )
        for cell in matches:
            self.assertNotEqual(
                cell.data_type,
                "f",
                f"{worksheet.title}!{cell.coordinate} stores {cell.value!r} as a formula",
            )
            self.assertFalse(
                cell.value.startswith("="),
                f"{worksheet.title}!{cell.coordinate} starts with a formula marker: {cell.value!r}",
            )

    def test_formula_names_are_stored_as_text_on_every_exported_sheet(self):
        system = self.build_system(
            PROJECT_NAME,
            WINNING_ALTERNATIVE,
            LOSING_ALTERNATIVE,
            CRITERION_NAME,
            CRITERION_UNIT,
            HEADER_CRITERION_NAME,
            HEADER_CRITERION_UNIT,
        )
        path = self.export(system)
        self.assert_no_formula_elements(path)

        workbook = openpyxl.load_workbook(path)
        recommendation = workbook["Recommendation"]
        rankings = workbook["Rankings"]
        details = workbook["Detailed Scores"]
        criteria = workbook["Criteria"]

        self.assert_payload_is_text(recommendation, PROJECT_NAME)
        self.assert_payload_is_text(recommendation, WINNING_ALTERNATIVE)
        self.assert_payload_is_text(rankings, WINNING_ALTERNATIVE)
        self.assert_payload_is_text(rankings, LOSING_ALTERNATIVE)
        self.assert_payload_is_text(details, WINNING_ALTERNATIVE)
        self.assert_payload_is_text(details, LOSING_ALTERNATIVE)
        self.assert_payload_is_text(details, CRITERION_NAME)
        self.assert_payload_is_text(details, HEADER_CRITERION_NAME)
        self.assert_payload_is_text(criteria, CRITERION_NAME)
        self.assert_payload_is_text(criteria, HEADER_CRITERION_NAME)
        self.assert_payload_is_text(criteria, CRITERION_UNIT)

    def test_safe_names_are_exported_unchanged(self):
        system = self.build_system(
            SAFE_PROJECT,
            SAFE_WINNER,
            SAFE_LOSER,
            "Total Cost",
            "$",
            "Quality",
            "score",
        )
        path = self.export(system)
        self.assert_no_formula_elements(path)

        workbook = openpyxl.load_workbook(path)
        recommendation = workbook["Recommendation"]
        values = [cell.value for row in recommendation.iter_rows() for cell in row]
        self.assertIn(SAFE_PROJECT, values)
        self.assertIn(SAFE_WINNER, values)
        rankings = [
            cell.value
            for row in workbook["Rankings"].iter_rows()
            for cell in row
        ]
        self.assertIn(SAFE_LOSER, rankings)


if __name__ == "__main__":
    unittest.main()
