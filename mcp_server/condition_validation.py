"""Validate workflow conditions against the trigger form fields."""
from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation


EMPTY_VALUE_OPERATORS = {"isEmpty", "isFilled"}
NUMERIC_VALUE_OPERATORS = {
    "quantityEquals", "quantityNotEquals", "quantityLess", "quantityGreater",
    "lessThan", "greaterThan",
}
ALLOWED_OPERATORS = {
    "equals", "notEquals", "isEmpty", "isFilled", "equal", "contains",
    "startsWith", "notEndsWith", "notStartsWith", "endsWith", "notContains",
    "notEqualCountry", "equalCountry", "notEqualState", "equalState",
    "equalDay", "notEqualDay", "quantityEquals", "quantityNotEquals",
    "quantityLess", "quantityGreater", "lessThan", "greaterThan", "before",
    "after", "equalDate", "notEqualDate",
}


class ConditionValidationError(ValueError):
    """A condition that is well-shaped but invalid for the trigger form."""

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


def options_for_question(question: dict) -> list[str]:
    raw = question.get("options")
    if isinstance(raw, str):
        return [item.strip() for item in raw.split("|") if item.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def validate_terms(
    questions: dict,
    terms: Iterable[dict],
    *,
    form_id: str,
    context: str,
) -> None:
    """Require real field ids, supported operators, and exact choice values."""
    for term in terms:
        field_id = str(term.get("field", ""))
        question = (questions or {}).get(field_id)
        if not isinstance(question, dict):
            raise ConditionValidationError(
                f"Field {field_id!r} in {context} is not on trigger form {form_id}.",
                hint="Call get_form_fields and use one of its field_id values.",
            )

        operator = term.get("operator")
        if operator not in ALLOWED_OPERATORS:
            raise ConditionValidationError(
                f"{operator!r} is not a supported condition operator in {context}.",
                hint=f"Allowed operators: {sorted(ALLOWED_OPERATORS)}",
            )

        value = str(term.get("value", ""))
        if operator in NUMERIC_VALUE_OPERATORS:
            try:
                numeric_value = Decimal(value.strip())
            except InvalidOperation:
                numeric_value = None
            if numeric_value is None or not numeric_value.is_finite():
                raise ConditionValidationError(
                    f"{value!r} is not a valid numeric value for operator {operator!r} in {context}.",
                    hint="Use a plain number such as 4, 4.5, or -2.",
                )

        options = options_for_question(question)
        if options and operator not in EMPTY_VALUE_OPERATORS and value not in options:
            label = question.get("text") or field_id
            raise ConditionValidationError(
                f"{value!r} is not an exact option for {label!r} in {context}.",
                hint=f"Allowed values: {options}",
            )


def validate_branch_outcomes(
    questions: dict,
    outcomes: Iterable[dict],
    *,
    form_id: str,
) -> None:
    for outcome in outcomes:
        if not isinstance(outcome, dict) or outcome.get("conditionValue") != "CUSTOM":
            continue
        validate_terms(
            questions,
            outcome.get("conditionTerms") or [],
            form_id=form_id,
            context=f"branch {outcome.get('branchName')!r}",
        )
