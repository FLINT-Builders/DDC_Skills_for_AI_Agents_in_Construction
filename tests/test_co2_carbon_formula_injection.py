"""Formula injection in the BIM CO2 carbon workbook.

The calculator copies element id, name, material, and level from the model
table into the Details sheet. A value that starts with "=" must be stored as
text. These tests fail while that value is written as a spreadsheet formula.
"""

from __future__ import annotations

import re
import sys
import types
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import Cell

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "1_DDC_Toolkit" / "BIM-Analysis" / "co2-carbon-footprint" / "SKILL.md"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# Concrete C30/37 is 250 kg CO2e per m3. Volume is chosen so the total is not
# confused with the per-unit factor, which is also 250.
VOLUME_M3 = 1.5
EXPECTED_TOTAL = 375.0

# Each payload still has to resolve to a coefficient when it is the material.
CASES = [
    ("element_id", "Element ID", "=1000+23"),
    ("name", "Element Name", "=2+2"),
    ("material", "Material", '=8+8&"Concrete C30/37"'),
    ("level", "Level", "=3+3"),
]


def _implementation() -> str:
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    for block in blocks:
        if "class CO2FootprintCalculator" in block:
            return block
    raise AssertionError(f"no calculator implementation in {SKILL}")


def _calculator():
    module = types.ModuleType("co2_carbon_footprint_skill")
    module.__file__ = str(SKILL)
    sys.modules[module.__name__] = module
    exec(compile(_implementation(), str(SKILL), "exec"), module.__dict__)
    return module.CO2FootprintCalculator()


def _element(field: str, payload: str) -> dict:
    element = {
        "element_id": "EL-1",
        "name": "Beam B1",
        "material": "Concrete C30/37",
        "level": "Level 01",
        "volume": VOLUME_M3,
    }
    element[field] = payload
    return element


def _store_text(cell: Cell, value) -> None:
    """Write a model-table cell as text, including a leading '='."""
    cell.value = "" if isinstance(value, str) else value
    if isinstance(value, str):
        cell._value = value
        cell.data_type = "s"


def _formula_bodies(path: Path) -> list[str]:
    bodies = []
    with zipfile.ZipFile(path) as workbook:
        for name in workbook.namelist():
            if not name.startswith("xl/worksheets/sheet") or not name.endswith(".xml"):
                continue
            root = ET.fromstring(workbook.read(name))
            for formula in root.iter(f"{{{SHEET_NS}}}f"):
                bodies.append(formula.text or "")
    return bodies


def _assert_payload_is_text(path: Path, column: str, payload: str) -> None:
    workbook = load_workbook(path)
    sheet = workbook["Details"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    total_column = headers["Total kg CO2e"]
    field_column = headers[column]
    rows = [
        row
        for row in sheet.iter_rows(min_row=2)
        if row[total_column - 1].value == EXPECTED_TOTAL
    ]
    assert rows, "calculated element was not written to the Details sheet"
    assert len(rows) == 1
    cell = rows[0][field_column - 1]
    assert cell.data_type != "f", (
        f"{column} was stored as a formula ({cell.value!r}); "
        "opening the workbook evaluates it"
    )
    planted = payload[1:] if payload.startswith("=") else payload
    bodies = _formula_bodies(path)
    assert planted not in bodies, f"workbook XML contains formula {planted!r}"
    assert payload not in bodies


@pytest.mark.parametrize("field,column,payload", CASES)
def test_calculate_element_does_not_write_formula(tmp_path, field, column, payload):
    calculator = _calculator()
    result = calculator.calculate_element(_element(field, payload))
    assert result is not None
    assert result.total_kgco2 == EXPECTED_TOTAL

    output = tmp_path / "carbon_report.xlsx"
    calculator.export_results(str(output))
    _assert_payload_is_text(output, column, payload)


@pytest.mark.parametrize("field,column,payload", CASES)
def test_model_table_export_does_not_write_formula(tmp_path, field, column, payload):
    calculator = _calculator()
    table = pd.DataFrame([_element(field, payload)])
    results = calculator.calculate_from_dataframe(table)
    assert len(results) == 1
    assert results[0].total_kgco2 == EXPECTED_TOTAL

    output = tmp_path / "carbon_report.xlsx"
    calculator.export_results(str(output))
    _assert_payload_is_text(output, column, payload)


@pytest.mark.parametrize("field,column,payload", CASES)
def test_excel_model_table_round_trip_does_not_write_formula(tmp_path, field, column, payload):
    """Quick-start path: read the model table from Excel, then export the report."""
    source = tmp_path / "bim_quantities.xlsx"
    incoming = Workbook()
    grid = incoming.active
    element = _element(field, payload)
    columns = list(element.keys())
    for index, name in enumerate(columns, start=1):
        _store_text(grid.cell(1, index), name)
        _store_text(grid.cell(2, index), element[name])
    incoming.save(source)

    table = pd.read_excel(source)
    calculator = _calculator()
    results = calculator.calculate_from_dataframe(table)
    assert len(results) == 1
    assert results[0].total_kgco2 == EXPECTED_TOTAL

    output = tmp_path / "carbon_report.xlsx"
    calculator.export_results(str(output))
    _assert_payload_is_text(output, column, payload)
