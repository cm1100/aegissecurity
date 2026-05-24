"""Fingerprint classifier — deterministic, rule-based framework labeling.

Input: an Agent (assembled by the correlator) plus the canonical events
that produced it. Output: a framework label, a confidence, a structured
evidence chain, and the name of the rule that fired.

Priority ladder (first match wins):
  1. Specific framework import detected   — LangGraph > LangChain > CrewAI
                                             > LlamaIndex > AutoGen.
     Imports are the strongest signal because they prove what the code
     was *built with*, not what the runtime happened to call.
  2. MCP config present, no framework import
                                          — pure-MCP agent.
     When a framework like LangChain *also* uses MCP for tools, the
     framework wins; mcp_config_present remains as a capability flag.
  3. Explicit framework_hint in event payload (no imports observed)
                                          — trust customer-declared
     framework hint at medium confidence when no stronger signal exists.
  4. External LLM destination + tool calls observed
                                          — direct SDK / agentic LLM
     pattern (Anthropic/OpenAI with tools, etc.).
  5. External LLM destination, no tool calls
                                          — bare LLM caller.
  6. No signals                           — UNKNOWN at confidence 0.

Each rule emits structured `Evidence` items with the source event id
attached when attributable, so the classification can be chained into
risk/policy reasoning downstream.

For the ML evolution discussed in the README: same input features
(imports, destinations, tools, config files, framework hints) become a
labeled training set. The rule ladder is the bootstrapping classifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from aegis.schemas import (
    Agent,
    CanonicalEvent,
    Evidence,
    Framework,
)
from aegis.storage import AgentRow, NormalizedEventRow, get_session
from aegis.utils import is_external_llm


# Order matters: most-specific imports first. LangGraph is built on
# LangChain, so seeing both should classify as LangGraph (more specific).
FRAMEWORK_IMPORT_RULES: list[tuple[str, Framework]] = [
    ("langgraph", Framework.LANGGRAPH),
    ("langchain", Framework.LANGCHAIN),
    ("crewai", Framework.CREWAI),
    ("llama_index", Framework.LLAMA_INDEX),
    ("autogen", Framework.AUTOGEN),
]

_FRAMEWORK_IMPORT_NAMES = {name for name, _ in FRAMEWORK_IMPORT_RULES}


@dataclass
class ClassificationResult:
    framework: Framework
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    rule_name: str = ""


def _rule_framework_import(
    agent: Agent, events: list[CanonicalEvent]
) -> Optional[ClassificationResult]:
    for import_name, framework in FRAMEWORK_IMPORT_RULES:
        if import_name not in agent.imports:
            continue
        evidence: list[Evidence] = []
        for ev in events:
            if import_name in ev.imports:
                evidence.append(
                    Evidence(
                        claim=f"repo imports '{import_name}'",
                        source_event_id=ev.event_id,
                        confidence=0.95,
                    )
                )
        # Note when other framework imports coexist (auditable, not blocking).
        other_imports = [
            i for i in agent.imports
            if i in _FRAMEWORK_IMPORT_NAMES and i != import_name
        ]
        if other_imports:
            evidence.append(
                Evidence(
                    claim=f"coexisting framework imports also detected: {sorted(other_imports)}",
                    confidence=0.5,
                )
            )
        return ClassificationResult(
            framework=framework,
            confidence=0.95,
            evidence=evidence,
            rule_name=f"framework_import:{import_name}",
        )
    return None


def _rule_mcp(
    agent: Agent, events: list[CanonicalEvent]
) -> Optional[ClassificationResult]:
    if not agent.mcp_config_present:
        return None
    # If a higher-level framework owns the agent, MCP is just a capability.
    if any(imp in _FRAMEWORK_IMPORT_NAMES for imp in agent.imports):
        return None

    evidence: list[Evidence] = []
    for ev in events:
        if ev.mcp_config_present:
            cfg = ev.config_files or ["mcp.json"]
            evidence.append(
                Evidence(
                    claim=f"MCP configuration detected: {cfg}",
                    source_event_id=ev.event_id,
                    confidence=0.90,
                )
            )
    if not evidence:
        evidence.append(
            Evidence(claim="agent flagged mcp_config_present", confidence=0.85)
        )
    return ClassificationResult(
        framework=Framework.MCP_AGENT,
        confidence=0.90,
        evidence=evidence,
        rule_name="mcp_config_present",
    )


def _rule_explicit_hint(
    agent: Agent, events: list[CanonicalEvent]
) -> Optional[ClassificationResult]:
    """Trust customer-declared framework_hint when no imports tell us otherwise."""
    if not agent.framework_alternatives:
        return None

    counts: dict[Framework, list] = {}
    for alt in agent.framework_alternatives:
        raw = alt.framework
        try:
            fw = Framework(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if fw == Framework.UNKNOWN:
            continue
        counts.setdefault(fw, []).append(alt)

    if not counts:
        return None

    # Most-common-wins; ties broken by Framework enum order for determinism.
    chosen = max(counts.keys(), key=lambda k: (len(counts[k]), -list(Framework).index(k)))
    matching = counts[chosen]

    evidence = [
        Evidence(
            claim=f"explicit framework_hint declares '{chosen.value}'",
            source_event_id=alt.source_event_id,
            confidence=0.80,
        )
        for alt in matching
    ]
    if len(counts) > 1:
        other_labels = sorted(fw.value for fw in counts if fw != chosen)
        evidence.append(
            Evidence(
                claim=f"conflicting framework_hints also seen: {other_labels}",
                confidence=0.4,
            )
        )
    return ClassificationResult(
        framework=chosen,
        confidence=0.80,
        evidence=evidence,
        rule_name="explicit_framework_hint",
    )


def _rule_direct_sdk_agentic(
    agent: Agent, events: list[CanonicalEvent]
) -> Optional[ClassificationResult]:
    external_dests = [d for d in agent.destinations if is_external_llm(d)]
    if not external_dests or not agent.tools:
        return None
    evidence: list[Evidence] = []
    for ev in events:
        if is_external_llm(ev.destination):
            evidence.append(
                Evidence(
                    claim=f"external LLM call to {ev.destination}",
                    source_event_id=ev.event_id,
                    confidence=0.80,
                )
            )
        if ev.tools_called:
            evidence.append(
                Evidence(
                    claim=f"tool-use observed: {sorted(set(ev.tools_called))}",
                    source_event_id=ev.event_id,
                    confidence=0.75,
                )
            )
    return ClassificationResult(
        framework=Framework.DIRECT_SDK_AGENTIC,
        confidence=0.80,
        evidence=evidence,
        rule_name="external_llm_with_tool_use",
    )


def _rule_llm_caller(
    agent: Agent, events: list[CanonicalEvent]
) -> Optional[ClassificationResult]:
    external_dests = [d for d in agent.destinations if is_external_llm(d)]
    if not external_dests:
        return None
    evidence: list[Evidence] = []
    for ev in events:
        if is_external_llm(ev.destination):
            evidence.append(
                Evidence(
                    claim=f"external LLM call to {ev.destination} (no tool-use observed)",
                    source_event_id=ev.event_id,
                    confidence=0.60,
                )
            )
    return ClassificationResult(
        framework=Framework.LLM_CALLER,
        confidence=0.60,
        evidence=evidence,
        rule_name="external_llm_call_only",
    )


_RULES: list[Callable[[Agent, list[CanonicalEvent]], Optional[ClassificationResult]]] = [
    _rule_framework_import,
    _rule_mcp,
    _rule_explicit_hint,
    _rule_direct_sdk_agentic,
    _rule_llm_caller,
]


def classify(agent: Agent, events: list[CanonicalEvent]) -> ClassificationResult:
    for rule in _RULES:
        result = rule(agent, events)
        if result is not None:
            return result
    return ClassificationResult(
        framework=Framework.UNKNOWN,
        confidence=0.0,
        evidence=[],
        rule_name="no_signals",
    )


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


def classify_persist(session: Session | None = None) -> int:
    """Classify every agent currently in storage. Idempotent."""
    if session is None:
        with get_session() as s:
            return _classify_persist(s)
    return _classify_persist(session)


def _classify_persist(session: Session) -> int:
    rows = session.query(AgentRow).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in rows:
        agent = Agent.model_validate(row.payload)
        events = _load_events(session, agent.event_ids)
        result = classify(agent, events)

        agent.framework = result.framework
        agent.framework_confidence = round(result.confidence, 4)
        agent.framework_evidence = list(result.evidence)
        agent.framework_rule = result.rule_name

        row.payload = agent.model_dump(mode="json")
        row.updated_at = now
    session.flush()
    return len(rows)


__all__ = [
    "ClassificationResult",
    "classify",
    "classify_persist",
    "FRAMEWORK_IMPORT_RULES",
]
