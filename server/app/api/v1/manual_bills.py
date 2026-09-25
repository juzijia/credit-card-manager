from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import require_agent_access
from app.db.session import get_session
from app.models.manual_bill import ManualBill, ManualBillPayment
from app.models.mortgage import MortgagePlan
from app.schemas.manual_bill import ManualBillCreate, ManualBillDue, ManualBillPaid, ManualBillRead, ManualBillUpdate
from app.schemas.repayment import MarkPaidCreate
from app.services.manual_bills import ensure_current_payments, list_current_payments, mark_paid, unmark_paid
from app.services.statements import local_today

router = APIRouter(prefix="/manual-bills", tags=["manual-bills"])
SessionDep = Annotated[Session, Depends(get_session)]


def manual_bill_due(
    bill: ManualBill, payment: ManualBillPayment, today: date
) -> ManualBillDue:
    return ManualBillDue(
        payment_id=payment.id,
        bill_id=bill.id,
        bill_type=bill.bill_type,
        name=bill.name,
        amount_minor=payment.amount_minor,
        due_date=payment.due_date,
        days_left=(payment.due_date - today).days,
        reminder_enabled=bill.reminder_enabled,
    )


@router.post(
    "",
    response_model=ManualBillRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agent_access)],
)
def create_manual_bill(payload: ManualBillCreate, session: SessionDep) -> ManualBill:
    bill = ManualBill(**payload.model_dump())
    session.add(bill)
    session.flush()
    ensure_current_payments(session, local_today(session.info["timezone"]))
    session.commit()
    session.refresh(bill)
    return bill


@router.get(
    "",
    response_model=list[ManualBillRead],
    dependencies=[Depends(require_agent_access)],
)
def list_manual_bills(session: SessionDep) -> list[ManualBill]:
    return list(session.scalars(select(ManualBill).order_by(ManualBill.created_at)))


@router.get(
    "/current",
    response_model=list[ManualBillDue],
    dependencies=[Depends(require_agent_access)],
)
def list_current_due_manual_bills(session: SessionDep) -> list[ManualBillDue]:
    today = local_today(session.info["timezone"])
    pairs = list_current_payments(session, today)
    return [
        manual_bill_due(bill, payment, today)
        for bill, payment in pairs
        if payment.paid_at is None
    ]


import re
from fastapi import Query

_PERIOD_REGEX = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")


@router.get(
    "/marked-paid",
    response_model=list[ManualBillPaid],
    dependencies=[Depends(require_agent_access)],
)
def list_current_marked_paid_manual_bills(
    session: SessionDep,
    period: Annotated[str | None, Query(description="Target period YYYY-MM")] = None,
) -> list[ManualBillPaid]:
    if period is not None and not _PERIOD_REGEX.fullmatch(period):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid period format, expected YYYY-MM",
        )
    today = local_today(session.info["timezone"])
    pairs = list_current_payments(session, today, period=period)
    return [
        ManualBillPaid(
            payment_id=payment.id,
            bill_id=bill.id,
            bill_type=bill.bill_type,
            name=bill.name,
            amount_minor=payment.amount_minor,
            paid_at=payment.paid_at,
            repayment_channel=payment.repayment_channel,
        )
        for bill, payment in pairs
        if payment.paid_at is not None
    ]


@router.post(
    "/generate-current-payments",
    response_model=list[ManualBillDue],
    dependencies=[Depends(require_agent_access)],
)
def generate_current_manual_bill_payments(session: SessionDep) -> list[ManualBillDue]:
    """Explicitly materialize this month's recurring bill payments."""
    today = local_today(session.info["timezone"])
    pairs = ensure_current_payments(session, today)
    session.commit()
    return [
        manual_bill_due(bill, payment, today)
        for bill, payment in pairs
        if payment.paid_at is None
    ]


def _current_payment_or_404(session: Session, bill_id: str) -> ManualBillPayment:
    today = local_today(session.info["timezone"])
    for bill, payment in ensure_current_payments(session, today):
        if bill.id == bill_id:
            return payment
    raise HTTPException(status_code=404, detail="Manual bill not found")


@router.post(
    "/{bill_id}/mark-paid",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_agent_access)],
)
def mark_manual_bill_paid(
    bill_id: str, session: SessionDep, payload: MarkPaidCreate | None = None
) -> None:
    payment = _current_payment_or_404(session, bill_id)
    mark_paid(payment, payload.repayment_channel if payload else None)
    session.commit()


@router.post(
    "/{bill_id}/unmark-paid",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_agent_access)],
)
def unmark_manual_bill_paid(bill_id: str, session: SessionDep) -> None:
    payment = _current_payment_or_404(session, bill_id)
    unmark_paid(payment)
    session.commit()


@router.put(
    "/{bill_id}",
    response_model=ManualBillRead,
    dependencies=[Depends(require_agent_access)],
)
def update_manual_bill(
    bill_id: str, payload: ManualBillUpdate, session: SessionDep
) -> ManualBill:
    bill = session.get(ManualBill, bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Manual bill not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, val in update_data.items():
        setattr(bill, field, val)

    today = local_today(session.info["timezone"])
    period = today.strftime("%Y-%m")
    current_payment = session.scalar(
        select(ManualBillPayment).where(
            ManualBillPayment.manual_bill_id == bill.id,
            ManualBillPayment.period == period,
            ManualBillPayment.paid_at.is_(None),
        )
    )
    if current_payment:
        if "monthly_amount_minor" in update_data:
            current_payment.amount_minor = bill.monthly_amount_minor
        if "due_day" in update_data:
            from calendar import monthrange
            max_day = monthrange(today.year, today.month)[1]
            current_payment.due_date = date(today.year, today.month, min(bill.due_day, max_day))

    session.commit()
    session.refresh(bill)
    return bill


@router.delete(
    "/{bill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_agent_access)],
)
def delete_manual_bill(bill_id: str, session: SessionDep) -> None:
    bill = session.get(ManualBill, bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Manual bill not found")

    if bill.source_type == "mortgage_plan" and bill.source_id:
        plan = session.get(MortgagePlan, bill.source_id)
        if plan:
            session.delete(plan)

    session.delete(bill)
    session.commit()
