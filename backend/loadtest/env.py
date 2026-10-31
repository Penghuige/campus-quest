"""Environment wiring for the load-test suite.

Mirrors the Makefile ``TEST_STACK_ENV`` compose defaults so the suite
reaches the local stack without an exported environment; an exported
real variable still wins — the same contract the Makefile gives its
own suites.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_API_BASE = "http://localhost:8000/api/v1"
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://test:test@localhost:15432/campusquest_test"
)


@dataclass(frozen=True)
class LoadtestEnv:
    """Where the suite points: the local SUT API and its database."""

    api_base: str
    database_url: str


def loadtest_env() -> LoadtestEnv:
    """Resolve the SUT coordinates from the environment, with defaults."""

    return LoadtestEnv(
        api_base=os.environ.get("CQ_LOADTEST_API_BASE", _DEFAULT_API_BASE),
        database_url=os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL),
    )
