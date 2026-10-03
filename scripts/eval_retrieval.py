"""Evaluate runbook retrieval against the labelled cases in evals/retrieval_cases.json.

Compares the shipped retriever (heading chunks + relevance threshold) with the
original baseline (one chunk per file, no threshold).

  python -m scripts.eval_retrieval
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from dataclasses import dataclass

from src.rag import TfidfRetriever, _tokenize

CASES = os.path.join(os.path.dirname(__file__), "..", "evals", "retrieval_cases.json")


@dataclass
class Scores:
    source_hit_at_1: float
    chunk_recall_at_k: float
    mrr: float
    unrelated_rejected: float


def load_cases() -> list[dict]:
    with open(CASES, encoding="utf-8") as f:
        return json.load(f)


def evaluate(retriever: TfidfRetriever, cases: list[dict]) -> Scores:
    hits = recall = rr = rejected = 0.0
    answerable = [c for c in cases if c["expected_source"]]
    unrelated = [c for c in cases if not c["expected_source"]]
    for case in answerable:
        results = retriever.retrieve(case["query"])
        if results and results[0].chunk.source == case["expected_source"]:
            hits += 1
        ids = [r.chunk.chunk_id for r in results]
        if any(cid in ids for cid in case["relevant_chunks"]):
            recall += 1
        for rank, cid in enumerate(ids, start=1):
            if cid in case["relevant_chunks"]:
                rr += 1 / rank
                break
    for case in unrelated:
        if not retriever.retrieve(case["query"]):
            rejected += 1
    n = max(1, len(answerable))
    return Scores(hits / n, recall / n, rr / n, rejected / max(1, len(unrelated)))


def baseline_file_level(cases: list[dict], runbooks_dir: str = "runbooks") -> Scores:
    """The original starter: whole files, raw TF-IDF, always returns something."""
    docs = {}
    for fname in sorted(os.listdir(runbooks_dir)):
        if fname.endswith(".md"):
            with open(os.path.join(runbooks_dir, fname), encoding="utf-8") as f:
                docs[fname] = Counter(_tokenize(f.read()))
    df: Counter[str] = Counter()
    for tf in docs.values():
        df.update(tf.keys())
    idf = {t: math.log((len(docs) + 1) / (v + 1)) + 1 for t, v in df.items()}

    def vec(tf: Counter[str]) -> dict[str, float]:
        return {t: f * idf.get(t, 0.0) for t, f in tf.items()}

    def cos(a: dict[str, float], b: dict[str, float]) -> float:
        num = sum(a[t] * b.get(t, 0.0) for t in a)
        da = math.sqrt(sum(v * v for v in a.values()))
        db = math.sqrt(sum(v * v for v in b.values()))
        return num / (da * db) if da and db else 0.0

    hits = rejected = 0.0
    answerable = [c for c in cases if c["expected_source"]]
    unrelated = [c for c in cases if not c["expected_source"]]
    for case in cases:
        q = vec(Counter(_tokenize(case["query"])))
        ranked = sorted(docs, key=lambda s: cos(q, vec(docs[s])), reverse=True)
        if case["expected_source"]:
            hits += ranked[0] == case["expected_source"]
        else:
            rejected += 0  # the baseline has no threshold: it always returns a runbook
    return Scores(hits / max(1, len(answerable)), float("nan"), float("nan"), rejected / max(1, len(unrelated)))


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Evaluate runbook retrieval")
    parser.add_argument(
        "--athena-url", help="also evaluate Athena (hybrid RAG) at this URL, e.g. http://localhost:8100"
    )
    args = parser.parse_args(argv)

    cases = load_cases()
    tfidf = TfidfRetriever()
    columns = {"baseline (file)": baseline_file_level(cases), "shipped (section)": evaluate(tfidf, cases)}
    if args.athena_url:
        from src.rag import AthenaRetriever

        columns["athena (hybrid)"] = evaluate(AthenaRetriever(tfidf, args.athena_url), cases)  # type: ignore[arg-type]
    print(f"{len(cases)} labelled queries ({sum(1 for c in cases if not c['expected_source'])} unrelated)\n")
    print(f"{'metric':28}" + "".join(f"{name:>19}" for name in columns))
    for field in ("source_hit_at_1", "chunk_recall_at_k", "mrr", "unrelated_rejected"):
        fmt = lambda v: "n/a" if v != v else f"{v:.2f}"  # noqa: E731 - NaN check
        print(f"{field:28}" + "".join(f"{fmt(getattr(col, field)):>19}" for col in columns.values()))


if __name__ == "__main__":
    main()
