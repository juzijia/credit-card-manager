"""Phase 2 currency-safety regression tests.

Frozen boundary under test:
- Cross-currency arithmetic is prohibited (no FX, no conversion).
- Dashboard aggregates only within a single currency; mixed currencies
  report per-currency totals and null the single scalar total.
- Notifications never sum across currencies and never render a foreign
  amount with the CNY '¥' symbol.
- Analytics stays CNY-only.
- Parsers with unproven foreign-currency settlement semantics safe-fail
  instead of importing foreign-currency statements.
"""

from datetime import date
import pytest
from fastapi.testclient import TestClient

from app.models.card import Card
from app.models.statement import ParseStatus, Statement
from app.services.eml_import import _parse_document
from app.parsers.base import ParsedBillingDocument, ParsedBillingUnit


@pytest.fixture(autouse=True)
def _freeze_currency_safety_date(monkeypatch):
    frozen_date = date(2026, 9, 15)
    for target in (
        "app.services.statements.local_today",
        "app.services.analytics.local_today",
        "app.services.system_settings.local_today",
        "app.api.v1.dashboard.local_today",
        "app.api.v1.cards.local_today",
    ):
        monkeypatch.setattr(target, lambda *args, **kwargs: frozen_date)


def _add_card(session, *, last4: str, currency: str) -> Card:
    card = Card(
        bank_code="CMB",
        bank_name="招商银行",
        display_name=f"招商银行 ****{last4}",
        last4=last4,
        currency=currency,
    )
    session.add(card)
    session.flush()
    return card


def _add_statement(
    session,
    *,
    card_id: str,
    total_due_minor: int,
    currency: str,
    due_date: date,
) -> Statement:
    statement = Statement(
        bank_code="CMB",
        card_id=card_id,
        statement_date=due_date - date.resolution * 20,
        due_date=due_date,
        total_due_minor=total_due_minor,
        minimum_due_minor=min(total_due_minor, 100),
        currency=currency,
        parse_status=ParseStatus.SUMMARY_OK,
    )
    session.add(statement)
    session.flush()
    return statement


