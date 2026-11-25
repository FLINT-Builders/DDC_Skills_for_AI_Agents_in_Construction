"""Contractor strings in the as-built tracker must not become Excel formulas.

The implementation under test is the Python block in SKILL.md. Closeout
staff import a contractor document list and later call export_to_excel.
These tests fail while a string that starts with "=" is written as a
formula cell. They pass once every attacker-controlled value that reaches
a worksheet is stored as text and the original characters are still present.

Fields assigned on the document but not written by export_to_excel
(reviewer, comments, submitted_by, file_path) are not covered. The Summary
sheet's project name is operator input and is not covered.
"""

import ast
import unittest
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd


SKILL_PATH = Path(__file__).resolve().parent / "SKILL.md"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# Distinct formula bodies so a hit can only come from that field.
TITLE = '=HYPERLINK("http://127.0.0.1/ddc-leak","drawing-title")'
DOCUMENT_NUMBER = "=1001+17"
DISCIPLINE = "=2002+28"
CONTRACTOR = "=3003+39"
REVISION = "=4004+46"

# openpyxl stores these as text. They must stay text, not be promoted later.
PLAIN_MARKERS = ("+1+1", "-2+3", "@SUM(1+1)", "\t=1+1")


def _load_skill():
    text = SKILL_PATH.read_text(encoding="utf-8")
    # The skill file is markdown. Pull the first fenced Python block, which
    # is the implementation agents run, and reject a fence that is not code.
    fence = "```python\n"
    start = text.find(fence)
    if start < 0:
        raise AssertionError(f"no python implementation block in {SKILL_PATH}")
    start += len(fence)
    end = text.find("```", start)
    code = text[start:end]
    ast.parse(code)
    namespace = {}
    exec(compile(code, str(SKILL_PATH), "exec"), namespace)
    return namespace["AsBuiltTracker"], namespace["DocumentType"]


def _formula_body(payload):
    if isinstance(payload, str) and payload.startswith("="):
        return payload[1:]
    return payload


