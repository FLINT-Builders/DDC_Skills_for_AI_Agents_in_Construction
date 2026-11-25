"""Failing tests for unbounded recursion in the critical path analyzer.

The implementation under test is the Python block in
``1_DDC_Toolkit/Schedule-Management/critical-path-analyzer/SKILL.md``.
``import_from_dataframe`` and ``add_activity`` store predecessor links in
the order supplied, and ``_topological_sort`` walks those links with a
recursive ``visit``. A schedule whose first row is the end of a long
chain makes that walk deeper than the interpreter recursion limit.

These tests fail while that recursion remains. They pass once the sort is
iterative: a chain longer than the recursion limit still returns early and
late dates, and only an absurdly large graph may be refused. A refusal
must be an explicit limit error, must not be ``RecursionError``, and must
not leave an export file behind. Raising the interpreter recursion limit
is not a fix.

Operations covered, because each one calls ``_topological_sort``:

- ``CriticalPathAnalyzer._forward_pass``
- ``CriticalPathAnalyzer._backward_pass``
- ``CriticalPathAnalyzer.calculate_critical_path``
- ``CriticalPathAnalyzer.suggest_acceleration``
- ``CriticalPathAnalyzer.export_analysis``

``analyze_delay_impact`` and ``get_schedule_dates`` do not sort the network
and are not part of this bug.
"""

from __future__ import annotations

import ast
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "1_DDC_Toolkit"
    / "Schedule-Management"
    / "critical-path-analyzer"
    / "SKILL.md"
)

# Longer than the limit that was in force when this module was imported.
# Vulnerable code raises RecursionError. Iterative code returns a result.
CHAIN_LENGTH = sys.getrecursionlimit() + 50
DURATION = 10

# A configured ceiling may refuse this. It must not recurse, and export
# must not create a file if it refuses.
OVERSIZE_LENGTH = 20_000

PROJECT_START = date(2024, 6, 1)
_LIMIT_WORDS = ("limit", "depth", "too many", "too large", "exceed", "maximum", "cap")


def _extract_analyzer_source(skill_text: str) -> str:
    marker = "```python\n"
    pos = 0
    while True:
        start_marker = skill_text.find(marker, pos)
        if start_marker < 0:
            raise AssertionError(f"{SKILL_PATH} has no CriticalPathAnalyzer python block")
        code_start = start_marker + len(marker)
        code_end = skill_text.find("\n```", code_start)
        if code_end < 0:
            raise AssertionError(f"{SKILL_PATH} has an unclosed python block")
        block = skill_text[code_start:code_end]
        if "class CriticalPathAnalyzer" in block:
            # Keep traceback line numbers aligned with SKILL.md.
            return ("\n" * skill_text[:code_start].count("\n")) + block
        pos = code_end + 1


def _load_namespace() -> dict:
    source = _extract_analyzer_source(SKILL_PATH.read_text(encoding="utf-8"))
    namespace: dict = {"__file__": str(SKILL_PATH)}
    exec(compile(source, str(SKILL_PATH), "exec"), namespace)
    return namespace


def _analyzer_source() -> str:
    return _extract_analyzer_source(SKILL_PATH.read_text(encoding="utf-8"))


def _new_analyzer():
    return _load_namespace()["CriticalPathAnalyzer"](PROJECT_START)


def _spine_id(length: int, index: int) -> str:
    """Lexical order visits the sink (index length-1) before the source."""
    return f"S{length - 1 - index:05d}"


def _side_id(length: int) -> str:
    return f"L{length:05d}"


def _schedule_frame(length: int, *, sink_first: bool) -> pd.DataFrame:
    """Finish-to-start spine plus one side leaf into the middle activity.

    ``sink_first=True`` lists the end of the chain on the first row, which
    is the order that drives the recursive visit to the bottom of the chain.
    """
    side = _side_id(length)
    mid = length // 2
    order = list(range(length - 1, -1, -1)) if sink_first else list(range(length))
    rows = []
    for index in order:
        if index == 0:
            predecessors = ""
        elif index == mid:
            predecessors = f" {_spine_id(length, index - 1)} , {side} ,"
        else:
            predecessors = f" {_spine_id(length, index - 1)} "
        rows.append(
            {
                "activity_id": _spine_id(length, index),
                "name": f"Spine {index}",
                "duration": DURATION,
                "predecessors": predecessors,
            }
        )
    rows.append(
        {
            "activity_id": side,
            "name": "Side leaf",
            "duration": DURATION,
            "predecessors": "",
        }
    )
    return pd.DataFrame(rows)


