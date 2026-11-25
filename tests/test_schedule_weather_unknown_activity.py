"""Weather-sensitive schedules must not be cleared by an indoor fallback.

The schedule weather optimizer in
``5_DDC_Innovative/weather-impact-analysis/SKILL.md`` looks up
``ScheduledActivity.activity_type`` and, on a miss, substitutes the
``interior_work`` template. That template is indoor, so impact calculation
returns workable at full productivity and the report shows no delay.

These tests load that skill source and run ``analyze_schedule``,
``generate_weather_report``, and ``suggest_reschedule``. They fail while a
name that is not an exact template key is reported as safe in bad weather.

A fix passes when an unmatched type cannot produce that safe report:

- raise an error that names the activity type, or
- mark the activity unanalyzed (``unanalyzed`` true, or
  ``analysis_status`` of ``unanalyzed`` / ``unknown`` / ``rejected``) and
  do not attach a measured all-clear productivity, and say so in the
  workbook, or
- score the activity with a real outdoor template (including after
  normalizing case, spaces, and hyphens) so this forecast records a delay.

``ActivityWeatherAnalyzer.analyze_day`` and ``find_optimal_days`` do not use
the indoor fallback; they are not covered here.
"""

from __future__ import annotations

import re
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "5_DDC_Innovative"
    / "weather-impact-analysis"
    / "SKILL.md"
)

LOCATION = (40.7128, -74.0060)
START = date(2026, 1, 5)
DURATION_DAYS = 3


