import asyncio
from types import SimpleNamespace
from decimal import Decimal
import time

import pytest
from fastapi.testclient import TestClient

from app.main import run_in_thread_orderly
from app.models.email_account import EmailAccount
from app.models.notification import NotificationSetting
from app.parsers.abc import AbcStatementParser
from app.parsers.base import ParsedSource, ParserError
from app.parsers.ccb import CcbStatementParser
from app.services.notification_service import PROVIDER_SCHEMAS, separate_provider_config


def test_get_imap_status_has_no_db_side_effects(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # Verify GET /imap/status on clean database does not create rows or modify state
    with client.app.state.session_factory() as session:
        initial_accounts = session.query(EmailAccount).count()

    response = client.get("/api/v1/imap/status", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["configured"] is False

    with client.app.state.session_factory() as session:
        final_accounts = session.query(EmailAccount).count()
    assert initial_accounts == final_accounts == 0


def test_get_notification_settings_has_no_db_side_effects(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # Verify GET /notifications/settings on clean database does not create rows
    with client.app.state.session_factory() as session:
        initial_settings = session.query(NotificationSetting).count()

    response = client.get("/api/v1/notifications/settings", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["setting"]["configured"] is False

    with client.app.state.session_factory() as session:
        final_settings = session.query(NotificationSetting).count()
    assert initial_settings == final_settings == 0


def test_provider_specific_secret_schema_classification() -> None:
    # Verify all providers in schema
    assert "webhook" in PROVIDER_SCHEMAS
    assert "telegram" in PROVIDER_SCHEMAS
    assert "bark" in PROVIDER_SCHEMAS

    # Webhook headers must be classified as secret
    webhook_cfg = {
        "url": "https://webhook.site/abc",
        "headers": {"Authorization": "Bearer token123", "X-API-Key": "key999"},
        "extra_unknown": "should-be-secret",
    }
    public_w, secret_w = separate_provider_config("webhook", webhook_cfg)
    assert public_w == {"url": "https://webhook.site/abc"}
    assert "headers" in secret_w
    assert secret_w["headers"]["Authorization"] == "Bearer token123"
    assert secret_w["extra_unknown"] == "should-be-secret"

    # Telegram bot token must be secret, chat_id public
    tg_cfg = {
        "bot_token": "123456:tg-token",
        "chat_id": "-100123456",
        "api_url": "https://api.telegram.org",
        "proxy_url": "socks5://127.0.0.1:1080",
    }
    public_tg, secret_tg = separate_provider_config("telegram", tg_cfg)
    assert public_tg == {
        "chat_id": "-100123456",
        "api_url": "https://api.telegram.org",
        "proxy_url": "socks5://127.0.0.1:1080",
    }
    assert secret_tg == {"bot_token": "123456:tg-token"}


def test_run_in_thread_orderly_waits_for_worker_on_cancel() -> None:
    async def scenario():
        finished = False

        def worker():
            nonlocal finished
            time.sleep(0.05)
            finished = True

        task = asyncio.create_task(run_in_thread_orderly(worker))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished is True

    asyncio.run(scenario())


def test_abc_parser_missing_total_due_raises_error() -> None:
    html = """
    <html>
    <body>
    <div>账单周期 2026/08/01 - 2026/08/31</div>
    <div>到期还款日 2026-09-25</div>
    <div>Card No 622848******1234</div>
    <div>信用额度 50,000.00</div>
    </body>
    </html>
    """
    source = ParsedSource(
        source_type="eml",
        subject="中国农业银行金穗信用卡电子对账单",
        sender="service@creditcard.abchina.com.cn",
        sha256="abc123sha",
        html=html,
    )
    with pytest.raises(ParserError, match="ABC total due amount not found"):
        AbcStatementParser().parse(source)


def test_abc_parser_explicit_zero_due_amount() -> None:
    html = """
    <html>
    <body>
    <div>账单周期 2026/08/01 - 2026/08/31</div>
    <div>到期还款日 2026-09-25</div>
    <div>Card No 622848******1234</div>
    <div>本期应还金额 New Balance 人民币(CNY) 0.00</div>
    </body>
    </html>
    """
    source = ParsedSource(
        source_type="eml",
        subject="中国农业银行金穗信用卡电子对账单",
        sender="service@creditcard.abchina.com.cn",
        sha256="abc123sha",
        html=html,
    )
    parsed = AbcStatementParser().parse(source)
    assert parsed.total_due_minor == 0


def test_ccb_parser_missing_total_due_raises_error() -> None:
    html = """
    <html>
    <body>
    <div>本期账单日 2026-08-15</div>
    <div>本期到期还款日 2026-09-05</div>
    </body>
    </html>
    """
    source = ParsedSource(
        source_type="eml",
        subject="中国建设银行信用卡电子账单",
        sender="service@vip.ccb.com",
        sha256="ccb123sha",
        html=html,
    )
    with pytest.raises(ParserError, match="CCB total due amount not found"):
        CcbStatementParser().parse(source)


def test_ccb_parser_explicit_zero_due_amount(monkeypatch) -> None:
    html = """
    <html>
    <body>
    <div>本期账单日 2026-08-15</div>
    <div>本期到期还款日 2026-09-05</div>
    <div>本期全部应还款额 0.00</div>
    <div>最低还款额 Min.Payment 0.00</div>
    <div>授信额度 Credit Limit CNY 20,000.00</div>
    </body>
    </html>
    """
    monkeypatch.setattr(
        "app.parsers.ccb.get_provider",
        lambda _name: SimpleNamespace(
            parse=lambda _path: [
                SimpleNamespace(
                    card_last4="5678",
                    date=None,
                    post_date=None,
                    amount=Decimal("0"),
                    currency="CNY",
                    description="",
                )
            ]
        ),
    )
    source = ParsedSource(
        source_type="eml",
        subject="中国建设银行信用卡电子账单",
        sender="service@vip.ccb.com",
        sha256="ccb123sha",
        html=html,
    )
    parsed = CcbStatementParser().parse(source)
    assert parsed.total_due_minor == 0
    assert parsed.minimum_due_minor == 0
    assert parsed.card_last4 == "5678"
