import calendar
import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.card import Card
from app.models.import_record import ImportRecord, ImportStatus
from app.models.statement import ParseStatus, Statement
from app.models.transaction import Transaction
from app.modules.core.transaction import classify_transaction
from app.parsers import default_registry, parse_eml_bytes
from app.parsers.base import (
    BankStatementParser,
    ParsedBillingDocument,
    ParsedBillingUnit,
    ParsedSource,
    ParsedStatement,
    ParsedTransaction,
    ParserError,
    billing_document_from_statement,
    statement_from_billing_unit,
)
from app.parsers.boc import BocStatementParser
from app.parsers.citic import CiticStatementParser
from app.schemas.import_record import ImportResult
from app.services.card_blacklist import is_card_ignored
from app.services.statement_date_semantics import (
    parsed_account_statement_day,
    parsed_statement_day_boundary,
)
from app.services.statements import local_today, statement_read

_BANK_NAMES = {
    "CGB": "广发银行",
    "CMB": "招商银行",
    "PAB": "平安银行",
    "CITIC": "中信银行",
    "ICBC": "中国工商银行",
    "CCB": "中国建设银行",
    "BOCOM": "交通银行",
    "ABC": "中国农业银行",
    "BOC": "中国银行",
}


@dataclass(slots=True)
class _PlannedUnit:
    unit: ParsedBillingUnit
    transactions: tuple[ParsedTransaction, ...]
    parsed: ParsedStatement
    card: Card | None = None
    existing: Statement | None = None


def _refresh_card_metadata(
    session: Session, card: Card, parsed: ParsedStatement
) -> bool:
    """仅用同卡最新一期账单更新卡片级元数据，不以银行维度混合。"""
    statement_date = parsed.statement_date
    newest_statement_date = session.scalar(
        select(Statement.statement_date)
        .where(Statement.card_id == card.id)
        .order_by(Statement.statement_date.desc())
        .limit(1)
    )
    if newest_statement_date and newest_statement_date > statement_date:
        return False

    semantic_statement_day = parsed_account_statement_day(parsed)
    semantic_boundary = parsed_statement_day_boundary(parsed)
    if semantic_statement_day is not None:
        if card.statement_day is None:
            card.statement_day = semantic_statement_day
        if card.statement_day == semantic_statement_day and semantic_boundary is not None:
            card.statement_day_boundary = semantic_boundary
    elif card.statement_day is not None:
        expected_day = min(
            card.statement_day,
            calendar.monthrange(statement_date.year, statement_date.month)[1],
        )
        if statement_date.day != expected_day:
            card.due_date_rule_type = "observed_only"
            card.due_date_rule_value = None
            card.statement_day_boundary = "uncertain"
            card.statement_day = statement_date.day
    else:
        card.statement_day = statement_date.day
    card.due_day = parsed.due_date.day
    if parsed.credit_limit_minor is not None:
        card.credit_limit_minor = parsed.credit_limit_minor
    return True


def _apply_annual_fee_transactions(
    session: Session,
    statement_card: Card | None,
    parsed: ParsedStatement,
    *,
    is_latest_statement: bool,
    document_bank_code: str,
) -> None:
    """Observed statement charges override dictionary values, never manual values.

    For CCB or multi-card statements, the physical transaction's card_ref (if present)
    attributes the fee to the specific physical Card, not necessarily statement_card.
    """
    candidates: list[ParsedTransaction] = []
    for item in parsed.transactions:
        description = item.raw_description or ""
        if (
            item.amount_minor > 0
            and ("年费" in description or "annual fee" in description.lower())
            and (item.transaction_date or item.posting_date)
        ):
            candidates.append(item)
    if not candidates:
        return

    # Sort ascending by charged date so that the latest transaction wins for each target card
    candidates.sort(
        key=lambda value: value.transaction_date or value.posting_date or date.min
    )

    for item in candidates:
        target_card = statement_card
        card_ref = _normalized_transaction_card_ref(document_bank_code, item.card_ref)
        if card_ref is not None:
            matched_cards = session.scalars(
                select(Card).where(
                    Card.bank_code == document_bank_code,
                    Card.last4 == card_ref,
                )
            ).all()
            if len(matched_cards) == 1:
                target_card = matched_cards[0]
            elif len(matched_cards) > 1:
                continue
            else:
                target_card = None

        if target_card is None:
            continue
        if target_card.annual_fee_source == "manual":
            continue
        if target_card.annual_fee_source is None and any(
            value is not None
            for value in (
                target_card.annual_fee_minor,
                target_card.annual_fee_policy,
                target_card.annual_fee_charge_date,
            )
        ):
            target_card.annual_fee_source = "manual"
            continue
        if target_card.annual_fee_source == "auto" and not is_latest_statement:
            continue

        charged_on = item.transaction_date or item.posting_date
        if charged_on is not None:
            target_card.annual_fee_charge_date = f"每年{charged_on.month}月{charged_on.day}日"
        target_card.annual_fee_minor = item.amount_minor
        target_card.annual_fee_source = "auto"


