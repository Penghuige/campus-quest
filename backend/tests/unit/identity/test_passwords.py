# backend/tests/unit/identity/test_passwords.py
"""Unit tests for Argon2id password hashing and JWT access tokens.

Spec §5.6: passwords are hashed with Argon2id, no plaintext or reversible
ciphertext is stored, length band is 10-128 with no composition rules.
backend-engineering §15: neither passwords nor tokens ever reach a log line.

All JWT behavior is Clock-free by design: `AccessTokenCodec.encode` takes
``now`` as an argument and PyJWT checks ``exp`` against wall-clock time, so
expiry is exercised deterministically by encoding a token whose ``exp`` is
already in the past — no sleeping, no monkeypatched time.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt as pyjwt
import pytest

from app.core.security import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    AccessTokenClaims,
    AccessTokenCodec,
    AccessTokenExpiredError,
    AccessTokenInvalidError,
    hash_password,
    hash_refresh_token,
    verify_password,
)

_SECRET = "unit-test-access-token-secret-0123456789"  # >= 32 bytes (RFC 7518)
_USER_ID = uuid4()
_SESSION_ID = uuid4()
# PyJWT validates ``exp`` against wall-clock time, so encode inputs must be
# anchored to the real now: freshly minted tokens decode immediately, the
# stale one is deterministically in the past.
_NOW = datetime.now(UTC).replace(microsecond=0)


class TestPasswordHashing:
    def test_hash_is_argon2id_and_contains_no_plaintext(self) -> None:
        password = "correct-horse-battery"

        encoded = hash_password(password)

        assert encoded.startswith("$argon2id$")
        assert password not in encoded
        # Salt and digest make the encoded string unique per call, so two
        # hashes of the same password never collide (rainbow-table resistance
        # starts with a fresh salt per hash).
        assert encoded != hash_password(password)

    def test_verify_roundtrip(self) -> None:
        encoded = hash_password("correct-horse-battery")

        assert verify_password("correct-horse-battery", encoded) is True

    def test_wrong_password_fails(self) -> None:
        encoded = hash_password("correct-horse-battery")

        assert verify_password("correct-horse", encoded) is False

    def test_verify_against_foreign_hash_fails(self) -> None:
        # A digest from one password must not verify against another's hash
        # even when both are valid Argon2id encodings.
        assert (
            verify_password("correct-horse-battery", hash_password("staple-horse"))
            is False
        )

    @pytest.mark.parametrize(
        ("length", "accepted"),
        [(9, False), (10, True), (128, True), (129, False)],
    )
    def test_hash_length_bounds(self, length: int, accepted: bool) -> None:
        password = "a" * length

        if accepted:
            assert verify_password(password, hash_password(password)) is True
        else:
            with pytest.raises(ValueError, match="10-128"):
                hash_password(password)

    def test_verify_length_bounds_rejected_before_hashing(self) -> None:
        # The band is enforced on verify too: input that could never have
        # been hashed is policy-invalid, not merely wrong, and must fail
        # before paying for an Argon2 run (cheap DoS guard on overlong input).
        encoded = hash_password("correct-horse-battery")
        with pytest.raises(ValueError, match="10-128"):
            verify_password("a" * 9, encoded)
        with pytest.raises(ValueError, match="10-128"):
            verify_password("a" * 129, encoded)

    def test_bounds_constants_match_spec(self) -> None:
        # Spec §5.6: recommended length 10-128, no composition rules. The
        # constants are the single source of truth for hash and verify alike.
        assert (PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH) == (10, 128)

    def test_hashing_never_logs_password(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        password = "correct-horse-battery"
        with caplog.at_level(logging.DEBUG, logger="app.core.security"):
            encoded = hash_password(password)
            verify_password(password, encoded)

        for record in caplog.records:
            assert password not in record.getMessage()


class TestRefreshTokenHashing:
    def test_hash_is_sha256_hex_and_contains_no_token(self) -> None:
        token = "x" * 43

        digest = hash_refresh_token(token)

        assert len(digest) == 64
        int(digest, 16)  # hex digest
        assert token not in digest

    def test_hash_is_deterministic_for_lookup(self) -> None:
        # The digest is the UNIQUE-index lookup key, so it must be stable
        # across calls (unlike the salted password hash).
        token = "x" * 43
        assert hash_refresh_token(token) == hash_refresh_token(token)
        assert hash_refresh_token(token) != hash_refresh_token("y" * 43)


class TestAccessTokenCodec:
    def _codec(self, ttl_minutes: int = 15) -> AccessTokenCodec:
        return AccessTokenCodec(secret=_SECRET, ttl_minutes=ttl_minutes)

    def test_encode_decode_roundtrip_claims(self) -> None:
        token = self._codec().encode(
            user_id=_USER_ID, session_id=_SESSION_ID, role="STUDENT", now=_NOW
        )

        claims = self._codec().decode(token)

        assert isinstance(claims, AccessTokenClaims)
        assert claims.sub == str(_USER_ID)
        assert claims.sid == str(_SESSION_ID)
        assert claims.role == "STUDENT"
        assert claims.iat == int(_NOW.timestamp())
        assert claims.exp == int(_NOW.timestamp()) + 15 * 60
        assert claims.jti  # unique token id present

    def test_algorithm_is_hs256(self) -> None:
        # The token is three dot-separated base64 segments; the header
        # explicitly names HS256 (decode pins the same algorithm).
        token = self._codec().encode(
            user_id=_USER_ID, session_id=_SESSION_ID, role="STUDENT", now=_NOW
        )
        header = pyjwt.get_unverified_header(token)
        assert header["alg"] == "HS256"

    def test_jti_unique_per_token(self) -> None:
        codec = self._codec()
        claims = [
            codec.decode(
                codec.encode(
                    user_id=_USER_ID,
                    session_id=_SESSION_ID,
                    role="STUDENT",
                    now=_NOW,
                )
            )
            for _ in range(2)
        ]
        assert claims[0].jti != claims[1].jti

    def test_expired_token_raises_typed_error(self) -> None:
        # exp is checked by PyJWT against wall-clock time: encoding with a
        # ``now`` far in the past makes the token deterministically expired.
        stale = self._codec().encode(
            user_id=_USER_ID,
            session_id=_SESSION_ID,
            role="STUDENT",
            now=_NOW - timedelta(hours=2),
        )

        with pytest.raises(AccessTokenExpiredError):
            self._codec().decode(stale)

    def test_wrong_secret_raises_invalid(self) -> None:
        token = self._codec().encode(
            user_id=_USER_ID, session_id=_SESSION_ID, role="STUDENT", now=_NOW
        )

        with pytest.raises(AccessTokenInvalidError):
            AccessTokenCodec(
                secret="another-secret-also-thirty-two-bytes-long", ttl_minutes=15
            ).decode(token)

    def test_garbage_token_raises_invalid(self) -> None:
        with pytest.raises(AccessTokenInvalidError):
            self._codec().decode("not-a-jwt")

    def test_tampered_payload_raises_invalid(self) -> None:
        token = self._codec().encode(
            user_id=_USER_ID, session_id=_SESSION_ID, role="ADMIN", now=_NOW
        )
        # Role escalation attempt: re-encode the payload with role swapped;
        # the signature no longer matches (backend-engineering §16: never
        # trust client-supplied role).
        header, _payload, signature = token.split(".")
        claims = {
            "sub": str(_USER_ID),
            "sid": str(_SESSION_ID),
            "role": "ADMIN",
            "exp": int(_NOW.timestamp()) + 900,
            "iat": int(_NOW.timestamp()),
            "jti": "tampered",
        }
        forged_payload = (
            base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        )
        forged = f"{header}.{forged_payload}.{signature}"

        with pytest.raises(AccessTokenInvalidError):
            self._codec().decode(forged)

    def test_decode_never_logs_token_or_secret(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        token = self._codec().encode(
            user_id=_USER_ID, session_id=_SESSION_ID, role="STUDENT", now=_NOW
        )
        with (
            caplog.at_level(logging.DEBUG, logger="app.core.security"),
            pytest.raises(AccessTokenInvalidError),
        ):
            self._codec().decode("garbage")

        for record in caplog.records:
            assert token not in record.getMessage()
            assert _SECRET not in record.getMessage()
