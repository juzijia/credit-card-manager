import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import SecretStr, ValidationError

from app.core.plain_secrets import get_secret, set_secret
from app.core.redaction import redact_sensitive
from app.core.security import AccessContext, require_agent_access
from app.db.session import get_session
from app.integrations.imap_sync import ImapRuntimeConfig, sync_mailbox
from app.models.email_account import EmailAccount
from app.models.imap_sync_run import ImapSyncRun
from app.schemas.imap import (
    ImapConfigUpdateDTO,
    ImapConfigWriteDTO,
    ImapStatusReadDTO,
    ImapTestWriteDTO,
)
from croniter import croniter
import re

router = APIRouter(prefix="/imap", tags=["imap"])
SessionDep = Annotated[Session, Depends(get_session)]

DEFAULT_ACCOUNT_NAME = "default"
AGENT_IMAP_SCHEDULE_FIELDS = frozenset(
    {"enabled", "schedule_type", "poll_seconds", "daily_time", "cron_expr"}
)


def get_default_email_account(session: Session) -> EmailAccount | None:
    account = session.scalar(select(EmailAccount).where(EmailAccount.name == DEFAULT_ACCOUNT_NAME))
    if account is None:
        account = session.scalar(select(EmailAccount).order_by(EmailAccount.created_at))
    return account


def create_or_update_default_email_account(
    session: Session,
    payload: ImapConfigWriteDTO,
) -> EmailAccount:
    account = get_default_email_account(session)
    username = payload.username.strip() if payload.username and payload.username.strip() else (account.username if account else "")
    if account is None:
        account = EmailAccount(
            name=DEFAULT_ACCOUNT_NAME,
            host=payload.host.strip(),
            port=payload.port,
            username=username,
            enabled=payload.enabled,
            mailbox=payload.mailbox,
            all_folders=payload.all_folders,
            schedule_type=payload.schedule_type,
            poll_seconds=payload.poll_seconds,
            daily_time=payload.daily_time.strip(),
            cron_expr=payload.cron_expr.strip(),
            initial_sync_months=payload.initial_sync_months,
        )
        session.add(account)
        session.flush()
    else:
        account.enabled = payload.enabled
        account.host = payload.host.strip()
        account.port = payload.port
        if username:
            account.username = username
        account.mailbox = payload.mailbox
        account.all_folders = payload.all_folders
        account.schedule_type = payload.schedule_type
        account.poll_seconds = payload.poll_seconds
        account.daily_time = payload.daily_time.strip()
        account.cron_expr = payload.cron_expr.strip()
        account.initial_sync_months = payload.initial_sync_months

    if payload.password is not None:
        set_secret("imap", account.id, payload.password.get_secret_value() or None)

    session.commit()
    session.refresh(account)
    return account


def build_imap_runtime_config(
    account: EmailAccount | None,
) -> ImapRuntimeConfig | None:
    if not account or not account.enabled:
        return None
    if not account.host or not account.username:
        return None
    password = get_secret("imap", account.id)
    if not password:
        return None
    return ImapRuntimeConfig(
        account_id=account.id,
        host=account.host,
        port=account.port,
        username=account.username,
        password=SecretStr(password),
        mailbox=account.mailbox,
        all_folders=account.all_folders,
        initial_sync_months=account.initial_sync_months,
    )


def _status_payload(account: EmailAccount | None) -> dict[str, object]:
    if account is None:
        return {
            "enabled": False,
            "configured": False,
            "host": None,
            "port": 993,
            "username_configured": False,
            "username": None,
            "mailbox": "INBOX",
            "all_folders": True,
            "schedule_type": "interval",
            "poll_seconds": 1800,
            "daily_time": "08:00",
            "cron_expr": "0 8 * * *",
            "initial_sync_months": 12,
        }

    return {
        "enabled": account.enabled,
        "configured": bool(
            account.enabled
            and account.host
            and account.username
            and get_secret("imap", account.id)
        ),
        "host": account.host,
        "port": account.port,
        "username_configured": bool(account.username),
        "username": account.username,
        "password": get_secret("imap", account.id) or "",
        "mailbox": account.mailbox,
        "all_folders": account.all_folders,
        "schedule_type": account.schedule_type,
        "poll_seconds": account.poll_seconds,
        "daily_time": account.daily_time,
        "cron_expr": account.cron_expr,
        "initial_sync_months": account.initial_sync_months,
    }


def validate_imap_schedule(
    schedule_type: str, daily_time: str, cron_expr: str
) -> None:
    if schedule_type == "cron":
        if not croniter.is_valid(cron_expr.strip()):
            raise HTTPException(
                status_code=400,
                detail="Cron 表达式格式无效，请输入标准 5 段格式（如 0 8 * * *）",
            )
    elif schedule_type == "daily" and not re.match(
        r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$", daily_time.strip()
    ):
        raise HTTPException(
            status_code=400,
            detail="按天定时时间格式无效，请输入 HH:MM（如 08:00）",
        )


