"""Failing spec for formula injection in the bid-analysis export.

The implementation under test is the Python block in SKILL.md. These tests
execute that block: add_bid, assignment of the public exclusion and
qualification lists, and export_analysis. They fail while bidder-controlled
text that starts with "=" is written into the workbook as a formula.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
import zipfile
from pathlib import Path

import openpyxl

SKILL_PATH = Path(__file__).resolve().parent / "SKILL.md"
OTHER_BIDDER = "Honest Co"
OTHER_PRICE = 222000.0
CRITERIA = (
    "Price",
    "Experience",
    "Schedule",
    "Safety Record",
    "References",
    "Capacity",
)
_FORMULA_ELEMENT = re.compile(r"<f\b[^>]*>(.*?)</f>|<f\b[^>]*/>", re.DOTALL)


def load_bid_analysis_comparator():
    """Execute the skill implementation from SKILL.md and return the class."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.find(marker)
    if start < 0:
        raise AssertionError(f"no Python implementation block in {SKILL_PATH}")
    start += len(marker)
    end = text.find("\n```", start)
    if end < 0:
        raise AssertionError(f"unterminated Python block in {SKILL_PATH}")
    code = text[start:end]
    if "class BidAnalysisComparator" not in code or "def export_analysis" not in code:
        raise AssertionError("implementation block is not the bid comparator")
    # dataclasses resolves the class module through sys.modules. Register the
    # skill block as a module so that lookup succeeds, then execute it unchanged.
    module = types.ModuleType("bid_analysis_comparator_skill")
    module.__file__ = str(SKILL_PATH)
    sys.modules[module.__name__] = module
    exec(compile(code, str(SKILL_PATH), "exec"), module.__dict__)
    return module.BidAnalysisComparator


def workbook_formulas(path: str) -> list[str]:
    """Return formula bodies stored in the xlsx the reviewer would open."""
    found: list[str] = []
    with zipfile.ZipFile(path) as workbook:
        for name in workbook.namelist():
            if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                continue
            xml = workbook.read(name).decode("utf-8")
            for match in _FORMULA_ELEMENT.finditer(xml):
                body = match.group(1) if match.group(1) is not None else ""
                found.append(f"{name}:{body}")
    loaded = openpyxl.load_workbook(path)
    try:
        for sheet in loaded.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.data_type == "f":
                        found.append(f"{sheet.title}!{cell.coordinate}={cell.value}")
    finally:
        loaded.close()
    return found


def workbook_xml(path: str) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as workbook:
        for name in workbook.namelist():
            if name.endswith(".xml"):
                parts.append(workbook.read(name).decode("utf-8"))
    return "\n".join(parts)


