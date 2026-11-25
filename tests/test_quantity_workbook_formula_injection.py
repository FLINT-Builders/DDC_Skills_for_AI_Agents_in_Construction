"""Quantity workbook formula injection.

These tests fail while category, material, type, and level labels that start
with "=" are written into Excel as formulas. They pass once those labels are
stored as text (or prefixed with a single quote or tab) before the workbook
is saved.

Run from the repository root:

    python3 -m unittest discover -s tests -v

The cited path is pandas-construction-analysis ``generate_qto_report`` plus
``DataFrame.to_excel``. The same unsanitized write is also exercised for the
other quantity-takeoff workbook writers in this repository:

- ``export_to_excel_formatted`` in the same skill
- ``BIMQuantityTakeoff.to_excel`` (bim-qto)
- ``BIMQuantityExtractor.generate_qto_report`` (ifc-qto-extraction)
- ``process_qto_from_bim`` (xlsx-construction)
- ``IFCQuantityTakeoff._summarize_elements`` and ``export_to_excel``
  (ifc-to-excel)

Not covered here: ``DataFrame.to_csv`` (no spreadsheet CSV importer is run),
the xlsxwriter engine (not installed; pandas uses it only when it is present),
``material_summary`` and ``analyze_by_level`` (they do not write a workbook),
``generate_qto`` / ``IFCExporter.convert`` (they shell out to a converter that
is not in this repository), and non-quantity skills that also call
``DataFrame.to_excel``.
"""

from __future__ import annotations

import ast
import os
import unittest
import zipfile
from pathlib import Path
from contextlib import contextmanager

import pandas as pd
from openpyxl import Workbook

REPO_ROOT = Path(__file__).resolve().parents[1]
PANDAS_SKILL = (
    REPO_ROOT
    / "2_DDC_Book"
    / "2.3-Pandas-LLM-Analysis"
    / "pandas-construction-analysis"
    / "SKILL.md"
)
BIM_QTO_SKILL = (
    REPO_ROOT
    / "2_DDC_Book"
    / "3.2-QTO-Auto-Estimates"
    / "bim-qto"
    / "SKILL.md"
)
IFC_QTO_SKILL = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "BIM-Analysis"
    / "ifc-qto-extraction"
    / "SKILL.md"
)
XLSX_QTO_SKILL = (
    REPO_ROOT
    / "4_DDC_Curated"
    / "Document-Generation"
    / "xlsx-construction"
    / "SKILL.md"
)
IFC_EXCEL_SKILL = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "CAD-Converters"
    / "ifc-to-excel"
    / "SKILL.md"
)

# Benign formulas. openpyxl treats every string longer than one character
# that starts with "=" as a formula, so the body does not change the path.
CATEGORY_FORMULA = "=2+2"
MATERIAL_FORMULA = "=SUM(1,1)"
LEVEL_FORMULA = "=4+4"
TYPE_FORMULA = "=3*3"
# These markers stay inline strings with openpyxl 3.1.5. They are included
# so a writer that starts treating them as formulas fails the same check.
PLUS_LABEL = "+2+2"
MINUS_LABEL = "-2+3"
AT_LABEL = "@SUM(1,1)"

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _python_blocks(skill_path: Path) -> list[str]:
    text = skill_path.read_text(encoding="utf-8")
    blocks = []
    fence = "```python\n"
    start = 0
    while True:
        begin = text.find(fence, start)
        if begin < 0:
            break
        begin += len(fence)
        end = text.find("```", begin)
        if end < 0:
            break
        blocks.append(text[begin:end])
        start = end + 3
    return blocks


def load_skill_namespace(skill_path: Path) -> dict:
    """Execute imports and definitions from a skill file. Skip usage lines."""
    namespace: dict = {"pd": pd}
    for block in _python_blocks(skill_path):
        try:
            tree = ast.parse(block)
        except SyntaxError:
            continue
        keep = [
            node
            for node in tree.body
            if isinstance(
                node,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                    ast.Import,
                    ast.ImportFrom,
                ),
            )
        ]
        if not keep:
            continue
        module = ast.Module(body=keep, type_ignores=[])
        ast.fix_missing_locations(module)
        try:
            exec(compile(module, str(skill_path), "exec"), namespace)
        except (ImportError, ModuleNotFoundError):
            continue
    return namespace


def exec_block_usage(skill_path: Path, needle: str, namespace: dict) -> None:
    """Run the non-definition statements of the code block that contains needle."""
    for block in _python_blocks(skill_path):
        if needle not in block:
            continue
        tree = ast.parse(block)
        usage = [
            node
            for node in tree.body
            if not isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            )
        ]
        module = ast.Module(body=usage, type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, str(skill_path), "exec"), namespace)
        return
    raise AssertionError(f"{needle!r} not found in {skill_path}")


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield path
    finally:
        os.chdir(previous)


