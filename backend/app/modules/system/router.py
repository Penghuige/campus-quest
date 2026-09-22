# backend/app/modules/system/router.py
"""The admin system-settings API: thin routes over the audited
current-value store (PR #2 hardening step 8; spec §16.1 term
configuration, §28 URL shapes, §29 envelope; Plan 08 T5/T9 — the
registry's full five-key surface).

Endpoints (``require_admin_actor`` — spec §33.4 management 2FA plus the
Admin-only narrowing the hardening ruling applied to the redemption
review family; an ACTIVE+TOTP Teacher is 403 — and the router-wide
store-backed management-network guard, Plan 08 T9's W4 footgun closure):

===========  =========================================================
Method path  Purpose
===========  =========================================================
GET          ``/admin/settings`` — every REGISTERED key's current
             state: the canonical stored value, the per-key change
             counter (``version``, 0020), and ``updated_at`` — ``null``
             fields mean "no row; the deployment seed/env fallback
             decides" (G7). Unregistered keys cannot appear.
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
PUT          ``/admin/settings/emoji-whitelist`` / ``.../abandon-
             daily-limit`` / ``.../management-network-enabled`` /
             ``.../management-network-cidrs`` — the registry's other
             four keys, one typed per-key request schema each (the
             "one request schema per known key" pattern this router
             seeded): ``list[str]`` emoji entries (each 1-8 code
             points, empty list legal), ``int >= 0``, ``bool``, and
             ``list[str]`` CIDRs. Every write normalizes through the
             key's registry entry and commits value + audit row as one
             unit; the response carries the stored canonical value and
             the row's new ``version``.
===========  =========================================================

Transport decisions:

- **Every write is audited; reads are not.** Listing a current
  configuration value is not a sensitive read (G12 names
  de-anonymization/PII/export surfaces); the CHANGE is the audited
  event, with the previous value preserved on the §30 snapshot pair
  (0016) and the write's ``version`` in ``details`` (0020).
- **Every PUT body takes the OPTIONAL ``reason``** (T10's
  audit-completeness gap-fill): the free-text why rides the audit
  row's ``reason`` column — ``None``/absent is legal, but a provided
  reason blank after trim is the typed §29 422 at the service gate
  (the reject-reason discipline), so a whitespace-only reason is
  refused rather than silently dropped from the audit trail.
- **The per-key schema lives here** (the term's ≤64 bound in the
  request DTO): the storage service is generic, the value semantics
  are this surface's contract — the Plan 08 pattern of one request
  schema per known key, now covering the full registry.
- **The management-network cross-key ruling (Plan 08 T9): a write
  that would leave the policy UNLOADABLE is refused at 422, never
  stored to blow up at request time.** Enabling
  ``MANAGEMENT_NETWORK_ENABLED`` with an empty effective CIDR list
  (store row or env fallback), or emptying ``MANAGEMENT_NETWORK_CIDRS``
  while the policy is enabled, answers the typed 422 BEFORE any write:
  the check resolves exactly the post-write pair the per-request
  loader would resolve (store-first, env-fallback per key), so a
  stored combination the loader would reject is unwritable — no
  runtime 500 from this pair, either direction.
- This router is a composition layer (the tasks-router precedent): it
  reads the setting through ``SystemSettingService`` and resolves the
  effective term through points' ``SystemAcademicTermProvider`` — the
  single definition of the row-over-seed priority (G7), shared with
  the redemption composition root.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_network_policy import load_management_network_policy
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.db.session import get_db_session
from app.modules.audit.context import AuditContext
from app.modules.identity.admin_router import require_management_network_from_store
from app.modules.identity.dependencies import require_admin_actor
from app.modules.identity.events import Actor
from app.modules.points.redemption_service import SystemAcademicTermProvider
from app.modules.system.models import SystemSetting
from app.modules.system.service import (
    ABANDON_DAILY_LIMIT,
    CURRENT_ACADEMIC_TERM,
    EMOJI_WHITELIST,
    MANAGEMENT_NETWORK_CIDRS,
    MANAGEMENT_NETWORK_ENABLED,
    SYSTEM_SETTING_REGISTRY,
    SystemSettingService,
    normalize_system_setting_value,
)

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AdminActor = Annotated[Actor, Depends(require_admin_actor)]

# Router-wide: the store-backed management-network guard beside every
# route's AdminActor (the admin family posture, Plan 08 T9).
router = APIRouter(dependencies=[Depends(require_management_network_from_store)])


# --- transport DTOs (explicit field sets) ---------------------------------


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
    reason: str | None = None


class EmojiWhitelistSettingRequest(BaseModel):
    """The emoji allowlist (spec §22): each entry 1-8 code points; the
    EMPTY list is legal at the key level (= all emoji banned) — the
    registry's own contract decides, this DTO only types the payload."""

    model_config = ConfigDict(extra="forbid")

    value: list[str]
    reason: str | None = None


