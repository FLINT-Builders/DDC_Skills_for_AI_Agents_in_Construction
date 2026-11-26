"""Failing tests for code injection in the excel-to-rvt Dynamo script generator.

DynamoScriptGenerator writes a Python file that an operator runs in Dynamo.
The spreadsheet path, schedule name, and Revit category are pasted into that
source. These tests load the class from the skill document, generate a script
with attacker-controlled strings, and execute that script.

Host APIs that exist only inside Revit (clr, RevitServices, Autodesk.Revit.DB,
Microsoft.Office.Interop.Excel) are stubbed just far enough for the imports and
attribute lookups in the generated file to succeed. Every generated statement
still runs. A stub records the path passed to Workbooks.Open and the name
assigned to the schedule.

The injection tests fail while a caller string can break out of its quotes or
identifier and run as Python. They pass when that string is only data: the
generated script runs, the side effect does not happen, and Workbooks.Open or
the schedule name receives the original string. A category that is not a single
identifier may instead be rejected, but only if rejection leaves no script
behind.
"""

from __future__ import annotations

import builtins
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "1_DDC_Toolkit" / "CAD-Converters" / "excel-to-rvt" / "SKILL.md"


def load_generator():
    """Execute the skill's implementation block and return DynamoScriptGenerator."""
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    try:
        code = next(block for block in blocks if "class DynamoScriptGenerator" in block)
    except StopIteration as exc:
        raise AssertionError(f"DynamoScriptGenerator not found in {SKILL}") from exc

    saved_pandas = sys.modules.get("pandas")
    if saved_pandas is None:
        # The implementation block imports pandas. DynamoScriptGenerator does
        # not use it; the stub only lets that import succeed.
        sys.modules["pandas"] = types.ModuleType("pandas")
    module = types.ModuleType("excel_to_rvt_skill")
    module.__file__ = str(SKILL)
    sys.modules["excel_to_rvt_skill"] = module
    exec(compile(code, str(SKILL), "exec"), module.__dict__)
    return module.DynamoScriptGenerator


class _Cell:
    def __init__(self, value):
        self.Value2 = value


class _Cells:
    def __getitem__(self, key):
        return _Cell(None)


class _Worksheet:
    def __init__(self):
        self.UsedRange = types.SimpleNamespace(
            Rows=types.SimpleNamespace(Count=1),
            Columns=types.SimpleNamespace(Count=1),
        )
        self.Cells = _Cells()


class _Worksheets:
    def __getitem__(self, index):
        return _Worksheet()


class _Workbook:
    def __init__(self):
        self.Worksheets = _Worksheets()

    def Close(self, save):
        return None


class _Workbooks:
    def __init__(self, recorder):
        self._recorder = recorder

    def Open(self, path):
        self._recorder["opened"].append(path)
        return _Workbook()


class _ExcelApplication:
    def __init__(self, recorder):
        self.Visible = False
        self.Workbooks = _Workbooks(recorder)

    def Quit(self):
        return None


class _ScheduleDefinition:
    def GetSchedulableFields(self):
        return []

    def AddField(self, field):
        return None


class _Schedule:
    def __init__(self, recorder):
        self._recorder = recorder
        self.Definition = _ScheduleDefinition()

    def __setattr__(self, name, value):
        if name == "Name":
            self._recorder["schedule_names"].append(value)
        object.__setattr__(self, name, value)


