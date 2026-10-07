from datetime import date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.manual_bill import ManualBill, ManualBillPayment
from app.models.mortgage import MortgagePlan
from app.services.mortgage import (
    calculate_equal_payment_schedule,
    calculate_equal_principal_schedule,
    generate_plan_schedule,
    get_plan_period_for_date,
)


@pytest.fixture(autouse=True)
def _freeze_mortgage_date(monkeypatch):
    frozen_date = date(2026, 9, 15)
    for target in (
        "app.services.statements.local_today",
        "app.api.v1.mortgage.local_today",
        "app.api.v1.manual_bills.local_today",
        "app.api.v1.dashboard.local_today",
    ):
        monkeypatch.setattr(target, lambda *args, **kwargs: frozen_date)


def test_mortgage_calculation_services() -> None:
    # 100万元, 30年(360期), 3.2%
    start = date(2026, 10, 1)
    sched_payment = calculate_equal_payment_schedule(100_000_000, 3.2, 360, start)
    assert len(sched_payment) == 360
    assert sched_payment[0].payment_minor == 432467  # 4324.67元
    total_p = sum(item.principal_minor for item in sched_payment)
    assert total_p == 100_000_000
    assert sched_payment[-1].remaining_principal_minor == 0

    # 等额本金
    sched_princ = calculate_equal_principal_schedule(100_000_000, 3.2, 360, start)
    assert len(sched_princ) == 360
    assert sched_princ[0].payment_minor == 544445  # 5444.45元
    total_p2 = sum(item.principal_minor for item in sched_princ)
    assert total_p2 == 100_000_000
    assert sched_princ[-1].remaining_principal_minor == 0


