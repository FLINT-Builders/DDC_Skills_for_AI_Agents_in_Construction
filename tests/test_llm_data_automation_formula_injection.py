"""Spreadsheet formula injection through the llm-data-automation skill.

The skill's documented path loads pdf_to_dataframe from SKILL.md, extracts
every PDF table header and cell, and writes them with DataFrame.to_excel.
These tests run that path. They fail while a header or cell that starts with
"=" is stored as an Excel formula, and they pass once those values are written
as text.

Run from the repository root:

    pip install pytest pandas openpyxl pdfplumber reportlab
    python -m pytest tests/test_llm_data_automation_formula_injection.py -q
"""

from __future__ import annotations

import ast
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import openpyxl
import pytest
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import PageBreak, SimpleDocTemplate, Table, TableStyle

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "2_DDC_Book"
    / "2.3-Pandas-LLM-Analysis"
    / "llm-data-automation"
    / "SKILL.md"
)
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# Payloads are arithmetic or a local workbook reference. Nothing here contacts
# a network or reads a credential.
HEADER_PAGE_1 = "=2+2"
HEADER_PAGE_2 = "=3+3"
CELL_ARITHMETIC = "=1+1"
CELL_FUNCTION = "=SUM(1,1)"
CELL_WORKBOOK_LINK = "='[costs.xlsx]Sheet1'!A1"
BENIGN_CELL = "Concrete"


def _skill_code_block() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = 0
    while True:
        begin = text.find(marker, start)
        if begin < 0:
            raise AssertionError(f"pdf_to_dataframe code block not found in {SKILL_PATH}")
        begin += len(marker)
        end = text.find("```", begin)
        block = text[begin:end]
        if "def pdf_to_dataframe" in block:
            return block
        start = end + 3


def load_pdf_to_dataframe():
    """Load the skill's pdf_to_dataframe, not a copy of it."""
    module = ast.parse(_skill_code_block())
    kept = [
        node
        for node in module.body
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef))
    ]
    namespace: dict = {}
    exec(compile(ast.Module(body=kept, type_ignores=[]), str(SKILL_PATH), "exec"), namespace)
    function = namespace.get("pdf_to_dataframe")
    if not callable(function):
        raise AssertionError("SKILL.md does not define pdf_to_dataframe")
    return function


def _table(rows: list[list[str]]) -> Table:
    table = Table(rows)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 1, colors.black),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def build_specification_pdf(path: Path) -> None:
    """Two-page specification whose table headers and cells start with '='."""
    page_1 = [
        [HEADER_PAGE_1, "Material"],
        [CELL_ARITHMETIC, BENIGN_CELL],
        ["150", "Brick"],
    ]
    page_2 = [
        [HEADER_PAGE_2, "Note"],
        [CELL_FUNCTION, "Safe"],
        [CELL_WORKBOOK_LINK, "Link"],
    ]
    document = SimpleDocTemplate(str(path), pagesize=letter)
    document.build([_table(page_1), PageBreak(), _table(page_2)])


def worksheet_cells(xlsx_path: Path) -> dict[str, dict[str, str | None]]:
    """Read the xlsx worksheet XML the skill just wrote."""
    with zipfile.ZipFile(xlsx_path) as workbook:
        sheet_names = [
            name
            for name in workbook.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        ]
        assert sheet_names, "workbook has no worksheet"
        root = ET.fromstring(workbook.read(sheet_names[0]))

    cells: dict[str, dict[str, str | None]] = {}
    for cell in root.iter(f"{{{SPREADSHEET_NS}}}c"):
        formula = cell.find(f"{{{SPREADSHEET_NS}}}f")
        inline = cell.find(f"{{{SPREADSHEET_NS}}}is/{{{SPREADSHEET_NS}}}t")
        cached = cell.find(f"{{{SPREADSHEET_NS}}}v")
        cells[cell.attrib["r"]] = {
            "formula": None if formula is None else (formula.text or ""),
            "inline": None if inline is None else inline.text,
            "cached": None if cached is None else cached.text,
        }
    return cells


def assert_payload_stored_as_text(xlsx_path: Path, payload: str) -> None:
    """The extracted text must survive in the workbook and must not be a formula."""
    written = worksheet_cells(xlsx_path)
    workbook = openpyxl.load_workbook(xlsx_path)
    matches = []
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                if payload not in str(cell.value):
                    continue
                matches.append(cell)
                xml_cell = written[cell.coordinate]
                assert xml_cell["formula"] is None, (
                    f"{cell.coordinate} stores {payload!r} as formula "
                    f"{xml_cell['formula']!r}. Excel recalculates formula cells "
                    f"when this workbook is opened."
                )
                assert cell.data_type != "f", (
                    f"{cell.coordinate} data_type is formula for {payload!r}"
                )
    assert matches, f"{payload!r} was dropped from the workbook instead of stored as text"


@pytest.fixture(scope="module")
def extracted_workbook(tmp_path_factory):
    directory = tmp_path_factory.mktemp("pdf-formula")
    pdf_path = directory / "construction_spec.pdf"
    xlsx_path = directory / "extracted_data.xlsx"
    build_specification_pdf(pdf_path)

    pdf_to_dataframe = load_pdf_to_dataframe()
    frame = pdf_to_dataframe(str(pdf_path))
    # The attacker's strings reached the frame the skill writes out.
    column_names = [str(name) for name in frame.columns]
    body_values = [str(value) for value in frame.to_numpy().ravel()]
    assert HEADER_PAGE_1 in column_names
    assert HEADER_PAGE_2 in column_names
    assert CELL_ARITHMETIC in body_values
    assert CELL_FUNCTION in body_values
    assert CELL_WORKBOOK_LINK in body_values
    assert BENIGN_CELL in body_values

    frame.to_excel(xlsx_path, index=False)
    return frame, xlsx_path


def test_pdf_header_starting_with_equals_is_not_an_excel_formula(extracted_workbook):
    _, xlsx_path = extracted_workbook
    assert_payload_stored_as_text(xlsx_path, HEADER_PAGE_1)
    assert_payload_stored_as_text(xlsx_path, HEADER_PAGE_2)


def test_pdf_body_cell_starting_with_equals_is_not_an_excel_formula(extracted_workbook):
    _, xlsx_path = extracted_workbook
    assert_payload_stored_as_text(xlsx_path, CELL_ARITHMETIC)


def test_pdf_function_formula_cell_is_not_an_excel_formula(extracted_workbook):
    _, xlsx_path = extracted_workbook
    assert_payload_stored_as_text(xlsx_path, CELL_FUNCTION)


def test_pdf_external_workbook_link_is_not_an_excel_formula(extracted_workbook):
    _, xlsx_path = extracted_workbook
    assert_payload_stored_as_text(xlsx_path, CELL_WORKBOOK_LINK)


def test_benign_pdf_cells_stay_text(extracted_workbook):
    _, xlsx_path = extracted_workbook
    assert_payload_stored_as_text(xlsx_path, BENIGN_CELL)
    written = worksheet_cells(xlsx_path)
    benign = [
        cell
        for cell in written.values()
        if cell["inline"] == BENIGN_CELL or cell["cached"] == BENIGN_CELL
    ]
    assert benign
    assert all(cell["formula"] is None for cell in benign)
