"""Spreadsheet formula injection through quality-report column names.

`export_quality_report` in the data-quality-check skill writes every
DataFrame column name into the Completeness sheet (as the row index) and
the Missing_Values sheet (as the Column field). These tests load that
skill code and run it on a CSV whose headers start with `=`.

They fail while those headers are stored as Excel formulas. They pass once
every column name is written as text, including on both sheets and when
detail sheets are omitted. A name may be kept readable by prefixing one
leading apostrophe, space, or tab; that prefix is the usual way to stop
Excel and openpyxl from treating the cell as a formula.
"""

from __future__ import annotations

import csv
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import openpyxl

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "2_DDC_Book"
    / "2.6-Data-Quality-Validation"
    / "data-quality-check"
    / "SKILL.md"
)

# Benign formulas. `=Summary!B2` reads the Overall Score cell that the
# report itself writes on the Summary sheet.
PAYLOADS = ("=1+1", "=Summary!B2")
SAFE_COLUMN = "Volume_m3"
_TEXT_PREFIXES = ("'", " ", "\t")
_FORMULA_RE = re.compile(r"<f(?:\s[^>]*)?>([^<]*)</f>")


def load_export_quality_report(skill_path: Path = SKILL_PATH):
    """Execute the skill's checker class and export function."""
    text = skill_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    namespace: dict = {"__name__": "data_quality_check_skill"}
    try:
        class_block = next(block for block in blocks if "class DataQualityChecker" in block)
        export_block = next(block for block in blocks if "def export_quality_report" in block)
    except StopIteration as exc:
        raise AssertionError(f"skill code blocks not found in {skill_path}") from exc
    exec(compile(class_block, str(skill_path), "exec"), namespace)
    exec(compile(export_block, str(skill_path), "exec"), namespace)
    return namespace["export_quality_report"]


def write_schedule_csv(path: Path) -> None:
    """CSV a caller can hand the checker: formula headers plus one real column."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([*PAYLOADS, SAFE_COLUMN])
        writer.writerow([1, 2, 3.5])
        writer.writerow([4, 5, 1.0])


def export_report(include_details: bool) -> Path:
    import pandas as pd

    export_quality_report = load_export_quality_report()
    temporary = tempfile.TemporaryDirectory(prefix="quality-report-")
    # Keep the directory alive for the returned path. The test process exits
    # before the directory needs to be removed.
    export_report.directories.append(temporary)
    root = Path(temporary.name)
    csv_path = root / "schedule.csv"
    workbook_path = root / "quality_report.xlsx"
    write_schedule_csv(csv_path)
    frame = pd.read_csv(csv_path)
    exported = export_quality_report(
        frame, workbook_path, include_details=include_details
    )
    if Path(exported) != workbook_path or not workbook_path.is_file():
        raise AssertionError(f"export did not write {workbook_path}")
    return workbook_path


def worksheet_formula_bodies(workbook_path: Path) -> list[tuple[str, str]]:
    found = []
    with zipfile.ZipFile(workbook_path) as archive:
        for name in archive.namelist():
            if not (name.startswith("xl/worksheets/") and name.endswith(".xml")):
                continue
            xml = archive.read(name).decode("utf-8")
            for body in _FORMULA_RE.findall(xml):
                found.append((name, body))
    return found


def stored_as_text(cell_value: str, payload: str) -> bool:
    if cell_value == payload:
        return True
    return cell_value[:1] in _TEXT_PREFIXES and cell_value[1:] == payload


def assert_payloads_are_text(sheet, payloads: tuple[str, ...] = PAYLOADS) -> None:
    formula_cells = []
    text_values = []
    for row in sheet.iter_rows():
        for cell in row:
            if cell.data_type == "f":
                formula_cells.append((cell.coordinate, cell.value))
            elif isinstance(cell.value, str):
                text_values.append(cell.value)

    leaked = [
        (coordinate, value)
        for coordinate, value in formula_cells
        if isinstance(value, str)
        and (value in payloads or value.lstrip("=") in {item.lstrip("=") for item in payloads})
    ]
    assert leaked == [], f"{sheet.title} stored column names as formulas: {leaked}"

    for payload in payloads:
        matches = [value for value in text_values if stored_as_text(value, payload)]
        assert matches, (
            f"{sheet.title} did not keep {payload!r} as a non-formula cell; "
            f"text cells are {text_values}"
        )


class QualityReportFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        export_report.directories = []

    def test_completeness_sheet_keeps_csv_column_names_as_text(self):
        workbook_path = export_report(include_details=True)
        workbook = openpyxl.load_workbook(workbook_path, data_only=False)
        self.assertIn("Completeness", workbook.sheetnames)
        assert_payloads_are_text(workbook["Completeness"])

    def test_missing_values_sheet_keeps_csv_column_names_as_text(self):
        workbook_path = export_report(include_details=True)
        workbook = openpyxl.load_workbook(workbook_path, data_only=False)
        self.assertIn("Missing_Values", workbook.sheetnames)
        assert_payloads_are_text(workbook["Missing_Values"])

    def test_workbook_xml_does_not_contain_column_name_formulas(self):
        workbook_path = export_report(include_details=True)
        bodies = worksheet_formula_bodies(workbook_path)
        payload_bodies = {payload.lstrip("=") for payload in PAYLOADS}
        leaked = [
            (name, body)
            for name, body in bodies
            if body in payload_bodies or f"={body}" in PAYLOADS or body in PAYLOADS
        ]
        self.assertEqual(leaked, [], f"worksheet XML contains column-name formulas: {leaked}")

    def test_completeness_sheet_is_safe_when_detail_sheets_are_omitted(self):
        workbook_path = export_report(include_details=False)
        workbook = openpyxl.load_workbook(workbook_path, data_only=False)
        self.assertIn("Completeness", workbook.sheetnames)
        self.assertNotIn("Missing_Values", workbook.sheetnames)
        assert_payloads_are_text(workbook["Completeness"])
        bodies = worksheet_formula_bodies(workbook_path)
        payload_bodies = {payload.lstrip("=") for payload in PAYLOADS}
        leaked = [item for item in bodies if item[1] in payload_bodies or item[1] in PAYLOADS]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
