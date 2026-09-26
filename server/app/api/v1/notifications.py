import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.plain_secrets import get_secret_json, set_secret_json
from app.core.redaction import redact_sensitive
from app.core.security import require_agent_access, require_owner_access
from app.db.session import get_session
from app.models.manual_bill import ManualBillNotificationLog
from app.models.notification import NotificationLog, NotificationSetting
from app.notifications.channels import PROVIDERS
from app.schemas.notification import (
    NotificationSettingWriteDTO,
    NotificationSettingsReadDTO,
    NotificationTestWriteDTO,
)
from app.services.notification_service import (
    assemble_provider_config,
    check_and_send_due_reminders,
    create_default_setting,
    get_default_setting,
    separate_provider_config,
    test_notification,
    validate_provider_config_endpoints,
)

router = APIRouter(
    prefix="/notifications",
    tags=["notifications"],
)
SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "/settings",
    response_model=NotificationSettingsReadDTO,
    dependencies=[Depends(require_agent_access)],
)
def get_settings_and_providers(session: SessionDep) -> dict[str, Any]:
    setting = get_default_setting(session)
    providers_list = [
        {"id": "disabled", "name": "已关闭"},
        *[{"id": k, "name": v["name"]} for k, v in PROVIDERS.items()],
    ]

    if not setting:
        return {
            "setting": {
                "enabled": False,
                "offset_days": [2, 1, 0, -1, -2],
                "push_time": "09:00",
                "provider": "disabled",
                "saved_provider": "disabled",
                "configured": False,
                "chat_id_configured": False,
                "only_if_unpaid": True,
            },
            "providers": providers_list,
        }

    try:
        offsets = json.loads(setting.offset_days_json)
    except Exception:
        offsets = [2, 1, 0, -1, -2]

    try:
        public_conf = json.loads(setting.public_config_json or "{}")
    except Exception:
        public_conf = {}
    secret_conf = get_secret_json("notification", setting.id) or {}
    combined_conf = {**public_conf, **secret_conf}
    configured = bool(combined_conf)

    is_enabled = bool(setting.enabled) and setting.provider != "disabled"
    current_provider = setting.provider if (is_enabled and setting.provider in PROVIDERS) else "disabled"

    return {
        "setting": {
            "enabled": is_enabled,
            "offset_days": offsets,
            "push_time": setting.push_time,
            "provider": current_provider,
            "saved_provider": setting.provider,
            "configured": configured,
            "chat_id_configured": bool(
                setting.provider == "telegram"
                and str(combined_conf.get("chat_id", "")).strip()
            ),
            "only_if_unpaid": setting.only_if_unpaid,
            "provider_config": combined_conf,
        },
        "providers": providers_list,
    }


@router.post("/settings", dependencies=[Depends(require_owner_access)])
def update_settings(
    payload: NotificationSettingWriteDTO, session: SessionDep
) -> dict[str, Any]:
    if payload.provider != "disabled" and payload.provider not in PROVIDERS:
        raise HTTPException(status_code=422, detail="Unsupported notification provider")
    setting = get_default_setting(session)
    if not setting:
        setting = create_default_setting(session)

    if payload.provider == "disabled" or not payload.enabled:
        setting.enabled = False
        if payload.provider != "disabled":
            setting.provider = payload.provider
        setting.push_time = payload.push_time
        setting.offset_days_json = json.dumps(payload.offset_days)
        setting.only_if_unpaid = payload.only_if_unpaid
        session.commit()
        session.refresh(setting)
        return {"success": True, "message": "通知已关闭"}

    setting.enabled = payload.enabled
    setting.offset_days_json = json.dumps(payload.offset_days)
    setting.push_time = payload.push_time
    setting.only_if_unpaid = payload.only_if_unpaid

    incoming = payload.provider_config.reveal()
    try:
        validate_provider_config_endpoints(payload.provider, incoming)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    previous_provider = setting.provider

    if previous_provider == payload.provider:
        assembled = assemble_provider_config(setting)
        for key, value in incoming.items():
            if value in (None, ""):
                assembled.pop(key, None)
            else:
                assembled[key] = value
    else:
        assembled = {k: v for k, v in incoming.items() if v not in (None, "")}

    public_conf, secret_conf = separate_provider_config(payload.provider, assembled)
    setting.provider = payload.provider
    setting.public_config_json = json.dumps(public_conf)

    session.commit()
    session.refresh(setting)

    if secret_conf:
        set_secret_json("notification", setting.id, secret_conf)
    else:
        set_secret_json("notification", setting.id, None)

    return {"success": True, "message": "通知设置已保存"}


@router.post("/test", dependencies=[Depends(require_owner_access)])
def test_provider(payload: NotificationTestWriteDTO, session: SessionDep) -> dict[str, Any]:
    if payload.provider == "disabled":
        return {"success": False, "message": "当前通知方式为已关闭，请先选择具体通知渠道"}
    setting = get_default_setting(session)

    if setting and setting.provider == payload.provider:
        assembled = assemble_provider_config(setting)
        for key, value in payload.provider_config.reveal().items():
            if value in (None, ""):
                assembled.pop(key, None)
            else:
                assembled[key] = value
    else:
        assembled = {k: v for k, v in payload.provider_config.reveal().items() if v not in (None, "")}
    try:
        validate_provider_config_endpoints(payload.provider, assembled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    success, msg = test_notification(payload.provider, assembled)
    return {"success": success, "message": msg}


@router.post("/trigger", dependencies=[Depends(require_agent_access)])
def trigger_check(session: SessionDep) -> dict[str, Any]:
    sent = check_and_send_due_reminders(session)
    return {
        "success": True,
        "sent_count": len(sent),
        "logs": [
            {
                "id": log.id,
                "statement_id": getattr(log, "statement_id", None),
                "manual_bill_payment_id": getattr(log, "manual_bill_payment_id", None),
                "offset_days": log.offset_days,
                "provider": log.provider,
                "status": log.status,
                "response": redact_sensitive(log.response_text or ""),
            }
            for log in sent
        ],
    }


@router.get("/logs", dependencies=[Depends(require_agent_access)])
def get_notification_logs(session: SessionDep, limit: int = 50) -> list[dict[str, Any]]:
    card_logs = session.scalars(
        select(NotificationLog).order_by(NotificationLog.sent_at.desc()).limit(limit)
    ).all()
    manual_logs = session.scalars(
        select(ManualBillNotificationLog)
        .order_by(ManualBillNotificationLog.sent_at.desc())
        .limit(limit)
    ).all()
    result = [
        {
            "kind": "credit_card",
            "id": log.id,
            "statement_id": log.statement_id,
            "manual_bill_payment_id": None,
            "offset_days": log.offset_days,
            "provider": log.provider,
            "status": log.status,
            "response": redact_sensitive(log.response_text or ""),
            "sent_at": log.sent_at.isoformat(),
        }
        for log in card_logs
    ]
    result.extend(
        {
            "kind": "manual_bill",
            "id": log.id,
            "statement_id": None,
            "manual_bill_payment_id": log.manual_bill_payment_id,
            "offset_days": log.offset_days,
            "provider": log.provider,
            "status": log.status,
            "response": redact_sensitive(log.response_text or ""),
            "sent_at": log.sent_at.isoformat(),
        }
        for log in manual_logs
    )
    result.sort(key=lambda item: item["sent_at"], reverse=True)
    return result[:limit]
