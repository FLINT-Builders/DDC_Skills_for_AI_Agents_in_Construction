"""Cycle handling for critical-path recursion published in the skills.

The 4D sequence analyzer and the schedule compressor both recurse over
predecessors and store the memo only after those calls return. A cycle never
hits a base case, so the call stack grows until Python raises RecursionError.

These tests load the Python blocks from the skill files and drive the public
methods with cyclic predecessor lists. They fail while that recursion is
unbounded. They pass when each method either finishes or refuses the cycle
without exhausting the stack, and a refusal reports a cycle.

Not covered, because those walks do not share this bug:
- GanttChartGenerator.get_critical_path only scans direct predecessors.
- CriticalPathAnalyzer._topological_sort marks a node visited before recursing.
- The insights critical-path analyzer uses a queue and a processed set.
- ScheduleCompressor.generate_compression_report only formats an existing plan.
"""

from __future__ import annotations

import re
import signal
import subprocess
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_4D = (
    REPO_ROOT
    / "2_DDC_Book"
    / "3.3-4D-BIM-CO2-Simulation"
    / "4d-simulation"
    / "SKILL.md"
)
SKILL_COMPRESSION = (
    REPO_ROOT
    / "3_DDC_Insights"
    / "Schedule-Optimization"
    / "schedule-compression"
    / "SKILL.md"
)

CYCLE_WORDS = ("cycle", "circular", "loop")


def extract_python_block(path: Path, must_contain: str) -> tuple[str, int]:
    """Return one fenced Python block and the 1-based line where its body starts."""
    text = path.read_text(encoding="utf-8")
    matches = [
        match
        for match in re.finditer(r"```python\n(.*?)```", text, flags=re.DOTALL)
        if must_contain in match.group(1)
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one Python block containing {must_contain!r} in {path}, "
            f"found {len(matches)}"
        )
    match = matches[0]
    start_line = text[: match.start(1)].count("\n") + 1
    return match.group(1), start_line


def execute_skill_block(path: Path, must_contain: str, module_name: str, extra: dict | None = None) -> dict:
    """Execute the published skill block with line numbers aligned to the file.

    The block is registered in sys.modules first. dataclasses looks the class
    module up there while it builds the published Activity type.
    """
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    if extra:
        module.__dict__.update(extra)
    sys.modules[module_name] = module
    body, start_line = extract_python_block(path, must_contain)
    padded = ("\n" * (start_line - 1)) + body
    code = compile(padded, str(path), "exec")
    exec(code, module.__dict__)
    return module.__dict__


def load_sequence_analyzer():
    namespace = execute_skill_block(
        SKILL_4D,
        "class SequenceAnalyzer",
        "skill_4d_simulation",
        {
            "pd": pd,
            "datetime": datetime,
            "timedelta": timedelta,
            "List": List,
            "Dict": Dict,
            "Optional": Optional,
        },
    )
    return namespace["SequenceAnalyzer"]


def load_schedule_compressor():
    namespace = execute_skill_block(
        SKILL_COMPRESSION,
        "class ScheduleCompressor",
        "skill_schedule_compression",
    )
    return namespace["ScheduleCompressor"]


def element_frame(element_ids: list[str]) -> pd.DataFrame:
    """Schedule rows the 4D analyzer reads: id plus start and end dates."""
    start = pd.to_datetime("2024-01-01")
    end = pd.to_datetime("2024-01-11")
    count = len(element_ids)
    return pd.DataFrame(
        {
            "ElementId": list(element_ids),
            "Category": ["Structure"] * count,
            "Level": ["Level 1"] * count,
            "Start_Date": [start] * count,
            "End_Date": [end] * count,
        }
    )