def test_due_single_currency_aggregates_normally(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    with client.app.state.session_factory() as session:
        cny_card = _add_card(session, last4="0001", currency="CNY")
        _add_statement(
            session,
            card_id=cny_card.id,
            total_due_minor=10000,
            currency="CNY",
            due_date=date(2026, 12, 1),
        )
        session.commit()

    due = client.get("/api/v1/due", headers=auth_headers)
    assert due.status_code == 200
    payload = due.json()
    assert payload["currency"] == "CNY"
    assert payload["total_remaining_minor"] == 10000
    assert payload["totals_by_currency"] == {"CNY": 10000}


def test_due_mixed_currencies_never_sum_across_currencies(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    with client.app.state.session_factory() as session:
        cny_card = _add_card(session, last4="0002", currency="CNY")
        usd_card = _add_card(session, last4="0003", currency="USD")
        _add_statement(
            session,
            card_id=cny_card.id,
            total_due_minor=10000,
            currency="CNY",
            due_date=date(2026, 12, 1),
        )
        _add_statement(
            session,
            card_id=usd_card.id,
            total_due_minor=20000,
            currency="USD",
            due_date=date(2026, 12, 1),
        )
        session.commit()

    payload = client.get("/api/v1/due", headers=auth_headers).json()
    assert payload["total_remaining_minor"] is None
    assert payload["currency"] == "MIXED"
    assert payload["totals_by_currency"] == {"CNY": 10000, "USD": 20000}
    assert len(payload["cards"]) == 2


def test_dashboard_utilization_uses_cny_numerator_only(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    with client.app.state.session_factory() as session:
        cny_card = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="招商 CNY",
            last4="0004",
            currency="CNY",
            credit_limit_minor=1000000,
        )
        usd_card = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="招商 USD",
            last4="0005",
            currency="USD",
            credit_limit_minor=1000000,
        )
        session.add_all([cny_card, usd_card])
        session.flush()
        _add_statement(
            session,
            card_id=cny_card.id,
            total_due_minor=100000,
            currency="CNY",
            due_date=date(2026, 12, 1),
        )
        _add_statement(
            session,
            card_id=usd_card.id,
            total_due_minor=99999999,
            currency="USD",
            due_date=date(2026, 12, 1),
        )
        session.commit()

    payload = client.get("/api/v1/dashboard", headers=auth_headers).json()
    assert payload["known_utilization_percent"] == 10.0
    assert payload["due"]["totals_by_currency"]["USD"] == 99999999


def test_notification_single_foreign_statement_has_no_yen_symbol(
    client: TestClient, monkeypatch
) -> None:
    dispatched = []

    def fake_dispatch(provider, config, title, content):
        dispatched.append((provider, title, content))
        return True, "mock sent"

    monkeypatch.setattr(
        "app.services.notification_service.dispatch_notification", fake_dispatch
    )

    from app.services.notification_service import (
        check_and_send_due_reminders,
        get_or_create_default_setting,
    )

    with client.app.state.session_factory() as session:
        usd_card = _add_card(session, last4="0006", currency="USD")
        _add_statement(
            session,
            card_id=usd_card.id,
            total_due_minor=50000,
            currency="USD",
            due_date=date(2026, 12, 1),
        )
        setting = get_or_create_default_setting(session)
        setting.enabled = True
        setting.offset_days_json = "[0]"
        setting.provider = "bark"
        session.commit()

        check_and_send_due_reminders(session, today=date(2026, 12, 1))

    assert len(dispatched) == 1
    title, content = dispatched[0][1], dispatched[0][2]
    assert "¥" not in title
    assert "¥" not in content
    assert "USD" in title
    assert "500.00 USD" in title


def test_notification_mixed_currencies_groups_by_currency(
    client: TestClient, monkeypatch
) -> None:
    dispatched = []

    def fake_dispatch(provider, config, title, content):
        dispatched.append((provider, title, content))
        return True, "mock sent"

    monkeypatch.setattr(
        "app.services.notification_service.dispatch_notification", fake_dispatch
    )

    from app.services.notification_service import (
        check_and_send_due_reminders,
        get_or_create_default_setting,
    )

    with client.app.state.session_factory() as session:
        cny_card = _add_card(session, last4="0007", currency="CNY")
        usd_card = _add_card(session, last4="0008", currency="USD")
        _add_statement(
            session,
            card_id=cny_card.id,
            total_due_minor=12000,
            currency="CNY",
            due_date=date(2026, 12, 1),
        )
        _add_statement(
            session,
            card_id=usd_card.id,
            total_due_minor=30000,
            currency="USD",
            due_date=date(2026, 12, 1),
        )
        setting = get_or_create_default_setting(session)
        setting.enabled = True
        setting.offset_days_json = "[0]"
        setting.provider = "bark"
        session.commit()

        check_and_send_due_reminders(session, today=date(2026, 12, 1))

    assert len(dispatched) == 1
    title, content = dispatched[0][1], dispatched[0][2]
    assert "¥120.00" in title
    assert "300.00 USD" in title
    assert "420.00" not in title
    assert "420.00" not in content
    assert "¥120.00" in content
    assert "300.00 USD" in content
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("•") and "USD" in stripped:
            assert "¥" not in stripped, f"foreign-currency detail row must not use ¥: {stripped}"


def test_statement_currency_schema_rejects_malformed_code(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "币种格式卡",
            "last4": "0009",
            "currency": "YUAN",
        },
    )
    assert response.status_code == 422


def test_unproven_foreign_parser_currency_safe_fails() -> None:
    from app.parsers.cgb import CgbStatementParser
    from app.parsers.base import ParsedSource, ParserError

    document = ParsedBillingDocument(
        bank_code="CGB",
        statement_date=date(2026, 11, 10),
        due_date=date(2026, 11, 28),
        billing_units=(
            ParsedBillingUnit(
                card_ref="1234",
                currency="USD",
                total_due_minor=10000,
                minimum_due_minor=1000,
            ),
        ),
        transactions=(),
    )
    parser = CgbStatementParser()

    class _Shim(CgbStatementParser):
        def parse_document(self, source):  # noqa: ANN001
            return document

    source = ParsedSource(
        source_type="eml",
        subject="",
        sender="",
        html="",
        sha256="a" * 64,
    )
    try:
        _parse_document(_Shim(), source)
    except ParserError as exc:
        assert "UNPROVEN_FOREIGN_CURRENCY" in str(exc)
        assert "currency=USD" in str(exc)
    else:
        raise AssertionError("unproven foreign currency must safe-fail")

    _ = parser


