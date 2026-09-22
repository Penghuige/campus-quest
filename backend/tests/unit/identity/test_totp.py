# backend/tests/unit/identity/test_totp.py
"""Unit tests for TOTP secret encryption, code verification, recovery codes.

Spec §5.6/§5.8: staff MUST use TOTP 2FA with one-time recovery codes shown
exactly once; the server stores the TOTP secret encrypted (Fernet, key from
Settings) and recovery codes as Argon2id hashes only.
backend-engineering §15/§16: neither the TOTP secret nor a recovery code
ever reaches a log line.

All code verification is Clock-driven: `verify_totp_code` takes the
business time as an argument, so window boundaries are exercised by
shifting a datetime, never by sleeping. The Settings tests live here (not
in tests/unit/core/test_config.py) because totp.py is the setting's
consumer and this file ships with it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.identity.totp import (
    RECOVERY_CODE_COUNT,
    RECOVERY_CODE_PATTERN,
    build_otpauth_uri,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_code,
    recovery_code_matches,
    verify_totp_code,
)

_NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
_STEP = timedelta(seconds=30)
# Generated once for the test run; production wires the key from Settings.
_FERNET = Fernet(Fernet.generate_key())


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/test")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest")
    monkeypatch.setenv("S3_ACCESS_KEY", "access")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")


class TestTotpSecretEncryption:
    def test_roundtrip_returns_original_secret(self) -> None:
        secret = generate_totp_secret()

        encrypted = encrypt_totp_secret(_FERNET, secret)
        decrypted = decrypt_totp_secret(_FERNET, encrypted)

        assert decrypted == secret

    def test_ciphertext_is_not_the_plaintext_secret(self) -> None:
        # What rests in `totp_credentials.secret_encrypted` must not contain
        # the base32 secret verbatim (spec §5.8: no plaintext at rest).
        secret = generate_totp_secret()

        encrypted = encrypt_totp_secret(_FERNET, secret)

        assert encrypted != secret.encode()
        assert secret.encode() not in encrypted

    def test_each_encryption_yields_a_fresh_token(self) -> None:
        # Fernet includes a random IV, so the same secret encrypts to a
        # different token every time — two users (or two setups) never leak
        # secret equality through equal ciphertext bytes.
        secret = generate_totp_secret()

        assert encrypt_totp_secret(_FERNET, secret) != encrypt_totp_secret(
            _FERNET, secret
        )

    def test_wrong_key_cannot_decrypt(self) -> None:
        encrypted = encrypt_totp_secret(_FERNET, generate_totp_secret())

        with pytest.raises(InvalidToken):
            decrypt_totp_secret(Fernet(Fernet.generate_key()), encrypted)


class TestTotpCodeVerification:
    def test_code_for_the_current_window_is_accepted(self) -> None:
        secret = generate_totp_secret()
        code = _code_at(secret, _NOW)

        assert verify_totp_code(secret, code, at=_NOW) is True

    def test_adjacent_window_is_accepted_with_standard_drift(self) -> None:
        # ±1 window is the standard clock-drift allowance (RFC 6238): a code
        # minted one 30-second step earlier must still verify.
        secret = generate_totp_secret()
        code = _code_at(secret, _NOW - _STEP)

        assert verify_totp_code(secret, code, at=_NOW) is True

    def test_code_beyond_the_drift_window_is_rejected(self) -> None:
        secret = generate_totp_secret()
        code = _code_at(secret, _NOW - 4 * _STEP)

        assert verify_totp_code(secret, code, at=_NOW) is False

    def test_wrong_code_is_rejected(self) -> None:
        secret = generate_totp_secret()
        code = _code_at(secret, _NOW)
        wrong = "000000" if code != "000000" else "111111"

        assert verify_totp_code(secret, wrong, at=_NOW) is False

    def test_code_for_a_different_secret_is_rejected(self) -> None:
        code = _code_at(generate_totp_secret(), _NOW)

        assert verify_totp_code(generate_totp_secret(), code, at=_NOW) is False

    def test_outer_whitespace_is_tolerated(self) -> None:
        # Authenticator apps and humans pad codes; the value is trimmed
        # before comparison instead of failing a well-meaning copy-paste.
        secret = generate_totp_secret()
        code = _code_at(secret, _NOW)

        assert verify_totp_code(secret, f"  {code}\n", at=_NOW) is True


class TestOtpauthUri:
    def test_uri_names_issuer_account_and_secret(self) -> None:
        secret = generate_totp_secret()

        uri = build_otpauth_uri(secret, "chen.li@campus.example.edu.cn")

        assert uri.startswith("otpauth://totp/CampusQuest:")
        assert "chen.li%40campus.example.edu.cn" in uri
        assert f"secret={secret}" in uri


class TestRecoveryCodes:
    def test_eight_codes_in_canonical_format(self) -> None:
        codes = generate_recovery_codes()

        assert len(codes) == RECOVERY_CODE_COUNT == 8
        for code in codes:
            assert RECOVERY_CODE_PATTERN.fullmatch(code)

    def test_codes_are_unique_per_batch(self) -> None:
        # A batch with a duplicate would leave the user fewer usable codes
        # than displayed (each hash row is distinct).
        codes = generate_recovery_codes()

        assert len(set(codes)) == len(codes)

    def test_hash_stores_no_plaintext_and_is_argon2id(self) -> None:
        code = generate_recovery_codes()[0]

        code_hash = hash_recovery_code(code)

        assert code not in code_hash
        assert code_hash.startswith("$argon2id$")

    def test_hash_is_salted_per_code(self) -> None:
        code = generate_recovery_codes()[0]

        assert hash_recovery_code(code) != hash_recovery_code(code)

    def test_match_roundtrip_and_mismatch(self) -> None:
        code = generate_recovery_codes()[0]
        other = generate_recovery_codes()[0]
        if other == code:
            other = generate_recovery_codes()[0]

        code_hash = hash_recovery_code(code)

        assert recovery_code_matches(code, code_hash) is True
        assert recovery_code_matches(other, code_hash) is False

    def test_codes_clear_the_password_length_band(self) -> None:
        # Recovery codes are verified through the shared Argon2id helpers,
        # which enforce the 10-128 band; the canonical format must stay
        # inside it or every verification would raise instead of comparing.
        from app.core.security import PASSWORD_MIN_LENGTH

        for code in generate_recovery_codes():
            assert len(code) >= PASSWORD_MIN_LENGTH


class TestTotpSettings:
    def test_default_totp_key_is_a_valid_fernet_key(self, monkeypatch) -> None:
        # The committed development sentinel must actually work locally:
        # Fernet construction would raise on anything that is not 32
        # url-safe base64 bytes.
        _set_required_env(monkeypatch)
        monkeypatch.delenv("TOTP_ENCRYPTION_KEY", raising=False)

        settings = Settings()

        Fernet(settings.totp_encryption_key)  # does not raise
        assert settings.staff_invitation_ttl_hours == 48

    def test_production_rejects_sentinel_totp_key(self, monkeypatch) -> None:
        # With the committed sentinel anyone can decrypt every stored TOTP
        # secret, so production must fail at settings load (same guard as
        # the OTP HMAC and token-signing sentinels).
        _set_required_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("OTP_HMAC_SECRET", "a-real-deployment-secret")
        monkeypatch.setenv("TOKEN_SECRET", "a-real-access-token-secret-0123456789")
        monkeypatch.delenv("TOTP_ENCRYPTION_KEY", raising=False)

        with pytest.raises(ValidationError, match="TOTP_ENCRYPTION_KEY"):
            Settings()

    def test_production_accepts_real_fernet_key(self, monkeypatch) -> None:
        _set_required_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("OTP_HMAC_SECRET", "a-real-deployment-secret")
        monkeypatch.setenv("TOKEN_SECRET", "a-real-access-token-secret-0123456789")
        monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())

        settings = Settings()

        assert settings.environment == "production"

    def test_non_fernet_totp_key_rejected_everywhere(self, monkeypatch) -> None:
        # A deployer pasting a passphrase (not a Fernet key) must fail at
        # settings load in any environment, not at the first login attempt.
        _set_required_env(monkeypatch)
        monkeypatch.setenv("TOTP_ENCRYPTION_KEY", "not-a-fernet-key")

        with pytest.raises(ValidationError, match="Fernet"):
            Settings()


def _code_at(secret: str, at: datetime) -> str:
    import pyotp

    return pyotp.TOTP(secret).at(at)
