import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

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
from app.schemas.catalog import CardExtractCandidate
from app.services.catalog_diff import diff_card_candidate

logger = logging.getLogger(__name__)

_DEFAULT_FIXTURES_PATH = Path(__file__).resolve().parents[1] / "data" / "catalog_fixtures.json"

DEFAULT_BANK_REGISTRIES = [
    {
        "bank_id": "CMB",
        "bank_name": "招商银行",
        "official_homepage": "https://www.cmbchina.com/",
        "credit_card_catalog_url": "https://market.cmbchina.com/ccard/cardlist/",
        "fee_schedule_url": "https://market.cmbchina.com/ccard/charge/",
        "notes": "招商银行信用卡官网卡片目录及资费标准",
        "enabled": True,
        "customer_service_phone": "400-820-5555 / 95555",
        "vip_service_phone": "400-888-5555",
        "overseas_phone": "+86-21-38784800",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://market.cmbchina.com/ccard/charge/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "ICBC",
        "bank_name": "中国工商银行",
        "official_homepage": "https://www.icbc.com.cn/",
        "credit_card_catalog_url": "https://www.icbc.com.cn/column/1438058341102.html",
        "fee_schedule_url": "https://www.icbc.com.cn/column/1438058341103.html",
        "notes": "工商银行牡丹信用卡目录",
        "enabled": True,
        "customer_service_phone": "95588",
        "vip_service_phone": "400-009-5588",
        "overseas_phone": "+86-10-95588",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://www.icbc.com.cn/column/1438058341103.html",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "CCB",
        "bank_name": "中国建设银行",
        "official_homepage": "https://www.ccb.com/",
        "credit_card_catalog_url": "https://creditcard.ccb.com/cn/creditcard/card_list.html",
        "fee_schedule_url": "https://creditcard.ccb.com/cn/creditcard/service.html",
        "notes": "建设银行龙卡信用卡产品线",
        "enabled": True,
        "customer_service_phone": "95533 / 400-820-0588",
        "vip_service_phone": "400-888-8858",
        "overseas_phone": "+86-21-38690588",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.ccb.com/cn/creditcard/service.html",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "BOC",
        "bank_name": "中国银行",
        "official_homepage": "https://www.boc.cn/",
        "credit_card_catalog_url": "https://www.boc.cn/bcservice/bc1/",
        "fee_schedule_url": "https://www.boc.cn/bcservice/bc4/",
        "notes": "中国银行信用卡中心产品中心",
        "enabled": True,
        "customer_service_phone": "400-669-5566 / 95566",
        "vip_service_phone": "400-669-5569",
        "overseas_phone": "+86-10-66085566",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://www.boc.cn/bcservice/bc4/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "ABC",
        "bank_name": "中国农业银行",
        "official_homepage": "https://www.abchina.com/",
        "credit_card_catalog_url": "https://www.abchina.com/cn/PersonalServices/CreditCard/CreditCardProducts/",
        "fee_schedule_url": "https://www.abchina.com/cn/PersonalServices/CreditCard/",
        "notes": "农业银行信用卡产品列表与资费标准",
        "enabled": True,
        "customer_service_phone": "400-669-5599 / 95599",
        "vip_service_phone": "400-619-5599",
        "overseas_phone": "+86-21-61195599",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://www.abchina.com/cn/PersonalServices/CreditCard/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "BOCOM",
        "bank_name": "交通银行",
        "official_homepage": "https://www.bankcomm.com/",
        "credit_card_catalog_url": "https://creditcard.bankcomm.com/content/dam/front/card/apply.html",
        "fee_schedule_url": "https://creditcard.bankcomm.com/content/pcc-charge/",
        "notes": "交通银行信用卡中心选卡中心",
        "enabled": True,
        "customer_service_phone": "400-800-9888 / 95559",
        "vip_service_phone": "400-866-6888",
        "overseas_phone": "+86-21-28283888",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.bankcomm.com/content/pcc-charge/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "CITIC",
        "bank_name": "中信银行",
        "official_homepage": "https://creditcard.ecitic.com/",
        "credit_card_catalog_url": "https://creditcard.ecitic.com/tuijian/index.shtml",
        "fee_schedule_url": "https://creditcard.ecitic.com/tariff/",
        "notes": "中信银行信用卡官网卡片推荐与资费公示",
        "enabled": True,
        "customer_service_phone": "400-889-5558",
        "vip_service_phone": "400-609-5558",
        "overseas_phone": "+86-755-82380730",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.ecitic.com/tariff/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "SPDB",
        "bank_name": "浦发银行",
        "official_homepage": "https://ccc.spdb.com.cn/",
        "credit_card_catalog_url": "https://ccc.spdb.com.cn/miniSite/card/apply.shtml",
        "fee_schedule_url": "https://ccc.spdb.com.cn/tariff/",
        "notes": "浦发银行信用卡中心办卡产品目录",
        "enabled": True,
        "customer_service_phone": "400-820-8788 / 95528",
        "vip_service_phone": "400-889-5528",
        "overseas_phone": "+86-21-38784988",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://ccc.spdb.com.cn/tariff/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "CGB",
        "bank_name": "广发银行",
        "official_homepage": "https://card.cgbchina.com.cn/",
        "credit_card_catalog_url": "https://card.cgbchina.com.cn/subsite/201908/cardmall/index.html",
        "fee_schedule_url": "https://card.cgbchina.com.cn/tariff/",
        "notes": "广发银行信用卡卡片商城与资费公示",
        "enabled": True,
        "customer_service_phone": "95508 / 400-830-8003",
        "vip_service_phone": "800-830-8003 / 400-889-5508",
        "overseas_phone": "+86-20-87310026",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://card.cgbchina.com.cn/tariff/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "CMBC",
        "bank_name": "中国民生银行",
        "official_homepage": "https://creditcard.cmbc.com.cn/",
        "credit_card_catalog_url": "https://creditcard.cmbc.com.cn/home/cn/web/product/hot/index.shtml",
        "fee_schedule_url": "https://creditcard.cmbc.com.cn/tariff/",
        "notes": "民生银行信用卡产品热销中心",
        "enabled": True,
        "customer_service_phone": "400-669-5568 / 95568",
        "vip_service_phone": "400-810-8008",
        "overseas_phone": "+86-10-38784800",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.cmbc.com.cn/tariff/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "CIB",
        "bank_name": "兴业银行",
        "official_homepage": "https://creditcard.cib.com.cn/",
        "credit_card_catalog_url": "https://creditcard.cib.com.cn/card/index.html",
        "fee_schedule_url": "https://creditcard.cib.com.cn/charge/",
        "notes": "兴业银行信用卡卡片中心与资费",
        "enabled": True,
        "customer_service_phone": "95561",
        "vip_service_phone": "400-889-5561",
        "overseas_phone": "+86-21-38769999",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.cib.com.cn/charge/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "PAB",
        "bank_name": "平安银行",
        "official_homepage": "https://creditcard.pingan.com/",
        "credit_card_catalog_url": "https://creditcard.pingan.com/buka/card/cardlist.shtml",
        "fee_schedule_url": "https://creditcard.pingan.com/tariff/",
        "notes": "平安银行信用卡卡片选卡大厅",
        "enabled": True,
        "customer_service_phone": "95511-2",
        "vip_service_phone": "400-889-5511",
        "overseas_phone": "+86-755-29999511",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.pingan.com/tariff/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "CEB",
        "bank_name": "中国光大银行",
        "official_homepage": "https://xyk.cebbank.com/",
        "credit_card_catalog_url": "https://xyk.cebbank.com/home/home-card/card-list.htm",
        "fee_schedule_url": "https://xyk.cebbank.com/tariff/",
        "notes": "光大银行信用卡中心卡片大全",
        "enabled": True,
        "customer_service_phone": "95595 / 400-788-8888",
        "vip_service_phone": "400-811-1333",
        "overseas_phone": "+86-10-95595",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://xyk.cebbank.com/tariff/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "PSBC",
        "bank_name": "中国邮政储蓄银行",
        "official_homepage": "https://www.psbc.com/",
        "credit_card_catalog_url": "https://www.psbc.com/cn/PersonalBanking/CreditCard/kpcp/",
        "fee_schedule_url": "https://www.psbc.com/cn/PersonalBanking/CreditCard/",
        "notes": "邮储银行信用卡卡片产品中心",
        "enabled": True,
        "customer_service_phone": "400-889-5580 / 95580",
        "vip_service_phone": "400-898-8880",
        "overseas_phone": "+86-10-95580",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://www.psbc.com/cn/PersonalBanking/CreditCard/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "HXB",
        "bank_name": "华夏银行",
        "official_homepage": "https://www.hxb.com.cn/",
        "credit_card_catalog_url": "https://creditcard.hxb.com.cn/",
        "fee_schedule_url": "https://creditcard.hxb.com.cn/charge/",
        "notes": "全国性股份制商业银行 - 华夏银行信用卡",
        "enabled": True,
        "customer_service_phone": "400-669-5577 / 95577",
        "vip_service_phone": "400-669-5577",
        "overseas_phone": "+86-10-95577",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.hxb.com.cn/charge/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "CZBANK",
        "bank_name": "浙商银行",
        "official_homepage": "https://www.czbank.com/",
        "credit_card_catalog_url": "https://creditcard.czbank.com/",
        "fee_schedule_url": "https://creditcard.czbank.com/fee/",
        "notes": "全国性股份制商业银行 - 浙商银行信用卡",
        "enabled": True,
        "customer_service_phone": "95527",
        "vip_service_phone": "400-889-5527",
        "overseas_phone": "+86-571-95527",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.czbank.com/fee/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "BJBANK",
        "bank_name": "北京银行",
        "official_homepage": "https://www.bankofbeijing.com.cn/",
        "credit_card_catalog_url": "https://creditcard.bankofbeijing.com.cn/",
        "fee_schedule_url": "https://creditcard.bankofbeijing.com.cn/charge/",
        "notes": "头部城市商业银行 - 北京银行京卡信用卡",
        "enabled": True,
        "customer_service_phone": "400-660-1169 / 95526",
        "vip_service_phone": "400-660-1169",
        "overseas_phone": "+86-10-95526",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.bankofbeijing.com.cn/charge/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "SHBANK",
        "bank_name": "上海银行",
        "official_homepage": "https://www.bosc.cn/",
        "credit_card_catalog_url": "https://card.bosc.cn/",
        "fee_schedule_url": "https://card.bosc.cn/charge/",
        "notes": "头部城市商业银行 - 上海银行信用卡",
        "enabled": True,
        "customer_service_phone": "95594",
        "vip_service_phone": "400-889-5594",
        "overseas_phone": "+86-21-95594",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://card.bosc.cn/charge/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "JSBANK",
        "bank_name": "江苏银行",
        "official_homepage": "https://www.jsbchina.cn/",
        "credit_card_catalog_url": "https://creditcard.jsbchina.cn/",
        "fee_schedule_url": "https://creditcard.jsbchina.cn/fee/",
        "notes": "头部城市商业银行 - 江苏银行信用卡",
        "enabled": True,
        "customer_service_phone": "95319",
        "vip_service_phone": "400-828-0888",
        "overseas_phone": "+86-25-95319",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.jsbchina.cn/fee/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "NBCB",
        "bank_name": "宁波银行",
        "official_homepage": "https://www.nbcb.com.cn/",
        "credit_card_catalog_url": "https://creditcard.nbcb.com.cn/",
        "fee_schedule_url": "https://creditcard.nbcb.com.cn/fee/",
        "notes": "头部城市商业银行 - 宁波银行信用卡",
        "enabled": True,
        "customer_service_phone": "95574",
        "vip_service_phone": "400-889-5574",
        "overseas_phone": "+86-574-95574",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.nbcb.com.cn/fee/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "HFB",
        "bank_name": "恒丰银行",
        "official_homepage": "https://www.hfbank.com.cn/",
        "credit_card_catalog_url": "https://creditcard.hfbank.com.cn/",
        "fee_schedule_url": "https://creditcard.hfbank.com.cn/fee/",
        "notes": "全国性股份制商业银行 - 恒丰银行信用卡",
        "enabled": True,
        "customer_service_phone": "95395",
        "vip_service_phone": "400-669-5395",
        "overseas_phone": "+86-535-95395",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.hfbank.com.cn/fee/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "CBHB",
        "bank_name": "渤海银行",
        "official_homepage": "https://www.cbhb.com.cn/",
        "credit_card_catalog_url": "https://creditcard.cbhb.com.cn/",
        "fee_schedule_url": "https://creditcard.cbhb.com.cn/fee/",
        "notes": "全国性股份制商业银行 - 渤海银行信用卡",
        "enabled": True,
        "customer_service_phone": "95541 / 400-888-8811",
        "vip_service_phone": "400-888-8811",
        "overseas_phone": "+86-22-95541",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://creditcard.cbhb.com.cn/fee/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
    {
        "bank_id": "HSBC",
        "bank_name": "汇丰银行(中国)",
        "official_homepage": "https://www.hsbc.com.cn/",
        "credit_card_catalog_url": "https://www.hsbc.com.cn/credit-cards/",
        "fee_schedule_url": "https://www.hsbc.com.cn/credit-cards/tariff/",
        "notes": "在华主要外资发卡行 - 汇丰中国信用卡",
        "enabled": True,
        "customer_service_phone": "95366",
        "vip_service_phone": "400-820-3090 / 400-820-8878",
        "overseas_phone": "+86-21-20756000",
        "service_hours": "7×24小时人工服务",
        "service_info_source_url": "https://www.hsbc.com.cn/credit-cards/tariff/",
        "service_info_verified_at": datetime(2026, 9, 11, 0, 0, 0),
    },
]
def ensure_bank_registries(session: Session) -> None:
    """Seed initial Bank Source Registries if not present."""
    for data in DEFAULT_BANK_REGISTRIES:
        existing = session.get(BankRegistry, data["bank_id"])
        if not existing:
            session.add(BankRegistry(**data))
        else:
            for k, v in data.items():
                if getattr(existing, k, None) is None and v is not None:
                    setattr(existing, k, v)
    session.commit()


def upsert_catalog_products(
    session: Session,
    data: dict[str, list[dict[str, Any]]],
    now: datetime | None = None,
) -> tuple[int, int]:
    """Upsert card products, benefits, artworks, and sources into catalog tables."""
    if now is None:
        now = datetime.now()
    created = 0
    updated = 0

    for bank_id, cards in data.items():
        bank_id_upper = bank_id.upper()
        for card_data in cards:
            cname = card_data.get("canonical_name", "").strip()
            if not cname:
                continue
            prod = session.scalar(
                select(CatalogProduct).where(
                    CatalogProduct.bank_id == bank_id_upper,
                    CatalogProduct.canonical_name == cname,
                )
            )
            if not prod:
                prod = CatalogProduct(bank_id=bank_id_upper, canonical_name=cname)
                session.add(prod)
                created += 1
            else:
                updated += 1
            prod.aliases_json = json.dumps(card_data.get("aliases", []), ensure_ascii=False)
            prod.networks_json = json.dumps(card_data.get("networks", []), ensure_ascii=False)
            prod.card_level = card_data.get("card_level")
            prod.status = card_data.get("status", "active")
            fee = card_data.get("annual_fee") or {}
            prod.annual_fee_primary_minor = fee.get("primary")
            prod.annual_fee_supp_minor = fee.get("supplementary")
            prod.annual_fee_waiver_desc = fee.get("waiver_description")
            prod.verified_at = now
            session.flush()

            session.query(CatalogBenefit).filter_by(product_id=prod.id).delete()
            for b in card_data.get("benefits", []):
                session.add(
                    CatalogBenefit(
                        product_id=prod.id,
                        category=b.get("category", "综合"),
                        title=b.get("title", ""),
                        description=b.get("description", ""),
                        quota=b.get("quota"),
                        cycle=b.get("cycle"),
                        source_url=b.get("source_url"),
                    )
                )

            session.query(CatalogArtwork).filter_by(product_id=prod.id).delete()
            for art in card_data.get("artworks", []):
                session.add(
                    CatalogArtwork(
                        product_id=prod.id,
                        artwork_url=art.get("artwork_url", ""),
                        image_hash=art.get("image_hash", ""),
                        is_primary=art.get("is_primary", True),
                    )
                )

    session.commit()
    return created, updated


def ensure_catalog_products(
    session: Session,
    fixtures_path: Path | None = None,
) -> tuple[int, int]:
    """Seed initial catalog products from bundled fixtures if catalog_products table is empty."""
    count = session.scalar(select(func.count(CatalogProduct.id))) or 0
    if count > 0:
        return 0, 0

    path = fixtures_path or _DEFAULT_FIXTURES_PATH
    if not path.exists():
        logger.warning("Catalog fixtures file not found at %s", path)
        return 0, 0

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load catalog fixtures from %s: %s", path, exc)
        return 0, 0

    return upsert_catalog_products(session, data)


def get_all_bank_registries(session: Session, enabled_only: bool = True) -> list[BankRegistry]:
    query = select(BankRegistry)
    if enabled_only:
        query = query.where(BankRegistry.enabled.is_(True))
    return list(session.scalars(query.order_by(BankRegistry.bank_id)))


def get_catalog_product_dict(session: Session, bank_id: str, canonical_name: str) -> dict[str, Any] | None:
    product = session.scalar(
        select(CatalogProduct)
        .where(
            CatalogProduct.bank_id == bank_id.upper(),
            CatalogProduct.canonical_name == canonical_name.strip(),
        )
        .options(
            selectinload(CatalogProduct.artworks),
            selectinload(CatalogProduct.benefits),
            selectinload(CatalogProduct.sources),
        )
    )
    if not product:
        return None

    return {
        "id": product.id,
        "bank_id": product.bank_id,
        "canonical_name": product.canonical_name,
        "aliases": json.loads(product.aliases_json),
        "networks": json.loads(product.networks_json),
        "card_level": product.card_level,
        "status": product.status,
        "annual_fee_primary_minor": product.annual_fee_primary_minor,
        "annual_fee_supp_minor": product.annual_fee_supp_minor,
        "annual_fee_waiver_desc": product.annual_fee_waiver_desc,
        "verified_at": product.verified_at,
        "artworks": [
            {
                "id": a.id,
                "artwork_url": a.artwork_url,
                "image_hash": a.image_hash,
                "local_path": a.local_path,
                "is_primary": a.is_primary,
            }
            for a in product.artworks
        ],
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
            for b in product.benefits
        ],
        "sources": [
            {
                "id": s.id,
                "title": s.title,
                "url": s.url,
                "source_type": s.source_type,
                "tier": s.tier,
            }
            for s in product.sources
        ],
    }


