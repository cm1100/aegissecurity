# Schemas

Four canonical schemas form the spine of the pipeline. Each is a Pydantic
v2 model in `src/aegis/schemas/`. Field-level docs live in the source.
This page is the audit-friendly cross-reference.

## 1. RawEvent (the discriminated union)

**File:** `src/aegis/schemas/raw.py`

`RawEvent` is `Annotated[Union[RuntimeEvent, NHIManifest, CapabilityHint,
SaaSAuditEvent], Field(discriminator="source")]`. The API accepts any of the
four shapes via a single field. The discriminator is the `source` value
(`runtime`, `nhi`, `repo`, or `saas`).

### Common base — `RawEventBase`

Every raw event carries these. **Only `timestamp` is required.** Every
identity field is optional because real signals arrive partial.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `event_id` | `str` | optional | Caller-provided id. Server generates `evt_<hex>` if absent. |
| `timestamp` | `datetime` | **required** | When the source observed the event (ISO 8601, any tz). |
| `nhi_id` | `str` | optional | IAM-grade identity (role ARN, role name). The strongest join key. |
| `host_id` | `str` | optional | Runtime host (EC2 instance id, hostname). |
| `pid` | `int` | optional | Process id. Only meaningful within ±5 min of timestamp. |
| `workload_id` | `str` | optional | Deployment-level identity (K8s workload, ECS service). |
| `repo` | `str` | optional | Source repository. |
| `provider` | `str` | optional | LLM provider declared by the source. |
| `model` | `str` | optional | LLM model id. |
| `framework_hint` | `str` | optional | Source-declared agent framework. |
| `tools_called` | `List[str]` | default `[]` | Tools the agent invoked. |
| `data_classes` | `List[DataClass]` | default `[]` | DLP-tagged data classes touched. |
| `destination` | `str` | optional | Network destination (host, URL fragment). |

### Source-specific extras

| Source | Extra fields | Why |
|---|---|---|
| `RuntimeEvent` | `syscalls: List[str]`, `bytes_out: Optional[int]` | eBPF-style observations. |
| `NHIManifest` | `role_arn: Optional[str]`, `permissions: List[str]`, `last_rotated: Optional[datetime]` | IAM-grade metadata. |
| `CapabilityHint` | `imports: List[str]`, `mcp_config_present: bool`, `has_aegislib: bool`, `config_files: List[str]` | Repo scan results. |
| `SaaSAuditEvent` | `action: Optional[str]`, `resource: Optional[str]`, `actor: Optional[str]` | Audit-log shape. |

### Validation

- `extra = forbid` — unknown fields raise 422 at the API boundary. This is
  schema rigor over schema permissiveness.
- Enum coercion — `data_classes` strings are validated against the
  `DataClass` enum at parse time; unknown values are rejected.

---

## 2. CanonicalEvent

**File:** `src/aegis/schemas/canonical.py`

The shape every source flows into. Inherits the identity-key fields from
`RawEventBase` but converts strings to typed enums and adds bookkeeping.

| Field | Type | Meaning |
|---|---|---|
| `event_id` | `str` | Stable across raw + canonical (same id, two tables). |
| `source` | `EventSource` | Enum: runtime / nhi / repo / saas. |
| `received_at` | `datetime` | Server wall-clock at ingest (UTC naive). |
| `observed_at` | `datetime` | Source timestamp (UTC naive after coercion). |
| `nhi_id`, `host_id`, `pid`, `workload_id`, `repo` | various optional | Identity keys, unchanged. |
| `provider` | `Optional[Provider]` | Enum. Derived from destination if not declared. |
| `model` | `Optional[str]` | Free-form (e.g., `claude-sonnet-4`). |
| `framework_hint` | `Optional[Framework]` | Enum. Unknown strings become `Framework.UNKNOWN` to preserve signal. |
| `tools_called` | `List[str]` | Lowercased, deduped. |
| `data_classes` | `List[DataClass]` | Enum list. |
| `destination` | `Optional[str]` | Free-form. |
| `imports`, `mcp_config_present`, `has_aegislib`, `config_files` | various | Carried from repo events. |
| `permissions`, `role_arn`, `last_rotated` | various | Carried from NHI events. |
| `action`, `resource`, `actor` | various | Carried from SaaS events. |
| `syscalls`, `bytes_out` | various | Carried from runtime events. |
| `content_hash` | `str` | sha256 of (source + sorted normalized fields, **excluding** event_id). Used for ingest dedup. |

---

## 3. Agent

**File:** `src/aegis/schemas/canonical.py`

The merged record — one logical agent assembled from many canonical events.

