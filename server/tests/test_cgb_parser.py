from datetime import date
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.parsers.base import ParsedSource, ParserError
from app.parsers.cgb import CgbStatementParser
from app.parsers.eml import load_eml
from app.parsers.registry import ParserRegistry


SYNTHETIC_HTML = """
<!doctype html>
<html><body>
  <table>
    <tr>
      <td>账单周期:2026/08/07-2026/09/06</td>
      <td>账单日:2026/09/06</td>
      <td>个人消费额度:50,000.00</td>
    </tr>
  </table>
  <table>
    <tr>
      <td>卡号末四位</td><td>本期账单金额</td><td>最低还款额</td>
      <td>最后还款日</td><td>入账货币</td><td>存款</td><td>卡片消费额度</td>
    </tr>
    <tr>
      <td>1234</td><td>12,345.67</td><td>1,234.56</td>
      <td>2026/09/26</td><td>人民币</td><td>0.00</td><td>50,000.00</td>
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
    <tr>
      <td>2026/08/12</td><td>2026/08/13</td><td>合成退款</td>
      <td>-20.00</td><td>人民币</td><td>-20.00</td><td>人民币</td>
    </tr>
  </table>
</body></html>
"""


def source(html: str = SYNTHETIC_HTML) -> ParsedSource:
    return ParsedSource(
        source_type="eml",
        subject="广发信用卡 2026年09月电子账单",
        sender="广发银行 <statement@creditcard.cgbchina.com.cn>",
        html=html,
        sha256="synthetic",
    )


def test_cgb_summary_is_parsed_with_exact_minor_units() -> None:
    result = CgbStatementParser().parse(source())

    assert result.bank_code == "CGB"
    assert result.card_last4 == "1234"
    assert result.statement_date == date(2026, 9, 6)
    assert result.due_date == date(2026, 9, 26)
    assert result.total_due_minor == 1_234_567
    assert result.minimum_due_minor == 123_456
    assert result.credit_limit_minor == 5_000_000
    assert result.currency == "CNY"
    assert len(result.transactions) == 2
    assert result.transactions[0].amount_minor == 8_880
    assert result.transactions[0].raw_description == "合成测试商户"
    assert result.transactions[1].amount_minor == -2_000
    assert result.warnings == ()


def test_cgb_parser_rejects_spoofed_sender_domain() -> None:
    spoofed = ParsedSource(
        source_type="eml",
        subject="广发信用卡 2026年09月电子账单",
        sender="statement@cgbchina.com.cn.example.com",
        html=SYNTHETIC_HTML,
        sha256="synthetic",
    )
    assert CgbStatementParser().can_handle(spoofed) is False


def test_cgb_parser_does_not_fall_back_to_numbers_in_disclaimer() -> None:
    html = "<html><body>账单日为5号，最后还款日为25号，消费10000元。</body></html>"
    with pytest.raises(ParserError, match="summary table"):
        CgbStatementParser().parse(source(html))


def test_registry_has_only_real_supported_bank() -> None:
    registry = ParserRegistry([CgbStatementParser()])
    assert registry.supported_banks == ("CGB",)
    assert registry.parse(source()).bank_code == "CGB"


def test_gbk_eml_loader(tmp_path: Path) -> None:
    message = EmailMessage()
    message["Subject"] = "广发信用卡 2026年09月电子账单"
    message["From"] = "statement@cgbchina.com.cn"
    message["To"] = "private@example.invalid"
    message.set_content(SYNTHETIC_HTML, subtype="html", charset="gbk")
    path = tmp_path / "synthetic.eml"
    path.write_bytes(message.as_bytes())

    loaded = load_eml(path)
    assert loaded.subject == "广发信用卡 2026年09月电子账单"
    assert "本期账单金额" in loaded.html
    assert loaded.sender == "statement@cgbchina.com.cn"
