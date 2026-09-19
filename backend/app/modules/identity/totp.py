# backend/app/modules/identity/totp.py
"""TOTP secret encryption, code verification, and recovery codes.

Spec §5.6/§5.8: Teacher/Admin accounts MUST use TOTP 2FA; recovery codes
are one-time and shown exactly once; the server stores the TOTP secret
encrypted and recovery codes as hashes.
backend-engineering §15/§16: neither a TOTP secret nor a recovery code is
ever logged; §20: no hand-rolled crypto — encryption is Fernet
(cryptography, controller-approved), codes are RFC 6238 via pyotp, and
recovery-code hashing reuses the Argon2id password primitives.

Design decisions:

- **The TOTP secret rests as a Fernet token** keyed by
  `Settings.totp_encryption_key` (sentinel-guarded in production). Fernet
  gives AES-128-CBC + HMAC-SHA256 authenticated encryption with a random
  IV per token — the at-rest requirement is confidentiality of a secret
  the authenticator app also holds, not password-grade key stretching:
  the secret is 160 random base32 bits (unlike a human password), so
  offline brute force of the ciphertext is infeasible by entropy, and the
  service must decrypt it on every verification anyway.
- **Code verification is business-Clock-driven**: `verify_totp_code` takes
  ``at`` (the injected `Clock.now()`), so window boundaries are pure
  datetime arithmetic (backend-engineering §11). The ±1 window is the
  standard RFC 6238 drift allowance (one 30-second step either side).
- **Recovery codes are Argon2id-hashed through the shared password
  helpers** (`app.core.security`), not SHA-256/HMAC: an attacker who dumps
  `recovery_codes` must pay the full Argon2 cost per candidate. The
  canonical format ``xxxxx-xxxxx`` (2x5 hex) is 40 random bits and —
  deliberately — 11 characters, inside the 10-128 password band the
  shared hasher enforces, so hashing and verification need no bypass.
  Argon2's internal comparison is constant-time; a wrong code costs the
  same as a right one, which keeps verification free of a timing oracle.
- Nothing here reads Settings, logs, or persists: key and clock are
  injected, storage belongs to the service transaction.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime

import pyotp
from cryptography.fernet import Fernet

from app.core.security import hash_password, verify_password

TOTP_ISSUER = "CampusQuest"
RECOVERY_CODE_COUNT = 8
# 2 x 5 hex chars joined by a dash: "0f1e2-d3c4b". See module docstring for
# why the format must stay >= 10 characters (shared Argon2id band).
RECOVERY_CODE_PATTERN = re.compile(r"[0-9a-f]{5}-[0-9a-f]{5}")

# RFC 6238 drift allowance: accept codes from ±1 time step (30s).
TOTP_VALID_WINDOW = 1

_HEX_ALPHABET = "0123456789abcdef"


def _hex_group(length: int) -> str:
    """``length`` CSPRNG hex characters (5 chars = 20 random bits)."""
    return "".join(secrets.choice(_HEX_ALPHABET) for _ in range(length))


def generate_totp_secret() -> str:
    """A fresh 160-bit base32 TOTP secret (pyotp/secrets CSPRNG)."""
    return pyotp.random_base32()


def encrypt_totp_secret(fernet: Fernet, secret: str) -> bytes:
    """Encrypt a TOTP secret into the bytes stored at rest."""
    return fernet.encrypt(secret.encode())


def decrypt_totp_secret(fernet: Fernet, encrypted: bytes) -> str:
    """Decrypt `totp_credentials.secret_encrypted` back into the secret.

    Raises:
        cryptography.fernet.InvalidToken: if the bytes were encrypted under
            a different key or were tampered with.
    """
    return fernet.decrypt(encrypted).decode()


def build_otpauth_uri(secret: str, account_name: str) -> str:
    """The provisioning URI authenticator apps render as a QR code."""
    return pyotp.TOTP(secret).provisioning_uri(
        name=account_name, issuer_name=TOTP_ISSUER
    )


def verify_totp_code(
    secret: str, code: str, *, at: datetime, valid_window: int = TOTP_VALID_WINDOW
) -> bool:
    """Whether ``code`` is a valid RFC 6238 code for ``secret`` at ``at``.

    Outer whitespace is tolerated (humans and apps pad codes); the
    comparison inside pyotp is constant-time over the HMAC inner state.
    """
    return bool(
        pyotp.TOTP(secret).verify(code.strip(), for_time=at, valid_window=valid_window)
    )


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """``count`` fresh one-time recovery codes in the canonical format.

    Generated with `secrets` (CSPRNG); each half carries 20 random bits.
    """
    return [f"{_hex_group(5)}-{_hex_group(5)}" for _ in range(count)]


def hash_recovery_code(code: str) -> str:
    """Hash one recovery code into a storable Argon2id verifier.

    Same primitive and parameters as passwords (`app.core.security`):
    fresh salt per code, nothing reversible, no plaintext fragment.
    """
    return hash_password(code)


def recovery_code_matches(code: str, code_hash: str) -> bool:
    """Check ``code`` against one stored recovery-code hash, constant time.

    Band violations (codes outside 10-128 characters can never match a
    stored verifier) surface as ``False`` rather than an exception, so a
    caller looping over a user's stored hashes needs no special casing.
    """
    try:
        return verify_password(code, code_hash)
    except ValueError:
        return False
