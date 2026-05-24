"""Regression suite — specific bugs caught during development.

Each test pins down a behavior that was once broken. The docstring
explains what the bug was and how it manifested, so the next maintainer
understands the WHY behind the test.
"""

from __future__ import annotations

import pytest

from aegis.risk.baselines import BaselineRegistry


# ---------------------------------------------------------------------------
# Bug 1 — /pipeline/run returned empty list when no agents existed
# ---------------------------------------------------------------------------

def test_pipeline_run_returns_int_zero_not_empty_list_when_no_agents(client):
    """Bug history: the API response wrote
        "agents": corr.agents and len(corr.agents) ...
    which short-circuits to [] (the empty list) when corr.agents is empty.
    Should return integer 0.
    """
    r = client.post("/pipeline/run").json()
    assert r["agents"] == 0
    assert isinstance(r["agents"], int), \
        f"agents was {r['agents']!r}, type {type(r['agents'])}"


# ---------------------------------------------------------------------------
# Bug 2 — baseline case mismatch with normalized agent fields
# ---------------------------------------------------------------------------

def test_baseline_registry_lowercases_strings_on_load():
    """Bug history: the normalizer lowercases tools/permissions
    (`S3:GetObject` → `s3:getobject`), but baselines stored title-case.
    The set-difference in `rule_unexpected_use` therefore flagged every
    permission as 'outside baseline', inflating drift for well-behaved
    agents. Baseline values must be normalized on load.
    """
    registry = BaselineRegistry.from_dict({
        "workloadA": {
            "tools": ["Aurora_Read", "Claim_Lookup"],
            "destinations": ["api.Internal.Corp"],
            "permissions": ["S3:GetObject", "KMS:Decrypt"],
            "data_classes": ["claims_data"],  # case-sensitive (enum value)
        }
    })
    b = registry.for_workload("workloadA")
    assert b.tools == frozenset({"aurora_read", "claim_lookup"})
    assert b.destinations == frozenset({"api.internal.corp"})
    assert b.permissions == frozenset({"s3:getobject", "kms:decrypt"})
    # data_classes intentionally preserved (enum values)
    assert b.data_classes == frozenset({"claims_data"})


def test_baseline_drift_does_not_misfire_on_lowercase_round_trip(client):
    """Integration test for Bug 2 — the approved internal agent's
    permissions should match the baseline despite case differences."""
    client.post("/events/batch", json=[
        {"event_id": "ev-nhi", "source": "nhi",
         "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-x", "workload_id": "internal-summarizer",
         "permissions": ["s3:GetObject", "kms:Decrypt"]},
        {"event_id": "ev-repo", "source": "repo",
         "timestamp": "2026-05-20T09:00:00Z",
         "repo": "internal-summarizer", "workload_id": "internal-summarizer",
         "imports": ["langchain", "aegislib"], "has_aegislib": True},
    ])
    client.post("/pipeline/run")
    agent = client.get("/agents").json()[0]
    detail = client.get(f"/agents/{agent['agent_id']}").json()
    # If the bug returned, rule_unexpected_use would fire for the
    # title-case permissions and drift would rise.
    finding_rule_ids = {f["rule_id"] for f in detail["findings"]}
    assert "unexpected_tool_use" not in finding_rule_ids, \
        "drift misfired — baseline failed to normalize permission case"


# ---------------------------------------------------------------------------
# Bug 3 — CLI list raised DetachedInstanceError
# ---------------------------------------------------------------------------

