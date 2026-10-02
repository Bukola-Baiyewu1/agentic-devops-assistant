"""The deterministic policy decides whether a model proposal may reach a human."""

from src.config import settings
from src.policy import Proposal, args_hash, validate_proposal
from src.rag import get_retriever

RESTART = "high-error-rate#restart-after-a-recent-deploy"
SCALE = "resource-saturation#scale-out-by-one-replica"
DISK = "disk-pressure#escalate-disk-pressure-to-a-human"


def chunks(*ids):
    r = get_retriever()
    return {i: r.get(i) for i in ids}


def prop(**kw):
    base = dict(
        decision="propose_action",
        reasoning="r",
        tool_name="restart_service",
        tool_args={"service": "web"},
        citation_chunk_id=RESTART,
        confidence=0.8,
    )
    base.update(kw)
    return Proposal(**base)


HEALTH = {"replicas": 1}


def check(p, retrieved=None, service="web", health=HEALTH):
    return validate_proposal(
        p, alert_service=service, retrieved=retrieved or chunks(RESTART, SCALE, DISK), health=health
    )


def test_valid_restart_passes():
    assert check(prop()) == []


def test_escalation_always_passes():
    assert check(Proposal(decision="escalate", reasoning="unsure")) == []


def test_unknown_tool_is_rejected():
    assert "not an allowed action" in check(prop(tool_name="delete_database"))[0]


def test_extra_arguments_are_rejected():
    assert check(prop(tool_args={"service": "web", "force": True})) == ["tool arguments failed the strict schema"]


def test_path_traversal_service_is_rejected():
    assert check(prop(tool_args={"service": "../../etc"})) == ["tool arguments failed the strict schema"]


def test_other_service_is_rejected():
    reasons = check(prop(tool_args={"service": "payments"}))
    assert any("different service" in r for r in reasons)
    assert any("allowed target" in r for r in reasons)


def test_missing_citation_is_rejected():
    assert "no runbook citation" in check(prop(citation_chunk_id=None))


def test_invented_citation_is_rejected():
    assert any("not one of the retrieved" in r for r in check(prop(citation_chunk_id="made-up#section")))


def test_citation_must_support_the_tool():
    assert any("does not support" in r for r in check(prop(citation_chunk_id=DISK)))


def test_scale_must_add_exactly_one_replica():
    p = prop(tool_name="scale_service", tool_args={"service": "web", "replicas": 50}, citation_chunk_id=SCALE)
    reasons = check(p)
    assert any("exactly one replica" in r for r in reasons)
    assert any("policy maximum" in r for r in reasons)
    ok = prop(tool_name="scale_service", tool_args={"service": "web", "replicas": 2}, citation_chunk_id=SCALE)
    assert check(ok) == []


def test_negative_replicas_rejected():
    p = prop(tool_name="scale_service", tool_args={"service": "web", "replicas": -1}, citation_chunk_id=SCALE)
    assert check(p) == ["tool arguments failed the strict schema"]


def test_low_confidence_is_rejected():
    assert any("below the threshold" in r for r in check(prop(confidence=0.1)))


def test_replica_limit_comes_from_settings():
    settings.max_replicas = 1
    p = prop(tool_name="scale_service", tool_args={"service": "web", "replicas": 2}, citation_chunk_id=SCALE)
    assert any("policy maximum of 1" in r for r in check(p))


def test_args_hash_is_canonical():
    assert args_hash("scale_service", {"replicas": 2, "service": "web"}) == args_hash(
        "scale_service", {"service": "web", "replicas": 2}
    )
    assert args_hash("scale_service", {"service": "web", "replicas": 2}) != args_hash(
        "scale_service", {"service": "web", "replicas": 3}
    )
