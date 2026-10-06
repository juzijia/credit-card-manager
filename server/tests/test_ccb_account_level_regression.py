import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.card import Card
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.parsers.eml import parse_eml_bytes
from app.services.card_history_rules import resolve_card_history_due_date_rule


def make_ccb_eml(
    *,
    account_total: str = "500.00",
    account_minimum: str = "50.00",
    statement_date: str = "2026-05-15",
    due_date: str = "2026-06-04",
    transactions_html: str = "",
    extra_payment_details_html: str = "",
) -> bytes:
    if not transactions_html:
        transactions_html = """
          <tr><td>2026-04-20</td><td>2026-04-21</td><td>0183/扫码</td>
              <td>0183 tx</td><td>CNY</td><td>100.00</td>
              <td>CNY</td><td>100.00</td></tr>
          <tr><td>2026-04-22</td><td>2026-04-23</td><td>6571/扫码</td>
              <td>6571 tx</td><td>CNY</td><td>400.00</td>
              <td>CNY</td><td>400.00</td></tr>
        """
    msg = EmailMessage()
    msg["Subject"] = "中国建设银行信用卡电子账单"
    msg["From"] = "service@vip.ccb.com"
    msg.set_content(
        f"""
        <div>本期账单日 {statement_date}</div>
        <div>本期到期还款日 {due_date}</div>
        <table>
          <tr><th>账户币种 Currency</th><th>本期全部应还款额 New Balance</th><th>最低还款额 Min.Payment</th></tr>
          <tr><td>人民币（CNY）</td><td>{account_total}</td><td>{account_minimum}</td></tr>
        </table>
        <table>
          <tr><td colspan="4">【应还款明细】请按以下卡号、币种分别还款</td></tr>
          <tr><th>信用卡卡号 Card Number</th><th>账户币种 Currency</th>
              <th>应还款额/溢缴款 New Balance</th><th>最低还款额 Min.Payment</th></tr>
          <tr><td>62216823****0183</td><td>人民币(CNY)</td><td>100.00</td><td>10.00</td></tr>
          <tr><td>62270803****6571</td><td>人民币(CNY)</td><td>400.00</td><td>40.00</td></tr>
          {extra_payment_details_html}
        </table>
        <table><tr><td>【交易明细】</td></tr>
          {transactions_html}
        </table>
        """,
        subtype="html",
    )
    return msg.as_bytes()


# Case A: One CCB account Statement for New Balance 500 with tx 0183=100 and 6571=400; tx physical card ids correct.
def test_case_a_single_ccb_account_statement_with_physical_attribution(client, auth_headers):
    with client.app.state.session_factory() as session:
        card_a = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 0183", last4="0183", currency="CNY")
        card_b = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 6571", last4="6571", currency="CNY")
        session.add_all([card_a, card_b])
        session.commit()
        card_a_id = card_a.id
        card_b_id = card_b.id

    raw = make_ccb_eml()
    res = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb.eml", raw, "message/rfc822")})
    assert res.status_code == 200, res.text
    payload = res.json()
    assert len(payload["statements"]) == 1
    stmt = payload["statements"][0]
    assert stmt["bank_code"] == "CCB"
    assert stmt["card_id"] is None
    assert stmt["total_due_minor"] == 50000

    txs = client.get("/api/v1/transactions", headers=auth_headers).json()
    assert len(txs) == 2
    by_desc = {tx["raw_description"]: tx for tx in txs}
    assert by_desc["0183 tx"]["card_id"] == card_a_id
    assert by_desc["0183 tx"]["amount_minor"] == 10000
    assert by_desc["6571 tx"]["card_id"] == card_b_id
    assert by_desc["6571 tx"]["amount_minor"] == 40000


