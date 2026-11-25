"""Identity of safety records created in the same second.

These tests load the SafetyManager implementation shipped in the
safety-inspection skill and run it. They fail while a second hazard,
incident, or inspection created in the same wall-clock second replaces
the earlier record. They pass once every record stays addressable by
its own id and an existing id is never overwritten.

Run:

    python3 -m pip install pandas
    python3 -m unittest tests.test_safety_record_id_collision -v

The skill implementation imports pandas before defining SafetyManager.
The tests execute that implementation, so pandas has to be installed
even though these cases do not call it.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import types
import unittest
from datetime import date, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "Field-Operations"
    / "safety-inspection"
    / "SKILL.md"
)
PYTHON_FENCE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)

CRITICAL_HAZARD = "Fall protection missing on floor 5"
MINOR_HAZARD = "Minor housekeeping observation"
INJURY_DESCRIPTION = "Worker fell from scaffold"
NEAR_MISS_DESCRIPTION = "Tool dropped from height"
FIRST_LOCATION = "FLOOR-5-WEST"
SECOND_LOCATION = "YARD-GATE"


def load_safety_skill():
    """Execute the skill's own Python blocks in one namespace.

    Dataclasses look up the defining module in sys.modules, so the
    namespace is registered before the skill code runs. The module name
    is not ``__main__``, so the skill's example block does not run.
    """
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = PYTHON_FENCE.findall(text)
    if len(blocks) < 2:
        raise AssertionError(f"Expected the skill's Python blocks in {SKILL_PATH}")
    module = types.ModuleType("safety_inspection_skill")
    module.__file__ = str(SKILL_PATH)
    sys.modules[module.__name__] = module
    for block in blocks:
        exec(compile(block, str(SKILL_PATH), "exec"), module.__dict__)
    return module.__dict__


class SafetyRecordIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_safety_skill()
        cls.manager_cls = cls.ns["SafetyManager"]
        cls.hazard_severity = cls.ns["HazardSeverity"]
        cls.hazard_status = cls.ns["HazardStatus"]
        cls.inspection_type = cls.ns["InspectionType"]
        cls.hazard_cls = cls.ns["Hazard"]
        cls.incident_cls = cls.ns["Incident"]
        cls.inspection_cls = cls.ns["Inspection"]
        cls._real_datetime = cls.ns["datetime"]

    def setUp(self):
        self.ns["datetime"] = self._real_datetime
        self.manager = self.manager_cls(project_id="PROJECT-COLLISION")
        self._stdout = contextlib.redirect_stdout(io.StringIO())
        self._stdout.__enter__()

    def tearDown(self):
        self._stdout.__exit__(None, None, None)
        self.ns["datetime"] = self._real_datetime

    def freeze_clock(self, moment=None):
        """Make datetime.now() in the skill return one fixed instant.

        add_hazard, report_incident, and create_inspection read that
        clock when they build ids. Freezing it runs those functions
        while both calls observe the same second.
        """
        moment = moment or self._real_datetime.now().replace(microsecond=0)
        real_datetime = self._real_datetime

        class FrozenDateTime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return moment.replace(tzinfo=tz)
                return moment

        self.ns["datetime"] = FrozenDateTime
        return moment

    def _inspection(self, location=FIRST_LOCATION):
        return self.manager.create_inspection(
            inspection_type=self.inspection_type.DAILY_PREWORK,
            inspector="Site Inspector",
            location=location,
            weather="Clear",
        )

    def test_same_second_hazards_stay_in_the_open_overdue_and_briefing_lists(self):
        inspection = self._inspection()
        failed = self.manager.complete_checklist_item(
            inspection_id=inspection.inspection_id,
            item_id="DP07",
            result="Fail",
            notes="Worker on scaffold without harness",
        )
        failed_item = next(item for item in failed.checklist_items if item["id"] == "DP07")
        self.assertEqual(failed_item["result"], "Fail")

        self.freeze_clock()
        yesterday = date.today() - timedelta(days=1)
        critical = self.manager.add_hazard(
            inspection_id=inspection.inspection_id,
            description=CRITICAL_HAZARD,
            severity=self.hazard_severity.CRITICAL,
            location=FIRST_LOCATION,
            assigned_to="Site Foreman",
            due_date=yesterday,
        )
        minor = self.manager.add_hazard(
            inspection_id=inspection.inspection_id,
            description=MINOR_HAZARD,
            severity=self.hazard_severity.OBSERVATION,
            location=SECOND_LOCATION,
            assigned_to="Subcontractor",
        )

        with self.subTest("both hazards remain stored under different ids"):
            self.assertNotEqual(critical.hazard_id, minor.hazard_id)
            self.assertIs(self.manager.hazards[critical.hazard_id], critical)
            self.assertIs(self.manager.hazards[minor.hazard_id], minor)
            self.assertEqual(
                sorted(hazard.description for hazard in self.manager.hazards.values()),
                sorted([CRITICAL_HAZARD, MINOR_HAZARD]),
            )

        with self.subTest("open list, overdue list, and severity counts keep the critical hazard"):
            open_descriptions = {hazard.description for hazard in self.manager.get_open_hazards()}
            overdue_descriptions = {hazard.description for hazard in self.manager.get_overdue_hazards()}
            self.assertEqual(open_descriptions, {CRITICAL_HAZARD, MINOR_HAZARD})
            self.assertEqual(overdue_descriptions, {CRITICAL_HAZARD})
            counts = self.manager._count_by_severity()
            self.assertEqual(counts[self.hazard_severity.CRITICAL.value], 1)
            self.assertEqual(counts[self.hazard_severity.OBSERVATION.value], 1)
            stats = self.manager.get_statistics()
            self.assertEqual(stats["hazards_identified"], 2)
            self.assertEqual(stats["hazards_open"], 2)
            self.assertEqual(stats["hazards_overdue"], 1)

        with self.subTest("daily briefing still names the critical hazard"):
            report = self.manager.generate_daily_safety_report()
            self.assertIn(CRITICAL_HAZARD, report)
            self.assertIn("Open Hazards: 2", report)
            self.assertIn("Overdue Hazards: 1", report)

        with self.subTest("inspection keeps both findings"):
            found = [hazard.description for hazard in inspection.hazards_found]
            self.assertCountEqual(found, [CRITICAL_HAZARD, MINOR_HAZARD])

    def test_correct_and_verify_follow_the_hazard_that_was_created(self):
        inspection = self._inspection()
        self.freeze_clock()
        critical = self.manager.add_hazard(
            inspection_id=inspection.inspection_id,
            description=CRITICAL_HAZARD,
            severity=self.hazard_severity.CRITICAL,
            location=FIRST_LOCATION,
            assigned_to="Site Foreman",
            due_date=date.today() - timedelta(days=1),
        )
        minor = self.manager.add_hazard(
            inspection_id=inspection.inspection_id,
            description=MINOR_HAZARD,
            severity=self.hazard_severity.OBSERVATION,
            location=SECOND_LOCATION,
            assigned_to="Subcontractor",
        )

        corrected = self.manager.correct_hazard(
            hazard_id=critical.hazard_id,
            corrective_action="Harness issued and worker retrained",
            corrected_by="Site Foreman",
        )
        self.assertIs(corrected, critical)
        self.assertEqual(corrected.description, CRITICAL_HAZARD)
        self.assertEqual(corrected.status, self.hazard_status.CORRECTED)
        self.assertIs(self.manager.hazards[minor.hazard_id], minor)
        self.assertEqual(minor.status, self.hazard_status.OPEN)
        self.assertEqual(minor.description, MINOR_HAZARD)

        verified = self.manager.verify_hazard_closure(
            hazard_id=critical.hazard_id,
            verified_by="Safety Officer",
        )
        self.assertIs(verified, critical)
        self.assertEqual(verified.status, self.hazard_status.VERIFIED)
        self.assertEqual(verified.description, CRITICAL_HAZARD)
        self.assertEqual(minor.status, self.hazard_status.OPEN)
        self.assertEqual(
            self.manager.hazards[critical.hazard_id].corrective_action,
            "Harness issued and worker retrained",
        )

    def test_same_second_incidents_stay_distinct(self):
        self.freeze_clock()
        injury = self.manager.report_incident(
            incident_type="Lost Time",
            description=INJURY_DESCRIPTION,
            location=FIRST_LOCATION,
            injured_party="Alex Rivera",
            witness_names=["Jordan Lee"],
            immediate_actions="Area barricaded",
            reported_by="Site Inspector",
        )
        injury.osha_recordable = True
        near_miss = self.manager.report_incident(
            incident_type="Near Miss",
            description=NEAR_MISS_DESCRIPTION,
            location=SECOND_LOCATION,
            injured_party="",
            reported_by="Subcontractor",
        )

        self.assertNotEqual(injury.incident_id, near_miss.incident_id)
        self.assertIs(self.manager.incidents[injury.incident_id], injury)
        self.assertIs(self.manager.incidents[near_miss.incident_id], near_miss)
        self.assertEqual(injury.injured_party, "Alex Rivera")
        self.assertTrue(injury.osha_recordable)
        self.assertEqual(near_miss.description, NEAR_MISS_DESCRIPTION)
        self.assertFalse(near_miss.osha_recordable)
        self.assertEqual(self.manager.get_statistics()["incidents_total"], 2)
        self.assertEqual(
            sorted(incident.description for incident in self.manager.incidents.values()),
            sorted([INJURY_DESCRIPTION, NEAR_MISS_DESCRIPTION]),
        )

    def test_same_second_inspections_stay_distinct_and_checklist_updates_the_created_one(self):
        self.freeze_clock()
        first = self.manager.create_inspection(
            inspection_type=self.inspection_type.DAILY_PREWORK,
            inspector="Site Inspector",
            location=FIRST_LOCATION,
        )
        second = self.manager.create_inspection(
            inspection_type=self.inspection_type.SCAFFOLD,
            inspector="Subcontractor",
            location=SECOND_LOCATION,
        )

        self.assertNotEqual(first.inspection_id, second.inspection_id)
        self.assertIs(self.manager.inspections[first.inspection_id], first)
        self.assertIs(self.manager.inspections[second.inspection_id], second)

        updated = self.manager.complete_checklist_item(
            inspection_id=first.inspection_id,
            item_id="DP07",
            result="Fail",
            notes="No harness",
        )
        self.assertIs(updated, first)
        first_item = next(item for item in first.checklist_items if item["id"] == "DP07")
        self.assertEqual(first_item["result"], "Fail")
        self.assertEqual(first_item["notes"], "No harness")
        self.assertTrue(
            all(item["result"] is None for item in second.checklist_items)
        )

    def test_add_hazard_does_not_overwrite_an_existing_id(self):
        moment = self.freeze_clock()
        existing_id = f"HAZ-{moment.strftime('%Y%m%d%H%M%S')}"
        sentinel = self.hazard_cls(
            hazard_id=existing_id,
            inspection_id="INS-EXISTING",
            description="SENTINEL-HAZARD",
            severity=self.hazard_severity.CRITICAL,
            location="Existing location",
            due_date=date.today() - timedelta(days=1),
            status=self.hazard_status.OPEN,
        )
        self.manager.hazards[existing_id] = sentinel
        inspection = self._inspection()

        created = self.manager.add_hazard(
            inspection_id=inspection.inspection_id,
            description=MINOR_HAZARD,
            severity=self.hazard_severity.OBSERVATION,
            location=SECOND_LOCATION,
        )

        self.assertIs(self.manager.hazards[existing_id], sentinel)
        self.assertEqual(self.manager.hazards[existing_id].description, "SENTINEL-HAZARD")
        self.assertEqual(self.manager.hazards[existing_id].status, self.hazard_status.OPEN)
        self.assertNotEqual(created.hazard_id, existing_id)
        self.assertIs(self.manager.hazards[created.hazard_id], created)
        self.assertEqual(created.description, MINOR_HAZARD)

    def test_report_incident_does_not_overwrite_an_existing_id(self):
        moment = self.freeze_clock()
        existing_id = f"INC-{moment.strftime('%Y%m%d%H%M%S')}"
        sentinel = self.incident_cls(
            incident_id=existing_id,
            project_id=self.manager.project_id,
            date=moment,
            type="Lost Time",
            description=INJURY_DESCRIPTION,
            location=FIRST_LOCATION,
            injured_party="Alex Rivera",
            osha_recordable=True,
        )
        self.manager.incidents[existing_id] = sentinel

        created = self.manager.report_incident(
            incident_type="Near Miss",
            description=NEAR_MISS_DESCRIPTION,
            location=SECOND_LOCATION,
            reported_by="Subcontractor",
        )

        self.assertIs(self.manager.incidents[existing_id], sentinel)
        self.assertEqual(self.manager.incidents[existing_id].injured_party, "Alex Rivera")
        self.assertTrue(self.manager.incidents[existing_id].osha_recordable)
        self.assertNotEqual(created.incident_id, existing_id)
        self.assertIs(self.manager.incidents[created.incident_id], created)
        self.assertEqual(created.description, NEAR_MISS_DESCRIPTION)

    def test_create_inspection_does_not_overwrite_an_existing_id(self):
        moment = self.freeze_clock()
        existing_id = f"INS-{moment.strftime('%Y%m%d%H%M%S')}"
        sentinel = self.inspection_cls(
            inspection_id=existing_id,
            inspection_type=self.inspection_type.DAILY_PREWORK,
            project_id=self.manager.project_id,
            date=date.today(),
            inspector="Site Inspector",
            location=FIRST_LOCATION,
            checklist_items=[{"id": "DP07", "result": "Fail", "notes": "No harness", "photo": None}],
        )
        self.manager.inspections[existing_id] = sentinel

        created = self.manager.create_inspection(
            inspection_type=self.inspection_type.SCAFFOLD,
            inspector="Subcontractor",
            location=SECOND_LOCATION,
        )

        self.assertIs(self.manager.inspections[existing_id], sentinel)
        self.assertEqual(self.manager.inspections[existing_id].location, FIRST_LOCATION)
        stored_item = next(
            item for item in self.manager.inspections[existing_id].checklist_items
            if item["id"] == "DP07"
        )
        self.assertEqual(stored_item["result"], "Fail")
        self.assertNotEqual(created.inspection_id, existing_id)
        self.assertIs(self.manager.inspections[created.inspection_id], created)
        self.assertEqual(created.location, SECOND_LOCATION)

    def test_scripted_loop_of_failed_items_keeps_every_hazard(self):
        inspection = self._inspection()
        self.freeze_clock()
        created = []
        for index in range(5):
            self.manager.complete_checklist_item(
                inspection_id=inspection.inspection_id,
                item_id=f"DP{index + 1:02d}",
                result="Fail",
                notes=f"Failed item {index}",
            )
            created.append(
                self.manager.add_hazard(
                    inspection_id=inspection.inspection_id,
                    description=f"Failed checklist hazard {index}",
                    severity=(
                        self.hazard_severity.CRITICAL
                        if index == 0
                        else self.hazard_severity.LOW
                    ),
                    location=f"Grid {index}",
                    due_date=date.today() - timedelta(days=1),
                )
            )

        self.assertEqual(len({hazard.hazard_id for hazard in created}), len(created))
        self.assertEqual(len(self.manager.hazards), len(created))
        for hazard in created:
            self.assertIs(self.manager.hazards[hazard.hazard_id], hazard)
        overdue = {hazard.hazard_id for hazard in self.manager.get_overdue_hazards()}
        self.assertEqual(overdue, {hazard.hazard_id for hazard in created})
        report = self.manager.generate_daily_safety_report()
        self.assertIn("Failed checklist hazard 0", report)
        self.assertIn("Open Hazards: 5", report)
        self.assertIn("Overdue Hazards: 5", report)


if __name__ == "__main__":
    unittest.main()
