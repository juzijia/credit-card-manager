from decimal import Decimal
from email.message import EmailMessage
import os
from pathlib import Path
import secrets
import shutil
import tempfile
from types import SimpleNamespace
from datetime import date
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest
from sqlalchemy import select

from app.api.v1.auth import _FAILED_ATTEMPTS
from app.core.config import Settings
from app.main import create_app
from app.models.manual_bill import ManualBillPayment
from app.parsers import parse_eml_bytes
from app.parsers.abc import AbcStatementParser
from app.parsers.base import ParsedSource, ParserError
from app.parsers.boc import BocStatementParser
from app.parsers.ccb import CcbStatementParser
from app.services.database_maintenance import verify_sqlite
from app.services.manual_bills import ensure_current_payments


class _FakeTable:
    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows

    def extract(self) -> list[list[str]]:
        return self._rows


class _FakePage:
    def __init__(self, tables: list[_FakeTable]) -> None:
        self._tables = tables

    def find_tables(self) -> SimpleNamespace:
        return SimpleNamespace(tables=self._tables)

    def get_text(self) -> str:
        return ""


class _FakeDocument:
    def __init__(self, pages: list[_FakePage]) -> None:
        self._pages = pages
        self.closed = False

    @property
    def page_count(self) -> int:
        return len(self._pages)

    def __getitem__(self, index: int) -> _FakePage:
        return self._pages[index]

    def __iter__(self):
        return iter(self._pages)

    def close(self) -> None:
        self.closed = True


def _synthetic_boc_eml() -> bytes:
    message = EmailMessage()
    message["Subject"] = "中国银行信用卡电子账单（脱敏合成测试）"
    message["From"] = "statement@bankofchina.com"
    message["To"] = "test@example.invalid"
    message.set_content("Synthetic BOC regression fixture")
    message.add_alternative(
        "<html><body>Synthetic BOC regression fixture</body></html>",
        subtype="html",
    )
    message.add_attachment(
        b"synthetic-pdf-placeholder",
        maintype="application",
        subtype="pdf",
        filename="statement.pdf",
    )
    return message.as_bytes()


@pytest.fixture
def fresh_isolated_env():
    td = tempfile.mkdtemp(prefix="ccm_rc_val_")
    db_file = Path(td) / "credit-card-manager.db"
    db_url = f"sqlite:///{db_file.as_posix()}"
    secrets_file = Path(td) / "secrets.json"
    owner_tok = secrets.token_urlsafe(32)

    repo_root = Path(__file__).resolve().parents[1]
    alembic_ini = repo_root / "alembic.ini"

    with patch.dict(
        os.environ,
        {
            "CCM_DATABASE_URL": db_url,
            "CCM_SECRETS_FILE": str(secrets_file),
        },
    ):
        cfg = Config(str(alembic_ini))
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        # Verify SQLite schema integrity immediately
        verify_sqlite(db_file)
        _FAILED_ATTEMPTS.clear()

        settings = Settings(
            database_url=db_url,
            env="production",
            auth_mode="session",
            allowed_host="localhost",
            owner_token=SecretStr(owner_tok),
            admin_token=SecretStr("01234567890123456789012345678901Fill"),
            background_tasks_enabled=False,
        )
        app = create_app(settings)
        client = TestClient(app, base_url="https://localhost")
        yield {
            "dir": td,
            "db_url": db_url,
            "db_file": db_file,
            "owner_token": owner_tok,
            "client": client,
            "settings": settings,
            "app": app,
        }
        _FAILED_ATTEMPTS.clear()
        shutil.rmtree(td, ignore_errors=True)


def test_rc_fresh_install_and_auth_lifecycle(fresh_isolated_env):
    client = fresh_isolated_env["client"]
    owner_tok = fresh_isolated_env["owner_token"]

    # 1. Healthcheck is accessible
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    # 2. Unauthenticated access to private API is rejected
    unauth = client.get("/api/v1/cards")
    assert unauth.status_code == 401

    # 3. Login with invalid token is rejected
    login_fail = client.post("/api/v1/auth/login", json={"owner_token": "wrong-token"})
    assert login_fail.status_code == 401

    # 4. Login with correct owner token succeeds and sets cookie + returns csrf_token
    login_ok = client.post("/api/v1/auth/login", json={"owner_token": owner_tok})
    assert login_ok.status_code == 200
    data = login_ok.json()
    assert data["success"] is True
    csrf_token = data["csrf_token"]
    assert bool(csrf_token)
    assert "__Host-ccm_session" in client.cookies

    # 5. Session check confirms owner role
    sess = client.get("/api/v1/auth/session")
    assert sess.status_code == 200
    assert sess.json()["authenticated"] is True
    assert sess.json()["role"] == "owner"


