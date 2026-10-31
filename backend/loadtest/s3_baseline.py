"""S3 read baseline (spec §Scenarios S3): moderate read concurrency.

The control leg — gives the write-path scenarios a comparison axis so
their numbers have context. 20 users at human pacing (3-10 s think
time); run via `make loadtest-s3`.
"""

from __future__ import annotations

from locust import between, task

from loadtest.common import API_PREFIX
from loadtest.runner import CampusQuestUser


class ReadBaselineUser(CampusQuestUser):
    wait_time = between(3, 10)

    @task(3)
    def task_list(self) -> None:
        self.call("GET", f"{API_PREFIX}/tasks", name="s3:tasks")

    @task(2)
    def task_detail(self) -> None:
        task_id = self.world["task_id"]
        self.call("GET", f"{API_PREFIX}/tasks/{task_id}", name="s3:task-detail")

    @task(2)
    def rankings(self) -> None:
        self.call("GET", f"{API_PREFIX}/rankings/daily", name="s3:rankings-daily")
        self.call(
            "GET",
            f"{API_PREFIX}/rankings/monthly",
            name="s3:rankings-monthly",
        )

    @task(2)
    def my_claims(self) -> None:
        self.call("GET", f"{API_PREFIX}/me/claims", name="s3:my-claims")
