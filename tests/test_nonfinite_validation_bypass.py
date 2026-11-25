"""Spec tests for non-finite numbers in the construction data validators.

These tests fail while the validators accept NaN or infinite quantities, unit
costs, totals, progress percentages, and the other numeric checks that use the
same comparisons. They pass once those values are rejected with an error and
are not handed to processing.

The validator implementations are the Python in the skill documents. Each test
loads that source and runs it. Attacker payloads are JSON texts that include
the non-standard NaN literal, parsed with the standard library json module,
which is the JSON input the skills tell the agent to accept.

Run from the repository root:

    python3 tests/test_nonfinite_validation_bypass.py
"""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT_SKILL = ROOT / "4_DDC_Curated" / "Data-Validation" / "input-validation" / "SKILL.md"
SCHEMA_SKILL = ROOT / "4_DDC_Curated" / "Data-Validation" / "schema-validation" / "SKILL.md"


def _python_blocks(path: Path):
    text = path.read_text(encoding="utf-8")
    return re.findall(r"```python\n(.*?)```", text, flags=re.S)


def _exec_blocks(path: Path, namespace: dict, skip):
    for index, block in enumerate(_python_blocks(path)):
        if skip(block):
            continue
        exec(compile(block, f"{path}#block{index}", "exec"), namespace)


class _RouteApp:
    """Enough of an app object for the schema skill's route decorator to load."""

    def post(self, _path):
        def decorator(fn):
            return fn
        return decorator


def _load_input_validation():
    namespace = {"__name__": "input_validation_skill"}
    # The usage example calls process_estimate on a hardcoded valid estimate.
    # The classes and validate_pipeline_inputs are what the tests execute.
    _exec_blocks(
        INPUT_SKILL,
        namespace,
        skip=lambda block: "process_estimate(" in block,
    )
    return namespace


def _load_schema_validation():
    namespace = {
        "__name__": "schema_validation_skill",
        "app": _RouteApp(),
        # Replaced per test. Defined here so the skill function can load.
        "process_estimate": lambda payload: payload,
    }
    # Usage and JSON-Schema export blocks print sample data at import time.
    _exec_blocks(
        SCHEMA_SKILL,
        namespace,
        skip=lambda block: "print(" in block,
    )
    return namespace


INPUT = _load_input_validation()
SCHEMA = _load_schema_validation()


def _loads(payload: str):
    parsed = json.loads(payload)
    return parsed


def _error_fields(result):
    return [issue.field for issue in result.errors]


def _schema_paths(result):
    return [error.path for error in result.errors]


def _run_estimate_gate(validator, estimate, processed):
    """Gate copied from the input-validation usage example.

    result = validator.validate(estimate)
    if result.is_valid:
        process_estimate(estimate)
    """
    result = validator.validate(estimate)
    if result.is_valid:
        processed.append(estimate)
    return result


def _matching_estimate():
    return _loads(
        """
        {
          "project_name": "Office Building",
          "estimate_date": "2026-01-15",
          "gross_area": 50000,
          "line_items": [
            {"description": "Concrete", "csi_code": "03 30 00", "quantity": 5000, "unit_cost": 150},
            {"description": "Steel", "csi_code": "05 12 00", "quantity": 200, "unit_cost": 2500}
          ],
          "total": 1250000
        }
        """
    )


def _inflated_estimate(quantity_token: str, unit_cost_token: str, total_token: str, gross_area_token: str = "50000"):
    return _loads(
        f"""
        {{
          "project_name": "Office Building",
          "estimate_date": "2026-01-15",
          "gross_area": {gross_area_token},
          "line_items": [
            {{"description": "Concrete", "csi_code": "03 30 00", "quantity": 5000, "unit_cost": 150}},
            {{"description": "Adjustment", "csi_code": "03 30 00", "quantity": {quantity_token}, "unit_cost": {unit_cost_token}}}
          ],
          "total": {total_token}
        }}
        """
    )


