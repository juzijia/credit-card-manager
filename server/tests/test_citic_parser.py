import hashlib
import json
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import pymupdf
import pytest

from app.parsers import parse_eml_bytes
from app.parsers.citic import CiticStatementParser


_PRIVATE_CITIC_EXPECTED_SHA256 = (
    "bd7f3dd51add0619268130a4490b6310928f37b49d06b9cb0bd7b327eb600c1b"
)


def _synthetic_citic_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=610, height=900)
    lines = [
        "Statement Date",
        "2026-09-05",
        "Payment Due Date",
        "2026-09-24",
        "Total Payment",
        "CNY",
        "62.34",
        "Min. Payment",
        "CNY",
        "3.12",
        "Card Number",
        "6226-89**-****-1234",
        "Trans Date",
        "Post Date",
        "Card Number",
        "Description",
        "Trx.Amt",
        "Setl.Amt",
        "20260819",
        "20260819",
        "1234",
        "SYNTHETIC MERCHANT ONE",
        "CNY 12.34",
        "CNY 12.34",
        "20260821",
        "20260821",
        "1234",
        "SYNTHETIC MERCHANT TWO",
        "CNY 20.00",
        "CNY 20.00",
        "20260823",
        "20260823",
        "1234",
        "SYNTHETIC MERCHANT THREE",
        "CNY 30.00",
        "CNY 30.00",
    ]
    for index, line in enumerate(lines):
        page.insert_text((50, 50 + index * 20), line, fontname="helv", fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


def _synthetic_citic_eml(pdf_data: bytes) -> bytes:
    message = EmailMessage()
    message["Subject"] = "中信银行信用卡电子账单（脱敏合成测试）"
    message["From"] = "statement@bill.citiccard.com"
    message["To"] = "test@example.invalid"
    message.set_content("Synthetic CITIC regression fixture")
    message.add_alternative(
        "<html><body>Synthetic CITIC regression fixture</body></html>",
        subtype="html",
    )
    message.add_attachment(
        pdf_data,
        maintype="application",
        subtype="pdf",
        filename="statement.pdf",
    )
    return message.as_bytes()


def _private_statement_digest(parsed) -> str:
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


def test_citic_parser_reads_detailed_pdf_attachment() -> None:
    source = parse_eml_bytes(_synthetic_citic_eml(_synthetic_citic_pdf()))
    parsed = CiticStatementParser().parse(source)

    assert parsed.statement_date == date(2026, 9, 5)
    assert parsed.due_date == date(2026, 9, 24)
    assert parsed.total_due_minor == 6_234
    assert parsed.minimum_due_minor == 312
    assert parsed.card_last4 == "1234"
    assert [item.amount_minor for item in parsed.transactions] == [1_234, 2_000, 3_000]
    assert parsed.credit_limit_minor is None
    assert "CITIC PDF does not expose a credit limit" in parsed.warnings


def test_private_citic_pdf_regression_when_available() -> None:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "dev"
        / "private-fixtures"
        / "citic"
        / "citic_statement.pdf"
    )
    if not fixture.exists():
        pytest.skip("private CITIC PDF is intentionally not stored in the repository")

    parsed = CiticStatementParser().parse_pdf_bytes(fixture.read_bytes())

    assert len(parsed.transactions) == 3
    assert all(item.transaction_date and item.posting_date for item in parsed.transactions)
    assert sum(item.amount_minor for item in parsed.transactions) == parsed.total_due_minor
    assert parsed.credit_limit_minor is None
    assert _private_statement_digest(parsed) == _PRIVATE_CITIC_EXPECTED_SHA256
