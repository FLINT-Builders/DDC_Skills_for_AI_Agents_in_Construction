"""Timing specs for unbounded section-number scans.

The cases below load the skill implementations as written and run them on
attacker-controlled specification text. They fail while those patterns still
rescan a long digit run from every offset. A bound on each digit run, applied
to one line at a time, makes the same cases pass.

Left out of this module, after timing them on the same digit run:
drawing-analyzer DIMENSION_PATTERNS and SCALE_PATTERNS (quadratic, but they
are dimension and scale scans, not section numbers); specification-extractor
ARTICLE_PATTERN and CSI_SECTION_PATTERN (re.match on a single line);
rag-construction's section split (the digit alternative sits behind a newline);
contract-clause-extractor's article/section header (it requires the word
ARTICLE or SECTION before the number).
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
import time
import types
import unittest
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PDF_SKILL = REPO / "2_DDC_Book" / "2.4-PDF-CAD-to-Data" / "pdf-to-structured" / "SKILL.md"
CONTRACT_SKILL = (
    REPO / "1_DDC_Toolkit" / "Document-Control" / "contract-clause-analyzer" / "SKILL.md"
)

# Three times as many digits. A linear scan stays near 3x. The unanchored
# digit-then-period patterns measured here land near 6x-9x.
PDF_SMALL = 12_000
PDF_LARGE = 36_000
CONTRACT_SMALL = 8_000
CONTRACT_LARGE = 24_000


def _load_namespace(skill_path: Path, marker: str) -> dict:
    """Exec the skill code block that defines marker, skipping example calls."""
    text = skill_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    block = next(b for b in blocks if marker in b)
    tree = ast.parse(block)
    kept = []
    for node in tree.body:
        if isinstance(node, ast.Expr):
            continue
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            continue
        kept.append(node)
    tree.body = kept
    module_name = "skill_under_test_" + str(abs(hash(skill_path)))
    module = types.ModuleType(module_name)
    module.__file__ = str(skill_path)
    sys.modules[module_name] = module
    exec(compile(tree, str(skill_path), "exec"), module.__dict__)
    return module.__dict__


def _flate_pdf(content_stream: bytes) -> bytes:
    """One-page PDF whose text content stream is Flate-compressed."""
    compressed = zlib.compress(content_stream)
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n",
        b"4 0 obj << /Length %d /Filter /FlateDecode >>\nstream\n" % len(compressed)
        + compressed
        + b"\nendstream\nendobj\n",
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    parts = [b"%PDF-1.4\n"]
    offsets = []
    for obj in objects:
        offsets.append(sum(len(part) for part in parts))
        parts.append(obj)
    xref_at = sum(len(part) for part in parts)
    xref = [b"xref\n0 6\n0000000000 65535 f \n"]
    for offset in offsets:
        xref.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    parts.append(b"".join(xref))
    parts.append(
        f"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return b"".join(parts)


def _digit_run_pdf(count: int) -> bytes:
    digits = b"9" * count
    stream = b"BT /F1 11 Tf 72 720 Td (" + digits + b") Tj ET"
    return _flate_pdf(stream)


def _spec_headings_pdf() -> bytes:
    stream = b"""BT
/F1 11 Tf
14 TL
72 720 Td
(03.10 CAST-IN-PLACE CONCRETE) Tj
T*
(1.2.3 MATERIALS AND METHODS) Tj
ET
"""
    return _flate_pdf(stream)


class SectionNumberRedosTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pdf_ns = _load_namespace(PDF_SKILL, "def parse_specification_pdf")
        contract_ns = _load_namespace(CONTRACT_SKILL, "class ContractClauseAnalyzer")
        cls.parse_specification_pdf = staticmethod(pdf_ns["parse_specification_pdf"])
        cls.analyzer = contract_ns["ContractClauseAnalyzer"]()

    def _time_pdf(self, count: int, directory: Path) -> float:
        path = directory / f"digits-{count}.pdf"
        payload = _digit_run_pdf(count)
        path.write_bytes(payload)
        # A few kilobytes is the whole attack file. Compression is why a
        # size gate on the upload would not stop the parse.
        self.assertLess(len(payload), 8_192)
        started = time.perf_counter()
        frame = self.parse_specification_pdf(str(path))
        elapsed = time.perf_counter() - started
        self.assertEqual(len(frame), 0)
        return elapsed

    def test_specification_pdf_digit_run_is_not_quadratic(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            # Compile and open once so the timed calls are the parse itself.
            self._time_pdf(64, directory)
            small = self._time_pdf(PDF_SMALL, directory)
            large = self._time_pdf(PDF_LARGE, directory)

        growth = large / small
        self.assertLess(
            growth,
            4.6,
            f"parse_specification_pdf grew {growth:.2f}x "
            f"({small:.3f}s at {PDF_SMALL} digits, {large:.3f}s at {PDF_LARGE})",
        )
        self.assertLess(
            large,
            1.5,
            f"parse_specification_pdf took {large:.3f}s on {PDF_LARGE} digits",
        )

    def test_specification_pdf_still_extracts_section_headings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.pdf"
            path.write_bytes(_spec_headings_pdf())
            frame = self.parse_specification_pdf(str(path))

        rows = frame.to_dict(orient="records")
        self.assertEqual(
            rows,
            [
                {
                    "section_number": "03.10",
                    "title": "CAST-IN-PLACE CONCRETE",
                    "level": 2,
                },
                {
                    "section_number": "1.2.3",
                    "title": "MATERIALS AND METHODS",
                    "level": 3,
                },
            ],
        )

    def _time_contract(self, count: int) -> float:
        started = time.perf_counter()
        result = self.analyzer.analyze_text("submitted-contract", "9" * count)
        elapsed = time.perf_counter() - started
        self.assertGreaterEqual(result.total_clauses, 1)
        return elapsed

    def test_contract_section_split_digit_run_is_not_quadratic(self):
        self._time_contract(64)
        small = self._time_contract(CONTRACT_SMALL)
        large = self._time_contract(CONTRACT_LARGE)
        growth = large / small
        self.assertLess(
            growth,
            4.5,
            f"ContractClauseAnalyzer.analyze_text grew {growth:.2f}x "
            f"({small:.3f}s at {CONTRACT_SMALL} digits, "
            f"{large:.3f}s at {CONTRACT_LARGE})",
        )
        self.assertLess(
            large,
            0.25,
            f"analyze_text took {large:.3f}s on {CONTRACT_LARGE} digits",
        )

    def test_contract_section_split_still_finds_headings(self):
        text = (
            "1.1 PAYMENT TERMS\n"
            "The contractor shall submit invoices.\n"
            "2.0 SCHEDULE\n"
            "Time is of the essence.\n"
        )
        result = self.analyzer.analyze_text("Construction Contract", text)
        titles = " ".join(clause.title for clause in result.clauses)
        self.assertGreaterEqual(result.total_clauses, 2)
        self.assertIn("PAYMENT", titles)
        self.assertIn("SCHEDULE", titles)


if __name__ == "__main__":
    unittest.main()
