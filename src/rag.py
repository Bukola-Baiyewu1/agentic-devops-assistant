"""Runbook retrieval with section-level, verifiable citations.

Runbooks are split by Markdown heading. Each chunk keeps its source file,
heading, exact line range, checksum, and a runbook version, so a proposal can
cite the precise passage that supports it (for example
`high-error-rate#restart-after-a-recent-deploy`, lines 12-14).

Scoring is TF-IDF cosine similarity: dependency-free, deterministic, and
measured against the labelled cases in `evals/retrieval_cases.json`
(`python -m scripts.eval_retrieval`). Results below
`AEGIS_RETRIEVAL_MIN_SCORE` are discarded, so an unrelated alert retrieves
nothing and the agent escalates instead of guessing.

The `Retriever` protocol is the seam for a future embedding/pgvector backend.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Protocol

from .config import settings

_WORD = re.compile(r"[a-z0-9_]+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "for",
        "from",
        "if",
        "in",
        "is",
        "it",
        "no",
        "not",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "with",
    ]
)


def _tokenize(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOPWORDS]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    source: str
    title: str
    heading: str
    text: str
    start_line: int
    end_line: int
    checksum: str
    runbook_version: str

    def citation(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "source": self.source,
            "heading": self.heading,
            "lines": f"{self.start_line}-{self.end_line}",
            "runbook_version": self.runbook_version,
        }


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: Chunk
    score: float

    def to_dict(self) -> dict:
        return {**asdict(self.chunk), "score": self.score}


class Retriever(Protocol):
    def retrieve(self, query: str, k: int | None = None) -> list[RetrievedChunk]: ...

    def get(self, chunk_id: str) -> Chunk | None: ...


def split_markdown(source: str, content: str) -> list[Chunk]:
    """Split one runbook into heading-delimited chunks with 1-based line ranges."""
    lines = content.splitlines()
    version = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    stem = source.rsplit(".", 1)[0]
    headings = {i: m for i, line in enumerate(lines) if (m := _HEADING.match(line))}
    h1 = next((m.group(2) for m in headings.values() if len(m.group(1)) == 1), None)
    title = h1 or stem
    starts = sorted(headings)
    if not starts or any(line.strip() for line in lines[: starts[0]]):
        starts.insert(0, 0)
    sections: list[tuple[str, int, int]] = []  # heading, start index, end index (exclusive)
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        m = headings.get(start)
        sections.append((m.group(2) if m else title, start, end))

    chunks: list[Chunk] = []
    seen: set[str] = set()
    for heading, start, end in sections:
        body = lines[start:end]
        # trim blank lines so the cited range is exactly the passage
        while body and not body[-1].strip():
            body.pop()
            end -= 1
        while body and not body[0].strip():
            body.pop(0)
            start += 1
        if not body:
            continue
        text = "\n".join(body).strip()
        slug = "overview" if heading == title else _slug(heading)
        chunk_id = f"{stem}#{slug}"
        n = 2
        while chunk_id in seen:
            chunk_id = f"{stem}#{slug}-{n}"
            n += 1
        seen.add(chunk_id)
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                source=source,
                title=title,
                heading=heading,
                text=text,
                start_line=start + 1,
                end_line=end,
                checksum=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                runbook_version=version,
            )
        )
    return chunks


class TfidfRetriever:
    def __init__(self, runbooks_dir: str | None = None):
        self.dir = runbooks_dir or settings.runbooks_dir
        self.chunks: list[Chunk] = []
        self._by_id: dict[str, Chunk] = {}
        self._vectors: list[dict[str, float]] = []
        self._idf: dict[str, float] = {}
        self.reload()

    def reload(self) -> None:
        chunks: list[Chunk] = []
        if os.path.isdir(self.dir):
            for fname in sorted(os.listdir(self.dir)):
                if fname.endswith(".md"):
                    with open(os.path.join(self.dir, fname), encoding="utf-8") as f:
                        chunks.extend(split_markdown(fname, f.read()))
        self.chunks = chunks
        self._by_id = {c.chunk_id: c for c in chunks}
        # Index the document title + heading with the body so a section such as
        # "Scale out by one replica" still matches a query about "CPU saturation".
        docs = [Counter(_tokenize(f"{c.title} {c.heading} {c.heading} {c.text}")) for c in chunks]
        df: Counter[str] = Counter()
        for d in docs:
            df.update(d.keys())
        n = max(1, len(chunks))
        self._idf = {t: math.log((n + 1) / (v + 1)) + 1 for t, v in df.items()}
        self._vectors = [self._weight(d) for d in docs]

    def _weight(self, tf: Counter[str]) -> dict[str, float]:
        vec = {t: (1 + math.log(f)) * self._idf.get(t, 0.0) for t, f in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        return {t: v / norm for t, v in vec.items()} if norm else {}

    def get(self, chunk_id: str) -> Chunk | None:
        return self._by_id.get(chunk_id)

    def retrieve(self, query: str, k: int | None = None) -> list[RetrievedChunk]:
        k = k or settings.retrieval_top_k
        q = self._weight(Counter(_tokenize(query)))
        if not q:
            return []
        scored = []
        for chunk, vec in zip(self.chunks, self._vectors, strict=True):
            score = sum(w * vec.get(t, 0.0) for t, w in q.items())
            if score >= settings.retrieval_min_score:
                scored.append(RetrievedChunk(chunk, round(score, 4)))
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:k]


rag = TfidfRetriever()


def set_retriever(r: TfidfRetriever) -> None:
    global rag
    rag = r


def get_retriever() -> TfidfRetriever:
    return rag
