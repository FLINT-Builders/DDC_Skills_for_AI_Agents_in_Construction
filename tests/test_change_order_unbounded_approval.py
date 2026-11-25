"""Spec for change-order approval limits in the change-order-manager skill.

These tests execute the Python implementation embedded in
``4_DDC_Curated/Contract-Legal/change-order-manager/SKILL.md``. They fail
while approval accepts any status and any amount, and they pass once approval
is allowed only from a submitted or under-review order and the approved amount
cannot exceed the proposed amount.

A refused approval must leave the order, its notes, and the revised contract
in the change order log untouched.
"""

import unittest
from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "4_DDC_Curated"
    / "Contract-Legal"
    / "change-order-manager"
    / "SKILL.md"
)

ORIGINAL_CONTRACT = 1_000_000.0
INFLATED_AMOUNT = 10_000_000.0


def load_skill_namespace():
    """Execute the skill's implementation block, not a reimplementation."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.find(marker)
    if start < 0:
        raise RuntimeError(f"no Python implementation in {SKILL_PATH}")
    start += len(marker)
    end = text.find("\n```", start)
    if end < 0:
        raise RuntimeError(f"unterminated Python implementation in {SKILL_PATH}")
    source = text[start:end]
    if "def approve_change_order" not in source or "def generate_log" not in source:
        raise RuntimeError("implementation block is not the change order manager")
    namespace = {"__name__": "change_order_manager_skill"}
    exec(compile(source, str(SKILL_PATH), "exec"), namespace)
    return namespace


SKILL = load_skill_namespace()


def revised_contract_line(amount):
    return f"| **Revised Contract** | **${amount:,.2f}** |"


class SecondLookupSwap(dict):
    """First lookup returns the checked stand-in; later lookups return the stored order."""

    def __init__(self, source, key, checked, stored):
        super().__init__(source)
        self._key = key
        self._checked = checked
        self._stored = stored
        self.reads = 0

    def __getitem__(self, key):
        if key == self._key:
            self.reads += 1
            if self.reads == 1:
                return self._checked
            return self._stored
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key == self._key:
            return self[key]
        return super().get(key, default)


class ChangeOrderApprovalCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ChangeOrderManager = SKILL["ChangeOrderManager"]
        cls.ChangeOrder = SKILL["ChangeOrder"]
        cls.ChangeOrderStatus = SKILL["ChangeOrderStatus"]
        cls.ChangeType = SKILL["ChangeType"]

    def setUp(self):
        self.manager = self.ChangeOrderManager(
            project_id="PRJ-001",
            project_name="Office Tower",
            original_contract=ORIGINAL_CONTRACT,
        )

    def _proposed_order(self, title="Added receptacle", labor=1_000.0):
        """Draft order whose proposed amount comes from the skill's pricing."""
        change_order = self.manager.create_change_order(
            title=title,
            description="Field change requested by the contractor",
            change_type=self.ChangeType.FIELD_CONDITION,
            created_by="contractor",
        )
        self.manager.set_cost_breakdown(change_order.id, labor=labor)
        self.assertGreater(change_order.proposed_amount, 0)
        self.assertLess(change_order.proposed_amount, INFLATED_AMOUNT)
        self.assertEqual(change_order.status, self.ChangeOrderStatus.DRAFT)
        return change_order

    def _snapshot(self, change_order):
        return {
            "status": change_order.status,
            "proposed_amount": change_order.proposed_amount,
            "approved_amount": change_order.approved_amount,
            "approved_date": change_order.approved_date,
            "approved_time_days": change_order.approved_time_days,
            "submitted_date": change_order.submitted_date,
            "executed_date": change_order.executed_date,
            "notes": [dict(note) for note in change_order.notes],
        }

    def _assert_revised_contract(self, expected):
        summary = self.manager.get_summary()
        self.assertEqual(summary["revised_contract"], expected)
        self.assertEqual(summary["total_approved"], expected - ORIGINAL_CONTRACT)
        log = self.manager.generate_log()
        self.assertIn(revised_contract_line(expected), log)
        self.assertNotIn(revised_contract_line(expected + INFLATED_AMOUNT), log)

    def _assert_approval_refused(self, change_order, amount, approved_time=30):
        before = self._snapshot(change_order)
        revised_before = self.manager.get_summary()["revised_contract"]
        with self.assertRaises(ValueError):
            self.manager.approve_change_order(
                change_order.id, amount, approved_time
            )
        self.assertEqual(self._snapshot(change_order), before)
        self._assert_revised_contract(revised_before)

    def test_draft_approval_does_not_inflate_revised_contract_log(self):
        """Attack path: draft, approve far above the proposal, read the log."""
        change_order = self._proposed_order()
        proposed = change_order.proposed_amount

        self._assert_approval_refused(change_order, INFLATED_AMOUNT)

        self.assertEqual(change_order.status, self.ChangeOrderStatus.DRAFT)
        self.assertEqual(change_order.approved_amount, 0.0)
        self.assertEqual(self.manager.get_summary()["revised_contract"], ORIGINAL_CONTRACT)
        self.assertNotIn(f"${INFLATED_AMOUNT:,.0f}", self.manager.generate_log())
        self.assertLess(proposed, INFLATED_AMOUNT)

    def test_line_item_draft_approval_does_not_inflate_revised_contract(self):
        """Same approval gap when the proposal is built from line items."""
        change_order = self.manager.create_change_order(
            title="Steel beam",
            description="Added beam",
            change_type=self.ChangeType.DESIGN_ERROR,
            created_by="contractor",
        )
        self.manager.add_line_item(
            change_order.id,
            "W12x26 Steel Beam",
            quantity=10,
            unit="LF",
            unit_price=85.00,
        )
        self.assertGreater(change_order.proposed_amount, 0)
        self.assertLess(change_order.proposed_amount, INFLATED_AMOUNT)
        self._assert_approval_refused(change_order, INFLATED_AMOUNT)

    def test_submitted_amount_above_proposal_is_refused_without_mutation(self):
        change_order = self._proposed_order()
        self.manager.submit_change_order(change_order.id)
        above_proposal = change_order.proposed_amount + 0.01

        self._assert_approval_refused(change_order, above_proposal)

        self.assertEqual(change_order.status, self.ChangeOrderStatus.SUBMITTED)
        self.assertEqual(len(change_order.notes), 1)

    def test_submitted_amount_at_or_below_proposal_updates_log(self):
        at_proposal = self._proposed_order(title="At proposal")
        self.manager.submit_change_order(at_proposal.id)
        approved = self.manager.approve_change_order(
            at_proposal.id, at_proposal.proposed_amount, approved_time=5
        )

        self.assertIs(approved, at_proposal)
        self.assertEqual(at_proposal.status, self.ChangeOrderStatus.APPROVED)
        self.assertEqual(at_proposal.approved_amount, at_proposal.proposed_amount)
        self.assertEqual(at_proposal.approved_time_days, 5)
        self.assertIsNotNone(at_proposal.approved_date)
        self._assert_revised_contract(ORIGINAL_CONTRACT + at_proposal.proposed_amount)

        negotiated = self._proposed_order(title="Negotiated down", labor=2_000.0)
        self.manager.submit_change_order(negotiated.id)
        reduced = negotiated.proposed_amount - 1.0
        self.manager.approve_change_order(negotiated.id, reduced, approved_time=0)

        self.assertEqual(negotiated.status, self.ChangeOrderStatus.APPROVED)
        self.assertEqual(negotiated.approved_amount, reduced)
        self._assert_revised_contract(
            ORIGINAL_CONTRACT + at_proposal.proposed_amount + reduced
        )

    def test_under_review_amount_within_proposal_is_approved(self):
        """Under-review is an allowed source status. No public transition sets it."""
        change_order = self._proposed_order()
        change_order.status = self.ChangeOrderStatus.UNDER_REVIEW

        self.manager.approve_change_order(
            change_order.id, change_order.proposed_amount, approved_time=2
        )

        self.assertEqual(change_order.status, self.ChangeOrderStatus.APPROVED)
        self._assert_revised_contract(ORIGINAL_CONTRACT + change_order.proposed_amount)

    def test_under_review_amount_above_proposal_is_refused(self):
        change_order = self._proposed_order()
        change_order.status = self.ChangeOrderStatus.UNDER_REVIEW
        self._assert_approval_refused(
            change_order, change_order.proposed_amount + 1_000.0
        )
        self.assertEqual(change_order.status, self.ChangeOrderStatus.UNDER_REVIEW)

    def test_rejected_order_cannot_be_approved_for_any_amount(self):
        change_order = self._proposed_order()
        self.manager.submit_change_order(change_order.id)
        self.manager.reject_change_order(change_order.id, "Scope not accepted")

        self._assert_approval_refused(change_order, change_order.proposed_amount)
        self._assert_approval_refused(change_order, INFLATED_AMOUNT)
        self.assertEqual(change_order.status, self.ChangeOrderStatus.REJECTED)

    def test_second_approval_cannot_replace_amount_after_approval(self):
        change_order = self._proposed_order()
        self.manager.submit_change_order(change_order.id)
        self.manager.approve_change_order(
            change_order.id, change_order.proposed_amount, approved_time=4
        )
        self._assert_approval_refused(change_order, INFLATED_AMOUNT, approved_time=99)
        self.assertEqual(change_order.status, self.ChangeOrderStatus.APPROVED)
        self.assertEqual(change_order.approved_amount, change_order.proposed_amount)
        self.assertEqual(change_order.approved_time_days, 4)
        self._assert_revised_contract(ORIGINAL_CONTRACT + change_order.proposed_amount)

    def test_executed_order_cannot_be_reapproved_for_a_larger_amount(self):
        change_order = self._proposed_order()
        proposed = change_order.proposed_amount
        self.manager.submit_change_order(change_order.id)
        self.manager.approve_change_order(change_order.id, proposed, approved_time=3)
        self.manager.execute_change_order(change_order.id)

        self._assert_approval_refused(change_order, INFLATED_AMOUNT)
        self.assertEqual(change_order.status, self.ChangeOrderStatus.EXECUTED)
        self.assertEqual(change_order.approved_amount, proposed)
        self._assert_revised_contract(ORIGINAL_CONTRACT + proposed)

    def test_pre_submission_statuses_cannot_be_approved_even_at_proposal(self):
        """Pricing, negotiating, and void are not approval sources."""
        for status in (
            self.ChangeOrderStatus.PRICING,
            self.ChangeOrderStatus.NEGOTIATING,
            self.ChangeOrderStatus.VOID,
        ):
            with self.subTest(status=status):
                change_order = self._proposed_order(title=status.value)
                change_order.status = status
                self._assert_approval_refused(
                    change_order, change_order.proposed_amount
                )
                self.assertEqual(change_order.status, status)

    def test_inflated_draft_does_not_change_a_legitimate_revised_contract(self):
        legitimate = self._proposed_order(title="Accepted extra")
        proposed = legitimate.proposed_amount
        self.manager.submit_change_order(legitimate.id)
        self.manager.approve_change_order(legitimate.id, proposed, approved_time=1)

        inflated = self._proposed_order(title="Unsubmitted extra", labor=100.0)
        self._assert_approval_refused(inflated, INFLATED_AMOUNT)

        self._assert_revised_contract(ORIGINAL_CONTRACT + proposed)
        log = self.manager.generate_log()
        self.assertIn(legitimate.title[:30], log)
        self.assertNotIn(f"${INFLATED_AMOUNT:,.0f}", log)

    def test_unknown_change_order_approval_does_not_create_a_record(self):
        before = self.manager.get_summary()["revised_contract"]
        with self.assertRaises(ValueError):
            self.manager.approve_change_order("CO-MISSING", INFLATED_AMOUNT, 12)
        self.assertEqual(self.manager.change_orders, {})
        self._assert_revised_contract(before)

    def test_approval_writes_the_order_that_was_checked(self):
        """A second lookup between the check and the update must not approve another order."""
        stored = self._proposed_order()
        before = self._snapshot(stored)
        checked = self.ChangeOrder(
            id=stored.id,
            number=stored.number,
            title=stored.title,
            description=stored.description,
            change_type=stored.change_type,
            status=self.ChangeOrderStatus.SUBMITTED,
            identified_date=stored.identified_date,
            proposed_amount=INFLATED_AMOUNT,
        )
        self.manager.change_orders = SecondLookupSwap(
            self.manager.change_orders, stored.id, checked, stored
        )

        try:
            self.manager.approve_change_order(stored.id, INFLATED_AMOUNT, 9)
        except ValueError:
            pass

        self.assertEqual(self._snapshot(stored), before)
        self.assertEqual(stored.status, self.ChangeOrderStatus.DRAFT)
        self.assertEqual(stored.approved_amount, 0.0)
        self._assert_revised_contract(ORIGINAL_CONTRACT)

    def test_execute_requires_an_approval_and_does_not_inflate_the_log(self):
        change_order = self._proposed_order()
        before = self._snapshot(change_order)
        with self.assertRaises(ValueError):
            self.manager.execute_change_order(change_order.id)
        self.assertEqual(self._snapshot(change_order), before)
        self._assert_revised_contract(ORIGINAL_CONTRACT)


if __name__ == "__main__":
    unittest.main()
