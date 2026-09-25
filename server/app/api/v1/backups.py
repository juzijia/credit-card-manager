from typing import Annotated
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.plain_secrets import get_secret, set_secret
from app.core.redaction import redact_sensitive
from app.core.security import require_agent_access
from app.db.session import get_session
from app.models.backup import BackupSetting
from app.schemas.backup import (
    BackupCleanupResponse,
    BackupItem,
    BackupRestoreResponse,
    BackupRunResponse,
    BackupSettingsRead,
    BackupSettingsUpdate,
    BackupTestResponse,
)
from app.services.backup import (
    BackupBusyError,
    cleanup_backups,
    delete_backup,
    execute_backup,
    get_backup_setting,
    list_backups,
    next_backup_run,
    prepare_pending_restore,
    test_webdav_connection,
)

router = APIRouter(prefix="/backups", tags=["backups"])
SessionDep = Annotated[Session, Depends(get_session)]


BACKUP_SECRET_NS = "backup"


def _setting_or_defaults(session: Session) -> BackupSetting:
    return get_backup_setting(session, create=False) or BackupSetting(id="default")


def _webdav_url_has_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.username is not None or parsed.password is not None
    except ValueError:
        return True


def _to_read_dto(setting: BackupSetting, request: Request) -> BackupSettingsRead:
    timezone = request.app.state.settings.timezone
    last_backup_at = setting.last_remote_backup_at
    if last_backup_at and last_backup_at.tzinfo is None:
        last_backup_at = last_backup_at.replace(tzinfo=ZoneInfo(timezone))
    return BackupSettingsRead(
        backup_password_set=get_secret(BACKUP_SECRET_NS, "crypt_password") is not None,
        webdav_url=(
            ""
            if setting.webdav_url and _webdav_url_has_credentials(setting.webdav_url)
            else setting.webdav_url or ""
        ),
        webdav_username=setting.webdav_username or "",
        webdav_password_set=get_secret(BACKUP_SECRET_NS, "webdav_password") is not None,
        schedule_type=setting.schedule_type or "off",
        cron_expr=setting.cron_expr or "0 3 * * *",
        retention_count=(
            setting.retention_count if setting.retention_count is not None else 14
        ),
        connection_verified=setting.webdav_verified_at is not None,
        next_run_at=next_backup_run(setting, timezone),
        last_backup_at=last_backup_at.isoformat() if last_backup_at else None,
        last_backup_status=setting.last_remote_status,
        last_error=setting.last_error,
    )


def _http_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT
        if isinstance(exc, BackupBusyError)
        else status.HTTP_400_BAD_REQUEST,
        detail=redact_sensitive(exc),
    )


@router.get(
    "/settings",
    response_model=BackupSettingsRead,
    dependencies=[Depends(require_agent_access)],
)
def get_settings_endpoint(request: Request, session: SessionDep) -> BackupSettingsRead:
    return _to_read_dto(_setting_or_defaults(session), request)


@router.put(
    "/settings",
    response_model=BackupSettingsRead,
    dependencies=[Depends(require_agent_access)],
)
def update_settings_endpoint(
    payload: BackupSettingsUpdate,
    request: Request,
    session: SessionDep,
) -> BackupSettingsRead:
    setting = get_backup_setting(session)
    assert setting is not None
    backup_password_changed = bool(
        payload.backup_password is not None
        and payload.backup_password.get_secret_value().strip()
    )
    webdav_password_changed = bool(
        payload.webdav_password is not None
        and payload.webdav_password.get_secret_value().strip()
    )
    webdav_url = payload.webdav_url.strip() if payload.webdav_url is not None else None
    if webdav_url and _webdav_url_has_credentials(webdav_url):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="WebDAV 地址不能包含用户名或密码，请使用独立凭据字段",
        )
    webdav_username = (
        payload.webdav_username.strip() if payload.webdav_username is not None else None
    )
    connection_changed = (
        backup_password_changed
        or webdav_password_changed
        or (webdav_url is not None and webdav_url != (setting.webdav_url or ""))
        or (
            webdav_username is not None
            and webdav_username != (setting.webdav_username or "")
        )
    )

    if webdav_url is not None:
        setting.webdav_url = webdav_url
    if webdav_username is not None:
        setting.webdav_username = webdav_username
    if payload.cron_expr is not None:
        setting.cron_expr = payload.cron_expr.strip()
    if payload.retention_count is not None:
        setting.retention_count = payload.retention_count
    if payload.schedule_type is not None:
        setting.schedule_type = payload.schedule_type
    if connection_changed:
        setting.webdav_verified_at = None

    if payload.cron_expr is not None and (
        len(setting.cron_expr.split()) != 5 or not croniter.is_valid(setting.cron_expr)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cron 表达式无效，请输入标准 5 段格式（如 0 3 * * *）",
        )
    if setting.schedule_type == "cron":
        if not (backup_password_changed or get_secret(BACKUP_SECRET_NS, "crypt_password")) or not (setting.webdav_url or "").strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="启用 Cron 前必须设置备份密码和 WebDAV 地址",
            )

    if backup_password_changed:
        set_secret(BACKUP_SECRET_NS, "crypt_password", payload.backup_password.get_secret_value())
    if webdav_password_changed:
        set_secret(BACKUP_SECRET_NS, "webdav_password", payload.webdav_password.get_secret_value())
    session.commit()
    session.refresh(setting)
    return _to_read_dto(setting, request)


@router.post(
    "/test",
    response_model=BackupTestResponse,
    dependencies=[Depends(require_agent_access)],
)
def test_backup_endpoint(request: Request, session: SessionDep) -> BackupTestResponse:
    try:
        return BackupTestResponse(
            **test_webdav_connection(session, request.app.state.settings)
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post(
    "/run",
    response_model=BackupRunResponse,
    dependencies=[Depends(require_agent_access)],
)
def run_backup_endpoint(request: Request, session: SessionDep) -> BackupRunResponse:
    try:
        return BackupRunResponse(**execute_backup(session, request.app.state.settings))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get(
    "",
    response_model=list[BackupItem],
    dependencies=[Depends(require_agent_access)],
)
def list_backups_endpoint(request: Request, session: SessionDep) -> list[BackupItem]:
    try:
        return [
            BackupItem(**item)
            for item in list_backups(session, request.app.state.settings)
        ]
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post(
    "/cleanup",
    response_model=BackupCleanupResponse,
    dependencies=[Depends(require_agent_access)],
)
def cleanup_backups_endpoint(
    request: Request, session: SessionDep
) -> BackupCleanupResponse:
    try:
        return BackupCleanupResponse(
            **cleanup_backups(session, request.app.state.settings)
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.delete(
    "/{backup_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_agent_access)],
)
def delete_backup_endpoint(
    backup_name: str, request: Request, session: SessionDep
) -> Response:
    try:
        delete_backup(session, request.app.state.settings, backup_name)
    except Exception as exc:
        raise _http_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{backup_name}/restore",
    response_model=BackupRestoreResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_agent_access)],
)
def restore_backup_endpoint(
    backup_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    session: SessionDep,
) -> BackupRestoreResponse:
    try:
        result = prepare_pending_restore(
            session, request.app.state.settings, backup_name
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    background_tasks.add_task(request.app.state.graceful_shutdown)
    return BackupRestoreResponse(**result)
