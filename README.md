# Aegis Discovery

> Convert raw discovery signals from multiple sources into one canonical AI
> Agent record, detect risk, and recommend a policy — with evidence.

This is the **core domain pipeline** of an enterprise AI-agent discovery
platform. Real telemetry from runtime probes, IAM manifests, repo
scanners, and SaaS audit logs arrives partial and in different schemas.
Aegis correlates those signals into one logical Agent per real-world
identity, scores its risk, and recommends a containment policy — with a
fully-attributed evidence chain.

The category is real and growing: Astrix Security raised $85M building
this, Token Security, Entro Security, and Oasis Security all do
variations. Non-human identities now outnumber humans 40:1 in
enterprise environments; **68% of identity incidents involve machine
identities**; and the EU AI Act's logging requirements take effect in
**2026**, creating regulatory tailwind for exactly this pipeline.

---

## Quickstart

```bash
git clone <repo>
cd aegis-discovery
make install        # create venv + install package + dev deps
make test           # 188 tests in ~3.5 s
make demo           # reset → ingest 5 samples → run pipeline → dashboard
make serve          # FastAPI on http://127.0.0.1:8000/docs
```

Or directly:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
aegis demo
```

Via Docker:

```bash
make docker-up      # builds the image and starts the container
curl http://localhost:8000/health
make docker-down
```

`aegis demo` resets the DB, ingests all 5 sample scenarios from
`samples/`, runs the full pipeline, and prints a rich dashboard plus a
drill-down on the highest-risk agent.

---

## The headline result

Scenario 01 — *shadow PHI agent on `claims-processor`* — reproduces the
spec example output **field-by-field**:

| Field | Spec example | Output |
|---|---|---|
| `nhi_id` | `role-aegis-shadow-agent` | `role-aegis-shadow-agent` ✓ |
| `workload_id` | `claims-processor` | `claims-processor` ✓ |
| `framework` | `langchain` | `langchain` ✓ |
| `provider` | `anthropic` | `anthropic` ✓ |
| `data_classes` | `[PHI, claims_data]` | `[PHI, claims_data]` ✓ |
| `tools` | `[aurora_read, external_llm_call]` | `[aurora_read, external_llm_call]` ✓ |
| `risk_score` | `87` | **`87`** ✓ |
| `risk_tier` | `HIGH` | `HIGH` ✓ |
| `recommended_policy` | `phi-handling-v3` | `phi-handling-v3` ✓ |

The full demo discovers 10 agents across 5 scenarios:

```
Discovered Agents (10)
  shadow-claims        HIGH 87   phi-handling-v3
  f1-clinical          HIGH 85   phi-handling-v3
  ehr-bot              HIGH 81   phi-handling-v3
  patient-portal       HIGH 75   phi-handling-v3
  billing-reporter     MED  58   external-egress-redact
  internal-summarizer  LOW  32   audit-all-llm-calls
  f4-experiments       LOW  30   external-egress-redact
  f2-pricing           LOW  29   external-egress-redact
  f3-docs-bot          LOW  21   audit-all-llm-calls
  orphan               LOW   2   —
```

---

## Architecture

```
                ┌─────────┐    ┌───────────┐    ┌───────────┐
   POST /events ➜ Ingest  ➜ Normalize ➜ Correlate ➜ Classify ➜ Risk ➜ Policy
                └─────────┘    └───────────┘    └───────────┘
                  raw_events    normalized_events  agents + edges + findings + policies
