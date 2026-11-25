"""Incident id collision tests for the incident-reporting skill.

These tests execute the IncidentManager implementation embedded in SKILL.md.
They fail while a second report or corrective action filed in the same
second silently replaces the earlier record. They pass once ids are
collision-free and an existing id is refused instead of overwritten.

Run from the repository root:

    python3 -m unittest 3_DDC_Insights/Safety-Quality/incident-reporting/test_incident_id_collision.py
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parent / "SKILL.md"
FIXED_NOW = datetime(2026, 6, 15, 12, 0, 0)
HOURS_WORKED = 200_000

INJURED = {
    "name": "Alex Rivera",
    "company": "Rivera Concrete",
    "role": "Laborer",
    "contact": "alex@example.com",
    "years_experience": 3,
}
WITNESS = {
    "name": "Jordan Lee",
    "company": "Lee Electric",
    "role": "Foreman",
    "contact": "jordan@example.com",
    "years_experience": 12,
}


def extract_incident_manager_source(skill_text: str) -> str:
    """Return the skill code block that defines IncidentManager."""
    blocks = re.findall(r"```python\n(.*?)```", skill_text, flags=re.DOTALL)
    for block in blocks:
        if "class IncidentManager" in block:
            return block
    raise AssertionError(f"IncidentManager source block not found in {SKILL_PATH}")


def load_incident_manager(now: datetime = FIXED_NOW):
    """Exec the skill implementation and freeze datetime.now to one second."""
    source = extract_incident_manager_source(SKILL_PATH.read_text(encoding="utf-8"))
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(source, str(SKILL_PATH), "exec"), namespace)

    real_datetime = namespace["datetime"]

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return now.replace(tzinfo=tz)
            return now

    namespace["datetime"] = FrozenDateTime
    return namespace


class IncidentIdCollisionTest(unittest.TestCase):
    def setUp(self):
        self.ns = load_incident_manager()
        self.manager = self.ns["IncidentManager"]()
        self.IncidentType = self.ns["IncidentType"]
        self.IncidentCategory = self.ns["IncidentCategory"]

    def _report(self, incident_type, description, reported_by, injured=None):
        return self.manager.report_incident(
            incident_type=incident_type,
            category=self.IncidentCategory.FALL,
            date_time=FIXED_NOW,
            location="Level 3, Grid B-2",
            project_id="PRJ-001",
            project_name="Office Tower",
            description=description,
            immediate_actions="Area secured",
            reported_by=reported_by,
            injured_person=injured,
        )

    def _file_second(self, description, reported_by):
        """File another incident in the same frozen second.

        A correct implementation either keeps both records under different
        ids or refuses the second call without changing the first.
        """
        try:
            return self._report(
                self.IncidentType.NEAR_MISS,
                description,
                reported_by,
            ), None
        except Exception as exc:
            return None, exc

    def test_same_second_near_miss_does_not_replace_lost_time_incident(self):
        lost = self._report(
            self.IncidentType.LOST_TIME,
            "LOST-TIME-INJURY-UNIQUE",
            "Site Manager",
            injured=INJURED,
        )
        lost.days_lost = 4
        self.manager.add_witness(lost.id, WITNESS)
        self.manager.conduct_investigation(
            lost.id,
            ["unguarded edge"],
            ["wet deck"],
        )
        self.manager.five_whys_analysis(
            lost.id,
            ["worker slipped", "no guardrail"],
        )
        action = self.manager.assign_corrective_action(
            lost.id,
            "Shore the excavation",
            "Site Super",
            due_days=5,
        )

        near, error = self._file_second("NEAR-MISS-UNIQUE", "Subcontractor Foreman")

        self.assertIn(lost.id, self.manager.incidents)
        self.assertIs(self.manager.incidents[lost.id], lost)
        self.assertEqual(lost.incident_type, self.IncidentType.LOST_TIME)
        self.assertTrue(lost.osha_recordable)
        self.assertEqual(lost.injured_person.name, "Alex Rivera")
        self.assertEqual(lost.days_lost, 4)
        self.assertEqual([person.name for person in lost.witnesses], ["Jordan Lee"])
        self.assertEqual(lost.root_causes, ["unguarded edge", "no guardrail"])
        self.assertEqual(lost.contributing_factors, ["wet deck"])
        self.assertIn(action, lost.corrective_actions)
        self.assertIs(self.manager.corrective_actions[action.id], action)

        if near is not None:
            self.assertIsNone(error)
            self.assertNotEqual(near.id, lost.id)
            self.assertIs(self.manager.incidents[near.id], near)
            self.assertEqual(near.incident_type, self.IncidentType.NEAR_MISS)
            self.assertEqual(len(self.manager.incidents), 2)
        else:
            self.assertIsNotNone(error)
            stored = list(self.manager.incidents.values())
            self.assertEqual(stored, [lost])
            self.assertFalse(
                any(item.incident_type == self.IncidentType.NEAR_MISS for item in stored)
            )

    def test_recordable_types_are_not_replaced_by_a_same_second_near_miss(self):
        recordable_types = (
            self.IncidentType.MEDICAL_TREATMENT,
            self.IncidentType.LOST_TIME,
            self.IncidentType.FATALITY,
        )
        for incident_type in recordable_types:
            with self.subTest(incident_type=incident_type):
                self.manager = self.ns["IncidentManager"]()
                original = self._report(
                    incident_type,
                    f"RECORDABLE-{incident_type.value}",
                    "Site Manager",
                    injured=INJURED,
                )
                self.assertTrue(original.osha_recordable)
                near, error = self._file_second(
                    f"NEAR-MISS-AFTER-{incident_type.value}",
                    "Subcontractor Foreman",
                )
                self.assertIs(self.manager.incidents[original.id], original)
                self.assertEqual(original.incident_type, incident_type)
                self.assertTrue(original.osha_recordable)
                if near is not None:
                    self.assertNotEqual(near.id, original.id)
                else:
                    self.assertIsNotNone(error)

    def test_metrics_trir_dart_and_trend_still_count_the_lost_time_injury(self):
        lost = self._report(
            self.IncidentType.LOST_TIME,
            "LOST-TIME-INJURY-UNIQUE",
            "Site Manager",
            injured=INJURED,
        )
        lost.days_lost = 4
        near, _error = self._file_second("NEAR-MISS-UNIQUE", "Subcontractor Foreman")
        expected_total = 2 if near is not None else 1
        expected_near_misses = 1 if near is not None else 0

        window_start = FIXED_NOW - timedelta(days=1)
        window_end = FIXED_NOW + timedelta(days=1)
        for project_id in (None, "PRJ-001"):
            metrics = self.manager.get_incident_metrics(
                project_id=project_id,
                start_date=window_start,
                end_date=window_end,
            )
            self.assertEqual(metrics["osha_recordables"], 1)
            self.assertEqual(metrics["lost_time_incidents"], 1)
            self.assertEqual(metrics["total_days_lost"], 4)
            self.assertEqual(
                self.manager.calculate_trir(HOURS_WORKED, project_id=project_id),
                1.0,
            )
            self.assertEqual(
                self.manager.calculate_dart(HOURS_WORKED, project_id=project_id),
                1.0,
            )
            self.assertEqual(metrics["total_incidents"], expected_total)
            self.assertEqual(metrics["near_misses"], expected_near_misses)

        trends = self.manager.get_trend_analysis(months=6)
        june = next(row for row in trends if row["month"] == "2026-06")
        self.assertEqual(june["total"], expected_total)
        self.assertEqual(june["near_misses"], expected_near_misses)
        self.assertEqual(june["recordables"], 1)

        untouched = self.ns["IncidentManager"]()
        self.assertEqual(untouched.calculate_trir(0), 0)
        self.assertEqual(untouched.calculate_dart(0), 0)

    def test_follow_up_operations_stay_on_the_lost_time_incident(self):
        lost = self._report(
            self.IncidentType.LOST_TIME,
            "LOST-TIME-INJURY-UNIQUE",
            "Site Manager",
            injured=INJURED,
        )
        action = self.manager.assign_corrective_action(
            lost.id,
            "Shore the excavation",
            "Site Super",
            due_days=5,
        )
        _near, _error = self._file_second("NEAR-MISS-UNIQUE", "Subcontractor Foreman")

        updated = self.manager.add_witness(lost.id, WITNESS)
        self.assertIs(updated, lost)
        investigated = self.manager.conduct_investigation(
            lost.id,
            ["unguarded edge"],
            ["wet deck"],
        )
        self.assertIs(investigated, lost)
        analysis = self.manager.five_whys_analysis(lost.id, ["no guardrail"])
        self.assertEqual(analysis["incident_id"], lost.id)
        self.assertEqual(lost.root_causes, ["unguarded edge", "no guardrail"])

        report = self.manager.generate_incident_report(lost.id)
        self.assertIn("LOST-TIME-INJURY-UNIQUE", report)
        self.assertIn("Alex Rivera", report)
        self.assertIn("**OSHA Recordable:** Yes", report)
        self.assertIn("lost_time", report)
        self.assertIn("Shore the excavation", report)
        self.assertIn("unguarded edge", report)
        self.assertNotIn("NEAR-MISS-UNIQUE", report)

        self.manager.complete_corrective_action(action.id, "shoring verified")
        self.assertEqual(action.status, "completed")
        self.assertEqual(action.verification_notes, "shoring verified")
        self.assertIs(self.manager.corrective_actions[action.id], action)
        self.assertIn(action, self.manager.incidents[lost.id].corrective_actions)

    def test_same_second_corrective_actions_do_not_overwrite(self):
        incident = self._report(
            self.IncidentType.NEAR_MISS,
            "SINGLE-NEAR-MISS",
            "Site Foreman",
        )
        first = self.manager.assign_corrective_action(
            incident.id,
            "Install guardrails",
            "Safety Manager",
            due_days=7,
        )
        try:
            second = self.manager.assign_corrective_action(
                incident.id,
                "Retrain crew",
                "Superintendent",
                due_days=3,
            )
            second_error = None
        except Exception as exc:
            second = None
            second_error = exc

        self.assertIs(self.manager.corrective_actions[first.id], first)
        self.assertEqual(first.description, "Install guardrails")
        self.assertEqual(first.status, "open")
        self.assertIn(first, incident.corrective_actions)

        if second is not None:
            self.assertIsNone(second_error)
            self.assertNotEqual(second.id, first.id)
            self.assertIs(self.manager.corrective_actions[second.id], second)
            self.assertIn(second, incident.corrective_actions)
            self.assertEqual(len(incident.corrective_actions), 2)
            self.assertEqual(len(self.manager.corrective_actions), 2)
        else:
            self.assertIsNotNone(second_error)
            self.assertEqual(incident.corrective_actions, [first])
            self.assertEqual(list(self.manager.corrective_actions), [first.id])

        self.manager.complete_corrective_action(first.id, "guardrails verified")
        self.assertEqual(first.status, "completed")
        self.assertEqual(first.verification_notes, "guardrails verified")
        self.assertIs(self.manager.corrective_actions[first.id], first)
        if second is not None:
            self.assertEqual(second.status, "open")
            self.assertEqual(second.verification_notes, "")

    def test_existing_incident_id_is_not_overwritten(self):
        """A generated id that is already stored must not replace that record."""
        stamped_id = f"INC-{FIXED_NOW.strftime('%Y%m%d%H%M%S')}"
        sentinel = self._report(
            self.IncidentType.LOST_TIME,
            "PRESEEDED-LOST-TIME",
            "Site Manager",
            injured=INJURED,
        )
        # Move the stored record onto the id a second-resolution generator emits,
        # including when report_incident itself just issued that id.
        if sentinel.id != stamped_id:
            self.manager.incidents.pop(sentinel.id)
            sentinel.id = stamped_id
            self.manager.incidents[stamped_id] = sentinel
        self.assertIs(self.manager.incidents[stamped_id], sentinel)

        near, error = self._file_second("COLLIDING-NEAR-MISS", "Subcontractor Foreman")

        self.assertIs(self.manager.incidents[stamped_id], sentinel)
        self.assertEqual(sentinel.incident_type, self.IncidentType.LOST_TIME)
        self.assertTrue(sentinel.osha_recordable)
        self.assertEqual(sentinel.description, "PRESEEDED-LOST-TIME")
        if near is not None:
            self.assertNotEqual(near.id, stamped_id)
            self.assertIs(self.manager.incidents[near.id], near)
        else:
            self.assertIsNotNone(error)
            self.assertEqual(list(self.manager.incidents), [stamped_id])

    def test_existing_corrective_action_id_is_not_overwritten(self):
        incident = self._report(
            self.IncidentType.LOST_TIME,
            "LOST-TIME-WITH-ACTION",
            "Site Manager",
            injured=INJURED,
        )
        first = self.manager.assign_corrective_action(
            incident.id,
            "Install guardrails",
            "Safety Manager",
            due_days=7,
        )
        stamped_id = f"CA-{FIXED_NOW.strftime('%Y%m%d%H%M%S')}"
        if first.id != stamped_id:
            self.manager.corrective_actions.pop(first.id)
            first.id = stamped_id
            self.manager.corrective_actions[stamped_id] = first

        try:
            second = self.manager.assign_corrective_action(
                incident.id,
                "Retrain crew",
                "Superintendent",
                due_days=3,
            )
            second_error = None
        except Exception as exc:
            second = None
            second_error = exc

        self.assertIs(self.manager.corrective_actions[stamped_id], first)
        self.assertEqual(first.description, "Install guardrails")
        self.assertEqual(first.status, "open")
        if second is not None:
            self.assertNotEqual(second.id, stamped_id)
            self.assertIs(self.manager.corrective_actions[second.id], second)
            self.assertIn(first, incident.corrective_actions)
            self.assertIn(second, incident.corrective_actions)
        else:
            self.assertIsNotNone(second_error)
            self.assertEqual(incident.corrective_actions, [first])
            self.assertEqual(list(self.manager.corrective_actions), [stamped_id])


if __name__ == "__main__":
    unittest.main()
