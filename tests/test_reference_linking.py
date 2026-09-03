import asyncio

from scholarly_retrieval.models import (
    CitationContext,
    ExtractedReference,
    Paper,
    ProviderBatch,
    ReferenceEvidenceLevel,
    ReferenceExtractionResult,
    SearchQuery,
)
from scholarly_retrieval.providers.base import ProviderCapabilities, ScholarlyProvider
from scholarly_retrieval.service import ScholarService


class LinkingProvider(ScholarlyProvider):
    name = "linking"
    capabilities = ProviderCapabilities(keyword_search=True, resolve_id=True)

    async def search(self, query: SearchQuery) -> ProviderBatch:
        if query.text == "Ambiguous title":
            return ProviderBatch(
                papers=[
                    Paper(record_id="linking:A", title=query.text),
                    Paper(record_id="linking:B", title=query.text),
                ]
            )
        if query.text == "Metadata title":
            assert query.year_from == 2019
            assert query.year_to == 2021
            assert query.venue == "Test Journal"
            return ProviderBatch(
                papers=[
                    Paper(
                        record_id="linking:metadata",
                        title=query.text,
                        publication_year=2020,
                        venue="Test Journal",
                    )
                ]
            )
        if query.text == "Clear winner":
            return ProviderBatch(
                papers=[
                    Paper(record_id="linking:weak", title="Clear winner extended appendix"),
                    Paper(record_id="linking:clear", title="Clear winner"),
                ]
            )
        if query.text == "Weak match":
            return ProviderBatch(
                papers=[
                    Paper(
                        record_id="linking:weak-only",
                        title="Weak match extended unrelated appendix and survey",
                    )
                ]
            )
        return ProviderBatch()

    async def resolve(self, identifier: str) -> Paper | None:
        if identifier == "seed":
            return Paper(record_id="linking:seed", title="Seed")
        if identifier == "10.1234/ref":
            return Paper(record_id="linking:ref", title="Resolved reference")
        return None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()


class ConcurrentLinkingProvider(LinkingProvider):
    name = "concurrent_linking"

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def resolve(self, identifier: str) -> Paper | None:
        if identifier == "seed":
            return Paper(record_id=f"{self.name}:seed", title="Seed")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return Paper(record_id=f"{self.name}:{identifier}", title=identifier)
        finally:
            self.active -= 1


class PersistentLinkingProvider(LinkingProvider):
    capabilities = ProviderCapabilities(
        keyword_search=True,
        resolve_id=True,
        references="list",
    )

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(
            papers=[Paper(record_id="linking:ref", title="Resolved reference")]
        )


def test_reference_linking_emits_edges_only_for_unique_verified_anchors() -> None:
    async def scenario() -> None:
        extraction = ReferenceExtractionResult(
            source_format="jats",
            bibliography_entry_count=3,
            cited_entry_count=2,
            references=[
                ExtractedReference(
                    reference_id="R1",
                    ordinal=1,
                    cited_in_text=True,
                    evidence_level=ReferenceEvidenceLevel.VERIFIED_ANCHOR,
                    raw_text="Resolved",
                    doi="10.1234/ref",
                    callout_count=2,
                    contexts=[
                        CitationContext(
                            context_id="R1:context-1",
                            text="Prior work [1].",
                            anchor_text="[1]",
                        )
                    ],
                ),
                ExtractedReference(
                    reference_id="R2",
                    ordinal=2,
                    cited_in_text=True,
                    evidence_level=ReferenceEvidenceLevel.VERIFIED_ANCHOR,
                    raw_text="Ambiguous",
                    title="Ambiguous title",
                    callout_count=1,
                ),
                ExtractedReference(
                    reference_id="R3",
                    ordinal=3,
                    cited_in_text=False,
                    evidence_level=ReferenceEvidenceLevel.BIBLIOGRAPHY_ONLY,
                    raw_text="Additional reading",
                    doi="10.1234/ref",
                ),
            ],
        )
        service = ScholarService([LinkingProvider()])
        result = await service.link_references("seed", extraction)
        await service.close()

        assert result.resolved_count == 2
        assert result.ambiguous_count == 1
        assert result.unresolved_count == 0
        assert [link.status for link in result.links] == [
            "resolved",
            "ambiguous",
            "resolved",
        ]
        assert len(result.edges) == 1
        assert result.edges[0].citing_record_id == "linking:seed"
        assert result.edges[0].cited_record_id == "linking:ref"
        assert result.assertions[0].evidence == {
            "method": "doi_resolve",
            "evidence_level": "verified_anchor",
            "callout_count": 2,
            "citation_contexts": [
                {
                    "context_id": "R1:context-1",
                    "text": "Prior work [1].",
                    "anchor_text": "[1]",
                    "section_title": None,
                    "source_locator": None,
                }
            ],
        }
        assert result.assertions[0].evidence_type == "fulltext_anchor"
        assert result.assertions[0].verification_status == "verified"
        assert result.edges[0].verification_status == "verified"
        assert len(result.links[1].entity_view.manifestations) == 2

    asyncio.run(scenario())


