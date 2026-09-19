# backend/app/modules/identity/ports.py
"""Ports owned by the identity module (docs/architecture/interfaces.md).

Task sequencing: registration (Task 3) depends on the port shapes below
long before the real implementations land, so the seams are frozen here and
tests inject deterministic fakes.

- ``PasswordHasher`` is a plain callable: Task 5 plugs in the Argon2id
  implementation (spec §5.6), and registration never needs verify/rehash
  behavior, so a function signature is the whole contract.
- ``PhoneVerificationPort`` resolves an already-verified phone-challenge
  token (spec §33.2) to its normalized E.164 number. Task 4 owns the
  challenge lifecycle — TTL, attempt limits, single-use consumption;
  registration only consumes a token that already passed verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VerifiedPhone:
    """A phone number certified by a completed OTP challenge (spec §5.4).

    The E.164 form is the only phone representation registration ever sees:
    raw input was normalized at challenge-creation time (Task 4), and the
    original formatting is never a uniqueness key.
    """

    phone_e164: str


class PasswordHasher(Protocol):
    """Hash a plaintext password into a storable verifier (spec §5.6)."""

    def __call__(self, password: str) -> str: ...


class PhoneVerificationPort(Protocol):
    """Resolve a verified phone-challenge token to its ``VerifiedPhone``.

    Implementations raise on unknown, expired, purpose-mismatched, or
    already-consumed tokens; a token that resolves here is proof the phone
    passed the OTP challenge, which is why registration creates the account
    directly in ACTIVE status instead of PENDING_PHONE.
    """

    async def verify_phone_token(self, token: str) -> VerifiedPhone: ...
