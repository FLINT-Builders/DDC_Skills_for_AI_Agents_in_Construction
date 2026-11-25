"""Spec for QTO spreadsheet formula injection.

A BIM CSV category (or other group key) that starts with "=" must still be
exported by the qto-report skill, and opening that workbook or CSV must not
evaluate it. These tests fail while the skill writes the key through as a
formula.

The skill text at 2_DDC_Book/3.2-QTO-Auto-Estimates/qto-report/SKILL.md is the
code under test. Each test executes that source.

Not covered, because the skill never writes their frames to a spreadsheet:
qto_with_waste, compare_qto, and apply_excel_rules. The JSON file written by
export_qto_report is not a spreadsheet and is not checked. Other skills in the
repository that call to_excel are outside this finding.
"""

from __future__ import annotations

import ast
import csv
import io
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from openpyxl import load_workbook

REPO = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO
    / "2_DDC_Book"
    / "3.2-QTO-Auto-Estimates"
    / "qto-report"
    / "SKILL.md"
)

# 100+23 evaluates to 123. None of the fixture quantities are 123.
FORMULA = "=100+23"
FORMULA_RESULT = "123"
# Reads the volume cell when this label is written in A2.
VOLUME_FORMULA = "=B2"
SENTINEL_VOLUME = 4242.5


def python_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    collecting = False
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("```"):
            if collecting:
                blocks.append("\n".join(buf))
                collecting = False
                buf = []
            elif line.startswith("```python"):
                collecting = True
            continue
        if collecting:
            buf.append(line)
    return blocks


def _exec_nodes(nodes: list[ast.stmt], namespace: dict, filename: str) -> None:
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, filename, "exec"), namespace)


def load_skill() -> tuple[dict, list[str]]:
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = python_blocks(text)
    namespace: dict = {}
    for block in blocks:
        try:
            tree = ast.parse(block)
        except SyntaxError:
            continue
        definitions = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef))
        ]
        if not definitions:
            continue
        try:
            _exec_nodes(definitions, namespace, str(SKILL_PATH))
        except ImportError:
            definitions = [
                node
                for node in definitions
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))
            ]
            _exec_nodes(definitions, namespace, str(SKILL_PATH))
    return namespace, blocks


def block_with(blocks: list[str], needle: str) -> str:
    matches = [block for block in blocks if needle in block]
    assert len(matches) == 1, needle
    return matches[0]


def usage_source(block: str) -> str:
    tree = ast.parse(block)
    segments = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
            continue
        segment = ast.get_source_segment(block, node)
        if segment:
            segments.append(segment)
    return "\n".join(segments)


def run_block(block: str, cwd: Path, namespace: dict) -> None:
    merged = dict(namespace)
    previous = Path.cwd()
    os.chdir(cwd)
    try:
        exec(compile(block, str(SKILL_PATH), "exec"), merged)
    finally:
        os.chdir(previous)
    namespace.clear()
    namespace.update(merged)


def formula_cells(path: Path) -> list[str]:
    workbook = load_workbook(path)
    found = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    found.append(f"{sheet.title}!{cell.coordinate}={cell.value!r}")
    return found


def worksheet_formula_bodies(path: Path) -> list[str]:
    bodies = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.startswith("xl/worksheets/sheet"):
                continue
            xml = archive.read(name).decode("utf-8")
            start = 0
            while True:
                open_at = xml.find("<f", start)
                if open_at < 0:
                    break
                content_at = xml.find(">", open_at)
                close_at = xml.find("</f>", content_at)
                if content_at < 0 or close_at < 0:
                    break
                bodies.append(xml[content_at + 1 : close_at])
                start = close_at + 4
    return bodies


def assert_workbook_is_text(path: Path, volume: float) -> None:
    formulas = formula_cells(path)
    assert formulas == [], f"{path.name} stored formula cells: {formulas}"
    bodies = worksheet_formula_bodies(path)
    assert bodies == [], f"{path.name} worksheet XML contains formulas: {bodies}"

    workbook = load_workbook(path)
    numbers = []
    texts = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, (int, float)):
                    numbers.append(float(cell.value))
                elif isinstance(cell.value, str):
                    texts.append(cell.value)
    assert any(abs(number - volume) < 1e-6 for number in numbers), numbers
    assert "Wall" in texts


