"""Due-date rule resolution orchestration.

Architecture boundary (frozen):
    Card / Statements → DueDateRuleResolver → EffectiveDueRule → InterestFreeCalculator

This module owns the resolution precedence between card-level explicit rules,
card history analysis, product overrides, and bank known rules. Phase 1 is a
behaviour-preserving extraction from the interest-free-estimate endpoint: the
precedence chain and reason codes are identical to the previous inline logic.

Current precedence (Phase 3):
    explicit Card rule (manual P0) > card history (future-behaviour
    determined) > bank billing rule (product_override > bank_default)

Known-rules query semantics (bank layer):
- multiple_known_rules banks resolve per CONFIRMED sub-rule matched by the
  card's statement day (ICBC current map 14-28, ICBC legacy 1->25);
  unassigned official candidates (ABC +19) are never runtime-assigned;
  statement days claimed by conflicting sub-rules stay unresolved.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.models.card import Card
from app.modules.optional.interest_free import target_statement_date as target_statement_date_calc
from app.services.bank_billing_rules import (
    ResolvedBoundaryRule,
    ResolvedDueDateRule,
    resolve_boundary_rule,
    resolve_due_date_rule,
)
from app.services.card_history_rules import (
    HistoryRuleClassification,
    resolve_card_history_due_date_rule,
)


# Endpoint-level unavailability reason codes (public contract, do not rename).
REASON_MISSING_STATEMENT_DAY = "missing_statement_day"
REASON_INVALID_STATEMENT_DAY = "invalid_statement_day"
REASON_INVALID_DUE_DATE_RULE = "invalid_due_date_rule"
REASON_AMBIGUOUS_DUE_DATE_RULE = "ambiguous_due_date_rule"
REASON_CONFLICTING_DUE_DATE_RULE = "conflicting_due_date_rule"
REASON_UNRESOLVED_DUE_DATE_RULE = "unresolved_due_date_rule"

_RULE_SOURCE_MANUAL = "manual"
_RULE_SOURCE_HISTORY = "history"
_RULE_SOURCE_PRODUCT = "product"
_RULE_SOURCE_BANK = "bank"

# Legacy constants for backwards compatibility with tests importing private aliases
_RULE_SOURCE_EXPLICIT_CARD = "explicit_card"
_RULE_SOURCE_BANK_BILLING_RULE = "bank_billing_rule"
_RULE_SOURCE_CARD_HISTORY = "card_history"


@dataclass(frozen=True)
class EffectiveDueRule:
    """A future-forecast-ready due-date rule plus its resolution provenance."""

    rule_type: str
    rule_value: int
    rule_source: str  # internal: "manual" | "history" | "product" | "bank"
    boundary: str
    product_name: Optional[str] = None
    history_evidence_count: Optional[int] = None
    last_verified_date: Optional[date] = None

    @property
    def api_rule_source(self) -> str:
        """Map internal provenance to public API schema literal."""
        mapping = {
            _RULE_SOURCE_MANUAL: "explicit_card",
            _RULE_SOURCE_HISTORY: "card_history",
            _RULE_SOURCE_PRODUCT: "product_override",
            _RULE_SOURCE_BANK: "bank_billing_rule",
        }
        return mapping.get(self.rule_source, self.rule_source)


@dataclass(frozen=True)
class RuleResolution:
    """Outcome of resolving a card's effective future due-date rule.

    Either `rule` is set (resolution succeeded) or `reason` carries the
    endpoint-level unavailability code.
    """

    rule: Optional[EffectiveDueRule] = None
    reason: Optional[str] = None


def _explicit_rule_is_valid(card: Card) -> bool:
    """Mirror the CardUpdate pair validation used by the endpoint.

    Reuses the schema validation without importing the API layer: the checks
    are the same invariant enforced by the DB CHECK constraints
    (observed_only -> value None; fixed_day 1..31; days_after_statement 1..100).
    """
    rule_type = card.due_date_rule_type
    rule_value = card.due_date_rule_value
    if rule_type == "observed_only":
        return rule_value is None
    if rule_type == "fixed_day":
        return isinstance(rule_value, int) and 1 <= rule_value <= 31
    if rule_type == "days_after_statement":
        return isinstance(rule_value, int) and 1 <= rule_value <= 100
    return False


def resolve_effective_due_rule(
    session: Session,
    card: Card,
    *,
    spend_date: Optional[date] = None,
    target_statement_date: Optional[date] = None,
    history_pairs: Optional[list[tuple[date | None, date | None]]] = None,
) -> RuleResolution:
    """Resolve the effective future due-date rule for a card.

    Precedence chain (Phase 3 + Phase 7):
    statement-day preconditions, explicit card rule (manual P0), card history
    (future-behaviour determined for target_statement_date / 60-month window),
    product override, bank default (multiple_known_rules queried by statement day),
    and statement-day boundary fallback for `uncertain` cards.
    """
    if card.statement_day is None:
        return RuleResolution(reason=REASON_MISSING_STATEMENT_DAY)
    if not 1 <= card.statement_day <= 31:
        return RuleResolution(reason=REASON_INVALID_STATEMENT_DAY)
    if not _explicit_rule_is_valid(card):
        return RuleResolution(reason=REASON_INVALID_DUE_DATE_RULE)

    boundary = card.statement_day_boundary
    if boundary == "uncertain":
        resolved_boundary = resolve_boundary_rule(card.bank_code)
        if resolved_boundary is not None:
            boundary = resolved_boundary.boundary

    if target_statement_date is None and spend_date is not None:
        try:
            target_statement_date = target_statement_date_calc(
                spend_date=spend_date,
                statement_day=card.statement_day,
                boundary_state=boundary,
            )
        except (ValueError, TypeError):
            pass

    rule_type = card.due_date_rule_type
    rule_value = card.due_date_rule_value
    rule_source = _RULE_SOURCE_MANUAL
    product_name: Optional[str] = None
    history_evidence_count: Optional[int] = None
    last_verified_date: Optional[date] = None

    if rule_type == "observed_only":
        # Precedence: card history (future behaviour determined) beats product
        # override and bank known rules; bank resolution itself prefers
        # product_override over bank_default.
        history_analysis = resolve_card_history_due_date_rule(
            session,
            card.id,
            card.statement_day,
            target_statement_date=target_statement_date,
            history_pairs=history_pairs,
            bank_code=card.bank_code,
        )
        if history_analysis.rule is not None:
            rule_type = history_analysis.rule.rule_type
            rule_value = history_analysis.rule.rule_value
            rule_source = _RULE_SOURCE_HISTORY
            history_evidence_count = history_analysis.applicable_pair_count
            last_verified_date = history_analysis.last_verified_date
        elif history_analysis.classification == HistoryRuleClassification.CONFLICTING:
            return RuleResolution(reason=REASON_CONFLICTING_DUE_DATE_RULE)
        elif (
            history_analysis.classification
            == HistoryRuleClassification.AMBIGUOUS_EQUIVALENT
        ):
            return RuleResolution(reason=REASON_AMBIGUOUS_DUE_DATE_RULE)
        else:  # INSUFFICIENT
            resolved_rule = resolve_due_date_rule(
                card.bank_code,
                card.statement_day,
                card.card_product,
            )
            if resolved_rule is None:
                return RuleResolution(reason=REASON_UNRESOLVED_DUE_DATE_RULE)
            rule_type = resolved_rule.rule_type
            rule_value = resolved_rule.rule_value
            if resolved_rule.source == "product_override":
                rule_source = _RULE_SOURCE_PRODUCT
                product_name = resolved_rule.product_name
            else:
                rule_source = _RULE_SOURCE_BANK

    return RuleResolution(
        rule=EffectiveDueRule(
            rule_type=rule_type,
            rule_value=rule_value,
            rule_source=rule_source,
            boundary=boundary,
            product_name=product_name,
            history_evidence_count=history_evidence_count,
            last_verified_date=last_verified_date,
        )
    )
