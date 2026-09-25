"""Service for bank billing rules fact dictionary.

Loads and resolves confirmed credit card billing rules (due date rules,
statement day policies, statement day boundaries) from the canonical dictionary:
server/app/data/bank_billing_rules.json.

Normalizes confirmed facts to existing Card/runtime rule families only:
- fixed_day = N (1 <= N <= 31)
- days_after_statement = N (1 <= N <= 100)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "bank_billing_rules.json"


def _is_strict_int(val: Any) -> bool:
    """Return True if val is a strict int and not a boolean."""
    return isinstance(val, int) and not isinstance(val, bool)


def _validate_due_rule_values(bcode: str, rtype: Any, rval: Any, context: str) -> None:
    if rtype == "fixed_day":
        if not _is_strict_int(rval) or not (1 <= rval <= 31):
            raise ValueError(
                f"{bcode} {context}: fixed_day value must be an integer between 1 and 31, got {rval!r}"
            )
    elif rtype == "days_after_statement":
        if not _is_strict_int(rval) or not (1 <= rval <= 100):
            raise ValueError(
                f"{bcode} {context}: days_after_statement value must be an integer between 1 and 100, got {rval!r}"
            )
    else:
        raise ValueError(f"{bcode} {context}: unsupported rule_type: {rtype!r}")


def _has_bank_level_executable_representation(dd: Dict[str, Any]) -> bool:
    """Return True if the due_date_rule carries a bank-level representation
    that the runtime resolver could execute as a bank default.

    Executable representations:
    - single rule: valid fixed_day / days_after_statement rule_type + value
    - statement_day_map: non-empty map
    - multiple_known_rules: at least one CONFIRMED, runtime-assignable sub-rule
      with a valid map or days_after_statement value
    """
    rep = dd.get("policy_representation")
    if rep == "statement_day_map":
        s_map = dd.get("statement_day_map")
        return isinstance(s_map, dict) and len(s_map) > 0
    if rep == "multiple_known_rules":
        for sub in dd.get("rules", []) or []:
            if not isinstance(sub, dict) or sub.get("status") != "CONFIRMED":
                continue
            if sub.get("runtime_assignable") is False:
                continue
            if sub.get("rule_type") == "statement_day_map":
                s_map = sub.get("statement_day_map")
                if isinstance(s_map, dict) and len(s_map) > 0:
                    return True
            elif sub.get("rule_type") == "days_after_statement":
                rval = sub.get("rule_value")
                if _is_strict_int(rval) and 1 <= rval <= 100:
                    return True
        return False
    rtype = dd.get("rule_type")
    rval = dd.get("rule_value")
    if rtype == "fixed_day":
        return _is_strict_int(rval) and 1 <= rval <= 31
    if rtype == "days_after_statement":
        return _is_strict_int(rval) and 1 <= rval <= 100
    return False


@dataclass(frozen=True)
class ResolvedDueDateRule:
    rule_type: str  # "fixed_day" | "days_after_statement"
    rule_value: int
    source: str     # "bank_default" | "product_override"
    product_name: Optional[str] = None


@dataclass(frozen=True)
class ResolvedBoundaryRule:
    boundary: str   # "current_cycle" | "next_cycle" | "uncertain"


class BankBillingRulesService:
    def __init__(self, data_path: Optional[Path] = None) -> None:
        self.data_path = data_path or _DATA_PATH
        self._rules: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if not self.data_path.exists():
            raise FileNotFoundError(f"Billing rules dictionary not found: {self.data_path}")
        with open(self.data_path, "r", encoding="utf-8") as f:
            self._rules = json.load(f)
        self.validate()

    def validate(self) -> None:
        """Validate structural and runtime safety invariants of the dictionary."""
        included = [
            b for b, r in self._rules.items()
            if r.get("maintenance_scope") == "included"
        ]
        if len(included) != 32:
            raise ValueError(f"Expected exactly 32 included maintenance banks, got {len(included)}")

        p0 = [b for b in included if self._rules[b].get("maintenance_tier") == "P0"]
        p1 = [b for b in included if self._rules[b].get("maintenance_tier") == "P1"]
        if len(p0) != 18 or len(p1) != 14:
            raise ValueError(f"Expected 18 P0 and 14 P1 banks, got P0={len(p0)}, P1={len(p1)}")

        for bcode, bdata in self._rules.items():
            dd = bdata.get("due_date_rule", {})
            st = dd.get("status")

            # Validate optional dual-status model fields (Phase 2)
            rev_st = dd.get("review_status")
            if rev_st is not None and rev_st not in ("CONFIRMED", "NEEDS_REVIEW", "TODO"):
                raise ValueError(f"{bcode} invalid review_status: {rev_st!r}")

            rt_st = dd.get("runtime_status")
            if rt_st is not None and rt_st not in ("RESOLVED", "PARTIAL", "SAFE_UNRESOLVED", "CONFLICT", "TODO"):
                raise ValueError(f"{bcode} invalid runtime_status: {rt_st!r}")

            # Dual-status semantic invariants (Phase 2 hardening)
            if rt_st is not None:
                executable = _has_bank_level_executable_representation(dd)
                if rt_st in ("SAFE_UNRESOLVED", "CONFLICT", "TODO"):
                    if executable:
                        raise ValueError(
                            f"{bcode} runtime_status {rt_st} contradicts a bank-level executable due-date representation"
                        )
                elif rt_st in ("RESOLVED", "PARTIAL"):
                    if not executable:
                        raise ValueError(
                            f"{bcode} runtime_status {rt_st} requires at least one executable bank-level representation"
                        )

            if st == "CONFIRMED":
                rep = dd.get("policy_representation")
                if rep == "multiple_known_rules":
                    pass  # validated in multiple_known_rules section below
                elif rep == "statement_day_map":
                    s_map = dd.get("statement_day_map")
                    if not s_map or not isinstance(s_map, dict):
                        raise ValueError(f"{bcode} confirmed statement_day_map must be a non-empty dict")
                    for k, v in s_map.items():
                        # Map key must be string representation of 1..31
                        if isinstance(k, bool):
                            raise ValueError(f"{bcode} statement_day_map key must not be bool: {k!r}")
                        try:
                            day_int = int(k)
                        except (ValueError, TypeError):
                            raise ValueError(f"{bcode} invalid map key (not integer): {k!r}")
                        if not (1 <= day_int <= 31):
                            raise ValueError(f"{bcode} map key statement day must be between 1 and 31, got {day_int}")

                        # Map value must be strict int 1..31
                        if not _is_strict_int(v) or not (1 <= v <= 31):
                            raise ValueError(
                                f"{bcode} map value due day must be an integer between 1 and 31, got {v!r}"
                            )
                else:
                    rtype = dd.get("rule_type")
                    rval = dd.get("rule_value")
                    _validate_due_rule_values(bcode, rtype, rval, "due_date_rule")
            else:
                # Non-CONFIRMED must not provide runtime_resolution
                if dd.get("runtime_resolution") is not None:
                    raise ValueError(f"{bcode} non-confirmed due rule must not have runtime_resolution")

            # Validate multiple_known_rules sub-rules
            if dd.get("policy_representation") == "multiple_known_rules":
                sub_rules = dd.get("rules", [])
                if not sub_rules or not isinstance(sub_rules, list):
                    raise ValueError(f"{bcode} multiple_known_rules must have a non-empty rules list")
                for sub in sub_rules:
                    if not isinstance(sub, dict):
                        raise ValueError(f"{bcode} sub-rule must be a dict")
                    sub_st = sub.get("status")
                    if sub_st not in ("CONFIRMED", "NEEDS_REVIEW", "TODO"):
                        raise ValueError(f"{bcode} sub-rule {sub.get('rule_id')} invalid status: {sub_st!r}")
                    sub_type = sub.get("rule_type")
                    if sub_type == "statement_day_map":
                        s_map = sub.get("statement_day_map")
                        if not s_map or not isinstance(s_map, dict):
                            raise ValueError(f"{bcode} sub-rule statement_day_map must be a non-empty dict")
                        for k, v in s_map.items():
                            if isinstance(k, bool):
                                raise ValueError(f"{bcode} map key must not be bool: {k!r}")
                            try:
                                day_int = int(k)
                            except (ValueError, TypeError):
                                raise ValueError(f"{bcode} invalid map key: {k!r}")
                            if not (1 <= day_int <= 31):
                                raise ValueError(f"{bcode} map key statement day must be between 1 and 31: {day_int}")
                            if not _is_strict_int(v) or not (1 <= v <= 31):
                                raise ValueError(f"{bcode} map value due day must be integer between 1 and 31: {v!r}")
                    elif sub_type == "days_after_statement":
                        rval = sub.get("rule_value")
                        if not _is_strict_int(rval) or not (1 <= rval <= 100):
                            raise ValueError(f"{bcode} sub-rule days_after_statement value must be between 1 and 100: {rval!r}")
                    else:
                        raise ValueError(f"{bcode} sub-rule unsupported rule_type: {sub_type!r}")

                    if "runtime_assignable" in sub:
                        ra = sub["runtime_assignable"]
                        if not isinstance(ra, bool):
                            raise ValueError(
                                f"{bcode} rule {sub.get('rule_id')} runtime_assignable must be a boolean, got {ra!r}"
                            )

            # Validate confirmed product overrides
            for po in bdata.get("product_overrides", []):
                if po.get("status") == "CONFIRMED":
                    pname = po.get("product_name", "unknown")
                    rtype = po.get("rule_type")
                    rval = po.get("rule_value")
                    _validate_due_rule_values(bcode, rtype, rval, f"product_override({pname})")

            # Check boundary
            bnd = bdata.get("statement_day_boundary", {})
            bval = bnd.get("boundary")
            if bval not in ("current_cycle", "next_cycle", "uncertain"):
                raise ValueError(f"{bcode} invalid boundary: {bval!r}")

    def get_billing_rule(self, bank_code: str) -> Optional[Dict[str, Any]]:
        return self._rules.get(bank_code.strip().upper())

    def list_included_banks(self) -> List[str]:
        return [
            b for b, r in self._rules.items()
            if r.get("maintenance_scope") == "included"
        ]

    def resolve_due_date_rule(
        self,
        bank_code: str,
        statement_day: Optional[int],
        card_product: Optional[str] = None
    ) -> Optional[ResolvedDueDateRule]:
        """Resolve a confirmed due date rule into runtime fixed_day or days_after_statement.

        Gate: Returns None if maintenance_scope != 'included'.

        Resolution order within the bank entry:
        1. Confirmed product overrides (exact product name match); these are
           independent product-level confirmed facts and are NOT blocked by
           the bank-level runtime gate.
        2. Bank-level runtime gate: when runtime_status is present it is the
           sole arbiter (only RESOLVED / PARTIAL may resolve; SAFE_UNRESOLVED,
           CONFLICT, TODO return None). Legacy status is consulted only when
           runtime_status is absent (not-yet-migrated dictionaries).
        3. multiple_known_rules CONFIRMED sub-rules, matched by statement_day
           (each sub-rule must be individually CONFIRMED; exactly one sub-rule
           may claim a given statement day, otherwise unresolved -> None).
        4. Confirmed single bank-level due_date_rule.
        """
        bank_info = self.get_billing_rule(bank_code)
        if not bank_info:
            return None

        # Maintenance scope runtime gate
        if bank_info.get("maintenance_scope") != "included":
            return None

        # 1. Check confirmed product overrides first
        if card_product:
            norm_product = card_product.strip()
            for po in bank_info.get("product_overrides", []):
                if po.get("status") == "CONFIRMED" and po.get("product_name") == norm_product:
                    rtype = po.get("rule_type")
                    rval = po.get("rule_value")
                    if rtype in ("fixed_day", "days_after_statement") and _is_strict_int(rval):
                        return ResolvedDueDateRule(
                            rule_type=rtype,
                            rule_value=rval,
                            source="product_override",
                            product_name=norm_product
                        )

        dd = bank_info.get("due_date_rule", {})

        # 2. Bank-level runtime gate (Phase 2 dual-status model):
        # runtime_status is the sole runtime arbiter when present; legacy
        # status is only consulted for dictionaries not yet migrated.
        runtime_status = dd.get("runtime_status")
        if runtime_status is not None:
            runtime_resolvable = runtime_status in ("RESOLVED", "PARTIAL")
        else:
            runtime_resolvable = dd.get("status") == "CONFIRMED"
        if not runtime_resolvable:
            return None

        # 3. multiple_known_rules: query CONFIRMED sub-rules by statement day
        if dd.get("policy_representation") == "multiple_known_rules":
            return self._resolve_multiple_known_rules(bcode=bank_code, dd=dd, statement_day=statement_day)

        # 4. Bank-level single rule
        rep = dd.get("policy_representation")
        if rep == "statement_day_map":
            if statement_day is None or isinstance(statement_day, bool):
                return None
            s_map = dd.get("statement_day_map", {})
            key = str(statement_day)
            if key not in s_map:
                # Missing map key -> unresolved/no result; do not guess
                return None
            val = s_map[key]
            if not _is_strict_int(val) or not (1 <= val <= 31):
                return None
            return ResolvedDueDateRule(
                rule_type="fixed_day",
                rule_value=val,
                source="bank_default"
            )

        rtype = dd.get("rule_type")
        rval = dd.get("rule_value")
        if rtype in ("fixed_day", "days_after_statement") and _is_strict_int(rval):
            return ResolvedDueDateRule(
                rule_type=rtype,
                rule_value=rval,
                source="bank_default"
            )

        return None

    def _resolve_multiple_known_rules(
        self,
        bcode: str,
        dd: Dict[str, Any],
        statement_day: Optional[int],
    ) -> Optional[ResolvedDueDateRule]:
        """Query CONFIRMED sub-rules of a multiple_known_rules bank by statement day.

        Sub-rule families:
        - statement_day_map sub-rules match only statement days present in the
          map (do NOT extrapolate to unlisted days).
        - days_after_statement sub-rules claim every statement day ONLY when no
          map sub-rule claims the given day; if a sub-rule is marked with
          runtime_assignable=False (e.g. ABC +19), it is an unassigned official
          candidate and is skipped entirely.

        A statement day claimed by more than one sub-rule stays unresolved.
        """
        if statement_day is None or isinstance(statement_day, bool):
            return None
        if not 1 <= statement_day <= 31:
            return None

        matches: List[ResolvedDueDateRule] = []
        map_claimed = False

        for sub in dd.get("rules", []):
            if sub.get("status") != "CONFIRMED":
                continue
            sub_type = sub.get("rule_type")
            if sub_type == "statement_day_map":
                s_map = sub.get("statement_day_map") or {}
                val = s_map.get(str(statement_day))
                if val is None:
                    continue
                if _is_strict_int(val) and 1 <= val <= 31:
                    matches.append(
                        ResolvedDueDateRule(
                            rule_type="fixed_day",
                            rule_value=val,
                            source="bank_default",
                        )
                    )
                    map_claimed = True
            elif sub_type == "days_after_statement":
                # Rules explicitly marked runtime_assignable=False are never runtime-assigned.
                if sub.get("runtime_assignable") is False:
                    continue
                rval = sub.get("rule_value")
                if _is_strict_int(rval) and 1 <= rval <= 100:
                    matches.append(
                        ResolvedDueDateRule(
                            rule_type="days_after_statement",
                            rule_value=rval,
                            source="bank_default",
                        )
                    )

        # A map claim suppresses blanket days_after candidates for that day;
        # if a days_after sub-rule also matched the same day, it is ambiguous.
        if map_claimed:
            map_matches = [
                m for m in matches if m.rule_type == "fixed_day"
            ]
            day_matches = [m for m in matches if m.rule_type == "days_after_statement"]
            if day_matches:
                return None  # blanket candidate conflicts with a map claim
            return map_matches[0] if len(map_matches) == 1 else None

        blanket = [m for m in matches if m.rule_type == "days_after_statement"]
        return blanket[0] if len(blanket) == 1 else None

    def resolve_boundary_rule(self, bank_code: str) -> Optional[ResolvedBoundaryRule]:
        """Resolve confirmed statement day boundary fact.

        Gate: Returns None if maintenance_scope != 'included'.
        """
        bank_info = self.get_billing_rule(bank_code)
        if not bank_info:
            return None

        # Maintenance scope runtime gate
        if bank_info.get("maintenance_scope") != "included":
            return None

        bnd = bank_info.get("statement_day_boundary", {})
        if bnd.get("status") == "CONFIRMED":
            bval = bnd.get("boundary")
            if bval in ("current_cycle", "next_cycle", "uncertain"):
                return ResolvedBoundaryRule(boundary=bval)
        return None


_instance: Optional[BankBillingRulesService] = None


def get_billing_rules_service() -> BankBillingRulesService:
    global _instance
    if _instance is None:
        _instance = BankBillingRulesService()
    return _instance


def get_billing_rule(bank_code: str) -> Optional[Dict[str, Any]]:
    return get_billing_rules_service().get_billing_rule(bank_code)


def resolve_due_date_rule(
    bank_code: str,
    statement_day: Optional[int],
    card_product: Optional[str] = None
) -> Optional[ResolvedDueDateRule]:
    return get_billing_rules_service().resolve_due_date_rule(bank_code, statement_day, card_product)


def resolve_boundary_rule(bank_code: str) -> Optional[ResolvedBoundaryRule]:
    return get_billing_rules_service().resolve_boundary_rule(bank_code)