```

Six stages, **each idempotent**, **each in its own module**:

| Stage | Module | Purpose |
|---|---|---|
| 1. Ingest | `aegis.ingest` | Validate, dedup via content hash, persist raw. |
| 2. Normalize | `aegis.normalize` | Coerce strings → enums; UTC-normalize timestamps; case-fold tools/imports. |
| 3. Correlate | `aegis.correlate` | **Centerpiece.** Weighted union-find with conflict guard. |
| 4. Classify | `aegis.classify` | 5-rule priority ladder picks a `Framework`. |
| 5. Risk | `aegis.risk` | 4 rules + 4-factor composite scorer (0–100). |
| 6. Policy | `aegis.policy` | 3-policy decision tree with structured evidence chain. |

The pipeline is invoked as one call via `POST /pipeline/run` or `aegis
run`. Every stage is also a standalone endpoint / CLI command for
operations and debugging.

**Deep-dive docs:**
- [`docs/CORRELATION.md`](docs/CORRELATION.md) — the union-find algorithm, the conflict guard, the time-window, and 2 worked examples.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — every design decision with its alternatives.
- [`docs/SCHEMAS.md`](docs/SCHEMAS.md) — field-by-field schema reference.
- [`docs/FAILURE_MODES.md`](docs/FAILURE_MODES.md) — 7 ways this can be wrong, honest residuals.

---

## How correlation works (60-second version)

Real telemetry is partial — runtime events carry `host_id+pid+destination`
but rarely `nhi_id`; IAM manifests carry `nhi_id+workload_id` but never
`pid`. The correlator's job is to **bridge** these events using whatever
keys they DO share.

We use **Kruskal-style weighted union-find**:

| Join key | Confidence | Why |
|---|---|---|
| `nhi_id` | 0.95 | IAM-grade identity. Hardest to fake. |
| `workload_id` | 0.90 | Deployment identity. A workload can host multiple roles. |
| `host_id + pid` (within ±5 min) | 0.85 | Runtime identity, time-bounded (PID reuse). |
| `repo` | 0.50 | Multiple agents can ship from one repo. Weakest. |

Edges are sorted by confidence descending; the **conflict guard** refuses
any merge that would put two distinct strong identifiers (`nhi_id` or
`workload_id`) into one cluster. Per-agent confidence is the **minimum
of the used merge edges** — honest about the weakest necessary link.

This handles every edge case the spec calls out: partial fields,
multiple agents on one host, duplicates, conflicting framework hints,
out-of-order events.

---

## How risk scoring works

A 0–100 composite over **four explicit factors**, each producing both a
value AND a rationale string:

| Factor | Weight | Formula |
|---|---|---|
| **Scope** | 0–25 | `min(25, 3·tools + 2·destinations + permissions)` |
| **Sensitivity** | 0–35 | Max-weight of observed data classes (PHI=35, PCI=35, SECRETS=35, …) |
| **Autonomy** | 0–20 | Framework category: agentic=20, direct_sdk=14, llm_caller=6 |
| **Drift** | 0–20 | Missing `aegislib` + unexpected baseline use combinations |

Tier mapping (spec):
- `0–39` → **LOW**
- `40–69` → **MEDIUM**
- `70–100` → **HIGH**

Tested at every boundary value with `pytest.mark.parametrize`.

Three required risk rules + one bonus:

1. **PHI to External LLM** (HIGH) — PHI in data_classes ∧ external LLM destination.
2. **Agent without aegislib** (MEDIUM → HIGH if sensitive data) — agentic framework + no `aegislib` import.
3. **Unexpected tool/destination/permission use** (HIGH) — outside the workload's declared baseline (`baselines.yaml`).
4. **Secrets handling** (MEDIUM → HIGH if external egress) — bonus rule, demonstrates extensibility.

---

## Policy recommender

Three sample policies from the spec catalog:

| Branch | Trigger | Policy |
|---|---|---|
| 1 | PHI ∧ external LLM | `phi-handling-v3` |
| 2 | External LLM (no PHI) | `external-egress-redact` |
| 3 | Any LLM signal | `audit-all-llm-calls` |
| 4 | Nothing | `None` |

Confidence: `min(0.98, base[policy] + 0.02 · |evidence|)`. The spec
example (3 evidence items, `phi-handling-v3`) reproduces **0.91**
exactly. Verified by `test_spec_example_confidence_reproduces_0_91`.

Every recommendation carries a structured evidence chain (`Evidence`
objects with `claim`, `source_event_id`, `confidence`) that combines:
- Agent-state claims (e.g., "Agent accessed PHI")
- Event-level attributions (e.g., "PHI accessed at `claims.patient_records`" — `event saas-1`)
- Risk-finding cross-references (e.g., "corroborated by risk finding: `phi_to_external_llm`")

---

## API surface

```
GET    /health
POST   /events                      ingest one event
POST   /events/batch                ingest many
GET    /events                      list raw events
GET    /events/normalized           list canonical events
POST   /pipeline/normalize          backfill canonical rows
POST   /pipeline/correlate          re-run correlator
POST   /pipeline/classify           re-run classifier
POST   /pipeline/risk               re-run risk + score
POST   /pipeline/policy             re-run policy recommender
POST   /pipeline/run                run the whole pipeline
GET    /agents                      list agents (table-ready)
GET    /agents/{id}                 full agent + findings + edges + policy
GET    /agents/{id}/graph           node-link projection (bonus)
```

Auto-generated OpenAPI at `http://localhost:8000/docs` when running
`aegis serve`.

