from datetime import date, datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.manual_bill import ManualBill, ManualBillPayment


@pytest.fixture(autouse=True)
def _freeze_manual_bills_date(monkeypatch):
    frozen_date = date(2026, 9, 15)
    for target in (
        "app.services.statements.local_today",
        "app.api.v1.manual_bills.local_today",
        "app.api.v1.dashboard.local_today",
    ):
        monkeypatch.setattr(target, lambda *args, **kwargs: frozen_date)


def test_manual_bill_is_created_paid_and_unmarked(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    created = client.post(
        "/api/v1/manual-bills",
        headers=auth_headers,
        json={
            "bill_type": "房租",
            "name": "测试房租",
            "statement_day": 1,
            "due_day": 5,
            "monthly_amount_minor": 680000,
            "reminder_enabled": True,
        },
    )
    assert created.status_code == 201, created.text
    bill = created.json()

    current = client.get("/api/v1/manual-bills/current", headers=auth_headers)
    assert current.status_code == 200
    assert current.json()[0]["name"] == "测试房租"

    paid = client.post(
        f"/api/v1/manual-bills/{bill['id']}/mark-paid",
        headers=auth_headers,
        json={"repayment_channel": "alipay"},
    )
    assert paid.status_code == 204
    assert client.get("/api/v1/manual-bills/current", headers=auth_headers).json() == []

    marked = client.get("/api/v1/manual-bills/marked-paid", headers=auth_headers)
    assert marked.status_code == 200
    assert marked.json()[0]["name"] == "测试房租"
    assert marked.json()[0]["repayment_channel"] == "alipay"

    unmarked = client.post(
        f"/api/v1/manual-bills/{bill['id']}/unmark-paid", headers=auth_headers
    )
    assert unmarked.status_code == 204
    assert len(client.get("/api/v1/manual-bills/current", headers=auth_headers).json()) == 1

    invalid = client.post(
        f"/api/v1/manual-bills/{bill['id']}/mark-paid",
        headers=auth_headers,
        json={"repayment_channel": "cash"},
    )
    assert invalid.status_code == 422

    # Update manual bill
    updated = client.put(
        f"/api/v1/manual-bills/{bill['id']}",
        headers=auth_headers,
        json={
            "name": "修改后的房租",
            "monthly_amount_minor": 720000,
            "due_day": 10,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "修改后的房租"
    assert updated.json()["monthly_amount_minor"] == 720000

    current_after_update = client.get("/api/v1/manual-bills/current", headers=auth_headers).json()
    assert current_after_update[0]["name"] == "修改后的房租"
    assert current_after_update[0]["amount_minor"] == 720000

    # Delete manual bill
    deleted = client.delete(
        f"/api/v1/manual-bills/{bill['id']}",
        headers=auth_headers,
    )
    assert deleted.status_code == 204
    assert client.get("/api/v1/manual-bills", headers=auth_headers).json() == []
    assert client.get("/api/v1/manual-bills/current", headers=auth_headers).json() == []


def test_list_marked_paid_period_filter_and_validation(client: TestClient, auth_headers: dict) -> None:
    # 1. Create a manual bill
    bill_res = client.post(
        "/api/v1/manual-bills",
        headers=auth_headers,
        json={
            "bill_type": "房租",
            "name": "多月份已还测试",
            "statement_day": 1,
            "due_day": 10,
            "monthly_amount_minor": 350000,
            "reminder_enabled": True,
            "enabled": True,
        },
    )
    assert bill_res.status_code == 201, bill_res.text
    bill_id = bill_res.json()["id"]

    # 2. Add payment records: 2026-07 (paid), 2026-08 (unpaid), and update 2026-09 (paid)
    with client.app.state.session_factory() as session:
        p_july = ManualBillPayment(
            manual_bill_id=bill_id,
            period="2026-07",
            due_date=date(2026, 7, 10),
            amount_minor=350000,
            paid_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
        )
        p_aug = ManualBillPayment(
            manual_bill_id=bill_id,
            period="2026-08",
            due_date=date(2026, 8, 10),
            amount_minor=350000,
            paid_at=None,
        )
        session.add_all([p_july, p_aug])
        p_sep = session.scalar(
            select(ManualBillPayment).where(
                ManualBillPayment.manual_bill_id == bill_id,
                ManualBillPayment.period == "2026-09",
            )
        )
        if p_sep:
            p_sep.paid_at = datetime(2026, 9, 8, tzinfo=timezone.utc)
        else:
            session.add(
                ManualBillPayment(
                    manual_bill_id=bill_id,
                    period="2026-09",
                    due_date=date(2026, 9, 10),
                    amount_minor=350000,
                    paid_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
                )
            )
        session.commit()

    # 3. Test without period (defaults to current month: 2026-09)
    res_default = client.get("/api/v1/manual-bills/marked-paid", headers=auth_headers)
    assert res_default.status_code == 200
    default_items = res_default.json()
    assert any(item["bill_id"] == bill_id for item in default_items)

    # 4. Test historical month 2026-07 -> should contain bill_id
    res_july = client.get("/api/v1/manual-bills/marked-paid?period=2026-07", headers=auth_headers)
    assert res_july.status_code == 200
    july_items = res_july.json()
    assert any(item["bill_id"] == bill_id for item in july_items)

    # 5. Test unpaid month 2026-08 -> should NOT contain bill_id
    res_aug = client.get("/api/v1/manual-bills/marked-paid?period=2026-08", headers=auth_headers)
    assert res_aug.status_code == 200
    aug_items = res_aug.json()
    assert not any(item["bill_id"] == bill_id for item in aug_items)

    # 6. Test invalid period -> 422
    assert client.get("/api/v1/manual-bills/marked-paid?period=2026-13", headers=auth_headers).status_code == 422
    assert client.get("/api/v1/manual-bills/marked-paid?period=invalid", headers=auth_headers).status_code == 422
    assert client.get("/api/v1/manual-bills/marked-paid?period=2026-9", headers=auth_headers).status_code == 422
