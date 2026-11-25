"""Spec tests for NCR response and remediation verification.

These tests load the quality-control workflow implementation from
``3_DDC_Insights/Safety-Quality/quality-control-workflow/SKILL.md`` and
exercise ``QualityControlManager.record_ncr_response`` and
``QualityControlManager.verify_remediation``.

They describe the required behavior:

- Recording a contractor response stores the response and must not mark the
  NCR accepted, in remediation, verified, or closed.
- Verification is allowed only when that same NCR is already in remediation.
- The verifier must be a non-blank identity distinct from the NCR contractor
  (trim and case-insensitive). The refusal must happen before any status or
  defect write.
- A later lookup of the same id must not apply the write to a different NCR
  than the one whose status and contractor were checked.
- Quality metrics and the QC report keep those defects and NCRs open when
  verification is refused.

``issue_ncr`` also assigns a status without reading the previous one. It does
not accept an NCR or close defects, so it is not part of this spec.

The tests fail while response recording forces ``accepted`` and verification
closes linked defects from any status for any name.
"""

from __future__ import annotations

import re
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "3_DDC_Insights"
    / "Safety-Quality"
    / "quality-control-workflow"
    / "SKILL.md"
)

# Status values that mean the response was approved or the defect is done.
APPROVAL_VALUES = frozenset(
    {"accepted", "in_remediation", "verified", "closed"}
)


def _load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    implementation = None
    for block in blocks:
        if "class QualityControlManager" in block:
            implementation = block
            break
    if implementation is None:
        raise RuntimeError(f"No QualityControlManager implementation in {SKILL_PATH}")

    module = types.ModuleType("quality_control_skill_under_test")
    sys.modules[module.__name__] = module
    namespace = module.__dict__
    code = compile(implementation, str(SKILL_PATH), "exec")
    exec(code, namespace)

    class _DatetimeProxy:
        """Unique second-resolution timestamps for id fields in the skill."""

        def __init__(self, real_datetime):
            self._real = real_datetime
            self._calls = 0

        def now(self, tz=None):
            self._calls += 1
            return self._real(2026, 1, 1) + timedelta(seconds=self._calls)

    namespace["datetime"] = _DatetimeProxy(namespace["datetime"])
    return namespace


SKILL = _load_skill_namespace()


class SecondLookup(dict):
    """Dictionary whose second read of ``target_id`` returns ``decoy``.

    The first ``__getitem__`` returns the stored NCR. A second read of that
    same id returns ``decoy`` so a check against one object cannot be reused
    to mutate another.
    """

    def __init__(self, source, target_id, decoy):
        super().__init__(source)
        self.target_id = target_id
        self.decoy = decoy
        self.reads = []

    def __getitem__(self, key):
        self.reads.append(key)
        if key == self.target_id and self.reads.count(key) >= 2:
            return self.decoy
        return super().__getitem__(key)


def _status_value(status):
    return status.value if hasattr(status, "value") else str(status)


def _is_approval(status):
    return _status_value(status) in APPROVAL_VALUES


def _identity(value):
    return str(value).strip().casefold()


class NCRApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.NCRStatus = SKILL["NCRStatus"]
        cls.QCStatus = SKILL["QCStatus"]
        cls.DefectSeverity = SKILL["DefectSeverity"]
        cls.NonConformanceReport = SKILL["NonConformanceReport"]
        cls.Manager = SKILL["QualityControlManager"]

    def setUp(self):
        self.contractor = "ABC Concrete"
        self.inspector = "QC Engineer"

    def _build(self, contractor=None, severity=None, title="Rebar Spacing Non-Conformance"):
        contractor = self.contractor if contractor is None else contractor
        severity = self.DefectSeverity.CRITICAL if severity is None else severity
        qc = self.Manager("PRJ-001", "Office Tower")
        qc.create_itp(
            "CONC",
            [
                {
                    "name": "Rebar placement",
                    "spec_ref": "03200",
                    "criteria": "Per drawings",
                    "hold_point": True,
                }
            ],
        )
        inspection = qc.schedule_inspection(
            "CONC-001",
            "Level 3, Grid A-C",
            datetime(2026, 1, 2),
            self.inspector,
        )
        qc.conduct_inspection(
            inspection.id, result="fail", notes="Rebar spacing incorrect"
        )
        defect = qc.record_defect(
            inspection.id,
            description="Rebar spacing 8 inches instead of 6 inches per spec",
            location="Level 3, Grid B-2",
            severity=severity,
            spec_reference="03200-3.2",
            assigned_to=contractor,
        )
        ncr = qc.create_ncr([defect.id], title, contractor)
        qc.issue_ncr(ncr.id)
        return qc, inspection, defect, ncr

    def _add_defect(self, qc, inspection, contractor, description):
        return qc.record_defect(
            inspection.id,
            description=description,
            location="Level 3, Grid C-4",
            severity=self.DefectSeverity.MAJOR,
            spec_reference="03200-3.2",
            assigned_to=contractor,
        )

    def _stored_ncr(self, qc, ncr_id):
        # Bypass SecondLookup so a snapshot is not itself a lookup under test.
        return dict.__getitem__(qc.ncrs, ncr_id)

    def _snapshot(self, qc):
        ncrs = []
        for ncr_id in sorted(qc.ncrs):
            ncr = self._stored_ncr(qc, ncr_id)
            ncrs.append(
                (
                    ncr_id,
                    ncr.status,
                    ncr.contractor,
                    ncr.contractor_response,
                    ncr.remediation_plan,
                    ncr.cost_impact,
                    ncr.schedule_impact_days,
                    tuple(ncr.defect_ids),
                )
            )
        defects = []
        for defect_id in sorted(qc.defects):
            defect = qc.defects[defect_id]
            defects.append(
                (
                    defect_id,
                    defect.status,
                    defect.verified_by,
                    defect.closed_date,
                )
            )
        return (tuple(ncrs), tuple(defects))

    def _assert_rejected(self, qc, ncr_id, verified_by, decoys=()):
        before = self._snapshot(qc)
        decoy_before = [
            (d.status, d.contractor_response, d.remediation_plan, tuple(d.defect_ids))
            for d in decoys
        ]
        with self.assertRaises((ValueError, PermissionError)):
            qc.verify_remediation(ncr_id, verified_by)
        self.assertEqual(self._snapshot(qc), before)
        decoy_after = [
            (d.status, d.contractor_response, d.remediation_plan, tuple(d.defect_ids))
            for d in decoys
        ]
        self.assertEqual(decoy_after, decoy_before)
        for decoy in decoys:
            for defect_id in decoy.defect_ids:
                if defect_id in qc.defects:
                    self.assertEqual(qc.defects[defect_id].status, "open")
                    self.assertEqual(qc.defects[defect_id].verified_by, "")
                    self.assertIsNone(qc.defects[defect_id].closed_date)

    def _record(self, qc, ncr):
        return qc.record_ncr_response(
            ncr.id,
            "Rebar was respaced after the pour was stopped.",
            "Remove and replace the affected mat.",
            12500.0,
            4,
        )

    def test_contractor_response_does_not_accept_issued_ncr(self):
        qc, _inspection, defect, ncr = self._build()
        issued = ncr.issued_date
        returned = self._record(qc, ncr)

        self.assertIs(returned, ncr)
        self.assertEqual(
            ncr.contractor_response,
            "Rebar was respaced after the pour was stopped.",
        )
        self.assertEqual(ncr.remediation_plan, "Remove and replace the affected mat.")
        self.assertEqual(ncr.cost_impact, 12500.0)
        self.assertEqual(ncr.schedule_impact_days, 4)
        self.assertEqual(ncr.contractor, self.contractor)
        self.assertEqual(ncr.issued_date, issued)
        self.assertFalse(
            _is_approval(ncr.status),
            f"recording a response set approval status {_status_value(ncr.status)}",
        )
        self.assertEqual(defect.status, "open")
        self.assertEqual(defect.verified_by, "")
        self.assertIsNone(defect.closed_date)

    def test_contractor_response_does_not_accept_from_other_open_states(self):
        for status_name in ("DRAFT", "DISPUTED"):
            with self.subTest(status=status_name):
                qc, _inspection, defect, ncr = self._build()
                ncr.status = getattr(self.NCRStatus, status_name)
                self._record(qc, ncr)
                self.assertFalse(_is_approval(ncr.status))
                self.assertEqual(defect.status, "open")
                self.assertEqual(defect.verified_by, "")
                self.assertIsNone(defect.closed_date)

    def test_contractor_response_does_not_rewrite_terminal_status_to_accepted(self):
        for status_name in ("IN_REMEDIATION", "VERIFIED", "CLOSED"):
            with self.subTest(status=status_name):
                qc, _inspection, defect, ncr = self._build()
                start = getattr(self.NCRStatus, status_name)
                ncr.status = start
                self._record(qc, ncr)
                self.assertNotEqual(_status_value(ncr.status), "accepted")
                if status_name in ("VERIFIED", "CLOSED"):
                    self.assertEqual(ncr.status, start)
                self.assertEqual(defect.status, "open")

    def test_verify_rejects_every_status_except_in_remediation_without_side_effects(self):
        for status in self.NCRStatus:
            if _status_value(status) == "in_remediation":
                continue
            with self.subTest(status=_status_value(status)):
                qc, _inspection, defect, ncr = self._build()
                second = self._add_defect(qc, qc.inspections[defect.inspection_id], self.contractor, "Uncapped dowel")
                ncr.defect_ids.append(second.id)
                unrelated = self._add_defect(
                    qc,
                    qc.inspections[defect.inspection_id],
                    "Other Co",
                    "Unrelated honeycomb",
                )
                ncr.status = status
                self._assert_rejected(qc, ncr.id, self.inspector)
                self.assertEqual(unrelated.status, "open")
                self.assertEqual(defect.status, "open")
                self.assertEqual(second.status, "open")

    def test_verify_rejects_contractor_identity_without_side_effects(self):
        aliases = [
            self.contractor,
            self.contractor.swapcase(),
            self.contractor.upper(),
            f"  {self.contractor}  ",
            "abc concrete",
        ]
        for alias in aliases:
            with self.subTest(verified_by=alias):
                qc, _inspection, defect, ncr = self._build()
                second = self._add_defect(
                    qc,
                    qc.inspections[defect.inspection_id],
                    self.contractor,
                    "Missing cover",
                )
                ncr.defect_ids.append(second.id)
                ncr.status = self.NCRStatus.IN_REMEDIATION
                self.assertEqual(_identity(alias), _identity(self.contractor))
                self._assert_rejected(qc, ncr.id, alias)

    def test_verify_rejects_blank_inspector_identity(self):
        for blank in ("", "   ", "\t", "\n"):
            with self.subTest(verified_by=repr(blank)):
                qc, _inspection, _defect, ncr = self._build()
                ncr.status = self.NCRStatus.IN_REMEDIATION
                self._assert_rejected(qc, ncr.id, blank)

    def test_verify_rejects_contractor_with_surrounding_space_on_the_ncr(self):
        qc, _inspection, defect, ncr = self._build(contractor="  ABC Concrete  ")
        ncr.status = self.NCRStatus.IN_REMEDIATION
        self._assert_rejected(qc, ncr.id, "abc concrete")
        self.assertEqual(defect.status, "open")

    def test_attack_path_contractor_cannot_close_defects_or_hide_them_in_metrics(self):
        qc, inspection, defect, ncr = self._build()
        second = self._add_defect(
            qc,
            inspection,
            self.contractor,
            "Tie wire left in the cover zone",
        )
        ncr.defect_ids.append(second.id)

        self._record(qc, ncr)
        for caller_name in (self.contractor, "Any Name", self.inspector):
            try:
                qc.verify_remediation(ncr.id, caller_name)
            except (ValueError, PermissionError):
                pass

        self.assertEqual(inspection.status, self.QCStatus.FAILED)
        self.assertFalse(_is_approval(ncr.status))
        self.assertNotEqual(_status_value(ncr.status), "verified")
        self.assertNotEqual(_status_value(ncr.status), "closed")
        for item in (defect, second):
            self.assertEqual(item.status, "open")
            self.assertEqual(item.verified_by, "")
            self.assertIsNone(item.closed_date)

        metrics = qc.get_quality_metrics()
        self.assertGreaterEqual(metrics["open_defects"], 2)
        self.assertGreaterEqual(metrics["open_ncrs"], 1)
        self.assertEqual(metrics["failed_inspections"], 1)

        report = qc.generate_qc_report()
        self.assertIn(defect.id, report)
        self.assertNotIn("| Open Defects | 0 |", report)
        self.assertNotIn("| Open NCRs | 0 |", report)

    def test_distinct_inspector_can_verify_in_remediation_and_close_only_linked_defects(self):
        qc, inspection, defect, ncr = self._build()
        linked = self._add_defect(qc, inspection, self.contractor, "Missing chair")
        ncr.defect_ids.append(linked.id)
        other_defect = self._add_defect(qc, inspection, "Other Co", "Separate honeycombing")
        other_ncr = qc.create_ncr([other_defect.id], "Separate NCR", "Other Co")
        qc.issue_ncr(other_ncr.id)
        other_ncr.status = self.NCRStatus.IN_REMEDIATION

        ncr.status = self.NCRStatus.IN_REMEDIATION
        returned = qc.verify_remediation(ncr.id, self.inspector)

        self.assertIs(returned, ncr)
        self.assertEqual(_status_value(ncr.status), "verified")
        for item in (defect, linked):
            self.assertEqual(item.status, "closed")
            self.assertEqual(item.verified_by, self.inspector)
            self.assertIsNotNone(item.closed_date)
        self.assertEqual(other_defect.status, "open")
        self.assertEqual(other_defect.verified_by, "")
        self.assertIsNone(other_defect.closed_date)
        self.assertEqual(_status_value(other_ncr.status), "in_remediation")

        metrics = qc.get_quality_metrics()
        self.assertEqual(metrics["open_defects"], 1)
        self.assertEqual(metrics["open_ncrs"], 1)
        report = qc.generate_qc_report()
        self.assertNotIn(defect.id, report)

    def test_response_updates_the_checked_ncr_not_a_later_lookup(self):
        qc, _inspection, defect, ncr = self._build()
        decoy_defect = self._add_defect(qc, qc.inspections[defect.inspection_id], "Other Co", "Decoy defect")
        decoy = self.NonConformanceReport(
            id="NCR-DECOY-RESPONSE",
            defect_ids=[decoy_defect.id],
            title="Decoy",
            description="Substituted after the status check",
            contractor="Other Co",
            spec_sections=["09900"],
            status=self.NCRStatus.CLOSED,
        )
        qc.ncrs = SecondLookup(qc.ncrs, ncr.id, decoy)

        returned = self._record(qc, ncr)

        self.assertIs(returned, ncr)
        self.assertEqual(
            ncr.contractor_response,
            "Rebar was respaced after the pour was stopped.",
        )
        self.assertEqual(ncr.remediation_plan, "Remove and replace the affected mat.")
        self.assertEqual(ncr.cost_impact, 12500.0)
        self.assertEqual(ncr.schedule_impact_days, 4)
        self.assertFalse(_is_approval(ncr.status))
        self.assertEqual(defect.status, "open")
        self.assertEqual(decoy.contractor_response, "")
        self.assertEqual(decoy.remediation_plan, "")
        self.assertEqual(decoy.cost_impact, 0.0)
        self.assertEqual(decoy.schedule_impact_days, 0)
        self.assertEqual(decoy.status, self.NCRStatus.CLOSED)
        self.assertEqual(decoy_defect.status, "open")

    def test_verify_rejects_contractor_even_if_a_later_lookup_returns_another_ncr(self):
        qc, inspection, defect, ncr = self._build()
        ncr.status = self.NCRStatus.IN_REMEDIATION
        decoy_defect = self._add_defect(qc, inspection, "Other Co", "Decoy defect")
        decoy = self.NonConformanceReport(
            id="NCR-DECOY-VERIFY",
            defect_ids=[decoy_defect.id],
            title="Decoy",
            description="Different contractor, so a second lookup would look eligible",
            contractor="Other Co",
            spec_sections=["03300"],
            status=self.NCRStatus.IN_REMEDIATION,
        )
        qc.ncrs = SecondLookup(qc.ncrs, ncr.id, decoy)

        self._assert_rejected(qc, ncr.id, self.contractor, decoys=(decoy,))
        self.assertEqual(ncr.status, self.NCRStatus.IN_REMEDIATION)
        self.assertEqual(defect.status, "open")
        self.assertEqual(decoy.status, self.NCRStatus.IN_REMEDIATION)
        self.assertEqual(decoy_defect.status, "open")

    def test_verify_closes_the_checked_ncr_not_a_later_lookup(self):
        qc, inspection, defect, ncr = self._build()
        ncr.status = self.NCRStatus.IN_REMEDIATION
        decoy_defect = self._add_defect(qc, inspection, "Other Co", "Decoy defect")
        decoy = self.NonConformanceReport(
            id="NCR-DECOY-CLOSE",
            defect_ids=[decoy_defect.id],
            title="Decoy",
            description="Issued report substituted between check and close",
            contractor="Other Co",
            spec_sections=["03300"],
            status=self.NCRStatus.ISSUED,
        )
        store = SecondLookup(qc.ncrs, ncr.id, decoy)
        qc.ncrs = store

        returned = qc.verify_remediation(ncr.id, self.inspector)

        self.assertIs(returned, ncr)
        self.assertEqual(ncr.status, self.NCRStatus.VERIFIED)
        self.assertEqual(defect.status, "closed")
        self.assertEqual(defect.verified_by, self.inspector)
        self.assertEqual(decoy.status, self.NCRStatus.ISSUED)
        self.assertEqual(decoy.contractor_response, "")
        self.assertEqual(decoy_defect.status, "open")
        self.assertEqual(decoy_defect.verified_by, "")
        self.assertIsNone(decoy_defect.closed_date)
        self.assertGreaterEqual(store.reads.count(ncr.id), 1)


if __name__ == "__main__":
    unittest.main()