def _transaction_identity(item: object) -> tuple[object, ...]:
    return (
        getattr(item, "transaction_date"),
        getattr(item, "posting_date"),
        getattr(item, "amount_minor"),
        getattr(item, "currency"),
        " ".join((getattr(item, "raw_description") or "").split()),
    )


def _resolve_transaction_card_id(
    session: Session,
    bank_code: str,
    item: ParsedTransaction,
    statement_card: Card | None,
) -> str | None:
    """Resolve physical card attribution (Transaction.card_id).

    For CCB:
      - normalize CCB card_ref
      - lookup Card by bank_code + last4
      - exactly 1 match -> Transaction.card_id = matched Card.id
      - 0 match -> card_id = None (physical card UNKNOWN, NO auto Card creation)
      - >1 match -> safe fail (raise ParserError with AMBIGUOUS_CARD_REFERENCE)
      - Never fallback to statement.card_id!
    For legacy single-card banks:
      - statement_card.id if statement_card else None
    """
    if bank_code == "CCB":
        card_ref = _normalized_transaction_card_ref(bank_code, item.card_ref)
        if card_ref is None:
            return None
        cards = session.scalars(
            select(Card).where(
                Card.bank_code == bank_code,
                Card.last4 == card_ref,
            )
        ).all()
        if len(cards) > 1:
            raise ParserError(
                f"provider=CCB; reason=AMBIGUOUS_CARD_REFERENCE; card_ref={card_ref}"
            )
        if len(cards) == 1:
            return cards[0].id
        return None

    return statement_card.id if statement_card else None


def _transaction_from_parsed(
    session: Session,
    statement_id: str,
    item: ParsedTransaction,
    bank_code: str,
    statement_card: Card | None,
) -> Transaction:
    classification = classify_transaction(item.amount_minor, item.raw_description)
    card_id = _resolve_transaction_card_id(
        session, bank_code, item, statement_card
    )
    return Transaction(
        statement_id=statement_id,
        card_id=card_id,
        transaction_date=item.transaction_date,
        posting_date=item.posting_date,
        amount_minor=item.amount_minor,
        currency=item.currency,
        raw_description=item.raw_description,
        transaction_type=classification.transaction_type.value,
        classification_source=classification.source.value,
        classification_rule_version=classification.rule_version,
    )


def _statement_parse_status(parsed: ParsedStatement) -> ParseStatus:
    transaction_warning = any(
        token in warning.lower()
        for warning in parsed.warnings
        for token in ("transaction", "流水", "交易")
    )
    return (
        ParseStatus.COMPLETE
        if parsed.transactions and not transaction_warning
        else ParseStatus.TRANSACTIONS_PARTIAL
    )


def _parse_document(
    parser: BankStatementParser, source: ParsedSource
) -> ParsedBillingDocument:
    parse_document = getattr(parser, "parse_document", None)
    if callable(parse_document):
        document = parse_document(source)
        if not isinstance(document, ParsedBillingDocument):
            raise TypeError("parse_document() must return ParsedBillingDocument")
    else:
        document = billing_document_from_statement(parser.parse(source))
    _reject_unproven_foreign_currency(document, source)
    return document


