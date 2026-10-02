import hashlib
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.bean_sieve import parse_file
from app.models.parsed_batch import ParsedBatch, ParsedEntry
from app.parsers.base import decimal_to_minor


def _minor_units(amount: Decimal) -> int:
    return decimal_to_minor(amount)


def stage_parsed_file(
    session: Session,
    path: Path,
    *,
    source_type: str,
    source_locator: str | None = None,
    message_id: str | None = None,
    provider_id: str | None = None,
) -> tuple[ParsedBatch, bool]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = session.scalar(select(ParsedBatch).where(ParsedBatch.source_sha256 == digest))
    if existing:
        return existing, True
    provider, transactions = parse_file(path, provider_id)
    if not transactions:
        raise ValueError(
            f"Provider {provider.provider_id} recognized the file but returned no transactions"
        )
    amounts = [_minor_units(item.amount) for item in transactions]
    batch = ParsedBatch(
        source_sha256=digest,
        source_type=source_type,
        source_locator=source_locator,
        message_id=message_id,
        provider_id=provider.provider_id,
        original_format=path.suffix.lower(),
        transaction_count=len(transactions),
    )
    session.add(batch)
    session.flush()
    session.add_all(
        [
            ParsedEntry(
                batch_id=batch.id,
                source_line=index,
                transaction_date=item.date,
                posting_date=item.post_date,
                amount_minor=amount,
                currency=item.currency,
                description=item.description,
                card_last4=item.card_last4,
            )
            for index, (item, amount) in enumerate(zip(transactions, amounts), start=1)
        ]
    )
    session.commit()
    return batch, False
