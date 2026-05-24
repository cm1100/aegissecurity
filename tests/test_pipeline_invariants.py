"""End-to-end pipeline invariants — properties that hold over any input.

These tests use real sample manifests because the invariants are
about full-pipeline behavior, not unit-level logic.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

SAMPLES_DIR = Path(__file__).parent.parent / "samples"


def _load(name: str) -> list[dict]:
    return json.loads((SAMPLES_DIR / name).read_text())["events"]


# ---------------------------------------------------------------------------
# Order independence
# ---------------------------------------------------------------------------

def test_pipeline_state_is_independent_of_ingest_order(client, app):
    """Shuffling event ingestion order must not change the final state.

    We pick scenario 04 (partial + out-of-order + duplicates) because it's
    designed to stress this property."""
    events = _load("04-partial-and-out-of-order.json")

    baseline_signature = None
    for seed in range(5):
        # Reset the DB by re-creating the app (per-test fixture handles this
        # but we need a fresh one per shuffle). Simplest: query and reset
        # raw tables manually.
        from aegis.storage import (
            AgentRow, CorrelationEdgeRow, FindingRow,
            NormalizedEventRow, PolicyRow, RawEventRow, get_session,
        )
        with get_session() as s:
            for table in (PolicyRow, FindingRow, CorrelationEdgeRow,
                          AgentRow, NormalizedEventRow, RawEventRow):
                s.query(table).delete()
            s.flush()

        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        client.post("/events/batch", json=shuffled)
        client.post("/pipeline/run")
        agents = client.get("/agents").json()
        signature = sorted(
            (a["nhi_id"], a["risk_tier"], a["recommended_policy"],
             tuple(sorted(a["data_classes"])))
            for a in agents
        )
        if baseline_signature is None:
            baseline_signature = signature
        else:
            assert signature == baseline_signature, \
                f"seed {seed} produced different state"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_pipeline_run_is_idempotent_across_5_invocations(client):
    client.post("/events/batch", json=_load("05-corporate-fleet.json"))
    snapshots = []
    for _ in range(5):
        client.post("/pipeline/run")
        snapshots.append(client.get("/agents").json())
    for i in range(1, len(snapshots)):
        assert snapshots[i] == snapshots[0]


def test_re_ingesting_same_events_does_not_change_agent_count(client):
    """Re-submission of identical events should be a no-op."""
    events = _load("01-shadow-phi.json")
    client.post("/events/batch", json=events)
    client.post("/pipeline/run")
    before = client.get("/agents").json()
    client.post("/events/batch", json=events)
    client.post("/pipeline/run")
    after = client.get("/agents").json()
    assert before == after


# ---------------------------------------------------------------------------
# Scoring + finding consistency
# ---------------------------------------------------------------------------

def test_risk_score_equals_sum_of_factor_values_capped_at_100(client):
    """For every agent: factor sum (capped) == reported risk_score."""
    client.post("/events/batch", json=_load("05-corporate-fleet.json"))
    client.post("/pipeline/run")
    for a in client.get("/agents").json():
        detail = client.get(f"/agents/{a['agent_id']}").json()
        factors = detail["agent"]["risk_factors"]
        factor_sum = min(100, sum(f["value"] for f in factors))
        assert a["risk_score"] == factor_sum, (
            f"agent {a['agent_id']}: factors sum to {factor_sum} "
            f"but risk_score = {a['risk_score']}"
        )


def test_high_tier_agents_always_have_at_least_one_finding(client):
    """No silent HIGH — every HIGH agent must have a finding that documents why."""
    client.post("/events/batch", json=_load("05-corporate-fleet.json"))
    client.post("/pipeline/run")
    for a in client.get("/agents").json():
        if a["risk_tier"] != "HIGH":
            continue
        detail = client.get(f"/agents/{a['agent_id']}").json()
        assert len(detail["findings"]) >= 1, \
            f"HIGH agent {a['agent_id']} has no findings"


def test_phi_handling_policy_implies_phi_in_data_classes(client):
    """Recommend phi-handling-v3 → must have PHI in data_classes."""
    for f in SAMPLES_DIR.glob("*.json"):
        events = json.loads(f.read_text()).get("events")
        if events:
            client.post("/events/batch", json=events)
    client.post("/pipeline/run")
    for a in client.get("/agents").json():
        if a["recommended_policy"] == "phi-handling-v3":
            assert "PHI" in a["data_classes"], \
                f"agent {a['agent_id']} got phi-handling-v3 without PHI"


def test_external_egress_redact_implies_external_destination(client):
    """external-egress-redact → must have an external LLM destination."""
    from aegis.utils import is_external_llm
    for f in SAMPLES_DIR.glob("*.json"):
        events = json.loads(f.read_text()).get("events")
        if events:
            client.post("/events/batch", json=events)
    client.post("/pipeline/run")
    for a in client.get("/agents").json():
        if a["recommended_policy"] == "external-egress-redact":
            external = [d for d in a["destinations"] if is_external_llm(d)]
            assert external, \
                f"agent {a['agent_id']} got external-egress-redact without external dest"


# ---------------------------------------------------------------------------
# Tier boundaries reflect spec
# ---------------------------------------------------------------------------

def test_no_agent_has_score_below_zero_or_above_100(client):
    """Score must always be in [0, 100]."""
    for f in SAMPLES_DIR.glob("*.json"):
        events = json.loads(f.read_text()).get("events")
        if events:
            client.post("/events/batch", json=events)
    client.post("/pipeline/run")
    for a in client.get("/agents").json():
        assert 0 <= a["risk_score"] <= 100, \
            f"agent {a['agent_id']} score out of range: {a['risk_score']}"


def test_tier_assignment_consistent_with_score(client):
    """0-39 → LOW, 40-69 → MEDIUM, 70-100 → HIGH."""
    for f in SAMPLES_DIR.glob("*.json"):
        events = json.loads(f.read_text()).get("events")
        if events:
            client.post("/events/batch", json=events)
    client.post("/pipeline/run")
    for a in client.get("/agents").json():
        score = a["risk_score"]
        tier = a["risk_tier"]
        if score >= 70:
            assert tier == "HIGH"
        elif score >= 40:
            assert tier == "MEDIUM"
        else:
            assert tier == "LOW"


# ---------------------------------------------------------------------------
# Correlation confidence sanity
# ---------------------------------------------------------------------------

def test_every_agent_has_correlation_confidence_in_valid_range(client):
    for f in SAMPLES_DIR.glob("*.json"):
        events = json.loads(f.read_text()).get("events")
        if events:
            client.post("/events/batch", json=events)
    client.post("/pipeline/run")
    for a in client.get("/agents").json():
        cc = a["correlation_confidence"]
        assert 0.0 <= cc <= 1.0, f"agent {a['agent_id']} confidence = {cc}"
        # Single-event agents always have 1.0
        if a["event_count"] == 1:
            assert cc == 1.0