def _schema_estimate(quantity_token="5000", unit_cost_token="150.00", amount_token="750000.00",
                     total_token="825000.00", subtotal_token="750000.00",
                     gross_area_token="50000", contingency_token="10"):
    return _loads(
        f"""
        {{
          "project_id": "PROJ-001",
          "project_name": "Downtown Office Tower",
          "estimate_type": "schematic",
          "estimate_date": "2026-01-15",
          "gross_area": {gross_area_token},
          "line_items": [
            {{
              "id": "1",
              "csi_code": "03 30 00",
              "description": "Cast-in-place concrete",
              "quantity": {quantity_token},
              "unit": "CY",
              "unit_cost": {unit_cost_token},
              "amount": {amount_token}
            }}
          ],
          "subtotal": {subtotal_token},
          "contingency_percent": {contingency_token},
          "total": {total_token}
        }}
        """
    )


class CostEstimateNonFiniteTests(unittest.TestCase):
    def setUp(self):
        self.validator = INPUT["CostEstimateValidator"]()
        self.processed = []

    def test_matching_finite_estimate_is_processed(self):
        estimate = _matching_estimate()
        result = _run_estimate_gate(self.validator, estimate, self.processed)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.errors, [])
        self.assertEqual(self.processed, [estimate])

    def test_negative_quantity_is_rejected(self):
        estimate = _matching_estimate()
        estimate["line_items"][0]["quantity"] = -1
        result = _run_estimate_gate(self.validator, estimate, self.processed)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(field.endswith(".quantity") for field in _error_fields(result)))
        self.assertEqual(self.processed, [])

    def test_finite_total_mismatch_is_rejected(self):
        estimate = _matching_estimate()
        estimate["total"] = 9999999
        result = _run_estimate_gate(self.validator, estimate, self.processed)
        self.assertFalse(result.is_valid)
        self.assertIn("total", _error_fields(result))
        self.assertEqual(self.processed, [])

    def test_nan_quantity_inflated_total_is_rejected_and_not_processed(self):
        estimate = _inflated_estimate("NaN", "50", "5000000")
        result = _run_estimate_gate(self.validator, estimate, self.processed)
        fields = _error_fields(result)
        self.assertEqual(self.processed, [])
        self.assertFalse(result.is_valid)
        self.assertTrue(any(field.endswith(".quantity") for field in fields))
        self.assertIn("total", fields)

    def test_nan_unit_cost_inflated_total_is_rejected_and_not_processed(self):
        estimate = _inflated_estimate("10", "NaN", "5000000")
        result = _run_estimate_gate(self.validator, estimate, self.processed)
        fields = _error_fields(result)
        self.assertEqual(self.processed, [])
        self.assertFalse(result.is_valid)
        self.assertTrue(any(field.endswith(".unit_cost") for field in fields))
        self.assertIn("total", fields)

    def test_nan_declared_total_is_rejected_and_not_processed(self):
        estimate = _matching_estimate()
        estimate["total"] = _loads('{"total": NaN}')["total"]
        result = _run_estimate_gate(self.validator, estimate, self.processed)
        self.assertEqual(self.processed, [])
        self.assertFalse(result.is_valid)
        self.assertIn("total", _error_fields(result))

    def test_infinite_quantity_with_zero_unit_cost_is_rejected(self):
        estimate = _inflated_estimate("Infinity", "0", "5000000")
        result = _run_estimate_gate(self.validator, estimate, self.processed)
        fields = _error_fields(result)
        self.assertEqual(self.processed, [])
        self.assertFalse(result.is_valid)
        self.assertTrue(any(field.endswith(".quantity") for field in fields))
        self.assertIn("total", fields)

    def test_nan_gross_area_is_rejected(self):
        estimate = _matching_estimate()
        estimate["gross_area"] = _loads('{"gross_area": NaN}')["gross_area"]
        result = _run_estimate_gate(self.validator, estimate, self.processed)
        self.assertEqual(self.processed, [])
        self.assertFalse(result.is_valid)
        self.assertTrue(any("gross_area" in field for field in _error_fields(result)))

    def test_pipeline_rejects_nan_quantity_estimate(self):
        estimate = _inflated_estimate("NaN", "50", "5000000")
        accepted = INPUT["validate_pipeline_inputs"]({"estimate": estimate})
        self.assertFalse(accepted)

    def test_pipeline_accepts_matching_estimate(self):
        accepted = INPUT["validate_pipeline_inputs"]({"estimate": _matching_estimate()})
        self.assertTrue(accepted)


