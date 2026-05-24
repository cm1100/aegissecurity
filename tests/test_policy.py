"""Policy recommender suite.

Decision-tree branches are tested in isolation; evidence-chain shape and
confidence formula are tested explicitly; the spec's example (3 evidence
items → confidence 0.91) is reproduced by `test_spec_example_confidence`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from aegis.ingest import parse_raw_event
from aegis.normalize import normalize_event
from aegis.policy import (
    POLICY_AUDIT,
    POLICY_EGRESS,
    POLICY_PHI,
    recommend_policy,
)
from aegis.schemas import Agent, CanonicalEvent, Framework


def _evt(event_id: str, **fields) -> CanonicalEvent:
    fields.setdefault("source", "saas")
    fields.setdefault("timestamp", "2026-05-20T10:15:00Z")
    payload: dict[str, Any] = {
        "source": fields.pop("source"),
        "timestamp": fields.pop("timestamp"),
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    raw = parse_raw_event(payload)
    return normalize_event(raw, event_id=event_id,
                           received_at=datetime(2026, 5, 20, 10, 30),
                           content_hash=event_id)


def _agent(**fields) -> Agent:
    fields.setdefault("agent_id", "agent_test")
    fields.setdefault("event_ids", [])
    return Agent(**fields)


# ---------------------------------------------------------------------------
# Decision tree — branch coverage
# ---------------------------------------------------------------------------

def test_phi_plus_external_llm_yields_phi_handling_v3():
    a = _agent(data_classes=["PHI"], destinations=["api.anthropic.com"])
    r = recommend_policy(a)
    assert r is not None
    assert r.policy == POLICY_PHI


def test_external_llm_only_yields_egress_redact():
    a = _agent(data_classes=["PII"], destinations=["api.openai.com"])
    r = recommend_policy(a)
    assert r.policy == POLICY_EGRESS


def test_internal_llm_agent_yields_audit_all():
    """LangChain agent with no external destination still needs audit logging."""
    a = _agent(
        framework=Framework.LANGCHAIN,
        destinations=["api.internal.corp/llm"],
        has_aegislib=False,
    )
    r = recommend_policy(a)
    assert r.policy == POLICY_AUDIT


def test_no_llm_signal_returns_none():
    a = _agent(framework=Framework.UNKNOWN, destinations=["api.internal.corp"])
    r = recommend_policy(a)
    assert r is None


def test_phi_without_external_destination_returns_none():
    """PHI alone doesn't trigger any spec policy — we don't recommend what we
    can't justify."""
    a = _agent(data_classes=["PHI"], destinations=["api.internal.corp"])
    r = recommend_policy(a)
    assert r is None


# ---------------------------------------------------------------------------
# Evidence chain — shape and content
# ---------------------------------------------------------------------------

def test_phi_evidence_chain_matches_spec_format():
    """Spec example shows three core claims — Agent accessed PHI, Agent called
    external LLM provider, Agent does not use aegislib. We must produce these."""
    a = _agent(data_classes=["PHI"], destinations=["api.anthropic.com"],
               has_aegislib=False)
    r = recommend_policy(a)
    claims = [ev.claim for ev in r.evidence]
    assert any("PHI" in c for c in claims)
    assert any("external llm" in c.lower() for c in claims)
    assert any("aegislib" in c for c in claims)


def test_evidence_attributes_to_source_events():
    a = _agent(
        data_classes=["PHI"], destinations=["api.anthropic.com"],
        event_ids=["saas-1", "rt-1"],
    )
    e1 = _evt("saas-1", source="saas", data_classes=["PHI"],
              resource="claims.patient_records")
    e2 = _evt("rt-1", source="runtime", destination="api.anthropic.com",
              host_id="h1", pid=1234)
    r = recommend_policy(a, [e1, e2])
    sources = {ev.source_event_id for ev in r.evidence if ev.source_event_id}
    assert "saas-1" in sources
    assert "rt-1" in sources


