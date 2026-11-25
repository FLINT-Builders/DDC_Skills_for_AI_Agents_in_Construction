"""Spec tests for NaN and non-finite values in the validation rules builder.

These tests fail while the skill accepts missing or non-finite numbers as
valid cost, schedule, and BIM records. They pass once required rules treat
NaN as missing and range rules reject every non-finite number.

The implementation under test is the code block in SKILL.md, loaded from
that file so a fix to the skill is what makes this suite pass.
"""

import math
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook


SKILL_PATH = Path(__file__).resolve().parent / "SKILL.md"

# Values a caller can hand the validator without going through pandas:
# JSON or direct input keeps these strings, and float() turns them into
# NaN or infinity. Range rules must reject all of them.
NON_FINITE_RANGE_INPUTS = [
    float("nan"),
    float("inf"),
    float("-inf"),
    "nan",
    "NaN",
    "NAN",
    "nan ",
    " nan",
    "inf",
    "+inf",
    "-inf",
    "Infinity",
    "+Infinity",
    "-Infinity",
    "infinity",
]


def load_skill():
    """Execute the implementation block published in SKILL.md."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    code = None
    for part in text.split("```python\n"):
        body, _, _ = part.partition("```")
        if "class ValidationRulesBuilder:" in body and "def _apply_rule" in body:
            code = body
            break
    if code is None:
        raise AssertionError(f"implementation block not found in {SKILL_PATH}")
    namespace = {"__name__": "validation_rules_builder_skill"}
    exec(compile(code, str(SKILL_PATH), "exec"), namespace)
    return (
        namespace["ValidationRulesBuilder"],
        namespace["ConstructionValidators"],
        namespace["Severity"],
    )


def write_sheet(path, headers, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)


def records_from_sheet(path):
    """Load a sheet the way the skill's caller is told to accept Excel."""
    frame = pd.read_excel(path)
    return frame.to_dict("records"), frame


def error_issues(summary, field=None, record_index=None):
    issues = [
        issue
        for issue in summary["issues"]
        if issue["severity"] == "error"
        and (field is None or issue["field"] == field)
        and (record_index is None or issue["record_index"] == record_index)
    ]
    return issues


def assert_rejected_record(summary, record_index, fields):
    """The record is not counted valid, and each named field has an error."""
    invalid_indexes = {
        issue["record_index"]
        for issue in summary["issues"]
        if issue["severity"] == "error"
    }
    assert record_index in invalid_indexes
    assert summary["valid_records"] == summary["total_records"] - len(invalid_indexes)
    for field in fields:
        assert error_issues(summary, field, record_index), (
            f"record {record_index} field {field} was accepted: {summary['issues']}"
        )


@pytest.fixture(scope="module")
def skill():
    return load_skill()


def test_required_rule_rejects_float_nan(skill):
    """NaN is missing. A required field must not come back valid."""
    ValidationRulesBuilder, _, Severity = skill
    builder = ValidationRulesBuilder().add_required_rule("desc_required", "description")

    for value in (float("nan"), math.nan):
        result = builder.validate_record({"description": value})[0]
        assert result.field == "description"
        assert result.severity == Severity.ERROR
        assert result.is_valid is False

    summary = builder.validate_records(
        [{"description": float("nan")}, {"description": "Concrete"}]
    )
    assert summary["valid_records"] == 1
    assert summary["error_count"] == 1
    assert_rejected_record(summary, 0, ["description"])


def test_required_rule_still_rejects_none_empty_string_and_empty_list(skill):
    ValidationRulesBuilder, _, _ = skill
    builder = ValidationRulesBuilder().add_required_rule("desc_required", "description")
    for value in (None, "", []):
        assert builder.validate_record({"description": value})[0].is_valid is False
    assert builder.validate_record({})[0].is_valid is False
    assert builder.validate_record({"description": "Concrete"})[0].is_valid is True


def test_range_rule_rejects_non_finite_numbers_and_their_spellings(skill):
    """Comparisons with NaN are false, so the range rule must decide first."""
    ValidationRulesBuilder, _, Severity = skill
    builder = ValidationRulesBuilder().add_range_rule(
        "quantity_positive", "quantity", min_value=0
    )
    numpy_values = pd.Series([float("nan"), float("inf"), float("-inf")], dtype="float64")

    values = list(NON_FINITE_RANGE_INPUTS) + [numpy_values.iloc[i] for i in range(3)]
    for value in values:
        result = builder.validate_record({"quantity": value})[0]
        assert result.is_valid is False, repr(value)
        assert result.field == "quantity"
        assert result.severity == Severity.ERROR

    both_bounds = ValidationRulesBuilder().add_range_rule(
        "progress_range", "percent_complete", min_value=0, max_value=100
    )
    for value in values:
        result = both_bounds.validate_record({"percent_complete": value})[0]
        assert result.is_valid is False, repr(value)


