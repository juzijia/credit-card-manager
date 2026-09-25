import re
from collections import Counter
from datetime import date
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
from bean_sieve import get_provider

from app.parsers.base import ParsedSource, ParsedStatement, ParsedTransaction, ParserError
from app.parsers.cgb import _parse_date, _parse_money_minor


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CARD_LAST4_RE = re.compile(r"\*{4}\s*(\d{4})$")

_STRONG_REMINDER_KEYWORDS_RE = re.compile(
    r"(?:还款提醒|还款提示|还款通知|到期还款提醒|账单还款提醒)"
)
_ADJUSTMENT_KEYWORDS_RE = re.compile(
    r"(?:调整通知|额度调整|额度变更|调整额度|调额通知|额度提升|额度降低)"
)
_OTHER_NON_STATEMENT_RE = re.compile(
    r"(?:业务通知|业务提醒|消费提醒|交易提醒|账单提醒|营销活动)"
)
_STATEMENT_SUBJECT_RE = re.compile(
    r"中国银行.*?(?:电子账单|电子对账单|电子合并账单|信用卡账单|对账单|Billing Statement)"
)


def _detect_message_type(source: ParsedSource) -> str:
    subject = source.subject or ""
    if _STRONG_REMINDER_KEYWORDS_RE.search(subject):
        return "repayment_reminder"
    if _ADJUSTMENT_KEYWORDS_RE.search(subject):
        return "adjustment_notice"
    if _OTHER_NON_STATEMENT_RE.search(subject):
        return "non_statement"

    if not _STATEMENT_SUBJECT_RE.search(subject):
        return "non_statement"

    html = source.html or ""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if title_m:
        title_text = title_m.group(1)
        if _STRONG_REMINDER_KEYWORDS_RE.search(title_text):
            return "repayment_reminder"
        if _ADJUSTMENT_KEYWORDS_RE.search(title_text):
            return "adjustment_notice"
        if _OTHER_NON_STATEMENT_RE.search(title_text):
            return "non_statement"

    for heading in re.findall(r"<h[1-4][^>]*>(.*?)</h[1-4]>", html, re.IGNORECASE | re.DOTALL):
        if _STRONG_REMINDER_KEYWORDS_RE.search(heading):
            return "repayment_reminder"
        if _ADJUSTMENT_KEYWORDS_RE.search(heading):
            return "adjustment_notice"

    return "monthly_statement"


def _cell(value: object) -> str:
    return " ".join(str(value or "").split())


def _money_from_cell(value: object, field: str) -> int | None:
    match = re.search(r"-?[\d,]+\.\d{2}", _cell(value))
    return _parse_money_minor(match.group(0), field, allow_negative=True) if match else None


def _summary_from_tables(doc: pymupdf.Document) -> dict[str, object]:
    result: dict[str, object] = {}
    if doc.page_count == 0:
        return result
    for table in doc[0].find_tables().tables:
        rows = table.extract()
        if not rows:
            continue
        table_text = " ".join(_cell(cell) for row in rows for cell in row)
        if "Payment Due Date" in table_text and "Statement Closing Date" in table_text:
            for row in rows[1:]:
                if len(row) >= 3 and _ISO_DATE_RE.fullmatch(_cell(row[0])) and _ISO_DATE_RE.fullmatch(_cell(row[1])):
                    result["due_date"] = _parse_date(_cell(row[0]), "due date")
                    result["statement_date"] = _parse_date(_cell(row[1]), "statement date")
                    result["total_due_minor"] = _money_from_cell(row[2], "total due")
                    break
        if "Credit Card No." in table_text:
            for row in rows:
                if len(row) < 4:
                    continue
                card_match = _CARD_LAST4_RE.search(_cell(row[0]))
                total_due = _money_from_cell(row[1], "total due")
                if card_match and total_due is not None:
                    result["card_last4"] = card_match.group(1)
                    result["card_total_due_minor"] = total_due
                    result["minimum_due_minor"] = _money_from_cell(row[3], "minimum due")
                    break
    return result


