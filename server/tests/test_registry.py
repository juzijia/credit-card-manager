from app.parsers import default_registry


def test_default_registry_supports_all_nine_banks() -> None:
    expected = ("CGB", "CMB", "PAB", "CITIC", "ICBC", "CCB", "BOCOM", "ABC", "BOC")
    assert default_registry.supported_banks == expected
