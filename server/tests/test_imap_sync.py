import hashlib
import threading
from datetime import date
from email.message import EmailMessage
from types import SimpleNamespace

from imapclient.exceptions import IMAPClientError
import pytest
from pydantic import SecretStr

from app.integrations import imap_sync
from app.integrations.imap_sync import ImapRuntimeConfig
from app.models.imap_state import ImapState
from app.models.imap_sync_run import ImapSyncRun
from app.models.repayment import Repayment
from app.models.statement import Statement
from app.parsers.base import ParsedStatement, ParserError
from app.services.eml_import import ImportConflict, import_eml_data


TEST_CONFIG = ImapRuntimeConfig(
    account_id="test",
    host="imap.example.test",
    port=993,
    username="user@example.test",
    password=SecretStr("app-password"),
)


class FakeImap:
    body_fetches: list[int] = []
    search_calls: list[object] = []

    def __init__(self, host, port, ssl, use_uid, timeout):
        assert (host, port, ssl, use_uid, timeout) == ("imap.example.test", 993, True, True, 30)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def login(self, username, password):
        assert username == "user@example.test"
        assert password == "app-password"

    def capabilities(self):
        return (b"IMAP4rev1", b"ID")

    def id_(self, parameters):
        assert parameters["name"] == "Private Credit Card Manager"

    def list_folders(self):
        return [((b"\\HasNoChildren",), b"/", "INBOX")]

    def select_folder(self, mailbox, readonly):
        assert mailbox == "INBOX"
        assert readonly is True
        return {b"EXISTS": 2, b"UIDVALIDITY": 42}

    def search(self, criteria):
        self.search_calls.append(criteria)
        if criteria != "ALL":
            assert criteria[0] in {"SINCE", "UID"}
        return [1, 2]

    def fetch(self, uids, queries):
        uid = uids[0]
        query = queries[0]
        if b"HEADER.FIELDS" in query:
            subject = "Weekly newsletter" if uid == 1 else "广发信用卡电子账单"
            return {uid: {b"BODY[HEADER.FIELDS]": f"Subject: {subject}\r\nFrom: bank@example.test\r\n\r\n".encode(), b"SEQ": uid}}
        self.body_fetches.append(uid)
        return {uid: {b"BODY[]": b"raw-message", b"SEQ": uid}}


def test_imap_fetches_body_only_for_candidate(client, monkeypatch) -> None:
    FakeImap.body_fetches = []
    FakeImap.search_calls = []
    monkeypatch.setattr(imap_sync, "IMAPClient", FakeImap)
    monkeypatch.setattr(imap_sync, "_stage_message", lambda *_args: True)
    with client.app.state.session_factory() as session:
        result = imap_sync.sync_mailbox(session, TEST_CONFIG)
        state = session.query(ImapState).one()
        run = session.query(ImapSyncRun).one()
        assert state.last_uid == 2
        assert state.pending_uids == "[]"
        assert run.mode == "incremental"
        assert run.status == "success"
        assert run.scanned == 2
    assert result.scanned == 2
    assert result.scanned_folders == 1
    assert result.candidates == 1
    assert FakeImap.body_fetches == [2]

    FakeImap.body_fetches = []
    with client.app.state.session_factory() as session:
        repeated = imap_sync.sync_mailbox(session, TEST_CONFIG)
        runs = session.query(ImapSyncRun).order_by(ImapSyncRun.created_at).all()
    assert repeated.scanned == 0
    assert len(runs) == 2
    assert runs[-1].status == "success"
    assert FakeImap.body_fetches == []


