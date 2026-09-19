# backend/tests/unit/identity/test_rbac.py
"""Unit tests for the role-only RBAC helpers (app/core/rbac.py).

Deliberately DB-free: these pin the pure predicates, their parity with
identity's frozen ``Role`` enum, and the ``require_role`` factory behavior
with FAKE bearers. The identity-side resolution (JWT decode, user row,
session liveness, status, TOTP state) is exercised for real against
PostgreSQL by tests/integration/identity/test_account_status.py.
"""

# NOTE: deliberately no `from __future__ import annotations`. The guarded
# route's annotation contains `require_role(*roles)`, which references the
# enclosing helper's local ``roles``; under PEP 563 that annotation becomes
# a string FastAPI can only evaluate against module globals, and the local
# name would turn the parameter back into a plain request field (422).
from dataclasses import dataclass
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core import rbac
from app.core.error_codes import ErrorCode
from app.core.errors import register_exception_handlers
from app.modules.identity.enums import Role


@dataclass(frozen=True, slots=True)
class FakeBearer:
    """Structural stand-in for ``Actor``: anything with a ``role``."""

    role: str


def _guard_app(*roles: str | Role) -> FastAPI:
    """A minimal app whose only route is guarded by ``require_role``.

    The §29 envelope handlers are attached so denials render exactly as
    they will in production (backend-engineering §10), not as framework
    defaults.
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/guarded")
    async def guarded(
        bearer: Annotated[FakeBearer, Depends(rbac.require_role(*roles))],
    ) -> dict[str, str]:
        return {"role": bearer.role}

    return app


def _install_bearer(app: FastAPI, bearer: FakeBearer) -> None:
    """Point core's bearer seam at a fake actor (the same override key the
    production composition root uses for the real ``get_actor``)."""

    async def _provider() -> FakeBearer:
        return bearer

    app.dependency_overrides[rbac.get_role_bearer] = _provider


@pytest.mark.parametrize(
    ("member", "is_student", "is_staff", "is_admin"),
    [
        (Role.STUDENT, True, False, False),
        (Role.TEACHER, False, True, False),
        (Role.ADMIN, False, True, True),
    ],
)
def test_predicates_match_the_frozen_role_members(
    member: Role, is_student: bool, is_staff: bool, is_admin: bool
) -> None:
    # The truth table doubles as the parity guard: rbac speaks plain
    # strings and never imports the enum, so its literals must equal the
    # frozen member values or the table flips.
    assert rbac.is_student(member) is is_student
    assert rbac.is_staff(member) is is_staff
    assert rbac.is_admin(member) is is_admin
    # Plain strings and enum members are interchangeable inputs.
    assert rbac.is_student(member.value) is is_student
    assert rbac.is_staff(member.value) is is_staff
    assert rbac.is_admin(member.value) is is_admin


def test_role_value_accepts_enum_members_and_plain_strings() -> None:
    assert rbac.role_value(Role.ADMIN) == "ADMIN"
    assert rbac.role_value("ADMIN") == "ADMIN"


def test_has_any_role_is_set_membership() -> None:
    assert rbac.has_any_role(Role.TEACHER, Role.TEACHER, Role.ADMIN)
    assert rbac.has_any_role("ADMIN", Role.TEACHER, Role.ADMIN)
    assert not rbac.has_any_role(Role.STUDENT, Role.TEACHER, Role.ADMIN)
    assert not rbac.has_any_role(Role.STUDENT)


def test_require_role_passes_the_allowed_bearer_through() -> None:
    app = _guard_app(Role.ADMIN)
    _install_bearer(app, FakeBearer(role="ADMIN"))

    response = TestClient(app).get("/guarded")

    assert response.status_code == 200
    assert response.json() == {"role": "ADMIN"}


def test_require_role_denies_other_roles_with_permission_denied() -> None:
    app = _guard_app(Role.ADMIN)
    _install_bearer(app, FakeBearer(role="STUDENT"))

    response = TestClient(app).get("/guarded")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED


@pytest.mark.parametrize("role", [Role.TEACHER, Role.ADMIN, "TEACHER", "ADMIN"])
def test_require_role_allows_any_listed_role(role: Role | str) -> None:
    # A Teacher-or-Admin operation admits both members; enum members and
    # plain strings name the same set.
    app = _guard_app(Role.TEACHER, Role.ADMIN)
    _install_bearer(app, FakeBearer(role=role if isinstance(role, str) else role.value))

    assert TestClient(app).get("/guarded").status_code == 200


def test_require_role_without_roles_is_a_factory_error() -> None:
    # require_role() would silently mean "deny everyone"; that is a wiring
    # bug, so it must fail at factory time instead of on every request.
    with pytest.raises(ValueError, match="at least one role"):
        rbac.require_role()


async def test_unwired_bearer_provider_fails_loud() -> None:
    with pytest.raises(RuntimeError, match="not wired"):
        await rbac.get_role_bearer()


def test_unwired_guard_surfaces_as_safe_internal_error() -> None:
    # An app that forgot the composition-root override must fail with the
    # safe 500 envelope (server-side traceback, generic response), never
    # by silently denying or granting.
    app = _guard_app(Role.ADMIN)

    response = TestClient(app, raise_server_exceptions=False).get("/guarded")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == ErrorCode.INTERNAL_ERROR
