from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "app"
FORBIDDEN_TRANSACTION_IMPORTS = (
    "app.services.transaction_classification",
    "app.services.transaction_lifecycle",
    "app.services.financial_analysis.semantics",
)
FORBIDDEN_INTEREST_FREE_IMPORTS = (
    "app.services.interest_free_engine",
    "app.services.financial_analysis.interest_free",
)


def test_transaction_has_single_module_entrypoint() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if "modules/core/transaction" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_TRANSACTION_IMPORTS:
            if forbidden in text:
                offenders.append(f"{path.relative_to(ROOT)} -> {forbidden}")

    assert not offenders, "legacy transaction imports remain:\n" + "\n".join(offenders)


def test_legacy_transaction_service_files_are_removed() -> None:
    assert not (ROOT / "services" / "transaction_classification.py").exists()
    assert not (ROOT / "services" / "transaction_lifecycle.py").exists()
    assert not (ROOT / "services" / "financial_analysis" / "semantics.py").exists()


def test_interest_free_has_single_module_entrypoint() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if "modules/optional/interest_free" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_INTEREST_FREE_IMPORTS:
            if forbidden in text:
                offenders.append(f"{path.relative_to(ROOT)} -> {forbidden}")

    assert not offenders, "legacy interest-free imports remain:\n" + "\n".join(offenders)


def test_legacy_interest_free_service_files_are_removed() -> None:
    assert not (ROOT / "services" / "interest_free_engine.py").exists()
    assert not (ROOT / "services" / "financial_analysis" / "interest_free.py").exists()
