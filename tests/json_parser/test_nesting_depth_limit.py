"""Nesting limit for the construction JSON parser.

The implementation under test is the Python block in
``2_DDC_Book/2.1-Data-Types-Classification/json-parser/SKILL.md``.
These tests fail while that block walks nested documents with unbounded
recursion. They pass once every entry point below refuses a document whose
container depth is greater than ``MAX_JSON_DEPTH`` and still flattens
documents at that depth.

Container depth is 0 for a scalar. For an object or array it is one plus the
maximum container depth of its children. Both objects and arrays count.
``{"k": 1}`` and ``[1]`` each have depth 1. ``{"k": {"k": 1}}`` has depth 2.
``{"a": [{"a": 1}]}`` has depth 3.

Refusal contract:
- ``parse_string`` and ``parse_file`` return ``JSONParseResult`` with
  ``success`` false, ``data`` None, ``record_count`` 0, and at least one
  error whose text contains "depth". They must not raise.
- ``flatten_json``, ``to_dataframe``, ``validate_schema``, and
  ``BIMJSONParser.parse_bim_elements`` raise an exception that is not
  ``RecursionError`` and whose message contains "depth".
- A refusal must not change ``sys.getrecursionlimit``.
- The same parser instance must still accept a shallow document afterward.

Operations that share the recursive walk and are covered here:
``flatten_json`` (nested-object branch and list-of-dicts branch),
``to_dataframe`` for one object and for a list of objects,
``validate_schema``, ``parse_string``, ``parse_file``, and
``parse_bim_elements`` for ``elements``, ``objects``, ``entities``, and a
top-level list.

Left untested because they do not recurse into nested containers:
``IoTJSONParser.parse_sensor_reading``, ``IoTJSONParser.parse_sensor_batch``,
``ConstructionJSONParser.extract_elements``,
``BIMJSONParser.extract_properties``, ``_count_records``, and the columnar
dict-of-lists branch of ``to_dataframe``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


MAX_JSON_DEPTH = 32
RECURSION_CRASH_OBJECT_DEPTH = 1000
RECURSION_CRASH_ARRAY_DEPTH = 10000
# Each repetition adds one object and one array, and one flatten_json call.
RECURSION_CRASH_LIST_OF_DICTS_LEVELS = 1000

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "2_DDC_Book"
    / "2.1-Data-Types-Classification"
    / "json-parser"
    / "SKILL.md"
)


def load_skill_namespace():
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("```", start)
    block = text[start:end]
    if "class ConstructionJSONParser" not in block:
        raise AssertionError("SKILL.md python block no longer defines the parser")
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(block, str(SKILL_PATH), "exec"), namespace)
    return namespace


def object_document(depth: int) -> str:
    if depth < 1:
        raise ValueError("depth must be positive")
    return '{"k":' * depth + "1" + "}" * depth


def array_document(depth: int) -> str:
    if depth < 1:
        raise ValueError("depth must be positive")
    return "[" * depth + "1" + "]" * depth


def list_of_dicts_document(dict_levels: int) -> str:
    """``dict_levels`` objects, each wrapped around a one-element array.

    Container depth is ``2 * dict_levels``. ``flatten_json`` recurses once
    per object through its list-of-dicts branch.
    """
    if dict_levels < 1:
        raise ValueError("dict_levels must be positive")
    return '{"a":[' * dict_levels + "1" + "]}" * dict_levels


def expected_object_key(depth: int) -> str:
    return "_".join(["k"] * depth)


def expected_list_of_dicts_key(dict_levels: int) -> str:
    return "a_0_" * (dict_levels - 1) + "a"


class ParserDepthLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = load_skill_namespace()
        cls.ns = ns
        cls.JSONParseResult = ns["JSONParseResult"]
        cls.Parser = ns["ConstructionJSONParser"]
        cls.BIMParser = ns["BIMJSONParser"]

    def setUp(self):
        self.parser = self.Parser()
        self.bim = self.BIMParser()
        self._recursion_limit = sys.getrecursionlimit()

    def tearDown(self):
        self.assertEqual(
            sys.getrecursionlimit(),
            self._recursion_limit,
            "a depth check must not raise the Python recursion limit",
        )

    def assert_depth_message(self, message: str):
        lowered = message.lower()
        self.assertIn("depth", lowered, message)
        self.assertNotIn(
            "recursion",
            lowered,
            "catching RecursionError is not a depth limit",
        )

    def assert_walk_refused(self, func):
        try:
            result = func()
        except RecursionError as exc:
            self.fail(f"unbounded recursion: {exc}")
        except Exception as exc:
            self.assert_depth_message(str(exc))
            return
        self.fail(f"nested document was accepted and returned {type(result).__name__}")

    def assert_parse_refused(self, func):
        try:
            result = func()
        except RecursionError as exc:
            self.fail(f"unbounded recursion while loading: {exc}")
        except Exception as exc:
            self.fail(
                "parse raised "
                f"{type(exc).__name__}: {exc}; expected a failed JSONParseResult"
            )
        self.assertIsInstance(result, self.JSONParseResult)
        self.assertFalse(result.success)
        self.assertIsNone(result.data)
        self.assertEqual(result.record_count, 0)
        self.assertTrue(result.errors)
        for error in result.errors:
            self.assert_depth_message(error)

    def assert_still_parses_shallow_document(self):
        result = self.parser.parse_string(
            '{"sensor_id": "sensor-1", "value": 21.5, "unit": "C"}'
        )
        self.assertTrue(result.success, result.errors)
        frame = self.parser.to_dataframe(result.data)
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.loc[0, "sensor_id"], "sensor-1")
        self.assertEqual(frame.loc[0, "value"], 21.5)

    def test_object_at_limit_flattens_and_loads(self):
        document = object_document(MAX_JSON_DEPTH)
        loaded = json.loads(document)
        key = expected_object_key(MAX_JSON_DEPTH)

        flat = self.parser.flatten_json(loaded)
        self.assertEqual(flat, {key: 1})

        frame = self.parser.to_dataframe(loaded)
        self.assertEqual(frame.to_dict(orient="records"), [{key: 1}])

        listed = self.parser.to_dataframe([loaded])
        self.assertEqual(listed.to_dict(orient="records"), [{key: 1}])

        validity = self.parser.validate_schema(loaded, [key])
        self.assertTrue(validity["valid"])
        self.assertEqual(validity["missing_fields"], [])
        self.assertEqual(validity["completeness"], 100)

        elements = self.bim.parse_bim_elements({"elements": [loaded]})
        self.assertEqual(elements.to_dict(orient="records"), [{key: 1}])

        parsed = self.parser.parse_string(document)
        self.assertTrue(parsed.success, parsed.errors)
        self.assertEqual(parsed.data, loaded)
        self.assertEqual(parsed.record_count, 1)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            handle.write(document)
            path = handle.name
        try:
            from_file = self.parser.parse_file(path)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertTrue(from_file.success, from_file.errors)
        self.assertEqual(from_file.data, loaded)

    def test_list_of_dicts_at_dict_recursion_limit_flattens(self):
        # 16 objects under 16 arrays: container depth is exactly the maximum.
        document = list_of_dicts_document(MAX_JSON_DEPTH // 2)
        loaded = json.loads(document)
        key = expected_list_of_dicts_key(MAX_JSON_DEPTH // 2)
        self.assertEqual(self.parser.flatten_json(loaded), {key: [1]})
        parsed = self.parser.parse_string(document)
        self.assertTrue(parsed.success, parsed.errors)
        self.assertEqual(parsed.data, loaded)

    def test_array_at_limit_loads(self):
        document = array_document(MAX_JSON_DEPTH)
        parsed = self.parser.parse_string(document)
        self.assertTrue(parsed.success, parsed.errors)
        self.assertEqual(parsed.data, json.loads(document))
        self.assertEqual(parsed.record_count, 1)

    def test_flatten_json_refuses_one_object_past_the_limit(self):
        loaded = json.loads(object_document(MAX_JSON_DEPTH + 1))
        self.assert_walk_refused(lambda: self.parser.flatten_json(loaded))
        self.assert_still_parses_shallow_document()

    def test_flatten_json_refuses_list_of_dicts_past_the_limit(self):
        # 17 objects, each inside an array: container depth 34, and the
        # list-of-dicts recursive call runs once per object.
        loaded = json.loads(list_of_dicts_document((MAX_JSON_DEPTH // 2) + 1))
        self.assert_walk_refused(lambda: self.parser.flatten_json(loaded))

    def test_to_dataframe_refuses_deep_object_and_deep_list(self):
        loaded = json.loads(object_document(MAX_JSON_DEPTH + 1))
        self.assert_walk_refused(lambda: self.parser.to_dataframe(loaded))
        self.assert_walk_refused(lambda: self.parser.to_dataframe([loaded]))

    def test_validate_schema_refuses_deep_object(self):
        loaded = json.loads(object_document(MAX_JSON_DEPTH + 1))
        self.assert_walk_refused(
            lambda: self.parser.validate_schema(loaded, ["k"])
        )

    def test_parse_bim_elements_refuses_every_collection_shape(self):
        loaded = json.loads(object_document(MAX_JSON_DEPTH + 1))
        cases = (
            {"elements": [loaded]},
            {"objects": [loaded]},
            {"entities": [loaded]},
            [loaded],
        )
        for payload in cases:
            with self.subTest(payload=type(payload).__name__):
                self.assert_walk_refused(
                    lambda payload=payload: self.bim.parse_bim_elements(payload)
                )

    def test_parse_string_and_parse_file_refuse_object_past_the_limit(self):
        document = object_document(MAX_JSON_DEPTH + 1)
        self.assert_parse_refused(lambda: self.parser.parse_string(document))
        self.assert_still_parses_shallow_document()

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            handle.write(document)
            path = handle.name
        try:
            self.assert_parse_refused(lambda: self.parser.parse_file(path))
        finally:
            self.assertTrue(Path(path).is_file(), "refusal must not delete the submitted file")
            Path(path).unlink(missing_ok=True)
        self.assert_still_parses_shallow_document()

    def test_parse_string_and_parse_file_refuse_array_past_the_limit(self):
        document = array_document(MAX_JSON_DEPTH + 1)
        self.assert_parse_refused(lambda: self.parser.parse_string(document))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            handle.write(document)
            path = handle.name
        try:
            self.assert_parse_refused(lambda: self.parser.parse_file(path))
        finally:
            Path(path).unlink(missing_ok=True)

    def _assert_loaded_document_refused(self, result, walk):
        """Accept either a failed parse or a controlled refusal while walking.

        The quick-start order is load, then ``to_dataframe`` / ``flatten_json``
        when ``success`` is true. Both outcomes must be a depth refusal, and
        neither may raise ``RecursionError``.
        """
        if result.success:
            self.assert_walk_refused(walk)
            self.fail("loader accepted a document past the nesting limit")
        self.assertIsNone(result.data)
        self.assertEqual(result.record_count, 0)
        self.assertTrue(result.errors)
        for error in result.errors:
            self.assert_depth_message(error)

    def test_api_pipeline_does_not_crash_on_recursion_depth_object(self):
        """Skill quick start for an API response: parse_string then to_dataframe.

        One thousand nested objects load, then the flattener recurses once
        per level.
        """
        document = object_document(RECURSION_CRASH_OBJECT_DEPTH)
        result = self.parser.parse_string(document)
        self._assert_loaded_document_refused(
            result, lambda: self.parser.to_dataframe(result.data)
        )
        self.assert_still_parses_shallow_document()

    def test_file_pipeline_does_not_crash_on_recursion_depth_object(self):
        """Skill quick start for a file: parse_file then to_dataframe."""
        document = object_document(RECURSION_CRASH_OBJECT_DEPTH)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            handle.write(document)
            path = handle.name
        try:
            result = self.parser.parse_file(path)
            self._assert_loaded_document_refused(
                result, lambda: self.parser.to_dataframe(result.data)
            )
        finally:
            self.assertTrue(Path(path).is_file(), "refusal must not delete the submitted file")
            Path(path).unlink(missing_ok=True)
        self.assert_still_parses_shallow_document()

    def test_flatten_and_callers_do_not_crash_on_recursion_depth_object(self):
        loaded = json.loads(object_document(RECURSION_CRASH_OBJECT_DEPTH))
        calls = (
            "flatten_json",
            lambda: self.parser.flatten_json(loaded),
            "to_dataframe",
            lambda: self.parser.to_dataframe(loaded),
            "to_dataframe_list",
            lambda: self.parser.to_dataframe([loaded]),
            "validate_schema",
            lambda: self.parser.validate_schema(loaded, ["k"]),
            "parse_bim_elements",
            lambda: self.bim.parse_bim_elements({"elements": [loaded]}),
        )
        for name, func in zip(calls[0::2], calls[1::2]):
            with self.subTest(operation=name):
                self.assert_walk_refused(func)

    def test_list_of_dicts_pipeline_does_not_crash_at_recursion_depth(self):
        document = list_of_dicts_document(RECURSION_CRASH_LIST_OF_DICTS_LEVELS)
        loaded = json.loads(document)
        with self.subTest(operation="parse_string_then_flatten"):
            result = self.parser.parse_string(document)
            self._assert_loaded_document_refused(
                result, lambda: self.parser.flatten_json(result.data)
            )
        bim_cases = (
            ("objects", {"objects": [loaded]}),
            ("entities", {"entities": [loaded]}),
            ("list", [loaded]),
        )
        for name, payload in bim_cases:
            with self.subTest(collection=name):
                self.assert_walk_refused(
                    lambda payload=payload: self.bim.parse_bim_elements(payload)
                )

    def test_import_of_recursion_depth_array_does_not_crash(self):
        """``parse_string`` / ``parse_file`` call ``json.loads`` / ``json.load``.

        Those decoders recurse once per array. A document of this depth
        raises ``RecursionError`` from the loader unless the parser refuses
        it before that walk.
        """
        document = array_document(RECURSION_CRASH_ARRAY_DEPTH)
        with self.subTest(operation="parse_string"):
            self.assert_parse_refused(lambda: self.parser.parse_string(document))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            handle.write(document)
            path = handle.name
        try:
            with self.subTest(operation="parse_file"):
                self.assert_parse_refused(lambda: self.parser.parse_file(path))
        finally:
            self.assertTrue(Path(path).is_file(), "refusal must not delete the submitted file")
            Path(path).unlink(missing_ok=True)
        self.assert_still_parses_shallow_document()


if __name__ == "__main__":
    unittest.main()
