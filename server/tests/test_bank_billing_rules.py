import copy
import json
from pathlib import Path
import pytest

from app.services.bank_billing_rules import (
    BankBillingRulesService,
    get_billing_rules_service,
    get_billing_rule,
    resolve_due_date_rule,
    resolve_boundary_rule,
    ResolvedDueDateRule,
    ResolvedBoundaryRule
)


def _build_valid_dummy_dict() -> dict:
    data = {}
    for i in range(32):
        bcode = f"BNK{i:02d}"
        tier = "P0" if i < 18 else "P1"
        data[bcode] = {
            "bank_code": bcode,
            "bank_name": f"Bank {i}",
            "maintenance_scope": "included",
            "maintenance_tier": tier,
            "due_date_rule": {"status": "TODO"},
            "statement_day_policy": {"status": "TODO"},
            "statement_day_boundary": {"status": "TODO", "boundary": "uncertain"},
            "product_overrides": []
        }
    return data


def test_production_maintenance_scope_count() -> None:
    service = get_billing_rules_service()
    included = service.list_included_banks()
    assert len(included) == 32


def test_maintenance_tier_split() -> None:
    service = get_billing_rules_service()
    rules = service._rules
    p0 = [b for b in service.list_included_banks() if rules[b].get("maintenance_tier") == "P0"]
    p1 = [b for b in service.list_included_banks() if rules[b].get("maintenance_tier") == "P1"]
    assert len(p0) == 18
    assert len(p1) == 14


def test_bank_codes_are_unique() -> None:
    service = get_billing_rules_service()
    included = service.list_included_banks()
    assert len(included) == len(set(included))


def test_resolver_never_returns_runtime_rule_for_non_confirmed_facts() -> None:
    # BOCOM / CITIC have no CONFIRMED bank-level facts at all.
    assert resolve_due_date_rule("BOCOM", statement_day=14) is None
    assert resolve_due_date_rule("CITIC", statement_day=5) is None
    # ICBC is multiple_known_rules: only CONFIRMED sub-rules resolve by
    # statement day; days outside both maps stay unresolved.
    assert resolve_due_date_rule("ICBC", statement_day=1) == ResolvedDueDateRule(
        rule_type="fixed_day", rule_value=25, source="bank_default"
    )
    assert resolve_due_date_rule("ICBC", statement_day=14) == ResolvedDueDateRule(
        rule_type="fixed_day", rule_value=1, source="bank_default"
    )
    assert resolve_due_date_rule("ICBC", statement_day=29) is None
    assert resolve_due_date_rule("ICBC", statement_day=31) is None


def test_multiple_known_rules_query_semantics() -> None:
    """ICBC/ABC multiple_known_rules queried by statement day.

    - ICBC current map covers 14..28 (due = next month 1..15).
    - ICBC legacy 1->25 is CONFIRMED and still effective (runtime-resolvable).
    - ABC +25 (account-verified) covers all statement days EXCEPT days
      claimed by the current App map (12..28): the map wins, and the conflict
      keeps those days unresolved at bank layer (card history still may).
    - ABC +19 is an unassigned official candidate: never runtime-assigned.
    """
    # ICBC current map
    for sd, due in [(14, 1), (20, 7), (28, 15)]:
        res = resolve_due_date_rule("ICBC", statement_day=sd)
        assert res == ResolvedDueDateRule(
            rule_type="fixed_day", rule_value=due, source="bank_default"
        )
    # ICBC legacy pair
    assert resolve_due_date_rule("ICBC", statement_day=1) == ResolvedDueDateRule(
        rule_type="fixed_day", rule_value=25, source="bank_default"
    )
    # ABC blanket +25 outside the map's claim
    for sd in [13, 29, 5]:
        assert resolve_due_date_rule("ABC", statement_day=sd) == ResolvedDueDateRule(
            rule_type="days_after_statement", rule_value=25, source="bank_default"
        )
    # ABC map-claimed days conflict with +25 -> unresolved at bank layer
    for sd in [12, 23, 28]:
        assert resolve_due_date_rule("ABC", statement_day=sd) is None
    # +19 must never surface, including on map-claimed days
    assert resolve_due_date_rule("ABC", statement_day=13) is not None
    for res_sd in range(1, 32):
        res = resolve_due_date_rule("ABC", statement_day=res_sd)
        assert res is None or res.rule_value != 19


