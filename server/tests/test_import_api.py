from datetime import date
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from app.parsers.base import ParsedStatement, ParsedTransaction
from app.models.card import Card
from app.services.eml_import import _refresh_card_metadata


def synthetic_cgb_eml() -> bytes:
    html = """
    <html><body>
      <table><tr>
        <td>账单周期:2026/08/07-2026/09/06</td>
        <td>账单日:2026/09/06</td>
      </tr></table>
      <table>
        <tr>
          <td>卡号末四位</td><td>本期账单金额</td><td>最低还款额</td>
          <td>最后还款日</td><td>入账货币</td><td>存款</td><td>卡片消费额度</td>
        </tr>
        <tr>
          <td>5678</td><td>8,231.42</td><td>823.14</td>
          <td>2026/09/26</td><td>人民币</td><td>0.00</td><td>80,000.00</td>
        </tr>
      </table>
      <table>
        <tr>
          <td>交易日期</td><td>入账日期</td><td>交易摘要</td><td>交易金额</td>
          <td>交易货币</td><td>入账金额</td><td>入账货币</td>
        </tr>
        <tr>
          <td>2026/08/10</td><td>2026/08/11</td><td>合成测试商户</td>
          <td>88.80</td><td>人民币</td><td>88.80</td><td>人民币</td>
        </tr>
      </table>
    </body></html>
    """
    message = EmailMessage()
    message["Subject"] = "广发信用卡 2026年09月电子账单"
    message["From"] = "statement@cgbchina.com.cn"
    message["To"] = "private@example.invalid"
    message["Message-ID"] = "<synthetic-cgb-202609@example.invalid>"
    message.set_content(html, subtype="html", charset="gbk")
    return message.as_bytes()


