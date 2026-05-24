# Architecture

This document is the debrief cheat sheet. It explains the *why* behind
every architectural choice in Aegis Discovery so the design is defensible
on call.

## The pipeline at a glance

```
                                                         ┌────────────────────┐
  POST /events  ─►  Ingest  ─►  Normalize  ─►  Correlate ─►  Classify         │
                     │           │              │          │                  │
                     │           │              │          ▼                  │
                     ▼           ▼              ▼        Risk Rules + Scorer  │
                  raw_events  normalized      agents      │                   │
                              _events       + edges       ▼                   │
                                                       findings  ─► Policy   │
                                                                      │       │
                                                                      ▼       │
                                                                   policies   │
                                                         └────────────────────┘
                                                                      │
                                                                      ▼
                                                              GET /agents
                                                              GET /agents/{id}
                                                              GET /agents/{id}/graph
```

Every stage is **one module** and **one storage table**. Every stage is
**idempotent**. The full pipeline is invoked via `POST /pipeline/run`
or `aegis run`.

## Module map

| Module | Owns |
|---|---|
| `aegis.schemas` | All Pydantic + enum definitions. Single source of truth. |
| `aegis.storage` | SQLAlchemy 2.0 models + session lifecycle. |
| `aegis.ingest` | Validate, dedup (content_hash), persist raw events. |
| `aegis.normalize` | Raw → CanonicalEvent. Pure coercion, no semantics. |
| `aegis.correlate` | The centerpiece. Weighted union-find with conflict guard. |
| `aegis.classify` | Deterministic rule-ladder picks a `Framework` for each agent. |
| `aegis.risk` | 4 risk rules + 4-factor scorer + workload baselines. |
| `aegis.policy` | Decision tree picks a policy with structured evidence. |
| `aegis.graph` | Projects an agent as a node-link graph (bonus task). |
| `aegis.api` | FastAPI routes — thin wrappers around the orchestrators. |
| `aegis.cli` | Typer CLI — same orchestrators, no duplicated logic. |

## Schema spine — why these four are the design

The system has exactly **four** schemas worth caring about, and every
other type is built from them.

1. **`RawEvent`** — what the customer sends. Every identity field is
   `Optional`. The discriminated union (`source` field) routes to the
   right per-source extras.
2. **`CanonicalEvent`** — what flows through every stage. Same identity
   fields, but typed (enums) and bookkeeped (content hash, received_at).
3. **`Agent`** — the assembled record. One per cluster. Carries the
   reconciled identity, the framework decision, the risk score, the
   policy recommendation, and the audit metadata (event_ids,
   correlation_confidence).
4. **`Evidence`** — the structured atom that flows through findings,
   scoring factors, and policy recommendations. Carries `claim`,
   `source_event_id`, `confidence`. **Strings would be cute output but
   uncomposable downstream**; structured Evidence lets a consumer chain
   findings → policy → audit-export without re-parsing.

These four schemas were defined upfront (Phase 1) and never refactored.
Schema-first design pays off: Phases 4–6 added new logic without changing
the spine.

## The decisions, with their alternatives

### 1. Union-Find for correlation (not SQL self-joins)

**Choice:** Weighted union-find with confidence-sorted edges (Kruskal's
algorithm) and a conflict guard for distinct strong identifiers.

**Alternatives considered:**
- *SQL self-joins on identity keys.* Doesn't handle transitive merges
  cleanly; query plan explodes with N keys.
- *Graph DB (Neo4j).* Right at scale, overkill for MVP.
- *Probabilistic record linkage (Fellegi-Sunter).* Requires labeled
  training data. Listed as future-work upgrade behind a flag.

**Defense:** Union-find is O(n α(n)) — effectively linear with path
compression. It handles transitive merges natively (the whole point).
The confidence-sorted Kruskal pass ensures strong evidence dominates
weak. The conflict guard turns it from a clustering algorithm into an
identity-aware one. See `docs/CORRELATION.md`.

### 2. Deterministic rules for classification (not ML)

**Choice:** A 5-rule priority ladder: framework imports → MCP config →
explicit hint → external LLM + tools → bare LLM caller → unknown.

**Alternatives considered:**
- *Trained ML classifier.* Spec explicitly says no, and rightly so for
  MVP. Production would graduate to logistic regression on
  bag-of-imports + destination one-hot, then gradient-boosted trees on
  richer features.

**Defense:** Deterministic rules are debuggable and auditable —
critical for an enterprise security tool. The rule ladder also IS the
bootstrapping classifier for ML: same input features, same labels.

### 3. SQLite via SQLAlchemy 2.0 (not Postgres, not in-memory)

**Choice:** SQLite, file-backed, with the SQLAlchemy ORM.

**Alternatives considered:**
- *In-memory dict.* Reviewer would lose state on restart; can't
  inspect.
- *Postgres.* Right for production at multi-million events; over-engineered
  for MVP review.
- *Datasette / DuckDB.* Interesting for analytics; not domain-fitting.

**Defense:** SQLite is zero-setup, queryable, persistent across runs.
It demonstrates production-thinking (real tables, real queries) without
the infra cost. The DB URL is injectable (`AEGIS_DB_URL`), so swapping
to Postgres is one env var.

### 4. Structured Evidence (not strings)

**Choice:** Every claim is an `Evidence` object with `claim`,
`source_event_id`, `confidence`.

**Alternatives considered:**
- *Plain strings (as the spec example shows).* Uncomposable, can't
  cross-reference between findings and policy.

