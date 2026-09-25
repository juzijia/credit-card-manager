import re
import unicodedata
from typing import Any

from app.schemas.catalog import CardExtractCandidate


def normalize_text(text: str | None) -> str:
    """Normalize string for diffing: Unicode NFKC, strip whitespace/punctuation variance, lower."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    # collapse consecutive whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()
    # remove minor decorative punctuation differences (e.g. trailing period)
    normalized = normalized.rstrip(".。;；,，")
    return normalized


def normalize_money(minor: int | None) -> int | None:
    return minor if minor is not None and minor >= 0 else None


def diff_card_candidate(
    candidate: CardExtractCandidate,
    current_product: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Compare candidate with current verified product data and return minimal field-level change requests.
    If no change, returns empty list.
    """
    changes: list[dict[str, Any]] = []
    bank_id = candidate.bank_id.upper()
    canonical_name = candidate.canonical_name.strip()

    # Rule: Check AI confidence
    if candidate.confidence < 0.6:
        changes.append({
            "bank_id": bank_id,
            "change_type": "NEEDS_REVIEW",
            "target_product_name": canonical_name,
            "old_value": None,
            "new_value": candidate.model_dump(),
            "source_url": candidate.sources[0].url if candidate.sources else "",
            "source_title": candidate.sources[0].title if candidate.sources else "Low confidence candidate",
            "evidence": f"AI extraction confidence is low ({candidate.confidence:.2f}). Needs manual review.",
            "confidence": candidate.confidence,
        })
        return changes

    # 1. Product existence
    if not current_product:
        # Check if discontinued from the start
        change_type = "ADD_CARD"
        primary_source = candidate.sources[0] if candidate.sources else None
        changes.append({
            "bank_id": bank_id,
            "change_type": change_type,
            "target_product_name": canonical_name,
            "old_value": None,
            "new_value": {
                "canonical_name": canonical_name,
                "aliases": candidate.aliases,
                "networks": candidate.networks,
                "card_level": candidate.card_level,
                "status": candidate.status,
                "annual_fee": candidate.annual_fee.model_dump(),
                "benefits": [b.model_dump() for b in candidate.benefits],
                "artworks": [a.model_dump() for a in candidate.artworks],
            },
            "source_url": primary_source.url if primary_source else "",
            "source_title": primary_source.title if primary_source else "Official Catalog",
            "evidence": f"Discovered new card product: {canonical_name}",
            "confidence": candidate.confidence,
        })
        return changes

    # Product exists: Check field-level diffs
    primary_source = candidate.sources[0] if candidate.sources else None
    src_url = primary_source.url if primary_source else ""
    src_title = primary_source.title if primary_source else "Official Catalog"

    # Status: active vs discontinued
    cur_status = current_product.get("status", "active")
    if candidate.status in ("discontinued", "inactive") and cur_status == "active":
        changes.append({
            "bank_id": bank_id,
            "change_type": "DISCONTINUE_CARD",
            "target_product_name": canonical_name,
            "old_value": {"status": cur_status},
            "new_value": {"status": candidate.status},
            "source_url": src_url,
            "source_title": src_title,
            "evidence": f"Card product {canonical_name} marked as discontinued/inactive by source.",
            "confidence": candidate.confidence,
        })

    # Annual fee: primary, supp, waiver description
    cur_af_prim = current_product.get("annual_fee_primary_minor")
    cur_af_supp = current_product.get("annual_fee_supp_minor")
    cur_af_desc = current_product.get("annual_fee_waiver_desc")

    cand_af_prim = candidate.annual_fee.primary
    cand_af_supp = candidate.annual_fee.supplementary
    cand_af_desc = candidate.annual_fee.waiver_description

    af_differs = False
    if cand_af_prim is not None and cand_af_prim != cur_af_prim:
        af_differs = True
    if cand_af_supp is not None and cand_af_supp != cur_af_supp:
        af_differs = True
    if cand_af_desc and normalize_text(cand_af_desc) != normalize_text(cur_af_desc):
        af_differs = True

    if af_differs:
        changes.append({
            "bank_id": bank_id,
            "change_type": "UPDATE_ANNUAL_FEE",
            "target_product_name": canonical_name,
            "old_value": {
                "annual_fee_primary_minor": cur_af_prim,
                "annual_fee_supp_minor": cur_af_supp,
                "annual_fee_waiver_desc": cur_af_desc,
            },
            "new_value": {
                "annual_fee_primary_minor": cand_af_prim,
                "annual_fee_supp_minor": cand_af_supp,
                "annual_fee_waiver_desc": cand_af_desc,
            },
            "source_url": src_url,
            "source_title": src_title,
            "evidence": f"Annual fee policy updated for {canonical_name}",
            "confidence": candidate.confidence,
        })

    # Benefits: Compare long-term benefits
    cur_benefits = current_product.get("benefits", [])
    # Index current benefits by normalized title & category
    cur_ben_map = {
        (normalize_text(b.get("category")), normalize_text(b.get("title"))): b
        for b in cur_benefits
    }
    cand_ben_map = {
        (normalize_text(b.category), normalize_text(b.title)): b
        for b in candidate.benefits
    }

    # Detect added or updated benefits
    for key, cand_b in cand_ben_map.items():
        if key not in cur_ben_map:
            changes.append({
                "bank_id": bank_id,
                "change_type": "UPDATE_BENEFIT",
                "target_product_name": canonical_name,
                "old_value": None,
                "new_value": cand_b.model_dump(),
                "source_url": cand_b.source_url or src_url,
                "source_title": src_title,
                "evidence": f"Added new core benefit: [{cand_b.category}] {cand_b.title}",
                "confidence": candidate.confidence,
            })
        else:
            cur_b = cur_ben_map[key]
            # Compare description, quota, cycle
            if (
                normalize_text(cand_b.description) != normalize_text(cur_b.get("description"))
                or normalize_text(cand_b.quota) != normalize_text(cur_b.get("quota"))
                or normalize_text(cand_b.cycle) != normalize_text(cur_b.get("cycle"))
            ):
                changes.append({
                    "bank_id": bank_id,
                    "change_type": "UPDATE_BENEFIT",
                    "target_product_name": canonical_name,
                    "old_value": cur_b,
                    "new_value": cand_b.model_dump(),
                    "source_url": cand_b.source_url or src_url,
                    "source_title": src_title,
                    "evidence": f"Updated core benefit terms: [{cand_b.category}] {cand_b.title}",
                    "confidence": candidate.confidence,
                })

    # Artworks: Compare image hashes
    cur_artworks = current_product.get("artworks", [])
    cur_hashes = {a.get("image_hash") for a in cur_artworks if a.get("image_hash")}
    for cand_art in candidate.artworks:
        if cand_art.image_hash and cand_art.image_hash not in cur_hashes:
            changes.append({
                "bank_id": bank_id,
                "change_type": "ADD_ARTWORK",
                "target_product_name": canonical_name,
                "old_value": None,
                "new_value": cand_art.model_dump(),
                "source_url": cand_art.artwork_url,
                "source_title": src_title,
                "evidence": f"Discovered new card artwork image hash {cand_art.image_hash[:10]}",
                "confidence": candidate.confidence,
            })

    return changes

