from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Iterable

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.card import Card
from app.models.repayment import Repayment
from app.models.statement import ParseStatus, Statement
from app.models.transaction import Transaction
from app.modules.core.transaction import TransactionType, get_effective_transaction_type
from app.services.statements import is_active_due_predicate


class CoverageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CoverageSnapshot:
    status: CoverageStatus
    complete_statement_count: int
    partial_statement_count: int
    unknown_transaction_count: int
    undated_transaction_count: int
    latest_statement_date: date | None


@dataclass(frozen=True, slots=True)
class StatementCoverageSnapshot:
    status: CoverageStatus
    unknown_transaction_count: int
    undated_transaction_count: int


def count_unknown_transactions(transactions: Iterable[Transaction]) -> int:
    return sum(
        get_effective_transaction_type(transaction) == TransactionType.UNKNOWN
        for transaction in transactions
    )


def count_undated_transactions(transactions: Iterable[Transaction]) -> int:
    return sum(
        transaction.transaction_date is None and transaction.posting_date is None
        for transaction in transactions
    )


def evaluate_statement_coverage(
    statement: Statement,
    transactions: Iterable[Transaction],
) -> StatementCoverageSnapshot:
    tx_list = list(transactions)
    unknown_count = count_unknown_transactions(tx_list)
    undated_count = count_undated_transactions(tx_list)

    if statement.parse_status == ParseStatus.FAILED:
        status = CoverageStatus.MISSING
    elif statement.parse_status in (ParseStatus.SUMMARY_OK, ParseStatus.TRANSACTIONS_PARTIAL):
        status = CoverageStatus.PARTIAL
    elif unknown_count > 0 or undated_count > 0:
        status = CoverageStatus.PARTIAL
    elif statement.parse_status == ParseStatus.COMPLETE:
        status = CoverageStatus.COMPLETE
    else:
        status = CoverageStatus.UNKNOWN

    return StatementCoverageSnapshot(
        status=status,
        unknown_transaction_count=unknown_count,
        undated_transaction_count=undated_count,
    )


def ratio_percent(numerator: int, denominator: int | None) -> float | None:
    if not denominator or denominator <= 0:
        return None
    return round(numerator / denominator * 100, 2)


def list_visible_cards(session: Session, *, currency: str = "CNY") -> list[Card]:
    return list(
        session.scalars(
            select(Card)
            .where(
                Card.currency == currency,
            )
            .order_by(Card.bank_name, Card.display_name, Card.last4)
        )
    )


def list_active_statements(
    session: Session,
    *,
    cutoff: date,
    currency: str | None = None,
) -> list[Statement]:
    """Canonical active/historical scope for outstanding statement analysis."""
    filters = [
        or_(
            Statement.card_id.is_(None),
            and_(
                Card.deleted_at.is_(None),
                Card.status == "active",
                Card.hidden.is_(False),
            ),
        ),
        is_active_due_predicate(cutoff),
    ]
    if currency is not None:
        filters.append(Statement.currency == currency)

    return list(
        session.scalars(
            select(Statement)
            .outerjoin(Statement.card)
            .where(*filters)
            .options(selectinload(Statement.repayments), selectinload(Statement.card))
            .order_by(Statement.due_date, Statement.id)
        )
    )


def list_visible_registered_repayments(session: Session) -> list[Repayment]:
    """Registered repayments for visible/enabled credit cards only."""
    return list(
        session.scalars(
            select(Repayment)
            .join(Repayment.statement)
            .outerjoin(Statement.card)
            .where(
                Statement.currency == "CNY",
                or_(
                    Statement.card_id.is_(None),
                    and_(
                        Card.deleted_at.is_(None),
                        Card.status == "active",
                        Card.hidden.is_(False),
                    ),
                ),
            )
            .order_by(Repayment.paid_at, Repayment.id)
        )
    )


