"""Live HTTP integration tests.

These tests start a real uvicorn process and hit it over HTTP. Unlike
the TestClient-based tests (which short-circuit the ASGI stack), these
prove the application actually boots, binds a port, accepts real
HTTP traffic, and produces valid responses.

If this suite passes, `aegis serve` is operationally green.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).parent.parent
VENV_PY = PROJECT_ROOT / ".venv" / "bin" / "python"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Boot uvicorn in a subprocess, yield the base URL, terminate on teardown."""
    port = _find_free_port()
    db_path = Path(tempfile.mkdtemp()) / "live.db"
    env = os.environ.copy()
    env["AEGIS_DB_URL"] = f"sqlite:///{db_path}"

    proc = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "aegis.api.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"

    # Wait up to 10 seconds for the server to come up
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=0.5)
            if r.status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.1)
    else:
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=2)
        pytest.fail(
            f"uvicorn did not become healthy.\nstdout:\n{stdout.decode()}\n"
            f"stderr:\n{stderr.decode()}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Basic liveness
# ---------------------------------------------------------------------------

def test_live_health_endpoint(live_server):
    r = requests.get(f"{live_server}/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_live_openapi_schema_available(live_server):
    r = requests.get(f"{live_server}/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["info"]["title"] == "Aegis Discovery"
    # All 13 endpoints discoverable
    paths = schema["paths"]
    expected = {"/health", "/events", "/events/batch", "/events/normalized",
                "/pipeline/normalize", "/pipeline/correlate", "/pipeline/classify",
                "/pipeline/risk", "/pipeline/policy", "/pipeline/run",
                "/agents", "/agents/{agent_id}", "/agents/{agent_id}/graph"}
    assert expected.issubset(set(paths))


def test_live_docs_page_renders(live_server):
    """FastAPI's /docs (Swagger UI) page renders HTML."""
    r = requests.get(f"{live_server}/docs")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# Real end-to-end through HTTP
# ---------------------------------------------------------------------------

def test_live_full_pipeline_with_shadow_phi(live_server):
    """Ingest scenario 01, run the pipeline, verify the agent over real HTTP."""
    events = json.loads(
        (PROJECT_ROOT / "samples" / "01-shadow-phi.json").read_text()
    )["events"]

    r = requests.post(f"{live_server}/events/batch", json=events)
    assert r.status_code == 201, r.text
    batch = r.json()
    assert batch["total"] == len(events)

    r = requests.post(f"{live_server}/pipeline/run")
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["agents"] >= 1

    r = requests.get(f"{live_server}/agents")
    assert r.status_code == 200
    agents = r.json()
    shadow = next(a for a in agents
                  if a["nhi_id"] == "role-aegis-shadow-agent")
    assert shadow["risk_tier"] == "HIGH"
    assert shadow["recommended_policy"] == "phi-handling-v3"
    # Note: live server uses default DB (no baselines.yaml mounted), so
    # score is the no-baseline floor (76). With baselines it would be 87.
    assert shadow["risk_score"] >= 70


def test_live_agent_detail_with_evidence_chain(live_server):
    """GET /agents/{id} returns the full evidence chain with both
    structured `evidence` and spec-shape `evidence_summary`."""
    events = json.loads(
        (PROJECT_ROOT / "samples" / "01-shadow-phi.json").read_text()
    )["events"]
    requests.post(f"{live_server}/events/batch", json=events)
    requests.post(f"{live_server}/pipeline/run")

    agents = requests.get(f"{live_server}/agents").json()
    shadow = next(a for a in agents
                  if a["nhi_id"] == "role-aegis-shadow-agent")
    aid = shadow["agent_id"]

    r = requests.get(f"{live_server}/agents/{aid}")
    assert r.status_code == 200
    detail = r.json()

    rec = detail["agent"]["recommended_policy"]
    assert rec["policy"] == "phi-handling-v3"
    # Spec-shape evidence (strings)
    assert "evidence_summary" in rec
    assert isinstance(rec["evidence_summary"], list)
    assert any("PHI" in claim for claim in rec["evidence_summary"])
    # Structured evidence (objects)
    assert isinstance(rec["evidence"], list)
    assert all("claim" in ev for ev in rec["evidence"])

    # Findings present
    assert len(detail["findings"]) >= 2
    rule_ids = {f["rule_id"] for f in detail["findings"]}
    assert "phi_to_external_llm" in rule_ids

    # Correlation edges present
    assert len(detail["correlation_edges"]) >= 1


def test_live_graph_endpoint_returns_node_link_json(live_server):
    events = json.loads(
        (PROJECT_ROOT / "samples" / "01-shadow-phi.json").read_text()
    )["events"]
    requests.post(f"{live_server}/events/batch", json=events)
    requests.post(f"{live_server}/pipeline/run")

    agents = requests.get(f"{live_server}/agents").json()
    aid = next(a for a in agents
               if a["nhi_id"] == "role-aegis-shadow-agent")["agent_id"]

    r = requests.get(f"{live_server}/agents/{aid}/graph")
    assert r.status_code == 200
    g = r.json()
    assert "nodes" in g and "edges" in g

    node_types = {n["type"] for n in g["nodes"]}
    assert {"agent", "identity", "tool", "data_class", "policy"}.issubset(node_types)


def test_live_unknown_agent_returns_404(live_server):
    r = requests.get(f"{live_server}/agents/agent_does_not_exist")
    assert r.status_code == 404


def test_live_bad_payload_returns_422(live_server):
    """Malformed event payload is rejected at the schema boundary."""
    r = requests.post(f"{live_server}/events", json={"source": "ufo"})
    assert r.status_code == 422


def test_live_dedup_through_http(live_server):
    """Posting the same event twice over HTTP → second response says deduped."""
    p = {"source": "runtime", "timestamp": "2026-05-20T10:00:00Z",
         "host_id": "live-test-host", "pid": 99,
         "destination": "api.anthropic.com"}
    r1 = requests.post(f"{live_server}/events", json=p).json()
    r2 = requests.post(f"{live_server}/events", json=p).json()
    assert r1["deduped"] is False
    assert r2["deduped"] is True
    assert r1["event_id"] == r2["event_id"]
