"""Failing spec for quadratic backtracking in duration extraction.

The document-classification skill compiles this pattern:

    duration_days = r'(\\d+)\\s*(?:calendar\\s+)?days?'

and runs it, with IGNORECASE, over caller-supplied document text:

* ConstructionInfoExtractor.extract_all uses re.findall on every pattern,
  including duration_days.
* ConstructionInfoExtractor.extract_change_order_details uses _find_first,
  which uses re.search, on duration_days.
* process_document_batch classifies with the untrained
  ConstructionDocumentClassifier (rule-based fallback) and then calls
  extract_all for every class other than RFI and Change Order, and
  extract_change_order_details for Change Order.

A run of digits with no following "day" makes the greedy digit match
fail and backtrack at every starting position. These tests execute the
code blocks in SKILL.md. They fail while that pattern is unbounded and
pass once extraction of a long digit run finishes within the time limit
below and still returns ordinary durations such as "14 calendar days".

Not covered here: extract_rfi_details does not read duration_days.
batch_classify does not call the extractor. The same class of pattern in
4_DDC_Curated/Contract-Legal/contract-clause-extractor/SKILL.md
(KEY_TERM_PATTERNS["time_periods"]) is a separate skill and is not loaded.
"""

from __future__ import annotations

import math
import multiprocessing
import os
import re
import tempfile
import traceback
from pathlib import Path

import pytest

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "5_DDC_Innovative"
    / "document-classification-nlp"
    / "SKILL.md"
)

# 25_000 nines is enough for the unbounded pattern to stay busy well past
# TIME_LIMIT_SECONDS on this interpreter, and small enough that a bounded
# digit run (or a hard length cap below a few thousand characters) returns
# immediately. A plain letter plus this run is what a submitted document
# would contain.
DIGIT_RUN = 25_000
TIME_LIMIT_SECONDS = 1.0

_SKILL = None


