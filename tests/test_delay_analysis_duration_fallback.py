"""Failing spec for inflated delay damages in the delay-analysis skill.

The implementation under test is the DelayAnalyzer block in
3_DDC_Insights/Schedule-Optimization/delay-analysis/SKILL.md. These tests
execute that block. They fail while a zero or missing calculated_impact is
replaced by the event's full calendar duration, while overlapping excusable
periods are summed twice, or while the recommended extension and compensable
damages are not capped by the actual overrun.

perform_windows_analysis is not covered. It sums calendar overlap into
window totals and does not feed recommended_time_extension or
contractor_delay_damages.
"""

import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "3_DDC_Insights"
    / "Schedule-Optimization"
    / "delay-analysis"
    / "SKILL.md"
)


def load_delay_analyzer():
    """Execute the skill implementation and return its public names."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    implementation = next(
        block for block in blocks if "class DelayAnalyzer" in block
    )
    namespace = {}
    exec(compile(implementation, str(SKILL_PATH), "exec"), namespace)
    return namespace


NS = load_delay_analyzer()
DelayAnalyzer = NS["DelayAnalyzer"]
DelayCause = NS["DelayCause"]
DelayType = NS["DelayType"]


def calendar_days(start, end):
    return (end - start).days


def union_days(ranges):
    """Count calendar days in the union of half-open [start, end) ranges."""
    covered = set()
    for start, end in ranges:
        day = start
        while day < end:
            covered.add(day.date())
            day += timedelta(days=1)
    return len(covered)


class DelayAnalysisFallbackTest(unittest.TestCase):
    def setUp(self):
        self.contract = datetime(2024, 12, 31)
        self.analyzer = DelayAnalyzer("Office Tower", self.contract)

    def add_baseline(self, activities):
        self.analyzer.add_schedule_version(
            "baseline",
            "baseline",
            datetime(2024, 1, 1),
            self.contract,
            activities,
        )

    def add_event(
        self,
        event_id,
        start,
        end,
        delay_type=None,
        activities=None,
        cause=None,
    ):
        return self.analyzer.add_delay_event(
            event_id,
            "Owner-caused delay notice supplied with the claim",
            cause or DelayCause.LATE_INFORMATION,
            delay_type or DelayType.EXCUSABLE_COMPENSABLE,
            start,
            end,
            activities or ["F100"],
            "Owner",
        )

    def assert_no_compensable_award(self, report, damages):
        self.assertEqual(damages["contractor_delay_damages"], 0)
        self.assertEqual(damages["owner_liquidated_damages"], 0)
        self.assertEqual(damages["recommended_time_extension"], 0)
        self.assertEqual(damages["excusable_compensable_days"], 0)
        self.assertEqual(damages["excusable_non_compensable_days"], 0)
        self.assertEqual(damages["non_excusable_days"], 0)
        self.assertEqual(report.recommended_extension, 0)
        self.assertEqual(report.potential_damages, 0)
        self.assertEqual(report.excusable_delay, 0)
        self.assertEqual(report.non_excusable_delay, 0)
        self.assertEqual(report.concurrent_delay, 0)
        self.assertEqual(sum(report.delay_by_cause.values()), 0)
        self.assertEqual(sum(report.delay_by_party.values()), 0)

    def test_unanalyzed_notice_on_time_project_is_not_a_sixty_day_award(self):
        """Attack path: a 60-day notice never analyzed, project finished on time.

        Default daily cost is 15000, so counting the raw duration recommends
        60 days and 900000. Neither figure may be awarded.
        """
        start = datetime(2024, 4, 1)
        end = datetime(2024, 5, 31)
        self.assertEqual(calendar_days(start, end), 60)
        self.assertEqual(self.analyzer.daily_cost, 15000)
        self.add_baseline(
            {"F100": {"finish": datetime(2024, 9, 30), "is_critical": False}}
        )
        self.add_event("D-60", start, end)
        self.assertEqual(self.analyzer.delay_events["D-60"].calculated_impact, 0)

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(self.contract)
        markdown = self.analyzer.generate_report_markdown(report)

        self.assertEqual(report.total_delay, 0)
        self.assert_no_compensable_award(report, damages)
        self.assertIn("**Total Delay** | **0 days**", markdown)
        self.assertIn("**Recommended Time Extension:** 0 days", markdown)
        self.assertIn("**Potential Delay Damages:** $0", markdown)
        self.assertNotIn("**Recommended Time Extension:** 60 days", markdown)
        self.assertNotIn("$900,000", markdown)
        self.assertNotIn("**late_information**: 60 days", markdown)
        self.assertNotIn("**Owner**: 60 days", markdown)

    def test_two_nonoverlapping_unanalyzed_notices_are_not_summed(self):
        """Non-overlapping notices never reach the concurrency halving."""
        first = (datetime(2024, 4, 1), datetime(2024, 5, 1))
        second = (datetime(2024, 6, 1), datetime(2024, 7, 1))
        self.assertEqual(calendar_days(*first), 30)
        self.assertEqual(calendar_days(*second), 30)
        self.assertTrue(first[1] <= second[0])
        self.add_event("D-A", *first)
        self.add_event("D-B", *second)

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(self.contract)

        self.assertEqual(report.total_delay, 0)
        self.assert_no_compensable_award(report, damages)

    def test_unanalyzed_notice_is_excluded_even_when_overrun_could_absorb_it(self):
        """A cap alone must not keep an unanalyzed notice's raw duration."""
        start = datetime(2024, 4, 1)
        end = datetime(2024, 5, 31)
        self.add_event("D-60", start, end)
        actual = self.contract + timedelta(days=100)

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(actual)

        self.assertEqual(report.total_delay, 100)
        self.assert_no_compensable_award(report, damages)

    def test_analyzed_zero_impact_is_not_replaced_by_calendar_duration(self):
        """Time impact of a 4-day non-critical delay is 0 and must stay 0.

        The overrun is 100 days, so a later cap at the overrun still leaves
        room for the 4-day fallback. Both damage entry points must honor 0.
        """
        start = datetime(2024, 6, 1)
        end = datetime(2024, 6, 5)
        self.assertEqual(calendar_days(start, end), 4)
        self.add_baseline(
            {"F100": {"finish": datetime(2024, 6, 30), "is_critical": False}}
        )
        self.add_event("D-4", start, end)
        tia = self.analyzer.perform_time_impact_analysis("D-4")
        self.assertEqual(tia["schedule_impact_days"], 0)
        self.assertFalse(tia["critical_path_impact"])
        self.assertEqual(self.analyzer.delay_events["D-4"].calculated_impact, 0)

        actual = self.contract + timedelta(days=100)
        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(actual)

        self.assertEqual(report.total_delay, 100)
        self.assert_no_compensable_award(report, damages)

    def test_analyzed_zero_impact_when_named_activity_is_not_on_the_schedule(self):
        """An activity id supplied on the notice is non-critical when absent."""
        start = datetime(2024, 6, 1)
        end = datetime(2024, 6, 6)
        self.assertEqual(calendar_days(start, end), 5)
        self.add_baseline(
            {"A100": {"finish": datetime(2024, 6, 30), "is_critical": True}}
        )
        self.add_event("D-MISS", start, end, activities=["NOT-ON-SCHEDULE"])
        tia = self.analyzer.perform_time_impact_analysis("D-MISS")
        self.assertEqual(tia["schedule_impact_days"], 0)

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(
            self.contract + timedelta(days=100)
        )

        self.assert_no_compensable_award(report, damages)

    def test_zero_impact_non_excusable_does_not_inflate_liquidated_damages(self):
        start = datetime(2024, 6, 1)
        end = datetime(2024, 6, 5)
        self.add_baseline(
            {"F100": {"finish": datetime(2024, 6, 30), "is_critical": False}}
        )
        self.add_event(
            "D-NE",
            start,
            end,
            delay_type=DelayType.NON_EXCUSABLE,
            cause=DelayCause.SUBCONTRACTOR,
        )
        self.analyzer.perform_time_impact_analysis("D-NE")

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(
            self.contract + timedelta(days=100)
        )

        self.assert_no_compensable_award(report, damages)
        self.assertEqual(report.delay_by_cause.get("subcontractor", 0), 0)

    def test_zero_impact_time_only_delay_does_not_inflate_the_extension(self):
        start = datetime(2024, 6, 1)
        end = datetime(2024, 6, 5)
        self.add_baseline(
            {"F100": {"finish": datetime(2024, 6, 30), "is_critical": False}}
        )
        self.add_event(
            "D-ENC",
            start,
            end,
            delay_type=DelayType.EXCUSABLE_NON_COMPENSABLE,
            cause=DelayCause.WEATHER,
        )
        self.analyzer.perform_time_impact_analysis("D-ENC")

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(
            self.contract + timedelta(days=100)
        )

        self.assert_no_compensable_award(report, damages)
        self.assertEqual(report.delay_by_cause.get("weather", 0), 0)

    def test_zero_impact_concurrent_notices_are_not_halved_from_full_duration(self):
        """Concurrency runs after the impact is chosen, so a 0 must not become duration // 2."""
        first = (datetime(2024, 6, 1), datetime(2024, 6, 5))
        second = (datetime(2024, 6, 3), datetime(2024, 6, 7))
        self.add_baseline(
            {"F100": {"finish": datetime(2024, 6, 30), "is_critical": False}}
        )
        self.add_event("D-C1", *first)
        self.add_event("D-C2", *second)
        self.analyzer.perform_time_impact_analysis("D-C1")
        self.analyzer.perform_time_impact_analysis("D-C2")
        self.assertEqual(self.analyzer.delay_events["D-C1"].calculated_impact, 0)
        self.assertEqual(self.analyzer.delay_events["D-C2"].calculated_impact, 0)
        overlaps = self.analyzer.identify_concurrent_delays()
        self.assertEqual(len(overlaps), 1)
        self.assertGreater(overlaps[0][2], 0)

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(
            self.contract + timedelta(days=100)
        )

        self.assert_no_compensable_award(report, damages)

    def test_concurrent_type_zero_impact_is_not_counted_by_cause_or_party(self):
        start = datetime(2024, 6, 1)
        end = datetime(2024, 6, 5)
        self.add_baseline(
            {"F100": {"finish": datetime(2024, 6, 30), "is_critical": False}}
        )
        self.add_event(
            "D-CT",
            start,
            end,
            delay_type=DelayType.CONCURRENT,
            cause=DelayCause.COORDINATION,
        )
        self.analyzer.perform_time_impact_analysis("D-CT")
        self.assertEqual(self.analyzer.delay_events["D-CT"].calculated_impact, 0)

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(
            self.contract + timedelta(days=100)
        )

        self.assert_no_compensable_award(report, damages)
        self.assertEqual(report.delay_by_cause.get("coordination", 0), 0)

    def test_critical_impact_above_overrun_is_capped_on_the_report(self):
        """A real critical-path impact still cannot exceed actual overrun."""
        start = datetime(2024, 4, 1)
        end = datetime(2024, 5, 31)
        self.assertEqual(calendar_days(start, end), 60)
        self.add_baseline(
            {"A100": {"finish": datetime(2024, 6, 30), "is_critical": True}}
        )
        self.add_event(
            "D-CRIT", start, end, activities=["A100"], cause=DelayCause.OWNER_CHANGE
        )
        tia = self.analyzer.perform_time_impact_analysis("D-CRIT")
        self.assertEqual(tia["schedule_impact_days"], 60)
        actual = self.contract + timedelta(days=10)

        report = self.analyzer.generate_analysis_report(actual)
        markdown = self.analyzer.generate_report_markdown(report)

        self.assertEqual(report.total_delay, 10)
        self.assertEqual(report.recommended_extension, 10)
        self.assertEqual(report.excusable_delay, 10)
        self.assertEqual(report.delay_by_cause.get("owner_change", 0), 10)
        self.assertEqual(report.delay_by_party.get("Owner", 0), 10)
        self.assertEqual(report.potential_damages, 10 * self.analyzer.daily_cost)
        self.assertLessEqual(report.recommended_extension, report.total_delay)
        self.assertIn("**Recommended Time Extension:** 10 days", markdown)
        self.assertIn("**Potential Delay Damages:** $150,000", markdown)
        self.assertNotIn("**Recommended Time Extension:** 60 days", markdown)
        self.assertNotIn("$900,000", markdown)

    def test_on_time_and_early_completion_cap_the_award_at_zero(self):
        start = datetime(2024, 4, 1)
        end = datetime(2024, 5, 31)
        self.add_baseline(
            {"A100": {"finish": datetime(2024, 6, 30), "is_critical": True}}
        )
        self.add_event(
            "D-CRIT", start, end, activities=["A100"], cause=DelayCause.OWNER_CHANGE
        )
        tia = self.analyzer.perform_time_impact_analysis("D-CRIT")
        self.assertEqual(tia["schedule_impact_days"], 60)

        on_time = self.analyzer.generate_analysis_report(self.contract)
        on_time_md = self.analyzer.generate_report_markdown(on_time)
        self.assertEqual(on_time.total_delay, 0)
        self.assertEqual(on_time.recommended_extension, 0)
        self.assertEqual(on_time.potential_damages, 0)
        self.assertEqual(on_time.excusable_delay, 0)
        self.assertEqual(on_time.delay_by_cause.get("owner_change", 0), 0)
        self.assertEqual(on_time.delay_by_party.get("Owner", 0), 0)
        self.assertIn("**Recommended Time Extension:** 0 days", on_time_md)
        self.assertIn("**Potential Delay Damages:** $0", on_time_md)
        self.assertNotIn("$900,000", on_time_md)

        early_completion = self.contract - timedelta(days=30)
        early = self.analyzer.generate_analysis_report(early_completion)
        early_md = self.analyzer.generate_report_markdown(early)
        self.assertEqual(early.total_delay, -30)
        self.assertEqual(early.recommended_extension, 0)
        self.assertEqual(early.potential_damages, 0)
        self.assertEqual(early.excusable_delay, 0)
        self.assertIn("**Total Delay** | **-30 days**", early_md)
        self.assertIn("**Recommended Time Extension:** 0 days", early_md)
        self.assertIn("**Potential Delay Damages:** $0", early_md)

    def test_analyzed_noncritical_impact_is_capped_and_not_replaced(self):
        """Non-critical analysis uses duration minus the assumed 5-day float.

        On an on-time project that positive result is still capped to zero.
        When the overrun is larger, the award is the analyzed impact, not the
        original 60-day calendar duration.
        """
        start = datetime(2024, 4, 1)
        end = datetime(2024, 5, 31)
        duration = calendar_days(start, end)
        self.assertEqual(duration, 60)
        self.add_baseline(
            {"F100": {"finish": datetime(2024, 9, 30), "is_critical": False}}
        )
        self.add_event("D-NC", start, end)
        tia = self.analyzer.perform_time_impact_analysis("D-NC")
        analyzed = max(0, duration - 5)
        self.assertEqual(tia["schedule_impact_days"], analyzed)
        self.assertEqual(
            self.analyzer.delay_events["D-NC"].calculated_impact, analyzed
        )

        on_time = self.analyzer.generate_analysis_report(self.contract)
        self.assertEqual(on_time.total_delay, 0)
        self.assertEqual(on_time.recommended_extension, 0)
        self.assertEqual(on_time.potential_damages, 0)
        self.assertEqual(on_time.excusable_delay, 0)
        self.assertEqual(sum(on_time.delay_by_cause.values()), 0)

        within_overrun = self.analyzer.generate_analysis_report(
            self.contract + timedelta(days=100)
        )
        damages = self.analyzer.calculate_delay_damages()
        self.assertEqual(damages["excusable_compensable_days"], analyzed)
        self.assertEqual(damages["recommended_time_extension"], analyzed)
        self.assertEqual(
            damages["contractor_delay_damages"], analyzed * self.analyzer.daily_cost
        )
        self.assertNotEqual(damages["excusable_compensable_days"], duration)
        self.assertEqual(within_overrun.total_delay, 100)
        self.assertEqual(within_overrun.recommended_extension, analyzed)
        self.assertEqual(
            within_overrun.potential_damages, analyzed * self.analyzer.daily_cost
        )
        self.assertEqual(within_overrun.excusable_delay, analyzed)
        self.assertEqual(within_overrun.delay_by_cause["late_information"], analyzed)
        self.assertEqual(within_overrun.delay_by_party["Owner"], analyzed)

    def test_fully_overlapping_excusable_periods_are_counted_once(self):
        window = (datetime(2024, 4, 1), datetime(2024, 5, 31))
        self.assertEqual(calendar_days(*window), 60)
        self.add_baseline(
            {
                "A100": {"finish": datetime(2024, 6, 30), "is_critical": True},
                "A101": {"finish": datetime(2024, 6, 30), "is_critical": True},
            }
        )
        self.add_event("D-1", *window, activities=["A100"])
        self.add_event("D-2", *window, activities=["A101"])
        self.analyzer.perform_time_impact_analysis("D-1")
        self.analyzer.perform_time_impact_analysis("D-2")
        expected = union_days([window, window])
        self.assertEqual(expected, 60)

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(
            self.contract + timedelta(days=200)
        )

        self.assertEqual(damages["excusable_compensable_days"], expected)
        self.assertEqual(damages["recommended_time_extension"], expected)
        self.assertEqual(
            damages["contractor_delay_damages"], expected * self.analyzer.daily_cost
        )
        self.assertEqual(report.recommended_extension, expected)
        self.assertEqual(
            report.potential_damages, expected * self.analyzer.daily_cost
        )
        self.assertEqual(report.excusable_delay, expected)
        self.assertEqual(report.delay_by_cause["late_information"], expected)
        self.assertEqual(report.delay_by_party["Owner"], expected)

    def test_partially_overlapping_excusable_periods_use_the_union(self):
        first = (datetime(2024, 4, 1), datetime(2024, 6, 1))
        second = (datetime(2024, 5, 1), datetime(2024, 7, 1))
        self.assertEqual(calendar_days(*first), 61)
        self.assertEqual(calendar_days(*second), 61)
        expected = union_days([first, second])
        self.assertEqual(expected, 91)
        self.assertLess(expected, calendar_days(*first) + calendar_days(*second))
        self.add_baseline(
            {
                "A100": {"finish": datetime(2024, 6, 30), "is_critical": True},
                "A101": {"finish": datetime(2024, 6, 30), "is_critical": True},
            }
        )
        self.add_event("D-1", *first, activities=["A100"])
        self.add_event("D-2", *second, activities=["A101"])
        self.analyzer.perform_time_impact_analysis("D-1")
        self.analyzer.perform_time_impact_analysis("D-2")

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(
            self.contract + timedelta(days=200)
        )

        self.assertEqual(damages["excusable_compensable_days"], expected)
        self.assertEqual(damages["recommended_time_extension"], expected)
        self.assertEqual(
            damages["contractor_delay_damages"], expected * self.analyzer.daily_cost
        )
        self.assertEqual(report.recommended_extension, expected)
        self.assertEqual(report.excusable_delay, expected)
        self.assertEqual(
            report.potential_damages, expected * self.analyzer.daily_cost
        )
        self.assertEqual(report.delay_by_party["Owner"], expected)

    def test_nonoverlapping_critical_impacts_within_overrun_still_sum(self):
        """A genuine pair of critical impacts below the overrun is unchanged."""
        first = (datetime(2024, 4, 1), datetime(2024, 4, 11))
        second = (datetime(2024, 6, 1), datetime(2024, 6, 11))
        self.assertEqual(calendar_days(*first), 10)
        self.assertEqual(calendar_days(*second), 10)
        self.assertEqual(union_days([first, second]), 20)
        self.add_baseline(
            {
                "A100": {"finish": datetime(2024, 6, 30), "is_critical": True},
                "A101": {"finish": datetime(2024, 6, 30), "is_critical": True},
            }
        )
        self.add_event("D-1", *first, activities=["A100"])
        self.add_event("D-2", *second, activities=["A101"])
        self.analyzer.perform_time_impact_analysis("D-1")
        self.analyzer.perform_time_impact_analysis("D-2")

        damages = self.analyzer.calculate_delay_damages()
        report = self.analyzer.generate_analysis_report(
            self.contract + timedelta(days=40)
        )

        self.assertEqual(damages["excusable_compensable_days"], 20)
        self.assertEqual(damages["recommended_time_extension"], 20)
        self.assertEqual(damages["contractor_delay_damages"], 20 * 15000)
        self.assertEqual(report.total_delay, 40)
        self.assertEqual(report.recommended_extension, 20)
        self.assertEqual(report.potential_damages, 20 * 15000)
        self.assertEqual(report.excusable_delay, 20)

    def test_mixed_excusable_types_union_time_and_cap_money_separately(self):
        """Overlapping compensable and time-only windows share one extension.

        calculate_delay_damages has no completion date, so it returns the
        uncapped union: each category keeps its own analyzed days, and the
        recommended extension is the union of those windows. The claim report,
        which knows the actual completion, caps that extension and the
        compensable damages at the overrun.
        """
        compensable = (datetime(2024, 4, 1), datetime(2024, 5, 31))
        time_only = (datetime(2024, 5, 1), datetime(2024, 6, 30))
        self.assertEqual(calendar_days(*compensable), 60)
        self.assertEqual(calendar_days(*time_only), 60)
        payable_time = union_days([compensable, time_only])
        compensable_only = union_days([compensable])
        self.assertEqual(payable_time, 90)
        self.assertEqual(compensable_only, 60)
        self.add_baseline(
            {
                "A100": {"finish": datetime(2024, 6, 30), "is_critical": True},
                "A101": {"finish": datetime(2024, 6, 30), "is_critical": True},
            }
        )
        self.add_event("D-PAY", *compensable, activities=["A100"])
        self.add_event(
            "D-TIME",
            *time_only,
            activities=["A101"],
            delay_type=DelayType.EXCUSABLE_NON_COMPENSABLE,
            cause=DelayCause.WEATHER,
        )
        self.analyzer.perform_time_impact_analysis("D-PAY")
        self.analyzer.perform_time_impact_analysis("D-TIME")

        damages = self.analyzer.calculate_delay_damages()
        self.assertEqual(damages["excusable_compensable_days"], compensable_only)
        self.assertEqual(damages["excusable_non_compensable_days"], calendar_days(*time_only))
        self.assertEqual(damages["recommended_time_extension"], payable_time)
        self.assertEqual(
            damages["contractor_delay_damages"],
            compensable_only * self.analyzer.daily_cost,
        )

        overrun = 50
        report = self.analyzer.generate_analysis_report(
            self.contract + timedelta(days=overrun)
        )
        capped_time = min(payable_time, overrun)
        capped_money_days = min(compensable_only, overrun)
        self.assertEqual(report.total_delay, overrun)
        self.assertEqual(report.recommended_extension, capped_time)
        self.assertEqual(report.excusable_delay, capped_time)
        self.assertEqual(
            report.potential_damages,
            capped_money_days * self.analyzer.daily_cost,
        )
        self.assertEqual(sum(report.delay_by_cause.values()), capped_time)
        self.assertEqual(sum(report.delay_by_party.values()), capped_time)
        self.assertLessEqual(report.recommended_extension, overrun)
        self.assertLessEqual(
            report.potential_damages, overrun * self.analyzer.daily_cost
        )


if __name__ == "__main__":
    unittest.main()
