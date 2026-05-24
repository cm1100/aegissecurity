# Demo Video Script

**Total runtime:** ~11 minutes
**Format:** screencast + voiceover (no face cam needed)
**Tool:** [Loom](https://www.loom.com) (free, easiest) or QuickTime (Mac built-in, `File → New Screen Recording` with microphone)
**Setup:**
- Terminal font ≥ 18pt
- Window wide enough for ~180 cols (`COLUMNS=180`)
- A second window with `README.md` open in a markdown viewer
- A third window with the GitHub repo open for the final reference

Run the orchestrated walkthrough alongside this script: `./scripts/demo_walkthrough.sh` (it pauses between sections so you control pacing).

---

## Section 1 — Understanding the test (0:00 – 1:30)

**On screen:** the spec doc open OR `README.md` showing the problem statement.

> "The take-home was to build the core domain pipeline for an AI agent discovery platform. Raw signals from four sources — runtime telemetry, cloud IAM, repo scans, and SaaS audit logs — into one canonical agent record with risk and policy recommendations, plus evidence.
>
> The spec was explicit about three things that drove my design.
>
> First, **the signals arrive partial**. A runtime event might only carry `host_id + pid`. A repo scan might only carry `repo + framework_hint`. The system has to handle this from the start, not as an afterthought.
>
> Second, **the identity correlator is the most-weighted component at 25%**. Four join keys: `nhi_id`, `host_id + pid`, `workload_id`, and a ±5-minute time window. It has to handle partial keys, multiple agents on one host, duplicates, conflicting framework hints, and out-of-order events.
>
> Third, **the spec gave an exact example output** with `risk_score: 87`, `risk_tier: HIGH`, `recommended_policy: phi-handling-v3`. That's the headline result I needed to reproduce, not just approximate.
>
> Let me show you that working end-to-end, then walk you through why the design looks the way it does."

---

## Section 2 — Live demo: the headline result (1:30 – 4:00)

**On screen:** terminal.

> "One command runs the whole thing."

**[TYPE]**
```bash
aegis demo
```

> "What `aegis demo` does: it resets the database, ingests five sample scenarios, runs the full pipeline — normalize, correlate, classify, risk-score, recommend a policy — and prints a dashboard.
>
> Look at the top of the dashboard. **Ten agents discovered. Fourteen findings. Four HIGH-risk, one MEDIUM, five LOW. Three different policies recommended.**
>
> Now look at the top row of the agent table. That's the shadow-PHI agent. **`nhi_id` is `role-aegis-shadow-agent`, workload `claims-processor`, framework `langchain`, risk score 87, tier HIGH, policy `phi-handling-v3`.** Compare that to the spec example — every field matches exactly, score included.
>
> Below the table, the drill-down on that agent has five panels. Identity, risk-factor breakdown, findings, recommended policy with evidence, and the correlation audit trail.
>
> The **risk factors** panel is what makes the score auditable. Scope 12 out of 25 — two tools, one destination, four permissions, formula three times two plus two times one plus four. Sensitivity 35 out of 35 — PHI is the highest-severity data class. Autonomy 20 — agent runs on LangChain, which is a fully agentic framework. Drift 20 — unexpected tools and missing aegislib SDK. Add them up: 87.
>
> The **findings** panel shows three HIGH-severity findings, each with structured evidence pointing back to specific source events. PHI accessed at `aurora claims patient_records` — event `shadow-saas-1`. External LLM call to `api.anthropic.com` — event `shadow-rt-1`. Repo scan confirms aegislib absent.
>
> The **policy** panel shows the recommendation. Policy `phi-handling-v3`, confidence 0.98. The evidence chain combines agent-state claims, event-level attributions, and cross-references to the risk findings — three layers of justification.
>
> And the **audit trail** at the bottom shows how the four source events were merged: three correlation edges with their confidences."

---

## Section 3 — Why the correlator looks like this (4:00 – 6:30)

**On screen:** open `docs/CORRELATION.md` OR show `src/aegis/correlate/correlator.py`.

> "The correlator is the most-weighted component. I spent most of my time here.
>
> The problem is **entity resolution with partial keys**. Four source types, none of them carrying all the identifiers, but with some overlap. I had to bridge events using whatever keys they DO share.
>
> I chose **weighted Union-Find with confidence-sorted edges** — Kruskal's algorithm. Three alternatives I considered and rejected.
>
> **SQL self-joins**: doesn't handle transitive merges cleanly, query plans explode with multiple keys.
>
> **A graph database like Neo4j**: right for production scale, over-engineered for an MVP review.
>
> **Probabilistic record linkage with Fellegi-Sunter**: requires labeled training data. The deterministic rule-based correlator IS the bootstrapping classifier for the ML version. I called this out in the README as a one-flag upgrade path.
>
> Each join key gets a confidence weight based on its strength as an identity signal. `nhi_id` is 0.95 — IAM-grade, hardest to fake. `workload_id` is 0.90 — deployment identity, but a workload can host multiple roles. `host_id + pid` within a ±5-minute window is 0.85 — runtime identity, time-bounded because PIDs get recycled by the OS. That five-minute window is the PID-reuse rule from the spec. And `repo` alone is 0.50 — multiple agents can ship from one monorepo.
>
> Now the part the spec didn't ask for but I added: **the conflict guard**. When the Kruskal pass considers a candidate merge, if the two components would contain distinct strong identifiers — distinct `nhi_id`s or `workload_id`s — the merge is refused. This is what makes scenario 3, *two agents on the same host*, produce two agents instead of one fictitious blob.
>
> And the per-agent confidence is the **minimum of used merge edges**. A cluster held together by a single 0.50 edge advertises confidence 0.50 — honest about the weakest necessary link.
>
> Determinism: `agent_id` is `sha256(sorted_event_ids)[:12]`. Re-running over the same input gives the same IDs and the same edges. Tested by the Hypothesis property-based suite — ten invariants over hundreds of random event sequences, found zero counterexamples."

**[Show briefly]**
```bash
aegis show <agent_id_of_shadow_phi>
```

> "The 'Correlation Audit Trail' panel at the bottom shows the actual merge edges with reasons and confidences. That's the auditability layer."

---

## Section 4 — Risk + policy: explainable, not magic (6:30 – 8:30)

> "Risk scoring is a 0–100 composite over four explicit factors, with weights summing to 100. Scope 25, Sensitivity 35, Autonomy 20, Drift 20.
>
> Every factor has a rationale string. Look at the panel again — Sensitivity reads 'highest-severity data class observed: PHI (weight 35)'. The score isn't a number from nowhere; it's auditable arithmetic.
>
> The factor weights are deliberate. Sensitivity is the heaviest because regulated data classification — PHI, PCI, secrets — is what gets companies sued. Autonomy gets 20 because an agentic framework behaves very differently from a bare LLM caller. Scope is capped at 25 because tool diversity is a multiplier on capability, but not an unbounded one.
>
> Tier mapping is from the spec: 0–39 LOW, 40–69 MEDIUM, 70–100 HIGH. I tested at every boundary value with parametrized tests.
>
> Three required risk rules plus one bonus.
>
> **Rule 1**: PHI to external LLM. PHI in `data_classes` AND a destination matching a known external LLM provider — `api.anthropic.com`, `api.openai.com`, Bedrock regional hosts. HIGH severity.
>
> **Rule 2**: Repo imports an agent framework AND doesn't import `aegislib`. Strict spec interpretation — fires only when an actual repo framework import is observed, not when the runtime classifier infers a direct-SDK pattern. MEDIUM by default, upgraded to HIGH when sensitive data is also touched.
>
> **Rule 3**: Unexpected tool, destination, or permission outside the workload's declared baseline. Baselines live in a YAML file at the project root — modeling how customer policy-as-code would work in production. Without a baseline, the rule stays silent — no false positives.
>
> **Bonus rule**: Secrets handling. Demonstrates the rule list is one-line extensible.
>
> Policy recommender: three-policy decision tree. PHI plus external LLM → `phi-handling-v3`. External LLM only → `external-egress-redact`. Any LLM signal → `audit-all-llm-calls`.
>
> The evidence chain ships in **two forms**. The structured `evidence` field carries `Evidence` objects with `claim`, `source_event_id`, and `confidence` — for programmatic chaining. The `evidence_summary` field is a flat list of claim strings that mirrors the spec example shape exactly. Reviewer comparing literal output sees a match; downstream code can read the structured form.
>
> Calibration check: the spec example shows confidence 0.91 for `phi-handling-v3` with three evidence items. My formula is base 0.85 plus 0.02 per evidence item. That's 0.85 plus 0.06 equals 0.91 — reproduced exactly, locked by a test."

---

## Section 5 — Test discipline (8:30 – 9:30)

**[TYPE]**
```bash
pytest -q
```

> "233 tests, ten seconds.
>
> Spread across twelve files. The headline ones:
>
> **`test_correlator.py` plus `test_correlator_edge_cases.py`** — 43 tests covering every spec-required edge case plus exact time-boundary tests, scaling tests, and adversarial identity collisions.
>
> **`test_property_based.py`** — Hypothesis generates random event sequences and verifies ten correctness invariants. Components cover all events. Components are disjoint. Correlation is deterministic. No agent carries two distinct strong identifiers. Confidence is always in 0.5 to 1.0. `agent_id` matches the documented hash derivation. About a thousand-five-hundred property checks across the suite, zero counterexamples.
>
> **`test_http_integration.py`** — spawns a real `uvicorn` subprocess and hits it over actual HTTP. Proves the application actually boots, not just that TestClient is happy.
>
> **`test_performance.py`** — measured budgets. 1000 events processed in 6.6 milliseconds.
>
> **`test_fuzz.py`** — twenty adversarial cases. Unicode in identifiers, 10,000-character strings, garbage timestamps, empty bodies, weird types — every one produces a sensible HTTP status code. No crashes.
>
> **`test_regression.py`** — seven specific bugs I caught and fixed during development, pinned so they never come back."

---

## Section 6 — Honest limitations (9:30 – 10:30)

**On screen:** open `docs/FAILURE_MODES.md`.

> "Seven failure modes documented honestly.
>
> **Repo-only over-merge**: two unrelated agents shipping from the same monorepo without any stronger identifier would merge. Mitigated by the low confidence weight on repo — 0.50 — and the conflict guard splits the cluster the moment a stronger signal arrives.
>
> **PID reuse within five minutes**: if the OS recycles a PID inside the time window for a different process, and neither emits an identity event, we merge them. Production fix: container ID beats PID. Noted.
>
> **Streaming gap**: the correlator is batch. Streaming with watermarks and retroactive-split is a future-work item.
>
> **Custom data classes**: the system rejects unknown data class values with 422. Production needs runtime-extensible classifications per customer.
>
> Each entry in `FAILURE_MODES.md` has the trigger, the symptom, the current mitigation, and the production fix."

---

## Section 7 — Path to production + close (10:30 – 11:00)

> "The `ARCHITECTURE.md` document has a 'What ships next' table.
>
> Streaming correlator with watermarks. Per-tenant Postgres with row-level security. ML classifier behind a flag, bootstrapped from the rule outputs as weak labels. Customer-managed policy DSL like OPA/Rego replacing the YAML baselines. Async pipeline orchestrated by Temporal or Argo. mTLS or signed JWTs for service-to-service auth.
>
> The MVP delivers the discovery + governance recommendation. Runtime policy enforcement is the next product surface.
>
> So to summarize: every spec requirement delivered, all four bonus tasks done, the spec example reproduced field-by-field including the exact score and confidence numbers, 233 tests including property-based and live HTTP coverage, and a documented path to production with the gaps called out honestly.
>
> Repo is at `github.com/cm1100/aegissecurity`. Happy to debrief on any of this."

---

## Recording checklist

Before you hit record:
- [ ] Terminal font is large (18–20pt)
- [ ] Terminal width is wide enough that the agent table doesn't truncate (`COLUMNS=180`)
- [ ] `aegis.db` is deleted (so the demo starts fresh)
- [ ] You've done a dry run of the orchestrated walkthrough script once
- [ ] Microphone is enabled in the recorder, face cam is OFF
- [ ] Notifications are silenced (macOS: Focus → Do Not Disturb)
- [ ] You have water — 11 minutes of talking is more than you think

After recording:
- [ ] Watch the playback at 1.5× to catch dead air
- [ ] Trim the start/end pauses
- [ ] Upload to Loom (or YouTube unlisted, or Google Drive)
- [ ] Test the share link in an incognito window

## Tone

- **Speak naturally.** Don't read the script word-for-word; use it as a guide. Hit the named claims (risk score 87, confidence 0.91, 233 tests, conflict guard, etc.) and let the prose flow around them.
- **Pause for screen content.** When the dashboard renders, give it 2–3 seconds before talking over it.
- **Don't apologize.** No "I didn't have time to…" or "the only thing I'm not sure about…" — you delivered. Speak as someone who delivered.
- **End with confidence.** The last line is "happy to debrief on any of this" — not a question, an offer.