def write_text_workbook(frame: pd.DataFrame, path: Path) -> None:
    """Write a workbook where every value is text or a number, never a formula.

    openpyxl's cell setter stores a string that starts with "=" as a formula.
    Setting data_type back to "s" keeps the characters as an inline string,
    which is how a BIM export stores a category name.
    """
    book = Workbook()
    sheet = book.active
    for column, name in enumerate(frame.columns, start=1):
        sheet.cell(1, column).value = str(name)
    for row_index, record in enumerate(frame.itertuples(index=False), start=2):
        for column, value in enumerate(record, start=1):
            cell = sheet.cell(row_index, column)
            cell.value = value
            if isinstance(value, str):
                cell.data_type = "s"
    book.save(path)


def workbook_formulas_and_text(path: Path) -> tuple[list[str], list[str]]:
    formulas: list[str] = []
    texts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not (
                name.startswith("xl/worksheets/") and name.endswith(".xml")
            ) and name != "xl/sharedStrings.xml":
                continue
            root = ast_from_xml(archive.read(name))
            for cell in root.iter(NS + "c"):
                formula = cell.find(NS + "f")
                if formula is not None and formula.text:
                    body = formula.text
                    formulas.append(body if body.startswith("=") else "=" + body)
            for node in root.iter(NS + "t"):
                if node.text:
                    texts.append(node.text)
    return formulas, texts


def ast_from_xml(payload: bytes):
    import xml.etree.ElementTree as ET

    return ET.fromstring(payload)


def assert_labels_are_text(test: unittest.TestCase, path: Path, payloads: list[str]) -> None:
    formulas, texts = workbook_formulas_and_text(path)
    for payload in payloads:
        test.assertNotIn(
            payload,
            formulas,
            f"{payload!r} was written as a formula in {path.name}: {formulas}",
        )
        test.assertTrue(
            any(
                text == payload or text[1:] == payload and text[:1] in ("'", "\t")
                for text in texts
            ),
            f"{payload!r} was not kept as text in {path.name}. "
            f"formulas={formulas!r} text={texts!r}",
        )


def assert_workbook_has_no_formulas(test: unittest.TestCase, path: Path) -> None:
    formulas, _texts = workbook_formulas_and_text(path)
    test.assertEqual(formulas, [], f"{path.name} already contained formulas: {formulas}")


