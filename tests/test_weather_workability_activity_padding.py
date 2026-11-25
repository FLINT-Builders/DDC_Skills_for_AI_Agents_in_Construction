"""Workability scores must not fall when the activity list contains names
that ConstructionWeatherRisk has no thresholds for.

The implementation under test is the Python block in the weather-api skill.
These tests execute that block. They fail while a fully stopped day can be
reported as a milder band because unknown names enlarge the denominator.
"""

import unittest
from datetime import datetime
from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "2_DDC_Book"
    / "2.2-Open-Data-Standards"
    / "weather-api"
    / "SKILL.md"
)

RISK_RANK = {"low": 0, "moderate": 1, "high": 2, "critical": 3}


def load_skill_namespace():
    """Execute the skill's implementation block and return its globals."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    code = text[start:end]
    namespace = {"__name__": "weather_api_skill"}
    exec(compile(code, str(SKILL_PATH), "exec"), namespace)
    return namespace


class WorkabilityActivityPaddingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = load_skill_namespace()
        cls.WeatherCondition = ns["WeatherCondition"]
        cls.WeatherRisk = ns["WeatherRisk"]
        cls.risk = ns["ConstructionWeatherRisk"]()
        cls.threshold_names = list(cls.risk.THRESHOLDS)

    def condition(self, *, hour, temperature, wind_speed, precipitation):
        return self.WeatherCondition(
            timestamp=datetime(2026, 11, 25, hour, 0),
            temperature=temperature,
            humidity=80.0,
            wind_speed=wind_speed,
            precipitation=precipitation,
            conditions="fixture",
        )

    def storm(self):
        """Heavy rain and high wind. Fails every published activity threshold."""
        return self.condition(hour=12, temperature=15.0, wind_speed=25.0, precipitation=25.0)

    def light_rain(self):
        """0.1 mm fails paint and roofing only. Concrete, crane, and earthwork pass."""
        return self.condition(hour=12, temperature=20.0, wind_speed=3.0, precipitation=0.1)

    def clear(self):
        return self.condition(hour=12, temperature=20.0, wind_speed=2.0, precipitation=0.0)

    def assert_not_understated(self, condition, activities):
        known = [name for name in activities if name in self.risk.THRESHOLDS]
        self.assertTrue(known, "fixture must include at least one threshold activity")
        baseline = self.risk.assess_workability(condition, known)
        reported = self.risk.assess_workability(condition, activities)
        self.assertGreaterEqual(
            RISK_RANK[reported.risk_level.value],
            RISK_RANK[baseline.risk_level.value],
            msg=(
                f"padded list {activities!r} reported {reported.risk_level.value} "
                f"({reported.workable_hours}h); checked activities {known!r} are "
                f"{baseline.risk_level.value} ({baseline.workable_hours}h)"
            ),
        )
        self.assertLessEqual(reported.workable_hours, baseline.workable_hours)

    def test_each_real_activity_alone_is_critical_in_the_storm(self):
        for name in ("concrete_pour", "crane_work", "exterior_paint", "roofing", "earthwork"):
            with self.subTest(activity=name):
                assessment = self.risk.assess_workability(self.storm(), [name])
                self.assertEqual(assessment.risk_level, self.WeatherRisk.CRITICAL)
                self.assertEqual(assessment.workable_hours, 0)
                self.assertEqual(assessment.affected_activities, [name])

    def test_default_activity_list_is_critical_in_the_storm(self):
        assessment = self.risk.assess_workability(self.storm(), None)
        self.assertEqual(assessment.risk_level, self.WeatherRisk.CRITICAL)
        self.assertEqual(assessment.workable_hours, 0)
        self.assertCountEqual(assessment.affected_activities, self.threshold_names)

    def test_clear_day_with_only_known_activities_stays_workable(self):
        assessment = self.risk.assess_workability(self.clear(), self.threshold_names)
        self.assertEqual(assessment.risk_level, self.WeatherRisk.LOW)
        self.assertEqual(assessment.workable_hours, 8)
        self.assertEqual(assessment.affected_activities, [])

    def test_one_stopped_activity_plus_two_unknown_names_stays_critical(self):
        """Attack path: one real name and two names that are not in the table."""
        cases = {
            "concrete_pour": ["office_coordination", "submittal_review"],
            "crane_work": ["site_walk", "lookahead_meeting"],
            "roofing": ["submittal", "rfi_review"],
            "earthwork": ["material_delivery", "crew_coordination"],
            "exterior_paint": ["indoor_finish", "inspection"],
        }
        storm = self.storm()
        for real_name, unknown_names in cases.items():
            with self.subTest(activity=real_name):
                padded = [real_name, *unknown_names]
                assessment = self.risk.assess_workability(storm, padded)
                self.assertEqual(assessment.risk_level, self.WeatherRisk.CRITICAL)
                self.assertEqual(assessment.workable_hours, 0)
                self.assert_not_understated(storm, padded)
                self.assertIn(real_name, assessment.affected_activities)

    def test_unknown_names_before_and_between_real_names_do_not_lower_a_full_stop(self):
        padded = [
            "lookahead_meeting",
            "concrete_pour",
            "crane_work",
            "rfi_review",
            "roofing",
            "earthwork",
            "exterior_paint",
            "site_orientation",
        ]
        assessment = self.risk.assess_workability(self.storm(), padded)
        self.assertEqual(assessment.risk_level, self.WeatherRisk.CRITICAL)
        self.assertEqual(assessment.workable_hours, 0)
        self.assert_not_understated(self.storm(), padded)

    def test_partial_stop_is_not_diluted_by_unknown_names(self):
        checked = ["exterior_paint", "concrete_pour"]
        padded = ["exterior_paint", "lookahead_meeting", "concrete_pour", "rfi_review"]
        rain = self.light_rain()
        baseline = self.risk.assess_workability(rain, checked)
        self.assertEqual(baseline.risk_level, self.WeatherRisk.HIGH)
        self.assertEqual(baseline.workable_hours, 4)
        self.assertEqual(baseline.affected_activities, ["exterior_paint"])
        reported = self.risk.assess_workability(rain, padded)
        self.assertGreaterEqual(RISK_RANK[reported.risk_level.value], RISK_RANK["high"])
        self.assertLessEqual(reported.workable_hours, 4)
        self.assert_not_understated(rain, padded)

    def test_four_of_five_stopped_activities_stay_critical_when_padded(self):
        """Precipitation of 6 mm stops every activity except earthwork (80%)."""
        weather = self.condition(hour=12, temperature=20.0, wind_speed=5.0, precipitation=6.0)
        checked = self.threshold_names
        padded = [*checked, "lookahead_meeting", "rfi_review"]
        baseline = self.risk.assess_workability(weather, checked)
        self.assertEqual(baseline.risk_level, self.WeatherRisk.CRITICAL)
        self.assertEqual(baseline.workable_hours, 0)
        reported = self.risk.assess_workability(weather, padded)
        self.assertEqual(reported.risk_level, self.WeatherRisk.CRITICAL)
        self.assertEqual(reported.workable_hours, 0)
        self.assert_not_understated(weather, padded)

    def test_weekly_forecast_uses_the_same_band_for_a_padded_list(self):
        storm = self.storm()
        padded = ["concrete_pour", "office_coordination", "submittal_review"]
        frame = self.risk.weekly_forecast_risk([storm], padded)
        self.assertEqual(len(frame), 1)
        row = frame.iloc[0]
        self.assertEqual(row["risk_level"], "critical")
        self.assertEqual(int(row["workable_hours"]), 0)

        checked_frame = self.risk.weekly_forecast_risk([storm], ["concrete_pour"])
        self.assertEqual(checked_frame.iloc[0]["risk_level"], "critical")
        self.assertEqual(int(checked_frame.iloc[0]["workable_hours"]), 0)

    def test_weekly_forecast_midday_storm_is_not_diluted(self):
        """A clear early hour must not change the midday representative."""
        padded = ["roofing", "crane_work", "submittal_review", "site_walk"]
        conditions = [
            self.condition(hour=3, temperature=20.0, wind_speed=2.0, precipitation=0.0),
            self.storm(),
        ]
        frame = self.risk.weekly_forecast_risk(conditions, padded)
        self.assertEqual(len(frame), 1)
        row = frame.iloc[0]
        self.assertEqual(row["risk_level"], "critical")
        self.assertEqual(int(row["workable_hours"]), 0)


if __name__ == "__main__":
    unittest.main()
