from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol


class ParserError(ValueError):
    """A source is recognized but cannot be parsed safely."""


def decimal_to_minor(amount: Decimal, field: str = "transaction") -> int:
    """Preserve exact cents; never silently round imported source amounts."""
    if not amount.is_finite():
        raise ParserError(f"Invalid {field} amount")
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise ParserError(f"{field} has more than two nonzero decimal places")
    return int(minor)


@dataclass(frozen=True, slots=True)
class ParsedSource:
    source_type: str
    subject: str
    sender: str
    html: str
    sha256: str
    message_id: str | None = None
    path: Path | None = None
    raw_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class ParsedTransaction:
    transaction_date: date | None
    posting_date: date | None
    amount_minor: int
    currency: str
    raw_description: str
    card_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    bank_code: str
    card_last4: str
    statement_date: date
    due_date: date
    total_due_minor: int
    minimum_due_minor: int | None
    currency: str
    credit_limit_minor: int | None = None
    billing_cycle_start: date | None = None
    billing_cycle_end: date | None = None
    transactions: tuple[ParsedTransaction, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedBillingUnit:
    """One independently payable card-by-currency unit in a source document."""

    card_ref: str
    currency: str
    total_due_minor: int
    minimum_due_minor: int | None


@dataclass(frozen=True, slots=True)
class ParsedAccountSummary:
    """Account-level reconciliation facts; never persisted as a Statement."""

    currency: str
    total_due_minor: int
    minimum_due_minor: int | None


@dataclass(frozen=True, slots=True)
class ParsedBillingDocument:
    bank_code: str
    statement_date: date
    due_date: date
    billing_units: tuple[ParsedBillingUnit, ...]
    transactions: tuple[ParsedTransaction, ...]
    account_summaries: tuple[ParsedAccountSummary, ...] = ()
    credit_limit_minor: int | None = None
    billing_cycle_start: date | None = None
    billing_cycle_end: date | None = None
    warnings: tuple[str, ...] = ()


def billing_document_from_statement(parsed: ParsedStatement) -> ParsedBillingDocument:
    """Compatibility adapter for parsers that still have one proven billing unit."""

    return ParsedBillingDocument(
        bank_code=parsed.bank_code,
        statement_date=parsed.statement_date,
        due_date=parsed.due_date,
        billing_units=(
            ParsedBillingUnit(
                card_ref=parsed.card_last4,
                currency=parsed.currency,
                total_due_minor=parsed.total_due_minor,
                minimum_due_minor=parsed.minimum_due_minor,
            ),
        ),
        transactions=parsed.transactions,
        credit_limit_minor=parsed.credit_limit_minor,
        billing_cycle_start=parsed.billing_cycle_start,
        billing_cycle_end=parsed.billing_cycle_end,
        warnings=parsed.warnings,
    )


def statement_from_billing_unit(
    document: ParsedBillingDocument,
    unit: ParsedBillingUnit,
    transactions: tuple[ParsedTransaction, ...],
) -> ParsedStatement:
    """Build the per-unit statement view consumed by existing import helpers."""

    return ParsedStatement(
        bank_code=document.bank_code,
        card_last4=unit.card_ref,
        statement_date=document.statement_date,
        due_date=document.due_date,
        total_due_minor=unit.total_due_minor,
        minimum_due_minor=unit.minimum_due_minor,
        currency=unit.currency,
        credit_limit_minor=document.credit_limit_minor,
        billing_cycle_start=document.billing_cycle_start,
        billing_cycle_end=document.billing_cycle_end,
        transactions=transactions,
        warnings=document.warnings,
    )


class BankStatementParser(Protocol):
    bank_code: str
    version: str

    def can_handle(self, source: ParsedSource) -> bool: ...

    def parse(self, source: ParsedSource) -> ParsedStatement: ...