def test_abc_plus_19_runtime_assignable_metadata_and_validation() -> None:
    """P1: abc_statement_plus_19 must have explicit runtime_assignable: false.

    - Dictionary entry is CONFIRMED, but carries runtime_assignable=False.
    - Loader validates runtime_assignable as a strict boolean.
    """
    service = get_billing_rules_service()
    abc_info = service.get_billing_rule("ABC")
    assert abc_info is not None
    sub_rules = abc_info.get("due_date_rule", {}).get("rules", [])
    r19 = next((r for r in sub_rules if r.get("rule_id") == "abc_statement_plus_19"), None)
    assert r19 is not None
    assert r19.get("status") == "CONFIRMED"
    assert r19.get("runtime_assignable") is False

    # Validation rejects non-boolean runtime_assignable
    invalid_rules = json.loads(json.dumps(service._rules))
    invalid_rules["ABC"]["due_date_rule"]["rules"][1]["runtime_assignable"] = "false"
    test_service = BankBillingRulesService.__new__(BankBillingRulesService)
    test_service._rules = invalid_rules
    with pytest.raises(ValueError, match="runtime_assignable must be a boolean"):
        test_service.validate()


def test_boc_confirmed_due_date_rule_resolutions() -> None:
    # BOC is now CONFIRMED as days_after_statement = 20 for all statement days
    for s_day in [1, 5, 10, 15, 20, 25, 28, 31]:
        res = resolve_due_date_rule("BOC", statement_day=s_day)
        assert res is not None, f"Failed for statement_day={s_day}"
        assert res.rule_type == "days_after_statement"
        assert res.rule_value == 20
        assert res.source == "bank_default"

def test_pab_statement_day_map_resolutions() -> None:
    """PAB restored full 26-entry statement_day_map resolutions."""
    # Representative mapped days
    for s_day, expected_due in [(1, 19), (10, 28), (13, 1), (20, 8), (28, 16)]:
        res = resolve_due_date_rule("PAB", statement_day=s_day)
        assert res == ResolvedDueDateRule(
            rule_type="fixed_day", rule_value=expected_due, source="bank_default"
        )

    # Unlisted days stay unresolved (do not extrapolate)
    for unlisted in [11, 12, 29, 30, 31]:
        assert resolve_due_date_rule("PAB", statement_day=unlisted) is None

    assert resolve_due_date_rule("PAB", statement_day=None) is None


def test_bocom_bank_due_rule_unresolved_with_official_provenance() -> None:
    """BOCOM 2016 notice cancelled uniform +25; bank due rule is unresolved with official provenance."""
    for s_day in [1, 5, 10, 14, 20, 25, 28]:
        assert resolve_due_date_rule("BOCOM", statement_day=s_day) is None
    info = get_billing_rule("BOCOM")
    assert info is not None
    due_rule = info.get("due_date_rule", {})
    assert due_rule.get("status") == "UNRESOLVED"
    assert due_rule.get("source_title") == "关于调整交通银行太平洋信用卡到期还款日规则的公告"
    assert "2016年官方公告取消原账单日起第25天的统一规则" in due_rule.get("evidence", "")


def test_citic_bank_due_rule_unresolved_with_official_provenance() -> None:
    """CITIC cardholder agreement defines due date per statement; bank due rule is unresolved."""
    for s_day in [1, 5, 10, 15, 20, 25]:
        assert resolve_due_date_rule("CITIC", statement_day=s_day) is None
    info = get_billing_rule("CITIC")
    assert info is not None
    due_rule = info.get("due_date_rule", {})
    assert due_rule.get("status") == "UNRESOLVED"
    assert due_rule.get("source_title") == "中信银行信用卡（个人卡）领用合约（2026年第一版）"
    assert "免息还款期最长不超过50天" in due_rule.get("evidence", "")


def test_cgb_bank_due_rule_unresolved_and_boundary_uncertain() -> None:
    """CGB due rule is unresolved; boundary is conservative uncertain (cutoff_dependent in reality)."""
    for s_day in [1, 5, 10, 15, 20, 25]:
        assert resolve_due_date_rule("CGB", statement_day=s_day) is None
    info = get_billing_rule("CGB")
    assert info is not None
    due_rule = info.get("due_date_rule", {})
    assert due_rule.get("source_title") == "广发银行信用卡章程与客户协议（个人卡）"
    bnd = info.get("statement_day_boundary", {})
    assert bnd.get("boundary") == "uncertain"
    assert bnd.get("status") == "UNRESOLVED"
    assert "cutoff_dependent" in bnd.get("evidence", "")
    assert resolve_boundary_rule("CGB") is None


