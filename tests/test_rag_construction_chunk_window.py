"""Forward-progress tests for the rag-construction fixed-size chunker.

The implementation under test is the Python block in
``2_DDC_Book/2.3-Pandas-LLM-Analysis/rag-construction/SKILL.md``.
``TextChunker._chunk_fixed_size`` is used for ``ChunkingStrategy.FIXED_SIZE``
and, because ``SEMANTIC`` has no handler, for ``ChunkingStrategy.SEMANTIC``.
``ConstructionRAG.add_document`` and ``add_documents`` call that chunker.

These tests fail while a chunk window can stop advancing. They pass once
every window moves forward (or an explicit ValueError / NotImplementedError
rejects an unsupported strategy or an overlap that is not smaller than the
chunk size).

Not the same loop, so not asserted here: ``_chunk_by_paragraph``,
``_chunk_by_section`` (oversized sections are split with PARAGRAPH), and
``_chunk_by_sentence``. The vector-search skill's ``DocumentIndexingPipeline``
splits on whitespace and steps by a positive ``chunk_size - chunk_overlap``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "2_DDC_Book"
    / "2.3-Pandas-LLM-Analysis"
    / "rag-construction"
    / "SKILL.md"
)
MARKER = "tail-marker-xyz"
# A correct chunker advances at least one character per iteration, so a
# subprocess backstop this long only fires when the real loop does not return.
LOOP_TIMEOUT_SECONDS = 3


def _load_skill_source() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", text, re.S)
    if match is None:
        raise AssertionError(f"no python block in {SKILL_PATH}")
    source = match.group(1)
    if "class TextChunker" not in source or "class ConstructionRAG" not in source:
        raise AssertionError("skill python block is not the RAG implementation")
    return source


def _payload(chunk_size: int, token_len: int, prefix_unit: str = "word ") -> str:
    """Place ``token_len`` non-whitespace characters so a chunk end falls inside them.

    The prefix length equals ``chunk_size``, which is the layout that makes
    ``end = start + chunk_size`` land on the token after the first window.
    """
    if chunk_size % len(prefix_unit) != 0:
        raise AssertionError("prefix unit must divide chunk_size")
    prefix = prefix_unit * (chunk_size // len(prefix_unit))
    return f"{prefix}{'_' * token_len} {MARKER}"


class _ChunkCase(unittest.TestCase):
    def run_case(
        self,
        *,
        entry: str,
        strategy: str,
        content: str,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        marker: str | None = None,
        allow_reject: bool = False,
    ) -> None:
        spec = {
            "skill": str(SKILL_PATH),
            "entry": entry,
            "strategy": strategy,
            "content": content,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "marker": marker,
        }
        try:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker", json.dumps(spec)],
                check=False,
                capture_output=True,
                text=True,
                timeout=LOOP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"{entry} {strategy} did not return in {LOOP_TIMEOUT_SECONDS}s "
                f"(chunk window is not advancing)\nstdout={exc.stdout!r}\nstderr={exc.stderr!r}"
            )

        self.assertEqual(
            completed.returncode,
            0,
            f"worker failed\nstdout={completed.stdout}\nstderr={completed.stderr}",
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"worker output is not json ({exc}): {completed.stdout!r}\n{completed.stderr}")

        error = result.get("error")
        if allow_reject and error in {"ValueError", "NotImplementedError"}:
            return
        self.assertIsNone(
            error,
            f"{entry} {strategy} raised {error}: {result.get('message')}",
        )
        chunks = result["chunks"]
        self.assertIsInstance(chunks, int)
        self.assertGreaterEqual(chunks, 1)
        self.assertLessEqual(chunks, len(content))
        if marker is not None:
            self.assertTrue(
                result["covers_marker"],
                f"chunks never reached the text after the long token: {result['preview']!r}",
            )
        else:
            self.assertTrue(
                result["covers_ends"],
                f"chunks dropped an end of the document: {result['preview']!r}",
            )


class FixedSizeChunkWindowTests(_ChunkCase):
    def test_normal_prose_fixed_size_completes(self) -> None:
        prose = "The concrete shall be ASTM C150 Type I or II. " * 40
        self.run_case(
            entry="chunk_document",
            strategy="FIXED_SIZE",
            content=prose,
            marker=None,
        )

    def test_fixed_size_token_at_overlap_boundary_does_not_stall(self) -> None:
        # chunk_size 500, overlap 50 -> a 450-character token keeps the next
        # start on the same whitespace and the loop appends the same window.
        content = _payload(500, 450)
        self.run_case(
            entry="chunk_document",
            strategy="FIXED_SIZE",
            content=content,
            marker=MARKER,
        )

    def test_token_one_shorter_than_boundary_still_completes(self) -> None:
        content = _payload(500, 449)
        self.run_case(
            entry="chunk_document",
            strategy="FIXED_SIZE",
            content=content,
            marker=MARKER,
        )

    def test_semantic_uses_same_window_and_must_not_stall(self) -> None:
        content = _payload(500, 450)
        self.run_case(
            entry="chunk_document",
            strategy="SEMANTIC",
            content=content,
            marker=MARKER,
            allow_reject=True,
        )

    def test_smaller_window_has_the_same_stall(self) -> None:
        content = _payload(100, 80)
        self.run_case(
            entry="chunk_document",
            strategy="FIXED_SIZE",
            content=content,
            chunk_size=100,
            chunk_overlap=20,
            marker=MARKER,
        )

    def test_token_after_short_word_does_not_stall(self) -> None:
        # Whitespace near the start of the window: end walks back onto that
        # space, then start = end - overlap moves to a negative index and sticks.
        content = f"word {'_' * 496} {MARKER}"
        self.run_case(
            entry="chunk_document",
            strategy="FIXED_SIZE",
            content=content,
            marker=MARKER,
        )

    def test_no_whitespace_run_does_not_walk_off_the_string(self) -> None:
        # A run longer than chunk_size has no whitespace to land on, so end
        # walks back to start and the next start becomes negative.
        content = ("A" * 1000) + ("B" * 1000)
        self.run_case(
            entry="chunk_document",
            strategy="FIXED_SIZE",
            content=content,
        )

    def test_overlap_equal_to_chunk_size_does_not_stall(self) -> None:
        content = "The concrete shall be ASTM C150 Type I or II. " * 10
        self.run_case(
            entry="chunk_document",
            strategy="FIXED_SIZE",
            content=content,
            chunk_size=500,
            chunk_overlap=500,
            allow_reject=True,
        )

    def test_overlap_greater_than_chunk_size_does_not_crash(self) -> None:
        content = "The concrete shall be ASTM C150 Type I or II. " * 10
        self.run_case(
            entry="chunk_document",
            strategy="FIXED_SIZE",
            content=content,
            chunk_size=500,
            chunk_overlap=600,
            allow_reject=True,
        )

    def test_add_document_fixed_size_does_not_stall(self) -> None:
        content = _payload(500, 450)
        self.run_case(
            entry="add_document",
            strategy="FIXED_SIZE",
            content=content,
            marker=MARKER,
        )

    def test_add_documents_semantic_does_not_stall(self) -> None:
        content = _payload(500, 450)
        self.run_case(
            entry="add_documents",
            strategy="SEMANTIC",
            content=content,
            marker=MARKER,
            allow_reject=True,
        )

    def test_paragraph_section_and_sentence_finish_on_the_same_text(self) -> None:
        content = _payload(500, 450)
        for strategy in ("PARAGRAPH", "SECTION", "SENTENCE"):
            with self.subTest(strategy=strategy):
                self.run_case(
                    entry="chunk_document",
                    strategy=strategy,
                    content=content,
                    marker=MARKER,
                )


def _worker(spec: dict) -> None:
    source = Path(spec["skill"]).read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)```", source, re.S)
    if match is None:
        raise RuntimeError("python block missing")
    code = match.group(1)
    # VectorStore.search annotates Tuple without importing it, so the class
    # body raises NameError before ConstructionRAG is defined. Binding the
    # name here does not change _chunk_fixed_size; it only lets the rest of
    # the published block finish evaluating.
    namespace = {"Tuple": tuple}
    exec(compile(code, spec["skill"], "exec"), namespace)

    chunking_strategy = namespace["ChunkingStrategy"]
    document_type = namespace["DocumentType"]
    document_cls = namespace["Document"]
    strategy = getattr(chunking_strategy, spec["strategy"])
    document = document_cls(
        id="submittal-1",
        title="Rebar shop drawing submittal",
        doc_type=document_type.SUBMITTAL,
        content=spec["content"],
        source="submittal.pdf",
    )
    entry = spec["entry"]
    chunks = None
    if entry == "chunk_document":
        chunker = namespace["TextChunker"](
            strategy,
            spec["chunk_size"],
            spec["chunk_overlap"],
        )
        chunks = chunker.chunk_document(document)
    elif entry == "add_document":
        rag = namespace["ConstructionRAG"](
            chunking_strategy=strategy,
            chunk_size=spec["chunk_size"],
        )
        rag.add_document(document)
        chunks = document.chunks
    elif entry == "add_documents":
        rag = namespace["ConstructionRAG"](
            chunking_strategy=strategy,
            chunk_size=spec["chunk_size"],
        )
        rag.add_documents([document])
        chunks = document.chunks
    else:
        raise RuntimeError(f"unknown entry {entry}")

    texts = [chunk.content for chunk in chunks]
    blob = "\n".join(texts)
    content = spec["content"].strip()
    marker = spec.get("marker")
    head = content[:20]
    tail = content[-20:]
    preview = (texts[0][:80] if texts else "") + (" | " + texts[-1][-80:] if texts else "")
    print(
        json.dumps(
            {
                "chunks": len(chunks),
                "error": None,
                "covers_marker": True if marker is None else marker in blob,
                "covers_ends": bool(texts) and head in blob and tail in blob,
                "preview": preview,
            }
        )
    )


def _worker_main() -> None:
    spec = json.loads(sys.argv[2])
    try:
        _worker(spec)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "chunks": None,
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "covers_marker": False,
                    "covers_ends": False,
                    "preview": "",
                }
            )
        )


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        _worker_main()
    else:
        unittest.main()
