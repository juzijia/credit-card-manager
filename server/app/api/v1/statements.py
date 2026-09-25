from datetime import date, datetime, time, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.security import require_agent_access
from app.db.session import get_session
from app.models.card import Card
from app.models.mixins import utc_now
from app.models.repayment import Repayment
from app.models.statement import Statement
from app.modules.core.transaction.queries import list_transactions
from app.schemas.repayment import MarkPaidCreate, RepaymentCreate, RepaymentRead
from app.schemas.statement import (
    StatementAnalysisCoverageRead,
    StatementAnalysisMetricsRead,
    StatementAnalysisRead,
    StatementCreate,
    StatementRead,
)
from app.services.financial_analysis.metrics import calculate_transaction_metrics
from app.services.financial_analysis.queries import evaluate_statement_coverage
from app.services.statements import local_today, remaining_minor, statement_read

router = APIRouter(prefix="/statements", tags=["statements"])
SessionDep = Annotated[Session, Depends(get_session)]


def load_statement(session: Session, statement_id: str) -> Statement:
    statement = session.scalar(
        select(Statement)
        .options(selectinload(Statement.repayments), selectinload(Statement.card))
        .where(Statement.id == statement_id)
    )
    if not statement:
        raise HTTPException(status_code=404, detail="Statement not found")
    return statement


@router.post(
    "",
    response_model=StatementRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agent_access)],
)
def create_statement(payload: StatementCreate, session: SessionDep) -> StatementRead:
    card = session.get(Card, payload.card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if payload.bank_code is not None and payload.bank_code != card.bank_code:
        raise HTTPException(
            status_code=422,
            detail="bank_code does not match card.bank_code",
        )
    # Card.currency is legacy / non-authoritative; Statement.currency is the
    # payable currency. A card may hold statements in different currencies
    # (unique on card_id + statement_date + currency).

    data = payload.model_dump()
    data["bank_code"] = card.bank_code
    statement = Statement(**data)
    session.add(statement)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A statement already exists for this card, date, and currency",
        ) from exc
    statement = load_statement(session, statement.id)
    return statement_read(statement, local_today(session.info["timezone"]))


@router.get(
    "",
    response_model=list[StatementRead],
    dependencies=[Depends(require_agent_access)],
)
def list_statements(session: SessionDep) -> list[StatementRead]:
    statements = session.scalars(
        select(Statement)
        .options(selectinload(Statement.repayments), selectinload(Statement.card))
        .order_by(Statement.statement_date.desc())
    ).all()
    today = local_today(session.info["timezone"])
    return [statement_read(statement, today) for statement in statements]