def test_cmb_statement_day_resolutions() -> None:
    res_9 = resolve_due_date_rule("CMB", statement_day=9)
    assert res_9 is not None
    assert res_9.rule_type == "fixed_day"
    assert res_9.rule_value == 27
    assert res_9.source == "bank_default"

    assert resolve_due_date_rule("CMB", statement_day=None) is None
    assert resolve_due_date_rule("CMB", statement_day=99) is None
    assert resolve_due_date_rule("CMB", statement_day=True) is None  # type: ignore


def test_ccb_statement_day_resolutions() -> None:
    res_17 = resolve_due_date_rule("CCB", statement_day=17)
    assert res_17 is not None
    assert res_17.rule_type == "fixed_day"
    assert res_17.rule_value == 6
    assert res_17.source == "bank_default"

    res_9 = resolve_due_date_rule("CCB", statement_day=9)
    assert res_9 is not None
    assert res_9.rule_type == "fixed_day"
    assert res_9.rule_value == 1


def test_njcb_bank_unresolved_and_product_override() -> None:
    assert resolve_due_date_rule("NJCB", statement_day=20) is None
    assert resolve_due_date_rule("NJCB", statement_day=20, card_product="普通金卡") is None

    res_xyf = resolve_due_date_rule("NJCB", statement_day=20, card_product="鑫易分消费信用卡")
    assert res_xyf is not None
    assert res_xyf.rule_type == "days_after_statement"
    assert res_xyf.rule_value == 25
    assert res_xyf.source == "product_override"
    assert res_xyf.product_name == "鑫易分消费信用卡"


def test_confirmed_direct_days_after_statement_resolutions() -> None:
    for bcode in ["CIB", "PSBC", "SPDB", "CMBC", "CEB", "HXB"]:
        res = resolve_due_date_rule(bcode, statement_day=5)
        assert res is not None, f"Failed for {bcode}"
        assert res.rule_type == "days_after_statement"
        assert res.rule_value == 20
        assert res.source == "bank_default"


def test_boundary_rule_resolutions() -> None:
    for bcode in ["CCB", "CIB"]:
        bnd = resolve_boundary_rule(bcode)
        assert bnd is not None, f"Failed for {bcode}"
        assert bnd.boundary == "current_cycle"

    assert resolve_boundary_rule("CGB") is None
    assert resolve_boundary_rule("CMB") is None
    assert resolve_boundary_rule("ICBC") is None
    assert resolve_boundary_rule("PAB") is None
    assert resolve_boundary_rule("UNKNOWN_BANK") is None


# ==============================================================================
# REGRESSION TESTS (Supervisor Review Gate)
# ==============================================================================

def test_maintenance_scope_runtime_gate(tmp_path: Path) -> None:
    """Excluded issuer with synthetic CONFIRMED due and boundary must resolve to None."""
    data = _build_valid_dummy_dict()
    # Add an excluded issuer with synthetic CONFIRMED fact
    data["EXCLUDED_BANK"] = {
        "bank_code": "EXCLUDED_BANK",
        "bank_name": "Excluded Bank",
        "maintenance_scope": "excluded",
        "maintenance_tier": None,
        "due_date_rule": {
            "status": "CONFIRMED",
            "rule_type": "days_after_statement",
            "rule_value": 20
        },
        "statement_day_boundary": {
            "status": "CONFIRMED",
            "boundary": "current_cycle"
        },
        "product_overrides": [
            {
                "product_name": "Special Card",
                "status": "CONFIRMED",
                "rule_type": "fixed_day",
                "rule_value": 15
            }
        ]
    }
    dict_file = tmp_path / "rules_gate.json"
    with open(dict_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    service = BankBillingRulesService(data_path=dict_file)
    assert service.resolve_due_date_rule("EXCLUDED_BANK", statement_day=10) is None
    assert service.resolve_due_date_rule("EXCLUDED_BANK", statement_day=10, card_product="Special Card") is None
    assert service.resolve_boundary_rule("EXCLUDED_BANK") is None


@pytest.mark.parametrize("invalid_fixed_day", [0, 32, -1, 99])
def test_invalid_fixed_day_rejected(tmp_path: Path, invalid_fixed_day: int) -> None:
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "rule_type": "fixed_day",
        "rule_value": invalid_fixed_day
    }
    p = tmp_path / "test.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with pytest.raises(ValueError, match="fixed_day value must be an integer between 1 and 31"):
        BankBillingRulesService(data_path=p)