def assert_export_has_no_formulas(test: unittest.TestCase, comparator) -> None:
    """The written workbook must not contain formula cells.

    A refusal is acceptable only when it does not leave those cells behind.
    A successful export must still contain the other bidder's name.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bid-analysis.xlsx")
        try:
            comparator.export_analysis(path)
        except Exception as exc:
            if os.path.exists(path):
                formulas = workbook_formulas(path)
                test.assertFalse(
                    formulas,
                    f"export raised {exc!r} after writing formula cells: {formulas}",
                )
            return
        test.assertTrue(os.path.exists(path))
        formulas = workbook_formulas(path)
        test.assertEqual(
            formulas,
            [],
            "bidder-controlled text was written as a spreadsheet formula",
        )
        test.assertIn(OTHER_BIDDER, workbook_xml(path))


def _score(comparator, bid, value: int) -> None:
    comparator.score_bid(bid.bid_id, {name: value for name in CRITERIA})


class BidExportFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.comparator_cls = load_bid_analysis_comparator()

    def _comparator(self):
        return self.comparator_cls("Office Tower", "Electrical")

    def test_bidder_name_is_not_a_formula_on_either_sheet(self):
        comparator = self._comparator()
        planted = comparator.add_bid("=B3", 111.0)
        planted.exclusions = ["night work excluded"]
        planted.qualifications = ["bond rate subject to award"]
        other = comparator.add_bid(OTHER_BIDDER, OTHER_PRICE)
        _score(comparator, planted, 10)
        _score(comparator, other, 1)
        assert_export_has_no_formulas(self, comparator)

    def test_joined_exclusions_are_not_a_formula(self):
        comparator = self._comparator()
        planted = comparator.add_bid("North Electric", 111.0)
        planted.exclusions = ["=1+1", "night work excluded"]
        planted.qualifications = ["bond rate subject to award"]
        comparator.add_bid(OTHER_BIDDER, OTHER_PRICE)
        assert_export_has_no_formulas(self, comparator)

    def test_qualifications_are_not_a_formula(self):
        comparator = self._comparator()
        planted = comparator.add_bid("North Electric", 111.0)
        planted.exclusions = ["night work excluded"]
        planted.qualifications = ["=2+2"]
        comparator.add_bid(OTHER_BIDDER, OTHER_PRICE)
        assert_export_has_no_formulas(self, comparator)

    def test_ordinary_bid_text_is_still_exported(self):
        comparator = self._comparator()
        bid = comparator.add_bid("North Electric", 111.0)
        bid.exclusions = ["night work excluded"]
        bid.qualifications = ["bond rate subject to award"]
        comparator.add_bid(OTHER_BIDDER, OTHER_PRICE)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bid-analysis.xlsx")
            comparator.export_analysis(path)
            xml = workbook_xml(path)
            self.assertEqual(workbook_formulas(path), [])
            self.assertIn("North Electric", xml)
            self.assertIn(OTHER_BIDDER, xml)
            self.assertIn("night work excluded", xml)
            self.assertIn("bond rate subject to award", xml)

    def test_compatible_client_does_not_evaluate_bid_fields(self):
        if shutil.which("soffice") is None:
            self.skipTest("LibreOffice soffice is not installed")
        comparator = self._comparator()
        planted = comparator.add_bid("=B3", 111.0)
        planted.exclusions = ["=1+1"]
        planted.qualifications = ["=2+2"]
        other = comparator.add_bid(OTHER_BIDDER, OTHER_PRICE)
        _score(comparator, planted, 10)
        _score(comparator, other, 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bid-analysis.xlsx")
            try:
                comparator.export_analysis(path)
            except Exception as exc:
                if os.path.exists(path):
                    formulas = workbook_formulas(path)
                    self.assertFalse(
                        formulas,
                        f"export raised {exc!r} after writing formula cells: {formulas}",
                    )
                return
            evaluated = _libreoffice_formula_cells(path)
            self.assertEqual(
                evaluated,
                [],
                "opening the export evaluated bidder-controlled formulas: "
                + ", ".join(evaluated),
            )


def _libreoffice_formula_cells(path: str) -> list[str]:
    """Load path in LibreOffice Calc and list cells Calc treats as formulas."""
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="lo-formula-")
    process = subprocess.Popen(
        [
            "soffice",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nologo",
            f"-env:UserInstallation=file://{profile}",
            f"--accept=socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        context = _uno_context(port)
        desktop = context.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", context
        )
        from com.sun.star.beans import PropertyValue

        hidden = PropertyValue()
        hidden.Name = "Hidden"
        hidden.Value = True
        document = desktop.loadComponentFromURL(
            Path(path).resolve().as_uri(), "_blank", 0, (hidden,)
        )
        try:
            found: list[str] = []
            sheets = document.Sheets
            for index in range(sheets.Count):
                sheet = sheets.getByIndex(index)
                cursor = sheet.createCursor()
                cursor.gotoEndOfUsedArea(False)
                last_row = cursor.RangeAddress.EndRow
                last_col = cursor.RangeAddress.EndColumn
                for row in range(last_row + 1):
                    for col in range(last_col + 1):
                        cell = sheet.getCellByPosition(col, row)
                        formula = cell.Formula or ""
                        if formula.startswith("="):
                            found.append(
                                f"{sheet.Name}!r{row + 1}c{col + 1} "
                                f"formula={formula} value={cell.Value} "
                                f"string={cell.String!r}"
                            )
            return found
        finally:
            document.close(True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _uno_context(port: int):
    import uno
    from com.sun.star.connection import NoConnectException

    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local
    )
    url = f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"
    deadline = time.time() + 20
    last_error = None
    while time.time() < deadline:
        try:
            return resolver.resolve(url)
        except NoConnectException as exc:
            last_error = exc
            time.sleep(0.25)
    raise AssertionError(f"LibreOffice did not accept a connection: {last_error}")


if __name__ == "__main__":
    unittest.main()
