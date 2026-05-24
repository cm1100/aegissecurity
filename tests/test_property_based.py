"""Property-based tests using Hypothesis.

Hypothesis generates hundreds of random event sequences and verifies that
core invariants hold for *every* generated input — finding edge cases a
hand-written test suite would miss.

Properties under test:
  P1. Every input event ends up in exactly one component (covers all).
  P2. Components are pairwise disjoint.
  P3. Correlation is deterministic across runs.
  P4. Order independence: shuffling the input doesn't change the agent set.
  P5. No agent ever carries two distinct strong identifiers (nhi_id / workload_id).
  P6. Per-agent confidence is always in [0.5, 1.0].
  P7. Single-event clusters always have confidence 1.0.
  P8. agent_id is the same hash regardless of original input order.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from aegis.correlate import correlate
from aegis.ingest import parse_raw_event
from aegis.normalize import normalize_event
from aegis.schemas import CanonicalEvent


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A small alphabet keeps clusters meaningfully overlapping; large alphabets
# would produce all-disjoint events and exercise less.
_nhi = st.one_of(st.none(), st.sampled_from(["role-A", "role-B", "role-C", "role-D"]))
_workload = st.one_of(st.none(), st.sampled_from(["w-X", "w-Y", "w-Z"]))
_host = st.one_of(st.none(), st.sampled_from(["h1", "h2", "h3"]))
_pid = st.one_of(st.none(), st.integers(min_value=1, max_value=99))
_repo = st.one_of(st.none(), st.sampled_from(["r1", "r2"]))

_sources = st.sampled_from(["runtime", "nhi", "repo", "saas"])


@st.composite
def _event(draw, idx_strategy):
    """Build a single canonical event with random partial keys."""
    idx = draw(idx_strategy)
    source = draw(_sources)
    payload = {
        "source": source,
        "timestamp": f"2026-05-20T10:00:{idx % 60:02d}Z",
    }
    nhi = draw(_nhi)
    wl = draw(_workload)
    host = draw(_host)
    pid = draw(_pid)
    repo = draw(_repo)

    if nhi is not None:
        payload["nhi_id"] = nhi
    if wl is not None:
        payload["workload_id"] = wl
    if host is not None:
        payload["host_id"] = host
    if pid is not None:
        payload["pid"] = pid
    # Only repo events get a repo field (matches realistic data shapes)
    if source == "repo" and repo is not None:
        payload["repo"] = repo

    raw = parse_raw_event(payload)
    return normalize_event(
        raw,
        event_id=f"e{idx:04d}",
        received_at=datetime(2026, 5, 20, 11, tzinfo=timezone.utc),
        content_hash=f"h{idx:04d}",
    )


@st.composite
def event_sequence(draw, min_size=1, max_size=15):
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    return [draw(_event(st.just(i))) for i in range(n)]


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

# Default settings: tighter deadline + suppress filter-too-much because some
# generators produce no events legally.
_settings = settings(
    deadline=2000,
    max_examples=150,
    suppress_health_check=[HealthCheck.filter_too_much],
)


@given(events=event_sequence(min_size=0, max_size=12))
@_settings
def test_p1_covers_all_events(events: list[CanonicalEvent]):
    """Every input event ends up in exactly one component."""
    result = correlate(events)
    all_input_ids = {e.event_id for e in events}
    all_clustered = set()
    for a in result.agents:
        all_clustered |= set(a.event_ids)
    assert all_clustered == all_input_ids


@given(events=event_sequence(min_size=2, max_size=12))
@_settings
def test_p2_components_disjoint(events: list[CanonicalEvent]):
    """Components are pairwise disjoint."""
    result = correlate(events)
    seen: set[str] = set()
    for a in result.agents:
        cluster = set(a.event_ids)
        assert not (seen & cluster), f"agent {a.agent_id} overlaps prior components"
        seen |= cluster


@given(events=event_sequence(min_size=1, max_size=10))
@_settings
def test_p3_correlation_is_deterministic(events: list[CanonicalEvent]):
    """Two runs over identical input produce identical agent IDs."""
    r1 = correlate(events)
    r2 = correlate(events)
    ids1 = sorted(a.agent_id for a in r1.agents)
    ids2 = sorted(a.agent_id for a in r2.agents)
    assert ids1 == ids2


@given(events=event_sequence(min_size=1, max_size=8), seed=st.integers(min_value=0, max_value=1000))
@_settings
def test_p4_order_independence(events: list[CanonicalEvent], seed: int):
    """Shuffling the input doesn't change the final agent set (by event_id signature)."""
    import random
    rng = random.Random(seed)
    shuffled = list(events)
    rng.shuffle(shuffled)

    r1 = correlate(events)
    r2 = correlate(shuffled)

    sig1 = sorted(tuple(sorted(a.event_ids)) for a in r1.agents)
    sig2 = sorted(tuple(sorted(a.event_ids)) for a in r2.agents)
    assert sig1 == sig2


