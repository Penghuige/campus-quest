"""S1 deadline submission rush (spec §Scenarios S1; the primary picture).

Flow per submitter: upload-intent -> presigned PUT to MinIO ->
upload-complete, at human pacing, concentrated near the deadline. The
suite deliberately replays the SAME intent once when ``complete``
fails unexpectedly (§32 idempotency under client retry) and holds a
20% "last second" cohort until deadline-5s (deadline race).

Payloads come from a shared per-family pool: one locust process is one
gevent process, and per-user generation of multi-MB files would measure
our CPU, not the server.
"""

from __future__ import annotations

import os
import random
import time
from datetime import UTC, datetime
from typing import Any

from locust import between, task

from loadtest.common import (
    API_PREFIX,
    TRACKER,
    Identity,
    classify_business_status,
    classify_status,
    elapsed_ms,
    request_with_refresh,
)
from loadtest.files import PROFILE, synthetic_file
from loadtest.runner import CampusQuestUser

INTENT_PATH = f"{API_PREFIX}/submissions/upload-intent"
COMPLETE_PATH = f"{API_PREFIX}/submissions/upload-complete"

_LAST_SECOND_ENV = "CQ_S1_LAST_SECOND_PCT"
_POOL: dict[str, bytes] = {}


def _payload_pool() -> dict[str, bytes]:
    """One parseable file per family at the band median, built once."""

    if not _POOL:
        rng = random.Random(20261030)
        for family, (_weight, low, high) in PROFILE.items():
            _POOL[family] = synthetic_file(rng, family, (low + high) // 2)
    return _POOL


class DeadlineRushUser(CampusQuestUser):
    wait_time = between(5, 15)

    @classmethod
    def identity_pool(cls) -> list[Identity]:
        # only the pre-claimed submitters; spawned count == real concurrency
        return [i for i in CampusQuestUser.identities if i.claim_id is not None]

    def on_start(self) -> None:
        super().on_start()
        deadline = datetime.fromisoformat(self.world["deadline_at"])
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        self._deadline_ts = deadline.timestamp()
        cohort = float(os.environ.get(_LAST_SECOND_ENV, "0.2"))
        self._last_second = random.random() < cohort
        self._held_for_deadline = False

    @task
    def submit(self) -> None:
        if self._last_second and not self._held_for_deadline:
            wait = self._deadline_ts - 5.0 - time.time()
            if wait > 0:
                time.sleep(wait)  # gevent-patched: other users keep running
            self._held_for_deadline = True
        self._submit_once()

    def _submit_once(self) -> None:
        pool = _payload_pool()
        families = list(pool)
        family = random.choices(
            families, weights=[PROFILE[f][0] for f in families], k=1
        )[0]
        payload = pool[family]

        captured: list[Any] = []

        def _tally(response: Any) -> None:
            captured.append(response)
            TRACKER.record(
                "s1:intent",
                classify_business_status(response.status_code),
                elapsed_ms(response),
            )

        def _tally_refresh(response: Any) -> None:
            TRACKER.record(
                "auth:refresh",
                classify_status(response.status_code),
                elapsed_ms(response),
            )

        outcome = request_with_refresh(
            self.client,
            self.identity,
            "POST",
            INTENT_PATH,
            name="s1:intent",
            on_response=_tally,
            on_refresh=_tally_refresh,
            json={
                "claim_id": self.identity.claim_id,
                "filename": f"loadtest.{family.lower()}",
                "declared_type": family,
                "size": len(payload),
            },
        )
        if outcome != "ok" or not captured:
            return  # counted (429 / business rejection); iteration ends
        try:
            grant = captured[-1].json()
        except ValueError:
            TRACKER.record("s1:intent", "err", None)
            return

        put_response = self.client.put(
            grant["upload_url"],
            data=payload,
            name="s1:put",
            headers={
                **grant["headers"],
                "Content-Length": str(grant["pinned_content_length"]),
            },
        )
        TRACKER.record(
            "s1:put",
            classify_status(put_response.status_code),
            elapsed_ms(put_response),
        )

        self._complete(grant)

    def _complete(self, grant: dict[str, Any]) -> None:
        captured: list[Any] = []

        def _tally(response: Any) -> None:
            captured.append(response)
            TRACKER.record(
                "s1:complete",
                classify_business_status(response.status_code),
                elapsed_ms(response),
            )

        def _tally_refresh(response: Any) -> None:
            TRACKER.record(
                "auth:refresh",
                classify_status(response.status_code),
                elapsed_ms(response),
            )

        request_with_refresh(
            self.client,
            self.identity,
            "POST",
            COMPLETE_PATH,
            name="s1:complete",
            on_response=_tally,
            on_refresh=_tally_refresh,
            json={"intent_id": grant["intent_id"]},
        )
        response = captured[-1] if captured else None
        # §32 replay: an unexpected failure (5xx, or the limiter's 429)
        # retries the SAME intent exactly once. The replayed response's
        # submission id is compared to the first attempt's — same id is
        # the §32 guarantee seen LIVE (plan RF1), tallied under its own
        # stat; a different id or a failed replay is an err.
        if response is not None and response.status_code in {429} | set(
            range(500, 600)
        ):
            first_id = _submission_id(response)
            replayed: list[Any] = []

            def _tally_replay(resp: Any) -> None:
                replayed.append(resp)
                TRACKER.record(
                    "s1:replay-idempotent",
                    "ok"
                    if first_id is not None and _submission_id(resp) == first_id
                    else "err",
                    elapsed_ms(resp),
                )

            request_with_refresh(
                self.client,
                self.identity,
                "POST",
                COMPLETE_PATH,
                name="s1:complete-replay",
                on_response=_tally_replay,
                on_refresh=_tally_refresh,
                json={"intent_id": grant["intent_id"]},
            )


def _submission_id(response: Any) -> str | None:
    try:
        payload = response.json()
        value = payload.get("id")
        return str(value) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None
