"""Failing tests for formula injection in change-order Excel exports.

The CWICR change-order skill and the cost-management change-order processor
write caller-supplied and price-book text into .xlsx cells. openpyxl stores a
string that starts with "=" as a formula, and a spreadsheet application
evaluates it when the exhibit is opened.

These tests execute the Python implementation published in each skill. They
fail while that text is stored as a formula. They pass once every such value
is written as plain text (no formula element in the workbook).

Run from the repository root:

    python3 -m pip install pandas openpyxl pytest
    python3 -m pytest tests/test_change_order_excel_formula_injection.py -v
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CWICR_SKILL = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "CWICR-Database"
    / "cwicr-change-order"
    / "SKILL.md"
)
PROCESSOR_SKILL = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "Cost-Management"
    / "change-order-processor"
    / "SKILL.md"
)

SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# Payloads are cell references, not commands. Each one reads a cost, quantity,
# or impact that a different line already stored as a number.
CO_NUMBER = "=Items!H2"
PROJECT_NAME = "=Items!F2"
HEADER_DESCRIPTION = "=Totals!D2"
UNMATCHED_CODE = "=J2"
BOOK_CODE = "BOOK-001"
BOOK_DESCRIPTION = "=H2"
BOOK_UNIT = "=F2"
CALLER_LINE_DESCRIPTION = "=K2"
HONEST_CODE = "CONC-001"
HONEST_DESCRIPTION = "Foundation concrete"
HONEST_UNIT_COST = 100


def _implementation(skill_path: Path) -> str:
    text = skill_path.read_text(encoding="utf-8")
    heading = text.find("## Technical Implementation")
    if heading < 0:
        raise AssertionError(f"{skill_path} has no Technical Implementation section")
    fence = text.find("```python\n", heading)
    if fence < 0:
        raise AssertionError(f"{skill_path} has no Python implementation block")
    end = text.find("\n```", fence + len("```python\n"))
    if end < 0:
        raise AssertionError(f"{skill_path} implementation block is not closed")
    return text[fence + len("```python\n") : end]


def _load_skill(skill_path: Path, extra_globals: dict | None = None) -> dict:
    # Leave __name__ unset. dataclasses looks the defining module up in
    # sys.modules, and a made-up module name makes that lookup fail.
    namespace: dict = {}
    if extra_globals:
        namespace.update(extra_globals)
    exec(compile(_implementation(skill_path), str(skill_path), "exec"), namespace)
    return namespace


def _load_cwicr_change_order() -> dict:
    # get_unit_cost is annotated with Tuple, and the published block does not
    # import that name, so the class body raises NameError on Python 3.12.
    # Binding typing.Tuple lets the published methods run. It does not change
    # create_change_order, add_change_item, or export_change_order.
    from typing import Tuple

    return _load_skill(CWICR_SKILL, {"Tuple": Tuple})


def formula_cells(path: Path) -> list[tuple[str, str, str | None]]:
    """Return (sheet name, cell reference, formula text) for every <f> element."""
    found: list[tuple[str, str, str | None]] = []
    with zipfile.ZipFile(path) as workbook:
        workbook_xml = ET.fromstring(workbook.read("xl/workbook.xml"))
        relationships = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in relationships
            if rel.tag == f"{{{PACKAGE_REL_NS}}}Relationship"
        }
        sheets = workbook_xml.find(f"{{{SPREADSHEET_NS}}}sheets")
        assert sheets is not None
        for sheet in sheets:
            name = sheet.attrib["name"]
            target = targets[sheet.attrib[f"{{{OFFICE_REL_NS}}}id"]]
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = "xl/" + target
            sheet_xml = ET.fromstring(workbook.read(target))
            for cell in sheet_xml.iter(f"{{{SPREADSHEET_NS}}}c"):
                formula = cell.find(f"{{{SPREADSHEET_NS}}}f")
                if formula is not None:
                    found.append((name, cell.attrib.get("r", ""), formula.text))
    return found


def _cell(worksheet, row: int, header: str):
    columns = {
        worksheet.cell(1, column).value: column
        for column in range(1, worksheet.max_column + 1)
    }
    return worksheet.cell(row, columns[header])


def _assert_plain_text(export, sheet: str, row: int, header: str) -> None:
    cell = _cell(export.book[sheet], row, header)
    written = [item for item in export.formulas if item[0] == sheet and item[1] == cell.coordinate]
    assert cell.data_type != "f" and not written, (
        f"{sheet}!{cell.coordinate} ({header}) was stored as a formula {cell.value!r}. "
        "Opening the workbook evaluates it against the other cost lines."
    )


@pytest.fixture
def cwicr_export(tmp_path: Path) -> SimpleNamespace:
    import openpyxl
    import pandas as pd

    namespace = _load_cwicr_change_order()
    processor_cls = namespace["CWICRChangeOrder"]
    change_type = namespace["ChangeType"]
    price_book = pd.DataFrame(
        [
            {
                "work_item_code": HONEST_CODE,
                "description": "Cast-in-place concrete",
                "unit": "m3",
                "labor_cost": 40.0,
                "material_cost": 55.0,
                "equipment_cost": 5.0,
            },
            {
                "work_item_code": BOOK_CODE,
                "description": BOOK_DESCRIPTION,
                "unit": BOOK_UNIT,
                "labor_cost": 10.0,
                "material_cost": 0.0,
                "equipment_cost": 0.0,
            },
        ]
    )
    processor = processor_cls(price_book, overhead_rate=0.12, profit_rate=0.08)
    processor.create_change_order(
        co_number=CO_NUMBER,
        project_name=PROJECT_NAME,
        description=HEADER_DESCRIPTION,
        justification="=Totals!D2",
    )
    processor.add_change_item(
        CO_NUMBER,
        HONEST_CODE,
        change_type.ADDITION,
        original_qty=3,
        revised_qty=8,
        description=HONEST_DESCRIPTION,
    )
    # Code is not in the price book, so the line description falls back to it
    # and the unit falls back to the literal "unit".
    processor.add_change_item(
        CO_NUMBER,
        UNMATCHED_CODE,
        change_type.MODIFICATION,
        original_qty=1,
        revised_qty=4,
    )
    # Description and unit are copied from the matched price-book row.
    processor.add_change_item(
        CO_NUMBER,
        BOOK_CODE,
        change_type.ADDITION,
        original_qty=0,
        revised_qty=5,
    )
    processor.add_change_item(
        CO_NUMBER,
        HONEST_CODE,
        change_type.ADDITION,
        original_qty=0,
        revised_qty=1,
        description=CALLER_LINE_DESCRIPTION,
    )

    output = tmp_path / "change_order.xlsx"
    processor.export_change_order(CO_NUMBER, str(output))
    book = openpyxl.load_workbook(output)
    items = book["Items"]
    honest_cost = _cell(items, 2, "Unit Cost")
    honest_description = _cell(items, 2, "Description")
    assert honest_cost.data_type != "f"
    assert honest_cost.value == HONEST_UNIT_COST
    assert honest_description.value == HONEST_DESCRIPTION
    assert _cell(items, 3, "Unit").value == "unit"
    return SimpleNamespace(
        path=output,
        book=book,
        formulas=formula_cells(output),
        total_impact=processor.get_change_order(CO_NUMBER).total_impact,
    )


@pytest.mark.parametrize(
    ("sheet", "row", "header"),
    [
        ("Summary", 2, "Change Order"),
        ("Summary", 2, "Project"),
        ("Summary", 2, "Description"),
        ("Items", 3, "Code"),
        ("Items", 3, "Description"),
        ("Items", 4, "Description"),
        ("Items", 4, "Unit"),
        ("Items", 5, "Description"),
    ],
)
def test_cwicr_change_order_text_is_not_an_excel_formula(
    cwicr_export, sheet, row, header
):
    _assert_plain_text(cwicr_export, sheet, row, header)


def test_cwicr_export_workbook_contains_no_formulas(cwicr_export):
    assert cwicr_export.formulas == []
    totals = _cell(cwicr_export.book["Totals"], 2, "Total Impact")
    assert totals.data_type != "f"
    assert totals.value == cwicr_export.total_impact


@pytest.fixture
def processor_export(tmp_path: Path) -> SimpleNamespace:
    import openpyxl

    namespace = _load_skill(PROCESSOR_SKILL)
    processor_cls = namespace["ChangeOrderProcessor"]
    change_type = namespace["ChangeType"]
    processor = processor_cls(project_name="=1+1", original_contract=1_000_000)
    change_order = processor.create_change_order(
        title="=Cost Details!E2",
        description="=Cost Details!F2",
        change_type=change_type.OWNER_REQUESTED,
        created_by="=1+1",
        rfi_reference="=1+1",
        location="=1+1",
    )
    processor.add_cost_item(
        change_order.co_number,
        description="=Cost Details!E2",
        quantity=4,
        unit="=Cost Details!C2",
        unit_cost=25,
        category="=Cost Details!F2",
    )
    output = tmp_path / "change_order_log.xlsx"
    processor.export_log(str(output))
    book = openpyxl.load_workbook(output)
    details = book["Cost Details"]
    assert _cell(details, 2, "Unit Cost").value == 25
    assert _cell(details, 2, "Quantity").value == 4
    assert _cell(details, 2, "Total").value == 100
    return SimpleNamespace(path=output, book=book, formulas=formula_cells(output))


@pytest.mark.parametrize(
    ("sheet", "row", "header"),
    [
        ("Change Orders", 2, "title"),
        ("Cost Details", 2, "Description"),
        ("Cost Details", 2, "Unit"),
        ("Cost Details", 2, "Category"),
    ],
)
def test_change_order_processor_text_is_not_an_excel_formula(
    processor_export, sheet, row, header
):
    _assert_plain_text(processor_export, sheet, row, header)


def test_change_order_processor_workbook_contains_no_formulas(processor_export):
    assert processor_export.formulas == []
