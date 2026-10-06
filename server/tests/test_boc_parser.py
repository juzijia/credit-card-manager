import hashlib
import json
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.parsers import load_eml, parse_eml_bytes
from app.parsers.boc import BocStatementParser
from app.parsers.base import ParserError


_PRIVATE_BOC_EXPECTED_SHA256 = (
    "20845bc08ba9b175792aaf0435d6ccfe26e0c6df08e6deeea1620d25ece06f70"
)


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


def _transaction_rows(start: int, stop: int) -> list[list[str]]:
    rows: list[list[str]] = []
    base = date(2026, 7, 1)
    for index in range(start, stop):
        transaction_date = base + timedelta(days=index)
        posting_date = transaction_date + timedelta(days=1)
        is_deposit = index == 15
        rows.append(
            [
                transaction_date.isoformat(),
                posting_date.isoformat(),
                "1234",
                f"脱敏交易 {index + 1}",
                "51.76" if is_deposit else "",
                "" if is_deposit else f"{index + 1}.00",
            ]
        )
    return rows


def _private_statement_digest(parsed) -> str:
    """Pin exact private values without committing those values to the repository."""
    payload = {
        "bank_code": parsed.bank_code,
        "card_last4": parsed.card_last4,
        "statement_date": parsed.statement_date.isoformat(),
        "due_date": parsed.due_date.isoformat(),
        "total_due_minor": parsed.total_due_minor,
        "minimum_due_minor": parsed.minimum_due_minor,
        "currency": parsed.currency,
        "credit_limit_minor": parsed.credit_limit_minor,
        "transactions": sorted(
            (
                item.transaction_date.isoformat() if item.transaction_date else None,
                item.posting_date.isoformat() if item.posting_date else None,
                item.amount_minor,
                item.currency,
                item.raw_description,
            )
            for item in parsed.transactions
        ),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def test_boc_parser_supplements_continuation_page_and_reads_minimum_due(
    monkeypatch,
) -> None:
    summary_tables = [
        _FakeTable(
            [
                ["Payment Due Date", "Statement Closing Date", "Total Due"],
                ["2026-08-20", "2026-07-31", "CNY 321.00"],
            ]
        ),
        _FakeTable(
            [
                ["Credit Card No.", "Current Balance", "Currency", "Minimum Payment Due"],
                ["**** 1234", "CNY 321.00", "CNY", "CNY 12.34"],
            ]
        ),
    ]
    fake_doc = _FakeDocument(
        [
            _FakePage(summary_tables),
            _FakePage([_FakeTable(_transaction_rows(0, 12))]),
            # Continuation page intentionally has no repeated table header.
            _FakePage([_FakeTable(_transaction_rows(12, 18))]),
        ]
    )

    bean_rows = []
    for index, row in enumerate(_transaction_rows(0, 12)):
        bean_rows.append(
            SimpleNamespace(
                date=date.fromisoformat(row[0]),
                post_date=date.fromisoformat(row[1]),
                card_last4="1234",
                amount=index + 1,
                currency="CNY",
                description=row[3],
            )
        )

    monkeypatch.setattr("app.parsers.boc.pymupdf.open", lambda **_kwargs: fake_doc)
    monkeypatch.setattr(
        "app.parsers.boc.get_provider",
        lambda _name: SimpleNamespace(parse=lambda _path: bean_rows),
    )

    parsed = BocStatementParser().parse(parse_eml_bytes(_synthetic_boc_eml()))

    assert parsed.statement_date == date(2026, 7, 31)
    assert parsed.due_date == date(2026, 8, 20)
    assert parsed.total_due_minor == 32_100
    assert parsed.minimum_due_minor == 1_234
    assert parsed.card_last4 == "1234"
    assert len(parsed.transactions) == 18
    assert sum(item.amount_minor < 0 for item in parsed.transactions) == 1
    assert parsed.credit_limit_minor is None
    assert "BOC PDF does not expose a credit limit" in parsed.warnings
    assert fake_doc.closed is True



def test_boc_parser_accepts_same_pdf_bytes_without_eml_wrapper(monkeypatch) -> None:
    summary_tables = [
        _FakeTable(
            [
                ["Payment Due Date", "Statement Closing Date", "Total Due"],
                ["2026-08-20", "2026-07-31", "CNY 321.00"],
            ]
        ),
        _FakeTable(
            [
                ["Credit Card No.", "Current Balance", "Currency", "Minimum Payment Due"],
                ["**** 1234", "CNY 321.00", "CNY", "CNY 12.34"],
            ]
        ),
    ]
    fake_doc = _FakeDocument([_FakePage(summary_tables)])
    monkeypatch.setattr("app.parsers.boc.pymupdf.open", lambda **_kwargs: fake_doc)
    monkeypatch.setattr(
        "app.parsers.boc.get_provider",
        lambda _name: SimpleNamespace(parse=lambda _path: []),
    )

    parsed = BocStatementParser().parse_pdf_bytes(b"synthetic-boc-pdf")

    assert parsed.bank_code == "BOC"
    assert parsed.statement_date == date(2026, 7, 31)
    assert parsed.due_date == date(2026, 8, 20)
    assert parsed.total_due_minor == 32_100
    assert parsed.minimum_due_minor == 1_234
    assert parsed.card_last4 == "1234"
    assert parsed.currency == "CNY"
    assert fake_doc.closed is True


def test_private_boc_fixture_regression_when_available() -> None:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "dev"
        / "private-fixtures"
        / "boc"
        / "boc_statement.eml"
    )
    if not fixture.exists():
        pytest.skip("private BOC fixture is intentionally not stored in the repository")

    parsed = BocStatementParser().parse(load_eml(fixture))

    assert parsed.statement_date is not None
    assert parsed.due_date is not None
    assert parsed.total_due_minor > 0
    assert parsed.minimum_due_minor is not None and parsed.minimum_due_minor > 0
    assert len(parsed.card_last4) == 4
    assert len(parsed.transactions) == 18
    assert any(item.amount_minor < 0 for item in parsed.transactions)
    assert parsed.credit_limit_minor is None
    assert _private_statement_digest(parsed) == _PRIVATE_BOC_EXPECTED_SHA256

