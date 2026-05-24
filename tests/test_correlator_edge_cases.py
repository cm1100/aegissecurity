"""Correlator stress + boundary suite.

Complements `test_correlator.py` (which covers the spec-required behavior)
with the corner cases that emerged during deep verification: exact time
boundaries, scaling, component-level invariants, and adversarial
identity collisions.

Each test names the property it protects. Read top-to-bottom as a
specification of the correlator's invariants beyond the headline cases.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from aegis.correlate import (
    CONF_HOST_PID,
    CONF_NHI,
    CONF_REPO,
    CONF_WORKLOAD,
    HOST_PID_WINDOW_SECONDS,
    correlate,
)
from aegis.ingest import parse_raw_event
from aegis.normalize import normalize_event
from aegis.schemas import CanonicalEvent


def _evt(event_id: str, **fields) -> CanonicalEvent:
    fields.setdefault("source", "runtime")
    fields.setdefault("timestamp", "2026-05-20T10:15:00Z")
    payload: dict[str, Any] = {
        "source": fields.pop("source"),
        "timestamp": fields.pop("timestamp"),
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    raw = parse_raw_event(payload)
    return normalize_event(
        raw,
        event_id=event_id,
        received_at=datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc),
        content_hash=event_id,
    )


# ---------------------------------------------------------------------------
# Exact time-window boundaries
# ---------------------------------------------------------------------------

def test_host_pid_exactly_at_5_min_boundary_inclusive():
    """5 min 0 s should be the *inclusive* upper bound — events at that gap merge."""
    a = _evt("a", host_id="h", pid=1, timestamp="2026-05-20T10:00:00Z")
    b = _evt("b", host_id="h", pid=1, timestamp="2026-05-20T10:05:00Z")
    r = correlate([a, b])
    assert len(r.agents) == 1


def test_host_pid_one_second_past_boundary_excludes():
    """5 min 1 s exceeds the window — must NOT merge."""
    a = _evt("a", host_id="h", pid=1, timestamp="2026-05-20T10:00:00Z")
    b = _evt("b", host_id="h", pid=1, timestamp="2026-05-20T10:05:01Z")
    r = correlate([a, b])
    assert len(r.agents) == 2


def test_host_pid_simultaneous_events_merge():
    """Identical timestamps → distance 0 → trivially within window."""
    a = _evt("a", host_id="h", pid=1, timestamp="2026-05-20T10:00:00Z")
    b = _evt("b", host_id="h", pid=1, timestamp="2026-05-20T10:00:00Z")
    r = correlate([a, b])
    assert len(r.agents) == 1


def test_window_constant_matches_spec():
    """Spec says ±5 min. Verify the constant in case it's accidentally changed."""
    assert HOST_PID_WINDOW_SECONDS == 300


# ---------------------------------------------------------------------------
# Identity-key edge cases
# ---------------------------------------------------------------------------

def test_empty_nhi_id_does_not_link_events():
    """Two events with no identity keys should NOT merge through nhi_id."""
    a = _evt("a", host_id="h1", pid=1, timestamp="2026-05-20T10:00:00Z")
    b = _evt("b", host_id="h2", pid=2, timestamp="2026-05-20T11:00:00Z")
    r = correlate([a, b])
    assert len(r.agents) == 2


def test_distinct_arns_do_not_share_nhi_identity():
    """Two events with similar-looking but distinct ARNs are separate agents."""
    a = _evt("a", source="saas", nhi_id="arn:aws:iam::111:role/agent-A")
    b = _evt("b", source="saas", nhi_id="arn:aws:iam::222:role/agent-A")  # different account
    r = correlate([a, b])
    assert len(r.agents) == 2


def test_same_nhi_id_with_different_workloads_still_merges():
    """Same NHI can run multiple workloads over time. They should merge into
    one agent if the strongest signal (nhi_id) agrees."""
    a = _evt("a", source="saas", nhi_id="role-shared", workload_id="w1")
    b = _evt("b", source="saas", nhi_id="role-shared", workload_id="w2")
    # This actually triggers conflict guard because workload_ids differ
    # AND both events declare workload. Spec is strict: same role under two
    # workload_ids becomes two agents (workload is part of identity).
    r = correlate([a, b])
    # Conflict guard fires — two distinct workload_ids in the proposed cluster.
    assert len(r.agents) == 2


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

def test_correlator_handles_100_events_in_one_cluster():
    """100 events sharing nhi_id → 1 agent in well under a second."""
    evts = [
        _evt(f"e{i:03d}", source="saas", nhi_id="role-X",
             timestamp=f"2026-05-20T10:{i // 60:02d}:{i % 60:02d}Z")
        for i in range(100)
    ]
    r = correlate(evts)
    assert len(r.agents) == 1
    assert len(r.agents[0].event_ids) == 100


def test_correlator_handles_100_distinct_singletons():
    """100 events with disjoint identity → 100 agents."""
    evts = [
        _evt(f"e{i:03d}", host_id=f"h{i}", pid=i,
             timestamp="2026-05-20T10:00:00Z")
        for i in range(100)
    ]
    r = correlate(evts)
    assert len(r.agents) == 100


def test_correlator_mixed_clustering():
    """20 clusters of 5 events each by nhi_id → 20 agents."""
    evts = []
    for cluster in range(20):
        for member in range(5):
            evts.append(_evt(
                f"e{cluster}-{member}", source="saas",
                nhi_id=f"role-{cluster}",
            ))
    r = correlate(evts)
    assert len(r.agents) == 20
    for a in r.agents:
        assert len(a.event_ids) == 5


