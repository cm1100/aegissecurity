"""The correlator — assembles partial canonical events into Agent records.

Algorithm (Kruskal-flavored union-find):

  1. Index every event by every non-null identity key (nhi_id, workload_id,
     (host_id, pid), repo).
  2. Generate weighted candidate edges from those indexes. host+pid edges
     are only generated within a ±5 min window to defend against PID reuse.
  3. Sort candidate edges by confidence DESC.
  4. Iterate edges, calling union(); skip edges that would merge
     components carrying conflicting strong identifiers (nhi_id /
     workload_id).
  5. Each resulting component → one Agent. Per-agent confidence = min of
     used merge edges; single-event agents get 1.0.
  6. Field reconciliation is "union with priorities": data_classes / tools
     / destinations / permissions / imports accumulate; framework hints
     are kept with provenance for the classifier (Phase 4) to pick a
     winner; nhi_id / workload_id / repo come from the (single) value
     present after conflict-aware merging.

Determinism: agent_id is sha256(sorted cluster event_ids)[:12]. Re-running
correlation over the same canonical-event set yields the same agent_ids
and the same edges (we sort edge candidates by (confidence DESC, a, b)).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from aegis.correlate.confidence import (
    CONF_HOST_PID,
    CONF_NHI,
    CONF_REPO,
    CONF_WORKLOAD,
    HOST_PID_WINDOW_SECONDS,
)
from aegis.correlate.union_find import WeightedUnionFind
from aegis.schemas import (
    Agent,
    CanonicalEvent,
    CorrelationEdge,
    DataClass,
    EventSource,
    Framework,
    FrameworkHint,
    Provider,
)
from aegis.storage import (
    AgentRow,
    CorrelationEdgeRow,
    NormalizedEventRow,
    get_session,
)


@dataclass(frozen=True)
class _Candidate:
    event_a: str
    event_b: str
    confidence: float
    reason: str


@dataclass
class CorrelationResult:
    agents: list[Agent] = field(default_factory=list)
    edges_by_agent: dict[str, list[CorrelationEdge]] = field(default_factory=dict)


def _agent_id_for(cluster_event_ids: Iterable[str]) -> str:
    seed = ":".join(sorted(cluster_event_ids))
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"agent_{digest[:12]}"


def _ordered_unique(items: Iterable) -> list:
    seen: set = set()
    out: list = []
    for item in items:
        if item is None or item == "":
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _build_candidates(events: list[CanonicalEvent]) -> list[_Candidate]:
    candidates: list[_Candidate] = []

    def emit_pairs(group: list[CanonicalEvent], confidence: float, reason: str) -> None:
        if len(group) < 2:
            return
        sorted_ids = sorted(e.event_id for e in group)
        anchor = sorted_ids[0]
        for other in sorted_ids[1:]:
            a, b = sorted((anchor, other))
            candidates.append(_Candidate(a, b, confidence, reason))

    by_nhi: dict[str, list[CanonicalEvent]] = defaultdict(list)
    by_workload: dict[str, list[CanonicalEvent]] = defaultdict(list)
    by_host_pid: dict[tuple[str, int], list[CanonicalEvent]] = defaultdict(list)
    by_repo: dict[str, list[CanonicalEvent]] = defaultdict(list)

    for e in events:
        if e.nhi_id:
            by_nhi[e.nhi_id].append(e)
        if e.workload_id:
            by_workload[e.workload_id].append(e)
        if e.host_id and e.pid is not None:
            by_host_pid[(e.host_id, e.pid)].append(e)
        if e.repo:
            by_repo[e.repo].append(e)

    for group in by_nhi.values():
        emit_pairs(group, CONF_NHI, "shared nhi_id")

    for group in by_workload.values():
        emit_pairs(group, CONF_WORKLOAD, "shared workload_id")

    for (host_id, pid), group in by_host_pid.items():
        # pairwise within ±HOST_PID_WINDOW_SECONDS. PID reuse rule —
        # events of the same (host, pid) but far apart in time are
        # almost certainly different processes.
        sorted_group = sorted(group, key=lambda e: e.observed_at)
        for i in range(len(sorted_group)):
            for j in range(i + 1, len(sorted_group)):
                dt = (sorted_group[j].observed_at - sorted_group[i].observed_at).total_seconds()
                if dt > HOST_PID_WINDOW_SECONDS:
                    break
                a, b = sorted((sorted_group[i].event_id, sorted_group[j].event_id))
                candidates.append(
                    _Candidate(
                        a, b, CONF_HOST_PID,
                        f"shared host+pid within {int(dt)}s",
                    )
                )

    for group in by_repo.values():
        emit_pairs(group, CONF_REPO, "shared repo")

    candidates.sort(key=lambda c: (-c.confidence, c.event_a, c.event_b))
    return candidates


def _reconcile(
    events: list[CanonicalEvent],
    used_edges: list[CorrelationEdge],
) -> Agent:
    agent_id = _agent_id_for(e.event_id for e in events)

    nhi_ids = {e.nhi_id for e in events if e.nhi_id}
    workload_ids = {e.workload_id for e in events if e.workload_id}
    repos = {e.repo for e in events if e.repo}

    nhi_id: Optional[str] = next(iter(sorted(nhi_ids)), None)
    workload_id: Optional[str] = next(iter(sorted(workload_ids)), None)
    repo: Optional[str] = next(iter(sorted(repos)), None)

    host_ids = sorted({e.host_id for e in events if e.host_id})

    framework_alternatives: list[FrameworkHint] = []
    for e in events:
        hint_value = e.framework_hint
        if hint_value:
            framework_alternatives.append(
                FrameworkHint(
                    framework=Framework(hint_value) if isinstance(hint_value, str) else hint_value,
                    source_event_id=e.event_id,
                    source=EventSource(e.source) if isinstance(e.source, str) else e.source,
                    confidence=0.5,  # placeholder; the Phase 4 classifier rewrites Agent.framework
                    rationale="event-level framework_hint",
                )
            )

    provider_value = next((e.provider for e in events if e.provider), None)
    provider = (
        Provider(provider_value) if isinstance(provider_value, str) and provider_value else provider_value
    )
    model = next((e.model for e in events if e.model), None)

    data_classes_raw = []
    for e in events:
        for dc in e.data_classes:
            data_classes_raw.append(dc if isinstance(dc, DataClass) else DataClass(dc))
    data_classes = _ordered_unique(data_classes_raw)

    tools = _ordered_unique(t for e in events for t in e.tools_called)
    destinations = _ordered_unique(e.destination for e in events if e.destination)
    permissions = _ordered_unique(p for e in events for p in e.permissions)
    imports = _ordered_unique(imp for e in events for imp in e.imports)
    config_files = _ordered_unique(cf for e in events for cf in e.config_files)

    has_aegislib = any(e.has_aegislib for e in events)
    mcp_config_present = any(e.mcp_config_present for e in events)

    sources_raw = {e.source if isinstance(e.source, str) else e.source.value for e in events}
    sources = [EventSource(s) for s in sorted(sources_raw)]

    observed: list[datetime] = [e.observed_at for e in events]
    first_seen = min(observed)
    last_seen = max(observed)

    confidence = min((edge.confidence for edge in used_edges), default=1.0)

    return Agent(
        agent_id=agent_id,
        nhi_id=nhi_id,
        workload_id=workload_id,
        host_ids=host_ids,
        repo=repo,
        framework=None,  # the Phase 4 classifier assigns this
        framework_alternatives=framework_alternatives,
        provider=provider,
        model=model,
        data_classes=data_classes,
        tools=tools,
        destinations=destinations,
        permissions=permissions,
        imports=imports,
        config_files=config_files,
        has_aegislib=has_aegislib,
        mcp_config_present=mcp_config_present,
        event_ids=sorted(e.event_id for e in events),
        sources=sources,
        first_seen=first_seen,
        last_seen=last_seen,
        correlation_confidence=round(confidence, 4),
    )


def correlate(events: list[CanonicalEvent]) -> CorrelationResult:
    if not events:
        return CorrelationResult()

    events_by_id = {e.event_id: e for e in events}
    event_ids = sorted(events_by_id.keys())

    uf = WeightedUnionFind(event_ids)
    for e in events:
        uf.declare_nhi(e.event_id, e.nhi_id)
        uf.declare_workload(e.event_id, e.workload_id)

    candidates = _build_candidates(events)

    used_edges_global: list[CorrelationEdge] = []
    for cand in candidates:
        if uf.would_conflict(cand.event_a, cand.event_b):
            continue
        if uf.union(cand.event_a, cand.event_b):
            used_edges_global.append(
                CorrelationEdge(
                    event_a=cand.event_a,
                    event_b=cand.event_b,
                    reason=cand.reason,
                    confidence=cand.confidence,
                )
            )

    components = uf.components()

    result = CorrelationResult()
    for component in components:
        cluster_ids = set(component)
        cluster_events = [events_by_id[eid] for eid in component]
        cluster_edges = [
            e for e in used_edges_global
            if e.event_a in cluster_ids and e.event_b in cluster_ids
        ]
        agent = _reconcile(cluster_events, cluster_edges)
        result.agents.append(agent)
        result.edges_by_agent[agent.agent_id] = cluster_edges

    result.agents.sort(key=lambda a: a.agent_id)
    return result


def _load_canonical_events(session: Session) -> list[CanonicalEvent]:
    rows = session.query(NormalizedEventRow).all()
    return [CanonicalEvent.model_validate(r.payload) for r in rows]


def correlate_persist(session: Session | None = None) -> CorrelationResult:
    """Run correlation over every normalized event and replace the agent table.

    Idempotent — repeated calls produce the same agent rows.
    """
    if session is None:
        with get_session() as s:
            return _run_persist(s)
    return _run_persist(session)


def _run_persist(session: Session) -> CorrelationResult:
    events = _load_canonical_events(session)
    result = correlate(events)

    session.query(CorrelationEdgeRow).delete()
    session.query(AgentRow).delete()
    session.flush()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for agent in result.agents:
        session.add(
            AgentRow(
                agent_id=agent.agent_id,
                payload=agent.model_dump(mode="json"),
                correlation_confidence=agent.correlation_confidence,
                created_at=now,
                updated_at=now,
            )
        )
        for edge in result.edges_by_agent.get(agent.agent_id, []):
            session.add(
                CorrelationEdgeRow(
                    agent_id=agent.agent_id,
                    event_a=edge.event_a,
                    event_b=edge.event_b,
                    reason=edge.reason,
                    confidence=edge.confidence,
                )
            )
    session.flush()
    return result
