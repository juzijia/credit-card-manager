def test_bank_provider_catalog_is_deployed(client, auth_headers) -> None:
    response = client.get("/api/v1/imports/providers", headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 26
    ids = {item["id"] for item in payload["providers"]}
    assert {"cgb_credit", "cmb_credit", "ccb_credit", "icbc_debit"} <= ids
    assert all(item["capability"] == "transactions" for item in payload["providers"])


def test_provider_catalog_requires_authentication(client) -> None:
    assert client.get("/api/v1/imports/providers").status_code == 401