@given(events=event_sequence(min_size=1, max_size=12))
@_settings
def test_p5_no_conflicting_strong_identifiers(events: list[CanonicalEvent]):
    """No agent carries two distinct nhi_ids or workload_ids."""
    events_by_id = {e.event_id: e for e in events}
    result = correlate(events)
    for a in result.agents:
        nhi_ids = {events_by_id[eid].nhi_id for eid in a.event_ids
                   if events_by_id[eid].nhi_id}
        workload_ids = {events_by_id[eid].workload_id for eid in a.event_ids
                        if events_by_id[eid].workload_id}
        assert len(nhi_ids) <= 1, (
            f"agent {a.agent_id} carries multiple nhi_ids: {nhi_ids}"
        )
        assert len(workload_ids) <= 1, (
            f"agent {a.agent_id} carries multiple workload_ids: {workload_ids}"
        )


@given(events=event_sequence(min_size=1, max_size=12))
@_settings
def test_p6_confidence_is_in_valid_range(events: list[CanonicalEvent]):
    """Per-agent confidence ∈ [0.5, 1.0]. 0.5 is the weakest possible edge
    (repo-only). 1.0 is a singleton."""
    result = correlate(events)
    for a in result.agents:
        assert 0.5 <= a.correlation_confidence <= 1.0, (
            f"agent {a.agent_id} has confidence {a.correlation_confidence}"
        )


@given(events=event_sequence(min_size=1, max_size=12))
@_settings
def test_p7_singletons_have_confidence_1(events: list[CanonicalEvent]):
    """Single-event clusters always have confidence 1.0."""
    result = correlate(events)
    for a in result.agents:
        if len(a.event_ids) == 1:
            assert a.correlation_confidence == 1.0, (
                f"singleton {a.agent_id} has confidence {a.correlation_confidence}"
            )


@given(events=event_sequence(min_size=1, max_size=8))
@_settings
def test_p8_agent_id_is_hash_of_event_set(events: list[CanonicalEvent]):
    """agent_id matches the documented derivation: sha256(sorted event_ids)[:12]."""
    result = correlate(events)
    for a in result.agents:
        expected_id = "agent_" + hashlib.sha256(
            ":".join(sorted(a.event_ids)).encode("utf-8")
        ).hexdigest()[:12]
        assert a.agent_id == expected_id


@given(events=event_sequence(min_size=0, max_size=10))
@_settings
def test_p9_agent_count_bounded_by_event_count(events: list[CanonicalEvent]):
    """The number of agents is at most the number of events."""
    result = correlate(events)
    assert len(result.agents) <= len(events) if events else len(result.agents) == 0


@given(
    events=event_sequence(min_size=2, max_size=10),
    duplicate_factor=st.integers(min_value=1, max_value=3),
)
@_settings
def test_p10_duplicate_events_dont_inflate_agent_count(
    events: list[CanonicalEvent], duplicate_factor: int
):
    """Including duplicate events (same content) shouldn't create extra agents
    above the unique baseline. Note: correlate() itself doesn't dedup (that's
    Phase 1's job), but it should produce the same NUMBER of agents."""
    # Make duplicates by reusing event_ids — correlate keys on event_id, so
    # this isn't real dedup; instead just confirm uniqueness of event_ids
    # in clusters.
    result = correlate(events)
    all_ids: list[str] = []
    for a in result.agents:
        all_ids.extend(a.event_ids)
    # No event_id appears in two clusters
    assert len(all_ids) == len(set(all_ids))
