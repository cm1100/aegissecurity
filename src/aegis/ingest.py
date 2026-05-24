"""Raw event ingestion.

Validates the incoming payload against the discriminated RawEvent union,
computes a content hash for idempotent dedup, and persists the row. Stays
deliberately thin — normalization is a separate phase so that the raw store
remains a faithful audit log of what arrived.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy.orm import Session

from aegis.schemas import RawEvent
from aegis.storage import RawEventRow

_raw_adapter: TypeAdapter[RawEvent] = TypeAdapter(RawEvent)


@dataclass
class IngestResult:
    event_id: str
    deduped: bool
    content_hash: str


def _content_hash(payload: dict[str, Any]) -> str:
    """Hash the identity-bearing fields of a raw event for dedup.

    event_id is excluded — two clients may send the same logical event with
    different ids, and we want them collapsed. received_at is also excluded
    because it's server-side wall-clock noise.
    """
    keys_for_hash = {
        k: v
        for k, v in payload.items()
        if k not in {"event_id"}
    }
    canonical = json.dumps(keys_for_hash, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_raw_event(payload: dict[str, Any]) -> RawEvent:
    """Validate against the discriminated union. Raises ValidationError on bad input."""
    return _raw_adapter.validate_python(payload)


def ingest_raw(
    session: Session, payload: dict[str, Any], *, normalize: bool = True
) -> IngestResult:
    event = parse_raw_event(payload)
    dumped = event.model_dump(mode="json")

    content_hash = _content_hash(dumped)

    existing = (
        session.query(RawEventRow).filter(RawEventRow.content_hash == content_hash).one_or_none()
    )
    if existing is not None:
        return IngestResult(event_id=existing.event_id, deduped=True, content_hash=content_hash)

    event_id = event.event_id or f"evt_{uuid.uuid4().hex[:16]}"
    received_at = datetime.now(timezone.utc).replace(tzinfo=None)

    observed_at_raw = dumped.get("timestamp")
    observed_at = (
        datetime.fromisoformat(observed_at_raw.replace("Z", "+00:00"))
        if isinstance(observed_at_raw, str)
        else observed_at_raw
    )
    if isinstance(observed_at, datetime) and observed_at.tzinfo is not None:
        observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)

    dumped["event_id"] = event_id

    row = RawEventRow(
        event_id=event_id,
        source=event.source,
        received_at=received_at,
        observed_at=observed_at,
        content_hash=content_hash,
        payload=dumped,
    )
    session.add(row)
    session.flush()

    if normalize:
        from aegis.normalize import normalize_and_persist

        normalize_and_persist(session, row)

    return IngestResult(event_id=event_id, deduped=False, content_hash=content_hash)


def ingest_many(
    session: Session, payloads: list[dict[str, Any]], *, normalize: bool = True
) -> list[IngestResult]:
    return [ingest_raw(session, p, normalize=normalize) for p in payloads]


__all__ = [
    "IngestResult",
    "ingest_raw",
    "ingest_many",
    "parse_raw_event",
]