def test_reference_linking_is_bounded_concurrent_and_preserves_input_order() -> None:
    async def scenario() -> None:
        extraction = ReferenceExtractionResult(
            source_format="bibtex",
            bibliography_entry_count=6,
            cited_entry_count=0,
            references=[
                ExtractedReference(
                    reference_id=f"R{index}",
                    ordinal=index,
                    cited_in_text=False,
                    evidence_level=ReferenceEvidenceLevel.BIBLIOGRAPHY_ONLY,
                    raw_text=f"Reference {index}",
                    doi=f"10.1234/ref-{index}",
                )
                for index in range(1, 7)
            ],
        )
        provider = ConcurrentLinkingProvider()
        service = ScholarService([provider])

        result = await service.link_references("seed", extraction)
        await service.close()

        assert provider.max_active == 4
        assert [link.reference.reference_id for link in result.links] == [
            "R1",
            "R2",
            "R3",
            "R4",
            "R5",
            "R6",
        ]
        assert result.assertions == []

    asyncio.run(scenario())


def test_reference_linking_uses_extracted_year_and_venue_for_title_fallback() -> None:
    async def scenario() -> None:
        extraction = ReferenceExtractionResult(
            source_format="jats",
            bibliography_entry_count=1,
            cited_entry_count=1,
            references=[
                ExtractedReference(
                    reference_id="R1",
                    ordinal=1,
                    cited_in_text=True,
                    evidence_level=ReferenceEvidenceLevel.VERIFIED_ANCHOR,
                    raw_text="Metadata title. Test Journal. 2020.",
                    title="Metadata title",
                    publication_year=2020,
                    venue="Test Journal",
                    callout_count=1,
                )
            ],
        )
        service = ScholarService([LinkingProvider()])

        result = await service.link_references("seed", extraction)
        await service.close()

        assert result.resolved_count == 1
        assert result.links[0].method == "title_metadata_search"
        assert result.links[0].candidates[0].record_id == "linking:metadata"
        assert len(result.edges) == 1

    asyncio.run(scenario())


def test_fulltext_linking_upgrades_a_persisted_provider_edge() -> None:
    async def scenario() -> None:
        service = ScholarService([PersistentLinkingProvider()], database_path=":memory:")
        provider_result = await service.references("seed", limit=1)
        assert provider_result.edges[0].verification_status == "provider_asserted"

        extraction = ReferenceExtractionResult(
            source_format="jats",
            bibliography_entry_count=1,
            cited_entry_count=1,
            references=[
                ExtractedReference(
                    reference_id="R1",
                    ordinal=1,
                    cited_in_text=True,
                    evidence_level=ReferenceEvidenceLevel.VERIFIED_ANCHOR,
                    raw_text="Resolved reference",
                    doi="10.1234/ref",
                    callout_count=1,
                )
            ],
        )
        fulltext_result = await service.link_references("seed", extraction)

        assert service.store is not None
        assert fulltext_result.edges[0].edge_id == provider_result.edges[0].edge_id
        persisted = service.store.get_citation_edge(fulltext_result.edges[0].edge_id or "")
        assert persisted is not None
        assert persisted.verification_status == "verified"
        assert {item.evidence_type for item in persisted.assertions} == {
            "provider_graph",
            "fulltext_anchor",
        }
        assert len(service.store.list_citation_edge_events(edge_id=persisted.edge_id)) == 1
        await service.close()

    asyncio.run(scenario())


def test_reference_scores_select_a_clear_winner_but_reject_a_low_single_candidate() -> None:
    async def scenario() -> None:
        extraction = ReferenceExtractionResult(
            source_format="jats",
            bibliography_entry_count=2,
            cited_entry_count=2,
            references=[
                ExtractedReference(
                    reference_id="R1",
                    ordinal=1,
                    cited_in_text=True,
                    evidence_level=ReferenceEvidenceLevel.VERIFIED_ANCHOR,
                    raw_text="Clear winner",
                    title="Clear winner",
                    callout_count=1,
                ),
                ExtractedReference(
                    reference_id="R2",
                    ordinal=2,
                    cited_in_text=True,
                    evidence_level=ReferenceEvidenceLevel.VERIFIED_ANCHOR,
                    raw_text="Weak match",
                    title="Weak match",
                    callout_count=1,
                ),
            ],
        )
        service = ScholarService([LinkingProvider()])

        result = await service.link_references("seed", extraction)
        await service.close()

        assert [link.status for link in result.links] == ["resolved", "unresolved"]
        assert result.links[0].candidates[0].record_id == "linking:clear"
        assert result.links[0].candidate_scores[0].total_score == 1.0
        assert result.links[0].decision_reason == "score_and_margin_satisfied"
        assert result.links[1].candidate_scores[0].total_score < 0.92
        assert result.links[1].decision_reason == "top_score_below_auto_match_threshold"
        assert len(result.edges) == 1
        assert result.edges[0].cited_record_id == "linking:clear"

    asyncio.run(scenario())


def test_reference_matching_policy_is_deployment_configurable(monkeypatch) -> None:
    monkeypatch.setenv("SCHOLAR_REFERENCE_AUTO_MATCH_THRESHOLD", "0.97")
    monkeypatch.setenv("SCHOLAR_REFERENCE_MINIMUM_MARGIN", "0.12")
    service = ScholarService([LinkingProvider()])

    assert service.configuration_status()["reference_matching_policy"] == {
        "auto_match_threshold": 0.97,
        "minimum_margin": 0.12,
    }
