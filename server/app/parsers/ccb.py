import re
from datetime import date
from email.utils import parseaddr
from pathlib import Path
from tempfile import TemporaryDirectory

from bean_sieve import get_provider
from bs4 import BeautifulSoup

from app.parsers.base import (
    ParsedAccountSummary,
    ParsedBillingDocument,
    ParsedBillingUnit,
    ParsedSource,
    ParsedStatement,
    ParsedTransaction,
    ParserError,
    decimal_to_minor,
    statement_from_billing_unit,
)
from app.parsers.cgb import _parse_date, _parse_money_minor


_MONEY = r"[¥￥]?\s*[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}\s*(?:元)?"
_CNY_LABELS = {"人民币（CNY）", "人民币(CNY)", "人民币CNY", "人民币", "CNY"}
_CURRENCY_LABELS = {"账户币种Currency", "账户币种", "币种Currency", "币种", "Currency"}
_TOTAL_LABELS = {"本期全部应还款额", "本期全部应还款额NewBalance", "NewBalance"}


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


_DASH_ZERO_PLACEHOLDERS = {"-", "\u2013", "\u2014", "\u2212", "\uff0d"}


def _amount_text(value: str) -> str:
    # CCB renders a zero payable as a dash placeholder in payment cells.
    if _compact(value) in _DASH_ZERO_PLACEHOLDERS:
        return "0.00"
    return value


def _currency_code(value: str) -> str | None:
    compact = _compact(value).upper()
    for code in ("CNY", "USD", "EUR"):
        if code in compact:
            return code
    if "人民币" in compact:
        return "CNY"
    if "美元" in compact:
        return "USD"
    if "欧元" in compact:
        return "EUR"
    return None


def _direct_rows(table) -> list:
    return [row for row in table.find_all("tr") if row.find_parent("table") is table]


def _extract_total_due(soup: BeautifulSoup) -> int:
    values: list[str] = []
    for table in soup.find_all("table"):
        rows = _direct_rows(table)
        # A span from an earlier row can shift even otherwise unmerged rows.
        if any(
            str(cell.get("rowspan", "1")) != "1"
            for row in rows
            for cell in row.find_all(["td", "th"], recursive=False)
        ):
            continue
        columns = None
        for row in rows:
            cells = row.find_all(["td", "th"], recursive=False)
            # Read only unmerged rows with an unambiguous header/value mapping.
            if any(
                cell.find("table") is not None
                or str(cell.get("colspan", "1")) != "1"
                for cell in cells
            ):
                columns = None
                continue
            texts = [_compact(cell.get_text()) for cell in cells]
            due_columns = [i for i, text in enumerate(texts) if text in _TOTAL_LABELS]
            if due_columns:
                currencies = [i for i, text in enumerate(texts) if text in _CURRENCY_LABELS]
                if len(due_columns) != 1 or len(currencies) != 1:
                    columns = None
                    continue
                columns = (len(cells), currencies[0], due_columns[0])
                continue
            if columns is None:
                continue
            width, currency_column, due_column = columns
            if len(cells) != width:
                columns = None
                continue
            if texts[currency_column] in _CNY_LABELS:
                values.append(_amount_text(cells[due_column].get_text(" ", strip=True)))

    # Preserve explicit label/value blocks; never scan across block/table nodes.
    for node in soup.find_all(["div", "p", "span"]):
        if node.find_parent("table") is not None or any(
            child.name not in {"span", "b", "strong", "em", "i", "font", "br"}
            for child in node.find_all(True)
        ):
            continue
        text = node.get_text(" ", strip=True)
        match = re.fullmatch(
            r"本期全部应还款额\s*(?:New\s*Balance\s*)?[:：]?\s*(" + _MONEY + ")",
            text,
        )
        if match:
            values.append(match.group(1))

    if not values:
        raise ParserError("CCB total due amount not found in an unambiguous structure")
    amounts = set()
    for value in values:
        if not re.fullmatch(_MONEY, value):
            raise ParserError("CCB total due amount is invalid")
        amounts.add(_parse_money_minor(value, "total due", allow_negative=True))
    if len(amounts) != 1:
        raise ParserError("CCB total due amounts conflict")
    return max(amounts.pop(), 0)


