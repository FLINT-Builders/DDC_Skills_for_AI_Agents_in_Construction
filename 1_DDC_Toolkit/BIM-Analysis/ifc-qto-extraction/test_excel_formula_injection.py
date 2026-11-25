"""Spec for Excel formula injection in the IFC quantity-takeoff skill.

Category, type name, and level are copied out of a converted BIM workbook and
written by ``generate_qto_report`` with ``DataFrame.to_excel``. These tests
fail while that writer stores a leading "=" as a worksheet formula. They pass
once those three fields are written as literal text on every sheet the report
creates (Detail, Summary, and By Level), including a level value produced by
joining several source levels.

Run from the repository root:

    python3 -m pytest 1_DDC_Toolkit/BIM-Analysis/ifc-qto-extraction/test_excel_formula_injection.py -q
"""

from __future__ import annotations

import math
import re
import sys
import types
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from openpyxl import Workbook, load_workbook

SKILL_MD = Path(__file__).with_name("SKILL.md")
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

# Names a model author can put on an element. Each one starts with "=" so a
# spreadsheet that treats the cell as a formula will read other sheets.
CATEGORY = '=SUM(Detail!E:E)'
TYPE_NAME = '=SUM(Summary!D:D)'
LEVEL = '=Detail!E2'
JOINED_LEVEL = '=Detail!E2, Roof'

BENIGN_CATEGORY = 'Walls'
BENIGN_TYPE = 'Generic 200mm'
BENIGN_LEVEL = 'Level 1'


def load_skill():
    """Execute the skill implementation embedded in SKILL.md."""
    text = SKILL_MD.read_text(encoding='utf-8')
    match = re.search(r'```python\n(.*?)```', text, re.DOTALL)
    if match is None:
        raise AssertionError(f'no Python block in {SKILL_MD}')
    # dataclasses looks up the class module in sys.modules while the class body runs.
    module = types.ModuleType('ifc_qto_extraction_skill')
    module.__file__ = str(SKILL_MD)
    sys.modules[module.__name__] = module
    exec(compile(match.group(1), str(SKILL_MD), 'exec'), module.__dict__)
    return module.__dict__


@pytest.fixture(scope='module')
def skill():
    return load_skill()


def write_text_cell(worksheet, row: int, column: int, text: str) -> None:
    """Store ``text`` as an inline string, even when it starts with '='.

    Assigning through ``cell.value`` would make openpyxl record a formula.
    BIM exports keep element names as text, so the fixture does too.
    """
    cell = worksheet.cell(row=row, column=column, value=None)
    cell._value = text
    cell.data_type = 's'


def formula_texts(path: Path) -> list[tuple[str, str]]:
    """Return ``(worksheet part, formula body)`` for every ``<f>`` in ``path``."""
    found = []
    with zipfile.ZipFile(path) as workbook:
        for name in sorted(workbook.namelist()):
            if not name.startswith('xl/worksheets/sheet'):
                continue
            root = ET.fromstring(workbook.read(name))
            for node in root.findall('.//m:f', NS):
                found.append((name, node.text or ''))
    return found


def inline_strings(path: Path) -> list[str]:
    found = []
    with zipfile.ZipFile(path) as workbook:
        for name in sorted(workbook.namelist()):
            if not name.startswith('xl/worksheets/'):
                continue
            root = ET.fromstring(workbook.read(name))
            for node in root.findall('.//m:is/m:t', NS):
                if node.text:
                    found.append(node.text)
    return found


def represents_payload(stored: str, payload: str) -> bool:
    """True when ``stored`` is the payload kept as text, or a defanged form of it."""
    if stored == payload:
        return True
    if stored.lstrip("' \t") == payload:
        return True
    if payload.startswith('=') and stored == payload[1:]:
        return True
    return False


def non_formula_strings(worksheet) -> list[str]:
    values = []
    for row in worksheet.iter_rows():
        for cell in row:
            if cell.data_type == 'f':
                continue
            if isinstance(cell.value, str):
                values.append(cell.value)
    return values


def numeric_values(worksheet) -> list[float]:
    values = []
    for row in worksheet.iter_rows():
        for cell in row:
            if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                values.append(float(cell.value))
    return values


def assert_literal_payload(worksheet, payload: str) -> None:
    matches = [value for value in non_formula_strings(worksheet) if represents_payload(value, payload)]
    assert matches, (
        f'{worksheet.title} does not keep {payload!r} as literal text; '
        f'text cells are {non_formula_strings(worksheet)!r}'
    )


def assert_no_formula_cells(path: Path, workbook) -> None:
    from_reader = [
        (sheet.title, cell.coordinate, cell.value)
        for sheet in workbook.worksheets
        for row in sheet.iter_rows()
        for cell in row
        if cell.data_type == 'f'
    ]
    from_xml = formula_texts(path)
    assert from_reader == [] and from_xml == [], (
        'takeoff workbook contains worksheet formulas: '
        f'reader={from_reader!r} xml={from_xml!r}'
    )


def assert_number(worksheet, expected: float) -> None:
    assert any(math.isclose(value, expected, rel_tol=0, abs_tol=1e-9) for value in numeric_values(worksheet)), (
        f'{worksheet.title} is missing quantity {expected}; saw {numeric_values(worksheet)!r}'
    )