**Defense:** Structured Evidence is strictly richer. A consumer that
wants the spec-style string list can extract `.claim`. The structure
enables: per-source attribution, per-claim confidence weighting, finding
→ policy chaining, exportable audit trails.

### 5. Baselines as a YAML config (not auto-learned)

**Choice:** `baselines.yaml` at the project root declares per-workload
expected tools/destinations/permissions. The drift rule (Rule 3) fires
only when an agent steps outside its baseline.

**Alternatives considered:**
- *Auto-learn baselines from observed activity.* Requires a known-good
  history. Promising for production; out of MVP scope.
- *Always-on baseline enforcement.* Would produce false positives until
  baselines exist.

**Defense:** Auto-learning needs a confident "training period"; for an
MVP demo, customer-managed YAML baselines are realistic (this is how
real policy-as-code repos work). Auto-learned baselines are a labeled
future-work item.

### 6. Deterministic agent IDs (not UUIDs)

**Choice:** `agent_id = "agent_" + sha256(sorted_event_ids)[:12]`.

**Alternatives considered:**
- *Server-generated UUIDs.* Easier but non-reproducible across runs.

**Defense:** Determinism is essential for tests and reviewer
reproducibility. Same cluster → same id, every time. The downside is
that adding events to a cluster mutates its id; production would
separate the stable customer-facing UUID from the content-hash id.

### 7. Eager normalization (not lazy)

**Choice:** `POST /events` normalizes synchronously, in the same
transaction.

**Alternatives considered:**
- *Lazy normalization.* Run when `/pipeline/normalize` is called.

**Defense:** Eager simplifies the operational story — the canonical
table is always current. `/pipeline/normalize` exists as a backfill for
when normalization rules change.

### 8. Delete-then-rewrite for agents/findings/policies (not differential update)

**Choice:** Every `correlate_persist`, `risk_persist`, `policy_persist`
clears its table and rewrites.

**Alternatives considered:**
- *Differential update.* Compute what changed, update only those rows.

**Defense:** Delete-then-rewrite is correct under any sequence of
changes — no diff logic to get wrong. The performance cost is bounded
by the agent count (small at MVP scale). Differential update is a
performance optimization that doesn't change semantics; defer until it
matters.

## The idempotency contract

Every stage of the pipeline is **idempotent** and **deterministic**.

| Stage | Mechanism |
|---|---|
| Ingest | `content_hash` excludes `event_id` — same logical event from any caller is one row. |
| Normalize | `persist_normalized` checks for an existing canonical row before inserting. |
| Correlate | Delete-then-rewrite the agent + edge tables. |
| Classify | In-place update of `agent.payload`. Same input → same labels. |
| Risk | Delete-then-rewrite findings; in-place update of agent risk fields. |
| Policy | Delete-then-rewrite policies; in-place update of agent policy field. |

Running `/pipeline/run` N times yields **byte-identical** agent state,
verified by `test_pipeline_run_is_idempotent_across_5_invocations`.

## The error model

| Where | What | Status |
|---|---|---|
| API boundary | Bad JSON, missing required field, unknown enum | 422 with Pydantic error detail |
| Unknown agent | `GET /agents/{id}` for a non-existent id | 404 with detail |
| Internal: malformed payload in DB | shouldn't happen (Pydantic validated at write) | bubbles as 500 with stack trace |

No silent failures. No "best-effort" partial results without a tracking
finding.

## Test architecture

| File | Tests | Owns |
|---|---|---|
| `test_ingest.py` | 11 | Schema, dedup, idempotency |
| `test_normalize.py` | 16 | Coercion, source extras, idempotency |
| `test_correlator.py` | 25 | Spec-required correlator behavior |
| `test_correlator_edge_cases.py` | 16 | Boundaries, scale, adversarial |
| `test_classify.py` | 21 | Rule ladder, evidence, framework selection |
| `test_risk.py` | 33 | 4 rules, scorer, tier boundaries |
| `test_policy.py` | 17 | Decision tree, evidence, confidence |
| `test_cli.py` | 15 | Every command, demo flow |
| `test_samples.py` | 6 | Scenario manifest assertions |
| `test_pipeline_invariants.py` | 10 | Order independence, idempotency, scoring consistency |
| `test_regression.py` | 7 | Pinned bugs from development |
| `test_graph.py` | 7 | Bonus task — graph projection |
| **Total** | **186** | |

## Performance characteristics (MVP scale)

| Workload | Wall-clock |
|---|---|
| 30 events → 10 agents (full demo) | < 200 ms |
| 100 events sharing one nhi_id | < 50 ms |
| 100 distinct singletons | < 60 ms |
| 5 / pipeline/run idempotency loop | < 1 s total |

Tested in `test_correlator_edge_cases.py::test_correlator_handles_100_*`.
Union-find with path compression scales linearly; SQLite is the only
bottleneck and is bounded by row count.

## What ships next (production path)

| Now | Next |
|---|---|
| Single-tenant SQLite | Per-tenant Postgres with row-level security |
| Batch correlation | Streaming correlator with watermarks |
| Deterministic rule classifier | ML classifier behind a `--ml` flag |
| YAML baselines | Auto-learned baselines + customer-managed PR-reviewable config repo |
| Spec catalog policies | Customer-extensible policy DSL (OPA / Rego) |
| In-process pipeline | Workflow engine (Temporal, Argo) for retry + observability |
| Sync `/pipeline/run` | Async pipeline w/ event-driven stage triggers |
| No auth | mTLS or signed-JWT service-to-service |
| FastAPI direct | API gateway + rate limiting + per-tenant quotas |
