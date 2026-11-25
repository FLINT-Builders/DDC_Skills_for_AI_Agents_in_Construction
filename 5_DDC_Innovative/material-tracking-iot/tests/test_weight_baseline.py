"""Weight readings must use the stored on-hand quantity as the only baseline.

The material tracker in ``SKILL.md`` books usage from a weight reading and
sets on-hand to the reported weight. These tests load that implementation
and drive it the way a weight-topic message does.

Required behavior (the fix these tests specify):

* The baseline is the material quantity already stored. A ``previous`` field
  on a weight message, and a ``previous_weight`` value passed to
  ``process_sensor_reading``, are ignored as baselines.
* A reported weight is a plausible change only when it is between zero and
  the stored quantity (inclusive) and the drop is at most half of that
  stored quantity. ``MAX_SINGLE_READING_DROP_FRACTION`` is that limit.
* A reading outside that band is rejected. On-hand, the movement ledger,
  and alerts stay as they were. Nothing is appended and then rolled back.
* An accepted reading sets on-hand to the reported weight and appends one
  usage movement to ``installed`` for exactly ``stored - reported``.
* That usage quantity is the ledger delta, including when the message
  claimed a much larger previous weight.

``record_delivery`` is not covered. It only adds stock for an explicit
receipt. RFID, GPS, and environment readings are not covered. They do not
take a caller-supplied weight baseline. No other operation in this skill
writes on-hand down from a sensor message.

These tests fail while a caller-supplied previous weight can change on-hand
and record the gap as installed usage.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import types
import unittest
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
MAX_SINGLE_READING_DROP_FRACTION = 0.5
SILO = "Silo-1"


def load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.S)
    if len(blocks) < 4:
        raise AssertionError(f"expected Python blocks in {SKILL_PATH}, found {len(blocks)}")
    module = types.ModuleType("material_tracking_skill")
    sys.modules[module.__name__] = module
    with contextlib.redirect_stdout(io.StringIO()):
        for index, block in enumerate(blocks):
            exec(compile(block, f"{SKILL_PATH}:block{index}", "exec"), module.__dict__)
    return module.__dict__


SKILL = load_skill_namespace()


class AuditList(list):
    """List that remembers every append, including ones later removed."""

    def __init__(self, iterable=()):
        super().__init__(iterable)
        self.appended = []

    def append(self, item):
        self.appended.append(item)
        super().append(item)


def watch_quantity(material):
    writes = []
    original = material.__class__

    class Watched(original):
        def __setattr__(self, name, value):
            if name == "quantity":
                writes.append(value)
            super().__setattr__(name, value)

    material.__class__ = Watched
    return writes


class WeightBaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tracking_type = SKILL["MaterialTrackingSystem"]
        cls.sensor_type = SKILL["SensorType"]
        cls.handler_type = SKILL["IoTMessageHandler"]
        cls.dashboard_type = SKILL["MaterialDashboard"]
        cls.fleet_type = SKILL["FleetTracker"]

    def new_tracker(self, quantity=100.0, extra_quantity=25.0):
        tracking = self.tracking_type()
        material = tracking.register_material("Cement", quantity, "kg")
        other = tracking.register_material("Rebar", extra_quantity, "kg")
        sensor = tracking.register_sensor(self.sensor_type.WEIGHT, SILO)
        tracking.movements = AuditList(tracking.movements)
        tracking.alerts = AuditList(tracking.alerts)
        return tracking, material, other, sensor

    def watch(self, tracking, material):
        material._original_quantity = material.quantity
        material._quantity_writes = watch_quantity(material)
        material._movement_mark = len(tracking.movements.appended)
        material._alert_mark = len(tracking.alerts.appended)
        material._updated = material.last_updated

    def publish(self, tracking, sensor, body):
        handler = self.handler_type(tracking)
        payload = json.dumps(body).encode("utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            handler.process_message(f"sensors/weight/{sensor.sensor_id}", payload)

    def usages_since(self, tracking, material):
        return [
            movement
            for movement in tracking.movements.appended[material._movement_mark :]
            if getattr(movement, "movement_type", None) == "usage"
        ]

    def assert_ledger_unchanged(self, tracking, material, other, other_quantity):
        writes = material._quantity_writes
        self.assertEqual(material.quantity, material._original_quantity)
        self.assertEqual(self.usages_since(tracking, material), [])
        self.assertTrue(
            all(value == material._original_quantity for value in writes),
            f"on-hand was written during a rejected reading: {writes}",
        )
        self.assertEqual(material.last_updated, material._updated)
        self.assertEqual(
            tracking.alerts.appended[material._alert_mark :],
            [],
            "a rejected weight reading created an alert",
        )
        self.assertEqual(other.quantity, other_quantity)
        report = tracking.get_inventory_report()
        reported = {
            item["id"]: item["quantity"] for item in report["materials"]
        }
        self.assertEqual(reported[material.material_id], material.quantity)
        self.assertEqual(
            tracking.get_material_history(material.material_id),
            [],
        )

    def assert_accepted_usage(self, tracking, material, sensor, stored, reported):
        expected_usage = stored - reported
        self.assertEqual(material.quantity, reported)
        usages = self.usages_since(tracking, material)
        self.assertEqual(len(usages), 1)
        movement = usages[0]
        self.assertEqual(movement.movement_type, "usage")
        self.assertEqual(movement.to_location, "installed")
        self.assertEqual(movement.from_location, sensor.location)
        self.assertEqual(movement.quantity, expected_usage)
        self.assertEqual(movement.material_id, material.material_id)
        self.assertEqual(movement.recorded_by, sensor.sensor_id)
        history = tracking.get_material_history(material.material_id)
        self.assertEqual(
            [(row["type"], row["to"], row["quantity"]) for row in history],
            [("usage", "installed", expected_usage)],
        )
        report = tracking.get_inventory_report()
        on_hand = next(
            item["quantity"]
            for item in report["materials"]
            if item["id"] == material.material_id
        )
        self.assertEqual(on_hand, reported)
        dashboard = self.dashboard_type(tracking, self.fleet_type())
        trend = dashboard.get_dashboard_data()["usage_trend"]
        self.assertEqual(sum(trend.values()), expected_usage)

    def test_mqtt_inflated_previous_cannot_zero_on_hand(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": 0,
                "previous": 10000,
                "material_id": material.material_id,
                "reorder_level": 1,
            },
        )
        self.assertEqual(material.quantity, 100)
        self.assert_ledger_unchanged(tracking, material, other, 25)

    def test_mqtt_missing_value_with_inflated_previous_cannot_zero_on_hand(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "previous": 10000,
                "material_id": material.material_id,
            },
        )
        self.assertEqual(material.quantity, 100)
        self.assert_ledger_unchanged(tracking, material, other, 25)

    def test_mqtt_inflated_previous_rejects_drop_over_half(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": 40,
                "previous": 10000,
                "material_id": material.material_id,
            },
        )
        self.assertEqual(material.quantity, 100)
        self.assert_ledger_unchanged(tracking, material, other, 25)

    def test_mqtt_inflated_previous_within_band_books_ledger_delta_only(self):
        tracking, material, other, sensor = self.new_tracker(1000)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": 900,
                "previous": 50000,
                "material_id": material.material_id,
            },
        )
        self.assert_accepted_usage(tracking, material, sensor, 1000, 900)
        self.assertEqual(other.quantity, 25)
        self.assertNotEqual(
            self.usages_since(tracking, material)[0].quantity,
            49100,
        )

    def test_mqtt_phantom_usage_is_not_recorded_when_weight_matches_ledger(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": 100,
                "previous": 150,
                "material_id": material.material_id,
            },
        )
        self.assertEqual(material.quantity, 100)
        self.assert_ledger_unchanged(tracking, material, other, 25)

    def test_mqtt_weight_above_ledger_does_not_increase_on_hand(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": 400,
                "previous": 500,
                "material_id": material.material_id,
            },
        )
        self.assertEqual(material.quantity, 100)
        self.assert_ledger_unchanged(tracking, material, other, 25)

    def test_mqtt_negative_weight_is_rejected(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": -5,
                "previous": 100,
                "material_id": material.material_id,
            },
        )
        self.assertEqual(material.quantity, 100)
        self.assert_ledger_unchanged(tracking, material, other, 25)

    def test_mqtt_without_previous_applies_plausible_ledger_drop(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": 80,
                "material_id": material.material_id,
            },
        )
        self.assert_accepted_usage(tracking, material, sensor, 100, 80)
        self.assertEqual(other.quantity, 25)

    def test_mqtt_matching_previous_still_uses_ledger_for_a_plausible_drop(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": 80,
                "previous": 100,
                "material_id": material.material_id,
            },
        )
        self.assert_accepted_usage(tracking, material, sensor, 100, 80)
        self.assertEqual(other.quantity, 25)

    def test_direct_reading_with_previous_weight_cannot_zero_on_hand(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        tracking.process_sensor_reading(
            sensor.sensor_id,
            0,
            "kg",
            {
                "material_id": material.material_id,
                "previous_weight": 10000,
            },
        )
        self.assertEqual(material.quantity, 100)
        self.assert_ledger_unchanged(tracking, material, other, 25)

    def test_direct_reading_without_previous_weight_books_ledger_delta(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        tracking.process_sensor_reading(
            sensor.sensor_id,
            80,
            "kg",
            {"material_id": material.material_id},
        )
        self.assert_accepted_usage(tracking, material, sensor, 100, 80)
        self.assertEqual(other.quantity, 25)

    def test_direct_lying_previous_weight_within_band_books_ledger_delta(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        tracking.process_sensor_reading(
            sensor.sensor_id,
            50,
            "kg",
            {
                "material_id": material.material_id,
                "previous_weight": 10000,
            },
        )
        self.assert_accepted_usage(tracking, material, sensor, 100, 50)
        self.assertEqual(self.usages_since(tracking, material)[0].quantity, 50)

    def test_direct_drop_just_over_half_is_rejected(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        tracking.process_sensor_reading(
            sensor.sensor_id,
            49,
            "kg",
            {"material_id": material.material_id},
        )
        self.assertEqual(material.quantity, 100)
        self.assert_ledger_unchanged(tracking, material, other, 25)

    def test_direct_drop_of_exactly_half_is_accepted(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        limit = 100 * MAX_SINGLE_READING_DROP_FRACTION
        tracking.process_sensor_reading(
            sensor.sensor_id,
            100 - limit,
            "kg",
            {"material_id": material.material_id},
        )
        self.assert_accepted_usage(tracking, material, sensor, 100, 50)

    def test_second_reading_uses_updated_ledger_not_earlier_previous(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": 80,
                "previous": 10000,
                "material_id": material.material_id,
            },
        )
        self.publish(
            tracking,
            sensor,
            {
                "value": 70,
                "previous": 10000,
                "material_id": material.material_id,
            },
        )
        self.assertEqual(material.quantity, 70)
        self.assertEqual(
            [movement.quantity for movement in self.usages_since(tracking, material)],
            [20, 10],
        )
        self.publish(
            tracking,
            sensor,
            {
                "value": 0,
                "previous": 10000,
                "material_id": material.material_id,
            },
        )
        self.assertEqual(material.quantity, 70)
        self.assertEqual(
            [movement.quantity for movement in self.usages_since(tracking, material)],
            [20, 10],
        )
        self.assertEqual(other.quantity, 25)

    def test_unknown_sensor_or_material_does_not_change_the_ledger(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": 0,
                "previous": 10000,
                "material_id": "MAT-NOT-REGISTERED",
            },
        )
        handler = self.handler_type(tracking)
        with contextlib.redirect_stdout(io.StringIO()):
            handler.process_message(
                "sensors/weight/SNS-WEIGHT-NOT-REGISTERED",
                json.dumps(
                    {
                        "value": 0,
                        "previous": 10000,
                        "material_id": material.material_id,
                    }
                ).encode("utf-8"),
            )
        self.assertEqual(material.quantity, 100)
        self.assert_ledger_unchanged(tracking, material, other, 25)
        self.assertIn(sensor.sensor_id, tracking.sensors)

    def test_inventory_report_does_not_treat_the_inflated_gap_as_use(self):
        tracking, material, other, sensor = self.new_tracker(100)
        self.watch(tracking, material)
        self.publish(
            tracking,
            sensor,
            {
                "value": 0,
                "previous": 10000,
                "material_id": material.material_id,
            },
        )
        report = tracking.get_inventory_report()
        on_hand = next(
            item["quantity"]
            for item in report["materials"]
            if item["id"] == material.material_id
        )
        self.assertEqual(on_hand, 100)
        history = tracking.get_material_history(material.material_id)
        self.assertEqual(history, [])
        dashboard = self.dashboard_type(tracking, self.fleet_type())
        trend = dashboard.get_dashboard_data()["usage_trend"]
        self.assertNotIn(10000, trend.values())
        self.assertEqual(sum(trend.values()), 0)
        self.assertEqual(other.quantity, 25)


if __name__ == "__main__":
    unittest.main()
