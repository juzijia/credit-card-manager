from dataclasses import dataclass
from pathlib import Path

from bean_sieve import get_provider, list_providers
from bean_sieve.providers import PROVIDERS, auto_detect_provider


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    id: str
    name: str
    formats: tuple[str, ...]
    kind: str


def bank_providers() -> tuple[ProviderInfo, ...]:
    result = []
    for item in list_providers():
        provider_id = item["id"]
        if provider_id.endswith("_credit"):
            kind = "credit"
        elif provider_id.endswith("_debit"):
            kind = "debit"
        else:
            continue
        result.append(
            ProviderInfo(
                id=provider_id,
                name=item["name"],
                formats=tuple(part.strip() for part in item["formats"].split(",")),
                kind=kind,
            )
        )
    return tuple(result)


def bank_detection_terms() -> tuple[str, ...]:
    terms: set[str] = set()
    for provider_id, provider in PROVIDERS.items():
        if not (provider_id.endswith("_credit") or provider_id.endswith("_debit")):
            continue
        candidates = [provider.provider_name, *provider.filename_keywords]
        terms.update(
            value.lower()
            for value in candidates
            if value and (any("\u4e00" <= char <= "\u9fff" for char in value) or len(value) >= 5)
        )
    return tuple(sorted(terms))


def parse_file(path: Path, provider_id: str | None = None):
    provider = get_provider(provider_id) if provider_id else auto_detect_provider(path)
    if provider is None or not (
        provider.provider_id.endswith("_credit")
        or provider.provider_id.endswith("_debit")
    ):
        raise ValueError("No bank provider recognizes this file")
    return provider, provider.parse(path)
