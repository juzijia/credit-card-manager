from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.redaction import redact_sensitive
from app.integrations.bean_sieve import bank_detection_terms, bank_providers
from app.models.imap_state import ImapState
from app.models.imap_sync_run import ImapSyncRun
from app.models.mixins import utc_now
from app.parsers import default_registry, parse_eml_bytes
from app.parsers.base import ParserError
from app.parsers.eml import MAX_EML_BYTES
from app.services.eml_import import ImportConflict, import_eml_data
from app.services.parsed_batches import stage_parsed_file

_HEADER_QUERY = b"BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)]"
_BODY_QUERY = b"BODY.PEEK[]"
_GENERIC_KEYWORDS = ("账单", "對賬單", "statement", "credit card")
_SAFE_NAME = re.compile(r"[^\w\-.\u4e00-\u9fff]+", re.UNICODE)
_SYNC_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class ImapRuntimeConfig:
    account_id: str
    host: str
    port: int
    username: str
    password: object
    mailbox: str = "INBOX"
    all_folders: bool = True
    initial_sync_months: int = 12


@dataclass(slots=True)
class SyncResult:
    scanned_folders: int = 0
    failed_folders: int = 0
    scanned: int = 0
    candidates: int = 0
    complete_statements: int = 0
    duplicate_statements: int = 0
    parsed_batches: int = 0
    duplicate_batches: int = 0
    failed_candidates: int = 0
    failure_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, int | list[str]]:
        return asdict(self)

    def merge(self, other: "SyncResult") -> None:
        for field in self.__dataclass_fields__:
            if field == "failure_reasons":
                self.failure_reasons.extend(other.failure_reasons)
            else:
                setattr(self, field, getattr(self, field) + getattr(other, field))

    def add_failure(self, reason: object) -> None:
        cleaned = redact_sensitive(reason)
        if cleaned and cleaned not in self.failure_reasons and len(self.failure_reasons) < 100:
            self.failure_reasons.append(cleaned)


_RUN_COUNT_FIELDS = (
    "scanned_folders",
    "failed_folders",
    "scanned",
    "candidates",
    "complete_statements",
    "duplicate_statements",
    "parsed_batches",
    "duplicate_batches",
    "failed_candidates",
)


def _finish_sync_run(
    session: Session,
    run_id: str,
    result: SyncResult,
    status: str,
    started_at: float,
) -> None:
    session.rollback()
    run = session.get(ImapSyncRun, run_id)
    if run is None:
        return
    run.status = status
    run.completed_at = utc_now()
    run.duration_ms = round((perf_counter() - started_at) * 1000)
    for name in _RUN_COUNT_FIELDS:
        setattr(run, name, getattr(result, name))
    run.failure_reasons = json.dumps(result.failure_reasons, ensure_ascii=False)
    session.commit()


def _decoded_header(value: str | None) -> str:
    chunks = []
    for part, encoding in decode_header(value or ""):
        chunks.append(part.decode(encoding or "utf-8", errors="replace") if isinstance(part, bytes) else part)
    return "".join(chunks)


def _payload(fetch_data: dict[object, object]) -> bytes:
    for item in fetch_data.values():
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if key != b"SEQ" and isinstance(value, bytes):
                return value
    raise OSError("IMAP server returned no message payload")


def _is_candidate(message: Message, *, broad: bool = False) -> bool:
    text = f"{_decoded_header(message.get('Subject'))} {_decoded_header(message.get('From'))}".lower()
    has_statement_term = any(term.lower() in text for term in _GENERIC_KEYWORDS)
    has_bank_term = any(term in text for term in bank_detection_terms())
    return (has_statement_term or has_bank_term) if broad else (has_statement_term and has_bank_term)


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = _SAFE_NAME.sub("_", Path(name).name).strip("._")
    return (cleaned or fallback)[:180]


