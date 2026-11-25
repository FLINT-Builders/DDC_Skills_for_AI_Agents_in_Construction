"""Safety alerts must fire for threshold crossings the aggregator stores.

The implementation under test is the SensorDataAggregator block in
5_DDC_Innovative/sensor-data-aggregator/SKILL.md. ingest_reading labels a
reading suspect when it jumps more than half from the recent average, or when
it falls outside configured min/max, then skips critical and warning alerts
for every suspect reading. ingest_batch uses that same method.

These tests state the required behavior. They fail while that skip is present
and pass once a stored reading that crosses a safety limit still produces an
alert. Operations that do not share this skip are not covered here:
get_zone_summary reports a zone status from the latest value without creating
an alert, and detect_anomalies only returns statistical outliers.
"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "5_DDC_Innovative"
    / "sensor-data-aggregator"
    / "SKILL.md"
)


def load_skill():
    """Execute the aggregator implementation shipped in the skill."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.find(marker)
    if start < 0:
        raise AssertionError(f"no python block in {SKILL_PATH}")
    start += len(marker)
    end = text.find("\n```", start)
    if end < 0:
        raise AssertionError(f"unterminated python block in {SKILL_PATH}")
    code = text[start:end]
    if "class SensorDataAggregator" not in code:
        raise AssertionError("SensorDataAggregator implementation block not found")
    namespace = {}
    exec(compile(code, str(SKILL_PATH), "exec"), namespace)
    return namespace


SKILL = load_skill()
SensorDataAggregator = SKILL["SensorDataAggregator"]
SensorType = SKILL["SensorType"]
AlertSeverity = SKILL["AlertSeverity"]


# Baseline, critical spike, and warning spike for every default safety limit.
# Baselines sit far enough below the limit that the spike deviates by more
# than half of the recent average, which is the suspect rule.
SAFETY_CASES = (
    (SensorType.GAS, "ppm", (10, 10, 11), 50, 25),
    (SensorType.NOISE, "dB", (40, 42, 41), 100, 90),
    (SensorType.DUST, "mg/m3", (1.0, 1.0, 1.1), 10, 4),
    (SensorType.VIBRATION, "mm/s", (2, 2, 2.2), 25, 12),
    (SensorType.AIR_QUALITY, "AQI", (30, 32, 31), 150, 120),
    (SensorType.TEMPERATURE, "C", (10, 11, 10), 40, 36),
    (SensorType.HUMIDITY, "%", (20, 22, 21), 90, 85),
)


