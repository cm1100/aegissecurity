from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aegis.ingest import parse_raw_event
from aegis.normalize import (
    coerce_framework,
    coerce_provider,
    derive_provider_from_destination,
    normalize_event,
)
from aegis.schemas import DataClass, EventSource, Framework, Provider


def _norm(raw_payload, event_id="evt_1"):
    raw = parse_raw_event(raw_payload)
    return normalize_event(
        raw,
        event_id=event_id,
        received_at=datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc),
        content_hash="deadbeef",
    )


def test_runtime_event_normalized():
    c = _norm({
        "source": "runtime",
        "timestamp": "2026-05-20T10:15:00Z",
        "host_id": "h1",
        "pid": 1234,
        "destination": "api.anthropic.com",
        "tools_called": ["aurora_read", "External_LLM_Call"],
        "syscalls": ["connect", "write"],
        "bytes_out": 4096,
    })
    assert c.source == EventSource.RUNTIME
    assert c.host_id == "h1"
    assert c.pid == 1234
    assert c.provider == Provider.ANTHROPIC  # derived from destination
    assert c.tools_called == ["aurora_read", "external_llm_call"]  # lowercased
    assert c.bytes_out == 4096


def test_nhi_manifest_normalized():
    c = _norm({
        "source": "nhi",
        "timestamp": "2026-05-20T10:14:00Z",
        "nhi_id": "role-aegis-shadow-agent",
        "workload_id": "claims-processor",
        "role_arn": "arn:aws:iam::123:role/shadow-agent",
        "permissions": ["aurora:read", "bedrock:invoke"],
    })
    assert c.source == EventSource.NHI
    assert c.nhi_id == "role-aegis-shadow-agent"
    assert c.role_arn == "arn:aws:iam::123:role/shadow-agent"
    assert c.permissions == ["aurora:read", "bedrock:invoke"]


def test_repo_hint_normalized():
    c = _norm({
        "source": "repo",
        "timestamp": "2026-05-20T09:00:00Z",
        "repo": "claims-processor",
        "imports": ["langchain", "anthropic", "LangChain"],  # dupes + case
        "has_aegislib": False,
        "framework_hint": "LangChain",
        "config_files": [".cursor/mcp.json"],
    })
    assert c.source == EventSource.REPO
    assert c.repo == "claims-processor"
    assert c.imports == ["langchain", "anthropic"]
    assert c.has_aegislib is False
    assert c.framework_hint == Framework.LANGCHAIN  # case-coerced
    assert c.config_files == [".cursor/mcp.json"]


def test_saas_event_normalized():
    c = _norm({
        "source": "saas",
        "timestamp": "2026-05-20T10:16:00Z",
        "nhi_id": "role-aegis-shadow-agent",
        "data_classes": ["PHI"],
        "action": "read",
        "resource": "claims.patient_records",
        "actor": "claims-processor",
    })
    assert c.source == EventSource.SAAS
    assert c.data_classes == [DataClass.PHI]
    assert c.action == "read"
    assert c.actor == "claims-processor"


def test_framework_hint_aliases():
    """Punctuation- and case-tolerant: lang-chain / LangChain / LANGCHAIN all map equal."""
    assert coerce_framework("langchain") == Framework.LANGCHAIN
    assert coerce_framework("LangChain") == Framework.LANGCHAIN
    assert coerce_framework("lang-chain") == Framework.LANGCHAIN
    assert coerce_framework("lang_chain") == Framework.LANGCHAIN
    assert coerce_framework("CrewAI") == Framework.CREWAI
    assert coerce_framework("llama-index") == Framework.LLAMA_INDEX
    assert coerce_framework("mcp") == Framework.MCP_AGENT


def test_framework_hint_unknown_becomes_unknown():
    """Don't drop unknown values — record that we saw something."""
    assert coerce_framework("invented-framework-9000") == Framework.UNKNOWN
    assert coerce_framework("") is None
    assert coerce_framework(None) is None


def test_provider_explicit_overrides_destination():
    c = _norm({
        "source": "runtime",
        "timestamp": "2026-05-20T10:15:00Z",
        "provider": "anthropic",
        "destination": "api.openai.com",
    })
    assert c.provider == Provider.ANTHROPIC


def test_provider_derived_from_destination():
    c = _norm({
        "source": "runtime",
        "timestamp": "2026-05-20T10:15:00Z",
        "destination": "api.openai.com",
    })
    assert c.provider == Provider.OPENAI


def test_provider_other_for_unknown_string():
    assert coerce_provider("custom-llm-inc") == Provider.OTHER


def test_destination_unrecognized_returns_none():
    assert derive_provider_from_destination("api.internal.corp") is None


def test_timestamp_normalized_to_utc_naive():
    c = _norm({
        "source": "runtime",
        "timestamp": "2026-05-20T15:15:00+05:00",  # IST
    })
    assert c.observed_at == datetime(2026, 5, 20, 10, 15)
    assert c.observed_at.tzinfo is None


def test_normalize_idempotent():
    payload = {
        "source": "runtime",
        "timestamp": "2026-05-20T10:15:00Z",
        "host_id": "h1",
        "pid": 1234,
        "destination": "api.anthropic.com",
        "tools_called": ["x"],
    }
    c1 = _norm(payload)
    c2 = _norm(payload)
    assert c1.model_dump() == c2.model_dump()


# --- API-level tests: ingest now eagerly normalizes ---

def _runtime():
    return {
        "source": "runtime",
        "timestamp": "2026-05-20T10:15:00Z",
        "host_id": "h1",
        "pid": 1234,
        "destination": "api.anthropic.com",
    }


def test_ingest_eagerly_normalizes(client):
    client.post("/events", json=_runtime())
    r = client.get("/events/normalized")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["provider"] == "anthropic"


def test_dedup_does_not_double_normalize(client):
    p = _runtime()
    client.post("/events", json=p)
    client.post("/events", json=p)
    rows = client.get("/events/normalized").json()
    assert len(rows) == 1


def test_pipeline_normalize_is_idempotent(client):
    client.post("/events", json=_runtime())
    r1 = client.post("/pipeline/normalize")
    assert r1.json()["normalized"] == 0  # already done eagerly
    r2 = client.post("/pipeline/normalize")
    assert r2.json()["normalized"] == 0


def test_pipeline_normalize_picks_up_unnormalized_rows(client, app):
    """Insert a raw row without normalization, then run pipeline."""
    from aegis.ingest import ingest_raw
    from aegis.storage import get_session, NormalizedEventRow

    with get_session() as s:
        ingest_raw(s, _runtime(), normalize=False)
        assert s.query(NormalizedEventRow).count() == 0

    r = client.post("/pipeline/normalize")
    assert r.json()["normalized"] == 1
    assert len(client.get("/events/normalized").json()) == 1
