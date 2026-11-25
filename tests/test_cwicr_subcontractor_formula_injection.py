"""Formula injection in the CWICR subcontractor bid-comparison workbook.

The implementation under test is the Python block in
``1_DDC_Toolkit/CWICR-Database/cwicr-subcontractor/SKILL.md``. These tests
call ``compare_bids`` and ``export_bid_comparison`` on that code.

They fail while a bidder-supplied company name that starts with ``=`` is
stored as a spreadsheet formula. They pass once every such name is
neutralized before it is written, including a name with more than one
leading ``=``.

``trade``, ``notes``, and scope-item text are not written by
``export_bid_comparison``, and ``generate_negotiation_points`` does not
export a workbook, so those paths are not covered here.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import openpyxl

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "CWICR-Database"
    / "cwicr-subcontractor"
    / "SKILL.md"
)

# Markers that spreadsheet formula injection uses. openpyxl 3.1.5 turns a
# string of length > 1 that starts with "=" into a formula cell; the other
# markers stay inline strings and LibreOffice Calc 24.2 does not evaluate
# them. Stripping markers still has to remove every leading "=".
FORMULA_MARKERS = ("=", "+", "-", "@", "\t", "\r")

# Row 2 of Evaluations is the best-value bidder. Row 3 column B is the
# competitor's bid amount, so this name reads another bidder's price.
BEST_VALUE_NAME = "=Evaluations!B3"
# Two leading markers: dropping only the first one still leaves a formula.
LOWEST_NAME = "==1+1"
# Neither best nor lowest, so it is written only on the Evaluations sheet.
OTHER_NAME = "=SUM(Evaluations!B2:B4)"
COMPETITOR_NAME = "Northwind Mechanical"
COMPETITOR_AMOUNT = 654321.0
LOWEST_AMOUNT = 11.0
OTHER_AMOUNT = 900000.0

SPREADSHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def load_skill_namespace():
    """Execute the skill's implementation block and return its globals."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    fence = "```python"
    start = 0
    implementation = None
    while True:
        opening = text.find(fence, start)
        if opening < 0:
            break
        body_start = opening + len(fence)
        closing = text.find("```", body_start)
        if closing < 0:
            break
        body = text[body_start:closing]
        if "class CWICRSubcontractor" in body and "def export_bid_comparison" in body:
            implementation = body
            break
        start = closing + 3
    if implementation is None:
        raise AssertionError(f"No CWICRSubcontractor implementation in {SKILL_PATH}")
    namespace = {}
    exec(compile(implementation, str(SKILL_PATH), "exec"), namespace)
    return namespace


def visible_bidder_name(payload: str) -> str:
    """Bidder name after every leading formula marker has been removed."""
    rest = payload
    while rest[:1] in FORMULA_MARKERS:
        rest = rest[1:]
    if rest == "":
        raise AssertionError(f"payload {payload!r} has no name text after markers")
    return rest


def assert_name_neutralized(test, cell, payload, where):
    """The cell keeps the bidder name and is not a formula."""
    test.assertNotEqual(
        cell.data_type,
        "f",
        f"{where} stored {payload!r} as a formula ({cell.value!r})",
    )
    test.assertIsInstance(
        cell.value,
        str,
        f"{where} should keep the bidder name as text, got {cell.value!r}",
    )
    test.assertFalse(
        cell.value.startswith("="),
        f"{where} still starts with a formula marker: {cell.value!r}",
    )
    test.assertIn(
        visible_bidder_name(payload),
        cell.value,
        f"{where} dropped the bidder name {payload!r} (cell is {cell.value!r})",
    )


def column_by_header(worksheet, header):
    for cell in worksheet[1]:
        if cell.value == header:
            return cell.column
    raise AssertionError(f"{worksheet.title} has no {header!r} column")


