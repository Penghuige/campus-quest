# backend/tests/unit/tasks/test_claim_lifecycle_property.py
"""Centralized claim-lifecycle pin + property suite (closes gap G-3).

Matrix claims-lifecycle.md row C3: the spec (§8.1) demands that claim
status transitions be defined by the service layer in one place. The
task side pins its transition table against spec §6.2
(test_task_lifecycle.py::test_transition_table_matches_spec_exactly);
the claim side had no equivalent. Unlike tasks, the claim relation is
not one table: it is the sum of the owning service methods (claim,
validation, reward lock, review, abandon — each transition site in its
domain service). This suite pins that scattered relation centrally:

- SPEC_CLAIM_EDGES below is transcribed ONLY from spec text (§8.1
  states, §11.1 flow, §11.4 revision expiry, §8.5 abandon, §14 approve
  transaction, §26 timeout worker) — it was written before reading the
  production transition sites.
- the property block proves the oracle is a well-formed lifecycle;
- the parity block proves production still implements exactly that
  relation's targets, that transition sites exist only in the five
  owning services, and that each TERMINAL transition has a single
  owner (terminals are irreversible — §8.1).
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.modules.tasks.enums import ClaimStatus

# --- The spec-derived oracle (transcription, not implementation copy) ---

SPEC_CLAIM_EDGES: dict[tuple[str, str], str] = {
    # §11: a submission moves the claim out of CLAIMED into machine validation
    ("CLAIMED", "submits_first_or_next_version"): "VALIDATING",
    # §11.1: VALIDATED -> UNDER_REVIEW (first machine-valid submission)
    ("VALIDATING", "machine_validation_passes"): "UNDER_REVIEW",
    # §11.1 human stage: teacher returns the work for quality fixes
    ("UNDER_REVIEW", "review_requests_changes"): "REVISION_REQUIRED",
    # §11.1 + §14 step 6: APPROVE completes the claim in one transaction
    ("UNDER_REVIEW", "review_approves"): "COMPLETED",
    # §11.4: resubmitting inside the revision window re-enters validation
    ("REVISION_REQUIRED", "resubmits_within_window"): "VALIDATING",
    # §8.5: student-initiated abandon
    ("CLAIMED", "abandons"): "ABANDONED",
    # §26: timeout worker expires never-submitted actionable claims
    ("CLAIMED", "deadline_passes_without_valid_submission"): "EXPIRED",
    # §11.4: revision window lapses without a resubmission
    ("REVISION_REQUIRED", "revision_deadline_passes"): "EXPIRED",
}

SPEC_TERMINAL_STATES = frozenset(
    {ClaimStatus.COMPLETED, ClaimStatus.ABANDONED, ClaimStatus.EXPIRED}
)

ALL_SPEC_STATES = frozenset(ClaimStatus)

# The five services that own claim-status transitions (§8.1 "service
# layer"); a new transition site anywhere else must update this pin and
# the spec story that justifies it.
TRANSITION_OWNERS = {
    "modules/tasks/claim_service.py",  # creation CLAIMED + timeout EXPIRED
    "modules/submissions/validation_service.py",  # submit -> VALIDATING
    "modules/submissions/reward_lock_service.py",  # machine pass -> UNDER_REVIEW
    "modules/submissions/review_service.py",  # REVISION_REQUIRED / COMPLETED
    "modules/tasks/abandon_service.py",  # ABANDONED
}

# Irreversible ends: exactly one production site each (§8.1 terminals).
TERMINAL_OWNERS = {
    ClaimStatus.COMPLETED: "modules/submissions/review_service.py",
    ClaimStatus.ABANDONED: "modules/tasks/abandon_service.py",
    ClaimStatus.EXPIRED: "modules/tasks/claim_service.py",
}

# --- Property block: the oracle is a well-formed lifecycle ---


def _reachable() -> set[str]:
    seen = {"CLAIMED"}
    frontier = deque(seen)
    while frontier:
        state = frontier.popleft()
        for (source, _action), target in SPEC_CLAIM_EDGES.items():
            if source == state and target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


def test_every_non_entry_state_is_reachable_from_claimed() -> None:
    assert _reachable() == {state.value for state in ALL_SPEC_STATES}


def test_terminal_states_have_no_outgoing_edges() -> None:
    terminals = {state.value for state in SPEC_TERMINAL_STATES}
    outgoing = [source for (source, _a) in SPEC_CLAIM_EDGES if source in terminals]
    assert outgoing == []


def test_oracle_covers_exactly_the_spec_states() -> None:
    touched = {s for (s, _a) in SPEC_CLAIM_EDGES} | set(SPEC_CLAIM_EDGES.values())
    assert touched == {state.value for state in ALL_SPEC_STATES}


@given(
    state=st.sampled_from([s.value for s in ALL_SPEC_STATES]),
    action=st.sampled_from(
        [
            "submits_first_or_next_version",
            "machine_validation_passes",
            "review_requests_changes",
            "review_approves",
            "resubmits_within_window",
            "abandons",
            "deadline_passes_without_valid_submission",
            "revision_deadline_passes",
        ]
    ),
)
@settings(max_examples=200, deadline=None)
def test_oracle_is_deterministic_and_terminals_are_absorbing(
    state: str, action: str
) -> None:
    """Any (state, action) resolves to at most one target, and a terminal
    state never resolves — the relation is a function partial on
    non-terminal states."""
    targets = {
        target
        for (source, act), target in SPEC_CLAIM_EDGES.items()
        if source == state and act == action
    }
    assert len(targets) <= 1
    if state in {s.value for s in SPEC_TERMINAL_STATES}:
        assert targets == set()


# --- Parity block: production implements exactly that relation ---


_ASSIGNMENT_RE = re.compile(r"\.status\s*=\s*ClaimStatus\.(\w+)")
_CREATION_KWARG_RE = re.compile(r"[^.\w]status\s*=\s*ClaimStatus\.(\w+)")
_APP_ROOT = Path(__file__).resolve().parents[3] / "app"


def _production_transition_sites() -> dict[str, list[tuple[str, int]]]:
    """Scan app/ for claim-status assignments: target -> [(relpath, line)].

    Attribute assignments (``claim.status = ClaimStatus.X``) are state
    transitions; the bare ``status=ClaimStatus.X`` kwarg form is claim
    CREATION (only CLAIMED may appear that way).
    """
    transitions: dict[str, list[tuple[str, int]]] = {}
    creations: dict[str, list[tuple[str, int]]] = {}
    for path in sorted((_APP_ROOT / "modules").rglob("*.py")):
        rel = str(path.relative_to(_APP_ROOT))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in _ASSIGNMENT_RE.finditer(line):
                transitions.setdefault(match.group(1), []).append((rel, lineno))
            stripped = _ASSIGNMENT_RE.sub("", line)
            for match in _CREATION_KWARG_RE.finditer(stripped):
                creations.setdefault(match.group(1), []).append((rel, lineno))
    transitions["__creations__"] = [
        site for sites in creations.values() for site in sites
    ]
    return transitions


def test_production_targets_are_exactly_the_spec_states() -> None:
    sites = _production_transition_sites()
    declared = {state.value for state in ALL_SPEC_STATES}
    targets = {t for t in sites if t != "__creations__"}
    assert targets == declared - {"CLAIMED"}


def test_claim_creation_sites_only_mint_claimed() -> None:
    sites = _production_transition_sites()
    creation_files = {rel for rel, _line in sites["__creations__"]}
    assert creation_files == {"modules/tasks/claim_service.py"}


def test_transition_sites_live_only_in_the_owning_services() -> None:
    sites = _production_transition_sites()
    owners = {
        rel
        for target, found in sites.items()
        if target != "__creations__"
        for rel, _line in found
    }
    assert owners == TRANSITION_OWNERS


@pytest.mark.parametrize("terminal,owner", sorted(TERMINAL_OWNERS.items()))
def test_each_terminal_transition_has_a_single_owner(
    terminal: ClaimStatus, owner: str
) -> None:
    sites = _production_transition_sites()
    found = sites.get(terminal.value, [])
    assert [(rel, line) for rel, line in found if rel == owner], (
        f"{terminal.value} must transition inside {owner}"
    )
    assert all(rel == owner for rel, _line in found), (
        f"{terminal.value} transitions leaked outside {owner}: {found}"
    )
