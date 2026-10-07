import copy
import io
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import models  # noqa: F401
from app.api.v1 import catalog
from app.core.config import Settings
from app.core.plain_secrets import set_secret
from app.db.base import Base
from app.db.session import create_database_engine, create_session_factory
from app.models.card import Card
from app.models.catalog import CatalogProduct
from app.services.catalog_service import ensure_bank_registries, upsert_catalog_products


MAX_BYTES = 8 * 1024 * 1024
VALID = {
    "CMB": [
        {
            "bank_id": "CMB",
            "canonical_name": "远端测试卡",
            "aliases": [],
            "networks": ["银联"],
            "annual_fee": {"primary": 10000, "supplementary": 0},
            "benefits": [],
            "artworks": [],
            "sources": [],
            "confidence": 1.0,
        }
    ]
}
ORIGINAL = {"CMB": [{"bank_id": "CMB", "canonical_name": "原有测试卡"}]}


class Response(io.BytesIO):
    status = 200

    def __init__(self, body):
        super().__init__(body)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def geturl(self):
        return catalog.DEFAULT_CATALOG_SYNC_URL


@pytest.fixture
def sync_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CCM_SECRETS_FILE", str(tmp_path / "secrets.json"))
    bundled = tmp_path / "catalog_fixtures.json"
    bundled.write_text(json.dumps(ORIGINAL, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(catalog, "_BUNDLED_FIXTURES_PATH", bundled)
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'catalog.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine, "Asia/Shanghai")
    with factory() as session:
        ensure_bank_registries(session)
        upsert_catalog_products(session, ORIGINAL)
    app = FastAPI()
    app.state.settings = Settings(env="test", auth_mode="dev_bypass")
    app.state.session_factory = factory
    app.include_router(catalog.router, prefix="/api/v1")
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, bundled=bundled, factory=factory)
    engine.dispose()


def remote(monkeypatch, body=None, error=None):
    response = Response(body or b"")
    requests = []

    def open_request(request, timeout):
        requests.append((request.full_url, timeout))
        if error:
            raise error
        return response

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *handlers: SimpleNamespace(open=open_request),
    )
    return response, requests


def assert_original_state(app):
    assert json.loads(app.bundled.read_text(encoding="utf-8")) == ORIGINAL
    with app.factory() as session:
        assert session.scalar(select(func.count(CatalogProduct.id))) == 1


def test_request_url_is_rejected_before_network(sync_app, monkeypatch):
    _, requests = remote(monkeypatch, json.dumps(VALID).encode())
    response = sync_app.client.post(
        "/api/v1/catalog/sync", json={"url": "http://127.0.0.1/private"}
    )
    assert response.status_code == 422
    assert not requests
    assert_original_state(sync_app)


def test_sync_uses_fixed_source_updates_database_and_keeps_bundle_read_only(
    sync_app, monkeypatch
):
    with sync_app.factory() as session:
        session.add(
            Card(
                bank_code="CMB",
                bank_name="招商银行",
                display_name="远端字典测试卡",
                last4="4321",
                currency="CNY",
                card_product="远端测试卡",
                annual_fee_minor=1,
                annual_fee_source="dictionary",
            )
        )
        session.commit()

    body = json.dumps(VALID, ensure_ascii=False).encode("utf-8")
    response, requests = remote(monkeypatch, body)
    result = sync_app.client.post("/api/v1/catalog/sync", json={})
    assert result.status_code == 200, result.text
    assert result.json()["source"] == "remote"
    assert result.json()["created"] == 1
    assert result.json()["refreshed_cards"] == 1
    assert result.json()["total"] == 2
    assert requests == [(catalog.DEFAULT_CATALOG_SYNC_URL, 10)]
    assert response.read_sizes == [MAX_BYTES + 1]
    assert json.loads(sync_app.bundled.read_text(encoding="utf-8")) == ORIGINAL
    with sync_app.factory() as session:
        product = session.scalar(
            select(CatalogProduct).where(CatalogProduct.canonical_name == "远端测试卡")
        )
        assert product is not None
        assert product.annual_fee_primary_minor == 10000
        card = session.scalar(select(Card).where(Card.last4 == "4321"))
        assert card is not None
        assert card.annual_fee_minor == 10000


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"\xff",
        b"[]",
        b"{}",
        b'{"CMB":{}}',
        b'{"CMB":[null]}',
        b'{"CMB":[{"bank_id":"CMB","canonical_name":42}]}',
    ],
)
def test_invalid_remote_data_preserves_bundle_and_products(sync_app, monkeypatch, body):
    remote(monkeypatch, body)
    result = sync_app.client.post("/api/v1/catalog/sync", json={})
    assert result.status_code == 422, result.text
    assert_original_state(sync_app)


