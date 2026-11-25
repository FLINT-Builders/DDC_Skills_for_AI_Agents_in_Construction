"""Eligibility must ignore credentials that have no usable expiry.

The matcher under test is the implementation in SKILL.md. These tests load
that source and call register_contractor and find_matches. A certification
or license counts toward a required credential only when its valid_until
is a real date on or after today. A missing or unparseable expiry must
leave the firm out of the shortlist and out of the recommendation report.

The suite fails while a missing valid_until is replaced with date.max.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
REQUIRED_CERT = "ISO-9001"
REQUIRED_LICENSE = "ELECTRICAL"
REGION = "NORTH"
UNDATED_ID = "undated-firm"
VALID_ID = "valid-firm"


def _load_skill():
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    wanted = []
    for block in blocks:
        if (
            "class ContractorProfile" in block
            or "class ContractorMatchingEngine" in block
            or "def generate_recommendation_report" in block
        ):
            wanted.append(block)
    assert len(wanted) == 3, f"expected profile, engine, and report blocks, found {len(wanted)}"
    namespace = {}
    for block in wanted:
        exec(block, namespace)
    return namespace


SKILL = _load_skill()
ContractorMatchingEngine = SKILL["ContractorMatchingEngine"]
ContractorProfile = SKILL["ContractorProfile"]
ContractorSize = SKILL["ContractorSize"]
ProjectRequirements = SKILL["ProjectRequirements"]
WorkCategory = SKILL["WorkCategory"]
generate_recommendation_report = SKILL["generate_recommendation_report"]


def _profile(contractor_id, company_name, certifications, licenses):
    return ContractorProfile(
        contractor_id=contractor_id,
        company_name=company_name,
        legal_name=company_name,
        registration_number="REG-1",
        size=ContractorSize.SMALL,
        founded_year=2000,
        employees_count=20,
        specializations=[WorkCategory.CONCRETE],
        equipment_owned=[],
        max_project_value=10_000_000,
        min_project_value=0,
        certifications=certifications,
        licenses=licenses,
        completed_projects=1,
        active_projects=0,
        operating_regions=[REGION],
        current_workload_pct=10,
    )


def _requirements(certifications=None, licenses=None):
    return ProjectRequirements(
        project_id="P-1",
        project_name="Pier repair",
        work_categories=[WorkCategory.CONCRETE],
        required_certifications=list(certifications or []),
        required_licenses=list(licenses or []),
        region=REGION,
        estimated_value=500_000,
        start_date=date.today() + timedelta(days=30),
        duration_months=6,
    )


def _ranked_ids(engine, requirements):
    matches = engine.find_matches(requirements)
    return [match["contractor_id"] for match in matches], matches


def _engine_with(certifications, licenses, contractor_id=UNDATED_ID, company_name="Undated Co"):
    engine = ContractorMatchingEngine()
    engine.register_contractor(
        _profile(contractor_id, company_name, certifications, licenses)
    )
    return engine


def test_omitted_certification_and_license_expiry_are_not_ranked():
    """Attack path: required name and type only, no valid_until on either."""
    engine = _engine_with(
        certifications=[{"name": REQUIRED_CERT}],
        licenses=[{"type": REQUIRED_LICENSE}],
    )
    ids, matches = _ranked_ids(
        engine,
        _requirements(certifications=[REQUIRED_CERT], licenses=[REQUIRED_LICENSE]),
    )
    assert ids == [], f"undated firm was ranked: {matches}"


def test_omitted_certification_expiry_is_not_ranked_when_license_is_current():
    engine = _engine_with(
        certifications=[{"name": REQUIRED_CERT}],
        licenses=[{"type": REQUIRED_LICENSE, "number": "L-1", "valid_until": date(2099, 1, 1)}],
    )
    ids, matches = _ranked_ids(
        engine,
        _requirements(certifications=[REQUIRED_CERT], licenses=[REQUIRED_LICENSE]),
    )
    assert ids == [], f"cert with no expiry was ranked: {matches}"


def test_omitted_license_expiry_is_not_ranked_when_certification_is_current():
    engine = _engine_with(
        certifications=[{"name": REQUIRED_CERT, "issuer": "Registrar", "valid_until": date(2099, 1, 1)}],
        licenses=[{"type": REQUIRED_LICENSE}],
    )
    ids, matches = _ranked_ids(
        engine,
        _requirements(certifications=[REQUIRED_CERT], licenses=[REQUIRED_LICENSE]),
    )
    assert ids == [], f"license with no expiry was ranked: {matches}"


@pytest.mark.parametrize(
    "expiry",
    [None, "", "2099-01-01", "not-a-date", 20990101],
)
def test_unparseable_certification_expiry_is_not_ranked(expiry):
    engine = _engine_with(
        certifications=[{"name": REQUIRED_CERT, "valid_until": expiry}],
        licenses=[{"type": REQUIRED_LICENSE, "number": "L-1", "valid_until": date(2099, 1, 1)}],
    )
    ids, matches = _ranked_ids(
        engine,
        _requirements(certifications=[REQUIRED_CERT], licenses=[REQUIRED_LICENSE]),
    )
    assert ids == [], f"unparseable cert expiry {expiry!r} was ranked: {matches}"


@pytest.mark.parametrize(
    "expiry",
    [None, "", "2099-01-01", "not-a-date", 20990101],
)
def test_unparseable_license_expiry_is_not_ranked(expiry):
    engine = _engine_with(
        certifications=[{"name": REQUIRED_CERT, "issuer": "Registrar", "valid_until": date(2099, 1, 1)}],
        licenses=[{"type": REQUIRED_LICENSE, "valid_until": expiry}],
    )
    ids, matches = _ranked_ids(
        engine,
        _requirements(certifications=[REQUIRED_CERT], licenses=[REQUIRED_LICENSE]),
    )
    assert ids == [], f"unparseable license expiry {expiry!r} was ranked: {matches}"


def test_expired_credentials_are_not_ranked():
    yesterday = date.today() - timedelta(days=1)
    engine = _engine_with(
        certifications=[{"name": REQUIRED_CERT, "valid_until": yesterday}],
        licenses=[{"type": REQUIRED_LICENSE, "valid_until": yesterday}],
    )
    ids, _matches = _ranked_ids(
        engine,
        _requirements(certifications=[REQUIRED_CERT], licenses=[REQUIRED_LICENSE]),
    )
    assert ids == []


def test_current_credentials_remain_ranked():
    engine = _engine_with(
        certifications=[{"name": REQUIRED_CERT, "valid_until": date.today()}],
        licenses=[{"type": REQUIRED_LICENSE, "valid_until": date(2099, 1, 1)}],
        contractor_id=VALID_ID,
        company_name="Valid Co",
    )
    ids, matches = _ranked_ids(
        engine,
        _requirements(certifications=[REQUIRED_CERT], licenses=[REQUIRED_LICENSE]),
    )
    assert ids == [VALID_ID]
    assert matches[0]["total_score"] > 0


def test_undated_firm_is_not_ranked_ahead_of_a_current_firm():
    engine = ContractorMatchingEngine()
    engine.register_contractor(
        _profile(
            UNDATED_ID,
            "Undated Co",
            [{"name": REQUIRED_CERT}],
            [{"type": REQUIRED_LICENSE}],
        )
    )
    engine.register_contractor(
        _profile(
            VALID_ID,
            "Valid Co",
            [{"name": REQUIRED_CERT, "valid_until": date(2099, 1, 1)}],
            [{"type": REQUIRED_LICENSE, "valid_until": date(2099, 1, 1)}],
        )
    )
    ids, matches = _ranked_ids(
        engine,
        _requirements(certifications=[REQUIRED_CERT], licenses=[REQUIRED_LICENSE]),
    )
    assert ids == [VALID_ID], f"shortlist was {matches}"


def test_recommendation_report_omits_firm_with_no_expiry(tmp_path):
    engine = ContractorMatchingEngine()
    engine.register_contractor(
        _profile(
            UNDATED_ID,
            "Undated Co",
            [{"name": REQUIRED_CERT}],
            [{"type": REQUIRED_LICENSE}],
        )
    )
    engine.register_contractor(
        _profile(
            VALID_ID,
            "Valid Co",
            [{"name": REQUIRED_CERT, "valid_until": date(2099, 1, 1)}],
            [{"type": REQUIRED_LICENSE, "valid_until": date(2099, 1, 1)}],
        )
    )
    output = tmp_path / "recommendations.xlsx"
    generate_recommendation_report(
        engine,
        _requirements(certifications=[REQUIRED_CERT], licenses=[REQUIRED_LICENSE]),
        str(output),
    )
    rankings = pd.read_excel(output, sheet_name="Rankings")
    names = rankings["Contractor"].tolist() if "Contractor" in rankings.columns else []
    assert "Undated Co" not in names
    assert "Valid Co" in names


def test_no_credential_requirement_still_ranks_a_firm():
    engine = _engine_with(certifications=[], licenses=[], contractor_id=VALID_ID, company_name="Valid Co")
    ids, _matches = _ranked_ids(engine, _requirements())
    assert ids == [VALID_ID]
