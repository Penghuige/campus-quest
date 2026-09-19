# backend/app/core/config.py
"""Typed deployment settings (docs/quality/backend-engineering.md §17).

Required deployment configuration is read from the environment and validated
at startup so a misconfigured deployment fails fast with a readable error.
"""

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# Development-only OTP HMAC key (spec §33.2: OTP is hashed at rest; with a
# KNOWN key anyone who can read Redis brute-forces the 10^6 code space
# offline instantly). Production settings reject it — see
# `_reject_insecure_otp_secret_in_production`.
_INSECURE_OTP_HMAC_SECRET = "dev-only-insecure-otp-hmac-secret"


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
    # into startup failures (currently the OTP HMAC sentinel below).
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

    @model_validator(mode="after")
    def _reject_insecure_otp_secret_in_production(self) -> "Settings":
        # The known sentinel defeats at-rest OTP hashing for anyone who can
        # read Redis (they hold the salt AND the HMAC key, so the 10^6 code
        # space falls to offline brute force — spec §33.2). A production
        # deployment must fail at startup, not run silently unprotected.
        if (
            self.environment == "production"
            and self.otp_hmac_secret == _INSECURE_OTP_HMAC_SECRET
        ):
            raise ValueError(
                "environment=production refuses the development-only default "
                "otp_hmac_secret: set OTP_HMAC_SECRET to a deployment-specific "
                "secret so stored OTP hashes cannot be brute-forced offline"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
