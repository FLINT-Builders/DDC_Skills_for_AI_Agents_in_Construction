"""Formula injection in the submittal log export.

These tests load the implementation embedded in
``1_DDC_Toolkit/Document-Control/submittal-tracker/SKILL.md`` and run
``create_submittal`` then ``export_log``. They fail while a title or spec
section that starts with ``=`` is written as an Excel formula cell.

Contractor names and review comments are stored on the submittal but
``export_log`` does not write them, so they are not injection sinks and are
not asserted here. ``submittal_type`` and ``status`` are enum values chosen
by the tracker, not free text, and are not asserted as sinks. Other
Document-Control skills that also call ``DataFrame.to_excel``
(rfi-management, meeting-minutes-generator, as-built-documentation,
contract-clause-analyzer) are outside this export and are not tested.
"""

from __future__ import annotations

import re
import sys
import types
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "1_DDC_Toolkit"
    / "Document-Control"
    / "submittal-tracker"
    / "SKILL.md"
)
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# A single leading character that keeps the original text but stops openpyxl
# from treating the cell as a formula. The cell must still contain the payload.
NEUTRAL_PREFIXES = ("'", " ", "\t")


def _load_skill():
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    assert match, f"no Python implementation block in {SKILL_PATH}"
    module = types.ModuleType("submittal_tracker_skill")
    module.__file__ = str(SKILL_PATH)
    sys.modules[module.__name__] = module
    exec(compile(match.group(1), str(SKILL_PATH), "exec"), module.__dict__)
    return module.__dict__


@pytest.fixture(scope="module")
def skill():
    return _load_skill()


def _export(skill, rows, path: Path):
    tracker = skill["SubmittalTracker"]("Office Tower")
    submittal_type = skill["SubmittalType"].PRODUCT_DATA
    required = date.today() + timedelta(days=14)
    for row in rows:
        tracker.create_submittal(
            spec_section=row.get("spec_section", "09 29 00"),
            title=row.get("title", "Gypsum Board"),
            submittal_type=submittal_type,
            contractor=row.get("contractor", "ABC Drywall"),
            required_date=required,
        )
    tracker.export_log(str(path))


def _sheet_cells(path: Path) -> dict[str, dict]:
    with zipfile.ZipFile(path) as workbook:
        sheets = [
            name
            for name in workbook.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        ]
        assert sheets, "export produced no worksheet"
        roots = [ET.fromstring(workbook.read(name)) for name in sheets]
    cells: dict[str, dict] = {}
    for root in roots:
        for cell in root.iter(f"{NS}c"):
            ref = cell.get("r")
            formula = cell.find(f"{NS}f")
            inline = cell.find(f"{NS}is/{NS}t")
            cells[ref] = {
                "formula": None if formula is None else (formula.text or ""),
                "text": None if inline is None else (inline.text or ""),
            }
    return cells


def _column_for_header(cells: dict[str, dict], header: str) -> str:
    matches = [
        ref[:-1]
        for ref, cell in cells.items()
        if ref[1:] == "1" and ref[:-1].isalpha() and cell["text"] == header
    ]
    assert matches, f"column {header!r} missing from exported log"
    assert len(matches) == 1, f"duplicate {header!r} header"
    return matches[0]


def _data_rows(cells: dict[str, dict], column: str) -> list[str]:
    refs = []
    for ref in cells:
        if ref.startswith(column) and ref[len(column) :].isdigit():
            row_number = int(ref[len(column) :])
            if row_number > 1:
                refs.append(ref)
    return sorted(refs, key=lambda ref: int(ref[len(column) :]))


def _is_inert_text(cell: dict, payload: str) -> bool:
    """True when this cell cannot execute and still shows the submitted text."""
    if cell["formula"] is not None:
        return False
    stored = cell["text"]
    if stored is None:
        return False
    if stored == payload:
        return True
    return any(stored == prefix + payload for prefix in NEUTRAL_PREFIXES)


def _assert_column_contains_inert_payload(path: Path, header: str, payload: str) -> None:
    cells = _sheet_cells(path)
    column = _column_for_header(cells, header)
    data_cells = [cells[ref] for ref in _data_rows(cells, column)]
    assert any(_is_inert_text(cell, payload) for cell in data_cells), (
        f"{header} value {payload!r} was not stored as inert text; cells={data_cells}"
    )


@pytest.mark.parametrize(
    "payload",
    [
        "=B2",
        "=1+1",
        "=SUM(A1:A9)",
        "==1+1",
        "= 1+1",
        '=CONCATENATE(A2,"marker")',
    ],
)
def test_title_formula_is_stored_as_text(skill, tmp_path, payload):
    path = tmp_path / "log.xlsx"
    _export(skill, [{"title": payload}], path)
    _assert_column_contains_inert_payload(path, "Title", payload)


@pytest.mark.parametrize(
    "payload",
    [
        "=B2",
        "=1+1",
        "=SUM(A1:A9)",
        "==1+1",
        "= 1+1",
        '=CONCATENATE(A2,"marker")',
    ],
)
def test_spec_section_formula_is_stored_as_text(skill, tmp_path, payload):
    path = tmp_path / "log.xlsx"
    _export(skill, [{"spec_section": payload}], path)
    _assert_column_contains_inert_payload(path, "Spec", payload)


def test_title_and_spec_section_on_the_same_row_are_both_inert(skill, tmp_path):
    path = tmp_path / "log.xlsx"
    title = '=CONCATENATE(A2,"title")'
    spec_section = '=SUM(A1:A9)'
    _export(skill, [{"title": title, "spec_section": spec_section}], path)
    _assert_column_contains_inert_payload(path, "Title", title)
    _assert_column_contains_inert_payload(path, "Spec", spec_section)


def test_later_row_is_not_left_as_a_formula(skill, tmp_path):
    path = tmp_path / "log.xlsx"
    payload = "=B3"
    _export(
        skill,
        [
            {"title": "Gypsum Board", "spec_section": "09 29 00"},
            {"title": payload, "spec_section": "03 30 00"},
        ],
        path,
    )
    cells = _sheet_cells(path)
    title_column = _column_for_header(cells, "Title")
    title_cells = [cells[ref] for ref in _data_rows(cells, title_column)]
    assert _is_inert_text(title_cells[0], "Gypsum Board")
    assert _is_inert_text(title_cells[1], payload)


def test_ordinary_log_keeps_text_and_adds_no_formulas(skill, tmp_path):
    path = tmp_path / "log.xlsx"
    _export(
        skill,
        [{"title": "Gypsum Board", "spec_section": "09 29 00", "contractor": "ABC Drywall"}],
        path,
    )
    cells = _sheet_cells(path)
    assert all(cell["formula"] is None for cell in cells.values())
    title_column = _column_for_header(cells, "Title")
    spec_column = _column_for_header(cells, "Spec")
    assert cells[f"{title_column}2"]["text"] == "Gypsum Board"
    assert cells[f"{spec_column}2"]["text"] == "09 29 00"


@pytest.mark.parametrize("payload", ["+1+1", "-1+1", "@SUM(A1)"])
def test_other_formula_prefixes_stay_text(skill, tmp_path, payload):
    """openpyxl only promotes a leading '=' to a formula. These prefixes must
    still round-trip as text so a fix cannot drop them."""
    path = tmp_path / "log.xlsx"
    _export(skill, [{"title": payload, "spec_section": payload}], path)
    _assert_column_contains_inert_payload(path, "Title", payload)
    _assert_column_contains_inert_payload(path, "Spec", payload)
