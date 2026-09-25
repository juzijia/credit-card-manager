from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import or_

from app.models.statement import Statement
from app.schemas.statement import StatementRead


def is_active_due_predicate(cutoff: date):
    """SQL filter predicate for active (non-historical) statements."""
    return or_(Statement.due_date.is_(None), Statement.due_date >= cutoff)


def paid_minor(statement: Statement) -> int:
    return sum(repayment.amount_minor for repayment in statement.repayments)


def remaining_minor(statement: Statement) -> int:
    return max(statement.total_due_minor - paid_minor(statement), 0)


def status_for(statement: Statement, today: date) -> str:
    if remaining_minor(statement) == 0:
        return "paid"
    if today <= statement.due_date:
        return "pending"
    return "overdue_hint"


def local_today(timezone_name: str) -> date:
    from datetime import datetime

    return datetime.now(ZoneInfo(timezone_name)).date()


def statement_read(statement: Statement, today: date) -> StatementRead:
    paid = paid_minor(statement)
    card = getattr(statement, "card", None)
    return StatementRead(
        id=statement.id,
        bank_code=statement.bank_code,
        card_id=statement.card_id,
        source_import_id=statement.source_import_id,
        statement_date=statement.statement_date,
        due_date=statement.due_date,
        total_due_minor=statement.total_due_minor,
        minimum_due_minor=statement.minimum_due_minor,
        currency=statement.currency,
        parse_status=statement.parse_status,
        paid_minor=paid,
        remaining_minor=max(statement.total_due_minor - paid, 0),
        status=status_for(statement, today),
        created_at=statement.created_at,
        updated_at=statement.updated_at,
        card_bank_name=card.bank_name if card else None,
        card_display_name=card.display_name if card else None,
        card_last4=card.last4 if card else None,
        card_product=card.card_product if card else None,
        card_status=card.status if card else None,
        card_is_deleted=bool(card and card.deleted_at is not None),
    )