@pytest.mark.parametrize("invalid_days_after", [0, 101, -5, 200])
def test_invalid_days_after_statement_rejected(tmp_path: Path, invalid_days_after: int) -> None:
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "rule_type": "days_after_statement",
        "rule_value": invalid_days_after
    }
    p = tmp_path / "test.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with pytest.raises(ValueError, match="days_after_statement value must be an integer between 1 and 100"):
        BankBillingRulesService(data_path=p)


@pytest.mark.parametrize("invalid_key", ["0", "32", "-1", "foo"])
def test_invalid_map_key_rejected(tmp_path: Path, invalid_key: str) -> None:
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "policy_representation": "statement_day_map",
        "statement_day_map": {invalid_key: 15}
    }
    p = tmp_path / "test.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with pytest.raises(ValueError):
        BankBillingRulesService(data_path=p)


@pytest.mark.parametrize("invalid_val", [0, 32, -1, 50])
def test_invalid_map_value_rejected(tmp_path: Path, invalid_val: int) -> None:
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "policy_representation": "statement_day_map",
        "statement_day_map": {"10": invalid_val}
    }
    p = tmp_path / "test.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with pytest.raises(ValueError, match="map value due day must be an integer between 1 and 31"):
        BankBillingRulesService(data_path=p)


def test_boolean_values_rejected(tmp_path: Path) -> None:
    # Bool in direct rule value
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "rule_type": "fixed_day",
        "rule_value": True
    }
    p = tmp_path / "test_bool_val.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with pytest.raises(ValueError, match="fixed_day value must be an integer between 1 and 31"):
        BankBillingRulesService(data_path=p)

    # Bool in map value
    data2 = _build_valid_dummy_dict()
    data2["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "policy_representation": "statement_day_map",
        "statement_day_map": {"10": True}
    }
    p2 = tmp_path / "test_bool_map.json"
    with open(p2, "w", encoding="utf-8") as f:
        json.dump(data2, f)
    with pytest.raises(ValueError, match="map value due day must be an integer between 1 and 31"):
        BankBillingRulesService(data_path=p2)


def test_malformed_confirmed_product_override_rejected(tmp_path: Path) -> None:
    # Product override with invalid days_after_statement = 105
    data = _build_valid_dummy_dict()
    data["BNK00"]["product_overrides"] = [
        {
            "product_name": "Test Card",
            "status": "CONFIRMED",
            "rule_type": "days_after_statement",
            "rule_value": 105
        }
    ]
    p = tmp_path / "test_po_105.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with pytest.raises(ValueError, match="days_after_statement value must be an integer between 1 and 100"):
        BankBillingRulesService(data_path=p)

    # Product override with fixed_day = 0
    data2 = _build_valid_dummy_dict()
    data2["BNK00"]["product_overrides"] = [
        {
            "product_name": "Test Card",
            "status": "CONFIRMED",
            "rule_type": "fixed_day",
            "rule_value": 0
        }
    ]
    p2 = tmp_path / "test_po_0.json"
    with open(p2, "w", encoding="utf-8") as f:
        json.dump(data2, f)
    with pytest.raises(ValueError, match="fixed_day value must be an integer between 1 and 31"):
        BankBillingRulesService(data_path=p2)

    # Product override with bool value
    data3 = _build_valid_dummy_dict()
    data3["BNK00"]["product_overrides"] = [
        {
            "product_name": "Test Card",
            "status": "CONFIRMED",
            "rule_type": "fixed_day",
            "rule_value": True
        }
    ]
    p3 = tmp_path / "test_po_bool.json"
    with open(p3, "w", encoding="utf-8") as f:
        json.dump(data3, f)
    with pytest.raises(ValueError, match="fixed_day value must be an integer between 1 and 31"):
        BankBillingRulesService(data_path=p3)


