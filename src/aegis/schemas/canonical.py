from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from aegis.schemas.enums import DataClass, EventSource, Framework, Provider
from aegis.schemas.findings import Evidence, PolicyRecommendation


class FrameworkHint(BaseModel):
    """One observation about an agent's framework, with provenance.

    Multiple hints can coexist on an agent; the reconciler picks a winner by
    source priority but retains the losers so the decision is auditable.
    """

    framework: Framework
    source_event_id: str
    source: EventSource
    confidence: float
    rationale: str


class CanonicalEvent(BaseModel):
    """The normalized event shape — every source flows into this."""

    model_config = ConfigDict(use_enum_values=True)

    event_id: str
    source: EventSource
    received_at: datetime
    observed_at: datetime

    nhi_id: Optional[str] = None
    host_id: Optional[str] = None
    pid: Optional[int] = None
    workload_id: Optional[str] = None
    repo: Optional[str] = None

    provider: Optional[Provider] = None
    model: Optional[str] = None
    framework_hint: Optional[Framework] = None
    tools_called: List[str] = Field(default_factory=list)
    data_classes: List[DataClass] = Field(default_factory=list)
    destination: Optional[str] = None

    imports: List[str] = Field(default_factory=list)
    mcp_config_present: bool = False
    has_aegislib: bool = False
    config_files: List[str] = Field(default_factory=list)

    permissions: List[str] = Field(default_factory=list)
    role_arn: Optional[str] = None
    last_rotated: Optional[datetime] = None

    action: Optional[str] = None
    resource: Optional[str] = None
    actor: Optional[str] = None

    syscalls: List[str] = Field(default_factory=list)
    bytes_out: Optional[int] = None

    content_hash: str


class Agent(BaseModel):
    """The merged record — one logical AI agent assembled from many events."""

    model_config = ConfigDict(use_enum_values=True)

    agent_id: str

    nhi_id: Optional[str] = None
    workload_id: Optional[str] = None
    host_ids: List[str] = Field(default_factory=list)
    repo: Optional[str] = None

    framework: Optional[Framework] = None
    framework_alternatives: List[FrameworkHint] = Field(default_factory=list)
    framework_confidence: float = 0.0
    framework_evidence: List[Evidence] = Field(default_factory=list)
    framework_rule: Optional[str] = None

    provider: Optional[Provider] = None
    model: Optional[str] = None

    data_classes: List[DataClass] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    destinations: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    imports: List[str] = Field(default_factory=list)
    config_files: List[str] = Field(default_factory=list)
    has_aegislib: bool = False
    mcp_config_present: bool = False

    event_ids: List[str] = Field(default_factory=list)
    sources: List[EventSource] = Field(default_factory=list)
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None

    correlation_confidence: float = 1.0

    recommended_policy: Optional[PolicyRecommendation] = None
