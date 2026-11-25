"""Estimate workbook cells must keep quantity and rule text as text.

A quantity takeoff can set an element id, name, or unit to a spreadsheet
formula. Pricing rules can do the same for a rule id or assembly code, and the
project name is written to the workbook as well. These tests load the
generator from SKILL.md, export a workbook, and require those values to come
back as text that still contains the original characters.

The tests fail while the generator writes a leading "=" into an Excel formula
cell.
"""

from __future__ import annotations

import re
import zipfile
from io import StringIO
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook
from openpyxl.cell.cell import Cell

SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"


def load_generator():
    source = SKILL_MD.read_text()
    match = re.search(r"```python\n(.*?)```", source, re.S)
    if match is None:
        raise AssertionError(f"{SKILL_MD} does not contain the generator implementation")
    namespace: dict = {}
    exec(match.group(1), namespace)
    return namespace


@pytest.fixture(scope="module")
def skill():
    return load_generator()


def _xml_for_sheet(path: Path, workbook, sheet_name: str) -> str:
    sheet_index = workbook.sheetnames.index(sheet_name) + 1
    with zipfile.ZipFile(path) as workbook_zip:
        return workbook_zip.read(f"xl/worksheets/sheet{sheet_index}.xml").decode()


def assert_stored_as_text(path: Path, sheet_name: str, coordinate: str, original: str) -> None:
    workbook = load_workbook(path)
    cell = workbook[sheet_name][coordinate]
    assert isinstance(cell, Cell)
    xml = _xml_for_sheet(path, workbook, sheet_name)
    element = re.search(rf'<c r="{re.escape(coordinate)}"(?: [^>]*)?>(.*?)</c>', xml)
    assert element is not None, f"{sheet_name}!{coordinate} is missing from {path}"
    assert cell.data_type != "f" and "<f" not in element.group(1), (
        f"{sheet_name}!{coordinate} was stored as a formula ({cell.value!r}), "
        f"so opening the workbook evaluates it"
    )
    rendered = "" if cell.value is None else str(cell.value)
    assert original in rendered, (
        f"{sheet_name}!{coordinate} no longer contains the supplied text {original!r}: {rendered!r}"
    )


def _priced_generator(skill, project_name: str, rule_name: str, rule_id: str = "W-001", assembly_code: str = "09.29.10"):
    generator = skill["AutoEstimateGenerator"](project_name)
    generator.add_pricing_rule(
        skill["PricingRule"](
            rule_id=rule_id,
            name=rule_name,
            element_type=skill["ElementType"].WALL,
            conditions={},
            unit_cost=45.0,
            assembly_code=assembly_code,
        )
    )
    return generator


def test_csv_quantity_takeoff_is_stored_as_text(skill, tmp_path: Path):
    """CSV quantity items are the file a model export hands the generator."""
    takeoff = pd.read_csv(
        StringIO(
            "element_id,element_type,name,quantity,unit\n"
            '"=1+1",wall,"=IF(TRUE,2,",10,"=2+3"\n'
            '"=7+7",wall,"=1+1",8,"=8+8"\n'
        )
    )
    generator = _priced_generator(skill, "Office Building A", "Drywall")
    generator.load_qto_from_df(takeoff)

    output = tmp_path / "estimate.xlsx"
    generator.export_to_excel(output)

    assert_stored_as_text(output, "Estimate", "A2", "=1+1")
    assert_stored_as_text(output, "Estimate", "B2", "=IF(TRUE,2,")
    assert_stored_as_text(output, "Estimate", "D2", "=2+3")
    assert_stored_as_text(output, "Estimate", "A3", "=7+7")
    assert_stored_as_text(output, "Estimate", "B3", "=1+1")
    assert_stored_as_text(output, "Estimate", "D3", "=8+8")