# Case B: Historical CCB Statement.card_id=0183 + Transaction.card_id=NULL => interest-free unavailable, reason unknown_physical_card.
def test_case_b_historical_ccb_statement_unknown_physical_card(client, auth_headers):
    with client.app.state.session_factory() as session:
        card = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 0183", last4="0183", currency="CNY")
        session.add(card)
        session.flush()
        stmt = Statement(
            bank_code="CCB",
            card_id=card.id,
            statement_date=date(2026, 5, 15),
            due_date=date(2026, 6, 4),
            total_due_minor=10000,
            currency="CNY",
        )
        session.add(stmt)
        session.flush()
        tx = Transaction(
            statement_id=stmt.id,
            card_id=None,
            transaction_date=date(2026, 5, 1),
            posting_date=date(2026, 5, 2),
            amount_minor=10000,
            currency="CNY",
            raw_description="tx without physical card",
        )
        session.add(tx)
        session.commit()
        tx_id = tx.id

    res = client.get(f"/api/v1/transactions/{tx_id}/interest-free", headers=auth_headers).json()
    assert res["available"] is False
    assert res["reason"] == "unknown_physical_card"


# Case C: Known CCB Transaction.card_id => interest-free uses physical card.
def test_case_c_known_ccb_transaction_card_id_uses_physical_card(client, auth_headers):
    with client.app.state.session_factory() as session:
        card = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 0183", last4="0183", currency="CNY")
        session.add(card)
        session.flush()
        stmt = Statement(
            bank_code="CCB",
            card_id=None,
            statement_date=date(2026, 5, 15),
            due_date=date(2026, 6, 4),
            total_due_minor=10000,
            currency="CNY",
        )
        session.add(stmt)
        session.flush()
        tx = Transaction(
            statement_id=stmt.id,
            card_id=card.id,
            transaction_date=date(2026, 5, 1),
            posting_date=date(2026, 5, 2),
            amount_minor=10000,
            currency="CNY",
            raw_description="tx with physical card",
        )
        session.add(tx)
        session.commit()
        tx_id = tx.id
        card_id = card.id

    res = client.get(f"/api/v1/transactions/{tx_id}/interest-free", headers=auth_headers).json()
    assert res["available"] is True
    assert res["estimate"]["card_id"] == card_id
    assert res["estimate"]["interest_free_days"] == (date(2026, 6, 4) - date(2026, 5, 1)).days


# Case D: CCB cards use account-level CCB statement/due history; non-CCB remains card-specific.
def test_case_d_ccb_cards_use_account_level_history(client, auth_headers):
    with client.app.state.session_factory() as session:
        ccb_card = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB Card", last4="1111", statement_day=15, currency="CNY")
        cmb_card = Card(bank_code="CMB", bank_name="招商银行", display_name="CMB Card", last4="2222", statement_day=15, currency="CNY")
        session.add_all([ccb_card, cmb_card])
        session.flush()

        s1 = Statement(bank_code="CCB", card_id=None, statement_date=date(2026, 3, 15), due_date=date(2026, 4, 4), total_due_minor=1000, currency="CNY")
        s2 = Statement(bank_code="CCB", card_id=None, statement_date=date(2026, 4, 15), due_date=date(2026, 5, 5), total_due_minor=1000, currency="CNY")
        s3 = Statement(bank_code="CMB", card_id=cmb_card.id, statement_date=date(2026, 3, 15), due_date=date(2026, 4, 2), total_due_minor=1000, currency="CNY")
        session.add_all([s1, s2, s3])
        session.commit()

        analysis_ccb = resolve_card_history_due_date_rule(session, ccb_card.id, statement_day=15, bank_code="CCB")
        assert analysis_ccb.applicable_pair_count == 2
        assert analysis_ccb.rule is not None

        analysis_cmb = resolve_card_history_due_date_rule(session, cmb_card.id, statement_day=15, bank_code="CMB")
        assert analysis_cmb.applicable_pair_count == 1
        assert analysis_cmb.classification.value == "INSUFFICIENT"


# Case E: Hard delete 0183 => account Statement survives, tx card_id NULL, 6571 survives.
def test_case_e_hard_delete_preserves_account_statement(client, auth_headers):
    with client.app.state.session_factory() as session:
        card_a = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 0183", last4="0183", currency="CNY")
        card_b = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 6571", last4="6571", currency="CNY")
        session.add_all([card_a, card_b])
        session.commit()
        card_a_id = card_a.id
        card_b_id = card_b.id

    raw = make_ccb_eml()
    res = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb.eml", raw, "message/rfc822")})
    stmt_id = res.json()["statements"][0]["id"]

    del_res = client.delete(f"/api/v1/cards/{card_a_id}?mode=hard", headers=auth_headers)
    assert del_res.status_code == 200

    assert client.get(f"/api/v1/statements/{stmt_id}", headers=auth_headers).status_code == 200
    assert client.get(f"/api/v1/cards/{card_b_id}", headers=auth_headers).status_code == 200
    txs = client.get("/api/v1/transactions", headers=auth_headers).json()
    by_desc = {tx["raw_description"]: tx for tx in txs}
    assert by_desc["0183 tx"]["card_id"] is None
    assert by_desc["6571 tx"]["card_id"] == card_b_id