def _extract_minimum_due(soup: BeautifulSoup) -> int:
    values: list[str] = []
    for table in soup.find_all("table"):
        rows = _direct_rows(table)
        if "信用卡卡号" in _compact(table.get_text(" ", strip=True)):
            continue
        columns = None
        for row in rows:
            cells = row.find_all(["td", "th"], recursive=False)
            if any(
                cell.find("table") is not None
                or str(cell.get("rowspan", "1")) != "1"
                or str(cell.get("colspan", "1")) != "1"
                for cell in cells
            ):
                columns = None
                continue
            texts = [_compact(cell.get_text()) for cell in cells]
            currency_columns = [i for i, text in enumerate(texts) if text in _CURRENCY_LABELS]
            minimum_columns = [
                i for i, text in enumerate(texts)
                if "最低还款额" in text or "Min.Payment" in text
            ]
            if currency_columns or minimum_columns:
                if len(currency_columns) == 1 and len(minimum_columns) == 1:
                    columns = (len(cells), currency_columns[0], minimum_columns[0])
                else:
                    columns = None
                continue
            if columns is None:
                continue
            width, currency_column, minimum_column = columns
            if len(cells) != width:
                columns = None
                continue
            if texts[currency_column] in _CNY_LABELS:
                values.append(_amount_text(cells[minimum_column].get_text(" ", strip=True)))

    for node in soup.find_all(["div", "p", "span"]):
        if node.find_parent("table") is not None:
            continue
        text = node.get_text(" ", strip=True)
        match = re.fullmatch(
            r"最低还款额\s*Min\.Payment\s*[:：]?\s*(" + _MONEY + ")",
            text,
        )
        if match:
            values.append(match.group(1))

    if not values:
        return 0
    amounts = {
        max(_parse_money_minor(value, "minimum due", allow_negative=True), 0)
        for value in values
        if re.fullmatch(_MONEY, value)
    }
    if len(amounts) != 1:
        raise ParserError("CCB minimum due amounts conflict")
    return amounts.pop()


def _billing_header_tag(text: str) -> str | None:
    currency = text in _CURRENCY_LABELS or "账户币种Currency" in text
    total = (
        ("应还款额" in text or "NewBalance" in text)
        and "最低" not in text
        and "本期全部" not in text
    )
    minimum = "最低还款额" in text or "Min.Payment" in text
    matches = [
        tag
        for tag, hit in (
            ("currency", currency),
            ("total", total),
            ("minimum", minimum),
        )
        if hit
    ]
    if len(matches) > 1:
        return "ambiguous"
    return matches[0] if matches else None


def _billing_groups_from_header(
    texts: list[str],
) -> tuple[int, tuple[tuple[int, int, int], ...]] | None:
    card_columns = [
        i for i, text in enumerate(texts)
        if "信用卡卡号" in text or "CardNumber" in text
    ]
    tags = [_billing_header_tag(text) for text in texts]
    if not card_columns:
        return None
    if len(card_columns) != 1:
        raise ParserError("CCB billing unit card anchor is ambiguous")
    card_idx = card_columns[0]
    if any(tags[:card_idx]):
        raise ParserError("CCB billing unit header is malformed")
    groups: list[tuple[int, int, int]] = []
    currency_idx = total_idx = -1
    state = 0
    for idx in range(card_idx + 1, len(texts)):
        tag = tags[idx]
        if state == 0:
            if tag is None:
                continue
            if tag != "currency":
                raise ParserError("CCB billing unit header is malformed")
            currency_idx = idx
            state = 1
        elif state == 1:
            if tag != "total":
                raise ParserError("CCB billing unit header is malformed")
            total_idx = idx
            state = 2
        else:
            if tag != "minimum":
                raise ParserError("CCB billing unit header is malformed")
            groups.append((currency_idx, total_idx, idx))
            state = 0
    if state != 0:
        raise ParserError("CCB billing unit header is malformed")
    if not groups:
        return None
    return card_idx, tuple(groups)


