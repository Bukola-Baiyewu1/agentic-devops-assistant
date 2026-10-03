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


# ---- Athena (hybrid RAG service) adapter ------------------------------------
def _athena_client(handler):
    import httpx

    return httpx.Client(transport=httpx.MockTransport(handler))


def _athena_body(relevant=True):
    return {
        "relevant": relevant,
        "results": [
            {
                "chunk_id": "high-error-rate#restart-after-a-recent-deploy",
                "source": "high-error-rate.md",
                "heading": "Restart after a recent deploy",
                "lines": "10-14",
                "text": "## Restart after a recent deploy\nrestart the service with restart_service.",
                "checksum": "abc123",
                "score": 0.9731,
            }
        ],
    }


def test_athena_retriever_maps_results_to_citable_chunks():
    import json

    from src.rag import AthenaRetriever, TfidfRetriever

    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        import httpx

        return httpx.Response(200, json=_athena_body())

    r = AthenaRetriever(TfidfRetriever(), "http://athena:8100", client=_athena_client(handler))
    results = r.retrieve("5xx after deploy", 3)
    assert seen == {"query": "5xx after deploy", "top_k": 3, "mode": "hybrid_rerank", "strategy": "headers"}
    chunk = results[0].chunk
    assert chunk.chunk_id == "high-error-rate#restart-after-a-recent-deploy"
    assert (chunk.start_line, chunk.end_line) == (10, 14) and chunk.runbook_version == "athena"
    assert results[0].score == 0.9731
    assert r.get(chunk.chunk_id) is chunk  # the approval page can show the cited text


def test_athena_irrelevant_result_means_escalate():
    import httpx

    from src.rag import AthenaRetriever, TfidfRetriever

    r = AthenaRetriever(
        TfidfRetriever(), client=_athena_client(lambda req: httpx.Response(200, json=_athena_body(False)))
    )
    assert r.retrieve("what is the capital of Finland") == []


def test_athena_outage_falls_back_to_tfidf_and_is_counted():
    import httpx

    from src.observability import RETRIEVER_FALLBACKS
    from src.rag import AthenaRetriever, TfidfRetriever

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    before = RETRIEVER_FALLBACKS._value.get()
    r = AthenaRetriever(TfidfRetriever(), client=_athena_client(down))
    results = r.retrieve("5xx error rate exception after deploy")
    assert results and results[0].chunk.source == "high-error-rate.md"
    assert RETRIEVER_FALLBACKS._value.get() == before + 1
    bad = AthenaRetriever(TfidfRetriever(), client=_athena_client(lambda req: httpx.Response(200, json={"oops": 1})))
    assert bad.retrieve("5xx error rate exception after deploy")[0].chunk.source == "high-error-rate.md"


def test_agent_plans_with_athena_and_policy_still_applies():
    import httpx

    from src import agent
    from src.demo import sim
    from src.rag import AthenaRetriever, TfidfRetriever, get_retriever, set_retriever

    original = get_retriever()
    set_retriever(
        AthenaRetriever(TfidfRetriever(), client=_athena_client(lambda req: httpx.Response(200, json=_athena_body())))
    )
    try:
        sim.inject_error()
        result = agent.plan(
            {"event_id": "e", "name": "High 5xx error rate", "description": "500s after deploy", "service": "web"}
        )
        assert result["proposal"]["tool_name"] == "restart_service"
        assert result["citation"]["chunk_id"] == "high-error-rate#restart-after-a-recent-deploy"
        assert result["citation"]["runbook_version"] == "athena"
    finally:
        set_retriever(original)
