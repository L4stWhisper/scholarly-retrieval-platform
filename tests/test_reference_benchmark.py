from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from scholarly_retrieval.cli import app
from scholarly_retrieval.models import (
    CitationAssertion,
    CitationEvidenceType,
    CitationVerificationStatus,
    ExtractedReference,
    Paper,
    Provenance,
    ReferenceEvidenceLevel,
    ReferenceExtractionResult,
    ReferenceLink,
    ReferenceLinkingResult,
    RelationKind,
    RunStatus,
    VisibleCitationEdge,
)
from scholarly_retrieval.reference_benchmark import (
    ReferenceGoldDataset,
    ReferenceGoldDocument,
    evaluate_reference_document,
    evaluate_reference_gold_dataset,
)

runner = CliRunner()


def _reference(
    reference_id: str,
    *,
    title: str,
    year: int,
    cited: bool = True,
) -> ExtractedReference:
    return ExtractedReference(
        reference_id=reference_id,
        ordinal=int(reference_id.removeprefix("r") or "1"),
        cited_in_text=cited,
        evidence_level=(
            ReferenceEvidenceLevel.VERIFIED_ANCHOR
            if cited
            else ReferenceEvidenceLevel.BIBLIOGRAPHY_ONLY
        ),
        raw_text=f"Ada Lovelace. {title}. {year}.",
        title=title,
        authors=["Ada Lovelace"],
        publication_year=year,
        venue="Journal of Evidence",
        callout_count=1 if cited else 0,
    )


def _gold_document(document_id: str = "doc-1") -> ReferenceGoldDocument:
    return ReferenceGoldDocument(
        document_id=document_id,
        domain="computer science",
        source_format="jats",
        seed_record_ids=["fixture:seed"],
        provenance="synthetic unit-test labels; two-curator protocol fixture",
        references=[
            {
                "reference_id": "r1",
                "cited_in_text": True,
                "title": "Graph Retrieval",
                "authors": ["Ada Lovelace"],
                "publication_year": 2024,
                "venue": "Journal of Evidence",
                "target_annotation": "linked",
                "accepted_target_record_ids": ["fixture:target"],
            },
            {
                "reference_id": "r2",
                "cited_in_text": False,
                "title": "Unindexed Report",
                "authors": ["Ada Lovelace"],
                "publication_year": 2020,
                "venue": "Journal of Evidence",
                "target_annotation": "no_match",
            },
        ],
    )


def _result(
    references: list[ExtractedReference],
    links: list[ReferenceLink],
    *,
    edge_target: str | None = None,
) -> ReferenceLinkingResult:
    assertions: list[CitationAssertion] = []
    edges: list[VisibleCitationEdge] = []
    if edge_target:
        assertion = CitationAssertion(
            subject_record_id="fixture:seed",
            object_record_id=edge_target,
            relation=RelationKind.REFERENCES,
            provenance=Provenance(provider="fulltext", source_record_id="doc-1"),
            evidence_type=CitationEvidenceType.FULLTEXT_ANCHOR,
            verification_status=CitationVerificationStatus.VERIFIED,
        )
        assertions.append(assertion)
        edges.append(
            VisibleCitationEdge(
                citing_record_id="fixture:seed",
                cited_record_id=edge_target,
                assertions=[assertion],
            )
        )
    return ReferenceLinkingResult(
        seed=Paper(record_id="fixture:seed", title="Seed"),
        extraction=ReferenceExtractionResult(
            source_format="jats",
            references=references,
            bibliography_entry_count=len(references),
            cited_entry_count=sum(reference.cited_in_text for reference in references),
        ),
        status=RunStatus.COMPLETE,
        links=links,
        assertions=assertions,
        edges=edges,
        resolved_count=sum(link.status == "resolved" for link in links),
        ambiguous_count=sum(link.status == "ambiguous" for link in links),
        unresolved_count=sum(link.status == "unresolved" for link in links),
    )


def _perfect_result() -> ReferenceLinkingResult:
    r1 = _reference("r1", title="Graph Retrieval", year=2024)
    r2 = _reference("r2", title="Unindexed Report", year=2020, cited=False)
    links = [
        ReferenceLink(
            reference=r1,
            status="resolved",
            method="title_metadata_search",
            candidates=[Paper(record_id="fixture:target", title="Graph Retrieval")],
        ),
        ReferenceLink(
            reference=r2,
            status="unresolved",
            method="title_metadata_search",
        ),
    ]
    return _result([r1, r2], links, edge_target="fixture:target")


def test_reference_document_reports_all_five_layers() -> None:
    metrics = evaluate_reference_document(_gold_document(), _perfect_result())

    assert metrics["detection"]["f1"] == 1.0
    assert metrics["cited_in_text_detection"]["f1"] == 1.0
    assert metrics["field_parsing"]["title"]["end_to_end_accuracy"] == 1.0
    assert metrics["candidate_recall"]["recall"] == 1.0
    assert metrics["candidate_recall"]["mrr"] == 1.0
    assert metrics["entity_linking"]["precision"] == 1.0
    assert metrics["entity_linking"]["correct_abstentions"] == 1
    assert metrics["verified_edges"]["f1"] == 1.0


