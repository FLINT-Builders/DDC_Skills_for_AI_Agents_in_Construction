"""Spreadsheet formula injection in the unit price CSV export.

The implementation under test is the Python block in SKILL.md
(import_from_csv and export_to_csv). A vendor price file can set
description, source, tags, code, and unit. add_price can also set
location. export_to_csv writes those strings with DataFrame.to_csv.

These tests fail while a text cell is written so a spreadsheet treats
it as a formula. They pass when every exported text cell that would
have started with ``=``, ``+``, ``-``, or ``@`` is forced to text,
including by prefixing a single quote, including after location
adjustment and escalation.

Run from the repository root. The directory name contains a dot, so pass
the file to Python rather than to ``python3 -m unittest``:

    python3 "2_DDC_Book/3.1-Cost-Estimation/unit-price-database-manager/test_csv_formula_injection.py" -v

Requires pandas, which SKILL.md lists as the dependency of this skill.
"""

from __future__ import annotations

import csv
import re
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

SKILL_PATH = Path(__file__).with_name("SKILL.md")

# Markers named by the remediation. A leading apostrophe forces text in
# Calc (verified against LibreOffice 24.2.7.2); any other first character
# outside this set is not a formula in that spreadsheet.
FORMULA_MARKERS = ("=", "+", "-", "@")

TEXT_FIELDS = (
    "code",
    "description",
    "unit",
    "location",
    "source",
    "tags",
)


def load_implementation():
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"no Python implementation block in {SKILL_PATH}")
    namespace: dict = {}
    exec(match.group(1), namespace)
    return namespace["UnitPriceDatabaseManager"], namespace["UnitPrice"]


def assert_text_cell(field_name: str, value: str) -> None:
    """Fail if a spreadsheet would treat this exported cell as a formula."""
    if value.startswith("'"):
        return
    if value.startswith(FORMULA_MARKERS):
        raise AssertionError(
            f"{field_name} is exported as a spreadsheet formula: {value!r}"
        )