def _stage_message(session: Session, raw: bytes, locator: str, result: SyncResult) -> bool:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    message_id = str(message.get("Message-ID", "")) or None
    with TemporaryDirectory(prefix="ccm-imap-") as directory:
        root = Path(directory)
        subject = _decoded_header(message.get("Subject"))
        eml_path = root / (_safe_filename(subject, "statement") + ".eml")
        eml_path.write_bytes(raw)

        # A native summary parser can promote the email directly into Card/Statement.
        handled = False
        errors: list[str] = []
        try:
            source = parse_eml_bytes(raw)
            default_registry.parser_for(source)
            imported = import_eml_data(session, raw)
            if imported.deduplicated:
                result.duplicate_statements += 1
            else:
                result.complete_statements += 1
            handled = True
        except ImportConflict as exc:
            session.rollback()
            result.add_failure(f"{locator}: {exc.detail['code']}: {exc}; 请手动上传账单核对差异。")
            return False
        except ParserError as exc:
            errors.append(str(exc))
            session.rollback()

        paths = [eml_path]
        supported = {fmt for provider in bank_providers() for fmt in provider.formats}
        for index, part in enumerate(message.walk(), start=1):
            if part.get_content_disposition() != "attachment":
                continue
            filename = _decoded_header(part.get_filename())
            suffix = Path(filename).suffix.lower()
            payload = part.get_payload(decode=True)
            if suffix not in supported or not payload or len(payload) > MAX_EML_BYTES:
                continue
            attachment = root / _safe_filename(filename, f"attachment-{index}{suffix}")
            attachment.write_bytes(payload)
            paths.append(attachment)

        for path in paths:
            try:
                _, duplicate = stage_parsed_file(
                    session,
                    path,
                    source_type="imap",
                    source_locator=locator,
                    message_id=message_id,
                )
                if duplicate:
                    result.duplicate_batches += 1
                else:
                    result.parsed_batches += 1
                handled = True
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
                session.rollback()
        if not handled:
            detail = "; ".join(dict.fromkeys(errors)) or "no parser handled the candidate"
            result.add_failure(f"{locator}: {detail}")
        return handled


def _sync_folder(
    client: IMAPClient,
    session: Session,
    account_key: str,
    mailbox: str,
    full_scan: bool,
    lookback_months: int | None,
    initial_sync_months: int = 12,
) -> SyncResult:
        result = SyncResult(scanned_folders=1)
        folder_info = client.select_folder(mailbox, readonly=True)
        uidvalidity = int(folder_info.get(b"UIDVALIDITY", 0))
        state = session.scalar(select(ImapState).where(ImapState.account_key == account_key, ImapState.mailbox == mailbox))
        if state is None:
            state = ImapState(account_key=account_key, mailbox=mailbox, uidvalidity=uidvalidity, last_uid=0, pending_uids="[]")
            session.add(state)
            session.flush()
        elif state.uidvalidity != uidvalidity:
            state.uidvalidity = uidvalidity
            state.last_uid = 0
            state.pending_uids = "[]"

        historical_scan = full_scan or lookback_months is not None
        if full_scan:
            criteria: list[object] | str = "ALL"
        elif lookback_months is not None:
            since = (datetime.now(UTC) - timedelta(days=lookback_months * 31)).date()
            criteria = ["SINCE", since]
        elif state.last_uid:
            criteria: list[object] = ["UID", f"{state.last_uid + 1}:*"]
        elif initial_sync_months == 0:
            criteria = "ALL"
        else:
            since = (
                datetime.now(UTC)
                - timedelta(days=initial_sync_months * 31)
            ).date()
            criteria = ["SINCE", since]
        discovered = [
            int(value)
            for value in client.search(criteria)
            if historical_scan or not state.last_uid or int(value) > state.last_uid
        ]
        try:
            pending = {int(value) for value in json.loads(state.pending_uids)}
        except (TypeError, ValueError, json.JSONDecodeError):
            pending = set()
        uids = sorted(pending | set(discovered))
        for uid in uids:
            result.scanned += 1
            try:
                header_data = client.fetch([uid], [_HEADER_QUERY])
                header = BytesParser(policy=policy.default).parsebytes(_payload(header_data))
            except (OSError, ValueError) as exc:
                result.failed_candidates += 1
                result.add_failure(f"{mailbox} UID {uid}: header fetch failed: {exc}")
                pending.add(uid)
                state.last_uid = max(state.last_uid, uid)
                state.pending_uids = json.dumps(sorted(pending))
                session.add(state)
                session.commit()
                continue
            if _is_candidate(header, broad=full_scan):
                result.candidates += 1
                try:
                    raw = _payload(client.fetch([uid], [_BODY_QUERY]))
                    if len(raw) > MAX_EML_BYTES:
                        raise ValueError("Message exceeds size limit")
                    folder_key = hashlib.sha256(mailbox.encode()).hexdigest()[:12]
                    if _stage_message(session, raw, f"imap:{folder_key}:{uidvalidity}:{uid}", result):
                        pending.discard(uid)
                    else:
                        result.failed_candidates += 1
                        pending.add(uid)
                except Exception as exc:
                    result.failed_candidates += 1
                    result.add_failure(f"{mailbox} UID {uid}: {type(exc).__name__}: {exc}")
                    pending.add(uid)
                    session.rollback()
            else:
                pending.discard(uid)
            state.last_uid = max(state.last_uid, uid)
            state.pending_uids = json.dumps(sorted(pending))
            session.add(state)
            session.commit()
        session.add(state)
        session.commit()
        return result


