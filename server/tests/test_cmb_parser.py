from datetime import date

import pytest

from app.parsers.base import ParsedSource, ParserError
from app.parsers.cmb import CmbStatementParser


def _cmb_source(
    *,
    summaries: list[str] | None = None,
    transactions: list[tuple[str, str, str, str, str, str]] | None = None,
) -> ParsedSource:
    if summaries is None:
        summaries = ["200.00"]
    if transactions is None:
        transactions = [
            ("0810", "0811", "消费1", "100.00", "1234", "100.00"),
            ("0810", "0811", "消费2", "100.00", "1234", "100.00"),
        ]

    summary_html = "".join(
        f'<tr><td><img data-code="bqyhje_img"></td><td>{amt}</td></tr>'
        for amt in summaries
    )
    tx_html = "".join(
        f"<tr><td></td><td>{t_d}</td><td>{p_d}</td><td>{desc}</td>"
        f"<td>{amt1}</td><td>{card}</td><td>CNY</td><td>{amt2}</td></tr>"
        for t_d, p_d, desc, amt1, card, amt2 in transactions
    )

    html = f"""
    <table>
      <tr><td><img data-code="dqhkr_img"></td><td>2026/09/25</td></tr>
      <tr><td><img data-code="zdzq_img"></td><td>2026/08/01 - 2026/08/31</td></tr>
      {summary_html}
    </table>
    <table>
      {tx_html}
    </table>
    """
    return ParsedSource(
        source_type="eml",
        subject="招商银行信用卡电子账单",
        sender="statement@message.cmbchina.com",
        html=html,
        sha256="synthetic-cmb",
    )


def test_cmb_single_card_success() -> None:
    parsed = CmbStatementParser().parse(_cmb_source())
    assert parsed.card_last4 == "1234"
    assert parsed.total_due_minor == 20000
    assert parsed.statement_date == date(2026, 8, 31)
    assert parsed.due_date == date(2026, 9, 25)
    assert len(parsed.transactions) == 2
    assert all(tx.card_ref == "1234" for tx in parsed.transactions)
    assert parsed.transactions[0].amount_minor == 10000
    assert parsed.transactions[1].amount_minor == 10000


def test_cmb_multiple_card_references_rejected() -> None:
    source = _cmb_source(
        transactions=[
            ("0810", "0811", "消费1", "100.00", "1234", "100.00"),
            ("0810", "0811", "消费2", "100.00", "5678", "100.00"),
        ]
    )
    with pytest.raises(ParserError, match="MULTIPLE_CARD_REFERENCES"):
        CmbStatementParser().parse(source)


def test_cmb_unknown_card_reference_rejected() -> None:
    source = _cmb_source(
        transactions=[
            ("0810", "0811", "消费1", "100.00", "1234", "100.00"),
            ("0810", "0811", "消费2", "100.00", "", "100.00"),
        ]
    )
    with pytest.raises(ParserError, match="UNRESOLVED_CARD_REFERENCE"):
        CmbStatementParser().parse(source)


def test_cmb_multiple_repayment_summaries_rejected() -> None:
    source = _cmb_source(summaries=["200.00", "200.00"])
    with pytest.raises(ParserError, match="MULTIPLE_REPAYMENT_SUMMARIES"):
        CmbStatementParser().parse(source)


def test_cmb_no_transactions_keeps_original_failure() -> None:
    source = _cmb_source(transactions=[])
    with pytest.raises(ParserError, match="CMB card last four digits not found"):
        CmbStatementParser().parse(source)