def test_rc_business_lifecycle_end_to_end(fresh_isolated_env):
    client = fresh_isolated_env["client"]
    owner_tok = fresh_isolated_env["owner_token"]
    login = client.post("/api/v1/auth/login", json={"owner_token": owner_tok})
    csrf = login.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    # A. Card Lifecycle
    card_res = client.post(
        "/api/v1/cards",
        headers=headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "经典白金卡",
            "last4": "8888",
            "currency": "CNY",
            "credit_limit_minor": 10000000,
            "bill_day": 5,
            "due_day": 25,
        },
    )
    assert card_res.status_code == 201
    card_id = card_res.json()["id"]

    # Patch limit
    patch_res = client.patch(
        f"/api/v1/cards/{card_id}",
        headers=headers,
        json={"credit_limit_minor": 12000000},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["credit_limit_minor"] == 12000000

    # Close and restore card
    close_res = client.post(f"/api/v1/cards/{card_id}/close", headers=headers)
    assert close_res.status_code == 200
    assert close_res.json()["status"] == "closed"

    restore_res = client.post(f"/api/v1/cards/{card_id}/restore", headers=headers)
    assert restore_res.status_code == 200
    reopen_res = client.post(f"/api/v1/cards/{card_id}/reopen", headers=headers)
    assert reopen_res.status_code == 200
    assert reopen_res.json()["status"] == "active"

    # B. Statement & Repayment Lifecycle
    stmt_res = client.post(
        "/api/v1/statements",
        headers=headers,
        json={
            "card_id": card_id,
            "statement_date": "2026-08-05",
            "due_date": "2026-08-25",
            "total_due_minor": 500000,
            "currency": "CNY",
        },
    )
    assert stmt_res.status_code == 201
    stmt_id = stmt_res.json()["id"]

    # Partial repayment
    rep1 = client.post(
        f"/api/v1/statements/{stmt_id}/repayments",
        headers=headers,
        json={"amount_minor": 200000, "idempotency_key": "rep-idempotency-1"},
    )
    assert rep1.status_code == 201
    stmt_check1 = client.get(f"/api/v1/statements/{stmt_id}")
    assert stmt_check1.json()["remaining_minor"] == 300000

    # Mark paid in full (remaining 300000)
    mark_paid = client.post(
        f"/api/v1/statements/{stmt_id}/mark-paid",
        headers={**headers, "Idempotency-Key": "mark-paid-idempotency-1"},
    )
    assert mark_paid.status_code == 200
    stmt_check2 = client.get(f"/api/v1/statements/{stmt_id}")
    assert stmt_check2.json()["remaining_minor"] == 0
    assert stmt_check2.json()["status"] == "paid"

    # Undo mark paid
    marked_rep_id = mark_paid.json()["id"]
    undo = client.delete(
        f"/api/v1/statements/{stmt_id}/repayments/{marked_rep_id}",
        headers=headers,
    )
    assert undo.status_code == 204
    stmt_check3 = client.get(f"/api/v1/statements/{stmt_id}")
    assert stmt_check3.json()["remaining_minor"] == 300000

    # C. Manual Bill Lifecycle
    mbill = client.post(
        "/api/v1/manual-bills",
        headers=headers,
        json={
            "bill_type": "房租",
            "name": "每月房租",
            "statement_day": 1,
            "due_day": 10,
            "monthly_amount_minor": 450000,
        },
    )
    assert mbill.status_code == 201
    mb_id = mbill.json()["id"]

    # Mark paid and unmark paid
    mp = client.post(f"/api/v1/manual-bills/{mb_id}/mark-paid", headers=headers)
    assert mp.status_code == 204

    # Marked-paid list shows it
    marked_list = client.get(f"/api/v1/manual-bills/marked-paid?period={date.today().strftime('%Y-%m')}")
    assert marked_list.status_code == 200
    assert any(r["bill_id"] == mb_id for r in marked_list.json())

    unmp = client.post(f"/api/v1/manual-bills/{mb_id}/unmark-paid", headers=headers)
    assert unmp.status_code == 204

    # D. Mortgage Lifecycle with Manual Override Preservation
    mortgage = client.post(
        "/api/v1/mortgage/save-bill",
        headers=headers,
        json={
            "name": "测试房贷",
            "loan_type": "commercial",
            "repayment_method": "equal_payment",
            "loan_term_months": 24,
            "commercial_principal_minor": 24_000_000,
            "commercial_actual_rate": 3.0,
            "first_payment_date": "2026-09-15",
            "reminder_enabled": True,
        },
    )
    assert mortgage.status_code == 201
    m_bill_id = mortgage.json()["manual_bill_id"]

    # Sync generates current payment
    client.post("/api/v1/manual-bills/generate-current-payments", headers=headers)
    payments = client.get("/api/v1/manual-bills/current", headers=headers).json()
    m_pay = next(p for p in payments if p["bill_id"] == m_bill_id)
    original_amount = m_pay["amount_minor"]
    assert original_amount > 0

    # User manually edits current payment to custom amount
    custom_amount = original_amount + 88888
    edit_mb = client.put(
        f"/api/v1/manual-bills/{m_bill_id}",
        headers=headers,
        json={
            "bill_type": "房贷",
            "name": "测试房贷",
            "statement_day": 1,
            "due_day": 18,
            "monthly_amount_minor": custom_amount,
            "reminder_enabled": True,
            "enabled": True,
        },
    )
    assert edit_mb.status_code == 200

    # Direct manual payment edit
    with fresh_isolated_env["app"].state.session_factory() as session:
        cur_payment = session.scalar(
            select(ManualBillPayment).where(
                ManualBillPayment.id == m_pay["payment_id"],
            )
        )
        cur_payment.amount_minor = custom_amount
        cur_payment.due_date = date(2026, 9, 18)
        session.commit()

        # Call ensure_current_payments again
        ensure_current_payments(session, date(2026, 9, 20))
        session.commit()

        cur_payment_after = session.scalar(
            select(ManualBillPayment).where(
                ManualBillPayment.id == m_pay["payment_id"],
            )
        )
        # Verify manual override is preserved
        assert cur_payment_after.amount_minor == custom_amount
        assert cur_payment_after.due_date == date(2026, 9, 18)


def test_rc_parser_safety_smoke(monkeypatch):
    # 1. ABC: missing total due raises ParserError
    abc_missing = ParsedSource(
        source_type="eml",
        subject="中国农业银行金穗信用卡电子账单",
        sender="service@creditcard.abchina.com.cn",
        sha256="abc-missing",
        html="<div>账单周期 2026/08/01 - 2026/08/31</div><div>到期还款日 2026-09-25</div><div>Card No 622848******1234</div>",
    )
    with pytest.raises(ParserError, match="ABC total due amount not found"):
        AbcStatementParser().parse(abc_missing)

    # ABC: explicit zero due amount parses as 0 minor
    abc_zero = ParsedSource(
        source_type="eml",
        subject="中国农业银行金穗信用卡电子账单",
        sender="service@creditcard.abchina.com.cn",
        sha256="abc-zero",
        html="<div>账单周期 2026/08/01 - 2026/08/31</div><div>到期还款日 2026-09-25</div><div>Card No 622848******1234</div><div>本期应还款额 New Balance 人民币(CNY) 0.00</div>",
    )
    assert AbcStatementParser().parse(abc_zero).total_due_minor == 0

    # 2. CCB: missing total due raises ParserError
    ccb_missing = ParsedSource(
        source_type="eml",
        subject="中国建设银行信用卡电子账单",
        sender="service@vip.ccb.com",
        sha256="ccb-missing",
        html="<div>本期账单日 2026-08-15</div><div>本期到期还款日 2026-09-05</div>",
    )
    with pytest.raises(ParserError, match="CCB total due amount not found"):
        CcbStatementParser().parse(ccb_missing)

    # CCB: explicit zero due amount parses as 0 minor
    ccb_zero = ParsedSource(
        source_type="eml",
        subject="中国建设银行信用卡电子账单",
        sender="service@vip.ccb.com",
        sha256="ccb-zero",
        html="<div>本期账单日 2026-08-15</div><div>本期到期还款日 2026-09-05</div><div>本期全部应还款额 0.00</div><div>最低还款额 Min.Payment 0.00</div><div>授信额度 Credit Limit CNY 20,000.00</div>",
    )
    monkeypatch.setattr(
        "app.parsers.ccb.get_provider",
        lambda _name: SimpleNamespace(
            parse=lambda _path: [
                SimpleNamespace(
                    card_last4="5678",
                    date=None,
                    post_date=None,
                    amount=Decimal(0),
                    currency="CNY",
                    description="",
                )
            ]
        ),
    )
    parsed_ccb = CcbStatementParser().parse(ccb_zero)
    assert parsed_ccb.total_due_minor == 0

    # 3. BOC: missing total due raises ParserError & explicit zero due parses as 0
    fake_doc_missing = _FakeDocument([_FakePage([_FakeTable([
        ["Payment Due Date", "Statement Closing Date", "Total Due"],
        ["2026-08-20", "2026-07-31", ""],
    ])])])
    monkeypatch.setattr("app.parsers.boc.pymupdf.open", lambda **_kwargs: fake_doc_missing)
    monkeypatch.setattr(
        "app.parsers.boc.get_provider",
        lambda _name: SimpleNamespace(parse=lambda _path: []),
    )
    with pytest.raises(ParserError, match="BOC total due amount not found"):
        BocStatementParser().parse(parse_eml_bytes(_synthetic_boc_eml()))

    summary_tables_zero = [
        _FakeTable([
            ["Payment Due Date", "Statement Closing Date", "Total Due"],
            ["2026-08-20", "2026-07-31", "0.00"],
        ]),
        _FakeTable([
            ["Credit Card No.", "Current Balance", "Currency", "Minimum Payment Due"],
            ["**** 1234", "0.00", "CNY", "0.00"],
        ]),
    ]
    fake_doc_zero = _FakeDocument([_FakePage(summary_tables_zero)])
    monkeypatch.setattr("app.parsers.boc.pymupdf.open", lambda **_kwargs: fake_doc_zero)
    monkeypatch.setattr(
        "app.parsers.boc.get_provider",
        lambda _name: SimpleNamespace(parse=lambda _path: []),
    )
    parsed_boc = BocStatementParser().parse(parse_eml_bytes(_synthetic_boc_eml()))
    assert parsed_boc.total_due_minor == 0
