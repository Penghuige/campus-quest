# backend/app/modules/system/service.py
"""The system-settings service: audited, TYPED writes and plain reads of
the platform's current-value configuration (PR #2 hardening step 8;
Plan 08 T5's versioned + typed-key surface).

The service is GENERIC over keys at the STORAGE layer, but NOT an
arbitrary KV dump: a module-level REGISTRY (``SYSTEM_SETTING_REGISTRY``)
maps every key the platform knows to the validator/normalizer that owns
its value semantics. An unregistered key is the typed §29
``VALIDATION_ERROR`` (422) — "settings" must not become a garbage sink
of ad-hoc rows nobody reads. Registering a key is the deliberate act of
giving it a contract; the registry entries (this wave):

=========================  =======================================
Key                        Value contract (stored canonical form)
=========================  =======================================
``CURRENT_ACADEMIC_TERM``  str, stripped non-blank, ≤64 (the
                           ``RewardRedemption.term_key`` width).
``EMOJI_WHITELIST``        list[str], each 1-8 code points; the empty
                           list is legal (= all emoji banned). Stored as
                           a JSON array.
``ABANDON_DAILY_LIMIT``    int ≥ 0 (0 = abandoning disabled). Stored as
                           decimal text.
``MANAGEMENT_NETWORK_ENABLED``  bool. Stored as ``"true"``/``"false"``.
``MANAGEMENT_NETWORK_CIDRS``    list[str], each a valid CIDR per the
                           standard library (strict — host bits set are
                           a configuration error); the empty list is
                           legal. Stored comma-separated in
                           ``ipaddress``'s canonical spelling.
=========================  =======================================

Validators take the CALLER's typed value (str / bool / int / list[str])
and return the canonical stored string; the storage, the audit
snapshots, and every reader therefore see exactly one spelling per key.

What else the service owns:

- **``set`` writes the value and its audit row in ONE transaction**
  (backend-engineering §5): the write path resolves first-write races
  INSIDE the database (``INSERT ... ON CONFLICT DO NOTHING RETURNING``;
  the loser then locks the winner's row ``FOR UPDATE`` — see ``set``),
  the flush-only ``AuditLogWriter.append`` joins the caller's
  transaction, and the service commits exactly once — value and trace
  commit or roll back together, the RedemptionService discipline. The
  optional free-text ``reason`` (T10's audit-completeness gap-fill)
  rides the audit row's ``reason`` column under the reject-reason
  discipline: ``None`` is legal (no reason given), but a PROVIDED
  reason must be non-blank after trim — whitespace-only is the typed
  §29 422 BEFORE any write, never a silently-dropped audit field. The
  audit row carries the value MIGRATION on the §30 snapshot pair
  (0016): the NEW value in ``after_snapshot.value`` and the PREVIOUS
  value in ``before_snapshot.value`` (``None`` on the first write), so
  the audit trail answers "what was it before?" without a time
  machine — and the chain stays CONNECTED under a lost first-write
  race (round-5 P1): the loser reads the winner's committed value
  under the row lock, never a pre-read stale ``None``.
- **Every write bumps the per-key ``version``** (0020): 1 on the first
  write, +1 on every subsequent set, recorded in the audit row's
  ``details.version``. An optimistic observation of the serialized
  write path — NOT a CAS contract (0020's docstring); last-writer-wins
  is unchanged.
- **The ``MANAGEMENT_NETWORK_*`` pair has ONE write path —
  ``set_management_network_policy`` (PR #5 final-review fix A, P0).**
  The two keys are one policy, but the admin surface writes them one
  key at a time, and the cross-key invariant (enabled=true requires a
  non-empty effective CIDR list) reads the OTHER key — so the check is
  only sound under mutual exclusion. The method takes a fixed
  transaction-scoped ``pg_advisory_xact_lock`` before reading the
  partner key's effective value (store row or env fallback), refuses
  any post-write pair the per-request loader could not load, and
  commits value + audit row + lock release as one unit. The plain
  ``set`` REFUSES the two policy keys: an unlocked second write path
  is exactly the cross-key race the advisory lock exists to close
  (two concurrent single-key writes used to validate against stale
  partner values and commit an unloadable pair, wedging every guarded
  admin surface on the loader's ValueError).
- **``MANAGEMENT_NETWORK_*`` values are non-sensitive by design and
  ride the snapshots in full** (Plan 08 T5): a CIDR allowlist is
  infrastructure fact, not PII — the full list lands in
  ``after_snapshot.value``, never a truncated summary.
- **``get`` is a read of the current value only**: ``None`` means "no
  row" — the caller decides the fallback (the academic-term provider
  falls back to the deployment seed; G7). Read-only, commits nothing.

Module boundaries: like ``audit``, this module imports nothing from the
domain modules — the arrow points IN (points' composition reads the
setting; the admin router writes it). The management-network policy
RESOLVER (``app.core.admin_network_policy``) still knows the stored
value FORMS but not this module (core never imports app.modules), and
the aggregate policy write path composes it the same way every reader
does: rows read here, raw strings handed to the resolver, store-first
with per-key env fallback.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_network_policy import load_management_network_policy
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.system.models import SystemSetting

__all__ = [
    "ABANDON_DAILY_LIMIT",
    "CURRENT_ACADEMIC_TERM",
    "EMOJI_WHITELIST",
    "MANAGEMENT_NETWORK_CIDRS",
    "MANAGEMENT_NETWORK_ENABLED",
    "MANAGEMENT_NETWORK_POLICY_LOCK",
    "SYSTEM_SETTING_REGISTRY",
    "SYSTEM_SETTING_UPDATED",
    "ManagementNetworkPolicyWriteResult",
    "SystemSettingService",
    "SystemSettingValueError",
    "normalize_system_setting_value",
]

# Audit action name (the STAFF_INVITATION_CREATED family; G12 durable
# audit): one row per applied setting write, inside the write
# transaction. Plan 08 T5 extends the row's shape with
# ``details.version`` (0020).
SYSTEM_SETTING_UPDATED = "SYSTEM_SETTING_UPDATED"

# The audit target vocabulary for setting writes: the setting's key IS
# the target id (a business key, not a UUID — the audit_logs polymorphic
# target design).
_AUDIT_TARGET_TYPE = "system_setting"

# The setting keys the platform knows. CURRENT_ACADEMIC_TERM is the term
# key snapshotted onto new reward redemptions (spec §16.1): read per
# request by points' SystemAcademicTermProvider, written through the
# admin settings API. Both import the name from this module so the
# storage address is spelled once. The EMOJI_WHITELIST /
# ABANDON_DAILY_LIMIT / MANAGEMENT_NETWORK_* keys are Plan 08 T5's
# runtime-administrable values (spec §22, §12.4, §33.4 adjacency);
# consumers are wired through their existing ports
# (community.EmojiWhitelistPort, AbandonService's injectable limit, and
# admin_network_policy's resolver) — the registry is the write-side
# contract those reads resolve against.
CURRENT_ACADEMIC_TERM = "CURRENT_ACADEMIC_TERM"
EMOJI_WHITELIST = "EMOJI_WHITELIST"
ABANDON_DAILY_LIMIT = "ABANDON_DAILY_LIMIT"
MANAGEMENT_NETWORK_ENABLED = "MANAGEMENT_NETWORK_ENABLED"
MANAGEMENT_NETWORK_CIDRS = "MANAGEMENT_NETWORK_CIDRS"

# CURRENT_ACADEMIC_TERM's bound is the RewardRedemption.term_key column
# width (the transport schema's contract, enforced here so the storage
# layer can never hold a value the snapshot column cannot).
_TERM_MAX_LENGTH = 64

# EMOJI_WHITELIST: each entry is 1-8 code points (a single grapheme
# cluster's worth — an emoji plus modifiers/variation selectors — but
# never a multi-emoji sequence, which no whitelist configuration should
# admit; spec §22).
_EMOJI_MIN_CODE_POINTS = 1
_EMOJI_MAX_CODE_POINTS = 8

# Details previews are bounded so a hostile over-long value cannot bloat
# the typed 422 envelope (backend-engineering §14: admin-supplied input
# is untrusted input).
_DETAILS_VALUE_PREVIEW_MAX = 120

# The blank-reason refusal message (the reject-reason precedent's shape:
# a provided reason must survive its trim).
_REASON_BLANK_MESSAGE = "系统设置修改原因不能为空白"

# The MANAGEMENT_NETWORK_* pair's SINGLE SERIAL DOMAIN (PR #5 final-review
# fix A, P0): both keys describe one policy, but the routes write them one
# key at a time, so the cross-key invariant (enabled=true requires a
# non-empty effective CIDR list) can only be checked against the OTHER
# key's current value under mutual exclusion. Every policy write takes
# this TRANSACTION-scoped advisory lock before reading the partner key,
# so two concurrent single-key writes serialize: the second validates
# against the first's COMMITTED value, and an unloadable pair (for
# example enabled=true with cidrs=[]) is unwritable, not merely
# unlikely. A single lock has no ordering to get wrong (no deadlock),
# and pg_advisory_xact_lock releases automatically at commit/rollback.
#
# The literal is hashtext('management_network_policy') = 120969870,
# evaluated on the project's PostgreSQL 16 — ANY fixed bigint keeps the
# writers agreed (the number is a rendezvous key, not a hash contract),
# but the provenance is recorded so a future second advisory lock in
# this module picks a different, equally documented literal.
MANAGEMENT_NETWORK_POLICY_LOCK: Final[int] = 120969870

#: The two keys the aggregate policy write path owns; ``set`` refuses
#: them so no caller can bypass the lock and the cross-key gate.
_POLICY_KEYS: Final[frozenset[str]] = frozenset(
    {MANAGEMENT_NETWORK_ENABLED, MANAGEMENT_NETWORK_CIDRS}
)

# The typed refusals of the aggregate policy path.
_POLICY_KEYS_REQUIRED_MESSAGE = "必须提供要修改的策略键（enabled 或 cidrs 之一）"
_POLICY_KEY_MESSAGE = "网络策略键必须通过 set_management_network_policy 整体校验写入"
_POLICY_UNLOADABLE_MESSAGE = (
    "网络策略配置不合法：启用管理网络限制时必须至少配置一个 CIDR"
)


@dataclass(frozen=True, slots=True)
class ManagementNetworkPolicyWriteResult:
    """One applied policy-key write: the changed key, its stored
    canonical value, and the row's new per-key change counter (0020) —
    the settings PUT response's exact facts."""

    key: str
    value: str
    version: int


