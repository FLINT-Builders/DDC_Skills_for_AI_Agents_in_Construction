"""Pass-gate tests for the BIM validation pipeline in the bim-validation-pipeline skill.

These tests load the classes from SKILL.md and run them on IFC files. They fail
while a model that misses the data its target LOD requires is still marked
passed, counted as passed in a batch, or omitted from the Excel report.

Not covered here, because those checks are never applied by this pass path:
IDSValidator is not called from BIMValidationPipeline.run, and LODValidator
does not record the connections, exact_size, or fabrication_ready flags.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

import ifcopenshell
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.project
import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.api.unit
import openpyxl

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "2_DDC_Book" / "4.3-BIM-Validation-Pipeline" / "bim-validation-pipeline" / "SKILL.md"

STRUCTURAL_PSETS = {
    "IfcWall": ["Pset_WallCommon", "BaseQuantities"],
    "IfcSlab": ["Pset_SlabCommon", "BaseQuantities"],
    "IfcColumn": ["Pset_ColumnCommon", "BaseQuantities"],
    "IfcBeam": ["Pset_BeamCommon", "BaseQuantities"],
}

LOD300_PROPERTY_ISSUES = (
    "Missing property: Material",
    "Missing property: Type",
)
LOD300_QUANTITY_ISSUES = (
    "Missing quantity: Length",
    "Missing quantity: Area",
    "Missing quantity: Volume",
)


def load_skill_namespace():
    """Execute the skill's implementation blocks in one namespace.

    The quick-start sample opens ``model.ifc`` at import time and is not part
    of the pipeline classes. Later blocks share ``element_util`` and ``pd``.
    """
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.S)
    module = types.ModuleType("bim_validation_pipeline_skill")
    module.__file__ = str(SKILL)
    sys.modules[module.__name__] = module
    for index, block in enumerate(blocks):
        if re.search(r"^ifc_model\s*=\s*ifcopenshell\.open\(", block, flags=re.M):
            continue
        exec(compile(block, f"{SKILL}:block{index}", "exec"), module.__dict__)
    return module.__dict__


def new_ifc():
    model = ifcopenshell.api.project.create_file()
    ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="Deliverable")
    ifcopenshell.api.unit.assign_unit(model)
    model_context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=model_context,
    )
    return model, body


def add_valid_body(model, body, element):
    """Swept solid that ifcopenshell.geom.create_shape accepts."""
    representation = ifcopenshell.api.geometry.add_wall_representation(
        model,
        context=body,
        length=0.9,
        height=2.1,
        thickness=0.05,
    )
    ifcopenshell.api.geometry.assign_representation(
        model, product=element, representation=representation
    )


def add_named_definitions(model, element, names):
    """Create property and quantity sets with no LOD values inside them."""
    for name in names:
        if name == "BaseQuantities":
            ifcopenshell.api.pset.add_qto(model, product=element, name=name)
        else:
            ifcopenshell.api.pset.add_pset(model, product=element, name=name)


def add_lod_values(model, element, *, manufacturer=False, weight=False):
    properties = {
        "Category": element.is_a(),
        "Material": "Concrete",
        "Type": "Standard",
    }
    if manufacturer:
        properties["Manufacturer"] = "Example Fabricator"
    pset = ifcopenshell.api.pset.add_pset(model, product=element, name="Pset_LOD")
    ifcopenshell.api.pset.edit_pset(model, pset=pset, properties=properties)
    quantities = {"Length": 1.0, "Area": 2.0, "Volume": 0.2}
    if weight:
        quantities["Weight"] = 10.0
    qto = ifcopenshell.api.pset.add_qto(model, product=element, name="BaseQuantities")
    ifcopenshell.api.pset.edit_qto(model, qto=qto, properties=quantities)


def write_opening_model(path, *, complete=False):
    """Door, window, and stair. Geometry is present; LOD data is optional."""
    model, body = new_ifc()
    for ifc_class, name in (
        ("IfcDoor", "D-001"),
        ("IfcWindow", "Win-001"),
        ("IfcStair", "Stair-001"),
    ):
        element = ifcopenshell.api.root.create_entity(model, ifc_class=ifc_class, name=name)
        add_valid_body(model, body, element)
        if complete:
            add_lod_values(model, element)
    model.write(path)
    return path


def write_structural_model(path):
    """Structural elements whose property-set names satisfy the general validator."""
    model, body = new_ifc()
    for ifc_class, pset_names in STRUCTURAL_PSETS.items():
        element = ifcopenshell.api.root.create_entity(
            model, ifc_class=ifc_class, name=f"{ifc_class}-001"
        )
        add_valid_body(model, body, element)
        add_named_definitions(model, element, pset_names)
    model.write(path)
    return path


def write_lod350_gap_model(path):
    """LOD300 data present, manufacturer and weight omitted."""
    model, body = new_ifc()
    element = ifcopenshell.api.root.create_entity(model, ifc_class="IfcDoor", name="D-350")
    add_valid_body(model, body, element)
    add_lod_values(model, element, manufacturer=False, weight=False)
    model.write(path)
    return path


def truthy_cell(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


class LodPassGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_skill_namespace()
        cls.Pipeline = cls.ns["BIMValidationPipeline"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def pipeline(self, **config):
        pipeline = self.Pipeline()
        pipeline.config.update(config)
        return pipeline

    def assert_bim_would_pass(self, pipeline, result):
        """The general validator has no error and is inside the warning limit."""
        bim = result["bim_validation"]
        self.assertEqual(bim["errors"], 0, bim["by_rule"])
        self.assertNotIn("GEO-001", bim["by_rule"])
        self.assertLessEqual(bim["warnings"], pipeline.config["warn_threshold"])
        self.assertGreater(result["bim_validation"]["stats"]["elements_with_geometry"], 0)
        self.assertEqual(result["lod_validation"]["target_lod"], pipeline.config["lod_target"])

    def test_default_run_fails_openings_that_miss_lod300_data(self):
        ifc_path = write_opening_model(self.path("openings.ifc"), complete=False)
        pipeline = self.pipeline()
        self.assertEqual(pipeline.config["lod_target"], "LOD300")
        self.assertNotIn("lod_issue_threshold", pipeline.config)

        result = pipeline.run(ifc_path)

        self.assert_bim_would_pass(pipeline, result)
        messages = [issue["issue"] for issue in result["lod_validation"]["issues"]]
        for expected in LOD300_PROPERTY_ISSUES + LOD300_QUANTITY_ISSUES:
            self.assertIn(expected, messages)
        summary = result["lod_validation"]["summary"]
        for ifc_class in ("IfcDoor", "IfcWindow", "IfcStair"):
            self.assertGreater(summary[ifc_class]["total"], 0)
            self.assertGreater(summary[ifc_class]["issues"], 0)
        self.assertGreater(result["lod_validation"]["total_issues"], 0)
        self.assertFalse(result["passed"])

    def test_structural_elements_with_empty_required_psets_fail_lod(self):
        ifc_path = write_structural_model(self.path("structure.ifc"))
        pipeline = self.pipeline()
        result = pipeline.run(ifc_path)

        self.assert_bim_would_pass(pipeline, result)
        self.assertNotIn("PROP-001", result["bim_validation"]["by_rule"])
        messages = [issue["issue"] for issue in result["lod_validation"]["issues"]]
        for expected in LOD300_PROPERTY_ISSUES + LOD300_QUANTITY_ISSUES:
            self.assertIn(expected, messages)
        summary = result["lod_validation"]["summary"]
        for ifc_class in STRUCTURAL_PSETS:
            self.assertGreater(summary[ifc_class]["issues"], 0)
        self.assertFalse(result["passed"])

    def test_lod350_target_fails_when_manufacturer_and_weight_are_missing(self):
        ifc_path = write_lod350_gap_model(self.path("lod350.ifc"))
        pipeline = self.pipeline(lod_target="LOD350")
        result = pipeline.run(ifc_path)

        self.assert_bim_would_pass(pipeline, result)
        messages = [issue["issue"] for issue in result["lod_validation"]["issues"]]
        self.assertIn("Missing property: Manufacturer", messages)
        self.assertIn("Missing quantity: Weight", messages)
        self.assertFalse(result["passed"])

    def test_model_that_meets_lod300_still_passes(self):
        ifc_path = write_opening_model(self.path("complete.ifc"), complete=True)
        pipeline = self.pipeline()
        result = pipeline.run(ifc_path)

        self.assert_bim_would_pass(pipeline, result)
        self.assertEqual(result["lod_validation"]["total_issues"], 0)
        self.assertTrue(result["passed"])

    def test_lod_issue_threshold_is_exclusive_and_defaults_to_zero(self):
        ifc_path = write_opening_model(self.path("openings.ifc"), complete=False)
        pipeline = self.pipeline()
        observed = pipeline.run(ifc_path)
        issue_count = observed["lod_validation"]["total_issues"]
        self.assertGreater(issue_count, 0)
        self.assert_bim_would_pass(pipeline, observed)
        self.assertFalse(observed["passed"])

        pipeline.config["lod_issue_threshold"] = issue_count
        at_threshold = pipeline.run(ifc_path)
        self.assertTrue(at_threshold["passed"])

        pipeline.config["lod_issue_threshold"] = issue_count - 1
        below_threshold = pipeline.run(ifc_path)
        self.assertFalse(below_threshold["passed"])

    def test_evaluate_pass_keeps_geometry_error_and_warning_gates(self):
        pipeline = self.pipeline()
        clean_lod = {"total_issues": 0, "issues": []}
        self.assertFalse(
            pipeline._evaluate_pass({"errors": 1, "warnings": 0}, clean_lod)
        )
        self.assertFalse(
            pipeline._evaluate_pass(
                {"errors": 0, "warnings": pipeline.config["warn_threshold"] + 1},
                clean_lod,
            )
        )
        self.assertTrue(
            pipeline._evaluate_pass(
                {"errors": 0, "warnings": pipeline.config["warn_threshold"]},
                clean_lod,
            )
        )
        self.assertFalse(
            pipeline._evaluate_pass({"errors": 0, "warnings": 0}, {"total_issues": 1, "issues": []})
        )

    def test_run_batch_does_not_count_an_lod_failure_as_passed(self):
        incomplete = write_opening_model(self.path("incomplete.ifc"), complete=False)
        complete = write_opening_model(self.path("complete.ifc"), complete=True)
        pipeline = self.pipeline()

        batch = pipeline.run_batch([incomplete, complete])

        by_file = {item["file"]: item for item in batch["results"]}
        self.assert_bim_would_pass(pipeline, by_file[incomplete])
        self.assertGreater(by_file[incomplete]["lod_validation"]["total_issues"], 0)
        self.assertFalse(by_file[incomplete]["passed"])
        self.assertEqual(by_file[complete]["lod_validation"]["total_issues"], 0)
        self.assertTrue(by_file[complete]["passed"])
        self.assertEqual(batch["total"], 2)
        self.assertEqual(batch["passed"], 1)
        self.assertEqual(batch["failed"], 1)

    def test_export_report_includes_lod_issues_and_a_failed_summary(self):
        ifc_path = write_opening_model(self.path("openings.ifc"), complete=False)
        pipeline = self.pipeline()
        result = pipeline.run(ifc_path)
        self.assertGreater(result["lod_validation"]["total_issues"], 0)
        self.assert_bim_would_pass(pipeline, result)

        output = self.path("report.xlsx")
        self.assertEqual(pipeline.export_report(output), output)

        workbook = openpyxl.load_workbook(output)
        self.assertIn("LOD Issues", workbook.sheetnames)
        sheet_text = []
        for row in workbook["LOD Issues"].iter_rows(values_only=True):
            sheet_text.append(" ".join("" if cell is None else str(cell) for cell in row))
        blob = "\n".join(sheet_text)
        for issue in result["lod_validation"]["issues"]:
            self.assertIn(issue["element"], blob)
            self.assertIn(issue["issue"], blob)

        summary = list(workbook["Summary"].iter_rows(values_only=True))
        passed_row = next(row for row in summary if row and row[0] == "Passed")
        self.assertFalse(truthy_cell(passed_row[1]))


if __name__ == "__main__":
    unittest.main()
