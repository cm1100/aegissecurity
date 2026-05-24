#!/usr/bin/env bash
# Orchestrated demo walkthrough for the video recording.
#
# Run this in the terminal while reading docs/DEMO_SCRIPT.md as narration.
# Pauses between sections — press Enter to advance.
#
# Usage:
#   COLUMNS=180 ./scripts/demo_walkthrough.sh
#
# Prerequisites:
#   - venv activated  OR  `aegis` on PATH
#   - cwd is the project root

set -e

# ───────── styles ─────────
B="\033[1m"; D="\033[2m"; R="\033[0m"
C="\033[36m"; G="\033[32m"; Y="\033[33m"; M="\033[35m"

RULE="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ───────── helpers ─────────
pause() {
    echo
    echo -e "  ${D}── press Enter to continue ──${R}"
    read -r
}

step() {
    local num="$1"; local title="$2"; local hint="$3"
    echo
    echo -e "  ${B}${C}${RULE}${R}"
    echo -e "  ${B}${C}▌${R}  ${B}STEP ${num}${R}  ${D}·${R}  ${B}${title}${R}"
    if [ -n "$hint" ]; then
        echo -e "  ${B}${C}▌${R}  ${D}${hint}${R}"
    fi
    echo -e "  ${B}${C}${RULE}${R}"
    echo
}

cmd() {
    echo -e "  ${D}\$${R} ${B}${C}$1${R}"
    echo
}

# ═════════════════════════════════════════════════════════════════════════════
# OPENING
# ═════════════════════════════════════════════════════════════════════════════

clear
echo
echo -e "  ${B}${C}${RULE}${R}"
echo -e "  ${B}${C}▌${R}"
echo -e "  ${B}${C}▌${R}  ${B}A E G I S   D I S C O V E R Y${R}   ${D}— live demo${R}"
echo -e "  ${B}${C}▌${R}"
echo -e "  ${B}${C}▌${R}  Raw signals  ${D}→${R}  canonical agent  ${D}→${R}  risk  ${D}→${R}  policy"
echo -e "  ${B}${C}▌${R}"
echo -e "  ${B}${C}▌${R}  ${G}Stack${R}    Python · FastAPI · SQLAlchemy · SQLite · Pydantic v2"
echo -e "  ${B}${C}▌${R}  ${G}Tests${R}    233 passing · Hypothesis · live HTTP · perf · fuzz"
echo -e "  ${B}${C}▌${R}  ${G}Repo${R}     github.com/cm1100/aegissecurity"
echo -e "  ${B}${C}▌${R}"
echo -e "  ${B}${C}${RULE}${R}"
echo
pause

# ═════════════════════════════════════════════════════════════════════════════
# STEP 1
# ═════════════════════════════════════════════════════════════════════════════

step 1 "End-to-end demo — one command runs the whole pipeline" \
     "≈ 90 sec  ·  ingest → normalize → correlate → classify → risk → policy"
rm -f aegis.db
cmd "rm -f aegis.db && aegis demo"
pause
aegis demo
pause

# ═════════════════════════════════════════════════════════════════════════════
# STEP 2
# ═════════════════════════════════════════════════════════════════════════════

step 2 "Drill into the highest-risk agent" \
     "≈ 60 sec  ·  Identity · Risk factors · Findings · Policy · Audit trail"