@router.get(
    "/status",
    response_model=ImapStatusReadDTO,
    dependencies=[Depends(require_agent_access)],
)
def imap_status(session: SessionDep) -> dict[str, object]:
    account = get_default_email_account(session)
    return _status_payload(account)


@router.post("/sync")
def imap_sync(
    session: SessionDep,
    access: Annotated[AccessContext, Depends(require_agent_access)],
    full_scan: bool = Query(default=False),
    months: int | None = Query(default=None, ge=1, le=24),
) -> dict[str, object]:
    if access.role == "agent" and (full_scan or months is not None):
        raise HTTPException(
            status_code=403,
            detail="Agent token may only run the default incremental IMAP sync",
        )
    account = get_default_email_account(session)
    runtime_config = build_imap_runtime_config(account)

    if runtime_config is None:
        raise HTTPException(status_code=400, detail="IMAP is not enabled or credentials are not configured in EmailAccount")

    try:
        return sync_mailbox(
            session,
            runtime_config,
            full_scan=full_scan,
            lookback_months=months,
            trigger="api",
        ).to_dict()
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=503, detail=redact_sensitive(exc)) from exc


@router.get("/runs", dependencies=[Depends(require_agent_access)])
def imap_sync_runs(
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, object]]:
    runs = session.scalars(
        select(ImapSyncRun).order_by(ImapSyncRun.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": run.id,
            "trigger": run.trigger,
            "mode": run.mode,
            "lookback_months": run.lookback_months,
            "status": run.status,
            "started_at": run.created_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "duration_ms": run.duration_ms,
            "scanned_folders": run.scanned_folders,
            "failed_folders": run.failed_folders,
            "scanned": run.scanned,
            "candidates": run.candidates,
            "complete_statements": run.complete_statements,
            "duplicate_statements": run.duplicate_statements,
            "parsed_batches": run.parsed_batches,
            "duplicate_batches": run.duplicate_batches,
            "failed_candidates": run.failed_candidates,
            "failure_reasons": json.loads(run.failure_reasons),
        }
        for run in runs
    ]


@router.post("/test", dependencies=[Depends(require_agent_access)])
def test_imap_connection(payload: ImapTestWriteDTO, session: SessionDep) -> dict[str, object]:
    from imapclient import IMAPClient
    account = get_default_email_account(session)

    password = payload.password.get_secret_value() if payload.password else None
    if not password and account:
        password = get_secret("imap", account.id)

    username = payload.username.strip() if payload.username and payload.username.strip() else (account.username if account else "")
    if not username:
        return {
            "success": False,
            "message": "请填写邮箱账户 (Username)",
            "folders_count": 0,
        }

    if not password:
        return {
            "success": False,
            "message": "请先输入 IMAP 授权码并保存配置",
            "folders_count": 0,
        }
    try:
        with IMAPClient(payload.host, payload.port, ssl=True, timeout=12) as client:
            client.login(username, password)
            if b"ID" in client.capabilities():
                client.id_({"name": "Private Credit Card Manager", "version": "0.1.0", "vendor": "local"})
            folders = client.list_folders()
            return {
                "success": True,
                "message": f"连接成功！成功登录并发现 {len(folders)} 个文件夹",
                "folders_count": len(folders),
            }
    except Exception as exc:
        return {
            "success": False,
            "message": f"连接失败: {redact_sensitive(exc)}",
            "folders_count": 0,
        }


@router.post("/config")
def update_imap_config(
    payload: ImapConfigUpdateDTO,
    session: SessionDep,
    access: Annotated[AccessContext, Depends(require_agent_access)],
) -> dict[str, object]:
    submitted_fields = payload.model_fields_set
    if access.role == "agent":
        if submitted_fields - AGENT_IMAP_SCHEDULE_FIELDS:
            raise HTTPException(
                status_code=403,
                detail="Agent token may only modify IMAP scheduling fields",
            )
        if not submitted_fields:
            raise HTTPException(status_code=422, detail="No scheduling fields supplied")
        if any(getattr(payload, name) is None for name in submitted_fields):
            raise HTTPException(
                status_code=422,
                detail="IMAP scheduling fields cannot be null",
            )

        account = get_default_email_account(session)
        if account is None:
            raise HTTPException(
                status_code=409,
                detail="Owner must configure IMAP before an Agent changes its schedule",
            )
        for name in submitted_fields:
            value = getattr(payload, name)
            setattr(account, name, value.strip() if isinstance(value, str) else value)
        validate_imap_schedule(
            account.schedule_type, account.daily_time, account.cron_expr
        )
        session.commit()
        session.refresh(account)
        message = "IMAP 同步计划已更新"
    else:
        try:
            owner_payload = ImapConfigWriteDTO.model_validate(
                payload.model_dump(exclude_unset=True)
            )
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        validate_imap_schedule(
            owner_payload.schedule_type,
            owner_payload.daily_time,
            owner_payload.cron_expr,
        )
        account = create_or_update_default_email_account(session, owner_payload)
        message = "IMAP 配置已更新"

    return {
        "success": True,
        "message": message,
        "status": _status_payload(account),
    }
