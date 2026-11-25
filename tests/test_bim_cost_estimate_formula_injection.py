"""Formula injection in the BIM cost estimate workbook.

The tests load the Python implementation embedded in
``1_DDC_Toolkit/Cost-Management/bim-cost-estimation-cwicr/SKILL.md`` and run
``estimate_from_qto``, ``estimate_element``, ``search_cwicr``, and
``generate_estimate_report``. They fail while a string that starts with a
formula marker is written into the workbook as an Excel formula. They pass
once every string written to the workbook is stored as text, with a leading
apostrophe when it starts with ``=``, ``+``, ``-``, ``@``, a tab, or a
carriage return.

Other skills in this repository also call ``DataFrame.to_excel``. They are
not this pipeline and are not covered here.
"""

import ast
import json
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import Cell

REPO = Path(__file__).resolve().parents[1]
SKILL = (
    REPO
    / "1_DDC_Toolkit"
    / "Cost-Management"
    / "bim-cost-estimation-cwicr"
    / "SKILL.md"
)
SPREADSHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
# Markers named by the remediation. openpyxl 3.1.5 promotes only a leading
# "=" (length > 1) to a formula cell; the other markers are still required
# to be quoted before they are written.
FORMULA_MARKERS = ("=", "+", "-", "@", "\t", "\r")

# Price-database record returned by the stand-in for Qdrant. Tests replace
# the string fields. The shape is the one search_cwicr reads: payload keys
# code, description, unit, unit_price, labor_cost, material_cost,
# equipment_cost, productivity, currency, plus score on the hit.
PRICE_PAYLOAD = {
    "code": "CW-100",
    "description": "Brick masonry laying",
    "unit": "m³",
    "unit_price": 10.0,
    "labor_cost": 4.0,
    "material_cost": 5.0,
    "equipment_cost": 1.0,
    "productivity": 1.0,
    "currency": "USD",
}


class _Hit:
    def __init__(self, payload, score=0.91):
        self.payload = payload
        self.score = score


class _QdrantClient:
    """Stand-in for the Qdrant client constructed in BIMCostEstimator.__init__.

    search() returns one hit so estimate_element keeps the line item. It does
    not rewrite the BIM type name; search_cwicr copies payload fields into
    WorkItem itself.
    """

    payload = dict(PRICE_PAYLOAD)

    def __init__(self, url, api_key=None):
        self.url = url
        self.api_key = api_key

    def search(self, **kwargs):
        return [_Hit(dict(self.payload))]


class _Embeddings:
    def create(self, **kwargs):
        data = types.SimpleNamespace(embedding=[0.0])
        return types.SimpleNamespace(data=[data])


class _Completions:
    def create(self, **kwargs):
        message = types.SimpleNamespace(
            content=json.dumps({"work_items": ["Brick masonry laying"]})
        )
        choice = types.SimpleNamespace(message=message)
        return types.SimpleNamespace(choices=[choice])


class _OpenAI:
    """Stand-in for openai.OpenAI. decompose_bim_type reads choices[0].message.content."""

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.embeddings = _Embeddings()
        self.chat = types.SimpleNamespace(completions=_Completions())


def _install_import_standins():
    qdrant_client = types.ModuleType("qdrant_client")
    qdrant_client.QdrantClient = _QdrantClient
    models = types.ModuleType("qdrant_client.models")

    class _Filter:
        def __init__(self, must=None):
            self.must = must

    class _FieldCondition:
        def __init__(self, key=None, match=None):
            self.key = key
            self.match = match

    class _MatchValue:
        def __init__(self, value=None):
            self.value = value

    models.Filter = _Filter
    models.FieldCondition = _FieldCondition
    models.MatchValue = _MatchValue
    qdrant_client.models = models
    sys.modules["qdrant_client"] = qdrant_client
    sys.modules["qdrant_client.models"] = models

    openai_mod = types.ModuleType("openai")
    openai_mod.OpenAI = _OpenAI
    sys.modules["openai"] = openai_mod


def load_skill_module():
    """Execute the skill's Python block and return its namespace."""
    _install_import_standins()
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("```python\n") + len("```python\n")
    end = text.index("\n```", start)
    source = text[start:end]
    # The fence is the implementation under test. Reject a parse error loudly.
    ast.parse(source)
    namespace = {"__name__": "bim_cost_estimation_cwicr_under_test"}
    exec(compile(source, str(SKILL), "exec"), namespace)
    return namespace