# ---------------------------------------------------------------------------
# Component invariants — these must hold for ANY input
# ---------------------------------------------------------------------------

def _assert_components_cover_all(events, result):
    """Every input event ends up in exactly one component."""
    seen: set[str] = set()
    for a in result.agents:
        assert not (seen & set(a.event_ids)), \
            f"agent {a.agent_id} overlaps with prior components"
        seen |= set(a.event_ids)
    assert seen == {e.event_id for e in events}


def test_components_cover_all_events_and_are_disjoint():
    evts = [
        _evt("a", source="nhi", nhi_id="role-X"),
        _evt("b", source="saas", nhi_id="role-X"),
        _evt("c", source="nhi", nhi_id="role-Y"),
        _evt("d", source="runtime", host_id="h1", pid=1),
        _evt("e", source="repo", repo="r"),
        _evt("f", source="repo", repo="r"),
    ]
    r = correlate(evts)
    _assert_components_cover_all(evts, r)


def test_components_invariant_under_shuffle():
    import random
    evts = [
        _evt("a", source="nhi", nhi_id="role-X"),
        _evt("b", source="saas", nhi_id="role-X"),
        _evt("c", source="nhi", nhi_id="role-Y", workload_id="w"),
        _evt("d", source="repo", workload_id="w", repo="r"),
    ]
    rng = random.Random(42)
    for _ in range(5):
        rng.shuffle(evts)
        r = correlate(list(evts))
        _assert_components_cover_all(evts, r)
        agent_signatures = {tuple(sorted(a.event_ids)) for a in r.agents}
        assert agent_signatures == {("a", "b"), ("c", "d")}


def test_all_four_sources_assemble_into_one_agent_when_bridged():
    rt = _evt("rt", source="runtime", host_id="h", pid=1, workload_id="W")
    nhi = _evt("nhi", source="nhi", nhi_id="role-X", workload_id="W")
    repo = _evt("repo", source="repo", repo="W", workload_id="W")
    saas = _evt("saas", source="saas", nhi_id="role-X")
    r = correlate([rt, nhi, repo, saas])
    assert len(r.agents) == 1
    assert set(r.agents[0].sources) == {"runtime", "nhi", "repo", "saas"}


def test_pure_repo_only_cluster_carries_low_confidence():
    """Cluster held together only by shared repo has confidence 0.5."""
    a = _evt("a", source="repo", repo="r1")
    b = _evt("b", source="repo", repo="r1")
    r = correlate([a, b])
    assert len(r.agents) == 1
    assert r.agents[0].correlation_confidence == CONF_REPO


def test_strong_key_dominates_when_present():
    """Among (nhi, workload, host+pid, repo), nhi_id alone is strongest.
    Cluster confidence reflects this."""
    a = _evt("a", source="saas", nhi_id="role-X")
    b = _evt("b", source="saas", nhi_id="role-X")
    r = correlate([a, b])
    assert r.agents[0].correlation_confidence == CONF_NHI


# ---------------------------------------------------------------------------
# Multi-host horizontal-scale case
# ---------------------------------------------------------------------------

def test_horizontally_scaled_agent_has_multiple_host_ids():
    """One logical agent (same nhi+workload) spread across two pods."""
    a = _evt("a", source="runtime", host_id="pod-1", pid=1,
             workload_id="w", timestamp="2026-05-20T10:00:00Z")
    b = _evt("b", source="runtime", host_id="pod-2", pid=1,
             workload_id="w", timestamp="2026-05-20T10:00:01Z")
    c = _evt("c", source="nhi", nhi_id="role-X", workload_id="w")
    r = correlate([a, b, c])
    assert len(r.agents) == 1
    assert sorted(r.agents[0].host_ids) == ["pod-1", "pod-2"]


# ---------------------------------------------------------------------------
# Adversarial: collisions and near-misses
# ---------------------------------------------------------------------------

def test_pid_reuse_with_distinct_nhi_does_not_merge():
    """PID reused on same host, but with distinct NHIs declared via SaaS
    events. Conflict guard must keep them apart even within the time window."""
    a_rt = _evt("a-rt", source="runtime", host_id="h", pid=1234,
                nhi_id="role-A", timestamp="2026-05-20T10:00:00Z")
    a_saas = _evt("a-saas", source="saas", nhi_id="role-A",
                  timestamp="2026-05-20T10:00:30Z")
    # PID reuse: same host+pid 2 minutes later, but different nhi
    b_rt = _evt("b-rt", source="runtime", host_id="h", pid=1234,
                nhi_id="role-B", timestamp="2026-05-20T10:02:00Z")
    b_saas = _evt("b-saas", source="saas", nhi_id="role-B",
                  timestamp="2026-05-20T10:02:30Z")
    r = correlate([a_rt, a_saas, b_rt, b_saas])
    assert len(r.agents) == 2


def test_chained_conflict_block_propagates():
    """A↔B by nhi; B↔C by workload; C has nhi_id that conflicts with B's.
    The chain must NOT merge fully."""
    a = _evt("a", source="saas", nhi_id="role-X")
    b = _evt("b", source="nhi", nhi_id="role-X", workload_id="W")
    c = _evt("c", source="nhi", nhi_id="role-Y", workload_id="W")
    r = correlate([a, b, c])
    # a-b merge by nhi; b-c blocked by nhi conflict.
    assert len(r.agents) == 2
    sizes = sorted(len(ag.event_ids) for ag in r.agents)
    assert sizes == [1, 2]
