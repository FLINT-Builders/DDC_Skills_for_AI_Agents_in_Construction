"""Formula injection in the resource-based estimator's Excel export.

The tests load the implementation embedded in the skill and run the export
the estimator runs. They fail while a work-item name, unit, or code that
starts with "=" is stored as an OOXML formula. A spreadsheet that opens
that workbook calculates the formula, including a formula that copies the
labor rate into the description cell. The tests pass once those values are
sanitized or written as text and the exported costs are unchanged.

Left untested:
- Resource.name from load_resources_from_df and get_resource_breakdown.
  export_to_excel never writes resource names.
- EstimateBuilder.import_from_df, add_item, and export_to_excel in
  2_DDC_Book/3.1-Cost-Estimation/estimate-builder/SKILL.md. That skill
  writes description, unit, notes, wbs code, project name, and markup name
  through the same unsanitized pandas/openpyxl path.
- export_to_excel in the other skills in this repository.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "2_DDC_Book"
    / "3.1-Cost-Estimation"
    / "cost-estimation-resource"
    / "SKILL.md"
)

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS, "pr": PKG_NS}

FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def load_skill_namespace() -> dict:
    """Execute the skill's implementation block, not the quick-start samples."""
    text = SKILL_PATH.read_text()
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    namespace: dict = {}
    exec(compile(text[start:end], str(SKILL_PATH), "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def skill():
    return load_skill_namespace()


def _sheet_targets(xlsx: Path) -> dict[str, str]:
    with zipfile.ZipFile(xlsx) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_by_id = {
        rel.get("Id"): rel.get("Target")
        for rel in rels.findall("pr:Relationship", NS)
    }
    targets = {}
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        target = rel_by_id[sheet.get(f"{{{REL_NS}}}id")]
        if target.startswith("/"):
            member = target.lstrip("/")
        elif target.startswith("xl/"):
            member = target
        else:
            member = "xl/" + target
        targets[sheet.get("name")] = member
    return targets


def read_sheet_cells(xlsx: Path, sheet_name: str) -> dict[str, dict]:
    member = _sheet_targets(xlsx)[sheet_name]
    with zipfile.ZipFile(xlsx) as archive:
        root = ElementTree.fromstring(archive.read(member))
    cells = {}
    for cell in root.findall("m:sheetData/m:row/m:c", NS):
        formula = cell.find("m:f", NS)
        value = cell.find("m:v", NS)
        inline = cell.find("m:is/m:t", NS)
        cells[cell.get("r")] = {
            "formula": None if formula is None else (formula.text or ""),
            "value": None if value is None or value.text is None else value.text,
            "inline": None if inline is None else (inline.text or ""),
            "type": cell.get("t"),
        }
    return cells


def recalculate(source: Path, destination: Path) -> None:
    completed = subprocess.run(
        ["ssconvert", str(source), str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "ssconvert failed to calculate the workbook "
            f"(exit {completed.returncode}): {completed.stderr}"
        )


def stored_text(info: dict) -> str | None:
    if info["inline"] is not None:
        return info["inline"]
    return info["value"]


def acceptable_text(original: str, stored: str | None) -> bool:
    """Text write keeps the name. Sanitizing may add one apostrophe or drop triggers."""
    if stored is None:
        return False
    if stored == original or stored == "'" + original:
        return True
    stripped = original
    while stripped[:1] in FORMULA_TRIGGERS:
        stripped = stripped[1:]
    return stored == stripped or stored == "'" + stripped


def assert_cell_not_executed(info: dict, original: str, forbidden_result: float | None = None):
    assert info["formula"] is None, (
        f"cell stored formula {info['formula']!r} "
        f"(cached value {info['value']!r}) for input {original!r}"
    )
    stored = stored_text(info)
    assert acceptable_text(original, stored), (
        f"cell value {stored!r} is not the text of {original!r}"
    )
    if forbidden_result is not None and info["value"] is not None:
        try:
            calculated = float(info["value"])
        except ValueError:
            calculated = None
        if calculated is not None:
            assert calculated != pytest.approx(forbidden_result), (
                f"cell calculated to {calculated}, the formula result of {original!r}"
            )


def _resources(skill: dict):
    resource = skill["Resource"](
        "L001",
        "Carpenter",
        skill["ResourceType"].LABOR,
        "MH",
        55.0,
    )
    return resource


def export_loaded_items(skill: dict, items: list[dict], quantity: float, output: Path) -> float:
    estimator = skill["ResourceBasedEstimator"]()
    estimator.add_resource(_resources(skill))
    items_df = pd.DataFrame(items)
    norms_df = pd.DataFrame(
        [
            {
                "work_item_code": item["code"],
                "resource_code": "L001",
                "consumption": 1.5,
                "waste_factor": 1.0,
            }
            for item in items
        ]
    )
    estimator.load_work_items_from_df(items_df, norms_df)
    estimate = estimator.calculate_estimate(
        [{"work_item_code": item["code"], "quantity": quantity} for item in items]
    )
    estimator.export_to_excel(estimate, str(output))
    return estimate["line_items"][0].labor_cost


def row_ref(column: str, row_number: int) -> str:
    return f"{column}{row_number}"


def _calculated_line_items(skill, items, quantity, directory: Path):
    source = directory / "estimate.xlsx"
    calculated = directory / "estimate-calculated.xlsx"
    labor_cost = export_loaded_items(skill, items, quantity=quantity, output=source)
    recalculate(source, calculated)
    return labor_cost, read_sheet_cells(calculated, "Line Items")


def test_loaded_work_item_name_formula_is_not_executed(skill, tmp_path):
    payload = "=E2"
    labor_cost, cells = _calculated_line_items(
        skill,
        [{"code": "03.01.01", "name": payload, "unit": "CY"}],
        100,
        tmp_path,
    )
    assert labor_cost == pytest.approx(8250.0)
    assert float(cells["E2"]["value"]) == pytest.approx(labor_cost)
    assert_cell_not_executed(cells["B2"], payload, forbidden_result=labor_cost)


def test_loaded_work_item_unit_formula_is_not_executed(skill, tmp_path):
    payload = "=1+1"
    labor_cost, cells = _calculated_line_items(
        skill,
        [{"code": "03.01.01", "name": "Concrete placement", "unit": payload}],
        100,
        tmp_path,
    )
    assert float(cells["E2"]["value"]) == pytest.approx(labor_cost)
    assert_cell_not_executed(cells["B2"], "Concrete placement")
    assert_cell_not_executed(cells["D2"], payload, forbidden_result=2)


def test_loaded_work_item_code_formula_is_not_executed(skill, tmp_path):
    payload = "=2+2"
    labor_cost, cells = _calculated_line_items(
        skill,
        [{"code": payload, "name": "Rebar", "unit": "TN"}],
        100,
        tmp_path,
    )
    assert float(cells["E2"]["value"]) == pytest.approx(labor_cost)
    assert_cell_not_executed(cells["A2"], payload, forbidden_result=4)


def _calculated_added_item(skill, code, name, unit, directory: Path):
    estimator = skill["ResourceBasedEstimator"]()
    estimator.add_resource(_resources(skill))
    estimator.add_work_item(
        skill["WorkItem"](
            code=code,
            name=name,
            unit=unit,
            resources=[skill["ResourceNorm"]("L001", 1.5)],
        )
    )
    estimate = estimator.calculate_estimate(
        [{"work_item_code": code, "quantity": 100}]
    )
    source = directory / "added.xlsx"
    calculated = directory / "added-calculated.xlsx"
    estimator.export_to_excel(estimate, str(source))
    recalculate(source, calculated)
    return estimate["line_items"][0].labor_cost, read_sheet_cells(calculated, "Line Items")


def test_add_work_item_name_formula_is_not_executed(skill, tmp_path):
    payload = "=E2"
    labor_cost, cells = _calculated_added_item(
        skill, "03.01.01", payload, "CY", tmp_path
    )
    assert labor_cost == pytest.approx(8250.0)
    assert float(cells["E2"]["value"]) == pytest.approx(labor_cost)
    assert_cell_not_executed(cells["B2"], payload, forbidden_result=labor_cost)


def test_add_work_item_unit_formula_is_not_executed(skill, tmp_path):
    payload = "=1+1"
    labor_cost, cells = _calculated_added_item(
        skill, "03.01.01", "Concrete placement", payload, tmp_path
    )
    assert float(cells["E2"]["value"]) == pytest.approx(labor_cost)
    assert_cell_not_executed(cells["D2"], payload, forbidden_result=2)


def test_add_work_item_code_formula_is_not_executed(skill, tmp_path):
    payload = "=3+3"
    labor_cost, cells = _calculated_added_item(
        skill, payload, "Rebar", "TN", tmp_path
    )
    assert float(cells["E2"]["value"]) == pytest.approx(labor_cost)
    assert_cell_not_executed(cells["A2"], payload, forbidden_result=6)


def test_benign_work_item_export_keeps_description_and_costs(skill, tmp_path):
    items = [
        {
            "code": "03.01.01",
            "name": "Cast-in-place Concrete Foundation",
            "unit": "CY",
        }
    ]
    source = tmp_path / "benign.xlsx"
    labor_cost = export_loaded_items(skill, items, quantity=100, output=source)
    cells = read_sheet_cells(source, "Line Items")
    assert cells["B2"]["formula"] is None
    assert cells["B2"]["inline"] == "Cast-in-place Concrete Foundation"
    assert cells["A2"]["inline"] == "03.01.01"
    assert cells["D2"]["inline"] == "CY"
    assert float(cells["E2"]["value"]) == pytest.approx(labor_cost)
    assert all(info["formula"] is None for info in cells.values())


def test_non_equals_prefixes_are_stored_as_text(skill, tmp_path):
    """xlsx string cells do not calculate '+', '-', or '@'. '=' is the live case."""
    payloads = ["+E2", "-E2", "@E2", "\t=E2", " =E2"]
    items = [
        {"code": f"C{index}", "name": payload, "unit": "CY"}
        for index, payload in enumerate(payloads)
    ]
    source = tmp_path / "prefixes.xlsx"
    calculated = tmp_path / "prefixes-calculated.xlsx"
    labor_cost = export_loaded_items(skill, items, quantity=10, output=source)
    raw = read_sheet_cells(source, "Line Items")
    for index, payload in enumerate(payloads):
        info = raw[row_ref("B", index + 2)]
        assert info["formula"] is None
        assert acceptable_text(payload, stored_text(info))

    recalculate(source, calculated)
    cells = read_sheet_cells(calculated, "Line Items")
    for index, payload in enumerate(payloads):
        assert_cell_not_executed(
            cells[row_ref("B", index + 2)],
            payload,
            forbidden_result=labor_cost,
        )