def _write_qto_workbook(path, rows):
    """Write a QTO sheet whose Type Name cells are text, including leading '='.

    openpyxl 3.1.5 stores a string that starts with '=' as a formula when the
    value is assigned. A type name exported as text has to be put back to a
    string cell, which is what pandas.read_excel then returns. Formula cells
    would be dropped: pandas 3.1.1 reads this engine with data_only=True.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Category", "Type Name", "Volume"])
    for index, (category, type_name, volume) in enumerate(rows, start=2):
        sheet.cell(row=index, column=1, value=category)
        name_cell = sheet.cell(row=index, column=2, value=type_name)
        if isinstance(name_cell.value, str):
            name_cell.data_type = "s"
        sheet.cell(row=index, column=3, value=volume)
    workbook.save(path)


def _formula_texts(path):
    """Return OOXML formula bodies written into the workbook."""
    found = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not (name.startswith("xl/worksheets/sheet") and name.endswith(".xml")):
                continue
            root = ET.fromstring(archive.read(name))
            for formula in root.findall(".//m:f", SPREADSHEET_NS):
                found.append((name, formula.text))
    return found


def _expected_text(original):
    """Cell text after the required neutralization, as openpyxl reloads it.

    XML parsers turn CR into LF, so a stored carriage return comes back as a
    newline. The apostrophe is part of the stored string.
    """
    normalized = original.replace("\r", "\n")
    if original[:1] in FORMULA_MARKERS:
        return "'" + normalized
    return normalized


def _header_map(sheet):
    headers = {}
    for cell in next(sheet.iter_rows(min_row=1, max_row=1)):
        if cell.value is not None:
            headers[str(cell.value)] = cell.column
    return headers


def _find_cell(sheet, column_name, original):
    column = _header_map(sheet)[column_name]
    accepted = {
        original,
        original.replace("\r", "\n"),
        _expected_text(original),
    }
    for row in sheet.iter_rows(min_row=2):
        cell = row[column - 1]
        if cell.value in accepted:
            return cell
    raise AssertionError(
        f"No {column_name!r} cell matching {original!r} on {sheet.title}"
    )


def _assert_neutralized(test, cell, original):
    test.assertIsInstance(cell, Cell)
    test.assertNotEqual(
        cell.data_type,
        "f",
        f"{cell.coordinate} on {cell.parent.title} was stored as a formula: {cell.value!r}",
    )
    test.assertEqual(cell.value, _expected_text(original))


class FormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = load_skill_module()
        cls.estimator = cls.skill["BIMCostEstimator"](
            qdrant_url="https://qdrant.invalid",
            qdrant_api_key="qdrant-key-placeholder",
            openai_api_key="openai-key-placeholder",
            language="EN",
        )

    def setUp(self):
        _QdrantClient.payload = dict(PRICE_PAYLOAD)

    def _estimate_qto(self, frame):
        return self.estimator.estimate_from_qto(frame)

    def test_type_names_from_qto_are_text_in_the_detail_sheet(self):
        type_names = [
            "=1+1",
            '=HYPERLINK("http://example.invalid/bid","Open estimate")',
            "+1+1",
            "-2+3",
            "@SUM(1+1)",
            "\t=1+1",
            "=",
            "Basic Wall: Concrete 200",
            "Wall = 200mm",
        ]
        rows = [("Walls", name, 1.25) for name in type_names]
        # Two elements of the same type are grouped by estimate_from_qto.
        rows.append(("Walls", "=1+1", 2.75))

        with tempfile.TemporaryDirectory() as tmp:
            qto_path = Path(tmp) / "model.xlsx"
            _write_qto_workbook(qto_path, rows)
            frame = pd.read_excel(qto_path)
            self.assertEqual(
                set(frame["Type Name"].astype(str)),
                {name.replace("\r", "\n") for name in type_names},
            )

            line_items = self._estimate_qto(frame)
            self.assertTrue(line_items, "price match produced no line items")
            grouped = [item for item in line_items if str(item.bim_type).lstrip("'") in {"=1+1", "'=1+1"}]
            self.assertEqual(len(grouped), 1)
            self.assertEqual(grouped[0].quantity, 4.0)

            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            result = self.estimator.generate_estimate_report(
                line_items, "Office", str(out_dir)
            )
            workbook_path = Path(result["excel_path"])
            self.assertEqual(workbook_path, out_dir / "Office_Estimate.xlsx")

            workbook = load_workbook(workbook_path)
            detail = workbook["Detail"]
            for name in type_names:
                cell = _find_cell(detail, "BIM Type", name)
                _assert_neutralized(self, cell, name)

            self.assertEqual(_formula_texts(workbook_path), [])

    def test_estimate_from_bim_model_writes_type_name_as_text(self):
        malicious = '=HYPERLINK("http://example.invalid/bid","Open estimate")'
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "Building.rvt"
            model_path.write_bytes(b"not a revit file")
            _write_qto_workbook(
                model_path.with_suffix(".xlsx"),
                [("Walls", malicious, 3.0), ("Walls", "Basic Wall: Concrete 200", 1.0)],
            )
            out_dir = Path(tmp) / "estimates"
            out_dir.mkdir()

            def exporter_not_installed(cmd, *args, **kwargs):
                # The skill always shells out to RvtExporter.exe, which is not
                # in this repository. The workbook beside the model is the
                # QTO file that call is specified to produce.
                self.assertEqual(cmd[0], r"C:\DDC\RvtExporter.exe")
                self.assertEqual(cmd[1], str(model_path))
                self.assertEqual(cmd[2:], ["complete", "bbox"])
                return subprocess.CompletedProcess(cmd, 0)

            with unittest.mock.patch("subprocess.run", exporter_not_installed):
                result = self.skill["estimate_from_bim_model"](
                    model_path=str(model_path),
                    qdrant_url="https://qdrant.invalid",
                    language="EN",
                    output_dir=str(out_dir),
                )

            workbook_path = Path(result["excel_path"])
            detail = load_workbook(workbook_path)["Detail"]
            _assert_neutralized(self, _find_cell(detail, "BIM Type", malicious), malicious)
            _assert_neutralized(
                self,
                _find_cell(detail, "BIM Type", "Basic Wall: Concrete 200"),
                "Basic Wall: Concrete 200",
            )
            self.assertEqual(_formula_texts(workbook_path), [])

    def test_price_payload_strings_are_text_in_the_detail_sheet(self):
        _QdrantClient.payload = {
            "code": "=1+1",
            "description": '=HYPERLINK("http://example.invalid/rates","Rate")',
            "unit": "+m",
            "unit_price": 12.5,
            "labor_cost": 2.0,
            "material_cost": 3.5,
            "equipment_cost": 1.5,
            "productivity": 1.0,
            "currency": "-USD",
        }
        frame = pd.DataFrame(
            [{"Category": "Walls", "Type Name": "Basic Wall: Concrete 200", "Volume": 2.0}]
        )
        line_items = self._estimate_qto(frame)
        self.assertEqual(len(line_items), 1)
        item = line_items[0]
        # search_cwicr copies these fields straight through.
        self.assertIn("1+1", str(item.work_item.cwicr_code))
        self.assertIn("HYPERLINK", str(item.work_item.description))

        with tempfile.TemporaryDirectory() as tmp:
            result = self.estimator.generate_estimate_report(
                line_items, "Rates", tmp
            )
            workbook_path = Path(result["excel_path"])
            detail = load_workbook(workbook_path)["Detail"]
            expected = {
                "CWICR Code": "=1+1",
                "Work Item": '=HYPERLINK("http://example.invalid/rates","Rate")',
                "Unit": "+m",
                "Currency": "-USD",
                "BIM Type": "Basic Wall: Concrete 200",
            }
            for column, original in expected.items():
                _assert_neutralized(self, _find_cell(detail, column, original), original)
            self.assertEqual(_formula_texts(workbook_path), [])

    def test_phase_trade_and_by_trade_sheet_quote_formula_markers(self):
        skill = self.skill
        work = skill["WorkItem"](
            cwicr_code="@CW",
            description="\tlay bricks",
            unit="\r=m3",
            unit_price=8.0,
            labor_cost=3.0,
            material_cost=4.0,
            equipment_cost=1.0,
            productivity=1.0,
            currency="USD",
            confidence=0.5,
        )
        item = skill["CostLineItem"](
            bim_type="Basic Wall: Concrete 200",
            work_item=work,
            quantity=2.0,
            quantity_unit=work.unit,
            total_cost=16.0,
            labor_cost=6.0,
            material_cost=8.0,
            equipment_cost=2.0,
            phase="=1+1",
            trade='=HYPERLINK("http://example.invalid/trade","Trade")',
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = self.estimator.generate_estimate_report([item], "Phase", tmp)
            workbook_path = Path(result["excel_path"])
            workbook = load_workbook(workbook_path)
            detail = workbook["Detail"]
            for column, original in {
                "Phase": "=1+1",
                "Trade": '=HYPERLINK("http://example.invalid/trade","Trade")',
                "CWICR Code": "@CW",
                "Work Item": "\tlay bricks",
                "Unit": "\r=m3",
                "BIM Type": "Basic Wall: Concrete 200",
                "Currency": "USD",
            }.items():
                _assert_neutralized(self, _find_cell(detail, column, original), original)

            by_trade = workbook["By Trade"]
            trade_cells = [
                cell
                for row in by_trade.iter_rows(min_row=2)
                for cell in row
                if isinstance(cell.value, str)
            ]
            self.assertTrue(trade_cells, "By Trade sheet has no trade name")
            matched = [
                cell
                for cell in trade_cells
                if cell.value in {
                    item.trade,
                    _expected_text(item.trade),
                }
            ]
            self.assertEqual(len(matched), 1)
            _assert_neutralized(self, matched[0], item.trade)
            self.assertEqual(_formula_texts(workbook_path), [])

    def test_summary_sheet_stays_numeric(self):
        frame = pd.DataFrame(
            [{"Category": "Doors", "Type Name": "=1+1", "Volume": 2.0}]
        )
        line_items = self._estimate_qto(frame)
        with tempfile.TemporaryDirectory() as tmp:
            result = self.estimator.generate_estimate_report(line_items, "Sum", tmp)
            summary = load_workbook(result["excel_path"])["Summary"]
            headers = _header_map(summary)
            self.assertIn("Metric", headers)
            self.assertIn("Value", headers)
            values = [
                row[headers["Value"] - 1].value
                for row in summary.iter_rows(min_row=2)
            ]
            self.assertEqual(values, [20.0, 8.0, 10.0, 2.0])
            detail = load_workbook(result["excel_path"])["Detail"]
            _assert_neutralized(self, _find_cell(detail, "BIM Type", "=1+1"), "=1+1")
            self.assertEqual(_formula_texts(result["excel_path"]), [])


if __name__ == "__main__":
    unittest.main()
