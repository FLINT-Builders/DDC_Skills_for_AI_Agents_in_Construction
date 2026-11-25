"""Spec for contingency draws in the curated budget variance analyzer.

The implementation under test is the Python block in
4_DDC_Curated/Financial-Management/budget-variance-analyzer/SKILL.md.
These tests load that block and call it. They describe the accounting a draw
must satisfy, and they fail until the skill does.

Contract for BudgetVarianceAnalyzer.use_contingency(amount, target_code, reason, approver):

- A draw is a transfer. The target cost code's revised_budget increases by
  amount and the contingency pool's revised_budget decreases by amount.
  The sum of cost-code budgets stays the same, so project variance
  (total budget minus actuals) and the default EAC do not improve just
  because contingency was drawn.
- The pool is every cost code whose category is CostCategory.CONTINGENCY.
  A code is not a pool merely because its name or description says
  "Contingency". At least one pool code must exist.
- Remaining contingency is the live sum of those codes' revised budgets
  (CostCode.budget must report that same remaining). A draw larger than the
  live remaining is rejected. A draw equal to the remaining is allowed.
  After the pool is reduced to zero, CostCode.budget stays 0 and must not
  fall back to original_budget.
- Reject a draw, and leave every budget, contingency_used, snapshot, and
  draw log unchanged, when amount is not greater than zero, reason or
  approver is blank, the target code is missing, or the target's category
  is contingency.
- Each accepted draw is appended to analyzer.contingency_draws. Each record
  exposes amount, target_code, reason, and approver (dict keys or attributes).

VarianceReport.budget_exceeds_original is true when the sum of cost-code
budgets is greater than the analyzer's original_budget. The markdown report
then contains the line:

    WARNING: cost-code budgets exceed original budget

revise_budget is not a contingency transfer. These tests do not require it
to reject increases. The toolkit analyzer under 1_DDC_Toolkit has no
use_contingency path and is not covered here.
"""

from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    ROOT
    / "4_DDC_Curated"
    / "Financial-Management"
    / "budget-variance-analyzer"
    / "SKILL.md"
)
WARNING = "WARNING: cost-code budgets exceed original budget"


def load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    for block in blocks:
        if "class BudgetVarianceAnalyzer" in block and "def use_contingency" in block:
            namespace = {}
            exec(compile(block, str(SKILL_PATH), "exec"), namespace, namespace)
            return namespace
    raise RuntimeError(f"no BudgetVarianceAnalyzer implementation in {SKILL_PATH}")


SKILL = load_skill_namespace()
BudgetVarianceAnalyzer = SKILL["BudgetVarianceAnalyzer"]
CostCategory = SKILL["CostCategory"]

STEEL = "05-100"
POOL = "19-800"


def call_draw(analyzer, amount, target_code, reason="covered variance", approver="Casey Lee"):
    """Call use_contingency with the arguments the loaded signature accepts.

    Approver is passed only when the method already declares it, so the
    current body still runs. Tests that require the parameter assert that
    separately.
    """
    method = analyzer.use_contingency
    params = inspect.signature(method).parameters
    args = [amount, target_code]
    kwargs = {}
    reason_param = params.get("reason")
    if reason_param is not None and reason_param.kind is inspect.Parameter.POSITIONAL_ONLY:
        args.append(reason)
    elif "reason" in params:
        kwargs["reason"] = reason
    if "approver" in params:
        kwargs["approver"] = approver
    return method(*args, **kwargs)


def record_field(record, name):
    if isinstance(record, dict):
        return record[name]
    return getattr(record, name)


def pool_codes(analyzer):
    return [cc for cc in analyzer.cost_codes.values() if cc.category == CostCategory.CONTINGENCY]


def pool_budget(analyzer):
    return sum(cc.budget for cc in pool_codes(analyzer))


def exceeds_original(report):
    if not hasattr(report, "budget_exceeds_original"):
        raise AssertionError(
            "VarianceReport.budget_exceeds_original is missing; "
            "reports must flag when the sum of cost-code budgets exceeds original_budget"
        )
    return bool(report.budget_exceeds_original)


