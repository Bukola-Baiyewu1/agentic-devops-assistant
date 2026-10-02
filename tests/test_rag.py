"""Stage 8: heading chunks, exact line citations, threshold, and evaluation."""

import os

from scripts.eval_retrieval import evaluate, load_cases
from src.config import settings
from src.rag import TfidfRetriever, get_retriever, split_markdown


def test_chunks_carry_citation_metadata():
    r = get_retriever()
    chunk = r.get("high-error-rate#restart-after-a-recent-deploy")
    assert chunk is not None
    assert chunk.source == "high-error-rate.md"
    assert chunk.heading == "Restart after a recent deploy"
    assert len(chunk.checksum) == 16 and len(chunk.runbook_version) == 12
    assert chunk.citation()["lines"] == f"{chunk.start_line}-{chunk.end_line}"


def test_line_ranges_point_at_the_exact_passage():
    r = get_retriever()
    for chunk in r.chunks:
        with open(os.path.join(settings.runbooks_dir, chunk.source), encoding="utf-8") as f:
            lines = f.read().splitlines()
        assert "\n".join(lines[chunk.start_line - 1 : chunk.end_line]).strip() == chunk.text


def test_split_handles_files_without_headings():
    chunks = split_markdown("notes.md", "just some text\nmore text\n")
    assert [c.chunk_id for c in chunks] == ["notes#overview"]
    assert (chunks[0].start_line, chunks[0].end_line) == (1, 2)


def test_duplicate_headings_get_unique_ids():
    chunks = split_markdown("x.md", "# T\n\n## Step\na\n\n## Step\nb\n")
    assert [c.chunk_id for c in chunks] == ["x#overview", "x#step", "x#step-2"]


def test_error_query_retrieves_error_runbook():
    results = get_retriever().retrieve("5xx error rate exception after deploy")
    assert results[0].chunk.source == "high-error-rate.md"
    assert "high-error-rate#restart-after-a-recent-deploy" in [r.chunk.chunk_id for r in results]


def test_unrelated_query_returns_nothing():
    assert get_retriever().retrieve("quarterly sales report is late") == []
    assert get_retriever().retrieve("") == []


def test_evaluation_meets_targets():
    scores = evaluate(TfidfRetriever(), load_cases())
    assert scores.source_hit_at_1 >= 0.9
    assert scores.chunk_recall_at_k >= 0.9
    assert scores.unrelated_rejected == 1.0


def test_checksum_changes_when_runbook_changes(tmp_path):
    (tmp_path / "a.md").write_text("# A\n\n## Fix\nrestart_service\n")
    first = TfidfRetriever(str(tmp_path)).get("a#fix")
    (tmp_path / "a.md").write_text("# A\n\n## Fix\nscale_service\n")
    second = TfidfRetriever(str(tmp_path)).get("a#fix")
    assert first.checksum != second.checksum
    assert first.runbook_version != second.runbook_version
