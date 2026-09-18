"""Drive the control loop in simulated time."""

from app import catalog
from app.models import Deployment


def drive(rt, clock, ticks: int = 1, rps: float = 10.0, services=None, until=None):
    """Send ``rps`` requests/s per service through the gateway for each tick,
    advance the clock one tick and run the control loop. Stops early when
    ``until()`` returns truthy; returns the number of ticks run."""
    services = services or catalog.names()
    per_tick = int(rps * rt.tick_s)
    for i in range(ticks):
        for s in services:
            for _ in range(per_tick):
                rt.gateway.send_sync(s)
        clock.advance(rt.tick_s)
        rt.tick(clock.now)
        if until is not None and until():
            return i + 1
    return ticks


def warm(rt, clock, ticks: int = 80):
    """Pods ready, then enough clean traffic for every detector layer to
    learn a baseline (z-score 20 readings, Isolation Forest 30, seasonal two
    120 s periods)."""
    clock.advance(15)
    drive(rt, clock, ticks)


def status(db, dep_id: str) -> str:
    db.expire_all()
    return db.get(Deployment, dep_id).status
