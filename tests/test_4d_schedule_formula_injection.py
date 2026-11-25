"""Failing spec for formula injection in export_4d_schedule.

export_4d_schedule writes the linked-element table with pandas to_excel.
A string that starts with "=" is stored as an Excel formula, including
Category, Level, and ElementId, and the same values again as the By_Level
and By_Category index labels. These tests load that function from SKILL.md
and fail while any of those cells are formula cells. They pass once every
string field is stored as text and the original characters are still in the
workbook.

Run from the repository root:

    pip install pandas openpyxl
    python -m unittest tests.test_4d_schedule_formula_injection
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "2_DDC_Book"
    / "3.3-4D-BIM-CO2-Simulation"
    / "4d-simulation"
    / "SKILL.md"
)

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# Distinct leading-"=" values. Each one is long enough that openpyxl treats
# it as a formula, and each one is unique so a sheet can be checked on its own.
ELEMENT_ID = "=100+1"
ELEMENT_ID_2 = "=100+2"
CATEGORY = "=200+1"
CATEGORY_2 = "=200+2"
LEVEL = "=300+1"
LEVEL_2 = "=300+2"
PHASE = "=400+1"
TRADE = "=500+1"
BENIGN_ELEMENT = "E001"
BENIGN_CATEGORY = "Foundation"
BENIGN_LEVEL = "Level 1"
BENIGN_PHASE = "Structure"
BENIGN_TRADE = "Civil"


def load_export_4d_schedule():
    """Execute the export function from the skill the product tells agents to run."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "def export_4d_schedule"
    start = text.find(marker)
    if start < 0:
        raise AssertionError(f"{SKILL_PATH} does not define export_4d_schedule")
    fence = text.rfind("```python", 0, start)
    end = text.find("```", start)
    if fence < 0 or end < 0:
        raise AssertionError("export_4d_schedule is not inside a python fence")
    code = text[fence + len("```python") : end]
    namespace = {"pd": pd}
    exec(code, namespace)
    return namespace["export_4d_schedule"]


def linked_elements():
    """A linked 4D table: two formula rows and one ordinary schedule row."""
    start = datetime(2024, 1, 1)
    end = datetime(2024, 1, 14)
    rows = [
        (ELEMENT_ID, CATEGORY, LEVEL, start, end, PHASE, TRADE),
        (ELEMENT_ID_2, CATEGORY_2, LEVEL_2, start, end, PHASE, TRADE),
        (BENIGN_ELEMENT, BENIGN_CATEGORY, BENIGN_LEVEL, start, end, BENIGN_PHASE, BENIGN_TRADE),
    ]
    return pd.DataFrame(
        rows,
        columns=["ElementId", "Category", "Level", "Start_Date", "End_Date", "Phase", "Trade"],
    )


def _local(tag: str) -> str:
    return f"{{{MAIN_NS}}}{tag}"


def workbook_sheets(path: Path):
    """Return {sheet name: {"formulas": [...], "texts": [...]}} from the xlsx package."""
    with zipfile.ZipFile(path) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in relationships.findall(f"{{{REL_NS}}}Relationship")
        }
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [(node.text or "") for node in shared_root.iter(_local("t"))]

        sheets = {}
        for sheet in workbook.findall(f"{_local('sheets')}/{_local('sheet')}"):
            name = sheet.attrib["name"]
            target = targets[sheet.attrib[f"{{{OFFICE_REL_NS}}}id"]].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            root = ElementTree.fromstring(archive.read(target))
            formulas = [(node.text or "") for node in root.iter(_local("f"))]
            texts = [(node.text or "") for node in root.iter(_local("t"))]
            sheets[name] = {"formulas": formulas, "texts": texts + shared}
        return sheets


def formula_carries(formula_text: str, payload: str) -> bool:
    """True when an OOXML formula element is the payload (with or without '=')."""
    body = payload[1:] if payload.startswith("=") else payload
    return formula_text == payload or formula_text == body or payload in formula_text


class Export4DFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep the loaded function off the class. Assigning it there would bind
        # it as a test method and pass the test instance in as the first argument.
        global _export_4d_schedule
        _export_4d_schedule = load_export_4d_schedule()

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.output = Path(self._tmpdir.name) / "4d_schedule.xlsx"
        _export_4d_schedule(linked_elements(), str(self.output))
        self.sheets = workbook_sheets(self.output)

    def tearDown(self):
        self._tmpdir.cleanup()

    def assert_payload_is_text(self, sheet_name: str, payload: str):
        sheet = self.sheets[sheet_name]
        leaked = [text for text in sheet["formulas"] if formula_carries(text, payload)]
        self.assertEqual(
            leaked,
            [],
            f"{sheet_name} stored {payload!r} as a formula cell: {sheet['formulas']}",
        )
        self.assertTrue(
            any(payload in text for text in sheet["texts"]),
            f"{sheet_name} no longer contains {payload!r} as text: {sheet['texts']}",
        )

    def test_workbook_has_schedule_level_and_category_sheets(self):
        self.assertEqual(
            set(self.sheets),
            {"Schedule", "By_Level", "By_Category"},
        )

    def test_schedule_sheet_keeps_string_fields_as_text(self):
        for payload in (
            ELEMENT_ID,
            ELEMENT_ID_2,
            CATEGORY,
            CATEGORY_2,
            LEVEL,
            LEVEL_2,
            PHASE,
            TRADE,
            BENIGN_ELEMENT,
            BENIGN_CATEGORY,
            BENIGN_LEVEL,
            BENIGN_PHASE,
            BENIGN_TRADE,
        ):
            with self.subTest(payload=payload):
                self.assert_payload_is_text("Schedule", payload)
        self.assertEqual(self.sheets["Schedule"]["formulas"], [])

    def test_by_level_keeps_level_labels_as_text(self):
        for payload in (LEVEL, LEVEL_2, BENIGN_LEVEL):
            with self.subTest(payload=payload):
                self.assert_payload_is_text("By_Level", payload)
        self.assertEqual(self.sheets["By_Level"]["formulas"], [])

    def test_by_category_keeps_category_labels_as_text(self):
        for payload in (CATEGORY, CATEGORY_2, BENIGN_CATEGORY):
            with self.subTest(payload=payload):
                self.assert_payload_is_text("By_Category", payload)
        self.assertEqual(self.sheets["By_Category"]["formulas"], [])


if __name__ == "__main__":
    unittest.main()
