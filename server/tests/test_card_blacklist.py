from datetime import date
from email.message import EmailMessage

from app.models.card import Card
from app.parsers.base import ParsedBillingDocument, ParsedBillingUnit
from app.services.card_blacklist import add_ignored_card, is_card_ignored


def test_card_level_ignore_matches_all_currencies_and_unignore_removes_it(
    client, auth_headers
) -> None:
    with client.app.state.session_factory() as session:
        assert add_ignored_card(
            session,
            bank_code="CCB",
            last4="0183",
            currency="CNY",
        )
        assert is_card_ignored(session, "CCB", "0183", "CNY")
        assert is_card_ignored(session, "CCB", "0183", "USD")
        assert is_card_ignored(session, "CCB", "0183", "EUR")
        # The same Card cannot gain a second currency-specific identity entry.
        assert not add_ignored_card(
            session,
            bank_code="CCB",
            last4="0183",
            currency="USD",
        )

    # Keep the legacy query parameter, but remove the Card-level ignore entirely.
    response = client.delete(
        "/api/v1/cards/ignored/CCB/0183?currency=EUR",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["removed"] is True
    with client.app.state.session_factory() as session:
        assert not is_card_ignored(session, "CCB", "0183", "CNY")
        assert not is_card_ignored(session, "CCB", "0183", "USD")
        assert not is_card_ignored(session, "CCB", "0183", "EUR")


def test_legacy_currency_specific_ignore_blocks_usd_billing_unit_import(
    client, auth_headers, monkeypatch
) -> None:
    with client.app.state.session_factory() as session:
        assert add_ignored_card(
            session,
            bank_code="CCB",
            last4="0183",
            currency="CNY",
        )

    class UsdBillingUnitParser:
        bank_code = "CCB"
        version = "supervisor-fix-test"

        def parse_document(self, _source) -> ParsedBillingDocument:
            return ParsedBillingDocument(
                bank_code="CCB",
                statement_date=date(2026, 10, 17),
                due_date=date(2026, 11, 6),
                billing_units=(
                    ParsedBillingUnit(
                        card_ref="0183",
                        currency="USD",
                        total_due_minor=100,
                        minimum_due_minor=10,
                    ),
                ),
                transactions=(),
            )

    monkeypatch.setattr(
        "app.services.eml_import.default_registry.parser_for",
        lambda _source: UsdBillingUnitParser(),
    )
    message = EmailMessage()
    message["Subject"] = "ignored-card-cross-currency-test"
    message["From"] = "test@example.invalid"
    message.set_content("<p>synthetic ignored card test</p>", subtype="html")

    response = client.post(
        "/api/v1/imports/eml",
        headers=auth_headers,
        files={"file": ("ignored.eml", message.as_bytes(), "message/rfc822")},
    )
    assert response.status_code == 422
    assert "CARD_IGNORED" in response.text
    assert "currency=USD" in response.text

    with client.app.state.session_factory() as session:
        assert session.query(Card).all() == []
