from datetime import date
from email.message import EmailMessage

import pytest
from bs4 import BeautifulSoup

from app.models.card import Card
from app.models.import_record import ImportRecord, ImportStatus
from app.models.statement import Statement
from app.parsers import default_registry
from app.parsers.base import (
    ParsedSource,
    ParserError,
    billing_document_from_statement,
)
from app.parsers.ccb import (
    CcbStatementParser,
    _extract_billing_units,
    _extract_minimum_due,
    _extract_total_due,
)
from app.parsers.eml import parse_eml_bytes


# Synthetic amounts and layout: the old flattened-text regex selects 123.45.
SUMMARY = """
<table>
  <tr><td>账户币种 Currency</td><td>上期全部应还款额 Last Statement Balance</td>
      <td>+</td><td>消费 New Spending</td><td>-</td><td>还款 Payment/Credit</td>
      <td>=</td><td>本期全部应还款额<br>New Balance</td></tr>
  <tr><td>人民币（CNY）</td><td>123.45</td><td></td><td>10.00</td>
      <td></td><td>65.56</td><td></td><td>67.89</td></tr>
</table>
"""


def source(summary: str = SUMMARY) -> ParsedSource:
    message = EmailMessage()
    message["Subject"] = "中国建设银行信用卡电子账单"
    message["From"] = "service@vip.ccb.com"
    message.set_content(
            "<div>本期账单日 2025-03-15</div>"
            "<div>本期到期还款日 2025-04-04</div>"
            "<div>最低还款额 Min.Payment 12.34</div>" + summary + """
            <table><tr><td>【交易明细】</td></tr>
              <tr><td>2025-03-02</td><td>2025-03-03</td><td>1234</td>
                  <td>synthetic purchase</td><td>CNY</td><td>10.00</td>
                  <td>CNY</td><td>10.00</td></tr>
            </table>""",
        subtype="html",
    )
    return parse_eml_bytes(message.as_bytes())


def multi_source(
    *,
    account_total: str = "406.80",
    second_transaction_ref: str = "6571/扫码",
) -> ParsedSource:
    message = EmailMessage()
    message["Subject"] = "中国建设银行信用卡电子账单"
    message["From"] = "service@vip.ccb.com"
    message.set_content(
        f"""
        <div>本期账单日 2024-10-17</div>
        <div>本期到期还款日 2024-11-06</div>
        <table>
          <tr><th>账户币种 Currency</th><th>上期全部应还款额</th>
              <th>+ 消费/取现/其它费用</th><th>- 还款/退货/费用返还</th>
              <th>本期全部应还款额 New Balance</th><th>最低还款额 Min.Payment</th></tr>
          <tr><td>人民币（CNY）</td><td>619.93</td><td>406.80</td>
              <td>619.93</td><td>{account_total}</td><td>203.40</td></tr>
        </table>
        <table>
          <tr><td colspan="4">【应还款明细】请按以下卡号、币种分别还款</td></tr>
          <tr><th>信用卡卡号 Card Number</th><th>账户币种 Currency</th>
              <th>应还款额/溢缴款 New Balance</th><th>最低还款额 Min.Payment</th></tr>
          <tr><td>62216823****0183</td><td>人民币(CNY)</td><td>3.40</td><td>3.40</td></tr>
          <tr><td>62270803****6571</td><td>人民币(CNY)</td><td>403.40</td><td>200.00</td></tr>
        </table>
        <table><tr><td>【交易明细】</td></tr>
          <tr><td>2024-09-20</td><td>2024-09-21</td><td>0183/扫码</td>
              <td>0183 settlement</td><td>USD</td><td>0.48</td>
              <td>CNY</td><td>3.40</td></tr>
          <tr><td>2024-09-22</td><td>2024-09-23</td><td>{second_transaction_ref}</td>
              <td>6571 settlement</td><td>CNY</td><td>403.40</td>
              <td>CNY</td><td>403.40</td></tr>
        </table>
        """,
        subtype="html",
    )
    return parse_eml_bytes(message.as_bytes())


