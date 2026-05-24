from __future__ import annotations


def _runtime_event(**overrides):
    base = {
        "source": "runtime",
        "timestamp": "2026-05-20T10:15:00Z",
        "host_id": "host-prod-01",
        "pid": 1234,
        "destination": "api.anthropic.com",
        "tools_called": ["aurora_read"],
    }
    base.update(overrides)
    return base


def _nhi_manifest(**overrides):
    base = {
        "source": "nhi",
        "timestamp": "2026-05-20T10:14:00Z",
        "nhi_id": "role-aegis-shadow-agent",
        "workload_id": "claims-processor",
        "permissions": ["aurora:read", "bedrock:invoke"],
    }
    base.update(overrides)
    return base


def _repo_hint(**overrides):
    base = {
        "source": "repo",
        "timestamp": "2026-05-20T09:00:00Z",
        "repo": "claims-processor",
        "imports": ["langchain", "anthropic"],
        "has_aegislib": False,
    }
    base.update(overrides)
    return base


def _saas_event(**overrides):
    base = {
        "source": "saas",
        "timestamp": "2026-05-20T10:16:00Z",
        "nhi_id": "role-aegis-shadow-agent",
        "data_classes": ["PHI"],
        "action": "read",
        "resource": "claims.patient_records",
    }
    base.update(overrides)
    return base


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ingest_runtime_event(client):
    r = client.post("/events", json=_runtime_event())
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False
    assert body["event_id"].startswith("evt_")
    assert len(body["content_hash"]) == 64


def test_ingest_all_four_sources(client):
    for payload in (_runtime_event(), _nhi_manifest(), _repo_hint(), _saas_event()):
        r = client.post("/events", json=payload)
        assert r.status_code == 201, r.text


def test_duplicate_event_is_deduped(client):
    """Same content twice → second call returns deduped=True with original id."""
    p = _runtime_event()
    r1 = client.post("/events", json=p)
    r2 = client.post("/events", json=p)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r2.json()["deduped"] is True
    assert r2.json()["event_id"] == r1.json()["event_id"]


def test_duplicate_with_different_caller_event_id_still_deduped(client):
    """event_id is not part of the content hash — two events with the same
    payload but different caller-supplied ids should still collapse."""
    p1 = _runtime_event(event_id="caller-1")
    p2 = _runtime_event(event_id="caller-2")
    r1 = client.post("/events", json=p1)
    r2 = client.post("/events", json=p2)
    assert r2.json()["deduped"] is True
    assert r2.json()["event_id"] == r1.json()["event_id"] == "caller-1"


def test_unknown_source_rejected(client):
    r = client.post("/events", json={"source": "ufo", "timestamp": "2026-05-20T10:00:00Z"})
    assert r.status_code == 422


def test_missing_required_timestamp_rejected(client):
    r = client.post("/events", json={"source": "runtime"})
    assert r.status_code == 422


def test_batch_ingest(client):
    payloads = [_runtime_event(), _nhi_manifest(), _repo_hint(), _saas_event()]
    r = client.post("/events/batch", json=payloads)
    assert r.status_code == 201
    body = r.json()
    assert body["total"] == 4
    assert body["deduped"] == 0


def test_batch_ingest_with_internal_dupes(client):
    p = _runtime_event()
    r = client.post("/events/batch", json=[p, p, p])
    assert r.status_code == 201
    assert r.json()["deduped"] == 2


def test_list_events(client):
    client.post("/events", json=_runtime_event())
    client.post("/events", json=_nhi_manifest())
    r = client.get("/events")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 2
    sources = {e["source"] for e in events}
    assert sources == {"runtime", "nhi"}


def test_list_events_filter_by_source(client):
    client.post("/events", json=_runtime_event())
    client.post("/events", json=_nhi_manifest())
    r = client.get("/events?source=runtime")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 1
    assert events[0]["source"] == "runtime"
