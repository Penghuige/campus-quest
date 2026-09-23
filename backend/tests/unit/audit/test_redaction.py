# backend/tests/unit/audit/test_redaction.py
"""The recursive secret-redaction layer audit payloads pass through
(G11 defense in depth; Plan 08 Task 1, plan step 1 verbatim).

Until Plan 08, snapshots were safe only because every caller built
them by hand. ``redact`` makes the safety a property of the WRITER:
any key naming a secret — nested in dicts, lists, dicts inside lists
inside dicts — is replaced with ``"[REDACTED]"`` while everything else
passes through untouched. The end-to-end half of the contract (the
writer forcing payloads through this function onto a real row) lives
in tests/integration/audit/test_audit_log.py.
"""

from __future__ import annotations

from app.modules.audit.redaction import REDACTED, SENSITIVE_KEYS, redact


def test_redacts_every_secret_at_any_nesting_depth() -> None:
    # Plan step 1's scenario: password_hash, otp, refresh_token,
    # totp_secret, and nested recovery codes all redacted; non-secret
    # keys and their values survive untouched.
    snapshot = {
        "username": "20250001",  # a plain fact, not a secret KEY
        "password_hash": "$argon2id$...",
        "nested": {
            "otp": "123456",
            "deeper": [{"refresh_token": "rt-secret"}, {"totp_secret": "BASE32"}],
        },
        "recovery_codes": ["code-one", "code-two"],
    }

    redacted = redact(snapshot)

    assert redacted == {
        "username": "20250001",
        "password_hash": REDACTED,
        "nested": {
            "otp": REDACTED,
            "deeper": [{"refresh_token": REDACTED}, {"totp_secret": REDACTED}],
        },
        "recovery_codes": REDACTED,
    }


def test_matches_key_names_case_insensitively() -> None:
    # The sensitive set is lowercase and the comparison lowercases the
    # key: a secret spelled in SHOUTING or Mixed case still redacts —
    # case folding only, exact name otherwise ("TotpSecret" is NOT
    # normalized to "totp_secret"; underscore-free camelCase is a
    # different name, the documented exact-match rule).
    assert redact({"PASSWORD_HASH": "x"}) == {"PASSWORD_HASH": REDACTED}
    assert redact({"Otp_Code": "123456"}) == {"Otp_Code": REDACTED}
    assert redact({"TOTP_SECRET": "s"}) == {"TOTP_SECRET": REDACTED}
    assert redact({"AccessToken": "at"}) == {"AccessToken": "at"}


def test_covers_the_full_secret_vocabulary() -> None:
    # Every documented spelling (the staff models' secret_encrypted,
    # the singular recovery_code, a misplaced session credential) is in
    # the module-level frozenset and redacts by behavior, not spelling.
    payload = dict.fromkeys(SENSITIVE_KEYS, "value")
    assert all(value == REDACTED for value in redact(payload).values())
    assert REDACTED not in payload.values()  # the input itself is intact


def test_exact_key_match_only() -> None:
    # Matching is on the full key name, never a substring: a key that
    # merely CONTAINS a secret word is a different business fact.
    assert redact({"password_hint": "at least 12 chars"}) == {
        "password_hint": "at least 12 chars"
    }
    assert redact({"otp_channel": "SMS"}) == {"otp_channel": "SMS"}


def test_never_mutates_the_caller_payload() -> None:
    # The caller's mapping is the input, not scratch space: redaction
    # rebuilds containers, so a snapshot reused after append still
    # carries its original values.
    inner = {"password": "hunter2"}
    payload = {"credentials": inner, "keep": "me"}

    result = redact(payload)

    assert result["credentials"] == {"password": REDACTED}
    assert inner == {"password": "hunter2"}
    assert payload == {"credentials": {"password": "hunter2"}, "keep": "me"}


def test_leaf_values_and_empty_containers_pass_through() -> None:
    # Nothing to redact: scalars keep their identity, empty structures
    # stay empty (and a scalar at the top level is returned unchanged).
    assert redact("plain text") == "plain text"
    assert redact(None) is None
    assert redact(42) == 42
    assert redact({}) == {}
    assert redact([]) == []
    assert redact({"facts": [1, 2.5, True, None]}) == {"facts": [1, 2.5, True, None]}