def test_boc_parser_missing_total_due_raises_error(monkeypatch) -> None:
    # Summary table row without total due and text without due amounts
    summary_table = _FakeTable(
        [
            ["Payment Due Date", "Statement Closing Date", "Total Due"],
            ["2026-08-20", "2026-07-31", ""],
            ["Credit Card No.", "Current Due", "Min Due", "Payment"],
            ["1234", "", "100.00", "0.00"],
        ]
    )
    fake_page = _FakePage([summary_table])
    fake_doc = _FakeDocument([fake_page])
    monkeypatch.setattr("app.parsers.boc.pymupdf.open", lambda **_kwargs: fake_doc)
    monkeypatch.setattr(
        "app.parsers.boc.get_provider",
        lambda _name: SimpleNamespace(parse=lambda _path: []),
    )
    from app.parsers.base import ParserError
    with pytest.raises(ParserError, match="BOC total due amount not found"):
        BocStatementParser().parse(parse_eml_bytes(_synthetic_boc_eml()))


def test_boc_parser_explicit_zero_due_amount(monkeypatch) -> None:
    summary_tables = [
        _FakeTable(
            [
                ["Payment Due Date", "Statement Closing Date", "Total Due"],
                ["2026-08-20", "2026-07-31", "0.00"],
            ]
        ),
        _FakeTable(
            [
                ["Credit Card No.", "Current Balance", "Currency", "Minimum Payment Due"],
                ["**** 1234", "0.00", "CNY", "0.00"],
            ]
        ),
    ]
    fake_page = _FakePage(summary_tables)
    fake_doc = _FakeDocument([fake_page])
    monkeypatch.setattr("app.parsers.boc.pymupdf.open", lambda **_kwargs: fake_doc)
    monkeypatch.setattr(
        "app.parsers.boc.get_provider",
        lambda _name: SimpleNamespace(parse=lambda _path: []),
    )
    parsed = BocStatementParser().parse(parse_eml_bytes(_synthetic_boc_eml()))
    assert parsed.total_due_minor == 0
    assert parsed.minimum_due_minor == 0


