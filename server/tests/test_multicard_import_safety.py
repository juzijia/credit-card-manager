from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.models.card import Card
from app.models.import_record import ImportRecord, ImportStatus
from app.models.repayment import Repayment
from app.models.statement import Statement
from app.models.transaction import Transaction


def _eml(bank, *, second_card="1234", extra_summary=False):
    if bank == "CMB":
        summary = '<tr><td><img data-code="bqyhje_img"></td><td>200.00</td></tr>'
        html = (
            '<table><tr><td><img data-code="dqhkr_img"></td><td>2026/09/25</td></tr>'
            '<tr><td><img data-code="zdzq_img"></td><td>2026/08/01-2026/08/31</td></tr>'
            + summary * (2 if extra_summary else 1) + '</table><table>'
        )
        for card in ("1234", second_card):
            html += (
                '<tr><td></td><td>0810</td><td>0811</td><td>合成消费</td>'
                f'<td>100.00</td><td>{card}</td><td>CNY</td><td>100.00</td></tr>'
            )
        subject, sender = "招商银行信用卡电子账单", "statement@message.cmbchina.com"
    else:
        summary = (
            '<tr><td>1234(牡丹贷记卡)</td><td>人民币(本位币)</td>'
            '<td>200.00/RMB</td><td>20.00/RMB</td><td>32,000.00/RMB</td></tr>'
        )
        html = (
            '<div>账单周期 2026年08月01日—2026年08月31日</div>'
            '<div>对账单生成日 2026年08月31日</div><div>到期还款日 2026年09月25日</div>'
            '<table>' + summary * (2 if extra_summary else 1) + '</table><table>'
        )
        for card in ("1234", second_card):
            html += (
                f'<tr><td>{card}</td><td>2026-08-10</td><td>2026-08-11</td>'
                '<td>消费</td><td>合成消费</td><td>人民币</td><td>100.00/RMB</td></tr>'
            )
        subject, sender = "中国工商银行客户对账单", "statement@creditcard.icbc.com.cn"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message.set_content(html + '</table>', subtype="html")
    return message.as_bytes()


def _import(client, headers, data):
    return client.post(
        "/api/v1/imports/eml", headers=headers,
        files={"file": ("synthetic.eml", data, "message/rfc822")},
    )


@pytest.mark.parametrize("bank", ["CMB", "ICBC"])
@pytest.mark.parametrize("case", ["other-card", "unknown-card", "multiple-summaries"])
def test_ambiguous_import_repeatedly_fails_without_financial_rows(
    client, auth_headers, bank, case
):
    data = _eml(
        bank,
        second_card={"other-card": "5678", "unknown-card": ""}.get(case, "1234"),
        extra_summary=case == "multiple-summaries",
    )
    for _ in range(2):
        response = _import(client, auth_headers, data)
        assert response.status_code == 422, response.text
        assert response.json()["detail"]
    with client.app.state.session_factory() as session:
        for model in (Card, Statement, Transaction, Repayment):
            assert session.scalar(select(model)) is None
        records = session.scalars(select(ImportRecord)).all()
        assert len(records) == 1
        assert records[0].parse_status == ImportStatus.FAILED


@pytest.mark.parametrize("bank", ["CMB", "ICBC"])
def test_single_card_reimport_and_rejection_preserve_existing_financial_data(
    client, auth_headers, bank
):
    data = _eml(bank)
    first = _import(client, auth_headers, data)
    assert first.status_code == 200, first.text
    statement_id = first.json()["statement"]["id"]
    assert first.json()["statement"]["total_due_minor"] == 20000
    with client.app.state.session_factory() as session:
        transactions = session.scalars(select(Transaction)).all()
        assert len(transactions) == 2  # Legitimate same-day, same-amount purchases.
        assert [item.amount_minor for item in transactions] == [10000, 10000]
        assert all(item.card_id == first.json()["statement"]["card_id"] for item in transactions)
        transactions[0].transaction_type = "reward"
        transactions[0].classification_source = "manual"
        refund = Transaction(
            statement_id=statement_id, card_id=transactions[0].card_id,
            transaction_date=transactions[0].transaction_date, amount_minor=-1000,
            currency="CNY", raw_description="人工关联退款", transaction_type="refund",
            classification_source="manual", original_transaction_id=transactions[0].id,
        )
        session.add(refund)
        session.add(Repayment(statement_id=statement_id, amount_minor=5000,
                              idempotency_key="synthetic-payment"))
        session.commit()
        original_ids = {item.id for item in transactions} | {refund.id}
        refund_link = (refund.id, refund.original_transaction_id)

    rejected = _import(client, auth_headers, _eml(bank, second_card="5678"))
    assert rejected.status_code == 422, rejected.text
    repeated = _import(client, auth_headers, data)
    assert repeated.status_code == 409, repeated.text
    assert repeated.json()["detail"]["code"] == "IMPORT_CORRECTION_BLOCKED"
    with client.app.state.session_factory() as session:
        assert len(session.scalars(select(Card)).all()) == 1
        assert len(session.scalars(select(Statement)).all()) == 1
        transactions = session.scalars(select(Transaction)).all()
        assert {item.id for item in transactions} == original_ids
        assert sum(item.classification_source == "manual" and item.transaction_type == "reward"
                   for item in transactions) == 1
        assert session.get(Transaction, refund_link[0]).original_transaction_id == refund_link[1]
        assert len(session.scalars(select(Repayment)).all()) == 1