@pytest.mark.parametrize("previous_uidvalidity", [None, 41, 42], ids=["new-folder", "uidvalidity-reset", "pending-conflict"])
@pytest.mark.parametrize("corrected_amount", [15000, 5000], ids=["confirmation-required", "blocked-by-repayment"])
def test_imap_state_survives_first_message_correction_rollback(
    client, monkeypatch, previous_uidvalidity, corrected_amount
) -> None:
    message = EmailMessage()
    message["Subject"] = "广发信用卡电子账单"
    message["From"] = "statement@example.invalid"
    message.set_content("<html><body>Synthetic IMAP correction</body></html>", subtype="html")
    raw = message.as_bytes()
    current = {"amount": 10000}
    parser = SimpleNamespace(version="test", parse=lambda _source: ParsedStatement(
        "CGB", "1234", date(2026, 9, 1), date(2026, 9, 20), current["amount"], 0, "CNY",
    ))
    monkeypatch.setattr(imap_sync.default_registry, "parser_for", lambda _source: parser)

    class CorrectionImap(FakeImap):
        def search(self, criteria):
            self.search_calls.append(criteria)
            return [2]  # The first message must reach the real importer before any cursor commit.

        def fetch(self, uids, queries):
            if b"HEADER.FIELDS" in queries[0]:
                return super().fetch(uids, queries)
            self.body_fetches.append(uids[0])
            return {uids[0]: {b"BODY[]": raw}}

    CorrectionImap.body_fetches = []
    CorrectionImap.search_calls = []
    monkeypatch.setattr(imap_sync, "IMAPClient", CorrectionImap)
    with client.app.state.session_factory() as session:
        original = import_eml_data(session, raw)
        session.add(Repayment(
            statement_id=original.statement.id, amount_minor=6000, idempotency_key="synthetic-payment",
        ))
        if previous_uidvalidity is not None:
            account_key = hashlib.sha256(
                (TEST_CONFIG.host + chr(0) + TEST_CONFIG.username).encode()
            ).hexdigest()
            session.add(ImapState(
                account_key=account_key, mailbox="INBOX", uidvalidity=previous_uidvalidity,
                last_uid=100, pending_uids="[2]" if previous_uidvalidity == 42 else "[99]",
            ))
        session.commit()
    current["amount"] = corrected_amount
    with client.app.state.session_factory() as session:
        result = imap_sync.sync_mailbox(session, TEST_CONFIG)

    with client.app.state.session_factory() as session:
        state = session.query(ImapState).one()
        expected_last_uid = 100 if previous_uidvalidity == 42 else 2
        assert (state.uidvalidity, state.last_uid, state.pending_uids) == (42, expected_last_uid, "[]")
        assert session.query(Statement).count() == 1
        assert session.get(Statement, original.statement.id).total_due_minor == 10000
        assert session.query(Repayment).one().amount_minor == 6000
        run = session.query(ImapSyncRun).one()
        assert run.status == "partial"
        assert run.failed_candidates == 1
        assert "请手动上传账单核对差异" in run.failure_reasons
    assert result.scanned == result.candidates == result.failed_candidates == 1
    assert result.complete_statements == result.parsed_batches == 0
    assert len(result.failure_reasons) == 1
    code = "IMPORT_CONFIRMATION_REQUIRED" if corrected_amount == 15000 else "IMPORT_CORRECTION_BLOCKED"
    assert code in result.failure_reasons[0]
    assert CorrectionImap.body_fetches == [2]
    assert CorrectionImap.search_calls[0][0] == ("UID" if previous_uidvalidity == 42 else "SINCE")

    with client.app.state.session_factory() as session:
        repeated = imap_sync.sync_mailbox(session, TEST_CONFIG)
    assert repeated.scanned == repeated.failed_candidates == 0
    assert CorrectionImap.body_fetches == [2]

    # Explicit historical scans can revisit conflicts without putting them back into automatic retries.
    with client.app.state.session_factory() as session:
        historical = imap_sync.sync_mailbox(session, TEST_CONFIG, full_scan=True)
        assert session.query(ImapState).one().pending_uids == "[]"
    assert historical.failed_candidates == 1
    assert code in historical.failure_reasons[0]
    assert CorrectionImap.body_fetches == [2, 2]


