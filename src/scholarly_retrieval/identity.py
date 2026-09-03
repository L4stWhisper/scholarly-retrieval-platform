"""Conservative, deterministic, and auditable paper identity resolution."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from difflib import SequenceMatcher

from .models import (
    EntityRelationKind,
    IdentifierScheme,
    IdentityDecision,
    IdentityDecisionKind,
    IdentityEvent,
    IdentityEventAction,
    IdentityResolution,
    Paper,
)
from .normalization import normalize_text

STRONG_SCHEMES = {
    IdentifierScheme.DOI,
    IdentifierScheme.ARXIV,
    IdentifierScheme.PMID,
    IdentifierScheme.PMCID,
}
TITLE_BLOCK_THRESHOLD = 0.75
REVIEW_THRESHOLD = 0.82
# These thresholds only generate review candidates. They must not drive
# automatic merges until a cross-domain gold set calibrates them.


def strong_identity_keys(paper: Paper) -> set[tuple[IdentifierScheme, str]]:
    return {
        (claim.scheme, claim.value) for claim in paper.identifiers if claim.scheme in STRONG_SCHEMES
    }


def resolve_identities(
    papers: list[Paper], *, events: list[IdentityEvent] | None = None
) -> IdentityResolution:
    """Merge safe must-links and retain cannot-link/review decisions as evidence."""

    if not papers:
        return IdentityResolution(papers=[])
    papers = deepcopy(papers)
    active_events = [
        event
        for event in events or []
        if event.action != IdentityEventAction.REVERT and event.reverted_by_event_id is None
    ]
    _apply_reviewed_work_families(papers, active_events)
    reviewed_pairs = {
        frozenset((event.left_record_id, event.right_record_id)): event for event in active_events
    }
    parents = list(range(len(papers)))
    decisions: list[IdentityDecision] = []
    keys = [strong_identity_keys(paper) for paper in papers]
    component_keys = [set(item) for item in keys]

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            target, source = min(left_root, right_root), max(left_root, right_root)
            parents[source] = target
            component_keys[target] |= component_keys[source]

    for left in range(len(papers)):
        for right in range(left + 1, len(papers)):
            reviewed = reviewed_pairs.get(
                frozenset((papers[left].record_id, papers[right].record_id))
            )
            if reviewed is not None:
                if (
                    reviewed.action == IdentityEventAction.SPLIT
                    or reviewed.relation == EntityRelationKind.DIFFERENT_WORK
                ):
                    decisions.append(
                        _decision(
                            papers[left],
                            papers[right],
                            IdentityDecisionKind.CANNOT_LINK,
                            [f"active identity event:{reviewed.event_id}", *reviewed.reasons],
                        )
                    )
                    continue
                if reviewed.action == IdentityEventAction.DEFER:
                    decisions.append(
                        _decision(
                            papers[left],
                            papers[right],
                            IdentityDecisionKind.REVIEW,
                            [f"active identity event:{reviewed.event_id}", *reviewed.reasons],
                        )
                    )
                    continue
                if (
                    reviewed.action == IdentityEventAction.MERGE
                    and reviewed.relation == EntityRelationKind.SAME_MANIFESTATION
                ):
                    conflicts = {
                        **_internal_identifier_conflicts(keys[left]),
                        **_internal_identifier_conflicts(keys[right]),
                        **_identifier_conflicts(keys[left], keys[right]),
                    }
                    if conflicts:
                        decisions.append(
                            _decision(
                                papers[left],
                                papers[right],
                                IdentityDecisionKind.CANNOT_LINK,
                                [
                                    "manual merge blocked by strong identifier conflict",
                                    f"active identity event:{reviewed.event_id}",
                                ],
                                conflicts=conflicts,
                            )
                        )
                    else:
                        union(left, right)
                        decisions.append(
                            _decision(
                                papers[left],
                                papers[right],
                                IdentityDecisionKind.MUST_LINK,
                                [f"active identity event:{reviewed.event_id}", *reviewed.reasons],
                            )
                        )
                    continue
            if papers[left].record_id == papers[right].record_id:
                union(left, right)
                decisions.append(
                    _decision(
                        papers[left],
                        papers[right],
                        IdentityDecisionKind.MUST_LINK,
                        ["same provider record identifier"],
                    )
                )
                continue
            shared = keys[left] & keys[right]
            title_score = _title_similarity(papers[left].title, papers[right].title)
            conflicts = {
                **_internal_identifier_conflicts(keys[left]),
                **_internal_identifier_conflicts(keys[right]),
                **_identifier_conflicts(keys[left], keys[right]),
            }
            if shared:
                left_root, right_root = find(left), find(right)
                if left_root != right_root:
                    # Pairwise compatibility is insufficient: joining two safe
                    # components can still introduce a transitive ID conflict.
                    conflicts.update(
                        _identifier_conflicts(component_keys[left_root], component_keys[right_root])
                    )
                if conflicts:
                    decisions.append(
                        _decision(
                            papers[left],
                            papers[right],
                            IdentityDecisionKind.CANNOT_LINK,
                            ["shared identifier contradicted by another strong identifier"],
                            shared=shared,
                            conflicts=conflicts,
                        )
                    )
                else:
                    union(left, right)
                    decisions.append(
                        _decision(
                            papers[left],
                            papers[right],
                            IdentityDecisionKind.MUST_LINK,
                            ["shared strong identifier"],
                            shared=shared,
                        )
                    )
                continue
            if title_score < TITLE_BLOCK_THRESHOLD:
                continue
            if conflicts:
                decisions.append(
                    _decision(
                        papers[left],
                        papers[right],
                        IdentityDecisionKind.CANNOT_LINK,
                        ["similar titles but mutually exclusive strong identifiers"],
                        conflicts=conflicts,
                        score=title_score,
                        components={"title": title_score},
                    )
                )
                continue
            score, components = _candidate_score(papers[left], papers[right], title_score)
            if score >= REVIEW_THRESHOLD:
                decisions.append(
                    _decision(
                        papers[left],
                        papers[right],
                        IdentityDecisionKind.REVIEW,
                        ["similar bibliographic metadata without a shared strong identifier"],
                        score=score,
                        components=components,
                    )
                )

    groups: dict[int, list[int]] = {}
    for index in range(len(papers)):
        groups.setdefault(find(index), []).append(index)
    merged: list[Paper] = []
    record_id_map: dict[str, str] = {}
    for indices in groups.values():
        canonical = _merge_group([papers[index] for index in indices])
        merged.append(canonical)
        for index in indices:
            record_id_map[papers[index].record_id] = canonical.record_id
    return IdentityResolution(
        papers=merged,
        decisions=decisions,
        record_id_map=record_id_map,
    )


def merge_exact_records(papers: list[Paper]) -> list[Paper]:
    """Compatibility wrapper returning only the safe canonical paper views."""

    return resolve_identities(papers).papers


def _merge_group(papers: list[Paper]) -> Paper:
    current = deepcopy(papers[0])
    for incoming in papers[1:]:
        current.identifiers = _unique_models(current.identifiers + incoming.identifiers)
        current.source_records = _unique_models(current.source_records + incoming.source_records)
        current.citation_counts = _unique_models(current.citation_counts + incoming.citation_counts)
        for field, claims in incoming.field_claims.items():
            current.field_claims[field] = _unique_models(
                current.field_claims.get(field, []) + claims
            )
        for field, claims in incoming.field_provenance.items():
            current.field_provenance[field] = _unique_models(
                current.field_provenance.get(field, []) + claims
            )
        if not current.abstract and incoming.abstract:
            current.abstract = incoming.abstract
        if len(incoming.authors) > len(current.authors):
            current.authors = incoming.authors
    return current


def _identifier_conflicts(
    left: set[tuple[IdentifierScheme, str]],
    right: set[tuple[IdentifierScheme, str]],
) -> dict[str, list[str]]:
    conflicts: dict[str, list[str]] = {}
    for scheme in STRONG_SCHEMES:
        left_values = {value for key_scheme, value in left if key_scheme == scheme}
        right_values = {value for key_scheme, value in right if key_scheme == scheme}
        if left_values and right_values and left_values != right_values:
            conflicts[scheme.value] = sorted(left_values | right_values)
    return conflicts


def _internal_identifier_conflicts(
    keys: set[tuple[IdentifierScheme, str]],
) -> dict[str, list[str]]:
    conflicts: dict[str, list[str]] = {}
    for scheme in STRONG_SCHEMES:
        values = {value for key_scheme, value in keys if key_scheme == scheme}
        if len(values) > 1:
            conflicts[scheme.value] = sorted(values)
    return conflicts


def _decision(
    left: Paper,
    right: Paper,
    kind: IdentityDecisionKind,
    reasons: list[str],
    *,
    shared: set[tuple[IdentifierScheme, str]] | None = None,
    conflicts: dict[str, list[str]] | None = None,
    score: float | None = None,
    components: dict[str, float] | None = None,
) -> IdentityDecision:
    return IdentityDecision(
        left_record_id=left.record_id,
        right_record_id=right.record_id,
        decision=kind,
        reasons=reasons,
        shared_identifiers=sorted(f"{scheme.value}:{value}" for scheme, value in shared or set()),
        conflicting_identifiers=conflicts or {},
        score=score,
        score_components=components or {},
    )


def _title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _title_key(left), _title_key(right)).ratio()


def _title_key(value: str) -> str:
    normalized = normalize_text(value).casefold()
    return " ".join(re.findall(r"\w+", normalized, flags=re.UNICODE))


def _candidate_score(
    left: Paper, right: Paper, title_score: float
) -> tuple[float, dict[str, float]]:
    author_score = _author_similarity(left, right)
    year_score = _year_similarity(left, right)
    components = {"title": title_score, "authors": author_score, "year": year_score}
    return 0.7 * title_score + 0.2 * author_score + 0.1 * year_score, components


def _author_similarity(left: Paper, right: Paper) -> float:
    left_names = {_title_key(author.name) for author in left.authors if author.name}
    right_names = {_title_key(author.name) for author in right.authors if author.name}
    if not left_names or not right_names:
        return 0.5
    return len(left_names & right_names) / len(left_names | right_names)


def _year_similarity(left: Paper, right: Paper) -> float:
    left_year = left.publication_year or (
        left.publication_date.year if left.publication_date else None
    )
    right_year = right.publication_year or (
        right.publication_date.year if right.publication_date else None
    )
    if left_year is None or right_year is None:
        return 0.5
    difference = abs(left_year - right_year)
    return 1.0 if difference == 0 else 0.5 if difference == 1 else 0.0


def _unique_models(values: list) -> list:
    result = []
    seen = set()
    for value in values:
        marker = value.model_dump_json()
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def _apply_reviewed_work_families(papers: list[Paper], events: list[IdentityEvent]) -> None:
    """Materialize reviewed VERSION_OF components without merging manifestations."""

    by_id = {paper.record_id: paper for paper in papers}
    parents = {record_id: record_id for record_id in by_id}

    def find(record_id: str) -> str:
        while parents[record_id] != record_id:
            parents[record_id] = parents[parents[record_id]]
            record_id = parents[record_id]
        return record_id

    for event in events:
        if (
            event.action != IdentityEventAction.MERGE
            or event.relation != EntityRelationKind.VERSION_OF
            or event.left_record_id not in by_id
            or event.right_record_id not in by_id
        ):
            continue
        left_root = find(event.left_record_id)
        right_root = find(event.right_record_id)
        if left_root != right_root:
            target, source = sorted((left_root, right_root))
            parents[source] = target

    components: dict[str, list[str]] = {}
    for record_id in by_id:
        components.setdefault(find(record_id), []).append(record_id)
    for members in components.values():
        if len(members) < 2:
            continue
        material = "\0".join(sorted(members)).encode()
        family_id = f"review-family:{hashlib.sha256(material).hexdigest()[:24]}"
        for record_id in members:
            by_id[record_id].work_family_id = family_id
