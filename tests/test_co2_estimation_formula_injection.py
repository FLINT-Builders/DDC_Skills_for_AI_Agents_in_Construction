"""Formula injection in the co2-estimation carbon workbook.

The skill's carbon report groups BIM rows by Category and Material, then
writes those names as Excel row labels. Names that start with "=" are stored
as worksheet formulas, so a spreadsheet recalculates them on open.

These tests fail while that behavior remains. They pass when
export_carbon_report writes every category and material name as text and the
workbook contains no formula cells. A fix that only moves the name from the
index into a column still fails if the cell is stored as a formula.

Accepted text forms for a name:
- the original name, stored as a string cell
- the original name with one leading "'", space, or tab guard character

Run from the repository root:

    python3 -m unittest tests.test_co2_estimation_formula_injection
"""

from __future__ import annotations

import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "2_DDC_Book"
    / "3.3-4D-BIM-CO2-Simulation"
    / "co2-estimation"
    / "SKILL.md"
)

# Labels an outside model author can put on a BIM category or material.
# "=" is the marker openpyxl stores as a formula. "+B2" stays text today and
# must not be turned into a formula by a partial fix.
CATEGORY_FORMULA = "=B2"
MATERIAL_FORMULA = "=1+B2"
CATEGORY_PLUS = "+B2"
BENIGN_CATEGORY = "Concrete"
BENIGN_MATERIAL = "Steel"

_FORMULA_TAG = re.compile(r"<f(?:\s[^>]*)?>.*?</f>|<f(?:\s[^>]*)?/>", re.DOTALL)
_GUARD_PREFIXES = ("'", " ", "\t")