def test_dual_status_model_validation(tmp_path: Path) -> None:
    """Validator rejects invalid review_status or runtime_status."""
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"]["review_status"] = "INVALID_STATUS"
    p = tmp_path / "test_invalid_review_status.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with pytest.raises(ValueError, match="invalid review_status: 'INVALID_STATUS'"):
        BankBillingRulesService(data_path=p)

    data2 = _build_valid_dummy_dict()
    data2["BNK00"]["due_date_rule"]["runtime_status"] = "UNKNOWN_RUNTIME"
    p2 = tmp_path / "test_invalid_runtime_status.json"
    with open(p2, "w", encoding="utf-8") as f:
        json.dump(data2, f)
    with pytest.raises(ValueError, match="invalid runtime_status: 'UNKNOWN_RUNTIME'"):
        BankBillingRulesService(data_path=p2)


def test_held_banks_dual_status_configuration() -> None:
    """Verify all 9 held banks carry the expected dual-status model configuration."""
    service = get_billing_rules_service()
    expected = {
        "ICBC": ("CONFIRMED", "PARTIAL"),
        "ABC": ("CONFIRMED", "PARTIAL"),
        "BOCOM": ("CONFIRMED", "SAFE_UNRESOLVED"),
        "CGB": ("CONFIRMED", "SAFE_UNRESOLVED"),
        "CITIC": ("CONFIRMED", "SAFE_UNRESOLVED"),
        "PAB": ("CONFIRMED", "PARTIAL"),
        "CCB": ("CONFIRMED", "RESOLVED"),
        "BOC": ("CONFIRMED", "RESOLVED"),
        "CMB": ("CONFIRMED", "RESOLVED"),
    }
    for bcode, (exp_review, exp_runtime) in expected.items():
        info = service.get_billing_rule(bcode)
        assert info is not None, f"Missing {bcode}"
        dd = info.get("due_date_rule", {})
        assert dd.get("review_status") == exp_review, f"{bcode} review_status mismatch"
        assert dd.get("runtime_status") == exp_runtime, f"{bcode} runtime_status mismatch"


# ---------------------------------------------------------------------------
# Hardening A — runtime_status is the sole runtime gate
# ---------------------------------------------------------------------------

def _cloned_service_with_dd(bcode: str, dd_overrides: dict, bank_overrides: dict | None = None) -> BankBillingRulesService:
    """Clone the real production dictionary, patch one bank's due_date_rule
    (and optionally other bank-level fields), and return a service instance
    (validation bypassed via __new__, mirroring the existing
    runtime_assignable test pattern)."""
    service = get_billing_rules_service()
    rules = json.loads(json.dumps(service._rules))
    rules[bcode]["due_date_rule"].update(dd_overrides)
    if bank_overrides:
        rules[bcode].update(bank_overrides)
    test_service = BankBillingRulesService.__new__(BankBillingRulesService)
    test_service._rules = rules
    return test_service


def test_runtime_gate_safe_unresolved_blocks_single_rule() -> None:
    """A1: legacy CONFIRMED + valid rule + runtime_status=SAFE_UNRESOLVED -> None."""
    svc = _cloned_service_with_dd("BOC", {"runtime_status": "SAFE_UNRESOLVED"})
    for s_day in [1, 10, 20, 31]:
        assert svc.resolve_due_date_rule("BOC", statement_day=s_day) is None


def test_runtime_gate_conflict_blocks_single_rule() -> None:
    """A2: legacy CONFIRMED + valid rule + runtime_status=CONFLICT -> None."""
    svc = _cloned_service_with_dd("BOC", {"runtime_status": "CONFLICT"})
    for s_day in [1, 10, 20, 31]:
        assert svc.resolve_due_date_rule("BOC", statement_day=s_day) is None


def test_runtime_gate_todo_blocks_single_rule() -> None:
    svc = _cloned_service_with_dd("BOC", {"runtime_status": "TODO"})
    for s_day in [1, 10, 20, 31]:
        assert svc.resolve_due_date_rule("BOC", statement_day=s_day) is None


def test_runtime_gate_blocks_multiple_known_rules_top_level() -> None:
    """A3: multiple_known_rules with CONFIRMED sub-rules must be blocked by a
    non-resolvable top-level runtime_status."""
    svc = _cloned_service_with_dd("ICBC", {"runtime_status": "SAFE_UNRESOLVED"})
    assert svc.resolve_due_date_rule("ICBC", statement_day=1) is None
    assert svc.resolve_due_date_rule("ICBC", statement_day=14) is None
    assert svc.resolve_due_date_rule("ICBC", statement_day=20) is None

    svc_conflict = _cloned_service_with_dd("ICBC", {"runtime_status": "CONFLICT"})
    assert svc_conflict.resolve_due_date_rule("ICBC", statement_day=1) is None


