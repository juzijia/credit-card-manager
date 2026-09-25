from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import require_agent_access
from app.db.session import get_session
from app.integrations.bean_sieve import bank_providers, parse_file
from app.models.parsed_batch import ParsedBatch
from app.parsers.base import ParserError
from app.parsers.eml import MAX_EML_BYTES
from app.schemas.import_record import ImportResult
from app.services.eml_import import import_eml_data, import_pdf_bytes

router = APIRouter(prefix="/imports", tags=["imports"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/providers", dependencies=[Depends(require_agent_access)])
def providers() -> dict[str, object]:
    items = bank_providers()
    return {"count": len(items), "providers": [{"id": item.id, "name": item.name, "formats": item.formats, "kind": item.kind, "capability": "transactions"} for item in items]}


@router.get("/batches", dependencies=[Depends(require_agent_access)])
def parsed_batches(session: SessionDep, provider_id: str | None = None) -> list[dict[str, object]]:
    query = select(ParsedBatch).order_by(ParsedBatch.created_at.desc()).limit(200)
    if provider_id:
        query = query.where(ParsedBatch.provider_id == provider_id)
    return [
        {
            "id": batch.id,
            "provider_id": batch.provider_id,
            "format": batch.original_format,
            "transaction_count": batch.transaction_count,
            "created_at": batch.created_at,
            "summary_available": False,
        }
        for batch in session.scalars(query)
    ]


@router.get(
    "/batches/{batch_id}", dependencies=[Depends(require_agent_access)]
)
def parsed_batch(batch_id: str, session: SessionDep) -> dict[str, object]:
    batch = session.scalar(select(ParsedBatch).options(selectinload(ParsedBatch.entries)).where(ParsedBatch.id == batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="Parsed batch not found")
    return {
        "id": batch.id,
        "provider_id": batch.provider_id,
        "format": batch.original_format,
        "transaction_count": batch.transaction_count,
        "summary_available": False,
        "transactions": [
            {
                "transaction_date": entry.transaction_date,
                "posting_date": entry.posting_date,
                "amount_minor": entry.amount_minor,
                "currency": entry.currency,
                "description": entry.description,
                "card_last4": entry.card_last4,
            }
            for entry in batch.entries
        ],
    }


@router.post("/inspect", dependencies=[Depends(require_agent_access)])
async def inspect_statement(
    file: Annotated[UploadFile, File(description="A statement supported by bean-sieve")],
    provider_id: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    data = await file.read(MAX_EML_BYTES + 1)
    if len(data) > MAX_EML_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 10 MiB size limit")
    suffix = Path(file.filename or "statement").suffix.lower()
    try:
        with TemporaryDirectory(prefix="ccm-inspect-") as directory:
            path = Path(directory) / ("statement" + suffix)
            path.write_bytes(data)
            provider, transactions = parse_file(path, provider_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "provider_id": provider.provider_id,
        "transaction_count": len(transactions),
        "currencies": sorted({item.currency for item in transactions}),
        "has_negative_amount": any(item.amount < 0 for item in transactions),
        "summary_available": False,
    }


@router.post(
    "/eml",
    response_model=ImportResult,
    dependencies=[Depends(require_agent_access)],
)
async def import_eml(
    session: SessionDep,
    file: Annotated[UploadFile, File(description="Bank statement in EML format")],
    card_id: str | None = None,
) -> ImportResult:
    data = await file.read(MAX_EML_BYTES + 1)
    if len(data) > MAX_EML_BYTES:
        raise HTTPException(status_code=413, detail="EML exceeds the 10 MiB size limit")
    try:
        return import_eml_data(session, data, card_id=card_id)
    except ParserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/file",
    response_model=ImportResult,
    dependencies=[Depends(require_agent_access)],
)
async def import_file(
    session: SessionDep,
    file: Annotated[UploadFile, File(description="Bank statement in EML or supported BOC/CITIC PDF format")],
    card_id: str | None = None,
) -> ImportResult:
    data = await file.read(MAX_EML_BYTES + 1)
    if len(data) > MAX_EML_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 10 MiB size limit")
    suffix = Path(file.filename or "statement").suffix.lower()
    if suffix == ".eml":
        try:
            return import_eml_data(session, data, card_id=card_id)
        except ParserError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if suffix == ".pdf":
        try:
            return import_pdf_bytes(session, data, card_id=card_id)
        except ParserError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(
        status_code=422,
        detail=f"Unsupported file type {suffix or '(none)'}; supported: .eml, .pdf",
    )
