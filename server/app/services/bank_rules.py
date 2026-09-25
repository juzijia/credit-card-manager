import json
import re
import unicodedata
from pathlib import Path
from typing import Any

_RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "bank_rules.json"
_CACHE: dict[str, Any] | None = None
_LAST_MTIME: float = 0.0


def _normalize_money_fields(rules: dict[str, Any]) -> dict[str, Any]:
    """Upgrade pre-P1 rule files from whole CNY values at the read boundary."""
    for bank in rules.values():
        for product in bank.get("products", {}).values():
            if "annual_fee_minor" not in product and "annual_fee_cny" in product:
                product["annual_fee_minor"] = product.pop("annual_fee_cny") * 100
    return rules


def get_bank_rules() -> dict[str, Any]:
    global _CACHE, _LAST_MTIME
    current_mtime = _RULES_PATH.stat().st_mtime if _RULES_PATH.exists() else 0.0
    if _CACHE is None or current_mtime != _LAST_MTIME:
        if _RULES_PATH.exists():
            try:
                _CACHE = _normalize_money_fields(json.loads(_RULES_PATH.read_text(encoding="utf-8")))
                _LAST_MTIME = current_mtime
            except Exception:
                _CACHE = {}
        else:
            _CACHE = {}
    return _CACHE


def reload_bank_rules() -> dict[str, Any]:
    global _CACHE
    _CACHE = None
    return get_bank_rules()


def get_rule_for_bank(bank_code: str) -> dict[str, Any] | None:
    rules = get_bank_rules()
    return rules.get(bank_code.upper())


def _normalized_product(value: str) -> str:
    return re.sub(
        r"[\s()（）/／·._-]+", "", unicodedata.normalize("NFKC", value).casefold()
    )


def _product_aliases(name: str, rule: dict[str, Any]) -> set[str]:
    candidates = {name}
    candidates.update(
        part.strip() for part in re.split(r"[/／]", name) if part.strip()
    )
    candidates.update(
        part.strip()
        for part in re.findall(r"[（(]([^）)]+)[）)]", name)
        if part.strip()
    )
    candidates.add(re.sub(r"\s*[（(][^）)]*[）)]\s*", "", name).strip())
    candidates.update(str(item).strip() for item in rule.get("aliases", ()))
    return {_normalized_product(item) for item in candidates if item}


def match_product_rule(
    bank_code: str, card_product: str | None
) -> tuple[str, dict[str, Any]] | None:
    """Match exact normalized names/aliases only; ambiguous aliases do not match."""
    if not card_product:
        return None
    bank = get_rule_for_bank(bank_code)
    products = bank.get("products", {}) if bank else {}
    target = _normalized_product(card_product)
    matches = [
        (name, rule)
        for name, rule in products.items()
        if target in _product_aliases(name, rule)
    ]
    return matches[0] if len(matches) == 1 else None


def validate_bank_rules(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Bank rule dictionary must be a non-empty object")
    if len(payload) > 100:
        raise ValueError("Bank rule dictionary contains too many banks")
    normalized: dict[str, Any] = {}
    for raw_code, raw_bank in payload.items():
        code = str(raw_code).upper()
        if not re.fullmatch(r"[A-Z0-9]{2,16}", code) or not isinstance(raw_bank, dict):
            raise ValueError(f"Invalid bank rule: {raw_code}")
        bank = dict(raw_bank)
        bank_name = bank.get("bank_name")
        if not isinstance(bank_name, str) or not bank_name.strip() or len(bank_name) > 64:
            raise ValueError(f"Invalid bank_name for {code}")
        products = bank.get("products", {})
        if not isinstance(products, dict) or len(products) > 500:
            raise ValueError(f"Invalid products for {code}")
        clean_products: dict[str, Any] = {}
        for raw_name, raw_product in products.items():
            if (
                not isinstance(raw_name, str)
                or not raw_name.strip()
                or len(raw_name) > 64
                or not isinstance(raw_product, dict)
            ):
                raise ValueError(f"Invalid product in {code}")
            product = dict(raw_product)
            fee = product.get("annual_fee_minor")
            if fee is not None and (
                not isinstance(fee, int) or not 0 <= fee <= 1_000_000
            ):
                raise ValueError(f"Invalid annual_fee_minor for {code}/{raw_name}")
            policy = product.get("policy")
            if policy is not None and (
                not isinstance(policy, str) or len(policy) > 256
            ):
                raise ValueError(f"Invalid policy for {code}/{raw_name}")
            aliases = product.get("aliases", [])
            if not isinstance(aliases, list) or any(
                not isinstance(item, str) or not item.strip() or len(item) > 64
                for item in aliases
            ):
                raise ValueError(f"Invalid aliases for {code}/{raw_name}")
            clean_products[raw_name.strip()] = product
        bank["bank_name"] = bank_name.strip()
        bank["products"] = clean_products
        normalized[code] = bank
    return normalized


def import_bank_rules(payload: object, *, merge: bool = True) -> dict[str, Any]:
    incoming = validate_bank_rules(payload)
    if merge:
        combined = json.loads(json.dumps(get_bank_rules(), ensure_ascii=False))
        for code, bank in incoming.items():
            existing = combined.get(code, {})
            products = dict(existing.get("products", {}))
            products.update(bank.get("products", {}))
            combined[code] = {**existing, **bank, "products": products}
        rules = validate_bank_rules(combined)
    else:
        rules = incoming
    _RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _RULES_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(_RULES_PATH)
    reload_bank_rules()
    return rules