def test_mortgage_plan_save_bill_and_dashboard_link(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # Save a mortgage bill
    payload = {
        "name": "上海住房贷款",
        "loan_type": "commercial",
        "repayment_method": "equal_payment",
        "loan_term_months": 360,
        "commercial_principal_minor": 100_000_000,
        "commercial_rate_mode": "lpr_bp",
        "lpr_rate": 3.50,
        "bp": -30.0,
        "commercial_actual_rate": 3.20,
        "fund_principal_minor": 0,
        "fund_rate": None,
        "first_payment_date": "2026-09-15",
        "reminder_enabled": True,
    }

    resp = client.post("/api/v1/mortgage/save-bill", headers=auth_headers, json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["plan"]["name"] == "上海住房贷款"
    assert data["manual_bill_id"] is not None

    # Check that manual bill exists in manual-bills list and is linked
    all_bills = client.get("/api/v1/manual-bills", headers=auth_headers).json()
    target_bill = next((b for b in all_bills if b["id"] == data["manual_bill_id"]), None)
    assert target_bill is not None
    assert target_bill["bill_type"] == "房贷"

    # Check dashboard /manual-bills/current
    cur_bills = client.get("/api/v1/manual-bills/current", headers=auth_headers).json()
    mortgage_bills = [b for b in cur_bills if b["name"] == "上海住房贷款"]
    assert len(mortgage_bills) == 1
    assert mortgage_bills[0]["amount_minor"] == 432467

    # Check dashboard API
    dash = client.get("/api/v1/dashboard", headers=auth_headers).json()
    manual_items = dash["manual_due"]["items"]
    found = [m for m in manual_items if m["name"] == "上海住房贷款"]
    assert len(found) == 1
    assert found[0]["amount_minor"] == 432467


def test_combined_mortgage_plan_save(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    payload = {
        "name": "组合房贷测试",
        "loan_type": "combined",
        "repayment_method": "equal_payment",
        "loan_term_months": 240,
        "commercial_principal_minor": 80_000_000,
        "commercial_actual_rate": 3.20,
        "fund_principal_minor": 40_000_000,
        "fund_rate": 2.85,
        "first_payment_date": "2026-09-20",
        "reminder_enabled": True,
    }
    resp = client.post("/api/v1/mortgage/save-bill", headers=auth_headers, json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["plan"]["loan_type"] == "combined"

    plans = client.get("/api/v1/mortgage/plans", headers=auth_headers).json()
    assert any(p["name"] == "组合房贷测试" for p in plans)


def test_mortgage_plan_start_period(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    payload = {
        "name": "从第13期开始的房贷",
        "loan_type": "commercial",
        "repayment_method": "equal_principal",
        "loan_term_months": 360,
        "commercial_principal_minor": 100_000_000,
        "commercial_actual_rate": 3.20,
        "first_payment_date": "2026-09-15",
        "start_period": 13,
        "reminder_enabled": True,
    }
    resp = client.post("/api/v1/mortgage/save-bill", headers=auth_headers, json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["plan"]["start_period"] == 13

    cur_bills = client.get("/api/v1/manual-bills/current", headers=auth_headers).json()
    target = next((b for b in cur_bills if b["name"] == "从第13期开始的房贷"), None)
    assert target is not None
    # In equal principal, period 13 payment must be strictly less than period 1 (544445)
    assert target["amount_minor"] < 544445


def test_mortgage_plan_lifecycle_bounds(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # Test A: Future start - today is 2026-09, first_payment_date is 2026-11-15
    payload_future = {
        "name": "未来开始的房贷",
        "loan_type": "commercial",
        "repayment_method": "equal_payment",
        "loan_term_months": 12,
        "commercial_principal_minor": 12_000_000,
        "commercial_actual_rate": 3.0,
        "first_payment_date": "2026-11-15",
        "reminder_enabled": True,
    }
    resp = client.post("/api/v1/mortgage/save-bill", headers=auth_headers, json=payload_future)
    assert resp.status_code == 201, resp.text
    future_bill_id = resp.json()["manual_bill_id"]

    # In 2026-09, /manual-bills/current and dashboard must NOT contain future mortgage
    cur_bills = client.get("/api/v1/manual-bills/current", headers=auth_headers).json()
    assert not any(b["name"] == "未来开始的房贷" for b in cur_bills)

    dash = client.get("/api/v1/dashboard", headers=auth_headers).json()
    assert not any(m["name"] == "未来开始的房贷" for m in dash["manual_due"]["items"])

    # Test B & C: In-range vs expired mortgage
    # Let's create a 2-month mortgage plan starting 2026-07-15 (periods: 2026-07, 2026-08).
    # In 2026-09, it has already ended!
    payload_ended = {
        "name": "已结束的房贷",
        "loan_type": "commercial",
        "repayment_method": "equal_payment",
        "loan_term_months": 2,
        "commercial_principal_minor": 2_000_000,
        "commercial_actual_rate": 3.0,
        "first_payment_date": "2026-07-15",
        "reminder_enabled": True,
    }
    resp_ended = client.post("/api/v1/mortgage/save-bill", headers=auth_headers, json=payload_ended)
    assert resp_ended.status_code == 201, resp_ended.text

    # ensure_current_payments must NOT generate payment for 2026-09
    cur_bills = client.get("/api/v1/manual-bills/current", headers=auth_headers).json()
    assert not any(b["name"] == "已结束的房贷" for b in cur_bills)

    dash = client.get("/api/v1/dashboard", headers=auth_headers).json()
    assert not any(m["name"] == "已结束的房贷" for m in dash["manual_due"]["items"])


def test_mortgage_payment_manual_override_preserved_on_sync(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # 1. Create a MortgagePlan active in current month (2026-09)
    payload = {
        "name": "人工修改防覆盖测试",
        "loan_type": "commercial",
        "repayment_method": "equal_payment",
        "loan_term_months": 24,
        "commercial_principal_minor": 24_000_000,
        "commercial_actual_rate": 3.0,
        "first_payment_date": "2026-09-15",
        "reminder_enabled": True,
    }
    resp = client.post("/api/v1/mortgage/save-bill", headers=auth_headers, json=payload)
    assert resp.status_code == 201, resp.text
    bill_id = resp.json()["manual_bill_id"]

    # Current payment generated by save-bill
    cur_bills = client.get("/api/v1/manual-bills/current", headers=auth_headers).json()
    target = next(b for b in cur_bills if b["bill_id"] == bill_id)
    original_amount = target["amount_minor"]
    assert original_amount > 0

    # 2. Modify manual bill (amount and due_day)
    custom_amount = original_amount + 88888
    custom_due_day = 28
    put_res = client.put(
        f"/api/v1/manual-bills/{bill_id}",
        headers=auth_headers,
        json={
            "bill_type": "房贷",
            "name": "人工修改防覆盖测试",
            "statement_day": 1,
            "due_day": custom_due_day,
            "monthly_amount_minor": custom_amount,
            "reminder_enabled": True,
            "enabled": True,
        },
    )
    assert put_res.status_code == 200, put_res.text

    # Verify payment in DB was manually updated or user updated current payment directly
    with client.app.state.session_factory() as session:
        payment = session.scalar(
            select(ManualBillPayment).where(
                ManualBillPayment.manual_bill_id == bill_id,
                ManualBillPayment.period == "2026-09",
            )
        )
        assert payment is not None
        # Manually adjust the payment snapshot
        payment.amount_minor = custom_amount
        payment.due_date = date(2026, 9, custom_due_day)
        session.commit()

    # 3. Call ensure_current_payments via API /manual-bills/generate-current-payments
    gen_res = client.post("/api/v1/manual-bills/generate-current-payments", headers=auth_headers)
    assert gen_res.status_code == 200, gen_res.text

    # 4. Assert manual adjustment is PRESERVED and not overwritten!
    cur_bills_after = client.get("/api/v1/manual-bills/current", headers=auth_headers).json()
    target_after = next(b for b in cur_bills_after if b["bill_id"] == bill_id)
    assert target_after["amount_minor"] == custom_amount
    assert target_after["due_date"] == f"2026-09-{custom_due_day:02d}"

    # 5. Entering next period (2026-10): ensure_current_payments creates period 2026-10 using MortgagePlan schedule
    with client.app.state.session_factory() as session:
        from app.services.manual_bills import ensure_current_payments
        oct_payments = ensure_current_payments(session, date(2026, 10, 1))
        oct_target = next(p for b, p in oct_payments if b.id == bill_id)
        assert oct_target.period == "2026-10"