def test_reference_document_localizes_detection_field_candidate_and_link_errors() -> None:
    gold_payload = _gold_document().model_dump(mode="json")
    gold_payload["references"].append(
        {
            "reference_id": "r3",
            "cited_in_text": True,
            "title": "Missing Work",
            "publication_year": 2019,
            "target_annotation": "linked",
            "accepted_target_record_ids": ["fixture:missing"],
        }
    )
    gold = ReferenceGoldDocument.model_validate(gold_payload)
    wrong = _reference("r1", title="Wrong Parsed Title", year=2023)
    no_match = _reference("r2", title="Unindexed Report", year=2020, cited=False)
    unexpected = _reference("r9", title="Unexpected", year=2022)
    links = [
        ReferenceLink(
            reference=wrong,
            status="resolved",
            method="title_metadata_search",
            candidates=[
                Paper(record_id="fixture:wrong", title="Wrong"),
                Paper(record_id="fixture:target", title="Graph Retrieval"),
            ],
        ),
        ReferenceLink(
            reference=no_match,
            status="resolved",
            method="title_metadata_search",
            candidates=[Paper(record_id="fixture:false", title="False Match")],
        ),
    ]
    metrics = evaluate_reference_document(
        gold,
        _result([wrong, no_match, unexpected], links, edge_target="fixture:wrong"),
    )

    assert metrics["detection"]["missing_reference_ids"] == ["r3"]
    assert metrics["detection"]["unexpected_reference_ids"] == ["r9"]
    assert metrics["field_parsing"]["title"]["incorrect_reference_ids"] == ["r1"]
    assert metrics["field_parsing"]["title"]["missing_reference_ids"] == ["r3"]
    assert metrics["candidate_recall"]["recall"] == 0.5
    assert metrics["candidate_recall"]["mrr"] == 0.25
    assert metrics["candidate_recall"]["missed_reference_ids"] == ["r3"]
    assert metrics["entity_linking"]["incorrect_resolved"] == 2
    assert metrics["entity_linking"]["incorrect_links"] == {
        "r1": "fixture:wrong",
        "r2": "fixture:false",
    }
    assert metrics["verified_edges"]["false_positive_edges"] == [
        ["fixture:seed", "fixture:wrong"]
    ]
    assert len(metrics["verified_edges"]["missing_target_alias_groups"]) == 2


def test_reference_dataset_reports_micro_metrics_and_rejects_missing_documents() -> None:
    dataset = ReferenceGoldDataset(
        dataset_id="reference-fixture",
        dataset_version="2026.09",
        license="CC0 fixture labels",
        annotation_protocol="Two curators adjudicate fields, targets, and anchors.",
        documents=[_gold_document()],
    )
    result = evaluate_reference_gold_dataset(dataset, {"doc-1": _perfect_result()})

    assert result["micro"]["candidate_recall"]["recall"] == 1.0
    assert result["micro"]["entity_linking"]["f1"] == 1.0
    assert result["micro"]["verified_edges"]["f1"] == 1.0
    with pytest.raises(ValueError, match="missing reference result documents"):
        evaluate_reference_gold_dataset(dataset, {})


def test_evaluate_references_cli_accepts_versioned_saved_results() -> None:
    dataset = ReferenceGoldDataset(
        dataset_id="reference-fixture",
        dataset_version="2026.09",
        license="CC0 fixture labels",
        annotation_protocol="Two curators adjudicate fields, targets, and anchors.",
        documents=[_gold_document()],
    )
    saved_results = {"doc-1": _perfect_result().model_dump(mode="json")}
    with patch(
        "pathlib.Path.read_text",
        side_effect=[dataset.model_dump_json(), json.dumps(saved_results)],
    ):
        evaluated = runner.invoke(
            app, ["evaluate-references", "gold.json", "results.json"]
        )

    assert evaluated.exit_code == 0, evaluated.stdout
    payload = json.loads(evaluated.stdout)
    assert payload["dataset_version"] == "2026.09"
    assert payload["micro"]["verified_edges"]["f1"] == 1.0


def test_reference_gold_rejects_invalid_target_annotations_and_alias_overlap() -> None:
    with pytest.raises(ValueError, match="require accepted target IDs"):
        type(_gold_document().references[0]).model_validate(
            {
                **_gold_document().references[0].model_dump(mode="json"),
                "accepted_target_record_ids": [],
            }
        )

    payload = _gold_document().model_dump(mode="json")
    payload["references"].append(
        {
            "reference_id": "r3",
            "cited_in_text": True,
            "target_annotation": "linked",
            "accepted_target_record_ids": ["fixture:target", "fixture:alias"],
        }
    )
    with pytest.raises(ValueError, match="overlapping target aliases"):
        ReferenceGoldDocument.model_validate(payload)

    invalid_doi = _gold_document().references[0].model_dump(mode="json")
    invalid_doi["doi"] = "not-a-doi"
    with pytest.raises(ValueError, match="gold DOI must be a valid DOI"):
        type(_gold_document().references[0]).model_validate(invalid_doi)