@pytest.mark.parametrize("failure_at", ["header", "body", "parser"])
def test_imap_transient_failures_remain_retryable(client, monkeypatch, failure_at):
    failing = {"enabled": True}

    class FailingImap(FakeImap):
        def fetch(self, uids, queries):
            stage = "header" if b"HEADER.FIELDS" in queries[0] else "body"
            if uids == [2] and failing["enabled"] and failure_at == stage:
                raise OSError("synthetic temporary fetch failure")
            return super().fetch(uids, queries)

    def stage_message(*_args):
        return not (failing["enabled"] and failure_at == "parser")

    FailingImap.body_fetches = []
    FailingImap.search_calls = []
    monkeypatch.setattr(imap_sync, "IMAPClient", FailingImap)
    monkeypatch.setattr(imap_sync, "_stage_message", stage_message)
    with client.app.state.session_factory() as session:
        first = imap_sync.sync_mailbox(session, TEST_CONFIG)
        assert first.failed_candidates == 1
        assert session.query(ImapState).one().pending_uids == "[2]"
        assert session.query(ImapState).one().last_uid == 2
    failing["enabled"] = False
    with client.app.state.session_factory() as session:
        recovered = imap_sync.sync_mailbox(session, TEST_CONFIG)
        assert recovered.scanned == 1
        assert recovered.failed_candidates == 0
        assert session.query(ImapState).one().pending_uids == "[]"


def test_imap_records_lookback_and_full_scan_modes(client, monkeypatch) -> None:
    FakeImap.body_fetches = []
    FakeImap.search_calls = []
    monkeypatch.setattr(imap_sync, "IMAPClient", FakeImap)
    monkeypatch.setattr(imap_sync, "_stage_message", lambda *_args: True)
    with client.app.state.session_factory() as session:
        lookback = imap_sync.sync_mailbox(session, TEST_CONFIG, lookback_months=6)
        full = imap_sync.sync_mailbox(session, TEST_CONFIG, full_scan=True)
        runs = session.query(ImapSyncRun).order_by(ImapSyncRun.created_at).all()

    assert lookback.scanned == 2
    assert full.scanned == 2
    assert FakeImap.search_calls[-1] == "ALL"
    assert [(run.mode, run.lookback_months) for run in runs] == [
        ("lookback", 6),
        ("full", None),
    ]


def test_stage_message_counts_native_and_batch_duplicates(client, monkeypatch) -> None:
    monkeypatch.setattr(imap_sync, "parse_eml_bytes", lambda _raw: object())
    monkeypatch.setattr(imap_sync.default_registry, "parser_for", lambda _source: object())
    monkeypatch.setattr(
        imap_sync,
        "import_eml_data",
        lambda *_args: SimpleNamespace(deduplicated=True),
    )
    monkeypatch.setattr(imap_sync, "bank_providers", lambda: [])
    monkeypatch.setattr(
        imap_sync,
        "stage_parsed_file",
        lambda *_args, **_kwargs: (object(), True),
    )
    result = imap_sync.SyncResult()

    with client.app.state.session_factory() as session:
        handled = imap_sync._stage_message(
            session,
            b"Subject: statement\r\nMessage-ID: <duplicate@example.test>\r\n\r\nbody",
            "imap:test:42:1",
            result,
        )

    assert handled is True
    assert result.complete_statements == 0
    assert result.duplicate_statements == 1
    assert result.duplicate_batches == 1


def test_stage_message_records_failure_reason(client, monkeypatch) -> None:
    def reject_native(_raw):
        raise ParserError("native parser miss")

    monkeypatch.setattr(imap_sync, "parse_eml_bytes", reject_native)
    monkeypatch.setattr(imap_sync, "bank_providers", lambda: [])
    monkeypatch.setattr(
        imap_sync,
        "stage_parsed_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("no transactions")),
    )
    result = imap_sync.SyncResult()

    with client.app.state.session_factory() as session:
        handled = imap_sync._stage_message(
            session,
            b"Subject: statement\r\n\r\nbody",
            "imap:test:42:2",
            result,
        )

    assert handled is False
    assert result.failure_reasons == [
        "imap:test:42:2: native parser miss; no transactions"
    ]


