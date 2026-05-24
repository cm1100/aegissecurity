"""Workload-keyed baselines for drift detection.

A baseline declares what an agent assigned to a given workload is allowed
to do. The presence of a baseline is what enables Rule 3 (unexpected tool
or permission use) — without one, we cannot tell drift from normal.

For the MVP the baselines are loaded from a YAML file at the project
root. In production this would come from the customer's onboarding
manifest, typically authored as code-review-able PRs against a policy
repo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml


@dataclass(frozen=True)
class Baseline:
    workload_id: str
    tools: frozenset[str] = field(default_factory=frozenset)
    destinations: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)
    data_classes: frozenset[str] = field(default_factory=frozenset)


class BaselineRegistry:
    def __init__(self, baselines: Optional[Iterable[Baseline]] = None):
        self._by_workload: dict[str, Baseline] = {}
        for b in baselines or []:
            self._by_workload[b.workload_id] = b

    def __len__(self) -> int:
        return len(self._by_workload)

    def for_workload(self, workload_id: Optional[str]) -> Optional[Baseline]:
        if not workload_id:
            return None
        return self._by_workload.get(workload_id)

    @classmethod
    def from_dict(cls, raw: dict[str, dict]) -> "BaselineRegistry":
        """Normalize baseline strings the same way the normalizer normalizes
        observed fields (lowercase, stripped). This keeps the set-difference
        in `rule_unexpected_use` honest — otherwise 's3:GetObject' would not
        compare equal to the normalized 's3:getobject'."""
        def _norm(values):
            return frozenset(v.strip().lower() for v in (values or []))

        baselines = [
            Baseline(
                workload_id=wid,
                tools=_norm(entry.get("tools")),
                destinations=_norm(entry.get("destinations")),
                permissions=_norm(entry.get("permissions")),
                # data_classes stay case-sensitive because they're enum values.
                data_classes=frozenset(entry.get("data_classes") or []),
            )
            for wid, entry in raw.items()
        ]
        return cls(baselines)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "BaselineRegistry":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls.from_dict(raw)


def default_registry() -> BaselineRegistry:
    """Load baselines from ./baselines.yaml if present, else return empty."""
    path = Path.cwd() / "baselines.yaml"
    if path.exists():
        return BaselineRegistry.from_yaml(path)
    return BaselineRegistry()
