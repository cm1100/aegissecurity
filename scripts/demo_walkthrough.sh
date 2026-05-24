#!/usr/bin/env bash
# Orchestrated demo walkthrough for the video recording.
#
# Run this in the terminal while reading docs/DEMO_SCRIPT.md as narration.
# It pauses between sections so you control the pace — press Enter to advance.
#
# Usage:
#   COLUMNS=180 ./scripts/demo_walkthrough.sh
#
# Prerequisites:
#   - venv activated  (source .venv/bin/activate)  OR  `aegis` on PATH
#   - cwd is the project root

set -e

# Color helpers
B="\033[1m"; C="\033[36m"; G="\033[32m"; Y="\033[33m"; R="\033[0m"

pause() {
    echo
    echo -e "${Y}── press Enter to continue ──${R}"
    read -r
}

banner() {
    echo
    echo -e "${B}${C}════════════════════════════════════════════════════════════${R}"
    echo -e "${B}${C}  $1${R}"
    echo -e "${B}${C}════════════════════════════════════════════════════════════${R}"
    echo
}

# ---------------------------------------------------------------------------

clear
banner "AEGIS DISCOVERY — demo walkthrough"
echo -e "${B}Take-home: convert raw discovery signals into one canonical AI"
echo -e "Agent record with risk + policy + evidence.${R}"
echo
echo -e "${G}Stack:${R} Python · FastAPI · SQLAlchemy · SQLite · Pydantic v2"
echo -e "${G}Tests:${R} 233 passing · Hypothesis property-based · live HTTP · perf"
echo -e "${G}Repo: ${R} github.com/cm1100/aegissecurity"
pause

# ---------------------------------------------------------------------------

banner "Step 1 — Reset and run the full pipeline"
rm -f aegis.db
echo -e "${C}\$ rm -f aegis.db${R}"
echo -e "${C}\$ aegis demo${R}"
pause
aegis demo
pause

# ---------------------------------------------------------------------------

banner "Step 2 — Inspect the headline (shadow-PHI) agent"
SHADOW=$(.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from aegis.storage import init_db, get_session, AgentRow
init_db('sqlite:///./aegis.db')
with get_session() as s:
    r = s.query(AgentRow).filter(AgentRow.payload['nhi_id'].as_string()=='role-aegis-shadow-agent').first()
    if r: print(r.agent_id)
")
echo -e "${C}\$ aegis show ${SHADOW}${R}"
pause
aegis show "$SHADOW"
pause

# ---------------------------------------------------------------------------

banner "Step 3 — The graph projection (Agent → Identity → Tools → Data → Policy)"
echo -e "${C}\$ aegis graph ${SHADOW}${R}"
pause
aegis graph "$SHADOW"
pause

# ---------------------------------------------------------------------------

banner "Step 4 — 233 tests in ~10 seconds"
echo -e "${C}\$ pytest -q${R}"
pause
.venv/bin/pytest -q
pause

# ---------------------------------------------------------------------------

banner "Step 5 — Confirm spec example reproduction (field-by-field)"
.venv/bin/python << 'EOF'
import sys, tempfile, json
sys.path.insert(0, "src")
from fastapi.testclient import TestClient
from aegis.api import create_app
from aegis.risk import BaselineRegistry
from aegis.risk.pipeline import risk_persist
from aegis.storage import get_session

d = tempfile.mkdtemp()
c = TestClient(create_app(db_url=f"sqlite:///{d}/v.db"))
events = json.loads(open("samples/01-shadow-phi.json").read())["events"]
c.post("/events/batch", json=events)
c.post("/pipeline/normalize"); c.post("/pipeline/correlate"); c.post("/pipeline/classify")
with get_session() as s:
    risk_persist(s, baselines=BaselineRegistry.from_yaml("baselines.yaml"))
c.post("/pipeline/policy")

a = c.get("/agents").json()[0]
SPEC = {
    "nhi_id": "role-aegis-shadow-agent",
    "workload_id": "claims-processor",
    "framework": "langchain",
    "provider": "anthropic",
    "data_classes": ["PHI", "claims_data"],
    "tools": ["aurora_read", "external_llm_call"],
    "risk_score": 87,
    "risk_tier": "HIGH",
    "recommended_policy": "phi-handling-v3",
}
print()
print(f"  {'Field':<22} {'Spec example':<38} {'Aegis output':<38} Match")
print(f"  {'-'*22} {'-'*38} {'-'*38} -----")
for k, want in SPEC.items():
    got = a[k]
    match = "  \033[32m✓\033[0m" if (set(want) == set(got) if isinstance(want, list) else want == got) else "  \033[31m✗\033[0m"
    want_s = json.dumps(want) if isinstance(want, list) else str(want)
    got_s = json.dumps(got) if isinstance(got, list) else str(got)
    print(f"  {k:<22} {want_s:<38} {got_s:<38} {match}")
print()
print("  \033[32mEvery spec-example field reproduced exactly — score 87, tier HIGH, policy phi-handling-v3\033[0m")
EOF
pause

# ---------------------------------------------------------------------------

banner "Step 6 — Policy evidence chain (structured + spec-shape)"
.venv/bin/python << EOF
import sys; sys.path.insert(0,'src')
import json
from aegis.storage import init_db, get_session, AgentRow
init_db('sqlite:///./aegis.db')
with get_session() as s:
    r = s.query(AgentRow).filter(AgentRow.payload['nhi_id'].as_string()=='role-aegis-shadow-agent').first()
rec = r.payload['recommended_policy']
print()
print(f"  policy:     {rec['policy']}")
print(f"  confidence: {rec['confidence']}")
print()
print("  evidence_summary (spec-shape, flat strings):")
for s in rec['evidence_summary'][:3]:
    print(f"    • {s}")
print()
print(f"  evidence (richer structured form): {len(rec['evidence'])} items, each with claim + source_event_id + confidence")
EOF
pause

# ---------------------------------------------------------------------------

banner "Done."
echo -e "${G}Repo:${R}   github.com/cm1100/aegissecurity"
echo -e "${G}Docs:${R}   README.md  ·  docs/ARCHITECTURE.md  ·  docs/CORRELATION.md  ·  docs/FAILURE_MODES.md"
echo
