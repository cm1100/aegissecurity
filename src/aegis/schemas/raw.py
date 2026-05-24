from __future__ import annotations

from datetime import datetime
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from aegis.schemas.enums import DataClass, EventSource


class RawEventBase(BaseModel):
    """Common fields across every raw event source.

    Every identity field is optional — the central design assumption is that
    real signals arrive partial and only become a coherent agent after
    correlation.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    event_id: Optional[str] = Field(
        default=None,
        description="Caller-provided id. If absent the ingest layer assigns one.",
    )
    timestamp: datetime = Field(description="When the source observed the event.")

    nhi_id: Optional[str] = None
    host_id: Optional[str] = None
    pid: Optional[int] = None
    workload_id: Optional[str] = None
    repo: Optional[str] = None

    provider: Optional[str] = None
    model: Optional[str] = None
    framework_hint: Optional[str] = None
    tools_called: List[str] = Field(default_factory=list)
    data_classes: List[DataClass] = Field(default_factory=list)
    destination: Optional[str] = None


class RuntimeEvent(RawEventBase):
    """Simulates eBPF / network capture telemetry."""

    source: Literal[EventSource.RUNTIME] = EventSource.RUNTIME
    syscalls: List[str] = Field(default_factory=list)
    bytes_out: Optional[int] = None


class NHIManifest(RawEventBase):
    """Simulates cloud IAM / identity inventory snapshots."""

    source: Literal[EventSource.NHI] = EventSource.NHI
    role_arn: Optional[str] = None
    permissions: List[str] = Field(default_factory=list)
    last_rotated: Optional[datetime] = None


class CapabilityHint(RawEventBase):
    """Simulates repo scan findings."""

    source: Literal[EventSource.REPO] = EventSource.REPO
    imports: List[str] = Field(default_factory=list)
    mcp_config_present: bool = False
    has_aegislib: bool = False
    config_files: List[str] = Field(default_factory=list)


class SaaSAuditEvent(RawEventBase):
    """Simulates SaaS / audit-log events."""

    source: Literal[EventSource.SAAS] = EventSource.SAAS
    action: Optional[str] = None
    resource: Optional[str] = None
    actor: Optional[str] = None


RawEvent = Annotated[
    Union[RuntimeEvent, NHIManifest, CapabilityHint, SaaSAuditEvent],
    Field(discriminator="source"),
]
