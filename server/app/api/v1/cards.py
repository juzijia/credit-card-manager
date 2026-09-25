from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.security import require_agent_access
from app.db.session import get_session
from app.models.card import Card
from app.models.mixins import utc_now
from app.models.notification import NotificationLog, NotificationSetting
from app.models.repayment import Repayment
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.schemas.card import CardCreate, CardDeletionPreview, CardRead, CardUpdate
from app.modules.optional.interest_free import (
    clamped_statement_date,
    estimate_due_date,
    forecast_interest_free_days,
)
from app.schemas.interest_free import FutureInterestFreeEstimateRead, InterestFreeRead
from app.models.import_record import ImportRecord, ImportStatus
from app.services.due_date_rule_resolver import resolve_effective_due_rule
from app.services.bank_rules import (
    get_bank_rules,
    import_bank_rules,
    match_product_rule,
)
from app.services.card_blacklist import (
    add_ignored_card,
    get_ignored_cards,
    remove_ignored_card,
)
from app.services.card_products import apply_product_dictionary
from app.services.statements import local_today

router = APIRouter(prefix="/cards", tags=["cards"])
SessionDep = Annotated[Session, Depends(get_session)]


class BankRulesImport(BaseModel):
    rules: dict[str, Any]
    apply_existing: bool = True
    mode: Literal["merge", "replace"] = "merge"


class CalendarEstimateRead(BaseModel):
    card_id: str
    statement_date: date
    due_date: date


@router.post(
    "",
    response_model=CardRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agent_access)],
)
def create_card(payload: CardCreate, session: SessionDep) -> Card:
    data = payload.model_dump()
    if "is_hidden" in data:
        if "hidden" not in data or not data["hidden"]:
            data["hidden"] = data.pop("is_hidden")
        else:
            data.pop("is_hidden", None)
    card = Card(**data)
    if any(
        value is not None
        for value in (
            card.annual_fee_minor,
            card.annual_fee_policy,
            card.annual_fee_charge_date,
        )
    ):
        card.annual_fee_source = "manual"
    else:
        card.annual_fee_source = None
        apply_product_dictionary(card)
    session.add(card)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A card with this bank, last four digits, and currency already exists",
        ) from exc
    session.refresh(card)
    return card


@router.get("/rules", dependencies=[Depends(require_agent_access)])
def get_rules() -> dict[str, object]:
    return get_bank_rules()


@router.post("/rules/import", dependencies=[Depends(require_agent_access)])
def import_rules(payload: BankRulesImport, session: SessionDep) -> dict[str, object]:
    try:
        rules = import_bank_rules(payload.rules, merge=payload.mode == "merge")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    matched = 0
    updated = 0
    if payload.apply_existing:
        for card in session.scalars(select(Card)):
            if match_product_rule(card.bank_code, card.card_product):
                matched += 1
                if apply_product_dictionary(card):
                    updated += 1
        session.commit()
    return {
        "success": True,
        "bank_count": len(rules),
        "matched_cards": matched,
        "updated_cards": updated,
    }


@router.get(
    "", response_model=list[CardRead], dependencies=[Depends(require_agent_access)]
)
def list_cards(
    session: SessionDep,
    include_hidden: Annotated[bool, Query()] = True,
    include_deleted: Annotated[bool, Query()] = False,
    status: Annotated[str | None, Query()] = None,
) -> list[Card]:
    query = select(Card).order_by(Card.created_at)
    if not include_deleted:
        query = query.where(Card.deleted_at.is_(None))
    if not include_hidden:
        query = query.where(Card.hidden.is_(False))
    if status:
        query = query.where(Card.status == status)
    return list(session.scalars(query))


