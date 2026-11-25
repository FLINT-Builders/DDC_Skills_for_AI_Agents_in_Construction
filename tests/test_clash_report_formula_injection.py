"""Clash-report Excel export must not run formulas planted in a model.

A consultant who can edit the shared IFC controls element Name and GlobalId.
The clash skill copies those strings into the workbook. These tests load that
skill, detect clashes, and export the workbook the skill writes.

They fail while a leading "=" is stored as a spreadsheet formula. They pass
when every such value is written as text (the original string, or that string
with one leading apostrophe) and a spreadsheet does not evaluate it.

Not covered here:
- generate_clash_html_report writes HTML, not a workbook.
- run_all_clash_tests only calls detect_hard_clashes and detect_soft_clashes.
- Names starting with "+", "-", or "@" are stored as text by this exporter
  and are not evaluated; only a leading "=" becomes a formula.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import openpyxl

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "5_DDC_Innovative" / "clash-detection-analysis" / "SKILL.md"

# Cube faces, vertex indices into the 8 corners built below.
_FACES = [
    (0, 1, 2, 3),
    (4, 5, 6, 7),
    (0, 1, 5, 4),
    (1, 2, 6, 5),
    (2, 3, 7, 6),
    (3, 0, 4, 7),
]


def _load_skill():
    """Execute the skill's detector and Excel exporter from SKILL.md."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    namespace = {}
    for block in blocks:
        if "class ClashDetector" in block or "def export_clash_report" in block:
            exec(block, namespace)
    missing = {"ClashDetector", "export_clash_report"} - set(namespace)
    if missing:
        raise AssertionError(f"{SKILL_PATH} is missing {sorted(missing)}")
    return namespace["ClashDetector"], namespace["export_clash_report"]


def _cube_vertices(shift_x: float = 0.0):
    corners = [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (1.0, 1.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
        (1.0, 0.0, 1.0),
        (1.0, 1.0, 1.0),
        (0.0, 1.0, 1.0),
    ]
    return [(x + shift_x, y, z) for x, y, z in corners]


def _write_model(elements, path: Path) -> None:
    """Write an IFC whose products carry the caller-supplied names and ids.

    elements: sequence of (ifc_class, name, global_id, x_offset).
    The two meshes overlap when x_offset is 0 and sit 50 mm apart when the
    second offset is 0.05, which is inside the skill's soft-clash clearance.
    """
    import ifcopenshell.api.context
    import ifcopenshell.api.geometry
    import ifcopenshell.api.project
    import ifcopenshell.api.root
    import ifcopenshell.api.unit

    model = ifcopenshell.api.project.create_file()
    ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="Project")
    ifcopenshell.api.unit.assign_unit(model)
    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=context,
    )
    for ifc_class, name, global_id, shift_x in elements:
        element = ifcopenshell.api.root.create_entity(model, ifc_class=ifc_class, name=name)
        element.GlobalId = global_id
        representation = ifcopenshell.api.geometry.add_mesh_representation(
            model,
            context=body,
            vertices=[_cube_vertices(shift_x)],
            faces=[_FACES],
        )
        ifcopenshell.api.geometry.assign_representation(
            model, product=element, representation=representation
        )
    model.write(str(path))


def _preserved(shown, payload: str) -> bool:
    """True when the cell still shows the model text and was not evaluated."""
    if shown == payload:
        return True
    return isinstance(shown, str) and shown.startswith("'") and shown[1:] == payload


