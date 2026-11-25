"""Formula injection in the closeout punch-list Excel export.

The skill implementation under test is the Python block in
``1_DDC_Toolkit/Project-Closeout/punch-list-manager/SKILL.md``. These tests
load that block and run ``export_to_excel``. They fail while a string that
starts with ``=`` is written as an Excel formula.

Run from the repository root:

    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

import re
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from openpyxl import load_workbook


SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "1_DDC_Toolkit"
    / "Project-Closeout"
    / "punch-list-manager"
    / "SKILL.md"
)

SHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
SHEET_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

# Payloads a subcontractor can put in a note, company name, or location.
EQUALS_SUM = "=1+1"
NEIGHBOR_REF = "=C2"
HYPERLINK = '=HYPERLINK("http://example.invalid/closeout","open")'
EMBEDDED_EQUALS = "Room = 301"


def load_skill_namespace():
    """Execute the skill's implementation block so the test follows that code."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.S)
    if match is None:
        raise AssertionError(f"no Python block in {SKILL_PATH}")
    code = match.group(1)
    if "def export_to_excel" not in code or "class PunchListManager" not in code:
        raise AssertionError("skill Python block is not the punch list manager")
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(code, str(SKILL_PATH), "exec"), namespace)
    return namespace


def formula_cells(path):
    """Return ``{(sheet name, cell ref): formula body}`` from the xlsx XML."""
    found = {}
    with zipfile.ZipFile(path) as workbook:
        workbook_xml = ET.fromstring(workbook.read("xl/workbook.xml"))
        rels_xml = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        targets = {
            rel.attrib["Id"]: rel.attrib["Target"] for rel in list(rels_xml)
        }
        for sheet in workbook_xml.findall("m:sheets/m:sheet", SHEET_NS):
            target = targets[sheet.attrib[SHEET_REL_ID]].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            sheet_xml = ET.fromstring(workbook.read(target))
            for cell in sheet_xml.findall(".//m:c", SHEET_NS):
                formula = cell.find("m:f", SHEET_NS)
                if formula is not None:
                    found[(sheet.attrib["name"], cell.attrib.get("r"))] = formula.text
    return found


def header_columns(worksheet):
    return {cell.value: cell.column for cell in worksheet[1]}


def row_for_item(worksheet, item_id):
    columns = header_columns(worksheet)
    id_column = columns["Item ID"]
    for row in range(2, worksheet.max_row + 1):
        if worksheet.cell(row, id_column).value == item_id:
            return row
    raise AssertionError(f"{item_id} not found on {worksheet.title}")


def row_for_value(worksheet, header, value):
    column = header_columns(worksheet)[header]
    for row in range(2, worksheet.max_row + 1):
        cell = worksheet.cell(row, column)
        if cell.value == value or cell.value == "'" + value:
            return row
    raise AssertionError(f"{header}={value!r} not found on {worksheet.title}")


class PunchListFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = load_skill_namespace()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def manager(self, project_name="Office Building A"):
        return self.skill["PunchListManager"](
            project_name, target_closeout_date=date(2026, 12, 31)
        )

    def add_item(self, manager, **overrides):
        fields = {
            "location": "Level 3, Room 301",
            "description": "Ceiling tile damaged",
            "category": self.skill["PunchItemCategory"].ARCHITECTURAL,
            "priority": self.skill["PunchItemPriority"].MEDIUM,
            "assigned_to": "ABC Ceilings",
            "due_date": date.today() + timedelta(days=14),
            "notes": "",
        }
        fields.update(overrides)
        return manager.add_item(**fields)

    def export(self, manager, name="punch.xlsx"):
        path = str(Path(self.directory.name) / name)
        returned = manager.export_to_excel(path)
        self.assertEqual(returned, path)
        self.assertTrue(Path(path).is_file())
        return path

    def assert_stored_as_text(self, path, sheet_name, coordinate, original):
        """A neutralized cell is not a formula and still shows the original text.

        A leading apostrophe is the accepted text-prefix mitigation. Setting
        the cell type to a string and keeping the original text is the other.
        """
        formulas = formula_cells(path)
        self.assertNotIn(
            (sheet_name, coordinate),
            formulas,
            f"{sheet_name}!{coordinate} was written as a formula "
            f"{formulas.get((sheet_name, coordinate))!r} for {original!r}",
        )
        cell = load_workbook(path)[sheet_name][coordinate]
        self.assertNotEqual(cell.data_type, "f")
        self.assertIn(cell.value, (original, "'" + original))

    def test_item_fields_that_start_with_equals_are_text(self):
        """Location, description, assignee, and notes are live cells on All Items."""
        manager = self.manager()
        by_location = self.add_item(manager, location=NEIGHBOR_REF)
        by_description = self.add_item(manager, description=EQUALS_SUM)
        by_assignee = self.add_item(manager, assigned_to=HYPERLINK)
        by_notes = self.add_item(manager, notes=HYPERLINK)
        safe = self.add_item(
            manager,
            location=EMBEDDED_EQUALS,
            description="See note = follow up",
            assigned_to="ABC Ceilings",
            notes="Measured twice. Width = 4 ft.",
        )
        path = self.export(manager)
        workbook = load_workbook(path)
        sheet = workbook["All Items"]

        cases = (
            (by_location, "Location", NEIGHBOR_REF),
            (by_description, "Description", EQUALS_SUM),
            (by_assignee, "Assigned To", HYPERLINK),
            (by_notes, "Notes", HYPERLINK),
        )
        for item, header, original in cases:
            with self.subTest(item=item.item_id, field=header):
                cell = sheet.cell(
                    row_for_item(sheet, item.item_id), header_columns(sheet)[header]
                )
                self.assert_stored_as_text(path, "All Items", cell.coordinate, original)

        safe_row = row_for_item(sheet, safe.item_id)
        columns = header_columns(sheet)
        self.assertEqual(sheet.cell(safe_row, columns["Location"]).value, EMBEDDED_EQUALS)
        self.assertEqual(
            sheet.cell(safe_row, columns["Description"]).value, "See note = follow up"
        )
        self.assertEqual(
            sheet.cell(safe_row, columns["Notes"]).value,
            "Measured twice. Width = 4 ft.",
        )
        self.assertEqual(sheet.cell(safe_row, columns["Location"]).data_type, "s")

    def test_reassigned_company_name_is_text(self):
        manager = self.manager()
        item = self.add_item(manager, assigned_to="Old Co")
        manager.reassign_item(item.item_id, HYPERLINK)
        path = self.export(manager)
        sheet = load_workbook(path)["All Items"]
        cell = sheet.cell(
            row_for_item(sheet, item.item_id), header_columns(sheet)["Assigned To"]
        )
        self.assert_stored_as_text(path, "All Items", cell.coordinate, HYPERLINK)

    def test_summary_project_name_is_text(self):
        manager = self.manager(project_name=EQUALS_SUM)
        self.add_item(manager)
        path = self.export(manager)
        sheet = load_workbook(path)["Summary"]
        cell = sheet.cell(2, header_columns(sheet)["Project"])
        self.assert_stored_as_text(path, "Summary", cell.coordinate, EQUALS_SUM)
        total = sheet.cell(2, header_columns(sheet)["Total Items"])
        self.assertEqual(total.value, 1)
        self.assertNotEqual(total.data_type, "f")

    def test_by_assignee_sheet_company_name_is_text(self):
        manager = self.manager()
        self.add_item(manager, assigned_to=HYPERLINK)
        self.add_item(manager, assigned_to="Safe Co")
        path = self.export(manager)
        sheet = load_workbook(path)["By Assignee"]
        self.assertIn("By Assignee", load_workbook(path).sheetnames)
        formula_row = row_for_value(sheet, "Assignee", HYPERLINK)
        cell = sheet.cell(formula_row, header_columns(sheet)["Assignee"])
        self.assert_stored_as_text(path, "By Assignee", cell.coordinate, HYPERLINK)
        safe_row = row_for_value(sheet, "Assignee", "Safe Co")
        safe = sheet.cell(safe_row, header_columns(sheet)["Assignee"])
        self.assertEqual(safe.value, "Safe Co")
        self.assertEqual(safe.data_type, "s")

    def test_overdue_sheet_location_description_and_assignee_are_text(self):
        manager = self.manager()
        overdue_location = self.add_item(
            manager, location=EQUALS_SUM, due_date=date.today() - timedelta(days=2)
        )
        overdue_description = self.add_item(
            manager, description=HYPERLINK, due_date=date.today() - timedelta(days=2)
        )
        overdue_assignee = self.add_item(
            manager, assigned_to=NEIGHBOR_REF, due_date=date.today() - timedelta(days=2)
        )
        path = self.export(manager)
        sheet = load_workbook(path)["Overdue"]
        columns = header_columns(sheet)
        cases = (
            (overdue_location, "Location", EQUALS_SUM),
            (overdue_description, "Description", HYPERLINK),
            (overdue_assignee, "Assigned To", NEIGHBOR_REF),
        )
        for item, header, original in cases:
            with self.subTest(item=item.item_id, field=header):
                cell = sheet.cell(row_for_item(sheet, item.item_id), columns[header])
                self.assert_stored_as_text(path, "Overdue", cell.coordinate, original)

    def test_notes_prefixed_by_add_note_stay_text_and_keep_the_note(self):
        """add_note inserts a leading newline and the date before the note text.

        The exported notes cell has to keep that text and must not be a formula,
        including when the note itself starts with ``=``.
        """
        manager = self.manager()
        item = self.add_item(manager, notes="")
        manager.add_note(item.item_id, HYPERLINK)
        self.assertTrue(item.notes.startswith("\n"))
        self.assertIn(HYPERLINK, item.notes)
        path = self.export(manager)
        sheet = load_workbook(path)["All Items"]
        cell = sheet.cell(
            row_for_item(sheet, item.item_id), header_columns(sheet)["Notes"]
        )
        self.assert_stored_as_text(path, "All Items", cell.coordinate, item.notes)

    def test_add_note_does_not_leave_an_existing_formula_note_executable(self):
        manager = self.manager()
        item = self.add_item(manager, notes=EQUALS_SUM)
        manager.add_note(item.item_id, "followed up")
        self.assertTrue(item.notes.startswith("="))
        self.assertIn("followed up", item.notes)
        path = self.export(manager)
        sheet = load_workbook(path)["All Items"]
        cell = sheet.cell(
            row_for_item(sheet, item.item_id), header_columns(sheet)["Notes"]
        )
        self.assert_stored_as_text(path, "All Items", cell.coordinate, item.notes)

    def test_other_formula_prefixes_are_not_executed(self):
        """xlsx cells store +, -, @, tab, and CR as text. Keep them that way."""
        payloads = ("+1+1", "-1+1", "@SUM(A1)", "\t=1+1", "\r=1+1", "=")
        manager = self.manager()
        items = [self.add_item(manager, location=payload) for payload in payloads]
        path = self.export(manager)
        sheet = load_workbook(path)["All Items"]
        columns = header_columns(sheet)
        for item, payload in zip(items, payloads):
            with self.subTest(payload=payload):
                cell = sheet.cell(row_for_item(sheet, item.item_id), columns["Location"])
                formulas = formula_cells(path)
                self.assertNotIn(("All Items", cell.coordinate), formulas)
                self.assertNotEqual(cell.data_type, "f")
                # XML parsers normalize a carriage return inside a string to a newline.
                accepted = {payload, "'" + payload}
                if payload.startswith("\r"):
                    normalized = "\n" + payload[1:]
                    accepted.update({normalized, "'" + normalized})
                self.assertIn(cell.value, accepted)


if __name__ == "__main__":
    unittest.main()
