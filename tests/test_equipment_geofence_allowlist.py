"""Spec for the equipment-telematics geofence allow list.

These tests load the implementation embedded in the skill and run the
ingest path. They fail while a registered machine that is omitted from a
non-empty allow list can sit inside the radius without a geofence fault.
An empty allow list means no equipment restriction.
"""

import unittest
from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "5_DDC_Innovative"
    / "equipment-telematics"
    / "SKILL.md"
)

DIAGNOSTIC_CODES = {
    "HYD_TEMP_HIGH",
    "COOLANT_TEMP_HIGH",
    "FUEL_LOW",
    "SERVICE_OVERDUE",
    "SERVICE_DUE",
}


def load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    source = text[start:end]
    namespace = {"__name__": "equipment_telematics_skill"}
    exec(compile(source, str(SKILL_PATH), "exec"), namespace)
    return namespace


def point_north_of(telematics_cls, lat, lon, meters):
    """Return a point the skill's own distance function places at ``meters``."""
    probe = telematics_cls("probe")
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        distance = probe._haversine_distance(lat, lon, lat + mid, lon)
        if distance < meters:
            lo = mid
        else:
            hi = mid
    return lat + lo, lon


class GeofenceAllowListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_skill_namespace()
        cls.center = (40.7128, -74.0060)
        cls.radius = 500.0
        cls.inside = point_north_of(
            cls.ns["EquipmentTelematics"], cls.center[0], cls.center[1], cls.radius * 0.5
        )

    def setUp(self):
        self.ns = type(self).ns
        self.telematics = self.ns["EquipmentTelematics"]("Site A Fleet")
        self._register("EX-001", "Excavator #1")
        self._register("EX-002", "Contractor Pump")

    def _register(self, equipment_id, name):
        equipment = self.telematics.register_equipment(
            equipment_id,
            name,
            self.ns["EquipmentType"].EXCAVATOR,
            "Caterpillar",
            "320",
            2022,
            f"SN-{equipment_id}",
            150,
            400,
        )
        equipment.next_service_hours = 100000
        return equipment

    def _ingest(self, equipment_id, lat, lon):
        location = self.ns["GPSLocation"](
            latitude=lat,
            longitude=lon,
            speed=1.0,
            timestamp=self.ns["datetime"].now(),
        )
        return self.telematics.ingest_reading(
            equipment_id,
            location,
            engine_hours=100.0,
            fuel_level=50.0,
            fuel_rate=12.0,
            engine_rpm=1600,
            hydraulic_temp=70.0,
            coolant_temp=80.0,
            load_percentage=40,
            operator_id="OP-101",
        )

    def _geofence_faults(self, equipment_id):
        return [
            fault
            for fault in self.telematics.faults
            if fault.equipment_id == equipment_id
            and not fault.resolved
            and fault.code not in DIAGNOSTIC_CODES
        ]

    def test_unlisted_machine_inside_allow_list_is_recorded(self):
        lat, lon = self.inside
        distance = self.telematics._haversine_distance(
            lat, lon, self.center[0], self.center[1]
        )
        self.assertLess(distance, self.radius)

        self.telematics.add_geofence(
            "SITE-A",
            "Site A Boundary",
            self.center[0],
            self.center[1],
            self.radius,
            allowed_equipment=["EX-001"],
        )
        reading = self._ingest("EX-002", lat, lon)

        self.assertEqual(reading.equipment_id, "EX-002")
        self.assertEqual(reading.location.latitude, lat)
        self.assertEqual(reading.location.longitude, lon)
        faults = self._geofence_faults("EX-002")
        self.assertTrue(
            faults,
            "a registered machine omitted from a non-empty allow list must "
            "raise a geofence fault while it is inside the radius",
        )
        self.assertFalse(faults[0].resolved)
        self.assertGreaterEqual(
            self.telematics.get_current_status("EX-002")["active_faults"], 1
        )
        report = self.telematics.generate_report()
        self.assertIn("## Active Faults", report)
        self.assertIn("Contractor Pump", report.split("## Active Faults", 1)[1])

    def test_unlisted_machine_inside_second_fence_is_recorded(self):
        lat, lon = self.inside
        self.telematics.add_geofence(
            "YARD",
            "Contractor Yard",
            self.center[0],
            self.center[1],
            self.radius,
            allowed_equipment=["EX-002"],
        )
        self.telematics.add_geofence(
            "SITE-A",
            "Site A Boundary",
            self.center[0],
            self.center[1],
            self.radius,
            allowed_equipment=["EX-001", "EX-003"],
        )
        self._ingest("EX-002", lat, lon)

        self.assertTrue(self._geofence_faults("EX-002"))

    def test_listed_machine_inside_allow_list_is_not_a_fault(self):
        lat, lon = self.inside
        self.telematics.add_geofence(
            "SITE-A",
            "Site A Boundary",
            self.center[0],
            self.center[1],
            self.radius,
            allowed_equipment=["EX-001", "EX-003"],
        )
        self._ingest("EX-001", lat, lon)

        self.assertEqual(self._geofence_faults("EX-001"), [])
        self.assertEqual(
            self.telematics.get_current_status("EX-001")["active_faults"], 0
        )

    def test_empty_allow_list_does_not_restrict_presence(self):
        lat, lon = self.inside
        self.telematics.add_geofence(
            "SITE-A",
            "Site A Boundary",
            self.center[0],
            self.center[1],
            self.radius,
        )
        self._ingest("EX-002", lat, lon)

        self.assertEqual(self._geofence_faults("EX-002"), [])
        self.assertEqual(self.telematics.get_fleet_summary()["active_faults"], 0)


if __name__ == "__main__":
    unittest.main()
