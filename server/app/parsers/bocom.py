import re
from datetime import date
from email.utils import parseaddr
from bs4 import BeautifulSoup
from app.parsers.base import ParsedSource, ParsedStatement, ParsedTransaction, ParserError
from app.parsers.cgb import _parse_date, _parse_money_minor
from bean_sieve import get_provider
from tempfile import TemporaryDirectory
from pathlib import Path


class BocomStatementParser:
    bank_code = "BOCOM"
    version = "1.0.0"

    def can_handle(self, source: ParsedSource) -> bool:
        address = parseaddr(source.sender)[1].lower()
        domain = address.rpartition("@")[2]
        trusted = domain == "bocomcc.com" or domain.endswith(".bocomcc.com")
        return trusted and "交通银行" in source.subject

    def parse(self, source: ParsedSource) -> ParsedStatement:
        if not self.can_handle(source):
            raise ParserError("Not a recognized BOCOM statement")
        soup = BeautifulSoup(source.html, "lxml")
        page_text = soup.get_text(" ", strip=True)

        # 账单周期
        stmt_match = re.search(
            r"账单周期.*?(\d{4}/\d{1,2}/\d{1,2})\s*-\s*(\d{4}/\d{1,2}/\d{1,2})",
            page_text,
        )
        if not stmt_match:
            raise ParserError("BOCOM statement cycle not found")
        stmt_date = _parse_date(stmt_match.group(2), "statement date")

        # 到期还款日
        due_match = re.search(r"到期还款日.*?(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})", page_text)
        if not due_match:
            raise ParserError("BOCOM due date not found")
        clean_due = due_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        due_date = _parse_date(clean_due, "due date")

        # 本期应还款
        amt_match = re.search(r"本期应还款\s*[:：]?\s*([¥￥]?\s*[\d,]+\.\d{2})", page_text)
        if not amt_match:
            raise ParserError("BOCOM total due not found")
        total_due = _parse_money_minor(amt_match.group(1), "total due", allow_negative=True)
        if total_due < 0:
            total_due = 0

        # 最低应还款
        min_due = 0
        min_match = re.search(r"最低应还款\s*[:：]?\s*([¥￥]?\s*[\d,]+\.\d{2})", page_text)
        if min_match:
            min_due = _parse_money_minor(min_match.group(1), "minimum due", allow_negative=True)
            if min_due < 0:
                min_due = 0

        # 卡尾号
        card_match = re.search(r"交通银行个人信用卡.*?\d{4}\*{6}(\d{4})", page_text)
        if not card_match:
            raise ParserError("BOCOM card last four digits not found")
        card_last4 = card_match.group(1)

        limit_match = re.search(
            r"(?:信用额度\s*)?Credit Limit\s*[¥￥]?\s*([\d,]+(?:\.\d{2})?)", page_text
        )
        credit_limit_minor = (
            _parse_money_minor(limit_match.group(1), "credit limit")
            if limit_match
            else None
        )

        # 借用 bean-sieve 提取明细
        transactions = []
        with TemporaryDirectory() as td:
            p = Path(td) / "bocom.eml"
            if source.raw_bytes:
                p.write_bytes(source.raw_bytes)
            elif source.path and source.path.exists():
                p.write_bytes(source.path.read_bytes())
            else:
                p.write_text(source.html, encoding="utf-8")
            prov = get_provider("bocom_credit")
            try:
                t_list = prov.parse(p)
                for t in t_list:
                    transactions.append(
                        ParsedTransaction(
                            transaction_date=t.date,
                            posting_date=t.post_date,
                            amount_minor=int(t.amount * 100),
                            currency=t.currency,
                            raw_description=t.description,
                        )
                    )
            except Exception:
                pass

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
