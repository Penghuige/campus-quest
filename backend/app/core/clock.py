# backend/app/core/clock.py
"""Injectable business-time sources.

`Clock` is the only source of business time in CampusQuest
(docs/architecture/interfaces.md; docs/quality/backend-engineering.md §11).
Production wiring uses `SystemClock` (UTC-aware); tests freeze time with
`FrozenClock`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class FrozenClock:
    current: datetime

    def __post_init__(self) -> None:
        if self.current.tzinfo is None:
            raise ValueError("FrozenClock requires timezone-aware datetime")

    def now(self) -> datetime:
        return self.current.astimezone(UTC)
