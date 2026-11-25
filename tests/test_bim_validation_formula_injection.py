"""Formula injection in the BIM validation report workbook.

The implementation under test is the engine in
``1_DDC_Toolkit/BIM-Analysis/bim-validation-report/SKILL.md``. A coordinator
loads an element table and exports a validation workbook. Element id, name,
and category come from that table. Project and model names are the strings
passed into the report. None of those values may be stored as Excel formulas.

These tests fail while the export still writes a leading ``=`` as a formula.
They pass once those values are neutralized before the workbook is written.

Run from the repository root:

    pip install pandas openpyxl pytest
    python -m pytest tests/test_bim_validation_formula_injection.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook, load_workbook

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "1_DDC_Toolkit"
    / "BIM-Analysis"
    / "bim-validation-report"
    / "SKILL.md"
)

# Benign arithmetic. openpyxl 3.1.5 stores a string whose length is greater
# than 1 and which starts with "=" as a formula cell. The other classic
# markers (+, -, @, tab, carriage return) stay inline strings in that writer
# and are not what these tests cover.
NAME_FORMULA = "=1+1"
CATEGORY_FORMULA = "=2+2"
ELEMENT_ID_FORMULA = "=5+5"
PROJECT_FORMULA = "=3+3"
MODEL_FORMULA = "=4+4"


def _load_skill():
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    implementation = next(
        (block for block in blocks if "class BIMValidationEngine" in block),
        None,
    )
    if implementation is None:
        raise AssertionError(f"no BIMValidationEngine implementation in {SKILL_PATH}")
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(implementation, str(SKILL_PATH), "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def skill():
    return _load_skill()


def _is_formula(value) -> bool:
    return isinstance(value, str) and len(value) > 1 and value.startswith("=")


def _assert_not_formula(cell, where: str) -> None:
    assert cell.data_type != "f" and not _is_formula(cell.value), (
        f"{where} cell {cell.coordinate} is an Excel formula {cell.value!r} "
        f"(data_type={cell.data_type})"
    )


def _write_text_table(path: Path, rows: list[dict]) -> None:
    """Write an element workbook whose payload cells are strings, not formulas.

    Assigning a leading "=" through openpyxl would already make the source
    cell a formula. BIM names are text, so the fixture forces the string type
    after that assignment and checks the saved cell really is text.
    """
    columns = [
        "element_id",
        "name",
        "category",
        "level",
        "material",
        "classification_code",
        "volume",
        "min_x",
        "max_x",
        "min_y",
        "max_y",
        "min_z",
        "max_z",
    ]
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(columns)
    for row_number, row in enumerate(rows, start=2):
        for column_number, column in enumerate(columns, start=1):
            value = row.get(column)
            cell = worksheet.cell(row_number, column_number, value)
            if isinstance(value, str):
                cell.data_type = "s"
    workbook.save(path)

    saved = load_workbook(path)
    saved_sheet = saved.active
    for row_number, row in enumerate(rows, start=2):
        for column_number, column in enumerate(columns, start=1):
            value = row.get(column)
            if not _is_formula(value):
                continue
            cell = saved_sheet.cell(row_number, column_number)
            assert cell.data_type == "s" and cell.value == value, (
                f"fixture stored {column} as {cell.data_type} {cell.value!r}, "
                "so the report would not be the step that introduces the formula"
            )


def _element(**overrides) -> dict:
    element = {
        "element_id": "W-01",
        "name": "Wall A",
        "category": "Walls",
        "level": None,
        "material": None,
        "classification_code": None,
        "volume": 0,
        "min_x": 0,
        "max_x": 0,
        "min_y": 0,
        "max_y": 0,
        "min_z": 0,
        "max_z": 0,
    }
    element.update(overrides)
    return element


def _report_from_table(skill, tmp_path: Path, rows: list[dict], project: str, model: str):
    source = tmp_path / "bim_elements.xlsx"
    _write_text_table(source, rows)
    elements = pd.read_excel(source)
    output = tmp_path / "validation_report.xlsx"
    skill["generate_validation_report"](
        elements,
        project_name=project,
        model_name=model,
        output_path=str(output),
    )
    assert output.is_file()
    return load_workbook(output), elements


def _column_cells(worksheet, header: str):
    headers = [cell.value for cell in worksheet[1]]
    assert header in headers, f"{worksheet.title} headers are {headers}"
    index = headers.index(header)
    cells = list(worksheet.iter_rows(min_row=2))
    assert cells, f"{worksheet.title} has no issue rows"
    return [row[index] for row in cells]


def _summary_cell(worksheet, metric: str):
    headers = [cell.value for cell in worksheet[1]]
    metric_index = headers.index("Metric")
    value_index = headers.index("Value")
    for row in worksheet.iter_rows(min_row=2):
        if row[metric_index].value == metric:
            return row[value_index]
    raise AssertionError(f"Summary sheet has no {metric} row")


def test_issue_element_name_from_text_table_is_not_a_formula(skill, tmp_path):
    workbook, elements = _report_from_table(
        skill,
        tmp_path,
        [_element(name=NAME_FORMULA)],
        project="Office Tower",
        model="Architectural Model",
    )
    assert elements.loc[0, "name"] == NAME_FORMULA
    for cell in _column_cells(workbook["Issues"], "element_name"):
        _assert_not_formula(cell, "Issues element_name")


def test_issue_element_category_from_text_table_is_not_a_formula(skill, tmp_path):
    workbook, elements = _report_from_table(
        skill,
        tmp_path,
        [_element(category=CATEGORY_FORMULA)],
        project="Office Tower",
        model="Architectural Model",
    )
    assert elements.loc[0, "category"] == CATEGORY_FORMULA
    for cell in _column_cells(workbook["Issues"], "element_category"):
        _assert_not_formula(cell, "Issues element_category")


def test_by_category_sheet_uses_the_element_category_text(skill, tmp_path):
    workbook, _elements = _report_from_table(
        skill,
        tmp_path,
        [_element(category=CATEGORY_FORMULA)],
        project="Office Tower",
        model="Architectural Model",
    )
    for cell in _column_cells(workbook["By Category"], "Category"):
        _assert_not_formula(cell, "By Category")


def test_issue_element_id_from_text_table_is_not_a_formula(skill, tmp_path):
    workbook, elements = _report_from_table(
        skill,
        tmp_path,
        [_element(element_id=ELEMENT_ID_FORMULA)],
        project="Office Tower",
        model="Architectural Model",
    )
    assert str(elements.loc[0, "element_id"]) == ELEMENT_ID_FORMULA
    for cell in _column_cells(workbook["Issues"], "element_id"):
        _assert_not_formula(cell, "Issues element_id")


def test_csv_element_name_reaches_the_issue_sheet_as_text(skill, tmp_path):
    source = tmp_path / "bim_elements.csv"
    pd.DataFrame([_element(name=NAME_FORMULA)]).to_csv(source, index=False)
    elements = pd.read_csv(source)
    assert elements.loc[0, "name"] == NAME_FORMULA
    output = tmp_path / "validation_report.xlsx"
    skill["generate_validation_report"](
        elements,
        project_name="Office Tower",
        model_name="Architectural Model",
        output_path=str(output),
    )
    workbook = load_workbook(output)
    for cell in _column_cells(workbook["Issues"], "element_name"):
        _assert_not_formula(cell, "Issues element_name from CSV")


def test_summary_project_name_is_not_a_formula(skill, tmp_path):
    workbook, _elements = _report_from_table(
        skill,
        tmp_path,
        [_element()],
        project=PROJECT_FORMULA,
        model="Architectural Model",
    )
    _assert_not_formula(_summary_cell(workbook["Summary"], "Project"), "Summary Project")


def test_summary_model_name_is_not_a_formula(skill, tmp_path):
    workbook, _elements = _report_from_table(
        skill,
        tmp_path,
        [_element()],
        project="Office Tower",
        model=MODEL_FORMULA,
    )
    _assert_not_formula(_summary_cell(workbook["Summary"], "Model"), "Summary Model")
