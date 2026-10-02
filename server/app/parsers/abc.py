import re
from datetime import date
from email.utils import parseaddr
from bs4 import BeautifulSoup
from app.parsers.base import ParsedSource, ParsedStatement, ParsedTransaction, ParserError, decimal_to_minor
from app.parsers.cgb import _parse_date, _parse_money_minor
from bean_sieve import get_provider
from tempfile import TemporaryDirectory
from pathlib import Path


class AbcStatementParser:
    bank_code = "ABC"
    version = "1.0.0"

    def can_handle(self, source: ParsedSource) -> bool:
        address = parseaddr(source.sender)[1].lower()
        domain = address.rpartition("@")[2]
        trusted = domain == "creditcard.abchina.com.cn" or domain.endswith(".abchina.com.cn")
        return trusted and "中国农业银行金穗信用卡" in source.subject

    def parse(self, source: ParsedSource) -> ParsedStatement:
        if not self.can_handle(source):
            raise ParserError("Not a recognized ABC statement")
        soup = BeautifulSoup(source.html, "lxml")
        page_text = soup.get_text(" ", strip=True)

        # 账单周期
        stmt_match = re.search(
            r"(?:账单周期|Statement Cycle).*?(\d{4}/\d{1,2}/\d{1,2})\s*-\s*(\d{4}/\d{1,2}/\d{1,2})",
            page_text,
        )
        if not stmt_match:
            raise ParserError("ABC statement cycle not found")
        stmt_date = _parse_date(stmt_match.group(2), "statement date")

        # 到期还款日
        due_match = re.search(r"(?:到期还款日|Payment Due Date).*?(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})", page_text)
        if not due_match:
            raise ParserError("ABC due date not found")
        clean_due = due_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        due_date = _parse_date(clean_due, "due date")

        # 卡号
        card_match = re.search(r"(?:卡号\s*\|?\s*)?Card No\s*\|?\s*\d{6}\*{6}(\d{4})", page_text)
        if not card_match:
            raise ParserError("ABC card last four digits not found")
        card_last4 = card_match.group(1)

        limit_match = re.search(
            r"(?:信用额度\s*)?Credit Limit.*?([\d,]+\.\d{2})",
            page_text,
        )
        credit_limit_minor = (
            _parse_money_minor(limit_match.group(1), "credit limit")
            if limit_match
            else None
        )

        # 应还金额
        amt_match = re.search(
            r"(?:本期应还金额\s*\|?\s*)?New Balance.*?(?:人民币\(CNY\)|CNY\)?).*?([¥￥]?\s*[\d,]+\.\d{2})",
            page_text,
        )
        if amt_match:
            total_due = _parse_money_minor(amt_match.group(1), "total due", allow_negative=True)
            if total_due < 0:
                total_due = 0
        else:
            amt_m2 = re.search(r"人民币\s*(?:RMB)?\s*([\d,]+\.\d{2})", page_text)
            if amt_m2:
                total_due = _parse_money_minor(amt_m2.group(1), "total due", allow_negative=True)
                if total_due < 0:
                    total_due = 0
            else:
                raise ParserError("ABC total due amount not found")

        # 借用 bean-sieve
        transactions = []
        with TemporaryDirectory() as td:
            p = Path(td) / "abc.eml"
            if source.raw_bytes:
                p.write_bytes(source.raw_bytes)
            elif source.path and source.path.exists():
                p.write_bytes(source.path.read_bytes())
            else:
                p.write_text(source.html, encoding="utf-8")
            prov = get_provider("abc_credit")
            try:
                t_list = prov.parse(p)
                for t in t_list:
                    transactions.append(
                        ParsedTransaction(
                            transaction_date=t.date,
                            posting_date=t.post_date,
                            amount_minor=decimal_to_minor(t.amount),
                            currency=t.currency,
                            raw_description=t.description,
                        )
                    )
            except ParserError:
                raise
            except Exception:
                pass

        return ParsedStatement(
            bank_code=self.bank_code,
            card_last4=card_last4,
            statement_date=stmt_date,
            due_date=due_date,
            total_due_minor=total_due,
            minimum_due_minor=0,
            currency="CNY",
            credit_limit_minor=credit_limit_minor,
            transactions=tuple(transactions),
            warnings=(),
        )
