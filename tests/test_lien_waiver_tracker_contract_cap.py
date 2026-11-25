"""Spec for subcontract caps on lien-waiver-tracker pay applications.

Loads the implementation embedded in the skill and drives the payment path:
register a subcontract, submit a pay application, record the conditional
waiver, then read the payable from the release check.

A request whose approved amount (requested amount minus retainage), together
with approved amounts already stored for that same subcontractor, would
exceed that subcontractor's contract amount must be rejected with ValueError.
The rejection must leave pay applications and waivers unchanged.

These tests fail while the skill approves amount_requested - retainage with
no contract cap.
"""

import types
import unittest
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "4_DDC_Curated"
    / "Contract-Legal"
    / "lien-waiver-tracker"
    / "SKILL.md"
)
PERIOD_END = datetime(2026, 1, 31)


def load_skill_module():
    """Execute the skill's implementation fence and return that module.

    The class under test lives in SKILL.md. Running that source, rather than
    a copy, is what the pay-application path in the skill actually does.
    """
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.find(marker)
    if start < 0:
        raise AssertionError(f"no python implementation fence in {SKILL_PATH}")
    start += len(marker)
    end = text.find("\n```", start)
    if end < 0:
        raise AssertionError(f"unterminated python fence in {SKILL_PATH}")
    source = text[start:end]
    if "def create_payment_application" not in source:
        raise AssertionError("implementation fence is not the tracker class")
    if "def can_release_payment" not in source:
        raise AssertionError("implementation fence has no release check")

    module = types.ModuleType("lien_waiver_tracker_skill")
    module.__file__ = str(SKILL_PATH)
    exec(compile(source, str(SKILL_PATH), "exec"), module.__dict__)
    return module


SKILL = load_skill_module()


