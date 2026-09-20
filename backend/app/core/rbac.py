# backend/app/core/rbac.py
"""Role-only authorization helpers shared by every module (spec §4).

Layering contract: this module lives in the core layer and imports
NOTHING from ``app.modules`` — not the ``Role`` enum, not ``Actor``. It
speaks two vocabularies instead:

- Roles as plain strings: ``StrEnum`` members ARE strings, so callers
  pass ``Role.ADMIN`` or ``"ADMIN"`` interchangeably (``role_value``
  normalizes). The literal member names below are pinned to the frozen
  identity enum by tests/unit/identity/test_rbac.py, so drift fails
  loudly instead of silently flipping an authorization decision.
- "A thing carrying a role" as the structural ``RoleBearer`` protocol:
  identity's frozen ``Actor`` satisfies it without core knowing the
  type, which is how a role guard can live here at all.

``require_role(*roles)`` returns a real FastAPI dependency, but a role
guard needs an authenticated bearer and authentication is identity-module
machinery (JWT decode, user row, session liveness — backend-engineering
§16: authorization is server-side). The seam is ``get_role_bearer``: a
provider dependency that raises until the composition root installs the
real one through FastAPI's own extension point::

    app.dependency_overrides[rbac.get_role_bearer] = identity_get_actor

(The composition root in app/main.py installs this once at startup; the
account-status integration test shows the same wiring.) An unwired guard
therefore fails loudly as a 500 ``INTERNAL_ERROR`` — never silently as
"deny everyone".

Denials raise ``BusinessError`` with the frozen §29 code
``PERMISSION_DENIED`` (403): a role mismatch is an authorization outcome,
not an authentication one, and the unified envelope handler renders it
without any per-route work (spec §29; backend-engineering §10).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from enum import Enum
from typing import Annotated, Any, Final, Protocol

from fastapi import Depends

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError

# Canonical role member names (spec §4; interfaces.md "Role"). Parity with
# app.modules.identity.enums.Role is pinned by tests/unit/identity/test_rbac.py.
_STUDENT: Final[str] = "STUDENT"
_TEACHER: Final[str] = "TEACHER"
_ADMIN: Final[str] = "ADMIN"
# The management family (spec §4.2-4.3): the only roles that may sit
# behind the management endpoints' mandatory-2FA gate (§33.4).
_STAFF_ROLES: Final[frozenset[str]] = frozenset({_TEACHER, _ADMIN})

_PERMISSION_DENIED_MESSAGE = "当前角色无权执行该操作"


def role_value(role: str | Enum) -> str:
    """``role`` as its canonical string (enum member value or the string).

    A ``StrEnum`` member takes the ``str`` fast path — it already IS its
    value; other enums project through ``.value``.
    """
    if isinstance(role, str):
        return role
    value = role.value
    if not isinstance(value, str):
        raise ValueError(f"role {role!r} carries a non-string value {value!r}")
    return value


def is_student(role: str | Enum) -> bool:
    """True for the STUDENT role (spec §4.1)."""
    return role_value(role) == _STUDENT


def is_staff(role: str | Enum) -> bool:
    """True for the management family: TEACHER or ADMIN (spec §4.2-4.3)."""
    return role_value(role) in _STAFF_ROLES


def is_admin(role: str | Enum) -> bool:
    """True for the ADMIN role (spec §4.3)."""
    return role_value(role) == _ADMIN


def has_any_role(role: str | Enum, *allowed: str | Enum) -> bool:
    """True iff ``role`` is one of ``allowed`` (set membership, that's all
    V1 role checks are — the resource-level rules are use-case code)."""
    return role_value(role) in {role_value(candidate) for candidate in allowed}


class RoleBearer(Protocol):
    """Anything with a ``role`` — structurally the identity ``Actor``."""

    @property
    def role(self) -> str: ...


async def get_role_bearer() -> RoleBearer:
    """Default bearer provider: deliberately unwired.

    The composition root replaces this dependency (FastAPI
    ``dependency_overrides``) with the identity module's ``get_actor``.
    Leaving it unwired is a startup-class bug, so it fails loudly instead
    of degrading to "deny everyone" — the misconfiguration surfaces as a
    safe 500 on the first guarded request.
    """
    raise RuntimeError(
        "core.rbac.get_role_bearer is not wired: install the identity "
        "module's actor dependency via "
        "app.dependency_overrides[rbac.get_role_bearer] at the "
        "composition root"
    )


def require_role(
    *roles: str | Enum,
) -> Callable[..., Coroutine[Any, Any, RoleBearer]]:
    """FastAPI dependency factory: admit the request only for ``roles``.

    Usage (backend-engineering §3 idiom)::

        actor: Annotated[Actor, Depends(require_role(Role.ADMIN))]

    The guard passes the resolved bearer through unchanged, so it doubles
    as the route's actor source. A role mismatch raises
    ``PERMISSION_DENIED`` (403). At least one role is required:
    ``require_role()`` would silently mean "deny everyone", which is a
    wiring bug, so it is rejected at factory time instead (fail loud on
    developer error, not on every request).
    """
    if not roles:
        raise ValueError("require_role requires at least one role")
    allowed: Final[frozenset[str]] = frozenset(role_value(role) for role in roles)

    async def role_guard(
        bearer: Annotated[RoleBearer, Depends(get_role_bearer)],
    ) -> RoleBearer:
        if role_value(bearer.role) not in allowed:
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _PERMISSION_DENIED_MESSAGE,
                status_code=403,
            )
        return bearer

    return role_guard
