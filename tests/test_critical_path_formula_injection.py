"""Schedule export must store activity names and ids as text, not formulas.

The critical path analyzer copies imported activity names and ids onto the
Schedule and Critical Path sheets. These tests load that implementation from
the skill and run import plus export. They fail while a value that starts
with '=' is written through pandas and openpyxl, which stores that value as
an Excel formula. They pass once every such value is written as text and the
original characters are still present (a single leading apostrophe, space, or
tab is accepted as a text marker).

Not covered here:
- suggest_acceleration returns a name in a dict and does not write a workbook.
- schedule-delay-analyzer writes activity names through the same Excel writer,
  but it is a different skill from the one these tests pin.
"""

from __future__ import annotations

import re
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "1_DDC_Toolkit"
    / "Schedule-Management"
    / "critical-path-analyzer"
    / "SKILL.md"
)

# A single leading text marker is the usual way to keep a formula-like
# string visible without letting the spreadsheet evaluate it.
_TEXT_MARKERS = ("'", " ", "\t")


def load_analyzer_class():
    """Execute the implementation block published in the skill."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    implementation = next(
        block for block in blocks if "class CriticalPathAnalyzer" in block
    )
    namespace: dict = {}
    exec(compile(implementation, str(SKILL_PATH), "exec"), namespace)
    return namespace["CriticalPathAnalyzer"]


def formula_elements(workbook_path: Path) -> list[str]:
    """Return every formula element in the workbook Excel will open."""
    found = []
    with zipfile.ZipFile(workbook_path) as archive:
        for name in archive.namelist():
            if not name.startswith("xl/worksheets/"):
                continue
            xml = archive.read(name).decode("utf-8")
            found.extend(re.findall(r"<f(?:\s[^>]*)?>.*?</f>", xml))
    return found


def comparable_text(value) -> str:
    text = "" if value is None else str(value)
    if text[:1] in _TEXT_MARKERS:
        return text[1:]
    return text


def column_cells(worksheet, header: str):
    headers = [cell.value for cell in next(worksheet.iter_rows(min_row=1, max_row=1))]
    index = headers.index(header)
    for row in worksheet.iter_rows(min_row=2):
        yield row[index]


def cell_matching(worksheet, header: str, original: str):
    matches = [
        cell
        for cell in column_cells(worksheet, header)
        if comparable_text(cell.value) == original or str(cell.value) == original
    ]
    return matches


def assert_stored_as_text(testcase: unittest.TestCase, cell, original: str) -> None:
    testcase.assertNotEqual(
        cell.data_type,
        "f",
        f"{cell.parent.title}!{cell.coordinate} stored {cell.value!r} as a formula",
    )
    testcase.assertEqual(
        comparable_text(cell.value),
        original,
        f"{cell.parent.title}!{cell.coordinate} should keep the activity text",
    )


class CriticalPathFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analyzer_cls = load_analyzer_class()

    def export(self, analyzer) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = Path(directory.name) / "critical_path.xlsx"
        analyzer.export_analysis(str(output))
        return output

    def open_sheets(self, workbook_path: Path):
        workbook = load_workbook(workbook_path, data_only=False)
        self.addCleanup(workbook.close)
        return workbook["Schedule"], workbook["Critical Path"]

    def test_imported_formula_name_and_id_are_text_on_both_sheets(self):
        """A subcontractor id and name that start with '=' stay text after import."""
        formula_id = "=2+2"
        formula_name = "=1+1"
        frame = pd.DataFrame(
            [
                {
                    "activity_id": formula_id,
                    "name": formula_name,
                    "duration": 5,
                    "predecessors": "",
                },
                {
                    "activity_id": "C",
                    "name": "Closeout",
                    "duration": 1,
                    "predecessors": formula_id,
                },
            ]
        )
        analyzer = self.analyzer_cls(project_start=date(2024, 6, 1))
        analyzer.import_from_dataframe(frame)
        workbook_path = self.export(analyzer)
        schedule, critical = self.open_sheets(workbook_path)

        for sheet, header, original in (
            (schedule, "Activity ID", formula_id),
            (schedule, "Name", formula_name),
            (critical, "Activity", formula_id),
            (critical, "Name", formula_name),
        ):
            with self.subTest(sheet=sheet.title, column=header):
                matches = cell_matching(sheet, header, original)
                self.assertEqual(
                    len(matches),
                    1,
                    f"{sheet.title} column {header} should contain {original!r}",
                )
                assert_stored_as_text(self, matches[0], original)

        closeout = cell_matching(schedule, "Name", "Closeout")
        self.assertEqual(len(closeout), 1)
        self.assertEqual(closeout[0].value, "Closeout")
        self.assertNotEqual(closeout[0].data_type, "f")
        self.assertEqual(formula_elements(workbook_path), [])

    def test_noncritical_formula_name_is_text_on_schedule_sheet(self):
        """A name that is not on the critical path is still written to Schedule.

        '=C2' would display as the duration cell if the sheet evaluated it.
        """
        formula_name = "=C2"
        frame = pd.DataFrame(
            [
                {
                    "activity_id": "LONG",
                    "name": "Structure",
                    "duration": 10,
                    "predecessors": "",
                },
                {
                    "activity_id": "SHORT",
                    "name": formula_name,
                    "duration": 1,
                    "predecessors": "",
                },
            ]
        )
        analyzer = self.analyzer_cls(project_start=date(2024, 6, 1))
        analyzer.import_from_dataframe(frame)
        workbook_path = self.export(analyzer)
        schedule, critical = self.open_sheets(workbook_path)

        schedule_names = cell_matching(schedule, "Name", formula_name)
        self.assertEqual(len(schedule_names), 1)
        assert_stored_as_text(self, schedule_names[0], formula_name)

        # The same string must not appear as a formula if a later change
        # also copies non-critical activities onto the critical-path sheet.
        for cell in column_cells(critical, "Name"):
            if comparable_text(cell.value) == formula_name:
                assert_stored_as_text(self, cell, formula_name)

        structure = cell_matching(schedule, "Name", "Structure")
        self.assertEqual(len(structure), 1)
        self.assertEqual(structure[0].value, "Structure")
        self.assertEqual(formula_elements(workbook_path), [])

    def test_add_activity_formula_name_and_id_are_text_on_both_sheets(self):
        """add_activity stores the same unsanitized strings that import uses."""
        formula_id = "=A1"
        formula_name = "=SUM(1,1)"
        analyzer = self.analyzer_cls(project_start=date(2024, 6, 1))
        analyzer.add_activity(formula_id, formula_name, 3, [])
        workbook_path = self.export(analyzer)
        schedule, critical = self.open_sheets(workbook_path)

        for sheet, header, original in (
            (schedule, "Activity ID", formula_id),
            (schedule, "Name", formula_name),
            (critical, "Activity", formula_id),
            (critical, "Name", formula_name),
        ):
            with self.subTest(sheet=sheet.title, column=header):
                matches = cell_matching(sheet, header, original)
                self.assertEqual(len(matches), 1)
                assert_stored_as_text(self, matches[0], original)

        self.assertEqual(formula_elements(workbook_path), [])

    def test_other_formula_prefixes_stay_literal_text(self):
        """'+', '-', and '@' are not promoted to formulas by this writer.

        They must remain the original text. A fix aimed at '=' must not drop them.
        """
        samples = ("+1+1", "-1+1", "@SUM(A1)")
        frame = pd.DataFrame(
            [
                {
                    "activity_id": f"ID{index}",
                    "name": name,
                    "duration": 2,
                    "predecessors": "" if index == 0 else "ID0",
                }
                for index, name in enumerate(samples)
            ]
        )
        analyzer = self.analyzer_cls(project_start=date(2024, 6, 1))
        analyzer.import_from_dataframe(frame)
        workbook_path = self.export(analyzer)
        schedule, _critical = self.open_sheets(workbook_path)

        for name in samples:
            with self.subTest(name=name):
                matches = cell_matching(schedule, "Name", name)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0].value, name)
                self.assertNotEqual(matches[0].data_type, "f")
        self.assertEqual(formula_elements(workbook_path), [])


if __name__ == "__main__":
    unittest.main()
