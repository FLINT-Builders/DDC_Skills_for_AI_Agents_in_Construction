"""Failing tests for quadratic regex backtracking in the drawing analyzer.

The implementation under test is the DrawingAnalyzer class shipped in
SKILL.md. A page of text that contains a long run of digits and no unit,
foot mark, or colon makes DIMENSION_PATTERNS and the digits-colon-digits
scale pattern rescan that run from every offset. These tests time out while
that behavior remains, and they pass once those patterns (and the methods
that apply them) finish on the same input.

Run from the repository root:

    pip install pdfplumber
    python3 2_DDC_Book/2.4-PDF-CAD-to-Data/drawing-analyzer/tests/test_quadratic_regex_dos.py -v
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
THIS_FILE = Path(__file__).resolve()

# n=8_000 of digits took about 0.63s for the fastest dimension pattern and
# about 2.25s inside _extract_dimensions. 80_000 digits is 100x that work.
# Bounded quantifiers finish the same input in well under a tenth of a second.
DIGIT_RUN = 80_000
PATTERN_LIMIT_S = 1.5
METHOD_LIMIT_S = 1.5

# Full analyze_pdf_drawing also pays for pdfplumber extraction (about 0.5s at
# 30_000 characters here). Vulnerable patterns add many seconds on top.
PDF_RUN = 30_000
PDF_LIMIT_S = 3.0


def load_drawing_analyzer():
    source = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", source, re.DOTALL)
    if match is None:
        raise RuntimeError(f"no Python block in {SKILL_PATH}")
    namespace = {"__file__": str(SKILL_PATH)}
    exec(compile(match.group(1), str(SKILL_PATH), "exec"), namespace)
    return namespace["DrawingAnalyzer"]


def _run_worker(mode: str, argument: str, limit: float) -> str:
    try:
        completed = subprocess.run(
            [sys.executable, str(THIS_FILE), mode, argument],
            check=False,
            capture_output=True,
            text=True,
            timeout=limit,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        raise AssertionError(
            f"{mode} {argument} did not finish within {limit:.1f}s "
            f"(quadratic backtracking). partial stdout: {stdout[-500:]}"
        ) from exc
    if completed.returncode != 0:
        raise AssertionError(
            f"{mode} {argument} exited {completed.returncode}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    if "OK" not in completed.stdout:
        raise AssertionError(completed.stdout or completed.stderr)
    return completed.stdout


def _pdf_bytes(text: str) -> bytes:
    """One-page PDF whose only text is `text`, drawn tiny and white.

    Digits, spaces, and letters need no PDF escaping. pdfplumber still
    extracts that text; color and size are not filters on extract_text.
    """
    if re.search(r"[()\\]", text):
        raise ValueError("PDF fixture text must not need literal escaping")
    stream = f"BT /F1 0.2 Tf 1 1 1 rg 2 2 Td ({text}) Tj ET\n".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


class DrawingAnalyzerRegexDosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analyzer_cls = load_drawing_analyzer()

    def test_each_dimension_pattern_finishes_on_a_digit_run(self):
        patterns = self.analyzer_cls.DIMENSION_PATTERNS
        self.assertGreater(len(patterns), 0)
        for index, pattern in enumerate(patterns):
            with self.subTest(index=index, pattern=pattern):
                _run_worker("dimension-pattern", str(index), PATTERN_LIMIT_S)

    def test_each_scale_pattern_finishes_on_a_digit_run(self):
        patterns = self.analyzer_cls.SCALE_PATTERNS
        self.assertGreater(len(patterns), 0)
        for index, pattern in enumerate(patterns):
            with self.subTest(index=index, pattern=pattern):
                _run_worker("scale-pattern", str(index), PATTERN_LIMIT_S)

    def test_extract_dimensions_finds_real_values_beside_a_digit_run(self):
        """The run is not the whole page. findall still rescans it."""
        stdout = _run_worker("extract-dimensions", str(DIGIT_RUN), METHOD_LIMIT_S)
        self.assertIn("value=2500", stdout)
        self.assertIn("value=12", stdout)

    def test_determine_scale_finishes_when_no_ratio_follows_the_run(self):
        stdout = _run_worker("determine-scale", str(DIGIT_RUN), METHOD_LIMIT_S)
        self.assertIn("scale=1.0", stdout)

    def test_analyze_pdf_drawing_finishes_on_a_hidden_digit_run(self):
        stdout = _run_worker("analyze-pdf", str(PDF_RUN), PDF_LIMIT_S)
        self.assertIn("EXTRACT_OK", stdout)
        self.assertIn("value=2500", stdout)

    def test_short_text_still_yields_dimensions_and_metric_scale(self):
        analyzer = self.analyzer_cls()
        text = "10'-6\"\n2500 mm\n12 SF\n1:100\n"
        values = sorted(dimension.value for dimension in analyzer._extract_dimensions(text))
        self.assertIn(126, values)
        self.assertIn(2500, values)
        self.assertIn(12, values)
        self.assertAlmostEqual(analyzer._determine_scale(text), 0.01)

    def test_long_prose_still_reaches_a_trailing_dimension_and_scale(self):
        analyzer = self.analyzer_cls()
        text = ("wall " * 5000) + "2500 mm\n12 SF\n1:100"
        started = time.perf_counter()
        values = [dimension.value for dimension in analyzer._extract_dimensions(text)]
        scale = analyzer._determine_scale(text)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0)
        self.assertIn(2500, values)
        self.assertIn(12, values)
        self.assertAlmostEqual(scale, 0.01)

    def test_title_block_and_annotations_stay_linear_on_a_digit_run(self):
        analyzer = self.analyzer_cls()
        text = "9" * DIGIT_RUN
        started = time.perf_counter()
        annotations = analyzer._extract_annotations(text)
        title = analyzer._extract_title_block(text)
        value, unit = analyzer._parse_dimension_value(text)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0)
        self.assertTrue(annotations)
        self.assertEqual(title.sheet_number, "")
        self.assertIsInstance(value, float)
        self.assertIsInstance(unit, str)

    def test_imperial_scale_match_with_equals_stays_fast(self):
        """Reachable input: a SCALE line whose match already contains '='.

        _parse_imperial_scale is only called with that match. A digit run that
        never reaches this method is covered by the scale-pattern tests.
        """
        analyzer = self.analyzer_cls()
        text = 'SCALE: ' + ("9" * 5000) + '" = 1\'-'
        started = time.perf_counter()
        factor = analyzer._determine_scale(text)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0)
        self.assertTrue(factor == factor)  # not NaN; inf is the current overflow


def _worker_dimension_pattern(index: int) -> None:
    analyzer_cls = load_drawing_analyzer()
    pattern = analyzer_cls.DIMENSION_PATTERNS[index]
    re.findall(pattern, "9" * DIGIT_RUN)
    print("OK")


def _worker_scale_pattern(index: int) -> None:
    analyzer_cls = load_drawing_analyzer()
    pattern = analyzer_cls.SCALE_PATTERNS[index]
    re.search(pattern, "9" * DIGIT_RUN, re.IGNORECASE)
    print("OK")


def _worker_extract_dimensions(length: int) -> None:
    analyzer = load_drawing_analyzer()()
    text = "2500 mm\n12 SF\n" + ("9" * length)
    started = time.perf_counter()
    dimensions = analyzer._extract_dimensions(text)
    elapsed = time.perf_counter() - started
    rendered = ",".join(f"value={dimension.value}" for dimension in dimensions)
    print(f"OK elapsed={elapsed:.3f} {rendered}")


def _worker_determine_scale(length: int) -> None:
    analyzer = load_drawing_analyzer()()
    text = "SHEET A101\n" + ("9" * length)
    started = time.perf_counter()
    scale = analyzer._determine_scale(text)
    elapsed = time.perf_counter() - started
    print(f"OK elapsed={elapsed:.3f} scale={scale}")


def _worker_analyze_pdf(length: int) -> None:
    import pdfplumber

    prefix = "2500 mm "
    run = "9" * length
    page_text = prefix + run
    with tempfile.TemporaryDirectory() as directory:
        pdf_path = Path(directory) / "sheet.pdf"
        pdf_path.write_bytes(_pdf_bytes(page_text))
        with pdfplumber.open(pdf_path) as pdf:
            extracted = pdf.pages[0].extract_text() or ""
        if run not in extracted or "2500" not in extracted:
            raise SystemExit(
                f"pdfplumber did not return the planted digit run: {extracted[:120]!r}"
            )
        print("EXTRACT_OK", flush=True)
        analyzer = load_drawing_analyzer()()
        result = analyzer.analyze_pdf_drawing(str(pdf_path))
        rendered = ",".join(f"value={dimension.value}" for dimension in result.dimensions)
        print(f"OK file={result.file_name} scale={result.scale_factor} {rendered}")


def _dispatch_worker(argv: list[str]) -> bool:
    modes = {
        "dimension-pattern": lambda arg: _worker_dimension_pattern(int(arg)),
        "scale-pattern": lambda arg: _worker_scale_pattern(int(arg)),
        "extract-dimensions": lambda arg: _worker_extract_dimensions(int(arg)),
        "determine-scale": lambda arg: _worker_determine_scale(int(arg)),
        "analyze-pdf": lambda arg: _worker_analyze_pdf(int(arg)),
    }
    if len(argv) < 2 or argv[1] not in modes:
        return False
    if len(argv) != 3:
        raise SystemExit(f"usage: {argv[0]} <mode> <argument>")
    modes[argv[1]](argv[2])
    return True


if __name__ == "__main__":
    if not _dispatch_worker(sys.argv):
        unittest.main()