def _load_skill():
    missing = []
    for name in ("pandas", "sklearn", "spacy", "openpyxl"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        pytest.fail(
            "Install the imports SKILL.md executes before running these "
            "tests: python3 -m pip install pandas scikit-learn spacy openpyxl. "
            f"Missing: {', '.join(missing)}"
        )

    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    # Leave __name__ unset. @dataclass looks the class module up in
    # sys.modules, and a made-up module name is not registered there.
    namespace = {}
    loaded = 0
    for block in blocks:
        if (
            "class ConstructionDocumentClassifier" in block
            or "class ConstructionInfoExtractor" in block
            or "def process_document_batch" in block
        ):
            exec(block, namespace)
            loaded += 1
    if loaded != 3:
        pytest.fail(
            f"Expected the classifier, extractor, and batch blocks in {SKILL_PATH}, "
            f"found {loaded}"
        )
    return namespace


def skill():
    global _SKILL
    if _SKILL is None:
        _SKILL = _load_skill()
    return _SKILL


def _digit_document(prefix: str) -> str:
    return prefix + ("9" * DIGIT_RUN)


def _empty(value) -> bool:
    """True when extraction found nothing.

    process_document_batch builds a DataFrame, which turns None into NaN.
    """
    if value is None or value == []:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def _worker(op: str, document: str, documents: list, output_path: str, queue):
    try:
        ns = skill()
        extractor = ns["ConstructionInfoExtractor"]()
        if op == "extract_all":
            value = extractor.extract_all(document)
        elif op == "extract_change_order_details":
            value = extractor.extract_change_order_details(document)
        elif op == "find_first_duration":
            value = extractor._find_first(document, extractor.patterns["duration_days"])
        elif op == "process_document_batch":
            frame = ns["process_document_batch"](documents, output_path)
            value = frame.to_dict(orient="records")
        else:
            raise AssertionError(f"unknown op {op}")
        queue.put({"ok": True, "value": value})
    except Exception:
        queue.put({"ok": False, "error": traceback.format_exc()})


def _run_bounded(op: str, document: str = "", documents=None):
    """Run one skill operation in a child process and require it to finish."""
    skill()
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    fd, output_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    proc = ctx.Process(
        target=_worker,
        args=(op, document, documents or [], output_path, queue),
    )
    proc.start()
    proc.join(TIME_LIMIT_SECONDS)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        pytest.fail(
            f"{op} was still running after {TIME_LIMIT_SECONDS:.0f}s on a "
            f"{DIGIT_RUN}-digit run. The duration pattern backtracks over the "
            "whole run at every digit."
        )
    try:
        payload = queue.get(timeout=2)
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass
    assert payload["ok"], payload.get("error")
    return payload["value"]


def test_extract_all_finishes_on_letter_with_digit_run():
    """Other documents reach extract_all. A digit run must not stall it."""
    value = _run_bounded(
        "extract_all",
        document=_digit_document("Please review the attached log.\n"),
    )
    assert not value["duration_days"]


def test_find_first_duration_finishes_on_digit_run():
    """_find_first is the re.search path used for a single duration."""
    value = _run_bounded(
        "find_first_duration",
        document=_digit_document("Reference log\n"),
    )
    assert value is None


def test_extract_change_order_details_finishes_on_digit_run():
    """Change orders reach duration_days through extract_change_order_details."""
    value = _run_bounded(
        "extract_change_order_details",
        document=_digit_document("Change order for unforeseen conditions.\n"),
    )
    assert not value["duration_impact"]


def test_batch_other_document_does_not_stall_following_rfi():
    """process_document_batch keeps going after a non-matching document."""
    rows = _run_bounded(
        "process_document_batch",
        documents=[
            _digit_document("Please review the attached log.\n"),
            "Request for information RFI-17\nPlease clarify the foundation spacing.\n",
        ],
    )
    assert [row["Classification"] for row in rows] == ["Other", "RFI"]
    assert _empty(rows[0]["duration_days"])
    assert rows[1]["rfi_number"] == "17"


def test_batch_change_order_digit_run_finishes():
    """The change-order branch of process_document_batch uses the same pattern."""
    rows = _run_bounded(
        "process_document_batch",
        documents=[
            _digit_document("Change order for unforeseen conditions.\nCost $10.00\n"),
        ],
    )
    assert rows[0]["Classification"] == "Change Order"
    assert rows[0]["cost_impact"] == 10.0
    assert _empty(rows[0]["duration_impact"])


def test_extract_all_finds_real_duration_after_digit_run():
    """A real duration after the run is still extracted, and the run stays cheap."""
    value = _run_bounded(
        "extract_all",
        document=_digit_document("Log follows.\n") + "\nSchedule impact is 10 days.\n",
    )
    assert value["duration_days"] == ["10"]


def test_extract_all_reads_ordinary_durations():
    extractor = skill()["ConstructionInfoExtractor"]()
    text = (
        "Weather delay of 14 calendar days, plus 3 days, a 1 day cure, "
        "then 21 DAYS, and 1000 days."
    )
    assert extractor.extract_all(text)["duration_days"] == [
        "14",
        "3",
        "1",
        "21",
        "1000",
    ]


def test_extract_change_order_details_reads_calendar_duration():
    extractor = skill()["ConstructionInfoExtractor"]()
    text = (
        "Change Order CO-9\n"
        "The duration impact is 45 calendar days.\n"
        "Cost $2,500.00\n"
    )
    details = extractor.extract_change_order_details(text)
    assert details["co_number"] == "9"
    assert details["duration_impact"] == "45"
    assert details["cost_impact"] == 2500.0


def test_batch_classifies_and_extracts_ordinary_documents(tmp_path):
    output = tmp_path / "batch.xlsx"
    frame = skill()["process_document_batch"](
        [
            "Request for information RFI-17\nPlease clarify the foundation spacing.\n",
            "Change order for additional work. Duration impact 45 calendar days. Cost $10.00\n",
            "Transmittal cover note with no form type.\n",
        ],
        str(output),
    )
    assert list(frame["Classification"]) == ["RFI", "Change Order", "Other"]
    assert frame.loc[0, "rfi_number"] == "17"
    assert frame.loc[1, "duration_impact"] == "45"
    assert output.is_file()