def _synthetic_boc_reminder_eml(
    subject: str = "中国银行信用卡还款提醒",
    html: str = "<html><body>尊敬的客户，您的中国银行信用卡（卡号末四位：3723），到期还款日：2026-09-15，本期应还款额：100.00元。还款提醒：请按时还款。</body></html>",
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "statement@bankofchina.com"
    message["To"] = "test@example.invalid"
    message["Message-ID"] = f"<synthetic-boc-reminder-{abs(hash(subject))}@example.invalid>"
    message.set_content(html)
    message.add_alternative(html, subtype="html")
    message.add_attachment(
        b"synthetic-pdf-placeholder",
        maintype="application",
        subtype="pdf",
        filename="reminder.pdf",
    )
    return message.as_bytes()


def test_boc_parser_test1_standard_monthly_statement(monkeypatch) -> None:
    summary_tables = [
        _FakeTable([
            ["Payment Due Date", "Statement Closing Date", "Total Due"],
            ["2026-08-20", "2026-07-31", "CNY 1,234.56"],
        ]),
        _FakeTable([
            ["Credit Card No.", "Current Balance", "Currency", "Minimum Payment Due"],
            ["**** 3723", "CNY 1,234.56", "CNY", "CNY 123.45"],
        ]),
    ]
    fake_doc = _FakeDocument([_FakePage(summary_tables)])
    monkeypatch.setattr("app.parsers.boc.pymupdf.open", lambda **_kwargs: fake_doc)
    monkeypatch.setattr("app.parsers.boc.get_provider", lambda _name: SimpleNamespace(parse=lambda _path: []))

    source = parse_eml_bytes(_synthetic_boc_eml())
    parser = BocStatementParser()
    assert parser.can_handle(source) is True
    parsed = parser.parse(source)
    assert parsed.statement_date == date(2026, 7, 31)
    assert parsed.due_date == date(2026, 8, 20)
    assert parsed.total_due_minor == 123456
    assert parsed.card_last4 == "3723"


def test_boc_parser_test2_repayment_reminder_rejected() -> None:
    eml = _synthetic_boc_reminder_eml(subject="中国银行信用卡还款提醒")
    source = parse_eml_bytes(eml)
    parser = BocStatementParser()
    assert parser.can_handle(source) is False
    with pytest.raises(ParserError, match="BOC message is repayment_reminder, not a monthly statement"):
        parser.parse(source)


def test_boc_parser_test3_reminder_with_all_statement_fields_still_rejected() -> None:
    html_with_fields = (
        "<html><body>"
        "<h2>还款提醒</h2>"
        "<div>尊敬的客户：您的中国银行信用卡（卡号末四位：3723）</div>"
        "<div>账单日：2026-08-25</div>"
        "<div>到期还款日：2026-09-15</div>"
        "<div>本期应还款额：CNY 888.88</div>"
        "<div>最低还款额：CNY 88.88</div>"
        "<div>温馨提示：请按时还款，避免逾期产生利息。</div>"
        "</body></html>"
    )
    eml = _synthetic_boc_reminder_eml(
        subject="中国银行信用卡电子账单还款提醒",
        html=html_with_fields,
    )
    source = parse_eml_bytes(eml)
    parser = BocStatementParser()
    assert parser.can_handle(source) is False
    with pytest.raises(ParserError, match="repayment_reminder"):
        parser.parse(source)


def test_boc_parser_test4_legitimate_zero_due_statement_accepted(monkeypatch) -> None:
    summary_tables = [
        _FakeTable([
            ["Payment Due Date", "Statement Closing Date", "Total Due"],
            ["2026-08-20", "2026-07-31", "0.00"],
        ]),
        _FakeTable([
            ["Credit Card No.", "Current Balance", "Currency", "Minimum Payment Due"],
            ["**** 3723", "0.00", "CNY", "0.00"],
        ]),
    ]
    fake_doc = _FakeDocument([_FakePage(summary_tables)])
    monkeypatch.setattr("app.parsers.boc.pymupdf.open", lambda **_kwargs: fake_doc)
    monkeypatch.setattr("app.parsers.boc.get_provider", lambda _name: SimpleNamespace(parse=lambda _path: []))

    source = parse_eml_bytes(_synthetic_boc_eml())
    parser = BocStatementParser()
    assert parser.can_handle(source) is True
    parsed = parser.parse(source)
    assert parsed.total_due_minor == 0
    assert parsed.statement_date == date(2026, 7, 31)
    assert parsed.due_date == date(2026, 8, 20)


def test_boc_parser_test5_reminder_with_different_dates_rejected() -> None:
    html_diff_dates = (
        "<html><body>"
        "<h3>中国银行信用卡还款通知</h3>"
        "<div>通知日期：2024-09-03</div>"
        "<div>到期还款日：2024-09-14</div>"
        "<div>卡号后四位：3723</div>"
        "<div>应还款金额：500.00元</div>"
        "</body></html>"
    )
    eml = _synthetic_boc_reminder_eml(
        subject="中国银行信用卡还款通知",
        html=html_diff_dates,
    )
    source = parse_eml_bytes(eml)
    parser = BocStatementParser()
    assert parser.can_handle(source) is False
    with pytest.raises(ParserError, match="repayment_reminder"):
        parser.parse(source)


def test_boc_parser_test6_adjustment_notice_rejected() -> None:
    eml = _synthetic_boc_reminder_eml(
        subject="中国银行信用卡额度调整通知",
        html="<html><body>尊敬的客户，您的中国银行信用卡额度调整通知如下...</body></html>",
    )
    source = parse_eml_bytes(eml)
    parser = BocStatementParser()
    assert parser.can_handle(source) is False
    with pytest.raises(ParserError, match="adjustment_notice"):
        parser.parse(source)


def test_boc_parser_test7_service_idempotency_and_reminder_rejection(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    # 1. Posting reminder EML must be rejected (422) and create no Statement
    reminder_eml = _synthetic_boc_reminder_eml()
    resp_reminder = client.post(
        "/api/v1/imports/eml",
        headers=auth_headers,
        files={"file": ("reminder.eml", reminder_eml, "message/rfc822")},
    )
    assert resp_reminder.status_code == 422

    assert len(client.get("/api/v1/statements", headers=auth_headers).json()) == 0

    # 2. Posting legitimate monthly statement EML succeeds
    summary_tables = [
        _FakeTable([
            ["Payment Due Date", "Statement Closing Date", "Total Due"],
            ["2026-08-20", "2026-07-31", "CNY 500.00"],
        ]),
        _FakeTable([
            ["Credit Card No.", "Current Balance", "Currency", "Minimum Payment Due"],
            ["**** 3723", "CNY 500.00", "CNY", "CNY 50.00"],
        ]),
    ]
    fake_doc = _FakeDocument([_FakePage(summary_tables)])
    monkeypatch.setattr("app.parsers.boc.pymupdf.open", lambda **_kwargs: fake_doc)
    monkeypatch.setattr("app.parsers.boc.get_provider", lambda _name: SimpleNamespace(parse=lambda _path: []))

    stmt_eml = _synthetic_boc_eml()
    first_resp = client.post(
        "/api/v1/imports/eml",
        headers=auth_headers,
        files={"file": ("boc_stmt.eml", stmt_eml, "message/rfc822")},
    )
    assert first_resp.status_code == 200
    first_data = first_resp.json()
    assert first_data["deduplicated"] is False
    assert first_data["statement"]["total_due_minor"] == 50000

    statements = client.get("/api/v1/statements", headers=auth_headers).json()
    assert len(statements) == 1

    # 3. Posting identical statement again is deduplicated (idempotency preserved)
    second_resp = client.post(
        "/api/v1/imports/eml",
        headers=auth_headers,
        files={"file": ("boc_stmt.eml", stmt_eml, "message/rfc822")},
    )
    assert second_resp.status_code == 200
    second_data = second_resp.json()
    assert second_data["deduplicated"] is True
    assert second_data["statement"]["id"] == first_data["statement"]["id"]
    assert len(client.get("/api/v1/statements", headers=auth_headers).json()) == 1


def test_boc_parser_monthly_statement_with_boilerplate_reminder_succeeds(monkeypatch) -> None:
    html = (
        "<html><body>"
        "<div>尊敬的客户，您的中国银行信用卡电子对账单如下：</div>"
        "<div>...账单明细请参见附件...</div>"
        "<div>温馨提示：请您按时还款，避免逾期。</div>"
        "</body></html>"
    )
    message = EmailMessage()
    message["Subject"] = "中国银行信用卡电子账单"
    message["From"] = "statement@bankofchina.com"
    message["To"] = "test@example.invalid"
    message.set_content(html)
    message.add_alternative(html, subtype="html")
    message.add_attachment(
        b"synthetic-pdf-placeholder",
        maintype="application",
        subtype="pdf",
        filename="statement.pdf",
    )

    summary_tables = [
        _FakeTable([
            ["Payment Due Date", "Statement Closing Date", "Total Due"],
            ["2026-08-20", "2026-07-31", "CNY 2,000.00"],
        ]),
        _FakeTable([
            ["Credit Card No.", "Current Balance", "Currency", "Minimum Payment Due"],
            ["**** 3723", "CNY 2,000.00", "CNY", "CNY 200.00"],
        ]),
    ]
    fake_doc = _FakeDocument([_FakePage(summary_tables)])
    monkeypatch.setattr("app.parsers.boc.pymupdf.open", lambda **_kwargs: fake_doc)
    monkeypatch.setattr("app.parsers.boc.get_provider", lambda _name: SimpleNamespace(parse=lambda _path: []))

    source = parse_eml_bytes(message.as_bytes())
    parser = BocStatementParser()
    assert parser.can_handle(source) is True
    parsed = parser.parse(source)
    assert parsed.statement_date == date(2026, 7, 31)
    assert parsed.due_date == date(2026, 8, 20)
    assert parsed.total_due_minor == 200000
    assert parsed.card_last4 == "3723"