def test_runtime_gate_legacy_fallback_when_runtime_status_absent() -> None:
    """A4: runtime_status missing + legacy status CONFIRMED -> still resolvable."""
    svc = _cloned_service_with_dd("BOC", {"runtime_status": None})
    res = svc.resolve_due_date_rule("BOC", statement_day=10)
    assert res == ResolvedDueDateRule(
        rule_type="days_after_statement", rule_value=20, source="bank_default"
    )

    # Also on a multiple_known_rules bank (ICBC PARTIAL removed -> legacy path)
    svc_icbc = _cloned_service_with_dd("ICBC", {"runtime_status": None})
    assert svc_icbc.resolve_due_date_rule("ICBC", statement_day=1) == ResolvedDueDateRule(
        rule_type="fixed_day", rule_value=25, source="bank_default"
    )


def test_runtime_gate_does_not_block_confirmed_product_override() -> None:
    """A5: bank-level SAFE_UNRESOLVED must not block a confirmed product override."""
    svc = _cloned_service_with_dd(
        "BOCOM",
        {"runtime_status": "SAFE_UNRESOLVED"},
        bank_overrides={
            "product_overrides": [
                {
                    "product_name": "Test Card",
                    "status": "CONFIRMED",
                    "rule_type": "fixed_day",
                    "rule_value": 25,
                }
            ],
        },
    )
    res = svc.resolve_due_date_rule("BOCOM", statement_day=10, card_product="Test Card")
    assert res == ResolvedDueDateRule(
        rule_type="fixed_day", rule_value=25, source="product_override", product_name="Test Card"
    )
    # Bank-level path stays blocked for non-matching products
    assert svc.resolve_due_date_rule("BOCOM", statement_day=10, card_product="Other") is None


# ---------------------------------------------------------------------------
# Hardening C — dual-status semantic invariants in validate()
# ---------------------------------------------------------------------------

def _write_dict(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / name
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return p


def test_invariant_safe_unresolved_contradiction_rejected(tmp_path: Path) -> None:
    """C1/counterexample 1: SAFE_UNRESOLVED must not carry an executable rule."""
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "rule_type": "days_after_statement",
        "rule_value": 25,
        "runtime_status": "SAFE_UNRESOLVED",
    }
    with pytest.raises(ValueError, match="contradicts a bank-level executable"):
        BankBillingRulesService(data_path=_write_dict(tmp_path, "c1.json", data))


def test_invariant_conflict_contradiction_rejected(tmp_path: Path) -> None:
    """C1/counterexample 2: CONFLICT must not carry an executable rule."""
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "rule_type": "fixed_day",
        "rule_value": 10,
        "runtime_status": "CONFLICT",
    }
    with pytest.raises(ValueError, match="contradicts a bank-level executable"):
        BankBillingRulesService(data_path=_write_dict(tmp_path, "c1b.json", data))


def test_invariant_todo_contradiction_rejected(tmp_path: Path) -> None:
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "rule_type": "days_after_statement",
        "rule_value": 25,
        "runtime_status": "TODO",
    }
    with pytest.raises(ValueError, match="contradicts a bank-level executable"):
        BankBillingRulesService(data_path=_write_dict(tmp_path, "c1c.json", data))


def test_invariant_safe_unresolved_statement_day_map_rejected(tmp_path: Path) -> None:
    """C1: SAFE_UNRESOLVED must not carry an executable statement_day_map."""
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "policy_representation": "statement_day_map",
        "statement_day_map": {"10": 25},
        "runtime_status": "SAFE_UNRESOLVED",
    }
    with pytest.raises(ValueError, match="contradicts a bank-level executable"):
        BankBillingRulesService(data_path=_write_dict(tmp_path, "c1d.json", data))


def test_invariant_safe_unresolved_multiple_known_rules_rejected(tmp_path: Path) -> None:
    """C1/counterexample 3: SAFE_UNRESOLVED + CONFIRMED runtime sub-rule."""
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "policy_representation": "multiple_known_rules",
        "rules": [
            {
                "rule_id": "sub_a",
                "status": "CONFIRMED",
                "rule_type": "days_after_statement",
                "rule_value": 25,
            }
        ],
        "runtime_status": "SAFE_UNRESOLVED",
    }
    with pytest.raises(ValueError, match="contradicts a bank-level executable"):
        BankBillingRulesService(data_path=_write_dict(tmp_path, "c1e.json", data))