def test_eml_import_creates_statement_and_is_idempotent(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    eml = synthetic_cgb_eml()
    files = {"file": ("statement.eml", eml, "message/rfc822")}

    first = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert first_payload["bank_code"] == "CGB"
    assert first_payload["parser_version"] == "1.0.0"
    assert first_payload["deduplicated"] is False
    assert first_payload["statement"]["total_due_minor"] == 823142
    assert first_payload["statement"]["parse_status"] == "complete"
    assert first_payload["statement"]["source_import_id"] == first_payload["import_id"]

    second = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert second_payload["deduplicated"] is True
    assert second_payload["import_id"] == first_payload["import_id"]
    assert second_payload["statement"]["id"] == first_payload["statement"]["id"]

    cards = client.get("/api/v1/cards", headers=auth_headers).json()
    statements = client.get("/api/v1/statements", headers=auth_headers).json()
    transactions = client.get(
        "/api/v1/transactions", headers=auth_headers
    ).json()
    assert len(cards) == 1
    assert cards[0]["last4"] == "5678"
    assert cards[0]["credit_limit_minor"] == 8_000_000
    assert cards[0]["statement_day"] == 6
    assert cards[0]["due_day"] == 26
    assert len(statements) == 1
    assert len(transactions) == 1
    assert transactions[0]["amount_minor"] == 8880
    assert transactions[0]["raw_description"] == "合成测试商户"
    assert transactions[0]["transaction_type"] == "purchase"
    assert transactions[0]["classification_source"] == "rule"
    assert transactions[0]["classification_rule_version"]


def test_reimport_with_new_parser_refreshes_summary_and_only_adds_missing_transactions(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    eml = synthetic_cgb_eml()
    files = {"file": ("statement.eml", eml, "message/rfc822")}
    first = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert first.status_code == 200, first.text

    original_transactions = client.get(
        "/api/v1/transactions", headers=auth_headers
    ).json()
    assert len(original_transactions) == 1
    original_transaction_id = original_transactions[0]["id"]
    manual = client.patch(
        f"/api/v1/transactions/{original_transaction_id}/semantics",
        headers=auth_headers,
        json={"transaction_type": "reward"},
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["classification_source"] == "manual"

    class UpdatedParser:
        bank_code = "CGB"
        version = "1.1.0-test"

        def parse(self, _source) -> ParsedStatement:
            return ParsedStatement(
                bank_code="CGB",
                card_last4="5678",
                statement_date=date(2026, 9, 6),
                due_date=date(2026, 9, 27),
                total_due_minor=823_142,
                minimum_due_minor=82_315,
                currency="CNY",
                # A missing source limit must not erase existing/manual card metadata.
                credit_limit_minor=None,
                warnings=("Synthetic source does not expose a credit limit",),
                transactions=(
                    ParsedTransaction(
                        transaction_date=date(2026, 8, 10),
                        posting_date=date(2026, 8, 11),
                        amount_minor=8_880,
                        currency="CNY",
                        raw_description="合成测试商户",
                    ),
                    ParsedTransaction(
                        transaction_date=date(2026, 8, 12),
                        posting_date=date(2026, 8, 13),
                        amount_minor=-2_000,
                        currency="CNY",
                        raw_description="合成测试还款",
                    ),
                    ParsedTransaction(
                        transaction_date=date(2026, 8, 14),
                        posting_date=date(2026, 8, 15),
                        amount_minor=48_000,
                        currency="CNY",
                        raw_description="信用卡年费",
                    ),
                ),
            )

    monkeypatch.setattr(
        "app.services.eml_import.default_registry.parser_for",
        lambda _source: UpdatedParser(),
    )

    second = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["code"] == "IMPORT_CONFIRMATION_REQUIRED"
    token = detail["confirmation_token"]
    assert token is not None

    confirmed = client.post(
        "/api/v1/imports/eml",
        headers=auth_headers,
        files=files,
        params={"confirmation_token": token},
    )
    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    assert payload["corrected"] is True
    assert payload["deduplicated"] is False
    assert payload["parser_version"] == "1.1.0-test"
    assert payload["statement"]["due_date"] == "2026-09-27"
    assert payload["statement"]["minimum_due_minor"] == 82_315
    assert payload["statement"]["parse_status"] == "complete"

    transactions = client.get(
        "/api/v1/transactions", headers=auth_headers
    ).json()
    assert len(transactions) == 3
    assert sorted(item["amount_minor"] for item in transactions) == [
        -2_000,
        8_880,
        48_000,
    ]
    preserved = next(item for item in transactions if item["id"] == original_transaction_id)
    assert preserved["transaction_type"] == "reward"
    assert preserved["classification_source"] == "manual"
    assert preserved["classification_rule_version"] is None

    cards = client.get("/api/v1/cards", headers=auth_headers).json()
    assert cards[0]["annual_fee_minor"] == 48000
    assert cards[0]["annual_fee_charge_date"] == "每年8月14日"
    assert cards[0]["annual_fee_source"] == "auto"

    manual = client.patch(
        f"/api/v1/cards/{cards[0]['id']}",
        headers=auth_headers,
        json={
            "annual_fee_minor": 9900,
            "annual_fee_charge_date": "每年1月2日",
        },
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["annual_fee_source"] == "manual"

    # A third import remains idempotent and does not duplicate either row.
    third = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert third.status_code == 200, third.text
    transactions = client.get(
        "/api/v1/transactions", headers=auth_headers
    ).json()
    assert len(transactions) == 3

    cards = client.get("/api/v1/cards", headers=auth_headers).json()
    assert cards[0]["credit_limit_minor"] == 8_000_000
    assert cards[0]["annual_fee_minor"] == 9900
    assert cards[0]["annual_fee_charge_date"] == "每年1月2日"
    assert cards[0]["annual_fee_source"] == "manual"


def test_generic_statement_day_regime_invalidation_cases(client) -> None:
    with client.app.state.session_factory() as session:
        def make_parsed(s_date: date, d_date: date) -> ParsedStatement:
            return ParsedStatement(
                bank_code="ICBC",
                card_last4="1646",
                statement_date=s_date,
                due_date=d_date,
                total_due_minor=10000,
                minimum_due_minor=1000,
                currency="CNY",
                transactions=(),
            )

        # 1. stored=31, incoming=2026-02-28 -> 保持31
        # => statement_day remains 31, due rule does not invalidate
        card_a = Card(
            bank_code="TEST", bank_name="TEST", display_name="TEST 1646", last4="1646",
            statement_day=31, due_day=25, due_date_rule_type="fixed_day", due_date_rule_value=25,
        )
        session.add(card_a)
        session.flush()
        _refresh_card_metadata(session, card_a, make_parsed(date(2026, 2, 28), date(2026, 3, 25)))
        assert card_a.statement_day == 31
        assert card_a.due_day == 25
        assert card_a.due_date_rule_type == "fixed_day"
        assert card_a.due_date_rule_value == 25

        # 2. stored=31, incoming=2028-02-29 (leap year) -> 保持31
        # => statement_day remains 31, due rule does not invalidate
        card_b = Card(
            bank_code="TEST", bank_name="TEST", display_name="TEST 1647", last4="1647",
            statement_day=31, due_day=25, due_date_rule_type="days_after_statement", due_date_rule_value=25,
        )
        session.add(card_b)
        session.flush()
        _refresh_card_metadata(session, card_b, make_parsed(date(2028, 2, 29), date(2028, 3, 25)))
        assert card_b.statement_day == 31
        assert card_b.due_day == 25
        assert card_b.due_date_rule_type == "days_after_statement"
        assert card_b.due_date_rule_value == 25

        # 3. stored=31, incoming=2026-04-30 -> 保持31
        # => statement_day remains 31, due rule does not invalidate
        card_c = Card(
            bank_code="TEST", bank_name="TEST", display_name="TEST 1648", last4="1648",
            statement_day=31, due_day=25, due_date_rule_type="fixed_day", due_date_rule_value=25,
        )
        session.add(card_c)
        session.flush()
        _refresh_card_metadata(session, card_c, make_parsed(date(2026, 4, 30), date(2026, 5, 25)))
        assert card_c.statement_day == 31
        assert card_c.due_day == 25
        assert card_c.due_date_rule_type == "fixed_day"
        assert card_c.due_date_rule_value == 25

        # 4. stored=30, incoming=2026-02-28 -> 保持30，不失效
        card_4 = Card(
            bank_code="TEST", bank_name="TEST", display_name="TEST 1650", last4="1650",
            statement_day=30, due_day=20, due_date_rule_type="fixed_day", due_date_rule_value=20,
        )
        session.add(card_4)
        session.flush()
        _refresh_card_metadata(session, card_4, make_parsed(date(2026, 2, 28), date(2026, 3, 20)))
        assert card_4.statement_day == 30
        assert card_4.due_day == 20
        assert card_4.due_date_rule_type == "fixed_day"
        assert card_4.due_date_rule_value == 20

        # 5. stored=29, incoming=2026-02-28 -> 保持29，不失效
        card_5 = Card(
            bank_code="TEST", bank_name="TEST", display_name="TEST 1651", last4="1651",
            statement_day=29, due_day=19, due_date_rule_type="fixed_day", due_date_rule_value=19,
        )
        session.add(card_5)
        session.flush()
        _refresh_card_metadata(session, card_5, make_parsed(date(2026, 2, 28), date(2026, 3, 19)))
        assert card_5.statement_day == 29
        assert card_5.due_day == 19
        assert card_5.due_date_rule_type == "fixed_day"
        assert card_5.due_date_rule_value == 19

        # 6. stored=30, incoming=2026-03-30 -> 保持30
        card_6 = Card(
            bank_code="TEST", bank_name="TEST", display_name="TEST 1652", last4="1652",
            statement_day=30, due_day=20, due_date_rule_type="fixed_day", due_date_rule_value=20,
        )
        session.add(card_6)
        session.flush()
        _refresh_card_metadata(session, card_6, make_parsed(date(2026, 3, 30), date(2026, 4, 20)))
        assert card_6.statement_day == 30
        assert card_6.due_day == 20
        assert card_6.due_date_rule_type == "fixed_day"
        assert card_6.due_date_rule_value == 20

        # 7. stored=31, incoming=2026-04-17 -> 真变化到17
        # => genuine change: old rule invalidates, statement_day = 17
        card_d = Card(
            bank_code="TEST", bank_name="TEST", display_name="TEST 1649", last4="1649",
            statement_day=31, due_day=25, due_date_rule_type="fixed_day", due_date_rule_value=25,
            statement_day_boundary="current_cycle",
        )
        session.add(card_d)
        session.flush()
        _refresh_card_metadata(session, card_d, make_parsed(date(2026, 4, 17), date(2026, 5, 6)))
        assert card_d.statement_day == 17
        assert card_d.due_day == 6
        assert card_d.due_date_rule_type == "observed_only"
        assert card_d.due_date_rule_value is None
        assert card_d.statement_day_boundary == "uncertain"

        # 8. stored=17, incoming=2026-05-31 -> 真变化到31
        card_e = Card(
            bank_code="CCB", bank_name="CCB", display_name="CCB 0183", last4="0183",
            statement_day=17, due_day=6, due_date_rule_type="fixed_day", due_date_rule_value=6,
            statement_day_boundary="next_cycle",
        )
        session.add(card_e)
        session.flush()
        _refresh_card_metadata(session, card_e, make_parsed(date(2026, 5, 31), date(2026, 6, 25)))
        assert card_e.statement_day == 31
        assert card_e.due_day == 25
        assert card_e.due_date_rule_type == "observed_only"
        assert card_e.due_date_rule_value is None
        assert card_e.statement_day_boundary == "uncertain"

        # 9. card.statement_day is None -> 记录 statement_date.day，不猜 month_end
        card_none = Card(
            bank_code="TEST", bank_name="TEST", display_name="TEST 1653", last4="1653",
            statement_day=None, due_day=None,
        )
        session.add(card_none)
        session.flush()
        _refresh_card_metadata(session, card_none, make_parsed(date(2026, 2, 28), date(2026, 3, 25)))
        assert card_none.statement_day == 28
        assert card_none.due_day == 25


@pytest.mark.parametrize(
    ("generation_date", "cycle_start", "due_date"),
    [
        (date(2026, 2, 28), date(2026, 2, 1), date(2026, 3, 25)),
        (date(2026, 4, 30), date(2026, 4, 1), date(2026, 5, 25)),
        (date(2026, 8, 31), date(2026, 8, 1), date(2026, 9, 25)),
    ],
)
def test_icbc_natural_month_import_preserves_account_semantics(
    client, generation_date: date, cycle_start: date, due_date: date
) -> None:
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="ICBC",
            bank_name="中国工商银行",
            display_name=f"ICBC {generation_date.month:02d}",
            last4=f"{generation_date.month:04d}",
            statement_day=1,
            due_day=25,
            due_date_rule_type="fixed_day",
            due_date_rule_value=25,
            statement_day_boundary="next_cycle",
        )
        session.add(card)
        session.flush()

        parsed = ParsedStatement(
            bank_code="ICBC",
            card_last4=card.last4,
            statement_date=generation_date,
            due_date=due_date,
            total_due_minor=10_000,
            minimum_due_minor=1_000,
            currency="CNY",
            billing_cycle_start=cycle_start,
            billing_cycle_end=generation_date,
        )
        _refresh_card_metadata(session, card, parsed)

        assert card.statement_day == 1
        assert card.statement_day_boundary == "next_cycle"
        assert card.due_date_rule_type == "fixed_day"
        assert card.due_date_rule_value == 25
        assert card.due_day == 25


def test_icbc_new_card_uses_proven_cycle_semantics_not_generation_day(client) -> None:
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="ICBC",
            bank_name="中国工商银行",
            display_name="ICBC new",
            last4="9001",
            statement_day=None,
            due_day=None,
        )
        session.add(card)
        session.flush()

        parsed = ParsedStatement(
            bank_code="ICBC",
            card_last4="9001",
            statement_date=date(2026, 8, 31),
            due_date=date(2026, 9, 25),
            total_due_minor=10_000,
            minimum_due_minor=1_000,
            currency="CNY",
            billing_cycle_start=date(2026, 8, 1),
            billing_cycle_end=date(2026, 8, 31),
        )
        _refresh_card_metadata(session, card, parsed)

        assert card.statement_day == 1
        assert card.statement_day_boundary == "next_cycle"
        assert card.due_day == 25
