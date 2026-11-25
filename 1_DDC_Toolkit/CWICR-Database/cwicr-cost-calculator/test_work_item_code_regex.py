"""Work-item codes from a quantity takeoff must be literal text.

These tests load the calculator embedded in SKILL.md and feed it codes the
way the skill tells an estimator to: a quantities workbook read with
pandas, then calculate_from_qto / calculate_item_cost and the report
helpers that call the same lookup.

They fail while a code that is not an exact database key is compiled as a
regular expression. They pass once unmatched codes stay unpriced, an
invalid pattern cannot abort the run, and a nested-quantifier code returns
immediately as missing data.
"""

import signal
import tempfile
import unittest
import warnings
from contextlib import contextmanager
from pathlib import Path

import pandas as pd


SKILL_PATH = Path(__file__).resolve().parent / "SKILL.md"
FIRST_CODE = "AAA-0001"
FIRST_DESCRIPTION = "FIRST-RATE"
SECOND_CODE = "BBB-0002"
REALISTIC_CODE = "CWICR-00001-CONCRETE"
# Nested quantifiers plus a lookahead. Pandas routes lookaheads to Python's
# re, which backtracks exponentially on ordinary work-item codes.
NESTED_QUANTIFIER = r"(?=.)(.*.*.*.*)*#"
LOOKUP_LIMIT_SECONDS = 1.0


def load_calculator_module():
    source = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = source.index(marker) + len(marker)
    end = source.index("\n```", start)
    namespace = {"__name__": "cwicr_cost_calculator_skill"}
    exec(compile(source[start:end], str(SKILL_PATH), "exec"), namespace)
    return namespace


@contextmanager
def fail_if_slower_than(seconds):
    def handler(signum, frame):
        raise AssertionError(
            f"work-item lookup did not finish within {seconds}s"
        )

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def price_database():
    return pd.DataFrame(
        {
            "work_item_code": [FIRST_CODE, SECOND_CODE, REALISTIC_CODE],
            "description": [FIRST_DESCRIPTION, "SECOND-RATE", "CONCRETE POUR"],
            "unit": ["m3", "m2", "m3"],
            "labor_cost": [100.0, 5.0, 8.0],
            "material_cost": [40.0, 1.0, 2.0],
            "equipment_cost": [10.0, 1.0, 1.0],
            "labor_norm": [1.0, 1.0, 1.0],
            "labor_rate": [1.0, 1.0, 1.0],
        }
    )


def read_quantities_workbook(rows):
    """Write and read a workbook the same way the skill's QTO example does."""
    frame = pd.DataFrame(rows)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "quantities.xlsx"
        frame.to_excel(path, index=False)
        return pd.read_excel(path)


def assert_unpriced(test, breakdown):
    test.assertEqual(breakdown.status, test.module["CostStatus"].MISSING_DATA)
    test.assertEqual(breakdown.description, "NOT FOUND")
    test.assertEqual(breakdown.total_cost, 0.0)
    test.assertNotEqual(breakdown.description, FIRST_DESCRIPTION)


class WorkItemCodeRegexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_calculator_module()

    def setUp(self):
        warnings.filterwarnings(
            "ignore",
            message="This pattern is interpreted as a regular expression",
            category=UserWarning,
        )
        self.calculator = self.module["CWICRCostCalculator"](price_database())
        self.first_total_for_two = 379.5

    def test_qto_workbook_wildcard_is_not_priced_as_first_database_item(self):
        quantities = read_quantities_workbook(
            [{"work_item": ".*", "quantity": 2}]
        )
        costs = self.calculator.calculate_from_qto(
            quantities, code_column="work_item", quantity_column="quantity"
        )
        self.assertEqual(len(costs), 1)
        self.assertEqual(costs.loc[0, "status"], "missing_data")
        self.assertEqual(costs.loc[0, "description"], "NOT FOUND")
        self.assertEqual(costs.loc[0, "total_cost"], 0.0)
        self.assertNotEqual(costs.loc[0, "description"], FIRST_DESCRIPTION)

    def test_qto_workbook_dot_is_not_a_single_character_wildcard(self):
        quantities = read_quantities_workbook(
            [{"work_item": "AAA-000.", "quantity": 1}]
        )
        costs = self.calculator.calculate_from_qto(
            quantities, code_column="work_item", quantity_column="quantity"
        )
        self.assertEqual(costs.loc[0, "status"], "missing_data")
        self.assertEqual(costs.loc[0, "description"], "NOT FOUND")
        self.assertEqual(costs.loc[0, "total_cost"], 0.0)

    def test_qto_workbook_invalid_pattern_does_not_abort_other_rows(self):
        quantities = read_quantities_workbook(
            [
                {"work_item": FIRST_CODE, "quantity": 2},
                {"work_item": "C(", "quantity": 1},
            ]
        )
        costs = self.calculator.calculate_from_qto(
            quantities, code_column="work_item", quantity_column="quantity"
        )
        self.assertEqual(len(costs), 2)
        self.assertEqual(costs.loc[0, "status"], "calculated")
        self.assertEqual(costs.loc[0, "description"], FIRST_DESCRIPTION)
        self.assertAlmostEqual(costs.loc[0, "total_cost"], self.first_total_for_two)
        self.assertEqual(costs.loc[1, "status"], "missing_data")
        self.assertEqual(costs.loc[1, "description"], "NOT FOUND")
        self.assertEqual(costs.loc[1, "total_cost"], 0.0)

    def test_qto_workbook_nested_quantifier_does_not_stall(self):
        quantities = read_quantities_workbook(
            [{"work_item": NESTED_QUANTIFIER, "quantity": 1}]
        )
        with fail_if_slower_than(LOOKUP_LIMIT_SECONDS):
            costs = self.calculator.calculate_from_qto(
                quantities, code_column="work_item", quantity_column="quantity"
            )
        self.assertEqual(len(costs), 1)
        self.assertEqual(costs.loc[0, "status"], "missing_data")
        self.assertEqual(costs.loc[0, "description"], "NOT FOUND")
        self.assertEqual(costs.loc[0, "total_cost"], 0.0)

    def test_calculate_item_cost_wildcard_is_missing(self):
        breakdown = self.calculator.calculate_item_cost(".*", 2)
        assert_unpriced(self, breakdown)

    def test_calculate_item_cost_invalid_pattern_is_missing(self):
        breakdown = self.calculator.calculate_item_cost("C(", 1)
        assert_unpriced(self, breakdown)

    def test_calculate_item_cost_nested_quantifier_does_not_stall(self):
        with fail_if_slower_than(LOOKUP_LIMIT_SECONDS):
            breakdown = self.calculator.calculate_item_cost(NESTED_QUANTIFIER, 1)
        assert_unpriced(self, breakdown)

    def test_calculate_estimate_wildcard_total_is_zero(self):
        summary = self.calculator.calculate_estimate(
            [{"work_item_code": ".*", "quantity": 2}]
        )
        self.assertEqual(summary.total_cost, 0.0)
        self.assertEqual(summary.labor_total, 0.0)

    def test_calculate_estimate_invalid_pattern_does_not_abort(self):
        summary = self.calculator.calculate_estimate(
            [
                {"work_item_code": FIRST_CODE, "quantity": 2},
                {"work_item_code": "C(", "quantity": 1},
            ]
        )
        self.assertEqual(summary.item_count, 2)
        self.assertAlmostEqual(summary.total_cost, self.first_total_for_two)

    def test_calculate_cost_wildcard_is_zero(self):
        total = self.module["calculate_cost"](price_database(), ".*", 2)
        self.assertEqual(total, 0.0)

    def test_calculate_cost_invalid_pattern_is_zero(self):
        total = self.module["calculate_cost"](price_database(), "C(", 1)
        self.assertEqual(total, 0.0)

    def test_estimate_project_cost_code_alias_wildcard_is_zero(self):
        report = self.module["estimate_project_cost"](
            price_database(), [{"code": ".*", "quantity": 1}]
        )
        self.assertEqual(report["total_cost"], 0.0)
        self.assertEqual(report["item_count"], 1)

    def test_summary_report_wildcard_total_is_zero(self):
        report = self.module["CostReportGenerator"](
            self.calculator
        ).generate_summary_report([{"work_item_code": ".*", "quantity": 1}])
        self.assertEqual(report["total_cost"], 0.0)

    def test_summary_report_invalid_pattern_does_not_abort(self):
        report = self.module["CostReportGenerator"](
            self.calculator
        ).generate_summary_report(
            [
                {"work_item_code": FIRST_CODE, "quantity": 2},
                {"code": "C(", "quantity": 1},
            ]
        )
        self.assertEqual(report["item_count"], 2)
        self.assertAlmostEqual(report["total_cost"], self.first_total_for_two)

    def test_detailed_report_wildcard_is_missing(self):
        report = self.module["CostReportGenerator"](
            self.calculator
        ).generate_detailed_report([{"work_item_code": ".*", "quantity": 2}])
        self.assertEqual(report.loc[0, "status"], "missing_data")
        self.assertEqual(report.loc[0, "description"], "NOT FOUND")
        self.assertEqual(report.loc[0, "total_cost"], 0.0)

    def test_detailed_report_invalid_pattern_does_not_abort(self):
        report = self.module["CostReportGenerator"](
            self.calculator
        ).generate_detailed_report(
            [
                {"work_item_code": FIRST_CODE, "quantity": 2},
                {"work_item_code": "C(", "quantity": 1},
            ]
        )
        self.assertEqual(report.loc[0, "status"], "calculated")
        self.assertAlmostEqual(report.loc[0, "total_cost"], self.first_total_for_two)
        self.assertEqual(report.loc[1, "status"], "missing_data")
        self.assertEqual(report.loc[1, "total_cost"], 0.0)


if __name__ == "__main__":
    unittest.main()
