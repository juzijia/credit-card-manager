import json
import shutil
import subprocess
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app import models  # noqa: F401
from app.core.config import Settings
from app.db.base import Base
from app.db.session import create_database_engine, create_session_factory
from app.models.manual_bill import ManualBillPayment
from app.models.parsed_batch import ParsedBatch, ParsedEntry
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.modules.core.transaction import classify_transaction_type
from app.parsers.abc import AbcStatementParser
from app.parsers.base import ParsedSource, ParsedTransaction, ParserError
from app.parsers.boc import _money_from_cell, _transactions_from_tables
from app.parsers.cgb import _parse_money_minor
from app.schemas.transaction import TransactionRead
from app.services.analytics import build_analytics
from app.services.eml_import import _transaction_from_parsed
from app.services.mortgage import calculate_equal_payment_schedule, calculate_equal_principal_schedule, round2
from app.services.parsed_batches import stage_parsed_file


@pytest.fixture
def database(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'finance.sqlite').as_posix()}"
    monkeypatch.setenv("CCM_SECRETS_FILE", str(tmp_path / "secrets.json"))
    engine = create_database_engine(url)
    Base.metadata.create_all(engine)
    with create_session_factory(engine, "Asia/Shanghai")() as session:
        yield session, url
    engine.dispose()


@pytest.mark.parametrize("description", ["中国移动自动续费代扣", "保险月缴代扣", "支付宝转账消费"])
def test_positive_debit_counts_as_consumption_after_import(database, description):
    session, _ = database
    today = date(2026, 9, 30)
    statement = Statement(bank_code="BOC", statement_date=today, due_date=date(2026, 10, 20), total_due_minor=1990)
    session.add(statement)
    session.flush()
    item = ParsedTransaction(today, today, 1990, "CNY", description)
    transaction = _transaction_from_parsed(session, statement.id, item, "BOC", None)
    session.add(transaction)
    session.commit()
    with patch("app.services.analytics.local_today", return_value=today):
        result = build_analytics(session, 1)
    assert result.monthly_consumption[0].amount_minor == 1990
    assert TransactionRead.model_validate(transaction).transaction_type == "purchase"


def test_old_automatic_classification_is_corrected_without_overriding_manual(database):
    session, _ = database
    today = date(2026, 9, 30)
    statement = Statement(bank_code="BOC", statement_date=today, due_date=date(2026, 10, 20), total_due_minor=3980)
    session.add(statement)
    session.flush()
    transactions = [Transaction(statement_id=statement.id, transaction_date=today, posting_date=today,
                                amount_minor=1990, currency="CNY", raw_description="保险月缴代扣",
                                transaction_type="repayment", classification_source=source,
                                classification_rule_version="2026-09-15-v2")
                    for source in ["rule", "manual"]]
    session.add_all(transactions)
    session.commit()
    with patch("app.services.analytics.local_today", return_value=today):
        result = build_analytics(session, 1)
    assert result.monthly_consumption[0].amount_minor == 1990
    assert [TransactionRead.model_validate(tx).transaction_type for tx in transactions] == ["purchase", "repayment"]
    # Reading the corrected classification must not rewrite source records.
    assert [tx.transaction_type for tx in transactions] == ["repayment", "repayment"]


@pytest.mark.parametrize("amount, description, expected", [
    (-1990, "自动还款", "repayment"), (-1990, "商品退款", "refund"),
    (1990, "代扣失败", "adjustment"), (1990, "信用卡年费代扣", "annual_fee"),
])
def test_repayment_and_other_explicit_categories_keep_their_meaning(amount, description, expected):
    assert classify_transaction_type(amount, description).value == expected


@pytest.mark.parametrize("value, expected", [
    ("12345.67", 1234567), ("12,345.67", 1234567), ("12 345.67", 1234567),
    ("1，234.00", 123400), ("￥12,345.67 元", 1234567), ("-12 345.67", -1234567),
    ("", None), ("Credit Card No.", None),
])
def test_boc_amount_cell_preserves_the_whole_amount(value, expected):
    assert _money_from_cell(value, "transaction") == expected


@pytest.mark.parametrize("value", ["12,34.56", "12.355", "12 34.56", "12.34 56.78"])
def test_boc_amount_cell_rejects_ambiguous_or_fractional_cent_values(value):
    with pytest.raises(ParserError):
        _money_from_cell(value, "transaction")


def test_boc_table_transaction_preserves_space_grouped_amount():
    rows = [["2026-09-02", "2026-09-03", "1234", "保险月缴", "", "12 345.67"]]
    table = SimpleNamespace(extract=lambda: rows)
    doc = [SimpleNamespace(find_tables=lambda: SimpleNamespace(tables=[table]))]
    transactions, _ = _transactions_from_tables(doc)
    assert transactions[0].amount_minor == 1234567


