"""Graph projection — bonus task: Agent → Identity → Tools → Data Classes → Policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SAMPLES_DIR = Path(__file__).parent.parent / "samples"


def _shadow(client):
    events = json.loads((SAMPLES_DIR / "01-shadow-phi.json").read_text())["events"]
    client.post("/events/batch", json=events)
    client.post("/pipeline/run")
    return client.get("/agents").json()[0]["agent_id"]


def test_graph_endpoint_returns_node_link_shape(client):
    aid = _shadow(client)
    g = client.get(f"/agents/{aid}/graph").json()
    assert "agent_id" in g
    assert g["agent_id"] == aid
    assert "nodes" in g and "edges" in g
    assert isinstance(g["nodes"], list)
    assert isinstance(g["edges"], list)


def test_graph_contains_required_node_types(client):
    aid = _shadow(client)
    g = client.get(f"/agents/{aid}/graph").json()
    types = {n["type"] for n in g["nodes"]}
    # Spec: Agent → Identity → Tools → Data Classes → Policy
    assert "agent" in types
    assert "identity" in types
    assert "tool" in types
    assert "data_class" in types
    assert "policy" in types


def test_graph_edges_point_from_agent(client):
    aid = _shadow(client)
    g = client.get(f"/agents/{aid}/graph").json()
    # Every edge should originate from the agent node in this simple projection
    for e in g["edges"]:
        assert e["source"] == aid


def test_graph_includes_phi_and_anthropic(client):
    aid = _shadow(client)
    g = client.get(f"/agents/{aid}/graph").json()
    labels = {n["label"] for n in g["nodes"]}
    assert "PHI" in labels
    assert "api.anthropic.com" in labels
    assert "phi-handling-v3" in labels


def test_graph_unknown_agent_returns_404(client):
    r = client.get("/agents/agent_nonexistent/graph")
    assert r.status_code == 404


def test_graph_policy_node_carries_confidence(client):
    aid = _shadow(client)
    g = client.get(f"/agents/{aid}/graph").json()
    policy_nodes = [n for n in g["nodes"] if n["type"] == "policy"]
    assert len(policy_nodes) == 1
    assert "confidence" in policy_nodes[0]
    assert policy_nodes[0]["confidence"] > 0.85


def test_graph_orphan_agent_has_no_policy_node(client):
    """An agent with no LLM signal → no policy node in the graph."""
    client.post("/events", json={
        "event_id": "etl-1", "source": "repo",
        "timestamp": "2026-05-20T10:00:00Z",
        "repo": "etl", "imports": ["pandas"]})
    client.post("/pipeline/run")
    aid = client.get("/agents").json()[0]["agent_id"]
    g = client.get(f"/agents/{aid}/graph").json()
    assert not any(n["type"] == "policy" for n in g["nodes"])
