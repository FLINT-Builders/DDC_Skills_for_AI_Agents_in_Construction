"""Action descriptions and assignees must be stored as text in the workbook.

These tests load the meeting-minutes implementation from its skill file and
call create_action, then export_all_actions. They fail while a description
or assignee that starts with "=" is written as a spreadsheet formula.

A stored value is text when the workbook contains no formula elements and the
cell keeps the original string, optionally with one leading text marker:
apostrophe, space, or tab.
"""

import re
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from openpyxl import load_workbook

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "Document-Control"
    / "meeting-minutes-generator"
    / "SKILL.md"
)
SPREADSHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
# Leading markers that keep a formula-like string as text when prefixed once.
TEXT_PREFIXES = ("", "'", " ", "\t")


def load_skill_implementation():
    """Execute the skill's implementation block and return its namespace."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.S)
    if match is None:
        raise AssertionError(f"no Python implementation block in {SKILL_PATH}")
    namespace = {}
    exec(compile(match.group(1), str(SKILL_PATH), "exec"), namespace)
    return namespace


def formula_texts(workbook_path):
    """Return every formula body stored in the workbook's worksheets."""
    formulas = []
    with zipfile.ZipFile(workbook_path) as archive:
        sheet_names = [
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        ]
        for name in sheet_names:
            root = ET.fromstring(archive.read(name))
            for formula in root.findall(".//m:f", SPREADSHEET_NS):
                formulas.append(formula.text)
    return formulas


def cell_for(worksheet, header, data_row):
    headers = [cell.value for cell in next(worksheet.iter_rows(min_row=1, max_row=1))]
    column = headers.index(header) + 1
    return worksheet.cell(row=data_row, column=column)


def assert_text_preserves(testcase, stored, original):
    testcase.assertIsInstance(stored, str)
    testcase.assertTrue(
        any(stored == prefix + original for prefix in TEXT_PREFIXES),
        f"cell {stored!r} does not keep {original!r} as text",
    )


class MeetingActionExportFormulaTests(unittest.TestCase):
    """export_all_actions must not turn meeting text into formulas."""

    @classmethod
    def setUpClass(cls):
        namespace = load_skill_implementation()
        cls.generator_cls = namespace["MeetingMinutesGenerator"]
        cls.meeting_type = namespace["MeetingType"]
        cls.priority = namespace["Priority"]

    def export(self, actions):
        """Create one meeting, record actions, and export the workbook.

        actions is a sequence of (description, assignee) pairs. The strings
        are the meeting text a participant can supply through create_action.
        """
        generator = self.generator_cls("Office Tower")
        meeting = generator.create_meeting(
            self.meeting_type.OAC,
            "Weekly OAC",
            date(2026, 11, 25),
            "10:00 AM",
            "11:00 AM",
            "Conference Room A",
        )
        for description, assignee in actions:
            created = generator.create_action(
                meeting.meeting_id,
                description,
                assignee,
                date(2026, 12, 1),
                self.priority.HIGH,
            )
            self.assertIsNotNone(created)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output_path = Path(directory.name) / "actions.xlsx"
        returned = generator.export_all_actions(str(output_path))
        self.assertEqual(returned, str(output_path))
        self.assertTrue(output_path.is_file())
        return output_path

    def assert_exported_text(self, actions, checks):
        """checks maps (data_row, header) to the original meeting string."""
        output_path = self.export(actions)
        self.assertEqual(
            formula_texts(output_path),
            [],
            "action workbook contains a spreadsheet formula",
        )
        workbook = load_workbook(output_path)
        worksheet = workbook.active
        for (data_row, header), original in checks.items():
            cell = cell_for(worksheet, header, data_row)
            self.assertNotEqual(
                cell.data_type,
                "f",
                f"{header} on row {data_row} was stored as a formula ({cell.value!r})",
            )
            assert_text_preserves(self, cell.value, original)
        return worksheet

    def test_description_starting_with_equals_is_stored_as_text(self):
        description = "=1+1"
        assignee = "Ada Lovelace"
        self.assert_exported_text(
            [(description, assignee)],
            {(2, "Description"): description, (2, "Assigned To"): assignee},
        )

    def test_assignee_starting_with_equals_is_stored_as_text(self):
        description = "Submit the revised schedule"
        assignee = "=2+3"
        self.assert_exported_text(
            [(description, assignee)],
            {(2, "Description"): description, (2, "Assigned To"): assignee},
        )

    def test_description_and_assignee_on_the_same_action_are_stored_as_text(self):
        description = "=1+1"
        assignee = "=3+4"
        self.assert_exported_text(
            [(description, assignee)],
            {(2, "Description"): description, (2, "Assigned To"): assignee},
        )

    def test_formula_on_a_later_action_is_stored_as_text(self):
        first = ("Record the decision", "Project Manager")
        second_description = "=SUM(1,2)"
        second_assignee = "Site Engineer"
        self.assert_exported_text(
            [first, (second_description, second_assignee)],
            {
                (2, "Description"): first[0],
                (2, "Assigned To"): first[1],
                (3, "Description"): second_description,
                (3, "Assigned To"): second_assignee,
            },
        )

    def test_function_formula_in_description_is_stored_as_text(self):
        description = '=HYPERLINK("http://example.invalid/a","Alex")'
        assignee = "Alex Kim"
        self.assert_exported_text(
            [(description, assignee)],
            {(2, "Description"): description, (2, "Assigned To"): assignee},
        )

    def test_equals_followed_by_space_is_stored_as_text(self):
        description = "= 1+1"
        assignee = "Quantity Surveyor"
        self.assert_exported_text(
            [(description, assignee)],
            {(2, "Description"): description, (2, "Assigned To"): assignee},
        )

    def test_equals_not_at_the_start_stays_unchanged_text(self):
        description = "total =1+1"
        assignee = "Hal"
        worksheet = self.assert_exported_text(
            [(description, assignee)],
            {(2, "Description"): description, (2, "Assigned To"): assignee},
        )
        self.assertEqual(cell_for(worksheet, "Description", 2).value, description)
        self.assertEqual(cell_for(worksheet, "Assigned To", 2).value, assignee)

    def test_single_equals_sign_stays_text(self):
        description = "="
        assignee = "Ivy"
        worksheet = self.assert_exported_text(
            [(description, assignee)],
            {(2, "Description"): description, (2, "Assigned To"): assignee},
        )
        self.assertEqual(cell_for(worksheet, "Description", 2).value, description)


if __name__ == "__main__":
    unittest.main()
