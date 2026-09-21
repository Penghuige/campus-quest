# backend/app/modules/system/router.py
"""The admin system-settings API: thin routes over the audited
current-value store (PR #2 hardening step 8; spec §16.1 term
configuration, §28 URL shapes, §29 envelope).

Plan 08 builds the full system-settings surface; this router seeds it
with the one setting V1 needs — the academic term snapshotted onto new
reward redemptions.

Endpoints (``require_admin_actor`` — spec §33.4 management 2FA plus the
Admin-only narrowing the hardening ruling applied to the redemption
review family; an ACTIVE+TOTP Teacher is 403):

===========  =========================================================
Method path  Purpose
===========  =========================================================
GET          ``/admin/settings/current-academic-term`` — the term the
             platform is on RIGHT NOW: the system_settings row when
             present, the deployment seed (CURRENT_ACADEMIC_TERM env,
             dev default "2026-fall") when not. Resolved through the
             SAME provider the redemption gate uses, so the admin
             answer and the gate can never disagree (the window_open
             one-rule ruling).
PUT          ``/admin/settings/current-academic-term`` — set the term
             (body ``value``; non-blank, ≤64 — the
             ``AcademicTermProvider`` validation semantics, i.e. the
             ``RewardRedemption.term_key`` column width). The value and
             its ``SYSTEM_SETTING_UPDATED`` audit row commit together;
             already-created redemptions keep their snapshots (spec
             §16.1 — history keeps the term it was created under).
===========  =========================================================

Transport decisions:

- **Every write is audited; reads are not.** Listing a current
  configuration value is not a sensitive read (G12 names
  de-anonymization/PII/export surfaces); the CHANGE is the audited
  event, with the old value preserved in ``details.old_value``.
- **The per-key schema lives here** (the term's ≤64 bound in the
  request DTO): the storage service is generic, the value semantics
  are this surface's contract — the Plan 08 pattern of one request
  schema per known key.
- This router is a composition layer (the tasks-router precedent): it
  reads the setting through ``SystemSettingService`` and resolves the
  effective term through points' ``SystemAcademicTermProvider`` — the
  single definition of the row-over-seed priority (G7), shared with
  the redemption composition root.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db_session
from app.modules.identity.dependencies import require_admin_actor
from app.modules.identity.events import Actor
from app.modules.points.redemption_service import SystemAcademicTermProvider
from app.modules.system.service import CURRENT_ACADEMIC_TERM, SystemSettingService

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AdminActor = Annotated[Actor, Depends(require_admin_actor)]


# --- transport DTOs (explicit field sets) ---------------------------------------------


class CurrentAcademicTermResponse(BaseModel):
    """The term the platform is currently on (row value or deployment
    seed — resolved through the provider, never two rules)."""

    model_config = ConfigDict(extra="forbid")

    value: str


class CurrentAcademicTermRequest(BaseModel):
    """The new term key (spec §16.1): non-blank and within the
    ``RewardRedemption.term_key`` column width — the provider's own
    validation semantics, enforced at the transport so an unusable term
    never reaches storage. Blank-after-strip still reaches the service
    gate, which answers the same §29 code (422)."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=64)


# --- provider dependencies ------------------------------------------------------------


def get_system_setting_service() -> SystemSettingService:
    return SystemSettingService()


SystemServiceDep = Annotated[SystemSettingService, Depends(get_system_setting_service)]

router = APIRouter()


# --- the admin settings surface -------------------------------------------------------


@router.get(
    "/admin/settings/current-academic-term",
    response_model=CurrentAcademicTermResponse,
)
async def get_current_academic_term(
    actor: AdminActor,
    db: DbSession,
    settings_service: SystemServiceDep,
) -> CurrentAcademicTermResponse:
    """The effective academic term: the audited system_settings row when
    present, the deployment seed when not — resolved through the same
    provider the redemption gate reads, so this answer is exactly what
    the next redemption would snapshot."""
    configured = await settings_service.get(db, CURRENT_ACADEMIC_TERM)
    provider = SystemAcademicTermProvider(
        configured_term=configured, fallback=get_settings()
    )
    return CurrentAcademicTermResponse(value=provider.current_term_key())


@router.put(
    "/admin/settings/current-academic-term",
    response_model=CurrentAcademicTermResponse,
)
async def put_current_academic_term(
    body: CurrentAcademicTermRequest,
    actor: AdminActor,
    db: DbSession,
    settings_service: SystemServiceDep,
) -> CurrentAcademicTermResponse:
    """Turn the term: the value row and its audit row commit as one
    unit, and every redemption created afterwards snapshots the new
    term (spec §16.1 — existing redemptions keep theirs)."""
    stored = await settings_service.set(
        db, actor=actor, key=CURRENT_ACADEMIC_TERM, value=body.value
    )
    return CurrentAcademicTermResponse(value=stored)
