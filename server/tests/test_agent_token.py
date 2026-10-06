import re
import secrets

from fastapi.testclient import TestClient

from app.core.plain_secrets import get_secret


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_singleton_agent_token_lifecycle_in_vault(
    client: TestClient, caplog, auth_headers: dict[str, str]
) -> None:
    initial = client.get("/api/v1/agent-token", headers=auth_headers)
    assert initial.status_code == 200
    assert initial.json() == {"enabled": False}
    assert client.post("/api/v1/agent-token/reveal", headers=auth_headers).status_code == 404

    first = client.post("/api/v1/agent-token/generate", headers=auth_headers).json()["token"]
    second = client.post("/api/v1/agent-token/generate", headers=auth_headers).json()["token"]
    assert first != second
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", second)
    assert client.get("/api/v1/cards", headers=_bearer(first)).status_code == 401

    saved = client.put("/api/v1/agent-token", headers=auth_headers, json={"token": first})
    assert saved.status_code == 200
    assert saved.json() == {"enabled": True}
    assert first not in saved.text
    assert get_secret("agent", "token") == first
    assert client.get("/api/v1/cards", headers=_bearer(first)).status_code == 200

    status_response = client.get("/api/v1/agent-token", headers=auth_headers)
    assert status_response.json() == {"enabled": True}
    assert first not in status_response.text
    assert "token_hash" not in status_response.text

    revealed = client.post("/api/v1/agent-token/reveal", headers=auth_headers)
    assert revealed.status_code == 200
    assert revealed.json() == {"token": first}
    assert revealed.headers["cache-control"] == "no-store"

    unchanged = client.put("/api/v1/agent-token", headers=auth_headers, json={})
    assert unchanged.json() == {"enabled": True}
    assert client.get("/api/v1/cards", headers=_bearer(first)).status_code == 200

    replaced = client.put("/api/v1/agent-token", headers=auth_headers, json={"token": second})
    assert replaced.json() == {"enabled": True}
    assert get_secret("agent", "token") == second
    assert client.get("/api/v1/cards", headers=_bearer(first)).status_code == 401
    assert client.get("/api/v1/cards", headers=_bearer(second)).status_code == 200
    assert client.post("/api/v1/agent-token/reveal", headers=auth_headers).json() == {"token": second}

    cleared = client.put("/api/v1/agent-token", headers=auth_headers, json={"token": ""})
    assert cleared.status_code == 200
    assert cleared.json() == {"enabled": False}
    assert get_secret("agent", "token") is None
    assert client.get("/api/v1/agent-token", headers=auth_headers).json() == {"enabled": False}
    assert client.get("/api/v1/cards", headers=_bearer(second)).status_code == 401
    assert client.post("/api/v1/agent-token/reveal", headers=auth_headers).status_code == 404

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert first not in log_text
    assert second not in log_text


def test_agent_token_api_rejects_weak_values_without_echoing_them(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    weak = "short-private-value"
    response = client.put("/api/v1/agent-token", headers=auth_headers, json={"token": weak})
    assert response.status_code == 422
    assert weak not in response.text
    assert client.get("/api/v1/agent-token", headers=auth_headers).json() == {"enabled": False}

    whitespace_padded = f"  {secrets.token_urlsafe(32)}  "
    response_ws = client.put(
        "/api/v1/agent-token", headers=auth_headers, json={"token": whitespace_padded}
    )
    assert response_ws.status_code == 422
    assert whitespace_padded not in response_ws.text


def test_legacy_multi_token_routes_are_not_exposed(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/agent-tokens", headers=auth_headers).status_code == 404
    assert client.post("/api/v1/agent-tokens", headers=auth_headers, json={}).status_code == 404
    assert client.delete("/api/v1/agent-tokens/legacy-id", headers=auth_headers).status_code == 404


def test_agent_token_ui_can_reveal_and_copy_saved_token(client: TestClient) -> None:
    html = client.get("/").text
    assert html.index("Agent Token") < html.index("系统备份") < html.index("关于系统")
    assert 'data-agent-action="copy"' in html
    assert 'data-agent-action="generate"' in html
    assert "await api('/agent-token/reveal', { method: 'POST' })" in html
    assert "if (!(await revealAgentToken())) return;" in html
    assert "await navigator.clipboard.writeText(agentToken.value);" in html
    assert "agentToken.value = '';" in html
    assert "const payload = agentTokenDirty.value ? { token } : {};" in html
    assert "localStorage.setItem('agentToken" not in html
    assert "供 Agent 或自动化工具访问" not in html
    assert "待禁用" not in html
    assert "已保存 Token 不可再次查看" not in html
    assert ">Agent API<" not in html
    assert "Math.random" not in html[
        html.index("async function generateAgentToken"):html.index("async function copyAgentToken")
    ]
