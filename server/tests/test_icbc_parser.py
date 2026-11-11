from datetime import date

import pytest

from app.parsers.base import ParsedSource, ParserError
from app.parsers.icbc import IcbcStatementParser
from app.services.statement_date_semantics import is_verified_icbc_natural_month


def _source(
    *,
    cycle_start: str = "2025年09月01日",
    cycle_end: str = "2025年09月30日",
    generation_date: str = "2025年09月30日",
    due_date: str = "2025年10月25日",
    transaction_date: str = "2025-09-01",
    description: str = "合成消费",
    extra_summaries: list[str] | None = None,
    transactions: list[tuple[str, str, str]] | None = None,
) -> ParsedSource:
    summary_html = """
      <table><tr>
        <td>1646(牡丹贷记卡)</td><td>人民币(本位币)</td>
        <td>100.00/RMB</td><td>10.00/RMB</td><td>32,000.00/RMB</td>
      </tr></table>
    """
    if extra_summaries:
        for s in extra_summaries:
            summary_html += f"<table><tr>{s}</tr></table>"

    if transactions is None:
        tx_html = f"""
      <table><tr>
        <td>1646</td><td>{transaction_date}</td><td>{transaction_date}</td>
        <td>消费</td><td>{description}</td><td>人民币</td><td>100.00/RMB</td>
      </tr></table>
        """
    else:
        tx_rows = "".join(
            f"<tr><td>{card}</td><td>{t_d}</td><td>{t_d}</td><td>消费</td><td>{desc}</td><td>人民币</td><td>100.00/RMB</td></tr>"
            for card, t_d, desc in transactions
        )
        tx_html = f"<table>{tx_rows}</table>"

    html = f"""
    <html><body>
      <div>账单周期&nbsp;&nbsp;{cycle_start}—{cycle_end}</div>
      <div>对账单生成日&nbsp;&nbsp;{generation_date}</div>
      <div>到期还款日 {due_date}</div>
      {summary_html}
      {tx_html}
    </body></html>
    """
    return ParsedSource(
        source_type="eml",
        subject="中国工商银行客户对账单(ICBC Peony Card Bank Statement)",
        sender="statement@creditcard.icbc.com.cn",
        html=html,
        sha256="synthetic-icbc",
    )


def test_icbc_parser_extracts_natural_month_cycle_without_relabeling_generation_date() -> None:
    parsed = IcbcStatementParser().parse(_source())

    assert parsed.billing_cycle_start == date(2025, 9, 1)
    assert parsed.billing_cycle_end == date(2025, 9, 30)
    assert parsed.statement_date == date(2025, 9, 30)
    assert parsed.due_date == date(2025, 10, 25)
    assert is_verified_icbc_natural_month(parsed) is True


def test_icbc_day_one_purchase_remains_in_new_natural_month_statement() -> None:
    parsed = IcbcStatementParser().parse(_source(transaction_date="2025-09-01"))

    assert parsed.transactions[0].transaction_date == date(2025, 9, 1)
    assert parsed.billing_cycle_start == date(2025, 9, 1)
    assert parsed.billing_cycle_end == date(2025, 9, 30)


def test_icbc_system_interest_can_remain_on_previous_physical_statement() -> None:
    parsed = IcbcStatementParser().parse(
        _source(
            cycle_start="2025年08月01日",
            cycle_end="2025年08月31日",
            generation_date="2025年08月31日",
            due_date="2025年9月25日",
            transaction_date="2025-09-01",
            description="透支利息 卡清算中心",
        )
    )

    assert parsed.statement_date == date(2025, 8, 31)
    assert parsed.transactions[0].transaction_date == date(2025, 9, 1)
    assert "透支利息" in parsed.transactions[0].raw_description
    assert parsed.transactions[0].card_ref == "1646"


def test_icbc_multiple_cards_in_transactions_rejected() -> None:
    source = _source(
        transactions=[
            ("1646", "2025-09-01", "消费1"),
            ("8888", "2025-09-02", "消费2"),
        ]
    )
    with pytest.raises(ParserError, match="MULTIPLE_CARD_REFERENCES"):
        IcbcStatementParser().parse(source)


def test_icbc_unknown_or_mismatched_card_in_transactions_rejected() -> None:
    source = _source(
        transactions=[
            ("1646", "2025-09-01", "消费1"),
            ("", "2025-09-02", "消费2"),
        ]
    )
    with pytest.raises(ParserError, match="UNRESOLVED_CARD_REFERENCE"):
        IcbcStatementParser().parse(source)


@pytest.mark.parametrize("summary", [
    "<td>1646(牡丹贷记卡)</td><td>人民币(本位币)</td><td>200.00/RMB</td><td>20.00/RMB</td><td>32,000.00/RMB</td>",
    "<td>8888(牡丹贷记卡)</td><td>人民币(本位币)</td><td>200.00/RMB</td><td>20.00/RMB</td><td>32,000.00/RMB</td>",
    "<td>1646(牡丹贷记卡)</td><td>美元</td><td>10.00/USD</td>",
    "<td>8888(牡丹贷记卡)</td><td>人民币(本位币)</td><td>无法读取</td>",
    "<td>8888（牡丹贷记卡）</td><td>人民币(本位币)</td><td>无法读取</td>",
])
def test_icbc_multiple_repayment_summaries_rejected(summary) -> None:
    with pytest.raises(ParserError, match="MULTIPLE_REPAYMENT_SUMMARIES"):
        IcbcStatementParser().parse(_source(extra_summaries=[summary]))


def test_icbc_empty_description_preserves_column_alignment() -> None:
    parsed = IcbcStatementParser().parse(_source(description=""))
    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].amount_minor == 10000
    assert parsed.transactions[0].card_ref == "1646"
