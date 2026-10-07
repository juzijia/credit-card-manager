from pathlib import Path
import json
import urllib.request
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import require_agent_access, require_owner_access
from app.db.session import get_session
from app.models.catalog import (
    BankRegistry,
    CatalogArtwork,
    CatalogBenefit,
    CatalogProduct,
    CatalogScanItem,
    CatalogScanRun,
    CatalogSource,
    DictionaryChangeRequest,
)
from app.schemas.catalog import (
    BankRegistryRead,
    BatchChangeApprovalRequest,
    CardExtractCandidate,
    CatalogProductRead,
    CatalogScanRunRead,
    CatalogStatsRead,
    DictionaryChangeRequestRead,
    ScanTriggerRequest,
)
from app.services.catalog_scanner import run_catalog_scan
from app.services.card_products import refresh_dictionary_cards
from app.services.catalog_service import (
    ensure_bank_registries,
    upsert_catalog_products,
    approve_change_request,
    create_scan_run,
    get_all_bank_registries,
    get_catalog_statistics,
    process_bank_candidates,
    reject_change_request,
)

router = APIRouter(prefix="/catalog", tags=["catalog"])
SessionDep = Annotated[Session, Depends(get_session)]


class IngestCandidatesPayload(BaseModel):
    bank_id: str
    run_id: str | None = None
    candidates: list[CardExtractCandidate]


@router.get(
    "/stats",
    response_model=CatalogStatsRead,
    dependencies=[Depends(require_agent_access)],
)
def get_stats(session: SessionDep) -> dict[str, Any]:
    return get_catalog_statistics(session)


@router.get(
    "/banks",
    response_model=list[BankRegistryRead],
    dependencies=[Depends(require_agent_access)],
)
def list_banks(session: SessionDep) -> list[BankRegistry]:
    return get_all_bank_registries(session, enabled_only=False)