| Field | Type | Meaning |
|---|---|---|
| `agent_id` | `str` | `agent_<sha256(sorted_event_ids)[:12]>` — deterministic across runs. |
| `nhi_id`, `workload_id`, `repo` | optional | Unique within the cluster (enforced by conflict guard). |
| `host_ids` | `List[str]` | Multiple allowed (horizontal scale). |
| `framework` | `Optional[Framework]` | Set by the classifier (Phase 4). Initially `None`. |
| `framework_alternatives` | `List[FrameworkHint]` | All observed hints with provenance. |
| `framework_confidence` | `float` | Classifier output. |
| `framework_evidence` | `List[Evidence]` | Structured rationale from the rule that fired. |
| `framework_rule` | `Optional[str]` | Name of the rule that picked the framework. |
| `provider` | `Optional[Provider]` | First non-null observation. |
| `model` | `Optional[str]` | First non-null observation. |
| `data_classes` | `List[DataClass]` | Set union over events. |
| `tools` | `List[str]` | Set union. |
| `destinations` | `List[str]` | Set union. |
| `permissions` | `List[str]` | Set union. |
| `imports`, `config_files` | `List[str]` | Set union. |
| `has_aegislib`, `mcp_config_present` | `bool` | Any-True wins. |
| `event_ids` | `List[str]` | Members of the cluster, sorted. |
| `sources` | `List[EventSource]` | Distinct sources observed. |
| `first_seen`, `last_seen` | `Optional[datetime]` | Observation window. |
| `correlation_confidence` | `float` | Min of merge-edge confidences along the spanning tree. `1.0` for singletons. |
| `recommended_policy` | `Optional[PolicyRecommendation]` | Set by Phase 6. |

### Reconciliation rules (Phase 3)

- **Unique-within-cluster:** `nhi_id`, `workload_id`, `repo`. Conflict guard prevents merges that would violate this.
- **Set-union:** `tools`, `destinations`, `data_classes`, `permissions`, `imports`, `config_files`, `host_ids`.
- **Any-True-wins:** `has_aegislib`, `mcp_config_present`.
- **First-non-null:** `provider`, `model`.

---

## 4. RiskFinding + Evidence

**File:** `src/aegis/schemas/findings.py`

### `Evidence`

The reusable evidence atom — same shape across findings, scoring, and
policy.

| Field | Type | Meaning |
|---|---|---|
| `claim` | `str` | Human-readable assertion. |
| `source_event_id` | `Optional[str]` | Event that produced this claim, when attributable. |
| `confidence` | `float` | Per-claim, default `1.0`. |

### `RiskFinding`

| Field | Type | Meaning |
|---|---|---|
| `rule_id` | `str` | Machine-readable (e.g., `phi_to_external_llm`). |
| `rule_name` | `str` | Human-readable. |
| `severity` | `RiskTier` | LOW / MEDIUM / HIGH. |
| `description` | `str` | One-sentence summary. |
| `evidence` | `List[Evidence]` | Structured claims with source attribution. |

---

## 5. PolicyRecommendation

**File:** `src/aegis/schemas/findings.py`

| Field | Type | Meaning |
|---|---|---|
| `policy` | `str` | One of `phi-handling-v3`, `external-egress-redact`, `audit-all-llm-calls`. |
| `confidence` | `float` | `min(0.98, base[policy] + 0.02·|evidence|)`. |
| `evidence` | `List[Evidence]` | Chain combining agent-state claims + event attributions + corroborating findings. |
| `alternatives_considered` | `List[str]` | Policies at lower branches of the decision tree. |

---

## 6. CorrelationEdge

**File:** `src/aegis/schemas/findings.py`

One audit row per merge — the spanning-tree edges actually used during
union-find.

| Field | Type | Meaning |
|---|---|---|
| `event_a`, `event_b` | `str` | Sorted lexicographically. |
| `reason` | `str` | E.g., `"shared nhi_id"`, `"shared host+pid within 120s"`. |
| `confidence` | `float` | Edge weight. Per-agent `correlation_confidence` is `min` of these. |

---

## Enum cheat sheet

**File:** `src/aegis/schemas/enums.py`

```
EventSource    runtime | nhi | repo | saas
RiskTier       LOW | MEDIUM | HIGH
DataClass      PHI | PCI | PII | SECRETS | CLAIMS_DATA | FINANCIAL | INTERNAL | PUBLIC
Framework      langchain | langgraph | crewai | llama_index | autogen | mcp_agent
               | direct_sdk_agentic | llm_caller | unknown
Provider       anthropic | openai | google | bedrock | azure_openai | other
```

`SENSITIVE_DATA_CLASSES` and `AGENTIC_FRAMEWORKS` are predefined sets used
by risk rules — see the source.
