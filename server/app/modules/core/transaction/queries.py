from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.statement import Statement
from app.models.transaction import Transaction


def list_transactions(
    session: Session,
    *,
    statement_id: str | None = None,
    card_id: str | None = None,
) -> list[Transaction]:
    """Return transaction details using the module-owned filter rules."""
    query = select(Transaction).join(Transaction.statement)
    if statement_id:
        query = query.where(Transaction.statement_id == statement_id)
    if card_id:
        query = query.where(Transaction.card_id == card_id)
    query = query.order_by(Transaction.posting_date.desc(), Transaction.id)
    return list(session.scalars(query))
