import pytest
from datetime import date
from types import SimpleNamespace
from sqlalchemy import select
from app.models.card import Card
from app.models.import_record import ImportRecord, ImportStatus
from app.models.repayment import Repayment
from app.models.statement import ParseStatus, Statement
from app.models.transaction import Transaction
from app.modules.core.transaction import ClassificationSource, TransactionType
from app.parsers.base import (
    ParsedBillingDocument,
    ParsedBillingUnit,
    ParsedSource,
    ParsedStatement,
    ParsedTransaction,
    ParserError,
    billing_document_from_statement,
)
from app.services.eml_import import ImportConflict, import_parsed_source


def _make_source(sha: str, source_type: str = "eml") -> ParsedSource:
    return ParsedSource(
        source_type=source_type,
        subject="test",
        sender="test@example.invalid",
        html="",
        sha256=sha,
    )


def _make_document(
    bank_code: str = "CGB",
    card_ref: str = "1234",
    statement_date: date = date(2026, 9, 1),
    due_date: date = date(2026, 9, 20),
    total_due_minor: int = 10000,
    minimum_due_minor: int = 1000,
    currency: str = "CNY",
    transactions: tuple[ParsedTransaction, ...] = (),
    warnings: tuple[str, ...] = (),
) -> ParsedBillingDocument:
    parsed = ParsedStatement(
        bank_code=bank_code,
        card_last4=card_ref,
        statement_date=statement_date,
        due_date=due_date,
        total_due_minor=total_due_minor,
        minimum_due_minor=minimum_due_minor,
        currency=currency,
        warnings=warnings,
        transactions=transactions,
    )
    return billing_document_from_statement(parsed)


def test_amount_correction_100_to_150_same_source_flow(client):
    tx1 = ParsedTransaction(
        transaction_date=date(2026, 8, 10),
        posting_date=date(2026, 8, 11),
        amount_minor=10000,
        currency="CNY",
        raw_description="old",
    )
    doc_100 = _make_document(total_due_minor=10000, transactions=(tx1,))
    source_same = _make_source("sha-same-source")
    parser = SimpleNamespace(version="1.0.0")

    with client.app.state.session_factory() as session:
        res1 = import_parsed_source(session, source_same, document=doc_100, parser=parser)
        assert res1.deduplicated is False
        assert res1.corrected is False
        statement_id = res1.statement.id

        tx2 = ParsedTransaction(
            transaction_date=date(2026, 8, 10),
            posting_date=date(2026, 8, 11),
            amount_minor=15000,
            currency="CNY",
            raw_description="new-150",
        )
        doc_150 = _make_document(total_due_minor=15000, transactions=(tx2,))
        with pytest.raises(ImportConflict) as exc_info:
            import_parsed_source(session, source_same, document=doc_150, parser=parser)
        detail = exc_info.value.detail
        assert detail["code"] == "IMPORT_CONFIRMATION_REQUIRED"
        token = detail["confirmation_token"]
        assert token is not None
        diff = detail["differences"][0]
        assert diff["old"]["total_due_minor"] == 10000
        assert diff["new"]["total_due_minor"] == 15000
        assert len(diff["transactions_removed"]) == 1
        assert len(diff["transactions_added"]) == 1

        stmt = session.get(Statement, statement_id)
        assert stmt.total_due_minor == 10000
        assert [t.amount_minor for t in stmt.transactions] == [10000]

        res_conf = import_parsed_source(
            session, source_same, document=doc_150, parser=parser, confirmation_token=token
        )
        assert res_conf.corrected is True
        assert res_conf.deduplicated is False
        session.expire_all()
        stmt_updated = session.get(Statement, statement_id)
        assert stmt_updated.total_due_minor == 15000
        assert [t.amount_minor for t in stmt_updated.transactions] == [15000]

        res_replay = import_parsed_source(session, source_same, document=doc_150, parser=parser)
        assert res_replay.deduplicated is True
        assert res_replay.corrected is False