def build_converted_workbook(path: Path) -> None:
    """Workbook shaped like a BIM converter export: names are text cells."""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = 'Elements'
    headers = ['Category', 'Type Name', 'Level', 'Area', 'Volume', 'Length']
    for column, header in enumerate(headers, start=1):
        write_text_cell(worksheet, 1, column, header)

    rows = [
        (BENIGN_CATEGORY, BENIGN_TYPE, BENIGN_LEVEL, 12.5, 3.5, 8),
        (CATEGORY, TYPE_NAME, LEVEL, 1.5, 2.25, 7),
        ('Slabs', 'Slab 200', '=Detail!E2', 5, 1, 2),
        ('Slabs', 'Slab 200', 'Roof', 15, 3, 6),
    ]
    for row_number, row in enumerate(rows, start=2):
        for column, value in enumerate(row, start=1):
            if column <= 3:
                write_text_cell(worksheet, row_number, column, value)
            else:
                worksheet.cell(row=row_number, column=column, value=value)
    workbook.save(path)

    # The skill must be the component that introduces formulas. If the fixture
    # already stored names as formulas, a later read would not prove that.
    assert formula_texts(path) == []
    stored = inline_strings(path)
    for payload in (CATEGORY, TYPE_NAME, LEVEL, '=Detail!E2', 'Roof'):
        assert payload in stored


def test_converted_text_names_stay_literal_through_the_takeoff_report(skill, tmp_path):
    """Attack path: text names in the converted workbook become the QTO report."""
    source = tmp_path / 'building.xlsx'
    output = tmp_path / 'QTO_report.xlsx'
    build_converted_workbook(source)

    extractor = skill['BIMQuantityExtractor']('/tmp/converters')
    frame = extractor.load_bim_data(str(source))
    quantities = extractor.extract_quantities(frame)
    extractor.generate_qto_report(quantities, str(output), project_name='Tower')

    report = load_workbook(output)
    assert_no_formula_cells(output, report)

    detail = report['Detail']
    summary = report['Summary']
    by_level = report['By Level']

    assert_literal_payload(detail, CATEGORY)
    assert_literal_payload(detail, TYPE_NAME)
    assert_literal_payload(detail, LEVEL)
    assert_literal_payload(detail, JOINED_LEVEL)
    assert_literal_payload(detail, BENIGN_CATEGORY)
    assert_literal_payload(detail, BENIGN_TYPE)
    assert_literal_payload(detail, BENIGN_LEVEL)

    # Summary groups by category, so the category is the row label.
    assert_literal_payload(summary, CATEGORY)
    assert_literal_payload(summary, BENIGN_CATEGORY)
    assert_literal_payload(summary, 'Slabs')

    # By Level groups by the (possibly joined) level and the category.
    assert_literal_payload(by_level, LEVEL)
    assert_literal_payload(by_level, JOINED_LEVEL)
    assert_literal_payload(by_level, CATEGORY)
    assert_literal_payload(by_level, BENIGN_LEVEL)

    # Quantities from the hostile rows and the benign row are still in the file.
    assert_number(detail, 2.25)
    assert_number(detail, 3.5)
    assert_number(detail, 4)  # 1 + 3 from the two slab rows joined into one item
    assert_number(summary, 2.25)
    assert_number(summary, 3.5)


def test_generate_qto_report_writes_every_sheet_label_as_text(skill, tmp_path):
    """Same writer, fed QuantityItem values directly, including a joined level."""
    quantity_item = skill['QuantityItem']
    output = tmp_path / 'QTO_direct.xlsx'
    items = [
        quantity_item(
            category=CATEGORY,
            type_name=TYPE_NAME,
            count=2,
            area=1.5,
            volume=2.25,
            length=7,
            level=LEVEL,
        ),
        quantity_item(
            category='Slabs',
            type_name='Slab 200',
            count=2,
            area=20,
            volume=4,
            length=8,
            level=JOINED_LEVEL,
        ),
        quantity_item(
            category=BENIGN_CATEGORY,
            type_name=BENIGN_TYPE,
            count=4,
            area=12.5,
            volume=3.5,
            length=8,
            level=BENIGN_LEVEL,
        ),
    ]

    extractor = skill['BIMQuantityExtractor']('/tmp/converters')
    extractor.generate_qto_report(items, str(output), project_name='Tower')

    report = load_workbook(output)
    assert_no_formula_cells(output, report)
    detail = report['Detail']
    summary = report['Summary']
    by_level = report['By Level']

    for payload in (CATEGORY, TYPE_NAME, LEVEL, JOINED_LEVEL, BENIGN_TYPE, BENIGN_LEVEL):
        assert_literal_payload(detail, payload)
    for payload in (CATEGORY, 'Slabs', BENIGN_CATEGORY):
        assert_literal_payload(summary, payload)
    for payload in (LEVEL, JOINED_LEVEL, CATEGORY, BENIGN_LEVEL, 'Slabs'):
        assert_literal_payload(by_level, payload)

    assert_number(detail, 2.25)
    assert_number(detail, 4)
    assert_number(detail, 3.5)
    assert_number(summary, 2.25)


def test_extract_by_level_items_keep_formula_markers_as_text(skill, tmp_path):
    """extract_by_level groups on Level, then the same report writer emits it."""
    source = tmp_path / 'by_level.xlsx'
    output = tmp_path / 'QTO_by_level.xlsx'
    build_converted_workbook(source)

    extractor = skill['BIMQuantityExtractor']('/tmp/converters')
    frame = extractor.load_bim_data(str(source))
    grouped = extractor.extract_by_level(frame)
    items = [item for group in grouped.values() for item in group]
    extractor.generate_qto_report(items, str(output))

    report = load_workbook(output)
    assert_no_formula_cells(output, report)
    assert_literal_payload(report['Detail'], LEVEL)
    assert_literal_payload(report['Detail'], CATEGORY)
    assert_literal_payload(report['Detail'], TYPE_NAME)
    assert_literal_payload(report['By Level'], LEVEL)
    assert_literal_payload(report['Summary'], CATEGORY)