class QuantityWorkbookFormulaInjectionTest(unittest.TestCase):
    def test_documented_writer_is_openpyxl(self) -> None:
        """The skill install is pandas + openpyxl. pandas prefers xlsxwriter when
        that package is installed, which this test does not cover."""
        import importlib.util

        self.assertIsNone(importlib.util.find_spec("xlsxwriter"))
        from pandas.io.excel._util import get_default_engine

        self.assertEqual(get_default_engine("xlsx", mode="writer"), "openpyxl")

    def test_qto_report_labels_from_text_export_are_not_formulas(self) -> None:
        namespace = load_skill_namespace(PANDAS_SKILL)
        self.assertIn("generate_qto_report", namespace)
        frame = pd.DataFrame(
            {
                "ElementId": ["E001", "E002", "E003", "E004"],
                "Category": [CATEGORY_FORMULA, "Wall", "Floor", AT_LABEL],
                "Material": ["Concrete", MATERIAL_FORMULA, MINUS_LABEL, "Steel"],
                "Volume_m3": [10.0, 5.0, 4.0, 2.0],
                "Area_m2": [3.0, 1.0, 1.0, 1.0],
            }
        )
        with working_directory(Path(self._tmpdir())) as directory:
            source = directory / "bim_export.xlsx"
            write_text_workbook(frame, source)
            assert_workbook_has_no_formulas(self, source)
            namespace["df"] = pd.read_excel(source)
            exec_block_usage(PANDAS_SKILL, "def generate_qto_report", namespace)
            report = directory / "qto_report.xlsx"
            self.assertTrue(report.is_file())
            assert_labels_are_text(
                self,
                report,
                [CATEGORY_FORMULA, MATERIAL_FORMULA, AT_LABEL, MINUS_LABEL],
            )

    def test_qto_report_plus_and_minus_labels_stay_text(self) -> None:
        namespace = load_skill_namespace(PANDAS_SKILL)
        frame = pd.DataFrame(
            {
                "ElementId": ["E001", "E002"],
                "Category": [PLUS_LABEL, "Wall"],
                "Material": [MINUS_LABEL, "Concrete"],
                "Volume_m3": [1.0, 2.0],
                "Area_m2": [1.0, 1.0],
            }
        )
        with working_directory(Path(self._tmpdir())) as directory:
            source = directory / "bim_export.xlsx"
            write_text_workbook(frame, source)
            namespace["df"] = pd.read_excel(source)
            exec_block_usage(PANDAS_SKILL, "def generate_qto_report", namespace)
            assert_labels_are_text(
                self,
                directory / "qto_report.xlsx",
                [PLUS_LABEL, MINUS_LABEL],
            )

    def test_export_to_excel_formatted_labels_are_not_formulas(self) -> None:
        namespace = load_skill_namespace(PANDAS_SKILL)
        self.assertIn("export_to_excel_formatted", namespace)
        elements = pd.DataFrame(
            {
                "ElementId": ["E001", "E002"],
                "Category": [CATEGORY_FORMULA, "Wall"],
                "Material": [MATERIAL_FORMULA, "Concrete"],
                "Volume_m3": [10.0, 4.0],
                "Area_m2": [3.0, 1.0],
                "Level": [LEVEL_FORMULA, "Level 1"],
            }
        )
        summary = namespace["generate_qto_report"](elements)
        with working_directory(Path(self._tmpdir())) as directory:
            namespace["elements"] = elements
            namespace["qto_summary"] = summary
            exec_block_usage(
                PANDAS_SKILL, "def export_to_excel_formatted", namespace
            )
            assert_labels_are_text(
                self,
                directory / "project_report.xlsx",
                [CATEGORY_FORMULA, MATERIAL_FORMULA, LEVEL_FORMULA],
            )

    def test_bim_qto_to_excel_labels_are_not_formulas(self) -> None:
        namespace = load_skill_namespace(BIM_QTO_SKILL)
        self.assertIn("BIMQuantityTakeoff", namespace)
        frame = pd.DataFrame(
            {
                "Category": [CATEGORY_FORMULA, "Walls"],
                "Type Name": [TYPE_FORMULA, TYPE_FORMULA],
                "Level": [LEVEL_FORMULA, "Level 1"],
                "Volume": [10.0, 4.0],
                "Material": [MATERIAL_FORMULA, "Concrete"],
            }
        )
        with working_directory(Path(self._tmpdir())) as directory:
            namespace["qto"] = namespace["BIMQuantityTakeoff"](frame)
            exec_block_usage(BIM_QTO_SKILL, "qto.to_excel", namespace)
            assert_labels_are_text(
                self,
                directory / "qto_report.xlsx",
                [CATEGORY_FORMULA, TYPE_FORMULA, LEVEL_FORMULA],
            )

    def test_ifc_qto_report_labels_are_not_formulas(self) -> None:
        namespace = load_skill_namespace(IFC_QTO_SKILL)
        self.assertIn("BIMQuantityExtractor", namespace)
        frame = pd.DataFrame(
            {
                "Category": [CATEGORY_FORMULA, "Walls"],
                "Type Name": [TYPE_FORMULA, "Basic Wall"],
                "Level": [LEVEL_FORMULA, "Level 1"],
                "Volume": [8.0, 2.0],
                "Area": [4.0, 1.0],
                "Length": [3.0, 1.0],
            }
        )
        extractor = namespace["BIMQuantityExtractor"](".")
        quantities = extractor.extract_quantities(frame)
        with working_directory(Path(self._tmpdir())) as directory:
            output = directory / "qto_report.xlsx"
            extractor.generate_qto_report(quantities, str(output))
            assert_labels_are_text(
                self,
                output,
                [CATEGORY_FORMULA, TYPE_FORMULA, LEVEL_FORMULA],
            )

    def test_process_qto_from_bim_labels_are_not_formulas(self) -> None:
        namespace = load_skill_namespace(XLSX_QTO_SKILL)
        self.assertIn("process_qto_from_bim", namespace)
        frame = pd.DataFrame(
            {
                "Category": [CATEGORY_FORMULA, "Wall"],
                "Type": [TYPE_FORMULA, "Basic Wall"],
                "Volume": [10.0, 2.0],
                "Area": [3.0, 1.0],
                "Length": [4.0, 1.0],
                "Count": [1, 1],
            }
        )
        with working_directory(Path(self._tmpdir())) as directory:
            source = directory / "project.xlsx"
            write_text_workbook(frame, source)
            assert_workbook_has_no_formulas(self, source)
            output = directory / "qto_output.xlsx"
            namespace["process_qto_from_bim"](str(source), str(output))
            assert_labels_are_text(
                self,
                output,
                [CATEGORY_FORMULA, TYPE_FORMULA],
            )

    def test_ifc_to_excel_summary_labels_are_not_formulas(self) -> None:
        namespace = load_skill_namespace(IFC_EXCEL_SKILL)
        self.assertIn("IFCQuantityTakeoff", namespace)
        frame = pd.DataFrame(
            {
                "GlobalId": ["id-1", "id-2"],
                "IfcType": ["IfcWall", "IfcWall"],
                "Type Name": [TYPE_FORMULA, "Basic Wall"],
                "Volume": [9.0, 1.0],
                "Area": [3.0, 1.0],
                "Length": [2.0, 1.0],
            }
        )
        takeoff = namespace["IFCQuantityTakeoff"](None)
        summary = takeoff._summarize_elements(frame, "Type Name")
        with working_directory(Path(self._tmpdir())) as directory:
            output = directory / "project_qto.xlsx"
            takeoff.export_to_excel({"Walls": summary}, str(output))
            assert_labels_are_text(self, output, [TYPE_FORMULA])

    def _tmpdir(self) -> str:
        holder = getattr(self, "_held_tmp", None)
        if holder is None:
            import tempfile

            holder = tempfile.TemporaryDirectory()
            self.addCleanup(holder.cleanup)
            self._held_tmp = holder
        return holder.name


if __name__ == "__main__":
    unittest.main()
