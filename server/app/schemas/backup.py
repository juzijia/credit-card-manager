from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class BackupSettingsRead(BaseModel):
    backup_password_set: bool
    backup_password: str = ""
    webdav_url: str
    webdav_username: str
    webdav_password_set: bool
    webdav_password: str = ""
    schedule_type: Literal["off", "cron"]
    cron_expr: str
    retention_count: int
    connection_verified: bool
    next_run_at: str | None
    last_backup_at: str | None
    last_backup_status: str | None
    last_error: str | None


class BackupSettingsUpdate(BaseModel):
    backup_password: SecretStr | None = None
    webdav_url: str | None = None
    webdav_username: str | None = None
    webdav_password: SecretStr | None = None
    schedule_type: Literal["off", "cron"] | None = None
    cron_expr: str | None = None
    retention_count: int | None = Field(default=None, ge=1, le=365)


class BackupRunResponse(BaseModel):
    filename: str
    status: str
    warning: str | None = None


class BackupTestResponse(BaseModel):
    success: bool
    message: str


class BackupItem(BaseModel):
    name: str
    backup_time: str
    size: int
    mod_time: str | None


class BackupCleanupResponse(BaseModel):
    kept: int
    deleted: int
    failed: int


class BackupRestoreResponse(BaseModel):
    status: str
    backup_name: str
