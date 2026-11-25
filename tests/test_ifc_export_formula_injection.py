"""Formula injection in the IFC data-extraction skill's spreadsheet export.

These tests execute the IFCExtractor and IFCExporter code published in
5_DDC_Innovative/ifc-data-extraction/SKILL.md. They fail while element text
from an IFC model is written into Excel or CSV as a spreadsheet formula.

Run:
    python3 -m unittest tests.test_ifc_export_formula_injection
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import ifcopenshell
import ifcopenshell.guid
import openpyxl

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "5_DDC_Innovative" / "ifc-data-extraction" / "SKILL.md"

# Arithmetic only. Each value is the number LibreOffice Calc produces when the
# matching string is stored as a formula. None of them collide with counts,
# volumes, or the storey elevation written by the exporter.
PAYLOADS = {
    "project_name": "=1000+1",
    "project_description": "=1000+3",
    "wall_global_id": "=1000+5",
    "wall_name": "=1000+7",
    "wall_description": "=SUM(1000,9)",
    "wall_object_type": "=1000+11",
    "storey_name": "=1000+13",
    "storey_description": "=1000+15",
    "material_name": "=1000+17",
    "type_name": "=1000+19",
    "space_name": "=1000+21",
    "space_long_name": "=SUM(1000,23)",
}
COMPUTED = {
    "=1000+1": 1001,
    "=1000+3": 1003,
    "=1000+5": 1005,
    "=1000+7": 1007,
    "=SUM(1000,9)": 1009,
    "=1000+11": 1011,
    "=1000+13": 1013,
    "=1000+15": 1015,
    "=1000+17": 1017,
    "=1000+19": 1019,
    "=1000+21": 1021,
    "=SUM(1000,23)": 1023,
}


def load_skill_classes():
    """Execute the extractor and exporter published in the skill document."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.S)
    namespace: dict = {}
    found = []
    for block in blocks:
        if "class IFCExtractor" in block or "class IFCExporter" in block:
            exec(compile(block, str(SKILL_PATH), "exec"), namespace)
            found.append("IFCExtractor" if "class IFCExtractor" in block else "IFCExporter")
    missing = {"IFCExtractor", "IFCExporter"} - set(namespace)
    if missing:
        raise AssertionError(
            f"SKILL.md no longer defines {sorted(missing)}; found blocks {found}"
        )
    return namespace["IFCExtractor"], namespace["IFCExporter"]


def _guid() -> str:
    return ifcopenshell.guid.new()


def build_ifc(path: Path) -> None:
    """Write an IFC model whose text attributes are spreadsheet formulas.

    The strings are the attacker's model content. ifcopenshell writes them
    into the SPF file and the test reloads that file through ifcopenshell.open,
    which is the same reader the skill uses.
    """
    model = ifcopenshell.file(schema="IFC4")
    model.create_entity(
        "IfcProject",
        GlobalId=_guid(),
        Name=PAYLOADS["project_name"],
        Description=PAYLOADS["project_description"],
    )
    storey = model.create_entity(
        "IfcBuildingStorey",
        GlobalId=_guid(),
        Name=PAYLOADS["storey_name"],
        Description=PAYLOADS["storey_description"],
        Elevation=100.0,
    )
    wall = model.create_entity(
        "IfcWall",
        GlobalId=PAYLOADS["wall_global_id"],
        Name=PAYLOADS["wall_name"],
        Description=PAYLOADS["wall_description"],
        ObjectType=PAYLOADS["wall_object_type"],
    )
    space = model.create_entity(
        "IfcSpace",
        GlobalId=_guid(),
        Name=PAYLOADS["space_name"],
        LongName=PAYLOADS["space_long_name"],
    )
    material = model.create_entity("IfcMaterial", Name=PAYLOADS["material_name"])
    wall_type = model.create_entity(
        "IfcWallType",
        GlobalId=_guid(),
        Name=PAYLOADS["type_name"],
        PredefinedType="STANDARD",
    )
    model.create_entity(
        "IfcRelAssociatesMaterial",
        GlobalId=_guid(),
        RelatingMaterial=material,
        RelatedObjects=(wall,),
    )
    model.create_entity(
        "IfcRelContainedInSpatialStructure",
        GlobalId=_guid(),
        RelatingStructure=storey,
        RelatedElements=(wall,),
    )
    model.create_entity(
        "IfcRelDefinesByType",
        GlobalId=_guid(),
        RelatingType=wall_type,
        RelatedObjects=(wall,),
    )
    model.write(str(path))