def test_amount_correction_different_source_flow(client):
    source_old = _make_source("sha-old-source")
    source_new = _make_source("sha-new-source")
    parser = SimpleNamespace(version="1.0.0")

    doc_old = _make_document(
        total_due_minor=10000,
        transactions=(
            ParsedTransaction(
                transaction_date=date(2026, 8, 10),
                posting_date=date(2026, 8, 11),
                amount_minor=10000,
                currency="CNY",
                raw_description="old-tx",
            ),
        ),
    )
    doc_new = _make_document(
        total_due_minor=15000,
        transactions=(
            ParsedTransaction(
                transaction_date=date(2026, 8, 10),
                posting_date=date(2026, 8, 11),
                amount_minor=15000,
                currency="CNY",
                raw_description="new-tx",
            ),
        ),
    )

    with client.app.state.session_factory() as session:
        res_old = import_parsed_source(session, source_old, document=doc_old, parser=parser)
        statement_id = res_old.statement.id
        old_import_id = res_old.import_id

        with pytest.raises(ImportConflict) as exc_info:
            import_parsed_source(session, source_new, document=doc_new, parser=parser)
        token = exc_info.value.detail["confirmation_token"]

        res_new = import_parsed_source(
            session, source_new, document=doc_new, parser=parser, confirmation_token=token
        )
        assert res_new.corrected is True
        new_import_id = res_new.import_id
        assert new_import_id != old_import_id

        stmt = session.get(Statement, statement_id)
        assert stmt.source_import_id == new_import_id
        assert stmt.total_due_minor == 15000

        with pytest.raises(ImportConflict) as exc_replay:
            import_parsed_source(session, source_old, document=doc_old, parser=parser)
        assert exc_replay.value.detail["code"] == "IMPORT_CONFIRMATION_REQUIRED"
        assert exc_replay.value.detail["differences"][0]["new"]["total_due_minor"] == 10000


def test_old_erroneous_transactions_cleared_on_confirmation(client):
    source = _make_source("sha-tx-disappear")
    parser = SimpleNamespace(version="1.0.0")

    doc_old = _make_document(
        total_due_minor=15000,
        transactions=(
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "correct"),
            ParsedTransaction(date(2026, 8, 12), date(2026, 8, 13), 5000, "CNY", "erroneous"),
        ),
    )
    doc_new = _make_document(
        total_due_minor=10000,
        transactions=(
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "correct"),
        ),
    )

    with client.app.state.session_factory() as session:
        res = import_parsed_source(session, source, document=doc_old, parser=parser)
        stmt = session.get(Statement, res.statement.id)
        assert len(stmt.transactions) == 2
        correct_tx_id = [t.id for t in stmt.transactions if t.amount_minor == 10000][0]

        with pytest.raises(ImportConflict) as exc_info:
            import_parsed_source(session, source, document=doc_new, parser=parser)
        token = exc_info.value.detail["confirmation_token"]

        import_parsed_source(session, source, document=doc_new, parser=parser, confirmation_token=token)

        session.expire_all()
        stmt = session.get(Statement, res.statement.id)
        assert len(stmt.transactions) == 1
        assert stmt.transactions[0].id == correct_tx_id
        assert stmt.transactions[0].amount_minor == 10000


def test_legitimate_duplicate_purchases_multiset_matching(client):
    source = _make_source("sha-multiset-dup")
    parser = SimpleNamespace(version="1.0.0")

    doc_3 = _make_document(
        total_due_minor=30000,
        transactions=tuple(
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "subway")
            for _ in range(3)
        ),
    )
    doc_2 = _make_document(
        total_due_minor=20000,
        transactions=tuple(
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "subway")
            for _ in range(2)
        ),
    )

    with client.app.state.session_factory() as session:
        res = import_parsed_source(session, source, document=doc_3, parser=parser)
        old_ids = [t.id for t in session.get(Statement, res.statement.id).transactions]
        assert len(old_ids) == 3

        with pytest.raises(ImportConflict) as exc_info:
            import_parsed_source(session, source, document=doc_2, parser=parser)
        token = exc_info.value.detail["confirmation_token"]

        import_parsed_source(session, source, document=doc_2, parser=parser, confirmation_token=token)

        session.expire_all()
        remaining = session.get(Statement, res.statement.id).transactions
        assert len(remaining) == 2
        assert set(t.id for t in remaining).issubset(set(old_ids))


