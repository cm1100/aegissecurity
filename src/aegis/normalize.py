"""Raw → canonical normalization.

Pure mapping of source-specific raw events into one canonical shape. Does not
classify (that's Phase 4) and does not correlate (Phase 3) — it just produces
a uniform record per event so downstream stages don't care which source
emitted it.

Coercion rules:
  - framework_hint string → Framework enum (case/punctuation tolerant);
    unknown strings become Framework.UNKNOWN so we record "saw something" rather
    than silently dropping the signal.
  - provider string → Provider enum; if absent, derived from the destination
    (api.anthropic.com → anthropic, etc.).
  - timestamp → UTC naive datetime for consistent comparison.
  - tools/imports → lowercase + dedup (preserves first-occurrence order).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from aegis.schemas import (
    CanonicalEvent,
    EventSource,
    Framework,
    Provider,
    RawEvent,
)
from aegis.schemas.enums import EXTERNAL_LLM_DOMAINS
from aegis.storage import NormalizedEventRow, RawEventRow

_FRAMEWORK_ALIASES: dict[str, Framework] = {
    "langchain": Framework.LANGCHAIN,
    "lang_chain": Framework.LANGCHAIN,
    "langgraph": Framework.LANGGRAPH,
    "lang_graph": Framework.LANGGRAPH,
    "crewai": Framework.CREWAI,
    "crew_ai": Framework.CREWAI,
    "llama_index": Framework.LLAMA_INDEX,
    "llamaindex": Framework.LLAMA_INDEX,
    "autogen": Framework.AUTOGEN,
    "mcp": Framework.MCP_AGENT,
    "mcp_agent": Framework.MCP_AGENT,
    "direct_sdk_agentic": Framework.DIRECT_SDK_AGENTIC,
    "direct_sdk": Framework.DIRECT_SDK_AGENTIC,
    "llm_caller": Framework.LLM_CALLER,
}

_PROVIDER_ALIASES: dict[str, Provider] = {
    "anthropic": Provider.ANTHROPIC,
    "openai": Provider.OPENAI,
    "google": Provider.GOOGLE,
    "gemini": Provider.GOOGLE,
    "bedrock": Provider.BEDROCK,
    "aws_bedrock": Provider.BEDROCK,
    "azure": Provider.AZURE_OPENAI,
    "azure_openai": Provider.AZURE_OPENAI,
}


def _norm_key(s: str) -> str:
    return s.strip().lower().replace("-", "_").replace(" ", "_")


def coerce_framework(value: Optional[str]) -> Optional[Framework]:
    if not value:
        return None
    return _FRAMEWORK_ALIASES.get(_norm_key(value), Framework.UNKNOWN)


def coerce_provider(value: Optional[str]) -> Optional[Provider]:
    if not value:
        return None
    return _PROVIDER_ALIASES.get(_norm_key(value), Provider.OTHER)


def derive_provider_from_destination(destination: Optional[str]) -> Optional[Provider]:
    if not destination:
        return None
    return EXTERNAL_LLM_DOMAINS.get(destination.strip().lower())


def _norm_list(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not item:
            continue
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _to_utc_naive(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    raise ValueError(f"unparseable timestamp: {value!r}")


def normalize_event(
    raw: RawEvent,
    *,
    event_id: str,
    received_at: datetime,
    content_hash: str,
) -> CanonicalEvent:
    observed_at = _to_utc_naive(raw.timestamp)
    received_at = _to_utc_naive(received_at)

    framework = coerce_framework(raw.framework_hint)
    provider = coerce_provider(raw.provider) or derive_provider_from_destination(raw.destination)

    tools = _norm_list(raw.tools_called)

    source_extras: dict[str, Any] = {}
    if raw.source == EventSource.REPO or raw.source == EventSource.REPO.value:
        source_extras["imports"] = _norm_list(getattr(raw, "imports", []))
        source_extras["mcp_config_present"] = bool(getattr(raw, "mcp_config_present", False))
        source_extras["has_aegislib"] = bool(getattr(raw, "has_aegislib", False))
        source_extras["config_files"] = _norm_list(getattr(raw, "config_files", []))
    elif raw.source == EventSource.NHI or raw.source == EventSource.NHI.value:
        source_extras["permissions"] = _norm_list(getattr(raw, "permissions", []))
        source_extras["role_arn"] = getattr(raw, "role_arn", None)
        last_rotated = getattr(raw, "last_rotated", None)
        source_extras["last_rotated"] = _to_utc_naive(last_rotated) if last_rotated else None
    elif raw.source == EventSource.SAAS or raw.source == EventSource.SAAS.value:
        source_extras["action"] = getattr(raw, "action", None)
        source_extras["resource"] = getattr(raw, "resource", None)
        source_extras["actor"] = getattr(raw, "actor", None)
    elif raw.source == EventSource.RUNTIME or raw.source == EventSource.RUNTIME.value:
        source_extras["syscalls"] = _norm_list(getattr(raw, "syscalls", []))
        source_extras["bytes_out"] = getattr(raw, "bytes_out", None)

    return CanonicalEvent(
        event_id=event_id,
        source=EventSource(raw.source) if isinstance(raw.source, str) else raw.source,
        received_at=received_at,
        observed_at=observed_at,
        nhi_id=raw.nhi_id,
        host_id=raw.host_id,
        pid=raw.pid,
        workload_id=raw.workload_id,
        repo=raw.repo,
        provider=provider,
        model=raw.model,
        framework_hint=framework,
        tools_called=tools,
        data_classes=list(raw.data_classes),
        destination=raw.destination,
        content_hash=content_hash,
        **source_extras,
    )


def _row_to_canonical(row: RawEventRow) -> CanonicalEvent:
    """Reconstitute a RawEvent from its persisted payload and normalize it."""
    from aegis.ingest import parse_raw_event

    raw = parse_raw_event(row.payload)
    return normalize_event(
        raw,
        event_id=row.event_id,
        received_at=row.received_at,
        content_hash=row.content_hash,
    )


def persist_normalized(session: Session, canonical: CanonicalEvent) -> NormalizedEventRow:
    existing = session.get(NormalizedEventRow, canonical.event_id)
    if existing is not None:
        return existing
    row = NormalizedEventRow(
        event_id=canonical.event_id,
        source=canonical.source if isinstance(canonical.source, str) else canonical.source.value,
        observed_at=canonical.observed_at,
        nhi_id=canonical.nhi_id,
        workload_id=canonical.workload_id,
        host_id=canonical.host_id,
        pid=canonical.pid,
        repo=canonical.repo,
        payload=canonical.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    return row


def normalize_and_persist(session: Session, raw_row: RawEventRow) -> CanonicalEvent:
    canonical = _row_to_canonical(raw_row)
    persist_normalized(session, canonical)
    return canonical


def normalize_pending(session: Session) -> int:
    """Normalize every raw event that does not yet have a canonical row.

    Idempotent — safe to call any number of times.
    """
    normalized_ids = {row.event_id for row in session.query(NormalizedEventRow.event_id).all()}
    raw_rows = session.query(RawEventRow).all()
    count = 0
    for raw_row in raw_rows:
        if raw_row.event_id in normalized_ids:
            continue
        normalize_and_persist(session, raw_row)
        count += 1
    return count


__all__ = [
    "coerce_framework",
    "coerce_provider",
    "derive_provider_from_destination",
    "normalize_event",
    "normalize_and_persist",
    "normalize_pending",
    "persist_normalized",
]
