"""Spreadsheet formula injection in document-classification-nlp.

These tests execute the classifier, extractors, and batch exporter in
``5_DDC_Innovative/document-classification-nlp/SKILL.md``. They fail while
extracted question, reason, and contractor lines that start with ``=`` are
written into the workbook as formulas. They pass once every such string is
sanitized before it is placed in the dataframe that ``to_excel`` writes.

Not asserted here, because opening the workbook does not run a formula for them
with pandas 3.1.1 and openpyxl 3.1.5:

- ``ConstructionDocumentClassifier.batch_classify`` never writes a workbook.
  Its date and reference captures cannot start with ``=``.
- ``ConstructionInfoExtractor.extract_all`` returns lists. ``DataFrame.to_excel``
  stringifies a list, so the cell text starts with ``[`` rather than ``=``.
- Cells whose text starts with ``+``, ``-``, or ``@`` are stored as inline
  strings. A drawing reference can capture a leading ``-`` and is stored the
  same way.
"""

from __future__ import annotations

import re
import sys
import types
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "5_DDC_Innovative" / "document-classification-nlp" / "SKILL.md"


def _load_skill():
    source = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", source, flags=re.DOTALL)
    module = types.ModuleType("document_classification_skill")
    module.__file__ = str(SKILL_PATH)
    sys.modules[module.__name__] = module
    namespace = module.__dict__
    for block in blocks:
        exec(compile(block, str(SKILL_PATH), "exec"), namespace)
    missing = [
        name
        for name in (
            "ConstructionDocumentClassifier",
            "ConstructionInfoExtractor",
            "process_document_batch",
        )
        if name not in namespace
    ]
    if missing:
        raise AssertionError(f"{SKILL_PATH} is missing {missing}")
    return namespace


SKILL = _load_skill()


def _workbook_formula_and_text(path: Path) -> tuple[list[str], str]:
    formulas: list[str] = []
    texts: list[str] = []
    with zipfile.ZipFile(path) as workbook:
        for name in workbook.namelist():
            if not name.endswith(".xml"):
                continue
            root = ET.fromstring(workbook.read(name))
            for element in root.iter():
                tag = element.tag.rsplit("}", 1)[-1]
                if not element.text:
                    continue
                if tag == "f":
                    formulas.append(element.text)
                elif tag == "t":
                    texts.append(element.text)
    return formulas, "\n".join(texts)


def _export(documents: list[str], output_path: Path):
    return SKILL["process_document_batch"](documents, str(output_path))


def _assert_marker_is_text_not_formula(path: Path, marker: str) -> None:
    formulas, text = _workbook_formula_and_text(path)
    assert formulas == []
    assert marker in text


@pytest.fixture()
def workbook(tmp_path: Path) -> Path:
    return tmp_path / "classification.xlsx"


def test_rfi_question_line_is_stored_as_text(workbook: Path) -> None:
    marker = "11001+11002"
    _export(
        ["Request for information\nQuestion: =" + marker + "\n"],
        workbook,
    )
    _assert_marker_is_text_not_formula(workbook, marker)


def test_rfi_clarify_line_with_leading_space_is_stored_as_text(workbook: Path) -> None:
    marker = "12001+12002"
    _export(["Please clarify    =" + marker], workbook)
    _assert_marker_is_text_not_formula(workbook, marker)


def test_rfi_clarify_line_with_leading_tab_is_stored_as_text(workbook: Path) -> None:
    marker = "12101+12102"
    _export(["Please clarify\t=" + marker], workbook)
    _assert_marker_is_text_not_formula(workbook, marker)


def test_rfi_confirm_line_is_stored_as_text(workbook: Path) -> None:
    marker = "13001+13002"
    _export(["Please confirm =" + marker], workbook)
    _assert_marker_is_text_not_formula(workbook, marker)


def test_rfi_advise_line_is_stored_as_text(workbook: Path) -> None:
    marker = "14001+14002"
    _export(["Please advise =" + marker], workbook)
    _assert_marker_is_text_not_formula(workbook, marker)


def test_change_order_reason_line_is_stored_as_text(workbook: Path) -> None:
    marker = "15001+15002"
    _export(["Change order\nReason: =" + marker + "\n"], workbook)
    _assert_marker_is_text_not_formula(workbook, marker)


def test_change_order_due_to_line_is_stored_as_text(workbook: Path) -> None:
    marker = "17001+17002"
    _export(
        ["Change order additional cost\nDue to =" + marker + ".\n"],
        workbook,
    )
    _assert_marker_is_text_not_formula(workbook, marker)


def test_change_order_because_of_line_is_stored_as_text(workbook: Path) -> None:
    marker = "19001+19002"
    _export(["Variation order\nBecause of =" + marker + ".\n"], workbook)
    _assert_marker_is_text_not_formula(workbook, marker)


def test_change_order_contractor_line_is_stored_as_text(workbook: Path) -> None:
    marker = "16001+16002"
    _export(
        ["Change order\nReason: unforeseen conditions\n\nContractor: =" + marker + "\n"],
        workbook,
    )
    _assert_marker_is_text_not_formula(workbook, marker)


def test_change_order_subcontractor_line_is_stored_as_text(workbook: Path) -> None:
    marker = "18001+18002"
    _export(
        [
            "Change order additional cost\n"
            "Reason: soil\n\n"
            "Subcontractor: =" + marker + "\n"
        ],
        workbook,
    )
    _assert_marker_is_text_not_formula(workbook, marker)


def test_batch_merges_every_captured_formula_as_text(workbook: Path) -> None:
    captured = {
        "Request for information\nQuestion: =21001+21002\n": "21001+21002",
        "Please clarify    =22001+22002": "22001+22002",
        "Please confirm =23001+23002": "23001+23002",
        "Please advise =24001+24002": "24001+24002",
        "Change order\nReason: =25001+25002\n\nContractor: =26001+26002\n": "25001+25002",
        "Change order\nReason: delay\n\nContractor: =26001+26002\n": "26001+26002",
        "Change order additional cost\nDue to =27001+27002.\n": "27001+27002",
        "Variation order\nBecause of =28001+28002.\n": "28001+28002",
        "Change order additional work\nSubcontractor: =29001+29002\n": "29001+29002",
    }
    _export(list(captured), workbook)
    formulas, text = _workbook_formula_and_text(workbook)
    assert formulas == []
    for marker in captured.values():
        assert marker in text