class ProgressAndQuantityRangeTests(unittest.TestCase):
    def test_percent_complete_in_range_is_accepted(self):
        result = INPUT["FieldDataValidator"]().validate(
            {"type": "progress", "percent_complete": 50, "previous_percent": 40}
        )
        self.assertTrue(result.is_valid)
        self.assertEqual(result.errors, [])

    def test_percent_complete_above_100_is_rejected(self):
        result = INPUT["FieldDataValidator"]().validate(
            {"type": "progress", "percent_complete": 150}
        )
        self.assertFalse(result.is_valid)
        self.assertIn("percent_complete", _error_fields(result))

    def test_nan_percent_complete_is_rejected(self):
        payload = _loads('{"type": "progress", "percent_complete": NaN, "previous_percent": 40}')
        result = INPUT["FieldDataValidator"]().validate(payload)
        self.assertFalse(result.is_valid)
        self.assertIn("percent_complete", _error_fields(result))
        accepted = INPUT["validate_pipeline_inputs"]({"field_data": payload})
        self.assertFalse(accepted)

    def test_nan_bim_area_volume_and_length_are_rejected(self):
        element = _loads(
            """
            {
              "guid": "g1",
              "ifc_type": "IfcWall",
              "name": "Wall",
              "geometry": {"origin": [0, 0, 0]},
              "area": NaN,
              "volume": NaN,
              "length": NaN
            }
            """
        )
        result = INPUT["BIMDataValidator"]().validate({"elements": [element]})
        fields = _error_fields(result)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(field.endswith(".area") for field in fields))
        self.assertTrue(any(field.endswith(".volume") for field in fields))
        self.assertTrue(any(field.endswith(".length") for field in fields))

    def test_negative_bim_area_is_rejected(self):
        result = INPUT["BIMDataValidator"]().validate(
            {
                "elements": [
                    {
                        "guid": "g1",
                        "ifc_type": "IfcWall",
                        "name": "Wall",
                        "geometry": {"origin": [0, 0, 0]},
                        "area": -1,
                    }
                ]
            }
        )
        self.assertFalse(result.is_valid)
        self.assertTrue(any(field.endswith(".area") for field in _error_fields(result)))

    def test_nan_workforce_count_is_rejected(self):
        payload = _loads(
            """
            {
              "type": "daily_report",
              "date": "2020-01-01",
              "weather": "clear",
              "workforce": [{"count": NaN}]
            }
            """
        )
        result = INPUT["FieldDataValidator"]().validate(payload)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("workforce" in field or "count" in field for field in _error_fields(result)))

    def test_nan_resource_allocation_is_rejected(self):
        payload = _loads(
            """
            {
              "project_name": "Tower",
              "start_date": "2026-01-01",
              "tasks": [
                {
                  "id": "a",
                  "start_date": "2026-01-01",
                  "end_date": "2026-01-10",
                  "resources": [{"id": "crew-1", "allocation": NaN}]
                },
                {
                  "id": "b",
                  "start_date": "2026-01-01",
                  "end_date": "2026-01-10",
                  "resources": [{"id": "crew-1", "allocation": 80}]
                }
              ]
            }
            """
        )
        result = INPUT["ScheduleValidator"]().validate(payload)
        self.assertFalse(result.is_valid)
        self.assertTrue(
            any("allocation" in field or "resource" in field for field in _error_fields(result))
        )