def test_ccb_proven_foreign_currency_is_allowed() -> None:
    from app.parsers.ccb import CcbStatementParser
    from app.parsers.base import ParsedSource

    document = ParsedBillingDocument(
        bank_code="CCB",
        statement_date=date(2026, 11, 10),
        due_date=date(2026, 11, 28),
        billing_units=(
            ParsedBillingUnit(
                card_ref="5678",
                currency="USD",
                total_due_minor=10000,
                minimum_due_minor=1000,
            ),
        ),
        transactions=(),
    )

    class _Shim(CcbStatementParser):
        def parse_document(self, source):  # noqa: ANN001
            return document

    source = ParsedSource(
        source_type="eml",
        subject="",
        sender="",
        html="",
        sha256="b" * 64,
    )
    parsed = _parse_document(_Shim(), source)
    assert parsed.billing_units[0].currency == "USD"


def test_analytics_stays_cny_only_with_foreign_data_present(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    with client.app.state.session_factory() as session:
        cny_card = _add_card(session, last4="0010", currency="CNY")
        usd_card = _add_card(session, last4="0011", currency="USD")
        _add_statement(
            session,
            card_id=cny_card.id,
            total_due_minor=50000,
            currency="CNY",
            due_date=date(2026, 12, 1),
        )
        _add_statement(
            session,
            card_id=usd_card.id,
            total_due_minor=7777777,
            currency="USD",
            due_date=date(2026, 12, 1),
        )
        session.commit()

    response = client.get("/api/v1/analytics", headers=auth_headers)
    assert response.status_code == 200
    text = response.text
    assert "7777777" not in text


def test_manual_bill_remains_cny_only(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    create = client.post(
        "/api/v1/manual-bills",
        headers=auth_headers,
        json={
            "bill_type": "utility",
            "name": "电费",
            "statement_day": 1,
            "due_day": 15,
            "monthly_amount_minor": 10000,
        },
    )
    if create.status_code == 404:
        listing = client.get("/openapi.json", headers=auth_headers).json()
        manual_route = next(
            (p for p in listing["paths"] if "manual" in p and p.endswith("bills")),
            None,
        )
        assert manual_route, "manual bills route not found"
        create = client.post(
            f"{manual_route}",
            headers=auth_headers,
            json={
                "bill_type": "utility",
                "name": "电费",
                "statement_day": 1,
                "due_day": 15,
                "monthly_amount_minor": 10000,
            },
        )
    assert create.status_code in (200, 201)
    assert "currency" not in create.json()


def _create_statement_payload(card_id: str, currency: str, total: int) -> dict:
    from datetime import timedelta

    today = date(2026, 9, 15)
    return {
        "card_id": card_id,
        "statement_date": today.isoformat(),
        "due_date": (today + timedelta(days=10)).isoformat(),
        "total_due_minor": total,
        "minimum_due_minor": min(total, 12345),
        "currency": currency,
        "parse_status": "summary_ok",
    }


def test_statement_currency_ignores_card_currency_authority(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    card = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "多币卡",
            "last4": "3010",
            "currency": "CNY",
        },
    ).json()

    created = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json=_create_statement_payload(card["id"], "USD", 50000),
    )
    assert created.status_code == 201, created.text
    assert created.json()["currency"] == "USD"


def test_same_card_same_date_multi_currency_statements(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    card = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "双币卡",
            "last4": "3011",
            "currency": "CNY",
        },
    ).json()

    cny = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json=_create_statement_payload(card["id"], "CNY", 12000),
    )
    assert cny.status_code == 201, cny.text

    usd = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json=_create_statement_payload(card["id"], "USD", 30000),
    )
    assert usd.status_code == 201, usd.text

    statements = client.get("/api/v1/statements", headers=auth_headers).json()
    card_statements = [s for s in statements if s["card_id"] == card["id"]]
    currencies = sorted(s["currency"] for s in card_statements)
    assert currencies == ["CNY", "USD"]
