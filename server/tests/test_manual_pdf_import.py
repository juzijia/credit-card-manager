"""Manual BOC/CITIC PDF statement import via POST /api/v1/imports/file.

Covers the bare-PDF happy path, EML-via-/file regression (identical to
/imports/eml), garbage/unsupported payload rejection, the 413 body-limit
gate, dedup idempotency, and authentication for the new route.
"""

from datetime import date
from email.message import EmailMessage

import pytest
import pymupdf
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.v1 import imports as imports_api
from app.core.config import Settings
from app.db.base import Base
from app.main import IMPORT_REQUEST_BODY_LIMIT, create_app
from app.parsers.base import ParsedStatement, ParsedTransaction, ParserError
from app.parsers.boc import BocStatementParser
from app.parsers.citic import CiticStatementParser


@pytest.fixture
def production_client(tmp_path, admin_token) -> TestClient:
    """Session-auth client (no dev_bypass), mirrors test_request_body_limit.py."""
    settings = Settings(
        _env_file=None,
        env="production",
        auth_mode="session",
        owner_token=SecretStr("synthetic-owner-token-" + "x" * 32),
        admin_token=SecretStr(admin_token),
        allowed_host="testserver",
        database_url=f"sqlite:///{(tmp_path / 'production.db').as_posix()}",
        error_log_path=None,
        background_tasks_enabled=False,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as test_client:
        yield test_client


def synthetic_citic_pdf() -> bytes:
    """Reuse the exact synthetic layout pinned by test_citic_parser.py."""
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


def synthetic_citic_eml() -> bytes:
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
        synthetic_citic_pdf(),
        maintype="application",
        subtype="pdf",
        filename="statement.pdf",
    )
    return message.as_bytes()



def synthetic_boc_parsed_statement() -> ParsedStatement:
    return ParsedStatement(
        bank_code="BOC",
        card_last4="4321",
        statement_date=date(2026, 8, 31),
        due_date=date(2026, 9, 20),
        total_due_minor=12_345,
        minimum_due_minor=1_234,
        currency="CNY",
        credit_limit_minor=None,
        transactions=(
            ParsedTransaction(
                transaction_date=date(2026, 8, 18),
                posting_date=date(2026, 8, 19),
                amount_minor=12_345,
                currency="CNY",
                raw_description="SYNTHETIC BOC MERCHANT",
            ),
        ),
        warnings=("BOC PDF does not expose a credit limit",),
    )