def install_revit_host(recorder):
    """Install stubs for the Revit and Excel names the generated scripts import.

    The names and attribute chain match the scripts DynamoScriptGenerator writes
    in the excel-to-rvt skill. The stubs do not rewrite generated statements.
    """
    saved = {}

    def add(name, module):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module

    clr = types.ModuleType("clr")
    clr.AddReference = lambda *args, **kwargs: None
    add("clr", clr)

    class _Document:
        def GetElement(self, element_id):
            return None

    class DocumentManager:
        class Instance:
            CurrentDBDocument = _Document()

    class TransactionManager:
        class Instance:
            @staticmethod
            def EnsureInTransaction(doc):
                return None

            @staticmethod
            def TransactionTaskDone():
                return None

    persistence = types.ModuleType("RevitServices.Persistence")
    persistence.DocumentManager = DocumentManager
    transactions = types.ModuleType("RevitServices.Transactions")
    transactions.TransactionManager = TransactionManager
    add("RevitServices", types.ModuleType("RevitServices"))
    add("RevitServices.Persistence", persistence)
    add("RevitServices.Transactions", transactions)

    class _BuiltInCategory:
        def __getattr__(self, name):
            # The generated source looks up BuiltInCategory.OST_<category>.
            # Record that attribute name. Do not invent a second OST_ prefix.
            recorder["builtins"].append(name)
            return name

    class _Category:
        @staticmethod
        def GetCategory(doc, built_in):
            recorder["category_args"].append(built_in)
            return types.SimpleNamespace(Id=f"id:{built_in}")

    class _ViewSchedule:
        @staticmethod
        def CreateSchedule(doc, category_id):
            recorder["created_schedules"].append(category_id)
            return _Schedule(recorder)

    database = types.ModuleType("Autodesk.Revit.DB")
    database.BuiltInCategory = _BuiltInCategory()
    database.Category = _Category
    database.ViewSchedule = _ViewSchedule
    database.ElementId = lambda value: value
    database.StorageType = types.SimpleNamespace(Double="double", Integer="integer", String="string")
    database.__all__ = [
        "BuiltInCategory",
        "Category",
        "ViewSchedule",
        "ElementId",
        "StorageType",
    ]
    add("Autodesk", types.ModuleType("Autodesk"))
    add("Autodesk.Revit", types.ModuleType("Autodesk.Revit"))
    add("Autodesk.Revit.DB", database)

    excel = types.ModuleType("Microsoft.Office.Interop.Excel")

    def application_class():
        return _ExcelApplication(recorder)

    excel.ApplicationClass = application_class
    add("Microsoft", types.ModuleType("Microsoft"))
    add("Microsoft.Office", types.ModuleType("Microsoft.Office"))
    add("Microsoft.Office.Interop", types.ModuleType("Microsoft.Office.Interop"))
    add("Microsoft.Office.Interop.Excel", excel)
    return saved


def restore_modules(saved):
    for name, previous in saved.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def fresh_recorder():
    return {
        "opened": [],
        "schedule_names": [],
        "builtins": [],
        "category_args": [],
        "created_schedules": [],
        "error": None,
    }


class DynamoInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator_type = load_generator()

    def setUp(self):
        self.generator = self.generator_type()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def marker(self, name):
        return self.root / name

    def marker_statement(self, marker, label):
        """Python statement that creates marker and closes the file."""
        path = repr(str(marker))
        text = repr(label)
        return f"(lambda _f: (_f.write({text}), _f.close()))(open({path}, 'w'))"

    def run_generated(self, script_path):
        recorder = fresh_recorder()
        saved = install_revit_host(recorder)
        namespace = {"__name__": "__dynamo_script__", "__builtins__": builtins}
        try:
            source = script_path.read_text(encoding="utf-8")
            try:
                exec(compile(source, str(script_path), "exec"), namespace)
            except SystemExit as exc:
                recorder["error"] = exc
            except Exception as exc:
                recorder["error"] = exc
        finally:
            restore_modules(saved)
        recorder["namespace"] = namespace
        return recorder

    def assert_script_ran_clean(self, recorder):
        self.assertIsNone(
            recorder["error"],
            f"generated script raised {recorder['error']!r}",
        )

    def test_benign_excel_path_is_opened(self):
        script = self.root / "update_revit.py"
        path = "enriched_data.xlsx"
        self.generator.generate_parameter_update_script(
            {"Classification": "OmniClass Number"},
            path,
            str(script),
        )
        recorder = self.run_generated(script)
        self.assert_script_ran_clean(recorder)
        self.assertEqual(recorder["opened"], [path])

    def test_benign_schedule_uses_wall_category_and_name(self):
        script = self.root / "create_schedule.py"
        name = "Door Schedule"
        self.generator.generate_schedule_creator(
            name,
            "Walls",
            ["Type", "Level"],
            str(script),
        )
        recorder = self.run_generated(script)
        self.assert_script_ran_clean(recorder)
        self.assertEqual(recorder["schedule_names"], [name])
        self.assertIn("OST_Walls", recorder["builtins"])
        self.assertEqual(recorder["category_args"], ["OST_Walls"])

    def test_excel_path_quote_break_does_not_run(self):
        marker = self.marker("excel_quote.txt")
        excel_path = "legit.xlsx'; " + self.marker_statement(marker, "excel") + "; #"
        self.assert_excel_path_is_data(excel_path, marker)

    def test_excel_path_newline_break_does_not_run(self):
        marker = self.marker("excel_newline.txt")
        excel_path = (
            "legit.xlsx'\n"
            + self.marker_statement(marker, "excel")
            + "\n#"
        )
        self.assert_excel_path_is_data(excel_path, marker)

    def test_excel_path_with_apostrophe_and_backslashes_is_data(self):
        marker = self.marker("excel_apostrophe.txt")
        excel_path = "C:\\Users\\O'Brien\\enriched_data.xlsx"
        # Touching the marker is not part of this input. The assertion below
        # still requires the generated script not to create it.
        self.assert_excel_path_is_data(excel_path, marker)

    def test_schedule_name_quote_break_does_not_run(self):
        marker = self.marker("schedule_quote.txt")
        schedule_name = 'Door"; ' + self.marker_statement(marker, "schedule") + "; #"
        self.assert_schedule_name_is_data(schedule_name, marker)

    def test_schedule_name_newline_break_does_not_run(self):
        marker = self.marker("schedule_newline.txt")
        schedule_name = (
            'Door"\n'
            + self.marker_statement(marker, "schedule")
            + "\n#"
        )
        self.assert_schedule_name_is_data(schedule_name, marker)

    def test_category_statement_break_does_not_run(self):
        marker = self.marker("category_statement.txt")
        category = (
            "Walls)\n"
            + self.marker_statement(marker, "category")
            + "\n#"
        )
        self.assert_category_does_not_run(category, marker)

    def test_category_expression_break_does_not_run(self):
        marker = self.marker("category_expression.txt")
        category = "Walls and " + self.marker_statement(marker, "category")
        self.assert_category_does_not_run(category, marker)

    def test_mappings_and_fields_with_statement_separators_stay_data(self):
        marker = self.marker("json_fields.txt")
        payload = "value'; " + self.marker_statement(marker, "mappings") + "; #"
        mappings = {payload: payload}
        fields = [payload, "Type"]
        parameter_script = self.root / "mappings.py"
        schedule_script = self.root / "fields.py"
        self.generator.generate_parameter_update_script(
            mappings,
            "enriched_data.xlsx",
            str(parameter_script),
        )
        self.generator.generate_schedule_creator(
            "Door Schedule",
            "Walls",
            fields,
            str(schedule_script),
        )
        parameter_run = self.run_generated(parameter_script)
        schedule_run = self.run_generated(schedule_script)
        self.assertFalse(marker.exists(), "mapping or field text ran as Python")
        self.assert_script_ran_clean(parameter_run)
        self.assert_script_ran_clean(schedule_run)
        self.assertEqual(parameter_run["namespace"]["mappings"], mappings)
        self.assertEqual(schedule_run["namespace"]["fields"], fields)

    def assert_excel_path_is_data(self, excel_path, marker):
        script = self.root / "update_revit.py"
        self.generator.generate_parameter_update_script(
            {"Classification": "OmniClass Number"},
            excel_path,
            str(script),
        )
        recorder = self.run_generated(script)
        self.assertFalse(
            marker.exists(),
            "spreadsheet path ran as Python when the generated script was executed",
        )
        self.assert_script_ran_clean(recorder)
        self.assertEqual(recorder["opened"], [excel_path])

    def assert_schedule_name_is_data(self, schedule_name, marker):
        script = self.root / "create_schedule.py"
        self.generator.generate_schedule_creator(
            schedule_name,
            "Walls",
            ["Type"],
            str(script),
        )
        recorder = self.run_generated(script)
        self.assertFalse(
            marker.exists(),
            "schedule name ran as Python when the generated script was executed",
        )
        self.assert_script_ran_clean(recorder)
        self.assertEqual(recorder["schedule_names"], [schedule_name])

    def assert_category_does_not_run(self, category, marker):
        script = self.root / "create_schedule.py"
        try:
            self.generator.generate_schedule_creator(
                "Door Schedule",
                category,
                ["Type"],
                str(script),
            )
        except Exception as exc:
            self.assertFalse(
                script.exists(),
                f"rejecting category {category!r} still wrote {script} ({exc!r})",
            )
            self.assertFalse(marker.exists(), "category text ran before rejection")
            return
        recorder = self.run_generated(script)
        self.assertFalse(
            marker.exists(),
            "category ran as Python when the generated script was executed",
        )
        self.assert_script_ran_clean(recorder)


if __name__ == "__main__":
    unittest.main()
