"""Policy recommender — picks one policy from the catalog, with evidence.

The catalog is the three sample policies named in the spec:

  phi-handling-v3         Strongest envelope: PHI-aware redaction, BAA-only
                          providers, full audit. Recommended when PHI flows
                          to an external LLM.
  external-egress-redact  Strip / mask sensitive fields before any external
                          LLM call. Recommended when the agent egresses to
                          an external LLM but no PHI is observed.
  audit-all-llm-calls     Bare audit envelope. Recommended for any other
                          LLM-using agent — even when no external egress
                          is observed (internal-only LLM agents still need
                          an audit trail per the EU AI Act 2026 logging
                          requirements).

Decision tree (most-specific first):

  1. data_classes contains PHI       AND has external LLM destination
                                     → phi-handling-v3
  2. has external LLM destination    (no PHI)
                                     → external-egress-redact
  3. any LLM signal at all           (framework set, provider set, or
                                     LLM-shaped destination name)
                                     → audit-all-llm-calls
  4. otherwise                       → no recommendation (None)

Confidence formula:
  confidence = min(0.98, base[policy] + 0.02 · |evidence|)

The base is calibrated so that a recommendation with the three canonical
spec-example evidence items reproduces the spec's 0.91 for phi-handling-v3
(0.85 + 0.02·3 = 0.91). Additional event-level attributions push the
confidence higher, capped at 0.98.

Each recommendation carries:
  - structured Evidence items (claim, source_event_id, confidence)
  - alternatives_considered (the policies rejected at this branch)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from aegis.utils import is_external_llm
from aegis.schemas import (
    Agent,
    CanonicalEvent,
    Evidence,
    PolicyRecommendation,
)
from aegis.schemas.enums import DataClass, Framework
from aegis.storage import (
    AgentRow,
    FindingRow,
    NormalizedEventRow,
    PolicyRow,
    get_session,
)

POLICY_PHI = "phi-handling-v3"
POLICY_EGRESS = "external-egress-redact"
POLICY_AUDIT = "audit-all-llm-calls"

POLICY_CATALOG = [POLICY_PHI, POLICY_EGRESS, POLICY_AUDIT]

_BASE_CONFIDENCE: dict[str, float] = {
    POLICY_PHI: 0.85,
    POLICY_EGRESS: 0.80,
    POLICY_AUDIT: 0.70,
}
_EVIDENCE_BONUS = 0.02
_CONFIDENCE_CAP = 0.98

_LLM_DESTINATION_KEYWORDS = (
    "anthropic", "openai", "bedrock", "gemini", "gpt", "claude", "llm",
)


def _agent_data_classes(agent: Agent) -> set[str]:
    return {dc if isinstance(dc, str) else dc.value for dc in agent.data_classes}


def _has_phi(agent: Agent) -> bool:
    return DataClass.PHI.value in _agent_data_classes(agent)


def _has_external_llm(agent: Agent) -> bool:
    return any(is_external_llm(d) for d in agent.destinations)


def _has_any_llm_signal(agent: Agent) -> bool:
    fw = agent.framework
    fw_value = fw if isinstance(fw, str) else (fw.value if fw else None)
    if fw_value and fw_value != Framework.UNKNOWN.value:
        return True
    if agent.provider:
        return True
    for d in agent.destinations:
        dl = d.lower()
        if any(k in dl for k in _LLM_DESTINATION_KEYWORDS):
            return True
    return False


def _attribute_phi_events(events: Iterable[CanonicalEvent]) -> list[Evidence]:
    out: list[Evidence] = []
    for ev in events:
        ev_dcs = {dc if isinstance(dc, str) else dc.value for dc in ev.data_classes}
        if DataClass.PHI.value in ev_dcs:
            claim = (
                f"PHI accessed at resource={ev.resource!r}" if ev.resource
                else "PHI access event"
            )
            out.append(
                Evidence(claim=claim, source_event_id=ev.event_id, confidence=0.95)
            )
    return out


def _attribute_external_llm_events(events: Iterable[CanonicalEvent]) -> list[Evidence]:
    out: list[Evidence] = []
    for ev in events:
        if is_external_llm(ev.destination):
            out.append(
                Evidence(
                    claim=f"external LLM call to {ev.destination}",
                    source_event_id=ev.event_id,
                    confidence=0.90,
                )
            )
    return out


def _confidence(policy: str, evidence_count: int) -> float:
    return min(_CONFIDENCE_CAP, _BASE_CONFIDENCE[policy] + _EVIDENCE_BONUS * evidence_count)


def _build_phi_recommendation(
    agent: Agent, events: list[CanonicalEvent], findings: list[dict]
) -> PolicyRecommendation:
    ext_dests = [d for d in agent.destinations if is_external_llm(d)]
    evidence: list[Evidence] = [
        Evidence(claim="Agent accessed PHI", confidence=0.95),
        Evidence(
            claim=f"Agent called external LLM provider: {ext_dests}",
            confidence=0.95,
        ),
    ]
    if not agent.has_aegislib:
        evidence.append(Evidence(claim="Agent does not use aegislib", confidence=0.90))

    evidence.extend(_attribute_phi_events(events))
    evidence.extend(_attribute_external_llm_events(events))

    for f in findings:
        if f.get("rule_id") in {"phi_to_external_llm", "missing_aegislib"}:
            evidence.append(
                Evidence(
                    claim=f"corroborated by risk finding: {f.get('rule_id')}",
                    confidence=0.95,
                )
            )

    return PolicyRecommendation(
        policy=POLICY_PHI,
        confidence=round(_confidence(POLICY_PHI, len(evidence)), 4),
        evidence=evidence,
        alternatives_considered=[POLICY_EGRESS, POLICY_AUDIT],
    )


def _build_egress_recommendation(
    agent: Agent, events: list[CanonicalEvent], findings: list[dict]
) -> PolicyRecommendation:
    ext_dests = [d for d in agent.destinations if is_external_llm(d)]
    evidence: list[Evidence] = [
        Evidence(
            claim=f"Agent calls external LLM provider: {ext_dests}",
            confidence=0.90,
        )
    ]
    if not agent.has_aegislib:
        evidence.append(Evidence(claim="Agent does not use aegislib", confidence=0.85))
    if agent.data_classes:
        dcs = sorted(_agent_data_classes(agent))
        evidence.append(
            Evidence(
                claim=f"non-PHI sensitive data observed: {dcs}",
                confidence=0.80,
            )
        )
    evidence.extend(_attribute_external_llm_events(events))

    return PolicyRecommendation(
        policy=POLICY_EGRESS,
        confidence=round(_confidence(POLICY_EGRESS, len(evidence)), 4),
        evidence=evidence,
        alternatives_considered=[POLICY_AUDIT],
    )


def _build_audit_recommendation(
    agent: Agent, events: list[CanonicalEvent], findings: list[dict]
) -> PolicyRecommendation:
    fw = agent.framework
    fw_value = fw if isinstance(fw, str) else (fw.value if fw else "unknown")
    evidence: list[Evidence] = [
        Evidence(
            claim=f"Agent exhibits LLM usage signals (framework={fw_value!r})",
            confidence=0.75,
        )
    ]
    if agent.provider:
        evidence.append(
            Evidence(
                claim=f"declared provider: {agent.provider}",
                confidence=0.75,
            )
        )
    if agent.destinations:
        evidence.append(
            Evidence(
                claim=f"observed destinations: {agent.destinations}",
                confidence=0.70,
            )
        )
    return PolicyRecommendation(
        policy=POLICY_AUDIT,
        confidence=round(_confidence(POLICY_AUDIT, len(evidence)), 4),
        evidence=evidence,
        alternatives_considered=[],
    )


def recommend_policy(
    agent: Agent,
    events: Optional[list[CanonicalEvent]] = None,
    findings: Optional[list[dict]] = None,
) -> Optional[PolicyRecommendation]:
    events = events or []
    findings = findings or []

    if _has_phi(agent) and _has_external_llm(agent):
        return _build_phi_recommendation(agent, events, findings)
    if _has_external_llm(agent):
        return _build_egress_recommendation(agent, events, findings)
    if _has_any_llm_signal(agent):
        return _build_audit_recommendation(agent, events, findings)
    return None


def _load_events(session: Session, event_ids: Iterable[str]) -> list[CanonicalEvent]:
    event_ids = list(event_ids)
    if not event_ids:
        return []
    rows = (
        session.query(NormalizedEventRow)
        .filter(NormalizedEventRow.event_id.in_(event_ids))
        .all()
    )
    return [CanonicalEvent.model_validate(r.payload) for r in rows]


def _load_findings(session: Session, agent_id: str) -> list[dict]:
    rows = (
        session.query(FindingRow)
        .filter(FindingRow.agent_id == agent_id)
        .all()
    )
    return [r.payload for r in rows]


def policy_persist(session: Optional[Session] = None) -> dict:
    if session is None:
        with get_session() as s:
            return _policy_persist(s)
    return _policy_persist(session)


def _policy_persist(session: Session) -> dict:
    session.query(PolicyRow).delete()
    session.flush()

    rows = session.query(AgentRow).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    counts: dict[str, int] = {}

    for row in rows:
        agent = Agent.model_validate(row.payload)
        events = _load_events(session, agent.event_ids)
        findings = _load_findings(session, agent.agent_id)

        rec = recommend_policy(agent, events, findings)

        payload = dict(row.payload)
        if rec is not None:
            payload["recommended_policy"] = rec.model_dump(mode="json")
            session.add(
                PolicyRow(
                    agent_id=agent.agent_id,
                    policy=rec.policy,
                    confidence=rec.confidence,
                    payload=rec.model_dump(mode="json"),
                )
            )
            counts[rec.policy] = counts.get(rec.policy, 0) + 1
        else:
            payload["recommended_policy"] = None

        row.payload = payload
        row.updated_at = now

    session.flush()
    return {
        "agents_recommended": sum(counts.values()),
        "by_policy": counts,
        "agents_total": len(rows),
    }


__all__ = [
    "POLICY_PHI",
    "POLICY_EGRESS",
    "POLICY_AUDIT",
    "POLICY_CATALOG",
    "recommend_policy",
    "policy_persist",
]
