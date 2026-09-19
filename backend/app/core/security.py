# backend/app/core/security.py
"""Password hashing, refresh-token hashing, and access-token codec.

Spec §5.6 (密码与会话): passwords use Argon2id, nothing reversible is
stored, the accepted length band is 10-128 with no composition rules;
sessions pair a short-lived access token with a rotatable, revocable
refresh token backed by a server-side session row. backend-engineering
§15: passwords and tokens (access or refresh) are never written to logs.

Design decisions:

- **Argon2id parameters** (via argon2-cffi ``PasswordHasher``): the library
  defaults, pinned explicitly here so a future argon2-cffi release cannot
  silently change the cost — ``time_cost=3``, ``memory_cost=64`` MiB
  (``65536`` KiB), ``parallelism=4``, ``hash_len=32``, ``salt_len=16``.
  That is RFC 9106's second recommended option; the first (t=1, p=4) is
  explicitly labeled as suitable only for memory-constrained environments,
  which a FastAPI backend is not. Verification accepts any hash the hasher
  could have produced, so raising the cost later re-hashes lazily on the
  next login, not by a migration.
- **The 10-128 band is enforced on both hash and verify, before any Argon2
  run** (spec §5.6 建议长度 10-128). Rejecting ``ValueError``-style, not by
  composition rules: the upper bound is a cheap DoS guard (Argon2 cost is
  driven by the *encoded* parameters, but hashing an unbounded password is
  still unbounded CPU/memory traffic), the lower bound is policy. Verify
  enforces the band too because such input can never match a stored hash
  and failing fast skips the expensive verification entirely.
- **Refresh tokens are hashed with plain SHA-256, not Argon2 and not an
  HMAC** (deliberate asymmetry with passwords and OTP codes): the digest is
  the ``user_sessions.refresh_token_hash`` UNIQUE-index lookup key, so it
  must be deterministic and cheap to compute on every rotation. The token
  is ``secrets.token_urlsafe(32)`` — 256 bits of CSPRNG entropy — so
  brute-forcing the digest to recover a token is infeasible *by entropy*,
  unlike a 10^6 OTP space or a human password, which is exactly the case
  Argon2/HMAC exist for. Sequential-memory hardness here would only slow
  the legitimate lookup.
- **Access tokens are PyJWT HS256 JWTs** signed with the deployment's
  ``token_secret`` (controller-approved dependency). Claims: ``sub`` (user
  id str), ``sid`` (session row id), ``role``, ``exp``/``iat`` (short TTL
  from settings), ``jti`` (unique per token). Decode pins
  ``algorithms=["HS256"]`` (rejects ``alg=none`` and algorithm confusion)
  and returns typed errors; a ``role`` claim is a snapshot for coarse UI
  decisions, never an authorization decision (backend-engineering §16).
- No function here reads settings or the clock: callers inject secret,
  TTL, and ``now``, so every behavior is testable without environment or
  sleeping.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import jwt as pyjwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# Spec §5.6: recommended password length 10-128, no composition rules.
# Shared by hash/verify here and by the registration guard in
# `app.modules.identity.service` so the band has one definition.
PASSWORD_MIN_LENGTH: Final[int] = 10
PASSWORD_MAX_LENGTH: Final[int] = 128

# Argon2id parameters (RFC 9106 second recommended option) — see module
# docstring for why they are pinned explicitly instead of trusting
# library defaults.
_ARGON2_TIME_COST: Final[int] = 3
_ARGON2_MEMORY_KIB: Final[int] = 65536  # 64 MiB
_ARGON2_PARALLELISM: Final[int] = 4
_ARGON2_HASH_LEN: Final[int] = 32
_ARGON2_SALT_LEN: Final[int] = 16

_password_hasher: Final[PasswordHasher] = PasswordHasher(
    time_cost=_ARGON2_TIME_COST,
    memory_cost=_ARGON2_MEMORY_KIB,
    parallelism=_ARGON2_PARALLELISM,
    hash_len=_ARGON2_HASH_LEN,
    salt_len=_ARGON2_SALT_LEN,
)

_LENGTH_MESSAGE: Final[str] = (
    f"password length must be {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters"
)


class AccessTokenError(Exception):
    """Base for every access-token decode failure."""


class AccessTokenExpiredError(AccessTokenError):
    def __init__(self) -> None:
        super().__init__("访问令牌已过期")


class AccessTokenInvalidError(AccessTokenError):
    def __init__(self) -> None:
        super().__init__("访问令牌无效")


def validate_password_length(password: str) -> None:
    """Raise ``ValueError`` unless ``password`` is inside the 10-128 band.

    Shared by ``hash_password``/``verify_password`` here and by the
    registration guard (`IdentityService._require_usable_password`), so the
    band has exactly one definition.
    """
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise ValueError(_LENGTH_MESSAGE)


def hash_password(password: str) -> str:
    """Hash a plaintext password into an Argon2id verifier (spec §5.6).

    Raises:
        ValueError: if the password is outside the 10-128 band. Raised
            before any hashing work, so overlong input cannot be used to
            buy expensive Argon2 runs (cheap DoS guard).
    """
    validate_password_length(password)
    return _password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    """Check ``password`` against an Argon2id verifier in constant time.

    Argon2's internal comparison is constant-time; a wrong password costs
    the same hash-and-compute as a right one, so callers can treat "wrong"
    and "unknown" identically without a timing side channel.

    Raises:
        ValueError: if the password is outside the 10-128 band (it can
            never match a stored hash, so this fails before paying for the
            Argon2 run).
    """
    validate_password_length(password)
    try:
        return _password_hasher.verify(encoded, password)
    except VerifyMismatchError:
        return False
    # Other argon2 exceptions (invalid hash format etc.) propagate: a
    # malformed stored verifier is a server fault, not an authentication
    # outcome.


def generate_refresh_token() -> str:
    """A fresh 256-bit refresh token (spec §5.6: rotatable, revocable).

    Only the SHA-256 digest of the returned value is ever persisted.
    """
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """SHA-256 hex digest of a refresh token — the DB lookup key.

    See the module docstring for why plain SHA-256 (deterministic, cheap
    UNIQUE-index lookup; 256-bit random tokens need no key-stretching)
    rather than Argon2id, and why the OTP HMAC scheme does not apply: the
    OTP hash exists to protect a 10^6 code space, this digest protects a
    2^256 one.
    """
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """Typed view of a verified access token's claims (spec §5.6).

    ``sub``/``sid`` stay strings (JWT claim values), validated as UUIDs at
    decode time; ``role`` is the login-time snapshot, never an
    authorization decision by itself (backend-engineering §16).
    """

    sub: str
    sid: str
    role: str
    exp: int
    iat: int
    jti: str


@dataclass(frozen=True, slots=True)
class AccessTokenCodec:
    """Encode/decode HS256 access tokens against one signing secret.

    Constructed from settings at the composition root; unit tests build
    instances directly with a test secret and TTL.
    """

    secret: str
    ttl_minutes: int

    def __post_init__(self) -> None:
        if not self.secret:
            raise ValueError("secret must be a non-empty string")
        if self.ttl_minutes < 1:
            raise ValueError("ttl_minutes must be >= 1")

    def encode(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID, role: str, now: datetime
    ) -> str:
        """Mint one access token; ``exp`` is ``now`` plus the TTL."""
        iat = int(now.timestamp())
        return pyjwt.encode(
            {
                "sub": str(user_id),
                "sid": str(session_id),
                "role": role,
                "iat": iat,
                "exp": iat + self.ttl_minutes * 60,
                "jti": uuid.uuid4().hex,
            },
            self.secret,
            algorithm="HS256",
        )

    def decode(self, token: str) -> AccessTokenClaims:
        """Verify signature, algorithm, and expiry; return typed claims.

        Expired tokens raise `AccessTokenExpiredError` so callers can
        prompt a refresh; every other failure (bad signature, wrong
        algorithm, garbage, missing/mistyped claims) raises
        `AccessTokenInvalidError`. The token itself never appears in any
        error or log line (backend-engineering §15).
        """
        try:
            claims: dict[str, object] = pyjwt.decode(
                token, self.secret, algorithms=["HS256"]
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise AccessTokenExpiredError from exc
        except pyjwt.PyJWTError as exc:
            raise AccessTokenInvalidError from exc
        return self._validated_claims(claims)

    @staticmethod
    def _validated_claims(claims: dict[str, object]) -> AccessTokenClaims:
        """Project decoded JSON into typed claims, rejecting shape drift.

        A syntactically valid JWT signed by us still gets its claim set
        checked: UUID-parsable ``sub``/``sid``, integer ``exp``/``iat``,
        non-empty ``role``/``jti``. Anything else is a token we did not
        mint (or a bug), and authenticates nothing.
        """

        def _require_str(name: str) -> str:
            value = claims.get(name)
            if not isinstance(value, str) or not value:
                raise AccessTokenInvalidError
            return value

        sub = _require_str("sub")
        sid = _require_str("sid")
        role = _require_str("role")
        jti = _require_str("jti")
        try:
            uuid.UUID(sub)
            uuid.UUID(sid)
        except ValueError as exc:
            raise AccessTokenInvalidError from exc
        exp = claims.get("exp")
        iat = claims.get("iat")
        if not isinstance(exp, int) or not isinstance(iat, int):
            raise AccessTokenInvalidError
        return AccessTokenClaims(sub=sub, sid=sid, role=role, exp=exp, iat=iat, jti=jti)


_timing_shield_hash: str | None = None


def timing_shield_hash() -> str:
    """A throwaway Argon2id verifier for unknown-user logins (§5.6).

    ``SessionService`` verifies submitted passwords against this digest
    when the username does not resolve, so an unknown user costs the same
    Argon2 run as a wrong password and the failure timing does not
    enumerate accounts. Created lazily on first use (one Argon2 hash at
    import time would tax every process start, including CLI and workers
    that never log in); the cached value is a random-token digest, so it
    matches nothing and reveals nothing.
    """
    global _timing_shield_hash
    if _timing_shield_hash is None:
        _timing_shield_hash = _password_hasher.hash(secrets.token_urlsafe(32))
    return _timing_shield_hash


def constant_time_equals(left: str, right: str) -> bool:
    """Constant-time string comparison for secret-shaped values."""
    return hmac.compare_digest(left.encode(), right.encode())