def _transactions_from_tables(doc: pymupdf.Document) -> tuple[list[ParsedTransaction], str | None]:
    transactions: list[ParsedTransaction] = []
    card_last4: str | None = None
    for page in doc:
        for table in page.find_tables().tables:
            for row in table.extract():
                if len(row) < 6:
                    continue
                transaction_date = _cell(row[0])
                posting_date = _cell(row[1])
                row_card_last4 = _cell(row[2])
                if not _ISO_DATE_RE.fullmatch(transaction_date):
                    continue
                if posting_date and not _ISO_DATE_RE.fullmatch(posting_date):
                    continue
                if re.fullmatch(r"\d{4}", row_card_last4):
                    card_last4 = row_card_last4
                description = _cell(row[3])
                deposit = _money_from_cell(row[4], "deposit")
                expense = _money_from_cell(row[5], "expense")
                if not description or (deposit is None and expense is None):
                    continue
                amount_minor = expense if expense is not None else -abs(deposit or 0)
                transactions.append(
                    ParsedTransaction(
                        transaction_date=_parse_date(transaction_date, "transaction date"),
                        posting_date=(
                            _parse_date(posting_date, "posting date")
                            if posting_date
                            else None
                        ),
                        amount_minor=amount_minor,
                        currency="CNY",
                        raw_description=description,
                    )
                )
    return transactions, card_last4


def _transaction_identity(transaction: ParsedTransaction) -> tuple[object, ...]:
    return (
        transaction.transaction_date,
        transaction.posting_date,
        transaction.amount_minor,
    )


