"""Linux process ownership helper for the Windows local Worker/Beat launcher.

Only handles its recorded Celery process in the recruitment-only profile.
No database writes, queue purge, container deletion or broad process matching.
"""

import argparse
import json
import os
import re
import signal
import time
from pathlib import Path

PROFILE = "app.workers.innovation_dev:celery_app"
CELERY = "/opt/cq-noble-venv/bin/celery"


def start_time(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def owned(record: dict, kind: str, identity: str) -> bool:
    pid = record["pid"]
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
        return (
            start_time(pid) == record["start_time"]
            and PROFILE in command
            and kind in command
            and record["identity"] == identity
            and (
                f"--hostname=innovation-dev@{identity}" in command
                if kind == "worker"
                else f"--pidfile=/evidence/{identity}-beat.celery.pid" in command
            )
        )
    except FileNotFoundError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("kind", choices=("worker", "beat"))
    parser.add_argument("identity", nargs="?", default="local")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,48}", args.identity):
        raise ValueError("invalid runtime identity")
    record_path = Path(f"/evidence/{args.identity}-{args.kind}.ownership.json")
    record = json.loads(record_path.read_text()) if record_path.exists() else None
    if args.action == "start":
        if record is not None and owned(record, args.kind, args.identity):
            raise RuntimeError("owned runtime is already running")
        pid = os.getpid()
        record_path.write_text(
            json.dumps(
                {"pid": pid, "start_time": start_time(pid), "identity": args.identity}
            )
        )
        command = [CELERY, "-A", PROFILE, args.kind, "--loglevel=INFO"]
        if args.kind == "worker":
            command += [
                "--pool=solo",
                "--concurrency=1",
                "--queues=innovation-dev",
                f"--hostname=innovation-dev@{args.identity}",
            ]
        else:
            command += [
                f"--pidfile=/evidence/{args.identity}-beat.celery.pid",
                f"--schedule=/evidence/{args.identity}-beat.schedule",
            ]
        os.execv(CELERY, command)
    if record is None or not owned(record, args.kind, args.identity):
        print("not running (no owned process)")
        if args.action == "status":
            raise SystemExit(1)
        return
    if args.action == "status":
        print(f"owned {args.kind} pid={record['pid']} identity={args.identity}")
        return
    os.kill(record["pid"], signal.SIGTERM)
    for _ in range(100):
        if not owned(record, args.kind, args.identity):
            print(f"stopped {args.kind}")
            return
        time.sleep(0.1)
    if owned(record, args.kind, args.identity):
        os.kill(record["pid"], signal.SIGKILL)
    print(f"stopped {args.kind} after bounded shutdown")


if __name__ == "__main__":
    main()
