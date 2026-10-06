from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier, Event, Lock

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.v1 import statements as statements_api
from app.models.repayment import Repayment


def _create_statement(
    client: TestClient, auth_headers: dict[str, str], total_due_minor: int = 10_000
) -> str:
    card = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "并发测试卡",
            "last4": "1234",
            "currency": "CNY",
        },
    )
    assert card.status_code == 201, card.text
    today = date.today()
    statement = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json={
            "card_id": card.json()["id"],
            "statement_date": today.isoformat(),
            "due_date": (today + timedelta(days=10)).isoformat(),
            "total_due_minor": total_due_minor,
            "minimum_due_minor": 1_000,
            "currency": "CNY",
            "parse_status": "summary_ok",
        },
    )
    assert statement.status_code == 201, statement.text
    return statement.json()["id"]


def _synchronize_remaining_reads(monkeypatch) -> None:
    original = statements_api.remaining_minor
    second_read = Event()
    counter_lock = Lock()
    calls = 0

    def synchronized(statement):
        nonlocal calls
        value = original(statement)
        with counter_lock:
            index = calls
            calls += 1
        if index == 0:
            second_read.wait(timeout=0.5)
        elif index == 1:
            second_read.set()
        return value

    monkeypatch.setattr(statements_api, "remaining_minor", synchronized)


def _run_concurrently(*requests):
    start = Barrier(len(requests))

    def run(request):
        start.wait()
        return request()

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        futures = [executor.submit(run, request) for request in requests]
        return [future.result(timeout=10) for future in futures]


def _repayment_total(client: TestClient, statement_id: str) -> int:
    with client.app.state.session_factory() as session:
        return session.scalar(
            select(func.coalesce(func.sum(Repayment.amount_minor), 0)).where(
                Repayment.statement_id == statement_id
            )
        )


def _post_repayment(
    client: TestClient,
    auth_headers: dict[str, str],
    statement_id: str,
    amount_minor: int,
    idempotency_key: str,
):
    return lambda: client.post(
        f"/api/v1/statements/{statement_id}/repayments",
        headers=auth_headers,
        json={"amount_minor": amount_minor, "idempotency_key": idempotency_key},
    )


def test_concurrent_overpayments_allow_exactly_one(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    statement_id = _create_statement(client, auth_headers)
    _synchronize_remaining_reads(monkeypatch)

    responses = _run_concurrently(
        _post_repayment(client, auth_headers, statement_id, 8_000, "concurrent-8000-a"),
        _post_repayment(client, auth_headers, statement_id, 8_000, "concurrent-8000-b"),
    )

    assert sorted(response.status_code for response in responses) == [201, 422]
    assert _repayment_total(client, statement_id) == 8_000


def test_concurrent_repayments_can_exactly_cover_remaining_amount(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    statement_id = _create_statement(client, auth_headers)
    _synchronize_remaining_reads(monkeypatch)

    responses = _run_concurrently(
        _post_repayment(client, auth_headers, statement_id, 6_000, "concurrent-6000"),
        _post_repayment(client, auth_headers, statement_id, 4_000, "concurrent-4000"),
    )

    assert [response.status_code for response in responses].count(201) == 2
    assert _repayment_total(client, statement_id) == 10_000


def test_concurrent_same_idempotency_key_returns_one_repayment(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    statement_id = _create_statement(client, auth_headers)
    _synchronize_remaining_reads(monkeypatch)

    responses = _run_concurrently(
        _post_repayment(client, auth_headers, statement_id, 8_000, "same-key-0001"),
        _post_repayment(client, auth_headers, statement_id, 8_000, "same-key-0001"),
    )

    assert [response.status_code for response in responses].count(201) == 2
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert _repayment_total(client, statement_id) == 8_000


def test_mark_paid_and_repayment_cannot_overpay(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    statement_id = _create_statement(client, auth_headers)
    _synchronize_remaining_reads(monkeypatch)

    repayment, marked = _run_concurrently(
        _post_repayment(client, auth_headers, statement_id, 8_000, "with-mark-paid"),
        lambda: client.post(
            f"/api/v1/statements/{statement_id}/mark-paid",
            headers={**auth_headers, "Idempotency-Key": "mark-paid-race"},
        ),
    )

    assert marked.status_code == 200
    assert repayment.status_code in {201, 422}
    assert _repayment_total(client, statement_id) == 10_000


def test_concurrent_repayments_recheck_existing_repayment(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    statement_id = _create_statement(client, auth_headers)
    existing = _post_repayment(
        client, auth_headers, statement_id, 2_000, "existing-2000"
    )()
    assert existing.status_code == 201
    _synchronize_remaining_reads(monkeypatch)

    responses = _run_concurrently(
        _post_repayment(client, auth_headers, statement_id, 5_000, "after-existing-a"),
        _post_repayment(client, auth_headers, statement_id, 5_000, "after-existing-b"),
    )

    assert sorted(response.status_code for response in responses) == [201, 422]
    assert _repayment_total(client, statement_id) == 7_000
