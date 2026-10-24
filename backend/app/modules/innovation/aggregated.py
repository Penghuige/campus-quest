"""The innovation module's single mount point for app/main.py.

Batch 1 carries the talent family only; later batches append their
routers to the same aggregate. main.py mounts this exactly once (the
boundary covenant's aggregate-entry rule): the talent routers keep
their own ``/ie/...`` prefixes and tags, so the URL surface is
byte-identical to mounting them individually. Deliberately NOT in
``__init__`` — see that module's docstring for the cycle.
"""

from fastapi import APIRouter

from app.modules.innovation import (
    public_talent_router,
    talent_file_router,
    talent_router,
)

router = APIRouter()
router.include_router(talent_router.router)
router.include_router(talent_file_router.router)
router.include_router(public_talent_router.router)
