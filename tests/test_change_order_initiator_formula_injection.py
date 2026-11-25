"""Initiator names must not become formulas in the change-order workbook.

These tests fail while the report writes initiator text through to Excel
unchanged. They pass once that text is neutralized before it is written,
including the All_COs and Root_Causes sheets.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import types
import unittest
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import openpyxl

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "5_DDC_Innovative" / "change-order-analysis" / "SKILL.md"
SPREADSHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Local-only markers. The hyperlink target is loopback so the payload is an
# address-bearing formula, not a request to any external host.
CREATE_MARKER = "co-init-marker-create"
DIRECT_MARKER = "co-init-marker-direct"
CREATE_INITIATOR = f'=1+1&"{CREATE_MARKER}"'
DIRECT_INITIATOR = (
    f'=HYPERLINK("http://127.0.0.1/{DIRECT_MARKER}","{DIRECT_MARKER}")'
)
PLAIN_INITIATOR = "contractor"


def _python_blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    in_block = False
    for line in markdown.splitlines():
        if line.startswith("```python"):
            in_block = True
            current = []
            continue
        if in_block and line.startswith("```"):
            in_block = False
            blocks.append("\n".join(current))
            continue
        if in_block:
            current.append(line)
    return blocks


def load_change_order_skill() -> dict:
    """Execute the report path published in SKILL.md.

    The ML block is not part of workbook generation and is skipped.
    """
    blocks = _python_blocks(SKILL_PATH.read_text(encoding="utf-8"))
    module = types.ModuleType("change_order_skill")
    sys.modules["change_order_skill"] = module
    namespace = module.__dict__
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        for index, block in enumerate(blocks):
            if "class ChangeOrderPredictor" in block:
                continue
            if "class ChangeOrder" not in block:
                continue
            exec(compile(block, f"{SKILL_PATH}:block{index}", "exec"), namespace)
    for name in ("ChangeOrderManager", "ChangeOrderAnalyzer", "ChangeOrderDetail"):
        if name not in namespace:
            raise AssertionError(f"{name} was not defined by {SKILL_PATH}")
    return namespace


def _formula_texts(path: Path) -> list[str]:
    texts: list[str] = []
    with zipfile.ZipFile(path) as workbook:
        for name in workbook.namelist():
            if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                continue
            root = ET.fromstring(workbook.read(name))
            for element in root.iter(SPREADSHEET_NS + "f"):
                texts.append("".join(element.itertext()))
    return texts


def _cells_containing(worksheet, marker: str) -> list:
    found = []
    for row in worksheet.iter_rows():
        for cell in row:
            if cell.value is not None and marker in str(cell.value):
                found.append(cell)
    return found


class ChangeOrderInitiatorFormulaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = load_change_order_skill()

    def _manager(self):
        return self.skill["ChangeOrderManager"]("P1", 1_000_000.0)

    def _detail(self, initiated_by: str, co_id: str, co_number: str):
        return self.skill["ChangeOrderDetail"](
            co_id=co_id,
            co_number=co_number,
            title="Additional reinforcement",
            description="Field condition",
            justification="",
            co_type=self.skill["ChangeOrderType"].FIELD_CONDITION,
            initiated_by=initiated_by,
            responsibility="TBD",
            status=self.skill["ChangeOrderStatus"].DRAFT,
            submitted_date=date(2026, 11, 25),
            cost_breakdown=self.skill["CostBreakdown"](labor=2500),
        )

    def _write_report(self, change_orders) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = Path(directory.name) / "change_order_analysis.xlsx"
        analyzer = self.skill["ChangeOrderAnalyzer"](list(change_orders))
        analyzer.generate_report(str(output))
        return output

    def _assert_initiator_neutralized(self, workbook_path: Path, marker: str):
        workbook = openpyxl.load_workbook(workbook_path)
        for sheet_name in ("All_COs", "Root_Causes"):
            self.assertIn(sheet_name, workbook.sheetnames)
            cells = _cells_containing(workbook[sheet_name], marker)
            self.assertTrue(
                cells,
                f"{sheet_name} dropped the initiator text {marker!r}",
            )
            for cell in cells:
                self.assertNotEqual(
                    cell.data_type,
                    "f",
                    f"{sheet_name}!{cell.coordinate} stored {cell.value!r} as a formula",
                )
                self.assertFalse(
                    str(cell.value).startswith("="),
                    f"{sheet_name}!{cell.coordinate} still begins with '=': {cell.value!r}",
                )
        for formula in _formula_texts(workbook_path):
            self.assertNotIn(
                marker,
                formula,
                f"workbook formula still contains initiator text: {formula!r}",
            )

    def _assert_plain_initiator_preserved(self, workbook_path: Path):
        workbook = openpyxl.load_workbook(workbook_path)
        for sheet_name in ("All_COs", "Root_Causes"):
            cells = [
                cell
                for row in workbook[sheet_name].iter_rows()
                for cell in row
                if cell.value == PLAIN_INITIATOR
            ]
            self.assertTrue(
                cells,
                f"{sheet_name} lost the ordinary initiator name {PLAIN_INITIATOR!r}",
            )
            for cell in cells:
                self.assertNotEqual(cell.data_type, "f")

    def test_create_change_order_initiator_is_text_in_report(self):
        manager = self._manager()
        malicious = manager.create_change_order(
            "Reinforcement",
            "Field condition",
            self.skill["ChangeOrderType"].FIELD_CONDITION,
            CREATE_INITIATOR,
        )
        malicious.cost_breakdown = self.skill["CostBreakdown"](labor=2500)
        plain = manager.create_change_order(
            "Ordinary",
            "Field condition",
            self.skill["ChangeOrderType"].FIELD_CONDITION,
            PLAIN_INITIATOR,
        )
        plain.cost_breakdown = self.skill["CostBreakdown"](labor=100)
        report = self._write_report(manager.change_orders.values())
        self._assert_initiator_neutralized(report, CREATE_MARKER)
        self._assert_plain_initiator_preserved(report)

    def test_loaded_change_order_detail_initiator_is_text_in_report(self):
        loaded = self._detail(DIRECT_INITIATOR, "CO-DIRECT", "CO-0099")
        plain = self._detail(PLAIN_INITIATOR, "CO-PLAIN", "CO-0100")
        report = self._write_report([loaded, plain])
        self._assert_initiator_neutralized(report, DIRECT_MARKER)
        self._assert_plain_initiator_preserved(report)


if __name__ == "__main__":
    unittest.main()
