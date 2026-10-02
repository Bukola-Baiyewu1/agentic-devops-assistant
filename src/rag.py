"""Retrieval over your runbooks, with source citations.

Uses a small, dependency-free TF-IDF cosine retriever so the project runs
offline with zero model downloads. The interface (`retrieve`) is what matters:
to upgrade to real embeddings + pgvector, you replace only the internals of
this file — nothing else in the codebase changes.
"""
import math
import os
import re
from collections import Counter
from typing import List, Dict

from . import config

_WORD = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _WORD.findall(text.lower())


class RunbookStore:
    def __init__(self, runbooks_dir: str = None):
        self.dir = runbooks_dir or config.RUNBOOKS_DIR
        self.chunks: List[Dict] = []      # {text, source}
        self._tf: List[Counter] = []
        self._idf: Dict[str, float] = {}
        self.reload()

    def reload(self):
        self.chunks = []
        if os.path.isdir(self.dir):
            for fname in sorted(os.listdir(self.dir)):
                if not fname.endswith(".md"):
                    continue
                path = os.path.join(self.dir, fname)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                # One chunk per file keeps citations clean and readable.
                self.chunks.append({"text": content.strip(), "source": fname})
        self._build_index()

    def _build_index(self):
        self._tf = [Counter(_tokenize(c["text"])) for c in self.chunks]
        df = Counter()
        for tf in self._tf:
            for term in tf:
                df[term] += 1
        n = max(1, len(self.chunks))
        self._idf = {t: math.log((n + 1) / (df_t + 1)) + 1 for t, df_t in df.items()}

    def _vec(self, tf: Counter) -> Dict[str, float]:
        return {t: freq * self._idf.get(t, 0.0) for t, freq in tf.items()}

    @staticmethod
    def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
        common = set(a) & set(b)
        num = sum(a[t] * b[t] for t in common)
        da = math.sqrt(sum(v * v for v in a.values()))
        db = math.sqrt(sum(v * v for v in b.values()))
        return num / (da * db) if da and db else 0.0

    def retrieve(self, query: str, k: int = 2) -> List[Dict]:
        """Return the k most relevant runbook chunks, each with its source."""
        if not self.chunks:
            return []
        qvec = self._vec(Counter(_tokenize(query)))
        scored = []
        for chunk, tf in zip(self.chunks, self._tf):
            score = self._cosine(qvec, self._vec(tf))
            scored.append({"text": chunk["text"], "source": chunk["source"], "score": round(score, 4)})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:k]


rag = RunbookStore()
