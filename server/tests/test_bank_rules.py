from app.models.card import Card
from app.services import bank_rules
from app.services.bank_rules import match_product_rule, validate_bank_rules
from app.services.card_products import apply_product_dictionary


def test_dictionary_has_no_ambiguous_grace_period_fields() -> None:
    rules = bank_rules.get_bank_rules()
    assert all("default_grace_days" not in bank for bank in rules.values())
    assert all("payment_due_rule" not in bank for bank in rules.values())
    assert all(
        "grace_days" not in product
        for bank in rules.values()
        for product in bank.get("products", {}).values()
    )


def test_product_matching_uses_exact_alias_not_fuzzy_substring() -> None:
    matched = match_product_rule("CGB", "高铁白")
    assert matched is not None
    assert matched[0] == "臻尚白金卡 (高铁白)"
    assert match_product_rule("CGB", "高铁") is None


def test_dictionary_updates_its_own_values_but_preserves_manual_values() -> None:
    dictionary_card = Card(
        bank_code="CMB",
        bank_name="招商银行",
        display_name="测试卡",
        last4="1234",
        card_product="经典白金卡",
        currency="CNY",
    )
    assert apply_product_dictionary(dictionary_card) is True
    assert dictionary_card.annual_fee_minor == 360000
    assert dictionary_card.annual_fee_source == "dictionary"

    manual_card = Card(
        bank_code="CMB",
        bank_name="招商银行",
        display_name="手工卡",
        last4="5678",
        card_product="经典白金卡",
        currency="CNY",
        annual_fee_minor=8800,
        annual_fee_policy="手工确认",
        annual_fee_source="manual",
    )
    assert apply_product_dictionary(manual_card) is False
    assert manual_card.annual_fee_minor == 8800
    assert manual_card.annual_fee_policy == "手工确认"


def test_rule_dictionary_import_is_validated_and_atomically_reloaded(
    tmp_path, monkeypatch
) -> None:
    payload = {
        "TST": {
            "bank_name": "测试银行",
            "products": {
                "测试白金卡": {
                    "annual_fee_minor": 50000,
                    "policy": "测试政策",
                    "aliases": ["测试白"],
                }
            },
        }
    }
    assert validate_bank_rules(payload)["TST"]["bank_name"] == "测试银行"

    original_path = bank_rules._RULES_PATH
    with monkeypatch.context() as scoped:
        scoped.setattr(bank_rules, "_RULES_PATH", tmp_path / "bank_rules.json")
        scoped.setattr(bank_rules, "_CACHE", None)
        bank_rules.import_bank_rules(payload, merge=False)
        assert bank_rules.get_bank_rules() == payload
        assert match_product_rule("TST", "测试白")[0] == "测试白金卡"
        updated = {
            "TST": {
                "bank_name": "测试银行",
                "products": {
                    "测试白金卡": {
                        "annual_fee_minor": 60000,
                        "policy": "更新后的政策",
                        "aliases": ["测试白"],
                    }
                },
            }
        }
        bank_rules.import_bank_rules(updated, merge=True)
        assert bank_rules.get_bank_rules()["TST"]["bank_name"] == "测试银行"
        assert match_product_rule("TST", "测试白")[1]["annual_fee_minor"] == 60000
        assert not (tmp_path / "bank_rules.json.tmp").exists()

    bank_rules._RULES_PATH = original_path
    bank_rules.reload_bank_rules()
