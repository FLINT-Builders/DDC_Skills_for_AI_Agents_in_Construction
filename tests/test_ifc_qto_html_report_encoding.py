"""HTML takeoff reports must show model names as text.

Category, type, and project names are copied out of the converted workbook
into the takeoff page. These tests fail while those strings are written into
the page as raw markup, and they pass once each name is HTML-encoded.
"""

import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "1_DDC_Toolkit"
    / "BIM-Analysis"
    / "ifc-qto-extraction"
    / "SKILL.md"
)

# Markup a model author can put in a category, type, or project name.
# The broken-out cells show quantities that are not in the workbook.
MALICIOUS_CATEGORY = (
    'Walls & <Beams></td><td class="number">1</td>'
    '<td class="number">1.00</td><td class="number">1.000</td>'
    '<td class="number">-</td></tr><tr><td>Injected category'
)
MALICIOUS_TYPE = (
    'Basic</td><td class="number">999999</td>'
    '<td class="number">999999.00</td><td class="number">999999.000</td>'
    '<td class="number">999999.00</td></tr><tr><td>'
    '<script>window.__qto_xss = 1</script>'
)
MALICIOUS_PROJECT = (
    'Tower</title><script>window.__qto_xss = 1</script><title>'
    '</p><div class="summary-card"><h3>Total Volume</h3>'
    '<div class="value">0.000 m³</div></div><p'
)
MALICIOUS_FAMILY = 'Family</td><td class="number">999999</td><td>x'


def load_extractor_class():
    """Execute the skill implementation shipped in SKILL.md."""
    skill = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", skill, flags=re.DOTALL)
    try:
        code = next(
            block
            for block in blocks
            if "class BIMQuantityExtractor" in block
            and "def generate_html_report" in block
        )
    except StopIteration as exc:
        raise AssertionError(
            f"No BIMQuantityExtractor implementation in {SKILL_PATH}"
        ) from exc
    namespace = {"__name__": "ifc_qto_skill"}
    exec(compile(code, str(SKILL_PATH), "exec"), namespace)
    return namespace["BIMQuantityExtractor"]