def test_invariant_resolved_requires_executable_representation(tmp_path: Path) -> None:
    """C2/counterexample 5: RESOLVED with no executable representation."""
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "UNRESOLVED",
        "rule_type": None,
        "rule_value": None,
        "runtime_status": "RESOLVED",
    }
    with pytest.raises(ValueError, match="requires at least one executable"):
        BankBillingRulesService(data_path=_write_dict(tmp_path, "c2.json", data))


def test_invariant_partial_requires_executable_representation(tmp_path: Path) -> None:
    """C3/counterexample 4: PARTIAL with no executable representation."""
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "TODO",
        "runtime_status": "PARTIAL",
    }
    with pytest.raises(ValueError, match="requires at least one executable"):
        BankBillingRulesService(data_path=_write_dict(tmp_path, "c3.json", data))


def test_invariant_resolved_statement_day_map_and_mkr_accepted(tmp_path: Path) -> None:
    """C2: RESOLVED is self-consistent with statement_day_map and
    multiple_known_rules representations (no rule_type/value requirement)."""
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "policy_representation": "statement_day_map",
        "statement_day_map": {"10": 25, "15": 30},
        "runtime_status": "RESOLVED",
    }
    BankBillingRulesService(data_path=_write_dict(tmp_path, "c2b.json", data))

    data2 = _build_valid_dummy_dict()
    data2["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "policy_representation": "multiple_known_rules",
        "rules": [
            {
                "rule_id": "sub_a",
                "status": "CONFIRMED",
                "rule_type": "days_after_statement",
                "rule_value": 25,
            }
        ],
        "runtime_status": "RESOLVED",
    }
    BankBillingRulesService(data_path=_write_dict(tmp_path, "c2c.json", data2))

    # PARTIAL with an executable representation also passes (lower bound only)
    data3 = _build_valid_dummy_dict()
    data3["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "policy_representation": "statement_day_map",
        "statement_day_map": {"10": 25},
        "runtime_status": "PARTIAL",
    }
    BankBillingRulesService(data_path=_write_dict(tmp_path, "c3b.json", data3))


def test_invariant_legacy_no_runtime_status_compatibility(tmp_path: Path) -> None:
    """C4/counterexample 6: legacy dictionaries without runtime_status keep
    the existing legacy status validation and resolve normally."""
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "CONFIRMED",
        "rule_type": "days_after_statement",
        "rule_value": 25,
    }
    svc = BankBillingRulesService(data_path=_write_dict(tmp_path, "c4.json", data))
    res = svc.resolve_due_date_rule("BNK00", statement_day=10)
    assert res == ResolvedDueDateRule(
        rule_type="days_after_statement", rule_value=25, source="bank_default"
    )

    # Legacy TODO with no rule still loads fine
    data2 = _build_valid_dummy_dict()
    svc2 = BankBillingRulesService(data_path=_write_dict(tmp_path, "c4b.json", data2))
    assert svc2.resolve_due_date_rule("BNK00", statement_day=10) is None


def test_invariant_product_override_preserved_under_safe_unresolved(tmp_path: Path) -> None:
    """Counterexample 7: confirmed product override survives a bank-level
    SAFE_UNRESOLVED runtime gate, end-to-end through a validated dictionary."""
    data = _build_valid_dummy_dict()
    data["BNK00"]["due_date_rule"] = {
        "status": "UNRESOLVED",
        "rule_type": None,
        "rule_value": None,
        "runtime_status": "SAFE_UNRESOLVED",
    }
    data["BNK00"]["product_overrides"] = [
        {
            "product_name": "Gold Card",
            "status": "CONFIRMED",
            "rule_type": "days_after_statement",
            "rule_value": 20,
        }
    ]
    svc = BankBillingRulesService(data_path=_write_dict(tmp_path, "c5.json", data))
    res = svc.resolve_due_date_rule("BNK00", statement_day=10, card_product="Gold Card")
    assert res == ResolvedDueDateRule(
        rule_type="days_after_statement",
        rule_value=20,
        source="product_override",
        product_name="Gold Card",
    )
    # Bank-level path remains blocked
    assert svc.resolve_due_date_rule("BNK00", statement_day=10) is None
