"""Time helpers.

All timestamps are stored as naive UTC (the V1 schema uses ``TIMESTAMP``
without a zone). ``datetime.utcnow()`` is deprecated, so this is the one
place that builds a naive-UTC "now". The source is swappable so the test
suite can run the control loop, the API and the simulated cluster on one
fake clock.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Optional

_source: Optional[Callable[[], datetime]] = None


def utcnow() -> datetime:
    if _source is not None:
        return _source()
    return datetime.now(UTC).replace(tzinfo=None)


def set_source(source: Optional[Callable[[], datetime]]) -> None:
    global _source
    _source = source