# Case F: Unknown ref 9999 => tx card_id NULL; no Card created.
def test_case_f_unknown_ref_leaves_tx_card_null(client, auth_headers):
    txs_html = """
      <tr><td>2026-04-20</td><td>2026-04-21</td><td>9999/扫码</td>
          <td>9999 tx</td><td>CNY</td><td>500.00</td>
          <td>CNY</td><td>500.00</td></tr>
    """
    raw = make_ccb_eml(transactions_html=txs_html)
    res = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb.eml", raw, "message/rfc822")})
    assert res.status_code == 200
    assert client.get("/api/v1/cards", headers=auth_headers).json() == []
    txs = client.get("/api/v1/transactions", headers=auth_headers).json()
    assert len(txs) == 1
    assert txs[0]["card_id"] is None


# Case G: Ambiguous duplicate CCB last4 => safe fail.
def test_case_g_ambiguous_duplicate_ccb_last4_safe_fails(client, auth_headers):
    with client.app.state.session_factory() as session:
        c1 = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB Card 1", last4="0183", currency="CNY")
        c2 = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB Card 2", last4="0183", currency="USD")
        session.add_all([c1, c2])
        session.commit()

    raw = make_ccb_eml()
    res = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb.eml", raw, "message/rfc822")})
    assert res.status_code == 422
    assert "AMBIGUOUS_CARD_REFERENCE" in res.text
    assert client.get("/api/v1/statements", headers=auth_headers).json() == []


# Case H: CCB bank outstanding=account amount; no fake per-card outstanding.
def test_case_h_ccb_bank_outstanding_equals_account_amount(client, auth_headers):
    with client.app.state.session_factory() as session:
        card1 = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 0183", last4="0183", currency="CNY", credit_limit_minor=100000)
        card2 = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 6571", last4="6571", currency="CNY", credit_limit_minor=100000)
        session.add_all([card1, card2])
        session.commit()

    future_due = (date.today() + timedelta(days=15)).strftime("%Y-%m-%d")
    raw = make_ccb_eml(account_total="500.00", due_date=future_due)
    res = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb.eml", raw, "message/rfc822")})
    assert res.status_code == 200

    due_res = client.get("/api/v1/dashboard", headers=auth_headers).json()["due"]
    assert due_res.get("total_remaining_minor") == 50000
    assert len(due_res["cards"]) == 1
    due_card = due_res["cards"][0]
    assert due_card["card_id"] is None
    assert due_card["remaining_minor"] == 50000

    analytics_res = client.get("/api/v1/analytics", headers=auth_headers).json()
    bank_util = {b["bank_code"]: b for b in analytics_res.get("bank_utilization", [])}
    assert "CCB" in bank_util
    assert bank_util["CCB"]["outstanding_minor"] == 50000

    card_util = {c["last4"]: c for c in analytics_res.get("card_utilization", [])}
    assert card_util["0183"]["outstanding_minor"] == 0
    assert card_util["6571"]["outstanding_minor"] == 0