def _normalized_reason(reason: str | None) -> str | None:
    """The optional free-text why of a settings write, in the
    reject-reason discipline (redemption_service's mandatory gate):
    ``None`` is legal (no reason given), but a PROVIDED reason must be
    non-blank after trim — whitespace-only is the typed §29 422, never
    a silently-dropped audit field (a reason the admin believes was
    recorded must either land on the row or be refused)."""
    if reason is None:
        return None
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            _REASON_BLANK_MESSAGE,
            status_code=422,
            details={"field": "reason"},
        )
    return text


class SystemSettingValueError(BusinessError):
    """A key outside the typed registry, or a value failing its key's
    contract — the request's shape is wrong, so this is the §29
    VALIDATION_ERROR business code (422), never a 500: the caller sent
    an unusable payload."""

    def __init__(self, key: str, value: Any, reason: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            f"系统设置 {key} 不合法：{reason}",
            status_code=422,
            details={
                "key": key,
                "reason": reason,
                "value": _value_preview(value),
            },
        )


def _value_preview(value: Any) -> str:
    """A bounded, JSON-safe rendering of a rejected value for the typed
    error's details (never the stored/audited path — diagnosis only)."""
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = repr(value)
    if len(rendered) > _DETAILS_VALUE_PREVIEW_MAX:
        rendered = rendered[: _DETAILS_VALUE_PREVIEW_MAX - 1] + "…"
    return rendered


