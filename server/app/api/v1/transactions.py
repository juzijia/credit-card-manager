from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import require_agent_access, require_owner_access
from app.db.session import get_session
from app.models.transaction import Transaction
from app.modules.core.transaction import (
    ClassificationSource,
    LifecycleViolationError,
    TransactionType,
    list_transactions as query_transactions,
    validate_purchase_type_update,
)
from app.schemas.transaction import TransactionRead, TransactionSemanticUpdate
from app.modules.optional.interest_free import calculate_interest_free_days
from app.schemas.interest_free import HistoricalInterestFreeEstimateRead, InterestFreeRead

router = APIRouter(
    prefix="/transactions",
    tags=["transactions"],
)
SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "",
    response_model=list[TransactionRead],
    dependencies=[Depends(require_agent_access)],
)
def list_transactions(
    session: SessionDep,
    statement_id: str | None = None,
    card_id: str | None = None,
) -> list[Transaction]:
    return query_transactions(
        session,
        statement_id=statement_id,
        card_id=card_id,
    )


@router.get(
    "/{transaction_id}/interest-free",
    response_model=InterestFreeRead,
    dependencies=[Depends(require_agent_access)],
)
def get_transaction_interest_free(
    transaction_id: str, session: SessionDep,
) -> InterestFreeRead:
    """Use the issued statement relation and due date, never future card rules.

    A missing transaction date cannot supply the calculator's required spend date.
    Missing posting dates use the transaction date with explicit estimation.
    """
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    unavailable = InterestFreeRead(available=False, mode="historical_actual")
    if transaction.transaction_date is None:
        return unavailable.model_copy(update={"reason": "missing_transaction_date"})
    statement = transaction.statement
    if statement is None or statement.due_date is None:
        return unavailable.model_copy(update={"reason": "missing_statement_facts"})
    if statement.due_date < transaction.transaction_date:
        return unavailable.model_copy(update={"reason": "due_date_before_transaction_date"})

    if transaction.card_id is not None:
        target_card_id = transaction.card_id
    elif statement.bank_code != "CCB":
        target_card_id = statement.card_id
    else:
        return unavailable.model_copy(update={"reason": "unknown_physical_card"})

    result = calculate_interest_free_days(
        card_id=target_card_id,
        transaction_date=transaction.transaction_date,
        posting_date=transaction.posting_date,
        due_date=statement.due_date,
        statement_date=statement.statement_date,
    )
    estimate = HistoricalInterestFreeEstimateRead.model_validate(result)
    return InterestFreeRead(available=True, mode="historical_actual", estimate=estimate)


@router.patch(
    "/{transaction_id}/semantics",
    response_model=TransactionRead,
    dependencies=[Depends(require_owner_access)],
)
def correct_transaction_semantics(
    transaction_id: str,
    payload: TransactionSemanticUpdate,
    session: SessionDep,
) -> Transaction:
    """Persist an Owner-confirmed semantic correction without changing source facts.

    Existing refund relationships are financial invariants. A semantic correction
    may refine an unresolved/unlinked observation, but it may not silently make a
    linked purchase/refund relation contradictory.
    """
    transaction = session.scalar(
        select(Transaction)
        .where(Transaction.id == transaction_id)
        .options(selectinload(Transaction.refunds))
    )
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found",
        )

    try:
        validate_purchase_type_update(transaction, payload.transaction_type)
    except LifecycleViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    if (
        transaction.original_transaction_id is not None
        and payload.transaction_type != TransactionType.REFUND
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Linked refund must remain classified as refund until the relation is removed",
        )

    transaction.transaction_type = payload.transaction_type.value
    transaction.classification_source = ClassificationSource.MANUAL.value
    transaction.classification_rule_version = None
    session.commit()
    session.refresh(transaction)
    return transaction
