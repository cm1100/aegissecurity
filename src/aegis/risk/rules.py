"""Risk rules — deterministic detectors that emit structured findings.

Three rules are required by the spec; one bonus rule (credential exposure
via the SECRETS data class) is included to demonstrate that the rule
list is extensible without touching the orchestration code.

Every rule:
  - takes (agent, events, baseline) and returns Optional[RiskFinding]
  - emits structured Evidence items tied to the source event when
    attributable
  - keeps its severity choice explicit and rule-local (Rule 2's
    MEDIUM/HIGH upgrade depends on whether sensitive data is touched)
"""

from __future__ import annotations

from typing import Callable, Optional

from aegis.utils import is_external_llm
from aegis.risk.baselines import Baseline
from aegis.schemas import Agent, CanonicalEvent, Evidence, RiskFinding, RiskTier
from aegis.schemas.enums import SENSITIVE_DATA_CLASSES, DataClass

# The spec's Rule 2 wording is repo-centric: "Repo imports an agent framework".
# These are the imports that count as agent frameworks. MCP and direct SDK
# are deliberately excluded — they aren't "agent framework imports" in the
# spec's sense (MCP is a protocol, direct SDK is a runtime pattern).
FRAMEWORK_IMPORT_NAMES = {
    "langchain",
    "langgraph",
    "crewai",
    "llama_index",
    "autogen",
}


def _agent_data_classes(agent: Agent) -> set[str]:
    return {dc if isinstance(dc, str) else dc.value for dc in agent.data_classes}


def _is_sensitive(dc_value: str) -> bool:
    try:
        return DataClass(dc_value) in SENSITIVE_DATA_CLASSES
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Rule 1 — PHI to external LLM
# ---------------------------------------------------------------------------

