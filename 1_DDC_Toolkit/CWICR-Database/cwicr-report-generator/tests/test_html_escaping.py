"""HTML cost reports must encode text that a bidder can put in the cost table.

CWICRReportGenerator and QuickReport.from_dataframe copy the project name,
currency, category, and line-item code, description, and unit into the page
that save_html writes. These tests fail while that text is inserted raw.
They pass when each of those values is HTML-encoded and the report template
and the original characters are still there.

Descriptions are inserted only up to 50 characters (add_line_items). The
encoded page must show that prefix as text.

Not covered here, because they are not HTML text sinks on this path:
add_summary (amounts are formatted as numbers), quantity, unit price, and
line total (same numeric format), section titles (fixed strings), chart_data
(stored, not written into the HTML), generate_excel, and generate_json.
"""

from __future__ import annotations

import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"

SUMMARY = {
    "labor": 100.0,
    "material": 200.0,
    "equipment": 0.0,
    "overhead": 0.0,
    "profit": 0.0,
    "total": 12345.67,
}


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def load_skill():
    """Execute the implementation block shipped in the skill."""
    source = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = source.index(marker) + len(marker)
    end = source.index("\n```", start)
    namespace: dict = {}
    exec(compile(source[start:end], str(SKILL_PATH), "exec"), namespace)
    return namespace["CWICRReportGenerator"], namespace["QuickReport"]


def visible_text(document: str) -> str:
    parser = _VisibleText()
    parser.feed(document)
    parser.close()
    return "".join(parser.parts)


class HtmlReportEscapingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Generator, cls.QuickReport = load_skill()

    def assert_encoded_text(self, document: str, payload: str) -> None:
        self.assertIn("<h1>Cost Estimation Report</h1>", document)
        self.assertNotIn(
            payload,
            document,
            "user-controlled markup was written into the HTML report",
        )
        self.assertIn(
            payload,
            visible_text(document),
            "encoded report must still show the original characters",
        )

    def save(self, generator) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cost_report.html"
            written = generator.save_html(str(path))
            self.assertEqual(written, str(path))
            self.assertTrue(path.is_file())
            return path.read_text(encoding="utf-8")

    def test_project_name_is_encoded_in_saved_report(self):
        payload = "</title><script>alert(1)</script>"
        generator = self.Generator(payload, currency="USD")
        generator.add_summary(SUMMARY)
        document = self.save(generator)
        self.assert_encoded_text(document, payload)
        self.assertIn("12,345.67", document)

    def test_currency_is_encoded_in_saved_report(self):
        payload = "USD<script>alert(1)</script>"
        generator = self.Generator("Office Building", currency=payload)
        generator.add_summary(SUMMARY)
        document = self.save(generator)
        self.assert_encoded_text(document, payload)
        self.assertIn("12,345.67", document)

    def test_category_name_is_encoded_in_saved_report(self):
        payload = "</table><h1 id=forged>FORGED TOTAL $1.00</h1>"
        generator = self.Generator("Office Building", currency="USD")
        generator.add_summary(SUMMARY)
        generator.add_breakdown_by_category({payload: 99999.5, "Concrete": 10.0})
        document = self.save(generator)
        self.assertIn('class="detail-table"', document)
        self.assertIn("99,999.50", document)
        self.assertIn("12,345.67", document)
        self.assert_encoded_text(document, payload)

    def test_line_item_code_description_and_unit_are_encoded(self):
        code = "<SCRIPT>alert(1)</SCRIPT>"
        description = "<img src=x onerror=alert(1)>"
        unit = "<i id=unitpwn>ea</i>"
        long_description = "<svg/onload=alert(1)>" + ("X" * 30) + "TAIL"
        shown = long_description[:50]
        self.assertLessEqual(len(description), 50)
        self.assertIn("<svg", shown)
        self.assertNotIn("TAIL", shown)

        generator = self.Generator("Office Building", currency="USD")
        generator.add_summary(SUMMARY)
        generator.add_line_items(
            [
                {
                    "code": code,
                    "description": description,
                    "quantity": 2,
                    "unit": unit,
                    "unit_price": 5,
                    "total": 10,
                },
                {
                    "code": "C-2",
                    "description": long_description,
                    "quantity": 1,
                    "unit": "m",
                    "unit_price": 1,
                    "total": 1,
                },
            ]
        )
        document = self.save(generator)
        self.assertIn('class="line-items"', document)
        self.assertIn("10.00", document)
        self.assert_encoded_text(document, code)
        self.assert_encoded_text(document, description)
        self.assert_encoded_text(document, unit)
        self.assert_encoded_text(document, shown)

    def test_quickreport_from_dataframe_encodes_saved_report(self):
        import pandas as pd

        project = "</title><script>alert(1)</script>"
        category = "</table><h1 id=forged>FORGED TOTAL $1.00</h1>"
        code = "<script>alert(1)</script>"
        description = "<img src=x onerror=alert(1)>"
        unit = "<i id=unitpwn>ea</i>"
        frame = pd.DataFrame(
            [
                {
                    "category": category,
                    "code": code,
                    "description": description,
                    "quantity": 1.0,
                    "unit": unit,
                    "unit_price": 9.0,
                    "total": 42.5,
                    "labor_cost": 1.0,
                    "material_cost": 2.0,
                    "equipment_cost": 0.0,
                    "overhead_cost": 0.0,
                    "profit_cost": 0.0,
                    "total_cost": 888.25,
                }
            ]
        )
        generator = self.QuickReport.from_dataframe(frame, project)
        document = self.save(generator)
        self.assertIn('class="detail-table"', document)
        self.assertIn('class="line-items"', document)
        self.assertIn("888.25", document)
        self.assertIn("42.50", document)
        for payload in (project, category, code, description, unit):
            self.assert_encoded_text(document, payload)


if __name__ == "__main__":
    unittest.main()
