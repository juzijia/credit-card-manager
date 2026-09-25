from app.models.card import Card
from app.services.bank_rules import match_product_rule


def apply_product_dictionary(card: Card) -> bool:
    """Apply a matched product rule without overriding user-maintained values."""
    matched = match_product_rule(card.bank_code, card.card_product)
    if matched is None:
        return False
    _, rule = matched

    changed = False

    if card.annual_fee_source == "manual":
        return changed
    if card.annual_fee_source not in (None, "dictionary"):
        if card.annual_fee_policy is None and isinstance(rule.get("policy"), str):
            card.annual_fee_policy = rule["policy"]
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

    fee = rule.get("annual_fee_minor")
    policy = rule.get("policy")
    if isinstance(fee, int) and card.annual_fee_minor != fee:
        card.annual_fee_minor = fee
        changed = True
    if isinstance(policy, str) and card.annual_fee_policy != policy:
        card.annual_fee_policy = policy
        changed = True
    if changed or card.annual_fee_source == "dictionary":
        card.annual_fee_source = "dictionary"
    return changed
