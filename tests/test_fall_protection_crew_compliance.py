"""Crew-wide fall-protection compliance for the safety checker skill.

These tests execute SafetyComplianceEngine.check_work_activity from the
skill document. They fail while one worker's harness marks the whole
elevated activity compliant, and while missing harnesses are never
recorded. They pass once every worker on an elevated activity must have
a harness unless a guardrail is verified on its own.

Run:
    python3 -m unittest discover -s tests -p 'test_fall_protection_crew_compliance.py' -v
"""

import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "5_DDC_Innovative"
    / "safety-compliance-checker"
    / "SKILL.md"
)
FALL_RULE_ID = "OSHA-1926.501"


def load_skill_namespace():
    """Execute the skill's own Python, in order, through the engine block."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    namespace = {"__name__": "safety_compliance_checker_skill"}
    saw_engine = False
    for block in blocks:
        defines_status = "class ComplianceStatus" in block
        defines_engine = "class SafetyComplianceEngine" in block
        if not defines_status and not defines_engine:
            continue
        with redirect_stdout(io.StringIO()):
            exec(compile(block, str(SKILL_PATH), "exec"), namespace)
        if defines_engine:
            saw_engine = True
            break
    if not saw_engine:
        raise AssertionError(
            "SafetyComplianceEngine block not found in {}".format(SKILL_PATH)
        )
    return namespace


class FallProtectionCrewComplianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = load_skill_namespace()
        cls.Engine = namespace["SafetyComplianceEngine"]
        cls.Status = namespace["ComplianceStatus"]

    def setUp(self):
        self.engine = self.Engine()

    def activity(self, workers, equipment=None, height_ft=18, activity_type="steel erection"):
        return {
            "type": activity_type,
            "location": "Level 4 east",
            "height_ft": height_ft,
            "workers": workers,
            "equipment": [] if equipment is None else equipment,
            "permits": [],
        }

    def fall_checks(self, checks):
        return [check for check in checks if check.rule_id == FALL_RULE_ID]

    def assert_fall_noncompliant_for(self, checks, unprotected_ids):
        fall = self.fall_checks(checks)
        self.assertEqual(
            len(fall),
            1,
            "elevated work must produce exactly one fall-protection check",
        )
        self.assertEqual(fall[0].status, self.Status.NON_COMPLIANT)
        recorded = " ".join(fall[0].findings).lower()
        self.assertIn("harness", recorded)
        for worker_id in unprotected_ids:
            self.assertIn(
                worker_id.lower(),
                recorded,
                "missing harness for {} was not recorded on the fall-protection check".format(
                    worker_id
                ),
            )
        return fall[0]

    def test_single_harness_does_not_clear_the_rest_of_the_crew(self):
        checks = self.engine.check_work_activity(
            self.activity(
                workers=[
                    {"id": "W1", "ppe": ["helmet", "harness"]},
                    {"id": "W2", "ppe": ["helmet"]},
                    {"id": "W3", "ppe": ["helmet"]},
                ],
                height_ft=20,
            )
        )
        self.assert_fall_noncompliant_for(checks, ["W2", "W3"])
        helmet_checks = [check for check in checks if check.rule_id == "OSHA-1926.100"]
        self.assertEqual(len(helmet_checks), 3)
        self.assertTrue(all(check.status == self.Status.COMPLIANT for check in helmet_checks))
        harness_mentions = [
            finding
            for check in checks
            for finding in check.findings
            if "harness" in finding.lower()
        ]
        self.assertTrue(harness_mentions)

    def test_harness_on_the_last_worker_only_does_not_clear_earlier_workers(self):
        checks = self.engine.check_work_activity(
            self.activity(
                workers=[
                    {"id": "W1", "ppe": ["helmet"]},
                    {"id": "W2"},
                    {"id": "W3", "ppe": ["helmet", "harness"]},
                ],
                height_ft=6,
            )
        )
        self.assert_fall_noncompliant_for(checks, ["W1", "W2"])

    def test_partial_crew_is_stored_as_a_fall_protection_violation(self):
        checks = self.engine.check_work_activity(
            self.activity(
                workers=[
                    {"id": "W1", "ppe": ["helmet", "harness"]},
                    {"id": "W2", "ppe": ["helmet"]},
                ],
                height_ft=12,
            )
        )
        for check in checks:
            if check.status == self.Status.NON_COMPLIANT:
                self.engine.create_violation(check, "Level 4 east")
        fall_violations = [
            violation
            for violation in self.engine.violations
            if violation.rule.rule_id == FALL_RULE_ID
        ]
        self.assertEqual(len(fall_violations), 1)
        description = fall_violations[0].description.lower()
        self.assertIn("harness", description)
        self.assertIn("w2", description)
        self.assertEqual(fall_violations[0].location, "Level 4 east")

    def test_every_worker_with_a_harness_is_compliant_without_a_guardrail(self):
        checks = self.engine.check_work_activity(
            self.activity(
                workers=[
                    {"id": "W1", "ppe": ["helmet", "harness"]},
                    {"id": "W2", "ppe": ["harness"]},
                ]
            )
        )
        fall = self.fall_checks(checks)
        self.assertEqual(len(fall), 1)
        self.assertEqual(fall[0].status, self.Status.COMPLIANT)
        self.assertEqual(fall[0].findings, [])

    def test_no_harnesses_and_no_guardrail_names_every_worker(self):
        checks = self.engine.check_work_activity(
            self.activity(
                workers=[
                    {"id": "W1", "ppe": ["helmet"]},
                    {"id": "W2", "ppe": ["helmet", "gloves"]},
                ]
            )
        )
        self.assert_fall_noncompliant_for(checks, ["W1", "W2"])

    def test_guardrail_alone_covers_a_crew_without_harnesses(self):
        checks = self.engine.check_work_activity(
            self.activity(
                workers=[
                    {"id": "W1", "ppe": ["helmet"]},
                    {"id": "W2", "ppe": []},
                ],
                equipment=["guardrail"],
            )
        )
        fall = self.fall_checks(checks)
        self.assertEqual(len(fall), 1)
        self.assertEqual(fall[0].status, self.Status.COMPLIANT)
        self.assertEqual(fall[0].findings, [])

    def test_guardrail_is_verified_without_iterating_workers(self):
        checks = self.engine.check_work_activity(
            self.activity(workers=[], equipment=["guardrail"], height_ft=10)
        )
        fall = self.fall_checks(checks)
        self.assertEqual(len(fall), 1)
        self.assertEqual(
            fall[0].status,
            self.Status.COMPLIANT,
            "a guardrail must count even when the worker list is empty",
        )

    def test_empty_crew_without_a_guardrail_is_not_compliant(self):
        checks = self.engine.check_work_activity(
            self.activity(workers=[], equipment=[], height_ft=8)
        )
        fall = self.fall_checks(checks)
        self.assertEqual(len(fall), 1)
        self.assertEqual(fall[0].status, self.Status.NON_COMPLIANT)
        self.assertTrue(fall[0].findings)

    def test_work_below_six_feet_does_not_require_harnesses(self):
        checks = self.engine.check_work_activity(
            self.activity(
                workers=[
                    {"id": "W1", "ppe": ["harness"]},
                    {"id": "W2", "ppe": ["helmet"]},
                ],
                height_ft=5,
            )
        )
        self.assertEqual(self.fall_checks(checks), [])

    def test_missing_helmet_is_still_recorded_separately(self):
        checks = self.engine.check_work_activity(
            self.activity(
                workers=[
                    {"id": "W1", "ppe": ["harness"]},
                    {"id": "W2", "ppe": ["harness"]},
                ]
            )
        )
        helmet_checks = [check for check in checks if check.rule_id == "OSHA-1926.100"]
        self.assertEqual(len(helmet_checks), 2)
        self.assertTrue(all(check.status == self.Status.NON_COMPLIANT for check in helmet_checks))
        self.assertTrue(all("hard hat" in " ".join(check.findings).lower() for check in helmet_checks))
        fall = self.fall_checks(checks)
        self.assertEqual(fall[0].status, self.Status.COMPLIANT)


if __name__ == "__main__":
    unittest.main()