def _load_reporting_functions():
    """Execute the reporting block from SKILL.md. The product ships this code there."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    namespace = {"pd": pd}
    for block in blocks:
        if "def export_carbon_report" in block and "def generate_carbon_report" in block:
            exec(block, namespace)  # noqa: S102 - the skill under test is the source
            return namespace["generate_carbon_report"], namespace["export_carbon_report"]
    raise AssertionError(f"reporting functions not found in {SKILL_PATH}")


def _string_matches_name(stored: str, original: str) -> bool:
    if stored == original:
        return True
    return (
        len(stored) == len(original) + 1
        and stored[0] in _GUARD_PREFIXES
        and stored[1:] == original
    )


def _formula_cells(path: Path):
    import openpyxl

    workbook = openpyxl.load_workbook(path)
    found = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    found.append(f"{sheet.title}!{cell.coordinate}={cell.value!r}")
    return found


def _worksheet_formula_xml(path: Path):
    found = []
    with zipfile.ZipFile(path) as workbook:
        for name in workbook.namelist():
            if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                continue
            xml = workbook.read(name).decode("utf-8")
            for match in _FORMULA_TAG.findall(xml):
                found.append(f"{name}:{match}")
    return found


def _sheet(path: Path, title: str):
    import openpyxl

    workbook = openpyxl.load_workbook(path)
    return workbook[title]


def _has_number(sheet, expected: float) -> bool:
    for row in sheet.iter_rows():
        for cell in row:
            if cell.data_type == "n" and cell.value is not None:
                if abs(float(cell.value) - expected) < 1e-6:
                    return True
    return False


def _has_text_name(sheet, original: str) -> bool:
    for row in sheet.iter_rows():
        for cell in row:
            if cell.data_type == "s" and isinstance(cell.value, str):
                if _string_matches_name(cell.value, original):
                    return True
    return False


class CarbonReportFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        generate, export = _load_reporting_functions()
        # Keep the skill functions off the class so unittest does not bind them.
        cls._reporting = (generate, export)

    def _export(self, report):
        temporary = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        temporary.close()
        path = Path(temporary.name)
        self.addCleanup(path.unlink, missing_ok=True)
        self._reporting[1](report, path)
        return path

    def _assert_workbook_has_no_formulas(self, path: Path):
        formulas = _formula_cells(path)
        self.assertEqual(
            formulas,
            [],
            "category or material names were stored as Excel formulas: "
            + ", ".join(formulas),
        )
        raw = _worksheet_formula_xml(path)
        self.assertEqual(
            raw,
            [],
            "worksheet XML contains formula elements: " + ", ".join(raw),
        )

    def test_grouped_bim_names_are_text_and_carbon_values_remain(self):
        """Full path: dataframe from a model, group, then export both sheets."""
        elements = pd.DataFrame(
            {
                "Category": [CATEGORY_FORMULA, CATEGORY_PLUS, BENIGN_CATEGORY],
                "Material": [MATERIAL_FORMULA, MATERIAL_FORMULA, BENIGN_MATERIAL],
                "CO2_kg": [10.0, 4.0, 5000.0],
            }
        )
        report = self._reporting[0](elements, "Consultant tower", 100.0)
        self.assertEqual(report["by_category"][CATEGORY_FORMULA], 10.0)
        self.assertEqual(report["by_category"][CATEGORY_PLUS], 4.0)
        self.assertEqual(report["by_material"][MATERIAL_FORMULA], 14.0)

        path = self._export(report)
        self._assert_workbook_has_no_formulas(path)

        by_category = _sheet(path, "By Category")
        by_material = _sheet(path, "By Material")
        self.assertTrue(_has_text_name(by_category, CATEGORY_FORMULA))
        self.assertTrue(_has_text_name(by_category, CATEGORY_PLUS))
        self.assertTrue(_has_text_name(by_category, BENIGN_CATEGORY))
        self.assertTrue(_has_number(by_category, 10.0))
        self.assertTrue(_has_number(by_category, 4.0))
        self.assertTrue(_has_number(by_category, 5000.0))
        self.assertTrue(_has_text_name(by_material, MATERIAL_FORMULA))
        self.assertTrue(_has_text_name(by_material, BENIGN_MATERIAL))
        self.assertTrue(_has_number(by_material, 14.0))
        self.assertTrue(_has_number(by_material, 5000.0))

    def test_duplicate_formula_labels_keep_the_summed_carbon_value(self):
        elements = pd.DataFrame(
            {
                "Category": [CATEGORY_FORMULA, CATEGORY_FORMULA],
                "Material": [MATERIAL_FORMULA, MATERIAL_FORMULA],
                "CO2_kg": [6.0, 4.0],
            }
        )
        report = self._reporting[0](elements, "Consultant tower", 80.0)
        self.assertEqual(report["by_category"][CATEGORY_FORMULA], 10.0)
        self.assertEqual(report["by_material"][MATERIAL_FORMULA], 10.0)

        path = self._export(report)
        self._assert_workbook_has_no_formulas(path)
        by_category = _sheet(path, "By Category")
        by_material = _sheet(path, "By Material")
        self.assertTrue(_has_text_name(by_category, CATEGORY_FORMULA))
        self.assertTrue(_has_text_name(by_material, MATERIAL_FORMULA))
        self.assertTrue(_has_number(by_category, 10.0))
        self.assertTrue(_has_number(by_material, 10.0))

    def test_export_writes_report_keys_as_text_without_regrouping(self):
        """The writer itself must neutralize keys, not only generate_carbon_report."""
        report = {
            "project": CATEGORY_FORMULA,
            "floor_area_m2": 100.0,
            "total_co2_kg": 42.5,
            "total_co2_tonnes": 0.0425,
            "co2_per_m2": 0.425,
            "by_category": {CATEGORY_FORMULA: 42.5, BENIGN_CATEGORY: 1.0},
            "by_material": {MATERIAL_FORMULA: 7.0, BENIGN_MATERIAL: 3.0},
            "benchmark_comparison": {"rating": "A+", "description": "Ultra-low carbon"},
        }
        path = self._export(report)
        self._assert_workbook_has_no_formulas(path)

        by_category = _sheet(path, "By Category")
        by_material = _sheet(path, "By Material")
        self.assertTrue(_has_text_name(by_category, CATEGORY_FORMULA))
        self.assertTrue(_has_text_name(by_category, BENIGN_CATEGORY))
        self.assertTrue(_has_number(by_category, 42.5))
        self.assertTrue(_has_number(by_category, 1.0))
        self.assertTrue(_has_text_name(by_material, MATERIAL_FORMULA))
        self.assertTrue(_has_text_name(by_material, BENIGN_MATERIAL))
        self.assertTrue(_has_number(by_material, 7.0))
        self.assertTrue(_has_number(by_material, 3.0))


if __name__ == "__main__":
    unittest.main()
