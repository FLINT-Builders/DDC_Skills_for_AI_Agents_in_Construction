"""Spreadsheet formula injection in progress-photo Excel exports.

The code under test is the Python implementation in each skill document.
Caller-controlled text that starts with ``=``, ``+``, ``-``, ``@``, a tab,
or a carriage return must be written as text, with one leading apostrophe.
These tests fail while the exporters pass that text through unchanged,
because pandas and openpyxl store a leading ``=`` as a worksheet formula.

Operations covered:
- BIM ``analyze_site_photos`` (filename from ``Path.name``) then ``export_report``
- BIM ``register_photo`` / ``analyze_photo`` / ``export_report``
  (filename, location, and level on the Photos sheet)
- Field ``catalog_photo`` / ``export_catalog``
  (filename, level, zone, location, captured-by, and tags)

Not written by those exporters, so not asserted here:
- BIM zone, photographer, file path, and GPS coordinates
- BIM progress and safety descriptions (the analyzer assigns them)
- Field description, activity code, and file path
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

REPO = Path(__file__).resolve().parents[1]
BIM_SKILL = REPO / "1_DDC_Toolkit/BIM-Analysis/progress-photo-analyzer/SKILL.md"
FIELD_SKILL = REPO / "1_DDC_Toolkit/Field-Operations/progress-photo-analyzer/SKILL.md"

# Characters Excel treats as formula introducers in spreadsheet exports.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

HYPERLINK = '=HYPERLINK("https://attacker.example/x","open")'
# No slash: Path.name keeps this whole string as the uploaded file's name.
FILENAME_FORMULA = '=HYPERLINK("https:"&CHAR(47)&CHAR(47)&"attacker.example","open.jpg")'


def load_skill(skill_path: Path) -> dict:
    """Execute the skill document's implementation block."""
    text = skill_path.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    namespace: dict = {"__file__": str(skill_path)}
    exec(compile(text[start:end], str(skill_path), "exec"), namespace)
    return namespace


def expected_text(original: str) -> str:
    """Text that must be stored once formula markers are neutralized.

    OOXML parsers turn a carriage return in cell text into a line feed, so
    the value read back from the workbook uses that character.
    """
    if original.startswith(FORMULA_PREFIXES):
        original = "'" + original
    return original.replace("\r", "\n")


def rows_by_header(worksheet):
    headers = [cell.value for cell in next(worksheet.iter_rows(min_row=1, max_row=1))]
    rows = []
    for row in worksheet.iter_rows(min_row=2):
        rows.append({headers[index]: row[index] for index in range(len(headers))})
    return rows


def assert_safe_text(test: unittest.TestCase, cell, original: str, label: str) -> None:
    expected = expected_text(original)
    test.assertNotEqual(
        cell.data_type,
        "f",
        f"{label} was stored as a formula: {cell.value!r}",
    )
    test.assertEqual(cell.value, expected, label)


def assert_workbook_has_no_formulas(test: unittest.TestCase, workbook_path: Path) -> None:
    with zipfile.ZipFile(workbook_path) as workbook:
        sheet_names = [
            name
            for name in workbook.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        ]
        test.assertTrue(sheet_names, "workbook has no worksheets")
        for name in sheet_names:
            xml = workbook.read(name).decode("utf-8")
            test.assertNotIn("<f>", xml, name)
            test.assertNotIn("<f ", xml, name)


class BimProgressPhotoFormulaTests(unittest.TestCase):
    def setUp(self):
        self.skill = load_skill(BIM_SKILL)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_analyze_site_photos_writes_formula_filename_as_text(self):
        formula_file = self.folder / FILENAME_FORMULA
        formula_file.write_bytes(b"site-photo")
        plain_file = self.folder / "site_photo_001.jpg"
        plain_file.write_bytes(b"site-photo")
        report = self.folder / "photo_analysis_report.xlsx"

        self.skill["analyze_site_photos"](
            [str(formula_file), str(plain_file)],
            "Office Tower",
            str(report),
        )

        rows = rows_by_header(load_workbook(report)["Photos"])
        self.assertEqual(len(rows), 2)
        assert_safe_text(self, rows[0]["Filename"], FILENAME_FORMULA, "Filename")
        assert_safe_text(self, rows[0]["Location"], "Site", "Location")
        assert_safe_text(self, rows[1]["Filename"], "site_photo_001.jpg", "Filename")
        assert_workbook_has_no_formulas(self, report)

    def test_export_report_neutralizes_filename_location_and_level(self):
        analyzer = self.skill["ProgressPhotoAnalyzer"]("Office Tower")
        photo_type = self.skill["PhotoType"].PROGRESS
        samples = [
            ("site_photo_001.jpg", "Level 3 - Core", "Level 3"),
            (FILENAME_FORMULA, HYPERLINK, "=1+1"),
            ("photo=1.jpg", "Grid B-3", "+Level"),
            ("crew.jpg", "foundation deck", HYPERLINK),
        ]
        for filename, location, level in samples:
            metadata = analyzer.register_photo(
                filename=filename,
                capture_date=datetime(2026, 11, 25, 8, 0, 0),
                location=location,
                level=level,
                photo_type=photo_type,
            )
            analyzer.analyze_photo(metadata.photo_id)

        report = self.folder / "photo_analysis_report.xlsx"
        analyzer.export_report(str(report))
        workbook = load_workbook(report)
        rows = rows_by_header(workbook["Photos"])
        self.assertEqual(len(rows), len(samples))
        for row, (filename, location, level) in zip(rows, samples):
            assert_safe_text(self, row["Filename"], filename, "Filename")
            assert_safe_text(self, row["Location"], location, "Location")
            assert_safe_text(self, row["Level"], level, "Level")

        progress = rows_by_header(workbook["Progress"])
        descriptions = [row["Description"].value for row in progress]
        self.assertIn("Foundation work visible", descriptions)
        for row in progress:
            self.assertNotEqual(row["Description"].data_type, "f")
            self.assertNotIn("HYPERLINK", str(row["Description"].value))
        assert_workbook_has_no_formulas(self, report)

    def test_export_report_neutralizes_each_formula_prefix(self):
        analyzer = self.skill["ProgressPhotoAnalyzer"]("Office Tower")
        photo_type = self.skill["PhotoType"].PROGRESS
        originals = []
        for prefix in FORMULA_PREFIXES:
            original = prefix + 'HYPERLINK("https://attacker.example/x","open")'
            originals.append(original)
            metadata = analyzer.register_photo(
                filename=original,
                capture_date=datetime(2026, 11, 25, 8, 0, 0),
                location=original,
                level=original,
                photo_type=photo_type,
            )
            analyzer.analyze_photo(metadata.photo_id)

        report = self.folder / "prefixes.xlsx"
        analyzer.export_report(str(report))
        rows = rows_by_header(load_workbook(report)["Photos"])
        self.assertEqual(len(rows), len(originals))
        for row, original in zip(rows, originals):
            for column in ("Filename", "Location", "Level"):
                with self.subTest(column=column, prefix=original[:1]):
                    assert_safe_text(self, row[column], original, column)
        assert_workbook_has_no_formulas(self, report)


