"""Schedule fields must be stored as text when the Gantt skill exports Excel.

The implementation under test is the Python in
2_DDC_Book/3.3-4D-BIM-CO2-Simulation/gantt-chart/SKILL.md. These tests fail
while a task name, work-breakdown code, or id that starts with "=" is written
into the workbook as a formula. They pass once those values are stored as text
and the original characters are still in the cell.

Run from the repository root:

    python3 -m unittest tests.test_gantt_chart_formula_injection
"""

from __future__ import annotations

import csv
import io
import re
import sys
import tempfile
import types
import unittest
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd
from openpyxl import load_workbook

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "2_DDC_Book" / "3.3-4D-BIM-CO2-Simulation" / "gantt-chart" / "SKILL.md"

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# Distinct payloads so each column can be identified after export.
# Each one is a formula a spreadsheet will calculate if the cell type is formula.
IMPORTED_ID = '=3+3'
IMPORTED_WBS = '=2+2'
IMPORTED_NAME = '=1+1'
DIRECT_ID = '=11+1'
DIRECT_WBS = '=10+1'
DIRECT_NAME = '=HYPERLINK("http://example.com/schedule","Foundation")'
PREDECESSOR_ID = '=4+4'
SUCCESSOR_ID = '=5+5'
SAFE_NAME = "Foundation"
SAFE_PROGRESS = 40


def load_gantt_skill():
    """Execute the skill implementation shipped in SKILL.md."""
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    implementation = next(
        (block for block in blocks if "def export_to_excel" in block),
        None,
    )
    if implementation is None:
        raise AssertionError(f"{SKILL} has no export_to_excel implementation")
    module = types.ModuleType("gantt_skill")
    module.__file__ = str(SKILL)
    sys.modules["gantt_skill"] = module
    exec(compile(implementation, str(SKILL), "exec"), module.__dict__)
    return module.__dict__


def schedule_csv(rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "task_id",
            "name",
            "start_date",
            "end_date",
            "wbs_code",
            "progress",
            "level",
        ],
    )
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)
    return pd.read_csv(buffer)


def sheet_xml(workbook_path: Path) -> dict:
    """Map a sheet name to its worksheet XML bytes."""
    with zipfile.ZipFile(workbook_path) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in relationships.findall(f"{{{PKG_NS}}}Relationship")
        }
        sheets = {}
        for sheet in workbook.findall(f"{{{MAIN_NS}}}sheets/{{{MAIN_NS}}}sheet"):
            target = targets[sheet.attrib[f"{{{REL_NS}}}id"]].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            sheets[sheet.attrib["name"]] = archive.read(target)
    return sheets


def xml_cell_has_formula(sheet_bytes: bytes, coordinate: str) -> bool:
    root = ElementTree.fromstring(sheet_bytes)
    cell_tag = f"{{{MAIN_NS}}}c"
    formula_tag = f"{{{MAIN_NS}}}f"
    for cell in root.iter(cell_tag):
        if cell.attrib.get("r") == coordinate:
            return cell.find(formula_tag) is not None
    return False


def header_columns(worksheet):
    return {cell.value: cell.column for cell in next(worksheet.iter_rows(max_row=1))}


def column_cell(worksheet, header: str, row_index: int):
    return worksheet.cell(row_index, header_columns(worksheet)[header])


def assert_field_is_text(test, worksheet, sheet_bytes, header, row_index, original):
    """The cell must keep the original text and must not be a formula record."""
    cell = column_cell(worksheet, header, row_index)
    text = "" if cell.value is None else str(cell.value)
    test.assertFalse(
        cell.data_type == "f" or xml_cell_has_formula(sheet_bytes, cell.coordinate),
        f"{worksheet.title}!{cell.coordinate} ({header}) stored {original!r} "
        f"as a formula ({cell.data_type}, value {cell.value!r})",
    )
    test.assertTrue(
        text.endswith(original),
        f"{worksheet.title}!{cell.coordinate} ({header}) should keep {original!r} "
        f"as text, got {cell.value!r}",
    )
    return cell


class GanttFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        skill = load_gantt_skill()
        cls.GanttChartGenerator = skill["GanttChartGenerator"]
        cls.Task = skill["Task"]

    def export(self, chart) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "schedule.xlsx"
        chart.export_to_excel(str(path))
        return path

    def test_imported_schedule_fields_are_stored_as_text(self):
        """A schedule file's id, WBS, and name survive import and export as text."""
        frame = schedule_csv(
            [
                {
                    "task_id": IMPORTED_ID,
                    "name": IMPORTED_NAME,
                    "start_date": "2024-06-01",
                    "end_date": "2024-06-30",
                    "wbs_code": IMPORTED_WBS,
                    "progress": SAFE_PROGRESS,
                    "level": 0,
                },
                {
                    "task_id": "T-OK",
                    "name": SAFE_NAME,
                    "start_date": "2024-07-01",
                    "end_date": "2024-07-15",
                    "wbs_code": "01",
                    "progress": 0,
                    "level": 0,
                },
            ]
        )
        # The characters that pandas read are the attacker's field values.
        self.assertEqual(frame.loc[0, "name"], IMPORTED_NAME)
        self.assertEqual(frame.loc[0, "task_id"], IMPORTED_ID)
        self.assertEqual(frame.loc[0, "wbs_code"], IMPORTED_WBS)

        chart = self.GanttChartGenerator("Office Building A")
        chart.import_from_df(frame)
        path = self.export(chart)

        workbook = load_workbook(path)
        tasks = workbook["Tasks"]
        xml = sheet_xml(path)["Tasks"]
        with self.subTest(field="ID"):
            assert_field_is_text(self, tasks, xml, "ID", 2, IMPORTED_ID)
        with self.subTest(field="WBS"):
            assert_field_is_text(self, tasks, xml, "WBS", 2, IMPORTED_WBS)
        with self.subTest(field="Name"):
            assert_field_is_text(self, tasks, xml, "Name", 2, IMPORTED_NAME)
        with self.subTest(field="safe name"):
            assert_field_is_text(self, tasks, xml, "Name", 3, SAFE_NAME)
        # Dates and progress are still on the sheet next to the injected name.
        self.assertEqual(column_cell(tasks, "Progress", 2).value, SAFE_PROGRESS)
        self.assertIn("Start", header_columns(tasks))
        self.assertIn("End", header_columns(tasks))

    def test_task_constructor_fields_are_stored_as_text(self):
        """add_task writes the same Tasks columns as import_from_df."""
        chart = self.GanttChartGenerator("Office Building A")
        chart.add_task(
            self.Task(
                DIRECT_ID,
                DIRECT_NAME,
                date(2024, 6, 1),
                date(2024, 6, 30),
                wbs_code=DIRECT_WBS,
                progress=SAFE_PROGRESS,
            )
        )
        path = self.export(chart)
        workbook = load_workbook(path)
        tasks = workbook["Tasks"]
        xml = sheet_xml(path)["Tasks"]
        with self.subTest(field="ID"):
            assert_field_is_text(self, tasks, xml, "ID", 2, DIRECT_ID)
        with self.subTest(field="WBS"):
            assert_field_is_text(self, tasks, xml, "WBS", 2, DIRECT_WBS)
        with self.subTest(field="Name"):
            assert_field_is_text(self, tasks, xml, "Name", 2, DIRECT_NAME)
        self.assertEqual(column_cell(tasks, "Progress", 2).value, SAFE_PROGRESS)

    def test_dependency_ids_are_stored_as_text(self):
        """Predecessor and successor ids are written by the same export."""
        chart = self.GanttChartGenerator("Office Building A")
        chart.add_task(
            self.Task(
                "T1",
                SAFE_NAME,
                date(2024, 6, 1),
                date(2024, 6, 30),
                wbs_code="01",
            )
        )
        chart.add_dependency(PREDECESSOR_ID, SUCCESSOR_ID)
        path = self.export(chart)
        workbook = load_workbook(path)
        dependencies = workbook["Dependencies"]
        xml = sheet_xml(path)["Dependencies"]
        with self.subTest(field="Predecessor"):
            assert_field_is_text(
                self, dependencies, xml, "Predecessor", 2, PREDECESSOR_ID
            )
        with self.subTest(field="Successor"):
            assert_field_is_text(
                self, dependencies, xml, "Successor", 2, SUCCESSOR_ID
            )


if __name__ == "__main__":
    unittest.main()