def write_vendor_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "code",
        "description",
        "unit",
        "base_price",
        "labor_cost",
        "material_cost",
        "equipment_cost",
        "source",
        "tags",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_export(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class CsvFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Manager, cls.UnitPrice = load_implementation()

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

    def _company_price(self):
        """A rate the attacker does not control, exported beside their row."""
        return self.UnitPrice(
            code="COMPANY.1",
            description="Confidential concrete rate",
            unit="CY",
            base_price=Decimal("450.00"),
            labor_cost=Decimal("150.00"),
            material_cost=Decimal("250.00"),
            equipment_cost=Decimal("50.00"),
            effective_date=date(2026, 1, 1),
            source="internal price book",
            location="National Average",
            tags=["concrete"],
        )

    def _vendor_row(self, **overrides) -> dict:
        row = {
            "code": "VENDOR.1",
            "description": "Vendor concrete",
            "unit": "CY",
            "base_price": "10.00",
            "labor_cost": "1",
            "material_cost": "2",
            "equipment_cost": "3",
            "source": "vendor file",
            "tags": "concrete",
        }
        row.update(overrides)
        return row

    def _import_and_export(self, vendor_rows: list[dict], location: str | None = None):
        manager = self.Manager()
        manager.add_price(self._company_price())
        vendor_path = self.root / "vendor.csv"
        export_path = self.root / "export.csv"
        write_vendor_csv(vendor_path, vendor_rows)
        imported = manager.import_from_csv(str(vendor_path))
        self.assertEqual(imported, len(vendor_rows))
        manager.export_to_csv(str(export_path), location=location)
        exported = read_export(export_path)
        self.assertGreaterEqual(len(exported), 2)
        company = next(row for row in exported if row["code"] == "COMPANY.1")
        self.assertEqual(company["description"], "Confidential concrete rate")
        if location is None:
            self.assertEqual(company["base_price"], "450.0")
        return manager, exported

    def test_equals_description_reads_other_rows_marker(self):
        """Description =D2 is the cell reference Calc evaluated to the company rate."""
        _, exported = self._import_and_export(
            [self._vendor_row(description="=D2")]
        )
        vendor = next(row for row in exported if row["code"] == "VENDOR.1")
        assert_text_cell("description", vendor["description"])

    def test_equals_formula_with_comma_is_not_hidden_by_csv_quoting(self):
        """to_csv quotes this value because of the comma; the parsed cell still starts with =."""
        payload = '=CONCAT("x",D2)'
        _, exported = self._import_and_export(
            [self._vendor_row(description=payload)]
        )
        vendor = next(row for row in exported if row["code"] == "VENDOR.1")
        assert_text_cell("description", vendor["description"])

    def test_hyperlink_shaped_description_is_not_a_formula(self):
        payload = '=HYPERLINK("http://example.invalid/rate","view")'
        _, exported = self._import_and_export(
            [self._vendor_row(description=payload)]
        )
        vendor = next(row for row in exported if row["code"] == "VENDOR.1")
        assert_text_cell("description", vendor["description"])

    def test_each_formula_marker_in_description(self):
        payloads = {
            "EQ": "=D2",
            "PLUS": "+1+1",
            "MINUS": "-2+3",
            "AT": "@SUM(D2)",
        }
        rows = [
            self._vendor_row(code=code, description=payload)
            for code, payload in payloads.items()
        ]
        _, exported = self._import_and_export(rows)
        for code in payloads:
            vendor = next(row for row in exported if row["code"] == code)
            assert_text_cell("description", vendor["description"])

    def test_imported_code_unit_source_and_tags(self):
        """Every text column import_from_csv copies into export_to_csv."""
        _, exported = self._import_and_export(
            [
                self._vendor_row(
                    code="=1+1",
                    description="plain description",
                    unit="+1+1",
                    source="-2+3",
                    tags="@SUM(D2)",
                )
            ]
        )
        vendor = next(
            row
            for row in exported
            if row["description"] in ("plain description", "'plain description")
        )
        for field in ("code", "unit", "source", "tags"):
            assert_text_cell(field, vendor[field])

    def test_tags_formula_round_trips_through_split_and_join(self):
        """import splits tags on commas and export joins them. A comma formula is restored."""
        _, exported = self._import_and_export(
            [self._vendor_row(tags='=CONCAT("x",D2)')]
        )
        vendor = next(row for row in exported if row["code"] == "VENDOR.1")
        assert_text_cell("tags", vendor["tags"])

    def test_location_adjustment_export_still_writes_description_formula(self):
        """export_to_csv(location=...) replaces the row via get_price, which copies description."""
        _, exported = self._import_and_export(
            [self._vendor_row(description="=D2")],
            location="New York",
        )
        vendor = next(row for row in exported if row["code"] == "VENDOR.1")
        self.assertEqual(vendor["location"], "New York")
        assert_text_cell("description", vendor["description"])

    def test_escalation_still_exports_description_formula(self):
        manager = self.Manager()
        manager.add_price(self._company_price())
        vendor_path = self.root / "vendor.csv"
        export_path = self.root / "export.csv"
        write_vendor_csv(vendor_path, [self._vendor_row(description="=D2", tags="concrete")])
        manager.import_from_csv(str(vendor_path))
        updated = manager.apply_escalation(3.5, categories=["concrete"])
        self.assertGreaterEqual(updated, 1)
        manager.export_to_csv(str(export_path))
        exported = read_export(export_path)
        poisoned = [
            row
            for row in exported
            if row["description"] in ("=D2", "'=D2") or row["description"].endswith("=D2")
        ]
        self.assertTrue(poisoned, exported)
        assert_text_cell("description", poisoned[0]["description"])

    def test_add_price_location_and_description(self):
        """add_price is the other way attacker-controlled text reaches export_to_csv."""
        manager = self.Manager()
        manager.add_price(self._company_price())
        manager.add_price(
            self.UnitPrice(
                code="VENDOR.2",
                description="=D2",
                unit="CY",
                base_price=Decimal("10"),
                labor_cost=Decimal("1"),
                material_cost=Decimal("2"),
                equipment_cost=Decimal("3"),
                effective_date=date(2026, 1, 1),
                source="+1+1",
                location="-2+3",
                tags=["@SUM(D2)"],
            )
        )
        export_path = self.root / "export.csv"
        manager.export_to_csv(str(export_path))
        exported = read_export(export_path)
        vendor = next(row for row in exported if row["code"] == "VENDOR.2")
        for field in ("description", "source", "location", "tags"):
            assert_text_cell(field, vendor[field])

    def test_benign_text_and_company_rate_round_trip(self):
        description = 'Cast-in-place concrete, 4000 PSI'
        _, exported = self._import_and_export(
            [
                self._vendor_row(
                    description=description,
                    source="RSMeans 2026",
                    tags="concrete,structural",
                    unit="CY",
                )
            ]
        )
        vendor = next(row for row in exported if row["code"] == "VENDOR.1")
        self.assertEqual(vendor["description"], description)
        self.assertEqual(vendor["source"], "RSMeans 2026")
        self.assertEqual(vendor["tags"], "concrete,structural")
        self.assertEqual(vendor["unit"], "CY")
        for field in TEXT_FIELDS:
            assert_text_cell(field, vendor[field])


if __name__ == "__main__":
    unittest.main()
