"""Shared transport dependencies for the innovation module's routers.

Equivalence note (batch 1): these four symbols were inlined from the
upstream branch's ``innovation/router.py`` and
``innovation/public_achievement_router.py`` so the talent routers can
land without the project-draft/achievement chains. The underlying
identity dependencies are main's; only the aliases live here.
"""

from typing import Annotated

from fastapi import Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.modules.identity.dependencies import (
    require_active_actor,
    require_approved_student_actor,
)
from app.modules.identity.events import Actor


def _private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


Database = Annotated[AsyncSession, Depends(get_db_session)]
Student = Annotated[Actor, Depends(require_approved_student_actor)]
Viewer = Annotated[Actor, Depends(require_active_actor)]
