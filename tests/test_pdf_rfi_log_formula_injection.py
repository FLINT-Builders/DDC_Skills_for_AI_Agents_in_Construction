"""RFI log cells copied from a PDF must stay text when the workbook is opened.

The pdf-construction skill extracts Subject, From, To, Spec Section, and
Drawing Ref from an RFI PDF and appends them to RFI_Log.xlsx. These tests
run that code. They fail while a leading "=" is stored as a formula, and
they pass once the same strings are saved as text and are not calculated.
"""

from __future__ import annotations

import csv
import importlib.util
import os
import re
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from openpyxl import load_workbook
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "4_DDC_Curated"
    / "Document-Generation"
    / "pdf-construction"
    / "SKILL.md"
)
SHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
COLUMNS = [
    "rfi_number",
    "subject",
    "from",
    "to",
    "date",
    "spec_section",
    "drawing_ref",
]
# Seed row occupies worksheet row 2. Column letters match COLUMNS.
NEIGHBOR = {
    "rfi_number": "SENTINEL_RFI",
    "subject": "SENTINEL_SUBJECT",
    "from": "SENTINEL_FROM",
    "to": "SENTINEL_TO",
    "date": "01/01/2026",
    "spec_section": "SENTINEL_SPEC",
    "drawing_ref": "SENTINEL_DRAW",
}
# Each planted formula reads one sentinel from the neighbor row.
PLANTED = {
    "subject": "=C2",
    "from": "=A2",
    "to": "=B2",
    "spec_section": "=D2",
    "drawing_ref": "=G2",
}


