from pathlib import Path
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
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
_FIXTURES_PATH = Path(__file__).resolve().parents[2] / "data" / "catalog_fixtures.json"

class CatalogSyncRequest(BaseModel):
    url: str | None = None

@router.post("/sync", dependencies=[Depends(require_owner_access)])
def sync_catalog(
    payload: CatalogSyncRequest,
    session: SessionDep,
) -> dict[str, Any]:
    import urllib.request
    from datetime import datetime
    from sqlalchemy import func

    ensure_bank_registries(session)
    source = "builtin"
    message = "已基于内置资料库更新完成"
    target_url = payload.url.strip() if (payload.url and payload.url.strip()) else None
    raw_text: str | None = None

    if target_url:
        try:
            req = urllib.request.Request(
                target_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CreditCardManager/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    raw_text = resp.read().decode("utf-8")
                    source = "remote"
                    message = f"已从远程源同步更新完成: {target_url}"
                    try:
                        _FIXTURES_PATH.write_text(raw_text, encoding="utf-8")
                    except Exception:
                        pass
                else:
                    source = "builtin_fallback"
                    message = f"远程源返回 HTTP {resp.status}，已自动降级基于内置资料库更新完成"
        except Exception as exc:
            source = "builtin_fallback"
            message = f"远程源拉取失败（{exc}），已自动降级基于内置资料库更新完成"

    if raw_text is None:
        if not _FIXTURES_PATH.exists():
            raise HTTPException(
                status_code=500,
                detail="未找到内置字典资料库文件",
            )
        try:
            raw_text = _FIXTURES_PATH.read_text(encoding="utf-8")
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"读取内置字典文件失败: {exc}",
            ) from exc

    try:
        data = json.loads(raw_text)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"字典文件格式解析失败: {exc}",
        ) from exc

    now = datetime.now()
    created, updated = upsert_catalog_products(session, data, now=now)
    total = session.scalar(select(func.count(CatalogProduct.id))) or 0
    return {
        "success": True,
        "source": source,
        "message": message,
        "url": target_url,
        "created": created,
        "updated": updated,
        "total": total,
    }

