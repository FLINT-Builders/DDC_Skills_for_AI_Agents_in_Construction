"""Approval integrity for the change-order-manager skill.

The tests load the Python implementation embedded in
``4_DDC_Curated/Contract-Legal/change-order-manager/SKILL.md`` and drive
``ChangeOrderManager``. They fail while a non-draft change order still
accepts line-item or cost-breakdown edits, or while the formal document
prints the live cost breakdown instead of the stored approved amount.

A later fix should make this file pass without changing these tests.
Refusal must be a ``ValueError`` raised before any edit is stored, and the
order that is edited must be the same order whose status was checked.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "4_DDC_Curated"
    / "Contract-Legal"
    / "change-order-manager"
    / "SKILL.md"
)

AGREED_AMOUNT = 500.0
DECOY_APPROVED_AMOUNT = 999_999.25
MALICIOUS_DESCRIPTION = "Unauthorized extra work"
MALICIOUS_QTY = 10_000.0
MALICIOUS_UNIT_PRICE = 250.0
RELOOKUP_DESCRIPTION = "RELOOKUP-DECOY-LINE"


@lru_cache(maxsize=1)
def load_skill():
    """Execute the skill's implementation block and return its globals."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    implementation = next(
        (block for block in blocks if "class ChangeOrderManager" in block),
        None,
    )
    if implementation is None:
        raise AssertionError(f"no ChangeOrderManager implementation in {SKILL_PATH}")
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(implementation, str(SKILL_PATH), "exec"), namespace)
    return namespace


def snapshot(change_order):
    breakdown = change_order.cost_breakdown
    return {
        "status": change_order.status,
        "approved_amount": change_order.approved_amount,
        "proposed_amount": change_order.proposed_amount,
        "approved_time_days": change_order.approved_time_days,
        "proposed_time_days": change_order.proposed_time_days,
        "line_items_id": id(change_order.line_items),
        "breakdown_id": id(breakdown),
        "items": tuple(
            (
                item.id,
                item.description,
                item.quantity,
                item.unit,
                item.unit_price,
                item.total_price,
            )
            for item in change_order.line_items
        ),
        "breakdown": (
            breakdown.labor,
            breakdown.materials,
            breakdown.equipment,
            breakdown.subcontractor,
            breakdown.overhead,
            breakdown.profit,
            breakdown.bond,
            breakdown.total,
        ),
        "notes": tuple(note.get("text") for note in change_order.notes),
        "references": (
            tuple(change_order.rfi_references),
            tuple(change_order.drawing_references),
            tuple(change_order.photo_attachments),
            tuple(change_order.backup_documents),
        ),
    }


def cost_summary_total(document: str) -> float:
    """Return the formal cost-summary total printed on a change order."""
    parts = document.split("## Cost Summary", 1)
    if len(parts) != 2:
        raise AssertionError(f"formal document has no cost summary:\n{document}")
    body = parts[1].split("\n## ", 1)[0]
    amounts = re.findall(r"Total[^$\n]*\$([0-9][0-9,]*\.\d{2})", body, flags=re.I)
    if not amounts:
        raise AssertionError(f"formal document has no cost-summary total:\n{body}")
    return float(amounts[-1].replace(",", ""))


class SequencedStore(dict):
    """Lookup map that can return a different order on a later fetch.

    ``add_line_item`` and ``generate_co_document`` resolve an id through
    ``change_orders``. A check that fetches the id again must not act on a
    different order than the one it checked. The first lookup returns
    ``sequence[0]``; every later lookup of that same id returns the last
    object in ``sequence``.
    """

    def __init__(self, key, sequence):
        if not sequence:
            raise ValueError("sequence must not be empty")
        super().__init__({key: sequence[0]})
        self.key = key
        self.sequence = list(sequence)
        self.lookups = 0

    def __getitem__(self, key):
        if key == self.key:
            index = min(self.lookups, len(self.sequence) - 1)
            self.lookups += 1
            return self.sequence[index]
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key == self.key:
            return self[key]
        return super().get(key, default)


@contextmanager
def empty_workdir():
    previous = os.getcwd()
    with tempfile.TemporaryDirectory() as name:
        os.chdir(name)
        try:
            yield Path(name)
        finally:
            os.chdir(previous)


class ChangeOrderApprovalIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        skill = load_skill()
        cls.ChangeOrderManager = skill["ChangeOrderManager"]
        cls.ChangeOrderStatus = skill["ChangeOrderStatus"]
        cls.ChangeType = skill["ChangeType"]

    def new_manager(self):
        return self.ChangeOrderManager(
            project_id="PRJ-001",
            project_name="Office Tower",
            original_contract=5_000_000.0,
        )

    def draft_with_price(self, manager, title="Additional Structural Steel"):
        change_order = manager.create_change_order(
            title=title,
            description="Add steel reinforcement at Level 5 per RFI-042",
            change_type=self.ChangeType.DESIGN_ERROR,
            created_by="Project Manager",
        )
        manager.add_line_item(
            change_order.id,
            "W12x26 Steel Beam",
            quantity=10,
            unit="LF",
            unit_price=100.0,
            csi_code="05 12 00",
        )
        return change_order

    def move_to(self, manager, change_order, status, approved_amount=AGREED_AMOUNT):
        """Reach ``status`` through the public transitions that exist.

        Under review, pricing, negotiating, and void have no transition
        method. Those cases assign the status field, then the test calls the
        public edit methods so the status check itself still runs in the skill.
        """
        if status == self.ChangeOrderStatus.DRAFT:
            return change_order
        if status == self.ChangeOrderStatus.SUBMITTED:
            return manager.submit_change_order(change_order.id)
        if status == self.ChangeOrderStatus.REJECTED:
            return manager.reject_change_order(change_order.id, "Price not accepted")
        if status == self.ChangeOrderStatus.APPROVED:
            return manager.approve_change_order(
                change_order.id, approved_amount, approved_time=5
            )
        if status == self.ChangeOrderStatus.EXECUTED:
            manager.approve_change_order(
                change_order.id, approved_amount, approved_time=5
            )
            return manager.execute_change_order(change_order.id)
        change_order.status = status
        return change_order

    def assert_rejected_edit(self, manager, change_order, mutate):
        before = snapshot(change_order)
        document_before = manager.generate_co_document(change_order.id)
        keys_before = set(manager.change_orders)
        with empty_workdir() as directory:
            rejected = False
            try:
                mutate()
            except ValueError:
                rejected = True
            written = sorted(path.name for path in directory.iterdir())
        self.assertEqual(written, [], "edit wrote a file before refusing")
        self.assertTrue(rejected, "non-draft cost edit was accepted")
        self.assertEqual(snapshot(change_order), before)
        self.assertEqual(set(manager.change_orders), keys_before)
        self.assertEqual(manager.generate_co_document(change_order.id), document_before)
        self.assertNotIn(MALICIOUS_DESCRIPTION, document_before)
        self.assertNotIn(RELOOKUP_DESCRIPTION, document_before)

    def assert_formal_total_is_approved_amount(self, manager, change_order):
        self.assertIn(
            change_order.status,
            (self.ChangeOrderStatus.APPROVED, self.ChangeOrderStatus.EXECUTED),
        )
        self.assertNotEqual(
            change_order.approved_amount,
            change_order.cost_breakdown.total,
            "fixture must keep the stored approval distinct from the live breakdown",
        )
        document = manager.generate_co_document(change_order.id)
        self.assertEqual(cost_summary_total(document), change_order.approved_amount)
        self.assertNotIn(MALICIOUS_DESCRIPTION, document)

    def non_draft_statuses(self):
        return (
            self.ChangeOrderStatus.SUBMITTED,
            self.ChangeOrderStatus.UNDER_REVIEW,
            self.ChangeOrderStatus.PRICING,
            self.ChangeOrderStatus.NEGOTIATING,
            self.ChangeOrderStatus.APPROVED,
            self.ChangeOrderStatus.REJECTED,
            self.ChangeOrderStatus.EXECUTED,
            self.ChangeOrderStatus.VOID,
        )

    def test_attack_path_approved_line_does_not_change_formal_total(self):
        """Create, approve an agreed amount, add a large line, print the order.

        The extra line must be refused. The status stays approved, the stored
        approved amount stays the agreed amount, and the formal total is that
        amount rather than the rebuilt breakdown.
        """
        manager = self.new_manager()
        approved = self.draft_with_price(manager)
        untouched = self.draft_with_price(manager, title="Unrelated draft")
        manager.approve_change_order(approved.id, AGREED_AMOUNT, approved_time=5)
        untouched_before = snapshot(untouched)

        def add_large_line():
            manager.add_line_item(
                approved.id,
                MALICIOUS_DESCRIPTION,
                quantity=MALICIOUS_QTY,
                unit="EA",
                unit_price=MALICIOUS_UNIT_PRICE,
            )

        self.assert_rejected_edit(manager, approved, add_large_line)
        self.assertEqual(approved.status, self.ChangeOrderStatus.APPROVED)
        self.assertEqual(approved.approved_amount, AGREED_AMOUNT)
        self.assertEqual(snapshot(untouched), untouched_before)
        self.assert_formal_total_is_approved_amount(manager, approved)

    def test_set_cost_breakdown_after_approval_does_not_change_formal_total(self):
        manager = self.new_manager()
        approved = self.draft_with_price(manager)
        manager.approve_change_order(approved.id, AGREED_AMOUNT, approved_time=5)

        def replace_breakdown():
            manager.set_cost_breakdown(
                approved.id,
                labor=50_000,
                materials=50_000,
                equipment=20_000,
                subcontractor=10_000,
            )

        self.assert_rejected_edit(manager, approved, replace_breakdown)
        self.assertEqual(approved.approved_amount, AGREED_AMOUNT)
        self.assert_formal_total_is_approved_amount(manager, approved)

    def test_direct_breakdown_on_approved_order_is_rejected(self):
        """``set_cost_breakdown`` is a second writer of the printed total."""
        manager = self.new_manager()
        change_order = manager.create_change_order(
            title="Paint",
            description="Repaint corridor",
            change_type=self.ChangeType.OWNER_DIRECTED,
            created_by="Estimator",
        )
        manager.set_cost_breakdown(
            change_order.id,
            labor=1_000,
            materials=1_000,
            equipment=0,
            subcontractor=0,
        )
        manager.approve_change_order(change_order.id, AGREED_AMOUNT, approved_time=0)

        def replace_breakdown():
            manager.set_cost_breakdown(
                change_order.id,
                labor=80_000,
                materials=80_000,
                equipment=0,
                subcontractor=0,
            )

        self.assert_rejected_edit(manager, change_order, replace_breakdown)
        self.assert_formal_total_is_approved_amount(manager, change_order)

    def test_line_item_and_breakdown_edits_require_draft(self):
        for status in self.non_draft_statuses():
            with self.subTest(status=status.value, operation="add_line_item"):
                manager = self.new_manager()
                change_order = self.draft_with_price(manager, title=f"Line {status.value}")
                self.move_to(manager, change_order, status)
                self.assertNotEqual(change_order.status, self.ChangeOrderStatus.DRAFT)

                def add_large_line(manager=manager, change_order=change_order):
                    manager.add_line_item(
                        change_order.id,
                        MALICIOUS_DESCRIPTION,
                        quantity=MALICIOUS_QTY,
                        unit="EA",
                        unit_price=MALICIOUS_UNIT_PRICE,
                    )

                self.assert_rejected_edit(manager, change_order, add_large_line)
                if status in (
                    self.ChangeOrderStatus.APPROVED,
                    self.ChangeOrderStatus.EXECUTED,
                ):
                    self.assertEqual(change_order.approved_amount, AGREED_AMOUNT)
                    self.assert_formal_total_is_approved_amount(manager, change_order)

            with self.subTest(status=status.value, operation="set_cost_breakdown"):
                manager = self.new_manager()
                change_order = self.draft_with_price(
                    manager, title=f"Breakdown {status.value}"
                )
                self.move_to(manager, change_order, status)

                def replace_breakdown(manager=manager, change_order=change_order):
                    manager.set_cost_breakdown(
                        change_order.id,
                        labor=40_000,
                        materials=40_000,
                        equipment=5_000,
                        subcontractor=5_000,
                    )

                self.assert_rejected_edit(manager, change_order, replace_breakdown)

    def test_formal_document_prints_approved_amount_when_breakdown_differs(self):
        manager = self.new_manager()
        approved = self.draft_with_price(manager)
        manager.submit_change_order(approved.id)
        manager.approve_change_order(approved.id, AGREED_AMOUNT, approved_time=5)
        self.assertGreater(approved.cost_breakdown.total, approved.approved_amount)
        self.assert_formal_total_is_approved_amount(manager, approved)

        executed_manager = self.new_manager()
        executed = self.draft_with_price(executed_manager, title="Executed steel")
        self.move_to(executed_manager, executed, self.ChangeOrderStatus.EXECUTED)
        self.assert_formal_total_is_approved_amount(executed_manager, executed)

    def test_formal_document_second_lookup_cannot_replace_approved_amount(self):
        """The total must be the approved amount on the order that was resolved.

        A later fetch of the same id returns a different approved order with a
        much larger breakdown and a different approved amount. Printing either
        figure from that later fetch is still the wrong order.
        """
        manager = self.new_manager()
        authorized = self.draft_with_price(manager)
        manager.approve_change_order(authorized.id, AGREED_AMOUNT, approved_time=5)
        # Price the decoy while it is still a draft. Marking it approved is
        # fixture state for the second lookup, not an edit under test.
        decoy = self.draft_with_price(manager, title="Decoy lookup")
        manager.add_line_item(
            decoy.id,
            RELOOKUP_DESCRIPTION,
            quantity=1,
            unit="LS",
            unit_price=1.0,
        )
        decoy.status = self.ChangeOrderStatus.APPROVED
        decoy.approved_amount = DECOY_APPROVED_AMOUNT
        self.assertNotEqual(decoy.cost_breakdown.total, authorized.approved_amount)
        self.assertNotEqual(decoy.approved_amount, authorized.approved_amount)

        manager.change_orders = SequencedStore(authorized.id, [authorized, decoy])
        document = manager.generate_co_document(authorized.id)
        self.assertEqual(cost_summary_total(document), authorized.approved_amount)
        self.assertNotIn(RELOOKUP_DESCRIPTION, document)
        self.assertNotIn(f"{DECOY_APPROVED_AMOUNT:,.2f}", document)

    def test_add_line_item_second_lookup_does_not_edit_approved_order(self):
        """A draft returned by the first fetch must not authorize a later one.

        The first lookup is a draft, so a status check on that object passes.
        The next lookup is an approved order. The approved order, its breakdown,
        and its formal total must stay as they were.
        """
        manager = self.new_manager()
        approved = self.draft_with_price(manager, title="Approved target")
        manager.approve_change_order(approved.id, AGREED_AMOUNT, approved_time=5)
        draft = self.draft_with_price(manager, title="Draft seen first")
        approved_before = snapshot(approved)
        manager.change_orders = SequencedStore(approved.id, [draft, approved])

        try:
            manager.add_line_item(
                approved.id,
                MALICIOUS_DESCRIPTION,
                quantity=MALICIOUS_QTY,
                unit="EA",
                unit_price=MALICIOUS_UNIT_PRICE,
            )
        except ValueError:
            pass

        self.assertEqual(snapshot(approved), approved_before)
        self.assertNotIn(MALICIOUS_DESCRIPTION, [item.description for item in approved.line_items])
        manager.change_orders = {approved.id: approved}
        self.assert_formal_total_is_approved_amount(manager, approved)

    def test_set_cost_breakdown_second_lookup_does_not_edit_approved_order(self):
        manager = self.new_manager()
        approved = self.draft_with_price(manager, title="Approved breakdown target")
        manager.approve_change_order(approved.id, AGREED_AMOUNT, approved_time=5)
        draft = self.draft_with_price(manager, title="Draft seen first")
        approved_before = snapshot(approved)
        manager.change_orders = SequencedStore(approved.id, [draft, approved])

        try:
            manager.set_cost_breakdown(
                approved.id,
                labor=70_000,
                materials=70_000,
                equipment=0,
                subcontractor=0,
            )
        except ValueError:
            pass

        self.assertEqual(snapshot(approved), approved_before)
        manager.change_orders = {approved.id: approved}
        self.assert_formal_total_is_approved_amount(manager, approved)

    def test_draft_line_item_and_breakdown_edits_still_price_the_order(self):
        manager = self.new_manager()
        change_order = self.draft_with_price(manager)
        self.assertEqual(change_order.status, self.ChangeOrderStatus.DRAFT)
        priced_total = change_order.cost_breakdown.total
        self.assertGreater(priced_total, 0)

        added = manager.add_line_item(
            change_order.id,
            "Additional angle",
            quantity=2,
            unit="EA",
            unit_price=50,
        )
        self.assertEqual(added.total_price, 100)
        self.assertEqual(len(change_order.line_items), 2)
        self.assertEqual(change_order.proposed_amount, change_order.cost_breakdown.total)
        self.assertGreater(change_order.proposed_amount, priced_total)
        draft_document = manager.generate_co_document(change_order.id)
        self.assertIn("Additional angle", draft_document)
        self.assertEqual(cost_summary_total(draft_document), change_order.cost_breakdown.total)

        manager.set_cost_breakdown(
            change_order.id,
            labor=200,
            materials=300,
            equipment=0,
            subcontractor=0,
        )
        self.assertEqual(change_order.status, self.ChangeOrderStatus.DRAFT)
        self.assertEqual(change_order.proposed_amount, change_order.cost_breakdown.total)
        self.assertEqual(
            cost_summary_total(manager.generate_co_document(change_order.id)),
            change_order.cost_breakdown.total,
        )

    def test_missing_change_order_is_rejected_without_creating_one(self):
        manager = self.new_manager()
        self.draft_with_price(manager)
        before = {key: snapshot(order) for key, order in manager.change_orders.items()}
        with self.assertRaises(ValueError):
            manager.add_line_item("CO-MISSING", "Ghost", 1, "EA", 1)
        with self.assertRaises(ValueError):
            manager.set_cost_breakdown("CO-MISSING", labor=10)
        self.assertEqual(set(manager.change_orders), set(before))
        for key, order in manager.change_orders.items():
            self.assertEqual(snapshot(order), before[key])


if __name__ == "__main__":
    unittest.main(verbosity=2)
