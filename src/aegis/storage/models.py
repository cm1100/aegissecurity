from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from aegis.storage.db import Base


class RawEventRow(Base):
    __tablename__ = "raw_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    observed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    content_hash: Mapped[str] = mapped_column(String, index=True, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class NormalizedEventRow(Base):
    __tablename__ = "normalized_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    nhi_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    workload_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    host_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    repo: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class AgentRow(Base):
    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    risk_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    risk_tier: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    correlation_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class CorrelationEdgeRow(Base):
    __tablename__ = "correlation_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String, index=True)
    event_a: Mapped[str] = mapped_column(String)
    event_b: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)


class FindingRow(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String, index=True)
    rule_id: Mapped[str] = mapped_column(String, index=True)
    severity: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PolicyRow(Base):
    __tablename__ = "policies"

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    policy: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