def test_direct_quantity_items_are_stored_as_text(skill, tmp_path: Path):
    """The quick start assigns quantity items directly, without the CSV loader."""
    generator = _priced_generator(skill, "Office Building A", "Interior Wall - Drywall")
    generator.qto_items = [
        skill["QTOItem"]("=4+4", skill["ElementType"].WALL, "=1+1", 12, "=5+5", {"material": "Drywall"})
    ]

    output = tmp_path / "estimate.xlsx"
    generator.export_to_excel(output)

    assert_stored_as_text(output, "Estimate", "A2", "=4+4")
    assert_stored_as_text(output, "Estimate", "B2", "=1+1")
    assert_stored_as_text(output, "Estimate", "D2", "=5+5")


def test_unmapped_quantity_items_are_stored_as_text(skill, tmp_path: Path):
    generator = _priced_generator(skill, "Office Building A", "Drywall")
    generator.load_qto_from_df(
        pd.DataFrame(
            [
                {
                    "element_id": "=3+4",
                    "element_type": "door",
                    "name": "=6+6",
                    "quantity": 1,
                    "unit": "=7+1",
                }
            ]
        )
    )

    output = tmp_path / "estimate.xlsx"
    generator.export_to_excel(output)

    assert_stored_as_text(output, "Unmapped", "A2", "=3+4")
    assert_stored_as_text(output, "Unmapped", "C2", "=6+6")
    assert_stored_as_text(output, "Unmapped", "E2", "=7+1")


def test_pricing_rule_text_from_table_is_stored_as_text(skill, tmp_path: Path):
    generator = skill["AutoEstimateGenerator"]("Office Building A")
    generator.load_pricing_rules_from_df(
        pd.DataFrame(
            [
                {
                    "rule_id": "=4+5",
                    "name": "Drywall",
                    "element_type": "wall",
                    "unit_cost": 12.5,
                    "assembly_code": "=8+8",
                }
            ]
        )
    )
    generator.load_qto_from_df(
        pd.DataFrame(
            [
                {
                    "element_id": "W-1",
                    "element_type": "wall",
                    "name": "Lobby wall",
                    "quantity": 3,
                    "unit": "SF",
                }
            ]
        )
    )

    output = tmp_path / "estimate.xlsx"
    generator.export_to_excel(output)

    assert_stored_as_text(output, "Estimate", "G2", "=8+8")
    assert_stored_as_text(output, "Estimate", "H2", "=4+5")


def test_direct_pricing_rule_text_is_stored_as_text(skill, tmp_path: Path):
    generator = _priced_generator(
        skill,
        "Office Building A",
        "Drywall",
        rule_id="=9+9",
        assembly_code="=2+2",
    )
    generator.qto_items = [
        skill["QTOItem"]("W-1", skill["ElementType"].WALL, "Lobby wall", 3, "SF", {})
    ]

    output = tmp_path / "estimate.xlsx"
    generator.export_to_excel(output)

    assert_stored_as_text(output, "Estimate", "G2", "=2+2")
    assert_stored_as_text(output, "Estimate", "H2", "=9+9")


def test_project_name_is_stored_as_text(skill, tmp_path: Path):
    generator = _priced_generator(skill, "=6+6", "Drywall")
    generator.qto_items = [
        skill["QTOItem"]("W-1", skill["ElementType"].WALL, "Lobby wall", 3, "SF", {})
    ]

    output = tmp_path / "estimate.xlsx"
    generator.export_to_excel(output)

    assert_stored_as_text(output, "Summary", "A2", "=6+6")


def test_ordinary_quantity_text_is_preserved(skill, tmp_path: Path):
    generator = _priced_generator(skill, "Office Building A", "Drywall")
    generator.qto_items = [
        skill["QTOItem"]("W-1", skill["ElementType"].WALL, "Interior Wall L1", 500, "SF", {})
    ]

    output = tmp_path / "estimate.xlsx"
    generator.export_to_excel(output)

    workbook = load_workbook(output)
    assert workbook["Estimate"]["B2"].value == "Interior Wall L1 (Drywall)"
    assert workbook["Estimate"]["B2"].data_type != "f"