def test_preserved_manual_classification_repayment_and_refund_links(client):
    source = _make_source("sha-preserve-all")
    parser = SimpleNamespace(version="1.0.0")

    doc1 = _make_document(
        total_due_minor=10000,
        transactions=(
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "orig-tx"),
            ParsedTransaction(date(2026, 8, 12), date(2026, 8, 13), -2000, "CNY", "refund-tx"),
        ),
    )
    doc2 = _make_document(
        total_due_minor=12000,
        due_date=date(2026, 9, 25),
        transactions=(
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "orig-tx"),
            ParsedTransaction(date(2026, 8, 12), date(2026, 8, 13), -2000, "CNY", "refund-tx"),
            ParsedTransaction(date(2026, 8, 15), date(2026, 8, 16), 2000, "CNY", "new-tx"),
        ),
    )

    with client.app.state.session_factory() as session:
        res = import_parsed_source(session, source, document=doc1, parser=parser)
        stmt = session.get(Statement, res.statement.id)
        purchase_tx = [t for t in stmt.transactions if t.amount_minor == 10000][0]
        refund_tx = [t for t in stmt.transactions if t.amount_minor == -2000][0]

        purchase_tx.classification_source = ClassificationSource.MANUAL.value
        purchase_tx.transaction_type = TransactionType.REWARD.value
        refund_tx.original_transaction_id = purchase_tx.id

        repayment = Repayment(
            statement_id=stmt.id,
            amount_minor=5000,
            idempotency_key="idemp-key-1",
        )
        session.add(repayment)
        session.commit()

        with pytest.raises(ImportConflict) as exc_info:
            import_parsed_source(session, source, document=doc2, parser=parser)
        token = exc_info.value.detail["confirmation_token"]

        import_parsed_source(session, source, document=doc2, parser=parser, confirmation_token=token)

        stmt = session.get(Statement, res.statement.id)
        assert stmt.due_date == date(2026, 9, 25)
        repayments = session.scalars(select(Repayment).where(Repayment.statement_id == stmt.id)).all()
        assert len(repayments) == 1
        assert repayments[0].amount_minor == 5000

        updated_purchase = session.get(Transaction, purchase_tx.id)
        assert updated_purchase.classification_source == ClassificationSource.MANUAL.value
        assert updated_purchase.transaction_type == TransactionType.REWARD.value

        updated_refund = session.get(Transaction, refund_tx.id)
        assert updated_refund.original_transaction_id == purchase_tx.id


@pytest.mark.parametrize("protection", ["manual", "merchant", "channel", "category", "external_ref", "refund_target"])
def test_removal_of_protected_transaction_is_blocked(client, protection):
    source = _make_source("sha-protected-removal")
    parser = SimpleNamespace(version="1.0.0")

    doc_old = _make_document(
        total_due_minor=10000,
        transactions=(
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "target-tx"),
        ),
    )
    doc_new = _make_document(
        total_due_minor=5000,
        transactions=(ParsedTransaction(date(2026, 8, 12), date(2026, 8, 13),
                                        5000, "CNY", "replacement"),),
    )

    with client.app.state.session_factory() as session:
        res = import_parsed_source(session, source, document=doc_old, parser=parser)
        stmt = session.get(Statement, res.statement.id)
        tx = stmt.transactions[0]

        if protection == "manual":
            tx.classification_source = ClassificationSource.MANUAL.value
        elif protection == "refund_target":
            session.add(Transaction(statement_id=stmt.id, amount_minor=-1000, currency="CNY",
                                    raw_description="linked refund", classification_source="rule",
                                    original_transaction_id=tx.id))
        else:
            setattr(tx, protection, "manual-value")
        session.commit()

        with pytest.raises(ImportConflict) as exc_info:
            import_parsed_source(session, source, document=doc_new, parser=parser)
        assert exc_info.value.detail["code"] == "IMPORT_CORRECTION_BLOCKED"
        diff = exc_info.value.detail["differences"][0]
        assert diff["blocked_reason"] is not None



def test_duplicate_group_reduction_with_protected_row_is_blocked(client):
    source = _make_source("sha-dup-group-protect")
    parser = SimpleNamespace(version="1.0.0")

    doc_2 = _make_document(
        total_due_minor=20000,
        transactions=(
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "dup-tx"),
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "dup-tx"),
        ),
    )
    doc_1 = _make_document(
        total_due_minor=10000,
        transactions=(
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "dup-tx"),
        ),
    )

    with client.app.state.session_factory() as session:
        res = import_parsed_source(session, source, document=doc_2, parser=parser)
        stmt = session.get(Statement, res.statement.id)
        stmt.transactions[0].category = "food"
        session.commit()

        with pytest.raises(ImportConflict) as exc_info:
            import_parsed_source(session, source, document=doc_1, parser=parser)
        assert exc_info.value.detail["code"] == "IMPORT_CORRECTION_BLOCKED"
        diff = exc_info.value.detail["differences"][0]
        assert diff["blocked_reason"] is not None


