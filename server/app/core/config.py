from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CCM_",
        # Resolve this next to the server package rather than against the
        # process working directory. The desktop launcher and pytest use
        # different working directories.
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Private Credit Card Manager"
    database_url: str = "sqlite:///./data/credit-card-manager.db"
    env: Literal["development", "production", "test"] = "development"
    auth_mode: Literal["session", "dev_bypass", "token", "local"] = "dev_bypass"
    owner_token: SecretStr | None = None
    admin_token: SecretStr | None = None
    session_idle_seconds: int = 86400  # 24 hours
    session_absolute_seconds: int = 2592000  # 30 days
    timezone: str = "Asia/Shanghai"
    allowed_host: str | None = None
    error_log_path: str | None = "./data/logs/errors.log"
    background_tasks_enabled: bool = True

    @model_validator(mode="after")
    def validate_auth_configuration(self) -> "Settings":
        if self.auth_mode in ("dev_bypass", "local") and self.env == "production":
            raise ValueError(
                f"auth_mode={self.auth_mode} is forbidden in production environment. "
                "Set auth_mode=session with CCM_OWNER_TOKEN or auth_mode=token "
                "with CCM_ADMIN_TOKEN."
            )

        if self.admin_token is not None:
            token = self.admin_token.get_secret_value()
            if len(token) < 32:
                raise ValueError(
                    "CCM_ADMIN_TOKEN must contain at least 32 characters when configured"
                )
        elif self.auth_mode == "token":
            raise ValueError(
                "CCM_ADMIN_TOKEN must contain at least 32 characters in token mode"
            )

        if self.auth_mode == "session":
            token = self.owner_token.get_secret_value() if self.owner_token else ""
            # Runtime cannot prove entropy from a string. The deployment contract
            # requires a token generated from 32 random bytes, e.g.
            # secrets.token_urlsafe(32), which is normally 43 URL-safe chars.
            if len(token) < 43:
                raise ValueError(
                    "CCM_OWNER_TOKEN must be a high-entropy token generated from at least "
                    "32 random bytes (for example secrets.token_urlsafe(32), typically 43+ chars)"
                )

        if self.env == "production":
            self.allowed_host = (self.allowed_host or "").strip() or None
            if self.allowed_host and "*" in self.allowed_host:
                raise ValueError(
                    "CCM_ALLOWED_HOST must be one exact hostname without wildcards"
                )
        return self

@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