def _extract_billing_units(soup: BeautifulSoup) -> tuple[ParsedBillingUnit, ...]:
    units: list[ParsedBillingUnit] = []
    found_section = False
    in_section = False
    for table in soup.find_all("table"):
        table_text = _compact(table.get_text(" ", strip=True))
        if in_section and "【交易明细】" in table_text:
            break
        if "应还款明细" in table_text or "请按以下卡号、币种分别还款" in table_text:
            found_section = True
            in_section = True
        if not in_section:
            continue
        columns = None
        for row in _direct_rows(table):
            cells = row.find_all(["td", "th"], recursive=False)
            if not cells:
                continue
            texts = [_compact(cell.get_text(" ", strip=True)) for cell in cells]
            if any(
                "信用卡卡号" in text or "CardNumber" in text for text in texts
            ) or any(_billing_header_tag(text) for text in texts):
                header = _billing_groups_from_header(texts)
                if header is not None:
                    columns = (len(cells), header[0], header[1])
                continue
            if columns is None:
                continue
            width, card_idx, groups = columns
            if len(cells) != width:
                continue
            raw_card = cells[card_idx].get_text(" ", strip=True)
            card_match = re.search(r"(\d{4})(?:\D*)$", raw_card)
            parsed: list[tuple[str, int, int]] = []
            for currency_idx, total_idx, minimum_idx in groups:
                currency_text = cells[currency_idx].get_text(" ", strip=True)
                total_text = _amount_text(cells[total_idx].get_text(" ", strip=True))
                minimum_text = _amount_text(cells[minimum_idx].get_text(" ", strip=True))
                if not currency_text and not total_text and not minimum_text:
                    continue
                currency = _currency_code(currency_text)
                if not currency:
                    raise ParserError("CCB billing unit currency is unrecognized")
                if not re.fullmatch(_MONEY, total_text) or not re.fullmatch(
                    _MONEY, minimum_text
                ):
                    raise ParserError("CCB billing unit amount is invalid")
                parsed.append(
                    (
                        currency,
                        max(
                            _parse_money_minor(
                                total_text, "billing unit total", allow_negative=True
                            ),
                            0,
                        ),
                        max(
                            _parse_money_minor(
                                minimum_text, "billing unit minimum", allow_negative=True
                            ),
                            0,
                        ),
                    )
                )
            if not parsed:
                continue
            if not card_match:
                raise ParserError("CCB billing unit card reference is missing")
            for currency, total_minor, minimum_minor in parsed:
                units.append(
                    ParsedBillingUnit(
                        card_ref=card_match.group(1),
                        currency=currency,
                        total_due_minor=total_minor,
                        minimum_due_minor=minimum_minor,
                    )
                )
    seen: set[tuple[str, str]] = set()
    for unit in units:
        key = (unit.card_ref, unit.currency)
        if key in seen:
            raise ParserError("CCB billing units contain duplicate card and currency")
        seen.add(key)
    if found_section and not units:
        raise ParserError("CCB billing unit section could not be resolved safely")
    return tuple(units)


def _parse_settlement_amount(cells: list[str]) -> tuple[int, str] | None:
    pairs: list[tuple[int, str]] = []
    index = 0
    while index < len(cells):
        currency = _currency_code(cells[index])
        if currency and index + 1 < len(cells) and re.fullmatch(_MONEY, cells[index + 1]):
            pairs.append(
                (
                    _parse_money_minor(
                        cells[index + 1], "transaction settlement amount", allow_negative=True
                    ),
                    currency,
                )
            )
            index += 2
            continue
        combined = re.fullmatch(r"\s*([A-Za-z]{3})\s*([\d,.-]+)\s*", cells[index])
        if combined:
            currency = _currency_code(combined.group(1))
            if currency and re.fullmatch(_MONEY, combined.group(2)):
                pairs.append(
                    (
                        _parse_money_minor(
                            combined.group(2),
                            "transaction settlement amount",
                            allow_negative=True,
                        ),
                        currency,
                    )
                )
        index += 1
    # CCB rows are ordered Trans.Curr/Amt, then Sett.Curr/Amt.
    return pairs[-1] if pairs else None


def _has_transaction_table(soup: BeautifulSoup) -> bool:
    return any(
        rows and "【交易明细】" in rows[0].get_text(" ", strip=True)
        for table in soup.find_all("table")
        if (rows := _direct_rows(table))
    )


def _parse_transactions(soup: BeautifulSoup) -> tuple[ParsedTransaction, ...]:
    transactions: list[ParsedTransaction] = []
    for table in soup.find_all("table"):
        rows = _direct_rows(table)
        if not rows or "【交易明细】" not in rows[0].get_text(" ", strip=True):
            continue
        for row in rows:
            cells = [
                cell.get_text(" ", strip=True)
                for cell in row.find_all(["td", "th"], recursive=False)
            ]
            if len(cells) < 6 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
                continue
            settlement = _parse_settlement_amount(cells[4:])
            if settlement is None:
                raise ParserError("CCB transaction settlement amount could not be resolved")
            amount_minor, currency = settlement
            if currency != "CNY" and amount_minor != 0:
                raise ParserError(
                    f"CCB transaction settled in non-CNY currency: currency={currency}, amount={amount_minor} (UNSUPPORTED_FOREIGN_PAYABLE)"
                )
            transactions.append(
                ParsedTransaction(
                    transaction_date=_parse_date(cells[0], "transaction date"),
                    posting_date=_parse_date(cells[1], "posting date"),
                    amount_minor=amount_minor,
                    currency=currency,
                    raw_description=cells[3],
                    card_ref=cells[2],
                )
            )
    return tuple(transactions)


