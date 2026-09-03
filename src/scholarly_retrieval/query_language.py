"""Exact local semantics for the provider-neutral Boolean search AST."""

from __future__ import annotations

from .models import (
    Paper,
    SearchExpression,
    SearchExpressionOperator,
    SearchField,
)
from .normalization import normalize_text


def matches_expression(paper: Paper, expression: SearchExpression) -> bool:
    """Evaluate one expression deterministically against normalized metadata."""

    if expression.operator == SearchExpressionOperator.AND:
        return all(matches_expression(paper, child) for child in expression.children)
    if expression.operator == SearchExpressionOperator.OR:
        return any(matches_expression(paper, child) for child in expression.children)
    if expression.operator == SearchExpressionOperator.NOT:
        return not matches_expression(paper, expression.children[0])

    wanted = _normalize(expression.value or "")
    values = _field_values(paper, expression.field)
    if expression.operator == SearchExpressionOperator.PHRASE:
        return any(wanted in _normalize(value) for value in values)
    # TERM is token-aware: every requested token must occur, but order does not matter.
    wanted_tokens = set(wanted.split())
    return any(wanted_tokens <= set(_normalize(value).split()) for value in values)


def _field_values(paper: Paper, field: SearchField) -> list[str]:
    mapping = {
        SearchField.TITLE: [paper.title],
        SearchField.ABSTRACT: [paper.abstract or ""],
        SearchField.AUTHOR: [author.name for author in paper.authors],
        SearchField.VENUE: [paper.venue or ""],
        SearchField.FIELD: paper.fields_of_study,
    }
    if field != SearchField.ALL:
        return mapping[field]
    return [value for values in mapping.values() for value in values]


def _normalize(value: str) -> str:
    return normalize_text(value).casefold()
