"""ReDoS specs for ConstructionDataProfiler percentage matching.

The percentage pattern in the data-profiler skill is applied to text cells
through ``_infer_construction_type`` and ``_detect_patterns``. A run of digits
followed by a non-digit makes those two unbounded digit quantifiers try every
split before the match fails.

These tests load the class from ``SKILL.md`` and time that path. They fail
while the overlapping pattern is still used, and they pass once quantifiers
cannot overlap or values past a short length are skipped before ``str.match``.
Short percentage values must still be recognized.

Run (from the repository root, after ``pip install pandas numpy``):

    python3 2_DDC_Book/2.1-Data-Types-Classification/data-profiler/tests/test_percentage_pattern_redos.py
"""

from __future__ import annotations

import io
import re
import time
import unittest
from pathlib import Path

import pandas as pd

SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
# Four times as many digits. Quadratic backtracking costs about sixteen times
# as long; a linear or length-capped match stays well under eight times.
SHORT_DIGITS = 3_000
LONG_DIGITS = 12_000
GROWTH_LIMIT = 8
# Floor so a few milliseconds of timer noise cannot fail a constant-time fix.
NOISE_FLOOR_SECONDS = 0.05


def _load_profiler_class():
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if match is None or "class ConstructionDataProfiler" not in match.group(1):
        raise AssertionError(f"profiler implementation not found in {SKILL_PATH}")
    namespace: dict = {}
    exec(match.group(1), namespace)
    return namespace["ConstructionDataProfiler"]


def _digit_letter_cell(digit_count: int) -> str:
    return ("9" * digit_count) + "a"


def _csv_frame(column: str, cell: str) -> pd.DataFrame:
    # The skill accepts a user CSV. pandas keeps a digit run plus a letter as text.
    return pd.read_csv(io.StringIO(f"{column}\n{cell}\n"))


def _elapsed(func) -> float:
    started = time.perf_counter()
    func()
    return time.perf_counter() - started


class PercentagePatternRedosTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The skill installs pandas and numpy, not pyarrow. That resolves to the
        # Python-backed string dtype, whose str.match calls re.match per cell.
        pd.options.mode.string_storage = "python"
        cls.profiler_cls = _load_profiler_class()

    def setUp(self):
        self.profiler = self.profiler_cls()

    def _assert_growth_is_bounded(self, run, label: str):
        short_seconds = _elapsed(lambda: run(SHORT_DIGITS))
        long_seconds = _elapsed(lambda: run(LONG_DIGITS))
        limit = max(NOISE_FLOOR_SECONDS, short_seconds * GROWTH_LIMIT)
        self.assertLess(
            long_seconds,
            limit,
            f"{label}: {LONG_DIGITS} digits took {long_seconds:.3f}s after "
            f"{SHORT_DIGITS} digits took {short_seconds:.3f}s "
            f"(limit {limit:.3f}s). Overlapping digit quantifiers are still "
            f"backtracking inside one cell.",
        )

    def test_detect_patterns_bounds_digit_run_growth(self):
        def run(digit_count: int):
            series = pd.Series([_digit_letter_cell(digit_count)], dtype="str")
            self.profiler._detect_patterns(series)

        self._assert_growth_is_bounded(run, "_detect_patterns")

    def test_infer_construction_type_bounds_digit_run_growth(self):
        def run(digit_count: int):
            frame = _csv_frame("legacy_note", _digit_letter_cell(digit_count))
            inferred = self.profiler._infer_construction_type(
                frame["legacy_note"], "legacy_note"
            )
            self.assertNotIn(inferred, ("percentage",))

        self._assert_growth_is_bounded(run, "_infer_construction_type")

    def test_profile_dataframe_unhinted_csv_column_bounds_growth(self):
        def run(digit_count: int):
            frame = _csv_frame("legacy_note", _digit_letter_cell(digit_count))
            self.assertEqual(str(frame["legacy_note"].dtype), "str")
            self.assertEqual(
                frame["legacy_note"].iloc[0], _digit_letter_cell(digit_count)
            )
            profile = self.profiler.profile_dataframe(frame, "legacy-export")
            self.assertEqual(profile.columns[0].name, "legacy_note")
            self.assertEqual(profile.columns[0].inferred_type, "text")

        self._assert_growth_is_bounded(run, "profile_dataframe unhinted column")

    def test_profile_dataframe_hinted_column_still_bounds_pattern_scan(self):
        # A cost hint returns before the pattern loop in _infer_construction_type.
        # _detect_patterns still matches every construction pattern.
        def run(digit_count: int):
            frame = _csv_frame("unit_cost", _digit_letter_cell(digit_count))
            profile = self.profiler.profile_dataframe(frame, "cost-export")
            self.assertEqual(profile.columns[0].inferred_type, "cost")

        self._assert_growth_is_bounded(run, "profile_dataframe hinted column")

    def test_short_percentage_values_are_still_detected(self):
        frame = pd.read_csv(io.StringIO("legacy_note\n85%\n12.5\n100\n"))
        profile = self.profiler.profile_dataframe(frame, "percentages")
        column = profile.columns[0]
        self.assertEqual(column.inferred_type, "percentage")
        self.assertTrue(
            any(pattern.startswith("percentage") for pattern in column.common_patterns),
            column.common_patterns,
        )


if __name__ == "__main__":
    unittest.main()