def test_correction_total_due_below_paid_minor_is_blocked(client):
    source = _make_source("sha-total-due-below-paid")
    parser = SimpleNamespace(version="1.0.0")

    doc_old = _make_document(total_due_minor=20000)
    doc_new = _make_document(total_due_minor=5000)

    with client.app.state.session_factory() as session:
        res = import_parsed_source(session, source, document=doc_old, parser=parser)
        stmt = session.get(Statement, res.statement.id)
        session.add(
            Repayment(statement_id=stmt.id, amount_minor=10000, idempotency_key="pay-1")
        )
        session.commit()

        with pytest.raises(ImportConflict) as exc_info:
            import_parsed_source(session, source, document=doc_new, parser=parser)
        assert exc_info.value.detail["code"] == "IMPORT_CORRECTION_BLOCKED"
        diff = exc_info.value.detail["differences"][0]
        assert diff["blocked_reason"] is not None


def test_transactions_partial_attempting_to_remove_transactions_is_blocked(client):
    source = _make_source("sha-partial-remove-block")
    parser = SimpleNamespace(version="1.0.0")

    doc_old = _make_document(
        total_due_minor=10000,
        transactions=(
            ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "real-tx"),
        ),
    )
    doc_partial = _make_document(
        total_due_minor=10000,
        transactions=(),
        warnings=("warning: partial transactions",),
    )

    with client.app.state.session_factory() as session:
        res = import_parsed_source(session, source, document=doc_old, parser=parser)

        with pytest.raises(ImportConflict) as exc_info:
            import_parsed_source(session, source, document=doc_partial, parser=parser)
        assert exc_info.value.detail["code"] == "IMPORT_CORRECTION_BLOCKED"
        diff = exc_info.value.detail["differences"][0]
        assert diff["blocked_reason"] is not None


@pytest.mark.parametrize("change", ["repayment", "manual_edit"])
def test_stale_confirmation_token_concurrent_repayment_or_edit(client, change):
    source = _make_source("sha-stale-test")
    parser = SimpleNamespace(version="1.0.0")

    tx = ParsedTransaction(date(2026, 8, 10), date(2026, 8, 11), 10000, "CNY", "unchanged")
    doc_old = _make_document(total_due_minor=10000, transactions=(tx,))
    doc_new = _make_document(total_due_minor=15000, transactions=(tx,))

    with client.app.state.session_factory() as session:
        res = import_parsed_source(session, source, document=doc_old, parser=parser)
        stmt_id = res.statement.id

        with pytest.raises(ImportConflict) as exc_info:
            import_parsed_source(session, source, document=doc_new, parser=parser)
        token = exc_info.value.detail["confirmation_token"]

        if change == "repayment":
            session.add(Repayment(statement_id=stmt_id, amount_minor=2000, idempotency_key="stale-pay"))
        else:
            session.get(Statement, stmt_id).transactions[0].category = "manual category"
        session.commit()

        with pytest.raises(ImportConflict) as exc_stale:
            import_parsed_source(session, source, document=doc_new, parser=parser, confirmation_token=token)
        assert exc_stale.value.detail["code"] == "IMPORT_CONFIRMATION_STALE"


def test_bare_pdf_reparse_uses_pdf_parser_does_not_fallback_to_eml(client, monkeypatch):
    from app.services.eml_import import import_pdf_bytes

    current = {"amount": 10000}

    def parse_pdf(_self, _data):
        return ParsedStatement("BOC", "1234", date(2026, 9, 1), date(2026, 9, 20),
                               current["amount"], 1000, "CNY")

    def not_citic(*_args):
        raise ParserError("not CITIC")

    def no_eml(*_args):
        raise AssertionError("bare PDF must not use EML registry")

    monkeypatch.setattr("app.parsers.boc.BocStatementParser.parse_pdf_bytes", parse_pdf)
    monkeypatch.setattr("app.parsers.citic.CiticStatementParser.parse_pdf_bytes", not_citic)
    monkeypatch.setattr("app.services.eml_import.default_registry.parser_for", no_eml)
    with client.app.state.session_factory() as session:
        first = import_pdf_bytes(session, b"synthetic pdf")
        current["amount"] = 15000
        with pytest.raises(ImportConflict) as conflict:
            import_pdf_bytes(session, b"synthetic pdf")
        assert conflict.value.detail["code"] == "IMPORT_CONFIRMATION_REQUIRED"
        corrected = import_pdf_bytes(session, b"synthetic pdf",
                                     confirmation_token=conflict.value.detail["confirmation_token"])
        assert corrected.corrected is True
        assert corrected.statement.id == first.statement.id
        assert corrected.statement.total_due_minor == 15000