def load_skill():
    """Execute the skill's Python samples in one module, in document order."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.S)
    assert blocks, f"no Python samples in {SKILL_PATH}"
    module = types.ModuleType("weather_impact_skill_under_test")
    module.__file__ = str(SKILL_PATH)
    # dataclasses resolves the class namespace through sys.modules.
    sys.modules[module.__name__] = module
    for index, block in enumerate(blocks):
        exec(compile(block, f"{SKILL_PATH}#block{index}", "exec"), module.__dict__)
    return module


@pytest.fixture(scope="module")
def skill():
    return load_skill()


class ForecastWeather:
    """Stand-in for WeatherDataService.get_forecast.

    ``analyze_schedule`` and ``suggest_reschedule`` call
    ``get_forecast(latitude, longitude)`` and then score each scheduled day
    with the real analyzer. The forecast itself is fixed so the activity-type
    lookup is the only thing under test.
    """

    def __init__(self, days):
        self._days = list(days)

    def get_forecast(self, latitude, longitude, days=14):
        return list(self._days)


def _forecast_day(skill, day, *, temp_low, temp_high, precip, condition):
    return skill.DailyForecast(
        date=day,
        temp_high=temp_high,
        temp_low=temp_low,
        sunrise=datetime.combine(day, datetime.min.time().replace(hour=6)),
        sunset=datetime.combine(day, datetime.min.time().replace(hour=18)),
        precipitation_total=precip,
        precipitation_probability=95,
        primary_condition=condition,
        severity=skill.WeatherSeverity.SEVERE,
    )


def severe_forecast(skill, start=START, count=10):
    """Cold heavy rain, then a clear window.

    Every outdoor template in the skill loses productivity on the cold rain
    days. Exact ``interior_work`` stays at full productivity. The clear days
    give ``suggest_reschedule`` a better window than the planned start.
    """
    days = []
    for offset in range(count):
        day = start + timedelta(days=offset)
        if offset < 4:
            days.append(
                _forecast_day(
                    skill,
                    day,
                    temp_low=-8,
                    temp_high=1,
                    precip=30,
                    condition=skill.WeatherCondition.HEAVY_RAIN,
                )
            )
        else:
            days.append(
                _forecast_day(
                    skill,
                    day,
                    temp_low=15,
                    temp_high=22,
                    precip=0,
                    condition=skill.WeatherCondition.CLEAR,
                )
            )
    return days


def optimizer_for(skill, forecast):
    return skill.ScheduleWeatherOptimizer(
        ForecastWeather(forecast),
        skill.ActivityWeatherAnalyzer(),
    )


def scheduled(skill, activity_type, name="Level 3 work"):
    end = START + timedelta(days=DURATION_DAYS - 1)
    return skill.ScheduledActivity(
        activity_id="A-100",
        activity_name=name,
        activity_type=activity_type,
        planned_start=START,
        planned_end=end,
        duration_days=DURATION_DAYS,
        is_critical=True,
    )


def _is_unanalyzed(activity):
    if activity.get("unanalyzed") is True:
        return True
    status = str(activity.get("analysis_status", "")).lower()
    return status in {"unanalyzed", "unknown", "rejected", "error"}


def _identifies_type(exc, activity_type):
    blob = " ".join(str(arg) for arg in getattr(exc, "args", ()))
    blob = f"{blob} {exc}".lower()
    if activity_type is None:
        return "none" in blob
    if str(activity_type) == "":
        return any(word in blob for word in ("empty", "blank", "missing", "activity type"))
    return str(activity_type).lower() in blob


def _is_measured_all_clear(activity):
    days = activity.get("day_analysis") or []
    productivity = activity.get("avg_productivity")
    delay = activity.get("expected_delay")
    if not days or productivity is None or delay is None:
        return False
    if _is_unanalyzed(activity):
        return False
    return (
        delay == 0
        and abs(float(productivity) - 1.0) < 1e-9
        and all(
            day.get("can_work") is True and abs(float(day.get("productivity", 0)) - 1.0) < 1e-9
            for day in days
        )
    )


def _analyze(optimizer, activity):
    try:
        analysis = optimizer.analyze_schedule([activity], LOCATION)
    except Exception as exc:
        if not _identifies_type(exc, activity.activity_type):
            raise AssertionError(
                f"refusal did not name activity type {activity.activity_type!r}: {exc!r}"
            ) from exc
        return None
    assert analysis["activities"], "analyze_schedule returned no activity row"
    row = analysis["activities"][0]
    if _is_unanalyzed(row):
        assert row.get("avg_productivity") is None
        assert row.get("expected_delay") is None
        assert not any(day.get("can_work") is True for day in row.get("day_analysis") or [])
        return None
    return row


def _assert_not_cleared(row):
    assert row is None or not _is_measured_all_clear(row), (
        "unmatched activity was reported workable at full productivity with "
        f"no delay: {row}"
    )


def _canonical_row(skill, forecast, activity_type):
    row = _analyze(
        optimizer_for(skill, forecast),
        scheduled(skill, activity_type, name=activity_type),
    )
    assert row is not None, f"{activity_type} was refused; the template should score"
    assert not _is_measured_all_clear(row), (
        f"{activity_type} was all-clear on a severe forecast; the fixture is wrong"
    )
    return row


@pytest.mark.parametrize(
    "activity_type",
    [
        "Concrete Placement",
        "concrete-placement",
        "CONCRETE_PLACEMENT",
        "Concrete-Placement",
    ],
)
def test_concrete_alias_is_not_reported_fully_workable(skill, activity_type):
    forecast = severe_forecast(skill)
    canonical = _canonical_row(skill, forecast, "concrete_placement")
    row = _analyze(
        optimizer_for(skill, forecast),
        scheduled(skill, activity_type, name="Slab pour"),
    )
    _assert_not_cleared(row)
    if row is not None:
        assert row["avg_productivity"] <= canonical["avg_productivity"]
        assert row["expected_delay"] >= canonical["expected_delay"]
        assert any(not day["can_work"] for day in row["day_analysis"])


@pytest.mark.parametrize(
    "alias, canonical",
    [
        ("Roofing Work", "roofing"),
        ("steel erection", "steel_erection"),
        ("Crane Operations", "crane_operations"),
        ("exterior painting", "painting_exterior"),
    ],
)
def test_other_weather_sensitive_aliases_are_not_cleared(skill, alias, canonical):
    forecast = severe_forecast(skill)
    canonical_row = _canonical_row(skill, forecast, canonical)
    row = _analyze(
        optimizer_for(skill, forecast),
        scheduled(skill, alias, name=alias),
    )
    _assert_not_cleared(row)
    if row is not None:
        assert row["avg_productivity"] <= canonical_row["avg_productivity"] + 1e-9
        assert row["expected_delay"] >= canonical_row["expected_delay"]


@pytest.mark.parametrize("activity_type", ["not_a_real_trade", "", None])
def test_unknown_activity_type_is_not_indoor_all_clear(skill, activity_type):
    forecast = severe_forecast(skill)
    row = _analyze(
        optimizer_for(skill, forecast),
        scheduled(skill, activity_type, name="Unlisted work"),
    )
    _assert_not_cleared(row)
    if row is not None:
        outdoor = _most_permissive_outdoor_productivity(skill, forecast)
        assert row["avg_productivity"] <= outdoor
        assert row["expected_delay"] > 0


def test_exact_templates_keep_their_weather_rules(skill):
    forecast = severe_forecast(skill)
    concrete = _canonical_row(skill, forecast, "concrete_placement")
    assert concrete["expected_delay"] >= DURATION_DAYS
    assert concrete["avg_productivity"] == 0

    interior = _analyze(
        optimizer_for(skill, forecast),
        scheduled(skill, "interior_work", name="Fit-out"),
    )
    assert interior is not None
    assert _is_measured_all_clear(interior)


def test_mixed_schedule_does_not_clear_the_unmatched_activity(skill):
    forecast = severe_forecast(skill)
    concrete = scheduled(skill, "concrete_placement", name="Known pour")
    disguised = scheduled(skill, "Concrete Placement", name="Disguised pour")
    optimizer = optimizer_for(skill, forecast)
    try:
        analysis = optimizer.analyze_schedule([concrete, disguised], LOCATION)
    except Exception as exc:
        assert _identifies_type(exc, "Concrete Placement")
        return

    by_name = {row["activity_name"]: row for row in analysis["activities"]}
    assert not _is_measured_all_clear(by_name["Known pour"])
    disguised_row = by_name["Disguised pour"]
    if _is_unanalyzed(disguised_row):
        assert disguised_row.get("avg_productivity") is None
        assert disguised_row.get("expected_delay") is None
    else:
        assert not _is_measured_all_clear(disguised_row)
        assert disguised_row["expected_delay"] > 0


def test_suggest_reschedule_does_not_skip_unmatched_concrete(skill):
    forecast = severe_forecast(skill)
    canonical = scheduled(skill, "concrete_placement", name="Known pour")
    alias = scheduled(skill, "Concrete Placement", name="Disguised pour")
    known = optimizer_for(skill, forecast).suggest_reschedule(
        canonical, LOCATION, flexibility_days=7
    )
    assert isinstance(known, date)
    assert known != START

    try:
        suggestion = optimizer_for(skill, forecast).suggest_reschedule(
            alias, LOCATION, flexibility_days=7
        )
    except Exception as exc:
        assert _identifies_type(exc, "Concrete Placement")
        return

    assert suggestion is not None, (
        "suggest_reschedule returned no suggestion, so the unsafe planned "
        "dates stay in place"
    )
    if isinstance(suggestion, date):
        assert suggestion != START
        assert abs((suggestion - known).days) <= 7
        return
    blob = str(suggestion).lower()
    assert "unanalyzed" in blob or "unknown" in blob


def test_suggest_reschedule_does_not_skip_unknown_type(skill):
    activity = scheduled(skill, "not_a_real_trade", name="Unlisted work")
    try:
        suggestion = optimizer_for(skill, severe_forecast(skill)).suggest_reschedule(
            activity, LOCATION, flexibility_days=7
        )
    except Exception as exc:
        assert _identifies_type(exc, "not_a_real_trade")
        return
    assert suggestion is not None, (
        "suggest_reschedule returned no suggestion, so the unsafe planned "
        "dates stay in place"
    )
    if isinstance(suggestion, date):
        assert suggestion != START
        return
    blob = str(suggestion).lower()
    assert "unanalyzed" in blob or "unknown" in blob


def test_weather_report_does_not_show_full_productivity_for_alias(skill, tmp_path):
    _assert_report_does_not_clear(
        skill,
        scheduled(skill, "Concrete Placement", name="Disguised pour"),
        tmp_path / "alias.xlsx",
        "Concrete Placement",
    )


def test_weather_report_does_not_show_full_productivity_for_unknown(skill, tmp_path):
    _assert_report_does_not_clear(
        skill,
        scheduled(skill, "not_a_real_trade", name="Unlisted work"),
        tmp_path / "unknown.xlsx",
        "not_a_real_trade",
    )


def test_weather_report_still_scores_exact_concrete_and_interior(skill, tmp_path):
    import pandas as pd

    forecast = severe_forecast(skill)
    path = tmp_path / "exact.xlsx"
    optimizer_for(skill, forecast).generate_weather_report(
        [
            scheduled(skill, "concrete_placement", name="Known pour"),
            scheduled(skill, "interior_work", name="Fit-out"),
        ],
        LOCATION,
        str(path),
    )
    activities = pd.read_excel(path, sheet_name="Activities")
    by_name = {
        row["Activity"]: row for _, row in activities.iterrows()
    }
    assert by_name["Known pour"]["Avg Productivity"] != "100%"
    assert by_name["Known pour"]["Expected Delay"] != "0 days"
    assert by_name["Fit-out"]["Avg Productivity"] == "100%"
    assert by_name["Fit-out"]["Expected Delay"] == "0 days"


def _assert_report_does_not_clear(skill, activity, path, activity_type):
    import pandas as pd

    try:
        optimizer_for(skill, severe_forecast(skill)).generate_weather_report(
            [activity], LOCATION, str(path)
        )
    except Exception as exc:
        assert not path.exists(), "workbook was created before the report refused the activity"
        assert _identifies_type(exc, activity_type)
        return

    assert path.exists()
    activities = pd.read_excel(path, sheet_name="Activities")
    matches = activities[activities["Activity"] == activity.activity_name]
    assert not matches.empty
    row = matches.iloc[0]
    productivity = str(row["Avg Productivity"])
    delay = str(row["Expected Delay"])
    combined = f"{productivity} {delay}".lower()
    assert not (productivity == "100%" and delay == "0 days")
    assert (
        "unanalyzed" in combined
        or "unknown" in combined
        or delay != "0 days"
    )


def _most_permissive_outdoor_productivity(skill, forecast):
    analyzer = skill.ActivityWeatherAnalyzer()
    planned = [day for day in forecast if START <= day.date <= START + timedelta(days=DURATION_DAYS - 1)]
    productivities = []
    for key, template in analyzer.ACTIVITY_TEMPLATES.items():
        if key == "interior_work" or template.indoor:
            continue
        scores = [
            analyzer._calculate_impact(day, template)["productivity"]
            for day in planned
        ]
        productivities.append(sum(scores) / len(scores))
    assert productivities
    return max(productivities)