def test_ccb_total_due_follows_header_column_and_preserves_other_fields():
    parsed = CcbStatementParser().parse(source())
    assert parsed.total_due_minor == 6789
    assert parsed.minimum_due_minor == 1234
    assert parsed.statement_date == date(2025, 3, 15)
    assert parsed.due_date == date(2025, 4, 4)
    assert parsed.card_last4 == "1234"
    assert len(parsed.transactions) == 1
    transaction = parsed.transactions[0]
    assert transaction.transaction_date == date(2025, 3, 2)
    assert transaction.posting_date == date(2025, 3, 3)
    assert transaction.amount_minor == 1000
    assert transaction.currency == "CNY"
    assert transaction.raw_description == "synthetic purchase"


def test_legacy_single_card_statement_wraps_as_one_billing_unit():
    legacy_statement = CcbStatementParser().parse(source())
    document = billing_document_from_statement(legacy_statement)
    assert len(document.billing_units) == 1
    assert document.billing_units[0].card_ref == "1234"
    assert document.billing_units[0].total_due_minor == 6789


def test_ccb_multi_card_document_has_two_units_and_settlement_transactions():
    document = default_registry.parse(multi_source())
    assert document.billing_units == ()
    assert len(document.account_summaries) == 1
    assert document.account_summaries[0].total_due_minor == 40_680
    assert document.account_summaries[0].minimum_due_minor == 20_340
    # The first row is USD original currency, but CCM persists CCB settlement facts.
    assert document.transactions[0].amount_minor == 340
    assert document.transactions[0].currency == "CNY"


def test_ccb_multi_card_import_is_one_record_two_statements_without_duplication(
    client, auth_headers
):
    raw = multi_source().raw_bytes
    files = {"file": ("ccb-multi.eml", raw, "message/rfc822")}
    first = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert len(payload["statements"]) == 1
    statement = payload["statements"][0]
    assert statement["bank_code"] == "CCB"
    assert statement["card_id"] is None
    assert statement["total_due_minor"] == 40_680

    transactions = client.get(
        "/api/v1/transactions", headers=auth_headers
    ).json()
    assert len(transactions) == 2
    for tx in transactions:
        assert tx["statement_id"] == statement["id"]

    replay = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert replay.status_code == 200, replay.text
    assert replay.json()["deduplicated"] is True
    assert len(replay.json()["statements"]) == 1
    assert len(client.get("/api/v1/transactions", headers=auth_headers).json()) == 2


def test_ccb_account_summary_mismatch_fails_atomically(client, auth_headers):
    # In CCB v2, if total due amounts conflict in HTML tables, it raises ParserError
    conflicting_source = source(
        SUMMARY + SUMMARY.replace("67.89", "88.88")
    )
    files = {
        "file": (
            "ccb-conflict.eml",
            conflicting_source.raw_bytes,
            "message/rfc822",
        )
    }
    response = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert response.status_code == 422
    assert "CCB total due amounts conflict" in response.text
    assert client.get("/api/v1/cards", headers=auth_headers).json() == []
    assert client.get("/api/v1/statements", headers=auth_headers).json() == []
    assert client.get("/api/v1/transactions", headers=auth_headers).json() == []


def test_ccb_unresolved_transaction_neither_attaches_nor_creates_card(
    client, auth_headers
):
    files = {
        "file": (
            "ccb-unresolved.eml",
            multi_source(second_transaction_ref="0515").raw_bytes,
            "message/rfc822",
        )
    }
    response = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert response.status_code == 200, response.text
    # No Card created from unresolved 0515
    assert client.get("/api/v1/cards", headers=auth_headers).json() == []
    # Account statement created
    stmts = client.get("/api/v1/statements", headers=auth_headers).json()
    assert len(stmts) == 1
    assert stmts[0]["card_id"] is None
    # Transactions created with card_id is None
    txs = client.get("/api/v1/transactions", headers=auth_headers).json()
    assert len(txs) == 2
    assert all(tx["card_id"] is None for tx in txs)


