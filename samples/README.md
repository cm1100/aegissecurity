# Sample Scenarios

Five hand-crafted scenarios that exercise distinct pipeline behavior. Each
file is a manifest of the form `{ "scenario": "...", "description": "...",
"events": [...] }` and can be ingested directly:

```bash
aegis ingest --dir samples/        # all five at once
aegis run
aegis list
```

Or run the full demo, which ingests, processes, and prints a dashboard:

```bash
aegis demo
```

## Scenarios

| File | Demonstrates | Expected outcome |
|---|---|---|
| `01-shadow-phi.json` | PHI access + external LLM + no `aegislib` | 1 agent, **HIGH**, `phi-handling-v3` |
| `02-approved-internal.json` | Hygienic agent: aegislib present, internal egress, internal data | 1 agent, **LOW**, `audit-all-llm-calls` |
| `03-multi-agent-same-host.json` | Two distinct agents sharing a host — conflict guard separates them | 2 agents (one **HIGH** PHI, one **MEDIUM** financial) |
| `04-partial-and-out-of-order.json` | Reverse-chronological ingestion, duplicate event, every event carries only 1-2 identity keys | 1 agent (transitive chain assembles), 1 deduped |
| `05-corporate-fleet.json` | 11 events across 5 logical agents — fleet-scale dispatch | 5 agents covering all three policy tiers |

## How realism plays into the design

Each scenario is built around the partial-information assumption from the
spec: real signals don't carry every key.

- **Runtime events** typically have `host_id + pid + destination` but rarely
  `nhi_id`. In K8s/EKS deployments, a service-mesh sidecar adds
  `workload_id` (this is the bridge the correlator uses).
- **NHI manifests** carry `nhi_id + workload_id + permissions` but never
  `host_id`.
- **Repo scans** carry `repo + imports + framework_hint` and sometimes
  `workload_id` from a deploy manifest.
- **SaaS audit events** carry `nhi_id + action + resource + data_classes`
  from the destination platform's DLP tagging.

Every scenario is constructed so the correlator must bridge events using
*partial* keys, not direct identity. This is exactly the test the spec
emphasizes.

## Why these specific scenarios

| Scenario | Pipeline stage stressed |
|---|---|
| 01 | Risk Rule 1 (PHI→ext LLM) + Rule 2 (missing aegislib) + Policy phi-handling-v3 |
| 02 | Risk score should land low (no rules fire) + Policy audit fallback |
| 03 | Correlator's conflict guard (`would_conflict`) — same host, distinct nhi_ids |
| 04 | Order-independence + dedup + transitive chain assembly |
| 05 | Multi-agent dispatch + policy distribution across the three catalog policies |
