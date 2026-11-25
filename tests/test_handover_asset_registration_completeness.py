"""Handover asset-registration completeness.

These tests execute the Python implementation in
``5_DDC_Innovative/as-built-documentation/SKILL.md``. They fail while
``generate_handover_checklist`` treats any one registered asset as proof
that all major equipment is registered, that warranty information is
entered, and that maintenance schedules are defined.

Contract the checklist must satisfy:

* ``AssetRegistry.required_asset_ids`` is the required major-equipment
  inventory. Entries are ``asset_id`` values, not asset tags.
* If that attribute is missing or empty, no required inventory was
  supplied. The Asset Registration category and all three of its items
  stay ``incomplete``.
* ``All major equipment registered`` is ``complete`` only when every
  required id is present in ``AssetRegistry.assets``.
* ``Warranty information entered`` is ``complete`` only when every
  required asset is registered and its ``warranty_end`` is not None.
  A past ``warranty_end`` still counts as entered. A warranty on any
  other asset does not.
* ``Maintenance schedules defined`` is ``complete`` only when every
  required asset is registered and its ``maintenance_schedules`` list
  is non-empty. A schedule on any other asset does not.
* The category ``complete`` flag is true only when all three items are
  ``complete``. It is not true merely because ``total_assets > 0``.
* The Training category stays incomplete, and ``overall_status`` stays
  ``incomplete`` while training is pending.

Operations covered: the category flag and the three item predicates in
``generate_handover_checklist``, fed by ``register_asset``,
``add_warranty``, ``add_maintenance_schedule``, and ``import_from_bim``.

Left untested because they do not share this completeness bug:
``check_handover_completeness`` (required document types), the Documents
category (mandatory requirements versus finalized documents),
``get_warranty_report`` (counts only), ``OMManualGenerator`` (lists
registered assets), ``export_to_cmms`` (exports rows), and the hard-coded
Training items.
"""

from __future__ import annotations

import contextlib
import io
import re
import unittest
from datetime import date
from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "5_DDC_Innovative"
    / "as-built-documentation"
    / "SKILL.md"
)

ITEM_EQUIPMENT = "All major equipment registered"
ITEM_WARRANTY = "Warranty information entered"
ITEM_MAINTENANCE = "Maintenance schedules defined"

REQUIRED_THREE = ("AHU-1", "CHILLER-1", "PUMP-1")