def rule_phi_to_external_llm(
    agent: Agent, events: list[CanonicalEvent], baseline: Optional[Baseline]
) -> Optional[RiskFinding]:
    dcs = _agent_data_classes(agent)
    if DataClass.PHI.value not in dcs:
        return None

    external_dests = [d for d in agent.destinations if is_external_llm(d)]
    if not external_dests:
        return None

    evidence: list[Evidence] = []
    for ev in events:
        ev_dcs = {dc if isinstance(dc, str) else dc.value for dc in ev.data_classes}
        if DataClass.PHI.value in ev_dcs:
            evidence.append(
                Evidence(
                    claim=f"PHI accessed at resource={ev.resource!r}",
                    source_event_id=ev.event_id,
                    confidence=0.95,
                )
            )
        if is_external_llm(ev.destination):
            evidence.append(
                Evidence(
                    claim=f"external LLM call to {ev.destination}",
                    source_event_id=ev.event_id,
                    confidence=0.90,
                )
            )
    return RiskFinding(
        rule_id="phi_to_external_llm",
        rule_name="PHI sent to external LLM provider",
        severity=RiskTier.HIGH,
        description=(
            "Agent accessed PHI-tagged resources and initiated a call to an "
            "external LLM provider in the same correlation window."
        ),
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Rule 2 — Agent without aegislib (MEDIUM, upgraded to HIGH if sensitive data)
# ---------------------------------------------------------------------------

def rule_missing_aegislib(
    agent: Agent, events: list[CanonicalEvent], baseline: Optional[Baseline]
) -> Optional[RiskFinding]:
    # Strict spec interpretation: Rule 2 is "Repo imports an agent framework
    # AND does not import aegislib". Both clauses are about repo state, so
    # we fire only when an agent-framework import is actually observed in
    # the repo signals (not just inferred by the runtime classifier).
    framework_imports = [imp for imp in agent.imports if imp in FRAMEWORK_IMPORT_NAMES]
    if not framework_imports:
        return None
    if agent.has_aegislib:
        return None

    dcs = _agent_data_classes(agent)
    has_sensitive = any(_is_sensitive(dc) for dc in dcs)
    severity = RiskTier.HIGH if has_sensitive else RiskTier.MEDIUM

    framework_label = framework_imports[0]
    evidence: list[Evidence] = [
        Evidence(
            claim=f"repo imports agent framework {framework_label!r} but does not import aegislib",
            confidence=0.90,
        )
    ]
    for ev in events:
        # Only repo events can confirm aegislib absence. Source is normalized
        # to a string via use_enum_values=True on CanonicalEvent.
        if ev.source != "repo":
            continue
        if not ev.has_aegislib:
            evidence.append(
                Evidence(
                    claim=f"repo scan confirms aegislib import absent (imports: {ev.imports!r})",
                    source_event_id=ev.event_id,
                    confidence=0.95,
                )
            )
    if has_sensitive:
        evidence.append(
            Evidence(
                claim=f"agent touches sensitive data classes: {sorted(dcs & {d.value for d in SENSITIVE_DATA_CLASSES})}",
                confidence=0.95,
            )
        )
    return RiskFinding(
        rule_id="missing_aegislib",
        rule_name="Agent framework in use without Aegis SDK",
        severity=severity,
        description=(
            "Agent uses a recognized agent framework but does not import "
            "aegislib, so policy enforcement at runtime cannot be guaranteed."
        ),
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Rule 3 — Unexpected tool / permission / destination use vs baseline
# ---------------------------------------------------------------------------

def rule_unexpected_use(
    agent: Agent, events: list[CanonicalEvent], baseline: Optional[Baseline]
) -> Optional[RiskFinding]:
    if baseline is None:
        return None  # no baseline → no false positives

    unexpected_tools = sorted(set(agent.tools) - baseline.tools)
    unexpected_destinations = sorted(set(agent.destinations) - baseline.destinations)
    unexpected_permissions = sorted(set(agent.permissions) - baseline.permissions)

    if not (unexpected_tools or unexpected_destinations or unexpected_permissions):
        return None

    evidence: list[Evidence] = []
    if unexpected_tools:
        evidence.append(
            Evidence(
                claim=f"tools outside baseline for workload={baseline.workload_id!r}: {unexpected_tools}",
                confidence=0.90,
            )
        )
    if unexpected_destinations:
        evidence.append(
            Evidence(
                claim=f"destinations outside baseline: {unexpected_destinations}",
                confidence=0.85,
            )
        )
    if unexpected_permissions:
        evidence.append(
            Evidence(
                claim=f"permissions outside baseline: {unexpected_permissions}",
                confidence=0.80,
            )
        )
    # attribute to events when easy
    for ev in events:
        for tool in ev.tools_called:
            if tool in unexpected_tools:
                evidence.append(
                    Evidence(
                        claim=f"unexpected tool call '{tool}' observed",
                        source_event_id=ev.event_id,
                        confidence=0.85,
                    )
                )
                break
    return RiskFinding(
        rule_id="unexpected_tool_use",
        rule_name="Tool, destination, or permission use outside baseline",
        severity=RiskTier.HIGH,
        description=(
            "Agent invoked tools / destinations / permissions not in the "
            "declared baseline for its workload."
        ),
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Bonus rule — Secrets data class touched
# ---------------------------------------------------------------------------

def rule_secrets_handling(
    agent: Agent, events: list[CanonicalEvent], baseline: Optional[Baseline]
) -> Optional[RiskFinding]:
    dcs = _agent_data_classes(agent)
    if DataClass.SECRETS.value not in dcs:
        return None

    evidence = [
        Evidence(
            claim="agent handles 'secrets'-tagged data",
            confidence=0.95,
        )
    ]
    for ev in events:
        ev_dcs = {dc if isinstance(dc, str) else dc.value for dc in ev.data_classes}
        if DataClass.SECRETS.value in ev_dcs:
            evidence.append(
                Evidence(
                    claim=f"secrets access observed at resource={ev.resource!r}",
                    source_event_id=ev.event_id,
                    confidence=0.95,
                )
            )
    severity = (
        RiskTier.HIGH
        if any(is_external_llm(d) for d in agent.destinations)
        else RiskTier.MEDIUM
    )
    return RiskFinding(
        rule_id="secrets_handling",
        rule_name="Agent processes credential / secrets data",
        severity=severity,
        description=(
            "Agent operates on 'secrets'-tagged data; combined with external "
            "egress this is a credential exfiltration risk."
        ),
        evidence=evidence,
    )


RuleFn = Callable[
    [Agent, list[CanonicalEvent], Optional[Baseline]],
    Optional[RiskFinding],
]

RULES: list[RuleFn] = [
    rule_phi_to_external_llm,
    rule_missing_aegislib,
    rule_unexpected_use,
    rule_secrets_handling,
]


def evaluate_rules(
    agent: Agent,
    events: list[CanonicalEvent],
    baseline: Optional[Baseline],
) -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    for rule in RULES:
        f = rule(agent, events, baseline)
        if f is not None:
            findings.append(f)
    return findings
