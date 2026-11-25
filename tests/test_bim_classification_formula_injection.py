"""Spreadsheet formula injection through BIM classification exports.

These tests fail while the vulnerability is present. They pass once
element_id, element_name, and category are sanitized on every result
frame before that frame is written to Excel.

The documented batch path is read_excel, classify_batch, then
DataFrame.to_excel with no extra arguments. export_results builds the
other result frame that carries the same fields.

Run (pandas and openpyxl installed, xlsxwriter not installed):

    python3 -m unittest tests.test_bim_classification_formula_injection -v
"""

import importlib.util
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook
from pandas.io.excel._util import get_default_engine

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "1_DDC_Toolkit" / "BIM-Analysis" / "bim-classification-ai" / "SKILL.md"

# Bodies that must still be visible as text after sanitizing the leading "=".
PRESERVED_TEXT = ("1+1", "2+3", "9+9", "@SUM(1,1)", "10*2", "Concrete Floor Slab", "Floors", "W-2", "W-3")

FORMULA_ELEMENT = re.compile(r"<f(?:\s[^>]*)?(?:/>|>.*?</f>)", re.DOTALL)


def load_skill_namespace():
    text = SKILL.read_text(encoding="utf-8")
    for part in text.split("```python\n")[1:]:
        code = part.split("\n```", 1)[0]
        if "class BIMClassificationAI" in code:
            namespace = {"__name__": "bim_classification_ai_skill"}
            exec(compile(code, str(SKILL), "exec"), namespace)
            return namespace
    raise AssertionError(f"BIMClassificationAI implementation not found in {SKILL}")


def write_text_element_table(path, rows):
    """Write an element table whose name cells are text, including a leading '='.

    BIM element names are strings. A writer that stores every value as a
    shared/inline string keeps a leading '=' inert. Setting data_type after
    the value assignment matches that file shape: openpyxl would otherwise
    promote the same characters to a formula while building this fixture.
    """
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["element_id", "name", "category"])
    for row in rows:
        worksheet.append([row["element_id"], row["name"], row["category"]])
        for column in range(1, 4):
            cell = worksheet.cell(worksheet.max_row, column)
            if isinstance(cell.value, str):
                cell.data_type = "s"
    workbook.save(path)


def worksheet_formula_snippets(path):
    snippets = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.startswith("xl/worksheets/"):
                continue
            xml = archive.read(name).decode("utf-8")
            for match in FORMULA_ELEMENT.finditer(xml):
                snippets.append(f"{name}: {match.group(0)}")
    return snippets


def cell_texts(path):
    workbook = load_workbook(path)
    texts = []
    formula_cells = []
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    formula_cells.append(f"{worksheet.title}!{cell.coordinate}={cell.value!r}")
                if cell.value is not None:
                    texts.append(str(cell.value))
    return "\n".join(texts), formula_cells


ELEMENT_ROWS = [
    {"element_id": "=9+9", "name": "=1+1", "category": "=2+3"},
    {"element_id": "W-2", "name": "=@SUM(1,1)", "category": "=10*2"},
    {"element_id": "W-3", "name": "Concrete Floor Slab", "category": "Floors"},
]


class BimClassificationFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = load_skill_namespace()
        cls.classifier_type = cls.skill["BIMClassificationAI"]

    def setUp(self):
        engine = get_default_engine("xlsx", mode="writer")
        self.assertEqual(
            engine,
            "openpyxl",
            "pandas writes xlsx with xlsxwriter when that package is installed. "
            "The skill install is pandas and openpyxl, so this spec expects openpyxl.",
        )
        self.assertIsNone(importlib.util.find_spec("xlsxwriter"))

    def assert_export_is_inert(self, path):
        formula_xml = worksheet_formula_snippets(path)
        self.assertEqual(
            formula_xml,
            [],
            "workbook contains worksheet formula elements:\n" + "\n".join(formula_xml),
        )
        text, formula_cells = cell_texts(path)
        self.assertEqual(
            formula_cells,
            [],
            "workbook contains formula cells:\n" + "\n".join(formula_cells),
        )
        for fragment in PRESERVED_TEXT:
            self.assertIn(fragment, text, f"exported workbook dropped {fragment!r}")
        # A normal name stays text equal to the model value, not a rewritten token.
        self.assertIn("Concrete Floor Slab", text)
        workbook = load_workbook(path)
        benign = [
            cell
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if cell.value == "Concrete Floor Slab"
        ]
        self.assertTrue(benign)
        self.assertNotEqual(benign[0].data_type, "f")

    def test_documented_batch_export_does_not_store_formulas(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bim_elements.xlsx"
            output = Path(directory) / "classified_elements.xlsx"
            write_text_element_table(source, ELEMENT_ROWS)

            # Documented batch path from SKILL.md.
            elements = pd.read_excel(source)
            self.assertEqual(elements.loc[0, "name"], "=1+1")
            self.assertEqual(elements.loc[0, "category"], "=2+3")
            self.assertEqual(str(elements.loc[0, "element_id"]), "=9+9")
            self.assertEqual(elements.loc[1, "name"], "=@SUM(1,1)")
            self.assertEqual(elements.loc[2, "name"], "Concrete Floor Slab")

            classifier = self.classifier_type()
            classified = classifier.classify_batch(elements)
            classified.to_excel(output)

            self.assert_export_is_inert(output)

    def test_export_results_workbook_does_not_store_formulas(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "classified_elements.xlsx"
            classifier = self.classifier_type()
            for row in ELEMENT_ROWS:
                classifier.classify_element(row["element_id"], row["name"], row["category"])
            classifier.export_results().to_excel(output)
            self.assert_export_is_inert(output)


if __name__ == "__main__":
    unittest.main()
