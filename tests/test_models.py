import pytest
from pydantic import ValidationError

from scholarly_retrieval.models import (
    GraphExpansionQuery,
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
    RelatedQuery,
    SearchQuery,
)


def test_search_query_rejects_inverted_year_range() -> None:
    with pytest.raises(ValidationError, match="year_from must not be greater"):
        SearchQuery(text="graph", year_from=2025, year_to=2024)


def test_search_query_normalizes_author_and_rejects_blanks() -> None:
    assert SearchQuery(text="graph", author="  Ada   Lovelace ").author == "Ada Lovelace"
    with pytest.raises(ValidationError, match="author filter must not be blank"):
        SearchQuery(text="graph", author="   ")


def test_search_query_normalizes_advanced_filters() -> None:
    query = SearchQuery(
        text="graph",
        title="  Citation   Graph ",
        work_types=[" Article ", "article"],
        min_citations=0,
        sort="newest",
    )
    assert query.title == "Citation Graph"
    assert query.work_types == ["article"]
    with pytest.raises(ValidationError, match="advanced text filters"):
        SearchQuery(text="graph", venue="  ")


def test_paper_title_is_normalized_and_required() -> None:
    assert Paper(record_id="fixture:1", title="  A   Paper ").title == "A Paper"
    with pytest.raises(ValidationError, match="paper title must not be blank"):
        Paper(record_id="fixture:2", title="  ")


def test_identifier_claim_assigns_authority_and_validates_confidence() -> None:
    provenance = Provenance(provider="fixture", source_record_id="1")
    claim = IdentifierClaim(
        scheme=IdentifierScheme.PMID,
        value="34265844",
        confidence=0.9,
        provenance=provenance,
    )

    assert claim.authority == "U.S. National Library of Medicine"
    assert claim.confidence == 0.9
    with pytest.raises(ValidationError):
        IdentifierClaim(
            scheme=IdentifierScheme.DOI,
            value="10.1234/x",
            confidence=1.1,
            provenance=provenance,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("depth", 0),
        ("depth", 4),
        ("frontier_cap", 0),
        ("top_k", 5001),
        ("per_node_limit", 1001),
    ],
)
def test_graph_expansion_query_rejects_unbounded_work(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        GraphExpansionQuery(identifier="seed", **{field: value})


def test_graph_expansion_query_normalizes_identifier() -> None:
    assert GraphExpansionQuery(identifier="  W1 ").identifier == "W1"
    with pytest.raises(ValidationError, match="identifier must not be blank"):
        GraphExpansionQuery(identifier="  ")


def test_graph_expansion_query_accepts_and_deduplicates_multiple_seeds() -> None:
    query = GraphExpansionQuery(identifier=" W1 ", identifiers=["W2", " W1 ", "W2"])
    assert query.seed_identifiers == ["W1", "W2"]

    with pytest.raises(ValidationError, match="at least one seed identifier"):
        GraphExpansionQuery()
    with pytest.raises(ValidationError, match="must not contain blanks"):
        GraphExpansionQuery(identifiers=["W1", "  "])
    with pytest.raises(ValidationError):
        GraphExpansionQuery(identifier="W0", identifiers=[f"W{i}" for i in range(1, 21)])


def test_graph_expansion_query_normalizes_and_validates_candidate_filters() -> None:
    query = GraphExpansionQuery(
        identifier="W1",
        year_from=2015,
        year_to=2024,
        include_work_types=[" Article ", "article", "Conference  Paper"],
    )
    assert query.include_work_types == ["article", "conference paper"]

    with pytest.raises(ValidationError, match="year_from must not be greater"):
        GraphExpansionQuery(identifier="W1", year_from=2025, year_to=2024)
    with pytest.raises(ValidationError, match="work types must not contain blanks"):
        GraphExpansionQuery(identifier="W1", include_work_types=["  "])
    with pytest.raises(ValidationError):
        GraphExpansionQuery(identifier="W1", max_runtime_seconds=0.01)


def test_related_query_requires_input_and_rejects_conflicting_feedback() -> None:
    with pytest.raises(ValidationError, match="requires text or a positive identifier"):
        RelatedQuery()
    with pytest.raises(ValidationError, match="both positive and negative"):
        RelatedQuery(positive_identifiers=["S1"], negative_identifiers=["S1"])

    query = RelatedQuery(
        text="  graph   retrieval ",
        positive_identifiers=[" S1 ", "S1"],
        include_work_types=[" Article ", "article"],
    )
    assert query.text == "graph retrieval"
    assert query.positive_identifiers == ["S1"]
    assert query.include_work_types == ["article"]
