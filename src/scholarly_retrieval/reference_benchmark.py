"""Versioned gold schema and layered metrics for the Reference pipeline."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    CitationVerificationStatus,
    ExtractedReference,
    ReferenceLink,
    ReferenceLinkingResult,
    ReferenceLinkStatus,
)
from .normalization import normalize_doi


class ReferenceTargetAnnotation(StrEnum):
    """Whether curators supplied a target Work for a bibliography entry."""

    LINKED = "linked"
    NO_MATCH = "no_match"
    NOT_ANNOTATED = "not_annotated"


class GoldReferenceItem(BaseModel):
    """One curated bibliography entry with optional field and target labels."""

    model_config = ConfigDict(extra="forbid")

    reference_id: str
    cited_in_text: bool
    title: str | None = None
    authors: list[str] | None = None
    publication_year: int | None = Field(default=None, ge=1000, le=3000)
    doi: str | None = None
    venue: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    target_annotation: ReferenceTargetAnnotation = ReferenceTargetAnnotation.NOT_ANNOTATED
    accepted_target_record_ids: list[str] = Field(default_factory=list)

    @field_validator(
        "reference_id", "title", "doi", "venue", "volume", "issue", "pages"
    )
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("gold reference text fields must not be blank")
        return normalized

    @field_validator("doi")
    @classmethod
    def doi_must_be_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_doi(value)
        if normalized is None:
            raise ValueError("gold DOI must be a valid DOI")
        return normalized

    @field_validator("authors")
    @classmethod
    def validate_authors(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [" ".join(value.split()) for value in values]
        if not normalized or any(not value for value in normalized):
            raise ValueError("annotated authors must be a non-empty list")
        return normalized

    @field_validator("accepted_target_record_ids")
    @classmethod
    def validate_target_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("accepted target IDs must not contain blanks")
        if len(normalized) != len(set(normalized)):
            raise ValueError("accepted target IDs must be unique")
        return normalized

    @model_validator(mode="after")
    def target_annotation_matches_ids(self) -> GoldReferenceItem:
        if self.target_annotation == ReferenceTargetAnnotation.LINKED:
            if not self.accepted_target_record_ids:
                raise ValueError("linked gold references require accepted target IDs")
        elif self.accepted_target_record_ids:
            raise ValueError("only linked gold references may contain accepted target IDs")
        return self


class ReferenceGoldDocument(BaseModel):
    """Gold labels for one legally obtained source document."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    domain: str
    source_format: str
    seed_record_ids: list[str] = Field(min_length=1)
    provenance: str
    cutoff_date: str | None = None
    references: list[GoldReferenceItem]

    @field_validator("document_id", "domain", "source_format", "provenance")
    @classmethod
    def required_text_must_not_be_blank(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("gold document text fields must not be blank")
        return normalized

    @field_validator("seed_record_ids")
    @classmethod
    def validate_seed_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("seed record IDs must not contain blanks")
        if len(normalized) != len(set(normalized)):
            raise ValueError("seed record IDs must be unique")
        return normalized

    @model_validator(mode="after")
    def references_are_unambiguous(self) -> ReferenceGoldDocument:
        reference_ids = [reference.reference_id for reference in self.references]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("gold reference IDs must be unique within a document")
        target_groups = [
            set(reference.accepted_target_record_ids)
            for reference in self.references
            if reference.target_annotation == ReferenceTargetAnnotation.LINKED
        ]
        for index, left in enumerate(target_groups):
            for right in target_groups[index + 1 :]:
                if left & right and left != right:
                    raise ValueError("overlapping target aliases must describe the same Work")
        return self


class ReferenceGoldDataset(BaseModel):
    """Versioned multi-document labels without redistributing provider payloads."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    dataset_id: str
    dataset_version: str
    license: str
    annotation_protocol: str
    documents: list[ReferenceGoldDocument] = Field(min_length=1)

    @field_validator(
        "schema_version", "dataset_id", "dataset_version", "license", "annotation_protocol"
    )
    @classmethod
    def manifest_text_must_not_be_blank(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("reference gold manifest fields must not be blank")
        return normalized

    @model_validator(mode="after")
    def document_ids_are_unique(self) -> ReferenceGoldDataset:
        document_ids = [document.document_id for document in self.documents]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("reference gold document IDs must be unique")
        return self


def evaluate_reference_document(
    gold: ReferenceGoldDocument,
    predicted: ReferenceLinkingResult,
) -> dict[str, Any]:
    """Evaluate five stages and retain IDs needed to localize every error."""

    predicted_references, duplicate_reference_ids = _first_by_reference_id(
        predicted.extraction.references
    )
    predicted_links, duplicate_link_ids = _first_link_by_reference_id(predicted.links)
    gold_by_id = {reference.reference_id: reference for reference in gold.references}

    detected_ids = set(predicted_references)
    gold_ids = set(gold_by_id)
    detection = _set_metrics(detected_ids, gold_ids)
    detection["missing_reference_ids"] = sorted(gold_ids - detected_ids)
    detection["unexpected_reference_ids"] = sorted(detected_ids - gold_ids)
    detection["duplicate_predicted_reference_ids"] = duplicate_reference_ids

    predicted_cited = {
        reference_id
        for reference_id, reference in predicted_references.items()
        if reference.cited_in_text
    }
    gold_cited = {
        reference.reference_id for reference in gold.references if reference.cited_in_text
    }
    cited_detection = _set_metrics(predicted_cited, gold_cited)
    cited_detection["missing_cited_reference_ids"] = sorted(gold_cited - predicted_cited)
    cited_detection["unexpected_cited_reference_ids"] = sorted(
        predicted_cited - gold_cited
    )

    fields = _field_metrics(gold.references, predicted_references)
    candidates = _candidate_metrics(gold.references, predicted_links)
    linking = _linking_metrics(gold.references, predicted_links)
    linking["duplicate_predicted_link_ids"] = duplicate_link_ids
    verified_edges = _verified_edge_metrics(gold, predicted)

    return {
        "document_id": gold.document_id,
        "domain": gold.domain,
        "source_format": gold.source_format,
        "detection": detection,
        "cited_in_text_detection": cited_detection,
        "field_parsing": fields,
        "candidate_recall": candidates,
        "entity_linking": linking,
        "verified_edges": verified_edges,
    }


def evaluate_reference_gold_dataset(
    dataset: ReferenceGoldDataset,
    predicted_by_document: dict[str, ReferenceLinkingResult],
) -> dict[str, Any]:
    """Evaluate a versioned suite and report per-document plus micro metrics."""

    expected = {document.document_id for document in dataset.documents}
    missing = sorted(expected - set(predicted_by_document))
    unknown = sorted(set(predicted_by_document) - expected)
    if missing:
        raise ValueError(f"missing reference result documents: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"unknown reference result documents: {', '.join(unknown)}")

    per_document = {
        document.document_id: evaluate_reference_document(
            document, predicted_by_document[document.document_id]
        )
        for document in dataset.documents
    }
    return {
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.dataset_version,
        "document_count": len(dataset.documents),
        "per_document": per_document,
        "micro": _micro_metrics(list(per_document.values())),
    }


def _field_metrics(
    gold_references: list[GoldReferenceItem],
    predicted_by_id: dict[str, ExtractedReference],
) -> dict[str, dict[str, Any]]:
    accessors = {
        "title": lambda item: item.title,
        "authors": lambda item: item.authors,
        "publication_year": lambda item: item.publication_year,
        "doi": lambda item: item.doi,
        "venue": lambda item: item.venue,
        "volume": lambda item: item.volume,
        "issue": lambda item: item.issue,
        "pages": lambda item: item.pages,
    }
    results: dict[str, dict[str, Any]] = {}
    for field_name, accessor in accessors.items():
        annotated = [item for item in gold_references if accessor(item) is not None]
        present: list[str] = []
        exact: list[str] = []
        incorrect: list[str] = []
        missing: list[str] = []
        for gold in annotated:
            predicted = predicted_by_id.get(gold.reference_id)
            predicted_value = getattr(predicted, field_name) if predicted else None
            if not _is_present(predicted_value):
                missing.append(gold.reference_id)
                continue
            present.append(gold.reference_id)
            if _normalized_field(field_name, predicted_value) == _normalized_field(
                field_name, accessor(gold)
            ):
                exact.append(gold.reference_id)
            else:
                incorrect.append(gold.reference_id)
        gold_count = len(annotated)
        predicted_count = len(present)
        exact_count = len(exact)
        results[field_name] = {
            "gold_annotated": gold_count,
            "predicted_present": predicted_count,
            "exact_matches": exact_count,
            "coverage": predicted_count / gold_count if gold_count else None,
            "precision_when_present": (
                exact_count / predicted_count if predicted_count else None
            ),
            "end_to_end_accuracy": exact_count / gold_count if gold_count else None,
            "missing_reference_ids": missing,
            "incorrect_reference_ids": incorrect,
        }
    return results


def _candidate_metrics(
    gold_references: list[GoldReferenceItem],
    links_by_id: dict[str, ReferenceLink],
) -> dict[str, Any]:
    eligible = [
        reference
        for reference in gold_references
        if reference.target_annotation == ReferenceTargetAnnotation.LINKED
    ]
    found: list[str] = []
    missed: list[str] = []
    ranks: dict[str, int] = {}
    reciprocal_rank_sum = 0.0
    for gold in eligible:
        accepted = set(gold.accepted_target_record_ids)
        link = links_by_id.get(gold.reference_id)
        candidate_ids = [candidate.record_id for candidate in link.candidates] if link else []
        rank = next(
            (
                index
                for index, record_id in enumerate(candidate_ids, start=1)
                if record_id in accepted
            ),
            None,
        )
        if rank is None:
            missed.append(gold.reference_id)
        else:
            found.append(gold.reference_id)
            ranks[gold.reference_id] = rank
            reciprocal_rank_sum += 1.0 / rank
    eligible_count = len(eligible)
    return {
        "gold_linkable": eligible_count,
        "target_found": len(found),
        "recall": len(found) / eligible_count if eligible_count else None,
        "mrr": reciprocal_rank_sum / eligible_count if eligible_count else None,
        "reciprocal_rank_sum": reciprocal_rank_sum,
        "target_ranks": ranks,
        "missed_reference_ids": missed,
    }


def _linking_metrics(
    gold_references: list[GoldReferenceItem],
    links_by_id: dict[str, ReferenceLink],
) -> dict[str, Any]:
    annotated = [
        reference
        for reference in gold_references
        if reference.target_annotation != ReferenceTargetAnnotation.NOT_ANNOTATED
    ]
    linkable = [
        reference
        for reference in annotated
        if reference.target_annotation == ReferenceTargetAnnotation.LINKED
    ]
    no_match = [
        reference
        for reference in annotated
        if reference.target_annotation == ReferenceTargetAnnotation.NO_MATCH
    ]
    resolved = 0
    correct = 0
    incorrect: dict[str, str | None] = {}
    abstained_linkable: list[str] = []
    correct_abstentions: list[str] = []
    contract_errors: list[str] = []
    for gold in annotated:
        link = links_by_id.get(gold.reference_id)
        is_resolved = link is not None and link.status == ReferenceLinkStatus.RESOLVED
        selected = link.candidates[0].record_id if link and link.candidates else None
        if is_resolved:
            resolved += 1
            if selected is None:
                contract_errors.append(gold.reference_id)
            if (
                gold.target_annotation == ReferenceTargetAnnotation.LINKED
                and selected in gold.accepted_target_record_ids
            ):
                correct += 1
            else:
                incorrect[gold.reference_id] = selected
        elif gold.target_annotation == ReferenceTargetAnnotation.LINKED:
            abstained_linkable.append(gold.reference_id)
        else:
            correct_abstentions.append(gold.reference_id)
    precision = correct / resolved if resolved else 0.0
    recall = correct / len(linkable) if linkable else 0.0
    return {
        "gold_annotated": len(annotated),
        "gold_linkable": len(linkable),
        "gold_no_match": len(no_match),
        "resolved": resolved,
        "correct_resolved": correct,
        "incorrect_resolved": len(incorrect),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "correct_abstentions": len(correct_abstentions),
        "correct_abstention_reference_ids": correct_abstentions,
        "abstained_linkable_reference_ids": abstained_linkable,
        "incorrect_links": incorrect,
        "resolved_without_candidate_reference_ids": contract_errors,
    }


def _verified_edge_metrics(
    gold: ReferenceGoldDocument,
    predicted: ReferenceLinkingResult,
) -> dict[str, Any]:
    target_groups: list[frozenset[str]] = []
    for reference in gold.references:
        if reference.target_annotation != ReferenceTargetAnnotation.LINKED:
            continue
        group = frozenset(reference.accepted_target_record_ids)
        if group not in target_groups:
            target_groups.append(group)
    verified = {
        (edge.citing_record_id, edge.cited_record_id)
        for edge in predicted.edges
        if edge.verification_status == CitationVerificationStatus.VERIFIED
    }
    matched_groups: set[int] = set()
    false_edges: list[list[str]] = []
    seed_ids = set(gold.seed_record_ids)
    for citing_id, cited_id in sorted(verified):
        match = next(
            (
                index
                for index, aliases in enumerate(target_groups)
                if index not in matched_groups and citing_id in seed_ids and cited_id in aliases
            ),
            None,
        )
        if match is None:
            false_edges.append([citing_id, cited_id])
        else:
            matched_groups.add(match)
    precision = len(matched_groups) / len(verified) if verified else 0.0
    recall = len(matched_groups) / len(target_groups) if target_groups else 0.0
    return {
        "gold": len(target_groups),
        "retrieved_verified": len(verified),
        "true_positive": len(matched_groups),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "non_verified_retrieved": sum(
            edge.verification_status != CitationVerificationStatus.VERIFIED
            for edge in predicted.edges
        ),
        "missing_target_alias_groups": [
            sorted(target_groups[index])
            for index in range(len(target_groups))
            if index not in matched_groups
        ],
        "false_positive_edges": false_edges,
    }


def _micro_metrics(documents: list[dict[str, Any]]) -> dict[str, Any]:
    detection = _sum_set_metrics([document["detection"] for document in documents])
    cited = _sum_set_metrics(
        [document["cited_in_text_detection"] for document in documents]
    )
    fields: dict[str, dict[str, int | float | None]] = {}
    field_names = documents[0]["field_parsing"] if documents else {}
    for field_name in field_names:
        values = [document["field_parsing"][field_name] for document in documents]
        gold_count = sum(value["gold_annotated"] for value in values)
        predicted_count = sum(value["predicted_present"] for value in values)
        exact_count = sum(value["exact_matches"] for value in values)
        fields[field_name] = {
            "gold_annotated": gold_count,
            "predicted_present": predicted_count,
            "exact_matches": exact_count,
            "coverage": predicted_count / gold_count if gold_count else None,
            "precision_when_present": (
                exact_count / predicted_count if predicted_count else None
            ),
            "end_to_end_accuracy": exact_count / gold_count if gold_count else None,
        }
    candidate_values = [document["candidate_recall"] for document in documents]
    candidate_gold = sum(value["gold_linkable"] for value in candidate_values)
    candidate_found = sum(value["target_found"] for value in candidate_values)
    reciprocal_sum = sum(value["reciprocal_rank_sum"] for value in candidate_values)
    candidates = {
        "gold_linkable": candidate_gold,
        "target_found": candidate_found,
        "recall": candidate_found / candidate_gold if candidate_gold else None,
        "mrr": reciprocal_sum / candidate_gold if candidate_gold else None,
    }
    link_values = [document["entity_linking"] for document in documents]
    link_gold = sum(value["gold_linkable"] for value in link_values)
    resolved = sum(value["resolved"] for value in link_values)
    correct = sum(value["correct_resolved"] for value in link_values)
    link_precision = correct / resolved if resolved else 0.0
    link_recall = correct / link_gold if link_gold else 0.0
    linking = {
        "gold_annotated": sum(value["gold_annotated"] for value in link_values),
        "gold_linkable": link_gold,
        "gold_no_match": sum(value["gold_no_match"] for value in link_values),
        "resolved": resolved,
        "correct_resolved": correct,
        "incorrect_resolved": sum(value["incorrect_resolved"] for value in link_values),
        "precision": link_precision,
        "recall": link_recall,
        "f1": _f1(link_precision, link_recall),
        "correct_abstentions": sum(value["correct_abstentions"] for value in link_values),
    }
    edge_values = [document["verified_edges"] for document in documents]
    edge_gold = sum(value["gold"] for value in edge_values)
    edge_retrieved = sum(value["retrieved_verified"] for value in edge_values)
    edge_correct = sum(value["true_positive"] for value in edge_values)
    edge_precision = edge_correct / edge_retrieved if edge_retrieved else 0.0
    edge_recall = edge_correct / edge_gold if edge_gold else 0.0
    edges = {
        "gold": edge_gold,
        "retrieved_verified": edge_retrieved,
        "true_positive": edge_correct,
        "precision": edge_precision,
        "recall": edge_recall,
        "f1": _f1(edge_precision, edge_recall),
        "non_verified_retrieved": sum(
            value["non_verified_retrieved"] for value in edge_values
        ),
    }
    return {
        "detection": detection,
        "cited_in_text_detection": cited,
        "field_parsing": fields,
        "candidate_recall": candidates,
        "entity_linking": linking,
        "verified_edges": edges,
    }


def _first_by_reference_id(
    references: list[ExtractedReference],
) -> tuple[dict[str, ExtractedReference], list[str]]:
    indexed: dict[str, ExtractedReference] = {}
    duplicates: set[str] = set()
    for reference in references:
        if reference.reference_id in indexed:
            duplicates.add(reference.reference_id)
        else:
            indexed[reference.reference_id] = reference
    return indexed, sorted(duplicates)


def _first_link_by_reference_id(
    links: list[ReferenceLink],
) -> tuple[dict[str, ReferenceLink], list[str]]:
    indexed: dict[str, ReferenceLink] = {}
    duplicates: set[str] = set()
    for link in links:
        reference_id = link.reference.reference_id
        if reference_id in indexed:
            duplicates.add(reference_id)
        else:
            indexed[reference_id] = link
    return indexed, sorted(duplicates)


def _set_metrics(retrieved: set[str], gold: set[str]) -> dict[str, int | float]:
    true_positive = len(retrieved & gold)
    precision = true_positive / len(retrieved) if retrieved else 0.0
    recall = true_positive / len(gold) if gold else 0.0
    return {
        "retrieved": len(retrieved),
        "gold": len(gold),
        "true_positive": true_positive,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
    }


def _sum_set_metrics(values: list[dict[str, Any]]) -> dict[str, int | float]:
    retrieved = sum(value["retrieved"] for value in values)
    gold = sum(value["gold"] for value in values)
    true_positive = sum(value["true_positive"] for value in values)
    precision = true_positive / retrieved if retrieved else 0.0
    recall = true_positive / gold if gold else 0.0
    return {
        "retrieved": retrieved,
        "gold": gold,
        "true_positive": true_positive,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
    }


def _normalized_field(field_name: str, value: Any) -> Any:
    if field_name == "publication_year":
        return value
    if field_name == "doi":
        return normalize_doi(value) if isinstance(value, str) else value
    if field_name == "authors":
        return tuple(_normalize_text(author) for author in value)
    return _normalize_text(str(value))


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"\w+", normalized, flags=re.UNICODE))


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, list)):
        return bool(value)
    return True


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0