def test_case_h2_ccb_bank_usage_no_double_count(client, auth_headers):
    today = date.today()
    prev_month = today.replace(day=1) - timedelta(days=1)
    tx_date = prev_month.strftime("%Y-%m-%d")
    future_due = (today + timedelta(days=15)).strftime("%Y-%m-%d")

    with client.app.state.session_factory() as session:
        card1 = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 0183", last4="0183", currency="CNY", credit_limit_minor=100000)
        card2 = Card(bank_code="CCB", bank_name="中国建设银行", display_name="CCB 6571", last4="6571", currency="CNY", credit_limit_minor=100000)
        session.add_all([card1, card2])
        session.commit()

    transactions_html = f"""
      <tr><td>{tx_date}</td><td>{tx_date}</td><td>0183/扫码</td>
          <td>0183 tx</td><td>CNY</td><td>300.00</td>
          <td>CNY</td><td>300.00</td></tr>
      <tr><td>{tx_date}</td><td>{tx_date}</td><td>6571/扫码</td>
          <td>6571 tx</td><td>CNY</td><td>200.00</td>
          <td>CNY</td><td>200.00</td></tr>
    """
    raw = make_ccb_eml(
        account_total="500.00",
        due_date=future_due,
        transactions_html=transactions_html,
    )
    res = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb.eml", raw, "message/rfc822")})
    assert res.status_code == 200, res.text

    analytics = client.get("/api/v1/analytics", headers=auth_headers).json()

    month_key = prev_month.strftime("%Y-%m")
    consumption = {m["month"]: m for m in analytics["monthly_consumption"]}
    assert month_key in consumption
    assert consumption[month_key]["amount_minor"] == 50000
    ccb_usage = [
        b for b in consumption[month_key]["bank_usage"] if b["bank_code"] == "CCB"
    ]
    assert len(ccb_usage) == 1
    assert ccb_usage[0]["amount_minor"] == 50000

    bank_util = {b["bank_code"]: b for b in analytics["bank_utilization"]}
    assert bank_util["CCB"]["outstanding_minor"] == 50000

    card_util = {c["last4"]: c for c in analytics["card_utilization"]}
    assert card_util["0183"]["outstanding_minor"] == 0
    assert card_util["6571"]["outstanding_minor"] == 0


# Case I: Duplicate account Statement blocked with nullable card_id.
def test_case_i_duplicate_account_statement_blocked(client, auth_headers):
    raw = make_ccb_eml()
    r1 = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb.eml", raw, "message/rfc822")})
    assert r1.status_code == 200
    assert r1.json()["deduplicated"] is False

    r2 = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb.eml", raw, "message/rfc822")})
    assert r2.status_code == 200
    assert r2.json()["deduplicated"] is True
    assert len(client.get("/api/v1/statements", headers=auth_headers).json()) == 1


# Case J: Non-zero USD/EUR payable evidence => safe fail atomically; USD 0.00 allowed.
def test_case_j_foreign_currency_payable_safe_fail(client, auth_headers):
    # J1. USD 0.00 in payment details allowed
    zero_usd_detail = """
      <tr><td>62216823****0183</td><td>美元(USD)</td><td>0.00</td><td>0.00</td></tr>
    """
    raw_ok = make_ccb_eml(extra_payment_details_html=zero_usd_detail)
    res_ok = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb_zero_usd.eml", raw_ok, "message/rfc822")})
    assert res_ok.status_code == 200, res_ok.text

    # J2. Non-zero USD payable in payment details fails atomically
    nonzero_usd_detail = """
      <tr><td>62216823****0183</td><td>美元(USD)</td><td>10.00</td><td>10.00</td></tr>
    """
    raw_fail = make_ccb_eml(statement_date="2026-06-15", extra_payment_details_html=nonzero_usd_detail)
    res_fail = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb_usd.eml", raw_fail, "message/rfc822")})
    assert res_fail.status_code == 422
    assert "UNSUPPORTED_FOREIGN_PAYABLE" in res_fail.text

    # J3. Account Summary: CNY 100.00, USD 20.00, NO minimum-due column => SAFE FAIL
    msg_j3 = EmailMessage()
    msg_j3["Subject"] = "中国建设银行信用卡电子账单"
    msg_j3["From"] = "service@vip.ccb.com"
    msg_j3.set_content(
        """
        <div>本期账单日 2026-07-15</div>
        <div>本期到期还款日 2026-08-04</div>
        <table>
          <tr><th>账户币种 Currency</th><th>本期全部应还款额 New Balance</th></tr>
          <tr><td>人民币（CNY）</td><td>100.00</td></tr>
          <tr><td>美元（USD）</td><td>20.00</td></tr>
        </table>
        <table><tr><td>【交易明细】</td></tr>
          <tr><td>2026-06-20</td><td>2026-06-21</td><td>0183/扫码</td>
              <td>0183 tx</td><td>CNY</td><td>100.00</td>
              <td>CNY</td><td>100.00</td></tr>
        </table>
        """,
        subtype="html",
    )
    res_j3 = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb_j3.eml", msg_j3.as_bytes(), "message/rfc822")})
    assert res_j3.status_code == 422
    assert "UNSUPPORTED_FOREIGN_PAYABLE" in res_j3.text

    # J4. Account Summary: CNY 100.00, USD 0.00, NO minimum-due column => PASS
    msg_j4 = EmailMessage()
    msg_j4["Subject"] = "中国建设银行信用卡电子账单"
    msg_j4["From"] = "service@vip.ccb.com"
    msg_j4.set_content(
        """
        <div>本期账单日 2026-08-15</div>
        <div>本期到期还款日 2026-09-04</div>
        <table>
          <tr><th>账户币种 Currency</th><th>本期全部应还款额 New Balance</th></tr>
          <tr><td>人民币（CNY）</td><td>100.00</td></tr>
          <tr><td>美元（USD）</td><td>0.00</td></tr>
        </table>
        <table><tr><td>【交易明细】</td></tr>
          <tr><td>2026-07-20</td><td>2026-07-21</td><td>0183/扫码</td>
              <td>0183 tx</td><td>CNY</td><td>100.00</td>
              <td>CNY</td><td>100.00</td></tr>
        </table>
        """,
        subtype="html",
    )
    res_j4 = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("ccb_j4.eml", msg_j4.as_bytes(), "message/rfc822")})
    assert res_j4.status_code == 200, res_j4.text


