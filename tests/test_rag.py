from src.rag import rag


def test_error_query_retrieves_error_runbook():
    results = rag.retrieve("5xx error rate exception after deploy")
    assert results, "expected at least one runbook"
    assert results[0]["source"] == "high-error-rate.md"
    # every result carries a citable source
    assert all("source" in r for r in results)


def test_saturation_query_retrieves_saturation_runbook():
    results = rag.retrieve("high cpu memory saturation latency")
    assert results[0]["source"] == "resource-saturation.md"