def test_card_candidate_lookup_ignores_card_currency(client, auth_headers):
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CCB",
            bank_name="中国建设银行",
            display_name="CCB existing USD card",
            last4="0183",
            currency="USD",
        )
        session.add(card)
        session.commit()
        existing_card_id = card.id

    files = {
        "file": ("ccb-multi.eml", multi_source().raw_bytes, "message/rfc822")
    }
    response = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert response.status_code == 200, response.text
    statements = response.json()["statements"]
    assert len(statements) == 1
    assert statements[0]["card_id"] is None
    # 0183 transaction is linked to existing card regardless of card.currency
    txs = client.get("/api/v1/transactions", headers=auth_headers).json()
    tx_0183 = next(tx for tx in txs if "0183" in tx["raw_description"])
    assert tx_0183["card_id"] == existing_card_id
    with client.app.state.session_factory() as session:
        cards = session.query(Card).all()
        assert len(cards) == 1




def test_hard_delete_one_card_preserves_shared_source_and_surviving_statement(
    client, auth_headers
):
    with client.app.state.session_factory() as session:
        card_a = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 0183", last4="0183", currency="CNY")
        card_b = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 6571", last4="6571", currency="CNY")
        session.add_all([card_a, card_b])
        session.commit()
        card_a_id = card_a.id
        card_b_id = card_b.id

    raw = multi_source().raw_bytes
    files = {"file": ("ccb-shared-source.eml", raw, "message/rfc822")}
    imported = client.post(
        "/api/v1/imports/eml", headers=auth_headers, files=files
    )
    assert imported.status_code == 200, imported.text
    payload = imported.json()
    import_id = payload["import_id"]
    stmt_id = payload["statements"][0]["id"]

    deleted = client.delete(
        f"/api/v1/cards/{card_a_id}?mode=hard", headers=auth_headers
    )
    assert deleted.status_code == 200, deleted.text
    assert client.get(
        f"/api/v1/cards/{card_a_id}", headers=auth_headers
    ).status_code == 404
    assert client.get(
        f"/api/v1/cards/{card_b_id}", headers=auth_headers
    ).status_code == 200
    # Account statement survives!
    assert client.get(
        f"/api/v1/statements/{stmt_id}", headers=auth_headers
    ).status_code == 200

    # tx 0183 card_id is set to None, tx 6571 card_id remains card_b_id
    txs = client.get("/api/v1/transactions", headers=auth_headers).json()
    by_desc = {tx["raw_description"]: tx for tx in txs}
    assert by_desc["0183 settlement"]["card_id"] is None
    assert by_desc["6571 settlement"]["card_id"] == card_b_id

    with client.app.state.session_factory() as session:
        record = session.get(ImportRecord, import_id)
        assert record is not None
        assert record.parse_status != ImportStatus.DELETED_BY_USER

    replay = client.post(
        "/api/v1/imports/eml", headers=auth_headers, files=files
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["deduplicated"] is True
    assert replay.json()["statements"][0]["id"] == stmt_id


def test_ccb_total_due_column_can_precede_previous_balance():
    html = """<table>
      <tr><th>账户币种 Currency</th><th>本期全部应还款额 New Balance</th>
          <th>上期余额 Last Statement Balance</th></tr>
      <tr><td>美元 USD</td><td>0.00</td><td>444.00</td></tr>
      <tr><td>人民币 CNY</td><td>67.89</td><td>123.45</td></tr>
    </table>"""
    assert CcbStatementParser().parse(source(html)).total_due_minor == 6789


def test_ccb_nested_layout_uses_plain_payment_table_after_merged_reconciliation():
    merged = SUMMARY.replace(
        "<td></td><td>10.00</td>", '<td colspan="2">10.00</td>'
    ).replace(
        "<td></td><td>65.56</td>", '<td colspan="2">65.56</td>'
    ).replace(
        "<td></td><td>67.89</td>", '<td colspan="2">67.89</td>'
    )
    payment = """<table><tbody>
      <tr><td colspan="3">应还款信息 Payment Information</td></tr>
      <tr><td>账户币种 Currency</td><td>本期全部应还款额 New Balance</td>
          <td>最低还款额 Min.Payment</td></tr>
      <tr><td>人民币（CNY）</td><td>67.89</td><td>12.34</td></tr>
    </tbody></table>"""
    html = f"<table><tr><td>{merged}{payment}</td></tr></table>"
    parsed = CcbStatementParser().parse(source(html))
    assert parsed.total_due_minor == 6789
    assert parsed.minimum_due_minor == 1234


@pytest.mark.parametrize("amount, expected", [("0.00", 0), ("-12.34", 0)])
def test_ccb_explicit_same_node_amount_preserves_zero_and_credit(amount, expected):
    parsed = CcbStatementParser().parse(source(f"<div>本期全部应还款额 {amount}</div>"))
    assert parsed.total_due_minor == expected


@pytest.mark.parametrize("html", [
    "<div>本期全部应还款额</div><div>上期余额 123.45</div>",
    SUMMARY.replace("67.89", "--"),
    SUMMARY.replace("67.89", "10 20.00"),
    SUMMARY.replace("67.89", "<span>10</span><span>20.00</span>"),
    "<div>本期全部应还款额 10 20.00</div>",
    "<div><section>本期全部应还款额</section><section>67.89</section></div>",
    '<table><tr><td rowspan="2">说明</td></tr>'
    '<tr><td>账户币种 Currency</td><td>本期全部应还款额 New Balance</td></tr>'
    '<tr><td>人民币 CNY</td><td>67.89</td></tr></table>',
    SUMMARY.replace("人民币（CNY）", "美元 USD"),
    SUMMARY.replace("<td>67.89</td>", '<td colspan="2">67.89</td>'),
    SUMMARY.replace("<td>账户币种", '<td rowspan="2">账户币种'),
    SUMMARY + SUMMARY.replace("67.89", "88.88"),
    "<table><tr><td>本期全部应还款额 New Balance</td></tr></table>"
    "<table><tr><td>人民币 CNY</td><td>123.45</td></tr></table>",
])
def test_ccb_total_due_rejects_unresolved_or_conflicting_structure(html):
    with pytest.raises(ParserError, match="CCB total due"):
        CcbStatementParser().parse(source(html))


def test_ccb_reimport_corrects_legacy_summary_without_duplicating_transactions(client, auth_headers):
    files = {"file": ("synthetic.eml", source().raw_bytes, "message/rfc822")}
    first = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert first.status_code == 200
    statement_id = first.json()["statement"]["id"]
    transactions = client.get("/api/v1/transactions", headers=auth_headers).json()
    assert len(transactions) == 1
    # Simulate the previous parser's persisted summary, using synthetic values.
    with client.app.state.session_factory() as session:
        session.get(Statement, statement_id).total_due_minor = 12345
        session.commit()

    replay = client.post("/api/v1/imports/eml", headers=auth_headers, files=files)
    assert replay.status_code == 409, replay.text
    detail = replay.json()["detail"]
    assert detail["code"] == "IMPORT_CONFIRMATION_REQUIRED"
    token = detail["confirmation_token"]
    assert token is not None
    assert len(detail["differences"]) == 1
    diff = detail["differences"][0]
    assert diff["old"]["total_due_minor"] == 12345
    assert diff["new"]["total_due_minor"] == 6789
    assert diff["blocked_reason"] is None

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
    assert payload["statement"]["id"] == statement_id
    assert payload["statement"]["total_due_minor"] == 6789
    assert payload["parser_version"] == "1.1.0"
    assert client.get("/api/v1/transactions", headers=auth_headers).json() == transactions


REPEATED_HEADER = (
    "<tr><th>信用卡卡号 Card Number</th><th>账户币种 Currency</th>"
    "<th>应还款额/溢缴款 New Balance</th><th>最低还款额 Min.Payment</th>"
    "<th>账户币种 Currency</th><th>应还款额/溢缴款 New Balance</th>"
    "<th>最低还款额 Min.Payment</th></tr>"
)


def billing_section(rows: str, header: str = REPEATED_HEADER) -> str:
    return (
        '<table><tr><td colspan="7">'
        "【应还款明细】请按以下卡号、币种分别还款</td></tr>"
        + header
        + rows
        + "</table>"
    )


def test_ccb_repeated_billing_columns_skip_empty_second_group():
    html = billing_section(
        "<tr><td>62270803****6571</td><td>人民币(CNY)</td><td>8.00</td><td>8.00</td>"
        "<td></td><td></td><td></td></tr>"
    )
    units = _extract_billing_units(BeautifulSoup(html, "lxml"))
    assert [
        (u.card_ref, u.currency, u.total_due_minor, u.minimum_due_minor) for u in units
    ] == [("6571", "CNY", 800, 800)]


def test_ccb_repeated_billing_columns_preserve_nonempty_foreign_group():
    row = (
        "<tr><td>62270803****6571</td><td>人民币(CNY)</td><td>8.00</td><td>8.00</td>"
        "<td>美元(USD)</td><td>10.00</td><td>10.00</td></tr>"
    )
    units = _extract_billing_units(BeautifulSoup(billing_section(row), "lxml"))
    assert [
        (u.card_ref, u.currency, u.total_due_minor, u.minimum_due_minor) for u in units
    ] == [("6571", "CNY", 800, 800), ("6571", "USD", 1000, 1000)]
    # The observed foreign group is still rejected by the existing policy.
    with pytest.raises(ParserError, match="UNSUPPORTED_FOREIGN_PAYABLE"):
        CcbStatementParser().parse(source(SUMMARY + billing_section(row)))


def test_ccb_repeated_billing_columns_extract_multiple_cards():
    html = billing_section(
        "<tr><td>62216823****0183</td><td>人民币(CNY)</td><td>3.40</td><td>3.40</td>"
        "<td></td><td></td><td></td></tr>"
        "<tr><td>62270803****6571</td><td>人民币(CNY)</td><td>403.40</td><td>200.00</td>"
        "<td></td><td></td><td></td></tr>"
    )
    units = _extract_billing_units(BeautifulSoup(html, "lxml"))
    assert [
        (u.card_ref, u.currency, u.total_due_minor, u.minimum_due_minor) for u in units
    ] == [("0183", "CNY", 340, 340), ("6571", "CNY", 40340, 20000)]


def test_ccb_repeated_billing_columns_reject_duplicate_card_currency():
    html = billing_section(
        "<tr><td>62270803****6571</td><td>人民币(CNY)</td><td>8.00</td><td>8.00</td>"
        "<td></td><td></td><td></td></tr>"
        "<tr><td>62270803****6571</td><td>人民币(CNY)</td><td>9.00</td><td>9.00</td>"
        "<td></td><td></td><td></td></tr>"
    )
    with pytest.raises(ParserError, match="duplicate card and currency"):
        _extract_billing_units(BeautifulSoup(html, "lxml"))


def test_ccb_billing_unit_card_anchor_must_be_unique():
    missing = billing_section(
        "",
        header=(
            "<tr><th>账户币种 Currency</th><th>应还款额/溢缴款 New Balance</th>"
            "<th>最低还款额 Min.Payment</th></tr>"
        ),
    )
    # A semantic header without a card anchor yields no groups, so the
    # section guard still fails closed.
    with pytest.raises(ParserError, match="could not be resolved safely"):
        _extract_billing_units(BeautifulSoup(missing, "lxml"))
    duplicated = billing_section(
        "",
        header=(
            "<tr><th>信用卡卡号 Card Number</th><th>Card Number</th>"
            "<th>账户币种 Currency</th><th>应还款额/溢缴款 New Balance</th>"
            "<th>最低还款额 Min.Payment</th></tr>"
        ),
    )
    with pytest.raises(ParserError, match="card anchor is ambiguous"):
        _extract_billing_units(BeautifulSoup(duplicated, "lxml"))


def test_ccb_billing_unit_malformed_triplet_header_fails_closed():
    html = billing_section(
        "<tr><td>62270803****6571</td><td>人民币(CNY)</td><td>8.00</td>"
        "<td>美元(USD)</td><td>10.00</td><td>10.00</td><td></td></tr><tr></tr>",
        header=(
            "<tr><th>信用卡卡号 Card Number</th><th>账户币种 Currency</th>"
            "<th>应还款额/溢缴款 New Balance</th><th>账户币种 Currency</th>"
            "<th>应还款额/溢缴款 New Balance</th><th>最低还款额 Min.Payment</th>"
            "<th></th></tr>"
        ),
    )
    with pytest.raises(ParserError, match="header is malformed"):
        _extract_billing_units(BeautifulSoup(html, "lxml"))


def test_ccb_billing_unit_section_without_units_still_fails_closed():
    html = billing_section(
        "<tr><td>62270803****6571</td><td></td><td></td><td></td>"
        "<td></td><td></td><td></td></tr>"
    )
    with pytest.raises(
        ParserError, match="CCB billing unit section could not be resolved safely"
    ):
        _extract_billing_units(BeautifulSoup(html, "lxml"))


def test_ccb_zero_amount_dash_placeholder_parses_to_zero():
    # CCB renders a fully repaid / zero-payable account as a dash placeholder.
    html = (
        "<table><tr><td>账户币种 Currency</td><td>上期全部应还款额 Last Statement Balance</td>"
        "<td>+</td><td>消费 New Spending</td><td>-</td><td>还款 Payment/Credit</td>"
        "<td>=</td><td>本期全部应还款额<br>New Balance</td></tr>"
        "<tr><td>人民币（CNY）</td><td>0.00</td><td></td><td>0.00</td>"
        "<td></td><td>0.00</td><td></td><td>-</td></tr></table>"
    )
    soup = BeautifulSoup(html, "lxml")
    assert _extract_total_due(soup) == 0
    assert _extract_minimum_due(soup) == 0


def zero_amount_source() -> ParsedSource:
    message = EmailMessage()
    message["Subject"] = "中国建设银行信用卡电子账单"
    message["From"] = "service@vip.ccb.com"
    summary = (
        "<table><tr><td>账户币种 Currency</td><td>上期全部应还款额 Last Statement Balance</td>"
        "<td>+</td><td>消费 New Spending</td><td>-</td><td>还款 Payment/Credit</td>"
        "<td>=</td><td>本期全部应还款额<br>New Balance</td></tr>"
        "<tr><td>人民币（CNY）</td><td>0.00</td><td></td><td>0.00</td>"
        "<td></td><td>0.00</td><td></td><td>-</td></tr></table>"
    )
    message.set_content(
        "<div>本期账单日 2025-03-15</div>"
        "<div>本期到期还款日 2025-04-04</div>"
        "<div>最低还款额 Min.Payment 0.00</div>"
        + summary
        + """
            <table><tr><td>【交易明细】</td></tr>
              <tr><td>2025-03-02</td><td>2025-03-03</td><td>6571</td>
                  <td>zero repayment</td><td>CNY</td><td>-197.58</td>
                  <td>CNY</td><td>-197.58</td></tr>
            </table>""",
        subtype="html",
    )
    return parse_eml_bytes(message.as_bytes())


def test_ccb_zero_amount_dash_document_parses_successfully():
    parsed = CcbStatementParser().parse(zero_amount_source())
    assert parsed.total_due_minor == 0
    assert parsed.minimum_due_minor == 0

    document = default_registry.parse(zero_amount_source())
    assert len(document.account_summaries) == 1
    assert document.account_summaries[0].total_due_minor == 0
    assert document.account_summaries[0].minimum_due_minor == 0