def _recalculate(xlsx_path: Path) -> openpyxl.Workbook:
    """Open the workbook in LibreOffice Calc, which evaluates formula cells."""
    soffice = shutil.which("soffice")
    if soffice is None:
        raise AssertionError("soffice is required to recalculate the clash workbook")
    with tempfile.TemporaryDirectory(prefix="lo-profile-") as profile, tempfile.TemporaryDirectory(
        prefix="lo-out-"
    ) as outdir:
        completed = subprocess.run(
            [
                soffice,
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--nologo",
                f"-env:UserInstallation=file://{profile}",
                "--convert-to",
                "xlsx",
                "--outdir",
                outdir,
                str(xlsx_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr or completed.stdout)
        converted = Path(outdir) / xlsx_path.name
        # Load before the temp directory is removed.
        return openpyxl.load_workbook(converted, data_only=True)


def _clash_sheets(workbook: openpyxl.Workbook):
    sheets = []
    for worksheet in workbook.worksheets:
        headers = [cell.value for cell in worksheet[1]]
        if "Element1_Name" in headers and worksheet.max_row > 1:
            sheets.append(worksheet)
    return sheets


def _assert_payloads_stay_text(xlsx_path: Path, payloads: list[str], benign: list[str]) -> None:
    written = openpyxl.load_workbook(xlsx_path)
    formula_cells = [
        f"{worksheet.title}!{cell.coordinate}={cell.value!r}"
        for worksheet in written.worksheets
        for row in worksheet.iter_rows()
        for cell in row
        if cell.data_type == "f"
    ]
    calculated = _recalculate(xlsx_path)
    clash_sheets = _clash_sheets(calculated)
    problems = []
    if formula_cells:
        problems.append("workbook stored formula cells: " + ", ".join(formula_cells))
    if len(clash_sheets) < 2:
        problems.append(
            "expected the All_Clashes sheet and a severity sheet, found "
            + ", ".join(sheet.title for sheet in clash_sheets)
        )
    for worksheet in clash_sheets:
        values = [cell.value for row in worksheet.iter_rows() for cell in row]
        for payload in payloads:
            if not any(_preserved(value, payload) for value in values):
                problems.append(
                    f"{worksheet.title} evaluated or dropped {payload!r}; cell values include {values}"
                )
        for text in benign:
            if text not in values:
                problems.append(f"{worksheet.title} lost benign value {text!r}; cell values include {values}")
    if problems:
        raise AssertionError("\n".join(problems))


class ClashReportFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep the skill functions off the class so unittest does not bind them.
        global _ClashDetector, _export_clash_report
        _ClashDetector, _export_clash_report = _load_skill()

    def _export(self, elements, detect):
        with tempfile.TemporaryDirectory(prefix="clash-model-") as directory:
            model_path = Path(directory) / "model.ifc"
            report_path = Path(directory) / "clashes.xlsx"
            _write_model(elements, model_path)
            detector = _ClashDetector(str(model_path))
            clashes = detect(detector)
            self.assertGreater(len(clashes), 0, "the planted elements did not reach the clash table")
            _export_clash_report(detector, str(report_path))
            # Copy the workbook out of the deleted temp dir before assertions reopen it.
            persisted = Path(tempfile.mkdtemp(prefix="clash-report-")) / "clashes.xlsx"
            shutil.copy(report_path, persisted)
            return persisted

    def test_hard_clash_names_and_ids_are_not_executed(self):
        # Overlapping wall and column. detect_hard_clashes copies Name and GlobalId.
        payloads = ['=1+1', '=INFO("osversion")', "=4+4", "=5+5"]
        report = self._export(
            [
                ("IfcWall", "=1+1", "=4+4", 0.0),
                ("IfcColumn", '=INFO("osversion")', "=5+5", 0.0),
            ],
            lambda detector: detector.detect_hard_clashes(["IfcWall"], ["IfcColumn"]),
        )
        _assert_payloads_stay_text(
            report,
            payloads,
            benign=["IfcWall", "IfcColumn", "Hard", "Critical"],
        )

    def test_soft_clash_names_and_ids_are_not_executed(self):
        # 50 mm apart: inside the default clearance, not a hard intersection.
        # detect_soft_clashes is a second copy of Name and GlobalId into the table.
        payloads = ["=7+7", "=3+3", "=8+8"]
        report = self._export(
            [
                ("IfcWall", "Wall-A", "=7+7", 0.0),
                ("IfcColumn", "=3+3", "=8+8", 0.05),
            ],
            lambda detector: detector.detect_soft_clashes(["IfcWall"], ["IfcColumn"], clearance=0.1),
        )
        _assert_payloads_stay_text(
            report,
            payloads,
            benign=["Wall-A", "IfcWall", "IfcColumn", "Soft", "Low"],
        )


if __name__ == "__main__":
    unittest.main()
