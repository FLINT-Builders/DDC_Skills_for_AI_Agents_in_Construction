"""HTML encoding for the KPI dashboard skill.

The implementation under test is the first Python block in
2_DDC_Book/4.1-Analytics-KPI-Dashboard/kpi-dashboard/SKILL.md.
generate_html_dashboard inserts the project name (document title and h1),
each recorded KPI name, and each KPI unit into the page as raw markup.

These tests fail while that markup is unencoded: headless Chrome executes it
and the page text shows the execution markers. They pass once those three
values are HTML-encoded before insertion. A custom KPI name is rendered only
after record_value stores a current value, so the name and unit cases record
one.

Not covered here, because they are not this HTML sink:
- export_to_excel writes names into a workbook
- get_dashboard_summary and get_kpi_details return plain dictionaries
- KPI target is interpolated, but a non-numeric target raises TypeError in
  record_value before any dashboard HTML is returned
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "2_DDC_Book/4.1-Analytics-KPI-Dashboard/kpi-dashboard/SKILL.md"

# Runtime result of 1+1. The payload source computes it and does not contain
# this attribute literal, so it shows up in the DOM only when the browser
# runs the markup.
EXECUTED = 'data-pwned="2"'
READ_KPI = 'data-read="saw-kpi"'
FIELD_MARKER = "KPI_FIELD_MARKER"
LABOR_VALUE = "92.00"

SCRIPT = (
    "<script>document.addEventListener('DOMContentLoaded',function(){"
    "document.documentElement.setAttribute('data-pwned', String(1+1));"
    "document.documentElement.setAttribute('data-read',"
    "document.body.textContent.indexOf('92.00')>=0?'saw-kpi':'miss');"
    "});</script>"
)
TITLE_BREAKOUT = (
    "</title><script>document.documentElement.setAttribute('data-pwned', String(1+1))"
    "</script><title>"
)
UNIT_MARKUP = (
    "<img src=x onerror=\"document.documentElement.setAttribute('data-pwned', String(1+1))\">"
)


def _chrome_bin() -> str:
    for name in ("google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    raise AssertionError("google-chrome is required to render the dashboard")


def _load_skill():
    text = SKILL.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"no Python implementation block in {SKILL}")
    namespace: dict = {}
    exec(compile(match.group(1), str(SKILL), "exec"), namespace)
    return namespace


def _dump_dom(html: str) -> str:
    """Open generated dashboard markup in headless Chrome and return the DOM."""
    with tempfile.TemporaryDirectory(prefix="kpi-dashboard-") as tmp:
        root = Path(tmp)
        page = root / "dashboard.html"
        page.write_text(html, encoding="utf-8")
        completed = subprocess.run(
            [
                "timeout",
                "--signal=KILL",
                "12",
                _chrome_bin(),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--no-first-run",
                "--disable-extensions",
                "--virtual-time-budget=2000",
                f"--user-data-dir={root / 'profile'}",
                "--dump-dom",
                str(page),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
    dom = completed.stdout
    if "<html" not in dom.lower():
        raise AssertionError(
            f"Chrome did not render the dashboard (rc={completed.returncode}, "
            f"output length={len(dom)})"
        )
    return dom


class KpiDashboardHtmlEncodingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = _load_skill()
        cls.dashboard_type = namespace["KPIDashboard"]
        cls.definition = namespace["KPIDefinition"]
        cls.category = namespace["KPICategory"]

    def _dashboard(self, project_name: str):
        dashboard = self.dashboard_type(project_name)
        dashboard.record_value("LABOR_PROD", 92)
        return dashboard

    def _assert_inert(self, dom: str, value_text: str):
        self.assertNotIn(EXECUTED, dom)
        self.assertNotIn(READ_KPI, dom)
        self.assertIn(FIELD_MARKER, dom)
        self.assertIn(value_text, dom)
        self.assertNotIn("<script", dom.lower())
        self.assertNotIn("<img", dom.lower())

    def test_project_name_script_is_not_executed(self):
        dashboard = self._dashboard(f"{SCRIPT}{FIELD_MARKER}")
        dom = _dump_dom(dashboard.generate_html_dashboard())
        self._assert_inert(dom, LABOR_VALUE)

    def test_project_name_title_breakout_is_not_executed(self):
        dashboard = self._dashboard(f"{TITLE_BREAKOUT}{FIELD_MARKER}")
        dom = _dump_dom(dashboard.generate_html_dashboard())
        self._assert_inert(dom, LABOR_VALUE)

    def test_custom_kpi_name_is_not_executed(self):
        dashboard = self._dashboard("Office Building A")
        dashboard.add_custom_kpi(
            self.definition(
                "WASTE_DIVERSION",
                f"{SCRIPT}{FIELD_MARKER}",
                self.category.SUSTAINABILITY,
                "%",
                75,
                60,
                50,
                True,
            )
        )
        dashboard.record_value("WASTE_DIVERSION", 70)
        dom = _dump_dom(dashboard.generate_html_dashboard())
        self._assert_inert(dom, LABOR_VALUE)
        self.assertIn("70.00", dom)

    def test_custom_kpi_unit_is_not_executed(self):
        dashboard = self._dashboard("Office Building A")
        dashboard.add_custom_kpi(
            self.definition(
                "WASTE_DIVERSION",
                "Waste Diversion Rate",
                self.category.SUSTAINABILITY,
                f"{UNIT_MARKUP}{FIELD_MARKER}",
                75,
                60,
                50,
                True,
            )
        )
        dashboard.record_value("WASTE_DIVERSION", 1.5)
        dom = _dump_dom(dashboard.generate_html_dashboard())
        self._assert_inert(dom, "1.50")


if __name__ == "__main__":
    unittest.main()
