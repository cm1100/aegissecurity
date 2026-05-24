"""Risk pipeline orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from aegis.risk.baselines import BaselineRegistry, default_registry
from aegis.risk.rules import evaluate_rules
from aegis.risk.scorer import score_agent
from aegis.schemas import Agent, CanonicalEvent
from aegis.storage import AgentRow, FindingRow, NormalizedEventRow, get_session


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


def risk_persist(
    session: Optional[Session] = None,
    baselines: Optional[BaselineRegistry] = None,
) -> dict[str, int]:
    if session is None:
        with get_session() as s:
            return _risk_persist(s, baselines)
    return _risk_persist(session, baselines)


def _risk_persist(
    session: Session, baselines: Optional[BaselineRegistry]
) -> dict[str, int]:
    registry = baselines if baselines is not None else default_registry()

    # Replace all findings + agent risk fields on every run (idempotent).
    session.query(FindingRow).delete()
    session.flush()

    rows = session.query(AgentRow).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    findings_total = 0
    high = medium = low = 0

    for row in rows:
        agent = Agent.model_validate(row.payload)
        events = _load_events(session, agent.event_ids)
        baseline = registry.for_workload(agent.workload_id)
        findings = evaluate_rules(agent, events, baseline)
        score = score_agent(agent, findings)

        # persist findings
        for f in findings:
            session.add(
                FindingRow(
                    agent_id=agent.agent_id,
                    rule_id=f.rule_id,
                    severity=f.severity if isinstance(f.severity, str) else f.severity.value,
                    payload=f.model_dump(mode="json"),
                )
            )

        # patch agent payload with score + tier + factor breakdown + findings refs
        # dict copy is required so SQLAlchemy sees a new object and persists the mutation.
        payload = dict(row.payload)
        payload["risk_score"] = score.total
        payload["risk_tier"] = score.tier.value if hasattr(score.tier, "value") else score.tier
        payload["risk_factors"] = [
            {
                "name": fac.name,
                "value": fac.value,
                "max_value": fac.max_value,
                "rationale": fac.rationale,
            }
            for fac in score.factors
        ]
        payload["finding_rule_ids"] = [f.rule_id for f in findings]
        row.payload = payload
        row.risk_score = score.total
        row.risk_tier = payload["risk_tier"]
        row.updated_at = now

        findings_total += len(findings)
        tier = payload["risk_tier"]
        if tier == "HIGH":
            high += 1
        elif tier == "MEDIUM":
            medium += 1
        else:
            low += 1

    session.flush()
    return {
        "agents_scored": len(rows),
        "findings": findings_total,
        "tiers": {"HIGH": high, "MEDIUM": medium, "LOW": low},
    }
