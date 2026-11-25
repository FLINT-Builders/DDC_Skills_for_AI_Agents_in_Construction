"""Release checks for the lien waiver tracker.

The implementation under test is the LienWaiverTracker class in
4_DDC_Curated/Contract-Legal/lien-waiver-tracker/SKILL.md. These tests
load that block and call it. They describe the release behavior the
skill claims to enforce: a payment is releasable only when the conditional
waiver for that same pay application is verified, its amount matches the
approved pay application amount, and its file is a non-empty file at the
moment of the check.

Recording a file path, or passing a name to receive_waiver, is not
verification. LienWaiver has no signature field, so these tests do not
invent a signature format. They do require the release check to read the
current file and the current amounts, rather than trust a path string or
an earlier status write.

Untested operations that do not make this release decision:
- generate_report, get_missing_waivers, and get_waiver_status_by_sub
- calculate_exposure's conditional_pending figure (it does not decide release)
- the separate subcontractor-payment-tracker and retention-tracker skills,
  which never call LienWaiverTracker

Run from the repository root:

    python3 -m unittest tests.test_lien_waiver_release -v
"""

import math
import os
import re
import tempfile
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


def load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    source = next(
        (block for block in blocks if "class LienWaiverTracker" in block),
        None,
    )
    if source is None:
        raise AssertionError(f"No LienWaiverTracker implementation in {SKILL_PATH}")
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(source, str(SKILL_PATH), "exec"), namespace)
    return namespace


SKILL = load_skill_namespace()
LienWaiverTracker = SKILL["LienWaiverTracker"]
WaiverStatus = SKILL["WaiverStatus"]
WaiverType = SKILL["WaiverType"]
PaymentStatus = SKILL["PaymentStatus"]


def _amounts_match(left, right):
    return math.isclose(left, right, rel_tol=0.0, abs_tol=0.01)


class LienWaiverReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tracker = LienWaiverTracker("PRJ-001", "Office Tower")
        self.tracker.add_subcontractor(
            "SUB-001",
            "ABC Mechanical",
            "HVAC",
            contract_amount=500000,
            contact_name="John Smith",
            contact_email="john@abcmech.example",
        )

    def _waiver_file(self, name, contents=b"conditional-progress-waiver\n"):
        path = Path(self.tmp.name) / name
        path.write_bytes(contents)
        return str(path)

    def _pay_app(self, number=1, amount_requested=50000, subcontractor_id="SUB-001"):
        return self.tracker.create_payment_application(
            number=number,
            period_end=datetime(2026, 3, 31),
            subcontractor_id=subcontractor_id,
            amount_requested=amount_requested,
        )

    def _listing(self):
        root = Path(self.tmp.name)
        listing = {}
        for path in root.rglob("*"):
            if path.is_file():
                listing[str(path)] = path.read_bytes()
        return listing

    def assert_held(self, result, approved_amount):
        problems = []
        if result.get("can_release"):
            problems.append("can_release is true")
        if result.get("amount") == approved_amount:
            problems.append(f"returns approved amount {approved_amount}")
        if result.get("reason") == "All waivers verified":
            problems.append("reason claims all waivers are verified")
        if problems:
            self.fail(f"{'; '.join(problems)}; result={result!r}")

    def assert_no_file_side_effects(self, before, result_files):
        self.assertEqual(before, result_files)

    def test_skill_module_loads_tracker(self):
        self.assertTrue(callable(LienWaiverTracker))
        self.assertIn("can_release_payment", LienWaiverTracker.__dict__)
        self.assertIn("receive_waiver", LienWaiverTracker.__dict__)
        self.assertIn("verify_waiver", LienWaiverTracker.__dict__)

    def test_requested_waiver_blocks_release(self):
        pay_app = self._pay_app()
        result = self.tracker.can_release_payment("SUB-001", 1)
        self.assert_held(result, pay_app.amount_approved)

    def test_rejected_waiver_blocks_release(self):
        pay_app = self._pay_app()
        self.tracker.reject_waiver("LW-SUB-001-1", "wrong form")
        result = self.tracker.can_release_payment("SUB-001", 1)
        self.assert_held(result, pay_app.amount_approved)

    def test_missing_pay_app_blocks_release(self):
        result = self.tracker.can_release_payment("SUB-001", 99)
        self.assertFalse(result["can_release"])
        self.assertNotIn("amount", result)

    def test_verified_matching_waiver_file_can_release(self):
        path = self._waiver_file("pa1.pdf")
        pay_app = self._pay_app()
        waiver = self.tracker.receive_waiver("LW-SUB-001-1", path)
        self.assertEqual(waiver.status, WaiverStatus.RECEIVED)
        self.assertFalse(pay_app.waivers_complete)
        self.tracker.verify_waiver("LW-SUB-001-1", "controller")
        self.tracker._check_pay_app_waivers("SUB-001", 1)
        self.assertTrue(pay_app.waivers_complete)
        self.assertTrue(
            _amounts_match(
                self.tracker.waivers["LW-SUB-001-1"].amount,
                pay_app.amount_approved,
            )
        )
        result = self.tracker.can_release_payment("SUB-001", 1)
        self.assertTrue(result["can_release"])
        self.assertTrue(_amounts_match(result["amount"], pay_app.amount_approved))
        self.assertEqual(result["reason"], "All waivers verified")

    def test_received_file_without_verify_does_not_release(self):
        path = self._waiver_file("received-only.pdf")
        pay_app = self._pay_app()
        before = self._listing()
        self.tracker.receive_waiver("LW-SUB-001-1", path, verified_by="clerk")
        waiver = self.tracker.waivers["LW-SUB-001-1"]
        self.assertEqual(waiver.status, WaiverStatus.RECEIVED)
        self.assertEqual(waiver.file_path, path)
        result = self.tracker.can_release_payment("SUB-001", 1)
        self.assert_no_file_side_effects(before, self._listing())
        self.assert_held(result, pay_app.amount_approved)
        self.assertFalse(pay_app.waivers_complete)
        self.assertTrue(Path(path).read_bytes())

    def test_received_missing_file_does_not_release(self):
        pay_app = self._pay_app()
        missing = str(Path(self.tmp.name) / "does-not-exist.pdf")
        before = self._listing()
        self.tracker.receive_waiver("LW-SUB-001-1", missing)
        result = self.tracker.can_release_payment("SUB-001", 1)
        self.assert_no_file_side_effects(before, self._listing())
        self.assertFalse(Path(missing).exists())
        self.assert_held(result, pay_app.amount_approved)
        self.assertFalse(pay_app.waivers_complete)

    def test_amount_is_compared_again_at_release(self):
        path = self._waiver_file("amount.pdf")
        pay_app = self._pay_app()
        approved = pay_app.amount_approved
        self.tracker.receive_waiver("LW-SUB-001-1", path)
        self.tracker.verify_waiver("LW-SUB-001-1", "controller")
        self.tracker.waivers["LW-SUB-001-1"].amount = 1.0
        before = self._listing()
        result = self.tracker.can_release_payment("SUB-001", 1)
        self.assert_no_file_side_effects(before, self._listing())
        self.assert_held(result, approved)
        self.assertNotEqual(result.get("amount"), 1.0)
        self.tracker._check_pay_app_waivers("SUB-001", 1)
        self.assertFalse(pay_app.waivers_complete)

    def test_pay_app_amount_is_read_again_at_release(self):
        path = self._waiver_file("pay-app-amount.pdf")
        pay_app = self._pay_app()
        self.tracker.receive_waiver("LW-SUB-001-1", path)
        self.tracker.verify_waiver("LW-SUB-001-1", "controller")
        pay_app.amount_approved = pay_app.amount_approved + 1000
        result = self.tracker.can_release_payment("SUB-001", 1)
        self.assert_held(result, pay_app.amount_approved)
        self.tracker._check_pay_app_waivers("SUB-001", 1)
        self.assertFalse(pay_app.waivers_complete)

    def test_file_removed_after_verify_blocks_release(self):
        path = self._waiver_file("vanishes.pdf")
        pay_app = self._pay_app()
        self.tracker.receive_waiver("LW-SUB-001-1", path)
        self.tracker.verify_waiver("LW-SUB-001-1", "controller")
        os.remove(path)
        before = self._listing()
        result = self.tracker.can_release_payment("SUB-001", 1)
        self.assert_no_file_side_effects(before, self._listing())
        self.assert_held(result, pay_app.amount_approved)
        self.tracker._check_pay_app_waivers("SUB-001", 1)
        self.assertFalse(pay_app.waivers_complete)

    def test_file_emptied_after_verify_blocks_release(self):
        path = self._waiver_file("emptied.pdf")
        pay_app = self._pay_app()
        self.tracker.receive_waiver("LW-SUB-001-1", path)
        self.tracker.verify_waiver("LW-SUB-001-1", "controller")
        Path(path).write_bytes(b"")
        result = self.tracker.can_release_payment("SUB-001", 1)
        self.assert_held(result, pay_app.amount_approved)
        self.assertEqual(Path(path).read_bytes(), b"")
        self.tracker._check_pay_app_waivers("SUB-001", 1)
        self.assertFalse(pay_app.waivers_complete)

    def test_directory_path_does_not_verify_the_waiver(self):
        folder = Path(self.tmp.name) / "not-a-waiver"
        folder.mkdir()
        pay_app = self._pay_app()
        self.tracker.receive_waiver("LW-SUB-001-1", str(folder))
        self.tracker.verify_waiver("LW-SUB-001-1", "controller")
        result = self.tracker.can_release_payment("SUB-001", 1)
        self.assert_held(result, pay_app.amount_approved)
        self.assertTrue(folder.is_dir())

    def test_sibling_pay_app_still_received_is_not_released(self):
        first_path = self._waiver_file("pa1.pdf")
        second_path = self._waiver_file("pa2.pdf")
        first = self._pay_app(number=1, amount_requested=50000)
        second = self._pay_app(number=2, amount_requested=80000)
        self.tracker.receive_waiver("LW-SUB-001-1", first_path)
        self.tracker.verify_waiver("LW-SUB-001-1", "controller")
        self.tracker.receive_waiver("LW-SUB-001-2", second_path)
        released = self.tracker.can_release_payment("SUB-001", 1)
        held = self.tracker.can_release_payment("SUB-001", 2)
        self.assertTrue(released["can_release"])
        self.assertTrue(_amounts_match(released["amount"], first.amount_approved))
        self.assert_held(held, second.amount_approved)
        self.assertNotEqual(held.get("amount"), first.amount_approved)
        self.assertFalse(second.waivers_complete)

    def test_other_subcontractor_received_waiver_does_not_release(self):
        self.tracker.add_subcontractor(
            "SUB-002",
            "XYZ Electric",
            "Electrical",
            contract_amount=350000,
            contact_name="Jane Doe",
            contact_email="jane@xyzelectric.example",
        )
        verified_path = self._waiver_file("sub1.pdf")
        received_path = self._waiver_file("sub2.pdf")
        self._pay_app(number=1, amount_requested=50000, subcontractor_id="SUB-001")
        other = self._pay_app(number=1, amount_requested=40000, subcontractor_id="SUB-002")
        self.tracker.receive_waiver("LW-SUB-001-1", verified_path)
        self.tracker.verify_waiver("LW-SUB-001-1", "controller")
        self.tracker.receive_waiver("LW-SUB-002-1", received_path)
        held = self.tracker.can_release_payment("SUB-002", 1)
        self.assert_held(held, other.amount_approved)
        self.assertFalse(other.waivers_complete)

    def test_received_unconditional_does_not_reduce_exposure(self):
        path = self._waiver_file("unconditional.pdf")
        pay_app = self._pay_app(amount_requested=100000)
        pay_app.status = PaymentStatus.RELEASED
        unconditional = self.tracker.convert_to_unconditional(
            "LW-SUB-001-1", datetime(2026, 4, 15)
        )
        self.tracker.receive_waiver(unconditional.id, path)
        exposure = self.tracker.calculate_exposure("SUB-001")
        self.assertEqual(exposure.unconditional_waivers, 0)
        self.assertTrue(_amounts_match(exposure.total_paid, pay_app.amount_approved))
        self.assertTrue(_amounts_match(exposure.exposure, pay_app.amount_approved))

    def test_missing_file_does_not_reduce_exposure_after_verify(self):
        path = self._waiver_file("unconditional-gone.pdf")
        pay_app = self._pay_app(amount_requested=100000)
        pay_app.status = PaymentStatus.RELEASED
        unconditional = self.tracker.convert_to_unconditional(
            "LW-SUB-001-1", datetime(2026, 4, 15)
        )
        self.tracker.receive_waiver(unconditional.id, path)
        self.tracker.verify_waiver(unconditional.id, "controller")
        os.remove(path)
        exposure = self.tracker.calculate_exposure("SUB-001")
        self.assertEqual(exposure.unconditional_waivers, 0)
        self.assertTrue(_amounts_match(exposure.exposure, pay_app.amount_approved))

    def test_verified_unconditional_file_reduces_exposure(self):
        path = self._waiver_file("unconditional-ok.pdf")
        pay_app = self._pay_app(amount_requested=100000)
        pay_app.status = PaymentStatus.RELEASED
        unconditional = self.tracker.convert_to_unconditional(
            "LW-SUB-001-1", datetime(2026, 4, 15)
        )
        self.assertTrue(_amounts_match(unconditional.amount, pay_app.amount_approved))
        self.tracker.receive_waiver(unconditional.id, path)
        self.tracker.verify_waiver(unconditional.id, "controller")
        exposure = self.tracker.calculate_exposure("SUB-001")
        self.assertTrue(
            _amounts_match(exposure.unconditional_waivers, pay_app.amount_approved)
        )
        self.assertTrue(_amounts_match(exposure.exposure, 0))


if __name__ == "__main__":
    unittest.main()
