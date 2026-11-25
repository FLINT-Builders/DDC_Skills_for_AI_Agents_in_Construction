"""Formula injection in the CWICR bid analyzer Excel export.

These tests fail while bidder-controlled text that starts with "=" is written
into bid_analysis.xlsx / bid_comparison.xlsx as a spreadsheet formula. They
pass once those values are stored as text (a leading apostrophe, or an
explicit text cell) in every export that writes them.

Run from the repository root, after installing the libraries the skill imports:

    python3 -m pip install 'pandas==2.2.3' 'openpyxl==3.1.5' 'numpy==2.1.3'
    python3 -m unittest tests.test_cwicr_bid_analyzer_formula_injection
"""

from __future__ import annotations

import re
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import Cell
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "CWICR-Database"
    / "cwicr-bid-analyzer"
    / "SKILL.md"
)

# Distinct payloads so a single sanitized column cannot hide another field.
DESCRIPTION_FORMULA = "=1+1"
ITEM_CODE_FORMULA = '=HYPERLINK("http://example.invalid","code")'
UNIT_FORMULA = "=3+3"
BIDDER_FORMULA = "=2+2"
BENIGN_DESCRIPTION = "Concrete placement"


def load_skill_namespace() -> dict:
    """Execute the implementation block shipped in the skill."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"no python implementation block in {SKILL_PATH}")
    module = types.ModuleType("cwicr_bid_analyzer_skill")
    module.__file__ = str(SKILL_PATH)
    sys.modules[module.__name__] = module
    exec(compile(match.group(1), str(SKILL_PATH), "exec"), module.__dict__)
    return module.__dict__


def write_contractor_bid(path: Path) -> None:
    """Bid workbook whose formula-looking cells are text, as Excel stores them
    when the bidder formats the column as text before typing.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        ["item_code", "description", "quantity", "unit", "unit_rate", "total_price"]
    )
    sheet.append(["WI-1", DESCRIPTION_FORMULA, 10, "m2", 100, 1000])
    sheet.append([ITEM_CODE_FORMULA, BENIGN_DESCRIPTION, 5, "ea", 50, 250])
    sheet.append(["WI-3", "Unit row", 2, UNIT_FORMULA, 80, 160])
    for row in sheet.iter_rows(min_row=2, max_col=6):
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    workbook.save(path)


def benchmark_table() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "work_item_code": "WI-1",
                "labor_cost": 10,
                "material_cost": 10,
                "equipment_cost": 0,
            },
            {
                "work_item_code": "WI-3",
                "labor_cost": 10,
                "material_cost": 0,
                "equipment_cost": 0,
            },
        ]
    )


def column_cells(sheet, header: str) -> list[Cell]:
    header_cell = next(
        (cell for cell in sheet[1] if cell.value == header),
        None,
    )
    if header_cell is None:
        raise AssertionError(f"column {header!r} missing on sheet {sheet.title!r}")
    return [
        sheet.cell(row, header_cell.column)
        for row in range(2, sheet.max_row + 1)
        if sheet.cell(row, header_cell.column).value is not None
    ]


def assert_text_not_formula(cells: list[Cell], payload: str, where: str) -> None:
    """A fix may keep the original text as a text cell or prefix one apostrophe."""
    matches = []
    for cell in cells:
        text = str(cell.value)
        preserved = text == payload or text.lstrip("'") == payload
        if not preserved:
            continue
        matches.append(cell)
        if cell.data_type == "f":
            raise AssertionError(
                f"{where} cell {cell.coordinate} stores {payload!r} as a formula"
            )
    if not matches:
        raise AssertionError(f"{where} does not contain {payload!r}")