def project_budget(analyzer):
    return sum(cc.budget for cc in analyzer.cost_codes.values())


def snapshot(analyzer):
    codes = tuple(
        (
            cc.code,
            cc.category.value,
            cc.original_budget,
            cc.revised_budget,
            cc.budget,
            cc.actual,
            cc.committed,
            cc.forecast,
            cc.percent_complete,
        )
        for cc in sorted(analyzer.cost_codes.values(), key=lambda item: item.code)
    )
    return (
        analyzer.original_budget,
        analyzer.contingency_used,
        tuple(sorted(analyzer.cost_codes)),
        codes,
        len(analyzer.snapshots),
        repr(getattr(analyzer, "contingency_draws", ())),
    )


def assert_rejected(test, analyzer, fn):
    before = snapshot(analyzer)
    try:
        fn()
    except ValueError:
        test.assertEqual(
            snapshot(analyzer),
            before,
            "rejected contingency draw changed analyzer state",
        )
    else:
        report = analyzer.generate_variance_report()
        test.fail(
            "contingency draw was accepted; "
            f"total_budget={report.total_budget} variance={report.variance} "
            f"vac={report.vac} eac={report.eac} "
            f"critical={[cc.code for cc in report.critical_items]} "
            f"contingency_used={analyzer.contingency_used} "
            f"pool_budget={pool_budget(analyzer)} "
            f"state={snapshot(analyzer)}"
        )


def add_steel_and_pool(pool=250_000, steel=800_000, actual=1_000_000, original=None, percent_complete=None):
    if original is None:
        original = steel + pool
    analyzer = BudgetVarianceAnalyzer("Office Tower", original)
    analyzer.add_cost_code(STEEL, "Structural Steel", CostCategory.SUBCONTRACTOR, steel)
    analyzer.add_cost_code(POOL, "Management reserve", CostCategory.CONTINGENCY, pool)
    if actual is not None:
        analyzer.update_actual(STEEL, actual)
    if percent_complete is not None:
        analyzer.update_forecast(STEEL, actual, percent_complete=percent_complete)
    return analyzer


