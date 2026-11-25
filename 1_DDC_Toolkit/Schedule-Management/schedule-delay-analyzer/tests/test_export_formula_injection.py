"""Delay workbook cells must keep activity names and descriptions as text.

``ScheduleDelayAnalyzer.export_analysis`` writes those fields with
``DataFrame.to_excel``. These tests load the class from ``SKILL.md`` and
export delays whose names or descriptions start with ``=``. The workbook
must store that text as text. A cell that is an Excel formula fails the test,
because a spreadsheet application evaluates it when the file is opened.

The other leading markers ``+``, ``-``, and ``@`` are already written as
inline strings. Those cases are included so a change cannot turn them into
formulas. ``documentation`` is not written by ``export_analysis`` and is not
covered here.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

import openpyxl

SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
SPREADSHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

# Strings a delay notice can carry. Each one starts with "=" so a spreadsheet
# would treat it as a formula if the cell is stored as a formula.
EQUALS_PAYLOADS = (
    "=2+2",
    "=SUM(1,1)",
    "=I2",
    "=G2*1000",
)


def _load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    namespace = {}
    exec(compile(text[start:end], str(SKILL_PATH), "exec"), namespace)
    return namespace


class DelayExportFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = _load_skill_namespace()
        cls.analyzer_cls = ns["ScheduleDelayAnalyzer"]
        cls.delay_type = ns["DelayType"]
        cls.delay_cause = ns["DelayCause"]

    def _new_analyzer(self):
        return self.analyzer_cls("Office Tower", date(2024, 12, 31))

    def _record(self, analyzer, activity_name, description, activity_id="A-300"):
        return analyzer.record_delay(
            activity_id=activity_id,
            activity_name=activity_name,
            delay_type=self.delay_type.EXCUSABLE_COMPENSABLE,
            cause=self.delay_cause.OWNER_CHANGE,
            start_date=date(2024, 3, 1),
            end_date=date(2024, 3, 15),
            on_critical_path=True,
            description=description,
            cost_impact=50000,
        )

    def _export(self, analyzer):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "delay_analysis.xlsx"
        analyzer.export_analysis(str(path))
        self.assertTrue(path.is_file(), "export_analysis did not write a workbook")
        return path

    def test_leading_equals_in_activity_name_is_stored_as_text(self):
        analyzer = self._new_analyzer()
        for index, payload in enumerate(EQUALS_PAYLOADS, start=1):
            self._record(
                analyzer,
                activity_name=payload,
                description="Owner directed extra foundation work",
                activity_id=f"A-{index}",
            )

        path = self._export(analyzer)
        self._assert_column_is_literal_text(path, "Activity", list(EQUALS_PAYLOADS))
        self._assert_column_is_literal_text(
            path,
            "Description",
            ["Owner directed extra foundation work"] * len(EQUALS_PAYLOADS),
        )

    def test_leading_equals_in_description_is_stored_as_text(self):
        analyzer = self._new_analyzer()
        for index, payload in enumerate(EQUALS_PAYLOADS, start=1):
            self._record(
                analyzer,
                activity_name="Foundation Work",
                description=payload,
                activity_id=f"A-{index}",
            )

        path = self._export(analyzer)
        self._assert_column_is_literal_text(
            path, "Activity", ["Foundation Work"] * len(EQUALS_PAYLOADS)
        )
        self._assert_column_is_literal_text(path, "Description", list(EQUALS_PAYLOADS))

    def test_leading_equals_in_both_fields_of_one_delay_is_stored_as_text(self):
        analyzer = self._new_analyzer()
        self._record(
            analyzer,
            activity_name="=2+2",
            description="=SUM(1,1)",
            activity_id="A-1",
        )
        self._record(
            analyzer,
            activity_name="Foundation Work",
            description="Owner requested additional scope",
            activity_id="A-2",
        )

        path = self._export(analyzer)
        self._assert_column_is_literal_text(
            path, "Activity", ["=2+2", "Foundation Work"]
        )
        self._assert_column_is_literal_text(
            path,
            "Description",
            ["=SUM(1,1)", "Owner requested additional scope"],
        )

    def test_other_leading_markers_stay_literal_text(self):
        payloads = ("+2+2", "-2+2", "@SUM(1,1)", "\t=2+2", " =2+2")
        analyzer = self._new_analyzer()
        for index, payload in enumerate(payloads, start=1):
            self._record(
                analyzer,
                activity_name=payload,
                description=payload,
                activity_id=f"A-{index}",
            )

        path = self._export(analyzer)
        self._assert_column_is_literal_text(path, "Activity", list(payloads))
        self._assert_column_is_literal_text(path, "Description", list(payloads))

    def _assert_column_is_literal_text(self, path, header, expected_values):
        workbook = openpyxl.load_workbook(path)
        worksheet = workbook.active
        columns = {cell.value: cell.column for cell in worksheet[1]}
        self.assertIn(header, columns, f"workbook is missing the {header} column")
        column = columns[header]
        letter = openpyxl.utils.get_column_letter(column)
        formulas = _formula_cells(path)

        self.assertEqual(
            worksheet.max_row,
            len(expected_values) + 1,
            "unexpected number of delay rows",
        )
        for offset, original in enumerate(expected_values, start=2):
            cell = worksheet.cell(row=offset, column=column)
            ref = f"{letter}{offset}"
            self.assertNotEqual(
                cell.data_type,
                "f",
                f"{header} cell {ref} was stored as a formula ({cell.value!r}). "
                "A spreadsheet evaluates that formula when the workbook is opened.",
            )
            self.assertNotIn(
                ref,
                formulas,
                f"{header} cell {ref} contains a formula element {formulas.get(ref)!r}",
            )
            self.assertIsInstance(cell.value, str)
            self.assertIn(
                original,
                cell.value,
                f"{header} cell {ref} dropped the delay text {original!r} "
                f"(stored {cell.value!r})",
            )


def _formula_cells(path):
    with zipfile.ZipFile(path) as workbook:
        xml = workbook.read("xl/worksheets/sheet1.xml")
    root = ElementTree.fromstring(xml)
    found = {}
    for cell in root.findall("m:sheetData/m:row/m:c", SPREADSHEET_NS):
        formula = cell.find("m:f", SPREADSHEET_NS)
        if formula is not None:
            found[cell.attrib.get("r")] = formula.text
    return found


if __name__ == "__main__":
    unittest.main()