# Only CCB foreign-currency settlement semantics (Sett.Curr/Sett.Amt) have been
# proven. Any other parser emitting a non-CNY billing unit must safe-fail until
# its settlement semantics are explicitly proven (Phase 2 boundary).
_PROVEN_FOREIGN_CURRENCIES: dict[str, frozenset[str]] = {
    "CCB": frozenset({"CNY", "USD", "EUR"}),
}


def _reject_unproven_foreign_currency(
    document: ParsedBillingDocument, source: ParsedSource
) -> None:
    allowed = _PROVEN_FOREIGN_CURRENCIES.get(document.bank_code, frozenset({"CNY"}))
    for unit in document.billing_units:
        currency = unit.currency.strip().upper()
        if currency not in allowed:
            raise ParserError(
                "; ".join(
                    [
                        f"provider={document.bank_code}",
                        f"source={source.sha256[:12]}",
                        "reason=UNPROVEN_FOREIGN_CURRENCY",
                        f"currency={currency}",
                    ]
                )
            )
    for summary in document.account_summaries:
        currency = summary.currency.strip().upper()
        if currency not in allowed:
            raise ParserError(
                "; ".join(
                    [
                        f"provider={document.bank_code}",
                        f"source={source.sha256[:12]}",
                        "reason=UNPROVEN_FOREIGN_CURRENCY",
                        f"currency={currency}",
                    ]
                )
            )


def _normalized_unit_card_ref(bank_code: str, card_ref: str) -> str | None:
    value = card_ref.strip()
    if len(value) == 4 and value.isdigit():
        return value
    if bank_code == "CCB" and value.endswith("/扫码"):
        prefix = value.removesuffix("/扫码")
        if len(prefix) == 4 and prefix.isdigit():
            return prefix
    return None


def _normalized_transaction_card_ref(
    bank_code: str, card_ref: str | None
) -> str | None:
    if card_ref is None:
        return None
    return _normalized_unit_card_ref(bank_code, card_ref)


def _gate_error(
    document: ParsedBillingDocument,
    source: ParsedSource,
    reason: str,
    *,
    card_ref: str | None = None,
    currency: str | None = None,
) -> ParserError:
    details = [
        f"provider={document.bank_code}",
        f"source={source.sha256[:12]}",
        f"reason={reason}",
    ]
    if card_ref is not None:
        details.append(f"card_ref={card_ref}")
    if currency is not None:
        details.append(f"currency={currency}")
    return ParserError("; ".join(details))


