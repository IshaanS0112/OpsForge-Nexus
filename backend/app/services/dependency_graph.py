"""Service dependency graph: blast radius, cascade roots, topological order.

The graph is a hand-defined DAG stored in ``service_dependencies``: an edge
``(service_name, depends_on_service)`` means the first calls the second.

* ``upstream(s)``   — what ``s`` depends on.
* ``downstream(s)`` — what depends on ``s``.
* ``blast_radius(s)`` — every service that can be hurt when ``s`` fails:
  breadth-first over dependents, O(V + E).

Cascade reasoning (used by ``incident_service``): among services that are
anomalous at about the same time and connected by edges, the likely root is
one with no anomalous upstream of its own.

Honest limit: the edges are declared, not discovered. A missing edge hides a
cascade (the downstream incident looks independent); a stale edge can blame
the wrong root. Discovering edges from trace data is out of scope.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ServiceDependency


class CycleError(ValueError):
    pass


class DependencyGraph:
    def __init__(self, edges: Iterable[tuple[str, str]] = (), nodes: Iterable[str] = ()) -> None:
        self._deps: dict[str, set[str]] = {}
        self._dependents: dict[str, set[str]] = {}
        for n in nodes:
            self._add_node(n)
        for service, dependency in edges:
            self._add_edge(service, dependency)
        cycle = self._find_cycle()
        if cycle:
            raise CycleError("dependency cycle: " + " -> ".join(cycle))

    # ------------------------------------------------------------ building --
    def _add_node(self, n: str) -> None:
        self._deps.setdefault(n, set())
        self._dependents.setdefault(n, set())

    def _add_edge(self, service: str, dependency: str) -> None:
        if service == dependency:
            raise CycleError(f"{service} cannot depend on itself")
        self._add_node(service)
        self._add_node(dependency)
        self._deps[service].add(dependency)
        self._dependents[dependency].add(service)

    def _find_cycle(self) -> list[str]:
        white, grey, black = 0, 1, 2
        color = {n: white for n in self._deps}
        stack_path: list[str] = []

        def visit(n: str) -> list[str]:
            color[n] = grey
            stack_path.append(n)
            for d in sorted(self._deps[n]):
                if color[d] == grey:
                    return stack_path[stack_path.index(d) :] + [d]
                if color[d] == white:
                    found = visit(d)
                    if found:
                        return found
            stack_path.pop()
            color[n] = black
            return []

        for n in sorted(self._deps):
            if color[n] == white:
                found = visit(n)
                if found:
                    return found
        return []

    def with_edge(self, service: str, dependency: str) -> DependencyGraph:
        """A new graph including the edge; raises CycleError if it would cycle."""
        return DependencyGraph([*self.edges(), (service, dependency)], nodes=self.nodes())

    @classmethod
    def from_db(cls, db: Session) -> DependencyGraph:
        rows = db.execute(select(ServiceDependency.service_name, ServiceDependency.depends_on_service)).all()
        return cls([(r[0], r[1]) for r in rows])

    # ------------------------------------------------------------- queries --
    def nodes(self) -> list[str]:
        return sorted(self._deps)

    def edges(self) -> list[tuple[str, str]]:
        return sorted((s, d) for s, deps in self._deps.items() for d in deps)

    def __contains__(self, service: str) -> bool:
        return service in self._deps

    def upstream(self, service: str) -> list[str]:
        return sorted(self._deps.get(service, ()))

    def downstream(self, service: str) -> list[str]:
        return sorted(self._dependents.get(service, ()))

    def _bfs(self, start: str, adjacency: dict[str, set[str]]) -> list[str]:
        seen, order = {start}, []
        queue = deque(sorted(adjacency.get(start, ())))
        while queue:
            n = queue.popleft()
            if n in seen:
                continue
            seen.add(n)
            order.append(n)
            queue.extend(sorted(adjacency.get(n, ())))
        return order

    def blast_radius(self, service: str) -> list[str]:
        """Every service reachable through dependents, nearest first."""
        return self._bfs(service, self._dependents)

    def upstream_closure(self, service: str) -> list[str]:
        """Everything ``service`` depends on, directly or transitively."""
        return self._bfs(service, self._deps)

    def distance(self, service: str, upstream: str) -> int | None:
        """Hops from ``service`` down its dependencies to ``upstream``."""
        seen, queue = {service}, deque([(service, 0)])
        while queue:
            n, dist = queue.popleft()
            if n == upstream:
                return dist
            for d in self._deps.get(n, ()):
                if d not in seen:
                    seen.add(d)
                    queue.append((d, dist + 1))
        return None

    def topological_order(self) -> list[str]:
        """Dependencies before their dependents (Kahn's algorithm, name
        tie-break). The control loop evaluates services in this order so an
        upstream root is known before its dependents are classified."""
        remaining = {n: len(self._deps[n]) for n in self._deps}
        ready = sorted(n for n, k in remaining.items() if k == 0)
        order = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for dependent in sorted(self._dependents[n]):
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    ready.append(dependent)
            ready.sort()
        return order

    def layers(self) -> dict[str, int]:
        """Layer = longest dependency chain beneath a service (0 = calls
        nothing). Used to lay the graph out in tiers."""
        layer: dict[str, int] = {}
        for n in self.topological_order():
            layer[n] = 1 + max((layer[d] for d in self._deps[n]), default=-1)
        return layer

    def roots_among(self, anomalous: Iterable[str]) -> list[str]:
        """Services in ``anomalous`` with no anomalous upstream: the likely
        origins of whatever the rest of the set is suffering from."""
        bad = set(anomalous)
        return sorted(s for s in bad if not bad.intersection(self.upstream_closure(s)))

    def to_dict(self) -> dict:
        return {
            "nodes": [
                {"name": n, "layer": lay, "upstream": self.upstream(n), "downstream": self.downstream(n)}
                for n, lay in sorted(self.layers().items(), key=lambda kv: (kv[1], kv[0]))
            ],
            "edges": [{"service": s, "depends_on": d} for s, d in self.edges()],
        }
