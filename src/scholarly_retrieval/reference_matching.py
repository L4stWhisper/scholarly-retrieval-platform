"""Conservative, explainable bibliography-to-paper candidate matching."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .models import ExtractedReference, Paper, ReferenceCandidateScore, ReferenceLinkStatus

FIELD_WEIGHTS = {
    "title": 0.65,
    "authors": 0.15,
    "year": 0.10,
    "venue": 0.10,
}


@dataclass(frozen=True)
class ReferenceMatchingPolicy:
    """High-precision defaults; deployments should recalibrate them on gold data."""

    auto_match_threshold: float = 0.92
    minimum_margin: float = 0.08

    def __post_init__(self) -> None:
        if not 0 <= self.auto_match_threshold <= 1:
            raise ValueError("reference auto-match threshold must be in [0, 1]")
        if not 0 <= self.minimum_margin <= 1:
            raise ValueError("reference minimum margin must be in [0, 1]")


def rank_reference_candidates(
    reference: ExtractedReference,
    candidates: list[Paper],
    *,
    identifier_match: bool = False,
) -> tuple[list[Paper], list[ReferenceCandidateScore]]:
    """Return candidates and scores in deterministic descending-score order."""

    pairs = [
        (
            candidate,
            score_reference_candidate(
                reference,
                candidate,
                identifier_match=identifier_match,
            ),
            index,
        )
        for index, candidate in enumerate(candidates)
    ]
    pairs.sort(key=lambda item: (-item[1].total_score, item[2], item[0].record_id))
    return [item[0] for item in pairs], [item[1] for item in pairs]


def score_reference_candidate(
    reference: ExtractedReference,
    candidate: Paper,
    *,
    identifier_match: bool = False,
) -> ReferenceCandidateScore:
    """Score only fields present in the bibliography, retaining missing evidence."""

    if identifier_match:
        return ReferenceCandidateScore(
            record_id=candidate.record_id,
            total_score=1.0,
            identifier_score=1.0,
            compared_fields=["identifier"],
        )

    values: dict[str, float] = {}
    missing: list[str] = []
    if reference.title:
        values["title"] = _text_similarity(reference.title, candidate.title)
    if reference.authors:
        if candidate.authors:
            values["authors"] = _author_similarity(
                reference.authors,
                [author.name for author in candidate.authors],
            )
        else:
            values["authors"] = 0.0
            missing.append("authors")
    if reference.publication_year is not None:
        if candidate.publication_year is None:
            values["year"] = 0.0
            missing.append("year")
        else:
            difference = abs(reference.publication_year - candidate.publication_year)
            values["year"] = 1.0 if difference == 0 else 0.8 if difference == 1 else 0.0
    if reference.venue:
        if candidate.venue:
            values["venue"] = _text_similarity(reference.venue, candidate.venue)
        else:
            values["venue"] = 0.0
            missing.append("venue")

    denominator = sum(FIELD_WEIGHTS[field] for field in values)
    total = (
        sum(FIELD_WEIGHTS[field] * score for field, score in values.items()) / denominator
        if denominator
        else 0.0
    )
    return ReferenceCandidateScore(
        record_id=candidate.record_id,
        total_score=total,
        title_score=values.get("title"),
        author_score=values.get("authors"),
        year_score=values.get("year"),
        venue_score=values.get("venue"),
        compared_fields=list(values),
        missing_candidate_fields=missing,
    )


def decide_reference_match(
    scores: list[ReferenceCandidateScore],
    *,
    policy: ReferenceMatchingPolicy,
    identifier_match: bool = False,
) -> tuple[ReferenceLinkStatus, str]:
    """Apply an explicit threshold and margin without silently taking top-1."""

    if not scores:
        return ReferenceLinkStatus.UNRESOLVED, "no_candidates"
    if identifier_match:
        if len(scores) == 1:
            return ReferenceLinkStatus.RESOLVED, "unique_strong_identifier"
        return ReferenceLinkStatus.AMBIGUOUS, "conflicting_strong_identifier_candidates"
    top = scores[0].total_score
    if top < policy.auto_match_threshold:
        return ReferenceLinkStatus.UNRESOLVED, "top_score_below_auto_match_threshold"
    if len(scores) > 1:
        margin = top - scores[1].total_score
        if margin < policy.minimum_margin:
            return ReferenceLinkStatus.AMBIGUOUS, "top_candidates_within_minimum_margin"
    return ReferenceLinkStatus.RESOLVED, "score_and_margin_satisfied"


def _text_similarity(left: str, right: str) -> float:
    normalized_left = _normalize_text(left)
    normalized_right = _normalize_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    sequence = SequenceMatcher(None, normalized_left, normalized_right, autojunk=False).ratio()
    left_tokens = set(normalized_left.split())
    right_tokens = set(normalized_right.split())
    token_union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(token_union) if token_union else 0.0
    return 0.7 * sequence + 0.3 * jaccard


def _author_similarity(expected: list[str], observed: list[str]) -> float:
    expected_surnames = {_surname(name) for name in expected if _surname(name)}
    observed_surnames = {_surname(name) for name in observed if _surname(name)}
    # Bibliographies commonly abbreviate a long author list with "et al.".
    # Measure coverage of the cited names instead of penalizing extra candidate authors.
    return (
        len(expected_surnames & observed_surnames) / len(expected_surnames)
        if expected_surnames
        else 0.0
    )


def _surname(name: str) -> str:
    normalized = _normalize_text(name)
    if not normalized:
        return ""
    if "," in unicodedata.normalize("NFKC", name):
        return _normalize_text(name.split(",", 1)[0]).replace(" ", "")
    return normalized.split()[-1]


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"\w+", normalized, flags=re.UNICODE))
