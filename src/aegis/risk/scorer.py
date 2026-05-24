"""Composite risk scorer — 0..100, weighted across four explicit factors.

Every factor exposes its numeric value AND a human-readable rationale,
so the final score reads as a justified arithmetic, not a magic number.

Factor weights and ceilings (totalling 100):

  Scope        25  Breadth of tool/destination/permission access. Computed
                   as `3·tools + 2·destinations + permissions`, capped at
                   25. Heavier weight on tools because tool diversity is
                   what makes an agent powerful.

  Sensitivity  35  Severity of the most-sensitive data class observed.
                   Max-of-weights rather than sum: PHI alone is already
                   maximally sensitive — touching PHI + PII isn't "more
                   regulated", it's still PHI.

  Autonomy     20  How unsupervised the agent runs. Framework category
                   drives this: agentic frameworks (LangChain etc.) get
                   the full 20; bare LLM callers get 6.

  Drift        20  Deviation from the agent's declared baseline plus the
                   "missing aegislib" signal. Composes from already-fired
                   findings so it stays explainable.

Tier mapping (per spec):
  0–39   LOW
  40–69  MEDIUM
  70–100 HIGH
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from aegis.schemas import Agent, RiskFinding, RiskTier
from aegis.schemas.enums import DataClass, Framework

AGENTIC_FRAMEWORK_VALUES = {
    Framework.LANGCHAIN.value,
    Framework.LANGGRAPH.value,
    Framework.CREWAI.value,
    Framework.LLAMA_INDEX.value,
    Framework.AUTOGEN.value,
    Framework.MCP_AGENT.value,
}

SENSITIVITY_WEIGHTS: dict[str, int] = {
    DataClass.PHI.value: 35,
    DataClass.PCI.value: 35,
    DataClass.SECRETS.value: 35,
    DataClass.CLAIMS_DATA.value: 28,
    DataClass.FINANCIAL.value: 25,
    DataClass.PII.value: 20,
    DataClass.INTERNAL.value: 5,
    DataClass.PUBLIC.value: 0,
}


@dataclass
class ScoreFactor:
    name: str
    value: int
    max_value: int
    rationale: str


@dataclass
class RiskScore:
    total: int
    tier: RiskTier
    factors: List[ScoreFactor] = field(default_factory=list)


def _scope_factor(agent: Agent) -> ScoreFactor:
    n_tools = len(agent.tools)
    n_dest = len(agent.destinations)
    n_perm = len(agent.permissions)
    points = 3 * n_tools + 2 * n_dest + n_perm
    value = min(25, points)
    return ScoreFactor(
        name="scope",
        value=value,
        max_value=25,
        rationale=f"{n_tools} tools, {n_dest} destinations, {n_perm} permissions → 3·{n_tools}+2·{n_dest}+{n_perm}={points} (capped at 25)",
    )


def _sensitivity_factor(agent: Agent) -> ScoreFactor:
    dc_values = [dc if isinstance(dc, str) else dc.value for dc in agent.data_classes]
    if not dc_values:
        return ScoreFactor(
            name="sensitivity", value=0, max_value=35,
            rationale="no data classes observed",
        )
    weighted = max(SENSITIVITY_WEIGHTS.get(dc, 0) for dc in dc_values)
    top_class = max(dc_values, key=lambda d: SENSITIVITY_WEIGHTS.get(d, 0))
    return ScoreFactor(
        name="sensitivity",
        value=weighted,
        max_value=35,
        rationale=f"highest-severity data class observed: {top_class!r} (weight {weighted})",
    )


def _autonomy_factor(agent: Agent) -> ScoreFactor:
    fw = agent.framework
    fw_value = fw if isinstance(fw, str) else (fw.value if fw else None)
    if fw_value in AGENTIC_FRAMEWORK_VALUES:
        return ScoreFactor(
            name="autonomy", value=20, max_value=20,
            rationale=f"agent runs on agentic framework {fw_value!r}",
        )
    if fw_value == Framework.DIRECT_SDK_AGENTIC.value:
        return ScoreFactor(
            name="autonomy", value=14, max_value=20,
            rationale="direct SDK use with observed tool calls",
        )
    if fw_value == Framework.LLM_CALLER.value:
        return ScoreFactor(
            name="autonomy", value=6, max_value=20,
            rationale="bare LLM caller, no observed tool use",
        )
    return ScoreFactor(
        name="autonomy", value=0, max_value=20,
        rationale=f"framework={fw_value!r} provides no autonomy signal",
    )


def _drift_factor(agent: Agent, findings: list[RiskFinding]) -> ScoreFactor:
    has_drift = any(f.rule_id == "unexpected_tool_use" for f in findings)
    missing_aegislib = any(f.rule_id == "missing_aegislib" for f in findings)
    if has_drift and missing_aegislib:
        return ScoreFactor(
            name="drift", value=20, max_value=20,
            rationale="unexpected tools/destinations AND missing aegislib",
        )
    if has_drift:
        return ScoreFactor(
            name="drift", value=14, max_value=20,
            rationale="tools/destinations outside declared baseline",
        )
    if missing_aegislib:
        return ScoreFactor(
            name="drift", value=10, max_value=20,
            rationale="agent framework present without aegislib SDK",
        )
    return ScoreFactor(
        name="drift", value=0, max_value=20,
        rationale="no drift signals",
    )


def _tier_for(total: int) -> RiskTier:
    if total >= 70:
        return RiskTier.HIGH
    if total >= 40:
        return RiskTier.MEDIUM
    return RiskTier.LOW


def score_agent(agent: Agent, findings: list[RiskFinding]) -> RiskScore:
    factors = [
        _scope_factor(agent),
        _sensitivity_factor(agent),
        _autonomy_factor(agent),
        _drift_factor(agent, findings),
    ]
    total = sum(f.value for f in factors)
    total = min(100, max(0, total))
    return RiskScore(
        total=total,
        tier=_tier_for(total),
        factors=factors,
    )