class SensorAlertSuppressionTest(unittest.TestCase):
    def setUp(self):
        self.aggregator = SensorDataAggregator("Site A")
        self.handled = []
        self.aggregator.register_alert_handler(self.handled.append)

    def _register(self, sensor_id, sensor_type, unit, thresholds=None):
        return self.aggregator.register_sensor(
            sensor_id,
            sensor_id,
            sensor_type,
            unit,
            {"zone": "A", "floor": 1},
            thresholds=thresholds,
        )

    def _alerts_for(self, sensor_id, value):
        return [
            alert
            for alert in self.aggregator.alerts
            if alert.sensor_id == sensor_id and alert.value == value
        ]

    def _assert_alert(self, sensor_id, value, severity):
        matches = [
            alert
            for alert in self._alerts_for(sensor_id, value)
            if alert.severity == severity
        ]
        self.assertTrue(
            matches,
            f"expected {severity.value} alert for {sensor_id} value {value}, "
            f"got {[(a.sensor_id, a.severity.value, a.value) for a in self.aggregator.alerts]}",
        )
        self.assertTrue(
            any(
                alert.sensor_id == sensor_id
                and alert.value == value
                and alert.severity == severity
                for alert in self.handled
            ),
            "alert handler was not called with the safety alert",
        )
        self.assertTrue(
            any(
                reading.sensor_id == sensor_id and reading.value == value
                for reading in self.aggregator.readings
            ),
            "threshold crossing was not stored",
        )

    def test_critical_spike_after_stable_baseline_alerts_for_each_safety_type(self):
        for sensor_type, unit, baseline, critical, _warning in SAFETY_CASES:
            with self.subTest(sensor_type=sensor_type.value):
                self.setUp()
                sensor_id = f"{sensor_type.value}-spike"
                self._register(sensor_id, sensor_type, unit)
                for value in baseline:
                    reading = self.aggregator.ingest_reading(sensor_id, value)
                    self.assertEqual(reading.quality, SKILL["DataQuality"].GOOD)
                spike = self.aggregator.ingest_reading(sensor_id, critical)
                self.assertEqual(spike.value, critical)
                self._assert_alert(sensor_id, critical, AlertSeverity.CRITICAL)

    def test_warning_spike_after_stable_baseline_alerts_for_each_safety_type(self):
        for sensor_type, unit, baseline, _critical, warning in SAFETY_CASES:
            with self.subTest(sensor_type=sensor_type.value):
                self.setUp()
                sensor_id = f"{sensor_type.value}-warn"
                self._register(sensor_id, sensor_type, unit)
                for value in baseline:
                    self.aggregator.ingest_reading(sensor_id, value)
                self.aggregator.ingest_reading(sensor_id, warning)
                self._assert_alert(sensor_id, warning, AlertSeverity.WARNING)

    def test_gas_batch_spike_alerts(self):
        self._register("GAS-BATCH", SensorType.GAS, "ppm")
        accepted = self.aggregator.ingest_batch(
            [
                {"sensor_id": "GAS-BATCH", "value": 10},
                {"sensor_id": "GAS-BATCH", "value": 10},
                {"sensor_id": "GAS-BATCH", "value": 10},
                {"sensor_id": "GAS-BATCH", "value": 50},
            ]
        )
        self.assertEqual(accepted, 4)
        self._assert_alert("GAS-BATCH", 50, AlertSeverity.CRITICAL)

    def test_noise_batch_spike_alerts(self):
        self._register("NOISE-BATCH", SensorType.NOISE, "dB")
        accepted = self.aggregator.ingest_batch(
            [
                {"sensor_id": "NOISE-BATCH", "value": 40},
                {"sensor_id": "NOISE-BATCH", "value": 41},
                {"sensor_id": "NOISE-BATCH", "value": 39},
                {"sensor_id": "NOISE-BATCH", "value": 100},
            ]
        )
        self.assertEqual(accepted, 4)
        self._assert_alert("NOISE-BATCH", 100, AlertSeverity.CRITICAL)

    def test_near_zero_baseline_then_critical_gas_alerts(self):
        self._register("GAS-ZERO", SensorType.GAS, "ppm")
        for _ in range(3):
            self.aggregator.ingest_reading("GAS-ZERO", 0)
        self.aggregator.ingest_reading("GAS-ZERO", 50)
        self._assert_alert("GAS-ZERO", 50, AlertSeverity.CRITICAL)

    def test_near_zero_baseline_then_critical_dust_alerts(self):
        self._register("DUST-ZERO", SensorType.DUST, "mg/m3")
        for value in (0, 0, 0):
            self.aggregator.ingest_reading("DUST-ZERO", value)
        self.aggregator.ingest_reading("DUST-ZERO", 10)
        self._assert_alert("DUST-ZERO", 10, AlertSeverity.CRITICAL)

    def test_negative_baseline_then_critical_temperature_alerts(self):
        self._register("TEMP-NEG", SensorType.TEMPERATURE, "C")
        for value in (-5, -5, -4):
            self.aggregator.ingest_reading("TEMP-NEG", value)
        self.aggregator.ingest_reading("TEMP-NEG", 40)
        self._assert_alert("TEMP-NEG", 40, AlertSeverity.CRITICAL)

    def test_value_above_max_and_critical_still_alerts(self):
        self._register(
            "GAS-MAX",
            SensorType.GAS,
            "ppm",
            thresholds={"warning": 20, "critical": 50, "min": 0, "max": 40},
        )
        for value in (10, 10, 10):
            self.aggregator.ingest_reading("GAS-MAX", value)
        self.aggregator.ingest_reading("GAS-MAX", 80)
        self._assert_alert("GAS-MAX", 80, AlertSeverity.CRITICAL)

    def test_value_below_min_and_at_critical_still_alerts(self):
        # critical is below min, so the range check marks the crossing suspect
        # before the spike check runs.
        self._register(
            "GAS-MIN",
            SensorType.GAS,
            "ppm",
            thresholds={"warning": 40, "critical": 50, "min": 80, "max": 200},
        )
        self.aggregator.ingest_reading("GAS-MIN", 60)
        self._assert_alert("GAS-MIN", 60, AlertSeverity.CRITICAL)

    def test_custom_threshold_type_spike_alerts(self):
        self._register(
            "PRESS-1",
            SensorType.PRESSURE,
            "kPa",
            thresholds={"warning": 20, "critical": 50},
        )
        for value in (8, 9, 8):
            self.aggregator.ingest_reading("PRESS-1", value)
        self.aggregator.ingest_reading("PRESS-1", 50)
        self._assert_alert("PRESS-1", 50, AlertSeverity.CRITICAL)

    def test_spike_inside_five_minute_window_uses_recent_average(self):
        self._register("VIB-TIME", SensorType.VIBRATION, "mm/s")
        now = datetime.now()
        for offset, value in ((4, 2), (3, 2), (2, 2)):
            self.aggregator.ingest_reading(
                "VIB-TIME",
                value,
                timestamp=now - timedelta(minutes=offset),
            )
        self.aggregator.ingest_reading("VIB-TIME", 25, timestamp=now)
        self._assert_alert("VIB-TIME", 25, AlertSeverity.CRITICAL)

    def test_only_the_spiking_sensor_is_alerted(self):
        self._register("GAS-A", SensorType.GAS, "ppm")
        self._register("NOISE-B", SensorType.NOISE, "dB")
        for value in (10, 10, 10):
            self.aggregator.ingest_reading("GAS-A", value)
            self.aggregator.ingest_reading("NOISE-B", 40)
        self.aggregator.ingest_reading("GAS-A", 50)
        self._assert_alert("GAS-A", 50, AlertSeverity.CRITICAL)
        self.assertFalse(self._alerts_for("NOISE-B", 50))
        self.assertFalse(
            any(alert.sensor_id == "NOISE-B" for alert in self.aggregator.alerts)
        )

    def test_first_critical_reading_without_baseline_alerts(self):
        self._register("GAS-FIRST", SensorType.GAS, "ppm")
        self.aggregator.ingest_reading("GAS-FIRST", 50)
        self._assert_alert("GAS-FIRST", 50, AlertSeverity.CRITICAL)

    def test_critical_rise_within_half_of_baseline_alerts(self):
        self._register("GAS-GRADUAL", SensorType.GAS, "ppm")
        for value in (40, 42, 44):
            self.aggregator.ingest_reading("GAS-GRADUAL", value)
        self.aggregator.ingest_reading("GAS-GRADUAL", 50)
        self._assert_alert("GAS-GRADUAL", 50, AlertSeverity.CRITICAL)


if __name__ == "__main__":
    unittest.main()
