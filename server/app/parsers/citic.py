import re
import warnings
from datetime import date
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

import pymupdf
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from app.parsers.base import ParsedSource, ParsedStatement, ParsedTransaction, ParserError
from app.parsers.cgb import _parse_date, _parse_money_minor


_PDF_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PDF_COMPACT_DATE_RE = re.compile(r"^\d{8}$")
_PDF_MONEY_RE = re.compile(r"^(?:CNY\s*)?(-?[\d,]+\.\d{2})$")
_PDF_CARD_RE = re.compile(r"\b\d{4}-\d{2}\*{2}-\*{4}-(\d{4})\b")


def _next_pdf_date(lines: list[str], label: str) -> date:
    try:
        start = lines.index(label) + 1
    except ValueError as exc:
        raise ParserError(f"CITIC PDF {label} not found") from exc
    for value in lines[start : start + 5]:
        if _PDF_DATE_RE.fullmatch(value):
            return _parse_date(value, label.lower())
    raise ParserError(f"CITIC PDF {label} value not found")


def _next_pdf_money(lines: list[str], label: str, field: str) -> int:
    try:
        start = lines.index(label) + 1
    except ValueError as exc:
        raise ParserError(f"CITIC PDF {label} not found") from exc
    for value in lines[start : start + 6]:
        match = _PDF_MONEY_RE.fullmatch(value)
        if match:
            amount = _parse_money_minor(match.group(1), field, allow_negative=True)
            return max(amount, 0)
    raise ParserError(f"CITIC PDF {label} value not found")


def _pdf_transactions(lines: list[str]) -> tuple[ParsedTransaction, ...]:
    transactions: list[ParsedTransaction] = []
    index = 0
    while index + 2 < len(lines):
        if not (
            _PDF_COMPACT_DATE_RE.fullmatch(lines[index])
            and _PDF_COMPACT_DATE_RE.fullmatch(lines[index + 1])
            and re.fullmatch(r"\d{4}", lines[index + 2])
        ):
            index += 1
            continue

        description: list[str] = []
        amounts: list[str] = []
        cursor = index + 3
        while cursor < len(lines) and cursor < index + 12 and len(amounts) < 2:
            money = _PDF_MONEY_RE.fullmatch(lines[cursor])
            if money:
                amounts.append(money.group(1))
            else:
                description.append(lines[cursor])
            cursor += 1
        if len(amounts) != 2 or not description:
            index += 1
            continue

        transactions.append(
            ParsedTransaction(
                transaction_date=_parse_date(
                    f"{lines[index][:4]}-{lines[index][4:6]}-{lines[index][6:]}",
                    "transaction date",
                ),
                posting_date=_parse_date(
                    f"{lines[index + 1][:4]}-{lines[index + 1][4:6]}-{lines[index + 1][6:]}",
                    "posting date",
                ),
                amount_minor=_parse_money_minor(
                    amounts[-1], "settlement amount", allow_negative=True
                ),
                currency="CNY",
                raw_description=" ".join(description),
            )
        )
        index = cursor
    return tuple(transactions)


class CiticStatementParser:
    bank_code = "CITIC"
    version = "1.1.0"

    def can_handle(self, source: ParsedSource) -> bool:
        address = parseaddr(source.sender)[1].lower()
        domain = address.rpartition("@")[2]
        trusted = domain == "bill.citiccard.com" or domain.endswith(".citiccard.com")
        return trusted and "中信银行信用卡电子账单" in source.subject

    def parse(self, source: ParsedSource) -> ParsedStatement:
        if not self.can_handle(source):
            raise ParserError("Not a recognized CITIC statement")

        raw = source.raw_bytes or (
            source.path.read_bytes()
            if source.path and source.path.exists()
            else None
        )
        if raw:
            message = BytesParser(policy=policy.default).parsebytes(raw)
            for part in message.walk():
                filename = part.get_filename() or ""
                if (
                    part.get_content_type() == "application/pdf"
                    or filename.lower().endswith(".pdf")
                ):
                    pdf_data = part.get_payload(decode=True)
                    if pdf_data:
                        return self.parse_pdf_bytes(pdf_data)

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(source.html, "lxml")
        page_text = soup.get_text(" ", strip=True)

        # 账单日
        stmt_match = re.search(r"账单日\s*[:：]?\s*(\d{4}年\d{1,2}月\d{1,2}日)", page_text)
        if not stmt_match:
            raise ParserError("CITIC statement date not found")
        clean_stmt = stmt_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        stmt_date = _parse_date(clean_stmt, "statement date")

        # 到期还款日
        due_match = re.search(r"到期还款日\s*[:：]?\s*(\d{4}年\d{1,2}月\d{1,2}日)", page_text)
        if not due_match:
            raise ParserError("CITIC due date not found")
        clean_due = due_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        due_date = _parse_date(clean_due, "due date")

        # 本期应还款总额
        amt_match = re.search(r"本期应还款总额\s*[:：]?\s*(?:CNY|RMB)?\s*([\d,]+\.\d{2})", page_text)
        if not amt_match:
            raise ParserError("CITIC total due not found")
        total_due = _parse_money_minor(amt_match.group(1), "total due", allow_negative=True)
        if total_due < 0:
            total_due = 0

        # 本期最低还款额
        min_due = 0
        min_match = re.search(r"本期最低还款额\s*[:：]?\s*(?:CNY|RMB)?\s*([\d,]+\.\d{2})", page_text)
        if min_match:
            min_due = _parse_money_minor(min_match.group(1), "minimum due", allow_negative=True)
            if min_due < 0:
                min_due = 0

        # 卡尾号
        card_match = re.search(r"\b\d{4}-\d{2}\*\*-\*{4}-(\d{4})\b", page_text)
        if not card_match:
            raise ParserError("CITIC card last four digits not found")
        card_last4 = card_match.group(1)

        return ParsedStatement(
            bank_code=self.bank_code,
            card_last4=card_last4,
            statement_date=stmt_date,
            due_date=due_date,
            total_due_minor=total_due,
            minimum_due_minor=min_due,
            currency="CNY",
            credit_limit_minor=None,
            transactions=(),
            warnings=(),
        )

    def parse_pdf_bytes(self, pdf_data: bytes) -> ParsedStatement:
        try:
            doc = pymupdf.open(stream=pdf_data, filetype="pdf")
        except Exception as exc:
            raise ParserError("CITIC PDF could not be opened") from exc
        try:
            lines = [
                line.strip()
                for page in doc
                for line in page.get_text().splitlines()
                if line.strip()
            ]
        finally:
            doc.close()

        statement_date = _next_pdf_date(lines, "Statement Date")
        due_date = _next_pdf_date(lines, "Payment Due Date")
        total_due = _next_pdf_money(lines, "Total Payment", "total due")
        minimum_due = _next_pdf_money(lines, "Min. Payment", "minimum due")
        card_match = next(
            (match for line in lines if (match := _PDF_CARD_RE.search(line))),
            None,
        )
        if card_match is None:
            raise ParserError("CITIC PDF card last four digits not found")
        transactions = _pdf_transactions(lines)
        if not transactions:
            raise ParserError("CITIC PDF transaction details were not found")

        return ParsedStatement(
            bank_code=self.bank_code,
            card_last4=card_match.group(1),
            statement_date=statement_date,
            due_date=due_date,
            total_due_minor=total_due,
            minimum_due_minor=minimum_due,
            currency="CNY",
            credit_limit_minor=None,
            transactions=transactions,
            warnings=("CITIC PDF does not expose a credit limit",),
        )
