from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.manual_bills import manual_bill_due
from app.core.security import require_agent_access
from app.db.session import get_session
from app.models.card import Card
from app.schemas.dashboard import (
    CreditLimitBank,
    DashboardResponse,
    DueCard,
    DueResponse,
    ManualDueResponse,
)
from app.services.financial_analysis.queries import (
    list_active_statements,
    ratio_percent,
)
from app.services.manual_bills import list_current_payments
from app.services.statements import local_today, remaining_minor, status_for
from app.services.system_settings import get_history_cutoff_date

router = APIRouter(tags=["dashboard"])
SessionDep = Annotated[Session, Depends(get_session)]


_BANK_NAMES = {
    "CGB": "广发银行",
    "CMB": "招商银行",
    "PAB": "平安银行",
    "CITIC": "中信银行",
    "ICBC": "中国工商银行",
    "CCB": "中国建设银行",
    "BOCOM": "交通银行",
    "ABC": "中国农业银行",
    "BOC": "中国银行",
}


def _sum_by_currency(items: list[DueCard]) -> dict[str, int]:
    """Per-currency totals; cross-currency arithmetic is prohibited."""
    totals: dict[str, int] = {}
    for item in items:
        totals[item.currency] = totals.get(item.currency, 0) + item.remaining_minor
    return totals


def build_due_response(session: Session) -> DueResponse:
    cutoff = get_history_cutoff_date(session)
    statements = list_active_statements(session, cutoff=cutoff)
    today = local_today(session.info["timezone"])
    cards: list[DueCard] = []
    for statement in statements:
        remaining = remaining_minor(statement)
        if remaining == 0:
            continue
        bank_name = (
            statement.card.bank_name
            if statement.card
            else _BANK_NAMES.get(statement.bank_code, statement.bank_code)
        )
        cards.append(
            DueCard(
                statement_id=statement.id,
                card_id=statement.card_id,
                bank=bank_name,
                display_name=statement.card.display_name if statement.card else None,
                last4=statement.card.last4 if statement.card else None,
                remaining_minor=remaining,
                due_date=statement.due_date,
                days_left=(statement.due_date - today).days,
                status=status_for(statement, today),
                currency=statement.currency,
                parse_status=statement.parse_status.value,
            )
        )

    currency_totals = _sum_by_currency(cards)
    # Single currency (or none outstanding): keep the aggregate scalar.
    # Mixed currencies: a single total would be cross-currency arithmetic,
    # which is prohibited; report per-currency totals instead.
    if len(currency_totals) <= 1:
        currency = next(iter(currency_totals), "CNY")
        total_remaining_minor: int | None = sum(currency_totals.values())
    else:
        currency = "MIXED"
        total_remaining_minor = None
    return DueResponse(
        total_remaining_minor=total_remaining_minor,
        currency=currency,
        totals_by_currency=currency_totals,
        cards=cards,
    )


def build_manual_due_response(session: Session) -> ManualDueResponse:
    today = local_today(session.info["timezone"])
    pairs = list_current_payments(session, today)
    items = [
        manual_bill_due(bill, payment, today)
        for bill, payment in pairs
        if payment.paid_at is None
    ]
    items.sort(key=lambda item: item.due_date)
    return ManualDueResponse(
        total_remaining_minor=sum(item.amount_minor for item in items), items=items
    )


@router.get(
    "/due", response_model=DueResponse, dependencies=[Depends(require_agent_access)]
)
def get_due(session: SessionDep) -> DueResponse:
    return build_due_response(session)


@router.get(
    "/dashboard",
    response_model=DashboardResponse,
    dependencies=[Depends(require_agent_access)],
)
def get_dashboard(session: SessionDep) -> DashboardResponse:
    due = build_due_response(session)
    manual_due = build_manual_due_response(session)
    cards = session.scalars(
        select(Card).where(
            Card.hidden.is_(False),
            Card.deleted_at.is_(None),
            Card.status == "active",
        )
    ).all()
    # A bank can issue multiple cards against one shared credit line. Count only
    # the largest known line for that bank/currency instead of adding the same
    # line once per card. This remains an explicit CCM estimate, not a bank fact.
    bank_limits: dict[tuple[str, str], CreditLimitBank] = {}
    for card in cards:
        if card.credit_limit_minor is None:
            continue
        key = (card.bank_name, card.currency)
        current = bank_limits.get(key)
        if current is None or card.credit_limit_minor > current.limit_minor:
            bank_limits[key] = CreditLimitBank(
                bank_name=card.bank_name,
                currency=card.currency,
                limit_minor=card.credit_limit_minor,
            )
    credit_limit_banks = sorted(
        bank_limits.values(), key=lambda item: (-item.limit_minor, item.bank_name)
    )
    total_limit = sum(
        item.limit_minor for item in credit_limit_banks if item.currency == "CNY"
    )
    # Utilization must stay CNY-only: the numerator is the CNY share of the
    # outstanding balance; foreign-currency remaining never enters this ratio.
    cny_outstanding = due.totals_by_currency.get("CNY", 0)
    known_utilization = ratio_percent(cny_outstanding, total_limit)
    next_due = min(
        [item.due_date for item in due.cards]
        + [item.due_date for item in manual_due.items],
        default=None,
    )
    return DashboardResponse(
        due=due,
        manual_due=manual_due,
        total_credit_limit_minor=total_limit,
        credit_limit_banks=credit_limit_banks,
        known_utilization_percent=known_utilization,
        next_due_date=next_due,
    )
