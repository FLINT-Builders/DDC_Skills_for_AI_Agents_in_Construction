"""Formula injection in the BIM quantity-takeoff Excel export.

The takeoff implementation lives in the bim-qto skill. These tests load that
implementation and run the documented path: read a schedule, then
BIMQuantityTakeoff.to_excel. They fail while type, category, or level text
that starts with "=" is stored as an Excel formula.
"""

from __future__ import annotations

import csv
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "2_DDC_Book"
    / "3.2-QTO-Auto-Estimates"
    / "bim-qto"
    / "SKILL.md"
)

# One leading character is the accepted way to keep the original text while
# forcing a text cell. Storing the original characters as a non-formula cell
# is also accepted.
TEXT_PREFIXES = ("'", " ", "\t", "\r")

WALL_TYPE = "=1+1"
DOOR_TYPE = "=SUM(1,1)"
WINDOW_TYPE = "=6+6"
FORMULA_CATEGORY = "=2+2"
FORMULA_LEVEL = "=3+3"
BENIGN_TYPE = "Basic Wall"
PLUS_TYPE = "+SUM(1)"
MINUS_CATEGORY = "-2+3"
AT_LEVEL = "@cmd"
MATERIAL_FORMULA = "=4+4"

FORMULA_PAYLOADS = (
    WALL_TYPE,
    DOOR_TYPE,
    WINDOW_TYPE,
    FORMULA_CATEGORY,
    FORMULA_LEVEL,
)


def load_takeoff_class():
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    for block in blocks:
        if "class BIMQuantityTakeoff" in block:
            namespace: dict = {}
            exec(block, namespace)
            return namespace["BIMQuantityTakeoff"]
    raise AssertionError(f"BIMQuantityTakeoff not found in {SKILL_PATH}")