def _add_schedule(analyzer, length: int, *, sink_first: bool) -> None:
    frame = _schedule_frame(length, sink_first=sink_first)
    # Same links as the table, through the direct API instead of the importer.
    for row in frame.to_dict(orient="records"):
        raw = row["predecessors"]
        if raw is None or (isinstance(raw, float) and pd.isna(raw)) or str(raw).strip() == "":
            predecessors = []
        else:
            predecessors = [part.strip() for part in str(raw).split(",") if part.strip()]
        analyzer.add_activity(
            str(row["activity_id"]),
            row["name"],
            int(row["duration"]),
            predecessors,
        )


def _import_schedule(analyzer, length: int, *, sink_first: bool) -> None:
    analyzer.import_from_dataframe(_schedule_frame(length, sink_first=sink_first))


def _expected_floats(length: int) -> dict:
    mid = length // 2
    expected = {}
    for index in range(length):
        start = index * DURATION
        expected[_spine_id(length, index)] = {
            "early_start": start,
            "early_finish": start + DURATION,
            "late_start": start,
            "late_finish": start + DURATION,
            "total_float": 0,
            "is_critical": True,
        }
    side_float = DURATION * (mid - 1)
    expected[_side_id(length)] = {
        "early_start": 0,
        "early_finish": DURATION,
        "late_start": side_float,
        "late_finish": side_float + DURATION,
        "total_float": side_float,
        "is_critical": False,
    }
    return expected


def _assert_schedule_result(analyzer, length: int) -> None:
    result = analyzer.calculate_critical_path()
    assert result.project_duration == length * DURATION
    expected = _expected_floats(length)
    assert set(result.critical_path) == {
        activity_id for activity_id, dates in expected.items() if dates["is_critical"]
    }
    assert result.near_critical == []
    assert result.total_float_days == expected[_side_id(length)]["total_float"]
    assert set(result.activities) == set(expected)
    for activity_id, dates in expected.items():
        activity = result.activities[activity_id]
        assert activity.early_start == dates["early_start"]
        assert activity.early_finish == dates["early_finish"]
        assert activity.late_start == dates["late_start"]
        assert activity.late_finish == dates["late_finish"]
        assert activity.total_float == dates["total_float"]
        assert activity.is_critical is dates["is_critical"]


def _is_configured_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, (RecursionError, MemoryError, OSError, SystemExit, KeyboardInterrupt)):
        return False
    message = str(exc).lower()
    return any(word in message for word in _LIMIT_WORDS)


def _functions_in(tree: ast.AST):
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{prefix}.{child.name}" if prefix else child.name
                yield qualified, child
                yield from walk(child, qualified)
            else:
                yield from walk(child, prefix)

    yield from walk(tree, "")


def _calls_in_function(function: ast.AST) -> set[str]:
    calls: set[str] = set()

    class Finder(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            if node is function:
                self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Name):
                calls.add(func.id)
            elif isinstance(func, ast.Attribute):
                calls.add(func.attr)
            self.generic_visit(node)

    Finder().visit(function)
    return calls


def _recursion_cycle(class_node: ast.ClassDef) -> list[str] | None:
    functions = dict(_functions_in(class_node))
    by_simple: dict[str, list[str]] = {}
    for qualified in functions:
        by_simple.setdefault(qualified.rsplit(".", 1)[-1], []).append(qualified)
    graph = {qualified: set() for qualified in functions}
    for qualified, function in functions.items():
        for callee in _calls_in_function(function):
            for target in by_simple.get(callee, []):
                graph[qualified].add(target)

    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        if node in visiting:
            return stack[stack.index(node) :] + [node]
        if node in visited:
            return None
        visiting.add(node)
        stack.append(node)
        for nxt in graph[node]:
            found = dfs(nxt)
            if found:
                return found
        stack.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in graph:
        found = dfs(node)
        if found:
            return found
    return None


def _stack_limit_calls(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"setrecursionlimit", "setrlimit"}:
            lines.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id in {"setrecursionlimit", "setrlimit"}:
            lines.append(node.lineno)
    return lines


def test_sort_does_not_recurse_or_raise_the_stack_limit():
    """The shipped sort must be iterative. A higher recursion limit is not a fix."""
    module = ast.parse(_analyzer_source())
    analyzer = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "CriticalPathAnalyzer"
    )
    cycle = _recursion_cycle(analyzer)
    assert cycle is None, (
        "CriticalPathAnalyzer still recurses ("
        + " -> ".join(cycle or [])
        + "). Replace the visit with an iterative topological sort."
    )
    limit_calls = _stack_limit_calls(module)
    assert limit_calls == [], (
        "SKILL.md raises the interpreter or OS stack limit at lines "
        f"{limit_calls}. Bound the graph or sort iteratively instead."
    )


def test_imported_sink_first_chain_returns_dates():
    analyzer = _new_analyzer()
    _import_schedule(analyzer, CHAIN_LENGTH, sink_first=True)
    _assert_schedule_result(analyzer, CHAIN_LENGTH)


