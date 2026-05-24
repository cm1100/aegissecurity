from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from aegis.classify import classify_persist
from aegis.correlate import correlate_persist
from aegis.ingest import ingest_many, ingest_raw
from aegis.normalize import normalize_pending
from aegis.policy import policy_persist
from aegis.risk import risk_persist
from aegis.storage import (
    AgentRow,
    CorrelationEdgeRow,
    FindingRow,
    NormalizedEventRow,
    RawEventRow,
    get_session,
)

router = APIRouter()


class IngestResponse(BaseModel):
    event_id: str
    deduped: bool
    content_hash: str


class BatchIngestResponse(BaseModel):
    results: list[IngestResponse]
    total: int
    deduped: int


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/events", response_model=IngestResponse, status_code=201)
def ingest_event(payload: dict[str, Any]) -> IngestResponse:
    try:
        with get_session() as session:
            result = ingest_raw(session, payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())
    return IngestResponse(
        event_id=result.event_id,
        deduped=result.deduped,
        content_hash=result.content_hash,
    )


@router.post("/events/batch", response_model=BatchIngestResponse, status_code=201)
def ingest_events_batch(payloads: list[dict[str, Any]]) -> BatchIngestResponse:
    try:
        with get_session() as session:
            results = ingest_many(session, payloads)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())
    return BatchIngestResponse(
        results=[
            IngestResponse(
                event_id=r.event_id, deduped=r.deduped, content_hash=r.content_hash
            )
            for r in results
        ],
        total=len(results),
        deduped=sum(1 for r in results if r.deduped),
    )


@router.get("/events")
def list_events(source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with get_session() as session:
        q = session.query(RawEventRow)
        if source:
            q = q.filter(RawEventRow.source == source)
        rows = q.order_by(RawEventRow.observed_at.desc()).limit(limit).all()
        return [
            {
                "event_id": r.event_id,
                "source": r.source,
                "observed_at": r.observed_at.isoformat(),
                "received_at": r.received_at.isoformat(),
                "content_hash": r.content_hash,
                "payload": r.payload,
            }
            for r in rows
        ]


@router.get("/events/normalized")
def list_normalized_events(
    source: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    with get_session() as session:
        q = session.query(NormalizedEventRow)
        if source:
            q = q.filter(NormalizedEventRow.source == source)
        rows = q.order_by(NormalizedEventRow.observed_at.desc()).limit(limit).all()
        return [r.payload for r in rows]


@router.post("/pipeline/normalize")
def run_normalize() -> dict[str, int]:
    """Idempotently normalize any raw events that don't yet have a canonical row."""
    with get_session() as session:
        n = normalize_pending(session)
    return {"normalized": n}


@router.post("/pipeline/classify")
def run_classify() -> dict[str, int]:
    """Assign a framework label to every agent based on its assembled signals."""
    with get_session() as session:
        n = classify_persist(session)
    return {"classified": n}


@router.post("/pipeline/risk")
def run_risk() -> dict[str, Any]:
    """Evaluate risk rules + score every agent. Idempotent."""
    with get_session() as session:
        return risk_persist(session)


@router.post("/pipeline/policy")
def run_policy() -> dict[str, Any]:
    """Recommend a policy for every agent. Idempotent."""
    with get_session() as session:
        return policy_persist(session)


@router.post("/pipeline/run")
def run_full_pipeline() -> dict[str, Any]:
    """Run the full discovery pipeline:
    normalize_pending → correlate → classify → risk → policy.

    Idempotent end-to-end — re-running over the same ingested events
    yields the same agents, the same findings, the same policy
    recommendations.
    """
    with get_session() as session:
        normalized = normalize_pending(session)
        corr = correlate_persist(session)
        classified = classify_persist(session)
        risk = risk_persist(session)
        policy = policy_persist(session)
    return {
        "normalized": normalized,
        "agents": len(corr.agents),
        "classified": classified,
        "risk": risk,
        "policy": policy,
    }


@router.post("/pipeline/correlate")
def run_correlate() -> dict[str, Any]:
    """Re-run correlation over all canonical events and replace the agent table.

    Idempotent — repeated calls over the same canonical events produce the
    same agent_ids and the same correlation edges.
    """
    with get_session() as session:
        result = correlate_persist(session)
    return {
        "agents": len(result.agents),
        "total_edges": sum(len(v) for v in result.edges_by_agent.values()),
    }


@router.get("/agents")
def list_agents() -> list[dict[str, Any]]:
    with get_session() as session:
        rows = session.query(AgentRow).order_by(AgentRow.agent_id).all()
        return [
            {
                "agent_id": r.agent_id,
                "nhi_id": r.payload.get("nhi_id"),
                "workload_id": r.payload.get("workload_id"),
                "repo": r.payload.get("repo"),
                "host_ids": r.payload.get("host_ids", []),
                "framework": r.payload.get("framework"),
                "framework_confidence": r.payload.get("framework_confidence", 0.0),
                "framework_rule": r.payload.get("framework_rule"),
                "provider": r.payload.get("provider"),
                "data_classes": r.payload.get("data_classes", []),
                "tools": r.payload.get("tools", []),
                "destinations": r.payload.get("destinations", []),
                "has_aegislib": r.payload.get("has_aegislib", False),
                "correlation_confidence": r.correlation_confidence,
                "risk_score": r.risk_score,
                "risk_tier": r.risk_tier,
                "recommended_policy": (
                    (r.payload.get("recommended_policy") or {}).get("policy")
                    if r.payload.get("recommended_policy") else None
                ),
                "policy_confidence": (
                    (r.payload.get("recommended_policy") or {}).get("confidence")
                    if r.payload.get("recommended_policy") else None
                ),
                "event_count": len(r.payload.get("event_ids", [])),
                "sources": r.payload.get("sources", []),
            }
            for r in rows
        ]


@router.get("/agents/{agent_id}/graph")
def get_agent_graph(agent_id: str) -> dict[str, Any]:
    """Return the agent's projection as a node-link graph.

    Format is generic enough for d3, Cytoscape, and similar viz libraries.
    Shape: { agent_id, nodes: [{id, type, label, ...}], edges: [{source, target, label}] }.
    """
    from aegis.graph import graph_for_agent_id

    g = graph_for_agent_id(agent_id)
    if g is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    return g


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str) -> dict[str, Any]:
    with get_session() as session:
        row = session.get(AgentRow, agent_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        edges = (
            session.query(CorrelationEdgeRow)
            .filter(CorrelationEdgeRow.agent_id == agent_id)
            .all()
        )
        finding_rows = (
            session.query(FindingRow)
            .filter(FindingRow.agent_id == agent_id)
            .all()
        )
        return {
            "agent": row.payload,
            "correlation_edges": [
                {
                    "event_a": e.event_a,
                    "event_b": e.event_b,
                    "reason": e.reason,
                    "confidence": e.confidence,
                }
                for e in edges
            ],
            "findings": [f.payload for f in finding_rows],
        }
