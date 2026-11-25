"""Approval state for the as-built tracker reference implementation.

The implementation under test is the Python block in
1_DDC_Toolkit/Closeout/as-built-tracker/SKILL.md. These tests fail while a
later submission can become the current revision of a document that closeout
reports still treat as approved.

Operations in that class:
- record_submission is the only method that replaces current_revision.
- import_document_list only inserts new rows, so it is not asserted here.
- review_submission does not replace a revision. The rejection test below
  still runs it, because a rejected document keeps approved_date and the next
  record_submission is what publishes the new revision.
- DocumentStatus.UNDER_REVIEW and DocumentStatus.RESUBMIT are never assigned
  by this skill, so there is no submission path in those states to run.

Left untested, separate skills with a similar gap:
- AsBuiltDocumentManager.upload_as_built in
  1_DDC_Toolkit/Document-Control/as-built-documentation/SKILL.md
- SubmittalTracker.submit and SubmittalTracker.resubmit in
  1_DDC_Toolkit/Document-Control/submittal-tracker/SKILL.md
"""

import builtins
import io
import os
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "1_DDC_Toolkit" / "Closeout" / "as-built-tracker" / "SKILL.md"


def load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"no Python implementation block in {SKILL_PATH}")
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(match.group(1), str(SKILL_PATH), "exec"), namespace)
    return namespace


class SequentialDocuments:
    """Return a different document object on each lookup.

    Membership does not count as a lookup. A fix that checks one object and
    then loads the document again writes to the later object.
    """

    def __init__(self, document_id, first, second):
        self.document_id = document_id
        self._objects = (first, second)
        self._reads = 0

    def __contains__(self, key):
        return key == self.document_id

    def _next(self, key):
        if key != self.document_id:
            raise KeyError(key)
        index = self._reads if self._reads < len(self._objects) else len(self._objects) - 1
        if self._reads < len(self._objects):
            self._reads += 1
        return self._objects[index]

    def __getitem__(self, key):
        return self._next(key)

    def get(self, key, default=None):
        if key != self.document_id:
            return default
        return self._next(key)


def document_view(doc):
    return (
        doc.status,
        doc.current_revision,
        doc.approved_date,
        doc.submitted_date,
        doc.file_path,
        doc.reviewer,
        doc.comments,
    )


class FilesystemSpy:
    """Record create and delete calls for one path while a submission is recorded."""

    def __init__(self, watched: Path):
        self.watched = watched.resolve()
        self.events = []
        self._orig_open = builtins.open
        self._orig_remove = os.remove
        self._orig_unlink = os.unlink

    def __enter__(self):
        builtins.open = self._open
        io.open = self._open
        os.remove = self._remove
        os.unlink = self._unlink
        return self

    def __exit__(self, exc_type, exc, tb):
        builtins.open = self._orig_open
        io.open = self._orig_open
        os.remove = self._orig_remove
        os.unlink = self._orig_unlink
        return False

    def _same(self, file):
        try:
            return Path(file).resolve() == self.watched
        except OSError:
            return False

    def _open(self, file, mode="r", *args, **kwargs):
        mode_text = mode if isinstance(mode, str) else ""
        if self._same(file) and any(flag in mode_text for flag in ("w", "a", "x", "+")):
            self.events.append(("open", mode_text))
        return self._orig_open(file, mode, *args, **kwargs)

    def _remove(self, path, *args, **kwargs):
        if self._same(path):
            self.events.append(("remove", ""))
        return self._orig_remove(path, *args, **kwargs)

    def _unlink(self, path, *args, **kwargs):
        if self._same(path):
            self.events.append(("unlink", ""))
        return self._orig_unlink(path, *args, **kwargs)


class AsBuiltApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = load_skill_namespace()
        cls.ns = ns
        cls.AsBuiltTracker = ns["AsBuiltTracker"]
        cls.DocumentType = ns["DocumentType"]
        cls.DocumentStatus = ns["DocumentStatus"]
        cls.AsBuiltDocument = ns["AsBuiltDocument"]

    def new_tracker(self):
        return self.AsBuiltTracker("Office Building A", handover_date=date(2026, 12, 31))

    def add_drawing(self, tracker, number="A-101"):
        return tracker.add_document(
            document_number=number,
            title="Floor Plan Level 1",
            doc_type=self.DocumentType.ARCHITECTURAL,
            discipline="Architecture",
            contractor="ABC Architects",
        )

    def approve(self, tracker, document_id, revision="A", file_path="as-builts/A-101-A.pdf"):
        recorded = tracker.record_submission(
            document_id,
            revision=revision,
            submitted_by="contractor",
            file_path=file_path,
        )
        self.assertIsNotNone(recorded)
        tracker.review_submission(
            document_id,
            approved=True,
            reviewer="PM",
            comments="Approved for handover",
        )
        doc = tracker.documents[document_id]
        self.assertEqual(doc.status, self.DocumentStatus.APPROVED)
        self.assertEqual(doc.current_revision, revision)
        self.assertEqual(doc.approved_date, date.today())
        return doc

    def submission_tuple(self, submission):
        return (
            submission.submission_id,
            submission.document_id,
            submission.revision,
            submission.file_path,
            submission.status,
            submission.review_comments,
        )

    def assert_clean_refusal(self, tracker, doc, before_doc, before_subs, new_file: Path, spy: FilesystemSpy):
        self.assertEqual(document_view(doc), before_doc)
        self.assertEqual(
            [self.submission_tuple(item) for item in tracker.submissions],
            before_subs,
        )
        self.assertFalse(new_file.exists(), "refusing a submission still created its file")
        self.assertEqual(spy.events, [], "refusing a submission still created or deleted its file")

    def assert_new_revision_is_not_approved(self, tracker, doc, new_revision, new_file: Path, workbook: Path):
        """The new revision and file are current only when every approval signal is gone."""
        transmittal = tracker.generate_transmittal(
            [doc.document_id],
            to="Owner",
            subject="Closeout handover",
        )
        shown_revision = transmittal["documents"][0]["revision"]
        frame = pd.read_excel(workbook, sheet_name="Documents")
        row = frame.loc[frame["ID"] == doc.document_id].iloc[0]
        excel_revision = str(row["Revision"])
        excel_status = str(row["Status"])
        excel_approved = not pd.isna(row["Approved"])
        forecast = tracker.forecast_completion()
        summary = tracker.get_summary()
        contractor = tracker.get_contractor_status(doc.contractor)

        file_recorded = any(
            item.document_id == doc.document_id
            and item.revision == new_revision
            and item.file_path == str(new_file)
            for item in tracker.submissions
        )
        new_revision_current = (
            doc.current_revision == new_revision
            or doc.file_path == str(new_file)
            or shown_revision == new_revision
            or excel_revision == new_revision
            or file_recorded
        )
        approval_attached = (
            doc.status == self.DocumentStatus.APPROVED
            or doc.approved_date is not None
            or excel_approved
            or excel_status == self.DocumentStatus.APPROVED.value
            or summary["approved"] > 0
            or summary["by_status"][self.DocumentStatus.APPROVED.value] > 0
            or contractor["approved"] > 0
        )
        # forecast_completion counts approved_date and ignores current status.
        if doc.approved_date is not None and doc.approved_date >= date.today() - timedelta(days=14):
            self.assertGreater(forecast["approval_rate_per_week"], 0)
            approval_attached = True

        self.assertFalse(
            new_revision_current and approval_attached,
            "unreviewed revision {rev} file {path} is current while an approval is still "
            "attached (status={status}, approved_date={approved}, excel_status={excel_status}, "
            "excel_approved={excel_approved}, transmittal={shown})".format(
                rev=new_revision,
                path=new_file,
                status=doc.status,
                approved=doc.approved_date,
                excel_status=excel_status,
                excel_approved=excel_approved,
                shown=shown_revision,
            ),
        )
        if new_revision_current:
            self.assertIsNone(doc.approved_date)
            self.assertNotEqual(doc.status, self.DocumentStatus.APPROVED)
            self.assertFalse(excel_approved)
            self.assertNotEqual(excel_status, self.DocumentStatus.APPROVED.value)
            self.assertEqual(summary["approved"], 0)
            self.assertEqual(contractor["approved"], 0)
            if doc.file_path == str(new_file):
                self.assertIsNone(doc.approved_date)

    def record(self, tracker, document_id, revision, file_path: Path):
        spy = FilesystemSpy(file_path)
        with spy:
            try:
                result = tracker.record_submission(
                    document_id,
                    revision=revision,
                    submitted_by="contractor",
                    file_path=str(file_path),
                )
            except Exception as exc:  # refusal may be a return or an exception
                return exc, spy
        return result, spy

    def test_initial_review_marks_the_submitted_revision_approved(self):
        tracker = self.new_tracker()
        doc = self.add_drawing(tracker)
        reviewed = "A"
        reviewed_file = "as-builts/A-101-A.pdf"
        self.approve(tracker, doc.document_id, reviewed, reviewed_file)
        doc = tracker.documents[doc.document_id]
        self.assertEqual(doc.current_revision, reviewed)
        self.assertEqual(doc.status, self.DocumentStatus.APPROVED)
        self.assertEqual(doc.approved_date, date.today())
        self.assertEqual(tracker.submissions[-1].revision, reviewed)
        self.assertEqual(tracker.submissions[-1].file_path, reviewed_file)
        self.assertEqual(tracker.submissions[-1].status, self.DocumentStatus.APPROVED)
        summary = tracker.get_summary()
        self.assertEqual(summary["approved"], 1)

    def test_submission_after_approval_cannot_replace_the_approved_revision(self):
        tracker = self.new_tracker()
        doc = self.add_drawing(tracker)
        self.approve(tracker, doc.document_id)
        doc = tracker.documents[doc.document_id]
        before_doc = document_view(doc)
        before_subs = [self.submission_tuple(item) for item in tracker.submissions]
        new_revision = "B"
        with tempfile.TemporaryDirectory() as tmp:
            new_file = Path(tmp) / "unreviewed-A-101-B.pdf"
            workbook = Path(tmp) / "closeout.xlsx"
            self.assertFalse(new_file.exists())
            result, spy = self.record(tracker, doc.document_id, new_revision, new_file)
            tracker.export_to_excel(str(workbook))
            self.assert_new_revision_is_not_approved(
                tracker, doc, new_revision, new_file, workbook
            )
            replaced = (
                doc.current_revision == new_revision
                or any(
                    item.revision == new_revision and item.file_path == str(new_file)
                    for item in tracker.submissions
                )
            )
            if not replaced:
                self.assert_clean_refusal(tracker, doc, before_doc, before_subs, new_file, spy)
                self.assertTrue(result is None or isinstance(result, Exception))

    def test_resubmission_after_rejection_does_not_keep_the_old_approval(self):
        tracker = self.new_tracker()
        doc = self.add_drawing(tracker)
        self.approve(tracker, doc.document_id, revision="A", file_path="as-builts/A-101-A.pdf")
        tracker.review_submission(
            doc.document_id,
            approved=False,
            reviewer="PM",
            comments="Dimensions do not match the field",
        )
        doc = tracker.documents[doc.document_id]
        self.assertEqual(doc.status, self.DocumentStatus.REJECTED)
        before_doc = document_view(doc)
        before_subs = [self.submission_tuple(item) for item in tracker.submissions]
        new_revision = "B"
        with tempfile.TemporaryDirectory() as tmp:
            new_file = Path(tmp) / "unreviewed-A-101-B.pdf"
            workbook = Path(tmp) / "closeout.xlsx"
            result, spy = self.record(tracker, doc.document_id, new_revision, new_file)
            tracker.export_to_excel(str(workbook))
            self.assert_new_revision_is_not_approved(
                tracker, doc, new_revision, new_file, workbook
            )
            replaced = doc.current_revision == new_revision or any(
                item.revision == new_revision and item.file_path == str(new_file)
                for item in tracker.submissions
            )
            if not replaced:
                self.assert_clean_refusal(tracker, doc, before_doc, before_subs, new_file, spy)
                self.assertTrue(result is None or isinstance(result, Exception))

    def test_check_and_update_apply_to_the_same_document_object(self):
        """A second lookup must not receive the write the first object was checked for."""
        tracker = self.new_tracker()
        reviewed = self.AsBuiltDocument(
            document_id="DOC-0001",
            document_number="A-101",
            title="Floor Plan Level 1",
            doc_type=self.DocumentType.ARCHITECTURAL,
            discipline="Architecture",
            contractor="ABC Architects",
            status=self.DocumentStatus.REJECTED,
            current_revision="A",
            required_date=date(2026, 12, 17),
            submitted_date=date.today(),
            approved_date=None,
            reviewer="PM",
            comments="Rejected",
            file_path="as-builts/A-101-A.pdf",
        )
        approved = self.AsBuiltDocument(
            document_id="DOC-0001",
            document_number="A-101",
            title="Floor Plan Level 1",
            doc_type=self.DocumentType.ARCHITECTURAL,
            discipline="Architecture",
            contractor="ABC Architects",
            status=self.DocumentStatus.APPROVED,
            current_revision="A",
            required_date=date(2026, 12, 17),
            submitted_date=date.today(),
            approved_date=date.today(),
            reviewer="PM",
            comments="Approved for handover",
            file_path="as-builts/A-101-A.pdf",
        )
        tracker.documents = SequentialDocuments(reviewed.document_id, reviewed, approved)
        before_approved = document_view(approved)
        new_revision = "B"
        with tempfile.TemporaryDirectory() as tmp:
            new_file = Path(tmp) / "unreviewed-A-101-B.pdf"
            self.record(tracker, reviewed.document_id, new_revision, new_file)
        self.assertEqual(
            document_view(approved),
            before_approved,
            "submission updated a different document object than the one it loaded first",
        )
        self.assertNotEqual(approved.current_revision, new_revision)
        self.assertNotEqual(approved.file_path, str(new_file))
        if reviewed.current_revision == new_revision or reviewed.file_path == str(new_file):
            self.assertIsNone(reviewed.approved_date)
            self.assertNotEqual(reviewed.status, self.DocumentStatus.APPROVED)


if __name__ == "__main__":
    unittest.main()