def test_alternatives_considered_populated():
    a = _agent(data_classes=["PHI"], destinations=["api.anthropic.com"])
    r = recommend_policy(a)
    assert POLICY_EGRESS in r.alternatives_considered
    assert POLICY_AUDIT in r.alternatives_considered


def test_evidence_incorporates_related_findings():
    a = _agent(data_classes=["PHI"], destinations=["api.anthropic.com"])
    findings = [
        {"rule_id": "phi_to_external_llm"},
        {"rule_id": "missing_aegislib"},
    ]
    r = recommend_policy(a, findings=findings)
    claims = [ev.claim for ev in r.evidence]
    assert any("phi_to_external_llm" in c for c in claims)
    assert any("missing_aegislib" in c for c in claims)


# ---------------------------------------------------------------------------
# Confidence formula
# ---------------------------------------------------------------------------

def test_evidence_summary_mirrors_spec_example_shape():
    """The spec example shows `evidence` as a flat list of strings.
    Our `evidence` is structured (richer); `evidence_summary` mirrors
    the spec example shape so consumers comparing literal output see a
    matching list of claims."""
    a = _agent(
        data_classes=["PHI"], destinations=["api.anthropic.com"],
        has_aegislib=False,
    )
    r = recommend_policy(a, events=[], findings=[])
    payload = r.model_dump(mode="json")

    # Spec-shape parity field
    assert "evidence_summary" in payload
    assert payload["evidence_summary"] == [
        "Agent accessed PHI",
        "Agent called external LLM provider: ['api.anthropic.com']",
        "Agent does not use aegislib",
    ]

    # Structured form remains
    assert len(payload["evidence"]) == 3
    assert payload["evidence"][0]["claim"] == "Agent accessed PHI"

    # Derived field stays in sync — adding to evidence updates the summary
    r2 = recommend_policy(a, findings=[{"rule_id": "phi_to_external_llm"}])
    p2 = r2.model_dump(mode="json")
    assert p2["evidence_summary"][-1] == p2["evidence"][-1]["claim"]


def test_spec_example_confidence_reproduces_0_91():
    """Spec example shows confidence=0.91 for phi-handling with 3 evidence items.
    Our formula: 0.85 base + 0.02 · |evidence| → 0.85 + 0.06 = 0.91."""
    a = _agent(
        data_classes=["PHI"], destinations=["api.anthropic.com"],
        has_aegislib=False,
        # no event attribution → only the 3 spec-style claims
    )
    r = recommend_policy(a, events=[], findings=[])
    assert len(r.evidence) == 3
    assert r.confidence == 0.91


def test_confidence_grows_with_evidence():
    """More evidence items → higher confidence (capped at 0.98)."""
    a = _agent(
        data_classes=["PHI"], destinations=["api.anthropic.com"],
        event_ids=["e1", "e2", "e3", "e4", "e5"],
    )
    events = [
        _evt(f"e{i}", source="saas", data_classes=["PHI"],
             resource=f"r{i}") for i in range(1, 6)
    ]
    r = recommend_policy(a, events)
    assert r.confidence > 0.91


def test_confidence_caps_at_0_98():
    """Even with mountains of evidence, confidence doesn't exceed 0.98."""
    a = _agent(
        data_classes=["PHI"], destinations=["api.anthropic.com"],
        has_aegislib=False,
    )
    findings = [{"rule_id": "phi_to_external_llm"}] * 50  # huge fake evidence
    r = recommend_policy(a, findings=findings)
    assert r.confidence <= 0.98


# ---------------------------------------------------------------------------
# End-to-end through API
# ---------------------------------------------------------------------------

def _shadow_phi_payloads():
    return [
        {"event_id": "rt-1", "source": "runtime",
         "timestamp": "2026-05-20T10:15:30Z",
         "host_id": "host-prod-01", "pid": 4242,
         "workload_id": "claims-processor",
         "destination": "api.anthropic.com",
         "tools_called": ["aurora_read", "external_llm_call"]},
        {"event_id": "nhi-1", "source": "nhi",
         "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-shadow", "workload_id": "claims-processor"},
        {"event_id": "repo-1", "source": "repo",
         "timestamp": "2026-05-20T09:00:00Z",
         "repo": "claims-processor", "workload_id": "claims-processor",
         "imports": ["langchain", "anthropic"], "has_aegislib": False},
        {"event_id": "saas-1", "source": "saas",
         "timestamp": "2026-05-20T10:16:00Z",
         "nhi_id": "role-shadow",
         "data_classes": ["PHI"], "action": "read",
         "resource": "claims.patient_records"},
    ]