class AbandonDailyLimitSettingRequest(BaseModel):
    """The per-natural-day abandon cap (spec §12.4): 0 disables
    abandoning; the registry mirrors the bound."""

    model_config = ConfigDict(extra="forbid")

    value: int = Field(ge=0)
    reason: str | None = None


class ManagementNetworkEnabledSettingRequest(BaseModel):
    """Whether the management-network restriction is on (spec §33.4
    adjacency). Setting ``true`` with an empty effective CIDR list is
    the cross-key typed 422 (see the module docstring)."""

    model_config = ConfigDict(extra="forbid")

    value: bool
    reason: str | None = None


class ManagementNetworkCidrsSettingRequest(BaseModel):
    """The management-network CIDR allowlist: strict standard-library
    parsing (host bits set are a configuration error); the EMPTY list
    is legal at the key level (= restriction allows nothing, so it may
    only be written while the policy is disabled — the cross-key 422
    otherwise)."""

    model_config = ConfigDict(extra="forbid")

    value: list[str]
    reason: str | None = None


class SystemSettingValueResponse(BaseModel):
    """One applied write: the stored canonical value and the row's new
    per-key change counter (0020)."""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: str
    version: int


class SystemSettingItemResponse(BaseModel):
    """One registered key's current state; ``null`` value/version means
    "no row — the deployment seed or the key's env fallback decides"
    (G7: the store is the fact, the seed bootstraps)."""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: str | None
    version: int | None
    updated_at: datetime | None


class SystemSettingsListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SystemSettingItemResponse]


# --- provider dependencies ------------------------------------------------


def get_system_setting_service() -> SystemSettingService:
    return SystemSettingService()


SystemServiceDep = Annotated[SystemSettingService, Depends(get_system_setting_service)]


# --- the write-path helper (cross-key ruling) -----------------------------


async def _assert_policy_stays_loadable(
    db: AsyncSession,
    service: SystemSettingService,
    *,
    key: str,
    canonical_value: str,
) -> None:
    """The management-network cross-key gate (Plan 08 T9): resolve the
    POST-WRITE pair exactly the per-request loader would and refuse the
    write when the policy would be enabled with zero networks.

    ``canonical_value`` is the write's own canonical stored form (from
    ``normalize_system_setting_value``); the OTHER key keeps its current
    row (or falls back to the env field when no row exists — the W4
    migration's per-key semantics), so what is checked here is
    precisely what ``require_management_network_from_store`` would
    resolve after the write commits. Non-network keys skip the check.
    """
    if key == MANAGEMENT_NETWORK_ENABLED:
        stored_enabled: str | None = canonical_value
        stored_cidrs: str | None = await service.get(db, MANAGEMENT_NETWORK_CIDRS)
    elif key == MANAGEMENT_NETWORK_CIDRS:
        stored_enabled = await service.get(db, MANAGEMENT_NETWORK_ENABLED)
        stored_cidrs = canonical_value
    else:
        return
    try:
        load_management_network_policy(
            stored_enabled=stored_enabled,
            stored_cidrs=stored_cidrs,
        )
    except ValueError as exc:
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            "网络策略配置不合法：启用管理网络限制时必须至少配置一个 CIDR",
            status_code=422,
            details={"key": key, "reason": str(exc)},
        ) from exc


async def _set_setting(
    db: AsyncSession,
    *,
    actor: Actor,
    service: SystemSettingService,
    key: str,
    value: object,
    request: Request,
    reason: str | None = None,
) -> SystemSettingValueResponse:
    """The shared write path: normalize (typed 422 for value-shape
    failures), run the cross-key gate, commit value + audit row (with
    the optional ``reason`` on the audit row) as one unit, and answer
    the stored canonical value with the new version."""
    _stored_key, canonical = normalize_system_setting_value(key, value)
    await _assert_policy_stays_loadable(
        db, service, key=_stored_key, canonical_value=canonical
    )
    stored = await service.set(
        db,
        actor=actor,
        key=_stored_key,
        value=value,
        reason=reason,
        audit_context=AuditContext.from_request(request),
    )
    row = await db.scalar(select(SystemSetting).where(SystemSetting.key == _stored_key))
    # The committed row cannot vanish (settings have no delete path).
    assert row is not None
    return SystemSettingValueResponse(
        key=_stored_key, value=stored, version=row.version
    )


# --- the admin settings surface -------------------------------------------