class ContingencyDrawTests(unittest.TestCase):
    def test_funded_draw_does_not_inflate_total_budget_variance_or_eac(self):
        analyzer = add_steel_and_pool(percent_complete=100)
        before = analyzer.generate_variance_report()
        self.assertEqual([cc.code for cc in before.critical_items], [STEEL])
        self.assertAlmostEqual(before.total_budget, 1_050_000, places=2)
        self.assertAlmostEqual(before.variance, 50_000, places=2)

        call_draw(analyzer, 200_000, STEEL, reason="steel overrun", approver="Casey Lee")

        steel = analyzer.cost_codes[STEEL]
        reserve = analyzer.cost_codes[POOL]
        report = analyzer.generate_variance_report()

        self.assertAlmostEqual(steel.revised_budget, 1_000_000, places=2)
        self.assertAlmostEqual(steel.budget, 1_000_000, places=2)
        self.assertAlmostEqual(reserve.revised_budget, 50_000, places=2)
        self.assertAlmostEqual(reserve.budget, 50_000, places=2)
        self.assertAlmostEqual(analyzer.contingency_used, 200_000, places=2)
        self.assertAlmostEqual(analyzer.original_budget, 1_050_000, places=2)
        self.assertAlmostEqual(project_budget(analyzer), 1_050_000, places=2)
        self.assertAlmostEqual(report.total_budget, 1_050_000, places=2)
        self.assertAlmostEqual(report.variance, before.variance, places=2)
        self.assertAlmostEqual(report.eac, 1_050_000, places=2)
        self.assertAlmostEqual(report.vac, report.total_budget - report.eac, places=2)
        self.assertAlmostEqual(report.variances_by_category["contingency"]["budget"], 50_000, places=2)
        self.assertAlmostEqual(report.variances_by_category["subcontractor"]["budget"], 1_000_000, places=2)
        self.assertNotIn(STEEL, [cc.code for cc in report.critical_items])
        self.assertFalse(exceeds_original(report))

        markdown = analyzer.generate_report_markdown(report)
        self.assertIn("| Total Budget | $1,050,000 |", markdown)
        self.assertNotIn("| Total Budget | $1,250,000 |", markdown)
        self.assertIn("**$50,000 (4.8%)**", markdown)
        self.assertNotIn("**$250,000 (20.0%)**", markdown)
        self.assertIn("| contingency | $50,000 |", markdown)
        self.assertNotIn("| contingency | $250,000 |", markdown)
        self.assertNotIn(WARNING, markdown)

    def test_partial_draw_reduces_contingency_category_and_keeps_project_variance(self):
        analyzer = add_steel_and_pool()
        call_draw(analyzer, 50_000, STEEL, reason="partial steel coverage")

        by_category = analyzer.analyze_by_category()
        report = analyzer.generate_variance_report()
        self.assertAlmostEqual(by_category["contingency"]["budget"], 200_000, places=2)
        self.assertAlmostEqual(by_category["subcontractor"]["budget"], 850_000, places=2)
        self.assertAlmostEqual(report.total_budget, 1_050_000, places=2)
        self.assertAlmostEqual(report.variance, 50_000, places=2)
        self.assertAlmostEqual(report.eac, 1_050_000, places=2)
        self.assertIn(STEEL, [cc.code for cc in analyzer.identify_critical_items()])

    def test_exact_remaining_draw_leaves_zero_budget_not_original(self):
        analyzer = add_steel_and_pool()
        try:
            call_draw(analyzer, 250_000, STEEL, reason="exhaust management reserve")
        except ValueError as exc:
            self.fail(f"draw equal to remaining contingency was rejected: {exc}")

        reserve = analyzer.cost_codes[POOL]
        self.assertAlmostEqual(reserve.revised_budget, 0, places=2)
        self.assertAlmostEqual(reserve.budget, 0, places=2)
        self.assertNotEqual(reserve.budget, reserve.original_budget)
        self.assertAlmostEqual(analyzer.cost_codes[STEEL].revised_budget, 1_050_000, places=2)
        self.assertAlmostEqual(project_budget(analyzer), 1_050_000, places=2)
        self.assertAlmostEqual(analyzer.generate_variance_report().total_budget, 1_050_000, places=2)

    def test_draw_above_remaining_is_rejected(self):
        analyzer = add_steel_and_pool()
        assert_rejected(self, analyzer, lambda: call_draw(analyzer, 250_001, STEEL, reason="over the reserve"))
        assert_rejected(self, analyzer, lambda: call_draw(analyzer, 5_000_000, STEEL, reason="uncapped draw"))
        self.assertIn(STEEL, [cc.code for cc in analyzer.identify_critical_items()])
        report = analyzer.generate_variance_report()
        self.assertAlmostEqual(report.total_budget, 1_050_000, places=2)
        self.assertAlmostEqual(report.variance, 50_000, places=2)
        self.assertIn(STEEL, [cc.code for cc in report.critical_items])

    def test_cap_uses_revised_contingency_balance_not_original(self):
        analyzer = add_steel_and_pool()
        analyzer.revise_budget(POOL, 40_000, reason="reserve reduced by management")
        self.assertAlmostEqual(analyzer.cost_codes[POOL].revised_budget, 40_000, places=2)
        self.assertAlmostEqual(analyzer.cost_codes[POOL].original_budget, 250_000, places=2)

        assert_rejected(
            self,
            analyzer,
            lambda: call_draw(analyzer, 40_001, STEEL, reason="above the revised reserve"),
        )
        self.assertAlmostEqual(analyzer.cost_codes[POOL].budget, 40_000, places=2)
        self.assertAlmostEqual(analyzer.cost_codes[STEEL].budget, 800_000, places=2)

        try:
            call_draw(analyzer, 40_000, STEEL, reason="take the revised reserve")
        except ValueError as exc:
            self.fail(f"draw equal to the revised contingency balance was rejected: {exc}")

        self.assertAlmostEqual(analyzer.cost_codes[POOL].revised_budget, 0, places=2)
        self.assertAlmostEqual(analyzer.cost_codes[POOL].budget, 0, places=2)
        self.assertAlmostEqual(analyzer.cost_codes[STEEL].revised_budget, 840_000, places=2)
        self.assertAlmostEqual(project_budget(analyzer), 840_000, places=2)

    def test_second_draw_cannot_exceed_the_reduced_pool(self):
        analyzer = add_steel_and_pool()
        call_draw(analyzer, 200_000, STEEL, reason="first steel draw")
        self.assertAlmostEqual(pool_budget(analyzer), 50_000, places=2)
        self.assertAlmostEqual(project_budget(analyzer), 1_050_000, places=2)

        assert_rejected(
            self,
            analyzer,
            lambda: call_draw(analyzer, 1_000_000, STEEL, reason="second draw larger than what remains"),
        )
        self.assertAlmostEqual(pool_budget(analyzer), 50_000, places=2)
        self.assertAlmostEqual(analyzer.contingency_used, 200_000, places=2)

        call_draw(analyzer, 50_000, STEEL, reason="drain the reserve")
        self.assertAlmostEqual(analyzer.cost_codes[POOL].budget, 0, places=2)
        self.assertAlmostEqual(project_budget(analyzer), 1_050_000, places=2)
        self.assertAlmostEqual(analyzer.contingency_used, 250_000, places=2)

        assert_rejected(self, analyzer, lambda: call_draw(analyzer, 1, STEEL, reason="reserve is empty"))
        self.assertAlmostEqual(analyzer.cost_codes[POOL].revised_budget, 0, places=2)
        self.assertAlmostEqual(analyzer.cost_codes[POOL].budget, 0, places=2)

    def test_unfunded_draw_cannot_hide_critical_overrun(self):
        analyzer = BudgetVarianceAnalyzer("Office Tower", 800_000)
        analyzer.add_cost_code(STEEL, "Structural Steel", CostCategory.SUBCONTRACTOR, 800_000)
        analyzer.update_actual(STEEL, 1_000_000)
        analyzer.update_forecast(STEEL, 1_000_000, percent_complete=100)

        before = analyzer.generate_variance_report()
        self.assertEqual([cc.code for cc in before.critical_items], [STEEL])
        self.assertAlmostEqual(before.variance, -200_000, places=2)

        assert_rejected(self, analyzer, lambda: call_draw(analyzer, 200_000, STEEL, reason="no reserve on the job"))
        assert_rejected(self, analyzer, lambda: call_draw(analyzer, 5_000_000, STEEL, reason="repeat a larger draw"))

        report = analyzer.generate_variance_report()
        self.assertEqual([cc.code for cc in analyzer.identify_critical_items()], [STEEL])
        self.assertEqual([cc.code for cc in report.critical_items], [STEEL])
        self.assertAlmostEqual(report.total_budget, 800_000, places=2)
        self.assertAlmostEqual(report.variance, -200_000, places=2)
        self.assertAlmostEqual(report.vac, before.vac, places=2)
        markdown = analyzer.generate_report_markdown(report)
        self.assertIn("| Total Budget | $800,000 |", markdown)
        self.assertIn("$-200,000 (-25.0%)", markdown)
        self.assertIn("| 05-100 |", markdown)
        self.assertNotIn("| Total Budget | $1,000,000 |", markdown)

    def test_non_positive_draw_rejected(self):
        for amount in (0, -1, -200_000):
            with self.subTest(amount=amount):
                analyzer = add_steel_and_pool()
                assert_rejected(
                    self,
                    analyzer,
                    lambda amount=amount: call_draw(analyzer, amount, STEEL, reason="non-positive draw"),
                )

    def test_unknown_target_rejected_without_increasing_contingency_used(self):
        analyzer = add_steel_and_pool()
        assert_rejected(self, analyzer, lambda: call_draw(analyzer, 10, "99-999", reason="missing code"))
        self.assertAlmostEqual(analyzer.contingency_used, 0, places=2)
        self.assertAlmostEqual(analyzer.cost_codes[STEEL].revised_budget, 800_000, places=2)
        self.assertAlmostEqual(analyzer.cost_codes[POOL].revised_budget, 250_000, places=2)

    def test_draw_onto_contingency_code_rejected(self):
        analyzer = add_steel_and_pool()
        assert_rejected(self, analyzer, lambda: call_draw(analyzer, 25_000, POOL, reason="draw onto the reserve itself"))
        self.assertAlmostEqual(project_budget(analyzer), 1_050_000, places=2)
        self.assertAlmostEqual(analyzer.cost_codes[POOL].revised_budget, 250_000, places=2)

    def test_description_named_contingency_in_other_category_is_not_a_pool(self):
        analyzer = BudgetVarianceAnalyzer("Office Tower", 1_800_000)
        analyzer.add_cost_code(STEEL, "Structural Steel", CostCategory.SUBCONTRACTOR, 800_000)
        analyzer.add_cost_code("90-900", "Contingency", CostCategory.FEE, 1_000_000)
        analyzer.update_actual(STEEL, 1_000_000)

        assert_rejected(
            self,
            analyzer,
            lambda: call_draw(analyzer, 200_000, STEEL, reason="fee line is not the reserve"),
        )
        self.assertAlmostEqual(analyzer.cost_codes["90-900"].revised_budget, 1_000_000, places=2)
        self.assertAlmostEqual(analyzer.cost_codes[STEEL].budget, 800_000, places=2)
        self.assertIn(STEEL, [cc.code for cc in analyzer.identify_critical_items()])

    def test_blank_reason_rejected(self):
        for reason in ("", "   ", "\n\t"):
            with self.subTest(reason=reason):
                analyzer = add_steel_and_pool()
                assert_rejected(
                    self,
                    analyzer,
                    lambda reason=reason: call_draw(analyzer, 1_000, STEEL, reason=reason),
                )

    def test_approver_is_required_and_recorded(self):
        params = inspect.signature(BudgetVarianceAnalyzer.use_contingency).parameters
        self.assertIn("approver", params)

        analyzer = add_steel_and_pool()
        call_draw(analyzer, 10_000, STEEL, reason="steel escalation", approver="Casey Lee")
        self.assertAlmostEqual(pool_budget(analyzer), 240_000, places=2)
        self.assertAlmostEqual(project_budget(analyzer), 1_050_000, places=2)

        draws = getattr(analyzer, "contingency_draws", None)
        self.assertIsInstance(draws, list)
        self.assertEqual(len(draws), 1)
        self.assertAlmostEqual(record_field(draws[0], "amount"), 10_000, places=2)
        self.assertEqual(record_field(draws[0], "target_code"), STEEL)
        self.assertEqual(record_field(draws[0], "reason"), "steel escalation")
        self.assertEqual(record_field(draws[0], "approver"), "Casey Lee")

    def test_blank_approver_rejected(self):
        params = inspect.signature(BudgetVarianceAnalyzer.use_contingency).parameters
        self.assertIn("approver", params)
        for approver in ("", "   "):
            with self.subTest(approver=approver):
                analyzer = add_steel_and_pool()
                assert_rejected(
                    self,
                    analyzer,
                    lambda approver=approver: call_draw(
                        analyzer, 1_000, STEEL, reason="needs a name", approver=approver
                    ),
                )

    def test_two_contingency_codes_draw_reduces_pool_sum(self):
        analyzer = BudgetVarianceAnalyzer("Office Tower", 1_050_000)
        analyzer.add_cost_code(STEEL, "Structural Steel", CostCategory.SUBCONTRACTOR, 800_000)
        analyzer.add_cost_code("19-810", "Management reserve", CostCategory.CONTINGENCY, 100_000)
        analyzer.add_cost_code("19-820", "Design contingency", CostCategory.CONTINGENCY, 150_000)
        analyzer.update_actual(STEEL, 1_000_000)

        call_draw(analyzer, 80_000, STEEL, reason="split across the reserve")

        self.assertAlmostEqual(pool_budget(analyzer), 170_000, places=2)
        self.assertAlmostEqual(analyzer.cost_codes[STEEL].revised_budget, 880_000, places=2)
        self.assertAlmostEqual(project_budget(analyzer), 1_050_000, places=2)
        self.assertTrue(all(cc.revised_budget >= -1e-9 for cc in pool_codes(analyzer)))
        for code in ("19-810", "19-820"):
            self.assertGreaterEqual(analyzer.cost_codes[code].budget, -1e-9)

    def test_two_contingency_codes_draw_above_combined_pool_rejected(self):
        analyzer = BudgetVarianceAnalyzer("Office Tower", 1_050_000)
        analyzer.add_cost_code(STEEL, "Structural Steel", CostCategory.SUBCONTRACTOR, 800_000)
        analyzer.add_cost_code("19-810", "Management reserve", CostCategory.CONTINGENCY, 100_000)
        analyzer.add_cost_code("19-820", "Design contingency", CostCategory.CONTINGENCY, 150_000)
        analyzer.update_actual(STEEL, 1_000_000)

        assert_rejected(self, analyzer, lambda: call_draw(analyzer, 250_001, STEEL, reason="above both reserves"))
        self.assertAlmostEqual(pool_budget(analyzer), 250_000, places=2)
        self.assertIn(STEEL, [cc.code for cc in analyzer.identify_critical_items()])

    def test_draw_log_records_each_accepted_draw_in_order(self):
        analyzer = add_steel_and_pool()
        call_draw(analyzer, 20_000, STEEL, reason="first", approver="Casey Lee")
        call_draw(analyzer, 30_000, STEEL, reason="second", approver="Rene Ortiz")

        self.assertAlmostEqual(pool_budget(analyzer), 200_000, places=2)
        self.assertAlmostEqual(analyzer.contingency_used, 50_000, places=2)
        draws = analyzer.contingency_draws
        self.assertEqual(len(draws), 2)
        self.assertEqual(
            [(record_field(item, "amount"), record_field(item, "reason"), record_field(item, "approver")) for item in draws],
            [(20_000, "first", "Casey Lee"), (30_000, "second", "Rene Ortiz")],
        )
        self.assertEqual([record_field(item, "target_code") for item in draws], [STEEL, STEEL])

    def test_report_flags_budgets_above_original(self):
        analyzer = add_steel_and_pool(original=1_050_000)
        balanced = analyzer.generate_variance_report()
        self.assertFalse(exceeds_original(balanced))
        self.assertNotIn(WARNING, analyzer.generate_report_markdown(balanced))

        analyzer.revise_budget(STEEL, 800_001, reason="unfunded increase")
        report = analyzer.generate_variance_report()
        self.assertTrue(exceeds_original(report))
        self.assertGreater(report.total_budget, analyzer.original_budget)
        self.assertIn(WARNING, analyzer.generate_report_markdown(report))

    def test_report_does_not_flag_when_budgets_within_original(self):
        analyzer = BudgetVarianceAnalyzer("Office Tower", 5_000_000)
        analyzer.add_cost_code(STEEL, "Structural Steel", CostCategory.SUBCONTRACTOR, 800_000)
        analyzer.add_cost_code(POOL, "Management reserve", CostCategory.CONTINGENCY, 250_000)
        report = analyzer.generate_variance_report()
        self.assertFalse(exceeds_original(report))
        self.assertNotIn(WARNING, analyzer.generate_report_markdown(report))

        equal = add_steel_and_pool(original=1_050_000)
        equal_report = equal.generate_variance_report()
        self.assertAlmostEqual(equal_report.total_budget, equal.original_budget, places=2)
        self.assertFalse(exceeds_original(equal_report))
        self.assertNotIn(WARNING, equal.generate_report_markdown(equal_report))


if __name__ == "__main__":
    unittest.main()