def list_calendar_statements(
    session: Session,
    *,
    start_date: date,
    end_date: date,
    currency: str = "CNY",
) -> list[Statement]:
    """Issued statements whose statement date falls in a calendar analysis window."""
    return list(
        session.scalars(
            select(Statement)
            .outerjoin(Statement.card)
            .where(
                Statement.currency == currency,
                Statement.statement_date.between(start_date, end_date),
                or_(
                    Statement.card_id.is_(None),
                    and_(
                        Card.deleted_at.is_(None),
                        Card.status == "active",
                        Card.hidden.is_(False),
                    ),
                ),
            )
            .options(selectinload(Statement.card))
            .order_by(Statement.statement_date, Statement.id)
        )
    )


def list_calendar_transactions(
    session: Session,
    *,
    start_date: date,
    end_date: date,
    currency: str = "CNY",
) -> list[Transaction]:
    """Transactions for calendar analysis using transaction_date then posting_date fallback."""
    return list(
        session.scalars(
            select(Transaction)
            .join(Transaction.statement)
            .outerjoin(Statement.card)
            .where(
                Transaction.currency == currency,
                or_(
                    Transaction.transaction_date.between(start_date, end_date),
                    and_(
                        Transaction.transaction_date.is_(None),
                        Transaction.posting_date.between(start_date, end_date),
                    ),
                ),
                or_(
                    Statement.card_id.is_(None),
                    and_(
                        Card.deleted_at.is_(None),
                        Card.status == "active",
                        Card.hidden.is_(False),
                    ),
                ),
            )
            .options(
                selectinload(Transaction.statement).selectinload(Statement.card)
            )
        )
    )


def list_statement_window_transactions(
    session: Session,
    *,
    start_date: date,
    end_date: date,
    currency: str = "CNY",
) -> list[Transaction]:
    """All transactions attached to issued statements in a statement-date window.

    Unlike calendar transaction queries, this intentionally includes rows with no
    transaction/posting date so completeness checks can report them instead of
    silently treating them as absent.
    """
    return list(
        session.scalars(
            select(Transaction)
            .join(Transaction.statement)
            .outerjoin(Statement.card)
            .where(
                Transaction.currency == currency,
                Statement.statement_date.between(start_date, end_date),
                or_(
                    Statement.card_id.is_(None),
                    and_(
                        Card.deleted_at.is_(None),
                        Card.status == "active",
                        Card.hidden.is_(False),
                    ),
                ),
            )
            .options(
                selectinload(Transaction.statement).selectinload(Statement.card)
            )
        )
    )


def evaluate_calendar_coverage(
    *,
    cards: list[Card],
    statements: list[Statement],
    transactions: list[Transaction],
    expected_months: set[str],
) -> CoverageSnapshot:
    relevant_statements = [
        statement
        for statement in statements
        if statement.statement_date.strftime("%Y-%m") in expected_months
    ]

    complete_statement_count = sum(
        statement.parse_status == ParseStatus.COMPLETE
        for statement in relevant_statements
    )
    partial_statement_count = len(relevant_statements) - complete_statement_count
    unknown_transaction_count = count_unknown_transactions(transactions)
    undated_transaction_count = count_undated_transactions(transactions)
    latest_statement_date = max(
        (statement.statement_date for statement in relevant_statements),
        default=None,
    )

    if not cards:
        status = CoverageStatus.UNKNOWN
    elif not relevant_statements:
        status = CoverageStatus.MISSING
    elif partial_statement_count or unknown_transaction_count or undated_transaction_count:
        status = CoverageStatus.PARTIAL
    else:
        # A COMPLETE statement means its own parsed detail is complete, but it does
        # not prove complete coverage of a calendar month because billing cycles
        # and calendar months do not align. Until CCM persists explicit source
        # coverage evidence, the calendar query must not overclaim completeness.
        status = CoverageStatus.UNKNOWN

    return CoverageSnapshot(
        status=status,
        complete_statement_count=complete_statement_count,
        partial_statement_count=partial_statement_count,
        unknown_transaction_count=unknown_transaction_count,
        undated_transaction_count=undated_transaction_count,
        latest_statement_date=latest_statement_date,
    )