def write_schedule_text(path: Path, rows: list[list]) -> None:
    """Write schedule cells as text, including values that start with '='.

    Setting data_type after value keeps the leading '=' from becoming a
    formula in the schedule itself. Revit-style type names are text.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Type Name", "Category", "Level", "Volume", "Material"])
    for row in rows:
        sheet.append(row)
        for column in range(1, len(row) + 1):
            cell = sheet.cell(sheet.max_row, column)
            if isinstance(cell.value, str):
                cell.data_type = "s"
    workbook.save(path)


def schedule_rows() -> list[list]:
    return [
        [WALL_TYPE, "Walls", FORMULA_LEVEL, 10, MATERIAL_FORMULA],
        [DOOR_TYPE, "Doors", "L1", 2, "Wood"],
        [WINDOW_TYPE, "Windows", "L1", 3, "Glass"],
        [BENIGN_TYPE, FORMULA_CATEGORY, "L2", 4, "Concrete"],
        [PLUS_TYPE, MINUS_CATEGORY, AT_LEVEL, 1, "Steel"],
    ]


def formula_cells(path: Path) -> list[str]:
    workbook = load_workbook(path, data_only=False)
    found = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    found.append(f"{sheet.title}!{cell.coordinate}={cell.value!r}")
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            if not name.endswith(".xml"):
                continue
            xml = package.read(name).decode("utf-8")
            if re.search(r"<f[\s>/]", xml):
                found.append(f"{name} contains a formula element")
    return found


def text_values(path: Path) -> list[str]:
    workbook = load_workbook(path, data_only=False)
    values = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    continue
                if isinstance(cell.value, str):
                    values.append(cell.value)
    return values


def preserves(payload: str, values: list[str]) -> bool:
    for value in values:
        if value == payload:
            return True
        if len(value) == len(payload) + 1 and value[0] in TEXT_PREFIXES and value[1:] == payload:
            return True
    return False


class TakeoffFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.takeoff_cls = load_takeoff_class()
        cls._workspace = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._workspace.cleanup)
        cls.directory = Path(cls._workspace.name)
        cls.schedule = cls.directory / "revit_schedule.xlsx"
        cls.exported = cls.directory / "qto_report.xlsx"
        write_schedule_text(cls.schedule, schedule_rows())
        frame = pd.read_excel(cls.schedule)
        cls.loaded = frame
        takeoff = cls.takeoff_cls(frame)
        takeoff.to_excel(cls.exported, "Office Building")

    def test_schedule_stores_type_names_as_text(self):
        workbook = load_workbook(self.schedule, data_only=False)
        formulas = [
            cell.coordinate
            for row in workbook.active.iter_rows()
            for cell in row
            if cell.data_type == "f"
        ]
        self.assertEqual(formulas, [])
        self.assertEqual(self.loaded.loc[0, "Type Name"], WALL_TYPE)
        self.assertEqual(self.loaded.loc[0, "Level"], FORMULA_LEVEL)
        self.assertEqual(self.loaded.loc[0, "Material"], MATERIAL_FORMULA)
        self.assertEqual(self.loaded.loc[3, "Category"], FORMULA_CATEGORY)
        self.assertEqual(self.loaded.loc[1, "Type Name"], DOOR_TYPE)
        self.assertEqual(self.loaded.loc[2, "Type Name"], WINDOW_TYPE)

    def test_export_does_not_store_schedule_text_as_formulas(self):
        self.assertEqual(formula_cells(self.exported), [])

    def test_formula_like_type_category_and_level_text_is_preserved(self):
        values = text_values(self.exported)
        missing = [payload for payload in FORMULA_PAYLOADS if not preserves(payload, values)]
        self.assertEqual(missing, [])

    def test_category_type_level_and_discipline_sheets_keep_their_labels(self):
        workbook = load_workbook(self.exported, data_only=False)
        expected = {
            "By Category": FORMULA_CATEGORY,
            "By Type": WALL_TYPE,
            "Level-Type Matrix": FORMULA_LEVEL,
            "Walls": WALL_TYPE,
            "Doors": DOOR_TYPE,
            "Windows": WINDOW_TYPE,
        }
        for sheet_name, payload in expected.items():
            sheet = workbook[sheet_name]
            values = [
                cell.value
                for row in sheet.iter_rows()
                for cell in row
                if isinstance(cell.value, str) and cell.data_type != "f"
            ]
            self.assertTrue(
                preserves(payload, values),
                f"{sheet_name} dropped {payload!r}",
            )
            self.assertNotIn(
                "f",
                {cell.data_type for row in sheet.iter_rows() for cell in row},
            )

    def test_benign_labels_and_non_equals_markers_stay_text(self):
        values = text_values(self.exported)
        for payload in (BENIGN_TYPE, PLUS_TYPE, MINUS_CATEGORY, AT_LEVEL, "Walls", "Doors", "Windows"):
            self.assertTrue(preserves(payload, values), payload)
        self.assertEqual(formula_cells(self.exported), [])

    def test_csv_schedule_reaches_the_same_export(self):
        csv_path = self.directory / "revit_schedule.csv"
        exported = self.directory / "qto_from_csv.xlsx"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Type Name", "Category", "Level", "Volume", "Material"])
            writer.writerow([WALL_TYPE, "Walls", FORMULA_LEVEL, 10, MATERIAL_FORMULA])
            writer.writerow([DOOR_TYPE, "Doors", "L1", 2, "Wood"])
            writer.writerow([WINDOW_TYPE, "Windows", "L1", 3, "Glass"])
            writer.writerow([BENIGN_TYPE, FORMULA_CATEGORY, "L2", 4, "Concrete"])
        frame = pd.read_csv(csv_path)
        self.assertEqual(frame.loc[0, "Type Name"], WALL_TYPE)
        self.assertEqual(frame.loc[0, "Level"], FORMULA_LEVEL)
        self.assertEqual(frame.loc[3, "Category"], FORMULA_CATEGORY)
        takeoff = self.takeoff_cls(frame)
        takeoff.to_excel(exported, "Office Building")
        self.assertEqual(formula_cells(exported), [])
        values = text_values(exported)
        for payload in (WALL_TYPE, DOOR_TYPE, WINDOW_TYPE, FORMULA_CATEGORY, FORMULA_LEVEL):
            self.assertTrue(preserves(payload, values), payload)


if __name__ == "__main__":
    unittest.main()
