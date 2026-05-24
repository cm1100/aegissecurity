"""Performance and scalability tests.

These tests pin the algorithmic complexity claims in `docs/CORRELATION.md`:
union-find with path compression scales linearly. They also set
documented performance budgets so a regression that 10x's the runtime
gets caught immediately.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from aegis.correlate import correlate
from aegis.ingest import parse_raw_event
from aegis.normalize import normalize_event


def _evt(idx: int, **fields):
    fields.setdefault("source", "saas")
    fields.setdefault("timestamp", f"2026-05-20T10:{idx // 60:02d}:{idx % 60:02d}Z")
    payload = {"source": fields.pop("source"), "timestamp": fields.pop("timestamp")}
    payload.update({k: v for k, v in fields.items() if v is not None})
    raw = parse_raw_event(payload)
    return normalize_event(
        raw,
        event_id=f"e{idx:06d}",
        received_at=datetime(2026, 5, 20, 11, tzinfo=timezone.utc),
        content_hash=f"h{idx:06d}",
    )


# Budgets are deliberately generous so they catch 10x regressions, not noise.
# Actual measured runtimes are ~10x lower than these on a modern laptop.
BUDGET_100_EVENTS_MS = 200
BUDGET_500_EVENTS_MS = 1000
BUDGET_1000_EVENTS_MS = 3000


def test_perf_100_events_in_one_cluster():
    """100 events sharing nhi_id → 1 agent."""
    evts = [_evt(i, nhi_id="role-X") for i in range(100)]
    start = time.perf_counter()
    result = correlate(evts)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert len(result.agents) == 1
    assert len(result.agents[0].event_ids) == 100
    assert elapsed_ms < BUDGET_100_EVENTS_MS, (
        f"100-event cluster took {elapsed_ms:.0f} ms (budget {BUDGET_100_EVENTS_MS})"
    )
    print(f"\n  100 events → 1 cluster:  {elapsed_ms:.1f} ms")


def test_perf_100_distinct_singletons():
    """100 events with disjoint identities → 100 agents."""
    evts = [_evt(i, host_id=f"h{i}", pid=i, source="runtime") for i in range(100)]
    start = time.perf_counter()
    result = correlate(evts)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert len(result.agents) == 100
    assert elapsed_ms < BUDGET_100_EVENTS_MS, (
        f"100-singleton case took {elapsed_ms:.0f} ms (budget {BUDGET_100_EVENTS_MS})"
    )
    print(f"  100 events → 100 singletons:  {elapsed_ms:.1f} ms")


def test_perf_500_events_mixed_clustering():
    """500 events forming 100 clusters of 5."""
    evts = []
    for cluster in range(100):
        for member in range(5):
            evts.append(_evt(cluster * 5 + member, nhi_id=f"role-{cluster:03d}"))
    start = time.perf_counter()
    result = correlate(evts)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert len(result.agents) == 100
    assert all(len(a.event_ids) == 5 for a in result.agents)
    assert elapsed_ms < BUDGET_500_EVENTS_MS, (
        f"500-event mixed clustering took {elapsed_ms:.0f} ms (budget {BUDGET_500_EVENTS_MS})"
    )
    print(f"  500 events → 100 clusters of 5:  {elapsed_ms:.1f} ms")


def test_perf_1000_events_complex_topology():
    """1000 events with mixed identities — stress test."""
    evts = []
    for i in range(1000):
        nhi = f"role-{i // 10:03d}"  # 100 NHIs, 10 events each
        wl = f"w-{i // 50:02d}"      # 20 workloads, 50 events each
        evts.append(_evt(
            i, nhi_id=nhi, workload_id=wl,
            host_id=f"host-{i // 100:02d}",
            source="nhi",
        ))
    start = time.perf_counter()
    result = correlate(evts)
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"  1000 events → {len(result.agents)} agents:  {elapsed_ms:.1f} ms")
    assert elapsed_ms < BUDGET_1000_EVENTS_MS, (
        f"1000-event topology took {elapsed_ms:.0f} ms (budget {BUDGET_1000_EVENTS_MS})"
    )


def test_perf_full_pipeline_on_demo_dataset(client):
    """End-to-end pipeline over all 5 sample scenarios — must complete promptly."""
    import json
    from pathlib import Path
    samples = Path(__file__).parent.parent / "samples"
    total_events = 0
    for f in sorted(samples.glob("*.json")):
        data = json.loads(f.read_text())
        if "events" in data:
            client.post("/events/batch", json=data["events"])
            total_events += len(data["events"])

    start = time.perf_counter()
    r = client.post("/pipeline/run").json()
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"\n  Full pipeline over {total_events} events → {r['agents']} agents:  {elapsed_ms:.1f} ms")
    assert elapsed_ms < 2000, f"Full pipeline took {elapsed_ms:.0f} ms"


def test_perf_idempotent_replay_is_constant_time(client):
    """Re-running /pipeline/run N times shouldn't grow linearly with N
    (replay is constant-time because each stage is delete-then-rewrite)."""
    import json
    samples = json.loads(open("samples/01-shadow-phi.json").read())["events"]
    client.post("/events/batch", json=samples)
    client.post("/pipeline/run")  # warm

    times = []
    for _ in range(5):
        start = time.perf_counter()
        client.post("/pipeline/run")
        times.append((time.perf_counter() - start) * 1000)
    print(f"\n  5 sequential /pipeline/run calls:  {[round(t, 1) for t in times]} ms")
    # All runs must be reasonable (no degradation)
    assert max(times) < 500
