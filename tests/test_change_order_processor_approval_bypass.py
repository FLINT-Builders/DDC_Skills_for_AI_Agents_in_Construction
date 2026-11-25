"""Approval integrity for the change-order processor skill.

The implementation under test is the ChangeOrderProcessor block in
1_DDC_Toolkit/Cost-Management/change-order-processor/SKILL.md. These tests
load that block and run it. They fail while an approved change order can
still gain cost or schedule time that get_summary and export_log then treat
as approved.

Run:
    python3 -m pip install pandas openpyxl
    python3 -m unittest discover -s tests -p 'test_change_order_processor_approval_bypass.py' -v
"""

import os
import re
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "1_DDC_Toolkit"
    / "Cost-Management"
    / "change-order-processor"
    / "SKILL.md"
)

ATTACK_ITEM = "Unapproved lump sum"
ATTACK_QUANTITY = 1
ATTACK_UNIT_COST = 100_000.0
ATTACK_DAYS = 40
MODEST_DAYS = 3


def load_skill_namespace():
    """Execute the skill's processor implementation and return its namespace."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    implementation = next(
        block for block in blocks if "class ChangeOrderProcessor" in block
    )
    namespace = {"__name__": "change_order_processor_skill"}
    exec(compile(implementation, str(SKILL_PATH), "exec"), namespace)
    namespace["_implementation_source"] = implementation
    return namespace


class SecondLookupMap(dict):
    """Return one order on the first lookup and a different order after that.

    A fix that checks the first resolved order and then edits that same
    object leaves `later` alone. A fix that resolves the id again and edits
    the second result mutates `later`.
    """

    def __init__(self, key, first, later):
        super().__init__({key: first})
        self.key = key
        self.first = first
        self.later = later
        self.getitem_count = 0

    def __getitem__(self, key):
        if key != self.key:
            return super().__getitem__(key)
        self.getitem_count += 1
        if self.getitem_count == 1:
            return self.first
        return self.later

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class ChangeOrderApprovalBypassTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_skill_namespace()
        cls.Processor = cls.ns["ChangeOrderProcessor"]
        cls.ChangeType = cls.ns["ChangeType"]
        cls.Status = cls.ns["ChangeOrderStatus"]
        source = cls.ns["_implementation_source"]
        for method_name in (
            "def add_cost_item",
            "def set_schedule_impact",
            "def approve_change_order",
            "def get_summary",
            "def export_log",
        ):
            if method_name not in source:
                raise AssertionError(f"{SKILL_PATH} is missing {method_name}")

    def setUp(self):
        self.processor = self.Processor(
            project_name="Office Tower",
            original_contract=1_000_000,
        )

    def _snapshot(self, change_order):
        return {
            "status": change_order.status,
            "descriptions": [item.description for item in change_order.cost_items],
            "item_count": len(change_order.cost_items),
            "direct_cost": change_order.direct_cost,
            "overhead_cost": change_order.overhead_cost,
            "profit_cost": change_order.profit_cost,
            "total_cost": change_order.total_cost,
            "schedule_impact_days": change_order.schedule_impact_days,
            "affected_activities": list(change_order.affected_activities),
            "approved_by": change_order.approved_by,
            "approved_date": change_order.approved_date,
            "approval_count": len(change_order.approvals),
        }

    def _approve_modest(self, title="Additional electrical outlets"):
        change_order = self.processor.create_change_order(
            title=title,
            description="Add outlets per owner request",
            change_type=self.ChangeType.OWNER_REQUESTED,
            created_by="Contractor PM",
        )
        self.processor.add_cost_item(
            change_order.co_number,
            "Electrical outlets",
            2,
            "EA",
            100,
            "material",
        )
        self.processor.set_schedule_impact(
            change_order.co_number,
            MODEST_DAYS,
            ["A100"],
        )
        self.processor.submit_for_review(change_order.co_number)
        self.processor.submit_for_approval(
            change_order.co_number,
            "Reviewer",
            "Cost verified",
        )
        self.processor.approve_change_order(
            change_order.co_number,
            "Owner Rep",
            "Owner",
            "Approved per request",
        )
        self.assertEqual(change_order.status, self.Status.APPROVED)
        self.assertGreater(change_order.total_cost, 0)
        self.assertEqual(change_order.schedule_impact_days, MODEST_DAYS)
        return change_order

    def _attempt(self, operation):
        try:
            operation()
        except Exception:
            return

    def _assert_approved_figures_not_increased(self, before_summary):
        summary = self.processor.get_summary()
        self.assertLessEqual(
            summary["approved_changes"],
            before_summary["approved_changes"],
        )
        self.assertLessEqual(
            summary["current_contract"],
            before_summary["current_contract"],
        )
        self.assertLessEqual(
            summary["total_schedule_impact_days"],
            before_summary["total_schedule_impact_days"],
        )
        self.assertEqual(summary["original_contract"], self.processor.original_contract)
        return summary

    def _assert_still_approved_matches_snapshot(self, change_order, before, attack_description):
        if change_order.status != self.Status.APPROVED:
            self.assertNotEqual(change_order.status, self.Status.APPROVED)
            self.assertIsNone(change_order.approved_date)
            self.assertEqual(change_order.approved_by, "")
            return
        self.assertEqual(change_order.approved_by, before["approved_by"])
        self.assertEqual(change_order.approved_date, before["approved_date"])
        self.assertEqual(len(change_order.approvals), before["approval_count"])
        self.assertAlmostEqual(change_order.direct_cost, before["direct_cost"])
        self.assertAlmostEqual(change_order.overhead_cost, before["overhead_cost"])
        self.assertAlmostEqual(change_order.profit_cost, before["profit_cost"])
        self.assertAlmostEqual(change_order.total_cost, before["total_cost"])
        self.assertEqual(change_order.schedule_impact_days, before["schedule_impact_days"])
        self.assertEqual(change_order.affected_activities, before["affected_activities"])
        self.assertEqual(
            [item.description for item in change_order.cost_items],
            before["descriptions"],
        )
        self.assertNotIn(attack_description, before["descriptions"])
        self.assertNotIn(
            attack_description,
            [item.description for item in change_order.cost_items],
        )

    def _add_unapproved_lump_sum(self, co_number):
        extended = ATTACK_QUANTITY * ATTACK_UNIT_COST
        self.assertGreater(extended, 0)
        self.processor.add_cost_item(
            co_number,
            ATTACK_ITEM,
            ATTACK_QUANTITY,
            "LS",
            ATTACK_UNIT_COST,
            "subcontractor",
            0.0,
        )

    def _export_summary_metrics(self, output_path):
        pandas = self.ns["pd"]
        frame = pandas.read_excel(output_path, sheet_name="Summary")
        return dict(zip(frame["Metric"], frame["Value"]))

    def _assert_export_matches_certified_figures(self, change_order, before, summary):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "change_order_log.xlsx"
            self.processor.export_log(str(output_path))
            self.assertTrue(output_path.is_file())
            metrics = self._export_summary_metrics(output_path)
            self.assertAlmostEqual(float(metrics["Approved Changes"]), summary["approved_changes"])
            self.assertAlmostEqual(float(metrics["Current Contract"]), summary["current_contract"])
            self.assertAlmostEqual(
                float(metrics["Schedule Impact (days)"]),
                summary["total_schedule_impact_days"],
            )
            self.assertAlmostEqual(
                float(metrics["Original Contract"]),
                self.processor.original_contract,
            )
            pandas = self.ns["pd"]
            orders = pandas.read_excel(output_path, sheet_name="Change Orders")
            matched = orders.loc[orders["co_number"] == change_order.co_number]
            self.assertEqual(len(matched), 1)
            row = matched.iloc[0]
            if row["status"] == self.Status.APPROVED.value:
                self.assertAlmostEqual(float(row["total_cost"]), before["total_cost"])
                self.assertEqual(int(row["schedule_impact"]), before["schedule_impact_days"])
                details = pandas.read_excel(output_path, sheet_name="Cost Details")
                self.assertNotIn(ATTACK_ITEM, set(details["Description"].astype(str)))

    def test_draft_change_order_still_accepts_cost_and_schedule_edits(self):
        change_order = self.processor.create_change_order(
            title="Draft piping change",
            description="Not yet submitted",
            change_type=self.ChangeType.FIELD_CONDITION,
            created_by="Contractor PM",
        )
        self.processor.add_cost_item(
            change_order.co_number,
            "Pipe",
            10,
            "LF",
            20,
            "material",
        )
        self.processor.set_schedule_impact(change_order.co_number, 4, ["P1"])
        self.assertEqual(change_order.status, self.Status.DRAFT)
        self.assertEqual(len(change_order.cost_items), 1)
        self.assertGreater(change_order.total_cost, 0)
        self.assertEqual(change_order.schedule_impact_days, 4)
        self.assertEqual(change_order.affected_activities, ["P1"])
        summary = self.processor.get_summary()
        self.assertEqual(summary["approved_changes"], 0)
        self.assertEqual(summary["current_contract"], self.processor.original_contract)
        self.assertEqual(summary["total_schedule_impact_days"], 0)

    def test_add_cost_item_after_approval_is_not_certified(self):
        change_order = self._approve_modest()
        before = self._snapshot(change_order)
        before_summary = self.processor.get_summary()
        self.assertGreater(ATTACK_UNIT_COST, before["total_cost"])
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                self._attempt(lambda: self._add_unapproved_lump_sum(change_order.co_number))
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                os.chdir(previous)
        self._assert_still_approved_matches_snapshot(change_order, before, ATTACK_ITEM)
        summary = self._assert_approved_figures_not_increased(before_summary)
        self._assert_export_matches_certified_figures(change_order, before, summary)

    def test_set_schedule_impact_after_approval_is_not_certified(self):
        change_order = self._approve_modest()
        before = self._snapshot(change_order)
        before_summary = self.processor.get_summary()
        self.assertNotEqual(ATTACK_DAYS, before["schedule_impact_days"])
        self.assertGreater(ATTACK_DAYS, before["schedule_impact_days"])
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                self._attempt(
                    lambda: self.processor.set_schedule_impact(
                        change_order.co_number,
                        ATTACK_DAYS,
                        ["A100", "A200"],
                    )
                )
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                os.chdir(previous)
        self._assert_still_approved_matches_snapshot(change_order, before, ATTACK_ITEM)
        summary = self._assert_approved_figures_not_increased(before_summary)
        self._assert_export_matches_certified_figures(change_order, before, summary)

    def test_attack_path_export_does_not_inflate_approved_contract(self):
        """The sequence from a modest approved order through a later cost and time edit."""
        change_order = self._approve_modest()
        before = self._snapshot(change_order)
        before_summary = self.processor.get_summary()
        self._attempt(lambda: self._add_unapproved_lump_sum(change_order.co_number))
        self._attempt(
            lambda: self.processor.set_schedule_impact(
                change_order.co_number,
                ATTACK_DAYS,
                ["A200"],
            )
        )
        self._assert_still_approved_matches_snapshot(change_order, before, ATTACK_ITEM)
        summary = self._assert_approved_figures_not_increased(before_summary)
        self._assert_export_matches_certified_figures(change_order, before, summary)
        if change_order.status == self.Status.APPROVED:
            self.assertAlmostEqual(summary["approved_changes"], before["total_cost"])
            self.assertEqual(summary["total_schedule_impact_days"], before["schedule_impact_days"])
            self.assertAlmostEqual(
                summary["current_contract"],
                self.processor.original_contract + before["total_cost"],
            )

    def test_edits_apply_only_to_the_named_change_order(self):
        approved = self._approve_modest("Approved outlets")
        draft = self.processor.create_change_order(
            title="Separate draft",
            description="Still pricing",
            change_type=self.ChangeType.DESIGN_CHANGE,
            created_by="Contractor PM",
        )
        approved_before = self._snapshot(approved)
        before_summary = self.processor.get_summary()

        self.processor.add_cost_item(draft.co_number, "Draft only", 4, "EA", 25, "labor")
        self.processor.set_schedule_impact(draft.co_number, 2, ["D1"])
        self.assertEqual(self._snapshot(approved), approved_before)
        self.assertEqual(
            [item.description for item in draft.cost_items],
            ["Draft only"],
        )
        self.assertEqual(draft.schedule_impact_days, 2)
        self.assertEqual(draft.affected_activities, ["D1"])
        self._assert_approved_figures_not_increased(before_summary)

        self._attempt(lambda: self._add_unapproved_lump_sum(approved.co_number))
        self._attempt(
            lambda: self.processor.set_schedule_impact(
                approved.co_number,
                ATTACK_DAYS,
                ["Z9"],
            )
        )
        self.assertEqual(
            [item.description for item in draft.cost_items],
            ["Draft only"],
        )
        self.assertEqual(draft.schedule_impact_days, 2)
        self.assertEqual(draft.affected_activities, ["D1"])
        self.assertEqual(draft.status, self.Status.DRAFT)
        self._assert_still_approved_matches_snapshot(approved, approved_before, ATTACK_ITEM)
        summary = self._assert_approved_figures_not_increased(before_summary)
        self._assert_export_matches_certified_figures(approved, approved_before, summary)

    def test_second_lookup_cannot_mutate_a_different_approved_order(self):
        """Resolving the id again must not edit an order other than the one checked."""
        approved = self._approve_modest("Locked order")
        decoy = self.processor.create_change_order(
            title="Decoy draft",
            description="First lookup",
            change_type=self.ChangeType.FIELD_CONDITION,
            created_by="Contractor PM",
        )
        decoy.co_number = approved.co_number
        approved_before = self._snapshot(approved)
        key = approved.co_number

        cost_map = SecondLookupMap(key, decoy, approved)
        self.processor.change_orders = cost_map
        self._attempt(lambda: self._add_unapproved_lump_sum(key))
        self.assertGreaterEqual(cost_map.getitem_count, 1)
        self._assert_still_approved_matches_snapshot(approved, approved_before, ATTACK_ITEM)

        fresh_decoy = self.Processor(
            project_name="unused",
            original_contract=1,
        ).create_change_order(
            title="Schedule decoy",
            description="First lookup",
            change_type=self.ChangeType.FIELD_CONDITION,
            created_by="Contractor PM",
        )
        fresh_decoy.co_number = key
        schedule_before = self._snapshot(approved)
        schedule_map = SecondLookupMap(key, fresh_decoy, approved)
        self.processor.change_orders = schedule_map
        self._attempt(
            lambda: self.processor.set_schedule_impact(key, ATTACK_DAYS, ["S1"])
        )
        self.assertGreaterEqual(schedule_map.getitem_count, 1)
        self._assert_still_approved_matches_snapshot(approved, schedule_before, ATTACK_ITEM)


if __name__ == "__main__":
    unittest.main()