def _validated(field: str, raw: str, max_length: int | None = None) -> str:
    """The stripped value, or the typed §29 validation error.

    ``max_length`` bounds the value at a column width when one applies
    (the key); values are unbounded TEXT — their shape is the key's
    registry contract.
    """
    if not isinstance(raw, str):
        raise SystemSettingValueError(field, raw, "必须是字符串")
    stripped = raw.strip()
    if not stripped:
        raise SystemSettingValueError(field, raw, "不能为空白")
    if max_length is not None and len(stripped) > max_length:
        raise SystemSettingValueError(field, raw, f"长度不能超过 {max_length} 个字符")
    return stripped


# --- the per-key validator/normalizers ----------------------------------------------
#
# Each takes the caller's TYPED value and returns the canonical stored
# string, raising SystemSettingValueError (422) for any value the key's
# contract refuses. The normalizer is the single authority for the
# stored spelling — readers and the audit snapshots never see a
# non-canonical form.


def _normalize_current_academic_term(raw: Any) -> str:
    return _validated(CURRENT_ACADEMIC_TERM, raw, _TERM_MAX_LENGTH)


def _normalize_emoji_whitelist(raw: Any) -> str:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise SystemSettingValueError(
            EMOJI_WHITELIST, raw, "必须是字符串数组（list[str]）"
        )
    for item in raw:
        if not (_EMOJI_MIN_CODE_POINTS <= len(item) <= _EMOJI_MAX_CODE_POINTS):
            raise SystemSettingValueError(
                EMOJI_WHITELIST,
                raw,
                f"每个表情必须为 {_EMOJI_MIN_CODE_POINTS}-"
                f"{_EMOJI_MAX_CODE_POINTS} 个码点（空表合法，表示全部禁用）",
            )
    # JSON keeps the list shape lossless (an emoji may itself be a
    # comma); ensure_ascii=False stores the emoji readably.
    return json.dumps(raw, ensure_ascii=False)