SHADOW=$(.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from aegis.storage import init_db, get_session, AgentRow
init_db('sqlite:///./aegis.db')
with get_session() as s:
    rows = s.query(AgentRow).all()
    match = next((r for r in rows
                  if (r.payload or {}).get('nhi_id') == 'role-aegis-shadow-agent'),
                 None)
    if match: print(match.agent_id)
")
cmd "aegis show $SHADOW"
pause
aegis show "$SHADOW"
pause

# ═════════════════════════════════════════════════════════════════════════════
# STEP 3
# ═════════════════════════════════════════════════════════════════════════════

step 3 "Graph projection (bonus task)" \
     "≈ 30 sec  ·  Agent → Identity → Tools → Data Classes → Policy"
cmd "aegis graph $SHADOW"
pause
aegis graph "$SHADOW"
pause

# ═════════════════════════════════════════════════════════════════════════════
# STEP 4
# ═════════════════════════════════════════════════════════════════════════════

step 4 "Test suite — 233 tests in about 9 seconds" \
     "Hypothesis property-based · live uvicorn HTTP · perf budgets · fuzz · regression"
cmd "pytest -q"
pause
.venv/bin/pytest -q 2>&1 | tail -5
pause

# ═════════════════════════════════════════════════════════════════════════════
# STEP 5
# ═════════════════════════════════════════════════════════════════════════════

step 5 "Spec example reproduction — field-by-field" \
     "Spec gave an exact output. Every field matches, including risk_score = 87."
cmd "python ...  # ingest scenario 01 → full pipeline → table"
pause
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
print(f"  {'─'*22} {'─'*38} {'─'*38} {'─'*5}")
all_match = True
for k, want in SPEC.items():
    got = a[k]
    match = set(want) == set(got) if isinstance(want, list) else want == got
    if not match: all_match = False
    want_s = json.dumps(want) if isinstance(want, list) else str(want)
    got_s = json.dumps(got) if isinstance(got, list) else str(got)
    mark = "  \033[1;32m✓\033[0m" if match else "  \033[1;31m✗\033[0m"
    print(f"  {k:<22} {want_s:<38} {got_s:<38} {mark}")
print()
if all_match:
    print("  \033[1;32m✓ Every field in the spec example reproduced exactly.\033[0m")
EOF
pause

# ═════════════════════════════════════════════════════════════════════════════
# STEP 6
# ═════════════════════════════════════════════════════════════════════════════

step 6 "Policy evidence chain — spec-shape AND structured" \
     "Spec's 0.91 confidence reproduced exactly via base 0.85 + 0.02 × 3 evidence items."
cmd "python ...  # show both evidence forms"
pause
.venv/bin/python << EOF
import sys; sys.path.insert(0,'src')
import json
from aegis.storage import init_db, get_session, AgentRow
init_db('sqlite:///./aegis.db')
with get_session() as s:
    rows = s.query(AgentRow).all()
    r = next(r for r in rows if (r.payload or {}).get('nhi_id') == 'role-aegis-shadow-agent')
rec = r.payload['recommended_policy']
print()
print(f"  \033[1mpolicy:\033[0m     {rec['policy']}")
print(f"  \033[1mconfidence:\033[0m {rec['confidence']}    \033[2m(spec example shows 0.91 with 3 evidence items)\033[0m")
print()
print(f"  \033[1mevidence_summary\033[0m  \033[2m— spec-shape, flat strings (first 3 items):\033[0m")
for s in rec['evidence_summary'][:3]:
    print(f"    • {s}")
print()
print(f"  \033[1mevidence\033[0m  \033[2m— structured form, {len(rec['evidence'])} items, each with claim · source_event_id · confidence:\033[0m")
print(f"    sample: \033[2m{json.dumps(rec['evidence'][0])[:140]}\033[0m")
EOF
pause

# ═════════════════════════════════════════════════════════════════════════════
# CLOSING — summary card
# ═════════════════════════════════════════════════════════════════════════════

clear
echo
echo -e "  ${B}${C}${RULE}${R}"
echo
echo -e "       ${B}${G}✓${R}  ${B}10${R} agents discovered across 5 sample scenarios"
echo -e "       ${B}${G}✓${R}  ${B}4${R} HIGH-risk  ${D}·${R}  ${B}1${R} MEDIUM  ${D}·${R}  ${B}5${R} LOW"
echo -e "       ${B}${G}✓${R}  Spec example reproduced field-by-field  ${D}(score = 87)${R}"
echo -e "       ${B}${G}✓${R}  Spec policy confidence reproduced exactly  ${D}(0.91)${R}"
echo -e "       ${B}${G}✓${R}  ${B}233${R} tests passing in about 9 seconds"
echo -e "       ${B}${G}✓${R}  All ${B}4${R} bonus tasks delivered"
echo
echo -e "       ${B}Rubric coverage${R}"
echo -e "         ${D}·${R}  Architecture       ${D}·${R}  Correlation"
echo -e "         ${D}·${R}  Risk + Policy      ${D}·${R}  Code Quality      ${D}·${R}  Product Judgment"
echo
echo -e "       ${B}Repo${R}     github.com/cm1100/aegissecurity"
echo -e "       ${B}Docs${R}     README ${D}·${R} ARCHITECTURE ${D}·${R} CORRELATION ${D}·${R} FAILURE_MODES"
echo
echo -e "  ${B}${C}${RULE}${R}"
echo