@router.get("/admin/settings", response_model=SystemSettingsListResponse)
async def list_system_settings(
    actor: AdminActor,
    db: DbSession,
) -> SystemSettingsListResponse:
    """Every REGISTERED key's current state, registry order, ``null``
    fields marking "no row" (the reader decides the fallback — the
    provider pattern, G7). Unregistered keys are unrepresentable."""
    rows = {
        row.key: row
        for row in await db.scalars(
            select(SystemSetting).where(SystemSetting.key.in_(SYSTEM_SETTING_REGISTRY))
        )
    }
    return SystemSettingsListResponse(
        items=[
            SystemSettingItemResponse(
                key=key,
                value=rows[key].value if key in rows else None,
                version=rows[key].version if key in rows else None,
                updated_at=rows[key].updated_at if key in rows else None,
            )
            for key in SYSTEM_SETTING_REGISTRY
        ]
    )


@router.get(
    "/admin/settings/current-academic-term", response_model=CurrentAcademicTermResponse
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
    "/admin/settings/current-academic-term", response_model=CurrentAcademicTermResponse
)
async def put_current_academic_term(
    body: CurrentAcademicTermRequest,
    actor: AdminActor,
    db: DbSession,
    settings_service: SystemServiceDep,
    request: Request,
) -> CurrentAcademicTermResponse:
    """Turn the term: the value row and its audit row commit as one
    unit, and every redemption created afterwards snapshots the new
    term (spec §16.1 — existing redemptions keep theirs). The optional
    ``reason`` rides the audit row (blank-after-trim is the typed 422
    at the service gate)."""
    stored = await settings_service.set(
        db,
        actor=actor,
        key=CURRENT_ACADEMIC_TERM,
        value=body.value,
        reason=body.reason,
        audit_context=AuditContext.from_request(request),
    )
    return CurrentAcademicTermResponse(value=stored)


@router.put(
    "/admin/settings/emoji-whitelist", response_model=SystemSettingValueResponse
)
async def put_emoji_whitelist(
    body: EmojiWhitelistSettingRequest,
    actor: AdminActor,
    db: DbSession,
    settings_service: SystemServiceDep,
    request: Request,
) -> SystemSettingValueResponse:
    """Set the emoji allowlist (each entry 1-8 code points; the empty
    list bans all emoji — spec §22). Stored as a JSON array."""
    return await _set_setting(
        db,
        actor=actor,
        service=settings_service,
        key=EMOJI_WHITELIST,
        value=body.value,
        request=request,
        reason=body.reason,
    )


@router.put(
    "/admin/settings/abandon-daily-limit", response_model=SystemSettingValueResponse
)
async def put_abandon_daily_limit(
    body: AbandonDailyLimitSettingRequest,
    actor: AdminActor,
    db: DbSession,
    settings_service: SystemServiceDep,
    request: Request,
) -> SystemSettingValueResponse:
    """Set the per-natural-day abandon cap (0 = abandoning disabled;
    spec §12.4). Stored as decimal text."""
    return await _set_setting(
        db,
        actor=actor,
        service=settings_service,
        key=ABANDON_DAILY_LIMIT,
        value=body.value,
        request=request,
        reason=body.reason,
    )


@router.put(
    "/admin/settings/management-network-enabled",
    response_model=SystemSettingValueResponse,
)
async def put_management_network_enabled(
    body: ManagementNetworkEnabledSettingRequest,
    actor: AdminActor,
    db: DbSession,
    settings_service: SystemServiceDep,
    request: Request,
) -> SystemSettingValueResponse:
    """Turn the management-network restriction on/off. Enabling it with
    an empty effective CIDR list is the typed 422 (the cross-key
    ruling — see the module docstring), so the per-request guard can
    never meet an unloadable policy."""
    return await _set_setting(
        db,
        actor=actor,
        service=settings_service,
        key=MANAGEMENT_NETWORK_ENABLED,
        value=body.value,
        request=request,
        reason=body.reason,
    )


@router.put(
    "/admin/settings/management-network-cidrs",
    response_model=SystemSettingValueResponse,
)
async def put_management_network_cidrs(
    body: ManagementNetworkCidrsSettingRequest,
    actor: AdminActor,
    db: DbSession,
    settings_service: SystemServiceDep,
    request: Request,
) -> SystemSettingValueResponse:
    """Set the management-network CIDR allowlist (strict parsing; stored
    comma-separated canonical). Emptying it while the policy is enabled
    is the typed 422 (the cross-key ruling's other direction)."""
    return await _set_setting(
        db,
        actor=actor,
        service=settings_service,
        key=MANAGEMENT_NETWORK_CIDRS,
        value=body.value,
        request=request,
        reason=body.reason,
    )
