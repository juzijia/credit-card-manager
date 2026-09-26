from datetime import datetime
from typing import Any

from pydantic import BaseModel, RootModel, field_validator


class NotificationSecretDTO(RootModel[dict[str, Any]]):
    """Provider configuration dictionary."""

    def reveal(self) -> dict[str, Any]:
        return dict(self.root)


class NotificationSettingWriteDTO(BaseModel):
    enabled: bool = True
    offset_days: list[int] = [2, 1, 0, -1, -2]
    push_time: str = "09:00"
    provider: str
    provider_config: NotificationSecretDTO = NotificationSecretDTO(root={})
    only_if_unpaid: bool = True

    @field_validator("offset_days")
    @classmethod
    def validate_offset_days(cls, value: list[int]) -> list[int]:
        if not value:
            return [2, 1, 0, -1, -2]
        if any(day < -30 or day > 30 for day in value):
            raise ValueError("offset_days must contain values from -30 to 30")
        return sorted(set(value), reverse=True)

    @field_validator("push_time")
    @classmethod
    def validate_push_time(cls, value: str) -> str:
        try:
            return datetime.strptime(value, "%H:%M").strftime("%H:%M")
        except ValueError as exc:
            raise ValueError("push_time must use HH:MM") from exc


class NotificationTestWriteDTO(BaseModel):
    provider: str
    provider_config: NotificationSecretDTO


class NotificationSettingReadDTO(BaseModel):
    enabled: bool
    offset_days: list[int]
    push_time: str
    provider: str
    saved_provider: str = "disabled"
    configured: bool
    chat_id_configured: bool
    only_if_unpaid: bool
    provider_config: dict[str, Any] = {}


class NotificationProviderReadDTO(BaseModel):
    id: str
    name: str


class NotificationSettingsReadDTO(BaseModel):
    setting: NotificationSettingReadDTO
    providers: list[NotificationProviderReadDTO]
