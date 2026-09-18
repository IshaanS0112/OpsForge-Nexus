"""The hand-defined service catalog.

Six small services run as real pods in the local kind cluster. Each entry
declares what the service calls (its dependency edges — the same edges seeded
into ``service_dependencies``) and how its pods behave at rest.

The dependency edges are hand-defined, not discovered from traffic. Discovery
from trace data is out of scope; see docs/architecture.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    depends_on: tuple[str, ...] = ()
    # Index into the NodePort block: live = base, blue = base+1, green = base+2.
    port_index: int = 0
    base_latency_ms: float = 20.0
    # SLO latency bound: a request slower than this counts against the budget.
    # Deeper call chains get looser bounds (latency adds up hop by hop).
    slo_latency_ms: float = 300.0
    initial_version: str = "v1.0.0"
    # Tier is presentation only (dashboard grouping).
    tier: str = "backend"
    labels: dict[str, str] = field(default_factory=dict)


CATALOG: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        "notification-svc", ("checkout-api",), port_index=0, base_latency_ms=15, slo_latency_ms=500, tier="edge"
    ),
    ServiceSpec(
        "checkout-api",
        ("payments-svc", "inventory-svc"),
        port_index=1,
        base_latency_ms=25,
        slo_latency_ms=450,
        tier="edge",
    ),
    ServiceSpec("payments-svc", ("ledger-svc",), port_index=2, base_latency_ms=30, slo_latency_ms=300),
    ServiceSpec("inventory-svc", ("catalog-svc",), port_index=3, base_latency_ms=20, slo_latency_ms=300),
    ServiceSpec("ledger-svc", (), port_index=4, base_latency_ms=15, slo_latency_ms=250, tier="data"),
    ServiceSpec("catalog-svc", (), port_index=5, base_latency_ms=10, slo_latency_ms=250, tier="data"),
)

_BY_NAME = {s.name: s for s in CATALOG}


def get(name: str) -> ServiceSpec | None:
    return _BY_NAME.get(name)


def names() -> list[str]:
    return [s.name for s in CATALOG]


def edges() -> list[tuple[str, str]]:
    """(service, depends_on) pairs."""
    return [(s.name, dep) for s in CATALOG for dep in s.depends_on]
