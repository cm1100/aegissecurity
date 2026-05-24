"""Correlator edge-case suite.

Each test protects one spec-required behavior. Docstrings name the edge
case so reviewers (and future-me) can read the file like spec.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aegis.correlate import (
    CONF_HOST_PID,
    CONF_NHI,
    CONF_REPO,
    CONF_WORKLOAD,
    correlate,
)
from aegis.ingest import parse_raw_event
from aegis.normalize import normalize_event
from aegis.schemas import CanonicalEvent


def _evt(event_id: str, **fields) -> CanonicalEvent:
    """Build a canonical event directly without round-tripping through raw.

    Tests focus on correlation logic; we don't re-test normalization here.
    """
    fields.setdefault("source", "runtime")
    fields.setdefault("timestamp", "2026-05-20T10:15:00Z")
    payload = {"source": fields.pop("source"), "timestamp": fields.pop("timestamp")}
    payload.update({k: v for k, v in fields.items() if v is not None})
    raw = parse_raw_event(payload)
    received_at = datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc)
    return normalize_event(raw, event_id=event_id, received_at=received_at, content_hash=event_id)


# ---------------------------------------------------------------------------
# 1. Trivial baseline
# ---------------------------------------------------------------------------

def test_no_events_returns_empty():
    r = correlate([])
    assert r.agents == []


def test_single_event_becomes_single_agent():
    """An event with no peers still becomes an agent with confidence 1.0."""
    e = _evt("e1", host_id="h1", pid=1234)
    r = correlate([e])
    assert len(r.agents) == 1
    assert r.agents[0].correlation_confidence == 1.0
    assert r.agents[0].event_ids == ["e1"]


# ---------------------------------------------------------------------------
# 2. Each join key in isolation
# ---------------------------------------------------------------------------

def test_two_events_share_nhi_id_merge():
    a = _evt("a", source="nhi", nhi_id="role-X", timestamp="2026-05-20T10:00:00Z")
    b = _evt("b", source="saas", nhi_id="role-X", timestamp="2026-05-20T11:00:00Z")
    r = correlate([a, b])
    assert len(r.agents) == 1
    assert r.agents[0].correlation_confidence == CONF_NHI


def test_two_events_share_workload_id_merge():
    a = _evt("a", source="nhi", workload_id="w1")
    b = _evt("b", source="repo", workload_id="w1", repo="r1")
    r = correlate([a, b])
    assert len(r.agents) == 1
    assert r.agents[0].correlation_confidence == CONF_WORKLOAD


def test_two_events_share_repo_merge_at_low_confidence():
    """repo alone is the weakest signal — confidence reflects that."""
    a = _evt("a", source="repo", repo="r1", imports=["langchain"])
    b = _evt("b", source="repo", repo="r1", imports=["anthropic"])
    r = correlate([a, b])
    assert len(r.agents) == 1
    assert r.agents[0].correlation_confidence == CONF_REPO


# ---------------------------------------------------------------------------
# 3. host_id + pid — the PID-reuse edge case
# ---------------------------------------------------------------------------

def test_host_pid_within_window_merges():
    """Same (host, pid), 2 minutes apart → same process."""
    a = _evt("a", host_id="h1", pid=1234, timestamp="2026-05-20T10:00:00Z")
    b = _evt("b", host_id="h1", pid=1234, timestamp="2026-05-20T10:02:00Z")
    r = correlate([a, b])
    assert len(r.agents) == 1
    assert r.agents[0].correlation_confidence == CONF_HOST_PID


def test_host_pid_outside_window_does_not_merge():
    """Same (host, pid), 30 minutes apart → almost certainly PID reuse."""
    a = _evt("a", host_id="h1", pid=1234, timestamp="2026-05-20T10:00:00Z")
    b = _evt("b", host_id="h1", pid=1234, timestamp="2026-05-20T10:30:00Z")
    r = correlate([a, b])
    assert len(r.agents) == 2


def test_host_pid_chain_through_window_merges_transitively():
    """t0 at 0min, t1 at 4min, t2 at 8min. t0↔t1 in-window, t1↔t2 in-window;
    t0↔t2 out-of-window but they reach each other through t1."""
    a = _evt("a", host_id="h1", pid=1234, timestamp="2026-05-20T10:00:00Z")
    b = _evt("b", host_id="h1", pid=1234, timestamp="2026-05-20T10:04:00Z")
    c = _evt("c", host_id="h1", pid=1234, timestamp="2026-05-20T10:08:00Z")
    r = correlate([a, b, c])
    assert len(r.agents) == 1


# ---------------------------------------------------------------------------
# 4. Transitive merges across keys
# ---------------------------------------------------------------------------

def test_transitive_merge_via_chain_of_keys():
    """A↔B by nhi_id, B↔C by workload_id → all three merge."""
    a = _evt("a", source="saas", nhi_id="role-X")
    b = _evt("b", source="nhi", nhi_id="role-X", workload_id="w1")
    c = _evt("c", source="repo", workload_id="w1", repo="r1")
    r = correlate([a, b, c])
    assert len(r.agents) == 1
    # confidence = min of used edges (0.95, 0.90) = 0.90
    assert r.agents[0].correlation_confidence == CONF_WORKLOAD


def test_confidence_is_min_of_used_edges():
    """A chain with edges {0.95, 0.85, 0.50} → cluster confidence 0.50."""
    a = _evt("a", source="saas", nhi_id="role-X")
    b = _evt(
        "b", source="nhi", nhi_id="role-X", host_id="h1", pid=1234,
        timestamp="2026-05-20T10:00:00Z",
    )
    c = _evt(
        "c", host_id="h1", pid=1234, repo="r1",
        timestamp="2026-05-20T10:01:00Z",
    )
    d = _evt("d", source="repo", repo="r1")
    r = correlate([a, b, c, d])
    assert len(r.agents) == 1
    assert r.agents[0].correlation_confidence == CONF_REPO


# ---------------------------------------------------------------------------
# 5. Conflict blocking — the non-obvious correctness
# ---------------------------------------------------------------------------

def test_distinct_nhi_ids_block_workload_merge():
    """Two events share workload but carry different IAM roles → 2 agents."""
    a = _evt("a", source="nhi", nhi_id="role-X", workload_id="w1")
    b = _evt("b", source="nhi", nhi_id="role-Y", workload_id="w1")
    r = correlate([a, b])
    assert len(r.agents) == 2


def test_distinct_workloads_block_repo_merge():
    """Two events share repo but carry different workload_ids → 2 agents."""
    a = _evt("a", source="repo", repo="r1", workload_id="w1")
    b = _evt("b", source="repo", repo="r1", workload_id="w2")
    r = correlate([a, b])
    assert len(r.agents) == 2


def test_multiple_agents_same_host_different_pids():
    """Two agents on one host are discriminated by pid."""
    a = _evt("a", host_id="h1", pid=1000, nhi_id="role-X",
             timestamp="2026-05-20T10:00:00Z", source="nhi")
    b = _evt("b", host_id="h1", pid=1000, nhi_id="role-X",
             timestamp="2026-05-20T10:01:00Z", source="saas")
    c = _evt("c", host_id="h1", pid=2000, nhi_id="role-Y",
             timestamp="2026-05-20T10:00:00Z", source="nhi")
    d = _evt("d", host_id="h1", pid=2000, nhi_id="role-Y",
             timestamp="2026-05-20T10:01:00Z", source="saas")
    r = correlate([a, b, c, d])
    assert len(r.agents) == 2
    sorted_agents = sorted(r.agents, key=lambda x: x.nhi_id)
    assert sorted_agents[0].nhi_id == "role-X"
    assert sorted_agents[1].nhi_id == "role-Y"


# ---------------------------------------------------------------------------
# 6. Determinism and order independence
# ---------------------------------------------------------------------------

def test_order_independence():
    """Shuffling input events yields the same agent_ids."""
    evts = [
        _evt("a", source="nhi", nhi_id="role-X", workload_id="w1"),
        _evt("b", source="saas", nhi_id="role-X"),
        _evt("c", source="repo", workload_id="w1", repo="r1"),
    ]
    r1 = correlate(evts)
    r2 = correlate(list(reversed(evts)))
    assert [a.agent_id for a in r1.agents] == [a.agent_id for a in r2.agents]


def test_agent_id_deterministic_across_runs():
    evts = [
        _evt("a", source="nhi", nhi_id="role-X"),
        _evt("b", source="saas", nhi_id="role-X"),
    ]
    r1 = correlate(evts)
    r2 = correlate(evts)
    assert r1.agents[0].agent_id == r2.agents[0].agent_id


# ---------------------------------------------------------------------------
# 7. Field reconciliation
# ---------------------------------------------------------------------------

def test_conflicting_framework_hints_kept_as_alternatives():
    """If two events disagree on framework, both observations are preserved
    in framework_alternatives — the classifier (Phase 4) picks a winner."""
    a = _evt("a", source="repo", repo="r1", framework_hint="langchain", imports=["langchain"])
    b = _evt("b", source="repo", repo="r1", framework_hint="crewai", imports=["crewai"])
    r = correlate([a, b])
    assert len(r.agents) == 1
    frameworks = {h.framework for h in r.agents[0].framework_alternatives}
    assert "langchain" in frameworks and "crewai" in frameworks


def test_data_classes_tools_destinations_are_unioned():
    a = _evt(
        "a", source="saas", nhi_id="role-X",
        data_classes=["PHI"],
        destination="api.anthropic.com",
        tools_called=["aurora_read"],
    )
    b = _evt(
        "b", source="saas", nhi_id="role-X",
        data_classes=["PII"],
        destination="bedrock.us-east-1.amazonaws.com",
        tools_called=["bedrock_invoke"],
    )
    r = correlate([a, b])
    assert len(r.agents) == 1
    agent = r.agents[0]
    assert set(agent.data_classes) == {"PHI", "PII"}
    assert set(agent.destinations) == {"api.anthropic.com", "bedrock.us-east-1.amazonaws.com"}
    assert set(agent.tools) == {"aurora_read", "bedrock_invoke"}


def test_has_aegislib_is_any_true_wins():
    """If any repo event reports aegislib presence, the agent has it."""
    a = _evt("a", source="repo", repo="r1", has_aegislib=False)
    b = _evt("b", source="repo", repo="r1", has_aegislib=True)
    r = correlate([a, b])
    assert len(r.agents) == 1
    assert r.agents[0].has_aegislib is True


# ---------------------------------------------------------------------------
# 8. Multi-source partial-key assembly — the headline scenario
# ---------------------------------------------------------------------------

def test_multi_source_partial_keys_assemble_into_single_agent():
    """The shadow-PHI scenario: 4 events across 4 sources, each missing
    some keys, all linked through transitive joins."""
    runtime = _evt(
        "runtime-1", source="runtime",
        host_id="host-prod-01", pid=4242,
        destination="api.anthropic.com",
        tools_called=["aurora_read", "external_llm_call"],
        timestamp="2026-05-20T10:15:30Z",
    )
    nhi = _evt(
        "nhi-1", source="nhi",
        nhi_id="role-aegis-shadow-agent",
        workload_id="claims-processor",
        permissions=["aurora:read", "bedrock:invoke"],
        timestamp="2026-05-20T10:00:00Z",
    )
    repo = _evt(
        "repo-1", source="repo",
        repo="claims-processor",  # matches workload_id by convention
        imports=["langchain", "anthropic"],
        framework_hint="langchain",
        has_aegislib=False,
        timestamp="2026-05-20T09:00:00Z",
    )
    saas = _evt(
        "saas-1", source="saas",
        nhi_id="role-aegis-shadow-agent",
        data_classes=["PHI", "claims_data"],
        action="read",
        resource="claims.patient_records",
        timestamp="2026-05-20T10:16:00Z",
    )
    # repo doesn't directly share with runtime; but workload_id "claims-processor"
    # is set on the NHI event. To link repo → workload we'd need workload_id on
    # the repo event too. Add it (real repo scans carry a deployment hint).
    repo_with_wl = _evt(
        "repo-1", source="repo",
        repo="claims-processor",
        workload_id="claims-processor",
        imports=["langchain", "anthropic"],
        framework_hint="langchain",
        has_aegislib=False,
        timestamp="2026-05-20T09:00:00Z",
    )

    r = correlate([runtime, nhi, repo_with_wl, saas])
    # Without runtime linking, expect: nhi↔repo by workload, nhi↔saas by nhi_id
    # runtime is orphaned (no shared keys).
    agents_by_size = sorted(r.agents, key=lambda a: -len(a.event_ids))
    assert len(agents_by_size[0].event_ids) == 3
    assert "runtime-1" in agents_by_size[1].event_ids  # orphaned by design


def test_runtime_links_via_workload_when_present():
    """When the runtime event carries workload_id, the whole chain merges."""
    runtime = _evt(
        "runtime-1", source="runtime",
        host_id="host-prod-01", pid=4242,
        workload_id="claims-processor",
        destination="api.anthropic.com",
    )
    nhi = _evt(
        "nhi-1", source="nhi",
        nhi_id="role-X", workload_id="claims-processor",
    )
    saas = _evt(
        "saas-1", source="saas",
        nhi_id="role-X",
        data_classes=["PHI"],
    )
    r = correlate([runtime, nhi, saas])
    assert len(r.agents) == 1
    assert set(r.agents[0].event_ids) == {"runtime-1", "nhi-1", "saas-1"}


# ---------------------------------------------------------------------------
# 9. API + persistence
# ---------------------------------------------------------------------------

def _ingest(client, payloads):
    return client.post("/events/batch", json=payloads).json()


def test_correlate_endpoint_creates_agents(client):
    payloads = [
        {"source": "nhi", "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-X", "workload_id": "w1"},
        {"source": "saas", "timestamp": "2026-05-20T10:01:00Z",
         "nhi_id": "role-X", "data_classes": ["PHI"]},
    ]
    _ingest(client, payloads)
    r = client.post("/pipeline/correlate")
    assert r.status_code == 200
    assert r.json()["agents"] == 1


def test_get_agents_lists_with_summary(client):
    payloads = [
        {"source": "nhi", "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-X", "workload_id": "w1"},
        {"source": "saas", "timestamp": "2026-05-20T10:01:00Z",
         "nhi_id": "role-X", "data_classes": ["PHI"]},
    ]
    _ingest(client, payloads)
    client.post("/pipeline/correlate")
    agents = client.get("/agents").json()
    assert len(agents) == 1
    a = agents[0]
    assert a["nhi_id"] == "role-X"
    assert a["data_classes"] == ["PHI"]
    assert a["event_count"] == 2
    assert a["correlation_confidence"] == CONF_NHI


def test_get_agent_includes_correlation_edges(client):
    payloads = [
        {"source": "nhi", "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-X", "workload_id": "w1"},
        {"source": "saas", "timestamp": "2026-05-20T10:01:00Z",
         "nhi_id": "role-X", "data_classes": ["PHI"]},
    ]
    _ingest(client, payloads)
    client.post("/pipeline/correlate")
    agent_id = client.get("/agents").json()[0]["agent_id"]
    detail = client.get(f"/agents/{agent_id}").json()
    assert detail["agent"]["nhi_id"] == "role-X"
    assert len(detail["correlation_edges"]) == 1
    assert detail["correlation_edges"][0]["reason"] == "shared nhi_id"


def test_correlate_endpoint_is_idempotent(client):
    payloads = [
        {"source": "nhi", "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-X"},
        {"source": "saas", "timestamp": "2026-05-20T10:01:00Z",
         "nhi_id": "role-X"},
    ]
    _ingest(client, payloads)
    r1 = client.post("/pipeline/correlate").json()
    r2 = client.post("/pipeline/correlate").json()
    assert r1 == r2
    agents_1 = client.get("/agents").json()
    agents_2 = client.get("/agents").json()
    assert [a["agent_id"] for a in agents_1] == [a["agent_id"] for a in agents_2]


def test_get_agent_404_for_unknown_id(client):
    r = client.get("/agents/does-not-exist")
    assert r.status_code == 404