def _sync_mailbox(
    session: Session,
    config: ImapRuntimeConfig,
    *,
    full_scan: bool = False,
    lookback_months: int | None = None,
) -> SyncResult:
    if not config.host or not config.username:
        raise ValueError("IMAP configuration missing host or username")
    password = config.password.get_secret_value() if hasattr(config.password, "get_secret_value") else str(config.password)
    account_key = hashlib.sha256((config.host + chr(0) + config.username).encode()).hexdigest()
    result = SyncResult()
    with IMAPClient(config.host, config.port, ssl=True, use_uid=True, timeout=30) as client:
        client.login(config.username, password)
        if b"ID" in client.capabilities():
            client.id_({"name": "Private Credit Card Manager", "version": "0.1.0", "vendor": "local"})
        if config.all_folders:
            folders = [
                name
                for flags, _delimiter, name in client.list_folders()
                if b"\\Noselect" not in flags and "\\Noselect" not in flags
            ]
        else:
            folders = [config.mailbox]
        for mailbox in folders:
            try:
                result.merge(
                    _sync_folder(
                        client,
                        session,
                        account_key,
                        mailbox,
                        full_scan,
                        lookback_months,
                        initial_sync_months=config.initial_sync_months,
                    )
                )
            except IMAPClientError as exc:
                session.rollback()
                result.failed_folders += 1
                result.add_failure(f"{mailbox}: {type(exc).__name__}: {exc}")
    return result


def sync_mailbox(
    session: Session,
    config: ImapRuntimeConfig,
    *,
    full_scan: bool = False,
    lookback_months: int | None = None,
    trigger: str = "manual",
) -> SyncResult:
    if lookback_months is not None and not 1 <= lookback_months <= 24:
        raise ValueError("邮箱抓取范围只能选择 1 至 24 个月")
    if not _SYNC_LOCK.acquire(blocking=False):
        raise ValueError("An IMAP synchronization is already running")
    try:
        mode = "full" if full_scan else "lookback" if lookback_months is not None else "incremental"
        started_at = perf_counter()
        run = ImapSyncRun(
            trigger=trigger,
            mode=mode,
            lookback_months=lookback_months,
            status="running",
        )
        session.add(run)
        session.commit()
        try:
            result = _sync_mailbox(
                session,
                config,
                full_scan=full_scan,
                lookback_months=lookback_months,
            )
        except IMAPClientError as exc:
            result = SyncResult()
            result.add_failure(f"{type(exc).__name__}: {exc}")
            _finish_sync_run(session, run.id, result, "failed", started_at)
            raise OSError("IMAP authentication or protocol error") from exc
        except Exception as exc:
            result = SyncResult()
            result.add_failure(f"{type(exc).__name__}: {exc}")
            _finish_sync_run(session, run.id, result, "failed", started_at)
            raise
        status = "partial" if result.failed_folders or result.failed_candidates else "success"
        _finish_sync_run(session, run.id, result, status, started_at)
        return result
    finally:
        _SYNC_LOCK.release()
