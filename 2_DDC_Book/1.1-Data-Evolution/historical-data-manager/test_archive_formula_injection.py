"""Spreadsheet formula injection through the historical cost archive export.

The historical data manager copies string fields from archived CSV and Excel
files into a normalized workbook. These tests load that code from SKILL.md and
run extract_from_csv, extract_from_legacy_excel, normalize_cost_data, and
DataFrame.to_excel, which is the export the quick start writes.

They fail while a text value that starts with "=" is stored as an Excel
formula. They pass once every string copied from an archive file is sanitized
before that workbook is written. Sanitized text must remain in the workbook
as a non-formula cell, and numeric costs must still be exported.

Not covered here, because those operations do not write the normalized
workbook: LegacySystemConnector.read_timberline_export,
read_primavera_xer, read_mc2_ice, extract_from_database_export, and
generate_migration_report. A formula string in the amount or cost field
aborts normalize_cost_data before to_excel, so that field is not asserted.
"""

from __future__ import annotations

import csv
import importlib.util
import re
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("openpyxl")
pytest.importorskip("xlrd")
xlwt = pytest.importorskip("xlwt")

from openpyxl import Workbook, load_workbook

# pandas selects xlsxwriter over openpyxl when it is installed
# (pandas/io/excel/_util.py get_default_engine). The skill installs openpyxl.
if importlib.util.find_spec("xlsxwriter") is not None:
    raise RuntimeError(
        "xlsxwriter is installed, so DataFrame.to_excel would not use openpyxl"
    )

SKILL_PATH = Path(__file__).with_name("SKILL.md")

# Cost indices published in HistoricalDataManager.normalize_cost_data.
_COST_INDEX_2015 = 0.82
_COST_INDEX_2016 = 0.84
_COST_INDEX_2018 = 0.90
_COST_INDEX_2026 = 1.18

_DESCRIPTION_FORMULA = "=1000+1"
_NOTES_FORMULA = "=Z99"
_PROJECT_FORMULA = "=PROJECTTOKEN"
_HEADER_FORMULA = "=HEADERTOKEN"
_MARKERS = ("1000+1", "Z99", "PROJECTTOKEN", "HEADERTOKEN")


def _load_historical_data_manager():
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    assert match, f"no Python implementation in {SKILL_PATH}"
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(match.group(1), str(SKILL_PATH), "exec"), namespace)
    return namespace["HistoricalDataManager"]


def _write_formula_as_text(cell, value: str) -> None:
    """Store value as an inline string, including when it starts with '='.

    openpyxl's cell setter would otherwise record that string as a formula.
    The archive file under test is a text field, matching the finding.
    """
    cell.value = value
    if value.startswith("="):
        cell.data_type = "s"
        cell._value = value


def _export_records(manager, records, destination: Path):
    normalized = manager.normalize_cost_data(records, base_year=2026)
    normalized.to_excel(destination, index=False)
    return normalized


def _assert_no_formula_cells(workbook_path: Path) -> None:
    with zipfile.ZipFile(workbook_path) as archive:
        sheet_names = [
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        ]
        assert sheet_names, "normalized workbook has no worksheets"
        for name in sheet_names:
            sheet_xml = archive.read(name).decode("utf-8")
            assert "<f" not in sheet_xml, (
                f"{name} contains an Excel formula element written from archive text"
            )

    workbook = load_workbook(workbook_path, data_only=False)
    try:
        for worksheet in workbook.worksheets:
            for row in worksheet.iter_rows():
                for cell in row:
                    assert cell.data_type != "f", (
                        f"{worksheet.title}!{cell.coordinate} is a formula: {cell.value!r}"
                    )
    finally:
        workbook.close()


def _assert_markers_kept_as_text(workbook_path: Path, markers=_MARKERS) -> None:
    workbook = load_workbook(workbook_path, data_only=False)
    try:
        stored = []
        for worksheet in workbook.worksheets:
            for row in worksheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str):
                        stored.append(cell.value)
    finally:
        workbook.close()
    for marker in markers:
        assert any(marker in value for value in stored), (
            f"archived text {marker!r} was dropped instead of sanitized"
        )


def _assert_escalated_cost(workbook_path: Path, original: float, escalated: float) -> None:
    workbook = load_workbook(workbook_path, data_only=False)
    try:
        worksheet = workbook.active
        headers = [cell.value for cell in next(worksheet.iter_rows(min_row=1, max_row=1))]
        amount_key = "amount" if "amount" in headers else "cost"
        amount_col = headers.index(amount_key) + 1
        normalized_col = headers.index("normalized_cost") + 1
        originals = []
        normalized_values = []
        for row in worksheet.iter_rows(min_row=2):
            originals.append(row[amount_col - 1].value)
            normalized_values.append(row[normalized_col - 1].value)
    finally:
        workbook.close()
    assert original in originals
    assert escalated == pytest.approx(normalized_values[originals.index(original)])


