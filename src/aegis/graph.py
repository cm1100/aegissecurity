"""Graph projection of an Agent.

Spec bonus: Agent → Identity → Tools → Data Classes → Policy

The graph is a portable node-link JSON usable by d3, Cytoscape, or any
graph viz. Each node has a stable id, a type, and a label; each edge has
a labeled relationship. The CLI renders the same graph as an ASCII tree
so reviewers can see it without a viz tool.
"""

from __future__ import annotations

from typing import Any, Optional

from aegis.schemas import Agent
from aegis.storage import AgentRow, get_session


def _node(node_id: str, type_: str, label: str, **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "type": type_, "label": label, **extra}


def _edge(src: str, dst: str, label: str) -> dict[str, str]:
    return {"source": src, "target": dst, "label": label}


def build_graph(agent: Agent) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    agent_node_id = agent.agent_id
    # The Agent model doesn't carry risk_score/tier directly (they live in
    # the AgentRow alongside the payload). The graph projection includes
    # only what's on the Agent struct; the caller (CLI / API) can supplement.

    nodes.append(_node(
        agent_node_id, "agent", agent.agent_id,
        framework=str(agent.framework) if agent.framework else None,
        correlation_confidence=agent.correlation_confidence,
    ))

    # Identity layer
    if agent.nhi_id:
        nid = f"nhi:{agent.nhi_id}"
        nodes.append(_node(nid, "identity", agent.nhi_id, subtype="nhi"))
        edges.append(_edge(agent_node_id, nid, "uses_identity"))
    if agent.workload_id:
        nid = f"workload:{agent.workload_id}"
        nodes.append(_node(nid, "identity", agent.workload_id, subtype="workload"))
        edges.append(_edge(agent_node_id, nid, "deployed_in"))
    if agent.repo:
        nid = f"repo:{agent.repo}"
        nodes.append(_node(nid, "identity", agent.repo, subtype="repo"))
        edges.append(_edge(agent_node_id, nid, "built_from"))
    for host in agent.host_ids:
        nid = f"host:{host}"
        nodes.append(_node(nid, "identity", host, subtype="host"))
        edges.append(_edge(agent_node_id, nid, "runs_on"))

    # Framework
    if agent.framework:
        fw = agent.framework if isinstance(agent.framework, str) else agent.framework.value
        nid = f"framework:{fw}"
        nodes.append(_node(nid, "framework", fw))
        edges.append(_edge(agent_node_id, nid, "classified_as"))

    # Tools
    for tool in agent.tools:
        nid = f"tool:{tool}"
        nodes.append(_node(nid, "tool", tool))
        edges.append(_edge(agent_node_id, nid, "calls"))

    # Destinations
    for dest in agent.destinations:
        nid = f"destination:{dest}"
        nodes.append(_node(nid, "destination", dest))
        edges.append(_edge(agent_node_id, nid, "talks_to"))

    # Data classes
    for dc in agent.data_classes:
        dc_value = dc if isinstance(dc, str) else dc.value
        nid = f"data_class:{dc_value}"
        nodes.append(_node(nid, "data_class", dc_value))
        edges.append(_edge(agent_node_id, nid, "accesses"))

    # Policy
    if agent.recommended_policy:
        policy_name = agent.recommended_policy.policy
        nid = f"policy:{policy_name}"
        nodes.append(_node(
            nid, "policy", policy_name,
            confidence=agent.recommended_policy.confidence,
        ))
        edges.append(_edge(agent_node_id, nid, "recommended_policy"))

    return {
        "agent_id": agent.agent_id,
        "nodes": nodes,
        "edges": edges,
    }


def graph_for_agent_id(agent_id: str) -> Optional[dict[str, Any]]:
    with get_session() as s:
        row = s.get(AgentRow, agent_id)
        if row is None:
            return None
        agent = Agent.model_validate(row.payload)
    return build_graph(agent)


def render_ascii_tree(graph: dict[str, Any]) -> str:
    """Render a graph as an indented tree grouped by node type.

    Reviewers who can't paste JSON into a viz tool still get a readable
    representation in the terminal.
    """
    by_type: dict[str, list[dict[str, Any]]] = {}
    for n in graph["nodes"]:
        if n["type"] == "agent":
            continue
        by_type.setdefault(n["type"], []).append(n)

    lines: list[str] = []
    agent_node = next(n for n in graph["nodes"] if n["type"] == "agent")
    lines.append(f"agent  {agent_node['id']}")

    type_order = ["identity", "framework", "tool", "destination",
                  "data_class", "policy"]
    groups = [(t, by_type[t]) for t in type_order if t in by_type]

    for i, (t, members) in enumerate(groups):
        last_group = (i == len(groups) - 1)
        prefix = "└── " if last_group else "├── "
        sub_prefix = "    " if last_group else "│   "
        lines.append(f"{prefix}{t}")
        for j, m in enumerate(members):
            last = (j == len(members) - 1)
            mark = "└── " if last else "├── "
            extras = ""
            if m["type"] == "policy" and "confidence" in m:
                extras = f"  (confidence {m['confidence']})"
            if m["type"] == "identity" and "subtype" in m:
                extras = f"  [{m['subtype']}]"
            lines.append(f"{sub_prefix}{mark}{m['label']}{extras}")

    return "\n".join(lines)