def test_pipeline_run_endpoint_produces_full_result(client):
    """The /pipeline/run endpoint chains normalize → correlate → classify
    → risk → policy in one call. End-to-end smoke."""
    client.post("/events/batch", json=_shadow_phi_payloads())
    r = client.post("/pipeline/run")
    assert r.status_code == 200
    body = r.json()
    assert body["classified"] == 1
    assert body["policy"]["agents_recommended"] == 1
    assert body["policy"]["by_policy"] == {POLICY_PHI: 1}


def test_get_agents_surfaces_policy_summary(client):
    client.post("/events/batch", json=_shadow_phi_payloads())
    client.post("/pipeline/run")
    agent = client.get("/agents").json()[0]
    assert agent["recommended_policy"] == POLICY_PHI
    assert 0.85 <= agent["policy_confidence"] <= 0.98


def test_get_agent_detail_returns_full_policy_with_evidence(client):
    client.post("/events/batch", json=_shadow_phi_payloads())
    client.post("/pipeline/run")
    aid = client.get("/agents").json()[0]["agent_id"]
    detail = client.get(f"/agents/{aid}").json()
    rec = detail["agent"]["recommended_policy"]
    assert rec["policy"] == POLICY_PHI
    assert len(rec["evidence"]) >= 3
    claims = [e["claim"] for e in rec["evidence"]]
    assert any("PHI" in c for c in claims)
    assert any("aegislib" in c for c in claims)
    assert "external-egress-redact" in rec["alternatives_considered"]


def test_policy_is_idempotent(client):
    client.post("/events/batch", json=_shadow_phi_payloads())
    client.post("/pipeline/run")
    a1 = client.get("/agents").json()[0]
    aid = a1["agent_id"]
    detail1 = client.get(f"/agents/{aid}").json()

    client.post("/pipeline/policy")
    client.post("/pipeline/policy")
    a2 = client.get("/agents").json()[0]
    detail2 = client.get(f"/agents/{aid}").json()

    assert a1["recommended_policy"] == a2["recommended_policy"]
    assert a1["policy_confidence"] == a2["policy_confidence"]
    assert detail1["agent"]["recommended_policy"]["evidence"] == \
           detail2["agent"]["recommended_policy"]["evidence"]


def test_clean_internal_agent_gets_audit_or_none(client):
    """An agent with no LLM signal at all should get no recommendation."""
    client.post("/events/batch", json=[
        {"event_id": "ev1", "source": "nhi", "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-Y", "workload_id": "internal-batch"},
        {"event_id": "ev2", "source": "repo", "timestamp": "2026-05-20T09:00:00Z",
         "repo": "internal-batch", "workload_id": "internal-batch",
         "has_aegislib": True, "imports": ["boto3"]},
    ])
    client.post("/pipeline/run")
    agent = client.get("/agents").json()[0]
    assert agent["recommended_policy"] is None


def test_external_llm_no_phi_gets_egress_redact(client):
    client.post("/events/batch", json=[
        {"event_id": "rt-1", "source": "runtime", "timestamp": "2026-05-20T10:00:00Z",
         "host_id": "h1", "pid": 1, "destination": "api.openai.com",
         "tools_called": ["search"]},
        {"event_id": "nhi-1", "source": "nhi", "timestamp": "2026-05-20T10:01:00Z",
         "nhi_id": "role-x", "host_id": "h1", "pid": 1},
    ])
    client.post("/pipeline/run")
    agent = client.get("/agents").json()[0]
    assert agent["recommended_policy"] == POLICY_EGRESS