class BocStatementParser:
    bank_code = "BOC"
    version = "1.1.0"

    def can_handle(self, source: ParsedSource) -> bool:
        address = parseaddr(source.sender)[1].lower()
        domain = address.rpartition("@")[2]
        trusted = domain == "bankofchina.com" or domain.endswith(".bankofchina.com")
        if not trusted:
            return False
        return _detect_message_type(source) == "monthly_statement"

    def parse(self, source: ParsedSource) -> ParsedStatement:
        msg_type = _detect_message_type(source)
        if msg_type != "monthly_statement":
            raise ParserError(f"BOC message is {msg_type}, not a monthly statement")
        if not self.can_handle(source):
            raise ParserError("Not a recognized BOC statement")

        pdf_data = None
        raw = source.raw_bytes or (source.path.read_bytes() if source.path and source.path.exists() else None)
        if raw:
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    fn = part.get_filename() or ""
                    if fn.lower().endswith(".pdf"):
                        pdf_data = part.get_payload(decode=True)
                        break
        if not pdf_data:
            raise ParserError("BOC PDF attachment not found")

        return self.parse_pdf_bytes(pdf_data)

    def parse_pdf_bytes(self, pdf_data: bytes) -> ParsedStatement:
        """Parse the same BOC PDF format used by the EML attachment path."""
        try:
            doc = pymupdf.open(stream=pdf_data, filetype="pdf")
        except Exception as exc:
            raise ParserError("BOC PDF could not be opened") from exc
        text = "\n".join(page.get_text() for page in doc)
        p0_header = doc[0].get_text()[:600] if doc.page_count > 0 else ""

        if _STRONG_REMINDER_KEYWORDS_RE.search(p0_header):
            doc.close()
            raise ParserError("BOC PDF attachment is a repayment reminder, not a monthly statement")
        if _ADJUSTMENT_KEYWORDS_RE.search(p0_header):
            doc.close()
            raise ParserError("BOC PDF attachment is an adjustment notice, not a monthly statement")

        table_summary = _summary_from_tables(doc)

        # 到期还款日
        due_date = table_summary.get("due_date")
        if not isinstance(due_date, date):
            due_m = re.search(r"(?:到期还款日|Payment Due Date)\s*[:：]?\s*(\d{4}-\d{2}-\d{2})", text)
            if not due_m:
                block_m = re.search(
                    r"(?:到期还款日|Payment Due Date)[\s\S]{1,150}?(?:账单日|Statement Closing Date)[\s\S]{1,250}?(\d{4}-\d{2}-\d{2})\s*\n\s*(\d{4}-\d{2}-\d{2})",
                    text,
                )
                if block_m:
                    due_date = _parse_date(block_m.group(1), "due date")
            else:
                due_date = _parse_date(due_m.group(1), "due date")

        if not isinstance(due_date, date):
            doc.close()
            raise ParserError("BOC due date not found")

        # 账单日
        stmt_date = table_summary.get("statement_date")
        if not isinstance(stmt_date, date):
            stmt_m = re.search(r"(?:账单日|Statement Closing Date)\s*[:：]?\s*(\d{4}-\d{2}-\d{2})", text)
            if not stmt_m:
                block_m = re.search(
                    r"(?:到期还款日|Payment Due Date)[\s\S]{1,150}?(?:账单日|Statement Closing Date)[\s\S]{1,250}?(\d{4}-\d{2}-\d{2})\s*\n\s*(\d{4}-\d{2}-\d{2})",
                    text,
                )
                if block_m:
                    stmt_date = _parse_date(block_m.group(2), "statement date")
            else:
                stmt_date = _parse_date(stmt_m.group(1), "statement date")

        if not isinstance(stmt_date, date):
            doc.close()
            raise ParserError("BOC statement date not found")

        # 应还款总计
        total_due = table_summary.get("total_due_minor")
        if not isinstance(total_due, int):
            amt_m = re.search(r"本期人民币欠款总计.*?([\d,]+\.\d{2})", text.replace("\n", " "))
            if not amt_m:
                amt_m = re.search(r"\b(\d+\.\d{2})\s*\n\s*USD", text)
            if not amt_m:
                raise ParserError("BOC total due amount not found")
            total_due = _parse_money_minor(amt_m.group(1), "total due")

        minimum_due = table_summary.get("minimum_due_minor")
        if not isinstance(minimum_due, int):
            minimum_due = None

        # 卡尾号
        card_last4 = table_summary.get("card_last4")
        if not isinstance(card_last4, str):
            card_m = re.search(r"\b(\d{4})\s*\n\s*人民币RMB", text)
            card_last4 = card_m.group(1) if card_m else None

        limit_m = re.search(r"(?:信用额度|授信额度).*?([\d,]+\.\d{2})", text.replace("\n", " "))
        credit_limit_minor = (
            _parse_money_minor(limit_m.group(1), "credit limit") if limit_m else None
        )

        # 借用 bean-sieve 提取明细
        transactions: list[ParsedTransaction] = []
        with TemporaryDirectory() as td:
            pdf_path = Path(td) / "boc.pdf"
            pdf_path.write_bytes(pdf_data)
            prov = get_provider("boc_credit")
            try:
                t_list = prov.parse(pdf_path)
                for t in t_list:
                    if t.card_last4:
                        card_last4 = t.card_last4
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

        # bean-sieve 0.7.0 requires a repeated header on every PDF page and
        # therefore misses BOC continuation pages. Reuse its parsed rows, then
        # supplement only rows discovered in the PDF tables that it omitted.
        table_transactions, table_card_last4 = _transactions_from_tables(doc)
        if not card_last4:
            card_last4 = table_card_last4
        unmatched_existing = Counter(_transaction_identity(item) for item in transactions)
        for item in table_transactions:
            identity = _transaction_identity(item)
            if unmatched_existing[identity]:
                unmatched_existing[identity] -= 1
            else:
                transactions.append(item)

        doc.close()

        if not card_last4:
            raise ParserError("BOC card last four digits not found")

        warnings = []
        if minimum_due is None:
            warnings.append("BOC minimum payment due is not available")
        if credit_limit_minor is None:
            warnings.append("BOC PDF does not expose a credit limit")

        return ParsedStatement(
            bank_code=self.bank_code,
            card_last4=card_last4,
            statement_date=stmt_date,
            due_date=due_date,
            total_due_minor=total_due,
            minimum_due_minor=minimum_due,
            currency="CNY",
            credit_limit_minor=credit_limit_minor,
            transactions=tuple(transactions),
            warnings=tuple(warnings),
        )