class BidComparisonFormulaInjectionTest(unittest.TestCase):
    """Award workbook produced from unsanitized subcontractor names."""

    @classmethod
    def setUpClass(cls):
        namespace = load_skill_namespace()
        pandas = namespace["pd"]
        cls.analyzer_cls = namespace["CWICRSubcontractor"]
        cls.bid_cls = namespace["SubcontractorBid"]
        cwicr = pandas.DataFrame(
            [
                {
                    "work_item_code": "ELEC-001",
                    "labor_cost": 100.0,
                    "material_cost": 0.0,
                    "equipment_cost": 0.0,
                }
            ]
        )
        cls.analyzer = cls.analyzer_cls(cwicr)
        scope = [{"work_item_code": "ELEC-001", "quantity": 1}]
        cls.benchmark = cls.analyzer.calculate_benchmark(scope)["total"]
        cls.bids = [
            cls.bid_cls(
                BEST_VALUE_NAME,
                "Electrical",
                cls.benchmark,
                scope,
                True,
                True,
                True,
                30,
                notes="=9+9",
            ),
            cls.bid_cls(
                COMPETITOR_NAME,
                "Plumbing",
                COMPETITOR_AMOUNT,
                scope,
                True,
                True,
                True,
                30,
                notes="=8+8",
            ),
            cls.bid_cls(
                LOWEST_NAME,
                "Mechanical",
                LOWEST_AMOUNT,
                scope,
                True,
                True,
                True,
                30,
            ),
            cls.bid_cls(
                OTHER_NAME,
                "Concrete",
                OTHER_AMOUNT,
                scope,
                True,
                True,
                True,
                30,
            ),
        ]
        cls.comparison = cls.analyzer.compare_bids(cls.bids)
        cls.tmpdir = tempfile.TemporaryDirectory(prefix="bid-comparison-")
        cls.workbook_path = str(Path(cls.tmpdir.name) / "bid_comparison.xlsx")
        cls.analyzer.export_bid_comparison(cls.comparison, cls.workbook_path)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def test_summary_and_evaluations_do_not_store_names_as_formulas(self):
        workbook = openpyxl.load_workbook(self.workbook_path)
        summary = workbook["Summary"]
        evaluations = workbook["Evaluations"]
        placements = [
            (
                summary.cell(2, column_by_header(summary, "Best Value Bidder")),
                BEST_VALUE_NAME,
                "Summary best-value bidder",
            ),
            (
                summary.cell(2, column_by_header(summary, "Lowest Bidder")),
                LOWEST_NAME,
                "Summary lowest bidder",
            ),
            (
                evaluations.cell(2, column_by_header(evaluations, "Subcontractor")),
                BEST_VALUE_NAME,
                "Evaluations row for the best-value bidder",
            ),
            (
                evaluations.cell(4, column_by_header(evaluations, "Subcontractor")),
                LOWEST_NAME,
                "Evaluations row for the lowest bidder",
            ),
            (
                evaluations.cell(5, column_by_header(evaluations, "Subcontractor")),
                OTHER_NAME,
                "Evaluations row for a bidder who is neither best nor lowest",
            ),
        ]
        for cell, payload, where in placements:
            with self.subTest(where=where):
                assert_name_neutralized(self, cell, payload, where)
        workbook.close()

    def test_workbook_package_has_no_formula_elements(self):
        formulas = []
        with zipfile.ZipFile(self.workbook_path) as package:
            for name in package.namelist():
                if not (name.startswith("xl/worksheets/") and name.endswith(".xml")):
                    continue
                root = ElementTree.fromstring(package.read(name))
                for formula in root.findall(".//m:f", SPREADSHEET_NS):
                    formulas.append(f"{name}:{formula.text}")
        self.assertEqual(
            formulas,
            [],
            "export wrote formula cells from bidder names: " + ", ".join(formulas),
        )

    def test_opening_the_workbook_does_not_evaluate_bidder_names(self):
        soffice = shutil.which("soffice")
        if soffice is None:
            self.fail("LibreOffice soffice is required to recalculate the workbook")

        with tempfile.TemporaryDirectory(prefix="lo-profile-") as profile:
            with tempfile.TemporaryDirectory(prefix="lo-out-") as outdir:
                completed = subprocess.run(
                    [
                        soffice,
                        "--headless",
                        "--norestore",
                        "--nolockcheck",
                        "--nologo",
                        f"-env:UserInstallation=file://{profile}",
                        "--convert-to",
                        "xlsx",
                        "--outdir",
                        outdir,
                        self.workbook_path,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    "LibreOffice failed to open the workbook:\n"
                    + completed.stdout
                    + completed.stderr,
                )
                converted = Path(outdir) / Path(self.workbook_path).name
                calculated = openpyxl.load_workbook(converted, data_only=True)

        summary = calculated["Summary"]
        evaluations = calculated["Evaluations"]
        best_value = summary.cell(
            2, column_by_header(summary, "Best Value Bidder")
        ).value
        lowest = summary.cell(2, column_by_header(summary, "Lowest Bidder")).value
        best_on_sheet = evaluations.cell(
            2, column_by_header(evaluations, "Subcontractor")
        ).value
        lowest_on_sheet = evaluations.cell(
            4, column_by_header(evaluations, "Subcontractor")
        ).value
        other_on_sheet = evaluations.cell(
            5, column_by_header(evaluations, "Subcontractor")
        ).value
        calculated.close()

        leaked = [
            ("Summary best-value bidder", best_value, BEST_VALUE_NAME, COMPETITOR_AMOUNT),
            ("Evaluations best-value bidder", best_on_sheet, BEST_VALUE_NAME, COMPETITOR_AMOUNT),
            ("Summary lowest bidder", lowest, LOWEST_NAME, 2),
            ("Evaluations lowest bidder", lowest_on_sheet, LOWEST_NAME, 2),
        ]
        for where, value, payload, computed in leaked:
            with self.subTest(where=where):
                self.assertNotEqual(
                    value,
                    computed,
                    f"{where} evaluated {payload!r} to {value!r} when the workbook was opened",
                )
                self.assertIsInstance(value, str, f"{where} calculated to {value!r}")
                self.assertFalse(str(value).startswith("="), f"{where} is {value!r}")
                self.assertIn(visible_bidder_name(payload), value)

        with self.subTest(where="Evaluations other bidder"):
            self.assertNotIsInstance(
                other_on_sheet,
                (int, float),
                f"opening the workbook evaluated {OTHER_NAME!r} to {other_on_sheet!r}",
            )
            self.assertIsInstance(other_on_sheet, str)
            self.assertFalse(str(other_on_sheet).startswith("="))
            self.assertIn(visible_bidder_name(OTHER_NAME), other_on_sheet)


if __name__ == "__main__":
    unittest.main()
