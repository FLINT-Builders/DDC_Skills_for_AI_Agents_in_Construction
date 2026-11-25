"""Approval-gate tests for the submittal tracker in SKILL.md.

These tests load and execute the Python implementation embedded in
``SKILL.md``. They fail while ``submit`` / ``resubmit`` will replace files
on a record that is not eligible for that action.

``resubmit`` must refuse unless status is ``revise_resubmit`` or ``rejected``.
``submit`` must refuse unless status is ``draft``. An allowed ``resubmit``
clears ``approved_date`` and leaves the record ``submitted``, so a new
``review`` is required before it can be approved again. A refusal must not
change the record, must not apply the caller's file list to a different
object fetched by a later lookup, and must not create the caller's file path.

Not covered here:
- ``SubmittalTracker.review`` has no caller or transition restrictions.
- ``AsBuiltTracker.record_submission`` in
  ``1_DDC_Toolkit/Closeout/as-built-tracker/SKILL.md``.
- ``upload_as_built`` in
  ``1_DDC_Toolkit/Document-Control/as-built-documentation/SKILL.md``.

Run from the repository root:

    python3 -m unittest discover -s 1_DDC_Toolkit/Document-Control/submittal-tracker -p 'test_resubmit_approval_gate.py' -t 1_DDC_Toolkit/Document-Control/submittal-tracker
"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl


SKILL_PATH = Path(__file__).resolve().parent / "SKILL.md"
MODULE = {}


def load_skill(path: Path) -> dict:
    """Execute the implementation block in SKILL.md with matching line numbers."""
    text = path.read_text(encoding="utf-8")
    marker = "```python\n"
    blocks = []
    cursor = 0
    while True:
        start = text.find(marker, cursor)
        if start == -1:
            break
        code_start = start + len(marker)
        end = text.find("```", code_start)
        if end == -1:
            break
        first_code_line = text[:code_start].count("\n") + 1
        code = text[code_start:end]
        if "class SubmittalTracker" in code:
            blocks.append((first_code_line, code))
        cursor = end + 3
    if len(blocks) != 1:
        raise AssertionError(f"expected one SubmittalTracker block in {path}")
    first_code_line, code = blocks[0]
    padded = ("\n" * (first_code_line - 1)) + code
    namespace = {"__file__": str(path)}
    exec(compile(padded, str(path), "exec"), namespace)
    return namespace


def setUpModule():
    MODULE.update(load_skill(SKILL_PATH))


def invoke(fn):
    try:
        fn()
    except Exception as exc:  # refusal may raise; mutation still fails the test
        return exc
    return None


def snapshot(sub):
    return {
        "status": sub.status,
        "revision": sub.revision,
        "files": sub.files,
        "file_names": list(sub.files),
        "submitted_date": sub.submitted_date,
        "approved_date": sub.approved_date,
        "comments": sub.comments,
        "comments_copy": list(sub.comments),
        "required_date": sub.required_date,
        "title": sub.title,
        "contractor": sub.contractor,
        "submittal_type": sub.submittal_type,
    }


def assert_unchanged(test, sub, before):
    test.assertEqual(sub.status, before["status"])
    test.assertEqual(sub.revision, before["revision"])
    test.assertIs(sub.files, before["files"])
    test.assertEqual(list(sub.files), before["file_names"])
    test.assertEqual(sub.submitted_date, before["submitted_date"])
    test.assertEqual(sub.approved_date, before["approved_date"])
    test.assertIs(sub.comments, before["comments"])
    test.assertEqual(list(sub.comments), before["comments_copy"])
    test.assertEqual(sub.required_date, before["required_date"])
    test.assertEqual(sub.title, before["title"])
    test.assertEqual(sub.contractor, before["contractor"])
    test.assertEqual(sub.submittal_type, before["submittal_type"])


def as_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return value


def read_register(path):
    workbook = openpyxl.load_workbook(path, data_only=True)
    try:
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()
    header = [cell for cell in rows[0]]
    return [dict(zip(header, row)) for row in rows[1:]]


class SwitchingSubmittals(dict):
    """First lookup returns ``first``; every later lookup returns ``second``.

    ``dict.get`` on a subclass does not call ``__getitem__``, so both paths
    share one counter. The stored item remains ``first`` for ``in`` and
    ``values()``.
    """

    def __init__(self, first, second):
        super().__init__({first.submittal_id: first})
        self.first = first
        self.second = second
        self.lookups = 0

    def _next(self, key):
        if key != self.first.submittal_id:
            raise KeyError(key)
        self.lookups += 1
        if self.lookups == 1:
            return self.first
        return self.second

    def __getitem__(self, key):
        return self._next(key)

    def get(self, key, default=None):
        if key != self.first.submittal_id:
            return default
        return self._next(key)


class SubmittalApprovalGateTests(unittest.TestCase):
    def tracker(self):
        return MODULE["SubmittalTracker"]("Office Tower")

    def status(self, name):
        return MODULE["SubmittalStatus"][name]

    def submittal_type(self, name):
        return MODULE["SubmittalType"][name]

    def new_package(self, spec, title, type_name, contractor, required_date):
        return self.tracker().create_submittal(
            spec,
            title,
            self.submittal_type(type_name),
            contractor,
            required_date,
        )

    def test_loaded_methods_are_the_skill_implementation(self):
        for method_name in ("submit", "resubmit", "review", "export_log"):
            code = getattr(MODULE["SubmittalTracker"], method_name).__code__
            self.assertTrue(
                code.co_filename.endswith(
                    "1_DDC_Toolkit/Document-Control/submittal-tracker/SKILL.md"
                ),
                msg=f"{method_name} was not loaded from SKILL.md",
            )
        skill_lines = SKILL_PATH.read_text(encoding="utf-8").splitlines()
        resubmit_line = MODULE["SubmittalTracker"].resubmit.__code__.co_firstlineno
        self.assertIn("def resubmit", skill_lines[resubmit_line - 1])

    def test_resubmit_after_approval_does_not_swap_shop_drawing(self):
        tracker = self.tracker()
        required = date.today() - timedelta(days=3)
        sub = tracker.create_submittal(
            "05 12 00",
            "Structural Steel Shop Drawing",
            self.submittal_type("SHOP_DRAWING"),
            "SteelCo",
            required,
        )
        sibling = tracker.create_submittal(
            "09 29 00",
            "Gypsum Board",
            self.submittal_type("PRODUCT_DATA"),
            "ABC Drywall",
            date.today() + timedelta(days=14),
        )
        approved_files = ["shop-drawing-rev0.pdf"]
        tracker.submit(sub.submittal_id, approved_files)
        tracker.review(
            sub.submittal_id,
            self.status("APPROVED"),
            "Architect",
            "Approved for fabrication",
        )
        tracker.submit(sibling.submittal_id, ["gypsum-data.pdf"])
        tracker.review(sibling.submittal_id, self.status("APPROVED"), "Architect")
        before = snapshot(sub)
        sibling_before = snapshot(sibling)
        swapped = str(Path(tempfile.gettempdir()) / "unreviewed-shop-drawing-swap.pdf")
        self.assertFalse(Path(swapped).exists())

        invoke(lambda: tracker.resubmit(sub.submittal_id, [swapped]))

        self.assertIs(tracker.submittals[sub.submittal_id], sub)
        assert_unchanged(self, sub, before)
        assert_unchanged(self, sibling, sibling_before)
        self.assertEqual(sub.comments[0].comment, "Approved for fabrication")
        self.assertFalse(Path(swapped).exists())
        self.assertFalse(any(item.submittal_id == sub.submittal_id for item in tracker.get_pending()))
        self.assertFalse(any(item.submittal_id == sub.submittal_id for item in tracker.get_overdue()))

        with tempfile.TemporaryDirectory() as directory:
            register = str(Path(directory) / "submittal-log.xlsx")
            tracker.export_log(register)
            rows = {row["ID"]: row for row in read_register(register)}
        self.assertEqual(rows[sub.submittal_id]["Status"], "approved")
        self.assertEqual(rows[sub.submittal_id]["Rev"], before["revision"])
        self.assertEqual(rows[sub.submittal_id]["Type"], "shop_drawing")
        self.assertEqual(as_date(rows[sub.submittal_id]["Approved"]), before["approved_date"])
        self.assertEqual(as_date(rows[sub.submittal_id]["Submitted"]), before["submitted_date"])

    def test_resubmit_after_approved_as_noted_does_not_swap_product_data(self):
        tracker = self.tracker()
        sub = tracker.create_submittal(
            "09 29 00",
            "Gypsum Board Product Data",
            self.submittal_type("PRODUCT_DATA"),
            "ABC Drywall",
            date.today() + timedelta(days=14),
        )
        approved_files = ["gypsum-product-data.pdf"]
        tracker.submit(sub.submittal_id, approved_files)
        tracker.review(
            sub.submittal_id,
            self.status("APPROVED_AS_NOTED"),
            "Architect",
            "Approved as noted",
        )
        before = snapshot(sub)
        swapped = ["gypsum-product-data-unreviewed.pdf"]

        invoke(lambda: tracker.resubmit(sub.submittal_id, swapped))

        assert_unchanged(self, sub, before)
        self.assertFalse(any(name.endswith("unreviewed.pdf") for name in sub.files))

    def test_resubmit_refused_unless_revise_or_rejected(self):
        cases = ("DRAFT", "SUBMITTED", "UNDER_REVIEW", "APPROVED", "APPROVED_AS_NOTED")
        for status_name in cases:
            with self.subTest(status=status_name):
                tracker = self.tracker()
                sub = tracker.create_submittal(
                    "01 33 00",
                    f"Package {status_name}",
                    self.submittal_type("CERTIFICATE"),
                    "Contractor",
                    date.today() + timedelta(days=7),
                )
                if status_name != "DRAFT":
                    tracker.submit(sub.submittal_id, [f"{status_name.lower()}-original.pdf"])
                    if status_name != "SUBMITTED":
                        tracker.review(sub.submittal_id, self.status(status_name), "Reviewer")
                before = snapshot(sub)
                invoke(lambda: tracker.resubmit(sub.submittal_id, ["replacement.pdf"]))
                assert_unchanged(self, sub, before)

    def test_allowed_resubmit_clears_approval_and_needs_a_new_review(self):
        for status_name in ("REVISE_RESUBMIT", "REJECTED"):
            with self.subTest(status=status_name):
                tracker = self.tracker()
                sub = tracker.create_submittal(
                    "05 12 00",
                    "Steel Shop Drawing",
                    self.submittal_type("SHOP_DRAWING"),
                    "SteelCo",
                    date.today() + timedelta(days=21),
                )
                kept = tracker.create_submittal(
                    "09 29 00",
                    "Unrelated Approved Package",
                    self.submittal_type("PRODUCT_DATA"),
                    "ABC Drywall",
                    date.today() + timedelta(days=21),
                )
                tracker.submit(sub.submittal_id, ["cycle-0.pdf"])
                tracker.submit(kept.submittal_id, ["kept.pdf"])
                tracker.review(sub.submittal_id, self.status("APPROVED"), "Architect", "First approval")
                tracker.review(kept.submittal_id, self.status("APPROVED"), "Architect")
                prior_approval = sub.approved_date
                self.assertIsNotNone(prior_approval)
                tracker.review(sub.submittal_id, self.status(status_name), "Architect", "Revise the gauge")
                self.assertEqual(sub.approved_date, prior_approval)
                revision_before = sub.revision
                caller_files = ["cycle-1-unreviewed.pdf", "calc-sheet.pdf"]

                invoke(lambda: tracker.resubmit(sub.submittal_id, caller_files))

                self.assertIs(tracker.submittals[sub.submittal_id], sub)
                self.assertEqual(sub.revision, revision_before + 1)
                self.assertEqual(sub.status, self.status("SUBMITTED"))
                self.assertEqual(list(sub.files), caller_files)
                self.assertIsNone(sub.approved_date)
                self.assertEqual(sub.submitted_date, date.today())
                self.assertEqual(len(sub.comments), 2)
                self.assertTrue(any(item.submittal_id == sub.submittal_id for item in tracker.get_pending()))
                self.assertEqual(kept.status, self.status("APPROVED"))
                self.assertEqual(list(kept.files), ["kept.pdf"])
                self.assertEqual(kept.approved_date, prior_approval)

                with tempfile.TemporaryDirectory() as directory:
                    register = str(Path(directory) / "submittal-log.xlsx")
                    tracker.export_log(register)
                    rows = {row["ID"]: row for row in read_register(register)}
                self.assertEqual(rows[sub.submittal_id]["Status"], "submitted")
                self.assertEqual(rows[sub.submittal_id]["Rev"], revision_before + 1)
                self.assertIsNone(as_date(rows[sub.submittal_id]["Approved"]))
                self.assertEqual(rows[kept.submittal_id]["Status"], "approved")
                self.assertEqual(as_date(rows[kept.submittal_id]["Approved"]), prior_approval)

                tracker.review(sub.submittal_id, self.status("APPROVED"), "Architect", "Second approval")
                self.assertEqual(sub.status, self.status("APPROVED"))
                self.assertEqual(sub.approved_date, date.today())
                self.assertEqual(list(sub.files), caller_files)
                blocked = snapshot(sub)
                invoke(lambda: tracker.resubmit(sub.submittal_id, ["cycle-2-blocked.pdf"]))
                assert_unchanged(self, sub, blocked)

    def test_resubmit_from_revise_without_a_prior_approval_date(self):
        tracker = self.tracker()
        sub = tracker.create_submittal(
            "05 12 00",
            "Steel Shop Drawing",
            self.submittal_type("SHOP_DRAWING"),
            "SteelCo",
            date.today() + timedelta(days=10),
        )
        tracker.submit(sub.submittal_id, ["first.pdf"])
        tracker.review(sub.submittal_id, self.status("REVISE_RESUBMIT"), "Architect", "Show connections")
        self.assertIsNone(sub.approved_date)
        caller_files = ["first-revised.pdf"]

        invoke(lambda: tracker.resubmit(sub.submittal_id, caller_files))

        self.assertEqual(sub.status, self.status("SUBMITTED"))
        self.assertEqual(sub.revision, 1)
        self.assertEqual(list(sub.files), caller_files)
        self.assertIsNone(sub.approved_date)
        self.assertEqual(sub.submitted_date, date.today())

    def test_submit_from_draft_records_files_without_an_approval(self):
        tracker = self.tracker()
        sub = tracker.create_submittal(
            "09 29 00",
            "Gypsum Board",
            self.submittal_type("PRODUCT_DATA"),
            "ABC Drywall",
            date.today() + timedelta(days=14),
        )
        caller_files = ["spec_sheet.pdf"]

        invoke(lambda: tracker.submit(sub.submittal_id, caller_files))

        self.assertEqual(sub.status, self.status("SUBMITTED"))
        self.assertEqual(sub.revision, 0)
        self.assertEqual(list(sub.files), caller_files)
        self.assertEqual(sub.submitted_date, date.today())
        self.assertIsNone(sub.approved_date)
        self.assertTrue(any(item.submittal_id == sub.submittal_id for item in tracker.get_pending()))

    def test_submit_after_approval_does_not_swap_files(self):
        for status_name, type_name, filename in (
            ("APPROVED", "SHOP_DRAWING", "approved-shop.pdf"),
            ("APPROVED_AS_NOTED", "PRODUCT_DATA", "approved-product.pdf"),
        ):
            with self.subTest(status=status_name):
                tracker = self.tracker()
                sub = tracker.create_submittal(
                    "05 50 00",
                    "Metal Fabrications",
                    self.submittal_type(type_name),
                    "FabCo",
                    date.today() - timedelta(days=1),
                )
                tracker.submit(sub.submittal_id, [filename])
                tracker.review(sub.submittal_id, self.status(status_name), "Architect", "Released")
                before = snapshot(sub)
                swapped = [f"swapped-{filename}"]
                self.assertFalse(Path(swapped[0]).exists())

                invoke(lambda: tracker.submit(sub.submittal_id, swapped))

                assert_unchanged(self, sub, before)
                self.assertFalse(Path(swapped[0]).exists())
                self.assertFalse(any(item.submittal_id == sub.submittal_id for item in tracker.get_pending()))
                self.assertFalse(any(item.submittal_id == sub.submittal_id for item in tracker.get_overdue()))

    def test_missing_id_does_not_create_a_record_or_a_file(self):
        tracker = self.tracker()
        sub = tracker.create_submittal(
            "09 29 00",
            "Gypsum Board",
            self.submittal_type("PRODUCT_DATA"),
            "ABC Drywall",
            date.today() + timedelta(days=5),
        )
        before = snapshot(sub)
        phantom = str(Path(tempfile.gettempdir()) / "phantom-submittal.pdf")
        self.assertFalse(Path(phantom).exists())

        invoke(lambda: tracker.resubmit("SUB-MISSING", [phantom]))
        invoke(lambda: tracker.submit("SUB-MISSING", [phantom]))

        self.assertNotIn("SUB-MISSING", tracker.submittals)
        self.assertEqual(set(tracker.submittals), {sub.submittal_id})
        assert_unchanged(self, sub, before)
        self.assertFalse(Path(phantom).exists())

    def test_resubmit_does_not_follow_a_second_lookup_after_an_approved_record(self):
        status = MODULE["SubmittalStatus"]
        submittal_type = MODULE["SubmittalType"]
        approved = MODULE["Submittal"](
            submittal_id="SUB-0001",
            spec_section="05 12 00",
            title="Approved shop drawing",
            submittal_type=submittal_type.SHOP_DRAWING,
            status=status.APPROVED,
            contractor="SteelCo",
            revision=3,
            submitted_date=date(2026, 1, 10),
            required_date=date(2026, 2, 1),
            approved_date=date(2026, 1, 12),
            comments=[],
            files=["sealed-shop.pdf"],
        )
        later = MODULE["Submittal"](
            submittal_id="SUB-0001",
            spec_section="05 12 00",
            title="Later revise record",
            submittal_type=submittal_type.SHOP_DRAWING,
            status=status.REVISE_RESUBMIT,
            contractor="SteelCo",
            revision=4,
            submitted_date=date(2026, 1, 20),
            required_date=date(2026, 2, 1),
            approved_date=date(2026, 1, 12),
            comments=[],
            files=["revise-me.pdf"],
        )
        tracker = self.tracker()
        tracker.submittals = SwitchingSubmittals(approved, later)
        approved_before = snapshot(approved)
        later_before = snapshot(later)

        invoke(lambda: tracker.resubmit("SUB-0001", ["attacker-swap.pdf"]))

        assert_unchanged(self, approved, approved_before)
        assert_unchanged(self, later, later_before)

    def test_resubmit_mutates_only_the_record_whose_status_was_checked(self):
        status = MODULE["SubmittalStatus"]
        submittal_type = MODULE["SubmittalType"]
        eligible = MODULE["Submittal"](
            submittal_id="SUB-0007",
            spec_section="05 12 00",
            title="Revise and resubmit",
            submittal_type=submittal_type.SHOP_DRAWING,
            status=status.REVISE_RESUBMIT,
            contractor="SteelCo",
            revision=4,
            submitted_date=date(2026, 1, 20),
            required_date=date(2026, 3, 1),
            approved_date=date(2026, 1, 12),
            comments=[],
            files=["reviewed-cycle.pdf"],
        )
        other = MODULE["Submittal"](
            submittal_id="SUB-0007",
            spec_section="05 12 00",
            title="Different approved object",
            submittal_type=submittal_type.PRODUCT_DATA,
            status=status.APPROVED,
            contractor="OtherCo",
            revision=2,
            submitted_date=date(2026, 1, 5),
            required_date=date(2026, 3, 1),
            approved_date=date(2026, 1, 8),
            comments=[],
            files=["do-not-touch.pdf"],
        )
        tracker = self.tracker()
        tracker.submittals = SwitchingSubmittals(eligible, other)
        other_before = snapshot(other)
        caller_files = ["new-cycle.pdf"]

        invoke(lambda: tracker.resubmit("SUB-0007", caller_files))

        self.assertEqual(eligible.status, status.SUBMITTED)
        self.assertEqual(eligible.revision, 5)
        self.assertEqual(list(eligible.files), caller_files)
        self.assertIsNone(eligible.approved_date)
        self.assertEqual(eligible.submitted_date, date.today())
        self.assertEqual(eligible.title, "Revise and resubmit")
        assert_unchanged(self, other, other_before)
        self.assertLessEqual(tracker.submittals.lookups, 1)

    def test_submit_mutates_only_the_draft_record_whose_status_was_checked(self):
        status = MODULE["SubmittalStatus"]
        submittal_type = MODULE["SubmittalType"]
        draft = MODULE["Submittal"](
            submittal_id="SUB-0003",
            spec_section="09 29 00",
            title="Draft product data",
            submittal_type=submittal_type.PRODUCT_DATA,
            status=status.DRAFT,
            contractor="ABC Drywall",
            revision=0,
            submitted_date=None,
            required_date=date(2026, 4, 1),
            approved_date=None,
            comments=[],
            files=[],
        )
        approved = MODULE["Submittal"](
            submittal_id="SUB-0003",
            spec_section="09 29 00",
            title="Approved product data",
            submittal_type=submittal_type.PRODUCT_DATA,
            status=status.APPROVED,
            contractor="ABC Drywall",
            revision=1,
            submitted_date=date(2026, 1, 2),
            required_date=date(2026, 4, 1),
            approved_date=date(2026, 1, 9),
            comments=[],
            files=["approved-product.pdf"],
        )
        tracker = self.tracker()
        tracker.submittals = SwitchingSubmittals(draft, approved)
        approved_before = snapshot(approved)
        caller_files = ["initial-submittal.pdf"]

        invoke(lambda: tracker.submit("SUB-0003", caller_files))

        self.assertEqual(draft.status, status.SUBMITTED)
        self.assertEqual(draft.revision, 0)
        self.assertEqual(list(draft.files), caller_files)
        self.assertEqual(draft.submitted_date, date.today())
        self.assertIsNone(draft.approved_date)
        self.assertEqual(draft.title, "Draft product data")
        assert_unchanged(self, approved, approved_before)
        self.assertLessEqual(tracker.submittals.lookups, 1)

    def test_submit_does_not_follow_a_second_lookup_after_an_approved_record(self):
        status = MODULE["SubmittalStatus"]
        submittal_type = MODULE["SubmittalType"]
        approved = MODULE["Submittal"](
            submittal_id="SUB-0004",
            spec_section="05 12 00",
            title="Approved",
            submittal_type=submittal_type.SHOP_DRAWING,
            status=status.APPROVED,
            contractor="SteelCo",
            revision=2,
            submitted_date=date(2026, 1, 2),
            required_date=date(2026, 4, 1),
            approved_date=date(2026, 1, 9),
            comments=[],
            files=["sealed.pdf"],
        )
        draft = MODULE["Submittal"](
            submittal_id="SUB-0004",
            spec_section="05 12 00",
            title="Draft looked up later",
            submittal_type=submittal_type.SHOP_DRAWING,
            status=status.DRAFT,
            contractor="SteelCo",
            revision=0,
            submitted_date=None,
            required_date=date(2026, 4, 1),
            approved_date=None,
            comments=[],
            files=[],
        )
        tracker = self.tracker()
        tracker.submittals = SwitchingSubmittals(approved, draft)
        approved_before = snapshot(approved)
        draft_before = snapshot(draft)

        invoke(lambda: tracker.submit("SUB-0004", ["should-not-land.pdf"]))

        assert_unchanged(self, approved, approved_before)
        assert_unchanged(self, draft, draft_before)


if __name__ == "__main__":
    unittest.main()