def test_stage_message_reports_correction_instead_of_hiding_it_in_batch_fallback(client, monkeypatch):
    monkeypatch.setattr(imap_sync, "parse_eml_bytes", lambda _raw: object())
    monkeypatch.setattr(imap_sync.default_registry, "parser_for", lambda _source: object())

    def conflict(*_args):
        raise ImportConflict("IMPORT_CONFIRMATION_REQUIRED", "statement changed")

    monkeypatch.setattr(imap_sync, "import_eml_data", conflict)
    monkeypatch.setattr(imap_sync, "bank_providers", lambda: [])
    monkeypatch.setattr(imap_sync, "stage_parsed_file", lambda *_args, **_kwargs: (object(), False))
    result = imap_sync.SyncResult()
    with client.app.state.session_factory() as session:
        with pytest.raises(ImportConflict, match="statement changed"):
            imap_sync._stage_message(session, b"Subject: statement\r\n\r\nbody", "imap:test:42:3", result)
    assert result.parsed_batches == 0


def test_imap_sync_runs_endpoint(client, auth_headers) -> None:
    with client.app.state.session_factory() as session:
        session.add(
            ImapSyncRun(
                trigger="api",
                mode="lookback",
                lookback_months=3,
                status="partial",
                scanned_folders=19,
                scanned=33,
                candidates=9,
                failed_candidates=2,
                failure_reasons='["example failure"]',
            )
        )
        session.commit()

    response = client.get("/api/v1/imap/runs", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()[0]["mode"] == "lookback"
    assert response.json()[0]["failure_reasons"] == ["example failure"]


def test_sync_lock_is_released_when_initial_commit_fails(client, monkeypatch) -> None:
    assert not imap_sync._SYNC_LOCK.locked()
    with client.app.state.session_factory() as session:
        monkeypatch.setattr(
            session,
            "commit",
            lambda: (_ for _ in ()).throw(RuntimeError("initial commit failed")),
        )
        try:
            with pytest.raises(RuntimeError, match="initial commit failed"):
                imap_sync.sync_mailbox(session, TEST_CONFIG)
            assert not imap_sync._SYNC_LOCK.locked()
        finally:
            if imap_sync._SYNC_LOCK.locked():
                imap_sync._SYNC_LOCK.release()

    monkeypatch.setattr(imap_sync, "_sync_mailbox", lambda *_args, **_kwargs: imap_sync.SyncResult())
    with client.app.state.session_factory() as retry_session:
        imap_sync.sync_mailbox(retry_session, TEST_CONFIG)
    assert not imap_sync._SYNC_LOCK.locked()


def test_sync_lock_is_released_after_connect_failure(client, monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise IMAPClientError("connect failed")

    monkeypatch.setattr(imap_sync, "_sync_mailbox", fail)
    with client.app.state.session_factory() as session:
        with pytest.raises(OSError):
            imap_sync.sync_mailbox(session, TEST_CONFIG)
    assert not imap_sync._SYNC_LOCK.locked()


def test_sync_lock_is_released_after_parser_failure(client, monkeypatch) -> None:
    FakeImap.body_fetches = []
    FakeImap.search_calls = []
    monkeypatch.setattr(imap_sync, "IMAPClient", FakeImap)
    monkeypatch.setattr(
        imap_sync,
        "_stage_message",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("parser failed")),
    )
    with client.app.state.session_factory() as session:
        result = imap_sync.sync_mailbox(session, TEST_CONFIG)
    assert result.failed_candidates == 1
    assert not imap_sync._SYNC_LOCK.locked()


def test_sync_lock_is_released_after_success(client, monkeypatch) -> None:
    monkeypatch.setattr(imap_sync, "_sync_mailbox", lambda *_args, **_kwargs: imap_sync.SyncResult())
    with client.app.state.session_factory() as session:
        imap_sync.sync_mailbox(session, TEST_CONFIG)
    assert not imap_sync._SYNC_LOCK.locked()


def test_busy_sync_does_not_release_lock_owned_by_other_caller(client) -> None:
    held_lock = threading.Lock()
    assert held_lock.acquire(blocking=False)
    original_lock = imap_sync._SYNC_LOCK
    imap_sync._SYNC_LOCK = held_lock
    try:
        with client.app.state.session_factory() as session:
            with pytest.raises(ValueError, match="already running"):
                imap_sync.sync_mailbox(session, TEST_CONFIG)
        assert held_lock.locked()
    finally:
        held_lock.release()
        imap_sync._SYNC_LOCK = original_lock
