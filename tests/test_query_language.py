import pytest
from pydantic import ValidationError

from scholarly_retrieval.models import (
    Author,
    Paper,
    SearchExpression,
    SearchExpressionOperator,
    SearchField,
)
from scholarly_retrieval.query_language import matches_expression


def _leaf(
    value: str,
    *,
    field: SearchField = SearchField.ALL,
    operator: SearchExpressionOperator = SearchExpressionOperator.TERM,
) -> SearchExpression:
    return SearchExpression(operator=operator, field=field, value=value)


def test_boolean_phrase_expression_has_exact_portable_semantics() -> None:
    paper = Paper(
        record_id="fixture:1",
        title="Graph Neural Networks for Scholarly Retrieval",
        abstract="A reproducible citation graph study",
        authors=[Author(name="Ada Researcher")],
    )
    expression = SearchExpression(
        operator=SearchExpressionOperator.AND,
        children=[
            _leaf(
                "graph neural networks",
                field=SearchField.TITLE,
                operator=SearchExpressionOperator.PHRASE,
            ),
            SearchExpression(
                operator=SearchExpressionOperator.NOT,
                children=[_leaf("clinical", field=SearchField.ABSTRACT)],
            ),
            SearchExpression(
                operator=SearchExpressionOperator.OR,
                children=[
                    _leaf("reproducible", field=SearchField.ABSTRACT),
                    _leaf("Bob", field=SearchField.AUTHOR),
                ],
            ),
        ],
    )

    assert matches_expression(paper, expression) is True


def test_expression_shape_rejects_ambiguous_boolean_nodes() -> None:
    with pytest.raises(ValidationError, match="at least two children"):
        SearchExpression(
            operator=SearchExpressionOperator.OR,
            children=[_leaf("only")],
        )
    with pytest.raises(ValidationError, match="cannot have children"):
        SearchExpression(
            operator=SearchExpressionOperator.PHRASE,
            value="bad",
            children=[_leaf("child")],
        )
