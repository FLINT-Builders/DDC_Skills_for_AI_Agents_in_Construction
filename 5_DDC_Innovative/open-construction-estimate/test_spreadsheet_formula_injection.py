"""Spreadsheet formula injection in the open construction estimate export.

These tests exercise OpenConstructionEstimator from SKILL.md. They fail while
element text and matched price-book text that start with "=" are written into
the workbook as formulas. They pass once every text column is stored as a
literal string.

Run:

    pip install pandas openpyxl scikit-learn
    python3 -m unittest 5_DDC_Innovative/open-construction-estimate/test_spreadsheet_formula_injection.py
"""

from __future__ import annotations

import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

SKILL_PATH = Path(__file__).resolve().parent / "SKILL.md"

# Price-book row that is a spreadsheet formula and still outranks a benign row
# for a reinforced-concrete query, because the formula text contains those words.
MALICIOUS_CODE = "=40+1"
MALICIOUS_DESCRIPTION = '=L2&"reinforced concrete wall"'
MALICIOUS_UNIT = "=60+1"

ELEMENT_ID = "=10+1"
ELEMENT_TYPE = "=20+1"
ELEMENT_DESCRIPTION = "=L2"
UNIT_PRICE = 12.5
QUANTITY = 4
EXPECTED_TOTAL = UNIT_PRICE * QUANTITY


def _install_embedding_import_guard() -> None:
    """The matcher imports SentenceTransformer while the module loads.

    estimate_element() with use_embeddings=False never constructs it. Guard the
    import so this test does not download a model, and fail if that path is used.
    """
    if "sentence_transformers" in sys.modules:
        return
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        module = types.ModuleType("sentence_transformers")

        class SentenceTransformer:
            def __init__(self, *args, **kwargs):
                raise AssertionError("semantic embedding path is not under test")

            def encode(self, *args, **kwargs):
                raise AssertionError("semantic embedding path is not under test")

        module.SentenceTransformer = SentenceTransformer
        sys.modules["sentence_transformers"] = module


def _python_blocks(skill_text: str) -> list[str]:
    blocks = []
    fence = None
    body: list[str] = []
    for line in skill_text.splitlines(keepends=True):
        stripped = line.strip()
        if fence is None and stripped.startswith("```"):
            fence = stripped[3:].strip().lower()
            body = []
            continue
        if fence is not None and stripped == "```":
            if fence in {"python", "py"}:
                blocks.append("".join(body))
            fence = None
            continue
        if fence is not None:
            body.append(line)
    return blocks


def _defines_estimator(block: str) -> bool:
    try:
        tree = ast.parse(block)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.ClassDef) and node.name == "OpenConstructionEstimator"
        for node in tree.body
    )


def load_estimator_classes():
    """Execute the skill's definition blocks, skipping the quick-start sample.

    The quick start reads a fixed CSV at import time. The estimator class is
    the export path under test. Later blocks are included when present so a
    sanitizer helper added beside the class is loaded too.
    """
    _install_embedding_import_guard()
    blocks = _python_blocks(SKILL_PATH.read_text(encoding="utf-8"))
    if not any(_defines_estimator(block) for block in blocks):
        raise AssertionError(f"{SKILL_PATH} does not define OpenConstructionEstimator")

    namespace: dict = {"__name__": "open_construction_estimate_skill"}
    for block in blocks:
        # The quick-start sample reads a fixed database path at load time.
        if "def find_matching_items" in block:
            continue
        exec(block, namespace)  # noqa: S102 - the skill source is the code under test
    return namespace["WorkItemMatcher"], namespace["OpenConstructionEstimator"]


