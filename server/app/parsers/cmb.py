import re
from datetime import date
from email.utils import parseaddr
from bs4 import BeautifulSoup
from app.parsers.base import ParsedSource, ParsedStatement, ParsedTransaction, ParserError
from app.parsers.cgb import _parse_date, _parse_money_minor


class CmbStatementParser:
    bank_code = "CMB"
    version = "1.0.1"

    def can_handle(self, source: ParsedSource) -> bool:
        address = parseaddr(source.sender)[1].lower()
        domain = address.rpartition("@")[2]
        trusted = domain == "message.cmbchina.com" or domain.endswith(".cmbchina.com")
        return trusted and "招商银行信用卡电子账单" in source.subject

    def parse(self, source: ParsedSource) -> ParsedStatement:
        if not self.can_handle(source):
            raise ParserError("Not a recognized CMB statement")
        soup = BeautifulSoup(source.html, "lxml")

        # 1. 到期还款日
        img_dqhkr = soup.find("img", attrs={"data-code": "dqhkr_img"})
        if not img_dqhkr:
            raise ParserError("CMB due date img not found")
        due_td = img_dqhkr.find_parent("td").find_next_sibling("td")
        due_date = _parse_date(due_td.get_text(strip=True), "due date")

        # 2. 账单周期与账单日
        img_zdzq = soup.find("img", attrs={"data-code": "zdzq_img"})
        if not img_zdzq:
            raise ParserError("CMB cycle img not found")
        zq_td = img_zdzq.find_parent("td").find_next_sibling("td")
        zq_str = zq_td.get_text(strip=True)
        m = re.search(r"\d{4}/\d{1,2}/\d{1,2}\s*-\s*(\d{4}/\d{1,2}/\d{1,2})", zq_str)
        if not m:
            raise ParserError("CMB statement closing date not found in cycle")
        stmt_date = _parse_date(m.group(1), "statement date")

        # 3. 本期应还金额
        img_bqyh_list = soup.find_all("img", attrs={"data-code": "bqyhje_img"})
        if not img_bqyh_list:
            raise ParserError("CMB total due img not found")
        if len(img_bqyh_list) > 1:
            raise ParserError("MULTIPLE_REPAYMENT_SUMMARIES: CMB statement contains multiple repayment summaries")
        total_td = img_bqyh_list[0].find_parent("td").find_next_sibling("td")
        total_due = _parse_money_minor(total_td.get_text(strip=True), "total due", allow_negative=True)
        if total_due < 0:
            total_due = 0

        # 4. 最低还款额
        min_due = 0
        img_zdhk = soup.find("img", attrs={"data-code": "zdhke_img"}) or soup.find("img", attrs={"data-code": "bqzdhke_img"})
        if img_zdhk:
            min_td = img_zdhk.find_parent("td").find_next_sibling("td")
            if min_td:
                min_due = _parse_money_minor(min_td.get_text(strip=True), "minimum due", allow_negative=True)
                if min_due < 0:
                    min_due = 0

        # 5. 消费交易明细与卡尾号
        transactions = []
        card_last4 = None
        for tr in soup.find_all("tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) == 8:
                d_trans = cells[1].get_text(strip=True)
                d_post = cells[2].get_text(strip=True)
                if re.match(r"^\d{4}$", d_post):
                    desc = cells[3].get_text(strip=True)
                    last4_candidate = cells[5].get_text(strip=True)
                    if not re.fullmatch(r"\d{4}", last4_candidate):
                        raise ParserError("UNRESOLVED_CARD_REFERENCE: CMB transaction has missing or invalid card last4")
                    if card_last4 is not None and last4_candidate != card_last4:
                        raise ParserError("MULTIPLE_CARD_REFERENCES: CMB statement contains multiple card references")
                    card_last4 = last4_candidate
                    amt_str = cells[7].get_text(strip=True)
                    amount_minor = _parse_money_minor(amt_str, "posting", allow_negative=True)
                    p_month, p_day = int(d_post[:2]), int(d_post[2:])
                    p_year = stmt_date.year
                    if stmt_date.month == 1 and p_month == 12:
                        p_year -= 1
                    post_date = date(p_year, p_month, p_day)
                    trans_date = None
                    if re.match(r"^\d{4}$", d_trans):
                        t_month, t_day = int(d_trans[:2]), int(d_trans[2:])
                        t_year = stmt_date.year
                        if stmt_date.month == 1 and t_month == 12:
                            t_year -= 1
                        trans_date = date(t_year, t_month, t_day)
                    transactions.append(
                        ParsedTransaction(
                            transaction_date=trans_date or post_date,
                            posting_date=post_date,
                            amount_minor=amount_minor,
                            currency="CNY",
                            raw_description=desc,
                            card_ref=last4_candidate,
                        )
                    )

        if not card_last4:
            raise ParserError("CMB card last four digits not found")

        credit_node = soup.find(id="creditLimit")
        credit_limit_minor = (
            _parse_money_minor(credit_node.get_text(strip=True), "credit limit")
            if credit_node
            else None
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