@pytest.mark.parametrize(
    "field, value",
    [
        ("annual_fee", {"primary": -1}),
        ("annual_fee", {"primary": "100"}),
        ("annual_fee", {"primary": 2**63}),
        ("bank_id", "BOC"),
        ("canonical_name", " "),
        ("benefits", ["not a benefit"]),
        ("artworks", [{"artwork_url": "x", "is_primary": "false"}]),
    ],
)
def test_entire_dictionary_is_validated_before_any_update(
    sync_app, monkeypatch, field, value
):
    data = copy.deepcopy(VALID)
    invalid = copy.deepcopy(data["CMB"][0])
    invalid["canonical_name"] = "第二张测试卡"
    invalid[field] = value
    data["CMB"].append(invalid)
    remote(monkeypatch, json.dumps(data).encode())
    result = sync_app.client.post("/api/v1/catalog/sync", json={})
    assert result.status_code == 422, result.text
    assert_original_state(sync_app)


def test_oversized_response_is_rejected_without_modifying_state(sync_app, monkeypatch):
    response, _ = remote(monkeypatch, b" " * (MAX_BYTES + 1))
    result = sync_app.client.post("/api/v1/catalog/sync", json={})
    assert result.status_code == 413
    assert response.read_sizes == [MAX_BYTES + 1]
    assert_original_state(sync_app)


def test_response_at_size_limit_is_accepted_without_rewriting_bundle(sync_app, monkeypatch):
    body = json.dumps(VALID).encode()
    body += b" " * (MAX_BYTES - len(body))
    remote(monkeypatch, body)
    result = sync_app.client.post("/api/v1/catalog/sync", json={})
    assert result.status_code == 200, result.text
    assert result.json()["source"] == "remote"
    assert json.loads(sync_app.bundled.read_text(encoding="utf-8")) == ORIGINAL
    with sync_app.factory() as session:
        assert session.scalar(
            select(func.count(CatalogProduct.id)).where(
                CatalogProduct.canonical_name == "远端测试卡"
            )
        ) == 1


def test_network_failure_uses_persisted_database_without_reapplying_bundle(
    sync_app, monkeypatch
):
    remote(monkeypatch, error=URLError("synthetic unavailable source"))
    result = sync_app.client.post("/api/v1/catalog/sync", json={})
    assert result.status_code == 200, result.text
    assert result.json()["source"] == "database_fallback"
    assert result.json()["updated"] == 0
    assert_original_state(sync_app)


def test_network_failure_after_remote_sync_preserves_new_database_facts(
    sync_app, monkeypatch
):
    remote(monkeypatch, json.dumps(VALID).encode())
    first = sync_app.client.post("/api/v1/catalog/sync", json={})
    assert first.status_code == 200
    remote(monkeypatch, error=URLError("synthetic unavailable source"))
    second = sync_app.client.post("/api/v1/catalog/sync", json={})
    assert second.status_code == 200
    assert second.json()["source"] == "database_fallback"
    with sync_app.factory() as session:
        product = session.scalar(
            select(CatalogProduct).where(CatalogProduct.canonical_name == "远端测试卡")
        )
        assert product is not None
        assert product.annual_fee_primary_minor == 10000
    assert json.loads(sync_app.bundled.read_text(encoding="utf-8")) == ORIGINAL


def test_fixed_source_redirect_is_not_followed(sync_app, monkeypatch):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/untrusted-catalog")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    monkeypatch.setattr(
        catalog,
        "DEFAULT_CATALOG_SYNC_URL",
        f"http://127.0.0.1:{server.server_port}/catalog.json",
    )
    try:
        result = sync_app.client.post("/api/v1/catalog/sync", json={})
        assert result.status_code == 200, result.text
        assert result.json()["source"] == "database_fallback"
        assert paths == ["/catalog.json"]
        assert_original_state(sync_app)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_bundled_dictionary_remains_compatible(sync_app, monkeypatch):
    bundled = Path(__file__).resolve().parents[1] / "app/data/catalog_fixtures.json"
    remote(monkeypatch, bundled.read_bytes())
    result = sync_app.client.post("/api/v1/catalog/sync", json={})
    assert result.status_code == 200, result.text
    assert result.json()["source"] == "remote"
    assert result.json()["created"] == 157
    assert json.loads(sync_app.bundled.read_text(encoding="utf-8")) == ORIGINAL


def test_agent_cannot_sync_even_with_valid_payload(sync_app, monkeypatch):
    set_secret("agent", "token", "t" * 32)
    _, requests = remote(monkeypatch, json.dumps(VALID).encode())
    result = sync_app.client.post(
        "/api/v1/catalog/sync",
        json={},
        headers={"Authorization": "Bearer " + "t" * 32},
    )
    assert result.status_code == 403
    assert not requests
    assert_original_state(sync_app)
