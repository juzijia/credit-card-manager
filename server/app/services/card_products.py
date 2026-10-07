import json
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.card import Card
from app.models.catalog import CatalogProduct


def _normalized_product(value: str) -> str:
    return re.sub(
        r"[\s()（）/／·._-]+", "", unicodedata.normalize("NFKC", value).casefold()
    )


def _product_aliases(product: CatalogProduct) -> set[str]:
    name = product.canonical_name
    candidates = {name}
    candidates.update(part.strip() for part in re.split(r"[/／]", name) if part.strip())
    candidates.update(
        part.strip()
        for part in re.findall(r"[（(]([^）)]+)[）)]", name)
        if part.strip()
    )
    candidates.add(re.sub(r"\s*[（(][^）)]*[）)]\s*", "", name).strip())
    try:
        stored_aliases = json.loads(product.aliases_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        stored_aliases = []
    if isinstance(stored_aliases, list):
        candidates.update(str(item).strip() for item in stored_aliases if str(item).strip())
    return {_normalized_product(item) for item in candidates if item}


def match_catalog_product(
    session: Session, bank_code: str, card_product: str | None
) -> CatalogProduct | None:
    """Match one catalog product by exact normalized canonical name or alias."""
    if not card_product:
        return None
    target = _normalized_product(card_product)
    products = session.scalars(
        select(CatalogProduct).where(CatalogProduct.bank_id == bank_code.upper())
    ).all()
    matches = [product for product in products if target in _product_aliases(product)]
    return matches[0] if len(matches) == 1 else None


def apply_product_dictionary(card: Card, session: Session) -> bool:
    """Apply catalog annual-fee facts without overriding user-maintained values."""
    product = match_catalog_product(session, card.bank_code, card.card_product)
    if product is None:
        return False

    changed = False
    if card.annual_fee_source == "manual":
        return changed
    if card.annual_fee_source not in (None, "dictionary"):
        if card.annual_fee_policy is None and isinstance(product.annual_fee_waiver_desc, str):
            card.annual_fee_policy = product.annual_fee_waiver_desc
            changed = True
        return changed

    # Existing unlabelled values predate source tracking and are treated as
    # manual to avoid silently replacing user-maintained data.
    if card.annual_fee_source is None and any(
        value is not None
        for value in (
            card.annual_fee_minor,
            card.annual_fee_policy,
            card.annual_fee_charge_date,
        )
    ):
        card.annual_fee_source = "manual"
        return changed

    fee = product.annual_fee_primary_minor
    policy = product.annual_fee_waiver_desc
    if isinstance(fee, int) and card.annual_fee_minor != fee:
        card.annual_fee_minor = fee
        changed = True
    if isinstance(policy, str) and card.annual_fee_policy != policy:
        card.annual_fee_policy = policy
        changed = True
    if changed or card.annual_fee_source == "dictionary":
        card.annual_fee_source = "dictionary"
    return changed


def refresh_dictionary_cards(session: Session) -> int:
    """Refresh catalog-managed annual-fee fields for existing non-deleted cards."""
    updated = 0
    cards = session.scalars(select(Card).where(Card.deleted_at.is_(None))).all()
    for card in cards:
        if apply_product_dictionary(card, session):
            updated += 1
    session.commit()
    return updated
