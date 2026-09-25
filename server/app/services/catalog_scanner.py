import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.catalog import BankRegistry, CatalogScanItem, CatalogScanRun
from app.schemas.catalog import (
    AnnualFeeSchema,
    ArtworkExtractSchema,
    BenefitExtractSchema,
    CardExtractCandidate,
    SourceExtractSchema,
)
from app.services.catalog_service import process_bank_candidates

logger = logging.getLogger(__name__)

_FIXTURES_PATH = Path(__file__).resolve().parents[1] / "data" / "catalog_fixtures.json"


def load_bank_candidates() -> dict[str, list[dict[str, Any]]]:
    if _FIXTURES_PATH.exists():
        try:
            return json.loads(_FIXTURES_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to load catalog_fixtures.json (%s), using fallback", e)
    return FIXTURE_BANK_CANDIDATES_FALLBACK


# Realistic official catalog fixtures for offline testing & repeatable fallback
FIXTURE_BANK_CANDIDATES_FALLBACK: dict[str, list[dict[str, Any]]] = {
    "CMB": [
        {
            "bank_id": "CMB",
            "canonical_name": "招商银行经典白金信用卡",
            "aliases": ["经典白", "经典白金卡"],
            "networks": ["UnionPay", "VISA"],
            "card_level": "Platinum",
            "status": "active",
            "annual_fee": {
                "primary": 360000,
                "supplementary": 200000,
                "waiver_description": "主卡年费3600元，可用10000永久积分兑换主卡年费；附属卡年费2000元，可用5000永久积分兑换。",
            },
            "benefits": [
                {
                    "category": "出行",
                    "title": "机场贵宾厅",
                    "description": "每年6次全流程机场贵宾厅服务，可免费携带1人",
                    "quota": "6次/年",
                    "cycle": "每核卡年",
                    "source_url": "https://market.cmbchina.com/ccard/classic/",
                },
                {
                    "category": "酒店",
                    "title": "300精选酒店贵宾价入住",
                    "description": "每年4间夜全球精选酒店特惠预订权益",
                    "quota": "4间夜/年",
                    "cycle": "每核卡年",
                    "source_url": "https://market.cmbchina.com/ccard/classic/",
                },
                {
                    "category": "健康",
                    "title": "体检/洗牙服务",
                    "description": "每年1次全国知名体检机构常规体检及1次洗牙服务",
                    "quota": "体检1次+洗牙1次",
                    "cycle": "每核卡年",
                    "source_url": "https://market.cmbchina.com/ccard/classic/",
                },
                {
                    "category": "保险",
                    "title": "航空意外及延误险",
                    "description": "最高1500万航空意外险及最高4000元旅行不便险",
                    "quota": "1500万元",
                    "cycle": "长期有效",
                    "source_url": "https://market.cmbchina.com/ccard/classic/",
                },
            ],
            "artworks": [
                {
                    "artwork_url": "https://market.cmbchina.com/ccard/classic/artwork.png",
                    "image_hash": hashlib.sha256(b"cmb_classic_artwork_v1").hexdigest(),
                    "is_primary": True,
                }
            ],
            "sources": [
                {
                    "title": "招商银行经典白金信用卡官方产品页面",
                    "url": "https://market.cmbchina.com/ccard/classic/",
                    "source_type": "official_site",
                    "tier": "S",
                }
            ],
            "confidence": 0.98,
        },
        {
            "bank_id": "CMB",
            "canonical_name": "招商银行自由人生白金信用卡",
            "aliases": ["鸟白", "智汇白金卡"],
            "networks": ["UnionPay"],
            "card_level": "Platinum",
            "status": "active",
            "annual_fee": {
                "primary": 80000,
                "supplementary": None,
                "waiver_description": "首年免年费，当年消费满9万元免次年年费。",
            },
            "benefits": [
                {
                    "category": "文娱",
                    "title": "每月积分兑换音乐/视频会员",
                    "description": "每月消费达标可用99积分兑换主流影音平台月卡",
                    "quota": "1次/月",
                    "cycle": "每月",
                    "source_url": "https://market.cmbchina.com/ccard/bird/",
                }
            ],
            "artworks": [
                {
                    "artwork_url": "https://market.cmbchina.com/ccard/bird/artwork.png",
                    "image_hash": hashlib.sha256(b"cmb_bird_artwork_v1").hexdigest(),
                    "is_primary": True,
                }
            ],
            "sources": [
                {
                    "title": "招商银行自由人生白金信用卡产品介绍",
                    "url": "https://market.cmbchina.com/ccard/bird/",
                    "source_type": "official_site",
                    "tier": "S",
                }
            ],
            "confidence": 0.95,
        },
    ],
    "BOC": [
        {
            "bank_id": "BOC",
            "canonical_name": "中国银行中银数字白金卡",
            "aliases": ["数字白金卡", "中银数字白"],
            "networks": ["UnionPay"],
            "card_level": "Platinum",
            "status": "active",
            "annual_fee": {
                "primary": 80000,
                "supplementary": None,
                "waiver_description": "首年免年费，刷卡消费满12笔免次年年费。",
            },
            "benefits": [
                {
                    "category": "积分",
                    "title": "快捷支付长期多倍积分",
                    "description": "微信/支付宝等移动快捷支付长期享受3倍积分权益",
                    "quota": "2万分/月封顶",
                    "cycle": "长期",
                    "source_url": "https://www.boc.cn/bcservice/bc1/202008/t20200803_18032731.html",
                }
            ],
            "artworks": [
                {
                    "artwork_url": "https://www.boc.cn/bcservice/bc1/images/boc_digital_plat.png",
                    "image_hash": hashlib.sha256(b"boc_digital_plat_v1").hexdigest(),
                    "is_primary": True,
                }
            ],
            "sources": [
                {
                    "title": "中国银行中银数字信用卡专区",
                    "url": "https://www.boc.cn/bcservice/bc1/202008/t20200803_18032731.html",
                    "source_type": "official_site",
                    "tier": "S",
                }
            ],
            "confidence": 0.96,
        }
    ],
}

FIXTURE_BANK_CANDIDATES = load_bank_candidates()


def run_catalog_scan(
    session: Session,
    scan_run: CatalogScanRun,
    bank_registries: list[BankRegistry],
    custom_candidates: dict[str, list[dict[str, Any]]] | None = None,
) -> CatalogScanRun:
    """
    Execute scan run across bank registries with strict single-bank isolation.
    If a bank fetch or diff fails, it logs to catalog_scan_items and continues the run.
    """
    candidate_source = custom_candidates if custom_candidates is not None else FIXTURE_BANK_CANDIDATES

    scan_run.total_banks = len(bank_registries)
    completed_banks = 0
    failed_banks = 0
    total_discovered = 0
    errors: list[str] = []

    for bank in bank_registries:
        item = CatalogScanItem(
            run_id=scan_run.id,
            bank_id=bank.bank_id,
            status="RUNNING",
            started_at=datetime.now(),
        )
        session.add(item)
        session.commit()

        try:
            raw_candidates = candidate_source.get(bank.bank_id, [])
            candidates = [CardExtractCandidate.model_validate(c) for c in raw_candidates]
            discovered, _ = process_bank_candidates(session, scan_run.id, bank.bank_id, candidates)
            total_discovered += discovered

            item.status = "SUCCESS"
            item.completed_at = datetime.now()
            completed_banks += 1
            session.commit()
        except Exception as exc:
            logger.error("Scan item failed for bank %s: %s", bank.bank_id, exc)
            item.status = "FAILED"
            item.completed_at = datetime.now()
            item.error_message = str(exc)
            failed_banks += 1
            errors.append(f"{bank.bank_id}: {exc}")
            session.commit()

    scan_run.completed_banks = completed_banks
    scan_run.failed_banks = failed_banks
    scan_run.discovered_changes = total_discovered
    scan_run.error_summary = "; ".join(errors) if errors else None
    scan_run.status = "COMPLETED" if failed_banks == 0 else ("PARTIAL" if completed_banks > 0 else "FAILED")
    scan_run.completed_at = datetime.now()
    session.commit()
    return scan_run
