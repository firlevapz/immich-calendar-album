"""Configuration loaded from environment variables via pydantic-settings."""

from __future__ import annotations

from pydantic import AnyHttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Immich ---
    immich_url: AnyHttpUrl
    immich_api_key: str

    # --- CalDAV ---
    caldav_url: AnyHttpUrl
    caldav_username: str = ""
    caldav_password: str = ""
    caldav_api_key: str = ""

    # --- Behaviour ---
    lookback_days: int = 7
    schedule_interval: int = 3600  # seconds between runs

    # --- Timezone ---
    # Honour the standard TZ environment variable; default to UTC.
    tz: str = "UTC"

    @field_validator("lookback_days", "schedule_interval")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v

    @model_validator(mode="after")
    def caldav_credentials_consistent(self) -> "Config":
        has_userpass = bool(self.caldav_username and self.caldav_password)
        has_apikey = bool(self.caldav_api_key)
        if self.caldav_username and not self.caldav_password and not has_apikey:
            raise ValueError(
                "CALDAV_USERNAME is set but CALDAV_PASSWORD is missing"
            )
        if self.caldav_password and not self.caldav_username and not has_apikey:
            raise ValueError(
                "CALDAV_PASSWORD is set but CALDAV_USERNAME is missing"
            )
        return self

    @property
    def caldav_write_enabled(self) -> bool:
        """True when credentials are sufficient to write back to the CalDAV server."""
        return bool(
            (self.caldav_username and self.caldav_password) or self.caldav_api_key
        )

    @property
    def immich_base_url(self) -> str:
        return str(self.immich_url).rstrip("/")

    @property
    def caldav_base_url(self) -> str:
        return str(self.caldav_url).rstrip("/")
