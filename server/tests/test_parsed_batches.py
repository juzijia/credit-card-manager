from types import SimpleNamespace

import pytest

from app.services import parsed_batches


def test_empty_provider_result_is_not_staged(client, monkeypatch, tmp_path) -> None:
    source = tmp_path / "statement.eml"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(
        parsed_batches,
        "parse_file",
        lambda *_args, **_kwargs: (SimpleNamespace(provider_id="cmb_credit"), []),
    )
    with client.app.state.session_factory() as session:
        with pytest.raises(ValueError, match="returned no transactions"):
            parsed_batches.stage_parsed_file(
                session,
                source,
                source_type="test",
            )
