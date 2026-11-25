"""ScheduleElementLinker must treat BIM level names and mapped activity
names as literal text.

The implementation under test is the ScheduleElementLinker class in
2_DDC_Book/3.3-4D-BIM-CO2-Simulation/4d-simulation/SKILL.md. These tests
execute that class. They fail while level names and mapping values are
compiled as regular expressions, and they pass once those strings are
matched literally (for example str.contains(..., regex=False, na=False)
or re.escape).

bim-to-schedule-4d compares levels with equality and is not covered here.
"""

from __future__ import annotations

import multiprocessing
import re
from pathlib import Path

import pandas as pd
import pytest

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "2_DDC_Book"
    / "3.3-4D-BIM-CO2-Simulation"
    / "4d-simulation"
    / "SKILL.md"
)

# A normal activity name is long enough for nested quantifiers such as
# (.*.*)*# to backtrack for far longer than MATCH_TIMEOUT_S.
MATCH_TIMEOUT_S = 3


def _linker_source() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    for block in blocks:
        if "class ScheduleElementLinker" in block and "def auto_link_by_level" in block:
            return block
    raise AssertionError(f"ScheduleElementLinker was not found in {SKILL_PATH}")


def _worker(source: str, call: str, payload: dict, queue) -> None:
    namespace: dict = {}
    exec(source, namespace, namespace)  # noqa: S102 — the skill source is the code under test
    linker = namespace["ScheduleElementLinker"](
        pd.DataFrame(payload["elements"]),
        pd.DataFrame(payload["schedule"]),
    )
    try:
        if call == "level":
            links = linker.auto_link_by_level()
        elif call == "category":
            links = linker.auto_link_by_category(payload["mapping"])
        else:
            raise AssertionError(call)
        queue.put({"status": "ok", "links": links.to_dict(orient="records")})
    except Exception as exc:
        queue.put(
            {
                "status": "error",
                "exc_type": type(exc).__name__,
                "message": str(exc),
            }
        )


def _link(call: str, elements: list[dict], schedule: list[dict], mapping=None):
    """Run one linker method in a child process so a backtracking match can be stopped."""
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(
        target=_worker,
        args=(
            _linker_source(),
            call,
            {"elements": elements, "schedule": schedule, "mapping": mapping},
            queue,
        ),
    )
    process.start()
    process.join(MATCH_TIMEOUT_S)
    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        pytest.fail(
            f"{call} did not finish within {MATCH_TIMEOUT_S}s. "
            "The name was compiled as a regular expression and the match backtracked."
        )
    if queue.empty():
        pytest.fail(f"{call} ended without a result (exit code {process.exitcode})")
    result = queue.get()
    if result["status"] == "error":
        pytest.fail(f"{call} raised {result['exc_type']}: {result['message']}")
    return result["links"]


def _by_element(links: list[dict]) -> dict:
    return {row["ElementId"]: row for row in links}


def test_parenthesized_level_links_only_the_literal_activity():
    """A level named (a) must not match every activity that contains the letter a."""
    links = _link(
        "level",
        elements=[
            {
                "ElementId": "E9",
                "Category": "Wall",
                "Level": "(a)",
            }
        ],
        schedule=[
            {
                "ActivityId": "A100",
                "Activity": "Foundation excavation and concrete pour",
                "Start_Date": "2024-01-01",
                "End_Date": "2024-01-31",
            },
            {
                "ActivityId": "A200",
                "Activity": "Install (a) anchors on the podium",
                "Start_Date": "2024-03-01",
                "End_Date": "2024-03-15",
            },
        ],
    )
    linked = _by_element(links)
    assert set(linked) == {"E9"}
    assert linked["E9"]["ActivityId"] == "A200"
    assert linked["E9"]["Activity"] == "Install (a) anchors on the podium"
    assert linked["E9"]["Start_Date"] == "2024-03-01"
    assert linked["E9"]["End_Date"] == "2024-03-15"


