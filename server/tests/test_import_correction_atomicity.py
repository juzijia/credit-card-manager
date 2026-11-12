from datetime import date
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.models.statement import Statement
from app.models.card import Card
from app.models.transaction import Transaction
from app.parsers.base import (
    ParsedSource, ParsedStatement, ParsedTransaction, ParserError,
    ParsedAccountSummary, ParsedBillingDocument, ParsedBillingUnit, billing_document_from_statement,
)
from app.services.eml_import import import_parsed_source


def _import(session, amount, *, confirmation_token=None, card_id=None):
    source = ParsedSource("eml", "synthetic", "synthetic@example.invalid", "", str(amount))
    parsed = ParsedStatement(
        "CGB", "1234", date(2026, 9, 1), date(2026, 9, 20), amount, 0, "CNY",
        transactions=(ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11),
                                        amount, "CNY", "合成消费"),),
    )
    kwargs = {} if confirmation_token is None else {"confirmation_token": confirmation_token}
    return import_parsed_source(
        session, source, document=billing_document_from_statement(parsed),
        parser=SimpleNamespace(version="test"), card_id=card_id, **kwargs,
    )


def test_changed_source_requires_confirmation_without_writing(client):
    with client.app.state.session_factory() as session:
        original = _import(session, 10000)
        with pytest.raises(ParserError) as conflict:
            _import(session, 15000)
        assert conflict.value.detail["code"] == "IMPORT_CONFIRMATION_REQUIRED"
        assert session.get(Statement, original.statement.id).total_due_minor == 10000
        assert [row.amount_minor for row in session.scalars(select(Transaction))] == [10000]


def test_confirmed_correction_rolls_back_if_commit_fails(client):
    with client.app.state.session_factory() as session:
        original = _import(session, 10000)
        with pytest.raises(ParserError) as conflict:
            _import(session, 15000)
        token = conflict.value.detail["confirmation_token"]

        def fail_commit(_session):
            raise RuntimeError("synthetic commit failure")

        event.listen(session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match="synthetic commit failure"):
                _import(session, 15000, confirmation_token=token)
        finally:
            event.remove(session, "before_commit", fail_commit)
        assert session.get(Statement, original.statement.id).total_due_minor == 10000
        assert [row.amount_minor for row in session.scalars(select(Transaction))] == [10000]
        updated = _import(session, 15000, confirmation_token=token)
        assert updated.statement.id == original.statement.id
        assert updated.statement.total_due_minor == 15000
        assert [row.amount_minor for row in session.scalars(select(Transaction))] == [15000]


def test_confirmation_is_bound_to_the_uploaded_source(client):
    with client.app.state.session_factory() as session:
        _import(session, 10000)
        with pytest.raises(ParserError) as conflict:
            _import(session, 15000)
        with pytest.raises(ParserError) as stale:
            _import(session, 20000, confirmation_token=conflict.value.detail["confirmation_token"])
        assert stale.value.detail["code"] == "IMPORT_CONFIRMATION_STALE"
        assert [row.amount_minor for row in session.scalars(select(Transaction))] == [10000]


def test_confirmation_cannot_recreate_a_deleted_statement(client):
    with client.app.state.session_factory() as session:
        original = _import(session, 10000)
        with pytest.raises(ParserError) as conflict:
            _import(session, 15000)
        session.delete(session.get(Statement, original.statement.id))
        session.commit()
        with pytest.raises(ParserError) as stale:
            _import(session, 15000, confirmation_token=conflict.value.detail["confirmation_token"])
        assert stale.value.detail["code"] == "IMPORT_CONFIRMATION_STALE"
        assert session.scalar(select(Statement)) is None


