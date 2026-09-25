from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ImapSecretDTO(BaseModel):
    """Write-only IMAP credential fields."""

    password: SecretStr | None = None


class ImapTestWriteDTO(ImapSecretDTO):
    host: str
    port: int = 993
    username: str


class ImapConfigWriteDTO(ImapSecretDTO):
    enabled: bool = True
    host: str
    port: int = 993
    username: str
    mailbox: str = "INBOX"
    all_folders: bool = True
    schedule_type: str = "interval"
    poll_seconds: int = 1800
    daily_time: str = "08:00"
    cron_expr: str = "0 8 * * *"
    initial_sync_months: int = Field(default=12, ge=1, le=24)


class ImapConfigUpdateDTO(ImapSecretDTO):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    mailbox: str | None = None
    all_folders: bool | None = None
    schedule_type: str | None = None
    poll_seconds: int | None = Field(default=None, ge=60)
    daily_time: str | None = None
    cron_expr: str | None = None
    initial_sync_months: int | None = Field(default=None, ge=1, le=24)


class ImapStatusReadDTO(BaseModel):
    enabled: bool
    configured: bool
    host: str | None
    port: int
    username_configured: bool
    username: str | None = None
    mailbox: str
    all_folders: bool
    schedule_type: str
    poll_seconds: int
    daily_time: str
    cron_expr: str
    initial_sync_months: int