## CLI surface

```bash
aegis init                  # create DB
aegis reset                 # wipe DB
aegis ingest <file>         # ingest a JSON file
aegis ingest --dir <dir>    # ingest a directory of JSON manifests
aegis normalize / correlate / classify / risk / policy   # per-stage
aegis run                   # full pipeline
aegis list                  # tabular agent dashboard
aegis show <agent_id>       # full drilldown
aegis graph <agent_id>      # ASCII tree projection
aegis graph <agent_id> --json   # JSON for d3/cytoscape
aegis demo                  # reset + ingest samples + run + dashboard
aegis serve                 # boot the API on 127.0.0.1:8000
```

---

## Sample scenarios

Five hand-crafted manifests in [`samples/`](samples/):

| File | Demonstrates | Outcome |
|---|---|---|
| `01-shadow-phi.json` | PHI + external LLM + no `aegislib` | 1 agent, HIGH 87, `phi-handling-v3` |
| `02-approved-internal.json` | Hygienic agent with `aegislib` | 1 agent, LOW 32, `audit-all-llm-calls` |
| `03-multi-agent-same-host.json` | Two distinct agents on one host | 2 agents — conflict guard splits them |
| `04-partial-and-out-of-order.json` | Reverse-order ingest + 1 dup + partial keys | 1 agent (transitive chain), 1 deduped |
| `05-corporate-fleet.json` | 11 events → 5 agents at fleet scale | 5 agents across all 3 policy tiers + 1 orphan |

Each manifest carries an `expected` block; tests in
`tests/test_samples.py` assert the pipeline produces the declared
outcome.

---

## How this evolves to ML (spec requirement)

The rule-based classifier (`src/aegis/classify.py`) IS the bootstrapping
classifier for an eventual ML model. The path:

1. **Today** — 5 deterministic rules over `imports`, `destinations`,
   `tools_called`, `config_files`, `framework_hint`. Each rule emits
   structured evidence so its decisions are auditable.
2. **Next** — Use rule outputs as **weak labels** on a few thousand
   real-world events. Train logistic regression on bag-of-imports +
   destination one-hot encoding. Compare ML predictions to rule
   predictions; investigate divergences.
3. **Production** — Graduate to gradient-boosted trees (XGBoost /
   LightGBM) over a richer feature set: call-graph patterns, syscall
   sequences, MCP message frames captured from AgentSight-style eBPF
   probes.
4. **Always** — Keep the rules as a guardrail. When ML and rules
   disagree, log a review item; never silently let ML override the rule.

The same approach extends to risk scoring: a regression model trained on
incident data eventually replaces hand-tuned factor weights.

---

## Known limitations (the honest list)

See [`docs/FAILURE_MODES.md`](docs/FAILURE_MODES.md) for the full catalog
with mitigations. Highlights:

- **Repo-only matches can over-merge** when no stronger identifier is
  present. Surfaced via low cluster confidence (0.50).
- **PID reuse within 5 minutes** can merge unrelated processes if
  neither has emitted an identity event. Mitigation: container-id
  beats PID; tracked as production work.
- **Custom data classes** outside our enum get rejected at the API
  boundary (HTTP 422). Production needs runtime-extensible
  classifications.
- **Time-zone confusion** with partner systems that emit tz-naive
  timestamps. Production should enforce tz-aware at the boundary.
- **Streaming gap** — the system is batch-only. Streaming would need
  retroactive cluster split on late-arriving conflict signals.
- **No multi-tenancy / auth** — explicit spec exclusion. Production
  requires mTLS or signed JWTs and row-level tenant partitioning.

---

## Path to production