def test_range_rule_keeps_finite_bounds_and_skips_only_none_and_empty(skill):
    ValidationRulesBuilder, _, _ = skill
    builder = ValidationRulesBuilder().add_range_rule(
        "quantity_positive", "quantity", min_value=0, max_value=100
    )
    for value in (None, ""):
        assert builder.validate_record({"quantity": value})[0].is_valid is True
    for value in (0, 0.0, 1, 100, "50", "0"):
        assert builder.validate_record({"quantity": value})[0].is_valid is True
    for value in (-0.1, 100.01, "abc", " "):
        assert builder.validate_record({"quantity": value})[0].is_valid is False


def test_cost_sheet_blank_and_nan_text_are_not_valid_records(skill, tmp_path):
    """Subcontractor workbook, loaded with pandas, then the built-in cost validator.

    Blank quantity/unit_cost cells and the text nan/inf become float NaN or
    infinity. Those lines must be errors. A later total may only include
    records this validator accepted, and that total has to be the finite line.
    """
    _, ConstructionValidators, _ = skill
    path = tmp_path / "cost_sheet.xlsx"
    write_sheet(
        path,
        ["cost_code", "description", "quantity", "unit_cost", "unit"],
        [
            ["A-100", "Concrete", None, None, "CY"],
            ["B-200", "Rebar", "nan", 12, "EA"],
            ["C-300", "Steel", 4, "inf", "LF"],
            ["D-400", "Valid item", 10, 25.5, "SF"],
            ["E-500", "Negative", -1, 5, "HR"],
        ],
    )
    records, frame = records_from_sheet(path)

    # Preconditions from pandas' read of this workbook: blanks and the
    # spellings above are non-finite floats before validation runs.
    assert frame["quantity"].dtype.kind == "f"
    assert frame["unit_cost"].dtype.kind == "f"
    for index in (0, 1):
        assert isinstance(records[index]["quantity"], float)
        assert math.isnan(records[index]["quantity"])
    assert math.isnan(records[0]["unit_cost"])
    assert math.isinf(records[2]["unit_cost"])
    assert records[3]["quantity"] == 10
    assert records[3]["unit_cost"] == 25.5

    summary = ConstructionValidators.cost_item_validator().validate_records(records)
    assert summary["total_records"] == 5
    assert summary["valid_records"] == 1
    assert_rejected_record(summary, 0, ["quantity", "unit_cost"])
    assert_rejected_record(summary, 1, ["quantity"])
    assert_rejected_record(summary, 2, ["unit_cost"])
    assert_rejected_record(summary, 4, ["quantity"])
    assert error_issues(summary, record_index=3) == []

    accepted = [
        record
        for index, record in enumerate(records)
        if index
        not in {
            issue["record_index"]
            for issue in summary["issues"]
            if issue["severity"] == "error"
        }
    ]
    extended = pd.Series(
        [record["quantity"] * record["unit_cost"] for record in accepted]
    )
    # skipna stays on, which is the default. Accepted rows must already be
    # finite, so the total cannot depend on NaN being dropped.
    assert extended.sum(skipna=False) == pytest.approx(10 * 25.5)


def test_cost_validator_rejects_direct_non_finite_quantity_and_unit_cost(skill):
    """Same built-in rules when the caller passes dicts instead of a sheet."""
    _, ConstructionValidators, _ = skill
    validator = ConstructionValidators.cost_item_validator()
    numpy_nan = pd.Series([float("nan")], dtype="float64").iloc[0]
    records = [
        {
            "cost_code": "A-100",
            "description": "Concrete",
            "quantity": numpy_nan,
            "unit_cost": float("inf"),
            "unit": "CY",
        },
        {
            "cost_code": "B-200",
            "description": "Rebar",
            "quantity": "nan",
            "unit_cost": "Infinity",
            "unit": "EA",
        },
        {
            "cost_code": "C-300",
            "description": float("nan"),
            "quantity": 2,
            "unit_cost": 3,
            "unit": "LF",
        },
        {
            "cost_code": "D-400",
            "description": "Valid item",
            "quantity": 10,
            "unit_cost": 25.5,
            "unit": "SF",
        },
    ]
    summary = validator.validate_records(records)
    assert summary["valid_records"] == 1
    assert_rejected_record(summary, 0, ["quantity", "unit_cost"])
    assert_rejected_record(summary, 1, ["quantity", "unit_cost"])
    assert_rejected_record(summary, 2, ["description"])
    assert error_issues(summary, record_index=3) == []

    description = validator.validate_record(records[2])
    description_results = [item for item in description if item.field == "description"]
    assert len(description_results) == 1
    assert description_results[0].is_valid is False


