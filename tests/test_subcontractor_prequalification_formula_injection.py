"""Formula injection in the subcontractor prequalification register.

These tests load the implementation embedded in
``1_DDC_Toolkit/Procurement/subcontractor-prequalification/SKILL.md`` and
call ``add_application`` then ``export_register``. They fail while an
applicant-controlled company name or trade that starts with ``=`` is written
through ``DataFrame.to_excel`` as an Excel formula. They pass once those
values are neutralized before the workbook is written (a leading apostrophe,
or the leading formula marker blocked) so the saved cells are text.

``export_register`` is the only writer in this skill. Fields it does not
write, and that this file therefore does not cover: ``notes``,
``contact_email``, ``bonding_capacity``, and ``project_name``. Exported
columns that the skill types as numbers or an enum, rather than applicant
free text (``Years``, ``Revenue``, ``EMR``, ``Status``, ``Score``), are not
given formula strings here.
"""

from __future__ import annotations

import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

import openpyxl

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "Procurement"
    / "subcontractor-prequalification"
    / "SKILL.md"
)


def load_prequalification_class():
    """Execute the skill's implementation block and return the class."""
    text = SKILL_MD.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    for block in blocks:
        if "class SubcontractorPrequalification" not in block:
            continue
        module = types.ModuleType("skill_under_test")
        # dataclasses looks up the class module in sys.modules while exec'ing.
        sys.modules["skill_under_test"] = module
        exec(compile(block, str(SKILL_MD), "exec"), module.__dict__)
        return module.SubcontractorPrequalification
    raise AssertionError(
        f"No SubcontractorPrequalification implementation in {SKILL_MD}"
    )


def _add(prequal, company_name, trade, *, revenue, emr):
    return prequal.add_application(
        company_name,
        trade,
        "applicant@example.com",
        years_in_business=8,
        annual_revenue=revenue,
        bonding_capacity=250000.0,
        emr_rate=emr,
    )


class PrequalificationFormulaInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prequalification_cls = load_prequalification_class()

    def _export(self, prequal):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "register.xlsx"
        prequal.export_register(str(path))
        self.assertTrue(path.is_file(), "export_register did not write a workbook")
        return openpyxl.load_workbook(path)

    def _header_columns(self, worksheet):
        return {
            cell.value: cell.column
            for cell in next(worksheet.iter_rows(min_row=1, max_row=1))
        }

    def _cell(self, worksheet, app_id, column_name):
        columns = self._header_columns(worksheet)
        self.assertIn(column_name, columns)
        id_column = columns["ID"]
        for row in range(2, worksheet.max_row + 1):
            if worksheet.cell(row=row, column=id_column).value == app_id:
                return worksheet.cell(row=row, column=columns[column_name])
        self.fail(f"{app_id} was not written to the register")

    def _assert_not_formula(self, cell):
        self.assertNotEqual(
            cell.data_type,
            "f",
            f"{cell.coordinate} was stored as a formula ({cell.value!r})",
        )
        if isinstance(cell.value, str):
            self.assertFalse(
                cell.value.startswith("="),
                f"{cell.coordinate} still starts with a formula marker ({cell.value!r})",
            )

    def test_company_name_formula_is_not_written_to_the_register(self):
        prequal = self.prequalification_cls("Office Tower")
        _add(prequal, "Northwind Concrete", "Concrete", revenue=1234567.89, emr=0.85)
        attacker = _add(prequal, "=E2", "Electrical", revenue=1.0, emr=1.1)

        worksheet = self._export(prequal).active
        self._assert_not_formula(self._cell(worksheet, attacker.app_id, "Company"))

    def test_trade_formula_is_not_written_to_the_register(self):
        prequal = self.prequalification_cls("Office Tower")
        _add(prequal, "Northwind Concrete", "Concrete", revenue=1234567.89, emr=0.85)
        attacker = _add(prequal, "Rival Electric", "=F2", revenue=1.0, emr=1.1)

        worksheet = self._export(prequal).active
        self._assert_not_formula(self._cell(worksheet, attacker.app_id, "Trade"))

    def test_formula_on_the_first_row_is_not_written_to_the_register(self):
        prequal = self.prequalification_cls("Office Tower")
        attacker = _add(prequal, "=E3", "=H3", revenue=1.0, emr=1.1)
        _add(prequal, "Northwind Concrete", "Concrete", revenue=1234567.89, emr=0.85)

        worksheet = self._export(prequal).active
        self._assert_not_formula(self._cell(worksheet, attacker.app_id, "Company"))
        self._assert_not_formula(self._cell(worksheet, attacker.app_id, "Trade"))

    def test_trade_formula_joining_revenue_emr_and_score_is_not_written(self):
        prequal = self.prequalification_cls("Office Tower")
        victim = _add(
            prequal, "Northwind Concrete", "Concrete", revenue=1234567.89, emr=0.85
        )
        prequal.score_application(
            victim.app_id,
            {
                "Safety Record": 8,
                "Financial Stability": 7,
                "Experience": 8,
                "References": 7,
                "Capacity": 8,
                "Insurance/Bonding": 9,
            },
        )
        attacker = _add(
            prequal,
            "Rival Electric",
            '=E2&"/"&F2&"/"&H2',
            revenue=1.0,
            emr=1.1,
        )

        worksheet = self._export(prequal).active
        self.assertEqual(
            self._cell(worksheet, victim.app_id, "Revenue").value,
            1234567.89,
        )
        self.assertEqual(self._cell(worksheet, victim.app_id, "EMR").value, 0.85)
        self.assertGreater(self._cell(worksheet, victim.app_id, "Score").value, 0)
        self._assert_not_formula(self._cell(worksheet, attacker.app_id, "Trade"))

    def test_arithmetic_formula_company_name_is_not_written(self):
        prequal = self.prequalification_cls("Office Tower")
        attacker = _add(prequal, "=1+1", "Plumbing", revenue=10.0, emr=1.0)

        worksheet = self._export(prequal).active
        self._assert_not_formula(self._cell(worksheet, attacker.app_id, "Company"))


if __name__ == "__main__":
    unittest.main()