def _legacy_provider_transactions(source: ParsedSource) -> tuple[ParsedTransaction, ...]:
    """Compatibility fallback only for legacy sources with no HTML transaction table."""

    transactions: list[ParsedTransaction] = []
    with TemporaryDirectory() as directory:
        path = Path(directory) / "ccb.eml"
        if source.raw_bytes:
            path.write_bytes(source.raw_bytes)
        elif source.path and source.path.exists():
            path.write_bytes(source.path.read_bytes())
        else:
            path.write_text(source.html, encoding="utf-8")
        try:
            raw_transactions = get_provider("ccb_credit").parse(path)
        except Exception:
            return ()
        for item in raw_transactions:
            amount_minor = decimal_to_minor(item.amount)
            if item.currency != "CNY" and amount_minor != 0:
                raise ParserError(
                    f"CCB legacy transaction settled in non-CNY currency: currency={item.currency} (UNSUPPORTED_FOREIGN_PAYABLE)"
                )
            transactions.append(
                ParsedTransaction(
                    transaction_date=item.date,
                    posting_date=item.post_date,
                    amount_minor=amount_minor,
                    currency=item.currency,
                    raw_description=item.description,
                    card_ref=item.card_last4,
                )
            )
    return tuple(transactions)


def _check_foreign_currency_payables(
    soup: BeautifulSoup,
    payment_details: tuple[ParsedBillingUnit, ...],
    transactions: tuple[ParsedTransaction, ...],
) -> None:
    for tx in transactions:
        if tx.currency != "CNY" and tx.amount_minor != 0:
            raise ParserError(
                f"CCB transaction settled in non-CNY currency: currency={tx.currency}, amount={tx.amount_minor} (UNSUPPORTED_FOREIGN_PAYABLE)"
            )

    for unit in payment_details:
        if unit.currency != "CNY":
            if unit.total_due_minor != 0 or (unit.minimum_due_minor or 0) != 0:
                raise ParserError(
                    f"CCB foreign payable in payment details is unsupported: currency={unit.currency}, total={unit.total_due_minor}, minimum={unit.minimum_due_minor} (UNSUPPORTED_FOREIGN_PAYABLE)"
                )

    for table in soup.find_all("table"):
        rows = _direct_rows(table)
        if any(
            str(cell.get("rowspan", "1")) != "1"
            for row in rows
            for cell in row.find_all(["td", "th"], recursive=False)
        ):
            continue
        columns = None
        for row in rows:
            cells = row.find_all(["td", "th"], recursive=False)
            if any(
                cell.find("table") is not None
                or str(cell.get("colspan", "1")) != "1"
                for cell in cells
            ):
                columns = None
                continue
            texts = [_compact(cell.get_text()) for cell in cells]
            due_columns = [i for i, text in enumerate(texts) if text in _TOTAL_LABELS]
            if due_columns:
                currencies = [i for i, text in enumerate(texts) if text in _CURRENCY_LABELS]
                minimums = [
                    i for i, text in enumerate(texts)
                    if "最低还款额" in text or "Min.Payment" in text
                ]
                if len(due_columns) != 1 or len(currencies) != 1:
                    columns = None
                    continue
                columns = (
                    len(cells),
                    currencies[0],
                    due_columns[0],
                    minimums[0] if len(minimums) == 1 else None,
                )
                continue
            if columns is None:
                continue
            width, currency_column, due_column, min_column = columns
            if len(cells) != width:
                columns = None
                continue
            currency_text = texts[currency_column]
            if currency_text not in _CNY_LABELS:
                curr = _currency_code(currency_text)
                if curr and curr != "CNY":
                    due_text = cells[due_column].get_text(" ", strip=True)
                    total_minor = 0
                    if re.fullmatch(_MONEY, due_text):
                        total_minor = max(
                            _parse_money_minor(due_text, "foreign total due", allow_negative=True),
                            0,
                        )
                    min_minor = 0
                    if min_column is not None:
                        min_text = cells[min_column].get_text(" ", strip=True)
                        if re.fullmatch(_MONEY, min_text):
                            min_minor = max(
                                _parse_money_minor(min_text, "foreign minimum due", allow_negative=True),
                                0,
                            )
                    if total_minor != 0 or min_minor != 0:
                        raise ParserError(
                            f"CCB foreign payable in account summary is unsupported: currency={curr}, total={total_minor}, minimum={min_minor} (UNSUPPORTED_FOREIGN_PAYABLE)"
                        )


