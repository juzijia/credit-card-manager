from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
import logging
import os
from pathlib import Path
import signal

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.formparsers import MultiPartException
import ipaddress
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.v1.imap import build_imap_runtime_config, get_default_email_account
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_error_log
from app.db.session import create_database_engine, create_session_factory
from app.integrations.imap_sync import sync_mailbox
from app.models.email_account import EmailAccount
from app.parsers.eml import MAX_EML_BYTES
from app.services.backup import backup_due, execute_backup, get_backup_setting
from app.services.catalog_service import ensure_bank_registries, ensure_catalog_products
from app.services.system_settings import ensure_history_cutoff_date

logger = logging.getLogger(__name__)

IMPORT_REQUEST_BODY_LIMIT = MAX_EML_BYTES + 64 * 1024
_IMPORT_UPLOAD_PATHS = frozenset(
    {
        "/api/v1/imports/inspect",
        "/api/v1/imports/eml",
        "/api/v1/imports/file",
    }
)


class _RequestBodyTooLarge(MultiPartException):
    pass


class RequestBodyLimitMiddleware:
    def __init__(
        self, app: ASGIApp, *, paths: frozenset[str], max_body_bytes: int
    ) -> None:
        self.app = app
        self.paths = paths
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        path = scope.get("path", "")

        # Range request mitigation for StaticFiles / FileResponse (GHSA-7f5h-v6xp-fcq8)
        if (
            path in {"/", "/favicon.ico", "/static"}
            or path.startswith("/static/")
        ) and "range" in headers:
            response = PlainTextResponse("Range requests are not supported", status_code=416)
            await response(scope, receive, send)
            return

        # URL-encoded form parsing mitigation (GHSA-82w8-qh3p-5jfq)
        content_type = headers.get("content-type", "").lower()
        if content_type.startswith("application/x-www-form-urlencoded"):
            response = JSONResponse(
                status_code=415,
                content={"detail": "application/x-www-form-urlencoded is not supported"},
            )
            await response(scope, receive, send)
            return

        if path not in self.paths:
            await self.app(scope, receive, send)
            return

        try:
            content_length = int(dict(scope.get("headers", ())).get(b"content-length", b""))
        except ValueError:
            content_length = 0
        if content_length > self.max_body_bytes:
            await JSONResponse(
                status_code=413, content={"detail": "Request body too large"}
            )(scope, receive, send)
            return

        received = 0
        too_large = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    too_large = True
                    raise _RequestBodyTooLarge("Request body too large")
            return message

        async def limited_send(message: Message) -> None:
            if not too_large:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _RequestBodyTooLarge:
            pass
        if too_large:
            await JSONResponse(
                status_code=413, content={"detail": "Request body too large"}
            )(scope, receive, send)


class CustomTrustedHostMiddleware:
    def __init__(self, app: ASGIApp, allowed_host: str | None = None) -> None:
        self.app = app
        self.allowed_host = allowed_host.lower().strip() if allowed_host else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host_header = headers.get("host", "").split(":")[0].strip().lower()
        if not host_header:
            response = PlainTextResponse("Invalid host header", status_code=400)
            await response(scope, receive, send)
            return

        if host_header in ("localhost", "127.0.0.1", "::1", "testserver") or host_header.endswith(".local"):
            await self.app(scope, receive, send)
            return

        if self.allowed_host and host_header == self.allowed_host:
            await self.app(scope, receive, send)
            return

        try:
            ip = ipaddress.ip_address(host_header)
            if ip.is_private or ip.is_loopback:
                await self.app(scope, receive, send)
                return
        except ValueError:
            pass

        response = PlainTextResponse("Invalid host header", status_code=400)
        await response(scope, receive, send)


def notification_run_due(
    now: datetime, push_time: str, last_run_date: date | None
) -> bool:
    try:
        hour_text, minute_text = push_time.split(":", 1)
        target = now.replace(
            hour=int(hour_text),
            minute=int(minute_text),
            second=0,
            microsecond=0,
        )
    except (TypeError, ValueError):
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    return last_run_date != now.date() and now >= target


def request_graceful_shutdown() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


