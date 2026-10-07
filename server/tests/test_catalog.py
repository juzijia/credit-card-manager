import json
import secrets
import pytest
from fastapi.testclient import TestClient

from app.core.plain_secrets import set_secret
from app.models.catalog import (
    BankRegistry,
    CatalogArtwork,
    CatalogBenefit,
    CatalogProduct,
    CatalogSource,
    DictionaryChangeRequest,
)
from app.schemas.catalog import CardExtractCandidate
from app.services.catalog_diff import diff_card_candidate, normalize_text


def test_normalize_text_and_no_change_diff():
    assert normalize_text(" 招商银行 经典白金卡。 ") == "招商银行 经典白金卡"
    assert normalize_text("全币种 (国际) 信用卡；") == "全币种 (国际) 信用卡"

    candidate = CardExtractCandidate.model_validate({
        "bank_id": "CMB",
        "canonical_name": "经典白金卡",
        "aliases": ["经典白"],
        "networks": ["UnionPay"],
        "card_level": "Platinum",
        "status": "active",
        "annual_fee": {
            "primary": 360000,
            "supplementary": 200000,
            "waiver_description": "主卡年费3600元，10000积分兑换",
        },
        "benefits": [
            {
                "category": "出行",
                "title": "贵宾厅",
                "description": "每年6次全流程贵宾厅",
                "quota": "6次/年",
                "cycle": "核卡年",
                "source_url": "https://cmb.test/classic",
            }
        ],
        "artworks": [
            {
                "artwork_url": "https://cmb.test/art.png",
                "image_hash": "hash123456",
                "is_primary": True,
            }
        ],
        "sources": [
            {"title": "官网", "url": "https://cmb.test", "tier": "S"}
        ],
        "confidence": 1.0,
    })

    current = {
        "status": "active",
        "annual_fee_primary_minor": 360000,
        "annual_fee_supp_minor": 200000,
        "annual_fee_waiver_desc": "主卡年费3600元，10000积分兑换。",
        "benefits": [
            {
                "category": "出行",
                "title": "贵宾厅",
                "description": "每年6次全流程贵宾厅。",
                "quota": "6次/年",
                "cycle": "核卡年",
            }
        ],
        "artworks": [
            {"image_hash": "hash123456"}
        ],
    }

    diffs = diff_card_candidate(candidate, current)
    assert diffs == []


def test_diff_engine_detects_all_required_change_types():
    # 1. New card
    cand_new = CardExtractCandidate.model_validate({
        "bank_id": "CMB",
        "canonical_name": "新版自由人生白金卡",
        "status": "active",
        "annual_fee": {"primary": 80000},
        "confidence": 0.95,
    })
    diff_new = diff_card_candidate(cand_new, None)
    assert len(diff_new) == 1
    assert diff_new[0]["change_type"] == "ADD_CARD"

    # 2. Low confidence -> NEEDS_REVIEW
    cand_low_conf = CardExtractCandidate.model_validate({
        "bank_id": "CMB",
        "canonical_name": "可疑联名卡",
        "status": "active",
        "confidence": 0.45,
    })
    diff_low = diff_card_candidate(cand_low_conf, None)
    assert len(diff_low) == 1
    assert diff_low[0]["change_type"] == "NEEDS_REVIEW"

    # 3. Discontinue card
    current_prod = {
        "status": "active",
        "annual_fee_primary_minor": 10000,
        "annual_fee_waiver_desc": "刷6次免次年",
        "benefits": [],
        "artworks": [{"image_hash": "hash_old"}],
    }
    cand_discont = CardExtractCandidate.model_validate({
        "bank_id": "CMB",
        "canonical_name": "停发卡",
        "status": "discontinued",
        "annual_fee": {"primary": 10000, "waiver_description": "刷6次免次年"},
        "confidence": 0.99,
    })
    diff_disc = diff_card_candidate(cand_discont, current_prod)
    assert any(d["change_type"] == "DISCONTINUE_CARD" for d in diff_disc)

    # 4. Update annual fee & waiver desc
    cand_fee = CardExtractCandidate.model_validate({
        "bank_id": "CMB",
        "canonical_name": "普通卡",
        "status": "active",
        "annual_fee": {"primary": 30000, "waiver_description": "每年刷卡12次免次年"},
        "confidence": 0.99,
    })
    diff_fee = diff_card_candidate(cand_fee, current_prod)
    assert any(d["change_type"] == "UPDATE_ANNUAL_FEE" for d in diff_fee)

    # 5. Add new core benefit & artwork
    cand_benefit_art = CardExtractCandidate.model_validate({
        "bank_id": "CMB",
        "canonical_name": "权益升级卡",
        "status": "active",
        "annual_fee": {"primary": 10000, "waiver_description": "刷6次免次年"},
        "benefits": [
            {
                "category": "健康",
                "title": "年度体检",
                "description": "每年免费体检一次",
                "quota": "1次/年",
            }
        ],
        "artworks": [
            {"artwork_url": "https://img.test/new.png", "image_hash": "hash_new_art"}
        ],
        "confidence": 0.95,
    })
    diff_ben = diff_card_candidate(cand_benefit_art, current_prod)
    types = {d["change_type"] for d in diff_ben}
    assert "UPDATE_BENEFIT" in types
    assert "ADD_ARTWORK" in types