def create_scan_run(
    session: Session,
    scope: str = "FULL",
    target_bank_id: str | None = None,
    target_card_id: str | None = None,
    triggered_by: str = "manual",
) -> CatalogScanRun:
    run = CatalogScanRun(
        scope=scope,
        target_bank_id=target_bank_id,
        target_card_id=target_card_id,
        status="RUNNING",
        triggered_by=triggered_by,
        started_at=datetime.now(),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def process_bank_candidates(
    session: Session,
    run_id: str,
    bank_id: str,
    candidates: list[CardExtractCandidate],
) -> tuple[int, list[DictionaryChangeRequest]]:
    """
    Process candidates for one bank within a run.
    Isolated so failures don't affect other banks.
    Returns (discovered_changes_count, created_requests).
    """
    created_requests: list[DictionaryChangeRequest] = []
    for cand in candidates:
        cur_prod = get_catalog_product_dict(session, bank_id, cand.canonical_name)
        diffs = diff_card_candidate(cand, cur_prod)
        for diff in diffs:
            # Avoid duplicate change requests in the same run for the same target & type
            existing = session.scalar(
                select(DictionaryChangeRequest).where(
                    DictionaryChangeRequest.run_id == run_id,
                    DictionaryChangeRequest.bank_id == bank_id,
                    DictionaryChangeRequest.target_product_name == diff["target_product_name"],
                    DictionaryChangeRequest.change_type == diff["change_type"],
                )
            )
            if existing:
                continue

            cr = DictionaryChangeRequest(
                run_id=run_id,
                bank_id=bank_id,
                card_id=cur_prod["id"] if cur_prod else None,
                change_type=diff["change_type"],
                target_product_name=diff["target_product_name"],
                old_value_json=json.dumps(diff["old_value"], ensure_ascii=False) if diff["old_value"] is not None else None,
                new_value_json=json.dumps(diff["new_value"], ensure_ascii=False) if diff["new_value"] is not None else None,
                source_url=diff["source_url"],
                source_title=diff["source_title"],
                evidence=diff["evidence"],
                confidence=diff["confidence"],
                status="PENDING",
            )
            session.add(cr)
            created_requests.append(cr)
    session.commit()
    return len(created_requests), created_requests


def approve_change_request(session: Session, request_id: str) -> dict[str, Any]:
    """
    Approve a single change request within a database transaction.
    Enforces optimistic verification and applies changes to formal tables.
    """
    cr = session.get(DictionaryChangeRequest, request_id)
    if not cr:
        raise ValueError("Change request not found")
    if cr.status != "PENDING":
        raise ValueError(f"Change request is not PENDING (current: {cr.status})")

    new_val = json.loads(cr.new_value_json) if cr.new_value_json else {}

    # Locate or create CatalogProduct
    product = session.scalar(
        select(CatalogProduct).where(
            CatalogProduct.bank_id == cr.bank_id,
            CatalogProduct.canonical_name == cr.target_product_name,
        )
    )

    now = datetime.now()

    if cr.change_type in ("ADD_CARD", "NEEDS_REVIEW"):
        if product:
            # Already exists! Mark request SUPERSEDED
            cr.status = "SUPERSEDED"
            session.commit()
            return {"status": "SUPERSEDED", "detail": "Product already exists in catalog"}

        product = CatalogProduct(
            bank_id=cr.bank_id,
            canonical_name=cr.target_product_name,
            aliases_json=json.dumps(new_val.get("aliases", []), ensure_ascii=False),
            networks_json=json.dumps(new_val.get("networks", []), ensure_ascii=False),
            card_level=new_val.get("card_level"),
            status=new_val.get("status", "active"),
            annual_fee_primary_minor=new_val.get("annual_fee", {}).get("primary"),
            annual_fee_supp_minor=new_val.get("annual_fee", {}).get("supplementary"),
            annual_fee_waiver_desc=new_val.get("annual_fee", {}).get("waiver_description"),
            verified_at=now,
        )
        session.add(product)
        session.flush()

        # Add benefits
        for b in new_val.get("benefits", []):
            session.add(CatalogBenefit(
                product_id=product.id,
                category=b["category"],
                title=b["title"],
                description=b["description"],
                quota=b.get("quota"),
                cycle=b.get("cycle"),
                source_url=b.get("source_url"),
            ))

        # Add artworks
        for a in new_val.get("artworks", []):
            session.add(CatalogArtwork(
                product_id=product.id,
                artwork_url=a["artwork_url"],
                image_hash=a.get("image_hash", ""),
                local_path=a.get("local_path"),
                is_primary=a.get("is_primary", False),
            ))

        # Add source
        session.add(CatalogSource(
            product_id=product.id,
            title=cr.source_title,
            url=cr.source_url,
            source_type="official_site",
            tier="S",
        ))

    elif cr.change_type == "DISCONTINUE_CARD":
        if not product:
            cr.status = "REJECTED"
            session.commit()
            raise ValueError("Target card product not found in catalog")
        product.status = new_val.get("status", "discontinued")
        product.verified_at = now

    elif cr.change_type == "UPDATE_ANNUAL_FEE":
        if not product:
            raise ValueError("Target card product not found in catalog")
        if new_val.get("annual_fee_primary_minor") is not None:
            product.annual_fee_primary_minor = new_val["annual_fee_primary_minor"]
        if new_val.get("annual_fee_supp_minor") is not None:
            product.annual_fee_supp_minor = new_val["annual_fee_supp_minor"]
        if new_val.get("annual_fee_waiver_desc") is not None:
            product.annual_fee_waiver_desc = new_val["annual_fee_waiver_desc"]
        product.verified_at = now

    elif cr.change_type == "UPDATE_BENEFIT":
        if not product:
            raise ValueError("Target card product not found in catalog")
        # Check if matching benefit exists by title & category
        ben = session.scalar(
            select(CatalogBenefit).where(
                CatalogBenefit.product_id == product.id,
                CatalogBenefit.category == new_val.get("category"),
                CatalogBenefit.title == new_val.get("title"),
            )
        )
        if ben:
            ben.description = new_val.get("description", ben.description)
            ben.quota = new_val.get("quota", ben.quota)
            ben.cycle = new_val.get("cycle", ben.cycle)
            ben.source_url = new_val.get("source_url", ben.source_url)
        else:
            session.add(CatalogBenefit(
                product_id=product.id,
                category=new_val["category"],
                title=new_val["title"],
                description=new_val["description"],
                quota=new_val.get("quota"),
                cycle=new_val.get("cycle"),
                source_url=new_val.get("source_url"),
            ))
        product.verified_at = now

    elif cr.change_type == "ADD_ARTWORK":
        if not product:
            raise ValueError("Target card product not found in catalog")
        session.add(CatalogArtwork(
            product_id=product.id,
            artwork_url=new_val["artwork_url"],
            image_hash=new_val.get("image_hash", ""),
            is_primary=new_val.get("is_primary", False),
        ))
        product.verified_at = now

    cr.status = "APPROVED"
    cr.reviewed_at = now
    session.commit()
    return {"status": "APPROVED", "product_id": product.id if product else None}


def reject_change_request(session: Session, request_id: str) -> dict[str, Any]:
    cr = session.get(DictionaryChangeRequest, request_id)
    if not cr:
        raise ValueError("Change request not found")
    if cr.status != "PENDING":
        raise ValueError(f"Change request is not PENDING (current: {cr.status})")
    cr.status = "REJECTED"
    cr.reviewed_at = datetime.now()
    session.commit()
    return {"status": "REJECTED", "id": request_id}


def get_catalog_statistics(session: Session) -> dict[str, Any]:
    total_products = session.scalar(select(func.count(CatalogProduct.id))) or 0
    total_banks = session.scalar(select(func.count(BankRegistry.bank_id)).where(BankRegistry.enabled.is_(True))) or 0
    total_artworks = session.scalar(select(func.count(CatalogArtwork.id))) or 0
    products_with_af = session.scalar(
        select(func.count(CatalogProduct.id)).where(CatalogProduct.annual_fee_primary_minor.is_not(None))
    ) or 0
    products_with_benefits = session.scalar(
        select(func.count(func.distinct(CatalogBenefit.product_id)))
    ) or 0
    last_verified = session.scalar(select(func.max(CatalogProduct.verified_at)))
    pending_count = session.scalar(
        select(func.count(DictionaryChangeRequest.id)).where(DictionaryChangeRequest.status == "PENDING")
    ) or 0

    # Count pending by change_type
    type_counts_rows = session.execute(
        select(DictionaryChangeRequest.change_type, func.count(DictionaryChangeRequest.id))
        .where(DictionaryChangeRequest.status == "PENDING")
        .group_by(DictionaryChangeRequest.change_type)
    ).all()
    changes_by_type = {row[0]: row[1] for row in type_counts_rows}

    return {
        "total_products": total_products,
        "total_banks": total_banks,
        "total_artworks": total_artworks,
        "products_with_annual_fee": products_with_af,
        "products_with_benefits": products_with_benefits,
        "last_verified_at": last_verified,
        "next_scheduled_at": "每年 3 月 / 9 月 (0 3 1 3,9 *)",
        "pending_changes_count": pending_count,
        "changes_by_type": changes_by_type,
    }
