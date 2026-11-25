"""Compliance must use each limit's own averaging window.

The implementation under test is the Python block in
``5_DDC_Innovative/environmental-monitoring/SKILL.md``. These tests execute
that block. They fail while ``check_compliance`` averages every reading in the
caller-supplied range and then labels that mean with the limit's regulatory
period (for example "8-hour avg").

Required behavior for a limit whose ``averaging_period_hours`` is H:

- If H == 0, ``measured_value`` is the maximum reading in the requested range
  and the status is based on that maximum. This is the ceiling / instantaneous
  path already used for the OSHA CO ceiling.
- If H > 0, ``measured_value`` is the highest arithmetic mean of the readings
  that fall inside any single window of length H contained in the requested
  range. Anchor each window on a reading, either ending at that timestamp and
  looking back H hours, or starting at that timestamp and looking forward H
  hours. Readings on the window boundary belong to the window. Do not include
  readings that fall outside that window, and do not average the rest of the
  requested range into it.
- Status stays exceedance at ``measured_value >= limit``, warning at
  ``measured_value >= 0.9 * limit``, otherwise compliant.
- ``generate_daily_report`` must use that result for the calendar day. A quiet
  overnight sample must not turn an 8-hour exceedance into a compliant day.

``_calculate_twa`` / ``_check_limits`` are not this bug: they average only the
trailing window that ends at ``datetime.now()``. ``generate_report``,
``get_current_conditions``, and ``get_exceedance_summary`` do not call
``check_compliance``. Those paths are intentionally not asserted here.
``record_batch`` only forwards each item to ``record_reading``; the
particulate test enters through ``record_batch`` so that path is exercised.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path


DAY = datetime(2026, 6, 15)
SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "5_DDC_Innovative"
    / "environmental-monitoring"
    / "SKILL.md"
)


def load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(text[start:end], str(SKILL_PATH), "exec"), namespace)
    return namespace


class EnvironmentalComplianceAveragingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = load_skill_namespace()
        cls.EnvironmentalMonitor = ns["EnvironmentalMonitor"]
        cls.ParameterType = ns["ParameterType"]
        cls.ComplianceStatus = ns["ComplianceStatus"]

    def setUp(self):
        self.monitor = self.EnvironmentalMonitor("Perimeter Site")

    def _station(self, parameters):
        return self.monitor.add_station(
            "STA-001",
            "North Perimeter",
            {"description": "North fence line"},
            parameters,
        )

    def _record_hourly(self, parameter, unit, value_at_hour, days=1):
        for day_offset in range(days):
            for hour in range(24):
                self.monitor.record_reading(
                    "STA-001",
                    parameter,
                    value_at_hour(day_offset, hour),
                    unit,
                    DAY + timedelta(days=day_offset, hours=hour),
                )

    def _records(self, start, end):
        return self.monitor.check_compliance(start, end)

    def _record(self, records, parameter, limit_value, regulation):
        matches = [
            record
            for record in records
            if record.parameter == parameter
            and record.limit_value == limit_value
            and record.regulation == regulation
        ]
        self.assertEqual(
            len(matches),
            1,
            f"expected one {regulation} {parameter} limit {limit_value}, got {matches}",
        )
        return matches[0]

    def test_daily_report_shift_over_noise_limit_is_not_diluted_by_quiet_hours(self):
        """Work-shift noise at 88 dBA exceeds the OSHA 8-hour 85 dBA limit.

        Hourly samples from 08:00 through 16:00 are 88 dBA. Every other hour
        of that calendar day is 40 dBA, and the following day is 40 dBA all
        day. The mean of the first calendar day is 58 dBA. That mean is what
        the report currently compares with the 85 dBA limit while calling the
        result an 8-hour average, so the day is reported compliant.
        """
        noise = self.ParameterType.NOISE
        self._station([noise])
        self._record_hourly(
            noise,
            "dBA",
            lambda day, hour: 88.0 if day == 0 and 8 <= hour <= 16 else 40.0,
            days=2,
        )

        report = self.monitor.generate_daily_report(DAY)

        self.assertEqual(report.readings_collected, 24)
        self.assertEqual(report.compliance_status, self.ComplianceStatus.EXCEEDANCE)
        self.assertEqual(report.exceedances, 1)
        self.assertEqual(report.summary["noise"]["exceedances"], 1)

        day_records = self._records(DAY, DAY + timedelta(days=1))
        eight_hour = self._record(day_records, noise, 85, "OSHA")
        self.assertEqual(eight_hour.status, self.ComplianceStatus.EXCEEDANCE)
        self.assertAlmostEqual(eight_hour.measured_value, 88.0)
        self.assertEqual(eight_hour.averaging_period, "8-hour avg")

        action_level = self._record(day_records, noise, 90, "OSHA")
        self.assertEqual(action_level.status, self.ComplianceStatus.WARNING)
        self.assertAlmostEqual(action_level.measured_value, 88.0)

        fifteen_min = self._record(day_records, noise, 115, "OSHA")
        self.assertEqual(fifteen_min.status, self.ComplianceStatus.COMPLIANT)
        self.assertAlmostEqual(fifteen_min.measured_value, 88.0)

        next_day = self.monitor.generate_daily_report(DAY + timedelta(days=1))
        self.assertEqual(next_day.compliance_status, self.ComplianceStatus.COMPLIANT)
        self.assertEqual(next_day.exceedances, 0)

    def test_check_compliance_over_two_days_still_uses_the_eight_hour_window(self):
        """A range longer than 8 hours must not be the OSHA 8-hour average."""
        noise = self.ParameterType.NOISE
        self._station([noise])
        self._record_hourly(
            noise,
            "dBA",
            lambda day, hour: 88.0 if day == 0 and 8 <= hour <= 16 else 40.0,
            days=2,
        )

        records = self._records(DAY, DAY + timedelta(days=2))
        eight_hour = self._record(records, noise, 85, "OSHA")

        self.assertEqual(eight_hour.status, self.ComplianceStatus.EXCEEDANCE)
        self.assertAlmostEqual(eight_hour.measured_value, 88.0)
        self.assertEqual(eight_hour.averaging_period, "8-hour avg")
        self.assertLess(eight_hour.measured_value, 90)

    def test_fifteen_minute_noise_limit_is_not_diluted_by_the_rest_of_the_day(self):
        """A 120 dBA pair inside one 15-minute window exceeds the 115 dBA limit.

        The rest of the day is 50 dBA, so the calendar-day mean is about
        55.6 dBA and the loudest 8-hour window is about 64 to 66 dBA. The
        15-minute limit has to use its own window: the two 120 dBA samples at
        12:00 and 12:10. Applying one mean to every noise limit cannot satisfy
        both the 15-minute exceedance and the 8-hour non-exceedance.
        """
        noise = self.ParameterType.NOISE
        self._station([noise])
        for hour in range(24):
            if hour == 12:
                continue
            self.monitor.record_reading(
                "STA-001",
                noise,
                50.0,
                "dBA",
                DAY + timedelta(hours=hour),
            )
        self.monitor.record_reading(
            "STA-001", noise, 120.0, "dBA", DAY + timedelta(hours=12)
        )
        self.monitor.record_reading(
            "STA-001",
            noise,
            120.0,
            "dBA",
            DAY + timedelta(hours=12, minutes=10),
        )

        records = self._records(DAY, DAY + timedelta(days=1))
        fifteen_min = self._record(records, noise, 115, "OSHA")
        self.assertEqual(fifteen_min.status, self.ComplianceStatus.EXCEEDANCE)
        self.assertAlmostEqual(fifteen_min.measured_value, 120.0)

        eight_hour = self._record(records, noise, 85, "OSHA")
        self.assertEqual(eight_hour.status, self.ComplianceStatus.COMPLIANT)
        self.assertLess(eight_hour.measured_value, 85)

        action_level = self._record(records, noise, 90, "OSHA")
        self.assertEqual(action_level.status, self.ComplianceStatus.COMPLIANT)
        self.assertLess(action_level.measured_value, 90)

        report = self.monitor.generate_daily_report(DAY)
        self.assertEqual(report.compliance_status, self.ComplianceStatus.EXCEEDANCE)

    def test_particulate_24_hour_limits_are_not_diluted_by_a_longer_range(self):
        """One day over the EPA 24-hour limit must fail even if the week is quiet.

        Day 0 is 80 µg/m³ PM2.5 and 200 µg/m³ PM10 every hour. The next six
        days are 10 µg/m³. The mean of the whole week is 20 µg/m³ PM2.5 and
        about 37 µg/m³ PM10, both under the 24-hour standards, which is how
        ``check_compliance`` currently reports those standards.
        """
        pm25 = self.ParameterType.PM25
        pm10 = self.ParameterType.PM10
        self._station([pm25, pm10])
        batch = []
        for day_offset in range(7):
            for hour in range(24):
                timestamp = DAY + timedelta(days=day_offset, hours=hour)
                batch.append(
                    {
                        "station_id": "STA-001",
                        "parameter": "pm25",
                        "value": 80.0 if day_offset == 0 else 10.0,
                        "unit": "µg/m³",
                        "timestamp": timestamp,
                    }
                )
                batch.append(
                    {
                        "station_id": "STA-001",
                        "parameter": "pm10",
                        "value": 200.0 if day_offset == 0 else 10.0,
                        "unit": "µg/m³",
                        "timestamp": timestamp,
                    }
                )
        stored = self.monitor.record_batch(batch)
        self.assertEqual(stored, 7 * 24 * 2)

        records = self._records(DAY, DAY + timedelta(days=7))
        pm25_day = self._record(records, pm25, 35, "EPA")
        self.assertEqual(pm25_day.status, self.ComplianceStatus.EXCEEDANCE)
        self.assertGreaterEqual(pm25_day.measured_value, 70)
        self.assertLessEqual(pm25_day.measured_value, 80)
        self.assertEqual(pm25_day.averaging_period, "24-hour avg")

        pm10_day = self._record(records, pm10, 150, "EPA")
        self.assertEqual(pm10_day.status, self.ComplianceStatus.EXCEEDANCE)
        self.assertGreaterEqual(pm10_day.measured_value, 150)
        self.assertLessEqual(pm10_day.measured_value, 200)
        self.assertEqual(pm10_day.averaging_period, "24-hour avg")

    def test_carbon_monoxide_eight_hour_limit_uses_its_own_window(self):
        """40 ppm across the shift exceeds the 35 ppm 8-hour limit.

        The other hours are 0 ppm, so the calendar-day mean is 15 ppm. The
        OSHA ceiling is instantaneous: the highest sample is 40 ppm, which
        stays under the 50 ppm ceiling.
        """
        co = self.ParameterType.CO
        self._station([co])
        self._record_hourly(
            co,
            "ppm",
            lambda day, hour: 40.0 if 8 <= hour <= 16 else 0.0,
        )

        records = self._records(DAY, DAY + timedelta(days=1))
        eight_hour = self._record(records, co, 35, "OSHA")
        self.assertEqual(eight_hour.status, self.ComplianceStatus.EXCEEDANCE)
        self.assertAlmostEqual(eight_hour.measured_value, 40.0)
        self.assertEqual(eight_hour.averaging_period, "8-hour avg")

        ceiling = self._record(records, co, 50, "OSHA")
        self.assertEqual(ceiling.status, self.ComplianceStatus.COMPLIANT)
        self.assertAlmostEqual(ceiling.measured_value, 40.0)
        self.assertEqual(ceiling.averaging_period, "Instantaneous")

        report = self.monitor.generate_daily_report(DAY)
        self.assertEqual(report.compliance_status, self.ComplianceStatus.EXCEEDANCE)

    def test_custom_limit_uses_its_own_averaging_period(self):
        """A caller-added 1-hour limit must not use the calendar-day mean.

        Four samples in the 13:00 hour are 40 C. Surrounding hours are 20 C,
        with 14:00 left unsampled so a one-hour window around 13:00 does not
        reach another hour. The day mean is about 23 C.
        """
        temperature = self.ParameterType.TEMPERATURE
        self._station([temperature])
        self.monitor.add_custom_limit(
            temperature,
            35,
            "C",
            1.0,
            "Site Plan",
            "1-hour heat limit",
        )
        for hour in range(24):
            if hour in (13, 14):
                continue
            self.monitor.record_reading(
                "STA-001",
                temperature,
                20.0,
                "C",
                DAY + timedelta(hours=hour),
            )
        for minute in (0, 10, 20, 30):
            self.monitor.record_reading(
                "STA-001",
                temperature,
                40.0,
                "C",
                DAY + timedelta(hours=13, minutes=minute),
            )

        records = self._records(DAY, DAY + timedelta(days=1))
        heat = self._record(records, temperature, 35, "Site Plan")
        self.assertEqual(heat.status, self.ComplianceStatus.EXCEEDANCE)
        self.assertAlmostEqual(heat.measured_value, 40.0)
        self.assertEqual(heat.averaging_period, "1-hour avg")


if __name__ == "__main__":
    unittest.main()