@router.get(
    "/products",
    response_model=list[CatalogProductRead],
    dependencies=[Depends(require_agent_access)],
)
def list_products(
    session: SessionDep,
    bank_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    query = select(CatalogProduct).options(
        selectinload(CatalogProduct.artworks),
        selectinload(CatalogProduct.benefits),
        selectinload(CatalogProduct.sources),
    )
    if bank_id:
        query = query.where(CatalogProduct.bank_id == bank_id.upper())
    if status:
        query = query.where(CatalogProduct.status == status)

    products = session.scalars(query.order_by(CatalogProduct.bank_id, CatalogProduct.canonical_name)).all()
    results: list[dict[str, Any]] = []
    for p in products:
        results.append({
            "id": p.id,
            "bank_id": p.bank_id,
            "canonical_name": p.canonical_name,
            "aliases": json.loads(p.aliases_json),
            "networks": json.loads(p.networks_json),
            "card_level": p.card_level,
            "status": p.status,
            "annual_fee_primary_minor": p.annual_fee_primary_minor,
            "annual_fee_supp_minor": p.annual_fee_supp_minor,
            "annual_fee_waiver_desc": p.annual_fee_waiver_desc,
            "verified_at": p.verified_at,
            "benefits": [
                {
                    "id": b.id,
                    "category": b.category,
                    "title": b.title,
                    "description": b.description,
                    "quota": b.quota,
                    "cycle": b.cycle,
                    "source_url": b.source_url,
                }
                for b in p.benefits
            ],
            "artworks": [
                {
                    "id": a.id,
                    "artwork_url": a.artwork_url,
                    "image_hash": a.image_hash,
                    "local_path": a.local_path,
                    "is_primary": a.is_primary,
                }
                for a in p.artworks
            ],
            "sources": [
                {
                    "id": s.id,
                    "title": s.title,
                    "url": s.url,
                    "source_type": s.source_type,
                    "tier": s.tier,
                }
                for s in p.sources
            ],
        })
    return results


@router.get(
    "/change-requests",
    response_model=list[DictionaryChangeRequestRead],
    dependencies=[Depends(require_agent_access)],
)
def list_change_requests(
    session: SessionDep,
    status: str | None = "PENDING",
    bank_id: str | None = None,
) -> list[dict[str, Any]]:
    query = select(DictionaryChangeRequest)
    if status and status != "ALL":
        query = query.where(DictionaryChangeRequest.status == status)
    if bank_id:
        query = query.where(DictionaryChangeRequest.bank_id == bank_id.upper())

    crs = session.scalars(query.order_by(DictionaryChangeRequest.created_at.desc())).all()
    results: list[dict[str, Any]] = []
    for c in crs:
        results.append({
            "id": c.id,
            "run_id": c.run_id,
            "bank_id": c.bank_id,
            "card_id": c.card_id,
            "change_type": c.change_type,
            "target_product_name": c.target_product_name,
            "old_value": json.loads(c.old_value_json) if c.old_value_json else None,
            "new_value": json.loads(c.new_value_json) if c.new_value_json else None,
            "source_url": c.source_url,
            "source_title": c.source_title,
            "evidence": c.evidence,
            "confidence": c.confidence,
            "status": c.status,
            "created_at": c.created_at,
            "reviewed_at": c.reviewed_at,
        })
    return results


@router.post(
    "/scan-runs",
    response_model=CatalogScanRunRead,
    dependencies=[Depends(require_agent_access)],
)
def trigger_scan_run(
    payload: ScanTriggerRequest,
    session: SessionDep,
) -> CatalogScanRun:
    banks = get_all_bank_registries(session, enabled_only=True)
    if payload.scope == "BANK" and payload.bank_id:
        banks = [b for b in banks if b.bank_id == payload.bank_id.upper()]
        if not banks:
            raise HTTPException(status_code=404, detail=f"Bank {payload.bank_id} not found in registry")

    run = create_scan_run(
        session=session,
        scope=payload.scope,
        target_bank_id=payload.bank_id,
        target_card_id=payload.card_id,
        triggered_by="manual",
    )
    # Execute scan synchronously (or backgroundable in production)
    run_catalog_scan(session, run, banks)
    return run


@router.get(
    "/scan-runs",
    response_model=list[CatalogScanRunRead],
    dependencies=[Depends(require_agent_access)],
)
def list_scan_runs(
    session: SessionDep,
    limit: int = 20,
) -> list[CatalogScanRun]:
    return list(
        session.scalars(
            select(CatalogScanRun).order_by(CatalogScanRun.started_at.desc()).limit(limit)
        )
    )


@router.post(
    "/ingest",
    dependencies=[Depends(require_agent_access)],
)
def ingest_candidates(
    payload: IngestCandidatesPayload,
    session: SessionDep,
) -> dict[str, Any]:
    """
    OpenClaw / AI write endpoint to submit extracted card candidates.
    Generates change requests via diff engine without directly writing to formal catalog tables.
    """
    bank_id = payload.bank_id.upper()
    run_id = payload.run_id
    if not run_id:
        run = create_scan_run(session, scope="BANK", target_bank_id=bank_id, triggered_by="openclaw")
        run_id = run.id

    discovered, requests = process_bank_candidates(session, run_id, bank_id, payload.candidates)
    return {
        "success": True,
        "run_id": run_id,
        "bank_id": bank_id,
        "discovered_changes": discovered,
        "change_request_ids": [r.id for r in requests],
    }


@router.post(
    "/change-requests/{request_id}/approve",
    dependencies=[Depends(require_owner_access)],
)
def approve_request(
    request_id: str,
    session: SessionDep,
) -> dict[str, Any]:
    try:
        return approve_change_request(session, request_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/change-requests/{request_id}/reject",
    dependencies=[Depends(require_owner_access)],
)
def reject_request(
    request_id: str,
    session: SessionDep,
) -> dict[str, Any]:
    try:
        return reject_change_request(session, request_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/change-requests/batch",
    dependencies=[Depends(require_owner_access)],
)
def batch_review_requests(
    payload: BatchChangeApprovalRequest,
    session: SessionDep,
) -> dict[str, Any]:
    success_count = 0
    errors: list[dict[str, str]] = []
    for req_id in payload.request_ids:
        try:
            if payload.action == "APPROVE":
                approve_change_request(session, req_id)
            else:
                reject_change_request(session, req_id)
            success_count += 1
        except Exception as exc:
            errors.append({"id": req_id, "error": str(exc)})

    return {
        "action": payload.action,
        "total": len(payload.request_ids),
        "success_count": success_count,
        "errors": errors,
    }


DEFAULT_CATALOG_SYNC_URL = "https://raw.githubusercontent.com/juzijia/credit-card-manager/main/server/app/data/catalog_fixtures.json"
MAX_CATALOG_SYNC_BYTES = 8 * 1024 * 1024
_BUNDLED_FIXTURES_PATH = Path(__file__).resolve().parents[2] / "data" / "catalog_fixtures.json"

class CatalogSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _NoCatalogRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_catalog_fixtures(raw_text: str, session: Session) -> dict[str, list[dict[str, Any]]]:
    candidates = TypeAdapter(dict[str, list[CardExtractCandidate]]).validate_python(
        json.loads(raw_text), strict=True
    )
    if not candidates or not any(candidates.values()):
        raise ValueError("字典资料不能为空")
    bank_ids = set(session.scalars(select(BankRegistry.bank_id)))
    for bank_id, cards in candidates.items():
        if bank_id not in bank_ids:
            raise ValueError("字典包含未知银行")
        names: set[str] = set()
        for card in cards:
            name = card.canonical_name.strip()
            if card.bank_id != bank_id or not 1 <= len(name) <= 64 or name in names:
                raise ValueError("卡片银行或名称无效，或存在重复卡片")
            names.add(name)
            if not 0 <= card.confidence <= 1:
                raise ValueError("卡片资料置信度无效")
            for fee in (card.annual_fee.primary, card.annual_fee.supplementary):
                if fee is not None and not 0 <= fee < 2**63:
                    raise ValueError("卡片年费金额无效")
    return {bank_id: [card.model_dump(exclude_unset=True) for card in cards]
            for bank_id, cards in candidates.items()}


@router.post("/sync", dependencies=[Depends(require_owner_access)])
def sync_catalog(
    payload: CatalogSyncRequest,
    session: SessionDep,
) -> dict[str, Any]:
    from datetime import datetime
    from sqlalchemy import func

    ensure_bank_registries(session)
    target_url = DEFAULT_CATALOG_SYNC_URL
    raw_bytes: bytes | None = None
    try:
        req = urllib.request.Request(
            target_url,
            headers={"User-Agent": "CreditCardManager/1.0"},
        )
        opener = urllib.request.build_opener(_NoCatalogRedirects())
        with opener.open(req, timeout=10) as resp:
            if resp.status != 200:
                raise OSError(f"HTTP {resp.status}")
            raw_bytes = resp.read(MAX_CATALOG_SYNC_BYTES + 1)
    except OSError:
        total = session.scalar(select(func.count(CatalogProduct.id))) or 0
        if total > 0:
            return {
                "success": True,
                "source": "database_fallback",
                "message": "可信源暂不可用，继续使用已持久化的本地字典",
                "url": target_url,
                "created": 0,
                "updated": 0,
                "total": total,
            }
        if not _BUNDLED_FIXTURES_PATH.exists():
            raise HTTPException(status_code=500, detail="未找到内置字典资料库文件")
        try:
            raw_text = _BUNDLED_FIXTURES_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=500, detail="读取内置字典文件失败") from exc
        source = "builtin_bootstrap"
        message = "可信源暂不可用，已使用内置资料初始化本地字典"
    else:
        if len(raw_bytes) > MAX_CATALOG_SYNC_BYTES:
            raise HTTPException(status_code=413, detail="字典响应超过 8 MiB 限制")
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=422, detail="字典文件必须使用 UTF-8 编码") from exc
        source = "remote"
        message = "已从可信源同步更新完成"

    try:
        data = _validate_catalog_fixtures(raw_text, session)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="字典文件格式或结构无效") from exc

    now = datetime.now()
    created, updated = upsert_catalog_products(session, data, now=now)
    refreshed_cards = refresh_dictionary_cards(session)
    total = session.scalar(select(func.count(CatalogProduct.id))) or 0
    return {
        "success": True,
        "source": source,
        "message": message,
        "url": target_url,
        "created": created,
        "updated": updated,
        "refreshed_cards": refreshed_cards,
        "total": total,
    }