| Now | Next |
|---|---|
| Single-tenant SQLite | Per-tenant Postgres with row-level security |
| Batch correlation | Streaming correlator with watermarks + sliding-window joins |
| Deterministic rule classifier | ML classifier behind a `--ml` flag |
| YAML baselines | Auto-learned baselines + customer policy-as-code repo |
| Spec catalog policies | OPA / Rego policy DSL, customer-extensible |
| Sync `/pipeline/run` | Async pipeline orchestrated by Temporal or Argo |
| No auth | mTLS or signed-JWT service-to-service |
| FastAPI direct | API gateway with rate limiting + per-tenant quotas |
| In-process correlator | Sharded by tenant with Redis-backed union-find for cross-shard joins |

---

## Why this matters (the product framing)

- **Scale of the NHI problem:** NHIs outnumber humans in modern
  enterprises **40:1 to 500:1**. 68% of identity-related incidents
  involve machine identities. 50% of enterprises have already suffered
  a breach due to unmanaged NHIs.
- **Regulatory tailwind:** The EU AI Act's **logging requirements take
  effect in 2026** — every production AI agent will need an auditable
  decision trail.
- **The category is real and well-funded:** Astrix Security ($85M from
  Anthropic-led Series B), Token Security, Entro, Oasis Security ($75M
  from Sequoia / Accel / Cyberstarts). Each is solving variations of
  this pipeline.
- **What we charge for:** Discovery → governance → runtime enforcement.
  This MVP nails the discovery + governance recommendation. Real-time
  runtime enforcement (the OPA/Rego policy engine) is the next product
  surface.

---

## Tech stack

- **Python 3.11+**
- **FastAPI** for the API surface (auto-generated OpenAPI)
- **SQLAlchemy 2.0** (typed mapped style) over **SQLite**
- **Pydantic v2** for schema validation
- **Typer** for the CLI (mirrors the API exactly)
- **Rich** for terminal rendering
- **pytest** for the 186-test suite

No LLMs called. No eBPF. No real AWS connectors. The spec's
"what-to-skip" list is honored verbatim.

---

## Tests

```bash
pytest -q
# 186 passed in ~4s
```

| File | Tests | Owns |
|---|---|---|
| `test_ingest.py` | 11 | Schema, dedup |
| `test_normalize.py` | 16 | Coercion |
| `test_correlator.py` | 25 | Spec correlator behavior |
| `test_correlator_edge_cases.py` | 16 | Boundaries, scale, adversarial |
| `test_classify.py` | 21 | Rule ladder, evidence |
| `test_risk.py` | 33 | 4 rules + scorer + tier boundaries |
| `test_policy.py` | 17 | Decision tree + confidence |
| `test_cli.py` | 15 | Every command |
| `test_samples.py` | 6 | Scenario manifests |
| `test_pipeline_invariants.py` | 10 | Order independence, idempotency |
| `test_regression.py` | 7 | Pinned bugs |
| `test_graph.py` | 7 | Bonus: graph projection |
| **Total** | **186** | |

---

## Spec compliance — submission checklist

| Spec requirement | Status |
|---|---|
| Input event processor (4 sources, JSON + API) | ✅ |
| Normalizer + 4 documented schemas | ✅ |
| Identity correlator (4 join keys, all edge cases) | ✅ |
| Fingerprint classifier (deterministic, no ML) | ✅ |
| Risk detection (3 required rules) | ✅ + 1 bonus |
| Risk scorer (0–100, 4 factors, tier mapping) | ✅ |
| Policy recommender (3 policies, evidence chain) | ✅ |
| README with setup + commands | ✅ |
| Sample input files (intentionally partial) | ✅ 5 scenarios |
| Sample output (end-to-end) | ✅ |
| Architecture explanation | ✅ `docs/ARCHITECTURE.md` |
| Known limitations | ✅ `docs/FAILURE_MODES.md` |
| What to build next | ✅ README + ARCHITECTURE |
| ML evolution explanation | ✅ README |
| Bonus: `GET /agents`, `GET /agents/{id}` | ✅ |
| Bonus: Graph output | ✅ JSON + ASCII tree |
| Bonus: Correlation edge-case tests | ✅ 43 dedicated tests |
| Bonus: Correlation confidence scoring | ✅ Per-edge + per-agent |
| Spec example output reproduced field-by-field | ✅ score=87, all fields match |
| Spec example policy confidence reproduced | ✅ 0.91 with 3 evidence items |

## License

MIT — see [LICENSE](LICENSE).
