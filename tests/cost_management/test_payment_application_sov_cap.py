"""Schedule-of-values cap for the payment application generator.

Loads the implementation shipped in the skill's Technical Implementation
block and bills through create_application, calculate_payment_due,
generate_g702, generate_g703, and export_application.

These tests fail while a caller-supplied progress map can push completed
and stored amounts above a line's scheduled value. They pass once each
period is capped so completed-and-stored to date cannot exceed that
scheduled value, percent complete cannot exceed 100, and balance to
finish cannot go negative. Current work is applied first; stored
materials may use only what remains under the cap. Payment due, the G702
summary, the G703 continuation, and the exported workbook must all use
those capped amounts.

Amounts that already sit at or under the scheduled value must be unchanged.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

SKILL_RELATIVE = Path(
    "1_DDC_Toolkit/Cost-Management/payment-application-generator/SKILL.md"
)
RETAINAGE = 0.10
CONCRETE = "03"
CONCRETE_VALUE = 2_000_000.0


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / SKILL_RELATIVE).is_file():
            return candidate
    raise RuntimeError(f"could not locate {SKILL_RELATIVE}")


def _load_implementation():
    skill_path = _repo_root() / SKILL_RELATIVE
    text = skill_path.read_text(encoding="utf-8")
    match = re.search(
        r"## Technical Implementation\s+```python\n(.*?)```",
        text,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError("Technical Implementation block not found in SKILL.md")
    namespace = {"__file__": str(skill_path)}
    exec(compile(match.group(1), str(skill_path), "exec"), namespace)
    return namespace["PaymentApplicationGenerator"]


PaymentApplicationGenerator = _load_implementation()


def _payment_due(total_completed: float, previous: float = 0.0, retainage: float = RETAINAGE) -> float:
    retainage_held = total_completed * retainage
    return (total_completed - retainage_held) - previous


def _generator(contract: float, items: list[dict], retainage: float = RETAINAGE):
    generator = PaymentApplicationGenerator(
        project_name="Office Tower",
        contractor="ABC Construction",
        owner="XYZ Development",
        original_contract=contract,
        retainage=retainage,
    )
    generator.setup_sov(items)
    return generator


def _concrete_generator(contract: float = CONCRETE_VALUE, value: float = CONCRETE_VALUE):
    return _generator(
        contract,
        [{"number": CONCRETE, "description": "Concrete", "value": value}],
    )


def _apply(generator, progress: dict, period_from: date, period_to: date):
    return generator.create_application(period_from, period_to, progress)


def _line(application, number: str = CONCRETE):
    matches = [item for item in application.items if item.item_number == number]
    assert len(matches) == 1
    return matches[0]


def _assert_line_capped(item, expected_total: float):
    assert item.total_completed_current == expected_total
    assert item.total_completed_current <= item.scheduled_value
    assert item.percent_complete <= 100
    assert item.balance_to_finish >= 0
    assert item.balance_to_finish == item.scheduled_value - item.total_completed_current


def _g703_row(frame: pd.DataFrame, description: str) -> pd.Series:
    rows = frame.loc[frame["Description"] == description]
    assert len(rows) == 1
    return rows.iloc[0]


class TestProgressAtOrUnderScheduledValue:
    def test_quick_start_amounts_are_unchanged(self):
        generator = _generator(
            10_000_000,
            [
                {"number": "01", "description": "General Conditions", "value": 500_000},
                {"number": "02", "description": "Site Work", "value": 800_000},
                {"number": "03", "description": "Concrete", "value": 2_000_000},
            ],
        )
        application = _apply(
            generator,
            {
                "01": {"work": 50_000},
                "02": {"work": 200_000, "materials": 50_000},
                "03": {"work": 100_000},
            },
            date(2024, 1, 1),
            date(2024, 1, 31),
        )

        assert _line(application, "01").work_completed_current == 50_000
        assert _line(application, "02").work_completed_current == 200_000
        assert _line(application, "02").materials_stored_current == 50_000
        assert _line(application, "03").work_completed_current == 100_000
        assert application.total_completed_to_date == 400_000

        payment = generator.calculate_payment_due(application)
        assert payment["current_payment_due"] == _payment_due(400_000)
        assert payment["balance_to_finish"] == 10_000_000 - 400_000
        assert payment["percent_complete"] <= 100

    def test_work_equal_to_scheduled_value_bills_the_full_line(self):
        generator = _concrete_generator()
        application = _apply(
            generator,
            {CONCRETE: {"work": CONCRETE_VALUE}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        item = _line(application)

        _assert_line_capped(item, CONCRETE_VALUE)
        assert item.percent_complete == 100
        assert item.balance_to_finish == 0
        assert item.work_completed_current == CONCRETE_VALUE
        assert item.materials_stored_current == 0

        payment = generator.calculate_payment_due(application)
        assert payment["current_payment_due"] == _payment_due(CONCRETE_VALUE)
        assert payment["percent_complete"] == 100
        assert payment["balance_to_finish"] == 0


class TestWorkAboveScheduledValue:
    def test_attack_path_caps_concrete_work_on_summary_and_continuation(self):
        """Caller reports 5,000,000 of work against a 2,000,000 concrete line."""
        generator = _concrete_generator()
        application = _apply(
            generator,
            {CONCRETE: {"work": 5_000_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        item = _line(application)

        assert item.work_completed_current == CONCRETE_VALUE
        assert item.materials_stored_current == 0
        _assert_line_capped(item, CONCRETE_VALUE)

        payment = generator.calculate_payment_due(application)
        assert payment["total_completed_to_date"] == CONCRETE_VALUE
        assert payment["current_payment_due"] == _payment_due(CONCRETE_VALUE)
        assert payment["percent_complete"] == 100
        assert payment["balance_to_finish"] == 0

        g702 = generator.generate_g702(application)
        assert g702["total_completed_to_date"] == CONCRETE_VALUE
        assert g702["current_payment_due"] == _payment_due(CONCRETE_VALUE)
        assert g702["balance_to_finish"] == 0
        assert g702["total_earned_less_retainage"] == CONCRETE_VALUE * (1 - RETAINAGE)

        g703 = generator.generate_g703(application)
        row = _g703_row(g703, "Concrete")
        assert row["Work Completed - This Period"] == CONCRETE_VALUE
        assert row["Total Completed & Stored"] == CONCRETE_VALUE
        assert row["% Complete"] == 100
        assert row["Balance to Finish"] == 0
        assert row["Retainage"] == CONCRETE_VALUE * RETAINAGE
        totals = _g703_row(g703, "TOTALS")
        assert totals["% Complete"] <= 100
        assert totals["Balance to Finish"] >= 0

    def test_line_cap_holds_when_contract_sum_is_larger_than_the_line(self):
        generator = _concrete_generator(contract=10_000_000)
        application = _apply(
            generator,
            {CONCRETE: {"work": 5_000_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        item = _line(application)
        _assert_line_capped(item, CONCRETE_VALUE)

        payment = generator.calculate_payment_due(application)
        assert payment["current_payment_due"] == _payment_due(CONCRETE_VALUE)
        assert payment["total_completed_to_date"] == CONCRETE_VALUE
        assert payment["balance_to_finish"] == 10_000_000 - CONCRETE_VALUE
        assert payment["percent_complete"] == round(CONCRETE_VALUE / 10_000_000 * 100, 1)

    def test_over_cap_on_one_line_does_not_reduce_another_line(self):
        generator = _generator(
            2_500_000,
            [
                {"number": "01", "description": "General Conditions", "value": 500_000},
                {"number": "03", "description": "Concrete", "value": CONCRETE_VALUE},
            ],
        )
        application = _apply(
            generator,
            {"01": {"work": 50_000}, CONCRETE: {"work": 5_000_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )

        general = _line(application, "01")
        concrete = _line(application)
        assert general.work_completed_current == 50_000
        assert general.total_completed_current == 50_000
        _assert_line_capped(concrete, CONCRETE_VALUE)

        expected_total = 50_000 + CONCRETE_VALUE
        payment = generator.calculate_payment_due(application)
        assert payment["total_completed_to_date"] == expected_total
        assert payment["current_payment_due"] == _payment_due(expected_total)

    def test_zero_scheduled_value_cannot_be_billed(self):
        generator = _concrete_generator(contract=2_000_000, value=0)
        application = _apply(
            generator,
            {CONCRETE: {"work": 5_000_000, "materials": 25_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        item = _line(application)
        _assert_line_capped(item, 0)
        assert item.work_completed_current == 0
        assert item.materials_stored_current == 0

        payment = generator.calculate_payment_due(application)
        assert payment["total_completed_to_date"] == 0
        assert payment["current_payment_due"] == 0
        assert payment["balance_to_finish"] == 2_000_000


class TestStoredMaterialsShareTheSameCap:
    def test_materials_alone_cannot_exceed_scheduled_value(self):
        generator = _concrete_generator()
        application = _apply(
            generator,
            {CONCRETE: {"materials": 3_000_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        item = _line(application)

        assert item.work_completed_current == 0
        assert item.materials_stored_current == CONCRETE_VALUE
        _assert_line_capped(item, CONCRETE_VALUE)

        payment = generator.calculate_payment_due(application)
        assert payment["current_payment_due"] == _payment_due(CONCRETE_VALUE)

        g703 = generator.generate_g703(application)
        row = _g703_row(g703, "Concrete")
        assert row["Materials Stored - This Period"] == CONCRETE_VALUE
        assert row["Total Completed & Stored"] == CONCRETE_VALUE
        assert row["% Complete"] <= 100

    def test_materials_only_consume_room_left_after_current_work(self):
        generator = _concrete_generator()
        application = _apply(
            generator,
            {CONCRETE: {"work": 1_500_000, "materials": 800_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        item = _line(application)

        assert item.work_completed_current == 1_500_000
        assert item.materials_stored_current == 500_000
        _assert_line_capped(item, CONCRETE_VALUE)

        payment = generator.calculate_payment_due(application)
        assert payment["current_payment_due"] == _payment_due(CONCRETE_VALUE)
        g702 = generator.generate_g702(application)
        assert g702["current_payment_due"] == _payment_due(CONCRETE_VALUE)

    def test_materials_are_zero_when_current_work_already_fills_the_line(self):
        generator = _concrete_generator()
        application = _apply(
            generator,
            {CONCRETE: {"work": CONCRETE_VALUE, "materials": 100_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        item = _line(application)

        assert item.work_completed_current == CONCRETE_VALUE
        assert item.materials_stored_current == 0
        _assert_line_capped(item, CONCRETE_VALUE)

    def test_under_cap_work_and_materials_are_kept(self):
        generator = _concrete_generator()
        application = _apply(
            generator,
            {CONCRETE: {"work": 100_000, "materials": 50_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        item = _line(application)
        assert item.work_completed_current == 100_000
        assert item.materials_stored_current == 50_000
        _assert_line_capped(item, 150_000)
        payment = generator.calculate_payment_due(application)
        assert payment["current_payment_due"] == _payment_due(150_000)


class TestLaterPeriodsCannotBillPastTheCap:
    def test_second_period_work_is_limited_to_the_remaining_balance(self):
        generator = _concrete_generator()
        first = _apply(
            generator,
            {CONCRETE: {"work": 1_500_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        first_payment = generator.calculate_payment_due(first)
        assert first_payment["current_payment_due"] == _payment_due(1_500_000)

        second = _apply(
            generator,
            {CONCRETE: {"work": 1_000_000}},
            date(2024, 2, 1),
            date(2024, 2, 29),
        )
        item = _line(second)
        assert item.work_completed_current == 500_000
        _assert_line_capped(item, CONCRETE_VALUE)

        second_payment = generator.calculate_payment_due(second)
        assert second_payment["total_completed_to_date"] == CONCRETE_VALUE
        assert second_payment["previous_payments"] == _payment_due(1_500_000)
        assert second_payment["current_payment_due"] == _payment_due(CONCRETE_VALUE, previous=_payment_due(1_500_000))
        assert second_payment["balance_to_finish"] == 0

        g702 = generator.generate_g702(second)
        assert g702["current_payment_due"] == second_payment["current_payment_due"]
        assert g702["previous_certificates"] == _payment_due(1_500_000)

    def test_prior_stored_materials_count_toward_the_cap(self):
        generator = _concrete_generator()
        first = _apply(
            generator,
            {CONCRETE: {"materials": 1_000_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        assert generator.calculate_payment_due(first)["current_payment_due"] == _payment_due(1_000_000)

        second = _apply(
            generator,
            {CONCRETE: {"work": 1_500_000}},
            date(2024, 2, 1),
            date(2024, 2, 29),
        )
        item = _line(second)
        assert item.work_completed_current == 1_000_000
        assert item.materials_stored_current == 0
        _assert_line_capped(item, CONCRETE_VALUE)

        second_payment = generator.calculate_payment_due(second)
        assert second_payment["current_payment_due"] == _payment_due(
            CONCRETE_VALUE, previous=_payment_due(1_000_000)
        )

    def test_later_materials_cannot_reopen_a_full_line(self):
        generator = _concrete_generator()
        first = _apply(
            generator,
            {CONCRETE: {"work": 1_800_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        assert generator.calculate_payment_due(first)["current_payment_due"] == _payment_due(1_800_000)

        second = _apply(
            generator,
            {CONCRETE: {"materials": 500_000}},
            date(2024, 2, 1),
            date(2024, 2, 29),
        )
        item = _line(second)
        assert item.materials_stored_current == 200_000
        assert item.work_completed_current == 0
        _assert_line_capped(item, CONCRETE_VALUE)

        second_payment = generator.calculate_payment_due(second)
        assert second_payment["current_payment_due"] == _payment_due(
            CONCRETE_VALUE, previous=_payment_due(1_800_000)
        )

    def test_excess_does_not_carry_into_a_later_application(self):
        generator = _concrete_generator()
        first = _apply(
            generator,
            {CONCRETE: {"work": 5_000_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        first_due = generator.calculate_payment_due(first)["current_payment_due"]
        assert first_due == _payment_due(CONCRETE_VALUE)

        second = _apply(
            generator,
            {},
            date(2024, 2, 1),
            date(2024, 2, 29),
        )
        item = _line(second)
        _assert_line_capped(item, CONCRETE_VALUE)
        assert item.work_completed_current == 0
        assert item.materials_stored_current == 0

        second_payment = generator.calculate_payment_due(second)
        assert second_payment["total_completed_to_date"] == CONCRETE_VALUE
        assert second_payment["previous_payments"] == first_due
        assert second_payment["current_payment_due"] == 0

        stored = generator.sov_items[CONCRETE]
        assert stored.total_completed_current <= stored.scheduled_value


class TestChangeOrderRaisesTheLineCap:
    def test_billing_the_revised_scheduled_value_is_allowed(self):
        generator = _concrete_generator()
        generator.add_change_order(500_000, "Additional concrete", item_number=CONCRETE)
        application = _apply(
            generator,
            {CONCRETE: {"work": 2_500_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        item = _line(application)
        assert item.scheduled_value == 2_500_000
        _assert_line_capped(item, 2_500_000)
        payment = generator.calculate_payment_due(application)
        assert payment["current_payment_due"] == _payment_due(2_500_000)
        assert payment["balance_to_finish"] == 0

    def test_work_above_the_revised_scheduled_value_is_capped(self):
        generator = _concrete_generator()
        generator.add_change_order(500_000, "Additional concrete", item_number=CONCRETE)
        application = _apply(
            generator,
            {CONCRETE: {"work": 3_000_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        item = _line(application)
        assert item.work_completed_current == 2_500_000
        _assert_line_capped(item, 2_500_000)
        g702 = generator.generate_g702(application)
        assert g702["current_payment_due"] == _payment_due(2_500_000)
        assert g702["balance_to_finish"] == 0
        assert g702["contract_sum_to_date"] == 2_500_000


class TestExportedWorkbookUsesCappedAmounts:
    def test_g702_and_g703_sheets_do_not_contain_the_uncapped_claim(self, tmp_path: Path):
        generator = _concrete_generator()
        application = _apply(
            generator,
            {CONCRETE: {"work": 5_000_000, "materials": 250_000}},
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        output = tmp_path / "pay_app_001.xlsx"
        written = generator.export_application(application, str(output))
        assert Path(written) == output
        assert output.is_file()

        g702 = pd.read_excel(output, sheet_name="G702 Summary")
        due = g702.loc[g702["Field"] == "current_payment_due", "Value"].iloc[0]
        completed = g702.loc[g702["Field"] == "total_completed_to_date", "Value"].iloc[0]
        balance = g702.loc[g702["Field"] == "balance_to_finish", "Value"].iloc[0]
        assert due == _payment_due(CONCRETE_VALUE)
        assert completed == CONCRETE_VALUE
        assert balance == 0

        g703 = pd.read_excel(output, sheet_name="G703 Continuation")
        row = _g703_row(g703, "Concrete")
        assert row["Work Completed - This Period"] == CONCRETE_VALUE
        assert row["Materials Stored - This Period"] == 0
        assert row["Total Completed & Stored"] == CONCRETE_VALUE
        assert row["% Complete"] == 100
        assert row["Balance to Finish"] == 0

        forbidden = {5_000_000, 5_000_000.0, 250_000, 250_000.0, 5_250_000, 5_250_000.0}
        leaked = []
        for sheet in ("G702 Summary", "G703 Continuation"):
            frame = pd.read_excel(output, sheet_name=sheet)
            for value in frame.to_numpy().ravel():
                if value in forbidden:
                    leaked.append((sheet, value))
        assert leaked == []


@pytest.mark.parametrize(
    "progress",
    [
        {CONCRETE: {"work": 5_000_000}},
        {CONCRETE: {"materials": 3_000_000}},
        {CONCRETE: {"work": 1_500_000, "materials": 800_000}},
        {CONCRETE: {"work": CONCRETE_VALUE, "materials": 1}},
    ],
)
def test_every_billed_line_stays_inside_its_scheduled_value(progress):
    generator = _concrete_generator()
    application = _apply(generator, progress, date(2024, 3, 1), date(2024, 3, 31))
    for item in application.items:
        assert item.total_completed_current <= item.scheduled_value
        assert item.percent_complete <= 100
        assert item.balance_to_finish >= 0
    payment = generator.calculate_payment_due(application)
    g702 = generator.generate_g702(application)
    assert payment["current_payment_due"] == g702["current_payment_due"]
    assert payment["current_payment_due"] <= _payment_due(CONCRETE_VALUE)
    assert g702["balance_to_finish"] >= 0