def assert_workbook_has_no_formula(path: Path, payload: str) -> None:
    """The .xlsx the estimator opens must not contain an OOXML formula element."""
    if not payload.startswith("=") or len(payload) < 2:
        raise AssertionError("test payload must be a leading-equals formula")
    formula_body = payload[1:]
    pattern = re.compile(rf"<f[^>]*>{re.escape(formula_body)}</f>")
    with zipfile.ZipFile(path) as workbook_zip:
        sheet_names = [
            name
            for name in workbook_zip.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        ]
        for name in sheet_names:
            xml = workbook_zip.read(name).decode("utf-8")
            if pattern.search(xml):
                raise AssertionError(
                    f"{path.name} sheet {name} contains formula <f>{formula_body}</f>"
                )


class BidAnalyzerFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = load_skill_namespace()
        cls.analyzer_type = cls.skill["CWICRBidAnalyzer"]

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bid_path = self.directory / "contractor_bid.xlsx"
        write_contractor_bid(self.bid_path)
        self.bid = pd.read_excel(self.bid_path)
        self.analyzer = self.analyzer_type(benchmark_table())

    def test_export_analysis_does_not_emit_formulas(self) -> None:
        # pd.read_excel, as in the skill quick start, returns the bidder's text.
        self.assertIn(DESCRIPTION_FORMULA, set(self.bid["description"].astype(str)))
        self.assertIn(ITEM_CODE_FORMULA, set(self.bid["item_code"].astype(str)))
        self.assertIn(UNIT_FORMULA, set(self.bid["unit"].astype(str)))

        analysis = self.analyzer.analyze_bid(self.bid, BIDDER_FORMULA)
        output = self.directory / "bid_analysis.xlsx"
        self.analyzer.export_analysis(analysis, str(output))

        workbook = load_workbook(output)
        line_items = workbook["Line Items"]
        flagged = workbook["Flagged Items"]
        summary = workbook["Summary"]

        assert_text_not_formula(
            column_cells(line_items, "Description"),
            DESCRIPTION_FORMULA,
            "Line Items Description",
        )
        assert_text_not_formula(
            column_cells(line_items, "Item Code"),
            ITEM_CODE_FORMULA,
            "Line Items Item Code",
        )
        assert_text_not_formula(
            column_cells(line_items, "Unit"),
            UNIT_FORMULA,
            "Line Items Unit",
        )
        assert_text_not_formula(
            column_cells(flagged, "Description"),
            DESCRIPTION_FORMULA,
            "Flagged Items Description",
        )
        assert_text_not_formula(
            column_cells(summary, "Bidder"),
            BIDDER_FORMULA,
            "Summary Bidder",
        )
        benign = column_cells(line_items, "Description")
        self.assertTrue(
            any(cell.value == BENIGN_DESCRIPTION and cell.data_type != "f" for cell in benign),
            "benign description was rewritten",
        )

        for payload in (
            DESCRIPTION_FORMULA,
            ITEM_CODE_FORMULA,
            UNIT_FORMULA,
            BIDDER_FORMULA,
        ):
            assert_workbook_has_no_formula(output, payload)

    def test_export_comparison_does_not_emit_bidder_formula(self) -> None:
        comparison = self.analyzer.compare_bids(
            [
                (BIDDER_FORMULA, self.bid),
                ("Safe Contractor", self.bid),
            ],
            "Building Project",
        )
        output = self.directory / "bid_comparison.xlsx"
        self.analyzer.export_comparison(comparison, str(output))

        workbook = load_workbook(output)
        assert_text_not_formula(
            column_cells(workbook["Overview"], "Bidder"),
            BIDDER_FORMULA,
            "Overview Bidder",
        )
        assert_text_not_formula(
            column_cells(workbook["Ranking"], "Bidder"),
            BIDDER_FORMULA,
            "Ranking Bidder",
        )
        safe_names = column_cells(workbook["Overview"], "Bidder")
        self.assertTrue(
            any(cell.value == "Safe Contractor" and cell.data_type != "f" for cell in safe_names)
        )
        assert_workbook_has_no_formula(output, BIDDER_FORMULA)


if __name__ == "__main__":
    unittest.main()
