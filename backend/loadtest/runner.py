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
from locust.exception import StopUser

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
    _overflow_warned = False

    @classmethod
    def identity_pool(cls) -> list[Identity]:
        """Which identities this scenario runs as (default: all).

        Scenarios override this so the spawned user count IS the real
        concurrency — round-robin-then-StopUser silently wastes slots
        (found live in the S2 smoke: 30-for-10 gave 29 fired)."""

        return CampusQuestUser.identities

    def on_start(self) -> None:
        if not CampusQuestUser.identities:
            CampusQuestUser.identities, CampusQuestUser.world = load_identities(
                latest_world_path()
            )
        pool = self.identity_pool()
        index = next(CampusQuestUser._cursor)
        if index >= len(pool):
            # Review I6: silently reusing identities (index % len) makes
            # -u 200 against a 50-identity world measure "50 people at 4x
            # rate" — shared claims collide (409) and the per-user 10/min
            # limiter trips (429). Excess users stop loudly instead.
            if not CampusQuestUser._overflow_warned:
                CampusQuestUser._overflow_warned = True
                print(
                    f"loadtest: WARNING -u exceeds the identity pool "
                    f"({len(pool)}); excess users stopped — seed more "
                    f"identities for honest higher concurrency"
                )
            raise StopUser()
        self.identity = pool[index]

    def call(self, method: str, path: str, *, name: str) -> str:
        def _tally(response: Any) -> None:
            TRACKER.record(
                name, classify_status(response.status_code), elapsed_ms(response)
            )

        def _tally_refresh(response: Any) -> None:
            TRACKER.record(
                "auth:refresh",
                classify_status(response.status_code),
                elapsed_ms(response),
            )

        return request_with_refresh(
            self.client,
            self.identity,
            method,
            path,
            name=name,
            base_headers={"Authorization": f"Bearer {self.identity.access_token}"},
            on_response=_tally,
            on_refresh=_tally_refresh,
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
