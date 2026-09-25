from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import require_agent_access
from app.db.session import get_session
from app.models.manual_bill import ManualBill
from app.models.mortgage import MortgagePlan
from app.schemas.mortgage import MortgagePlanCreate, MortgagePlanRead, MortgageSaveResponse
from app.services.manual_bills import ensure_current_payments
from app.services.mortgage import generate_plan_schedule, get_plan_period_for_date
from app.services.statements import local_today

router = APIRouter(prefix="/mortgage", tags=["mortgage"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.post(
    "/save-bill",
    response_model=MortgageSaveResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agent_access)],
)
def save_mortgage_bill(payload: MortgagePlanCreate, session: SessionDep) -> MortgageSaveResponse:
    # 1. Create MortgagePlan entity
    plan = MortgagePlan(**payload.model_dump())
    session.add(plan)
    session.flush()

    # 2. Calculate initial monthly amount and due day from schedule
    today = local_today(session.info.get("timezone", "Asia/Shanghai"))
    sched_item = get_plan_period_for_date(plan, today)
    if not sched_item:
        full_sched = generate_plan_schedule(plan)
        target_p = getattr(plan, "start_period", 1) or 1
        sched_item = next((item for item in full_sched if item.period == target_p), full_sched[0] if full_sched else None)

    amount_minor = sched_item.payment_minor if sched_item else 10000
    due_day = plan.first_payment_date.day
    statement_day = 1

    # 3. Create or link associated ManualBill (source_type="mortgage_plan", source_id=plan.id)
    bill = ManualBill(
        bill_type="房贷",
        name=plan.name,
        statement_day=statement_day,
        due_day=due_day,
        monthly_amount_minor=amount_minor,
        reminder_enabled=plan.reminder_enabled,
        enabled=True,
        source_type="mortgage_plan",
        source_id=plan.id,
    )
    session.add(bill)
    session.flush()

    # Link plan back to bill
    plan.manual_bill_id = bill.id

    # Materialize payment for the current period so it appears in dashboard/manual_due immediately
    ensure_current_payments(session, today)

    session.commit()
    session.refresh(plan)

    return MortgageSaveResponse(
        plan=MortgagePlanRead.model_validate(plan),
        manual_bill_id=bill.id,
    )


@router.get(
    "/plans",
    response_model=list[MortgagePlanRead],
    dependencies=[Depends(require_agent_access)],
)
def list_mortgage_plans(session: SessionDep) -> list[MortgagePlanRead]:
    plans = session.scalars(select(MortgagePlan).order_by(MortgagePlan.created_at.desc())).all()
    return [MortgagePlanRead.model_validate(p) for p in plans]

