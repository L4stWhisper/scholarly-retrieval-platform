import pytest

from scholarly_retrieval.models import (
    Author,
    ExtractedReference,
    Paper,
    ReferenceCandidateScore,
    ReferenceEvidenceLevel,
)
from scholarly_retrieval.reference_matching import (
    ReferenceMatchingPolicy,
    decide_reference_match,
    score_reference_candidate,
)


def sample_reference() -> ExtractedReference:
    return ExtractedReference(
        reference_id="R1",
        ordinal=1,
        cited_in_text=True,
        evidence_level=ReferenceEvidenceLevel.VERIFIED_ANCHOR,
        raw_text="Attention Is All You Need, NeurIPS 2017",
        title="Attention Is All You Need",
        authors=["Vaswani, Ashish"],
        publication_year=2017,
        venue="NeurIPS",
    )


def test_reference_candidate_score_exposes_each_available_field() -> None:
    score = score_reference_candidate(
        sample_reference(),
        Paper(
            record_id="paper:exact",
            title="Attention Is All You Need",
            authors=[Author(name="Ashish Vaswani"), Author(name="Noam Shazeer")],
            publication_year=2017,
            venue="NeurIPS",
        ),
    )

    assert score.total_score == 1.0
    assert score.title_score == 1.0
    assert score.author_score == 1.0
    assert score.year_score == 1.0
    assert score.venue_score == 1.0
    assert score.compared_fields == ["title", "authors", "year", "venue"]
    assert score.missing_candidate_fields == []


def test_missing_candidate_metadata_lowers_score_instead_of_being_ignored() -> None:
    score = score_reference_candidate(
        sample_reference(),
        Paper(
            record_id="paper:sparse",
            title="Attention Is All You Need",
            publication_year=2017,
        ),
    )

    assert score.total_score == pytest.approx(0.75)
    assert score.missing_candidate_fields == ["authors", "venue"]


def test_match_decision_requires_both_threshold_and_margin() -> None:
    policy = ReferenceMatchingPolicy(auto_match_threshold=0.92, minimum_margin=0.08)
    close_scores = [
        ReferenceCandidateScore(record_id="A", total_score=0.96),
        ReferenceCandidateScore(record_id="B", total_score=0.90),
    ]
    low_score = [ReferenceCandidateScore(record_id="C", total_score=0.80)]

    assert decide_reference_match(close_scores, policy=policy) == (
        "ambiguous",
        "top_candidates_within_minimum_margin",
    )
    assert decide_reference_match(low_score, policy=policy) == (
        "unresolved",
        "top_score_below_auto_match_threshold",
    )


@pytest.mark.parametrize(
    ("threshold", "margin"),
    [(-0.1, 0.1), (1.1, 0.1), (0.9, -0.1), (0.9, 1.1)],
)
def test_reference_matching_policy_rejects_invalid_bounds(
    threshold: float,
    margin: float,
) -> None:
    with pytest.raises(ValueError):
        ReferenceMatchingPolicy(
            auto_match_threshold=threshold,
            minimum_margin=margin,
        )
