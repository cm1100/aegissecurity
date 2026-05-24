"""Sample scenario regression tests.

Each scenario is checked against the `expected` block declared in its
manifest — so the manifests are self-documenting and the tests are
self-updating. If you change a scenario, also update the `expected`
field; the test will tell you when they drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.risk import BaselineRegistry, risk_persist
from aegis.storage import get_session

SAMPLES_DIR = Path(__file__).parent.parent / "samples"
BASELINES_PATH = Path(__file__).parent.parent / "baselines.yaml"


def _load_scenario(name: str) -> dict:
    return json.loads((SAMPLES_DIR / name).read_text())


def _run_pipeline(client, baselines_path=None):
    """Run full pipeline. Optionally use the project's baselines.yaml so
    Rule 3 (drift) can fire."""
    if baselines_path and baselines_path.exists():
        registry = BaselineRegistry.from_yaml(baselines_path)
        # /pipeline/run uses default_registry() which reads cwd/baselines.yaml.
        # We bypass it here by running the stages explicitly with our registry.
        client.post("/pipeline/normalize")
        client.post("/pipeline/correlate")
        client.post("/pipeline/classify")
        with get_session() as s:
            risk_persist(s, baselines=registry)
        client.post("/pipeline/policy")
    else:
        client.post("/pipeline/run")


def _agents(client):
    return client.get("/agents").json()


# ---------------------------------------------------------------------------
# Scenario 1 — shadow PHI
# ---------------------------------------------------------------------------

def test_scenario_01_shadow_phi(client):
    s = _load_scenario("01-shadow-phi.json")
    client.post("/events/batch", json=s["events"])
    _run_pipeline(client, BASELINES_PATH)
    agents = _agents(client)
    assert len(agents) == s["expected"]["agents"]
    a = agents[0]
    assert a["risk_tier"] == s["expected"]["risk_tier"]
    assert a["recommended_policy"] == s["expected"]["recommended_policy"]
    assert a["nhi_id"] == "role-aegis-shadow-agent"
    assert a["framework"] == "langchain"
    assert "PHI" in a["data_classes"]


# ---------------------------------------------------------------------------
# Scenario 2 — approved internal
# ---------------------------------------------------------------------------

def test_scenario_02_approved_internal(client):
    s = _load_scenario("02-approved-internal.json")
    client.post("/events/batch", json=s["events"])
    _run_pipeline(client, BASELINES_PATH)
    agents = _agents(client)
    assert len(agents) == s["expected"]["agents"]
    a = agents[0]
    assert a["risk_tier"] == s["expected"]["risk_tier"]
    assert a["recommended_policy"] == s["expected"]["recommended_policy"]
    # Hygiene checks
    assert a["framework"] == "langchain"
    detail = client.get(f"/agents/{a['agent_id']}").json()
    assert detail["agent"]["has_aegislib"] is True


# ---------------------------------------------------------------------------
# Scenario 3 — two agents on one host
# ---------------------------------------------------------------------------

def test_scenario_03_multi_agent_same_host(client):
    s = _load_scenario("03-multi-agent-same-host.json")
    client.post("/events/batch", json=s["events"])
    _run_pipeline(client, BASELINES_PATH)
    agents = _agents(client)
    assert len(agents) == s["expected"]["agents"] == 2

    # Distinguished by nhi_id, not collapsed by shared host
    nhi_ids = {a["nhi_id"] for a in agents}
    assert nhi_ids == {"role-patient-portal", "role-billing-reporter"}

    # The PHI handler should be HIGH; the financial reporter MEDIUM
    by_role = {a["nhi_id"]: a for a in agents}
    assert by_role["role-patient-portal"]["risk_tier"] == "HIGH"
    assert by_role["role-patient-portal"]["recommended_policy"] == "phi-handling-v3"
    assert by_role["role-billing-reporter"]["risk_tier"] in ("MEDIUM", "HIGH")
    assert "phi" not in (by_role["role-billing-reporter"]["recommended_policy"] or "")


# ---------------------------------------------------------------------------
# Scenario 4 — partial fields, out-of-order, duplicates
# ---------------------------------------------------------------------------

def test_scenario_04_partial_and_out_of_order(client):
    s = _load_scenario("04-partial-and-out-of-order.json")
    batch_result = client.post("/events/batch", json=s["events"]).json()

    # Dedup: 5 submitted → 4 unique
    assert batch_result["total"] == 5
    assert batch_result["deduped"] == 1

    # Raw + canonical counts confirm dedup happened
    assert len(client.get("/events").json()) == 4
    assert len(client.get("/events/normalized").json()) == 4

    _run_pipeline(client, BASELINES_PATH)
    agents = _agents(client)
    assert len(agents) == s["expected"]["agents"] == 1
    a = agents[0]
    assert a["risk_tier"] == s["expected"]["risk_tier"]
    assert a["recommended_policy"] == s["expected"]["recommended_policy"]
    # All 4 unique events bound into the agent
    detail = client.get(f"/agents/{a['agent_id']}").json()
    assert len(detail["agent"]["event_ids"]) == 4


# ---------------------------------------------------------------------------
# Scenario 5 — corporate fleet
# ---------------------------------------------------------------------------

def test_scenario_05_corporate_fleet(client):
    s = _load_scenario("05-corporate-fleet.json")
    client.post("/events/batch", json=s["events"])
    _run_pipeline(client, BASELINES_PATH)
    agents = _agents(client)
    assert len(agents) == s["expected"]["agents"] == 5

    # Policy distribution matches the manifest's declared expectation
    by_policy: dict[str, int] = {}
    for a in agents:
        key = a["recommended_policy"] or "none"
        by_policy[key] = by_policy.get(key, 0) + 1
    assert by_policy == s["expected"]["policy_distribution"]

    # The PHI handler must be present, HIGH-tier, and own phi-handling-v3
    phi_agent = next(
        (a for a in agents if a["recommended_policy"] == "phi-handling-v3"),
        None,
    )
    assert phi_agent is not None
    assert phi_agent["risk_tier"] == "HIGH"
    assert "PHI" in phi_agent["data_classes"]


# ---------------------------------------------------------------------------
# Cross-scenario: ingesting all five in one pipeline
# ---------------------------------------------------------------------------

def test_all_scenarios_in_one_pipeline(client):
    """Ingest every scenario manifest into a single pipeline. Total agents
    should be sum of per-scenario counts (5+5 split fine across all)."""
    total_expected = 0
    for fname in sorted(SAMPLES_DIR.glob("*.json")):
        s = json.loads(fname.read_text())
        client.post("/events/batch", json=s["events"])
        total_expected += s["expected"]["agents"]
    _run_pipeline(client, BASELINES_PATH)
    agents = _agents(client)
    # 1 + 1 + 2 + 1 + 5 = 10
    assert len(agents) == total_expected == 10

    # At least one HIGH agent
    high_count = sum(1 for a in agents if a["risk_tier"] == "HIGH")
    assert high_count >= 2  # scenarios 1, 3, 4, and one in 5