def _legacy_transaction_card_ref(value: str | None) -> str | None:
    if value and re.fullmatch(r"\d{4}", value.strip()):
        return value.strip()
    if value:
        match = re.fullmatch(r"(\d{4})/扫码", value.strip())
        if match:
            return match.group(1)
    return None


class CcbStatementParser:
    bank_code = "CCB"
    version = "1.1.0"

    def can_handle(self, source: ParsedSource) -> bool:
        address = parseaddr(source.sender)[1].lower()
        domain = address.rpartition("@")[2]
        trusted = domain == "vip.ccb.com" or domain.endswith(".ccb.com")
        return trusted and "中国建设银行信用卡电子账单" in source.subject

    def parse_document(self, source: ParsedSource) -> ParsedBillingDocument:
        if not self.can_handle(source):
            raise ParserError("Not a recognized CCB statement")
        soup = BeautifulSoup(source.html, "lxml")
        page_text = soup.get_text(" ", strip=True)

        stmt_match = re.search(
            r"本期账单日.*?(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})", page_text
        )
        if not stmt_match:
            raise ParserError("CCB statement date not found")
        clean_stmt = (
            stmt_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        )
        statement_date = _parse_date(clean_stmt, "statement date")

        due_match = re.search(
            r"本期到期还款日.*?(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})", page_text
        )
        if not due_match:
            raise ParserError("CCB due date not found")
        clean_due = (
            due_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        )
        due_date = _parse_date(clean_due, "due date")

        account_total = _extract_total_due(soup)
        account_minimum = _extract_minimum_due(soup)
        account_summaries = (
            ParsedAccountSummary(
                currency="CNY",
                total_due_minor=account_total,
                minimum_due_minor=account_minimum,
            ),
        )
        transactions = _parse_transactions(soup)
        if not transactions and not _has_transaction_table(soup):
            transactions = _legacy_provider_transactions(source)
        payment_details = _extract_billing_units(soup)

        _check_foreign_currency_payables(soup, payment_details, transactions)

        limit_match = re.search(
            r"授信额度\s*Credit Limit\s*CNY\s*([\d,]+(?:\.\d{2})?)", page_text
        )
        credit_limit_minor = (
            _parse_money_minor(limit_match.group(1), "credit limit")
            if limit_match
            else None
        )

        return ParsedBillingDocument(
            bank_code=self.bank_code,
            statement_date=statement_date,
            due_date=due_date,
            billing_units=(),
            transactions=transactions,
            account_summaries=account_summaries,
            credit_limit_minor=credit_limit_minor,
        )

    def parse(self, source: ParsedSource) -> ParsedStatement:
        """Legacy single-unit adapter used by existing direct parser consumers."""

        document = self.parse_document(source)
        soup = BeautifulSoup(source.html, "lxml")
        payment_details = _extract_billing_units(soup)
        distinct_cards = list(dict.fromkeys(unit.card_ref for unit in payment_details))
        if len(distinct_cards) > 1:
            raise ParserError(
                "CCB source contains multiple billing units; use ParsedBillingDocument"
            )
        if len(distinct_cards) == 1:
            card_ref = distinct_cards[0]
        else:
            card_ref = next(
                (
                    normalized
                    for item in document.transactions
                    if (normalized := _legacy_transaction_card_ref(item.card_ref)) is not None
                ),
                None,
            )
        if card_ref is None:
            raise ParserError("CCB card last four digits not found")

        summary = document.account_summaries[0]
        return ParsedStatement(
            bank_code=document.bank_code,
            card_last4=card_ref,
            statement_date=document.statement_date,
            due_date=document.due_date,
            total_due_minor=summary.total_due_minor,
            minimum_due_minor=summary.minimum_due_minor,
            currency=summary.currency,
            credit_limit_minor=document.credit_limit_minor,
            billing_cycle_start=document.billing_cycle_start,
            billing_cycle_end=document.billing_cycle_end,
            transactions=document.transactions,
            warnings=document.warnings,
        )
