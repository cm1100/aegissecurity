"""Adversarial / fuzz tests.

Pushes weird inputs through the API and asserts the system handles them
gracefully — accepts what it should, rejects what it shouldn't, never
crashes. A 500 status code from any of these inputs is a bug.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Boundary timestamps
# ---------------------------------------------------------------------------

def test_far_future_timestamp_accepted(client):
    r = client.post("/events", json={
        "source": "runtime", "timestamp": "2099-12-31T23:59:59Z",
        "host_id": "h", "pid": 1,
    })
    assert r.status_code == 201


def test_unix_epoch_timestamp_accepted(client):
    r = client.post("/events", json={
        "source": "runtime", "timestamp": "1970-01-01T00:00:00Z",
        "host_id": "h", "pid": 1,
    })
    assert r.status_code == 201


def test_microsecond_precision_timestamp_accepted(client):
    r = client.post("/events", json={
        "source": "runtime", "timestamp": "2026-05-20T10:15:30.123456Z",
        "host_id": "h", "pid": 1,
    })
    assert r.status_code == 201


def test_garbage_timestamp_rejected_422(client):
    r = client.post("/events", json={
        "source": "runtime", "timestamp": "yesterday at 5pm",
    })
    assert r.status_code == 422


def test_null_timestamp_rejected_422(client):
    r = client.post("/events", json={"source": "runtime", "timestamp": None})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Field-content adversarial cases
# ---------------------------------------------------------------------------

def test_very_long_strings_handled(client):
    """10K-char identifiers shouldn't break ingest."""
    long_id = "x" * 10_000
    r = client.post("/events", json={
        "source": "nhi", "timestamp": "2026-05-20T10:00:00Z",
        "nhi_id": long_id,
    })
    assert r.status_code == 201
    assert r.json()["deduped"] is False


def test_unicode_in_identifiers_preserved(client):
    """Emojis and multi-byte chars in field values round-trip cleanly."""
    r = client.post("/events", json={
        "source": "nhi", "timestamp": "2026-05-20T10:00:00Z",
        "nhi_id": "role-🛡️-shadow-代理",
    })
    assert r.status_code == 201
    # Round-trip through the normalized view
    rows = client.get("/events/normalized").json()
    nhi_ids = {r["nhi_id"] for r in rows}
    assert "role-🛡️-shadow-代理" in nhi_ids


def test_empty_string_identifiers_are_not_identity_keys(client):
    """nhi_id='' should NOT bridge events. Treat empty as 'not set'."""
    client.post("/events", json={
        "event_id": "fz-a", "source": "nhi",
        "timestamp": "2026-05-20T10:00:00Z",
        "nhi_id": "", "host_id": "ha", "pid": 1,
    })
    client.post("/events", json={
        "event_id": "fz-b", "source": "saas",
        "timestamp": "2026-05-20T10:01:00Z",
        "nhi_id": "", "host_id": "hb", "pid": 2,
    })
    client.post("/pipeline/run")
    agents = client.get("/agents").json()
    # Two distinct events, neither with a real identity → 2 agents
    assert len(agents) == 2


def test_negative_pid_accepted_but_doesnt_correlate(client):
    """Negative PIDs are unusual but shouldn't crash. They form their own clusters."""
    client.post("/events", json={
        "source": "runtime", "timestamp": "2026-05-20T10:00:00Z",
        "host_id": "h", "pid": -1,
    })
    r = client.post("/events", json={
        "source": "runtime", "timestamp": "2026-05-20T10:00:00Z",
        "host_id": "h", "pid": -1,
    })
    # Same content → second deduped
    assert r.json()["deduped"] is True


def test_huge_tools_list_handled(client):
    """100-tool agent ingests and scores without issue."""
    r = client.post("/events", json={
        "source": "runtime", "timestamp": "2026-05-20T10:00:00Z",
        "host_id": "huge", "pid": 1,
        "destination": "api.anthropic.com",
        "tools_called": [f"tool_{i}" for i in range(100)],
    })
    assert r.status_code == 201
    client.post("/pipeline/run")
    agent = next(a for a in client.get("/agents").json()
                 if a["host_ids"] == ["huge"])
    assert len(agent["tools"]) == 100
    # Scope is capped at 25 even with 100 tools
    detail = client.get(f"/agents/{agent['agent_id']}").json()
    scope = next(f for f in detail["agent"]["risk_factors"] if f["name"] == "scope")
    assert scope["value"] == 25