def call_bounded(operation: Callable, seconds: float = 2.0):
    """Run operation on the main thread and stop a non-terminating walk."""

    def _handle_alarm(signum, frame):
        raise TimeoutError("dependency walk did not finish")

    previous = signal.signal(signal.SIGALRM, _handle_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return operation()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def assert_cycle_does_not_crash(operation: Callable):
    """Fail on RecursionError or a hang. A cycle refusal must name the cycle."""
    try:
        return call_bounded(operation)
    except RecursionError as exc:
        raise AssertionError(
            "cyclic predecessors exhausted the Python call stack"
        ) from exc
    except TimeoutError as exc:
        raise AssertionError(
            "cyclic predecessors did not finish the dependency walk"
        ) from exc
    except Exception as exc:
        label = f"{type(exc).__name__}: {exc}".lower()
        if not any(word in label for word in CYCLE_WORDS):
            raise AssertionError(
                "operation failed without reporting a dependency cycle "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        return None


def _run_published_cycle(kind: str) -> int:
    """Child-process entry: exit 0 only when the published walk survives a cycle."""
    try:
        if kind == "4d":
            analyzer_cls = load_sequence_analyzer()
            analyzer = analyzer_cls(element_frame(["A", "B"]))
            # A lists B as predecessor, and B lists A.
            analyzer.add_dependency("B", "A", lag_days=0)
            analyzer.add_dependency("A", "B", lag_days=0)
            assert_cycle_does_not_crash(analyzer.calculate_critical_path)
        elif kind == "compression":
            compressor_cls = load_schedule_compressor()
            compressor = compressor_cls()
            compressor.add_activity(
                "A", "Element A", 5, 3, 100.0, 150.0, predecessors=["B"]
            )
            compressor.add_activity(
                "B", "Element B", 5, 3, 100.0, 150.0, predecessors=["A"]
            )
            assert_cycle_does_not_crash(compressor.calculate_project_duration)
        else:
            print(f"unknown kind {kind}", file=sys.stderr)
            return 2
    except AssertionError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def test_planner_process_survives_4d_cycle():
    """The attack path runs as its own process, the way a planner script exits."""
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--run-cycle", "4d"],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=REPO_ROOT,
    )
    assert completed.returncode == 0, (
        "4D critical-path process crashed on a two-node cycle\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert "RecursionError" not in completed.stderr


def test_compressor_process_survives_cycle():
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--run-cycle", "compression"],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=REPO_ROOT,
    )
    assert completed.returncode == 0, (
        "schedule-compression process crashed on a two-node cycle\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert "RecursionError" not in completed.stderr


def test_mutual_cycle_with_lag_does_not_crash():
    analyzer_cls = load_sequence_analyzer()
    analyzer = analyzer_cls(element_frame(["A", "B"]))
    analyzer.add_dependency("B", "A", lag_days=3)
    analyzer.add_dependency("A", "B", lag_days=3)
    recorded = list(analyzer.dependencies)

    assert_cycle_does_not_crash(analyzer.calculate_critical_path)

    assert analyzer.dependencies == recorded


def test_self_cycle_does_not_crash():
    analyzer_cls = load_sequence_analyzer()
    analyzer = analyzer_cls(element_frame(["A"]))
    analyzer.add_dependency("A", "A")

    assert_cycle_does_not_crash(analyzer.calculate_critical_path)


def test_three_node_cycle_does_not_crash():
    analyzer_cls = load_sequence_analyzer()
    analyzer = analyzer_cls(element_frame(["A", "B", "C"]))
    # A depends on C, C depends on B, B depends on A.
    analyzer.add_dependency("C", "A")
    analyzer.add_dependency("B", "C")
    analyzer.add_dependency("A", "B")

    assert_cycle_does_not_crash(analyzer.calculate_critical_path)


def test_cycle_among_otherwise_valid_tasks_does_not_crash():
    analyzer_cls = load_sequence_analyzer()
    analyzer = analyzer_cls(element_frame(["A", "B", "C", "D"]))
    analyzer.add_dependency("A", "B")
    analyzer.add_dependency("D", "C")
    analyzer.add_dependency("C", "D")

    assert_cycle_does_not_crash(analyzer.calculate_critical_path)


def test_acyclic_chain_still_returns_longest_element():
    """A fix has to keep the published result for a chain with no cycle."""
    analyzer_cls = load_sequence_analyzer()
    analyzer = analyzer_cls(element_frame(["A", "B"]))
    analyzer.add_dependency("A", "B")

    result = call_bounded(analyzer.calculate_critical_path)

    assert result == ["B"]


def test_two_starts_of_equal_length_stay_critical():
    analyzer_cls = load_sequence_analyzer()
    analyzer = analyzer_cls(element_frame(["A", "B"]))

    result = call_bounded(analyzer.calculate_critical_path)

    assert result == ["A", "B"]


def _cyclic_compressor():
    compressor_cls = load_schedule_compressor()
    compressor = compressor_cls()
    compressor.add_activity(
        "A",
        "Element A",
        normal_duration=10,
        crash_duration=6,
        normal_cost=1000.0,
        crash_cost=1600.0,
        predecessors=["B"],
        is_critical=True,
    )
    compressor.add_activity(
        "B",
        "Element B",
        normal_duration=10,
        crash_duration=6,
        normal_cost=1000.0,
        crash_cost=1600.0,
        predecessors=["A"],
        is_critical=True,
    )
    return compressor


def _assert_compressor_cycle(operation: Callable, compressor) -> None:
    before = {
        activity_id: activity.current_duration
        for activity_id, activity in compressor.activities.items()
    }
    result = assert_cycle_does_not_crash(operation)
    if result is None:
        after = {
            activity_id: activity.current_duration
            for activity_id, activity in compressor.activities.items()
        }
        assert after == before


def test_calculate_project_duration_cycle_does_not_crash():
    compressor = _cyclic_compressor()
    _assert_compressor_cycle(compressor.calculate_project_duration, compressor)


def test_identify_critical_path_cycle_does_not_crash():
    compressor = _cyclic_compressor()
    _assert_compressor_cycle(compressor.identify_critical_path, compressor)


def test_crash_schedule_cycle_does_not_crash_or_mutate_first():
    compressor = _cyclic_compressor()
    _assert_compressor_cycle(lambda: compressor.crash_schedule(1), compressor)


def test_fast_track_schedule_cycle_does_not_crash():
    compressor = _cyclic_compressor()
    _assert_compressor_cycle(
        lambda: compressor.fast_track_schedule(1), compressor
    )


def test_optimize_compression_cycle_does_not_crash():
    compressor = _cyclic_compressor()
    _assert_compressor_cycle(
        lambda: compressor.optimize_compression(1, max_budget=10000.0),
        compressor,
    )


def test_generate_cost_curve_cycle_does_not_crash():
    compressor = _cyclic_compressor()
    _assert_compressor_cycle(lambda: compressor.generate_cost_curve(1), compressor)


def test_self_cycle_duration_does_not_crash():
    compressor_cls = load_schedule_compressor()
    compressor = compressor_cls()
    compressor.add_activity(
        "A", "Element A", 5, 3, 100.0, 150.0, predecessors=["A"]
    )
    assert_cycle_does_not_crash(compressor.calculate_project_duration)


def test_acyclic_compression_duration_is_sum_of_chain():
    compressor_cls = load_schedule_compressor()
    compressor = compressor_cls()
    compressor.add_activity("A", "Element A", 5, 3, 100.0, 150.0, predecessors=[])
    compressor.add_activity(
        "B", "Element B", 5, 3, 100.0, 150.0, predecessors=["A"]
    )

    duration = call_bounded(compressor.calculate_project_duration)

    assert duration == 10


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-cycle":
        sys.exit(_run_published_cycle(sys.argv[2]))