def csv_fields(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [field for row in csv.reader(handle) for field in row]


def assert_csv_fields_are_text(path: Path) -> None:
    for field in csv_fields(path):
        stripped = field.lstrip(" \t")
        if stripped.startswith("'"):
            stripped = stripped[1:]
        assert not stripped.startswith("="), f"{path.name} has formula field {field!r}"


def evaluate_with_calc(src: Path) -> str:
    outdir = src.parent / f"{src.stem}-calc"
    outdir.mkdir(exist_ok=True)
    profile = (outdir / "profile").resolve()
    completed = subprocess.run(
        [
            "soffice",
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to",
            "csv",
            "--outdir",
            str(outdir),
            str(src.resolve()),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return (outdir / f"{src.stem}.csv").read_text(encoding="utf-8")


def assert_calc_does_not_compute(csv_text: str, result: str = FORMULA_RESULT) -> None:
    fields = [field.strip() for row in csv.reader(io.StringIO(csv_text)) for field in row]
    assert result not in fields, fields


def write_bim_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def four_categories(formula: str) -> list[dict]:
    # The quick-start snippet assigns four unit prices, so the export has four groups.
    categories = [formula, "Wall", "Floor", "Beam"]
    volumes = [SENTINEL_VOLUME, 10, 20, 30]
    rows = []
    for index, (category, volume) in enumerate(zip(categories, volumes), start=1):
        rows.append(
            {
                "Category": category,
                "Volume": volume,
                "Area": index + 4,
                "ElementId": index,
            }
        )
    return rows


@pytest.fixture(scope="module")
def skill_source():
    namespace, blocks = load_skill()
    assert "export_qto_report" in namespace
    assert "generate_qto" in namespace
    return namespace, blocks


@pytest.fixture
def skill(skill_source):
    namespace, blocks = skill_source
    return dict(namespace), blocks


def test_quick_start_does_not_store_or_evaluate_category_formula(skill, tmp_path):
    namespace, blocks = skill
    write_bim_csv(tmp_path / "revit_export.csv", four_categories(FORMULA))
    quick_start = block_with(blocks, 'qto.to_excel("qto_report.xlsx")')
    run_block(quick_start, tmp_path, namespace)

    workbook = tmp_path / "qto_report.xlsx"
    assert_workbook_is_text(workbook, SENTINEL_VOLUME)
    assert_calc_does_not_compute(evaluate_with_calc(workbook))


def test_quick_start_category_formula_cannot_read_takeoff_volume(skill, tmp_path):
    namespace, blocks = skill
    rows = four_categories(VOLUME_FORMULA)
    write_bim_csv(tmp_path / "revit_export.csv", rows)
    import pandas as pd

    grouped = pd.read_csv(tmp_path / "revit_export.csv").groupby("Category").size()
    assert grouped.index[0] == VOLUME_FORMULA

    quick_start = block_with(blocks, 'qto.to_excel("qto_report.xlsx")')
    run_block(quick_start, tmp_path, namespace)
    workbook = tmp_path / "qto_report.xlsx"
    assert_workbook_is_text(workbook, SENTINEL_VOLUME)

    evaluated = list(csv.reader(io.StringIO(evaluate_with_calc(workbook))))
    sentinel = [
        row
        for row in evaluated
        if len(row) > 1 and _as_float(row[1]) == SENTINEL_VOLUME
    ]
    assert sentinel, evaluated
    category_value = _as_float(sentinel[0][0])
    volume_value = _as_float(sentinel[0][1])
    assert category_value != volume_value


def test_revit_loader_and_generate_qto_do_not_export_formulas(skill, tmp_path):
    namespace, blocks = skill
    write_bim_csv(tmp_path / "revit_schedule.csv", four_categories(FORMULA))
    loader = usage_source(block_with(blocks, "def process_revit_export"))
    run_block(loader, tmp_path, namespace)
    to_excel = "qto.to_excel(\"qto_report.xlsx\")"
    quick_start = block_with(blocks, to_excel)
    assert to_excel in quick_start
    run_block(to_excel, tmp_path, namespace)

    # process_revit_export keeps the Volume column and adds Volume_m3 beside it.
    workbook = tmp_path / "qto_report.xlsx"
    assert_workbook_is_text(workbook, SENTINEL_VOLUME)
    assert_calc_does_not_compute(evaluate_with_calc(workbook))


def test_export_qto_report_workbook_and_csv_are_not_formulas(skill, tmp_path):
    namespace, blocks = skill
    import pandas as pd

    frame = pd.DataFrame(four_categories(FORMULA))
    namespace = dict(namespace)
    namespace["qto"] = namespace["generate_qto"](frame)
    usage = usage_source(block_with(blocks, "def export_qto_report"))
    run_block(usage, tmp_path, namespace)

    workbook = tmp_path / "project_qto.xlsx"
    assert_workbook_is_text(workbook, SENTINEL_VOLUME)
    # Summary is the first sheet, which is the sheet Calc converts.
    assert_calc_does_not_compute(evaluate_with_calc(workbook))

    csv_path = tmp_path / "project_qto.csv"
    assert_csv_fields_are_text(csv_path)
    assert_calc_does_not_compute(evaluate_with_calc(csv_path))


def test_multi_level_group_keys_are_not_formulas(skill, tmp_path):
    namespace, blocks = skill
    import pandas as pd

    namespace = dict(namespace)
    namespace["df"] = pd.DataFrame(
        {
            "Level": [FORMULA, "L1"],
            "Category": ["Wall", "Floor"],
            "Material": ["Concrete", FORMULA],
            "Volume": [10.0, 20.0],
            "Area": [5.0, 6.0],
        }
    )
    usage = usage_source(block_with(blocks, "def generate_multi_level_qto"))
    run_block(usage, tmp_path, namespace)

    workbook = tmp_path / "qto_multi_level.xlsx"
    assert_workbook_is_text(workbook, 10.0)
    assert_calc_does_not_compute(evaluate_with_calc(workbook))


def test_pivot_category_header_is_not_a_formula(skill, tmp_path):
    namespace, blocks = skill
    import pandas as pd

    namespace = dict(namespace)
    namespace["df"] = pd.DataFrame(
        {
            "Level": ["L1", "L1"],
            "Category": [FORMULA, "Wall"],
            "Volume": [10.0, 20.0],
        }
    )
    usage = usage_source(block_with(blocks, "def generate_qto_pivot"))
    run_block(usage, tmp_path, namespace)

    workbook = tmp_path / "qto_pivot.xlsx"
    assert formula_cells(workbook) == []
    assert worksheet_formula_bodies(workbook) == []
    assert_calc_does_not_compute(evaluate_with_calc(workbook))
    texts = []
    for sheet in load_workbook(workbook).worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value == "Wall":
                    texts.append(cell.value)
    assert texts


def test_cost_estimate_category_column_is_not_a_formula(skill, tmp_path):
    namespace, blocks = skill
    import pandas as pd

    grouped = namespace["generate_qto"](pd.DataFrame(four_categories(FORMULA)))
    namespace = dict(namespace)
    namespace["qto"] = grouped
    usage = usage_source(block_with(blocks, "def calculate_cost_from_qto"))
    run_block(usage, tmp_path, namespace)

    workbook = tmp_path / "cost_estimate.xlsx"
    assert formula_cells(workbook) == []
    assert worksheet_formula_bodies(workbook) == []
    assert_calc_does_not_compute(evaluate_with_calc(workbook))


def test_material_breakdown_group_keys_are_not_formulas(skill, tmp_path):
    namespace, blocks = skill
    import pandas as pd

    namespace = dict(namespace)
    namespace["df"] = pd.DataFrame(
        {
            "Category": [FORMULA, "Wall"],
            "Material": ["Concrete", FORMULA],
            "Type": ["Pad", "Pad"],
            "Volume": [10.0, 20.0],
            "Area": [5.0, 6.0],
            "ElementId": [1, 2],
        }
    )
    usage = usage_source(block_with(blocks, "def material_breakdown_qto"))
    run_block(usage, tmp_path, namespace)

    workbook = tmp_path / "material_breakdown.xlsx"
    assert_workbook_is_text(workbook, 10.0)
    assert_calc_does_not_compute(evaluate_with_calc(workbook))


def _as_float(value: str):
    try:
        return float(value)
    except ValueError:
        return None