# Case K: Legacy non-CCB unchanged.
def test_case_k_legacy_non_ccb_unchanged(client, auth_headers):
    try:
        from test_cgb_parser import SYNTHETIC_HTML
    except ImportError:
        from tests.test_cgb_parser import SYNTHETIC_HTML

    msg = EmailMessage()
    msg["Subject"] = "广发信用卡 2026年09月电子账单"
    msg["From"] = "广发银行 <statement@creditcard.cgbchina.com.cn>"
    msg.set_content(SYNTHETIC_HTML, subtype="html")
    raw = msg.as_bytes()

    res = client.post("/api/v1/imports/eml", headers=auth_headers, files={"file": ("cgb.eml", raw, "message/rfc822")})
    assert res.status_code == 200, res.text
    stmts = res.json()["statements"]
    assert len(stmts) == 1
    assert stmts[0]["card_id"] is not None
    assert stmts[0]["bank_code"] == "CGB"


# Case L: Current schema/index validation.
def test_case_l_migration_current_schema_structure(tmp_path: Path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    database_path = tmp_path / "test_schema.db"
    monkeypatch.setenv("CCM_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = Config(str(alembic_ini))
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as conn:
        stmt_cols = {r[1]: r for r in conn.execute("PRAGMA table_info(statements)")}
        assert "bank_code" in stmt_cols
        assert stmt_cols["bank_code"][3] == 1  # notnull
        assert stmt_cols["card_id"][3] == 0    # nullable

        tx_cols = {r[1]: r for r in conn.execute("PRAGMA table_info(transactions)")}
        assert "card_id" in tx_cols

        indexes = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='statements'")}
        assert "uq_statement_period_account" in indexes
        assert "uq_statement_period_card" in indexes


# Case N: Manual Statement bank/card mismatch rejected.
def test_case_n_manual_statement_bank_card_mismatch_rejected(client, auth_headers):
    with client.app.state.session_factory() as session:
        card = Card(bank_code="CMB", bank_name="招商银行", display_name="CMB Card", last4="1234", currency="CNY")
        session.add(card)
        session.commit()
        card_id = card.id

    res_ok = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json={
            "card_id": card_id,
            "bank_code": "CMB",
            "statement_date": "2026-08-01",
            "due_date": "2026-08-20",
            "total_due_minor": 10000,
            "currency": "CNY",
        },
    )
    assert res_ok.status_code == 201
    assert res_ok.json()["bank_code"] == "CMB"

    res_fail = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json={
            "card_id": card_id,
            "bank_code": "CCB",
            "statement_date": "2026-09-01",
            "due_date": "2026-09-20",
            "total_due_minor": 10000,
            "currency": "CNY",
        },
    )
    assert res_fail.status_code == 422
    assert "does not match" in res_fail.text
