"""Spreadsheet formula injection through PDF table extraction.

The skills below read table cells from a PDF and write those strings into
.xlsx (and, for one exporter, .csv) with no check for a leading '='.
openpyxl stores a string that starts with '=' as a formula cell. These tests
run the skill code on a PDF whose table contains that text. They fail while
the workbook still contains formula cells, and they pass once every exported
cell that started as formula text is stored as text.

The text has to survive. A fix that drops the cell, or that only quotes a CSV
field, still fails. A leading apostrophe or tab is accepted as a text marker.

Operations that share this write path and are covered here:

- pdf-to-structured: quick start, extract_tables_from_pdf usage, batch_extract_tables
  (per-table workbook and all_tables.xlsx), extract_bom_from_pdf usage,
  clean_extracted_data usage, export_to_multiple_formats (.xlsx and .csv)
- etl-pipeline: extract_from_pdfs then generate_excel_report
- llm-data-automation: pdf_to_dataframe usage that calls DataFrame.to_excel

Not covered, because those snippets never write a spreadsheet:

- pdf-to-structured extract_table_from_area, extract_schedule_from_pdf,
  parse_specification_pdf, extract_text_with_layout
- pdf-to-structured ocr_scanned_pdf and ocr_table_from_scanned_pdf (OCR; the
  batch path under test does not call them)
- etl-pipeline simple_etl_pipeline (reads an existing workbook, not a PDF)
"""

from __future__ import annotations

import ast
import csv
import re
import zipfile
from pathlib import Path

import pdfplumber
from openpyxl.cell.cell import Cell
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

REPO = Path(__file__).resolve().parents[1]
PDF_SKILL = REPO / "2_DDC_Book/2.4-PDF-CAD-to-Data/pdf-to-structured/SKILL.md"
ETL_SKILL = REPO / "2_DDC_Book/4.2-ETL-Automation/etl-pipeline/SKILL.md"
LLM_SKILL = REPO / "2_DDC_Book/2.3-Pandas-LLM-Analysis/llm-data-automation/SKILL.md"

# Cell text taken from the PDF. The ones that start with '=' are formulas once
# a spreadsheet stores them as formula cells. The others are ordinary text that
# this openpyxl version stores as strings; they must still round-trip.
FORMULA_TEXT = ("=1+1", "=2+2", "=SUM(1,1)")
PLAIN_TEXT = ("rebar", "plate", "A-1", "+2+2", "-3+1", "@SUM(1,1)")
TEXT_MARKERS = ("'", "\t")

VENDOR_ROWS = [
    ["Item", "Description", "=SUM(1,1)", "Unit"],
    ["A-1", "=1+1", "4", "ea"],
    ["A-2", "+2+2", "8", "ea"],
    ["A-3", "-3+1", "1", "ea"],
    ["A-4", "@SUM(1,1)", "2", "ea"],
    ["A-5", "rebar", "9", "kg"],
    ["=2+2", "plate", "3", "ea"],
]
BENIGN_ROWS = [
    ["Item", "Description", "Quantity", "Unit"],
    ["B-1", "concrete", "10", "m3"],
]


def _python_blocks(skill_path: Path) -> list[str]:
    text = skill_path.read_text(encoding="utf-8")
    return re.findall(r"```python\n(.*?)```", text, re.S)


def _block(skill_path: Path, needle: str) -> str:
    found = [block for block in _python_blocks(skill_path) if needle in block]
    assert len(found) == 1, f"{needle!r} matched {len(found)} code blocks in {skill_path}"
    return found[0]


def _exec_definitions(namespace: dict, source: str, filename: str) -> None:
    tree = ast.parse(source)
    body = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, filename, "exec"), namespace)


def _exec_block(namespace: dict, source: str, filename: str, replacements: list[tuple[str, str]]) -> None:
    for old, new in replacements:
        assert old in source, f"{old!r} is not in {filename}"
        source = source.replace(old, new)
    exec(compile(source, filename, "exec"), namespace)


def _write_pdf(path: Path, rows: list[list[str]]) -> None:
    """Vendor-style schedule: a ruled table whose cells are the given strings."""
    document = SimpleDocTemplate(str(path), pagesize=letter)
    table = Table(rows, colWidths=[70, 160, 110, 50])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    document.build([table])


def _recovered(stored, original: str) -> bool:
    if stored == original:
        return True
    return (
        isinstance(stored, str)
        and len(stored) == len(original) + 1
        and stored[0] in TEXT_MARKERS
        and stored[1:] == original
    )


