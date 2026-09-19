# backend/app/modules/identity/schemas.py
"""Identity command and response schemas (spec §5, §40).

Two deliberately different shapes (backend-engineering §9):

- ``RegisterStudent`` is an internal command dataclass, not a Pydantic
  request model: the HTTP request schema (untrusted transport input)
  arrives with the router in Task 9 and constructs this command after its
  own parsing.
- ``UserPublic`` is the response contract. It enumerates its fields and is
  built explicitly from the ORM object — never serialized from it — so
  ``password_hash``, ``phone_e164``, and any future internal column are
  unrepresentable in a response, privacy by construction (spec §40).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User


@dataclass(frozen=True, slots=True)
class RegisterStudent:
    """Command for ``IdentityService.register_student`` (spec §5.1-5.4).

    ``student_number`` and ``nickname`` are raw caller input; the service
    validates/normalizes them through the Task-2 validators before any
    persistence. ``phone_token`` references an already-verified phone
    challenge (Task 4 owns the lifecycle). ``password`` is plaintext in
    memory only and is hashed through the ``PasswordHasher`` port before
    the User row exists.
    """

    student_number: str
    nickname: str
    phone_token: str
    password: str


class UserPublic(BaseModel):
    """Public account view returned to the account owner (spec §40).

    The owner sees their own username/student number; no other surface
    exposes it. Status is included because ACTIVE (vs PENDING_PHONE)
    is the registration outcome clients branch on.
    """

    id: UUID
    username: str
    nickname: str
    role: Role
    status: UserStatus

    @classmethod
    def from_user(cls, user: User) -> UserPublic:
        """Project an ORM ``User`` into the public contract, field by field."""

        return cls(
            id=user.id,
            username=user.username,
            nickname=user.nickname,
            role=Role(user.role),
            status=UserStatus(user.status),
        )
