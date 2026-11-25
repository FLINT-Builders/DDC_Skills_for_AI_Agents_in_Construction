"""Weather history must advance one day at a time and stop at a fixed cap.

These tests load the open-data integrator implementation from its skill
file and call WeatherDataConnector.fetch, the method that walks a schedule
from start_date through end_date.

They fail while that walk sets the cursor to the first of the next month
(or to January 1 of the same year on or after December 28) and only then
slices the list. A passing run requires both:

- the cursor advances with timedelta(days=1), including across month and
  year boundaries and across 29 February in a leap year
- at most MAX_HISTORY_DAYS rows are built, and a longer range returns the
  leading days only

MAX_HISTORY_DAYS is 30 because that is the limit already written on the
return of fetch. The cap has to apply before another row is constructed.
Building the whole range and slicing afterward still fails.

fetch_data, enrich_project_data, and get_weather_risk are not exercised.
Each of them passes date objects through json.dumps before calling fetch,
and that call raises TypeError, so those operations never reach the walk.
"""

from __future__ import annotations

import re
import signal
import sys
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "2_DDC_Book"
    / "2.2-Open-Data-Standards"
    / "open-data-integrator"
    / "SKILL.md"
)

# Same bound as the records[:30] limit on WeatherDataConnector.fetch.
MAX_HISTORY_DAYS = 30


class HistoryRunaway(BaseException):
    """The history walk built another row past the fixed cap, or did not return."""


def _load_skill():
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    implementation = next(
        (block for block in blocks if "class WeatherDataConnector" in block),
        None,
    )
    if implementation is None:
        raise AssertionError(f"no WeatherDataConnector implementation in {SKILL_PATH}")

    # The skill imports requests and never calls it. Keep the import from
    # failing in an environment that does not have the package installed.
    if "requests" not in sys.modules:
        try:
            import requests  # noqa: F401
        except ImportError:
            stub = types.ModuleType("requests")

            def _refuse(name):
                raise AssertionError(
                    f"weather history must not call requests.{name}"
                )

            stub.__getattr__ = _refuse
            sys.modules["requests"] = stub

    # dataclasses looks up the class module in sys.modules while building fields.
    module = types.ModuleType("open_data_integrator_skill")
    module.__file__ = str(SKILL_PATH)
    sys.modules[module.__name__] = module
    exec(compile(implementation, str(SKILL_PATH), "exec"), module.__dict__)
    return module.__dict__


class WeatherHistoryYearEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = _load_skill()

    def setUp(self):
        connector_type = self.namespace["WeatherDataConnector"]
        self.connector = connector_type()

    def fetch(self, params):
        namespace = self.namespace
        original = namespace["DataRecord"]
        built = 0

        class CountingRecord(original):
            def __init__(self, *args, **kwargs):
                nonlocal built
                built += 1
                if built > MAX_HISTORY_DAYS:
                    raise HistoryRunaway(
                        f"history loop built {built} rows; "
                        f"limit is {MAX_HISTORY_DAYS}"
                    )
                super().__init__(*args, **kwargs)

        namespace["DataRecord"] = CountingRecord

        def _alarm(signum, frame):
            raise HistoryRunaway("fetch did not return")

        previous = signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        try:
            try:
                return self.connector.fetch(params)
            except HistoryRunaway as exc:
                self.fail(str(exc))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            namespace["DataRecord"] = original

    def assert_daily_prefix(self, records, start, end, lat=1.0, lon=2.0):
        if end < start:
            expected = []
        else:
            span = (end - start).days + 1
            count = min(span, MAX_HISTORY_DAYS)
            expected = [start + timedelta(days=offset) for offset in range(count)]

        self.assertEqual(
            [record.data["date"] for record in records],
            [day.isoformat() for day in expected],
        )
        for record, day in zip(records, expected):
            self.assertEqual(
                record.timestamp, datetime.combine(day, datetime.min.time())
            )
            self.assertEqual(record.source_id, "openweathermap")
            self.assertEqual(record.metadata.get("lat"), lat)
            self.assertEqual(record.metadata.get("lon"), lon)
            self.assertGreaterEqual(day, start)
            self.assertLessEqual(day, end)

    def test_december_28_only_returns_that_day(self):
        start = end = date(2024, 12, 28)
        records = self.fetch(
            {"lat": 1.0, "lon": 2.0, "start_date": start, "end_date": end}
        )
        self.assert_daily_prefix(records, start, end)

    def test_december_28_through_31_returns_each_day_once(self):
        start = date(2024, 12, 28)
        end = date(2024, 12, 31)
        records = self.fetch(
            {"lat": 1.0, "lon": 2.0, "start_date": start, "end_date": end}
        )
        self.assert_daily_prefix(records, start, end)

    def test_schedule_crossing_new_year_uses_the_next_year(self):
        start = date(2024, 12, 20)
        end = date(2025, 1, 8)
        records = self.fetch(
            {"lat": 1.0, "lon": 2.0, "start_date": start, "end_date": end}
        )
        self.assert_daily_prefix(records, start, end)
        self.assertIn("2025-01-01", [record.data["date"] for record in records])
        self.assertNotIn("2024-01-01", [record.data["date"] for record in records])

    def test_december_window_stays_in_december_until_the_cap(self):
        start = date(2024, 12, 1)
        end = date(2024, 12, 31)
        records = self.fetch(
            {"lat": 1.0, "lon": 2.0, "start_date": start, "end_date": end}
        )
        self.assert_daily_prefix(records, start, end)
        self.assertEqual(records[-1].data["date"], "2024-12-30")

    def test_year_end_project_schedule_stops_after_thirty_days(self):
        # Same keys enrich_project_data and get_weather_risk pass to fetch:
        # lat, lon, start_date, end_date from the caller's schedule.
        project = {
            "location": {"lat": 40.7128, "lon": -74.0060},
            "start_date": date(2024, 6, 1),
            "end_date": date(2024, 12, 31),
        }
        location = project["location"]
        params = {
            "lat": location.get("lat"),
            "lon": location.get("lon"),
            "start_date": project.get("start_date", date.today()),
            "end_date": project.get("end_date", date.today()),
        }
        records = self.fetch(params)
        self.assert_daily_prefix(
            records,
            date(2024, 6, 1),
            date(2024, 12, 31),
            lat=40.7128,
            lon=-74.0060,
        )
        self.assertEqual(len(records), MAX_HISTORY_DAYS)
        self.assertEqual(records[0].data["date"], "2024-06-01")
        self.assertEqual(records[-1].data["date"], "2024-06-30")

    def test_january_includes_the_31st(self):
        start = date(2024, 1, 27)
        end = date(2024, 2, 2)
        records = self.fetch(
            {"lat": 1.0, "lon": 2.0, "start_date": start, "end_date": end}
        )
        self.assert_daily_prefix(records, start, end)

    def test_leap_day_is_included(self):
        start = date(2024, 2, 27)
        end = date(2024, 3, 2)
        records = self.fetch(
            {"lat": 1.0, "lon": 2.0, "start_date": start, "end_date": end}
        )
        self.assert_daily_prefix(records, start, end)
        self.assertIn("2024-02-29", [record.data["date"] for record in records])

    def test_november_does_not_skip_to_december(self):
        start = date(2024, 11, 28)
        end = date(2024, 12, 2)
        records = self.fetch(
            {"lat": 1.0, "lon": 2.0, "start_date": start, "end_date": end}
        )
        self.assert_daily_prefix(records, start, end)

    def test_range_ending_on_december_27_returns_every_day(self):
        start = date(2024, 12, 1)
        end = date(2024, 12, 27)
        records = self.fetch(
            {"lat": 1.0, "lon": 2.0, "start_date": start, "end_date": end}
        )
        self.assert_daily_prefix(records, start, end)

    def test_end_before_start_returns_no_rows(self):
        start = date(2024, 12, 31)
        end = date(2024, 12, 1)
        records = self.fetch(
            {"lat": 1.0, "lon": 2.0, "start_date": start, "end_date": end}
        )
        self.assert_daily_prefix(records, start, end)


if __name__ == "__main__":
    unittest.main()
