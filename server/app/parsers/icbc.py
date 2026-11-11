import re
from datetime import date
from email.utils import parseaddr
from bs4 import BeautifulSoup
from app.parsers.base import ParsedSource, ParsedStatement, ParsedTransaction, ParserError
from app.parsers.cgb import _parse_date, _parse_money_minor


class IcbcStatementParser:
    bank_code = "ICBC"
    version = "1.1.1"

    def can_handle(self, source: ParsedSource) -> bool:
        address = parseaddr(source.sender)[1].lower()
        domain = address.rpartition("@")[2]
        trusted = domain == "icbc.com.cn" or domain.endswith(".icbc.com.cn")
        return trusted and "中国工商银行客户对账单" in source.subject

    def parse(self, source: ParsedSource) -> ParsedStatement:
        if not self.can_handle(source):
            raise ParserError("Not a recognized ICBC statement")
        soup = BeautifulSoup(source.html, "lxml")
        page_text = soup.get_text(" ", strip=True)

        cycle_match = re.search(
            r"账单周期\s*[:：]?\s*"
            r"(\d{4}年\d{1,2}月\d{1,2}日)\s*[—–~-]\s*"
            r"(\d{4}年\d{1,2}月\d{1,2}日)",
            page_text,
        )
        billing_cycle_start = None
        billing_cycle_end = None
        if cycle_match:
            clean_start = cycle_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
            clean_end = cycle_match.group(2).replace("年", "-").replace("月", "-").replace("日", "")
            billing_cycle_start = _parse_date(clean_start, "billing cycle start")
            billing_cycle_end = _parse_date(clean_end, "billing cycle end")
            if billing_cycle_end < billing_cycle_start:
                raise ParserError("ICBC billing cycle end precedes start")

        # Physical statement record date printed by ICBC as 对账单生成日.
        # This is not necessarily the account's configured Card.statement_day.
        stmt_match = re.search(r"对账单生成日\s*(\d{4}年\d{1,2}月\d{1,2}日)", page_text)
        if not stmt_match and billing_cycle_end is not None:
            stmt_date = billing_cycle_end
        elif stmt_match:
            clean_stmt = stmt_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
            stmt_date = _parse_date(clean_stmt, "statement generation date")
        else:
            raise ParserError("ICBC statement generation date not found")

        # 到期还款日
        due_match = re.search(r"到期还款日\s*[:：]?\s*(\d{4}年\d{1,2}月\d{1,2}日)", page_text)
        if not due_match:
            raise ParserError("ICBC due date not found")
        clean_due = due_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        due_date = _parse_date(clean_due, "due date")

        # 卡尾号与还款额
        summary_anchors = re.findall(r"(?<!\d)\d{4}\s*[（(][^）)]*[）)]", page_text)
        if len(summary_anchors) > 1:
            raise ParserError("MULTIPLE_REPAYMENT_SUMMARIES: ICBC statement contains multiple repayment summaries")

        cny_matches = list(
            re.finditer(
                r"(\d{4})\(.*?\)\s*\|?\s*人民币.*?\|?\s*([\d,]+\.\d{2})/RMB\s*\|?\s*([\d,]+\.\d{2})/RMB\s*\|?\s*([\d,]+\.\d{2})/RMB",
                page_text,
            )
        )
        if len(cny_matches) > 1:
            raise ParserError("MULTIPLE_REPAYMENT_SUMMARIES: ICBC contains multiple CNY repayment summaries")
        if not cny_matches:
            raise ParserError("ICBC repayment summary with card last four digits not found")

        row_match = cny_matches[0]
        card_last4 = row_match.group(1)
        total_due = _parse_money_minor(row_match.group(2), "total due", allow_negative=True)
        min_due = _parse_money_minor(row_match.group(3), "minimum due", allow_negative=True)
        credit_limit_minor = _parse_money_minor(row_match.group(4), "credit limit")
        if total_due < 0:
            total_due = 0
        if min_due < 0:
            min_due = 0

        # 交易明细
        transactions = []
        for tr in soup.find_all("tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 7:
                continue
            txts = [c.get_text(strip=True) for c in cells]
            # Preserve missing fields in aligned rows; retain legacy padded-row support.
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", txts[1]):
                txts = [text for text in txts if text]
            if len(txts) < 7 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", txts[1]):
                continue
            if not re.fullmatch(r"\d{4}", txts[0]):
                raise ParserError("UNRESOLVED_CARD_REFERENCE: ICBC transaction has invalid or missing card reference")
            if txts[0] != card_last4:
                raise ParserError("MULTIPLE_CARD_REFERENCES: ICBC transaction card does not match summary card")

            t_date = _parse_date(txts[1], "trans date")
            p_date = _parse_date(txts[2], "post date")
            desc = f"{txts[3]} {txts[4]}".strip()
            amt_str = txts[6].split("/")[0]
            allow_neg = "存入" in txts[6] or "还款" in txts[3]
            amt = _parse_money_minor(amt_str, "amount", allow_negative=True)
            if allow_neg and amt > 0:
                amt = -amt
            transactions.append(
                ParsedTransaction(
                    transaction_date=t_date,
                    posting_date=p_date,
                    amount_minor=amt,
                    currency="CNY",
                    raw_description=desc,
                    card_ref=txts[0],
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
            billing_cycle_start=billing_cycle_start,
            billing_cycle_end=billing_cycle_end,
            transactions=tuple(transactions),
            warnings=(),
        )