def load_skill_namespace():
    """Execute every Python block in the skill, in order, as one program."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    if len(blocks) < 5:
        raise AssertionError(f"expected the skill's Python blocks, found {len(blocks)}")

    namespace = {}
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        for index, block in enumerate(blocks):
            exec(compile(block, f"{SKILL_PATH}:block{index}", "exec"), namespace)

    if "generate_handover_checklist" not in namespace:
        raise AssertionError("generate_handover_checklist was not defined by the skill")
    return namespace


class HandoverAssetRegistrationCompletenessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = load_skill_namespace()
        cls.generate = staticmethod(ns["generate_handover_checklist"])
        cls.DocumentManager = ns["DocumentManager"]
        cls.AssetRegistry = ns["AssetRegistry"]
        cls.AssetDetails = ns["AssetDetails"]
        cls.MaintenanceSchedule = ns["MaintenanceSchedule"]

    def setUp(self):
        self.docs = self.DocumentManager("P-100", "North Tower")
        self.registry = self.AssetRegistry("P-100")

    def _asset(self, asset_id, tag=None):
        return self.AssetDetails(
            asset_id=asset_id,
            asset_tag=tag if tag is not None else f"TAG-{asset_id}",
            asset_name=f"Equipment {asset_id}",
            asset_type="equipment",
            category="equipment",
            manufacturer="Acme",
            model_number=f"MODEL-{asset_id}",
            serial_number=f"SN-{asset_id}",
        )

    def _register(self, asset_id, *, warranty=False, maintenance=False, tag=None):
        self.registry.register_asset(self._asset(asset_id, tag=tag))
        if warranty:
            self.registry.add_warranty(
                asset_id,
                date(2026, 1, 15),
                2,
                "Acme Warranty",
                "Parts and labor",
            )
        if maintenance:
            self.registry.add_maintenance_schedule(
                asset_id,
                self.MaintenanceSchedule(
                    schedule_id=f"MS-{asset_id}",
                    task_description="Inspect and service",
                    frequency="quarterly",
                    next_due=date(2026, 6, 1),
                    responsible_party="Facilities",
                    estimated_duration_hours=2.0,
                ),
            )

    def _require(self, asset_ids):
        self.registry.required_asset_ids = list(asset_ids)

    def _checklist(self):
        return self.generate(self.docs, self.registry)

    def _category(self, checklist, name):
        matches = [category for category in checklist["categories"] if category["name"] == name]
        self.assertEqual(len(matches), 1, name)
        return matches[0]

    def _item_status(self, category, item):
        matches = [entry for entry in category["items"] if entry["item"] == item]
        self.assertEqual(len(matches), 1, item)
        return matches[0]["status"]

    def _assert_asset_items(self, equipment, warranty, maintenance, category_complete):
        checklist = self._checklist()
        assets = self._category(checklist, "Asset Registration")
        training = self._category(checklist, "Training")
        expectations = {
            ITEM_EQUIPMENT: equipment,
            ITEM_WARRANTY: warranty,
            ITEM_MAINTENANCE: maintenance,
        }
        for item, expected in expectations.items():
            with self.subTest(item=item):
                self.assertEqual(self._item_status(assets, item), expected)
        with self.subTest(flag="category"):
            self.assertIs(assets["complete"], category_complete)
        with self.subTest(flag="training"):
            self.assertIs(training["complete"], False)
        with self.subTest(flag="overall"):
            self.assertEqual(checklist["overall_status"], "incomplete")
        return checklist

    def _finalize_standard_documents(self):
        for requirement in self.docs.requirements.values():
            if not requirement.is_mandatory:
                continue
            document = self.docs.upload_document(
                requirement.doc_type,
                requirement.system,
                requirement.description,
                "/tmp/handover-not-a-real-file.pdf",
                "contractor",
                requirement_id=requirement.requirement_id,
            )
            self.assertTrue(self.docs.approve_document(document.doc_id, "reviewer"))
            self.assertTrue(self.docs.finalize_document(document.doc_id))

    def test_attack_path_one_equipped_asset_does_not_complete_registration(self):
        self._finalize_standard_documents()
        self._register("AHU-1", warranty=True, maintenance=True)

        checklist = self._assert_asset_items(
            "incomplete",
            "incomplete",
            "incomplete",
            False,
        )
        documents = self._category(checklist, "Documents")
        self.assertIs(documents["complete"], True)
        self.assertFalse(hasattr(self.registry, "required_asset_ids"))

    def test_explicit_empty_required_list_does_not_complete_one_equipped_asset(self):
        self._require([])
        self._register("AHU-1", warranty=True, maintenance=True)
        self._assert_asset_items("incomplete", "incomplete", "incomplete", False)

    def test_zero_assets_stay_incomplete_with_no_required_list(self):
        self._assert_asset_items("incomplete", "incomplete", "incomplete", False)

    def test_zero_assets_stay_incomplete_when_equipment_is_required(self):
        self._require(REQUIRED_THREE)
        self._assert_asset_items("incomplete", "incomplete", "incomplete", False)

    def test_bim_import_of_one_asset_does_not_complete_registration(self):
        self.registry.import_from_bim(
            [
                {
                    "tag": "T-PUMP",
                    "name": "Domestic water pump",
                    "type": "pump",
                    "category": "equipment",
                    "manufacturer": "Acme",
                    "model": "P-100",
                    "serial": "SN-PUMP",
                }
            ]
        )
        self.assertEqual(len(self.registry.assets), 1)
        self._assert_asset_items("incomplete", "incomplete", "incomplete", False)

    def test_bim_import_then_warranty_and_schedule_still_needs_required_inventory(self):
        self.registry.import_from_bim(
            [
                {
                    "tag": "T-AHU",
                    "name": "Air handling unit",
                    "type": "ahu",
                    "category": "equipment",
                    "manufacturer": "Acme",
                    "model": "AHU-100",
                    "serial": "SN-AHU",
                }
            ]
        )
        imported_id = next(iter(self.registry.assets))
        self.registry.add_warranty(imported_id, date(2026, 1, 15), 2, "Acme Warranty")
        self.registry.add_maintenance_schedule(
            imported_id,
            self.MaintenanceSchedule(
                schedule_id="MS-IMP",
                task_description="Filter change",
                frequency="quarterly",
                next_due=date(2026, 6, 1),
                responsible_party="Facilities",
                estimated_duration_hours=1.5,
            ),
        )
        self._assert_asset_items("incomplete", "incomplete", "incomplete", False)

    def test_one_of_three_required_assets_does_not_complete_any_asset_item(self):
        self._require(REQUIRED_THREE)
        self._register("AHU-1", warranty=True, maintenance=True)
        self._assert_asset_items("incomplete", "incomplete", "incomplete", False)

    def test_equal_count_with_different_ids_does_not_complete_registration(self):
        self._require(["AHU-1", "CHILLER-1"])
        self._register("AHU-1", warranty=True, maintenance=True)
        self._register("PUMP-9", warranty=True, maintenance=True, tag="TAG-NOT-CHILLER")
        self._assert_asset_items("incomplete", "incomplete", "incomplete", False)

    def test_warranty_on_one_required_asset_does_not_complete_warranty_item(self):
        self._require(["AHU-1", "CHILLER-1"])
        self._register("AHU-1", warranty=True, maintenance=True)
        self._register("CHILLER-1", warranty=False, maintenance=True)
        self._assert_asset_items("complete", "incomplete", "complete", False)

    def test_schedule_on_one_required_asset_does_not_complete_maintenance_item(self):
        self._require(["AHU-1", "CHILLER-1"])
        self._register("AHU-1", warranty=True, maintenance=True)
        self._register("CHILLER-1", warranty=True, maintenance=False)
        self._assert_asset_items("complete", "complete", "incomplete", False)

    def test_records_on_an_extra_asset_do_not_cover_required_assets(self):
        self._require(["AHU-1", "CHILLER-1"])
        self._register("AHU-1", warranty=False, maintenance=False)
        self._register("CHILLER-1", warranty=False, maintenance=False)
        self._register("SPARE-1", warranty=True, maintenance=True)
        self._assert_asset_items("complete", "incomplete", "incomplete", False)

    def test_category_stays_incomplete_when_required_asset_has_no_warranty_or_schedule(self):
        self._require(["AHU-1"])
        self._register("AHU-1", warranty=False, maintenance=False)
        self._assert_asset_items("complete", "incomplete", "incomplete", False)

    def test_bim_partial_warranty_and_schedule_do_not_complete_imported_inventory(self):
        self.registry.import_from_bim(
            [
                {
                    "tag": "T-AHU",
                    "name": "Air handling unit",
                    "type": "ahu",
                    "category": "equipment",
                    "manufacturer": "Acme",
                    "model": "AHU-100",
                    "serial": "SN-AHU",
                },
                {
                    "tag": "T-CHILLER",
                    "name": "Chiller",
                    "type": "chiller",
                    "category": "equipment",
                    "manufacturer": "Acme",
                    "model": "CH-50",
                    "serial": "SN-CH",
                },
            ]
        )
        by_tag = {asset.asset_tag: asset.asset_id for asset in self.registry.assets.values()}
        self._require([by_tag["T-AHU"], by_tag["T-CHILLER"]])
        self.registry.add_warranty(by_tag["T-AHU"], date(2026, 1, 15), 2, "Acme Warranty")
        self.registry.add_maintenance_schedule(
            by_tag["T-AHU"],
            self.MaintenanceSchedule(
                schedule_id="MS-AHU",
                task_description="Filter change",
                frequency="quarterly",
                next_due=date(2026, 6, 1),
                responsible_party="Facilities",
                estimated_duration_hours=1.5,
            ),
        )
        self.assertNotEqual(by_tag["T-AHU"], by_tag["T-CHILLER"])
        self.assertNotIn(by_tag["T-AHU"], ("T-AHU", "T-CHILLER"))
        self._assert_asset_items("complete", "incomplete", "incomplete", False)

    def test_every_required_asset_with_warranty_and_schedule_completes_the_category(self):
        self._require(["AHU-1", "CHILLER-1"])
        self._register("AHU-1", warranty=True, maintenance=True, tag="TAG-AHU-1")
        self._register("CHILLER-1", warranty=True, maintenance=True, tag="TAG-CHILLER-1")
        self._assert_asset_items("complete", "complete", "complete", True)

    def test_single_listed_asset_fully_recorded_completes_the_category(self):
        self._require(["AHU-1"])
        self._register("AHU-1", warranty=True, maintenance=True, tag="TAG-AHU-1")
        self._assert_asset_items("complete", "complete", "complete", True)

    def test_extra_asset_missing_records_does_not_undo_a_complete_required_set(self):
        self._require(["AHU-1"])
        self._register("AHU-1", warranty=True, maintenance=True, tag="TAG-AHU-1")
        self._register("SPARE-1", warranty=False, maintenance=False, tag="TAG-SPARE-1")
        self._assert_asset_items("complete", "complete", "complete", True)

    def test_past_warranty_end_counts_as_warranty_entered(self):
        self._require(["AHU-1"])
        self._register("AHU-1", warranty=False, maintenance=True, tag="TAG-AHU-1")
        self.registry.add_warranty("AHU-1", date(2020, 1, 1), 1, "Former provider")
        recorded = self.registry.assets["AHU-1"]
        self.assertIsNotNone(recorded.warranty_end)
        self.assertLess(recorded.warranty_end, date.today())
        self._assert_asset_items("complete", "complete", "complete", True)

    def test_imported_asset_completes_only_when_its_id_is_the_required_list(self):
        self.registry.import_from_bim(
            [
                {
                    "tag": "T-AHU",
                    "name": "Air handling unit",
                    "type": "ahu",
                    "category": "equipment",
                    "manufacturer": "Acme",
                    "model": "AHU-100",
                    "serial": "SN-AHU",
                }
            ]
        )
        imported = next(iter(self.registry.assets.values()))
        self.assertNotEqual(imported.asset_id, imported.asset_tag)
        self._require([imported.asset_id])
        self.registry.add_warranty(imported.asset_id, date(2026, 1, 15), 2, "Acme Warranty")
        self.registry.add_maintenance_schedule(
            imported.asset_id,
            self.MaintenanceSchedule(
                schedule_id="MS-IMP",
                task_description="Filter change",
                frequency="quarterly",
                next_due=date(2026, 6, 1),
                responsible_party="Facilities",
                estimated_duration_hours=1.5,
            ),
        )
        self._assert_asset_items("complete", "complete", "complete", True)


if __name__ == "__main__":
    unittest.main()