def _is_computed_payload(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return any(value == result for result in COMPUTED.values())


def calculated_payloads(workbook_path: Path) -> list[tuple[str, str, object]]:
    workbook = openpyxl.load_workbook(workbook_path, data_only=True)
    found = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if _is_computed_payload(cell.value):
                    found.append((sheet.title, cell.coordinate, cell.value))
    return found


def convert_with_calc(sources: list[Path], output_dir: Path) -> None:
    """Open each file in LibreOffice Calc and save the calculated workbook."""
    profile = output_dir / "lo-profile"
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        "soffice",
        "--headless",
        "--norestore",
        "--nolockcheck",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to",
        "xlsx",
        "--outdir",
        str(output_dir),
        *[str(source) for source in sources],
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "LibreOffice Calc failed to open the export:\n"
            f"{completed.stdout}\n{completed.stderr}"
        )


class IFCExportFormulaInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("soffice") is None:
            raise AssertionError(
                "LibreOffice Calc (soffice) is required to open the exported workbooks"
            )
        cls.workdir = Path(tempfile.mkdtemp(prefix="ifc-formula-"))
        cls.ifc_path = cls.workdir / "model.ifc"
        build_ifc(cls.ifc_path)
        # Reload through the skill's reader so a string that did not survive
        # the IFC file cannot be treated as attacker-controlled input.
        reloaded = ifcopenshell.open(str(cls.ifc_path))
        wall = reloaded.by_type("IfcWall")[0]
        if wall.Name != PAYLOADS["wall_name"] or wall.Description != PAYLOADS["wall_description"]:
            raise AssertionError("IFC reader did not preserve formula text on the wall")

        extractor_cls, exporter_cls = load_skill_classes()
        extractor = extractor_cls(str(cls.ifc_path))
        exporter = exporter_cls(extractor)
        cls.xlsx_path = cls.workdir / "model.xlsx"
        cls.csv_dir = cls.workdir / "csv"
        exporter.to_excel(str(cls.xlsx_path))
        exporter.to_csv(str(cls.csv_dir))

        cls.calc_dir = cls.workdir / "calc"
        cls.calc_dir.mkdir()
        csv_files = sorted(cls.csv_dir.glob("*.csv"))
        if not csv_files:
            raise AssertionError("to_csv wrote no CSV files")
        convert_with_calc([cls.xlsx_path, *csv_files], cls.calc_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def test_excel_export_does_not_store_model_text_as_formulas(self):
        workbook = openpyxl.load_workbook(self.xlsx_path)
        formula_cells = []
        sheets_with_rows = set()
        for sheet in workbook.worksheets:
            if sheet.max_row >= 2:
                sheets_with_rows.add(sheet.title)
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.data_type == "f":
                        formula_cells.append(f"{sheet.title}!{cell.coordinate}={cell.value!r}")
        self.assertTrue(
            {"Project", "Elements", "Quantities", "Levels", "Spaces", "Materials"}
            <= sheets_with_rows,
            f"export did not write every sheet, got {sorted(sheets_with_rows)}",
        )
        elements = workbook["Elements"]
        headers = [cell.value for cell in elements[1]]
        type_column = headers.index("IFC_Type") + 1
        exported_types = [
            elements.cell(row, type_column).value
            for row in range(2, elements.max_row + 1)
        ]
        self.assertIn("IfcWall", exported_types)
        self.assertEqual(
            formula_cells,
            [],
            "model text was stored as an Excel formula",
        )

    def test_excel_workbook_does_not_evaluate_model_text(self):
        calculated = calculated_payloads(self.calc_dir / "model.xlsx")
        self.assertEqual(
            calculated,
            [],
            "LibreOffice Calc evaluated model text as a formula in the xlsx export",
        )

    def test_csv_files_do_not_evaluate_model_text(self):
        calculated = []
        for csv_path in sorted(self.csv_dir.glob("*.csv")):
            workbook_path = self.calc_dir / (csv_path.stem + ".xlsx")
            self.assertTrue(
                workbook_path.exists(),
                f"LibreOffice did not open {csv_path.name}",
            )
            for hit in calculated_payloads(workbook_path):
                calculated.append((csv_path.name, *hit))
        self.assertEqual(
            calculated,
            [],
            "LibreOffice Calc evaluated model text as a formula in a CSV export",
        )


if __name__ == "__main__":
    unittest.main()