def test_dot_level_does_not_take_the_first_activity():
    """A level named '.' matches any character as a regex and would bind the first activity."""
    links = _link(
        "level",
        elements=[
            {"ElementId": "E1", "Category": "Wall", "Level": "."},
            {"ElementId": "E2", "Category": "Slab", "Level": "level 2"},
        ],
        schedule=[
            {
                "ActivityId": "A1",
                "Activity": "Foundation excavation and concrete pour",
                "Start_Date": "2024-01-01",
                "End_Date": "2024-02-01",
            },
            {
                "ActivityId": "A2",
                "Activity": "Level 2 slab formwork",
                "Start_Date": "2024-04-01",
                "End_Date": "2024-04-30",
            },
        ],
    )
    linked = _by_element(links)
    assert "E1" not in linked
    assert linked["E2"]["ActivityId"] == "A2"
    assert linked["E2"]["Start_Date"] == "2024-04-01"
    assert linked["E2"]["End_Date"] == "2024-04-30"


def test_invalid_level_pattern_does_not_abort_linking():
    """An invalid regular expression in a level name must not raise, and a real level still links."""
    links = _link(
        "level",
        elements=[
            {"ElementId": "E2", "Category": "Slab", "Level": "Level 2"},
            {"ElementId": "E3", "Category": "Wall", "Level": "["},
        ],
        schedule=[
            {
                "ActivityId": "A1",
                "Activity": "Foundation excavation and concrete pour",
                "Start_Date": "2024-01-01",
                "End_Date": "2024-02-01",
            },
            {
                "ActivityId": "A2",
                "Activity": "Level 2 slab formwork",
                "Start_Date": "2024-04-01",
                "End_Date": "2024-04-30",
            },
        ],
    )
    linked = _by_element(links)
    assert "E3" not in linked
    assert linked["E2"]["ActivityId"] == "A2"
    assert linked["E2"]["Start_Date"] == "2024-04-01"
    assert linked["E2"]["End_Date"] == "2024-04-30"


def test_nested_quantifier_level_finishes_without_linking():
    """(.*.*)*# against ordinary activity names must return, and must not link those activities."""
    activities = [
        "Foundation excavation and concrete pour for the north core wall",
        "Level 1 structural steel erection and decking installation",
        "Level 2 slab formwork reinforcement and concrete placement",
    ]
    links = _link(
        "level",
        elements=[
            {
                "ElementId": "E-redos",
                "Category": "Wall",
                "Level": "(.*.*)*#",
            }
        ],
        schedule=[
            {
                "ActivityId": f"A{i}",
                "Activity": name,
                "Start_Date": "2024-01-01",
                "End_Date": "2024-02-01",
            }
            for i, name in enumerate(activities, start=1)
        ],
    )
    assert links == []


def test_category_mapping_value_is_a_literal_activity_name():
    """auto_link_by_category passes the mapping value to the same unescaped contains call."""
    links = _link(
        "category",
        elements=[
            {"ElementId": "W1", "Category": "Wall", "Level": "Level 1"},
            {"ElementId": "S1", "Category": "Slab", "Level": "Level 1"},
        ],
        schedule=[
            {
                "ActivityId": "A1",
                "Activity": "Foundation excavation",
                "Start_Date": "2024-01-01",
                "End_Date": "2024-01-31",
            },
            {
                "ActivityId": "A2",
                "Activity": "Floor Construction",
                "Start_Date": "2024-02-01",
                "End_Date": "2024-02-28",
            },
        ],
        mapping={"Wall": ".", "Slab": "Floor Construction"},
    )
    linked = _by_element(links)
    assert "W1" not in linked
    assert linked["S1"]["ActivityId"] == "A2"
    assert linked["S1"]["Start_Date"] == "2024-02-01"
    assert linked["S1"]["End_Date"] == "2024-02-28"