def _normalize_abandon_daily_limit(raw: Any) -> str:
    # bool is an int subclass — a boolean here is a payload shape error,
    # not the integers 0/1.
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise SystemSettingValueError(ABANDON_DAILY_LIMIT, raw, "必须是整数（int）")
    if raw < 0:
        raise SystemSettingValueError(
            ABANDON_DAILY_LIMIT, raw, "必须 ≥ 0（0 表示禁止放弃）"
        )
    return str(raw)


def _normalize_management_network_enabled(raw: Any) -> str:
    if not isinstance(raw, bool):
        raise SystemSettingValueError(
            MANAGEMENT_NETWORK_ENABLED, raw, "必须是布尔值（bool）"
        )
    return "true" if raw else "false"


def _normalize_management_network_cidrs(raw: Any) -> str:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise SystemSettingValueError(
            MANAGEMENT_NETWORK_CIDRS, raw, "必须是字符串数组（list[str]，空表合法）"
        )
    canonical: list[str] = []
    for item in raw:
        try:
            # strict (the default): host bits set are a configuration
            # error, not a network — the admin_network_policy parsing
            # ruling, applied at the write boundary so the store can
            # never hold a value the loader would reject.
            canonical.append(str(ipaddress.ip_network(item)))
        except ValueError as exc:
            raise SystemSettingValueError(
                MANAGEMENT_NETWORK_CIDRS,
                raw,
                f"包含非法 CIDR：{item!r}（{exc}）",
            ) from exc
    # Comma-separated canonical spelling — the exact grammar
    # admin_network_policy.parse_management_networks reads back (a CIDR
    # never contains a comma, so the round-trip is lossless).
    return ",".join(canonical)


#: The typed key registry: every writable system-settings key and its
#: validator/normalizer. An unregistered key is unwritable (the typed
#: 422 below) — adding a key means adding its contract HERE, in the
#: same deliberate act (never a router-only key).
SystemSettingNormalizer = Callable[[Any], str]

SYSTEM_SETTING_REGISTRY: Mapping[str, SystemSettingNormalizer] = {
    CURRENT_ACADEMIC_TERM: _normalize_current_academic_term,
    EMOJI_WHITELIST: _normalize_emoji_whitelist,
    ABANDON_DAILY_LIMIT: _normalize_abandon_daily_limit,
    MANAGEMENT_NETWORK_ENABLED: _normalize_management_network_enabled,
    MANAGEMENT_NETWORK_CIDRS: _normalize_management_network_cidrs,
}


def normalize_system_setting_value(key: Any, value: Any) -> tuple[str, str]:
    """Resolve ``key`` through the registry and normalize ``value`` to
    its canonical stored form.

    Returns ``(stored_key, stored_value)`` — the exact registry key and
    the canonical string. Raises the typed §29 ``VALIDATION_ERROR``
    (422) for a key the registry does not know (unregistered keys are
    unwritable) or a value failing the key's contract.
    """
    if not isinstance(key, str) or key not in SYSTEM_SETTING_REGISTRY:
        raise SystemSettingValueError(
            key if isinstance(key, str) else repr(key),
            value,
            "未注册的设置键（不可写入）",
        )
    return key, SYSTEM_SETTING_REGISTRY[key](value)