def test_schedule_validator_rejects_nan_name_duration_and_percent(skill, tmp_path):
    _, ConstructionValidators, _ = skill
    path = tmp_path / "schedule.xlsx"
    write_sheet(
        path,
        ["activity_id", "activity_name", "duration", "percent_complete"],
        [
            ["A1001", "Pour", None, None],
            ["B2002", "nan", 5, 40],
            ["C3003", "Cure", "inf", 40],
            ["D4004", "Form", 3, "nan"],
            ["E5005", "Set", 8, 150],
            ["F6006", "Finish", 4, 25],
        ],
    )
    records, _ = records_from_sheet(path)
    assert math.isnan(records[0]["duration"])
    assert math.isnan(records[0]["percent_complete"])
    assert isinstance(records[1]["activity_name"], float) and math.isnan(
        records[1]["activity_name"]
    )
    assert math.isinf(records[2]["duration"])
    assert math.isnan(records[3]["percent_complete"])

    summary = ConstructionValidators.schedule_activity_validator().validate_records(
        records
    )
    assert summary["total_records"] == 6
    assert summary["valid_records"] == 1
    assert_rejected_record(summary, 0, ["duration", "percent_complete"])
    assert_rejected_record(summary, 1, ["activity_name"])
    assert_rejected_record(summary, 2, ["duration"])
    assert_rejected_record(summary, 3, ["percent_complete"])
    assert_rejected_record(summary, 4, ["percent_complete"])
    assert error_issues(summary, record_index=5) == []


def test_bim_validator_rejects_nan_required_identifiers(skill, tmp_path):
    """A valid status must not hide missing guid, type, or level.

    The level pattern is only a warning. Counting the element valid when
    those fields arrived as NaN is the bypass.
    """
    _, ConstructionValidators, Severity = skill
    path = tmp_path / "bim.xlsx"
    write_sheet(
        path,
        ["element_guid", "element_type", "level", "status"],
        [
            [None, None, None, "New"],
            ["guid-1", "Wall", "Level 1", "New"],
            [None, "Slab", "Level 2", "Existing"],
            ["guid-3", None, "Level 3", "Temporary"],
            ["guid-4", "Beam", None, "Demolished"],
        ],
    )
    records, _ = records_from_sheet(path)
    assert math.isnan(records[0]["element_guid"])
    assert math.isnan(records[0]["element_type"])
    assert math.isnan(records[0]["level"])
    assert records[0]["status"] == "New"
    assert math.isnan(records[2]["element_guid"])
    assert math.isnan(records[3]["element_type"])
    assert math.isnan(records[4]["level"])

    validator = ConstructionValidators.bim_element_validator()
    summary = validator.validate_records(records)
    assert summary["total_records"] == 5
    assert summary["valid_records"] == 1
    assert_rejected_record(
        summary, 0, ["element_guid", "element_type", "level"]
    )
    assert_rejected_record(summary, 2, ["element_guid"])
    assert_rejected_record(summary, 3, ["element_type"])
    assert_rejected_record(summary, 4, ["level"])
    assert error_issues(summary, record_index=1) == []

    # The required-rule results themselves are invalid. Level also has a
    # warning-severity pattern rule, which must not be the only failure.
    first = validator.validate_record(records[0])
    for field in ("element_guid", "element_type", "level"):
        required_results = [
            item
            for item in first
            if item.field == field and item.severity == Severity.ERROR
        ]
        assert required_results, field
        assert all(item.is_valid is False for item in required_results)


def test_iloc_numpy_scalars_from_the_same_sheet_are_rejected(skill, tmp_path):
    """Row access via iloc yields numpy floats, which share the same checks."""
    _, ConstructionValidators, _ = skill
    path = tmp_path / "iloc_cost.xlsx"
    write_sheet(
        path,
        ["cost_code", "description", "quantity", "unit_cost", "unit"],
        [["A-100", "Concrete", None, 15, "CY"]],
    )
    _, frame = records_from_sheet(path)
    row = frame.iloc[0]
    record = {column: row[column] for column in frame.columns}
    assert type(record["quantity"]).__name__ == "float64"
    assert math.isnan(record["quantity"])

    summary = ConstructionValidators.cost_item_validator().validate_records([record])
    assert summary["valid_records"] == 0
    assert_rejected_record(summary, 0, ["quantity"])
