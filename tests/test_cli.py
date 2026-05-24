"""CLI tests via Typer's CliRunner.

The CLI is a thin shell around the orchestrators tested in earlier
files; these tests verify wiring, output rendering, and the
demo-mode end-to-end run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aegis.cli import app
from aegis.storage import reset_db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the global DB url at a per-test SQLite file."""
    db_url = f"sqlite:///{tmp_path}/cli.db"
    monkeypatch.setenv("AEGIS_DB_URL", db_url)
    # Wide COLUMNS so Rich doesn't truncate table columns when run under pytest.
    monkeypatch.setenv("COLUMNS", "200")
    # Also reset the module-level engine cache
    import aegis.storage.db as dbmod
    dbmod._engine = None
    dbmod._SessionLocal = None
    dbmod.DEFAULT_DB_URL = db_url  # picked up by init_db() with no args
    reset_db(db_url)
    yield db_url


@pytest.fixture
def runner():
    return CliRunner()


def _write(path: Path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _shadow_phi_events():
    return [
        {"event_id": "rt-1", "source": "runtime",
         "timestamp": "2026-05-20T10:15:30Z",
         "host_id": "h", "pid": 4242, "workload_id": "claims-processor",
         "destination": "api.anthropic.com",
         "tools_called": ["aurora_read", "external_llm_call"]},
        {"event_id": "nhi-1", "source": "nhi",
         "timestamp": "2026-05-20T10:00:00Z",
         "nhi_id": "role-shadow", "workload_id": "claims-processor"},
        {"event_id": "repo-1", "source": "repo",
         "timestamp": "2026-05-20T09:00:00Z",
         "repo": "claims-processor", "workload_id": "claims-processor",
         "imports": ["langchain"], "has_aegislib": False},
        {"event_id": "saas-1", "source": "saas",
         "timestamp": "2026-05-20T10:16:00Z",
         "nhi_id": "role-shadow", "data_classes": ["PHI"]},
    ]


# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------

def test_init_and_reset(runner, isolated_db):
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0
    assert "initialized" in r.output
    r = runner.invoke(app, ["reset"])
    assert r.exit_code == 0


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def test_ingest_single_file(runner, isolated_db, tmp_path):
    f = _write(tmp_path / "ev.json", _shadow_phi_events()[0])
    r = runner.invoke(app, ["ingest", str(f)])
    assert r.exit_code == 0
    assert "ingested 1" in r.output


def test_ingest_list_file(runner, isolated_db, tmp_path):
    f = _write(tmp_path / "evs.json", _shadow_phi_events())
    r = runner.invoke(app, ["ingest", str(f)])
    assert r.exit_code == 0
    assert "ingested 4" in r.output


def test_ingest_directory(runner, isolated_db, tmp_path):
    d = tmp_path / "samples"
    d.mkdir()
    for i, e in enumerate(_shadow_phi_events()):
        _write(d / f"{i}.json", e)
    r = runner.invoke(app, ["ingest", "--dir", str(d)])
    assert r.exit_code == 0
    assert "ingested 4" in r.output


def test_ingest_manifest_with_events_key(runner, isolated_db, tmp_path):
    f = _write(tmp_path / "manifest.json",
               {"scenario": "shadow-phi", "events": _shadow_phi_events()})
    r = runner.invoke(app, ["ingest", str(f)])
    assert r.exit_code == 0
    assert "ingested 4" in r.output


def test_ingest_dedup_round_trip(runner, isolated_db, tmp_path):
    f = _write(tmp_path / "evs.json", _shadow_phi_events())
    runner.invoke(app, ["ingest", str(f)])
    r = runner.invoke(app, ["ingest", str(f)])
    assert "0 new, 4 deduped" in r.output


def test_ingest_missing_args(runner, isolated_db):
    r = runner.invoke(app, ["ingest"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def test_run_pipeline_end_to_end(runner, isolated_db, tmp_path):
    f = _write(tmp_path / "evs.json", _shadow_phi_events())
    runner.invoke(app, ["ingest", str(f)])
    r = runner.invoke(app, ["run"])
    assert r.exit_code == 0
    assert "Pipeline complete" in r.output


def test_individual_stages_compose(runner, isolated_db, tmp_path):
    f = _write(tmp_path / "evs.json", _shadow_phi_events())
    runner.invoke(app, ["ingest", str(f)])
    for stage in ("normalize", "correlate", "classify", "risk", "policy"):
        r = runner.invoke(app, [stage])
        assert r.exit_code == 0, f"{stage} failed: {r.output}"


# ---------------------------------------------------------------------------
# Read-side
# ---------------------------------------------------------------------------

def test_list_empty(runner, isolated_db):
    r = runner.invoke(app, ["list"])
    assert "No agents" in r.output


def test_list_after_run(runner, isolated_db, tmp_path):
    f = _write(tmp_path / "evs.json", _shadow_phi_events())
    runner.invoke(app, ["ingest", str(f)])
    runner.invoke(app, ["run"])
    r = runner.invoke(app, ["list"])
    assert r.exit_code == 0
    assert "Discovered Agents" in r.output
    assert "HIGH" in r.output
    assert "phi-handling-v3" in r.output


def test_show_known_agent(runner, isolated_db, tmp_path):
    f = _write(tmp_path / "evs.json", _shadow_phi_events())
    runner.invoke(app, ["ingest", str(f)])
    runner.invoke(app, ["run"])
    # Pull the agent_id out of the JSON store via API list logic.
    from aegis.storage import AgentRow, get_session
    with get_session() as s:
        aid = s.query(AgentRow).first().agent_id
    r = runner.invoke(app, ["show", aid])
    assert r.exit_code == 0
    assert "Identity" in r.output
    assert "Risk" in r.output
    assert "Findings" in r.output
    assert "Recommended Policy" in r.output
    assert "Correlation Audit Trail" in r.output


def test_show_unknown_agent_returns_nonzero(runner, isolated_db):
    r = runner.invoke(app, ["show", "agent_does_not_exist"])
    assert r.exit_code != 0
    assert "not found" in r.output


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def test_demo_built_in_scenario(runner, isolated_db, tmp_path):
    """With no samples/ directory, demo falls back to the built-in shadow-PHI."""
    # ensure no samples dir is found at cwd — point demo elsewhere
    nonexistent = tmp_path / "nope"
    r = runner.invoke(app, ["demo", "--samples", str(nonexistent)])
    assert r.exit_code == 0, r.output
    assert "built-in" in r.output
    assert "Discovery summary" in r.output
    assert "Discovered Agents" in r.output
    assert "phi-handling-v3" in r.output


def test_demo_from_sample_directory(runner, isolated_db, tmp_path):
    d = tmp_path / "samples" / "shadow-phi"
    d.mkdir(parents=True)
    for i, e in enumerate(_shadow_phi_events()):
        _write(d / f"{i}.json", e)
    r = runner.invoke(app, ["demo", "--samples", str(tmp_path / "samples")])
    assert r.exit_code == 0
    assert "Ingesting samples" in r.output
    assert "phi-handling-v3" in r.output
