"""Spec tests for self-linked activities in the insights critical path analyzer.

The tests load the Python implementation shipped in
3_DDC_Insights/Schedule-Optimization/critical-path-analyzer/SKILL.md and run it.
They fail while a schedule can make critical-path sequencing loop, and they pass
once that input is rejected.

Rejection must happen before a successful analysis is returned. If a self-link
is stored and then the call raises, sequencing is still required to reject that
graph instead of walking it.

Not covered here, because they do not share this successor walk:
- generate_gantt_data, which only reads stored dates
- 1_DDC_Toolkit/Schedule-Management/critical-path-analyzer (topological sort
  marks activities visited before following predecessors)
- schedule-compression and 4d-simulation (different predecessor recursion)
"""

import multiprocessing as mp
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = (
    REPO
    / "3_DDC_Insights"
    / "Schedule-Optimization"
    / "critical-path-analyzer"
    / "SKILL.md"
)
TIMEOUT_SECONDS = 2

# Words a fix must use so a hang-turned-crash (KeyError, TypeError, ...)
# does not count as rejecting the cycle.
REJECTION_MARKERS = (
    "cycle",
    "circular",
    "self",
    "predecessor",
    "loop",
    "invalid",
    "already",
)


def load_analyzer_class():
    """Execute the skill's implementation block and return CriticalPathAnalyzer."""
    text = SKILL.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    namespace = {"__file__": str(SKILL)}
    exec(compile(text[start:end], str(SKILL), "exec"), namespace)
    return namespace["CriticalPathAnalyzer"]


def _self_link_stored(analyzer) -> bool:
    for activity in analyzer.activities.values():
        if activity.id in activity.predecessors or activity.id in activity.successors:
            return True
    return False


def _load_self_linked(analyzer, predecessors):
    analyzer.import_schedule(
        [
            {"id": "A", "name": "Start", "duration": 5, "predecessors": []},
            {
                "id": "B",
                "name": "Self linked",
                "duration": 3,
                "predecessors": predecessors,
            },
        ]
    )


class AnalysisReturned(Exception):
    """Critical-path calculation finished and handed back a result."""


def _require_rejection_if_link_remains(analyzer):
    """A refusal that already stored the self-link must not leave a walkable graph."""
    if not _self_link_stored(analyzer):
        return
    analyzer.calculate_critical_path()
    raise AnalysisReturned(
        "cycle remained linked and critical path calculation returned a result"
    )


def _exercise(loader, follow):
    analyzer_cls = load_analyzer_class()
    analyzer = analyzer_cls()
    try:
        loader(analyzer)
    except Exception:
        _require_rejection_if_link_remains(analyzer)
        raise
    follow(analyzer)


def _child(case, connection):
    try:
        if case == "import_self_second":
            _exercise(
                lambda analyzer: _load_self_linked(analyzer, ["A", "B"]),
                lambda analyzer: analyzer.calculate_critical_path(),
            )
        elif case == "import_self_first":
            _exercise(
                lambda analyzer: _load_self_linked(analyzer, ["B", "A"]),
                lambda analyzer: analyzer.calculate_critical_path(),
            )
        elif case == "add_activity":
            def load_added(analyzer):
                analyzer.add_activity("A", "Start", 5, predecessors=[])
                analyzer.add_activity(
                    "B", "Self linked", 3, predecessors=["A", "B"]
                )

            _exercise(load_added, lambda analyzer: analyzer.calculate_critical_path())
        elif case == "simulate_delay":
            _exercise(
                lambda analyzer: _load_self_linked(analyzer, ["A", "B"]),
                lambda analyzer: analyzer.simulate_delay("A", 2),
            )
        elif case == "float_report":
            _exercise(
                lambda analyzer: _load_self_linked(analyzer, ["A", "B"]),
                lambda analyzer: analyzer.get_float_report(),
            )
        elif case == "compression":
            _exercise(
                lambda analyzer: _load_self_linked(analyzer, ["A", "B"]),
                lambda analyzer: analyzer.find_compression_opportunities(1),
            )
        elif case == "successor_cycle_walk":
            # A non-self successor cycle cannot be built by one import pass.
            # The sequence walk is still required to stop when the next id is
            # already on the path, which is the same loop the self-link reaches.
            def load_cycle(analyzer):
                analyzer.add_activity("A", "Start", 5)
                analyzer.add_activity("B", "Next", 3, predecessors=["A"])
                analyzer.activities["B"].successors.append("A")

            def walk(analyzer):
                analyzer._build_critical_path_sequence(
                    [analyzer.activities["A"], analyzer.activities["B"]]
                )

            _exercise(load_cycle, walk)
        elif case == "benign":
            analyzer = load_analyzer_class()()
            analyzer.import_schedule(
                [
                    {"id": "A", "name": "Start", "duration": 5, "predecessors": []},
                    {
                        "id": "B",
                        "name": "Next",
                        "duration": 3,
                        "predecessors": ["A"],
                    },
                ]
            )
            analysis = analyzer.calculate_critical_path()
            connection.send(
                ("ok", list(analysis.critical_path), analysis.project_duration)
            )
            return
        else:
            raise RuntimeError(f"unknown case {case}")
        connection.send(("returned",))
    except AnalysisReturned as exc:
        connection.send(("returned", str(exc)))
    except Exception as exc:
        connection.send(("error", type(exc).__name__, str(exc)))


def run_case(case):
    ctx = mp.get_context("fork")
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_child, args=(case, child))
    process.start()
    child.close()
    process.join(TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)
        raise AssertionError(
            f"{case} did not finish within {TIMEOUT_SECONDS}s"
        )
    if not parent.poll():
        raise AssertionError(
            f"{case} ended without a result (exit code {process.exitcode})"
        )
    return parent.recv()


class CriticalPathSelfLinkTests(unittest.TestCase):
    def _assert_rejected(self, case):
        status = run_case(case)
        self.assertEqual(
            status[0],
            "error",
            f"{case} completed the cyclic schedule instead of rejecting it: {status}",
        )
        message = status[2].lower()
        self.assertTrue(
            any(marker in message for marker in REJECTION_MARKERS),
            f"{case} raised {status[1]} without identifying an invalid cycle: {status[2]!r}",
        )

    def test_import_self_predecessor_after_start_is_rejected(self):
        self._assert_rejected("import_self_second")

    def test_import_self_predecessor_before_other_predecessor_is_rejected(self):
        self._assert_rejected("import_self_first")

    def test_add_activity_self_predecessor_is_rejected(self):
        self._assert_rejected("add_activity")

    def test_simulate_delay_rejects_self_linked_schedule(self):
        self._assert_rejected("simulate_delay")

    def test_float_report_rejects_self_linked_schedule(self):
        self._assert_rejected("float_report")

    def test_compression_opportunities_reject_self_linked_schedule(self):
        self._assert_rejected("compression")

    def test_sequence_walk_rejects_successor_cycle_already_on_path(self):
        self._assert_rejected("successor_cycle_walk")

    def test_acyclic_schedule_still_returns_critical_path(self):
        status = run_case("benign")
        self.assertEqual(status[0], "ok", status)
        self.assertEqual(status[1], ["A", "B"])
        self.assertEqual(status[2], 8)


if __name__ == "__main__":
    unittest.main()