class SchemaNonFiniteTests(unittest.TestCase):
    def _process(self, payload, endpoint):
        processed = []
        SCHEMA["process_estimate"] = lambda body: processed.append(body)
        if endpoint == "/api/estimates":
            SCHEMA["create_estimate"](payload)
        result = SCHEMA["validate_api_request"](endpoint, payload)
        return result, processed

    def test_valid_estimate_is_processed(self):
        payload = _schema_estimate()
        result, processed = self._process(payload, "/api/estimates")
        self.assertTrue(result.is_valid)
        self.assertEqual(result.errors, [])
        self.assertEqual(processed, [payload])

    def test_negative_quantity_is_rejected_and_not_processed(self):
        payload = _schema_estimate(quantity_token="-5", amount_token="1", subtotal_token="1", total_token="1")
        result, processed = self._process(payload, "/api/estimates")
        self.assertFalse(result.is_valid)
        self.assertTrue(any(path.endswith(".quantity") for path in _schema_paths(result)))
        self.assertEqual(processed, [])

    def test_nan_quantity_is_rejected_and_not_processed(self):
        payload = _schema_estimate(quantity_token="NaN")
        result, processed = self._process(payload, "/api/estimates")
        self.assertEqual(processed, [])
        self.assertFalse(result.is_valid)
        self.assertTrue(any(path.endswith(".quantity") for path in _schema_paths(result)))

    def test_nan_unit_cost_is_rejected_and_not_processed(self):
        payload = _schema_estimate(unit_cost_token="NaN")
        result, processed = self._process(payload, "/api/estimates")
        self.assertEqual(processed, [])
        self.assertFalse(result.is_valid)
        self.assertTrue(any(path.endswith(".unit_cost") for path in _schema_paths(result)))

    def test_nan_total_is_rejected_and_not_processed(self):
        payload = _schema_estimate(total_token="NaN")
        result, processed = self._process(payload, "/api/estimates")
        self.assertEqual(processed, [])
        self.assertFalse(result.is_valid)
        self.assertIn("total", _schema_paths(result))

    def test_nan_gross_area_is_rejected(self):
        payload = _schema_estimate(gross_area_token="NaN")
        result, processed = self._process(payload, "/api/estimates")
        self.assertEqual(processed, [])
        self.assertFalse(result.is_valid)
        self.assertIn("gross_area", _schema_paths(result))

    def test_nan_contingency_percent_is_rejected(self):
        payload = _schema_estimate(contingency_token="NaN")
        result, processed = self._process(payload, "/api/estimates")
        self.assertEqual(processed, [])
        self.assertFalse(result.is_valid)
        self.assertIn("contingency_percent", _schema_paths(result))

    def test_nan_percent_complete_is_rejected(self):
        payload = _loads(
            """
            {
              "project_id": "PROJ-001",
              "schedule_name": "Tower",
              "data_date": "2026-01-15",
              "start_date": "2026-01-01",
              "finish_date": "2026-02-01",
              "tasks": [
                {
                  "id": "1",
                  "name": "Foundation",
                  "start_date": "2026-01-01",
                  "finish_date": "2026-01-31",
                  "duration": 20,
                  "percent_complete": NaN
                }
              ]
            }
            """
        )
        result, _processed = self._process(payload, "/api/schedules")
        self.assertFalse(result.is_valid)
        self.assertTrue(any(path.endswith(".percent_complete") for path in _schema_paths(result)))

    def test_nan_bim_area_is_rejected(self):
        payload = _loads(
            """
            {
              "guid": "12345678-1234-1234-1234-123456789abc",
              "ifc_class": "IfcWall",
              "quantities": {"area": NaN, "volume": NaN, "length": NaN}
            }
            """
        )
        result = SCHEMA["validate_api_request"]("/api/bim/elements", payload)
        paths = _schema_paths(result)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(path.endswith(".area") for path in paths))
        self.assertTrue(any(path.endswith(".volume") for path in paths))
        self.assertTrue(any(path.endswith(".length") for path in paths))


if __name__ == "__main__":
    unittest.main()
