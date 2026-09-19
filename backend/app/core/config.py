# backend/app/core/config.py
"""Typed deployment settings (docs/quality/backend-engineering.md §17).

Required deployment configuration is read from the environment and validated
at startup so a misconfigured deployment fails fast with a readable error.
"""

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# Development-only secrets (backend-engineering §17 fail-fast): all are
# KNOWN values committed to the repository, so production must override them.
# - OTP HMAC key: with it, anyone who can read Redis brute-forces the 10^6
#   code space offline instantly (spec §33.2).
# - Access-token signing key: with it, anyone can forge valid JWTs (§5.6).
# - TOTP encryption key: with it, anyone who can read the database decrypts
#   every stored staff TOTP secret (§5.6/§5.8).
# Production settings reject all three — see `_reject_insecure_secrets_in_production`.
_INSECURE_OTP_HMAC_SECRET = "dev-only-insecure-otp-hmac-secret"
_INSECURE_TOKEN_SECRET = "dev-only-insecure-access-token-secret"
# A Fernet key is 32 url-safe base64 bytes; this sentinel decodes to the
# ASCII marker "dev-only-insecure-totp-key------" so the committed value is
# both structurally valid and self-describing.
_INSECURE_TOTP_ENCRYPTION_KEY = "ZGV2LW9ubHktaW5zZWN1cmUtdG90cC1rZXktLS0tLS0="
_INSECURE_SECRET_SENTINELS = (
    _INSECURE_OTP_HMAC_SECRET,
    _INSECURE_TOKEN_SECRET,
    _INSECURE_TOTP_ENCRYPTION_KEY,
)

# (field, env var) pairs the production guard checks against their dev-only
# sentinel defaults; extend this table when a new committed-secret default
# lands.
_INSECURE_PRODUCTION_SENTINELS: tuple[tuple[str, str], ...] = (
    ("otp_hmac_secret", "OTP_HMAC_SECRET"),
    ("token_secret", "TOKEN_SECRET"),
    ("totp_encryption_key", "TOTP_ENCRYPTION_KEY"),
)


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    s3_endpoint_url: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    business_timezone: str
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    max_upload_bytes_default: int = 200 * 1024 * 1024

    # Deployment profile: "production" turns insecure development defaults
    # into startup failures (the OTP HMAC, access-token, and TOTP-encryption
    # sentinels).
    environment: Literal["development", "production"] = "development"

    # Phone OTP challenge lifecycle (spec §33.2 recommended defaults:
    # 5-minute TTL, 5 verification attempts, resend cooldown, per-phone and
    # per-IP hourly/daily request caps). Consumed by `OtpPolicy.from_settings`.
    otp_ttl_seconds: int = 300
    otp_max_verify_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60
    otp_verified_token_ttl_seconds: int = 600
    otp_phone_hourly_request_limit: int = 5
    otp_phone_daily_request_limit: int = 20
    otp_ip_hourly_request_limit: int = 50
    otp_ip_daily_request_limit: int = 200
    # HMAC key for at-rest OTP/token hashing in Redis (spec §33.2: never
    # plaintext). The default exists for local development only; production
    # deployments must set OTP_HMAC_SECRET and fail fast otherwise.
    otp_hmac_secret: str = _INSECURE_OTP_HMAC_SECRET
    # HS256 signing key for short-lived access tokens (spec §5.6). Same
    # deal: a committed development default that production refuses.
    token_secret: str = _INSECURE_TOKEN_SECRET
    # Fernet key encrypting staff TOTP secrets at rest (spec §5.6, §5.8):
    # `totp_credentials.secret_encrypted` must never hold plaintext. Same
    # sentinel pattern — development default, production refuses it.
    totp_encryption_key: str = _INSECURE_TOTP_ENCRYPTION_KEY
    # Staff invitation link lifetime (spec §5.8: 一次性、短时有效); 48h is
    # the plan's default window.
    staff_invitation_ttl_hours: int = 48
    # Region for parsing domestic phone input into E.164 (spec §5.4).
    phone_default_region: str = "CN"

    @field_validator("business_timezone")
    @classmethod
    def _validate_business_timezone(cls, value: str) -> str:
        # Fail fast on an invalid IANA name at settings load, not deep inside
        # a request that first needs a business-timezone period.
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"business_timezone must be a valid IANA timezone name, got {value!r}"
            ) from exc
        return value

    @field_validator("token_secret")
    @classmethod
    def _validate_token_secret_length(cls, value: str) -> str:
        # RFC 7518 §3.2: an HS256 key should be at least 32 bytes; PyJWT
        # warns on every encode/decode with a shorter one. A deployment
        # that sets a short TOKEN_SECRET must fail at settings load, not
        # spam warnings (or run weakly) at runtime.
        if len(value) < 32:
            raise ValueError(
                f"token_secret must be at least 32 characters for HS256 "
                f"(got {len(value)})"
            )
        return value

    @field_validator("totp_encryption_key")
    @classmethod
    def _validate_totp_encryption_key(cls, value: str) -> str:
        # Fernet accepts exactly 32 url-safe base64-encoded bytes; anything
        # else raises at first use. A deployer pasting a passphrase must
        # fail at settings load in any environment, not at the first staff
        # login attempt.
        try:
            Fernet(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "totp_encryption_key must be a valid Fernet key "
                "(32 url-safe base64-encoded bytes, e.g. "
                "Fernet.generate_key())"
            ) from exc
        return value

    @model_validator(mode="after")
    def _reject_insecure_secrets_in_production(self) -> "Settings":
        # A known sentinel defeats the secret's purpose in production (see
        # the constants' comments): a deployment must fail at startup, not
        # run silently unprotected. Every listed secret is checked so the
        # one error names everything the deployer must set.
        offenders = [
            env_name
            for field_name, env_name in _INSECURE_PRODUCTION_SENTINELS
            if getattr(self, field_name) in _INSECURE_SECRET_SENTINELS
        ]
        if self.environment == "production" and offenders:
            raise ValueError(
                "environment=production refuses development-only default "
                f"secrets: set {' and '.join(offenders)} to "
                "deployment-specific values (a committed sentinel lets "
                "anyone brute-force stored OTP hashes offline, forge "
                "access tokens, or decrypt stored TOTP secrets)"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
