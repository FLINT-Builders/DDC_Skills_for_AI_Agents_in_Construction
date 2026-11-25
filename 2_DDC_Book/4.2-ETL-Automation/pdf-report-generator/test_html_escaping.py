"""HTML escaping spec for the construction report generator.

These tests load the implementation in SKILL.md and run the report methods
that write caller-supplied strings into an HTML page. They fail while those
strings are inserted as raw markup, and they pass once every inserted string
is encoded so the same characters come back as text.

The chart section, the image section, the KPI unit field, and the Excel
export are not rendered by to_html, so they are not part of this spec.
SPI, CPI, TRIR, budget, and actual cost are formatted with numeric format
specs and never reach the page when the caller supplies text.
"""

from __future__ import annotations

import re
import sys
import types
from html.parser import HTMLParser
from pathlib import Path

import pytest

SKILL_PATH = Path(__file__).with_name("SKILL.md")

# Characters that must round-trip as data: an ampersand entity, a tag,
# and a quote. Used in element text (paragraphs, headings, cells, titles).
TEXT_PAYLOAD = 'a&lt;b <script>alert(1)</script> "q"'

# Breaks out of the class attribute on a KPI card, then inserts a handler.
ATTR_PAYLOAD = '"><img src=x onerror=alert(1)>'

# Closes the document <title> and then inserts a script element.
TITLE_PAYLOAD = '</title><script>alert(1)</script>'

ALLOWED_TAGS = {
    "html",
    "head",
    "title",
    "style",
    "body",
    "div",
    "h1",
    "h2",
    "p",
    "table",
    "tr",
    "th",
    "td",
}


