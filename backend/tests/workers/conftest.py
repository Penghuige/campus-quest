# backend/tests/workers/conftest.py
"""Shared fixtures for the worker test suite.

Eager-mode worker tests construct Celery apps, and every Celery app
CONSTRUCTION makes itself the thread-local ``current_app`` (``set_as_current``
is the default) — state that outlives the test. A later suite in the same
process then resolves that stale app through ``celery.current_app``
before any freshly built one: the known eager-app ordering hazard, seen
as tests/unit/test_app_wiring.py's lifespan binding reading the worker
tests' broker URL when worker tests ran first. Save/restore the Celery
app slots around EVERY worker test so this suite leaves no app state
behind, whichever order suites run in.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from celery import _state  # noqa: PLC2701  (test-only save/restore)


@pytest.fixture(autouse=True)
def _restore_celery_app_state() -> Iterator[None]:
    """Undo this test's side effects on Celery's global app slots.

    Celery exposes no public API for this; the two slots are the
    thread-local ``current_app`` (set by every app construction) and
    the process-wide ``default_app`` (set by ``set_default`` in
    ``get_celery_app``).
    """
    before_current = _state._tls.current_app
    before_default = _state.default_app
    yield
    _state._tls.current_app = before_current
    _state.default_app = before_default
