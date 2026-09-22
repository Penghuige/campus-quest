# backend/app/modules/audit/redaction.py
"""Recursive secret redaction for audit payloads (G11; Plan 08 Task 1).

The §30 snapshots were safe until now only because every CALLER built
them by hand; the final plan review ruled that insufficient as an
invariant: one future caller dropping a credential-bearing dict into
``details`` would persist a secret onto an append-only, undeletable
row. ``AuditLogWriter.append`` therefore FORCES every structured
payload column through :func:`redact` — defense in depth, so a safe
snapshot is the writer's guarantee, not the caller's premise.

``reason`` is deliberately NOT redacted: it is the free-text WHY of
the action (§30), a sentence a human typed, not a structured value a
caller could smuggle a credential into by key name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "REDACTED",
    "SENSITIVE_KEYS",
    "redact",
]

# The replacement marker for every redacted value.
REDACTED = "[REDACTED]"

# Key names whose VALUES never belong on an audit row (plan Global
# Constraints: password hashes, OTPs, refresh tokens, TOTP secrets,
# raw recovery codes — plus the secret/ciphertext spellings the staff
# models use and the session credential a caller might misplace).
# Lowercase here; matching is case-insensitive on the FULL key name
# (exact match, not substring — "password_hint" is not a secret).
# Extend this set, not a caller-side allowlist, when a new secret
# spelling appears.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "otp",
        "otp_code",
        "password",
        "password_hash",
        "recovery_code",
        "recovery_codes",
        "refresh_token",
        "secret",
        "secret_encrypted",
        "totp_secret",
    }
)


def redact(value: Any) -> Any:
    """A redacted copy of ``value``: every dict entry whose key (compared
    case-insensitively) names a secret in :data:`SENSITIVE_KEYS` becomes
    ``"[REDACTED]"``, at any nesting depth of dicts and lists.

    The input is never mutated — dicts and lists are rebuilt, leaf
    values (str/int/float/bool/None) pass through as-is. Non-str dict
    keys cannot match the (string) sensitive set and are kept verbatim.
    """
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def _is_sensitive(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in SENSITIVE_KEYS
