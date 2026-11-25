"""Floor assemblies must be held to the energy-code floor U-value limit.

`ComplianceChecker.check_envelope` in
`5_DDC_Innovative/energy-simulation/SKILL.md` stores `floor_u_max` for
IECC 2021 and ASHRAE 90.1-2019, then reviews only walls, windows, and
roofs. These tests execute that implementation. They fail while a floor
whose assembly U-value is above `floor_u_max` still leaves the envelope
result compliant and absent from `issues`.

Run from the repository root:

    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

import contextlib
import io
import re
import unittest
from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "5_DDC_Innovative"
    / "energy-simulation"
    / "SKILL.md"
)

U_VALUE_ISSUE = "Exceeds maximum U-value"
SHGC_ISSUE = "Exceeds maximum SHGC"


def load_skill_namespace():
    """Execute the skill's Python blocks in one namespace.

    The skill ships its implementation as fenced code in SKILL.md. Loading
    those blocks is the path an assistant follows when it runs the skill.
    """
    source = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"^```python\n(.*?)^```", source, flags=re.DOTALL | re.MULTILINE)
    if len(blocks) != 4:
        raise AssertionError(f"expected 4 Python blocks in {SKILL_PATH}, found {len(blocks)}")
    namespace = {"__name__": "energy_simulation_skill"}
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        for index, block in enumerate(blocks, start=1):
            exec(compile(block, f"{SKILL_PATH}#block{index}", "exec"), namespace)
    for name in (
        "BuildingGeometry",
        "ComplianceChecker",
        "MaterialLayer",
        "WallAssembly",
        "Window",
        "Zone",
    ):
        if name not in namespace:
            raise AssertionError(f"{name} was not defined by {SKILL_PATH}")
    return namespace


class EnvelopeFloorComplianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_skill_namespace()
        cls.codes = {
            "IECC_2021_5A": cls.ns["ComplianceChecker"]("IECC_2021_5A"),
            "ASHRAE_90.1_2019_4A": cls.ns["ComplianceChecker"]("ASHRAE_90.1_2019_4A"),
        }

    def assembly(self, name, target_u):
        """Build a real assembly whose U-value is `target_u`.

        Resistance matches `WallAssembly`: 0.13 inside film, layer R, 0.04
        outside film. The returned U-value is the property `check_envelope`
        reads for walls and roofs (`assembly.u_value`).
        """
        layer_r = (1.0 / target_u) - 0.17
        assembly = self.ns["WallAssembly"](
            name,
            [self.ns["MaterialLayer"](name, thickness_m=layer_r, conductivity=1.0, density=1.0, specific_heat=1.0)],
        )
        self.assertEqual(assembly.u_value, target_u)
        return assembly

    def geometry(self, floors, wall_u=0.20, roof_u=0.10, window_u=1.2, window_shgc=0.25):
        zone = self.ns["Zone"](
            zone_id="z1",
            name="Office",
            floor_area_m2=100.0,
            volume_m3=300.0,
            occupancy=10,
            lighting_power_density=6.0,
            equipment_power_density=8.0,
            ventilation_rate=8.0,
        )
        return self.ns["BuildingGeometry"](
            zones=[zone],
            walls=[
                {
                    "zone": "z1",
                    "orientation": "south",
                    "area": 40.0,
                    "assembly": self.assembly("wall", wall_u),
                }
            ],
            windows=[
                {
                    "zone": "z1",
                    "orientation": "south",
                    "area": 10.0,
                    "window_type": self.ns["Window"]("glazing", window_u, window_shgc),
                }
            ],
            roofs=[
                {
                    "zone": "z1",
                    "area": 100.0,
                    "assembly": self.assembly("roof", roof_u),
                }
            ],
            floors=floors,
        )

    def floor(self, zone, target_u, is_ground):
        return {
            "zone": zone,
            "area": 100.0,
            "assembly": self.assembly(f"floor-{zone}", target_u),
            "is_ground": is_ground,
        }

    def u_issue(self, element, actual, required):
        return {
            "element": element,
            "actual": actual,
            "required": required,
            "issue": U_VALUE_ISSUE,
        }

    def assert_envelope(self, code_key, building, expected_issues):
        checker = self.codes[code_key]
        result = checker.check_envelope(building)
        requirements = checker.requirements
        self.assertEqual(
            set(result),
            {"code", "climate_zone", "compliant", "issues"},
        )
        self.assertEqual(result["code"], requirements.code_name)
        self.assertEqual(result["climate_zone"], requirements.climate_zone)
        self.assertEqual(result["compliant"], expected_issues == [])
        self.assertCountEqual(result["issues"], expected_issues)
        return result

    def test_stored_floor_limits_are_the_code_maxima(self):
        iecc = self.codes["IECC_2021_5A"].requirements
        ashrae = self.codes["ASHRAE_90.1_2019_4A"].requirements
        self.assertEqual(iecc.code_name, "IECC 2021")
        self.assertEqual(iecc.climate_zone, "5A")
        self.assertEqual(iecc.floor_u_max, 0.30)
        self.assertEqual(ashrae.code_name, "ASHRAE 90.1-2019")
        self.assertEqual(ashrae.climate_zone, "4A")
        self.assertEqual(ashrae.floor_u_max, 0.32)
        self.assertLess(iecc.roof_u_max, iecc.floor_u_max)
        self.assertLess(iecc.floor_u_max, iecc.wall_u_max)
        self.assertLess(ashrae.roof_u_max, ashrae.floor_u_max)
        self.assertLess(ashrae.floor_u_max, ashrae.wall_u_max)

    def test_iecc_ground_floor_above_floor_u_max_fails(self):
        """Attack path: compliant walls, windows, and roof, illegal ground floor.

        IECC floor_u_max is 0.30. The floor assembly U-value is 0.50.
        """
        floor_u = 0.50
        requirements = self.codes["IECC_2021_5A"].requirements
        self.assertGreater(floor_u, requirements.floor_u_max)
        building = self.geometry(
            floors=[self.floor("z1", floor_u, is_ground=True)]
        )
        self.assert_envelope(
            "IECC_2021_5A",
            building,
            [self.u_issue("Floor z1", floor_u, requirements.floor_u_max)],
        )

    def test_iecc_exposed_floor_above_floor_u_max_fails(self):
        floor_u = 0.50
        requirements = self.codes["IECC_2021_5A"].requirements
        building = self.geometry(
            floors=[self.floor("z1", floor_u, is_ground=False)]
        )
        self.assert_envelope(
            "IECC_2021_5A",
            building,
            [self.u_issue("Floor z1", floor_u, requirements.floor_u_max)],
        )

    def test_ashrae_floor_above_its_own_floor_u_max_fails(self):
        floor_u = 0.50
        requirements = self.codes["ASHRAE_90.1_2019_4A"].requirements
        self.assertGreater(floor_u, requirements.floor_u_max)
        building = self.geometry(
            floors=[self.floor("z1", floor_u, is_ground=True)]
        )
        self.assert_envelope(
            "ASHRAE_90.1_2019_4A",
            building,
            [self.u_issue("Floor z1", floor_u, requirements.floor_u_max)],
        )

    def test_floor_above_floor_max_but_under_wall_max_fails_both_codes(self):
        """A floor limit borrowed from walls would accept these assemblies."""
        cases = {
            "IECC_2021_5A": 0.325,
            "ASHRAE_90.1_2019_4A": 0.385,
        }
        for code_key, floor_u in cases.items():
            with self.subTest(code_key=code_key):
                requirements = self.codes[code_key].requirements
                self.assertGreater(floor_u, requirements.floor_u_max)
                self.assertLess(floor_u, requirements.wall_u_max)
                building = self.geometry(
                    floors=[self.floor("z1", floor_u, is_ground=True)]
                )
                self.assert_envelope(
                    code_key,
                    building,
                    [self.u_issue("Floor z1", floor_u, requirements.floor_u_max)],
                )

    def test_floor_under_floor_max_but_over_roof_max_passes_both_codes(self):
        """A floor limit borrowed from roofs would reject these assemblies."""
        cases = {
            "IECC_2021_5A": 0.25,
            "ASHRAE_90.1_2019_4A": 0.295,
        }
        for code_key, floor_u in cases.items():
            with self.subTest(code_key=code_key):
                requirements = self.codes[code_key].requirements
                self.assertGreater(floor_u, requirements.roof_u_max)
                self.assertLess(floor_u, requirements.floor_u_max)
                building = self.geometry(
                    floors=[self.floor("z1", floor_u, is_ground=False)]
                )
                self.assert_envelope(code_key, building, [])

    def test_floor_equal_to_floor_u_max_passes_both_codes(self):
        """The other envelope checks use a strict greater-than comparison."""
        for code_key in self.codes:
            with self.subTest(code_key=code_key):
                floor_u = self.codes[code_key].requirements.floor_u_max
                building = self.geometry(
                    floors=[self.floor("z1", floor_u, is_ground=True)]
                )
                self.assert_envelope(code_key, building, [])

    def test_value_between_the_two_floor_limits_follows_the_selected_code(self):
        floor_u = 0.31
        iecc_max = self.codes["IECC_2021_5A"].requirements.floor_u_max
        ashrae_max = self.codes["ASHRAE_90.1_2019_4A"].requirements.floor_u_max
        self.assertGreater(floor_u, iecc_max)
        self.assertLess(floor_u, ashrae_max)
        building = self.geometry(
            floors=[self.floor("z1", floor_u, is_ground=False)]
        )
        self.assert_envelope(
            "IECC_2021_5A",
            building,
            [self.u_issue("Floor z1", floor_u, iecc_max)],
        )
        self.assert_envelope("ASHRAE_90.1_2019_4A", building, [])

    def test_only_floors_over_the_limit_are_reported_and_later_floors_count(self):
        """The legal floor is first. Ground and exposed floors are both reviewed."""
        requirements = self.codes["IECC_2021_5A"].requirements
        legal_u = 0.20
        ground_u = 0.50
        exposed_u = 0.40
        self.assertLess(legal_u, requirements.floor_u_max)
        self.assertGreater(ground_u, requirements.floor_u_max)
        self.assertGreater(exposed_u, requirements.floor_u_max)
        building = self.geometry(
            floors=[
                self.floor("slab", legal_u, is_ground=True),
                self.floor("basement", ground_u, is_ground=True),
                self.floor("overhang", exposed_u, is_ground=False),
            ]
        )
        self.assert_envelope(
            "IECC_2021_5A",
            building,
            [
                self.u_issue("Floor basement", ground_u, requirements.floor_u_max),
                self.u_issue("Floor overhang", exposed_u, requirements.floor_u_max),
            ],
        )

    def test_wall_window_and_roof_failures_are_reported_with_the_floor(self):
        floor_u = 0.50
        wall_u = 0.50
        roof_u = 0.50
        window_u = 2.5
        window_shgc = 0.55
        for code_key in self.codes:
            with self.subTest(code_key=code_key):
                requirements = self.codes[code_key].requirements
                self.assertGreater(wall_u, requirements.wall_u_max)
                self.assertGreater(roof_u, requirements.roof_u_max)
                self.assertGreater(window_u, requirements.window_u_max)
                self.assertGreater(window_shgc, requirements.window_shgc_max)
                self.assertGreater(floor_u, requirements.floor_u_max)
                building = self.geometry(
                    floors=[self.floor("z1", floor_u, is_ground=True)],
                    wall_u=wall_u,
                    roof_u=roof_u,
                    window_u=window_u,
                    window_shgc=window_shgc,
                )
                self.assert_envelope(
                    code_key,
                    building,
                    [
                        self.u_issue("Wall z1 south", wall_u, requirements.wall_u_max),
                        self.u_issue(
                            "Window z1 south", window_u, requirements.window_u_max
                        ),
                        {
                            "element": "Window z1 south",
                            "actual": window_shgc,
                            "required": requirements.window_shgc_max,
                            "issue": SHGC_ISSUE,
                        },
                        self.u_issue("Roof z1", roof_u, requirements.roof_u_max),
                        self.u_issue("Floor z1", floor_u, requirements.floor_u_max),
                    ],
                )

    def test_building_with_no_floors_stays_compliant_when_other_elements_pass(self):
        for code_key in self.codes:
            with self.subTest(code_key=code_key):
                self.assert_envelope(code_key, self.geometry(floors=[]), [])


if __name__ == "__main__":
    unittest.main()