# ---------------------------------------------------------------------------
# Schema violation cases
# ---------------------------------------------------------------------------

def test_unknown_data_class_rejected(client):
    """Custom data classes outside our enum are rejected at the boundary."""
    r = client.post("/events", json={
        "source": "saas", "timestamp": "2026-05-20T10:00:00Z",
        "nhi_id": "x", "data_classes": ["BIOMETRIC"],
    })
    assert r.status_code == 422


def test_extra_field_rejected(client):
    """`extra='forbid'` schema rigor — unknown fields rejected."""
    r = client.post("/events", json={
        "source": "runtime", "timestamp": "2026-05-20T10:00:00Z",
        "host_id": "h", "pid": 1,
        "definitely_not_a_real_field": "boom",
    })
    assert r.status_code == 422


def test_wrong_type_for_pid_rejected(client):
    r = client.post("/events", json={
        "source": "runtime", "timestamp": "2026-05-20T10:00:00Z",
        "host_id": "h", "pid": "not-an-int",
    })
    assert r.status_code == 422


def test_pid_passed_as_numeric_string_coerced(client):
    """Pydantic accepts '42' for int fields by default."""
    r = client.post("/events", json={
        "source": "runtime", "timestamp": "2026-05-20T10:00:00Z",
        "host_id": "h", "pid": "42",
    })
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# Malformed JSON / empty body
# ---------------------------------------------------------------------------

def test_empty_body_rejected(client):
    r = client.post("/events", json={})
    assert r.status_code == 422


def test_array_at_top_level_for_single_event_rejected(client):
    """POST /events expects an object; an array should fail validation."""
    r = client.post("/events", json=[
        {"source": "runtime", "timestamp": "2026-05-20T10:00:00Z"}
    ])
    # The Pydantic discriminated union expects a dict, not a list
    assert r.status_code in (422, 400)


# ---------------------------------------------------------------------------
# Batch fuzz
# ---------------------------------------------------------------------------

def test_batch_with_mix_of_valid_and_invalid_rejects_whole_batch(client):
    """If any event in the batch is invalid, the whole batch fails (atomic)."""
    r = client.post("/events/batch", json=[
        {"source": "runtime", "timestamp": "2026-05-20T10:00:00Z",
         "host_id": "h", "pid": 1},
        {"source": "INVALID", "timestamp": "2026-05-20T10:00:00Z"},
    ])
    assert r.status_code == 422
    # Nothing got persisted
    assert len(client.get("/events").json()) == 0


def test_empty_batch_succeeds(client):
    r = client.post("/events/batch", json=[])
    assert r.status_code == 201
    assert r.json()["total"] == 0


# ---------------------------------------------------------------------------
# Pipeline robustness
# ---------------------------------------------------------------------------

def test_pipeline_run_with_zero_events_no_crash(client):
    r = client.post("/pipeline/run")
    assert r.status_code == 200
    assert r.json()["agents"] == 0


def test_re_running_individual_stages_in_any_order(client):
    """The pipeline stages can be called in arbitrary order — each is
    idempotent and tolerant of stale upstream state."""
    client.post("/events", json={
        "source": "saas", "timestamp": "2026-05-20T10:00:00Z",
        "nhi_id": "fuzz-role", "data_classes": ["PHI"],
    })
    # Risk before correlate is fine (no agents yet)
    r = client.post("/pipeline/risk").json()
    assert r["agents_scored"] == 0
    # Policy before correlate is fine
    r = client.post("/pipeline/policy").json()
    assert r["agents_total"] == 0
    # Now do it in the right order
    client.post("/pipeline/correlate")
    client.post("/pipeline/classify")
    client.post("/pipeline/risk")
    client.post("/pipeline/policy")
    agents = client.get("/agents").json()
    assert len(agents) == 1
