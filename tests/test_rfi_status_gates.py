"""Status gates for the rfi-management skill.

The implementation under test is the Python block in
1_DDC_Toolkit/Document-Control/rfi-management/SKILL.md. These tests load that
block and call it. They fail while respond_to_rfi and close_rfi accept any
current status.

Spec:
- respond_to_rfi is allowed only when the same RFI object is Open.
- close_rfi is allowed only when the same RFI object is Answered.
- A refused call raises ValueError and must not change that RFI, rewrite the
  log, or print a notification. Writing the file and then raising is a failure.
- A second lookup of the RFI between the status check and the update is a
  failure: the object whose status was checked is the only object that may change.

Not covered here, because they are not status-transition operations on this
class:
- There is no method that sets Pending Review or Void. Those values are only
  fixtures passed into respond_to_rfi and close_rfi.
- Direct writes to RFI fields by the holder of the dataclass.
- The n8n example, which only formats a notification and does not change status.
- Binding cost or schedule impact to an authenticated reviewer. response_by and
  submitted_by are caller-supplied strings, not a principal, so a name compare
  would not be the check these tests require.

Run:
    python3 -m unittest discover -s tests -p 'test_rfi_status_gates.py' -v
"""

import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "1_DDC_Toolkit" / "Document-Control" / "rfi-management" / "SKILL.md"


def load_skill():
    text = SKILL.read_text(encoding="utf-8")
    marker = "## Python Implementation"
    marker_at = text.index(marker)
    fence = text.index("```python", marker_at)
    code_start = text.index("\n", fence) + 1
    code_end = text.index("\n```", code_start)
    code = text[code_start:code_end]
    namespace = {"__name__": "rfi_management_skill"}
    exec(compile(code, str(SKILL), "exec"), namespace)
    namespace["__source__"] = code
    return namespace


class FirstLookupMap(dict):
    """First lookup of the authorized number returns that RFI.

    Every later lookup of the same number returns a different object. A
    transition that checks one object and updates another fails the caller.
    """

    def __init__(self, authorized, decoy):
        super().__init__({authorized.rfi_number: authorized})
        self.authorized = authorized
        self.decoy = decoy
        self.lookups = 0

    def _resolve(self, key):
        self.lookups += 1
        if key != self.authorized.rfi_number:
            return dict.get(self, key)
        if self.lookups == 1:
            return self.authorized
        return self.decoy

    def get(self, key, default=None):
        found = self._resolve(key)
        if found is None:
            return default
        return found

    def __getitem__(self, key):
        found = self._resolve(key)
        if found is None:
            raise KeyError(key)
        return found


def snapshot(rfi):
    return {
        "status": rfi.status,
        "response": rfi.response,
        "response_by": rfi.response_by,
        "date_responded": rfi.date_responded,
        "date_closed": rfi.date_closed,
        "date_submitted": rfi.date_submitted,
        "cost_impact": rfi.cost_impact,
        "cost_amount": rfi.cost_amount,
        "schedule_impact": rfi.schedule_impact,
        "schedule_days": rfi.schedule_days,
        "change_order_ref": rfi.change_order_ref,
        "attachments": list(rfi.attachments),
    }


def fingerprint(path):
    data = Path(path).read_bytes()
    stat = Path(path).stat()
    return (hashlib.sha256(data).hexdigest(), stat.st_mtime_ns, stat.st_size)


class RFIStatusGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = load_skill()
        cls.ns = ns
        cls.Manager = ns["RFIManager"]
        cls.Status = ns["RFIStatus"]
        cls.Priority = ns["RFIPriority"]
        source = ns["__source__"]
        if "def respond_to_rfi" not in source or "def close_rfi" not in source:
            raise AssertionError("skill implementation block did not contain the RFI transitions")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "rfi_log.xlsx")
        self.manager = self.Manager("PROJECT-2026-001", storage_path=self.path)

    def _new_rfi(self, subject="Panel location"):
        return self.manager.create_rfi(
            subject=subject,
            question="Which room is the panel in?",
            submitted_by="Casey Contractor",
            submitted_by_company="Asker Co",
            assigned_to="Architect",
            priority=self.Priority.HIGH,
            days_for_response=5,
        )

    def _set_status(self, rfi, status):
        rfi.status = status
        self.manager._save_rfis()

    def _respond(self, number):
        return self.manager.respond_to_rfi(
            number,
            response="Room 105 is the official location.",
            response_by="Casey Contractor",
            attachments=["forged-direction.pdf"],
            cost_impact=True,
            cost_amount=125000.0,
            schedule_impact=True,
            schedule_days=14,
        )

    def _assert_refused(self, rfi, call):
        before_rfi = snapshot(rfi)
        before_file = fingerprint(self.path)
        stdout = io.StringIO()
        refused = False
        with redirect_stdout(stdout):
            try:
                call()
            except ValueError:
                refused = True
            except Exception as exc:
                self.fail(f"expected ValueError, got {type(exc).__name__}: {exc}")
        self.assertTrue(
            refused,
            "transition succeeded for status %s; log would record response %r "
            "cost %s schedule %s"
            % (rfi.status, rfi.response, rfi.cost_amount, rfi.schedule_days),
        )
        self.assertEqual(snapshot(rfi), before_rfi)
        self.assertEqual(fingerprint(self.path), before_file)
        self.assertEqual(stdout.getvalue(), "")
        reloaded = self.Manager("PROJECT-2026-001", storage_path=self.path)
        loaded = reloaded.rfis[rfi.rfi_number]
        self.assertEqual(loaded.status, before_rfi["status"])

    def test_respond_refuses_every_status_except_open(self):
        refused = [
            self.Status.DRAFT,
            self.Status.PENDING,
            self.Status.ANSWERED,
            self.Status.CLOSED,
            self.Status.VOID,
        ]
        for status in refused:
            with self.subTest(status=status.value):
                self.setUp()
                rfi = self._new_rfi(subject=f"Respond from {status.value}")
                if status != self.Status.DRAFT:
                    self._set_status(rfi, status)
                self._assert_refused(rfi, lambda: self._respond(rfi.rfi_number))

    def test_close_refuses_every_status_except_answered(self):
        refused = [
            self.Status.DRAFT,
            self.Status.OPEN,
            self.Status.PENDING,
            self.Status.CLOSED,
            self.Status.VOID,
        ]
        for status in refused:
            with self.subTest(status=status.value):
                self.setUp()
                rfi = self._new_rfi(subject=f"Close from {status.value}")
                if status == self.Status.OPEN:
                    with redirect_stdout(io.StringIO()):
                        self.manager.submit_rfi(rfi.rfi_number)
                elif status != self.Status.DRAFT:
                    self._set_status(rfi, status)
                self._assert_refused(
                    rfi,
                    lambda: self.manager.close_rfi(rfi.rfi_number, change_order_ref="CO-99"),
                )

    def test_attack_path_cannot_answer_and_close_a_draft(self):
        rfi = self._new_rfi()
        self.assertEqual(rfi.status, self.Status.DRAFT)
        self._assert_refused(rfi, lambda: self._respond(rfi.rfi_number))
        self._assert_refused(
            rfi,
            lambda: self.manager.close_rfi(rfi.rfi_number, change_order_ref="CO-99"),
        )
        self.assertEqual(rfi.status, self.Status.DRAFT)
        self.assertEqual(rfi.cost_amount, 0.0)
        self.assertEqual(rfi.schedule_days, 0)
        self.assertEqual(rfi.response, "")
        self.assertIsNone(rfi.date_closed)

    def test_open_rfi_can_be_answered_and_then_closed(self):
        rfi = self._new_rfi()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.manager.submit_rfi(rfi.rfi_number)
            answered = self._respond(rfi.rfi_number)
            self.assertEqual(answered.status, self.Status.ANSWERED)
            self.assertEqual(answered.response, "Room 105 is the official location.")
            self.assertEqual(answered.cost_amount, 125000.0)
            self.assertEqual(answered.schedule_days, 14)
            self.assertIn("forged-direction.pdf", answered.attachments)
            closed = self.manager.close_rfi(rfi.rfi_number, change_order_ref="CO-7")
        self.assertIs(closed, rfi)
        self.assertEqual(closed.status, self.Status.CLOSED)
        self.assertEqual(closed.change_order_ref, "CO-7")
        self.assertIsNotNone(closed.date_closed)
        reloaded = self.Manager("PROJECT-2026-001", storage_path=self.path)
        loaded = reloaded.rfis[rfi.rfi_number]
        self.assertEqual(loaded.status, self.Status.CLOSED)

    def test_submit_still_requires_a_draft(self):
        rfi = self._new_rfi()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.manager.submit_rfi(rfi.rfi_number)
        self.assertEqual(rfi.status, self.Status.OPEN)
        before = snapshot(rfi)
        before_file = fingerprint(self.path)
        with self.assertRaises(ValueError):
            self.manager.submit_rfi(rfi.rfi_number)
        self.assertEqual(snapshot(rfi), before)
        self.assertEqual(fingerprint(self.path), before_file)

    def test_missing_rfi_does_not_create_a_log_row(self):
        rfi = self._new_rfi()
        before = fingerprint(self.path)
        before_numbers = set(self.manager.rfis)
        with self.assertRaises(ValueError):
            self._respond("RFI-9999")
        with self.assertRaises(ValueError):
            self.manager.close_rfi("RFI-9999", change_order_ref="CO-1")
        self.assertEqual(fingerprint(self.path), before)
        self.assertEqual(set(self.manager.rfis), before_numbers)
        self.assertEqual(rfi.status, self.Status.DRAFT)

    def test_respond_updates_only_the_rfi_whose_status_was_checked(self):
        rfi = self._new_rfi()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.manager.submit_rfi(rfi.rfi_number)
        decoy = deepcopy(rfi)
        decoy.status = self.Status.DRAFT
        decoy.response = ""
        decoy.response_by = ""
        decoy.date_responded = None
        decoy.cost_impact = False
        decoy.cost_amount = 0.0
        decoy.schedule_impact = False
        decoy.schedule_days = 0
        decoy.attachments = []
        self.manager.rfis = FirstLookupMap(rfi, decoy)
        with redirect_stdout(io.StringIO()):
            updated = self._respond(rfi.rfi_number)
        self.assertIs(updated, rfi)
        self.assertEqual(rfi.status, self.Status.ANSWERED)
        self.assertEqual(decoy.status, self.Status.DRAFT)
        self.assertEqual(decoy.response, "")
        self.assertEqual(decoy.cost_amount, 0.0)
        self.assertEqual(decoy.schedule_days, 0)
        self.assertEqual(decoy.attachments, [])
        self.assertIsNone(decoy.date_responded)

    def test_close_updates_only_the_rfi_whose_status_was_checked(self):
        rfi = self._new_rfi()
        with redirect_stdout(io.StringIO()):
            self.manager.submit_rfi(rfi.rfi_number)
            self._respond(rfi.rfi_number)
        decoy = deepcopy(rfi)
        decoy.status = self.Status.DRAFT
        decoy.date_closed = None
        decoy.change_order_ref = ""
        self.manager.rfis = FirstLookupMap(rfi, decoy)
        updated = self.manager.close_rfi(rfi.rfi_number, change_order_ref="CO-7")
        self.assertIs(updated, rfi)
        self.assertEqual(rfi.status, self.Status.CLOSED)
        self.assertEqual(rfi.change_order_ref, "CO-7")
        self.assertEqual(decoy.status, self.Status.DRAFT)
        self.assertEqual(decoy.change_order_ref, "")
        self.assertIsNone(decoy.date_closed)


if __name__ == "__main__":
    unittest.main()