class _ReportPage(HTMLParser):
    """Collect the title, paragraphs, table rows, and tag names of a report."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.tags = []
        self.paragraphs = []
        self._title_parts = []
        self._in_title = False
        self._row = None
        self._cell = None
        self._in_p = False
        self._p_parts = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "title":
            self._in_title = True
            self._title_parts = []
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "p":
            self._in_p = True
            self._p_parts = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "p" and self._in_p:
            self.paragraphs.append("".join(self._p_parts))
            self._in_p = False

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        if self._cell is not None:
            self._cell.append(data)
        if self._in_p:
            self._p_parts.append(data)

    @property
    def title(self):
        return "".join(self._title_parts)


def _expected_rows(quantities):
    by_category = {}
    for item in quantities:
        by_category.setdefault(item.category, []).append(item)
    rows = [[
        "Category / Type",
        "Count",
        "Area (m²)",
        "Volume (m³)",
        "Length (m)",
    ]]
    for category, items in sorted(by_category.items()):
        rows.append([
            category,
            f"{sum(item.count for item in items):,}",
            f"{sum(item.area for item in items):,.2f}",
            f"{sum(item.volume for item in items):,.3f}",
            "-",
        ])
        for item in sorted(items, key=lambda entry: entry.type_name):
            rows.append([
                "\xa0\xa0\xa0" + item.type_name,
                f"{item.count:,}",
                f"{item.area:,.2f}",
                f"{item.volume:,.3f}",
                f"{item.length:,.2f}",
            ])
    return rows


def _assert_report_is_text(testcase, html_path, quantities, project_name):
    page = _ReportPage()
    page.feed(Path(html_path).read_text(encoding="utf-8"))
    page.close()

    expected = _expected_rows(quantities)
    testcase.assertEqual(page.rows[:1], expected[:1])
    testcase.assertEqual(sorted(page.rows[1:]), sorted(expected[1:]))
    testcase.assertEqual(page.title, f"QTO Report - {project_name}")
    testcase.assertEqual(page.paragraphs, [f"Project: {project_name}"])
    testcase.assertNotIn("script", page.tags)
    testcase.assertNotIn("iframe", page.tags)
    testcase.assertNotIn("img", page.tags)


class QuantityTakeoffHtmlEncodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.extractor_cls = load_extractor_class()

    def setUp(self):
        self.extractor = self.extractor_cls("/tmp/ddc-converters")

    def test_category_type_and_project_names_stay_text(self):
        frame = pd.DataFrame({
            "Category": [MALICIOUS_CATEGORY, MALICIOUS_CATEGORY, "Slabs"],
            "Type Name": [MALICIOUS_TYPE, MALICIOUS_TYPE, "Concrete Slab"],
            "Area": [10.5, 1.5, 20.0],
            "Volume": [3.0, 0.25, 5.0],
            "Length": [4.0, 4.0, 0.0],
            "Level": ["Level 1", "Level 1", "Level 2"],
        })
        quantities = self.extractor.extract_quantities(frame)
        self.assertEqual(
            sorted(item.category for item in quantities),
            sorted({MALICIOUS_CATEGORY, "Slabs"}),
        )
        self.assertIn(MALICIOUS_TYPE, [item.type_name for item in quantities])

        with tempfile.TemporaryDirectory() as directory:
            report_path = str(Path(directory) / "qto.html")
            self.extractor.generate_html_report(
                quantities,
                report_path,
                project_name=MALICIOUS_PROJECT,
            )
            _assert_report_is_text(
                self,
                report_path,
                quantities,
                MALICIOUS_PROJECT,
            )

    def test_names_from_the_converted_workbook_stay_text(self):
        frame = pd.DataFrame({
            "Category": [MALICIOUS_CATEGORY],
            "Type Name": [MALICIOUS_TYPE],
            "Area": [10.5],
            "Volume": [3.25],
            "Length": [8.0],
            "Level": ["Level 1"],
        })
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "model.xlsx"
            frame.to_excel(workbook, index=False)
            loaded = self.extractor.load_bim_data(str(workbook))
            quantities = self.extractor.extract_quantities(loaded)
            report_path = str(Path(directory) / "qto.html")
            self.extractor.generate_html_report(
                quantities,
                report_path,
                project_name=MALICIOUS_PROJECT,
            )
            _assert_report_is_text(
                self,
                report_path,
                quantities,
                MALICIOUS_PROJECT,
            )

    def test_group_by_column_stays_text(self):
        frame = pd.DataFrame({
            "Category": ["Walls"],
            "Family Name": [MALICIOUS_FAMILY],
            "Area": [2.0],
            "Volume": [1.0],
            "Length": [6.0],
        })
        quantities = self.extractor.extract_quantities(frame, group_by="Family Name")
        self.assertEqual(quantities[0].type_name, MALICIOUS_FAMILY)

        with tempfile.TemporaryDirectory() as directory:
            report_path = str(Path(directory) / "qto.html")
            self.extractor.generate_html_report(
                quantities,
                report_path,
                project_name="North Wing",
            )
            _assert_report_is_text(
                self,
                report_path,
                quantities,
                "North Wing",
            )

    def test_plain_names_keep_their_quantities(self):
        frame = pd.DataFrame({
            "Category": ["Walls", "Walls"],
            "Type Name": ["Basic Wall", "Basic Wall"],
            "Area": [10.0, 2.5],
            "Volume": [1.0, 0.5],
            "Length": [3.0, 1.0],
        })
        quantities = self.extractor.extract_quantities(frame)
        with tempfile.TemporaryDirectory() as directory:
            report_path = str(Path(directory) / "qto.html")
            self.extractor.generate_html_report(
                quantities,
                report_path,
                project_name="North Wing",
            )
            _assert_report_is_text(
                self,
                report_path,
                quantities,
                "North Wing",
            )


if __name__ == "__main__":
    unittest.main()