def abc_source(tmp_path, amount):
    html = f'''<html><body><span>账单周期 Statement Cycle 2026/09/01-2026/09/30</span>
    <p>到期还款日 Payment Due Date 2026/10/20</p><p>Card No 123456******1234</p>
    <p>Credit Limit 10,000.00</p><p>人民币 RMB 12.35</p>
    <table><tr><td>260902</td><td>260903</td><td>1234</td><td>保险月缴代扣</td>
    <td>-{amount}/CNY</td><td>-{amount}/CNY</td></tr></table></body></html>'''
    email = EmailMessage()
    email["From"] = "statements@creditcard.abchina.com.cn"
    email["Subject"] = "中国农业银行金穗信用卡"
    email.set_content(html, subtype="html")
    raw = email.as_bytes()
    path = tmp_path / "abc.eml"
    path.write_bytes(raw)
    source = ParsedSource("eml", str(email["Subject"]), str(email["From"]), html, "synthetic-source", raw_bytes=raw)
    return source, path


@pytest.mark.parametrize("amount", ["12.345", "12.355"])
def test_real_abc_import_paths_both_reject_fractional_cents(database, tmp_path, amount):
    session, _ = database
    source, path = abc_source(tmp_path, amount)
    with pytest.raises(ParserError, match="decimal"):
        AbcStatementParser().parse(source)
    with pytest.raises(ParserError, match="decimal"):
        stage_parsed_file(session, path, source_type="eml", provider_id="abc_credit")
    assert session.scalar(select(ParsedBatch)) is None


def test_real_abc_import_paths_preserve_exact_cents(database, tmp_path):
    session, _ = database
    source, path = abc_source(tmp_path, "12.34")
    parsed = AbcStatementParser().parse(source)
    batch, _ = stage_parsed_file(session, path, source_type="eml", provider_id="abc_credit")
    staged = session.scalar(select(ParsedEntry).where(ParsedEntry.batch_id == batch.id))
    assert parsed.transactions[0].amount_minor == staged.amount_minor == 1234


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_money_is_reported_as_a_parser_error(value):
    with pytest.raises(ParserError):
        _parse_money_minor(value, "transaction", allow_negative=True)


@pytest.mark.parametrize("value, expected", [("12.340", 1234), ("-12.340", -1234)])
def test_extra_trailing_zeros_do_not_change_exact_minor_units(value, expected):
    assert _parse_money_minor(value, "transaction", allow_negative=True) == expected


@pytest.mark.parametrize("value, expected", [
    (100000.125, 100000.13), (1.005, 1.01), (-1.005, -1.01), (1.2349999999999999, 1.23),
])
def test_mortgage_rounding_uses_decimal_half_up(value, expected):
    assert round2(value) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to compare the frontend calculator")
@pytest.mark.parametrize("method", ["equal_principal", "equal_payment"])
@pytest.mark.parametrize("principal, rate, months", [(120000150, 0, 12), (100000000, 3.2, 360), (10001, 2.75, 12)])
def test_full_mortgage_schedule_matches_frontend(method, principal, rate, months):
    function = calculate_equal_principal_schedule if method == "equal_principal" else calculate_equal_payment_schedule
    backend = function(principal, rate, months, date(2026, 10, 2))
    script = '''const m=require('./web/js/mortgage.js');
    const [method,p,rate,n]=JSON.parse(process.argv[1]);
    const result=m.calculateSingleLoan(p/100,rate,n,method);
    console.log(JSON.stringify(result.schedule.map(x=>[x.payment,x.principal,x.interest,x.remainingPrincipal].map(v=>Math.round(v*100)))));'''
    frontend = json.loads(subprocess.run(["node", "-e", script, json.dumps([method, principal, rate, months])],
                                        cwd=Path(__file__).resolve().parents[2], text=True, capture_output=True, check=True).stdout)
    assert [[row.payment_minor, row.principal_minor, row.interest_minor, row.remaining_principal_minor]
            for row in backend] == frontend
    assert sum(row.principal_minor for row in backend) == principal


def test_saved_mortgage_bill_uses_the_preview_rounding(database):
    from fastapi.testclient import TestClient
    from app.main import create_app

    session, url = database
    today = date(2026, 10, 2)
    settings = Settings(database_url=url, env="test", auth_mode="dev_bypass",
                        background_tasks_enabled=False, error_log_path=None)
    with TestClient(create_app(settings)) as client, patch("app.api.v1.mortgage.local_today", return_value=today):
        response = client.post("/api/v1/mortgage/save-bill", json={
            "name": "回归测试房贷", "loan_type": "commercial", "repayment_method": "equal_principal",
            "loan_term_months": 12, "commercial_principal_minor": 120000150,
            "commercial_actual_rate": 0, "first_payment_date": today.isoformat(),
        })
        assert response.status_code == 201, response.text
        payment = session.scalar(select(ManualBillPayment).where(
            ManualBillPayment.manual_bill_id == response.json()["manual_bill_id"]))
        assert payment.amount_minor == 10000013
