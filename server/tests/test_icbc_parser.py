from datetime import date

from app.parsers.base import ParsedSource
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
) -> ParsedSource:
    html = f"""
    <html><body>
      <div>账单周期&nbsp;&nbsp;{cycle_start}—{cycle_end}</div>
      <div>对账单生成日&nbsp;&nbsp;{generation_date}</div>
      <div>到期还款日 {due_date}</div>
      <table><tr>
        <td>1646(牡丹贷记卡)</td><td>人民币(本位币)</td>
        <td>100.00/RMB</td><td>10.00/RMB</td><td>32,000.00/RMB</td>
      </tr></table>
      <table><tr>
        <td>1646</td><td>{transaction_date}</td><td>{transaction_date}</td>
        <td>消费</td><td>{description}</td><td>人民币</td><td>100.00/RMB</td>
      </tr></table>
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
