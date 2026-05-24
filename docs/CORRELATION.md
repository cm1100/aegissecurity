# Correlation — Identity Resolution with Partial Keys

The correlator is the most-weighted component (25% of the rubric). This doc
explains the algorithm, the design choices, and why it produces honest
output instead of just plausible output.

## The problem

Real telemetry from real sources is **partial**:

| Source | Typical keys present | Typical keys missing |
|---|---|---|
| Runtime (eBPF) | `host_id`, `pid`, `destination` | `nhi_id`, `repo` |
| NHI manifest | `nhi_id`, `workload_id`, `permissions` | `host_id`, `pid` |
| Repo scan | `repo`, `imports`, `framework_hint` | `host_id`, `pid` |
| SaaS audit | `nhi_id`, `resource`, `data_classes` | `host_id`, `pid` |

No source by itself can produce an Agent record. The correlator's job is to
**bridge** these events using the keys they DO share, then merge the
events into one Agent — with field reconciliation and an audit trail.

The wrong solution would be: hard-coded rules per source pair ("when
runtime sees pid X, look up matching SaaS event"). That's brittle, doesn't
scale to new sources, and silently fails on partial data.

The right solution is **entity resolution as graph clustering**: every
shared key is an edge, and a connected component is an agent.

## The algorithm: Kruskal-style union-find with conflict guard

We use **weighted union-find** (disjoint-set union). It's the textbook
data structure for entity resolution.

### Step 1 — Index events by every non-null key

```
index_nhi      : nhi_id      → [event_id, ...]
index_workload : workload_id → [event_id, ...]
index_host_pid : (host_id, pid) → [(event_id, timestamp), ...]
index_repo     : repo        → [event_id, ...]
```

Each event lands in 0–4 indices depending on which keys it carries.

### Step 2 — Generate weighted candidate edges

For every group with ≥ 2 members in any index, emit a merge candidate:

| Index | Confidence | Notes |
|---|---|---|
| `nhi_id` | **0.95** | IAM-grade identity. Hardest to fake. |
| `workload_id` | **0.90** | Deployment identity. Strong but a workload can host multiple roles. |
| `host_id` + `pid` (within ±5 min) | **0.85** | Runtime identity, time-bounded. |
| `host_id` + `pid` (outside ±5 min) | **DO NOT EMIT** | PID reuse is the textbook gotcha. |
| `repo` | **0.50** | Multiple agents can ship from one repo. Weakest signal. |

The ±5 min window for host+pid is the **PID-reuse rule**: PIDs get
recycled by the OS, so two events sharing `(host_id, pid)` far apart in
time are almost certainly different processes. The window is the
`HOST_PID_WINDOW_SECONDS` constant (`= 300`). Tested at exact boundaries
in `test_correlator_edge_cases.py`.

### Step 3 — Kruskal: process edges in confidence order

Sort candidate edges by `(−confidence, event_a, event_b)` for
determinism. Iterate top-down:

1. Look up the components containing `event_a` and `event_b`.
2. If they're already in the same component, skip.
3. **Run the conflict guard.** If merging would put two distinct strong
   identifiers in one component, refuse. The conflict guard checks two
   things: distinct `nhi_id`s across the would-be merged components, or
   distinct `workload_id`s. Either is a hard block.
4. Otherwise, `union`. Record the edge as "used" — it becomes part of the
   spanning tree and contributes to the per-agent confidence.

The conflict guard is what makes "two agents on the same host" produce
two agents instead of one — even if they share a workload. **Identity is
strongest**; weaker bridging signals cannot override it.

### Step 4 — Components → Agents

Each connected component in the union-find is one Agent. The reconciler
walks the events in each component and produces the Agent record:

- `nhi_id`, `workload_id`, `repo`: unique within cluster (enforced by
  conflict guard).
- `tools`, `destinations`, `data_classes`, `permissions`, `imports`,
  `config_files`, `host_ids`: set-union.
- `has_aegislib`, `mcp_config_present`: any-True wins.
- `framework_alternatives`: all observed `framework_hint` values are kept
  with their source event id and source type. The classifier (Phase 4)
  picks the final `framework`.
- `provider`, `model`: first non-null observation.

### Step 5 — Confidence

Per-agent `correlation_confidence` = **min of used merge edges**.

- A cluster held together by a strong signal (`nhi_id` 0.95) reports 0.95.
- A cluster held together by `repo` alone reports 0.50.
- A chain `A↔B by nhi (0.95)` then `B↔C by workload (0.90)` reports 0.90 —
  the chain is only as strong as its weakest link.
- A single-event cluster reports 1.0 — no uncertainty, we know exactly
  what we have.

Min is more honest than mean: it surfaces the weakest necessary edge.
Product would over-penalize deep chains. Min wins.

## Determinism

`agent_id = "agent_" + sha256(":".join(sorted_event_ids))[:12]`.

Re-running correlation over the same canonical events yields:
- The **same agent ids** (sorted event_ids → same hash).
- The **same edge ordering** (sorted by `(−confidence, a, b)`).
- The **same field reconciliation** (deterministic set-union ordering via
  `_ordered_unique` which preserves first-occurrence).

Tested by `test_order_independence` and
`test_agent_id_deterministic_across_runs`.

## Idempotency

`correlate_persist(session)` is **delete-then-rewrite**. Every invocation:

1. Deletes all `AgentRow` and `CorrelationEdgeRow`.
2. Recomputes from the current `NormalizedEventRow` table.
3. Writes fresh agents + edges.

Same input → same output, every time. Tested by
`test_correlate_endpoint_is_idempotent`.

## Worked example — Scenario 01 (shadow PHI)

Four events:

| event_id | source | nhi_id | workload | host+pid | repo |
|---|---|---|---|---|---|
| `shadow-rt-1` | runtime | — | `claims-processor` | `(host-prod-01, 4242)` | — |
| `shadow-nhi-1` | nhi | `role-aegis-shadow-agent` | `claims-processor` | — | — |
| `shadow-repo-1` | repo | — | `claims-processor` | — | `claims-processor` |
| `shadow-saas-1` | saas | `role-aegis-shadow-agent` | — | — | — |

Candidate edges (sorted by confidence desc):

```
nhi_id matches (0.95):
  shadow-nhi-1 ↔ shadow-saas-1

workload_id matches (0.90):
  shadow-rt-1 ↔ shadow-nhi-1
  shadow-rt-1 ↔ shadow-repo-1
  shadow-nhi-1 ↔ shadow-repo-1

repo matches (0.50): (only shadow-repo-1 has a repo)
```

Kruskal pass:
1. `nhi-1 ↔ saas-1` at 0.95 → union (new component).
2. `rt-1 ↔ nhi-1` at 0.90 → union (no conflict).
3. `rt-1 ↔ repo-1` at 0.90 → both in different components, union.
4. `nhi-1 ↔ repo-1` at 0.90 → same component, skip.

Used edges: 3. Cluster confidence = min(0.95, 0.90, 0.90) = **0.90**.

One agent: `agent_3cb782fe5e42`. All 4 events bound. `nhi_id`,
`workload_id`, `repo` reconciled cleanly. Verified by `test_scenario_01_shadow_phi`.

## Worked example — Scenario 03 (two agents same host)

Six events: agent X (`role-patient-portal`, pid 1000) and agent Y
(`role-billing-reporter`, pid 2000) on the same host. Both runtime events
share `(host_id, pid≠same)` so no host+pid edge between X and Y. But:

The workload+nhi structure cleanly separates them. Even if some weak edge
proposed merging X and Y (say, via shared repo), the conflict guard would
refuse because the proposed cluster would contain two distinct
`nhi_id`s — a hard violation.

Output: 2 agents. Verified by `test_scenario_03_multi_agent_same_host`
and by `test_chained_conflict_block_propagates` (the property tests).

## Why this design choice over alternatives

| Alternative | Why we didn't pick it |
|---|---|
| SQL self-joins on identity keys | Doesn't generalize to transitive merges; query plan explodes with N keys. |
| Streaming join with watermarks | Right for production at scale; complexity-out-of-scope for MVP. **Listed as future work.** |
| Probabilistic record linkage (Fellegi-Sunter) | Requires labeled training data; deterministic is the bootstrapping classifier. **Listed as one-flag upgrade.** |
| Hand-coded rules per source pair | Brittle, doesn't compose, fails silently on new sources. |

## Honest gaps (and how we contain them)

1. **Repo-only matches over-merge.** Two unrelated agents shipping from
   the same monorepo would erroneously share an agent record. Mitigation:
   `repo` carries only 0.50 confidence, and the conflict guard splits the
   cluster the moment a stronger identifier surfaces. Long-term: drop
   `repo` as a primary join key once `workload_id` is universally tagged
   by the deploy platform.

2. **PID reuse within 5 minutes.** If a process dies and the OS recycles
   its PID within 5 minutes to a different agent, we'd merge them.
   Mitigation: `(host_id, pid)` is the lowest-strength runtime signal at
   0.85; conflict guard catches it when other identifiers contradict.
   Long-term: replace PID with a deploy-platform-issued container-id.

3. **Repo with multiple workloads.** Two agents in one repo are separate
   agents only if they have distinct `nhi_id` or `workload_id`. If
   neither is observed, they over-merge. Mitigation: in practice the deploy
   platform always tags one of these.

4. **Out-of-order ingest with stale conflict signal.** If a conflicting
   `nhi_id` arrives AFTER initial merges, the current implementation
   rebuilds (delete-then-rewrite). Streaming would need a more careful
   handler. See `docs/FAILURE_MODES.md`.

## What ships next

- **Streaming correlator** with watermarks and sliding-window joins for
  late-arriving events.
- **Probabilistic correlation** behind a `--probabilistic` flag (the
  Fellegi-Sunter scoring upgrade path).
- **Persistent agent ID** — separate the hash-derived id from a stable
  customer-facing UUID so it survives cluster mutation.
- **Multi-tenant correlation** — partition the union-find by tenant.
- **Graph-database backend** — Neo4j or Memgraph for sub-second queries
  over multi-million-event datasets.