@router.get("/marked-paid", dependencies=[Depends(require_agent_access)])
def list_current_month_marked_paid(session: SessionDep) -> list[dict[str, object]]:
    """列出本地当月由“标记已还清”创建、仍可人工撤销的记录。"""
    today = local_today(session.info["timezone"])
    month_start = date(today.year, today.month, 1)
    next_month = (
        date(today.year + 1, 1, 1)
        if today.month == 12
        else date(today.year, today.month + 1, 1)
    )
    local_zone = ZoneInfo(session.info["timezone"])
    month_start_utc = (
        datetime.combine(month_start, time.min, tzinfo=local_zone)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    next_month_utc = (
        datetime.combine(next_month, time.min, tzinfo=local_zone)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    rows = session.execute(
        select(Repayment, Statement, Card)
        .join(Statement, Repayment.statement_id == Statement.id)
        .outerjoin(Card, Statement.card_id == Card.id)
        .where(
            Repayment.note == "Marked paid in full",
            Repayment.paid_at >= month_start_utc,
            Repayment.paid_at < next_month_utc,
            or_(
                and_(Statement.due_date >= month_start, Statement.due_date < next_month),
                and_(Statement.statement_date >= month_start, Statement.statement_date < next_month),
            ),
        )
        .order_by(Repayment.paid_at.desc())
    ).all()
    return [
        {
            "repayment_id": repayment.id,
            "statement_id": statement.id,
            "display_name": card.display_name if card else None,
            "last4": card.last4 if card else None,
            "amount_minor": repayment.amount_minor,
            "paid_at": repayment.paid_at.isoformat(),
            "repayment_channel": repayment.repayment_channel,
            "statement_date": statement.statement_date.isoformat(),
            "due_date": statement.due_date.isoformat(),
        }
        for repayment, statement, card in rows
    ]


@router.get(
    "/{statement_id}",
    response_model=StatementRead,
    dependencies=[Depends(require_agent_access)],
)
def get_statement(statement_id: str, session: SessionDep) -> StatementRead:
    statement = load_statement(session, statement_id)
    return statement_read(statement, local_today(session.info["timezone"]))


@router.get(
    "/{statement_id}/analysis",
    response_model=StatementAnalysisRead,
    dependencies=[Depends(require_agent_access)],
)
def get_statement_analysis(
    statement_id: str, session: SessionDep
) -> StatementAnalysisRead:
    statement = load_statement(session, statement_id)
    transactions = list_transactions(session, statement_id=statement.id)
    metrics = calculate_transaction_metrics(transactions)
    coverage = evaluate_statement_coverage(statement, transactions)
    return StatementAnalysisRead(
        statement_id=statement.id,
        metrics=StatementAnalysisMetricsRead.model_validate(metrics),
        coverage=StatementAnalysisCoverageRead(
            status=coverage.status.value,
            unknown_transaction_count=coverage.unknown_transaction_count,
            undated_transaction_count=coverage.undated_transaction_count,
        ),
    )


@router.post(
    "/{statement_id}/repayments",
    response_model=RepaymentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agent_access)],
)
def create_repayment(
    statement_id: str, payload: RepaymentCreate, session: SessionDep
) -> Repayment:
    session.execute(text("BEGIN IMMEDIATE"))
    statement = load_statement(session, statement_id)
    existing = session.scalar(
        select(Repayment).where(
            Repayment.statement_id == statement_id,
            Repayment.idempotency_key == payload.idempotency_key,
        )
    )
    if existing:
        return existing

    amount_remaining = remaining_minor(statement)
    if payload.amount_minor > amount_remaining:
        raise HTTPException(
            status_code=422,
            detail="Repayment cannot exceed the current remaining amount",
        )

    paid_at = payload.paid_at
    if paid_at is not None:
        paid_at = paid_at.astimezone(timezone.utc).replace(tzinfo=None)
    repayment = Repayment(
        statement_id=statement_id,
        amount_minor=payload.amount_minor,
        paid_at=paid_at or utc_now(),
        note=payload.note,
        repayment_channel=payload.repayment_channel,
        idempotency_key=payload.idempotency_key,
    )
    session.add(repayment)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(Repayment).where(
                Repayment.statement_id == statement_id,
                Repayment.idempotency_key == payload.idempotency_key,
            )
        )
        if existing:
            return existing
        raise
    session.refresh(repayment)
    return repayment


@router.post(
    "/{statement_id}/mark-paid",
    response_model=RepaymentRead | None,
    dependencies=[Depends(require_agent_access)],
)
def mark_statement_paid(
    statement_id: str,
    session: SessionDep,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],
    payload: MarkPaidCreate | None = None,
) -> Repayment | None:
    session.execute(text("BEGIN IMMEDIATE"))
    statement = load_statement(session, statement_id)
    existing = session.scalar(
        select(Repayment).where(
            Repayment.statement_id == statement_id,
            Repayment.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing

    amount_remaining = remaining_minor(statement)
    if amount_remaining == 0:
        return None

    repayment = Repayment(
        statement_id=statement_id,
        amount_minor=amount_remaining,
        paid_at=utc_now(),
        note="Marked paid in full",
        repayment_channel=payload.repayment_channel if payload else None,
        idempotency_key=idempotency_key,
    )
    session.add(repayment)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(Repayment).where(
                Repayment.statement_id == statement_id,
                Repayment.idempotency_key == idempotency_key,
            )
        )
        if existing:
            return existing
        raise
    session.refresh(repayment)
    return repayment


@router.delete(
    "/{statement_id}/repayments/{repayment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_agent_access)],
)
def undo_mark_statement_paid(
    statement_id: str, repayment_id: str, session: SessionDep
) -> None:
    """撤销界面“还清”创建的整笔还款，不影响用户录入的普通还款。"""
    repayment = session.scalar(
        select(Repayment).where(
            Repayment.id == repayment_id, Repayment.statement_id == statement_id
        )
    )
    if not repayment:
        raise HTTPException(status_code=404, detail="Repayment not found")
    if repayment.note != "Marked paid in full":
        raise HTTPException(
            status_code=422, detail="Only a mark-paid repayment can be undone"
        )
    session.delete(repayment)
    session.commit()
