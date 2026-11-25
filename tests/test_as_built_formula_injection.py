"""As-built register cells must stay text when field data starts with '='.

The implementation under test is the first Python block in the as-built
documentation skill. These checks fail while export_register writes that
text into formula cells, and they pass once every attacker-supplied string
that reaches the workbook is stored as text that still contains the
original value.

Run from the repository root:

    python -m unittest tests.test_as_built_formula_injection -v

Requires pandas and openpyxl, the libraries the skill imports.
"""

from __future__ import annotations

import re
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "1_DDC_Toolkit" / "Document-Control" / "as-built-documentation" / "SKILL.md"

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS}


def load_skill():
    text = SKILL.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.S)
    if match is None:
        raise AssertionError(f"no Python implementation block in {SKILL}")
    module = types.ModuleType("as_built_documentation_skill")
    module.__file__ = str(SKILL)
    # dataclasses looks the defining module up in sys.modules.
    sys.modules[module.__name__] = module
    exec(compile(match.group(1), str(SKILL), "exec"), module.__dict__)
    return module.AsBuiltDocumentManager, module.DocumentType


def _sheet_xml_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relation_id = None
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        if sheet.attrib.get("name") == sheet_name:
            relation_id = sheet.attrib.get(f"{{{REL_NS}}}id")
            break
    if relation_id is None:
        raise AssertionError(f"workbook has no sheet named {sheet_name!r}")
    target = None
    for rel in relationships.findall(f"{{{PKG_NS}}}Relationship"):
        if rel.attrib.get("Id") == relation_id:
            target = rel.attrib.get("Target")
            break
    if not target:
        raise AssertionError(f"no relationship for sheet {sheet_name!r}")
    if target.startswith("/"):
        return target.lstrip("/")
    if target.startswith("xl/"):
        return target
    return "xl/" + target.lstrip("/")


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall("m:si", NS):
        values.append("".join(node.text or "" for node in item.findall(".//m:t", NS)))
    return values


def cell_record(xlsx_path: Path, sheet_name: str, ref: str) -> tuple[str, str]:
    """Return (kind, text) for one cell in the xlsx package.

    kind is "formula" when the cell has an f element, "text" when the
    package stores characters, or "value" for a numeric or boolean cell.
    """
    with zipfile.ZipFile(xlsx_path) as archive:
        root = ET.fromstring(archive.read(_sheet_xml_path(archive, sheet_name)))
        cell = root.find(f".//m:c[@r='{ref}']", NS)
        if cell is None:
            raise AssertionError(f"{sheet_name}!{ref} is missing from {xlsx_path}")
        formula = cell.find("m:f", NS)
        if formula is not None:
            return "formula", formula.text or ""
        kind = cell.attrib.get("t")
        if kind == "inlineStr":
            text = "".join(node.text or "" for node in cell.findall(".//m:t", NS))
            return "text", text
        value_node = cell.find("m:v", NS)
        value = value_node.text or "" if value_node is not None else ""
        if kind == "s":
            shared = _shared_strings(archive)
            return "text", shared[int(value)]
        if kind in {"str", "inlineStr"}:
            return "text", value
        return "value", value


class AsBuiltFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Manager, cls.DocumentType = load_skill()

    def export(self, **overrides) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        fields = {
            "document_number": "A-101",
            "title": "Floor Plan Level 1",
            "discipline": "Architectural",
            "revision": "0",
            "description": "Wall moved 6 inches south",
            "location": "Grid B-3",
            "marked_by": "Site Superintendent",
            "verified_by": "",
        }
        fields.update(overrides)
        manager = self.Manager("Office Tower")
        document = manager.register_document(
            document_number=fields["document_number"],
            title=fields["title"],
            document_type=self.DocumentType.DRAWING,
            discipline=fields["discipline"],
            original_file="drawings/A-101.pdf",
            revision=fields["revision"],
        )
        manager.add_markup(
            document.document_id,
            description=fields["description"],
            location=fields["location"],
            marked_by=fields["marked_by"],
            cloud_reference=fields.get("cloud_reference", ""),
        )
        if fields["verified_by"]:
            manager.verify_document(document.document_id, fields["verified_by"])
        output = Path(directory.name) / "register.xlsx"
        manager.export_register(str(output))
        return output

    def assert_stored_as_text(self, xlsx_path: Path, sheet: str, ref: str, original: str):
        kind, stored = cell_record(xlsx_path, sheet, ref)
        self.assertNotEqual(
            kind,
            "formula",
            f"{sheet}!{ref} stored {original!r} as formula {stored!r}",
        )
        self.assertIn(
            original,
            stored,
            f"{sheet}!{ref} no longer contains the supplied text {original!r} (stored {stored!r})",
        )

    def test_markup_description_cannot_read_another_drawing(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = self.Manager("Office Tower")
        first = manager.register_document(
            document_number="A-101",
            title="LEVEL-1-FLOOR-PLAN",
            document_type=self.DocumentType.DRAWING,
            discipline="Architectural",
            original_file="drawings/A-101.pdf",
        )
        second = manager.register_document(
            document_number="A-102",
            title="Roof Plan",
            document_type=self.DocumentType.DRAWING,
            discipline="Architectural",
            original_file="drawings/A-102.pdf",
        )
        payload = "=Register!C2"
        manager.add_markup(
            second.document_id,
            description=payload,
            location="Grid C-4",
            marked_by="Field Engineer",
        )
        # The first drawing's title is an ordinary record on the register.
        self.assertEqual(first.document_id, "DOC-0001")
        output = Path(directory.name) / "register.xlsx"
        manager.export_register(str(output))
        self.assert_stored_as_text(output, "Markups", "C2", payload)

    def test_location_formula_is_stored_as_text(self):
        payload = "=1+1"
        workbook = self.export(location=payload)
        self.assert_stored_as_text(workbook, "Markups", "D2", payload)

    def test_marked_by_formula_is_stored_as_text(self):
        payload = "=2+2"
        workbook = self.export(marked_by=payload)
        self.assert_stored_as_text(workbook, "Markups", "E2", payload)

    def test_document_number_formula_is_stored_as_text_on_both_sheets(self):
        payload = "=4+4"
        workbook = self.export(document_number=payload)
        self.assert_stored_as_text(workbook, "Register", "B2", payload)
        self.assert_stored_as_text(workbook, "Markups", "A2", payload)

    def test_document_title_formula_is_stored_as_text(self):
        payload = "=3+3"
        workbook = self.export(title=payload)
        self.assert_stored_as_text(workbook, "Register", "C2", payload)

    def test_discipline_formula_is_stored_as_text(self):
        payload = "=5+5"
        workbook = self.export(discipline=payload)
        self.assert_stored_as_text(workbook, "Register", "E2", payload)

    def test_revision_formula_is_stored_as_text(self):
        payload = "=6+6"
        workbook = self.export(revision=payload)
        self.assert_stored_as_text(workbook, "Register", "F2", payload)

    def test_verified_by_formula_is_stored_as_text(self):
        payload = "=7+7"
        workbook = self.export(verified_by=payload)
        self.assert_stored_as_text(workbook, "Register", "J2", payload)

    def test_plain_markup_text_is_preserved(self):
        workbook = self.export()
        kind, stored = cell_record(workbook, "Markups", "C2")
        self.assertEqual(kind, "text")
        self.assertEqual(stored, "Wall moved 6 inches south")
        kind, stored = cell_record(workbook, "Register", "C2")
        self.assertEqual(kind, "text")
        self.assertEqual(stored, "Floor Plan Level 1")
