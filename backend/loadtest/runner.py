"""Locust runtime pieces: the authenticated user base and summary output.

Imported only inside locust processes (locustfiles, the locust CLI) —
never from pytest: ``import locust`` monkey-patches the process with
gevent at import time (see common.py's module note).
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any

from locust import HttpUser, events

from loadtest.common import (
    TRACKER,
    Identity,
    classify_status,
    elapsed_ms,
    latest_world_path,
    load_identities,
    request_with_refresh,
)


class CampusQuestUser(HttpUser):
    """Base user: picks an identity from the seeded world, authenticates,
    tallies every call through the shared tracker."""

    abstract = True
    host = "http://localhost:8000"

    world: dict[str, Any] = {}
    identities: list[Identity] = []
    _cursor = itertools.count()

    def on_start(self) -> None:
        if not CampusQuestUser.identities:
            CampusQuestUser.identities, CampusQuestUser.world = load_identities(
                latest_world_path()
            )
        index = next(CampusQuestUser._cursor) % len(CampusQuestUser.identities)
        self.identity = CampusQuestUser.identities[index]

    def call(self, method: str, path: str, *, name: str) -> str:
        def _tally(response: Any) -> None:
            TRACKER.record(
                name, classify_status(response.status_code), elapsed_ms(response)
            )

        # a 401 that survived refresh was tallied as "err" per-response
        # already; nothing further to do here.
        return request_with_refresh(
            self.client,
            self.identity,
            method,
            path,
            name=name,
            base_headers={"Authorization": f"Bearer {self.identity.access_token}"},
            on_response=_tally,
        )


def write_summary(environment: Any, run_dir: Path, scenario: str) -> Path:
    """Merge tracker tallies + locust's per-name stats into summary.json."""

    summary: dict[str, Any] = TRACKER.summary()
    summary["scenario"] = scenario
    entries = []
    try:
        for entry in environment.stats.entries.values():
            entries.append(
                {
                    "name": entry.name,
                    "requests": entry.num_requests,
                    "failures": entry.num_failures,
                    "p99_ms": entry.get_response_time_percentile(0.99),
                }
            )
    except Exception as exc:
        # The tracker stays the source of truth; record WHY the locust
        # cross-check is missing instead of silently dropping it.
        summary["locust_entries_error"] = f"{type(exc).__name__}: {exc}"
        entries = []
    summary["locust_entries"] = entries
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "summary.json"
    path.write_text(json.dumps(summary, indent=2))
    return path


@events.test_stop.add_listener
def _write_summary_on_stop(environment: Any, **_: Any) -> None:
    out_dir = os.environ.get("CQ_LOADTEST_OUT")
    scenario = os.environ.get("CQ_LOADTEST_SCENARIO", "run")
    if out_dir:
        path = write_summary(environment, Path(out_dir), scenario)
        print(f"loadtest: summary -> {path}")
