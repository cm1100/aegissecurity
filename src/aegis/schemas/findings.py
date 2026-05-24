from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

from aegis.schemas.enums import RiskTier


class Evidence(BaseModel):
    """A structured evidence atom carried through findings, scoring, and policy.

    Strings make for cute output but break programmatic chaining; carrying the
    source event id and a numeric confidence keeps the whole evidence graph
    queryable and auditable.
    """

    claim: str
    source_event_id: Optional[str] = None
    confidence: float = 1.0


class RiskFinding(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    rule_id: str
    rule_name: str
    severity: RiskTier
    description: str
    evidence: List[Evidence] = Field(default_factory=list)


class PolicyRecommendation(BaseModel):
    """A policy choice + structured evidence.

    `evidence` is the rich form (claim + source_event_id + confidence) for
    programmatic chaining. `evidence_summary` is the spec-shape flat list
    of claim strings, derived automatically — so consumers comparing
    literal output to the spec's example see a matching shape, while
    consumers that want attribution can read the structured form.
    """

    policy: str
    confidence: float
    evidence: List[Evidence] = Field(default_factory=list)
    alternatives_considered: List[str] = Field(default_factory=list)

    @computed_field
    @property
    def evidence_summary(self) -> List[str]:
        return [e.claim for e in self.evidence]


class CorrelationEdge(BaseModel):
    """One audit row explaining why two events were merged into the same agent."""

    event_a: str
    event_b: str
    reason: str
    confidence: float
