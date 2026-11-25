"""Spec for confining drone survey report writes to the output directory.

These tests load ``DroneDataProcessor`` from the skill's published source in
``SKILL.md`` and call ``process_survey``. They fail while a flight id can
choose a path outside the output directory. They pass once a flight id cannot
create, modify, or delete anything outside that directory, including when a
check accepts one path and the write opens another.

A safe flight id must still write its JSON report inside the output directory.

Not covered here:
- ``ConstructionProgressMonitor.generate_progress_report`` writes an Excel
  file to an output path the caller passes in. That path is not built from a
  flight id.
- Backslash separators. This host resolves paths with POSIX rules, and
  ``posixpath.join`` does not treat ``\\`` as a separator.
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
PATH_FIELDS = (
    "report_path",
    "orthomosaic_path",
    "dem_path",
    "dsm_path",
    "point_cloud_path",
)


def _definition_block(source: str) -> ast.Module:
    """Keep imports and definitions so the published classes execute as written."""
    tree = ast.parse(source)
    body = [
        node
        for node in tree.body
        if isinstance(
            node,
            (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        )
    ]
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    return module


def load_processor_namespace() -> dict:
    """Execute the skill blocks that define ``DroneDataProcessor``."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.S)
    if len(blocks) < 2:
        raise AssertionError(f"{SKILL_PATH} does not contain the processor source")
    namespace: dict = {"__file__": str(SKILL_PATH)}
    for block in blocks[:2]:
        exec(compile(_definition_block(block), str(SKILL_PATH), "exec"), namespace, namespace)
    if "DroneDataProcessor" not in namespace or "SurveyFlight" not in namespace:
        raise AssertionError("DroneDataProcessor was not defined by SKILL.md")
    return namespace


def make_flight(namespace: dict, flight_id: str):
    camera = namespace["CameraParameters"](
        focal_length_mm=24.0,
        sensor_width_mm=13.2,
        sensor_height_mm=8.8,
        image_width_px=4000,
        image_height_px=3000,
    )
    geo = namespace["GeoReference"](
        crs="EPSG:4326",
        origin=(0.0, 0.0, 0.0),
        rotation=(0.0, 0.0, 0.0),
    )
    return namespace["SurveyFlight"](
        flight_id=flight_id,
        date=datetime(2024, 6, 1, tzinfo=timezone.utc),
        site_name="North pad",
        images=[],
        camera=camera,
        geo_reference=geo,
        flight_altitude=80.0,
        gsd=2.0,
    )


def snapshot(root: Path) -> dict:
    """Record files, directories, and links under ``root`` without following links."""
    state = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[relative] = ("symlink", os_readlink(path))
        elif path.is_dir():
            state[relative] = ("dir",)
        elif path.is_file():
            state[relative] = ("file", path.read_bytes())
    return state


def os_readlink(path: Path) -> str:
    return str(path.readlink())


def outside(root: Path, output: Path, state: dict) -> dict:
    confined = {}
    leaked = {}
    for relative, value in state.items():
        if (root / relative).is_relative_to(output):
            confined[relative] = value
        else:
            leaked[relative] = value
    return leaked


class FlightIdReportPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = load_processor_namespace()

    def setUp(self):
        self._tmp = __import__("tempfile").TemporaryDirectory(prefix="drone-survey-")
        self.root = Path(self._tmp.name)
        self.output = self.root / "reports"
        self.trusted = self.root / "trusted"
        self.trusted.mkdir()
        self.victim = self.trusted / "kept_report.json"
        self.victim_bytes = b'{"trusted": true, "job": "kept"}\n'
        self.victim.write_bytes(self.victim_bytes)
        self.processor = self.namespace["DroneDataProcessor"](str(self.output))
        self.before = snapshot(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _process(self, flight_id: str):
        flight = make_flight(self.namespace, flight_id)
        try:
            return self.processor.process_survey(
                flight,
                generate_ortho=True,
                generate_dem=True,
                generate_pointcloud=True,
            )
        except Exception as exc:
            return exc

    def _assert_confined(self, result, flight_id: str):
        after = snapshot(self.root)
        self.assertEqual(
            outside(self.root, self.output, self.before),
            outside(self.root, self.output, after),
            f"flight id {flight_id!r} changed a path outside {self.output}",
        )
        if isinstance(result, Exception):
            return
        for field in PATH_FIELDS:
            value = getattr(result, field, None)
            if not value:
                continue
            resolved = Path(value).resolve()
            self.assertTrue(
                resolved.is_relative_to(self.output.resolve()),
                f"{field} for flight id {flight_id!r} resolved to {resolved}, "
                f"outside {self.output.resolve()}",
            )
            if resolved.exists():
                self.assertTrue(
                    resolved.is_relative_to(self.output.resolve()),
                    f"{field} exists at {resolved}",
                )

    def test_safe_flight_id_writes_report_inside_output(self):
        flight_id = "FLIGHT-001"
        result = self._process(flight_id)
        self.assertNotIsInstance(result, Exception)
        self._assert_confined(result, flight_id)
        report = Path(result.report_path).resolve()
        self.assertTrue(report.is_file())
        self.assertTrue(report.is_relative_to(self.output.resolve()))
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        self.assertEqual(self.victim.read_bytes(), self.victim_bytes)

    def test_parent_segment_does_not_overwrite_outside_json(self):
        result = self._process("../trusted/kept")
        self._assert_confined(result, "../trusted/kept")
        self.assertEqual(self.victim.read_bytes(), self.victim_bytes)

    def test_nested_parent_segments_do_not_overwrite_outside_json(self):
        (self.output / "nested").mkdir(parents=True)
        self.before = snapshot(self.root)
        flight_id = "nested/../../trusted/kept"
        result = self._process(flight_id)
        self._assert_confined(result, flight_id)
        self.assertEqual(self.victim.read_bytes(), self.victim_bytes)

    def test_parent_segment_does_not_create_new_outside_json(self):
        created = self.trusted / "brand_new_report.json"
        self.assertFalse(created.exists())
        result = self._process("../trusted/brand_new")
        self._assert_confined(result, "../trusted/brand_new")
        self.assertFalse(created.exists())
        self.assertEqual(self.victim.read_bytes(), self.victim_bytes)

    def test_absolute_flight_id_does_not_write_outside_output(self):
        absolute_dir = self.root / "absolute"
        absolute_dir.mkdir()
        target = absolute_dir / "kept_report.json"
        target.write_bytes(self.victim_bytes)
        self.before = snapshot(self.root)
        flight_id = str(absolute_dir / "kept")
        result = self._process(flight_id)
        self._assert_confined(result, flight_id)
        self.assertEqual(target.read_bytes(), self.victim_bytes)

    def test_sibling_prefix_directory_is_outside_output(self):
        sibling = self.root / "reports_backup"
        sibling.mkdir()
        target = sibling / "kept_report.json"
        target.write_bytes(self.victim_bytes)
        self.before = snapshot(self.root)
        flight_id = "../reports_backup/kept"
        result = self._process(flight_id)
        self._assert_confined(result, flight_id)
        self.assertEqual(target.read_bytes(), self.victim_bytes)

    def test_symlink_component_does_not_write_outside_output(self):
        outside_dir = self.root / "linked_target"
        outside_dir.mkdir()
        target = outside_dir / "kept_report.json"
        target.write_bytes(self.victim_bytes)
        (self.output / "via").symlink_to(outside_dir, target_is_directory=True)
        self.before = snapshot(self.root)
        flight_id = "via/kept"
        result = self._process(flight_id)
        self._assert_confined(result, flight_id)
        self.assertEqual(target.read_bytes(), self.victim_bytes)

    def test_report_write_is_confined_when_products_are_disabled(self):
        flight = make_flight(self.namespace, "../trusted/kept")
        try:
            result = self.processor.process_survey(
                flight,
                generate_ortho=False,
                generate_dem=False,
                generate_pointcloud=False,
            )
        except Exception as exc:
            result = exc
        self._assert_confined(result, "../trusted/kept")
        self.assertEqual(self.victim.read_bytes(), self.victim_bytes)


if __name__ == "__main__":
    unittest.main()
