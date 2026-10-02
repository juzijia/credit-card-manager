import re
from datetime import date
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr

from bs4 import BeautifulSoup, Tag

from app.parsers.base import ParsedSource, ParsedStatement, ParsedTransaction, ParserError, decimal_to_minor

_SPACE_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$")
_STATEMENT_DATE_RE = re.compile(
    r"账单日\s*[:：]\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})"
)
_CARD_LAST4_RE = re.compile(r"^\d{4}$")


def _text(tag: Tag) -> str:
    return _SPACE_RE.sub(" ", tag.get_text(" ", strip=True)).strip()


def _direct_cells(row: Tag) -> list[str]:
    return [_text(cell) for cell in row.find_all(["th", "td"], recursive=False)]


def _parse_date(value: str, field: str) -> date:
    match = _DATE_RE.fullmatch(value.strip())
    if not match:
        raise ParserError(f"Invalid {field} date format")
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError as exc:
        raise ParserError(f"Invalid {field} date") from exc


def _parse_money_minor(
    value: str, field: str, *, allow_negative: bool = False
) -> int:
    normalized = (
        value.strip()
        .replace(",", "")
        .replace("￥", "")
        .replace("¥", "")
        .replace("元", "")
    )
    if not normalized:
        raise ParserError(f"Missing {field}")
    negative = normalized.startswith("(") and normalized.endswith(")")
    if negative:
        normalized = normalized[1:-1]
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ParserError(f"Invalid {field} amount") from exc
    minor = decimal_to_minor(amount, field)
    if negative:
        minor = -minor
    if minor < 0 and not allow_negative:
        raise ParserError(f"{field} cannot be negative")
    return minor


class CgbStatementParser:
    bank_code = "CGB"
    version = "1.0.0"

    _headers = (
        "卡号末四位",
        "本期账单金额",
        "最低还款额",
        "最后还款日",
        "入账货币",
    )
    _transaction_headers = (
        "交易日期",
        "入账日期",
        "交易摘要",
        "交易金额",
        "交易货币",
        "入账金额",
        "入账货币",
    )

    def can_handle(self, source: ParsedSource) -> bool:
        address = parseaddr(source.sender)[1].lower()
        domain = address.rpartition("@")[2]
        trusted_domain = domain == "cgbchina.com.cn" or domain.endswith(
            ".cgbchina.com.cn"
        )
        return trusted_domain and "广发信用卡" in source.subject

    def parse(self, source: ParsedSource) -> ParsedStatement:
        if not self.can_handle(source):
            raise ParserError("Source is not a recognized CGB credit-card statement")

        soup = BeautifulSoup(source.html, "lxml")
        rows = soup.find_all("tr")
        summary_cells: list[str] | None = None
        for index, row in enumerate(rows):
            header_cells = _direct_cells(row)
            if tuple(header_cells[:5]) != self._headers:
                continue
            for candidate in rows[index + 1 : index + 12]:
                cells = _direct_cells(candidate)
                if (
                    len(cells) >= 5
                    and _CARD_LAST4_RE.fullmatch(cells[0])
                    and _DATE_RE.fullmatch(cells[3])
                ):
                    summary_cells = cells
                    break
            if summary_cells:
                break

        if summary_cells is None:
            raise ParserError("CGB summary table was not found")

        page_text = _SPACE_RE.sub(" ", soup.get_text(" ", strip=True))
        statement_date_match = _STATEMENT_DATE_RE.search(page_text)
        if not statement_date_match:
            raise ParserError("CGB statement date was not found")

        currency_label = summary_cells[4]
        currency_map = {"人民币": "CNY", "美元": "USD"}
        currency = currency_map.get(currency_label)
        if currency is None:
            raise ParserError("Unsupported CGB statement currency")

        total_due = _parse_money_minor(summary_cells[1], "total due")
        minimum_due = _parse_money_minor(summary_cells[2], "minimum due")
        if minimum_due > total_due:
            raise ParserError("Minimum due exceeds total due")
        credit_limit_minor = (
            _parse_money_minor(summary_cells[6], "credit limit")
            if len(summary_cells) > 6 and summary_cells[6]
            else None
        )

        transactions, transaction_warnings = self._parse_transactions(rows, currency_map)
        warnings = list(transaction_warnings)
        if not transactions:
            warnings.append("CGB transaction details were not found")

        return ParsedStatement(
            bank_code=self.bank_code,
            card_last4=summary_cells[0],
            statement_date=_parse_date(
                statement_date_match.group(1), "statement"
            ),
            due_date=_parse_date(summary_cells[3], "due"),
            total_due_minor=total_due,
            minimum_due_minor=minimum_due,
            currency=currency,
            credit_limit_minor=credit_limit_minor,
            transactions=transactions,
            warnings=tuple(warnings),
        )

    def _parse_transactions(
        self, rows: list[Tag], currency_map: dict[str, str]
    ) -> tuple[tuple[ParsedTransaction, ...], tuple[str, ...]]:
        header_index: int | None = None
        for index, row in enumerate(rows):
            if tuple(_direct_cells(row)[:7]) == self._transaction_headers:
                header_index = index
                break
        if header_index is None:
            return (), ()

        transactions: list[ParsedTransaction] = []
        warnings: list[str] = []
        for row_number, row in enumerate(rows[header_index + 1 :], start=1):
            cells = _direct_cells(row)
            if len(cells) < 7:
                continue
            if not (_DATE_RE.fullmatch(cells[0]) and _DATE_RE.fullmatch(cells[1])):
                continue
            try:
                currency = currency_map.get(cells[6])
                if currency is None:
                    raise ParserError("unsupported posting currency")
                transactions.append(
                    ParsedTransaction(
                        transaction_date=_parse_date(cells[0], "transaction"),
                        posting_date=_parse_date(cells[1], "posting"),
                        amount_minor=_parse_money_minor(
                            cells[5], "posting", allow_negative=True
                        ),
                        currency=currency,
                        raw_description=cells[2],
                    )
                )
            except ParserError:
                warnings.append(f"CGB transaction row {row_number} could not be parsed")
        return tuple(transactions), tuple(warnings)
