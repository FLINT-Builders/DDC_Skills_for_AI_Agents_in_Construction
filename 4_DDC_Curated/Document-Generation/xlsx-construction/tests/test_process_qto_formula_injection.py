"""Spec for formula injection in process_qto_from_bim.

These tests load the function from the xlsx-construction skill and run it on a
BIM workbook whose Category, Type, Volume, Area, or Length cells are text that
starts with "=". They fail while that text is written back as an Excel formula.
"""

from __future__ import annotations

import re
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from openpyxl import Workbook, load_workbook

SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
SHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def load_process_qto_from_bim():
    """Execute the skill's Python blocks and return process_qto_from_bim.

    The integration example is skipped because it imports a toolkit that is not
    part of this function and it calls the processor on a fixed path.
    """
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    namespace: dict = {"__name__": "xlsx_construction_skill"}
    for block in blocks:
        if "ddc_toolkit" in block:
            continue
        exec(block, namespace)
    function = namespace.get("process_qto_from_bim")
    if function is None:
        raise AssertionError(
            f"process_qto_from_bim was not defined in {SKILL_PATH}"
        )
    return function


def _write_inline_text(cell, value: str) -> None:
    """Store value as an inline string, including when it starts with '='.

    openpyxl's value setter would otherwise turn a leading '=' into a formula.
    A BIM export stores category and type names as text.
    """
    cell._value = value
    cell.data_type = "s"


def write_bim_inline(path: Path, rows: list[tuple]) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "BIM"
    headers = ["Category", "Type", "Volume", "Area", "Length", "Count"]
    for column, header in enumerate(headers, 1):
        worksheet.cell(1, column, header)
    for row_index, row in enumerate(rows, 2):
        for column, value in enumerate(row, 1):
            cell = worksheet.cell(row_index, column)
            if isinstance(value, str) and value.startswith("="):
                _write_inline_text(cell, value)
            else:
                cell.value = value
    workbook.save(path)


def write_bim_shared_strings(path: Path, rows: list[tuple]) -> None:
    """Write a workbook that stores every text cell in shared strings.

    Spreadsheet exports commonly use shared strings rather than inline strings.
    """
    headers = ["Category", "Type", "Volume", "Area", "Length", "Count"]
    table = [headers, *rows]
    shared: list[str] = []

    def shared_index(value: str) -> int:
        if value not in shared:
            shared.append(value)
        return shared.index(value)

    sheet_rows = []
    for row_index, row in enumerate(table, 1):
        cells = []
        for column, value in enumerate(row):
            ref = f"{chr(ord('A') + column)}{row_index}"
            text = isinstance(value, str)
            if row_index == 1 or text:
                cells.append(
                    f'<c r="{ref}" t="s"><v>{shared_index(str(value))}</v></c>'
                )
            else:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    items = "".join(f"<si><t>{escape(value)}</t></si>" for value in shared)
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{MAIN_NS}" count="{len(shared)}" '
        f'uniqueCount="{len(shared)}">{items}</sst>'
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{MAIN_NS}"><sheetData>'
        f'{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{MAIN_NS}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="BIM" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        "</Relationships>"
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", package_rels)
        package.writestr("xl/workbook.xml", workbook_xml)
        package.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        package.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        package.writestr("xl/sharedStrings.xml", shared_xml)


def formula_texts(path: Path) -> list[str]:
    texts = []
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                continue
            root = ET.fromstring(package.read(name))
            for element in root.findall(".//m:f", SHEET_NS):
                texts.append(element.text or "")
    return texts


def close_numbers(actual, expected) -> bool:
    if expected is None:
        return True
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return abs(float(actual) - float(expected)) < 1e-9
    return actual == expected


class ProcessQtoFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.process_qto_from_bim = load_process_qto_from_bim()

    def _run(self, rows, shared_strings=False):
        from tempfile import TemporaryDirectory

        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        source = root / "bim.xlsx"
        output = root / "qto.xlsx"
        if shared_strings:
            write_bim_shared_strings(source, rows)
        else:
            write_bim_inline(source, rows)
        type(self).process_qto_from_bim(str(source), str(output))
        return output

    def assert_no_formula_cells(self, path: Path):
        planted = formula_texts(path)
        self.assertEqual(
            planted,
            [],
            f"QTO workbook contains formula cells: {planted}",
        )
        workbook = load_workbook(path)
        worksheet = workbook["QTO Summary"]
        for row in worksheet.iter_rows():
            for cell in row:
                self.assertNotEqual(
                    cell.data_type,
                    "f",
                    f"{cell.coordinate} is a formula {cell.value!r}",
                )

    def assert_measures(self, path: Path, volume, area, length, count):
        workbook = load_workbook(path)
        worksheet = workbook["QTO Summary"]
        matches = []
        for row in worksheet.iter_rows(min_row=2, max_col=6):
            if all(
                close_numbers(cell.value, expected)
                for cell, expected in zip(row[2:], (volume, area, length, count))
            ):
                matches.append(row)
        self.assertEqual(
            len(matches),
            1,
            "expected one QTO row with the aggregated measures",
        )
        for cell in matches[0]:
            self.assertNotEqual(cell.data_type, "f", cell.value)
        return matches[0]

    def test_category_formula_text_is_not_stored_as_a_formula(self):
        output = self._run(
            [
                ("=1+1", "Basic Wall", 10, 4, 8, 1),
                ("Walls", "Basic Wall", 2, 1, 1, 1),
            ]
        )
        self.assert_no_formula_cells(output)
        self.assert_measures(output, volume=10, area=4, length=8, count=1)
        self.assert_measures(output, volume=2, area=1, length=1, count=1)
        benign = load_workbook(output)["QTO Summary"]
        names = [row[0].value for row in benign.iter_rows(min_row=2, max_col=1)]
        self.assertIn("Walls", names)

    def test_type_formula_reads_quantity_column_only_as_text(self):
        # Two source rows share a type name that is itself a formula over the
        # quantity column. Aggregation must keep the summed volume and must not
        # leave that name as a formula that Excel calculates on open.
        output = self._run(
            [
                ("Walls", "=SUM(C:C)", 4, 2, 3, 1),
                ("Walls", "=SUM(C:C)", 6, 1, 1, 1),
            ]
        )
        self.assert_no_formula_cells(output)
        row = self.assert_measures(output, volume=10, area=3, length=4, count=2)
        self.assertNotEqual(row[1].data_type, "f")

    def test_category_and_type_formulas_on_one_group(self):
        output = self._run(
            [
                ("=1+1", "=SUM(C:C)", 3.5, 1, 2, 1),
                ("=1+1", "=SUM(C:C)", 1.5, 1, 2, 1),
                ("Doors", "D1", 1, 2, 0, 1),
            ]
        )
        self.assert_no_formula_cells(output)
        self.assert_measures(output, volume=5, area=2, length=4, count=2)
        workbook = load_workbook(output)
        door = [
            row
            for row in workbook["QTO Summary"].iter_rows(min_row=2, max_col=6)
            if row[0].value == "Doors" and row[1].value == "D1"
        ]
        self.assertEqual(len(door), 1)
        self.assertTrue(close_numbers(door[0][2].value, 1))
        self.assertTrue(close_numbers(door[0][5].value, 1))

    def test_shared_string_formula_prefixes_are_not_stored_as_formulas(self):
        output = self._run(
            [
                ("=SUM(C:C)", "Wall", 10, 1, 2, 1),
                ("Walls", "=SUM(C:C)", 4.5, 2, 3, 1),
                ("Walls", "=SUM(C:C)", 5.5, 1, 1, 1),
            ],
            shared_strings=True,
        )
        self.assert_no_formula_cells(output)
        self.assert_measures(output, volume=10, area=1, length=2, count=1)
        self.assert_measures(output, volume=10, area=3, length=4, count=2)

    def test_volume_formula_text_is_not_stored_as_a_formula(self):
        output = self._run([("Walls", "Basic Wall", "=SUM(C:C)", 4, 8, 1)])
        self.assert_no_formula_cells(output)
        row = self.assert_measures(output, volume=None, area=4, length=8, count=1)
        self.assertEqual(row[0].value, "Walls")
        self.assertEqual(row[1].value, "Basic Wall")
        self.assertNotEqual(row[2].data_type, "f")

    def test_area_formula_text_is_not_stored_as_a_formula(self):
        output = self._run([("Walls", "Basic Wall", 9, "=SUM(D:D)", 8, 1)])
        self.assert_no_formula_cells(output)
        row = self.assert_measures(output, volume=9, area=None, length=8, count=1)
        self.assertNotEqual(row[3].data_type, "f")

    def test_length_formula_text_is_not_stored_as_a_formula(self):
        output = self._run([("Walls", "Basic Wall", 9, 4, "=SUM(E:E)", 1)])
        self.assert_no_formula_cells(output)
        row = self.assert_measures(output, volume=9, area=4, length=None, count=1)
        self.assertNotEqual(row[4].data_type, "f")


if __name__ == "__main__":
    unittest.main()