def _worksheet_xml(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as workbook:
        names = [name for name in workbook.namelist() if name.startswith("xl/worksheets/sheet")]
        return [workbook.read(name).decode("utf-8") for name in names]


def assert_workbook_stores_text(
    path: Path,
    required: tuple[str, ...] = FORMULA_TEXT,
    plain: tuple[str, ...] = PLAIN_TEXT,
) -> None:
    """The workbook must still contain the PDF text, and none of it as a formula."""
    import openpyxl

    workbook = openpyxl.load_workbook(path)
    stored = []
    try:
        for worksheet in workbook.worksheets:
            for row in worksheet.iter_rows():
                for cell in row:
                    if not isinstance(cell, Cell):
                        continue
                    assert cell.data_type != "f", (
                        f"{path.name} {worksheet.title}!{cell.coordinate} is a formula cell "
                        f"({cell.value!r}). PDF text that starts with '=' must be stored as text."
                    )
                    if cell.value not in (None, ""):
                        stored.append(cell.value)
    finally:
        workbook.close()

    for xml in _worksheet_xml(path):
        assert not re.search(r"<f[\s>/]", xml), f"{path.name} worksheet XML still contains a formula element"

    for original in required:
        assert any(_recovered(value, original) for value in stored), (
            f"{path.name} lost {original!r} from the PDF. Stored values: {stored!r}"
        )
    for original in plain:
        assert any(_recovered(value, original) for value in stored), (
            f"{path.name} lost ordinary cell {original!r}. Stored values: {stored!r}"
        )


def assert_csv_stores_text(path: Path, required: tuple[str, ...] = FORMULA_TEXT) -> None:
    """Parsed CSV fields must not be raw spreadsheet formulas. Quoting is not enough."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        fields = [field for row in csv.reader(handle) for field in row]
    for field in fields:
        assert not (isinstance(field, str) and len(field) > 1 and field.startswith("=")), (
            f"{path.name} has a field that is still formula text {field!r}."
        )
    for original in required:
        assert any(_recovered(field, original) for field in fields), (
            f"{path.name} lost {original!r}. Fields: {fields!r}"
        )


def _vendor_pdf(directory: Path, name: str = "vendor_schedule.pdf") -> Path:
    path = directory / name
    _write_pdf(path, VENDOR_ROWS)
    # The attack depends on the extractor seeing the formula as cell text.
    with pdfplumber.open(path) as pdf:
        table = pdf.pages[0].extract_table()
    assert table is not None
    flat = [cell for row in table for cell in row]
    for original in FORMULA_TEXT:
        assert original in flat, f"fixture PDF did not yield {original!r}: {table!r}"
    return path


def test_batch_extract_tables_writes_formula_text_as_text(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "pdfs"
    out_dir = tmp_path / "extracted"
    pdf_dir.mkdir()
    out_dir.mkdir()
    _vendor_pdf(pdf_dir)
    _write_pdf(pdf_dir / "benign_schedule.pdf", BENIGN_ROWS)

    namespace: dict = {}
    _exec_block(
        namespace,
        _block(PDF_SKILL, "def batch_extract_tables"),
        str(PDF_SKILL),
        [
            ('"./pdf_documents/"', repr(str(pdf_dir))),
            ('"./extracted/"', repr(str(out_dir))),
        ],
    )

    written = sorted(out_dir.glob("*.xlsx"))
    names = [path.name for path in written]
    assert "vendor_schedule_p1_t1.xlsx" in names
    assert "benign_schedule_p1_t1.xlsx" in names
    assert "all_tables.xlsx" in names
    assert namespace["count"] == 2

    for path in written:
        if path.name.startswith("benign_"):
            assert_workbook_stores_text(path, required=(), plain=("concrete",))
        elif path.name == "all_tables.xlsx":
            assert_workbook_stores_text(path, required=FORMULA_TEXT, plain=PLAIN_TEXT + ("concrete",))
        else:
            assert_workbook_stores_text(path)
    benign = out_dir / "benign_schedule_p1_t1.xlsx"
    import openpyxl

    workbook = openpyxl.load_workbook(benign)
    values = [cell.value for row in workbook.active.iter_rows() for cell in row]
    workbook.close()
    assert "concrete" in values


def test_quick_start_writes_formula_text_as_text(tmp_path: Path) -> None:
    pdf_path = _vendor_pdf(tmp_path, "construction_spec.pdf")
    out_path = tmp_path / "extracted_data.xlsx"
    namespace: dict = {}
    _exec_block(
        namespace,
        _block(PDF_SKILL, 'pdfplumber.open("construction_spec.pdf")'),
        str(PDF_SKILL),
        [
            ('"construction_spec.pdf"', repr(str(pdf_path))),
            ('"extracted_data.xlsx"', repr(str(out_path))),
        ],
    )
    assert_workbook_stores_text(out_path)


def test_extract_tables_from_pdf_usage_writes_formula_text_as_text(tmp_path: Path) -> None:
    pdf_path = _vendor_pdf(tmp_path, "material_specification.pdf")
    out_path = tmp_path / "materials.xlsx"
    namespace: dict = {}
    _exec_block(
        namespace,
        _block(PDF_SKILL, "def extract_tables_from_pdf"),
        str(PDF_SKILL),
        [
            ('"material_specification.pdf"', repr(str(pdf_path))),
            ('"materials.xlsx"', repr(str(out_path))),
        ],
    )
    assert_workbook_stores_text(out_path)


def test_extract_bom_usage_writes_formula_text_as_text(tmp_path: Path) -> None:
    pdf_path = _vendor_pdf(tmp_path, "project_bom.pdf")
    out_path = tmp_path / "bom_extracted.xlsx"
    namespace: dict = {}
    _exec_block(
        namespace,
        _block(PDF_SKILL, "def extract_bom_from_pdf"),
        str(PDF_SKILL),
        [
            ('"project_bom.pdf"', repr(str(pdf_path))),
            ('"bom_extracted.xlsx"', repr(str(out_path))),
        ],
    )
    assert_workbook_stores_text(out_path)


def test_clean_extracted_data_usage_writes_formula_text_as_text(tmp_path: Path) -> None:
    pdf_path = _vendor_pdf(tmp_path, "document.pdf")
    out_path = tmp_path / "clean_data.xlsx"
    namespace: dict = {}
    filename = str(PDF_SKILL)
    _exec_definitions(namespace, _block(PDF_SKILL, "def extract_tables_from_pdf"), filename)
    _exec_block(
        namespace,
        _block(PDF_SKILL, "def clean_extracted_data"),
        filename,
        [
            ('"document.pdf"', repr(str(pdf_path))),
            ('"clean_data.xlsx"', repr(str(out_path))),
        ],
    )
    assert_workbook_stores_text(out_path)


def test_export_to_multiple_formats_writes_formula_text_as_text(tmp_path: Path) -> None:
    pdf_path = _vendor_pdf(tmp_path, "document.pdf")
    base = tmp_path / "extracted_data"
    namespace: dict = {}
    filename = str(PDF_SKILL)
    _exec_definitions(namespace, _block(PDF_SKILL, "def extract_tables_from_pdf"), filename)
    _exec_block(
        namespace,
        _block(PDF_SKILL, "def export_to_multiple_formats"),
        filename,
        [
            ('"document.pdf"', repr(str(pdf_path))),
            ('"extracted_data"', repr(str(base))),
        ],
    )
    assert_workbook_stores_text(Path(str(base) + ".xlsx"))
    assert_csv_stores_text(Path(str(base) + ".csv"))


def test_etl_pdf_extract_then_excel_report_writes_formula_text_as_text(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    _vendor_pdf(pdf_dir)
    out_path = tmp_path / "project_report.xlsx"

    namespace: dict = {}
    filename = str(ETL_SKILL)
    _exec_definitions(namespace, _block(ETL_SKILL, "def extract_excel_files"), filename)
    _exec_definitions(namespace, _block(ETL_SKILL, "def extract_from_pdfs"), filename)
    _exec_definitions(namespace, _block(ETL_SKILL, "def generate_excel_report"), filename)

    frame = namespace["extract_from_pdfs"](str(pdf_dir))
    summary = namespace["pd"].DataFrame({"note": ["tables"]})
    namespace["generate_excel_report"](frame, summary, str(out_path))
    assert_workbook_stores_text(out_path)


def test_llm_pdf_to_dataframe_usage_writes_formula_text_as_text(tmp_path: Path) -> None:
    pdf_path = _vendor_pdf(tmp_path, "construction_spec.pdf")
    out_path = tmp_path / "extracted_data.xlsx"
    namespace: dict = {}
    _exec_block(
        namespace,
        _block(LLM_SKILL, "def pdf_to_dataframe"),
        str(LLM_SKILL),
        [
            ('"construction_spec.pdf"', repr(str(pdf_path))),
            ('"extracted_data.xlsx"', repr(str(out_path))),
        ],
    )
    assert_workbook_stores_text(out_path)


def test_fixture_pdf_cells_match_the_table_that_was_written(tmp_path: Path) -> None:
    """Guard the fixture itself: a failed extraction must not look like a fix."""
    path = _vendor_pdf(tmp_path)
    with pdfplumber.open(path) as pdf:
        table = pdf.pages[0].extract_tables()
    assert len(table) == 1
    assert table[0][0][2] == "=SUM(1,1)"
    assert table[0][1][1] == "=1+1"
    assert table[0][6][0] == "=2+2"
    assert table[0][5][1] == "rebar"
