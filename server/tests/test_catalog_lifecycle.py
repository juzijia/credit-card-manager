from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.api.v1.catalog import trigger_scan_run
from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.models.catalog import BankRegistry
from app.schemas.catalog import ScanTriggerRequest


_TEST_ADMIN_TOKEN = "test-admin-token-that-is-at-least-32-characters"
_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_ADMIN_TOKEN}"}


def make_app(database_path: Path):
    settings = Settings(
        _env_file=None,
        env="test",
        auth_mode="token",
        admin_token=SecretStr(_TEST_ADMIN_TOKEN),
        database_url=f"sqlite:///{database_path.as_posix()}",
        timezone="Asia/Shanghai",
        error_log_path=None,
        background_tasks_enabled=False,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    return app


def bank_snapshot(app) -> list[tuple[str, str, str, str, bool, str | None, str | None]]:
    with app.state.session_factory() as session:
        return [
            (
                bank.bank_id,
                bank.bank_name,
                bank.official_homepage,
                bank.credit_card_catalog_url,
                bank.enabled,
                bank.customer_service_phone,
                bank.service_info_source_url,
            )
            for bank in session.scalars(select(BankRegistry).order_by(BankRegistry.bank_id))
        ]


def test_lifespan_initializes_registries_before_first_request(tmp_path: Path) -> None:
    app = make_app(tmp_path / "fresh.db")

    with TestClient(app):
        snapshot = bank_snapshot(app)

    assert snapshot


def test_catalog_gets_are_read_only_after_startup(tmp_path: Path) -> None:
    app = make_app(tmp_path / "gets.db")
    with TestClient(app) as client:
        before = bank_snapshot(app)
        assert client.get("/api/v1/catalog/banks", headers=_AUTH_HEADERS).status_code == 200
        assert client.get("/api/v1/catalog/stats", headers=_AUTH_HEADERS).status_code == 200
        assert bank_snapshot(app) == before


def test_concurrent_catalog_gets_are_safe(tmp_path: Path) -> None:
    app = make_app(tmp_path / "concurrent.db")
    with TestClient(app) as client:
        before = bank_snapshot(app)

        def request(path: str) -> int:
            return client.get(path, headers=_AUTH_HEADERS).status_code

        paths = ["/api/v1/catalog/banks", "/api/v1/catalog/stats"] * 4
        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(request, paths))

        assert statuses == [200] * len(paths)
        assert bank_snapshot(app) == before


def test_repeated_startup_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "restart.db"
    app = make_app(database_path)
    with TestClient(app):
        first = bank_snapshot(app)

    app = make_app(database_path)
    with TestClient(app):
        second = bank_snapshot(app)

    assert second == first


def test_startup_preserves_custom_fields_and_disabled_state(tmp_path: Path) -> None:
    app = make_app(tmp_path / "custom.db")
    with app.state.session_factory() as session:
        session.add(
            BankRegistry(
                bank_id="CMB",
                bank_name="Custom CMB",
                official_homepage="https://custom.example/",
                credit_card_catalog_url="https://custom.example/cards",
                enabled=False,
                customer_service_phone="custom-hotline",
                service_info_source_url="https://custom.example/source",
            )
        )
        session.commit()

    with TestClient(app):
        pass

    with app.state.session_factory() as session:
        cmb = session.get(BankRegistry, "CMB")
        assert cmb is not None
        assert cmb.enabled is False
        assert cmb.customer_service_phone == "custom-hotline"
        assert cmb.service_info_source_url == "https://custom.example/source"
        assert cmb.bank_name == "Custom CMB"


def test_scan_run_does_not_seed_an_empty_registry(tmp_path: Path) -> None:
    app = make_app(tmp_path / "empty.db")
    with app.state.session_factory() as session:
        with pytest.raises(Exception, match="not found in registry"):
            trigger_scan_run(
                ScanTriggerRequest(scope="BANK", bank_id="CMB"),
                session,
            )
        assert session.scalar(select(BankRegistry)) is None


def test_get_banks_does_not_call_registry_seeder(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path / "patch.db")
    with TestClient(app) as client:
        def fail_if_called(_session) -> None:
            raise AssertionError("GET /catalog/banks must not seed registries")

        monkeypatch.setattr("app.main.ensure_bank_registries", fail_if_called)
        response = client.get("/api/v1/catalog/banks", headers=_AUTH_HEADERS)

    assert response.status_code == 200

def test_lifespan_propagates_bootstrap_failure(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path / "bootstrap_fail.db")

    def fail_bootstrap(_session) -> None:
        raise RuntimeError("Simulated catalog bootstrap failure")

    monkeypatch.setattr("app.main.ensure_bank_registries", fail_bootstrap)

    with pytest.raises(RuntimeError, match="Simulated catalog bootstrap failure"):
        with TestClient(app):
            pass