def _python_blocks(skill_text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", skill_text, re.DOTALL)


def _block(blocks: list[str], needle: str) -> str:
    matches = [block for block in blocks if needle in block]
    assert len(matches) == 1, needle
    return matches[0]


def _load_skill_blocks() -> tuple[str, str]:
    skill_text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = _python_blocks(skill_text)
    extract = _block(blocks, "def extract_rfi_data")
    append = _block(blocks, 'tracker.to_excel("RFI_Log.xlsx"')
    return extract, append


def _require_openpyxl_writer() -> None:
    """pandas selects xlsxwriter when that package is installed.

    The skill calls DataFrame.to_excel without an engine. The documented
    install is pandas and openpyxl, so the test refuses a machine where
    xlsxwriter would silently replace that writer.
    """
    if importlib.util.find_spec("xlsxwriter") is not None:
        raise AssertionError(
            "uninstall xlsxwriter before running this test; "
            "pandas would not use the openpyxl writer the skill documents"
        )


def _write_rfi_pdf(path: Path, fields: dict[str, str], rfi_number: str = "45") -> None:
    lines = [
        f"RFI # {rfi_number}",
        f"Subject: {fields['subject']}",
        f"From: {fields['from']}",
        f"To: {fields['to']}",
        f"Date: {fields['date']}",
        f"Specification Section: {fields['spec_section']}",
        f"Drawing Ref: {fields['drawing_ref']}",
    ]
    pdf = canvas.Canvas(str(path), pagesize=letter)
    y = 750
    for line in lines:
        pdf.drawString(72, y, line)
        y -= 18
    pdf.save()


def _seed_log(path: Path) -> None:
    pd.DataFrame([NEIGHBOR], columns=COLUMNS).to_excel(path, index=False)


def _run_skill(directory: Path) -> dict:
    extract_block, append_block = _load_skill_blocks()
    previous = Path.cwd()
    os.chdir(directory)
    try:
        namespace: dict = {}
        exec(extract_block, namespace)
        extracted = namespace["extract_rfi_data"]("RFI_045.pdf")
        exec(append_block, namespace)
        return extracted
    finally:
        os.chdir(previous)


def _formula_coordinates(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as workbook:
        sheet = workbook.read("xl/worksheets/sheet1.xml")
    root = ET.fromstring(sheet)
    found = {}
    for cell in root.findall("m:sheetData/m:row/m:c", SHEET_NS):
        formula = cell.find("m:f", SHEET_NS)
        if formula is not None and formula.text:
            found[cell.attrib["r"]] = formula.text
    return found


def _calculated_rows(path: Path, work: Path) -> list[list[str]]:
    soffice = shutil.which("soffice")
    if soffice is None:
        raise AssertionError(
            "LibreOffice soffice is required to open the log and check "
            "that planted formulas are not calculated"
        )
    profile = work / "lo-profile"
    profile.mkdir(parents=True)
    outdir = work / "lo-out"
    outdir.mkdir()
    completed = subprocess.run(
        [
            soffice,
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to",
            "csv",
            "--outdir",
            str(outdir),
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    csv_path = outdir / f"{path.stem}.csv"
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def _literal(value: str) -> str:
    text = value.strip()
    if text.startswith("'"):
        text = text[1:]
    return text


def _field_problems(log_path: Path, field: str, planted: str, work: Path) -> list[str]:
    workbook = load_workbook(log_path)
    sheet = workbook.active
    headers = [cell.value for cell in sheet[1]]
    if headers != COLUMNS:
        return [f"header row is {headers!r}"]
    column = headers.index(field) + 1
    cell = sheet.cell(sheet.max_row, column)
    coordinate = cell.coordinate
    formulas = _formula_coordinates(log_path)
    calculated = _calculated_rows(log_path, work)
    if not calculated or calculated[0] != COLUMNS:
        return [f"calculated sheet headers are {calculated[:1]!r}"]
    displayed = calculated[-1][column - 1]

    problems = []
    if cell.data_type == "f" or coordinate in formulas:
        problems.append(
            f"{field} cell {coordinate} is an Excel formula "
            f"({cell.value!r}, xml={formulas.get(coordinate)!r})"
        )
    if _literal(displayed) != planted:
        problems.append(
            f"opening the log calculated {field}: displayed {displayed!r}, "
            f"planted text {planted!r}"
        )
    if cell.data_type != "f" and _literal(str(cell.value)) != planted:
        problems.append(
            f"{field} was not stored as the extracted text {planted!r} "
            f"(cell value {cell.value!r}, type {cell.data_type})"
        )
    return problems


@pytest.fixture(autouse=True)
def _documented_excel_writer():
    _require_openpyxl_writer()


def test_equals_fields_from_an_rfi_pdf_are_text_and_do_not_read_other_rows(tmp_path):
    """A filed RFI whose fields start with '=' must not read other log rows."""
    fields = {
        "subject": PLANTED["subject"],
        "from": PLANTED["from"],
        "to": PLANTED["to"],
        "date": "01/15/2026",
        "spec_section": PLANTED["spec_section"],
        "drawing_ref": PLANTED["drawing_ref"],
    }
    _write_rfi_pdf(tmp_path / "RFI_045.pdf", fields)
    _seed_log(tmp_path / "RFI_Log.xlsx")

    extracted = _run_skill(tmp_path)

    for field, planted in PLANTED.items():
        assert extracted[field] == planted, field
    problems = []
    for field, planted in PLANTED.items():
        problems.extend(
            _field_problems(
                tmp_path / "RFI_Log.xlsx", field, planted, tmp_path / f"calc-{field}"
            )
        )
    assert not problems, "\n".join(problems)


def test_arithmetic_formula_in_subject_is_not_evaluated(tmp_path):
    planted = "=1+1"
    fields = {
        "subject": planted,
        "from": "Subcontractor",
        "to": "Document Control",
        "date": "01/15/2026",
        "spec_section": "03 30 00",
        "drawing_ref": "A-101",
    }
    _write_rfi_pdf(tmp_path / "RFI_045.pdf", fields, rfi_number="99")
    _seed_log(tmp_path / "RFI_Log.xlsx")

    extracted = _run_skill(tmp_path)

    assert extracted["subject"] == planted
    assert extracted["rfi_number"] == "99"
    problems = _field_problems(
        tmp_path / "RFI_Log.xlsx", "subject", planted, tmp_path / "calc"
    )
    assert not problems, "\n".join(problems)


def test_hyperlink_formula_in_from_field_is_not_evaluated(tmp_path):
    planted = '=HYPERLINK("http://example.invalid/rfi","click")'
    fields = {
        "subject": "Beam pocket at grid B",
        "from": planted,
        "to": "Document Control",
        "date": "01/15/2026",
        "spec_section": "03 30 00",
        "drawing_ref": "A-101",
    }
    _write_rfi_pdf(tmp_path / "RFI_045.pdf", fields)
    _seed_log(tmp_path / "RFI_Log.xlsx")

    extracted = _run_skill(tmp_path)

    assert extracted["from"] == planted
    problems = _field_problems(
        tmp_path / "RFI_Log.xlsx", "from", planted, tmp_path / "calc"
    )
    assert not problems, "\n".join(problems)


def test_ordinary_rfi_text_is_still_appended(tmp_path):
    fields = {
        "subject": "Clarify beam pocket at grid B",
        "from": "North Concrete",
        "to": "Document Control",
        "date": "01/15/2026",
        "spec_section": "03 30 00",
        "drawing_ref": "S-201",
    }
    _write_rfi_pdf(tmp_path / "RFI_045.pdf", fields, rfi_number="12")
    _seed_log(tmp_path / "RFI_Log.xlsx")

    extracted = _run_skill(tmp_path)

    assert extracted == {
        "rfi_number": "12",
        "subject": fields["subject"],
        "from": fields["from"],
        "to": fields["to"],
        "date": fields["date"],
        "spec_section": fields["spec_section"],
        "drawing_ref": fields["drawing_ref"],
    }
    workbook = load_workbook(tmp_path / "RFI_Log.xlsx")
    sheet = workbook.active
    appended = [cell.value for cell in sheet[sheet.max_row]]
    assert appended == [
        "12",
        fields["subject"],
        fields["from"],
        fields["to"],
        fields["date"],
        fields["spec_section"],
        fields["drawing_ref"],
    ]
    assert _formula_coordinates(tmp_path / "RFI_Log.xlsx") == {}
