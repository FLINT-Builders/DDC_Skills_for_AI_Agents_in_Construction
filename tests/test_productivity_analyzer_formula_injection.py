"""Spreadsheet formula injection in the productivity analyzer export.

These tests load the implementation embedded in the productivity-analyzer
skill and run the real import and export path. They fail while description,
activity_code, unit, or conditions that start with "=" are written into the
workbook as formulas. They pass once every Excel write stores those strings
as text, including rows that arrived through import_from_dataframe.

Run from the repository root:

    python3 -m unittest tests.test_productivity_analyzer_formula_injection
"""

from __future__ import annotations

import re
import unittest
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "Analytics"
    / "productivity-analyzer"
    / "SKILL.md"
)

# Distinct bodies so each field can be found after a fix that keeps the text.
DESCRIPTION = "=100+1"
ACTIVITY_CODE = "=200+2"
UNIT = "=300+3"
CONDITIONS = "=400+4"
PAYLOADS = (DESCRIPTION, ACTIVITY_CODE, UNIT, CONDITIONS)


def load_productivity_analyzer():
    """Execute the skill's implementation block and return the class."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.S)
    if match is None:
        raise AssertionError(f"No Python implementation block in {SKILL_PATH}")
    namespace: dict = {}
    exec(compile(match.group(1), str(SKILL_PATH), "exec"), namespace)
    try:
        return namespace["ProductivityAnalyzer"]
    except KeyError as exc:
        raise AssertionError(
            "ProductivityAnalyzer is not defined by the skill implementation"
        ) from exc


def formula_elements(xlsx_path: Path) -> list[str]:
    """Return every formula element stored in the workbook's worksheets."""
    found: list[str] = []
    with zipfile.ZipFile(xlsx_path) as workbook:
        for name in workbook.namelist():
            if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                continue
            xml = workbook.read(name).decode("utf-8")
            found.extend(re.findall(r"<f\b[^>]*>.*?</f>", xml))
    return found


class ProductivityFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analyzer_cls = load_productivity_analyzer()

    def setUp(self):
        self.output = Path(self.id().replace(".", "_") + ".xlsx")

    def tearDown(self):
        if self.output.exists():
            self.output.unlink()

    def _assert_workbook_has_no_formulas(self, xlsx_path: Path):
        workbook = load_workbook(xlsx_path)
        formula_cells = []
        texts = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    texts.append(str(cell.value))
                    if cell.data_type == "f" or (
                        isinstance(cell.value, str) and cell.value.startswith("=")
                    ):
                        formula_cells.append(
                            f"{sheet.title}!{cell.coordinate} "
                            f"type={cell.data_type!r} value={cell.value!r}"
                        )
        stored_formulas = formula_elements(xlsx_path)
        self.assertEqual(
            formula_cells,
            [],
            "user-supplied activity text was stored as an Excel formula:\n"
            + "\n".join(formula_cells),
        )
        self.assertEqual(
            stored_formulas,
            [],
            "worksheet XML contains formula elements:\n" + "\n".join(stored_formulas),
        )
        for payload in PAYLOADS:
            body = payload[1:]
            self.assertTrue(
                any(payload in text or body in text for text in texts),
                f"{payload!r} was dropped instead of written as text",
            )
            for text in texts:
                if payload in text or body in text:
                    self.assertFalse(
                        text.startswith("="),
                        f"{payload!r} is still formula text in {text!r}",
                    )

    def test_add_record_export_stores_activity_text_as_text(self):
        analyzer = self.analyzer_cls()
        analyzer.add_record(
            date=date(2026, 1, 1),
            activity_code=ACTIVITY_CODE,
            description=DESCRIPTION,
            planned_output=100,
            actual_output=10,
            unit=UNIT,
            manhours=8,
            crew_size=2,
            conditions=CONDITIONS,
        )
        analyzer.export_analysis(str(self.output))
        self._assert_workbook_has_no_formulas(self.output)

        workbook = load_workbook(self.output)
        summary = workbook["Summary"]
        self.assertEqual(summary["F2"].value, 10)
        self.assertNotEqual(summary["F2"].data_type, "f")
        self.assertIn("Problem Areas", workbook.sheetnames)
        self.assertIn("Raw Data", workbook.sheetnames)

    def test_import_from_dataframe_export_stores_activity_text_as_text(self):
        frame = pd.DataFrame(
            [
                {
                    "date": "2026-01-02",
                    "activity_code": ACTIVITY_CODE,
                    "description": DESCRIPTION,
                    "planned_output": 100,
                    "actual_output": 40,
                    "unit": UNIT,
                    "manhours": 8,
                    "crew_size": 3,
                    "conditions": CONDITIONS,
                }
            ]
        )
        analyzer = self.analyzer_cls()
        analyzer.import_from_dataframe(frame)
        analyzer.export_analysis(str(self.output))
        self._assert_workbook_has_no_formulas(self.output)

        workbook = load_workbook(self.output)
        summary = workbook["Summary"]
        self.assertEqual(summary["F2"].value, 40)
        self.assertNotEqual(summary["F2"].data_type, "f")

    def test_each_field_is_sanitized_on_its_own_sheet(self):
        """A fix that cleans only one column or one sheet still fails."""
        analyzer = self.analyzer_cls()
        analyzer.add_record(
            date=date(2026, 1, 3),
            activity_code="concrete_pour",
            description=DESCRIPTION,
            planned_output=20,
            actual_output=18,
            unit="m3",
            manhours=40,
            crew_size=5,
            conditions="normal",
        )
        analyzer.add_record(
            date=date(2026, 1, 4),
            activity_code=ACTIVITY_CODE,
            description="Rebar cage",
            planned_output=100,
            actual_output=50,
            unit="kg",
            manhours=10,
            crew_size=4,
            conditions="normal",
        )
        analyzer.add_record(
            date=date(2026, 1, 5),
            activity_code="formwork",
            description="Wall forms",
            planned_output=80,
            actual_output=70,
            unit=UNIT,
            manhours=12,
            crew_size=3,
            conditions="normal",
        )
        analyzer.add_record(
            date=date(2026, 1, 6),
            activity_code="painting",
            description="Interior paint",
            planned_output=30,
            actual_output=28,
            unit="m2",
            manhours=6,
            crew_size=2,
            conditions=CONDITIONS,
        )
        analyzer.export_analysis(str(self.output))
        self._assert_workbook_has_no_formulas(self.output)

        workbook = load_workbook(self.output)
        summary_text = {
            (cell.coordinate, str(cell.value))
            for row in workbook["Summary"].iter_rows()
            for cell in row
            if isinstance(cell.value, str)
        }
        raw_text = {
            str(cell.value)
            for row in workbook["Raw Data"].iter_rows()
            for cell in row
            if isinstance(cell.value, str)
        }
        problem_text = {
            str(cell.value)
            for row in workbook["Problem Areas"].iter_rows()
            for cell in row
            if isinstance(cell.value, str)
        }
        self.assertTrue(
            any(DESCRIPTION[1:] in value for _, value in summary_text),
            f"description was not written to Summary: {summary_text}",
        )
        self.assertTrue(
            any(ACTIVITY_CODE[1:] in value for value in raw_text),
            "activity_code was not written to Raw Data",
        )
        self.assertTrue(
            any(UNIT[1:] in value for value in raw_text),
            "unit was not written to Raw Data",
        )
        self.assertTrue(
            any(CONDITIONS[1:] in value for value in raw_text),
            "conditions were not written to Raw Data",
        )
        self.assertTrue(
            any(ACTIVITY_CODE[1:] in value for value in problem_text),
            "activity_code was not written to Problem Areas",
        )


if __name__ == "__main__":
    unittest.main()
