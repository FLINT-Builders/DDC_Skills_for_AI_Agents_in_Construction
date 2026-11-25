"""HTML encoding requirements for the Gantt chart skill.

The generator in
``2_DDC_Book/3.3-4D-BIM-CO2-Simulation/gantt-chart/SKILL.md`` imports schedule
rows and writes an HTML page. Task names and the project name are attacker-
controlled text (a schedule file, a data frame, or the project title taken
from that schedule). These tests open that page and require the text to be
HTML-encoded so the browser does not run it.

They fail while the names are concatenated into the page raw. They pass once
``generate_html_gantt`` encodes the project name and every task name before
inserting them.
"""

from __future__ import annotations

import html
import os
import re
import signal
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "2_DDC_Book"
    / "3.3-4D-BIM-CO2-Simulation"
    / "gantt-chart"
    / "SKILL.md"
)

# Created only by script execution. The payload source uses setAttribute
# quotes, so this exact attribute is absent from the page until the browser
# runs the task name.
EXECUTED_ATTR = 'data-xss="executed"'
SCRIPT_NAME = (
    '<script>document.documentElement.setAttribute("data-xss","executed")</script>'
)
IMAGE_NAME = (
    "<img src=x onerror=\"document.documentElement.setAttribute('data-xss','executed')\">"
)
TITLE_BREAKOUT = (
    '</title><script>document.documentElement.setAttribute("data-xss","executed")</script>'
)


def _load_skill():
    source = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", source, re.DOTALL)
    for block in blocks:
        if "class GanttChartGenerator" in block and "def generate_html_gantt" in block:
            namespace: dict = {}
            exec(block, namespace)
            return namespace["GanttChartGenerator"], namespace["Task"]
    raise AssertionError(f"GanttChartGenerator implementation not found in {SKILL_PATH}")


GanttChartGenerator, Task = _load_skill()


def _schedule_frame(name: str, *, milestone: bool = False) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "task_id": "T-EXT",
                "name": name,
                "start_date": "2024-06-01",
                "end_date": "2024-06-01" if milestone else "2024-06-10",
                "wbs_code": "01",
                "progress": 0,
                "level": 0,
                "is_milestone": milestone,
                "is_summary": False,
                "parent_id": "",
            }
        ]
    )


def _page_from_frame(frame: pd.DataFrame, project_name: str = "Office Building A") -> str:
    chart = GanttChartGenerator(project_name)
    chart.import_from_df(frame)
    return chart.generate_html_gantt()


def _rendered_dom(page: str) -> str:
    """Open the generated page in Chrome and return the DOM after scripts run."""
    chrome = "google-chrome"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        html_path = root / "gantt.html"
        html_path.write_text(page, encoding="utf-8")
        dom_path = root / "dom.html"
        with dom_path.open("w", encoding="utf-8") as dom_file:
            proc = subprocess.Popen(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    f"--user-data-dir={root / 'profile'}",
                    "--virtual-time-budget=1000",
                    "--dump-dom",
                    html_path.as_uri(),
                ],
                stdout=dom_file,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        return dom_path.read_text(encoding="utf-8", errors="replace")


def _assert_encoded_text(page: str, raw: str) -> None:
    encoded = html.escape(raw, quote=True)
    assert raw not in page
    assert encoded in page


def _assert_script_did_not_run(page: str, raw: str) -> None:
    dom = _rendered_dom(page)
    assert "2024-06-01" in dom
    assert EXECUTED_ATTR not in dom
    _assert_encoded_text(page, raw)


def test_spreadsheet_task_name_does_not_run_in_generated_page(tmp_path: Path):
    """A task name from an .xlsx schedule is imported and written into the page."""
    sheet = tmp_path / "schedule.xlsx"
    frame = _schedule_frame(SCRIPT_NAME)
    frame.to_excel(sheet, index=False)

    imported = pd.read_excel(sheet)
    page = _page_from_frame(imported)

    assert imported.loc[0, "name"] == SCRIPT_NAME
    _assert_script_did_not_run(page, SCRIPT_NAME)


def test_csv_task_name_does_not_run_in_generated_page(tmp_path: Path):
    sheet = tmp_path / "schedule.csv"
    _schedule_frame(SCRIPT_NAME).to_csv(sheet, index=False)

    imported = pd.read_csv(sheet)
    page = _page_from_frame(imported)

    assert imported.loc[0, "name"] == SCRIPT_NAME
    _assert_script_did_not_run(page, SCRIPT_NAME)


def test_dataframe_task_name_imported_by_import_from_df_is_encoded():
    page = _page_from_frame(_schedule_frame(SCRIPT_NAME))
    _assert_script_did_not_run(page, SCRIPT_NAME)


def test_milestone_task_name_from_dataframe_is_encoded():
    page = _page_from_frame(_schedule_frame(SCRIPT_NAME, milestone=True))
    _assert_script_did_not_run(page, SCRIPT_NAME)
    assert "milestone" in page


def test_image_event_handler_in_task_name_does_not_run():
    page = _page_from_frame(_schedule_frame(IMAGE_NAME))
    _assert_script_did_not_run(page, IMAGE_NAME)


def test_task_name_passed_to_add_task_is_encoded():
    chart = GanttChartGenerator("Office Building A")
    chart.add_task(
        Task(
            "T-EXT",
            SCRIPT_NAME,
            date(2024, 6, 1),
            date(2024, 6, 10),
        )
    )
    page = chart.generate_html_gantt()
    _assert_script_did_not_run(page, SCRIPT_NAME)


def test_project_name_is_encoded_in_title_and_heading():
    chart = GanttChartGenerator(TITLE_BREAKOUT)
    chart.add_task(
        Task("T1", "Foundation", date(2024, 6, 1), date(2024, 6, 10))
    )
    page = chart.generate_html_gantt()
    dom = _rendered_dom(page)

    assert "2024-06-01" in dom
    assert EXECUTED_ATTR not in dom
    encoded = html.escape(TITLE_BREAKOUT, quote=True)
    assert TITLE_BREAKOUT not in page
    # The title element and the h2 both receive the project name.
    assert page.count(encoded) >= 2


def test_markup_punctuation_in_task_name_is_encoded_and_other_task_remains():
    raw = 'North & South <wing> "A"'
    frame = pd.DataFrame(
        [
            {
                "task_id": "T1",
                "name": raw,
                "start_date": "2024-06-01",
                "end_date": "2024-06-05",
                "is_milestone": False,
            },
            {
                "task_id": "T2",
                "name": "Foundation",
                "start_date": "2024-06-06",
                "end_date": "2024-06-12",
                "is_milestone": False,
            },
        ]
    )
    page = _page_from_frame(frame)
    _assert_encoded_text(page, raw)
    assert "Foundation" in page


def test_safe_task_name_is_still_visible():
    page = _page_from_frame(_schedule_frame("Foundation"))
    assert "Foundation" in page
    assert "<h2>Office Building A</h2>" in page