def _plan_document(
    document: ParsedBillingDocument, source: ParsedSource
) -> list[_PlannedUnit]:
    if document.bank_code == "CCB" and not document.billing_units:
        cny_summary = next(
            (s for s in document.account_summaries if s.currency.strip().upper() == "CNY"),
            None,
        )
        if cny_summary is None or len(document.account_summaries) != 1:
            raise _gate_error(
                document,
                source,
                "MISSING_CNY_ACCOUNT_SUMMARY" if cny_summary is None else "MULTIPLE_ACCOUNT_SUMMARIES",
            )

        unit = ParsedBillingUnit(
            card_ref="",
            currency="CNY",
            total_due_minor=cny_summary.total_due_minor,
            minimum_due_minor=cny_summary.minimum_due_minor,
        )
        parsed = ParsedStatement(
            bank_code=document.bank_code,
            card_last4="",
            statement_date=document.statement_date,
            due_date=document.due_date,
            total_due_minor=cny_summary.total_due_minor,
            minimum_due_minor=cny_summary.minimum_due_minor,
            currency="CNY",
            credit_limit_minor=document.credit_limit_minor,
            billing_cycle_start=document.billing_cycle_start,
            billing_cycle_end=document.billing_cycle_end,
            transactions=document.transactions,
            warnings=document.warnings,
        )
        return [
            _PlannedUnit(
                unit=unit,
                transactions=document.transactions,
                parsed=parsed,
                card=None,
            )
        ]

    if not document.billing_units:
        raise _gate_error(document, source, "NO_BILLING_UNITS")

    normalized_units: list[ParsedBillingUnit] = []
    unit_indexes: dict[tuple[str, str], int] = {}
    for unit in document.billing_units:
        card_ref = _normalized_unit_card_ref(document.bank_code, unit.card_ref)
        currency = unit.currency.strip().upper()
        if card_ref is None:
            raise _gate_error(
                document,
                source,
                "UNRESOLVED_BILLING_UNIT_CARD_REFERENCE",
                card_ref=unit.card_ref,
                currency=currency,
            )
        key = (card_ref, currency)
        if key in unit_indexes:
            raise _gate_error(
                document,
                source,
                "DUPLICATE_BILLING_UNIT_KEY",
                card_ref=card_ref,
                currency=currency,
            )
        unit_indexes[key] = len(normalized_units)
        normalized_units.append(
            ParsedBillingUnit(
                card_ref=card_ref,
                currency=currency,
                total_due_minor=unit.total_due_minor,
                minimum_due_minor=unit.minimum_due_minor,
            )
        )

    seen_summary_currencies: set[str] = set()
    for summary in document.account_summaries:
        currency = summary.currency.strip().upper()
        if currency in seen_summary_currencies:
            raise _gate_error(
                document,
                source,
                "DUPLICATE_ACCOUNT_SUMMARY",
                currency=currency,
            )
        seen_summary_currencies.add(currency)
        units_total = sum(
            unit.total_due_minor
            for unit in normalized_units
            if unit.currency == currency
        )
        if units_total != summary.total_due_minor:
            raise _gate_error(
                document,
                source,
                f"ACCOUNT_SUMMARY_MISMATCH expected={summary.total_due_minor} actual={units_total}",
                currency=currency,
            )

    assigned: list[list[ParsedTransaction]] = [
        [] for _unit in normalized_units
    ]
    if len(normalized_units) == 1:
        assigned[0].extend(document.transactions)
    else:
        for item in document.transactions:
            card_ref = _normalized_transaction_card_ref(
                document.bank_code, item.card_ref
            )
            currency = item.currency.strip().upper()
            if card_ref is None or (card_ref, currency) not in unit_indexes:
                raise _gate_error(
                    document,
                    source,
                    "UNRESOLVED_CARD_REFERENCE",
                    card_ref=item.card_ref,
                    currency=currency,
                )
            assigned[unit_indexes[(card_ref, currency)]].append(item)

    if sum(len(items) for items in assigned) != len(document.transactions):
        raise _gate_error(document, source, "TRANSACTION_ASSIGNMENT_COUNT_MISMATCH")

    return [
        _PlannedUnit(
            unit=unit,
            transactions=tuple(assigned[index]),
            parsed=statement_from_billing_unit(
                document, unit, tuple(assigned[index])
            ),
        )
        for index, unit in enumerate(normalized_units)
    ]


def _resolve_card(
    session: Session,
    document: ParsedBillingDocument,
    source: ParsedSource,
    unit: ParsedBillingUnit,
    selected_card: Card | None,
) -> Card:
    if is_card_ignored(
        session, document.bank_code, unit.card_ref, unit.currency
    ):
        raise _gate_error(
            document,
            source,
            "CARD_IGNORED",
            card_ref=unit.card_ref,
            currency=unit.currency,
        )

    cards = session.scalars(
        select(Card).where(
            Card.bank_code == document.bank_code,
            Card.last4 == unit.card_ref,
        )
    ).all()
    if len(cards) > 1:
        if selected_card is not None and any(
            card.id == selected_card.id for card in cards
        ):
            return selected_card
        raise _gate_error(
            document,
            source,
            "AMBIGUOUS_CARD_REFERENCE",
            card_ref=unit.card_ref,
            currency=unit.currency,
        )
    if cards:
        return cards[0]

    bank_name = _BANK_NAMES.get(document.bank_code, document.bank_code)
    card = Card(
        bank_code=document.bank_code,
        bank_name=bank_name,
        display_name=f"{bank_name} ****{unit.card_ref}",
        last4=unit.card_ref,
        currency=unit.currency,
    )
    session.add(card)
    session.flush()
    return card