def test_fixture_scanner_and_approval_lifecycle(client: TestClient, auth_headers: dict[str, str]):
    with client.app.state.session_factory() as s:
        s.query(CatalogArtwork).delete()
        s.query(CatalogBenefit).delete()
        s.query(CatalogSource).delete()
        s.query(CatalogProduct).delete()
        s.commit()

    # 1. Trigger scan run
    scan_resp = client.post(
        "/api/v1/catalog/scan-runs",
        headers=auth_headers,
        json={"scope": "FULL"},
    )
    assert scan_resp.status_code == 200
    run_data = scan_resp.json()
    assert run_data["status"] == "COMPLETED"
    assert run_data["total_banks"] >= 2
    assert run_data["discovered_changes"] > 0

    # 2. Check stats endpoint
    stats_resp = client.get("/api/v1/catalog/stats", headers=auth_headers)
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats["pending_changes_count"] > 0
    assert "ADD_CARD" in stats["changes_by_type"]

    # 3. List change requests
    crs_resp = client.get("/api/v1/catalog/change-requests", headers=auth_headers)
    assert crs_resp.status_code == 200
    crs = crs_resp.json()
    assert len(crs) > 0
    add_req = next(c for c in crs if c["change_type"] == "ADD_CARD")
    assert add_req["status"] == "PENDING"
    assert add_req["confidence"] >= 0.9

    # 4. Owner approves the change request
    approve_resp = client.post(
        f"/api/v1/catalog/change-requests/{add_req['id']}/approve",
        headers=auth_headers,
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "APPROVED"

    # 5. Verify formal catalog product created in Source of Truth
    prods_resp = client.get("/api/v1/catalog/products", headers=auth_headers)
    assert prods_resp.status_code == 200
    prods = prods_resp.json()
    matched = next((p for p in prods if p["canonical_name"] == add_req["target_product_name"]), None)
    assert matched is not None
    assert matched["verified_at"] is not None

    # 6. Rejecting or approving non-pending returns error
    dup_approve = client.post(
        f"/api/v1/catalog/change-requests/{add_req['id']}/approve",
        headers=auth_headers,
    )
    assert dup_approve.status_code == 422


def test_agent_can_ingest_candidates_but_cannot_approve(client: TestClient):
    token_str = secrets.token_urlsafe(32)
    set_secret("agent", "token", token_str)

    agent_headers = {"Authorization": f"Bearer {token_str}"}

    # 1. Agent calls ingest
    payload = {
        "bank_id": "ICBC",
        "candidates": [
            {
                "bank_id": "ICBC",
                "canonical_name": "工银奋斗信用卡",
                "aliases": ["奋斗卡"],
                "networks": ["UnionPay"],
                "status": "active",
                "annual_fee": {"primary": 20000, "waiver_description": "消费5笔免当年"},
                "confidence": 0.99,
                "sources": [{"title": "工行官网", "url": "https://icbc.com.cn", "tier": "S"}],
            }
        ],
    }
    ingest_res = client.post("/api/v1/catalog/ingest", headers=agent_headers, json=payload)
    assert ingest_res.status_code == 200
    data = ingest_res.json()
    assert data["discovered_changes"] == 1
    cr_id = data["change_request_ids"][0]

    # 2. Agent tries to approve -> 403 Forbidden!
    approve_res = client.post(
        f"/api/v1/catalog/change-requests/{cr_id}/approve",
        headers=agent_headers,
    )
    assert approve_res.status_code == 403

    # 3. Agent cannot directly call batch review -> 403 Forbidden!
    batch_res = client.post(
        "/api/v1/catalog/change-requests/batch",
        headers=agent_headers,
        json={"request_ids": [cr_id], "action": "APPROVE"},
    )
    assert batch_res.status_code == 403


def test_bank_service_information_endpoint(client: TestClient, auth_headers: dict[str, str]):
    resp = client.get("/api/v1/catalog/banks", headers=auth_headers)
    assert resp.status_code == 200
    banks = resp.json()
    assert len(banks) >= 14
    cmb = next(b for b in banks if b["bank_id"] == "CMB")
    assert "400-820-5555" in cmb["customer_service_phone"]
    assert "400-888-5555" in cmb["vip_service_phone"]
    assert cmb["service_info_source_url"] is not None
    assert cmb["service_info_verified_at"] is not None


def test_update_annual_fee_patch_semantics_does_not_clear_existing_fields(
    client: TestClient, auth_headers: dict
) -> None:
    # 1. Initialize catalog and add a card product with full annual fee details
    with client.app.state.session_factory() as session:
        bank = session.get(BankRegistry, "CMB")
        if not bank:
            bank = BankRegistry(
                bank_id="CMB",
                bank_name="招商银行",
                official_homepage="https://www.cmbchina.com",
                credit_card_catalog_url="https://market.cmbchina.com",
            )
            session.add(bank)
        prod = CatalogProduct(
            id="test-prod-fee-patch",
            bank_id="CMB",
            canonical_name="招商经典白金卡",
            annual_fee_primary_minor=80000,
            annual_fee_supp_minor=40000,
            annual_fee_waiver_desc="刷6次免次年",
            status="active",
        )
        session.add(prod)
        session.commit()

    # 2. Case A: Candidate only provides new waiver_desc; primary and supp are None
    req_id_a = "cr-test-patch-fee-a"
    req_id_b = "cr-test-patch-fee-b"

    with client.app.state.session_factory() as session:
        cr_a = DictionaryChangeRequest(
            id=req_id_a,
            bank_id="CMB",
            change_type="UPDATE_ANNUAL_FEE",
            target_product_name="招商经典白金卡",
            old_value_json=json.dumps({
                "annual_fee_primary_minor": 80000,
                "annual_fee_supp_minor": 40000,
                "annual_fee_waiver_desc": "刷6次免次年",
            }),
            new_value_json=json.dumps({
                "annual_fee_primary_minor": None,
                "annual_fee_supp_minor": None,
                "annual_fee_waiver_desc": "刷12次免次年",
            }),
            source_url="https://cmb.example.com",
            source_title="招行信用卡官网",
            evidence="AI 仅提取到减免政策",
            confidence=0.9,
            status="PENDING",
        )
        session.add(cr_a)
        session.commit()

    # Approve request A
    app_a = client.post(f"/api/v1/catalog/change-requests/{req_id_a}/approve", headers=auth_headers)
    assert app_a.status_code == 200, app_a.text

    # Verify primary and supp remain intact, while waiver_desc is updated
    with client.app.state.session_factory() as session:
        p = session.get(CatalogProduct, "test-prod-fee-patch")
        assert p.annual_fee_primary_minor == 80000
        assert p.annual_fee_supp_minor == 40000
        assert p.annual_fee_waiver_desc == "刷12次免次年"

    # 3. Case B: Candidate only updates primary fee; supp and waiver_desc are None
    with client.app.state.session_factory() as session:
        cr_b = DictionaryChangeRequest(
            id=req_id_b,
            bank_id="CMB",
            change_type="UPDATE_ANNUAL_FEE",
            target_product_name="招商经典白金卡",
            old_value_json=json.dumps({
                "annual_fee_primary_minor": 80000,
                "annual_fee_supp_minor": 40000,
                "annual_fee_waiver_desc": "刷12次免次年",
            }),
            new_value_json=json.dumps({
                "annual_fee_primary_minor": 90000,
                "annual_fee_supp_minor": None,
                "annual_fee_waiver_desc": None,
            }),
            source_url="https://cmb.example.com",
            source_title="招行信用卡官网",
            evidence="仅更新主卡年费",
            confidence=0.95,
            status="PENDING",
        )
        session.add(cr_b)
        session.commit()

    app_b = client.post(f"/api/v1/catalog/change-requests/{req_id_b}/approve", headers=auth_headers)
    assert app_b.status_code == 200, app_b.text

    with client.app.state.session_factory() as session:
        p = session.get(CatalogProduct, "test-prod-fee-patch")
        assert p.annual_fee_primary_minor == 90000
        assert p.annual_fee_supp_minor == 40000
        assert p.annual_fee_waiver_desc == "刷12次免次年"
