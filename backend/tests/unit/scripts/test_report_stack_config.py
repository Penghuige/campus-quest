# backend/tests/unit/scripts/test_report_stack_config.py
"""Freeze-pins for the report viewer's stack form (campus reviewer ruling
2026-10-26): the service rides the "report" profile so bare
`docker compose up -d` (gate prerequisite, other sessions, production —
which reuses this compose file) never starts it, while `make stack`
(= --profile report up -d) couples its lifecycle to the stack.

Text-level pins in the repo's freeze-test style: the compose block is
small and ours; a drift here (dropped profile, published port widened
beyond loopback, mount losing :ro) must turn a test red before it ships.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_COMPOSE = _REPO_ROOT / "infra" / "docker-compose.yml"
_MAKEFILE = _REPO_ROOT / "Makefile"


def _report_viewer_block() -> str:
    text = _COMPOSE.read_text(encoding="utf-8")
    match = re.search(r"\n  report-viewer:\n(.*?)(?=\n  \w|\Z)", text, re.DOTALL)
    assert match is not None, "report-viewer service missing from compose file"
    return match.group(0)


def test_viewer_rides_the_report_profile() -> None:
    assert 'profiles: ["report"]' in _report_viewer_block()


def test_viewer_binds_loopback_only() -> None:
    assert "127.0.0.1:8200:8200" in _report_viewer_block()


def test_viewer_mounts_reports_read_only() -> None:
    assert "../reports:/srv/reports:ro" in _report_viewer_block()


def test_make_stack_enables_the_report_profile() -> None:
    text = _MAKEFILE.read_text(encoding="utf-8")
    stack = re.search(r"^stack:\n(.*?)(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert stack is not None, "make stack target missing"
    assert "--profile report up -d" in stack.group(1)


def test_stack_down_is_a_full_compose_down() -> None:
    text = _MAKEFILE.read_text(encoding="utf-8")
    down = re.search(r"^stack-down:\n(.*?)(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert down is not None, "make stack-down target missing"
    assert "docker compose -f infra/docker-compose.yml down" in down.group(1)
