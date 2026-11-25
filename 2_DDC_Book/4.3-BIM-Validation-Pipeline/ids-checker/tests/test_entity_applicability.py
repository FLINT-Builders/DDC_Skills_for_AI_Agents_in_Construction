"""IDS checker applicability: concrete IFC classes must be checked.

Loads the implementation embedded in SKILL.md and runs the documented
check_model / check_element path. Entity filters such as IfcElement and
IfcTypeObject are IFC supertypes. A concrete class matches a filter when
it is that class or a subtype of it, not when the filter text happens to
occur inside the class name.

A specification run that applies no requirement to any element is not
COMPLIANT. These tests fail while substring matching skips those
requirements and an empty result set is reported as a pass.
"""

import unittest
from pathlib import Path


SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"

# Subtypes of IfcBuiltElement, which is a subtype of IfcElement.
# buildingSMART IFC4.3 RC1 reference schema, ENTITY IfcBuiltElement.
IFC_ELEMENT_SUBTYPES = (
    "IfcBeam",
    "IfcBuildingElementProxy",
    "IfcChimney",
    "IfcColumn",
    "IfcCovering",
    "IfcCurtainWall",
    "IfcDoor",
    "IfcFooting",
    "IfcMember",
    "IfcPlate",
    "IfcRailing",
    "IfcRamp",
    "IfcRoof",
    "IfcShadingDevice",
    "IfcSlab",
    "IfcStair",
    "IfcWall",
    "IfcWindow",
    "IfcElementAssembly",
    "IfcBeamStandardCase",
    "IfcColumnStandardCase",
    "IfcDoorStandardCase",
    "IfcSlabStandardCase",
    "IfcWallStandardCase",
    "IfcWindowStandardCase",
)

# Subtypes of IfcTypeObject, and not subtypes of IfcElement.
# IfcDoorType / IfcWallType / IfcWindowType / IfcSlabType -> IfcBuiltElementType
# -> IfcElementType -> IfcTypeProduct -> IfcTypeObject.
IFC_TYPE_OBJECT_SUBTYPES = (
    "IfcDoorType",
    "IfcWallType",
    "IfcWindowType",
    "IfcSlabType",
    "IfcElementType",
    "IfcTypeProduct",
)

NOT_IFC_TYPE_OBJECT = IFC_ELEMENT_SUBTYPES + (
    "IfcSpace",
    "IfcBuildingStorey",
)


def load_skill_namespace():
    """Execute the IDSChecker implementation from SKILL.md."""
    text = SKILL_MD.read_text(encoding="utf-8")
    lines = text.splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        if lines[index].startswith("```python"):
            index += 1
            body = []
            while index < len(lines) and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            blocks.append("\n".join(body))
        index += 1
    implementation = next(
        (block for block in blocks if "class IDSChecker" in block),
        None,
    )
    if implementation is None:
        raise AssertionError(f"IDSChecker implementation not found in {SKILL_MD}")
    namespace = {"__name__": "ids_checker_skill"}
    exec(compile(implementation, str(SKILL_MD), "exec"), namespace)
    return namespace


class EntityApplicabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = load_skill_namespace()
        cls.IDSChecker = ns["IDSChecker"]
        cls.CheckResult = ns["CheckResult"]

    def setUp(self):
        self.checker = self.IDSChecker("Project BIM Validation")

    def assert_matches(self, element_type, entity_filter):
        matched = self.checker._matches_applicability(
            {"id": "x", "type": element_type},
            {"entity": entity_filter},
        )
        self.assertTrue(
            matched,
            f"{element_type} must match applicability entity {entity_filter}",
        )

    def assert_does_not_match(self, element_type, entity_filter):
        matched = self.checker._matches_applicability(
            {"id": "x", "type": element_type},
            {"entity": entity_filter},
        )
        self.assertFalse(
            matched,
            f"{element_type} must not match applicability entity {entity_filter}",
        )

    def failures_by(self, element_id=None, requirement_id=None):
        found = []
        for item in self.checker.get_failed_checks():
            if element_id is not None and item["element_id"] != element_id:
                continue
            if requirement_id is not None and item["requirement"] != requirement_id:
                continue
            found.append(item)
        return found

    def results_for(self, element_id, requirement_id):
        return [
            result
            for result in self.checker.results
            if result.element_id == element_id
            and result.requirement_id == requirement_id
        ]

    def test_applicability_follows_ifc_class_hierarchy(self):
        for element_type in IFC_ELEMENT_SUBTYPES:
            with self.subTest(element_type=element_type, entity="IfcElement"):
                self.assert_matches(element_type, "IfcElement")
        for element_type in IFC_TYPE_OBJECT_SUBTYPES:
            with self.subTest(element_type=element_type, entity="IfcTypeObject"):
                self.assert_matches(element_type, "IfcTypeObject")
            with self.subTest(element_type=element_type, entity="IfcElement"):
                self.assert_does_not_match(element_type, "IfcElement")
        for element_type in ("IfcSpace", "IfcBuildingStorey"):
            with self.subTest(element_type=element_type, entity="IfcElement"):
                self.assert_does_not_match(element_type, "IfcElement")
        for element_type in NOT_IFC_TYPE_OBJECT:
            with self.subTest(element_type=element_type, entity="IfcTypeObject"):
                self.assert_does_not_match(element_type, "IfcTypeObject")

        self.assert_matches("IfcWall", "IfcWall")
        self.assert_matches("IfcWallStandardCase", "IfcWall")
        self.assert_matches("IfcDoorStandardCase", "IfcDoor")
        self.assert_does_not_match("IfcDoor", "IfcWall")
        self.assert_does_not_match("IfcWall", "IfcDoor")
        self.assert_matches("IfcSpace", "IfcSpace")

    def test_check_element_applies_ifc_element_requirements_to_concrete_wall(self):
        spec_id = self.checker.create_standard_lod_spec(300)
        wall = {"id": "Wall_001", "type": "IfcWall", "properties": {}}

        results = self.checker.check_element(wall, spec_id)

        by_requirement = {result.requirement_id: result for result in results}
        for requirement_id, property_name in (
            ("LOD-300-01", "Pset_ElementCommon.Type"),
            ("LOD-300-02", "BaseQuantities.Length"),
            ("LOD-300-03", "Pset_MaterialCommon.Material"),
        ):
            self.assertIn(requirement_id, by_requirement)
            result = by_requirement[requirement_id]
            self.assertEqual(result.element_id, "Wall_001")
            self.assertEqual(result.element_type, "IfcWall")
            self.assertEqual(result.result, self.CheckResult.FAIL)
            self.assertIn(property_name, result.message)

    def test_lod300_model_missing_properties_is_non_compliant(self):
        spec_id = self.checker.create_standard_lod_spec(300)
        elements = [
            {"id": "Wall_001", "type": "IfcWall", "properties": {}},
            {"id": "Door_001", "type": "IfcDoor", "properties": {}},
            {"id": "Slab_001", "type": "IfcSlab", "properties": {}},
        ]

        report = self.checker.check_model(elements, spec_id)

        self.assertEqual(report["specification"], spec_id)
        self.assertEqual(report["elements_checked"], 3)
        self.assertEqual(report["status"], "NON-COMPLIANT")
        self.assertGreater(report["total_checks"], 0)
        self.assertGreater(report["failed"], 0)
        for element_id in ("Wall_001", "Door_001", "Slab_001"):
            for requirement_id in ("LOD-300-01", "LOD-300-02", "LOD-300-03"):
                self.assertTrue(
                    self.failures_by(element_id, requirement_id),
                    f"{element_id} was not failed for {requirement_id}",
                )

    def test_lod350_model_missing_properties_is_non_compliant(self):
        spec_id = self.checker.create_standard_lod_spec(350)
        elements = [
            {"id": "Wall_001", "type": "IfcWall", "properties": {}},
            {"id": "Door_001", "type": "IfcDoor", "properties": {}},
            {
                "id": "Proxy_001",
                "type": "IfcBuildingElementProxy",
                "properties": {},
                "classifications": {},
            },
        ]

        report = self.checker.check_model(elements, spec_id)

        self.assertGreater(report["total_checks"], 0)
        self.assertGreater(report["failed"], 0)
        self.assertEqual(report["status"], "NON-COMPLIANT")
        for element_id in ("Wall_001", "Door_001", "Proxy_001"):
            for requirement_id, fragment in (
                ("LOD-350-01", "Pset_ElementCommon.Type"),
                ("LOD-350-02", "BaseQuantities.Length"),
                ("LOD-350-03", "Pset_MaterialCommon.Material"),
                ("LOD-350-05", "Uniformat"),
            ):
                failed = self.failures_by(element_id, requirement_id)
                self.assertTrue(failed, f"{element_id} was not failed for {requirement_id}")
                self.assertIn(fragment, failed[0]["message"])
            warnings = [
                result
                for result in self.results_for(element_id, "LOD-350-04")
                if result.result == self.CheckResult.WARNING
            ]
            self.assertTrue(warnings, f"{element_id} was not warned for LOD-350-04")
            self.assertIn("Pset_ElementCommon.FireRating", warnings[0].message)

    def test_lod200_model_missing_type_is_non_compliant(self):
        spec_id = self.checker.create_standard_lod_spec(200)
        report = self.checker.check_model(
            [
                {"id": "Window_001", "type": "IfcWindow", "properties": {}},
                {"id": "Column_001", "type": "IfcColumn", "properties": {}},
            ],
            spec_id,
        )

        self.assertEqual(report["status"], "NON-COMPLIANT")
        self.assertGreater(report["failed"], 0)
        for element_id in ("Window_001", "Column_001"):
            failed = self.failures_by(element_id, "LOD-200-01")
            self.assertTrue(failed)
            self.assertIn("Pset_ElementCommon.Type", failed[0]["message"])

    def test_lod350_complete_door_is_compliant(self):
        spec_id = self.checker.create_standard_lod_spec(350)
        door = {
            "id": "Door_001",
            "type": "IfcDoor",
            "properties": {
                "Pset_ElementCommon": {"Type": "Single", "FireRating": "60"},
                "BaseQuantities": {"Length": 2.1},
                "Pset_MaterialCommon": {"Material": "Timber"},
            },
            "classifications": {"Uniformat": "B2030"},
        }

        report = self.checker.check_model([door], spec_id)

        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["status"], "COMPLIANT")
        self.assertGreater(report["passed"], 0)
        for requirement_id in (
            "LOD-350-01",
            "LOD-350-02",
            "LOD-350-03",
            "LOD-350-04",
            "LOD-350-05",
        ):
            results = self.results_for("Door_001", requirement_id)
            self.assertTrue(results, f"Door_001 was not checked for {requirement_id}")
            self.assertEqual(results[0].result, self.CheckResult.PASS)

    def test_lod350_missing_optional_fire_rating_still_checks_the_door(self):
        spec_id = self.checker.create_standard_lod_spec(350)
        door = {
            "id": "Door_001",
            "type": "IfcDoor",
            "properties": {
                "Pset_ElementCommon": {"Type": "Single"},
                "BaseQuantities": {"Length": 2.1},
                "Pset_MaterialCommon": {"Material": "Timber"},
            },
            "classifications": {"Uniformat": "B2030"},
        }

        report = self.checker.check_model([door], spec_id)

        self.assertEqual(report["failed"], 0)
        self.assertGreater(report["warnings"], 0)
        self.assertEqual(report["status"], "COMPLIANT")
        warnings = self.results_for("Door_001", "LOD-350-04")
        self.assertTrue(warnings)
        self.assertEqual(warnings[0].result, self.CheckResult.WARNING)
        self.assertIn("FireRating", warnings[0].message)

    def test_cobie_concrete_elements_and_types_missing_data_are_non_compliant(self):
        spec_id = self.checker.create_standard_cobie_spec()
        elements = [
            {
                "id": "Space_001",
                "type": "IfcSpace",
                "properties": {
                    "Pset_SpaceCommon": {"Name": "Office 101"},
                    "COBie_Space": {"SpaceNumber": "SP-101", "RoomTag": "A101"},
                },
            },
            {"id": "Door_001", "type": "IfcDoor", "properties": {}},
            {"id": "Wall_001", "type": "IfcWall", "properties": {}},
            {"id": "Slab_001", "type": "IfcSlab", "properties": {}},
            {"id": "DoorType_001", "type": "IfcDoorType", "properties": {}},
            {"id": "WallType_001", "type": "IfcWallType", "properties": {}},
        ]

        report = self.checker.check_model(elements, spec_id)

        self.assertEqual(report["status"], "NON-COMPLIANT")
        self.assertGreater(report["failed"], 0)
        self.assertGreater(report["total_checks"], 0)

        for requirement_id in ("CB-SP-01", "CB-SP-02", "CB-SP-03"):
            space_results = self.results_for("Space_001", requirement_id)
            self.assertTrue(space_results)
            self.assertEqual(space_results[0].result, self.CheckResult.PASS)

        for element_id in ("Door_001", "Wall_001", "Slab_001"):
            for requirement_id, fragment in (
                ("CB-CO-01", "COBie_Component.Name"),
                ("CB-CO-02", "COBie_Component.TypeName"),
            ):
                failed = self.failures_by(element_id, requirement_id)
                self.assertTrue(failed, f"{element_id} was not failed for {requirement_id}")
                self.assertIn(fragment, failed[0]["message"])
            serial = self.results_for(element_id, "CB-CO-03")
            self.assertTrue(serial, f"{element_id} was not checked for CB-CO-03")
            self.assertEqual(serial[0].result, self.CheckResult.WARNING)
            self.assertFalse(
                self.failures_by(element_id, "CB-TY-01"),
                f"{element_id} is not an IfcTypeObject",
            )

        for element_id in ("DoorType_001", "WallType_001"):
            for requirement_id, fragment in (
                ("CB-TY-01", "COBie_Type.Name"),
                ("CB-TY-02", "COBie_Type.Manufacturer"),
                ("CB-TY-03", "COBie_Type.ModelNumber"),
            ):
                failed = self.failures_by(element_id, requirement_id)
                self.assertTrue(failed, f"{element_id} was not failed for {requirement_id}")
                self.assertIn(fragment, failed[0]["message"])
            self.assertFalse(
                self.failures_by(element_id, "CB-CO-01"),
                f"{element_id} is not an IfcElement",
            )

        typed = self.IDSChecker("Type object")
        typed.create_standard_cobie_spec()
        type_report = typed.check_model(
            [
                {
                    "id": "ElemType_001",
                    "type": "IfcElementType",
                    "properties": {
                        "COBie_Type": {
                            "Name": "Generic",
                            "Manufacturer": "Acme",
                            "ModelNumber": "M-1",
                        }
                    },
                }
            ],
            spec_id,
        )
        self.assertEqual(type_report["failed"], 0)
        self.assertGreater(type_report["passed"], 0)
        self.assertEqual(type_report["status"], "COMPLIANT")
        for requirement_id in ("CB-TY-01", "CB-TY-02", "CB-TY-03"):
            type_results = [
                result
                for result in typed.results
                if result.element_id == "ElemType_001"
                and result.requirement_id == requirement_id
            ]
            self.assertTrue(type_results, f"ElemType_001 was not checked for {requirement_id}")
            self.assertEqual(type_results[0].result, self.CheckResult.PASS)
        component_hits = [
            result
            for result in typed.results
            if result.element_id == "ElemType_001" and result.requirement_id == "CB-CO-01"
        ]
        self.assertFalse(component_hits)

    def test_custom_abstract_entity_requirements_apply_to_subtypes(self):
        self.checker.create_specification("CUSTOM", "Custom handover rules")
        self.checker.add_property_requirement(
            "CUSTOM",
            "CU-01",
            "Element mark",
            "IfcElement",
            "Pset_ElementCommon",
            "Tag",
        )
        self.checker.add_classification_requirement(
            "CUSTOM",
            "CU-02",
            "Uniformat",
            "IfcElement",
            "Uniformat",
        )
        self.checker.add_property_requirement(
            "CUSTOM",
            "CU-03",
            "Type manufacturer",
            "IfcTypeObject",
            "COBie_Type",
            "Manufacturer",
        )

        report = self.checker.check_model(
            [
                {"id": "Beam_001", "type": "IfcBeam", "properties": {}, "classifications": {}},
                {"id": "DoorType_001", "type": "IfcDoorType", "properties": {}},
                {
                    "id": "Space_001",
                    "type": "IfcSpace",
                    "properties": {},
                    "classifications": {},
                },
            ],
            "CUSTOM",
        )

        self.assertEqual(report["status"], "NON-COMPLIANT")
        beam_tag = self.failures_by("Beam_001", "CU-01")
        beam_class = self.failures_by("Beam_001", "CU-02")
        self.assertTrue(beam_tag)
        self.assertIn("Pset_ElementCommon.Tag", beam_tag[0]["message"])
        self.assertTrue(beam_class)
        self.assertIn("Uniformat", beam_class[0]["message"])
        self.assertFalse(self.failures_by("Beam_001", "CU-03"))

        manufacturer = self.failures_by("DoorType_001", "CU-03")
        self.assertTrue(manufacturer)
        self.assertIn("COBie_Type.Manufacturer", manufacturer[0]["message"])
        self.assertFalse(self.failures_by("DoorType_001", "CU-01"))
        self.assertFalse(self.failures_by("DoorType_001", "CU-02"))

        self.assertFalse(self.failures_by("Space_001", "CU-01"))
        self.assertFalse(self.failures_by("Space_001", "CU-02"))
        self.assertFalse(self.failures_by("Space_001", "CU-03"))
        self.assertFalse(self.results_for("Space_001", "CU-01"))

    def test_no_applicable_checks_is_not_compliant(self):
        self.checker.create_specification("WIN", "Windows only")
        self.checker.add_property_requirement(
            "WIN",
            "W-01",
            "Window name",
            "IfcWindow",
            "Pset_WindowCommon",
            "Name",
        )
        elements = [
            {"id": "Space_001", "type": "IfcSpace", "properties": {}},
            {"id": "Storey_001", "type": "IfcBuildingStorey", "properties": {}},
        ]

        report = self.checker.check_model(elements, "WIN")

        self.assertEqual(report["elements_checked"], 2)
        self.assertEqual(report["total_checks"], 0)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["passed"], 0)
        self.assertIn(report["status"], {"NON-COMPLIANT", "INCOMPLETE"})
        self.assertNotEqual(report["status"], "COMPLIANT")
        self.assertEqual(self.checker.get_failed_checks(), [])


if __name__ == "__main__":
    unittest.main()