class _MarkupCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag.lower())
        self.attrs.append((tag.lower(), list(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _load_skill_namespace() -> dict:
    """Execute the implementation fence in SKILL.md, unchanged.

    The block is registered as a module so dataclasses can resolve the class
    namespace. Nothing in the block is rewritten.
    """
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"no python implementation block in {SKILL_PATH}")
    module = types.ModuleType("pdf_report_generator_skill")
    module.__file__ = str(SKILL_PATH)
    sys.modules[module.__name__] = module
    exec(compile(match.group(1), str(SKILL_PATH), "exec"), module.__dict__)
    return module.__dict__


@pytest.fixture(scope="module")
def skill():
    return _load_skill_namespace()


def _generator(skill: dict, project_name: str, report_type: str):
    return skill["PDFReportGenerator"](project_name, skill["ReportType"][report_type])


def assert_payload_is_data(html: str, payload: str, source: str) -> None:
    """The payload must survive as text and must not become markup or a handler."""
    collector = _MarkupCollector()
    collector.feed(html)
    collector.close()

    unexpected = [tag for tag in collector.tags if tag not in ALLOWED_TAGS]
    handlers: list[str] = []
    attr_values: list[str] = []
    for tag, attrs in collector.attrs:
        for name, value in attrs:
            attr_values.append(value or "")
            lowered = name.lower()
            if lowered.startswith("on"):
                handlers.append(f"{tag} {name}")
            if value and value.strip().lower().startswith("javascript:"):
                handlers.append(f"{tag} {name}={value}")

    preserved = payload in "\n".join(collector.text) or payload in "\n".join(attr_values)
    problems: list[str] = []
    if unexpected or handlers:
        problems.append(
            f"{source}: report data was parsed as HTML "
            f"(tags={unexpected}, handlers={handlers})"
        )
    if not preserved:
        problems.append(f"{source}: report data was not preserved as text ({payload!r})")
    if problems:
        raise AssertionError("; ".join(problems))


def test_progress_report_text_and_activity_table_are_data(skill) -> None:
    report = _generator(skill, "Office Building A", "PROGRESS")
    summary = f"summary {TEXT_PAYLOAD}"
    issue = f"issue {TEXT_PAYLOAD}"
    activity = f"activity {TEXT_PAYLOAD}"
    column = f"column {TEXT_PAYLOAD}"
    report.generate_progress_report(
        {
            "overall_progress": 10,
            "planned_progress": 10,
            "spi": 1.0,
            "cpi": 1.0,
            "days_remaining": 5,
            "summary": summary,
            "activities": [{column: activity, "Status": "Open"}],
            "issues": [issue],
        }
    )

    html = report.to_html()
    assert_payload_is_data(html, summary, "progress summary")
    assert_payload_is_data(html, issue, "progress issue")
    assert_payload_is_data(html, activity, "progress activity cell")
    assert_payload_is_data(html, column, "progress activity column")


def test_progress_and_safety_kpi_values_from_text_fields_are_data(skill) -> None:
    progress_value = f"progress {TEXT_PAYLOAD}"
    days_value = f"days {TEXT_PAYLOAD}"
    progress = _generator(skill, "Office Building A", "PROGRESS")
    progress.generate_progress_report(
        {
            "overall_progress": progress_value,
            # The status comparison accepts text when both sides are text.
            # A numeric planned value raises before the page is built.
            "planned_progress": "",
            "spi": 1.0,
            "cpi": 1.0,
            "days_remaining": days_value,
            "summary": "ok",
        }
    )
    progress_html = progress.to_html()
    assert_payload_is_data(progress_html, progress_value, "overall progress KPI")
    assert_payload_is_data(progress_html, days_value, "days remaining KPI")

    incident_days = f"incident-days {TEXT_PAYLOAD}"
    near_misses = f"near {TEXT_PAYLOAD}"
    observations = f"observations {TEXT_PAYLOAD}"
    safety = _generator(skill, "Office Building A", "SAFETY")
    safety.generate_safety_report(
        {
            "period": "Current",
            "days_without_incident": incident_days,
            "trir": 0.0,
            "near_misses": near_misses,
            "observations": observations,
        }
    )
    safety_html = safety.to_html()
    assert_payload_is_data(safety_html, incident_days, "days without incident KPI")
    assert_payload_is_data(safety_html, near_misses, "near misses KPI")
    assert_payload_is_data(safety_html, observations, "observations KPI")


def test_safety_report_incident_table_and_training_are_data(skill) -> None:
    training = f"training {TEXT_PAYLOAD}"
    incident = f"incident {TEXT_PAYLOAD}"
    column = f"column {TEXT_PAYLOAD}"
    report = _generator(skill, "Office Building A", "SAFETY")
    report.generate_safety_report(
        {
            "period": "Current",
            "days_without_incident": 1,
            "trir": 0.0,
            "near_misses": 0,
            "observations": 0,
            "incidents": [{column: incident}],
            "training": training,
        }
    )

    html = report.to_html()
    assert_payload_is_data(html, training, "safety training")
    assert_payload_is_data(html, incident, "safety incident cell")
    assert_payload_is_data(html, column, "safety incident column")


def test_cost_report_period_and_breakdown_are_data(skill) -> None:
    period = f"period {TEXT_PAYLOAD}"
    category = f"category {TEXT_PAYLOAD}"
    column = f"column {TEXT_PAYLOAD}"
    report = _generator(skill, "Office Building A", "COST")
    report.generate_cost_report(
        {
            "period": period,
            "budget": 1000,
            "actual_cost": 800,
            "cpi": 1.0,
            "cost_breakdown": [{column: category}],
        }
    )

    html = report.to_html()
    assert_payload_is_data(html, period, "cost period")
    assert_payload_is_data(html, category, "cost breakdown cell")
    assert_payload_is_data(html, column, "cost breakdown column")


def test_project_name_in_the_document_title_is_data(skill) -> None:
    report = _generator(skill, TITLE_PAYLOAD, "WEEKLY")
    html = report.to_html()
    assert_payload_is_data(html, TITLE_PAYLOAD, "document title")


def test_direct_sections_encode_every_inserted_string(skill) -> None:
    report = _generator(skill, "Office Building A", "WEEKLY")
    header_title = f"header {TEXT_PAYLOAD}"
    subtitle = f"subtitle {TEXT_PAYLOAD}"
    text_title = f"text-title {TEXT_PAYLOAD}"
    narrative = f"narrative {TEXT_PAYLOAD}"
    table_title = f"table-title {TEXT_PAYLOAD}"
    column = f"column {TEXT_PAYLOAD}"
    cell = f"cell {TEXT_PAYLOAD}"
    kpi_title = f"kpi-title {TEXT_PAYLOAD}"
    kpi_name = f"kpi-name {TEXT_PAYLOAD}"
    kpi_value = f"kpi-value {TEXT_PAYLOAD}"
    kpi_target = f"kpi-target {TEXT_PAYLOAD}"

    report.add_header(header_title, subtitle)
    report.add_text(text_title, narrative)
    frame = skill_dataframe(skill, [{column: cell}])
    report.add_table(table_title, frame)
    report.add_kpi_cards(
        kpi_title,
        [
            skill["KPICard"](
                kpi_name,
                kpi_value,
                target=kpi_target,
                status=ATTR_PAYLOAD,
            )
        ],
    )

    html = report.to_html()
    for payload, source in (
        (header_title, "header title"),
        (subtitle, "header subtitle"),
        (text_title, "text section title"),
        (narrative, "text section body"),
        (table_title, "table title"),
        (column, "table column"),
        (cell, "table cell"),
        (kpi_title, "kpi section title"),
        (kpi_name, "kpi name"),
        (kpi_value, "kpi value"),
        (kpi_target, "kpi target"),
        (ATTR_PAYLOAD, "kpi status attribute"),
    ):
        assert_payload_is_data(html, payload, source)


def skill_dataframe(skill: dict, rows: list[dict]):
    pandas = __import__("pandas")
    return pandas.DataFrame(rows)
