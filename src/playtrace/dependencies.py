"""Deterministic dependency traversal and staleness propagation."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

from playtrace.models import DependencyEdge


class DependencyCycleError(ValueError):
    pass


class DependencyGraph:
    def __init__(self, edges: Iterable[DependencyEdge] = ()) -> None:
        self._downstream: dict[str, set[str]] = defaultdict(set)
        self._nodes: set[str] = set()
        for edge in edges:
            self.add(edge)

    def add(self, edge: DependencyEdge) -> None:
        self._nodes.update((edge.upstream_id, edge.downstream_id))
        self._downstream[edge.upstream_id].add(edge.downstream_id)
        if self._has_path(edge.downstream_id, edge.upstream_id):
            self._downstream[edge.upstream_id].remove(edge.downstream_id)
            raise DependencyCycleError(
                f"dependency would create a cycle: {edge.upstream_id} -> {edge.downstream_id}"
            )

    def affected_by(self, changed_ids: Iterable[str]) -> tuple[str, ...]:
        """Return changed IDs and their transitive downstream dependants."""
        seen = set(changed_ids)
        queue = deque(sorted(seen))
        while queue:
            current = queue.popleft()
            for dependant in sorted(self._downstream.get(current, ())):
                if dependant not in seen:
                    seen.add(dependant)
                    queue.append(dependant)
        return tuple(sorted(seen))

    def _has_path(self, start: str, target: str) -> bool:
        queue = deque([start])
        seen: set[str] = set()
        while queue:
            current = queue.popleft()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            queue.extend(self._downstream.get(current, ()))
        return False