async def run_in_thread_orderly(function, *args, **kwargs):
    """Wait for an in-flight worker before propagating task cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise


def compute_next_imap_delay(account: EmailAccount | None, timezone: str) -> float:
    if account is None:
        return 300.0
    stype = account.schedule_type
    now = datetime.now(ZoneInfo(timezone))
    if stype == "daily":
        try:
            hour_str, min_str = account.daily_time.split(":")
            target = now.replace(hour=int(hour_str), minute=int(min_str), second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return max(5.0, (target - now).total_seconds())
        except Exception:
            logger.exception("Failed to parse imap daily_time '%s', fallback to 900s", account.daily_time)
            return 900.0
    elif stype == "cron":
        try:
            from croniter import croniter
            c = croniter(account.cron_expr, now)
            next_time = c.get_next(datetime)
            return max(5.0, (next_time - now).total_seconds())
        except Exception:
            logger.exception("Failed to compute next cron time for '%s', fallback to 900s", account.cron_expr)
            return 900.0
    else:
        return max(60.0, float(account.poll_seconds))


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_error_log(resolved_settings.error_log_path)
    engine = create_database_engine(resolved_settings.database_url)
    session_factory = create_session_factory(engine, resolved_settings.timezone)

    async def imap_loop() -> None:
        while True:
            delay = 300.0
            try:
                with session_factory() as session:
                    account = get_default_email_account(session)
                    delay = compute_next_imap_delay(account, resolved_settings.timezone)
                    runtime_config = build_imap_runtime_config(account)
                    if runtime_config is not None:
                        await run_in_thread_orderly(
                            sync_mailbox,
                            session,
                            runtime_config,
                            trigger="scheduled",
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Scheduled IMAP synchronization failed (%s)", type(exc).__name__
                )
            await asyncio.sleep(delay)

    async def reminder_loop() -> None:
        from app.services.notification_service import (
            check_and_send_due_reminders,
            get_default_setting,
        )
        from app.services.manual_bills import ensure_current_payments

        last_run_date: date | None = None
        while True:
            try:
                with session_factory() as session:
                    now = datetime.now(ZoneInfo(resolved_settings.timezone))
                    ensure_current_payments(session, now.date())
                    session.commit()
                    setting = get_default_setting(session)
                    if setting and setting.enabled and notification_run_due(
                        now, setting.push_time, last_run_date
                    ):
                        logs = await run_in_thread_orderly(
                            check_and_send_due_reminders,
                            session,
                        )
                        if not logs or all(log.status == "success" for log in logs):
                            last_run_date = now.date()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Scheduled notification reminder check failed (%s)",
                    type(exc).__name__,
                )
            await asyncio.sleep(300)

    async def backup_loop() -> None:
        while True:
            try:
                with session_factory() as session:
                    setting = get_backup_setting(session, create=False)
                    now = datetime.now(ZoneInfo(resolved_settings.timezone))
                    if backup_due(setting, now, resolved_settings.timezone):
                        await run_in_thread_orderly(
                            execute_backup,
                            session,
                            resolved_settings,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Scheduled backup failed (%s)", type(exc).__name__)
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        with session_factory() as session:
            ensure_history_cutoff_date(session)
            ensure_bank_registries(session)
            ensure_catalog_products(session)
        background_enabled = resolved_settings.background_tasks_enabled
        imap_task = asyncio.create_task(imap_loop()) if background_enabled else None
        reminder_task = asyncio.create_task(reminder_loop()) if background_enabled else None
        backup_task = asyncio.create_task(backup_loop()) if background_enabled else None
        try:
            yield
        finally:
            if imap_task:
                imap_task.cancel()
                try:
                    await imap_task
                except asyncio.CancelledError:
                    pass
            if reminder_task:
                reminder_task.cancel()
                try:
                    await reminder_task
                except asyncio.CancelledError:
                    pass
            if backup_task:
                backup_task.cancel()
                try:
                    await backup_task
                except asyncio.CancelledError:
                    pass
            engine.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        paths=_IMPORT_UPLOAD_PATHS,
        max_body_bytes=IMPORT_REQUEST_BODY_LIMIT,
    )
    if resolved_settings.env == "production":
        app.add_middleware(
            CustomTrustedHostMiddleware,
            allowed_host=resolved_settings.allowed_host,
        )
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.graceful_shutdown = request_graceful_shutdown

    web_dir = Path(__file__).resolve().parents[2] / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(
                web_dir / "index.html",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

        @app.get("/favicon.ico", include_in_schema=False)
        def favicon() -> FileResponse:
            return FileResponse(web_dir / "favicon.svg", media_type="image/svg+xml")

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(Exception)
    async def log_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled request error: %s %s (%s)",
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        return JSONResponse(status_code=500, content={"detail": "服务内部错误，请查看错误日志"})

    app.include_router(api_router)
    return app


app = create_app()