@router.get(
    "/calendar-estimates",
    response_model=list[CalendarEstimateRead],
    dependencies=[Depends(require_agent_access)],
)
def list_calendar_estimates(
    session: SessionDep,
    month: Annotated[
        date, Query(le=date(9998, 12, 1), description="First day of the displayed month")
    ],
) -> list[CalendarEstimateRead]:
    if month.day != 1:
        raise HTTPException(status_code=422, detail="month must be the first day")
    today = local_today(session.info["timezone"])
    cards = session.scalars(
        select(Card).where(Card.deleted_at.is_(None), Card.status == "active", Card.hidden.is_(False))
    ).all()
    issued = set(session.execute(select(Statement.card_id, Statement.statement_date)).all())
    estimates = []
    for card in cards:
        # CCB bills at account level; a per-card forecast would falsely split the account.
        if (
            card.bank_code == "CCB"
            or card.statement_day is None
            or not 1 <= card.statement_day <= 31
        ):
            continue
        for offset in range(-4, 1):
            absolute_month = month.year * 12 + month.month - 1 + offset
            if absolute_month < 12:
                continue
            anchor = date(absolute_month // 12, absolute_month % 12 + 1, 1)
            statement_date = clamped_statement_date(anchor, card.statement_day)
            if statement_date <= today or (card.id, statement_date) in issued:
                continue
            resolution = resolve_effective_due_rule(
                session, card, target_statement_date=statement_date
            )
            if resolution.rule is None:
                continue
            due_date = estimate_due_date(
                statement_date, resolution.rule.rule_type, resolution.rule.rule_value
            )
            target_month = (month.year, month.month)
            if (
                (statement_date.year, statement_date.month) != target_month
                and (due_date.year, due_date.month) != target_month
            ):
                continue
            estimates.append(CalendarEstimateRead(
                card_id=card.id, statement_date=statement_date, due_date=due_date
            ))
    return estimates


@router.get("/ignored", dependencies=[Depends(require_agent_access)])
def list_ignored_cards(session: SessionDep) -> list[dict[str, str]]:
    return get_ignored_cards(session)


@router.delete("/ignored/{bank_code}/{last4}", dependencies=[Depends(require_agent_access)])
def unignore_card(
    bank_code: str,
    last4: str,
    session: SessionDep,
    currency: Annotated[str, Query()] = "CNY",
) -> dict[str, Any]:
    removed = remove_ignored_card(session, bank_code=bank_code, last4=last4, currency=currency)
    return {"success": True, "removed": removed}


@router.get(
    "/{card_id}",
    response_model=CardRead,
    dependencies=[Depends(require_agent_access)],
)
def get_card(card_id: str, session: SessionDep) -> Card:
    card = session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


@router.get(
    "/{card_id}/interest-free-estimate",
    response_model=InterestFreeRead,
    dependencies=[Depends(require_agent_access)],
)
def get_card_interest_free_estimate(
    card_id: str,
    spend_date: Annotated[
        date, Query(le=date(9998, 12, 31), description="Spend date; year at most 9998 to leave room for forecast dates.")
    ],
    session: SessionDep,
) -> InterestFreeRead:
    """Forecast default T+0/T+1/T+2 scenarios from confirmed rule evidence."""
    card = session.get(Card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    unavailable = InterestFreeRead(available=False, mode="future_estimate")
    resolution = resolve_effective_due_rule(session, card, spend_date=spend_date)
    if resolution.rule is None:
        return unavailable.model_copy(update={"reason": resolution.reason})

    result = forecast_interest_free_days(
        card_id=card.id,
        spend_date=spend_date,
        statement_day=card.statement_day,
        due_date_rule_type=resolution.rule.rule_type,
        due_date_rule_value=resolution.rule.rule_value,
        boundary_state=resolution.rule.boundary,
        rule_source=resolution.rule.api_rule_source,
    )
    return InterestFreeRead(
        available=True, mode="future_estimate",
        estimate=FutureInterestFreeEstimateRead.model_validate(result).model_copy(
            update={
                "resolved_rule_type": resolution.rule.rule_type,
                "resolved_rule_value": resolution.rule.rule_value,
                "history_evidence_count": resolution.rule.history_evidence_count,
                "last_verified_date": resolution.rule.last_verified_date,
            }
        ),
    )


@router.patch(
    "/{card_id}",
    response_model=CardRead,
    dependencies=[Depends(require_agent_access)],
)
def update_card(card_id: str, payload: CardUpdate, session: SessionDep) -> Card:
    card = session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    changes = payload.model_dump(exclude_unset=True)
    changes.pop("annual_fee_source", None)
    for required_field in ("bank_name", "display_name", "statement_day_boundary"):
        if required_field in changes and changes[required_field] is None:
            raise HTTPException(
                status_code=422, detail=f"{required_field} cannot be null"
            )
    annual_fields = {
        "annual_fee_minor",
        "annual_fee_policy",
        "annual_fee_charge_date",
    }
    annual_changed = any(
        field in changes and changes[field] != getattr(card, field)
        for field in annual_fields
    )
    product_changed = (
        "card_product" in changes and changes["card_product"] != card.card_product
    )
    if "statement_day" in changes and changes["statement_day"] != card.statement_day:
        card.due_date_rule_type = "observed_only"
        card.due_date_rule_value = None
        card.statement_day_boundary = "uncertain"
        card.due_day = None
    if "is_hidden" in changes and "hidden" not in changes:
        changes["hidden"] = changes.pop("is_hidden")
    if "status" in changes:
        new_status = changes["status"]
        if new_status == "closed":
            card.status = "closed"
            card.closed_at = card.closed_at or utc_now()
            card.enabled = False
        elif new_status == "active":
            card.status = "active"
            card.closed_at = None
            card.enabled = True
        changes.pop("status")
        changes.pop("closed_at", None)
        changes.pop("enabled", None)

    for field, value in changes.items():
        setattr(card, field, value)

    if annual_changed:
        card.annual_fee_source = "manual"
    elif product_changed:
        apply_product_dictionary(card)

    session.commit()
    session.refresh(card)
    return card


@router.get(
    "/{card_id}/deletion-preview",
    response_model=CardDeletionPreview,
    dependencies=[Depends(require_agent_access)],
)
def get_card_deletion_preview(card_id: str, session: SessionDep) -> CardDeletionPreview:
    card = session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    statement_count = session.scalar(
        select(func.count(Statement.id)).where(Statement.card_id == card_id)
    ) or 0
    transaction_count = session.scalar(
        select(func.count(Transaction.id))
        .join(Statement, Transaction.statement_id == Statement.id)
        .where(Statement.card_id == card_id)
    ) or 0
    repayment_count = session.scalar(
        select(func.count(Repayment.id))
        .join(Statement, Repayment.statement_id == Statement.id)
        .where(Statement.card_id == card_id)
    ) or 0
    notification_setting_count = session.scalar(
        select(func.count(NotificationSetting.id)).where(NotificationSetting.card_id == card_id)
    ) or 0

    return CardDeletionPreview(
        card_id=card.id,
        bank_name=card.bank_name,
        display_name=card.display_name,
        last4=card.last4,
        statement_count=statement_count,
        transaction_count=transaction_count,
        repayment_count=repayment_count,
        notification_setting_count=notification_setting_count,
    )


@router.delete(
    "/{card_id}",
    dependencies=[Depends(require_agent_access)],
)
def delete_card(
    card_id: str,
    session: SessionDep,
    mode: Annotated[Literal["soft", "hard"], Query()] = "soft",
    ignore_future: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    card = session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    if mode == "soft":
        now = utc_now()
        card.deleted_at = now
        card.status = "closed"
        card.closed_at = card.closed_at or now
        card.enabled = False
        session.commit()
        return {
            "success": True,
            "mode": "soft",
            "id": card_id,
            "message": "Card soft-deleted; historical data preserved.",
        }

    # Set Transaction.card_id = None for transactions attached to this physical card before deleting the card
    session.execute(
        update(Transaction)
        .where(Transaction.card_id == card_id)
        .values(card_id=None)
    )

    statements = session.scalars(select(Statement).where(Statement.card_id == card_id)).all()
    statement_ids = [s.id for s in statements]
    source_import_ids = [s.source_import_id for s in statements if s.source_import_id]

    deleted_counts = {
        "statements": len(statements),
        "transactions": 0,
        "repayments": 0,
        "notification_logs": 0,
    }

    if statement_ids:
        deleted_counts["transactions"] = session.execute(
            delete(Transaction).where(Transaction.statement_id.in_(statement_ids))
        ).rowcount
        deleted_counts["repayments"] = session.execute(
            delete(Repayment).where(Repayment.statement_id.in_(statement_ids))
        ).rowcount
        deleted_counts["notification_logs"] = session.execute(
            delete(NotificationLog).where(NotificationLog.statement_id.in_(statement_ids))
        ).rowcount
        session.execute(delete(Statement).where(Statement.card_id == card_id))

    # Tombstone a source only after its last billing-unit Statement is deleted.
    if source_import_ids:
        unique_source_ids = set(source_import_ids)
        surviving_source_ids = set(
            session.scalars(
                select(Statement.source_import_id)
                .where(Statement.source_import_id.in_(unique_source_ids))
                .distinct()
            ).all()
        )
        import_records = session.scalars(
            select(ImportRecord).where(ImportRecord.id.in_(unique_source_ids))
        ).all()
        for rec in import_records:
            if rec.id not in surviving_source_ids:
                rec.parse_status = ImportStatus.DELETED_BY_USER

    if ignore_future:
        add_ignored_card(
            session=session,
            bank_code=card.bank_code,
            last4=card.last4,
            bank_name=card.bank_name,
            currency=card.currency or "CNY",
            reason="deleted_by_user",
            commit=False,
        )

    session.execute(delete(NotificationSetting).where(NotificationSetting.card_id == card_id))
    session.delete(card)
    session.commit()
    return {
        "success": True,
        "mode": "hard",
        "id": card_id,
        "deleted_counts": deleted_counts,
        "ignored_future": ignore_future,
        "message": "Card and all associated data permanently deleted.",
    }


@router.post(
    "/{card_id}/close",
    response_model=CardRead,
    dependencies=[Depends(require_agent_access)],
)
def close_card(card_id: str, session: SessionDep) -> Card:
    card = session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    card.status = "closed"
    card.closed_at = card.closed_at or utc_now()
    card.enabled = False
    session.commit()
    session.refresh(card)
    return card


@router.post(
    "/{card_id}/reopen",
    response_model=CardRead,
    dependencies=[Depends(require_agent_access)],
)
def reopen_card(card_id: str, session: SessionDep) -> Card:
    card = session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    card.status = "active"
    card.closed_at = None
    card.enabled = True
    session.commit()
    session.refresh(card)
    return card


@router.post(
    "/{card_id}/restore",
    response_model=CardRead,
    dependencies=[Depends(require_agent_access)],
)
def restore_card(card_id: str, session: SessionDep) -> Card:
    card = session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    card.deleted_at = None
    session.commit()
    session.refresh(card)
    return card