def _statements_by_source(session: Session, import_id: str) -> list[Statement]:
    return list(
        session.scalars(
            select(Statement)
            .options(
                selectinload(Statement.card),
                selectinload(Statement.repayments),
                selectinload(Statement.transactions),
            )
            .where(Statement.source_import_id == import_id)
        ).all()
    )


def _reparse_existing_source(
    session: Session, record: ImportRecord, source: ParsedSource
) -> list[Statement] | None:
    """Refresh only an exact existing topology; never perform historical splitting."""
    statements = _statements_by_source(session, record.id)
    if not statements:
        return None
    try:
        parser = default_registry.parser_for(source)
        document = _parse_document(parser, source)
        planned = _plan_document(document, source)
    except ParserError:
        return statements

    existing_by_key = {
        (
            statement.bank_code,
            statement.card.last4 if statement.card else "",
            statement.currency,
            statement.statement_date,
        ): statement
        for statement in statements
    }
    planned_by_key = {
        (
            document.bank_code,
            item.unit.card_ref,
            item.unit.currency,
            document.statement_date,
        ): item
        for item in planned
    }
    if set(existing_by_key) != set(planned_by_key):
        # A changed unit topology would be a historical repair, which Phase 1 forbids.
        return statements

    for key, item in planned_by_key.items():
        statement = existing_by_key[key]
        item.card = statement.card
        if statement.card is not None:
            is_latest = _refresh_card_metadata(session, statement.card, item.parsed)
            _apply_annual_fee_transactions(
                session,
                statement.card,
                item.parsed,
                is_latest_statement=is_latest,
                document_bank_code=document.bank_code,
            )
        else:
            _apply_annual_fee_transactions(
                session,
                None,
                item.parsed,
                is_latest_statement=True,
                document_bank_code=document.bank_code,
            )
        statement.due_date = item.parsed.due_date
        statement.total_due_minor = item.parsed.total_due_minor
        statement.minimum_due_minor = item.parsed.minimum_due_minor
        statement.parse_status = _statement_parse_status(item.parsed)

        existing = Counter(
            _transaction_identity(transaction)
            for transaction in statement.transactions
        )
        for transaction in item.transactions:
            identity = _transaction_identity(transaction)
            if existing[identity]:
                existing[identity] -= 1
                continue
            session.add(
                _transaction_from_parsed(
                    session,
                    statement.id,
                    transaction,
                    document.bank_code,
                    statement.card,
                )
            )

    record.bank_code = document.bank_code
    record.parser_version = parser.version
    record.parse_status = ImportStatus.SUMMARY_OK
    record.error_message = None
    session.commit()
    return _statements_by_source(session, record.id)


def _result(
    session: Session,
    record: ImportRecord,
    statements: list[Statement],
    *,
    deduplicated: bool,
) -> ImportResult:
    if not statements:
        raise RuntimeError("Imported statements could not be reloaded")
    ordered = sorted(
        statements,
        key=lambda statement: (
            statement.card.last4 if statement.card else "",
            statement.currency,
            statement.id,
        ),
    )
    today = local_today(session.info["timezone"])
    reads = [statement_read(statement, today) for statement in ordered]
    return ImportResult(
        import_id=record.id,
        bank_code=record.bank_code or "",
        parser_version=record.parser_version or "",
        deduplicated=deduplicated,
        statement=reads[0],
        statements=reads,
    )


def _mark_failed_import(
    session: Session,
    source: ParsedSource,
    error: ParserError,
    *,
    bank_code: str | None = None,
    parser_version: str | None = None,
) -> None:
    session.rollback()
    record = session.scalar(
        select(ImportRecord).where(ImportRecord.source_sha256 == source.sha256)
    )
    if record is None:
        record = ImportRecord(
            source_type=source.source_type,
            source_sha256=source.sha256,
            message_id=source.message_id,
            parse_status=ImportStatus.FAILED,
        )
        session.add(record)
    record.bank_code = bank_code or record.bank_code
    record.parser_version = parser_version or record.parser_version
    record.parse_status = ImportStatus.FAILED
    record.error_message = str(error)
    session.commit()