class LienWaiverContractCapTests(unittest.TestCase):
    def setUp(self):
        self.tracker = SKILL.LienWaiverTracker("PRJ-001", "Office Tower")

    def add_sub(self, sub_id, contract_amount):
        return self.tracker.add_subcontractor(
            sub_id,
            f"{sub_id} Mechanical",
            "HVAC",
            contract_amount,
            "Ada Applicant",
            "ada@example.com",
        )

    def approved_total(self, sub_id):
        return sum(
            pay_app.amount_approved
            for pay_app in self.tracker.pay_apps.values()
            if pay_app.subcontractor_id == sub_id
        )

    def assert_unchanged(self, sub_id, pay_apps, waivers):
        self.assertEqual(self.tracker.pay_apps.keys(), pay_apps.keys())
        self.assertEqual(self.tracker.waivers.keys(), waivers.keys())
        for key, pay_app in pay_apps.items():
            stored = self.tracker.pay_apps[key]
            self.assertEqual(stored.amount_requested, pay_app.amount_requested)
            self.assertEqual(stored.amount_approved, pay_app.amount_approved)
            self.assertEqual(stored.retainage, pay_app.retainage)
            self.assertEqual(stored.status, pay_app.status)
            self.assertEqual(stored.subcontractor_id, pay_app.subcontractor_id)
        for key, waiver in waivers.items():
            stored = self.tracker.waivers[key]
            self.assertEqual(stored.amount, waiver.amount)
            self.assertEqual(stored.status, waiver.status)
            self.assertEqual(stored.subcontractor_id, waiver.subcontractor_id)
        self.assertLessEqual(
            self.approved_total(sub_id),
            self.tracker.subcontractors[sub_id].contract_amount,
        )

    def submit(self, sub_id, number, amount_requested, retainage_rate=None):
        kwargs = {}
        if retainage_rate is not None:
            kwargs["retainage_rate"] = retainage_rate
        return self.tracker.create_payment_application(
            number,
            PERIOD_END,
            sub_id,
            amount_requested,
            **kwargs,
        )

    def reject(self, sub_id, number, amount_requested, retainage_rate=None):
        pay_apps = dict(self.tracker.pay_apps)
        waivers = dict(self.tracker.waivers)
        with self.assertRaises(ValueError):
            self.submit(sub_id, number, amount_requested, retainage_rate)
        self.assert_unchanged(sub_id, pay_apps, waivers)
        release = self.tracker.can_release_payment(sub_id, number)
        if number not in {
            int(key.rsplit("-", 1)[1])
            for key in pay_apps
            if key.startswith(f"{sub_id}-")
        }:
            self.assertFalse(release["can_release"])
            self.assertNotIn("amount", release)

    def release_payable(self, sub_id, number):
        waiver_id = f"LW-{sub_id}-{number}"
        self.tracker.receive_waiver(waiver_id, "waiver-record.pdf", "PM")
        release = self.tracker.can_release_payment(sub_id, number)
        self.assertTrue(release["can_release"], release)
        self.assertIn("amount", release)
        stored = self.tracker.pay_apps[f"{sub_id}-{number}"]
        self.assertEqual(release["amount"], stored.amount_approved)
        contract = self.tracker.subcontractors[sub_id].contract_amount
        self.assertLessEqual(release["amount"], contract)
        self.assertLessEqual(self.approved_total(sub_id), contract)
        self.assertEqual(
            self.tracker.waivers[waiver_id].amount,
            stored.amount_approved,
        )
        return release["amount"]

    def test_requested_amount_above_contract_is_rejected(self):
        contract = 500_000.0
        self.add_sub("SUB-001", contract)
        # Default retainage is 10%, so this request would be stored as 540_000.
        self.reject("SUB-001", 1, 600_000.0)

    def test_rejected_request_stores_nothing_before_refusing(self):
        self.add_sub("SUB-001", 100_000.0)
        self.reject("SUB-001", 1, 150_000.0, retainage_rate=0.0)
        self.assertEqual(self.tracker.pay_apps, {})
        self.assertEqual(self.tracker.waivers, {})
        self.assertEqual(
            self.tracker.get_waiver_status_by_sub("SUB-001")["total_amount"],
            0,
        )

    def test_cumulative_pending_approvals_cannot_exceed_contract(self):
        contract = 500_000.0
        self.add_sub("SUB-001", contract)
        first = self.submit("SUB-001", 1, 400_000.0)
        self.assertEqual(first.amount_approved, 360_000.0)
        self.assertEqual(first.retainage, 40_000.0)
        # Each request is below the contract; the approved sum is not.
        self.reject("SUB-001", 2, 200_000.0)
        self.assertEqual(self.approved_total("SUB-001"), 360_000.0)
        self.assertEqual(self.release_payable("SUB-001", 1), 360_000.0)
        second = self.tracker.can_release_payment("SUB-001", 2)
        self.assertFalse(second["can_release"])
        self.assertNotIn("amount", second)

    def test_release_check_payable_stays_within_contract(self):
        contract = 100_000.0
        self.add_sub("SUB-001", contract)
        self.submit("SUB-001", 1, 50_000.0)
        self.assertEqual(self.release_payable("SUB-001", 1), 45_000.0)
        verified = self.tracker.verify_waiver("LW-SUB-001-1", "CA")
        self.assertEqual(verified.status, SKILL.WaiverStatus.VERIFIED)
        release = self.tracker.can_release_payment("SUB-001", 1)
        self.assertEqual(release["amount"], 45_000.0)
        self.assertLessEqual(release["amount"], contract)

    def test_cap_binds_approved_amount_when_gross_request_is_larger(self):
        # Retainage keeps the approved amount on the contract while the
        # requested amount is above it. That request is still payable.
        contract = 1_000.0
        self.add_sub("SUB-001", contract)
        pay_app = self.submit("SUB-001", 1, 2_000.0, retainage_rate=0.5)
        self.assertEqual(pay_app.amount_approved, contract)
        self.assertEqual(self.release_payable("SUB-001", 1), contract)

    def test_approved_amount_one_cent_over_contract_is_rejected(self):
        self.add_sub("SUB-001", 1_000.0)
        self.submit("SUB-001", 1, 2_000.0, retainage_rate=0.5)
        self.reject("SUB-001", 2, 0.02, retainage_rate=0.5)

    def test_exact_contract_amount_can_be_released(self):
        contract = 100_000.0
        self.add_sub("SUB-001", contract)
        pay_app = self.submit("SUB-001", 1, contract, retainage_rate=0.0)
        self.assertEqual(pay_app.amount_approved, contract)
        self.assertEqual(self.release_payable("SUB-001", 1), contract)

    def test_zero_retainage_request_above_contract_is_rejected(self):
        self.add_sub("SUB-001", 100_000.0)
        self.reject("SUB-001", 1, 100_000.01, retainage_rate=0.0)

    def test_negative_retainage_cannot_push_approved_amount_over_contract(self):
        # Same approval formula. A negative rate would store twice the request.
        self.add_sub("SUB-001", 1_000.0)
        self.reject("SUB-001", 1, 1_000.0, retainage_rate=-1.0)
        self.assertEqual(self.tracker.pay_apps, {})

    def test_cap_uses_the_subcontractor_on_the_pay_application(self):
        self.add_sub("SUB-A", 1_000.0)
        self.add_sub("SUB-B", 500_000.0)
        self.reject("SUB-A", 1, 50_000.0, retainage_rate=0.0)
        pay_app = self.submit("SUB-B", 1, 50_000.0, retainage_rate=0.0)
        self.assertEqual(pay_app.subcontractor_id, "SUB-B")
        self.assertEqual(self.release_payable("SUB-B", 1), 50_000.0)
        self.assertLessEqual(self.approved_total("SUB-A"), 1_000.0)
        other = self.tracker.can_release_payment("SUB-A", 1)
        self.assertFalse(other["can_release"])
        self.assertNotIn("amount", other)

    def test_replacing_a_pay_app_cannot_raise_it_above_the_contract(self):
        contract = 100_000.0
        self.add_sub("SUB-001", contract)
        original = self.submit("SUB-001", 1, 40_000.0, retainage_rate=0.0)
        self.assertEqual(original.amount_approved, 40_000.0)
        self.reject("SUB-001", 1, 150_000.0, retainage_rate=0.0)
        stored = self.tracker.pay_apps["SUB-001-1"]
        self.assertEqual(stored.amount_approved, 40_000.0)
        self.assertEqual(self.tracker.waivers["LW-SUB-001-1"].amount, 40_000.0)
        self.assertEqual(self.release_payable("SUB-001", 1), 40_000.0)

    def test_in_limit_replacement_counts_once_toward_the_cap(self):
        contract = 100_000.0
        self.add_sub("SUB-001", contract)
        self.submit("SUB-001", 1, 40_000.0, retainage_rate=0.0)
        replaced = self.submit("SUB-001", 1, contract, retainage_rate=0.0)
        self.assertEqual(replaced.amount_approved, contract)
        self.assertEqual(self.approved_total("SUB-001"), contract)
        self.assertEqual(len(self.tracker.pay_apps), 1)
        self.assertEqual(self.tracker.waivers["LW-SUB-001-1"].amount, contract)
        self.assertEqual(self.release_payable("SUB-001", 1), contract)

    def test_zero_contract_rejects_a_positive_approval(self):
        self.add_sub("SUB-001", 0.0)
        self.submit("SUB-001", 1, 0.0, retainage_rate=0.0)
        self.reject("SUB-001", 2, 0.01, retainage_rate=0.0)


if __name__ == "__main__":
    unittest.main()