def _read_sheets(xlsx_path):
    """Return {sheet_name: [row_dict, ...]} from the xlsx XML Excel opens.

    Each row maps a column letter to {"formula": str | None, "text": str | None}.
    Formula text is the OOXML <f> body (no leading "="). String text includes
    inline strings and shared strings.
    """
    ns = {"m": MAIN_NS}
    with zipfile.ZipFile(xlsx_path) as package:
        workbook = ElementTree.fromstring(package.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(package.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels
            if rel.tag == f"{{{PKG_REL_NS}}}Relationship"
        }
        shared = []
        if "xl/sharedStrings.xml" in package.namelist():
            sroot = ElementTree.fromstring(package.read("xl/sharedStrings.xml"))
            for item in sroot.findall("m:si", ns):
                parts = [node.text or "" for node in item.findall(".//m:t", ns)]
                shared.append("".join(parts))

        sheets = {}
        for sheet in workbook.findall("m:sheets/m:sheet", ns):
            name = sheet.attrib["name"]
            rid = sheet.attrib[f"{{{REL_NS}}}id"]
            target = rid_to_target[rid].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            root = ElementTree.fromstring(package.read(target))
            rows = []
            for row in root.findall("m:sheetData/m:row", ns):
                values = {}
                for cell in row.findall("m:c", ns):
                    ref = cell.attrib["r"]
                    column = "".join(ch for ch in ref if ch.isalpha())
                    formula_node = cell.find("m:f", ns)
                    formula = (
                        formula_node.text
                        if formula_node is not None and formula_node.text
                        else None
                    )
                    text = None
                    cell_type = cell.attrib.get("t")
                    if cell_type == "inlineStr":
                        node = cell.find("m:is/m:t", ns)
                        text = "" if node is None or node.text is None else node.text
                    elif cell_type == "s":
                        node = cell.find("m:v", ns)
                        if node is not None and node.text is not None:
                            text = shared[int(node.text)]
                    elif formula is None:
                        node = cell.find("m:v", ns)
                        if node is not None and node.text is not None:
                            text = node.text
                    values[column] = {"formula": formula, "text": text}
                rows.append(values)
            sheets[name] = rows
        return sheets


def _header_columns(rows):
    columns = {}
    for column, cell in rows[0].items():
        columns[cell["text"]] = column
    return columns


class AsBuiltFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.AsBuiltTracker, cls.DocumentType = _load_skill()

    def _export(self, tracker):
        from tempfile import TemporaryDirectory

        # Caller uses the returned path inside the directory's lifetime.
        self._tmpdir = TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        path = Path(self._tmpdir.name) / "tracker.xlsx"
        tracker.export_to_excel(str(path))
        return _read_sheets(path)

    def _assert_column_is_text(self, rows, header, payload):
        columns = _header_columns(rows)
        self.assertIn(header, columns, sorted(columns))
        column = columns[header]
        body = _formula_body(payload)
        formulas = [
            row[column]["formula"]
            for row in rows[1:]
            if column in row and row[column]["formula"] is not None
        ]
        self.assertNotIn(
            body,
            formulas,
            f"{header} stored {payload!r} as a formula cell: {formulas}",
        )
        texts = [
            row[column]["text"] or ""
            for row in rows[1:]
            if column in row
        ]
        self.assertTrue(
            any(body in text for text in texts),
            f"{header} dropped {payload!r}; cell texts were {texts}",
        )

    def test_imported_document_list_is_text_on_every_sheet(self):
        tracker = self.AsBuiltTracker(
            "Office Building A", handover_date=date(2026, 12, 31)
        )
        tracker.import_document_list(
            pd.DataFrame(
                [
                    {
                        "document_number": DOCUMENT_NUMBER,
                        "title": TITLE,
                        "type": "architectural",
                        "discipline": DISCIPLINE,
                        "contractor": CONTRACTOR,
                        "required_date": date(2020, 1, 1),
                    }
                ]
            )
        )

        sheets = self._export(tracker)

        expectations = (
            ("Documents", "Number", DOCUMENT_NUMBER),
            ("Documents", "Title", TITLE),
            ("Documents", "Discipline", DISCIPLINE),
            ("Documents", "Contractor", CONTRACTOR),
            ("Overdue", "document_number", DOCUMENT_NUMBER),
            ("Overdue", "title", TITLE),
            ("Overdue", "contractor", CONTRACTOR),
            ("By Contractor", "contractor", CONTRACTOR),
        )
        for sheet_name, header, payload in expectations:
            with self.subTest(sheet=sheet_name, column=header):
                self._assert_column_is_text(sheets[sheet_name], header, payload)

    def test_add_document_and_revision_are_text(self):
        tracker = self.AsBuiltTracker(
            "Office Building A", handover_date=date(2026, 12, 31)
        )
        document = tracker.add_document(
            document_number=DOCUMENT_NUMBER,
            title=TITLE,
            doc_type=self.DocumentType.ARCHITECTURAL,
            discipline=DISCIPLINE,
            contractor=CONTRACTOR,
            required_date=date(2020, 1, 1),
        )
        tracker.record_submission(
            document.document_id, revision=REVISION, submitted_by="contractor"
        )

        sheets = self._export(tracker)
        expectations = (
            ("Documents", "Number", DOCUMENT_NUMBER),
            ("Documents", "Title", TITLE),
            ("Documents", "Discipline", DISCIPLINE),
            ("Documents", "Contractor", CONTRACTOR),
            ("Documents", "Revision", REVISION),
            ("Overdue", "document_number", DOCUMENT_NUMBER),
            ("Overdue", "title", TITLE),
            ("Overdue", "contractor", CONTRACTOR),
            ("By Contractor", "contractor", CONTRACTOR),
        )
        for sheet_name, header, payload in expectations:
            with self.subTest(sheet=sheet_name, column=header):
                self._assert_column_is_text(sheets[sheet_name], header, payload)

    def test_plain_titles_and_non_formula_markers_round_trip(self):
        tracker = self.AsBuiltTracker(
            "Office Building A", handover_date=date(2026, 12, 31)
        )
        tracker.add_document(
            document_number="A-001",
            title="Floor Plans Level 1-5",
            doc_type=self.DocumentType.ARCHITECTURAL,
            discipline="Architecture",
            contractor="ABC Architects",
            required_date=date(2020, 1, 1),
        )
        for index, marker in enumerate(PLAIN_MARKERS, start=1):
            tracker.add_document(
                document_number=f"P-{index:03d}",
                title=marker,
                doc_type=self.DocumentType.MECHANICAL,
                discipline="HVAC",
                contractor=f"Plain {index}",
                required_date=date(2020, 1, 2),
            )

        sheets = self._export(tracker)
        documents = sheets["Documents"]
        columns = _header_columns(documents)
        titles = [
            row[columns["Title"]]["text"]
            for row in documents[1:]
        ]
        formulas = [
            row[columns["Title"]]["formula"]
            for row in documents[1:]
        ]
        self.assertIn("Floor Plans Level 1-5", titles)
        for marker in PLAIN_MARKERS:
            # A sanitizer may prefix the marker. The original characters must
            # remain, and the cell must not be a formula.
            self.assertTrue(
                any(marker in title for title in titles),
                f"{marker!r} missing from {titles}",
            )
        self.assertTrue(all(formula is None for formula in formulas), formulas)


if __name__ == "__main__":
    unittest.main()