def _write_price_book(path: Path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_csv(path, index=False)


def _export(rows: list[dict], elements: list[dict], output: Path) -> Path:
    work_item_matcher, estimator_cls = load_estimator_classes()
    database = output.with_suffix(".csv")
    _write_price_book(database, rows)
    matcher = work_item_matcher(str(database), use_embeddings=False)
    estimator = estimator_cls(matcher, region="default")
    estimator.estimate_project(elements)
    estimator.export_estimate(str(output))
    return output


def _cell(worksheet, header: str, row: int = 2):
    headers = [cell.value for cell in worksheet[1]]
    if header not in headers:
        raise AssertionError(f"{worksheet.title} missing column {header!r}: {headers}")
    return worksheet.cell(row=row, column=headers.index(header) + 1)


def _literal_text_errors(cell, payload: str) -> list[str]:
    value = cell.value
    if isinstance(value, str) and value.startswith("'") and value[1:] == payload:
        value = payload
    errors = []
    if value != payload:
        errors.append(
            f"{cell.parent.title}!{cell.coordinate} held {cell.value!r}, expected {payload!r}"
        )
    if cell.data_type == "f":
        errors.append(
            f"{cell.parent.title}!{cell.coordinate} stored {payload!r} as an Excel formula"
        )
    return errors


class EstimateFormulaInjectionTests(unittest.TestCase):
    def test_element_text_is_stored_as_literal_text(self):
        rows = [
            {
                "code": "03.31.13",
                "description": "reinforced concrete wall",
                "unit": "m3",
                "unit_price": UNIT_PRICE,
                "category_l1": "Concrete",
            },
            {
                "code": "09.90.00",
                "description": "paint finish",
                "unit": "m2",
                "unit_price": 3.0,
                "category_l1": "Finishes",
            },
        ]
        element = {
            "id": ELEMENT_ID,
            "ifc_type": ELEMENT_TYPE,
            "description": ELEMENT_DESCRIPTION,
            "material": "reinforced concrete",
            "category": "wall",
            "quantity": QUANTITY,
        }
        with tempfile.TemporaryDirectory() as tmp:
            workbook_path = _export(rows, [element], Path(tmp) / "estimate.xlsx")
            workbook = load_workbook(workbook_path)
            details = workbook["Details"]
            # The formula is carried on the element. Matching still selects the
            # concrete price row from the other columns of the query.
            self.assertEqual(
                _cell(details, "matched_description").value, "reinforced concrete wall"
            )
            self.assertEqual(_cell(details, "total_cost").value, EXPECTED_TOTAL)
            by_type = workbook["By_Type"]
            self.assertEqual(_cell(by_type, "total_cost").value, EXPECTED_TOTAL)
            errors = []
            errors.extend(_literal_text_errors(_cell(details, "element_id"), ELEMENT_ID))
            errors.extend(_literal_text_errors(_cell(details, "element_type"), ELEMENT_TYPE))
            errors.extend(
                _literal_text_errors(_cell(details, "element_description"), ELEMENT_DESCRIPTION)
            )
            errors.extend(_literal_text_errors(_cell(by_type, "element_type"), ELEMENT_TYPE))
            self.assertEqual(errors, [])

    def test_matched_price_book_text_is_stored_as_literal_text(self):
        rows = [
            {
                "code": MALICIOUS_CODE,
                "description": MALICIOUS_DESCRIPTION,
                "unit": MALICIOUS_UNIT,
                "unit_price": UNIT_PRICE,
                "category_l1": "Concrete",
            },
            {
                "code": "09.90.00",
                "description": "paint finish",
                "unit": "m2",
                "unit_price": 3.0,
                "category_l1": "Finishes",
            },
        ]
        element = {
            "id": "W-1",
            "ifc_type": "Wall",
            "description": "reinforced concrete wall",
            "material": "reinforced concrete",
            "category": "wall",
            "quantity": QUANTITY,
        }
        with tempfile.TemporaryDirectory() as tmp:
            workbook_path = _export(rows, [element], Path(tmp) / "estimate.xlsx")
            workbook = load_workbook(workbook_path)
            details = workbook["Details"]
            errors = []
            errors.extend(_literal_text_errors(_cell(details, "matched_code"), MALICIOUS_CODE))
            errors.extend(
                _literal_text_errors(_cell(details, "matched_description"), MALICIOUS_DESCRIPTION)
            )
            errors.extend(_literal_text_errors(_cell(details, "unit"), MALICIOUS_UNIT))
            self.assertEqual(errors, [])
            self.assertEqual(_cell(details, "element_description").value, "reinforced concrete wall")
            self.assertGreater(_cell(details, "match_confidence").value, 0.5)
            self.assertEqual(_cell(details, "total_cost").value, EXPECTED_TOTAL)

    def test_ordinary_description_exports_as_text(self):
        rows = [
            {
                "code": "03.31.13",
                "description": "reinforced concrete wall",
                "unit": "m3",
                "unit_price": UNIT_PRICE,
                "category_l1": "Concrete",
            }
        ]
        element = {
            "id": "W-1",
            "ifc_type": "Wall",
            "description": "reinforced concrete wall 300mm",
            "material": "reinforced concrete",
            "quantity": QUANTITY,
        }
        with tempfile.TemporaryDirectory() as tmp:
            workbook_path = _export(rows, [element], Path(tmp) / "estimate.xlsx")
            workbook = load_workbook(workbook_path)
            details = workbook["Details"]
            self.assertEqual(
                _literal_text_errors(
                    _cell(details, "element_description"), "reinforced concrete wall 300mm"
                ),
                [],
            )
            self.assertEqual(
                _literal_text_errors(
                    _cell(details, "matched_description"), "reinforced concrete wall"
                ),
                [],
            )
            self.assertEqual(_cell(details, "total_cost").value, EXPECTED_TOTAL)


if __name__ == "__main__":
    unittest.main()
