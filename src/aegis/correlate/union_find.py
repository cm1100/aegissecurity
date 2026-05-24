"""Weighted Union-Find with per-component identity tracking.

Standard DSU with two extensions:

  1. Each component carries the set of strong identifiers (nhi_id,
     workload_id) seen across its events.
  2. `would_conflict(a, b)` returns True if merging the components
     containing a and b would put two distinct strong identifiers in the
     same component. The correlator uses this to refuse merges that would
     fuse genuinely distinct agents — the canonical case is "same workload
     but distinct IAM roles".
"""

from __future__ import annotations

from collections import defaultdict
from typing import Hashable, Iterable


class WeightedUnionFind:
    def __init__(self, items: Iterable[Hashable]):
        self._parent: dict[Hashable, Hashable] = {}
        self._rank: dict[Hashable, int] = {}
        self._comp_nhi: dict[Hashable, set[str]] = {}
        self._comp_workload: dict[Hashable, set[str]] = {}
        for item in items:
            self._parent[item] = item
            self._rank[item] = 0
            self._comp_nhi[item] = set()
            self._comp_workload[item] = set()

    def find(self, x: Hashable) -> Hashable:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def declare_nhi(self, item: Hashable, nhi_id: str | None) -> None:
        if nhi_id:
            self._comp_nhi[self.find(item)].add(nhi_id)

    def declare_workload(self, item: Hashable, workload_id: str | None) -> None:
        if workload_id:
            self._comp_workload[self.find(item)].add(workload_id)

    def would_conflict(self, a: Hashable, b: Hashable) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        merged_nhi = self._comp_nhi[ra] | self._comp_nhi[rb]
        if len(merged_nhi) > 1:
            return True
        merged_workload = self._comp_workload[ra] | self._comp_workload[rb]
        if len(merged_workload) > 1:
            return True
        return False

    def union(self, a: Hashable, b: Hashable) -> bool:
        """Returns True if the call caused a merge."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False

        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

        self._comp_nhi[ra] |= self._comp_nhi[rb]
        self._comp_workload[ra] |= self._comp_workload[rb]
        del self._comp_nhi[rb]
        del self._comp_workload[rb]
        return True

    def components(self) -> list[list[Hashable]]:
        groups: dict[Hashable, list[Hashable]] = defaultdict(list)
        for item in self._parent:
            groups[self.find(item)].append(item)
        return list(groups.values())