def _deduplicated_result_for_source(
    session: Session, source: ParsedSource
) -> ImportResult | None:
    """Return the already-imported statements for an exact source, or None.

    Handles both the tombstone gate and the dedup path that re-parses an
    existing successful import. Shared by the EML and bare-PDF import cores so
    a source arriving over either channel is deduplicated identically.
    """
    record = session.scalar(
        select(ImportRecord).where(ImportRecord.source_sha256 == source.sha256)
    )
    if record and record.parse_status == ImportStatus.DELETED_BY_USER:
        raise ParserError("Source was deleted by user (tombstone)")
    if record and record.parse_status == ImportStatus.SUMMARY_OK:
        statements = _reparse_existing_source(session, record, source)
        if statements:
            return _result(session, record, statements, deduplicated=True)
    return None


def import_parsed_source(
    session: Session,
    source: ParsedSource,
    *,
    document: ParsedBillingDocument | None = None,
    parser: BankStatementParser | None = None,
    card_id: str | None = None,
) -> ImportResult:
    """Shared persistence core for a fully-parsed source (EML or bare PDF).

    Dedup/Tombstone, document planning, card resolution, Statement/Transaction
    creation, and failed-import bookkeeping are identical for both channels so
    EML and manual PDF imports can never diverge in semantics. When ``document``
    is None the source is parsed through the registry (EML path); a pre-parsed
    billing document may be supplied instead (bare-PDF path, which no registry
    parser can recognize by sender/subject).
    """
    record = _deduplicated_result_for_source(session, source)
    if record is not None:
        return record

    record = session.scalar(
        select(ImportRecord).where(ImportRecord.source_sha256 == source.sha256)
    )
    try:
        if document is None:
            parser = default_registry.parser_for(source)
            document = _parse_document(parser, source)
        planned = _plan_document(document, source)

        selected_card = session.get(Card, card_id) if card_id else None
        if card_id and selected_card is None:
            raise _gate_error(document, source, "SELECTED_CARD_NOT_FOUND")
        if selected_card is not None and document.bank_code != "CCB":
            selected_key = (selected_card.bank_code, selected_card.last4)
            unit_keys = {
                (document.bank_code, item.unit.card_ref) for item in planned
            }
            if selected_key not in unit_keys:
                raise _gate_error(
                    document,
                    source,
                    "SELECTED_CARD_DOES_NOT_MATCH_DOCUMENT",
                    card_ref=selected_card.last4,
                )

        for item in planned:
            if document.bank_code == "CCB" and not document.billing_units:
                item.card = None
                continue
            unit_selected_card = (
                selected_card
                if selected_card is not None
                and selected_card.bank_code == document.bank_code
                and selected_card.last4 == item.unit.card_ref
                else None
            )
            item.card = _resolve_card(
                session,
                document,
                source,
                item.unit,
                unit_selected_card,
            )

        for item in planned:
            if item.card is None and not (document.bank_code == "CCB" and not document.billing_units):
                raise RuntimeError("Billing unit card resolution was incomplete")
            if item.card is not None:
                item.existing = session.scalar(
                    select(Statement)
                    .options(
                        selectinload(Statement.card),
                        selectinload(Statement.repayments),
                        selectinload(Statement.transactions),
                    )
                    .where(
                        Statement.card_id == item.card.id,
                        Statement.statement_date == document.statement_date,
                        Statement.currency == item.unit.currency,
                    )
                )
            else:
                item.existing = session.scalar(
                    select(Statement)
                    .options(
                        selectinload(Statement.card),
                        selectinload(Statement.repayments),
                        selectinload(Statement.transactions),
                    )
                    .where(
                        Statement.bank_code == document.bank_code,
                        Statement.card_id.is_(None),
                        Statement.statement_date == document.statement_date,
                        Statement.currency == item.unit.currency,
                    )
                )
        existing_count = sum(item.existing is not None for item in planned)
        if existing_count not in (0, len(planned)):
            raise _gate_error(document, source, "PARTIAL_DUPLICATE_STATEMENT_SET")

        if record is None:
            record = ImportRecord(
                source_type=source.source_type,
                source_sha256=source.sha256,
                message_id=source.message_id,
                parse_status=ImportStatus.SUMMARY_OK,
            )
            session.add(record)
            session.flush()
        record.bank_code = document.bank_code
        record.parser_version = parser.version
        record.error_message = None

        if existing_count == len(planned):
            record.parse_status = ImportStatus.DUPLICATE
            session.commit()
            return _result(
                session,
                record,
                [item.existing for item in planned if item.existing is not None],
                deduplicated=True,
            )

        for item in planned:
            if item.card is not None:
                is_latest = _refresh_card_metadata(session, item.card, item.parsed)
                _apply_annual_fee_transactions(
                    session,
                    item.card,
                    item.parsed,
                    is_latest_statement=is_latest,
                    document_bank_code=document.bank_code,
                )
            else:
                _apply_annual_fee_transactions(
                    session,
                    None,
                    item.parsed,
                    is_latest_statement=True,
                    document_bank_code=document.bank_code,
                )
            statement = Statement(
                bank_code=document.bank_code,
                card_id=item.card.id if item.card else None,
                source_import_id=record.id,
                statement_date=document.statement_date,
                due_date=document.due_date,
                total_due_minor=item.unit.total_due_minor,
                minimum_due_minor=item.unit.minimum_due_minor,
                currency=item.unit.currency,
                parse_status=_statement_parse_status(item.parsed),
            )
            session.add(statement)
            session.flush()
            session.add_all(
                [
                    _transaction_from_parsed(
                        session,
                        statement.id,
                        transaction,
                        document.bank_code,
                        item.card,
                    )
                    for transaction in item.transactions
                ]
            )

        record.parse_status = ImportStatus.SUMMARY_OK
        session.commit()
        loaded = _statements_by_source(session, record.id)
        return _result(session, record, loaded, deduplicated=False)
    except ParserError as error:
        _mark_failed_import(
            session,
            source,
            error,
            bank_code=document.bank_code if document else None,
            parser_version=getattr(parser, "version", None),
        )
        raise


