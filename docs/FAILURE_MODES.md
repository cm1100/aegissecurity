# Failure Modes

The seven ways this system can be wrong, documented up front. Each entry
includes the *trigger condition*, the *symptom*, and the *mitigation* (if
any). Where we have a regression test pinning the behavior, the test name
is cited.

The point of this doc is to surface honest gaps for the debrief. A
production-bound version of this system would harden every entry below.

## 1. Repo-only over-merge (false positive merge)

**Trigger:** Two unrelated agents ship from the same monorepo. The repo
scanner sees one repo URL → both events tagged with the same `repo`
key. Without a stronger identifier (`nhi_id` or `workload_id`) bridging
them to a different cluster, they merge.

**Symptom:** A single "ghost" agent appears with the union of both
agents' tools, destinations, and data classes. Risk score inflates.

**Mitigation in place:** `repo` is weighted at only 0.50 — the lowest of
the four join keys. The conflict guard splits the cluster the moment a
stronger identifier (distinct `nhi_id` or `workload_id`) surfaces.

**Honest residual risk:** When NO other identifier is observed, the
over-merge stands. The cluster confidence reads 0.50 — which is the
algorithm's way of telling the operator "look closer".

**Tested by:** `test_pure_repo_only_cluster_carries_low_confidence`
(confidence flag), `test_two_events_share_repo_merge_at_low_confidence`.

**Production fix:** Drop `repo` as a primary join key once
`workload_id` is universally tagged by the deploy platform. Use `repo`
only as a corroborating signal.

---

## 2. PID reuse within the 5-minute window

**Trigger:** Process A on `host-X` with PID `1234` dies; the OS reuses
PID `1234` for unrelated process B within 5 minutes; both processes
emit runtime events.

**Symptom:** Events from A and B merge into one agent because both
share `(host-X, 1234)` within the time window.

**Mitigation in place:** Conflict guard splits them if A and B carry
distinct `nhi_id`s (e.g., from a NHI manifest or SaaS audit event).

**Honest residual risk:** If neither process has emitted an
identity-bearing event yet, they merge.

**Tested by:** `test_pid_reuse_with_distinct_nhi_does_not_merge`,
`test_host_pid_outside_window_does_not_merge`.

**Production fix:** Replace PID with a deploy-platform-issued
container-id (e.g., Docker cgroup id) — far more unique than (host, pid).

---

## 3. Conflicting `nhi_id`s observed AFTER initial merge (streaming gap)

**Trigger:** The system receives events in this order:
1. Event A (no nhi_id) shares `workload_id=W` with Event B (no nhi_id).
   They merge.
2. Event C (nhi_id=X) arrives, sharing `workload_id=W`. C joins the
   cluster.
3. Event D (nhi_id=Y, also workload_id=W) arrives. The conflict guard
   refuses to add D — but A, B, C are already merged with X and the
   system can't retroactively split them in streaming mode.

**Symptom:** In batch mode (`/pipeline/run`) this is fine — correlation
is rebuilt from scratch every time, so D's conflict surfaces in
re-correlation.

**Honest residual risk:** A real streaming correlator would need to
detect mid-cluster conflict and split. Out of MVP scope.

**Tested by:** `test_pipeline_state_is_independent_of_ingest_order`
verifies that batch correlation handles any ordering correctly.

**Production fix:** Streaming correlator with retroactive split — when
a cluster discovers it carries conflicting identifiers, re-cluster the
events with the conflict guard re-applied.

---

## 4. Custom data classes not in our enum

**Trigger:** A customer's DLP system tags data with classifications like
`"HIPAA_PHI"` or `"GDPR_PII_EU"` — strings we don't know.

**Symptom:** The Pydantic ingest layer rejects the event with HTTP 422.
Today this manifests as `test_unknown_source_rejected`-style behavior on
the `data_classes` field. The customer has to omit the unknown class.

**Honest residual risk:** Schema rigor is generally a good thing but
this surface is too rigid for production.

**Production fix:** Make `DataClass` extensible at runtime. Customer
onboarding registers their custom taxonomy; sensitivity weights are
defined per-customer.

---

## 5. Workload + Repo conflict (rare)

**Trigger:** Two repos `repo-A` and `repo-B` both deploy under
`workload_id=W`. Events from each repo would merge via workload, then
split via the conflict guard checking workload — but the guard only
checks `nhi_id` and `workload_id`, NOT `repo`.

**Symptom:** Both repos' imports/config-files end up on one Agent
record, even though they're logically separate.

**Mitigation in place:** None for the repo dimension.

**Honest residual risk:** This is a real edge case for organizations
with multi-repo deploys.

**Production fix:** Extend the conflict guard to refuse merges where
distinct `repo` values would coexist, OR treat `repo` more like a
tag (multi-valued) than an identity key.

---

## 6. Framework hint conflicts at agent level

**Trigger:** Repo A says `framework_hint="langchain"`, Repo B says
`framework_hint="crewai"`, and both merge into one cluster.

**Symptom:** `framework_alternatives` carries both hints. The
classifier in Phase 4 picks the priority winner (specific imports beat
hints; LangGraph beats LangChain). If both are equally specific, the
explicit-hint rule picks "most common".

**Mitigation in place:** All hints are preserved in
`framework_alternatives` for audit; the classifier reports its winning
rule via `framework_rule`.

**Honest residual risk:** A reviewer can read the
`framework_alternatives` list to see we had ambiguous input.

**Tested by:** `test_conflicting_framework_hints_kept_as_alternatives`,
`test_conflicting_explicit_hints_pick_most_common`.

---

## 7. Time-zone confusion on partner events

**Trigger:** A partner SaaS system emits timestamps in local time
without tz info. The normalizer treats them as UTC.

**Symptom:** Events placed in the wrong window for the host+pid
correlator → either missed merge or over-merge.

**Mitigation in place:** `_to_utc_naive` requires explicit timezone
info to convert; tz-naive strings are accepted as-is (treated as UTC).

**Honest residual risk:** A non-malicious partner could silently send
local time and we'd silently misinterpret it.

**Production fix:** Enforce timezone-aware timestamps at the API
boundary. Reject tz-naive datetimes with HTTP 422.

---

## Bonus: 8. Confidence inflation under many redundant edges

**Trigger:** A cluster has many redundant edges of the same confidence
(e.g., 10 events all sharing `nhi_id`).

**Symptom:** Per-agent confidence is `min(used edges)` = the same as
having one edge. We DON'T inflate confidence because of edge count, but
a less honest implementation might.

**Mitigation in place:** Min-of-edges is the design choice and is
explicit in the code (`docs/CORRELATION.md`).

**Tested by:** `test_correlator_handles_100_events_in_one_cluster`
verifies confidence stays at 0.95 (the per-key constant) regardless of
edge count.

---

## Where these gaps don't matter (yet)

For the MVP:

1. Repo-only merges are caught by visible low cluster confidence —
   operator-readable.
2. PID reuse within 5 min on the same host with NO nhi_id is rare in
   modern container deployments.
3. Streaming gap doesn't apply because the system is batch-only.
4. Custom data classes can be added to the enum by editing one file.
5. Workload+repo conflict is rare; affects audit clarity, not safety.
6. Framework conflicts are auditable in `framework_alternatives`.
7. Time-zone confusion requires partner cooperation to enforce.

For production: all seven are tracked as items in the engineering
backlog with explicit fixes (see `docs/ARCHITECTURE.md` § "What ships
next").
