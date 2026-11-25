"""Same-second defect, inspection, and NCR ids must not replace earlier records.

The quality-control skill publishes QualityControlManager inside SKILL.md.
These tests load that class from the skill file and call the methods an agent
runs: schedule_inspection, conduct_inspection, record_defect, create_ncr,
verify_remediation, get_quality_metrics, and generate_qc_report.

They fail while those methods name records with the current second and store
them in a dictionary keyed only by that id. A later record in the same second
replaces the earlier one, so a critical defect drops out of the open and
overdue counts and out of the Critical Open Defects section, and an NCR raised
from the original id describes and closes the replacement.

Run:

    python3 -m pytest 3_DDC_Insights/Safety-Quality/quality-control-workflow/tests/test_second_resolution_ids.py -v
"""

import re
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"

CRITICAL_DESCRIPTION = "Rebar spacing 8 inches instead of 6 inches per spec"
COSMETIC_DESCRIPTION = "Paint scuff on column cover"
CRITICAL_SPEC = "03200-3.2"
COSMETIC_SPEC = "09900-1.1"
RECORDED_AT = datetime(2026, 1, 15, 10, 30, 0)
TWO_DAYS_LATER = RECORDED_AT + timedelta(days=2)


def load_skill_namespace():
    """Execute the QualityControlManager implementation published in SKILL.md."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    implementation = next(
        block for block in blocks if "class QualityControlManager" in block
    )
    module = types.ModuleType("quality_control_workflow_skill")
    module.__file__ = str(SKILL_PATH)
    exec(compile(implementation, str(SKILL_PATH), "exec"), module.__dict__)
    record_defect = module.QualityControlManager.record_defect
    assert record_defect.__code__.co_filename == str(SKILL_PATH)
    return module


class FrozenClock:
    """Pin the datetime global the skill methods call, then restore it."""

    active = None

    def __init__(self, module, moment):
        self.module = module
        self.original = module.datetime
        base = self.original

        class FrozenDateTime(base):
            @classmethod
            def now(cls, tz=None):
                current = FrozenClock.active
                if tz is not None and current.tzinfo is None:
                    return current.replace(tzinfo=tz)
                return current

        FrozenClock.active = moment
        self._frozen_type = FrozenDateTime
        module.datetime = FrozenDateTime

    def set(self, moment):
        FrozenClock.active = moment

    def restore(self):
        self.module.datetime = self.original


def manager_for(module):
    qc = module.QualityControlManager("PRJ-001", "Office Tower")
    qc.create_itp(
        "CONC",
        [
            {
                "name": "Rebar placement",
                "spec_ref": "03200",
                "criteria": "Per drawings",
                "hold_point": True,
            },
            {
                "name": "Concrete finish",
                "spec_ref": "03350",
                "criteria": "Level within 1/8 inch",
            },
        ],
    )
    return qc


def failed_rebar_inspection(qc, module):
    inspection = qc.schedule_inspection(
        "CONC-001",
        location="Level 3, Grid A-C",
        scheduled_date=RECORDED_AT,
        inspector="QC Engineer",
    )
    qc.conduct_inspection(
        inspection.id,
        result="fail",
        notes="Rebar spacing incorrect",
    )
    return inspection


def record_pair(qc, module, inspection):
    critical = qc.record_defect(
        inspection.id,
        description=CRITICAL_DESCRIPTION,
        location="Level 3, Grid B-2",
        severity=module.DefectSeverity.CRITICAL,
        spec_reference=CRITICAL_SPEC,
        assigned_to="ABC Concrete",
    )
    cosmetic = qc.record_defect(
        inspection.id,
        description=COSMETIC_DESCRIPTION,
        location="Level 3, Grid B-2",
        severity=module.DefectSeverity.COSMETIC,
        spec_reference=COSMETIC_SPEC,
        assigned_to="ABC Concrete",
    )
    return critical, cosmetic


@pytest.fixture
def skill():
    module = load_skill_namespace()
    clock = FrozenClock(module, RECORDED_AT)
    try:
        yield module, clock
    finally:
        clock.restore()


def test_same_second_defect_records_keep_the_critical_defect(skill):
    module, _clock = skill
    qc = manager_for(module)
    inspection = failed_rebar_inspection(qc, module)
    critical, cosmetic = record_pair(qc, module, inspection)

    assert critical.id != cosmetic.id
    assert len(qc.defects) == 2
    stored_critical = qc.defects[critical.id]
    stored_cosmetic = qc.defects[cosmetic.id]
    assert stored_critical is not stored_cosmetic
    assert stored_critical.severity == module.DefectSeverity.CRITICAL
    assert stored_critical.description == CRITICAL_DESCRIPTION
    assert stored_critical.spec_reference == CRITICAL_SPEC
    assert stored_critical.status == "open"
    assert stored_cosmetic.severity == module.DefectSeverity.COSMETIC
    assert stored_cosmetic.description == COSMETIC_DESCRIPTION


def test_scripted_loop_of_same_second_defects_keeps_every_result(skill):
    module, _clock = skill
    qc = manager_for(module)
    inspection = failed_rebar_inspection(qc, module)
    severities = [
        module.DefectSeverity.CRITICAL,
        module.DefectSeverity.MAJOR,
        module.DefectSeverity.MINOR,
        module.DefectSeverity.COSMETIC,
    ]
    recorded = []
    for index, severity in enumerate(severities):
        recorded.append(
            qc.record_defect(
                inspection.id,
                description=f"Inspection result {index}",
                location=f"Grid {index}",
                severity=severity,
                spec_reference=f"03200-{index}",
            )
        )

    assert len({defect.id for defect in recorded}) == len(recorded)
    assert len(qc.defects) == len(recorded)
    stored = {defect.description: defect for defect in qc.defects.values()}
    for index, severity in enumerate(severities):
        kept = stored[f"Inspection result {index}"]
        assert kept.severity == severity
        assert kept.spec_reference == f"03200-{index}"


def test_metrics_and_report_still_show_the_critical_defect(skill):
    module, clock = skill
    qc = manager_for(module)
    inspection = failed_rebar_inspection(qc, module)
    critical, cosmetic = record_pair(qc, module, inspection)

    clock.set(TWO_DAYS_LATER)
    metrics = qc.get_quality_metrics()
    report = qc.generate_qc_report()

    assert metrics["open_defects"] == 2
    assert metrics["overdue_defects"] == 1
    overdue = [
        defect
        for defect in qc.defects.values()
        if defect.status == "open" and defect.due_date and defect.due_date < TWO_DAYS_LATER
    ]
    assert [defect.id for defect in overdue] == [critical.id]
    assert overdue[0].severity == module.DefectSeverity.CRITICAL
    assert "Critical Open Defects" in report
    assert critical.id in report
    assert CRITICAL_DESCRIPTION in report
    assert cosmetic.id in qc.defects


def test_ncr_for_the_critical_id_describes_that_defect(skill):
    module, _clock = skill
    qc = manager_for(module)
    inspection = failed_rebar_inspection(qc, module)
    critical, cosmetic = record_pair(qc, module, inspection)

    ncr = qc.create_ncr(
        [critical.id],
        "Rebar Spacing Non-Conformance",
        "ABC Concrete",
    )

    linked = qc.defects[ncr.defect_ids[0]]
    assert linked.severity == module.DefectSeverity.CRITICAL
    assert linked.description == CRITICAL_DESCRIPTION
    assert linked.spec_reference == CRITICAL_SPEC
    assert CRITICAL_DESCRIPTION in ncr.description
    assert CRITICAL_SPEC in ncr.description
    assert COSMETIC_DESCRIPTION not in ncr.description
    assert ncr.spec_sections == [CRITICAL_SPEC]
    assert cosmetic.id in qc.defects
    assert qc.defects[cosmetic.id].description == COSMETIC_DESCRIPTION


def test_verify_remediation_closes_the_critical_defect_only(skill):
    module, _clock = skill
    qc = manager_for(module)
    inspection = failed_rebar_inspection(qc, module)
    critical, cosmetic = record_pair(qc, module, inspection)
    ncr = qc.create_ncr(
        [critical.id],
        "Rebar Spacing Non-Conformance",
        "ABC Concrete",
    )
    qc.issue_ncr(ncr.id)

    verified = qc.verify_remediation(ncr.id, verified_by="QC Engineer")

    closed = qc.defects[verified.defect_ids[0]]
    assert closed.id == critical.id
    assert closed.severity == module.DefectSeverity.CRITICAL
    assert closed.description == CRITICAL_DESCRIPTION
    assert closed.status == "closed"
    assert closed.verified_by == "QC Engineer"
    assert qc.defects[cosmetic.id].status == "open"
    assert qc.defects[cosmetic.id].description == COSMETIC_DESCRIPTION
    assert qc.defects[cosmetic.id].severity == module.DefectSeverity.COSMETIC


def test_same_second_inspections_keep_the_failed_rebar_inspection(skill):
    module, _clock = skill
    qc = manager_for(module)
    first = qc.schedule_inspection(
        "CONC-001",
        location="Level 3, Grid A-C",
        scheduled_date=RECORDED_AT,
        inspector="QC Engineer",
    )
    second = qc.schedule_inspection(
        "CONC-002",
        location="Level 4, Grid D-E",
        scheduled_date=RECORDED_AT,
        inspector="QC Engineer",
    )

    assert first.id != second.id
    assert len(qc.inspections) == 2
    assert qc.inspections[first.id].location == "Level 3, Grid A-C"
    assert qc.inspections[second.id].location == "Level 4, Grid D-E"

    failed = qc.conduct_inspection(
        first.id,
        result="fail",
        notes="Rebar spacing incorrect",
    )
    assert failed is qc.inspections[first.id]
    assert failed.status == module.QCStatus.FAILED
    assert failed.location == "Level 3, Grid A-C"
    assert qc.inspections[second.id].status == module.QCStatus.PENDING


def test_same_second_ncrs_keep_the_rebar_ncr(skill):
    module, clock = skill
    qc = manager_for(module)
    inspection = failed_rebar_inspection(qc, module)
    critical = qc.record_defect(
        inspection.id,
        description=CRITICAL_DESCRIPTION,
        location="Level 3, Grid B-2",
        severity=module.DefectSeverity.CRITICAL,
        spec_reference=CRITICAL_SPEC,
    )
    clock.set(RECORDED_AT + timedelta(seconds=1))
    cover = qc.record_defect(
        inspection.id,
        description="Cover depth 1 inch instead of 2 inches",
        location="Level 3, Grid C-4",
        severity=module.DefectSeverity.MAJOR,
        spec_reference="03200-3.4",
    )
    clock.set(RECORDED_AT + timedelta(seconds=2))
    rebar_ncr = qc.create_ncr(
        [critical.id],
        "Rebar Spacing Non-Conformance",
        "ABC Concrete",
    )
    cover_ncr = qc.create_ncr(
        [cover.id],
        "Cover Depth Non-Conformance",
        "ABC Concrete",
    )

    assert rebar_ncr.id != cover_ncr.id
    assert len(qc.ncrs) == 2
    assert qc.ncrs[rebar_ncr.id].title == "Rebar Spacing Non-Conformance"
    assert qc.ncrs[rebar_ncr.id].defect_ids == [critical.id]
    assert CRITICAL_DESCRIPTION in qc.ncrs[rebar_ncr.id].description
    assert qc.ncrs[cover_ncr.id].title == "Cover Depth Non-Conformance"
    assert "Cover depth 1 inch instead of 2 inches" in qc.ncrs[cover_ncr.id].description

    issued = qc.issue_ncr(rebar_ncr.id)
    assert issued is qc.ncrs[rebar_ncr.id]
    assert issued.status == module.NCRStatus.ISSUED
    assert issued.title == "Rebar Spacing Non-Conformance"
    assert qc.ncrs[cover_ncr.id].status == module.NCRStatus.DRAFT


def test_real_clock_burst_keeps_every_defect():
    """Two or more record_defect calls in one wall-clock second must all remain.

    This does not freeze time. The skill's own datetime.now() supplies the ids.
    """
    module = load_skill_namespace()
    qc = manager_for(module)
    inspection = qc.schedule_inspection(
        "CONC-001",
        location="Level 3, Grid A-C",
        scheduled_date=datetime.now(),
        inspector="QC Engineer",
    )
    qc.conduct_inspection(inspection.id, result="fail", notes="Rebar spacing incorrect")

    recorded = []
    for index in range(20):
        severity = (
            module.DefectSeverity.CRITICAL if index == 0 else module.DefectSeverity.COSMETIC
        )
        recorded.append(
            qc.record_defect(
                inspection.id,
                description=f"Burst defect {index}",
                location="Level 3, Grid B-2",
                severity=severity,
                spec_reference=CRITICAL_SPEC if index == 0 else COSMETIC_SPEC,
            )
        )

    assert len(qc.defects) == 20
    assert len({defect.id for defect in recorded}) == 20
    critical = qc.defects[recorded[0].id]
    assert critical.severity == module.DefectSeverity.CRITICAL
    assert critical.description == "Burst defect 0"
    assert critical.status == "open"
    for index in range(1, 20):
        kept = qc.defects[recorded[index].id]
        assert kept.description == f"Burst defect {index}"
        assert kept.severity == module.DefectSeverity.COSMETIC
