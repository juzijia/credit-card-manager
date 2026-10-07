from fastapi.testclient import TestClient

from app.models.card import Card
from app.services.card_products import apply_product_dictionary, match_catalog_product


def test_catalog_product_matching_uses_exact_alias_not_fuzzy_substring(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        matched = match_catalog_product(session, "CGB", "高铁白")
        assert matched is not None
        assert matched.canonical_name == "臻尚白金卡 (高铁白)"
        assert match_catalog_product(session, "CGB", "高铁") is None


def test_catalog_dictionary_updates_its_own_values_but_preserves_manual_values(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        dictionary_card = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="测试卡",
            last4="1234",
            card_product="经典白金卡",
            currency="CNY",
        )
        assert apply_product_dictionary(dictionary_card, session) is True
        assert dictionary_card.annual_fee_minor == 360000
        assert dictionary_card.annual_fee_policy == "主卡年费3600元，可用10000永久积分兑换主卡年费"
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
        assert apply_product_dictionary(manual_card, session) is False
        assert manual_card.annual_fee_minor == 8800
        assert manual_card.annual_fee_policy == "手工确认"


def test_catalog_alias_preserves_unambiguous_legacy_product_name(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        product = match_catalog_product(session, "SPDB", "浦发美国运通白金卡 (AE白)")
        assert product is not None
        assert product.canonical_name == "浦发美国运通白金信用卡"