def test_multi_unit_confirmation_never_partially_updates(client):
    def import_units(session, amount, token=None):
        document = ParsedBillingDocument(
            bank_code="CGB", statement_date=date(2026, 9, 1), due_date=date(2026, 9, 20),
            billing_units=tuple(ParsedBillingUnit(card, "CNY", amount, 0) for card in ("1234", "5678")),
            transactions=tuple(ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11),
                                                amount, "CNY", "合成消费", card)
                               for card in ("1234", "5678")),
        )
        return import_parsed_source(
            session, ParsedSource("eml", "synthetic", "test@example.invalid", "", f"multi-{amount}"),
            document=document, parser=SimpleNamespace(version="test"), confirmation_token=token,
        )

    with client.app.state.session_factory() as session:
        import_units(session, 10000)
        with pytest.raises(ParserError) as conflict:
            import_units(session, 15000)
        assert len(conflict.value.detail["differences"]) == 2
        assert [row.total_due_minor for row in session.scalars(select(Statement))] == [10000, 10000]
        result = import_units(session, 15000, conflict.value.detail["confirmation_token"])
        assert len(result.statements) == 2
        assert [row.total_due_minor for row in session.scalars(select(Statement))] == [15000, 15000]
        assert [row.amount_minor for row in session.scalars(select(Transaction))] == [15000, 15000]


def test_concurrent_confirmation_is_idempotent(client):
    with client.app.state.session_factory() as session:
        _import(session, 10000)
        with pytest.raises(ParserError) as conflict:
            _import(session, 15000)
        token = conflict.value.detail["confirmation_token"]
    start = Barrier(2)

    def confirm():
        start.wait()
        with client.app.state.session_factory() as session:
            return _import(session, 15000, confirmation_token=token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(confirm) for _ in range(2)]
        results = [future.result() for future in futures]
    assert sorted(result.deduplicated for result in results) == [False, True]
    assert results[0].statement.id == results[1].statement.id
    with client.app.state.session_factory() as session:
        assert [row.total_due_minor for row in session.scalars(select(Statement))] == [15000]
        assert [row.amount_minor for row in session.scalars(select(Transaction))] == [15000]


def test_reimport_cannot_switch_to_another_physical_card_with_same_tail(client):
    with client.app.state.session_factory() as session:
        original = _import(session, 10000)
        other_card = Card(bank_code="CGB", bank_name="合成银行", display_name="另一张卡",
                          last4="1234", currency="USD")
        session.add(other_card)
        session.commit()
        with pytest.raises(ParserError) as blocked:
            _import(session, 10000, card_id=other_card.id)
        assert blocked.value.detail["code"] == "IMPORT_CORRECTION_BLOCKED"
        statements = session.scalars(select(Statement)).all()
        assert len(statements) == 1
        assert statements[0].card_id == original.statement.card_id


@pytest.mark.parametrize("existing_account", [False, True])
def test_ccb_account_and_card_statement_cannot_double_count_a_period(client, existing_account):
    with client.app.state.session_factory() as session:
        card = Card(bank_code="CCB", bank_name="合成银行", display_name="合成卡", last4="1234")
        session.add(card)
        session.flush()
        statement = Statement(
            bank_code="CCB", card_id=None if existing_account else card.id,
            statement_date=date(2026, 9, 1), due_date=date(2026, 9, 20),
            total_due_minor=10000, currency="CNY",
        )
        session.add(statement)
        session.commit()
        document = ParsedBillingDocument(
            bank_code="CCB", statement_date=date(2026, 9, 1), due_date=date(2026, 9, 20),
            billing_units=(ParsedBillingUnit("1234", "CNY", 15000, 0),) if existing_account else (),
            transactions=(), account_summaries=(ParsedAccountSummary("CNY", 15000, 0),),
        )
        with pytest.raises(ParserError) as blocked:
            import_parsed_source(
                session, ParsedSource("eml", "synthetic", "test@example.invalid", "", "new-ccb-source"),
                document=document, parser=SimpleNamespace(version="test"),
            )
        assert blocked.value.detail["code"] == "IMPORT_CORRECTION_BLOCKED"
        assert [row.total_due_minor for row in session.scalars(select(Statement))] == [10000]
