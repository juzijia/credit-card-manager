from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.security import require_agent_access
from app.db.session import get_session
from app.schemas.analytics import AnalyticsResponse
from app.services.analytics import build_analytics

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
)
SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "",
    response_model=AnalyticsResponse,
    dependencies=[Depends(require_agent_access)],
)
def get_analytics(
    session: SessionDep,
    months: Annotated[int, Query(ge=1, le=24)] = 12,
    repayment_card_id: str | None = None,
) -> AnalyticsResponse:
    return build_analytics(session, months, repayment_card_id=repayment_card_id)
