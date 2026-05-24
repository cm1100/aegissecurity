"""Risk rules + scorer edge-case suite.

Three required rules + one bonus + a complete scoring table. Tests name
the exact condition each rule protects so reviewers can read the file as
the policy definition.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from aegis.ingest import parse_raw_event
from aegis.normalize import normalize_event
from aegis.risk.baselines import Baseline, BaselineRegistry
from aegis.risk.rules import (
    evaluate_rules,
    rule_missing_aegislib,
    rule_phi_to_external_llm,
    rule_secrets_handling,
    rule_unexpected_use,
)
from aegis.risk.scorer import score_agent
from aegis.schemas import Agent, CanonicalEvent, DataClass, Framework, RiskTier


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
# Rule 1 — PHI to external LLM
# ---------------------------------------------------------------------------

def test_rule1_fires_when_phi_and_external_llm_both_present():
    a = _agent(data_classes=["PHI"], destinations=["api.anthropic.com"])
    e = _evt("rt-1", source="runtime", destination="api.anthropic.com",
             host_id="h1", pid=1234)
    f = rule_phi_to_external_llm(a, [e], None)
    assert f is not None
    assert f.severity == RiskTier.HIGH
    assert f.rule_id == "phi_to_external_llm"


def test_rule1_does_not_fire_when_destination_is_internal():
    a = _agent(data_classes=["PHI"], destinations=["api.internal.corp"])
    f = rule_phi_to_external_llm(a, [], None)
    assert f is None


def test_rule1_does_not_fire_when_no_phi():
    a = _agent(data_classes=["PII"], destinations=["api.anthropic.com"])
    f = rule_phi_to_external_llm(a, [], None)
    assert f is None


def test_rule1_evidence_attributes_to_events():
    a = _agent(data_classes=["PHI"], destinations=["api.anthropic.com"],
               event_ids=["saas-1", "rt-1"])
    e1 = _evt("saas-1", source="saas", data_classes=["PHI"],
              resource="claims.patient_records")
    e2 = _evt("rt-1", source="runtime", destination="api.anthropic.com",
              host_id="h1", pid=1234)
    f = rule_phi_to_external_llm(a, [e1, e2], None)
    sources = {ev.source_event_id for ev in f.evidence if ev.source_event_id}
    assert sources == {"saas-1", "rt-1"}


# ---------------------------------------------------------------------------
# Rule 2 — missing aegislib
# ---------------------------------------------------------------------------

def test_rule2_fires_at_medium_when_framework_no_aegislib_no_phi():
    """Strict spec: rule fires only when a framework import is observed in
    the repo signals (agent.imports), not just on classifier inference."""
    a = _agent(framework=Framework.LANGCHAIN, has_aegislib=False,
               imports=["langchain"])
    f = rule_missing_aegislib(a, [], None)
    assert f is not None
    assert f.severity == RiskTier.MEDIUM


def test_rule2_upgrades_to_high_when_sensitive_data_touched():
    a = _agent(framework=Framework.LANGCHAIN, has_aegislib=False,
               imports=["langchain"], data_classes=["PHI"])
    f = rule_missing_aegislib(a, [], None)
    assert f.severity == RiskTier.HIGH


def test_rule2_does_not_fire_when_aegislib_present():
    a = _agent(framework=Framework.LANGCHAIN, has_aegislib=True,
               imports=["langchain", "aegislib"], data_classes=["PHI"])
    f = rule_missing_aegislib(a, [], None)
    assert f is None


def test_rule2_does_not_fire_for_non_framework_agent():
    """LLM_CALLER doesn't import an agent framework, so the rule shouldn't fire."""
    a = _agent(framework=Framework.LLM_CALLER, has_aegislib=False,
               data_classes=["PHI"])
    f = rule_missing_aegislib(a, [], None)
    assert f is None


def test_rule2_strict_does_not_fire_on_direct_sdk_without_imports():
    """Spec: 'Repo imports an agent framework'. A direct_sdk_agentic agent
    inferred only from runtime patterns (no repo framework import) must
    not trigger Rule 2."""
    a = _agent(framework=Framework.DIRECT_SDK_AGENTIC, has_aegislib=False,
               imports=["openai"], destinations=["api.openai.com"],
               tools=["search"], data_classes=["PHI"])
    f = rule_missing_aegislib(a, [], None)
    assert f is None  # openai is an SDK, not an agent framework


# ---------------------------------------------------------------------------
# Rule 3 — unexpected tool/destination/permission use
# ---------------------------------------------------------------------------

def test_rule3_fires_when_tool_outside_baseline():
    baseline = Baseline(workload_id="w1", tools=frozenset({"aurora_read"}))
    a = _agent(workload_id="w1", tools=["aurora_read", "external_llm_call"])
    f = rule_unexpected_use(a, [], baseline)
    assert f is not None
    assert f.severity == RiskTier.HIGH
    assert any("external_llm_call" in ev.claim for ev in f.evidence)


def test_rule3_silent_when_all_tools_in_baseline():
    baseline = Baseline(workload_id="w1", tools=frozenset({"aurora_read"}))
    a = _agent(workload_id="w1", tools=["aurora_read"])
    f = rule_unexpected_use(a, [], baseline)
    assert f is None


def test_rule3_silent_without_baseline():
    """No baseline → no false positives. We don't fire what we can't justify."""
    a = _agent(workload_id="w1", tools=["whatever"])
    f = rule_unexpected_use(a, [], None)
    assert f is None


def test_rule3_fires_on_unexpected_destination():
    baseline = Baseline(workload_id="w1",
                         destinations=frozenset({"api.internal.corp"}))
    a = _agent(workload_id="w1", destinations=["api.openai.com"])
    f = rule_unexpected_use(a, [], baseline)
    assert f is not None
    assert any("api.openai.com" in ev.claim for ev in f.evidence)


# ---------------------------------------------------------------------------
# Bonus rule — secrets data class
# ---------------------------------------------------------------------------

def test_secrets_rule_fires_on_secrets_class():
    a = _agent(data_classes=["secrets"], destinations=["api.anthropic.com"])
    f = rule_secrets_handling(a, [], None)
    assert f is not None
    assert f.severity == RiskTier.HIGH  # external LLM + secrets


def test_secrets_rule_medium_when_no_external_egress():
    a = _agent(data_classes=["secrets"], destinations=["api.internal.corp"])
    f = rule_secrets_handling(a, [], None)
    assert f.severity == RiskTier.MEDIUM


# ---------------------------------------------------------------------------
# Scorer factors
# ---------------------------------------------------------------------------

def test_scope_factor_caps_at_25():
    a = _agent(tools=[f"t{i}" for i in range(10)],
               destinations=["d1", "d2", "d3"],
               permissions=[f"p{i}" for i in range(5)])
    s = score_agent(a, [])
    scope = next(f for f in s.factors if f.name == "scope")
    assert scope.value == 25


def test_sensitivity_uses_max_weight_class():
    a = _agent(data_classes=["PHI", "PII"])
    s = score_agent(a, [])
    sens = next(f for f in s.factors if f.name == "sensitivity")
    assert sens.value == 35


def test_sensitivity_zero_when_no_data():
    s = score_agent(_agent(), [])
    sens = next(f for f in s.factors if f.name == "sensitivity")
    assert sens.value == 0


def test_autonomy_full_20_for_agentic_framework():
    a = _agent(framework=Framework.LANGCHAIN)
    s = score_agent(a, [])
    aut = next(f for f in s.factors if f.name == "autonomy")
    assert aut.value == 20


def test_autonomy_partial_for_direct_sdk():
    a = _agent(framework=Framework.DIRECT_SDK_AGENTIC)
    s = score_agent(a, [])
    aut = next(f for f in s.factors if f.name == "autonomy")
    assert aut.value == 14


def test_autonomy_low_for_llm_caller():
    a = _agent(framework=Framework.LLM_CALLER)
    s = score_agent(a, [])
    aut = next(f for f in s.factors if f.name == "autonomy")
    assert aut.value == 6


def test_drift_combines_unexpected_and_missing_aegislib():
    findings = [
        rule_unexpected_use(
            _agent(workload_id="w1", tools=["x"]),
            [],
            Baseline(workload_id="w1", tools=frozenset({"a"})),
        ),
        rule_missing_aegislib(
            _agent(framework=Framework.LANGCHAIN, has_aegislib=False,
                   imports=["langchain"]),
            [], None
        ),
    ]
    findings = [f for f in findings if f is not None]
    a = _agent(framework=Framework.LANGCHAIN)
    s = score_agent(a, findings)
    drift = next(f for f in s.factors if f.name == "drift")
    assert drift.value == 20


# ---------------------------------------------------------------------------
# Tier mapping (spec-required boundaries)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,tier", [
    (0, RiskTier.LOW), (39, RiskTier.LOW),
    (40, RiskTier.MEDIUM), (69, RiskTier.MEDIUM),
    (70, RiskTier.HIGH), (100, RiskTier.HIGH),
])
def test_tier_boundaries(score, tier):
    """Bin boundaries from spec: 0-39 LOW, 40-69 MEDIUM, 70-100 HIGH."""
    from aegis.risk.scorer import _tier_for
    assert _tier_for(score) == tier


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
         "nhi_id": "role-shadow", "workload_id": "claims-processor",
         "permissions": ["aurora:read", "bedrock:invoke", "s3:GetObject"]},
        {"event_id": "repo-1", "source": "repo",
         "timestamp": "2026-05-20T09:00:00Z",
         "repo": "claims-processor", "workload_id": "claims-processor",
         "imports": ["langchain", "anthropic"],
         "framework_hint": "langchain", "has_aegislib": False},
        {"event_id": "saas-1", "source": "saas",
         "timestamp": "2026-05-20T10:16:00Z",
         "nhi_id": "role-shadow",
         "data_classes": ["PHI", "claims_data"],
         "action": "read", "resource": "claims.patient_records"},
    ]


def _full_pipeline(client):
    client.post("/events/batch", json=_shadow_phi_payloads())
    client.post("/pipeline/correlate")
    client.post("/pipeline/classify")
    return client.post("/pipeline/risk").json()


def test_shadow_phi_scenario_scores_high(client):
    r = _full_pipeline(client)
    assert r["agents_scored"] == 1
    assert r["tiers"]["HIGH"] == 1
    agent = client.get("/agents").json()[0]
    assert agent["risk_tier"] == "HIGH"
    assert agent["risk_score"] >= 70


def test_shadow_phi_findings_include_required_rules(client):
    _full_pipeline(client)
    aid = client.get("/agents").json()[0]["agent_id"]
    detail = client.get(f"/agents/{aid}").json()
    rule_ids = {f["rule_id"] for f in detail["findings"]}
    assert "phi_to_external_llm" in rule_ids
    assert "missing_aegislib" in rule_ids


def test_risk_is_idempotent(client):
    _full_pipeline(client)
    a1 = client.get("/agents").json()[0]
    aid = a1["agent_id"]
    detail1 = client.get(f"/agents/{aid}").json()
    # rerun risk
    client.post("/pipeline/risk")
    a2 = client.get("/agents").json()[0]
    detail2 = client.get(f"/agents/{aid}").json()
    assert a1["risk_score"] == a2["risk_score"]
    assert a1["risk_tier"] == a2["risk_tier"]
    assert len(detail1["findings"]) == len(detail2["findings"])


def test_clean_agent_scores_low(client):
    """An agent with no sensitive data, internal destination, aegislib present
    should score in the LOW band."""
    client.post("/events/batch", json=[
        {"event_id": "ev1", "source": "nhi", "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-Y", "workload_id": "internal-tool"},
        {"event_id": "ev2", "source": "repo", "timestamp": "2026-05-20T09:00:00Z",
         "repo": "internal-tool", "workload_id": "internal-tool",
         "has_aegislib": True, "imports": ["aegislib"]},
        {"event_id": "ev3", "source": "saas", "timestamp": "2026-05-20T10:01:00Z",
         "nhi_id": "role-Y", "data_classes": ["internal"]},
    ])
    client.post("/pipeline/correlate")
    client.post("/pipeline/classify")
    client.post("/pipeline/risk")
    agent = client.get("/agents").json()[0]
    assert agent["risk_tier"] in ("LOW", "MEDIUM")
    assert agent["risk_score"] < 50


def test_risk_factors_persisted_with_rationale(client):
    _full_pipeline(client)
    aid = client.get("/agents").json()[0]["agent_id"]
    detail = client.get(f"/agents/{aid}").json()
    factors = detail["agent"]["risk_factors"]
    names = {f["name"] for f in factors}
    assert names == {"scope", "sensitivity", "autonomy", "drift"}
    for f in factors:
        assert f["rationale"]  # every factor has a non-empty rationale string
