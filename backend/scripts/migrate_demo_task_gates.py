"""Idempotent gate migration for the demo tasks (QA #12-B, owner ruling).

Run at deploy time against the target database (207 production) AFTER
seed_demo_content has ever run there. Two movements, both idempotent
by construction (match on the seeded title prefix AND the current
allowed set — a row already in the target state simply does not match):

1. Structured family: the three crawl-shaped demo tasks widen their
   upload gate from ["CSV"] to ["CSV", "XLSX"] — the UI offers the
   global universe, so the gate must cover what a compliant upload is.
2. Document family (the review-materials task): flips to the
   document-type gate ["DOCX", "PDF"] with an EMPTY submission_schema
   per spec §10.1 (two-family exclusivity). NOTE: this leg gated its
   update on the DOCX/PDF enum members existing (dispatch 2); those
   members have since shipped (app/modules/submissions/enums.py), so
   the guard below is now a satisfied-precondition record — on any
   current deployment this leg simply runs (or reports already
   applied).

Usage:
    uv run python scripts/migrate_demo_task_gates.py          # apply
    uv run python scripts/migrate_demo_task_gates.py --dry-run

Exit codes: 0 applied-or-already-applied; 2 no demo tasks found
(fresh deployment — run seed_demo_content first).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.session import get_async_session_maker
from app.modules.tasks.models import Task

MARKER = "【演示】"

# The document-family demo task is identified by its title fragment;
# every OTHER marker task is structured-family (the seed has shipped
# more than one catalogue revision — matching "everything else" keeps
# the migration robust against both the current and legacy shapes).
DOCUMENT_KEYWORD = "复习资料"

STRUCTURED_GATE = ["CSV", "XLSX"]
DOCUMENT_GATE = ["DOCX", "PDF"]


async def _run(dry_run: bool) -> int:
    factory = get_async_session_maker()
    structured_updated = 0
    document_updated = 0
    document_pending_enum = False
    async with factory() as db:
        tasks = (
            await db.execute(
                select(Task.id, Task.title, Task.allowed_file_types).where(
                    Task.title.like(f"{MARKER}%")
                )
            )
        ).all()
        if not tasks:
            print("no demo tasks found — run seed_demo_content first")
            return 2

        document_member_ready = True
        try:
            from app.modules.submissions.enums import FileType

            FileType("DOCX")
            FileType("PDF")
        except ValueError:
            document_member_ready = False

        for task_id, title, allowed in tasks:
            if DOCUMENT_KEYWORD in title:
                if not document_member_ready:
                    document_pending_enum = True
                    print(
                        f"[document] DEFERRED (DOCX/PDF enum not yet present): {title}"
                    )
                    continue
                if list(allowed) == ["CSV"] or "DOCX" not in allowed:
                    print(f"[document] converting: {title}")
                    document_updated += 1
                    if not dry_run:
                        await db.execute(
                            update(Task)
                            .where(Task.id == task_id)
                            .values(
                                allowed_file_types=DOCUMENT_GATE,
                                submission_schema=None,
                                submission_schema_version=None,
                            )
                        )
                else:
                    print(f"[document] already applied: {title} -> {allowed}")
            else:
                if list(allowed) == ["CSV"]:
                    print(f"[structured] widening: {title}")
                    structured_updated += 1
                    if not dry_run:
                        await db.execute(
                            update(Task)
                            .where(Task.id == task_id)
                            .values(allowed_file_types=STRUCTURED_GATE)
                        )
                else:
                    print(f"[structured] already applied: {title} -> {allowed}")
        if not dry_run:
            await db.commit()
    print(
        f"done: structured widened={structured_updated}, "
        f"document converted={document_updated}"
        + (
            f", document DEFERRED={document_pending_enum} (rerun after dispatch 2)"
            if document_pending_enum
            else ""
        )
        + (" [DRY RUN]" if dry_run else "")
    )
    return 0


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    get_settings()  # fail fast on missing env
    return asyncio.run(_run(dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
