import re
from datetime import date
from email.utils import parseaddr
from bs4 import BeautifulSoup
from app.parsers.base import ParsedSource, ParsedStatement, ParsedTransaction, ParserError
from app.parsers.cgb import _parse_date, _parse_money_minor


class PingAnStatementParser:
    bank_code = "PAB"
    version = "1.0.0"

    def can_handle(self, source: ParsedSource) -> bool:
        address = parseaddr(source.sender)[1].lower()
        domain = address.rpartition("@")[2]
        trusted = domain == "service.pingan.com" or domain.endswith(".pingan.com")
        return trusted and "平安信用卡电子账单" in source.subject

    def parse(self, source: ParsedSource) -> ParsedStatement:
        if not self.can_handle(source):
            raise ParserError("Not a recognized PingAn statement")
        soup = BeautifulSoup(source.html, "lxml")
        page_text = soup.get_text(" ", strip=True)

        # 账单日
        stmt_match = re.search(r"本期账单日\s*[:：]?\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})", page_text)
        if not stmt_match:
            raise ParserError("PingAn statement date not found")
        clean_stmt = stmt_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        stmt_date = _parse_date(clean_stmt, "statement date")

        # 还款日
        due_match = re.search(r"本期还款日\s*[:：]?\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})", page_text)
        if not due_match:
            raise ParserError("PingAn due date not found")
        clean_due = due_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        due_date = _parse_date(clean_due, "due date")

        # 应还金额
        amt_match = re.search(r"本期应还金额\s*[:：]?\s*([¥￥]?\s*[\d,]+\.\d{2})", page_text)
        if not amt_match:
            raise ParserError("PingAn total due not found")
        total_due = _parse_money_minor(amt_match.group(1), "total due", allow_negative=True)
        if total_due < 0:
            total_due = 0

        # 最低还款额
        min_due = 0
        min_match = re.search(r"本期最低应还金额\s*[:：]?\s*([¥￥]?\s*[\d,]+\.\d{2})", page_text)
        if min_match:
            min_due = _parse_money_minor(min_match.group(1), "minimum due", allow_negative=True)
            if min_due < 0:
                min_due = 0

        # 卡尾号
        card_match = re.search(r"平安银行.*?（(\d{4})）", page_text)
        if not card_match:
            raise ParserError("PingAn card last four digits not found")
        card_last4 = card_match.group(1)

        limit_match = re.search(r"信用额度\s*[¥￥]\s*([\d,]+\.\d{2})", page_text)
        credit_limit_minor = (
            _parse_money_minor(limit_match.group(1), "credit limit")
            if limit_match
            else None
        )

        # 交易明细
        transactions = []
        for tr in soup.find_all("tr"):
            cells = tr.find_all("td", recursive=False)
            txts = [c.get_text(strip=True) for c in cells if c.get_text(strip=True)]
            if (
                len(txts) >= 4
                and re.match(r"^\d{4}-\d{2}-\d{2}$", txts[0])
                and re.match(r"^\d{4}-\d{2}-\d{2}$", txts[1])
            ):
                t_date = _parse_date(txts[0], "transaction date")
                p_date = _parse_date(txts[1], "posting date")
                desc = txts[2]
                amt = _parse_money_minor(txts[3], "amount", allow_negative=True)
                transactions.append(
                    ParsedTransaction(
                        transaction_date=t_date,
                        posting_date=p_date,
                        amount_minor=amt,
                        currency="CNY",
                        raw_description=desc,
                    )
                )

        return ParsedStatement(
            bank_code=self.bank_code,
            card_last4=card_last4,
            statement_date=stmt_date,
            due_date=due_date,
            total_due_minor=total_due,
            minimum_due_minor=min_due,
            currency="CNY",
            credit_limit_minor=credit_limit_minor,
            transactions=tuple(transactions),
            warnings=(),
        )
