# backend/app/core/config.py
"""Typed deployment settings (docs/quality/backend-engineering.md §17).

Required deployment configuration is read from the environment and validated
at startup so a misconfigured deployment fails fast with a readable error.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