def test_imported_source_first_chain_returns_dates():
    analyzer = _new_analyzer()
    _import_schedule(analyzer, CHAIN_LENGTH, sink_first=False)
    _assert_schedule_result(analyzer, CHAIN_LENGTH)


def test_add_activity_sink_first_chain_returns_dates():
    analyzer = _new_analyzer()
    _add_schedule(analyzer, CHAIN_LENGTH, sink_first=True)
    _assert_schedule_result(analyzer, CHAIN_LENGTH)


def test_forward_and_backward_passes_on_sink_first_chain():
    analyzer = _new_analyzer()
    _import_schedule(analyzer, CHAIN_LENGTH, sink_first=True)
    analyzer._forward_pass()
    analyzer._backward_pass()
    expected = _expected_floats(CHAIN_LENGTH)
    for activity_id, dates in expected.items():
        activity = analyzer.activities[activity_id]
        assert activity.early_finish == dates["early_finish"]
        assert activity.late_start == dates["late_start"]
        assert activity.total_float == dates["total_float"]
        assert activity.is_critical is dates["is_critical"]


def test_suggest_acceleration_on_sink_first_chain():
    analyzer = _new_analyzer()
    _import_schedule(analyzer, CHAIN_LENGTH, sink_first=True)
    suggestions = analyzer.suggest_acceleration(target_reduction=5)
    critical_ids = {_spine_id(CHAIN_LENGTH, index) for index in range(CHAIN_LENGTH)}
    assert {item["activity"] for item in suggestions} == critical_ids
    assert all(item["max_reduction"] == int(DURATION * 0.2) for item in suggestions)
    assert _side_id(CHAIN_LENGTH) not in {item["activity"] for item in suggestions}


def test_export_analysis_on_sink_first_chain(tmp_path: Path):
    analyzer = _new_analyzer()
    _import_schedule(analyzer, CHAIN_LENGTH, sink_first=True)
    output = tmp_path / "analysis.xlsx"
    returned = analyzer.export_analysis(str(output))
    assert returned == str(output)
    assert output.is_file()
    summary = pd.read_excel(output, sheet_name="Summary")
    assert int(summary.loc[0, "Project Duration"]) == CHAIN_LENGTH * DURATION
    critical = pd.read_excel(output, sheet_name="Critical Path")
    assert set(critical["Activity"]) == {_spine_id(CHAIN_LENGTH, index) for index in range(CHAIN_LENGTH)}


def test_oversized_chain_does_not_recurse():
    analyzer = _new_analyzer()
    _import_schedule(analyzer, OVERSIZE_LENGTH, sink_first=True)
    try:
        result = analyzer.calculate_critical_path()
    except RecursionError as exc:
        pytest.fail(f"calculate_critical_path raised RecursionError: {exc}")
    except Exception as exc:
        assert _is_configured_limit_error(exc), exc
        return
    assert result.project_duration == OVERSIZE_LENGTH * DURATION


def test_oversized_export_creates_no_file_when_refused(tmp_path: Path):
    analyzer = _new_analyzer()
    _import_schedule(analyzer, OVERSIZE_LENGTH, sink_first=True)
    output = tmp_path / "analysis.xlsx"
    try:
        returned = analyzer.export_analysis(str(output))
    except RecursionError as exc:
        pytest.fail(f"export_analysis raised RecursionError: {exc}")
    except Exception as exc:
        assert _is_configured_limit_error(exc), exc
        assert not output.exists(), "export created a file before refusing the schedule"
        return
    assert returned == str(output)
    assert output.is_file()


def test_quick_start_network_is_unchanged():
    """The branched network from the skill's quick start keeps its dates."""
    analyzer = _new_analyzer()
    analyzer.add_activity("A", "Site Preparation", 10, [])
    analyzer.add_activity("B", "Foundation Excavation", 15, ["A"])
    analyzer.add_activity("C", "Foundation Concrete", 20, ["B"])
    analyzer.add_activity("D", "Structural Steel", 30, ["C"])
    analyzer.add_activity("E", "MEP Rough-in", 25, ["C"])
    analyzer.add_activity("F", "Exterior Walls", 20, ["D"])
    analyzer.add_activity("G", "Interior Finish", 15, ["E", "F"])

    result = analyzer.calculate_critical_path()
    assert result.project_duration == 110
    assert set(result.critical_path) == {"A", "B", "C", "D", "F", "G"}
    assert result.near_critical == []
    assert result.activities["E"].total_float == 25
    assert result.activities["E"].is_critical is False
    assert result.activities["G"].early_start == 95
    assert result.activities["G"].late_finish == 110
