# Innovation recruitment spec-test matrix

Expectations recorded from approved R3 spec before locating implementation tests.

| # | Spec | Expected behavior | Test | Status |
| --- | --- | --- | --- | --- |
| R1 | §3 | Incomplete drafts save; publication requires bounded fields, explicit booleans, future aware deadline and valid selected contacts | `tests/unit/innovation/test_recruitment_schemas.py` + `tests/integration/innovation/test_recruitment_lifecycle.py::test_database_enforces_private_content_bounds` | covered |
| R2 | §3 | Publish requires active student, approved qualification AND owning project | `tests/integration/innovation/test_recruitment_lifecycle.py::test_publish_requires_qualification_and_owned_project` | covered |
| R3 | §4 | Draft save does not alter immutable public content | `tests/integration/innovation/test_recruitment_lifecycle.py::test_saved_changes_do_not_change_published_revision` + `tests/integration/innovation/test_recruitment_lifecycle.py::test_database_rejects_public_history_mutation` | covered |
| R4 | §5/7 | Old replay returns receipt and current state without changing new cycle | `tests/integration/innovation/test_recruitment_lifecycle.py::test_close_reopen_and_replay_cannot_change_new_cycle` + `tests/integration/innovation/test_recruitment_lifecycle.py::test_create_and_update_unknown_result_retry` | covered |
| R5 | §6 | Last revoke requires close; revoked draft needs fresh selection/publication | `tests/integration/innovation/test_recruitment_lifecycle.py::test_revoke_last_contact_requires_explicit_close` + `tests/integration/innovation/test_recruitment_lifecycle.py::test_revoked_contact_requires_fresh_authorization` | covered |
| R6 | §7 | Audit failure rolls back publication and contact grants | `tests/integration/innovation/test_recruitment_lifecycle.py::test_audit_failure_rolls_back_publication` | covered |
| R7 | §7 | Independent PG create/version races have one winner; suspension forbids publish | `tests/integration/innovation/test_recruitment_races.py` | covered |
| R8 | §5/8 | Public surfaces exclude expired, closed, down and invalid owner | pending | gap G-1 |
| R9 | §8 | Summary prefers approved snapshot; no private draft leakage | pending | gap G-1 |
| R10 | §6 | Deliberate contact read, 30/60 cap, Redis/audit failure safe | pending | gap G-1 |
| R11 | §5/7 | 30-day reminders stay OPEN, catch up once, edits preserve schedule, old-cycle fenced | pending | gap G-2 |
| R12 | §7 | Real Celery/Beat/IN_APP wiring; stale suppression, original events preserved | pending | gap G-2 |
| R13 | §8 | Save/publish UI separation; unknown request retained; auth clears private data | pending | gap G-3 |
| R14 | §8/9 | Mobile, keyboard, focus, axe, pinned-font pixels | pending | gap G-3 |

## 缺口明细

- G-1: Task 2 public projection/contact tests and implementation pending.
- G-2: Task 3 reminder/production composition pending.
- G-3: Task 4 UI/browser/accessibility/visual pending.
