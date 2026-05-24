"""Classifier rule-ladder edge-case suite.

Each test names the rule it protects so reviewers can read the file as a
priority-ladder specification.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from aegis.classify import classify
from aegis.ingest import parse_raw_event
from aegis.normalize import normalize_event
from aegis.schemas import Agent, CanonicalEvent, Evidence, EventSource, Framework, FrameworkHint


def _evt(event_id: str, **fields) -> CanonicalEvent:
    fields.setdefault("source", "repo")
    fields.setdefault("timestamp", "2026-05-20T10:15:00Z")
    payload: dict[str, Any] = {
        "source": fields.pop("source"),
        "timestamp": fields.pop("timestamp"),
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    raw = parse_raw_event(payload)
    return normalize_event(
        raw,
        event_id=event_id,
        received_at=datetime(2026, 5, 20, 10, 30),
        content_hash=event_id,
    )


def _agent(**fields) -> Agent:
    fields.setdefault("agent_id", "agent_test")
    fields.setdefault("event_ids", [])
    return Agent(**fields)


# ---------------------------------------------------------------------------
# Rule 1 — framework imports (highest priority)
# ---------------------------------------------------------------------------

def test_langchain_import_classifies_as_langchain():
    e = _evt("e1", repo="r1", imports=["langchain", "anthropic"])
    a = _agent(imports=["langchain", "anthropic"], event_ids=["e1"])
    r = classify(a, [e])
    assert r.framework == Framework.LANGCHAIN
    assert r.confidence >= 0.9
    assert any("langchain" in ev.claim for ev in r.evidence)
    assert r.rule_name == "framework_import:langchain"


def test_crewai_import_classifies_as_crewai():
    e = _evt("e1", repo="r1", imports=["crewai"])
    a = _agent(imports=["crewai"], event_ids=["e1"])
    r = classify(a, [e])
    assert r.framework == Framework.CREWAI


def test_langgraph_beats_langchain_when_both_present():
    """LangGraph is built on LangChain — when both imports are seen, the
    more-specific framework wins, but the coexistence is recorded."""
    e = _evt("e1", repo="r1", imports=["langgraph", "langchain"])
    a = _agent(imports=["langgraph", "langchain"], event_ids=["e1"])
    r = classify(a, [e])
    assert r.framework == Framework.LANGGRAPH
    coexist = [ev for ev in r.evidence if "coexisting" in ev.claim]
    assert len(coexist) == 1
    assert "langchain" in coexist[0].claim


def test_evidence_attributes_to_source_event():
    """Each evidence item carries the event_id that produced it."""
    e1 = _evt("repo-A", repo="r1", imports=["langchain"])
    e2 = _evt("repo-B", repo="r2", imports=["langchain"])
    a = _agent(imports=["langchain"], event_ids=["repo-A", "repo-B"])
    r = classify(a, [e1, e2])
    sources = {ev.source_event_id for ev in r.evidence if ev.source_event_id}
    assert sources == {"repo-A", "repo-B"}


# ---------------------------------------------------------------------------
# Rule 2 — MCP config (capability becomes framework iff no higher-level import)
# ---------------------------------------------------------------------------

def test_mcp_config_alone_classifies_as_mcp_agent():
    e = _evt(
        "e1", repo="r1",
        config_files=[".cursor/mcp.json"], mcp_config_present=True,
    )
    a = _agent(
        mcp_config_present=True, config_files=[".cursor/mcp.json"],
        event_ids=["e1"],
    )
    r = classify(a, [e])
    assert r.framework == Framework.MCP_AGENT
    assert r.rule_name == "mcp_config_present"


def test_framework_import_beats_mcp_capability():
    """A LangChain agent that also uses MCP is still LangChain — the MCP
    config is a capability, not the primary framework."""
    e = _evt(
        "e1", repo="r1",
        imports=["langchain"], mcp_config_present=True,
        config_files=[".cursor/mcp.json"],
    )
    a = _agent(
        imports=["langchain"], mcp_config_present=True,
        config_files=[".cursor/mcp.json"], event_ids=["e1"],
    )
    r = classify(a, [e])
    assert r.framework == Framework.LANGCHAIN


# ---------------------------------------------------------------------------
# Rule 3 — explicit framework_hint (medium confidence fallback)
# ---------------------------------------------------------------------------

def test_explicit_framework_hint_used_when_no_imports():
    e = _evt(
        "e1", source="repo", repo="r1",
        framework_hint="LangChain",  # case-normalised by Phase 2
    )
    a = _agent(
        framework_alternatives=[
            FrameworkHint(
                framework=Framework.LANGCHAIN,
                source_event_id="e1",
                source=EventSource.REPO,
                confidence=0.5,
                rationale="event-level framework_hint",
            )
        ],
        event_ids=["e1"],
    )
    r = classify(a, [e])
    assert r.framework == Framework.LANGCHAIN
    assert r.rule_name == "explicit_framework_hint"
    assert r.confidence == 0.80  # weaker than import-based 0.95


def test_imports_override_explicit_hint():
    """If imports tell us crewai but the hint says langchain, imports win."""
    e = _evt(
        "e1", source="repo", repo="r1",
        framework_hint="langchain", imports=["crewai"],
    )
    a = _agent(
        imports=["crewai"],
        framework_alternatives=[
            FrameworkHint(
                framework=Framework.LANGCHAIN, source_event_id="e1",
                source=EventSource.REPO, confidence=0.5,
                rationale="event-level framework_hint",
            )
        ],
        event_ids=["e1"],
    )
    r = classify(a, [e])
    assert r.framework == Framework.CREWAI


def test_conflicting_explicit_hints_pick_most_common():
    """Two events declare langchain, one declares crewai → langchain wins,
    conflict noted in evidence."""
    a = _agent(
        framework_alternatives=[
            FrameworkHint(framework=Framework.LANGCHAIN, source_event_id="e1",
                          source=EventSource.REPO, confidence=0.5, rationale="x"),
            FrameworkHint(framework=Framework.LANGCHAIN, source_event_id="e2",
                          source=EventSource.NHI, confidence=0.5, rationale="x"),
            FrameworkHint(framework=Framework.CREWAI, source_event_id="e3",
                          source=EventSource.SAAS, confidence=0.5, rationale="x"),
        ],
    )
    r = classify(a, [])
    assert r.framework == Framework.LANGCHAIN
    assert any("conflicting" in ev.claim for ev in r.evidence)


def test_unknown_framework_hint_is_ignored():
    """A `framework_hint = unknown` (or no usable hints) doesn't trigger
    this rule — falls through to lower rules."""
    a = _agent(
        framework_alternatives=[
            FrameworkHint(framework=Framework.UNKNOWN, source_event_id="e1",
                          source=EventSource.REPO, confidence=0.0, rationale="x"),
        ],
        destinations=["api.anthropic.com"],
    )
    r = classify(a, [])
    # Falls through to LLM_CALLER (no tools)
    assert r.framework == Framework.LLM_CALLER


# ---------------------------------------------------------------------------
# Rule 4 — external LLM + tool-use → direct SDK / agentic
# ---------------------------------------------------------------------------

def test_external_llm_with_tools_classifies_as_direct_sdk_agentic():
    e = _evt(
        "rt-1", source="runtime",
        host_id="h1", pid=1234,
        destination="api.anthropic.com",
        tools_called=["aurora_read", "bedrock_invoke"],
    )
    a = _agent(
        destinations=["api.anthropic.com"],
        tools=["aurora_read", "bedrock_invoke"],
        event_ids=["rt-1"],
    )
    r = classify(a, [e])
    assert r.framework == Framework.DIRECT_SDK_AGENTIC
    assert r.confidence == 0.80


def test_bedrock_destination_recognized_as_external_llm():
    e = _evt(
        "rt-1", source="runtime",
        host_id="h1", pid=1234,
        destination="bedrock-runtime.us-east-1.amazonaws.com",
        tools_called=["claims_lookup"],
    )
    a = _agent(
        destinations=["bedrock-runtime.us-east-1.amazonaws.com"],
        tools=["claims_lookup"],
        event_ids=["rt-1"],
    )
    r = classify(a, [e])
    assert r.framework == Framework.DIRECT_SDK_AGENTIC


# ---------------------------------------------------------------------------
# Rule 5 — bare LLM caller (lowest-confidence positive match)
# ---------------------------------------------------------------------------

def test_external_llm_without_tools_classifies_as_llm_caller():
    e = _evt(
        "rt-1", source="runtime",
        host_id="h1", pid=1234,
        destination="api.openai.com",
        # no tools_called
    )
    a = _agent(
        destinations=["api.openai.com"],
        event_ids=["rt-1"],
    )
    r = classify(a, [e])
    assert r.framework == Framework.LLM_CALLER
    assert r.confidence == 0.60


# ---------------------------------------------------------------------------
# Rule 6 — no signals → UNKNOWN
# ---------------------------------------------------------------------------

def test_no_signals_returns_unknown():
    a = _agent()
    r = classify(a, [])
    assert r.framework == Framework.UNKNOWN
    assert r.confidence == 0.0
    assert r.rule_name == "no_signals"


def test_only_internal_destinations_returns_unknown():
    e = _evt(
        "rt-1", source="runtime",
        host_id="h1", pid=1234,
        destination="api.internal.corp",
    )
    a = _agent(destinations=["api.internal.corp"], event_ids=["rt-1"])
    r = classify(a, [e])
    assert r.framework == Framework.UNKNOWN


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_classify_is_deterministic():
    e = _evt("e1", repo="r1", imports=["langchain"])
    a = _agent(imports=["langchain"], event_ids=["e1"])
    r1 = classify(a, [e])
    r2 = classify(a, [e])
    assert r1.framework == r2.framework
    assert r1.confidence == r2.confidence
    assert [(ev.claim, ev.source_event_id) for ev in r1.evidence] == \
           [(ev.claim, ev.source_event_id) for ev in r2.evidence]


# ---------------------------------------------------------------------------
# API + end-to-end
# ---------------------------------------------------------------------------

def _shadow_phi_payloads():
    return [
        {"event_id": "rt-1", "source": "runtime",
         "timestamp": "2026-05-20T10:15:30Z",
         "host_id": "host-prod-01", "pid": 4242,
         "workload_id": "claims-processor",
         "destination": "api.anthropic.com",
         "tools_called": ["aurora_read"]},
        {"event_id": "nhi-1", "source": "nhi",
         "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-shadow", "workload_id": "claims-processor"},
        {"event_id": "repo-1", "source": "repo",
         "timestamp": "2026-05-20T09:00:00Z",
         "repo": "claims-processor", "workload_id": "claims-processor",
         "imports": ["langchain", "anthropic"],
         "framework_hint": "langchain", "has_aegislib": False},
        {"event_id": "saas-1", "source": "saas",
         "timestamp": "2026-05-20T10:16:00Z",
         "nhi_id": "role-shadow", "data_classes": ["PHI"]},
    ]


def test_classify_endpoint_labels_existing_agents(client):
    client.post("/events/batch", json=_shadow_phi_payloads())
    client.post("/pipeline/correlate")
    r = client.post("/pipeline/classify")
    assert r.status_code == 200
    assert r.json()["classified"] == 1
    agent = client.get("/agents").json()[0]
    assert agent["framework"] == "langchain"
    assert agent["framework_confidence"] == 0.95
    assert agent["framework_rule"] == "framework_import:langchain"


def test_classify_endpoint_is_idempotent(client):
    client.post("/events/batch", json=_shadow_phi_payloads())
    client.post("/pipeline/correlate")
    r1 = client.post("/pipeline/classify").json()
    r2 = client.post("/pipeline/classify").json()
    assert r1 == r2
    agents_1 = client.get("/agents").json()
    agents_2 = client.get("/agents").json()
    assert [a["framework"] for a in agents_1] == [a["framework"] for a in agents_2]


def test_classify_persists_evidence_chain(client):
    client.post("/events/batch", json=_shadow_phi_payloads())
    client.post("/pipeline/correlate")
    client.post("/pipeline/classify")
    agent_id = client.get("/agents").json()[0]["agent_id"]
    detail = client.get(f"/agents/{agent_id}").json()
    fw_evidence = detail["agent"]["framework_evidence"]
    assert len(fw_evidence) >= 1
    assert any("langchain" in ev["claim"] for ev in fw_evidence)
    assert any(ev["source_event_id"] == "repo-1" for ev in fw_evidence)


def test_classify_runtime_only_agent_as_direct_sdk(client):
    """A runtime-only signal (no repo data) classifies via Rule 4."""
    client.post("/events", json={
        "event_id": "rt-only", "source": "runtime",
        "timestamp": "2026-05-20T10:15:30Z",
        "host_id": "h-prod", "pid": 1234,
        "destination": "api.anthropic.com",
        "tools_called": ["claims_lookup"],
    })
    client.post("/pipeline/correlate")
    client.post("/pipeline/classify")
    agent = client.get("/agents").json()[0]
    assert agent["framework"] == "direct_sdk_agentic"
    assert agent["framework_rule"] == "external_llm_with_tool_use"


def test_classify_preserves_other_agent_fields(client):
    """Classifying must not clobber correlation data."""
    client.post("/events/batch", json=_shadow_phi_payloads())
    client.post("/pipeline/correlate")
    before = client.get("/agents").json()[0]
    client.post("/pipeline/classify")
    after = client.get("/agents").json()[0]
    assert before["agent_id"] == after["agent_id"]
    assert before["nhi_id"] == after["nhi_id"]
    assert before["data_classes"] == after["data_classes"]
    assert before["correlation_confidence"] == after["correlation_confidence"]
    assert before["event_count"] == after["event_count"]