def test_csv_archive_text_is_not_written_as_a_formula(tmp_path: Path):
    HistoricalDataManager = _load_historical_data_manager()
    source = tmp_path / "old_project.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["description", "amount", _HEADER_FORMULA, "project_id", "vendor"]
        )
        writer.writerow(
            [_DESCRIPTION_FORMULA, 2500, _NOTES_FORMULA, _PROJECT_FORMULA, "Acme Concrete"]
        )
        writer.writerow(["Rebar", 1800, "plain note", "SAFE-PROJECT", "Safe Vendor"])

    manager = HistoricalDataManager(str(tmp_path))
    record = manager.extract_from_csv(str(source), 2015)
    assert record.data_type == "cost"
    assert record.extracted_data[0]["description"] == _DESCRIPTION_FORMULA
    assert record.extracted_data[0][_HEADER_FORMULA] == _NOTES_FORMULA
    assert record.extracted_data[0]["project_id"] == _PROJECT_FORMULA

    destination = tmp_path / "historical_costs_normalized.xlsx"
    _export_records(manager, [record], destination)

    # extract_from_csv assigns its own project_id and normalize_cost_data
    # overwrites that column, so the archived project_id cell is not exported.
    _assert_no_formula_cells(destination)
    _assert_markers_kept_as_text(
        destination, ("1000+1", "Z99", "HEADERTOKEN")
    )
    _assert_escalated_cost(
        destination,
        1800,
        1800 * _COST_INDEX_2026 / _COST_INDEX_2015,
    )
    workbook = load_workbook(destination, data_only=False)
    try:
        values = [
            cell.value
            for row in workbook.active.iter_rows()
            for cell in row
            if isinstance(cell.value, str)
        ]
    finally:
        workbook.close()
    assert "Rebar" in values
    assert "Safe Vendor" in values


def test_xlsx_text_cells_are_not_written_as_formulas(tmp_path: Path):
    HistoricalDataManager = _load_historical_data_manager()
    source = tmp_path / "legacy.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    headers = ["description", "amount", "notes", "project_id", _HEADER_FORMULA]
    for column, header in enumerate(headers, start=1):
        _write_formula_as_text(worksheet.cell(1, column), header)
    values = [_DESCRIPTION_FORMULA, 3200, _NOTES_FORMULA, _PROJECT_FORMULA, "kept"]
    for column, value in enumerate(values, start=1):
        cell = worksheet.cell(2, column)
        if isinstance(value, str):
            _write_formula_as_text(cell, value)
        else:
            cell.value = value
    workbook.save(source)

    reread = load_workbook(source, data_only=False)
    try:
        assert reread.active["A2"].data_type == "s"
        assert reread.active["A2"].value == _DESCRIPTION_FORMULA
        assert reread.active["E1"].data_type == "s"
    finally:
        reread.close()

    manager = HistoricalDataManager(str(tmp_path))
    records = manager.extract_from_legacy_excel(str(source), 2016)
    assert len(records) == 1
    assert records[0].data_type == "cost"
    assert records[0].extracted_data[0]["description"] == _DESCRIPTION_FORMULA
    assert records[0].project_id == _PROJECT_FORMULA

    destination = tmp_path / "historical_costs_normalized.xlsx"
    _export_records(manager, records, destination)

    _assert_no_formula_cells(destination)
    _assert_markers_kept_as_text(destination)
    _assert_escalated_cost(
        destination,
        3200,
        3200 * _COST_INDEX_2026 / _COST_INDEX_2016,
    )


def test_xls_text_labels_are_not_written_as_formulas(tmp_path: Path):
    HistoricalDataManager = _load_historical_data_manager()
    source = tmp_path / "legacy.xls"
    book = xlwt.Workbook()
    sheet = book.add_sheet("Costs")
    for column, header in enumerate(
        ["description", "cost", "notes", "project_id", _HEADER_FORMULA]
    ):
        sheet.write(0, column, header)
    sheet.write(1, 0, _DESCRIPTION_FORMULA)
    sheet.write(1, 1, 900)
    sheet.write(1, 2, _NOTES_FORMULA)
    sheet.write(1, 3, _PROJECT_FORMULA)
    sheet.write(1, 4, "kept")
    book.save(str(source))

    manager = HistoricalDataManager(str(tmp_path))
    records = manager.extract_from_legacy_excel(str(source), 2018)
    assert len(records) == 1
    assert records[0].data_type == "cost"
    assert records[0].extracted_data[0]["description"] == _DESCRIPTION_FORMULA
    assert records[0].extracted_data[0]["cost"] == 900
    assert records[0].project_id == _PROJECT_FORMULA

    destination = tmp_path / "historical_costs_normalized.xlsx"
    _export_records(manager, records, destination)

    _assert_no_formula_cells(destination)
    _assert_markers_kept_as_text(destination)
    _assert_escalated_cost(
        destination,
        900,
        900 * _COST_INDEX_2026 / _COST_INDEX_2018,
    )
