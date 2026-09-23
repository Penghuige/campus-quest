# backend/tests/e2e/clock_control.py
"""Mid-test time control for the committed e2e suite (plan 10 task 1).

The e2e flows need what a plain ``FrozenClock`` cannot give: moving
time FORWARD mid-flight (deadline boundary / expiry / retry-ladder
tests advance the clock between API calls instead of sleeping), then
running the worker scan AT the advanced instant. Three pieces:

- ``SteppableClock`` — a ``FrozenClock`` whose instant advances
  monotonically; anything accepting the ``Clock`` protocol accepts it.
- ``install_clock_override`` — the API seam: FastAPI's
  ``get_business_clock`` dependency overridden to return one shared
  steppable instance (the moderation-audit suite's
  ``dependency_overrides`` convention, but held for a WHOLE flow
  instead of one request).
- ``trigger_expire_claims_scan`` — the worker seam: the REAL expiry
  job's async body (``_discover_due`` + ``_expire_one``) invoked with
  an explicit ``now``. The Celery wrappers sample ``SystemClock``
  themselves (by design — the queue never owns deadline judgements),
  but both async cores take ``now`` as a parameter, exactly like the
  validation job's ``clock=`` injection; grep confirms the same shape
  in ``dispatch_due_notifications.collect_due_deliveries`` and
  ``requeue_stale_validating``'s collectors, so later task modules add
  their own triggers the same way instead of sleeping to due-ness.

G6 note: the trigger awaits the job cores directly on the caller's
event loop; each core runs through ``run_with_session`` (per-call
engine created and disposed inside the await), so no pooled connection
ever crosses a loop boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI

from app.core.clock import FrozenClock


class SteppableClock(FrozenClock):
    """A ``FrozenClock`` the test can advance mid-flight.

    ``FrozenClock`` is a frozen dataclass, so mutation goes through
    ``object.__setattr__`` — the same escape hatch the dataclass
    machinery itself uses in ``__init__``. Advancing is monotonic-only
    and requires timezone-aware instants (the ``FrozenClock``
    invariants); ``current`` stays in sync, so reads of the inherited
    field never lie about the stepped time.
    """

    def advance_to(self, moment: datetime) -> None:
        """Move the clock forward to ``moment`` (exactly; equal is a
        no-op)."""
        if moment.tzinfo is None:
            raise ValueError("SteppableClock requires timezone-aware datetime")
        current = self.now()
        if moment < current:
            raise ValueError(f"clock only moves forward: {moment} < {current}")
        object.__setattr__(self, "current", moment)

    def advance(self, delta: timedelta) -> None:
        """Move the clock forward by ``delta``."""
        if delta < timedelta(0):
            raise ValueError(f"delta must be non-negative, got {delta}")
        self.advance_to(self.now() + delta)


def install_clock_override(app: FastAPI, clock: FrozenClock) -> Callable[[], None]:
    """Point ``get_business_clock`` at ``clock`` for this app instance.

    Returns a restore function that removes the override (composition
    smoke teardown discipline: nothing the test wired survives it).
    Request-level consumers — liveness comparisons, deadline gates,
    notification scheduling — all read the stepped instant from here.
    """
    from app.modules.identity.dependencies import get_business_clock

    app.dependency_overrides[get_business_clock] = lambda: clock

    def restore() -> None:
        app.dependency_overrides.pop(get_business_clock, None)

    return restore


async def trigger_expire_claims_scan(now: datetime) -> list[dict[str, Any]]:
    """Run the REAL expiry job composition at an explicit instant.

    This is the production ``workers.expire_claims_scan`` +
    ``workers.expire_claim`` pair minus the broker hop: discovery
    (``_discover_due``) then one per-id expiry (``_expire_one``), each
    on the shared per-job session source with real commits — the Celery
    shells differ only in sampling ``SystemClock`` themselves and
    enqueuing through ``.delay``. Every outcome (EXPIRED / NOT_DUE /
    PROTECTED / ...) is a normal payload, never an exception.

    Call this AFTER ``SteppableClock.advance_to``/``advance`` moved the
    shared clock, passing the same instant — one authoritative ``now``
    for the API judgements and the worker re-judgement alike (G14).
    """
    from app.workers.jobs.expire_claims import _discover_due, _expire_one

    claim_ids = await _discover_due(now)
    return [await _expire_one(claim_id, now) for claim_id in claim_ids]


__all__ = [
    "SteppableClock",
    "install_clock_override",
    "trigger_expire_claims_scan",
]
