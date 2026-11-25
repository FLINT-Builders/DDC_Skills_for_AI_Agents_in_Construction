"""Punch list Excel export must not store free-text fields as formulas.

These tests fail while the vulnerability is present. They load the punch list
implementations embedded in the skill documents and run add/assign/complete
then export_to_excel. A value that starts with "=" is written by pandas and
openpyxl as an OOXML formula cell (`<f>`). Opening that workbook calculates
the formula, including references to other rows' assignees and back-charge
amounts.

Run from the repository root:

    python3 -m unittest discover -s tests -p 'test_punch_list_formula_injection.py' -v

Requires pandas and openpyxl (the skill install: pip install pandas openpyxl).
xlsxwriter must not be installed, because pandas would then write xlsx with
that engine instead of openpyxl.
"""

from __future__ import annotations

import re
import sys
import types
import unittest
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
FIELD_SKILL = (
    REPO
    / "1_DDC_Toolkit"
    / "Field-Operations"
    / "punch-list-manager"
    / "SKILL.md"
)
CLOSEOUT_SKILL = (
    REPO
    / "1_DDC_Toolkit"
    / "Project-Closeout"
    / "punch-list-manager"
    / "SKILL.md"
)

_SHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _load_skill_classes(skill_path: Path) -> dict:
    """Execute the skill's implementation fences, not its usage examples.

    dataclasses resolve annotations against the class module, so the fences
    run inside a real module entry rather than a bare dict.
    """
    text = skill_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    module_name = "skill_" + "_".join(skill_path.relative_to(REPO).parts[:-1])
    module = types.ModuleType(module_name)
    sys.modules[module_name] = module
    loaded = False
    for block in blocks:
        if "class PunchListManager" not in block and "class PunchItem" not in block:
            continue
        exec(compile(block, f"<{module_name}>", "exec"), module.__dict__)
        loaded = True
    if not loaded or not hasattr(module, "PunchListManager"):
        raise AssertionError(f"No PunchListManager implementation in {skill_path}")
    return module.__dict__


def formula_cells(workbook_path: Path) -> list[str]:
    """Return worksheet cells stored as OOXML formulas.

    Reads the xlsx package XML directly. A cell a spreadsheet will calculate
    contains an `<f>` child, which openpyxl writes when a string value starts
    with "=". Self-closing cells are skipped so an empty neighbor is not
    reported as the formula cell.
    """
    found: list[str] = []
    with zipfile.ZipFile(workbook_path) as package:
        sheet_names = sorted(
            name
            for name in package.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        for name in sheet_names:
            root = ET.fromstring(package.read(name))
            for cell in root.findall(".//m:c", _SHEET_NS):
                formula = cell.find("m:f", _SHEET_NS)
                if formula is None:
                    continue
                coordinate = cell.attrib.get("r", "?")
                found.append(f"{name}!{coordinate} formula={formula.text}")
    return found


class FieldOperationsPunchListFormulaTests(unittest.TestCase):
    """Field-Operations punch-list-manager export_to_excel."""

    @classmethod
    def setUpClass(cls):
        cls.ns = _load_skill_classes(FIELD_SKILL)

    def test_exported_free_text_starting_with_equals_is_not_a_formula(self):
        """Description, location, place, assignee, and photo paths are attacker-controlled.

        drawing_markup, spec_reference, completion_notes, verification notes,
        created_by, and back_charge_ref are not columns of this workbook, so
        they are not asserted here.
        """
        manager_cls = self.ns["PunchListManager"]
        trade = self.ns["TradeCategory"]
        priority = self.ns["PunchItemPriority"]
        manager = manager_cls(project_id="PROJECT-2026-001")
        punch_list = manager.create_punch_list(
            name="Floor 5 walk",
            walk_date=date.today(),
            attendees=["Inspector", "Trade partner"],
            area="Building A",
            created_by="PM",
        )
        recorded = manager.add_item(
            punch_list_id=punch_list.list_id,
            description="Paint chip at door frame",
            location="Room 101",
            trade=trade.DRYWALL,
            priority=priority.LOW,
            building="A",
            floor="1",
            room="101",
            created_by="Inspector",
        )
        with redirect_stdout(StringIO()):
            manager.assign_item(recorded.item_id, assigned_to="ABC Painting", assigned_by="GC")
        manager.add_back_charge(recorded.item_id, 12500.50, reference="BC-1")

        planted = manager.add_item(
            punch_list_id=punch_list.list_id,
            description="=O2",
            location="=C2",
            trade=trade.ELECTRICAL,
            priority=priority.MEDIUM,
            building="=D2",
            floor="=E2",
            room="=F2",
            photo_before="=P2",
            created_by="Inspector",
        )
        with redirect_stdout(StringIO()):
            manager.assign_item(planted.item_id, assigned_to="=J2", assigned_by="GC")
        manager.mark_complete(
            planted.item_id,
            completed_by="=L2",
            photo_after="=Q2",
            completion_notes="notes are not an export column",
        )

        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "punch-list.xlsx"
            manager.export_to_excel(str(output))
            planted_formulas = formula_cells(output)

        self.assertEqual(
            planted_formulas,
            [],
            "export_to_excel wrote punch fields as worksheet formulas: "
            + "; ".join(planted_formulas),
        )


class CloseoutPunchListFormulaTests(unittest.TestCase):
    """Project-Closeout punch-list-manager shares the same unsanitized to_excel path.

    add_photo stores a path the workbook never writes, and verified_by is not
    an export column. add_note prefixes a newline and a date, so a note body
    alone does not start the cell with "=" unless notes already did.
    """

    @classmethod
    def setUpClass(cls):
        cls.ns = _load_skill_classes(CLOSEOUT_SKILL)

    def test_exported_free_text_starting_with_equals_is_not_a_formula(self):
        manager_cls = self.ns["PunchListManager"]
        category = self.ns["PunchItemCategory"]
        priority = self.ns["PunchItemPriority"]
        manager = manager_cls(
            project_name="Office Building A",
            target_closeout_date=date.today() + timedelta(days=30),
        )
        manager.add_item(
            location="=1+1",
            description="=2+2",
            category=category.ARCHITECTURAL,
            priority=priority.MEDIUM,
            assigned_to="ABC Ceilings",
            due_date=date.today() - timedelta(days=2),
            notes="=3+3",
        )
        reassigned = manager.add_item(
            location="Level 1 lobby",
            description="Door closer missing",
            category=category.GENERAL,
            priority=priority.HIGH,
            assigned_to="Original Trade",
            due_date=date.today() - timedelta(days=1),
            notes="ordinary note",
        )
        manager.reassign_item(reassigned.item_id, new_assignee="=4+4")

        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "closeout-punch-list.xlsx"
            manager.export_to_excel(str(output))
            planted_formulas = formula_cells(output)

        self.assertEqual(
            planted_formulas,
            [],
            "export_to_excel wrote punch fields as worksheet formulas: "
            + "; ".join(planted_formulas),
        )


if __name__ == "__main__":
    unittest.main()