class SystemSettingService:
    """Reads and audited writes of the ``system_settings`` current-value
    store (see the module docstring).

    ``audit`` defaults to a fresh ``AuditLogWriter`` — stateless and
    flush-only; the default means the default writer, never "no
    auditing" (the RedemptionService wiring ruling: a wiring slip must
    not silently drop audit rows).
    """

    def __init__(self, audit: AuditLogWriter | None = None) -> None:
        self._audit = audit if audit is not None else AuditLogWriter()

    async def get(self, db: AsyncSession, key: str) -> str | None:
        """The key's current value (canonical stored string), or ``None``
        when no row exists (the caller's fallback decides — see the
        module docstring)."""
        return cast(
            "str | None",
            await db.scalar(
                select(SystemSetting.value).where(SystemSetting.key == key)
            ),
        )

    async def set(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        key: str,
        value: Any,
        reason: str | None = None,
        audit_context: AuditContext | None = None,
    ) -> str:
        """Store ``value`` as the key's current value and write the
        ``SYSTEM_SETTING_UPDATED`` audit row in the SAME transaction —
        conflict-decided insert or locked update, flush, audit, one
        commit (§5; see the write-path comment for the race pattern).

        The two ``MANAGEMENT_NETWORK_*`` keys are REFUSED here: they are
        one policy split across two rows, and writing either without the
        aggregate cross-key gate is exactly how an unloadable pair gets
        stored — ``set_management_network_policy`` is their only write
        path (the single serial domain; PR #5 fix A).

        ``value`` is the CALLER's typed value, validated and normalized
        through the key's registry entry (unregistered keys are the
        typed 422). ``reason`` is the write's optional free-text why
        (``None`` legal, blank-after-trim the typed 422 — see
        ``_normalized_reason``); it lands verbatim-trimmed on the audit
        row's ``reason`` column. Returns the stored (canonical) value.
        The value MIGRATION rides the §30 snapshot pair (0016):
        ``before_snapshot={"value": ...}`` is the previous value
        (``None`` on the first write), ``after_snapshot={"value": ...}``
        the stored one — configuration facts, no PII (G11). The write's
        ``version`` (0020) rides ``details.version``: 1 on the first
        write, +1 per subsequent write. The ``before`` is TRUE under a
        lost first-write race: the loser reads the winner's committed
        value under the row lock, so the audit chain stays connected."""
        stored_key, stored_value = normalize_system_setting_value(key, value)
        if stored_key in _POLICY_KEYS:
            # Validate-before-touch shape kept: the refusal spends no
            # lock and writes nothing, and names the one legal path.
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _POLICY_KEY_MESSAGE,
                status_code=422,
                details={"key": stored_key},
            )
        # Validate-before-touch: a refused reason spends no insert, no
        # lock, and leaks no key existence (the _require_reason rule).
        reason_text = _normalized_reason(reason)
        await self._store_and_audit(
            db,
            actor=actor,
            stored_key=stored_key,
            stored_value=stored_value,
            reason_text=reason_text,
            audit_context=audit_context,
        )
        await db.commit()
        return stored_value

    async def set_management_network_policy(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        enabled: bool | None = None,
        cidrs: Sequence[str] | None = None,
        reason: str | None = None,
        audit_context: AuditContext | None = None,
    ) -> ManagementNetworkPolicyWriteResult:
        """Write ONE key of the ``MANAGEMENT_NETWORK_*`` pair inside the
        policy's single serial domain (PR #5 final-review fix A, P0).

        Exactly one of ``enabled`` / ``cidrs`` must be provided (``None``
        means "leave that key at its current effective value"); the
        method resolves the OTHER key's EFFECTIVE value — its store row,
        or the deprecated env field while no row exists (the W4
        per-key migration) — so what it validates is precisely the pair
        the per-request guard would resolve after the commit:

        1. normalize the provided value (typed 422, before any lock);
        2. ``pg_advisory_xact_lock(MANAGEMENT_NETWORK_POLICY_LOCK)`` —
           transaction-scoped, one fixed key, no deadlock ordering;
        3. read the partner key's effective value UNDER that lock;
        4. build the post-write pair and load it through
           ``load_management_network_policy`` — enabled=true with an
           empty network list is the typed 422 here, so an unloadable
           pair is unwritable from any direction;
        5. store the changed key and its audit row and commit ONCE (the
           lock releases with the commit, making the validated pair
           visible atomically).

        The first write to a key overrides its env fallback by creating
        the row (the W4 migration's move-to-store semantics). Returns
        the changed key's stored canonical value and new version.
        """
        provided: dict[str, Any] = {}
        if enabled is not None:
            provided[MANAGEMENT_NETWORK_ENABLED] = enabled
        if cidrs is not None:
            provided[MANAGEMENT_NETWORK_CIDRS] = list(cidrs)
        if not provided:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _POLICY_KEYS_REQUIRED_MESSAGE,
                status_code=422,
                details={"field": "enabled/cidrs"},
            )
        # Validate-before-touch: a refused value or reason spends no
        # advisory lock and no row read.
        reason_text = _normalized_reason(reason)
        canonical = {
            key: normalize_system_setting_value(key, value)[1]
            for key, value in provided.items()
        }
        # THE aggregate lock (see MANAGEMENT_NETWORK_POLICY_LOCK): every
        # policy writer rendezvouses here, so the partner-key read below
        # cannot interleave with another writer's validate-then-commit.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:lock)"),
            {"lock": MANAGEMENT_NETWORK_POLICY_LOCK},
        )
        # The post-write pair exactly as the guard resolves it: the
        # changed key's canonical value, the partner's row-or-env value.
        stored_enabled = canonical.get(
            MANAGEMENT_NETWORK_ENABLED,
            await self.get(db, MANAGEMENT_NETWORK_ENABLED),
        )
        stored_cidrs = canonical.get(
            MANAGEMENT_NETWORK_CIDRS,
            await self.get(db, MANAGEMENT_NETWORK_CIDRS),
        )
        try:
            load_management_network_policy(
                stored_enabled=stored_enabled,
                stored_cidrs=stored_cidrs,
            )
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _POLICY_UNLOADABLE_MESSAGE,
                status_code=422,
                details={"key": next(iter(canonical)), "reason": str(exc)},
            ) from exc
        changed_key, changed_value = next(iter(canonical.items()))
        new_version = await self._store_and_audit(
            db,
            actor=actor,
            stored_key=changed_key,
            stored_value=changed_value,
            reason_text=reason_text,
            audit_context=audit_context,
        )
        await db.commit()  # value + audit row + lock release: one unit
        return ManagementNetworkPolicyWriteResult(
            key=changed_key, value=changed_value, version=new_version
        )

    async def _store_and_audit(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        stored_key: str,
        stored_value: str,
        reason_text: str | None,
        audit_context: AuditContext | None,
    ) -> int:
        """The locked upsert plus the ``SYSTEM_SETTING_UPDATED`` audit
        append, flush-only — the caller owns the commit (so the aggregate
        policy path can hold the advisory lock across the whole unit).
        Returns the write's new ``version`` (0020).

        Chain-true first write (owner round-5 P1): the race decides
        INSIDE the database. INSERT ... ON CONFLICT DO NOTHING
        RETURNING — the winner (a row returned) created the key in
        THIS transaction, so previous is None by construction and the
        version is the server default's 1. The loser (no row
        returned: a concurrent transaction committed this key while
        this one waited on the conflict) then locks the winner's
        committed row FOR UPDATE and reads the TRUE previous before
        updating, so the audited chain stays connected (None→X, X→Y)
        even under a lost first-write race — and the version keeps
        counting every applied write (1, 2, ...). The pass-4 UPSERT
        shape pre-read the old value and audited a stale None→Y for
        the loser — the owner ruled that unacceptable: under
        concurrency the audit migration must not be false
        (quality-gates §16/G12)."""
        inserted = await db.execute(
            pg_insert(SystemSetting)
            .values(
                key=stored_key,
                value=stored_value,
                updated_by_user_id=actor.user_id,
            )
            .on_conflict_do_nothing(index_elements=[SystemSetting.key])
            .returning(SystemSetting.key)
        )
        previous: str | None
        new_version: int
        if inserted.first() is not None:
            previous = None  # this transaction created the key
            new_version = 1  # the column's server default
        else:
            row = await db.scalar(
                select(SystemSetting)
                .where(SystemSetting.key == stored_key)
                .with_for_update()
                # Fresh values under the row lock even if this
                # session's identity map already holds the instance
                # (the redemption service's locked-select discipline).
                .execution_options(populate_existing=True)
            )
            # The conflict-proven row cannot vanish under the lock: the
            # DO NOTHING insert returned nothing only because another
            # transaction COMMITTED this key, and settings have no
            # delete path.
            assert row is not None
            previous = row.value
            new_version = row.version + 1
            row.value = stored_value
            row.updated_by_user_id = actor.user_id
            row.version = new_version
        await db.flush()
        await self._audit.append(
            db,
            actor=actor,
            action=SYSTEM_SETTING_UPDATED,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=stored_key,
            reason=reason_text,
            details={"version": new_version},
            before_snapshot={"value": previous},
            after_snapshot={"value": stored_value},
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        return new_version