def import_eml_data(
    session: Session, data: bytes, *, card_id: str | None = None
) -> ImportResult:
    """Import a bank statement from raw EML bytes (thin wrapper)."""
    source = parse_eml_bytes(data)
    return import_parsed_source(session, source, card_id=card_id)


def import_pdf_bytes(
    session: Session, data: bytes, *, card_id: str | None = None
) -> ImportResult:
    """Import a supported bare statement PDF through one proven bank parser.

    Bare PDFs do not carry trustworthy mail sender/subject metadata, so each
    proven pure-PDF parser gets one chance. Exactly one parser must succeed:
    unsupported or ambiguous documents fail closed before persistence.
    """
    source = ParsedSource(
        source_type="pdf",
        subject="",
        sender="",
        html="",
        sha256=hashlib.sha256(data).hexdigest(),
        raw_bytes=data,
    )

    matches: list[tuple[BankStatementParser, ParsedBillingDocument]] = []
    for parser in (BocStatementParser(), CiticStatementParser()):
        try:
            parsed = parser.parse_pdf_bytes(data)
        except ParserError:
            continue
        matches.append((parser, billing_document_from_statement(parsed)))

    if not matches:
        raise ParserError("No supported bank PDF parser recognizes this document")
    if len(matches) > 1:
        raise ParserError("More than one supported bank PDF parser recognizes this document")

    parser, document = matches[0]
    _reject_unproven_foreign_currency(document, source)
    return import_parsed_source(
        session,
        source,
        document=document,
        parser=parser,
        card_id=card_id,
    )
