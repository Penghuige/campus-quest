"""The innovation module's single mount point for app/main.py.

Batch 1 carries the talent family; batch 2 appends the project library
(drafts, owner profile, qualification, operations grants). Later batches
extend the same aggregate. main.py mounts this exactly once (the
boundary covenant's aggregate-entry rule): every sub-router keeps its
own ``/ie/...`` or ``/admin/ie/...`` prefixes and tags, so the URL
surface is byte-identical to mounting them individually. Deliberately
NOT in ``__init__`` — see that module's docstring for the cycle.
"""

from fastapi import APIRouter

from app.modules.innovation import (
    operations_router,
    owner_router,
    public_talent_router,
    qualification_router,
    talent_file_router,
    talent_router,
)
from app.modules.innovation import (
    router as project_drafts_router,
)

router = APIRouter()
router.include_router(talent_router.router)
router.include_router(talent_file_router.router)
router.include_router(public_talent_router.router)
router.include_router(project_drafts_router.router)
router.include_router(owner_router.router)
router.include_router(qualification_router.self_router)
router.include_router(qualification_router.admin_router)
router.include_router(operations_router.admin_router)
router.include_router(operations_router.student_router)