def test_bare_boc_pdf_dispatches_to_boc_parser_and_persists(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    calls = {"boc": 0, "citic": 0}

    def parse_boc(_self, _data: bytes) -> ParsedStatement:
        calls["boc"] += 1
        return synthetic_boc_parsed_statement()

    def reject_citic(_self, _data: bytes) -> ParsedStatement:
        calls["citic"] += 1
        raise ParserError("not a CITIC PDF")

    monkeypatch.setattr(BocStatementParser, "parse_pdf_bytes", parse_boc)
    monkeypatch.setattr(CiticStatementParser, "parse_pdf_bytes", reject_citic)

    response = client.post(
        "/api/v1/imports/file",
        headers=auth_headers,
        files={"file": ("boc-statement.pdf", b"synthetic-boc-pdf", "application/pdf")},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["bank_code"] == "BOC"
    assert payload["parser_version"] == "1.1.0"
    assert payload["statement"]["card_last4"] == "4321"
    assert payload["statement"]["total_due_minor"] == 12_345
    assert calls == {"boc": 1, "citic": 1}

    statements = client.get("/api/v1/statements", headers=auth_headers).json()
    transactions = client.get("/api/v1/transactions", headers=auth_headers).json()
    assert len(statements) == 1
    assert statements[0]["bank_code"] == "BOC"
    assert len(transactions) == 1
    assert transactions[0]["amount_minor"] == 12_345


def test_bare_pdf_ambiguous_bank_match_fails_closed_without_persistence(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    boc = synthetic_boc_parsed_statement()
    citic = ParsedStatement(
        bank_code="CITIC",
        card_last4="1234",
        statement_date=boc.statement_date,
        due_date=boc.due_date,
        total_due_minor=boc.total_due_minor,
        minimum_due_minor=boc.minimum_due_minor,
        currency="CNY",
        transactions=boc.transactions,
    )
    monkeypatch.setattr(BocStatementParser, "parse_pdf_bytes", lambda _self, _data: boc)
    monkeypatch.setattr(CiticStatementParser, "parse_pdf_bytes", lambda _self, _data: citic)

    response = client.post(
        "/api/v1/imports/file",
        headers=auth_headers,
        files={"file": ("ambiguous.pdf", b"synthetic-ambiguous-pdf", "application/pdf")},
    )

    assert response.status_code == 422, response.text
    assert "More than one supported bank PDF parser" in response.json()["detail"]
    assert client.get("/api/v1/statements", headers=auth_headers).json() == []
    assert client.get("/api/v1/transactions", headers=auth_headers).json() == []
    assert client.get("/api/v1/cards", headers=auth_headers).json() == []


def test_bare_pdf_import_via_imports_file_persists_statement_and_transactions(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    pdf = synthetic_citic_pdf()
    response = client.post(
        "/api/v1/imports/file",
        headers=auth_headers,
        files={"file": ("statement.pdf", pdf, "application/pdf")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["bank_code"] == "CITIC"
    assert payload["parser_version"] == "1.1.0"
    assert payload["deduplicated"] is False
    statement = payload["statement"]
    assert statement["statement_date"] == "2026-09-05"
    assert statement["due_date"] == "2026-09-24"
    assert statement["total_due_minor"] == 6234
    assert statement["minimum_due_minor"] == 312
    assert statement["currency"] == "CNY"
    assert statement["parse_status"] == "complete"
    assert statement["card_last4"] == "1234"
    assert statement["source_import_id"] == payload["import_id"]

    transactions = client.get(
        "/api/v1/transactions", headers=auth_headers
    ).json()
    assert len(transactions) == 3
    assert sorted(item["amount_minor"] for item in transactions) == [
        1234,
        2000,
        3000,
    ]
    assert all(item["currency"] == "CNY" for item in transactions)
    assert {item["transaction_date"] for item in transactions} == {
        "2026-08-19",
        "2026-08-21",
        "2026-08-23",
    }

    statements = client.get("/api/v1/statements", headers=auth_headers).json()
    assert len(statements) == 1
    assert statements[0]["id"] == statement["id"]
    assert statements[0]["bank_code"] == "CITIC"


def test_eml_through_imports_file_matches_imports_eml(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    eml = synthetic_citic_eml()
    files = {"file": ("statement.eml", eml, "message/rfc822")}

    baseline = client.post(
        "/api/v1/imports/eml", headers=auth_headers, files=files
    )
    assert baseline.status_code == 200, baseline.text
    baseline_payload = baseline.json()
    assert baseline_payload["deduplicated"] is False

    # Same source through /imports/file must resolve to the identical import
    # (cross-channel dedup is shared between the two cores).
    via_file = client.post(
        "/api/v1/imports/file", headers=auth_headers, files=files
    )
    assert via_file.status_code == 200, via_file.text
    payload = via_file.json()
    assert payload["deduplicated"] is True
    assert payload["import_id"] == baseline_payload["import_id"]
    assert (
        payload["statement"]["id"] == baseline_payload["statement"]["id"]
    )
    assert payload["bank_code"] == "CITIC"
    assert payload["parser_version"] == baseline_payload["parser_version"]

    statements = client.get("/api/v1/statements", headers=auth_headers).json()
    assert len(statements) == 1
    transactions = client.get(
        "/api/v1/transactions", headers=auth_headers
    ).json()
    assert len(transactions) == 3


def test_garbage_pdf_returns_422_and_persists_nothing(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/imports/file",
        headers=auth_headers,
        files={"file": ("garbage.pdf", b"this is not a real pdf", "application/pdf")},
    )
    assert response.status_code == 422, response.text

    assert client.get("/api/v1/statements", headers=auth_headers).json() == []
    assert client.get("/api/v1/transactions", headers=auth_headers).json() == []
    assert client.get("/api/v1/cards", headers=auth_headers).json() == []


def test_unsupported_suffix_returns_422(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/imports/file",
        headers=auth_headers,
        files={"file": ("statement.txt", b"anything", "text/plain")},
    )
    assert response.status_code == 422, response.text
    assert "Unsupported file type" in response.json()["detail"]

    assert client.get("/api/v1/statements", headers=auth_headers).json() == []


def test_oversized_file_returns_413_without_business_call(
    production_client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    business_call = False

    def tracked_business(*_args, **_kwargs):
        nonlocal business_call
        business_call = True

    monkeypatch.setattr(imports_api, "import_pdf_bytes", tracked_business)
    monkeypatch.setattr(imports_api, "import_eml_data", tracked_business)

    response = production_client.post(
        "/api/v1/imports/file",
        headers=auth_headers,
        files={"file": ("oversized.pdf", b"x" * (IMPORT_REQUEST_BODY_LIMIT + 1))},
    )
    assert response.status_code == 413, response.text
    assert business_call is False


def test_same_pdf_imported_twice_is_deduplicated(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    pdf = synthetic_citic_pdf()
    files = {"file": ("statement.pdf", pdf, "application/pdf")}

    first = client.post("/api/v1/imports/file", headers=auth_headers, files=files)
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert first_payload["deduplicated"] is False

    second = client.post("/api/v1/imports/file", headers=auth_headers, files=files)
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert second_payload["deduplicated"] is True
    assert second_payload["import_id"] == first_payload["import_id"]
    assert (
        second_payload["statement"]["id"] == first_payload["statement"]["id"]
    )

    statements = client.get("/api/v1/statements", headers=auth_headers).json()
    assert len(statements) == 1
    transactions = client.get(
        "/api/v1/transactions", headers=auth_headers
    ).json()
    assert len(transactions) == 3


def test_pdf_duplicate_statement_can_be_imported_again(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    eml = synthetic_citic_eml()
    pdf = synthetic_citic_pdf()
    assert client.post(
        "/api/v1/imports/file",
        headers=auth_headers,
        files={"file": ("statement.eml", eml, "message/rfc822")},
    ).status_code == 200

    files = {"file": ("statement.pdf", pdf, "application/pdf")}
    first = client.post("/api/v1/imports/file", headers=auth_headers, files=files)
    assert first.status_code == 200, first.text
    assert first.json()["deduplicated"] is True

    second = client.post("/api/v1/imports/file", headers=auth_headers, files=files)
    assert second.status_code == 200, second.text
    assert second.json()["deduplicated"] is True
    assert second.json()["import_id"] == first.json()["import_id"]
    assert len(client.get("/api/v1/statements", headers=auth_headers).json()) == 1


def test_imports_file_requires_authentication(
    production_client: TestClient, auth_headers: dict[str, str]
) -> None:
    files = {"file": ("statement.pdf", synthetic_citic_pdf(), "application/pdf")}

    anonymous = production_client.post("/api/v1/imports/file", files=files)
    assert anonymous.status_code == 401, anonymous.text

    # No statements must exist after an anonymous attempt.
    assert (
        production_client.get("/api/v1/statements", headers=auth_headers).json()
        == []
    )