class FieldProgressPhotoFormulaTests(unittest.TestCase):
    def setUp(self):
        self.skill = load_skill(FIELD_SKILL)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_export_catalog_neutralizes_caller_text(self):
        analyzer = self.skill["ProgressPhotoAnalyzer"]("Office Tower")
        category = self.skill["PhotoCategory"].PROGRESS
        captured = datetime(2026, 11, 25, 8, 0, 0)
        plain_tags = ["concrete", "forming"]
        formula_tags = [HYPERLINK]
        embedded_tags = ["concrete", "=1+1"]

        analyzer.catalog_photo(
            filename="IMG_001.jpg",
            captured_date=captured,
            category=category,
            location="Column Grid B-3",
            level="Level 5",
            captured_by="Site Super",
            zone="Zone A",
            tags=plain_tags,
        )
        analyzer.catalog_photo(
            filename=HYPERLINK,
            captured_date=captured,
            category=category,
            location=HYPERLINK,
            level=FILENAME_FORMULA,
            captured_by=HYPERLINK,
            zone="+north",
            description=HYPERLINK,
            tags=formula_tags,
        )
        analyzer.catalog_photo(
            filename="IMG_003.jpg",
            captured_date=captured,
            category=category,
            location="Stairs",
            level="Level 2",
            captured_by="Site Super",
            zone="Zone B",
            tags=embedded_tags,
        )

        report = self.folder / "photo_catalog.xlsx"
        analyzer.export_catalog(str(report))
        rows = rows_by_header(load_workbook(report)["Sheet1"])
        self.assertEqual(len(rows), 3)

        assert_safe_text(self, rows[0]["Filename"], "IMG_001.jpg", "Filename")
        assert_safe_text(self, rows[0]["Location"], "Column Grid B-3", "Location")
        assert_safe_text(self, rows[0]["Level"], "Level 5", "Level")
        assert_safe_text(self, rows[0]["Zone"], "Zone A", "Zone")
        assert_safe_text(self, rows[0]["By"], "Site Super", "By")
        assert_safe_text(self, rows[0]["Tags"], "concrete, forming", "Tags")

        assert_safe_text(self, rows[1]["Filename"], HYPERLINK, "Filename")
        assert_safe_text(self, rows[1]["Location"], HYPERLINK, "Location")
        assert_safe_text(self, rows[1]["Level"], FILENAME_FORMULA, "Level")
        assert_safe_text(self, rows[1]["Zone"], "+north", "Zone")
        assert_safe_text(self, rows[1]["By"], HYPERLINK, "By")
        assert_safe_text(self, rows[1]["Tags"], HYPERLINK, "Tags")

        # A formula marker after other text is ordinary cell text.
        assert_safe_text(self, rows[2]["Tags"], "concrete, =1+1", "Tags")
        assert_workbook_has_no_formulas(self, report)

    def test_export_catalog_neutralizes_each_formula_prefix(self):
        analyzer = self.skill["ProgressPhotoAnalyzer"]("Office Tower")
        category = self.skill["PhotoCategory"].PROGRESS
        captured = datetime(2026, 11, 25, 8, 0, 0)
        originals = [
            prefix + 'HYPERLINK("https://attacker.example/x","open")'
            for prefix in FORMULA_PREFIXES
        ]
        for original in originals:
            analyzer.catalog_photo(
                filename=original,
                captured_date=captured,
                category=category,
                location=original,
                level=original,
                captured_by=original,
                zone=original,
                tags=[original],
            )

        report = self.folder / "prefixes.xlsx"
        analyzer.export_catalog(str(report))
        rows = rows_by_header(load_workbook(report)["Sheet1"])
        for row, original in zip(rows, originals):
            for column in ("Filename", "Level", "Zone", "Location", "By", "Tags"):
                with self.subTest(column=column, prefix=original[:1]):
                    assert_safe_text(self, row[column], original, column)
        assert_workbook_has_no_formulas(self, report)


if __name__ == "__main__":
    unittest.main()
