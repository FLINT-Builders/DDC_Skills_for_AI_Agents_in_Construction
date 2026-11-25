"""Capacity checks must sum every overlapping allocation.

The resource-pool optimizer in SKILL.md decides free capacity from the
single largest overlapping booking. These tests load that implementation
and run the allocation paths a scheduler actually calls.

They fail while that largest-only check is still in place, and they pass
once every overlapping quantity is summed and a request that would exceed
capacity is rejected without being stored.

Not covered here, because they do not commit capacity with that check:
detect_conflicts, calculate_utilization, suggest_optimization, generate_report.
"""

import unittest
from datetime import datetime
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
WINDOW_START = datetime(2026, 3, 1)
WINDOW_END = datetime(2026, 3, 31)


def load_skill_namespace():
    """Execute the implementation block from SKILL.md."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.find(marker)
    if start < 0:
        raise AssertionError(f"no python block in {SKILL_PATH}")
    start += len(marker)
    end = text.find("\n```", start)
    implementation = text[start:end]
    if "class ResourcePoolOptimizer" not in implementation:
        raise AssertionError("implementation block is not the optimizer")
    namespace = {}
    exec(compile(implementation, str(SKILL_PATH), "exec"), namespace)
    return namespace


def peak_committed(pool, resource_id):
    """Peak quantity committed on one resource.

    An allocation covers time t when start <= t < end. That is the same
    overlap rule as ResourcePoolOptimizer._get_availability:
    existing.start < request.end and existing.end > request.start.
    """
    allocs = [a for a in pool.allocations.values() if a.resource_id == resource_id]
    peak = 0.0
    for instant in {a.start_date for a in allocs}:
        total = sum(
            a.quantity
            for a in allocs
            if a.start_date <= instant < a.end_date
        )
        if total > peak:
            peak = total
    return peak


class OverlappingCapacityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = load_skill_namespace()
        cls.ResourceType = ns["ResourceType"]
        cls.Optimizer = ns["ResourcePoolOptimizer"]

    def setUp(self):
        self.pool = self.Optimizer("Shared equipment")
        self.pool.add_resource(
            "CR-1", "Crew", self.ResourceType.EQUIPMENT, capacity=10
        )

    def _request(self, project, quantity, start=WINDOW_START, end=WINDOW_END):
        return self.pool.add_request(
            project,
            project,
            self.ResourceType.EQUIPMENT,
            start,
            end,
            quantity=quantity,
        )

    def test_third_overlapping_allocation_of_four_is_rejected(self):
        """Attack path: capacity 10, then 4 + 4 + 4 on one window."""
        first = self._request("P1", 4)
        second = self._request("P2", 4)
        third = self._request("P3", 4)

        self.pool.allocate(first.id, "CR-1", 4)
        self.pool.allocate(second.id, "CR-1", 4)
        stored = set(self.pool.allocations)

        with self.assertRaises(ValueError):
            self.pool.allocate(third.id, "CR-1", 4)

        self.assertEqual(stored, set(self.pool.allocations))
        self.assertNotIn(third.id, {a.request_id for a in self.pool.allocations.values()})
        self.assertLessEqual(peak_committed(self.pool, "CR-1"), 10)
        self.assertEqual(8, peak_committed(self.pool, "CR-1"))

    def test_default_quantity_uses_the_same_sum(self):
        """allocate() without an explicit quantity still refuses the third 4."""
        first = self._request("P1", 4)
        second = self._request("P2", 4)
        third = self._request("P3", 4)
        self.pool.allocate(first.id, "CR-1")
        self.pool.allocate(second.id, "CR-1")
        stored = set(self.pool.allocations)

        with self.assertRaises(ValueError):
            self.pool.allocate(third.id, "CR-1")

        self.assertEqual(stored, set(self.pool.allocations))
        self.assertLessEqual(peak_committed(self.pool, "CR-1"), 10)

    def test_one_past_the_true_remainder_is_rejected_and_not_stored(self):
        """4 + 4 leaves 2. The next 2 fits; one more unit must not be stored."""
        first = self._request("P1", 4)
        second = self._request("P2", 4)
        remainder = self._request("P3", 2)
        extra = self._request("P4", 1)

        self.pool.allocate(first.id, "CR-1", 4)
        self.pool.allocate(second.id, "CR-1", 4)
        self.pool.allocate(remainder.id, "CR-1", 2)
        self.assertEqual(10, peak_committed(self.pool, "CR-1"))
        stored = set(self.pool.allocations)

        with self.assertRaises(ValueError):
            self.pool.allocate(extra.id, "CR-1", 1)

        self.assertEqual(stored, set(self.pool.allocations))
        self.assertEqual(10, peak_committed(self.pool, "CR-1"))

    def test_partial_overlap_sums_every_booking_in_the_window(self):
        """A request that overlaps two smaller bookings is still over capacity."""
        early = self._request("P1", 4, datetime(2026, 3, 1), datetime(2026, 3, 20))
        late = self._request("P2", 4, datetime(2026, 3, 10), datetime(2026, 3, 30))
        middle = self._request("P3", 4, datetime(2026, 3, 15), datetime(2026, 3, 18))
        self.pool.allocate(early.id, "CR-1", 4)
        self.pool.allocate(late.id, "CR-1", 4)
        stored = set(self.pool.allocations)

        with self.assertRaises(ValueError):
            self.pool.allocate(middle.id, "CR-1", 4)

        self.assertEqual(stored, set(self.pool.allocations))
        self.assertLessEqual(peak_committed(self.pool, "CR-1"), 10)

    def test_allocation_outside_the_window_does_not_mask_the_overlap_sum(self):
        """A large booking in January must not change the February sum."""
        january = self._request(
            "P0", 9, datetime(2026, 1, 1), datetime(2026, 1, 31)
        )
        first = self._request(
            "P1", 4, datetime(2026, 2, 1), datetime(2026, 2, 28)
        )
        second = self._request(
            "P2", 4, datetime(2026, 2, 1), datetime(2026, 2, 28)
        )
        third = self._request(
            "P3", 4, datetime(2026, 2, 1), datetime(2026, 2, 28)
        )
        self.pool.allocate(january.id, "CR-1", 9)
        self.pool.allocate(first.id, "CR-1", 4)
        self.pool.allocate(second.id, "CR-1", 4)
        stored = set(self.pool.allocations)

        with self.assertRaises(ValueError):
            self.pool.allocate(third.id, "CR-1", 4)

        self.assertEqual(stored, set(self.pool.allocations))
        february = [
            a
            for a in self.pool.allocations.values()
            if a.start_date == datetime(2026, 2, 1)
        ]
        self.assertEqual(8, sum(a.quantity for a in february))
        self.assertLessEqual(peak_committed(self.pool, "CR-1"), 10)

    def test_auto_allocate_does_not_commit_more_than_capacity(self):
        """auto_allocate shares the check via find_available_resources and allocate."""
        self._request("P1", 4)
        self._request("P2", 4)
        self._request("P3", 4)

        self.pool.auto_allocate()

        self.assertLessEqual(peak_committed(self.pool, "CR-1"), 10)
        committed = sum(
            a.quantity
            for a in self.pool.allocations.values()
            if a.resource_id == "CR-1"
            and a.start_date < WINDOW_END
            and a.end_date > WINDOW_START
        )
        self.assertLessEqual(committed, 10)
        for alloc in self.pool.allocations.values():
            others = sum(
                other.quantity
                for other in self.pool.allocations.values()
                if other.id != alloc.id
                and other.resource_id == alloc.resource_id
                and other.start_date < alloc.end_date
                and other.end_date > alloc.start_date
            )
            self.assertLessEqual(others + alloc.quantity, 10)

    def test_find_available_resources_reports_the_summed_remainder(self):
        """After 4 + 4, free capacity on the shared window is 2, not 6."""
        first = self._request("P1", 4)
        second = self._request("P2", 4)
        probe = self._request("P3", 4)
        self.pool.allocate(first.id, "CR-1", 4)
        self.pool.allocate(second.id, "CR-1", 4)

        found = self.pool.find_available_resources(probe)

        self.assertEqual([("CR-1", 2.0)], [(item[0].id, item[1]) for item in found])

    def test_second_booking_that_exceeds_capacity_is_rejected(self):
        """A single existing booking is already the whole overlap sum."""
        first = self._request("P1", 6)
        second = self._request("P2", 6)
        self.pool.allocate(first.id, "CR-1", 6)
        stored = set(self.pool.allocations)

        with self.assertRaises(ValueError):
            self.pool.allocate(second.id, "CR-1", 6)

        self.assertEqual(stored, set(self.pool.allocations))
        self.assertEqual(6, peak_committed(self.pool, "CR-1"))

    def test_back_to_back_windows_do_not_consume_each_other(self):
        """end == next start is not an overlap under the optimizer's rule."""
        first = self._request("P1", 10, datetime(2026, 4, 1), datetime(2026, 4, 10))
        second = self._request("P2", 10, datetime(2026, 4, 10), datetime(2026, 4, 20))

        self.pool.allocate(first.id, "CR-1", 10)
        self.pool.allocate(second.id, "CR-1", 10)

        self.assertEqual(10, peak_committed(self.pool, "CR-1"))

    def test_another_resource_keeps_its_own_capacity(self):
        self.pool.add_resource(
            "CR-2", "Other crew", self.ResourceType.EQUIPMENT, capacity=10
        )
        first = self._request("P1", 4)
        second = self._request("P2", 4)
        third = self._request("P3", 4)
        self.pool.allocate(first.id, "CR-1", 4)
        self.pool.allocate(second.id, "CR-1", 4)

        created = self.pool.allocate(third.id, "CR-2", 4)

        self.assertEqual("CR-2", created.resource_id)
        self.assertEqual(8, peak_committed(self.pool, "CR-1"))
        self.assertEqual(4, peak_committed(self.pool, "CR-2"))


if __name__ == "__main__":
    unittest.main()