def test_cli_list_does_not_raise_when_reading_after_session_close():
    """Bug history: aegis list and aegis demo accessed SQLAlchemy ORM
    attributes after the session block closed, raising
    DetachedInstanceError. Read commands must snapshot row data to
    dicts before the session ends.

    This test runs the CLI through Typer's runner — if the bug returns
    the invocation will exit non-zero with a DetachedInstanceError.
    """
    import tempfile
    from typer.testing import CliRunner
    from aegis.cli import app
    from aegis.storage import reset_db

    with tempfile.TemporaryDirectory() as d:
        import os
        os.environ["AEGIS_DB_URL"] = f"sqlite:///{d}/r.db"
        os.environ["COLUMNS"] = "180"
        import aegis.storage.db as dbmod
        dbmod._engine = None
        dbmod._SessionLocal = None
        dbmod.DEFAULT_DB_URL = os.environ["AEGIS_DB_URL"]
        reset_db()

        runner = CliRunner()
        # ingest + run + list — must complete cleanly
        import json as _json
        ev_path = f"{d}/ev.json"
        with open(ev_path, "w") as f:
            _json.dump([
                {"event_id": "regress-1", "source": "nhi",
                 "timestamp": "2026-05-20T10:00:00Z",
                 "nhi_id": "role-x", "workload_id": "w"},
            ], f)
        for cmd in (["ingest", ev_path], ["run"], ["list"]):
            r = runner.invoke(app, cmd)
            assert r.exit_code == 0, f"{cmd}: {r.output}"


# ---------------------------------------------------------------------------
# Bug 4 — SQLAlchemy JSON-column mutation not detected without dict copy
# ---------------------------------------------------------------------------

def test_risk_factors_persisted_in_agent_payload(client):
    """Bug history: risk_persist mutated `row.payload` in place
    (`payload["risk_factors"] = ...`) and reassigned, but SQLAlchemy
    didn't detect the mutation because the dict identity was the same.
    Fix: make a shallow copy first so SQLAlchemy sees a new dict.
    """
    client.post("/events/batch", json=[
        {"event_id": "ev1", "source": "saas",
         "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-x", "data_classes": ["PHI"]},
        {"event_id": "ev2", "source": "runtime",
         "timestamp": "2026-05-20T10:01:00Z",
         "nhi_id": "role-x", "host_id": "h", "pid": 1,
         "destination": "api.anthropic.com"},
    ])
    client.post("/pipeline/run")
    aid = client.get("/agents").json()[0]["agent_id"]
    detail = client.get(f"/agents/{aid}").json()
    # If the bug returned, risk_factors would be missing from the payload.
    assert "risk_factors" in detail["agent"]
    assert len(detail["agent"]["risk_factors"]) == 4
    for f in detail["agent"]["risk_factors"]:
        assert "rationale" in f and f["rationale"]


# ---------------------------------------------------------------------------
# Bug 5 — Unknown framework hint dropped instead of recorded
# ---------------------------------------------------------------------------

def test_unknown_framework_hint_preserved_as_unknown_not_dropped(client):
    """Bug history (avoided by design): an early version dropped framework
    hints we didn't recognize. We chose to keep them as Framework.UNKNOWN
    so signal isn't lost.
    """
    client.post("/events", json={
        "event_id": "ev-weird", "source": "repo",
        "timestamp": "2026-05-20T10:00:00Z",
        "repo": "weird", "framework_hint": "invented-framework-9000",
    })
    norm = client.get("/events/normalized").json()[0]
    assert norm["framework_hint"] == "unknown", \
        "unknown framework_hint must be preserved as 'unknown', not None"


# ---------------------------------------------------------------------------
# Bug 6 — Content hash includes event_id (would defeat dedup)
# ---------------------------------------------------------------------------

def test_content_hash_excludes_event_id(client):
    """Bug history (avoided): if the dedup hash included event_id, two
    clients submitting the same logical event with different ids would
    create duplicate rows. The hash must EXCLUDE event_id.
    """
    p1 = {"event_id": "client-A", "source": "runtime",
          "timestamp": "2026-05-20T10:00:00Z",
          "host_id": "h", "pid": 1}
    p2 = {"event_id": "client-B", "source": "runtime",
          "timestamp": "2026-05-20T10:00:00Z",
          "host_id": "h", "pid": 1}
    r1 = client.post("/events", json=p1).json()
    r2 = client.post("/events", json=p2).json()
    assert r2["deduped"] is True
    assert r2["event_id"] == r1["event_id"]
