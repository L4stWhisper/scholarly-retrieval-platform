from __future__ import annotations

import asyncio

import httpx
import pytest

from scholarly_retrieval.models import (
    Author,
    CitationCountClaim,
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
    ProviderBatch,
    RelatedQuery,
    RelationKind,
    RetrievalMethod,
    RunStatus,
    SearchQuery,
    SourceRecord,
    UnresolvedReference,
)
from scholarly_retrieval.providers.base import (
    ProviderCapabilities,
    ProviderOperationError,
    ScholarlyProvider,
)
from scholarly_retrieval.providers.registry import ProviderRegistry, default_provider_registry
from scholarly_retrieval.service import ScholarService


class FakeProvider(ScholarlyProvider):
    name = "fake"
    capabilities = ProviderCapabilities(
        keyword_search=True, resolve_id=True, references="list", citations="list"
    )

    async def search(self, query: SearchQuery) -> ProviderBatch:
        return ProviderBatch(papers=[Paper(record_id="fake:S", title=query.text)])

    async def resolve(self, identifier: str) -> Paper | None:
        return Paper(record_id="fake:SEED", title="Seed")

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(papers=[Paper(record_id="fake:R", title="Reference")])

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(papers=[Paper(record_id="fake:C", title="Citing")])


class NativeIdentifierProvider(FakeProvider):
    name = "native_identifier"

    def __init__(self) -> None:
        self.citation_identifier: str | None = None

    async def resolve(self, identifier: str) -> Paper | None:
        if identifier != "native:W1":
            return None
        provenance = Provenance(provider=self.name, source_record_id="W1")
        return Paper(
            record_id="native_identifier:W1",
            title="Portable seed",
            identifiers=[
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value="10.1234/portable-seed",
                    provenance=provenance,
                )
            ],
            source_records=[SourceRecord(provider=self.name, source_record_id="W1")],
        )

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        self.citation_identifier = identifier
        return ProviderBatch(papers=[Paper(record_id="native_identifier:C", title="Citing")])


class PortableIdentifierProvider(FakeProvider):
    name = "portable_identifier"

    def __init__(self) -> None:
        self.resolve_identifiers: list[str] = []
        self.citation_identifier: str | None = None

    async def resolve(self, identifier: str) -> Paper | None:
        self.resolve_identifiers.append(identifier)
        if identifier != "10.1234/portable-seed":
            return None
        provenance = Provenance(provider=self.name, source_record_id="S2")
        return Paper(
            record_id="portable_identifier:S2",
            title="Portable seed",
            identifiers=[
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value="10.1234/portable-seed",
                    provenance=provenance,
                )
            ],
            source_records=[SourceRecord(provider=self.name, source_record_id="S2")],
        )

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        self.citation_identifier = identifier
        return ProviderBatch(papers=[Paper(record_id="portable_identifier:C", title="Citing")])


class RankedKeywordProvider(FakeProvider):
    def __init__(self, name: str, titles: list[str]) -> None:
        self.name = name
        self.titles = titles
        self.received_limit: int | None = None

    async def search(self, query: SearchQuery) -> ProviderBatch:
        self.received_limit = query.limit
        return ProviderBatch(
            papers=[
                Paper(
                    record_id=f"{self.name}:{index}",
                    title=title,
                    source_records=[
                        SourceRecord(
                            provider=self.name,
                            source_record_id=str(index),
                            provider_rank=index,
                        )
                    ],
                )
                for index, title in enumerate(self.titles, start=1)
            ]
        )


class PartialCitationProvider(FakeProvider):
    name = "partial_citation"

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(
            papers=[Paper(record_id="partial_citation:C", title="Recovered citation")],
            status=RunStatus.PARTIAL,
            truncated=True,
            context={"failed_partition_count": 1},
        )


class LocalAdvancedFilterProvider(FakeProvider):
    name = "local_filters"

    def __init__(self) -> None:
        self.received_limit: int | None = None

    async def search(self, query: SearchQuery) -> ProviderBatch:
        self.received_limit = query.limit
        return ProviderBatch(
            papers=[
                Paper(
                    record_id="local_filters:match",
                    title="Matching paper",
                    authors=[Author(name="Ada Researcher")],
                    publication_year=2024,
                    open_access=True,
                ),
                Paper(
                    record_id="local_filters:wrong-year",
                    title="Old paper",
                    authors=[Author(name="Ada Researcher")],
                    publication_year=2019,
                    open_access=True,
                ),
                Paper(
                    record_id="local_filters:wrong-author",
                    title="Other paper",
                    authors=[Author(name="Bob Author")],
                    publication_year=2024,
                    open_access=True,
                ),
            ],
            filter_execution={"text": "provider"},
        )


class SelfRelationProvider(FakeProvider):
    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(papers=[Paper(record_id="fake:SEED", title="Seed")])


class DepositorReferenceProvider(FakeProvider):
    name = "crossref"


def test_provider_registry_adds_connector_without_service_changes() -> None:
    registry = ProviderRegistry()
    registry.register("fake", lambda _store: FakeProvider())

    service = ScholarService(registry=registry)

    assert registry.names == ("fake",)
    assert set(service.provider_capabilities()) == {"fake"}


def test_default_registry_includes_openreview_and_acl_anthology() -> None:
    names = default_provider_registry().names

    assert "openreview" in names
    assert "acl_anthology" in names


def test_provider_registry_rejects_duplicate_and_mismatched_names() -> None:
    registry = ProviderRegistry()
    registry.register("fake", lambda _store: FakeProvider())
    with pytest.raises(ValueError, match="already registered"):
        registry.register("fake", lambda _store: FakeProvider())

    mismatched = ProviderRegistry()
    mismatched.register("alias", lambda _store: FakeProvider())
    with pytest.raises(ValueError, match="factory name mismatch"):
        ScholarService(registry=mismatched)


def test_search_applies_year_author_and_open_access_filters_locally() -> None:
    async def scenario() -> None:
        provider = LocalAdvancedFilterProvider()
        service = ScholarService(providers=[provider])
        result = await service.search(
            SearchQuery(
                text="paper",
                year_from=2024,
                year_to=2024,
                author="Ada",
                open_access=True,
                limit=1,
            )
        )

        assert [paper.record_id for paper in result.papers] == ["local_filters:match"]
        assert provider.received_limit == 3
        assert result.provider_reports[0].filter_execution == {
            "text": "provider",
            "year_from": "local",
            "year_to": "local",
            "author": "local",
            "open_access": "local",
        }

    asyncio.run(scenario())


def test_keyword_search_overfetches_and_globally_ranks_relevant_candidates() -> None:
    async def scenario() -> None:
        first = RankedKeywordProvider(
            "first",
            [
                "Federated learning systems",
                "Dense retrieval for open domain question answering",
                "A third candidate",
            ],
        )
        second = RankedKeywordProvider(
            "second",
            [
                "Dense passage retrieval for question answering",
                "Vision-language navigation",
                "Another candidate",
            ],
        )
        service = ScholarService([first, second])

        result = await service.search(
            SearchQuery(text="dense retrieval open domain question answering", limit=1)
        )

        assert result.papers[0].title == "Dense retrieval for open domain question answering"
        assert first.received_limit == 3
        assert second.received_limit == 3

    asyncio.run(scenario())


def test_relation_translates_native_seed_id_to_portable_identifier() -> None:
    async def scenario() -> None:
        native = NativeIdentifierProvider()
        portable = PortableIdentifierProvider()
        service = ScholarService([native, portable])

        result = await service.citations("native:W1", limit=10)

        assert native.citation_identifier == "native:W1"
        assert portable.resolve_identifiers == ["native:W1", "10.1234/portable-seed"]
        assert portable.citation_identifier == "10.1234/portable-seed"
        assert {paper.record_id for paper in result.papers} == {
            "native_identifier:C",
            "portable_identifier:C",
        }
        portable_resolve = next(
            report
            for report in result.provider_reports
            if report.provider == "portable_identifier" and report.operation == "resolve"
        )
        assert portable_resolve.status == RunStatus.COMPLETE
        assert portable_resolve.context == {
            "identifier_translated_from": "native:W1",
            "identifier_used": "10.1234/portable-seed",
        }

    asyncio.run(scenario())


def test_provider_partial_batch_stays_partial_while_preserving_results() -> None:
    async def scenario() -> None:
        result = await ScholarService([PartialCitationProvider()]).citations("seed")

        assert result.status == RunStatus.PARTIAL
        assert [paper.title for paper in result.papers] == ["Recovered citation"]
        citation_report = next(
            report for report in result.provider_reports if report.operation == "citations"
        )
        assert citation_report.status == RunStatus.PARTIAL
        assert citation_report.context == {"failed_partition_count": 1}

    asyncio.run(scenario())


class ThrottledProvider(FakeProvider):
    name = "throttled"

    async def search(self, query: SearchQuery) -> ProviderBatch:
        request = httpx.Request("GET", "https://example.org/search")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)


class ThrottledResolveProvider(FakeProvider):
    name = "throttled_resolve"

    async def resolve(self, identifier: str) -> Paper | None:
        request = httpx.Request("GET", "https://example.org/paper/seed")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)


class CountOnlyProvider(FakeProvider):
    name = "count_only"
    capabilities = ProviderCapabilities(
        keyword_search=True, resolve_id=True, references="list", citations="count"
    )

    def __init__(self) -> None:
        self.citations_called = False

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        self.citations_called = True
        raise AssertionError("count-only providers must not be called for citation lists")


class LimitedRelationProvider(FakeProvider):
    name = "limited_relation"

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        raise ProviderOperationError("response_too_large", "bounded upstream response too large")


class ResolveOnlyProvider(FakeProvider):
    name = "resolve_only"
    capabilities = ProviderCapabilities(
        keyword_search=False,
        resolve_id=True,
        references="list",
        citations="list",
    )

    def __init__(self) -> None:
        self.search_called = False

    async def search(self, query: SearchQuery) -> ProviderBatch:
        self.search_called = True
        raise AssertionError("resolve-only providers must not receive keyword searches")


class MixedReferenceProvider(FakeProvider):
    name = "mixed"

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(
            papers=[Paper(record_id="mixed:R", title="Resolved reference")],
            unresolved_references=[
                UnresolvedReference(
                    provider=self.name,
                    seed_record_id="mixed:SEED",
                    ordinal=2,
                    raw={"unstructured": "Unknown work"},
                    reason="missing_strong_identifier",
                    provenance=Provenance(
                        provider=self.name,
                        source_record_id="mixed:SEED#reference-2",
                    ),
                )
            ],
            total_available=2,
        )


class DoiResolvingProvider(FakeProvider):
    def __init__(self, name: str) -> None:
        self.name = name

    async def resolve(self, identifier: str) -> Paper | None:
        provenance = Provenance(provider=self.name, source_record_id=f"{self.name}:1")
        return Paper(
            record_id=f"{self.name}:1",
            title="Resolved Paper",
            identifiers=[
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value="10.1234/resolved",
                    provenance=provenance,
                )
            ],
            source_records=[SourceRecord(provider=self.name, source_record_id=f"{self.name}:1")],
        )


class MalformedProvider(FakeProvider):
    name = "malformed"

    async def search(self, query: SearchQuery) -> ProviderBatch:
        raise ValueError("fixture payload missing required title")


class DriftedPayloadProvider(FakeProvider):
    name = "drifted"

    async def search(self, query: SearchQuery) -> ProviderBatch:
        raise AttributeError("fixture field changed from object to string")


class PagedSearchProvider(FakeProvider):
    name = "paged"

    async def search(self, query: SearchQuery) -> ProviderBatch:
        return ProviderBatch(
            papers=[Paper(record_id="paged:S", title=query.text)],
            total_available=10,
            truncated=True,
            next_cursor="opaque-continuation",
        )


class MissingSeedProvider(FakeProvider):
    name = "missing_seed"

    def __init__(self) -> None:
        self.relations_called = False

    async def resolve(self, identifier: str) -> Paper | None:
        return None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        self.relations_called = True
        return ProviderBatch()


class RelatedFixtureProvider(FakeProvider):
    capabilities = ProviderCapabilities(
        keyword_search=True,
        resolve_id=True,
        references="list",
        citations="list",
        related=True,
    )

    def __init__(self, name: str, order: tuple[str, str]) -> None:
        self.name = name
        self.order = order

    def _related_paper(self, local_id: str, rank: int) -> Paper:
        doi = f"10.1234/{local_id.casefold()}"
        provenance = Provenance(provider=self.name, source_record_id=local_id)
        return Paper(
            record_id=f"{self.name}:{local_id}",
            title=f"Related {local_id}",
            identifiers=[
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value=doi,
                    provenance=provenance,
                )
            ],
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=local_id,
                    provider_rank=rank,
                    retrieval_method=RetrievalMethod.OPENALEX_SEMANTIC,
                    retrieval_context={"fixture": self.name},
                )
            ],
        )

    async def related(self, query: RelatedQuery) -> ProviderBatch:
        return ProviderBatch(
            papers=[
                self._related_paper(local_id, rank)
                for rank, local_id in enumerate(self.order, start=1)
            ]
        )

    async def resolve(self, identifier: str) -> Paper | None:
        if identifier == "NEG":
            return self._related_paper("Y", 1)
        return await super().resolve(identifier)


class AdvancedSearchProvider(FakeProvider):
    name = "advanced"

    async def search(self, query: SearchQuery) -> ProviderBatch:
        assert query.limit == 6

        def paper(identifier: str, *, citations: int, year: int, title: str) -> Paper:
            return Paper(
                record_id=f"advanced:{identifier}",
                title=title,
                abstract="Graph retrieval evidence",
                publication_year=year,
                work_type="article",
                venue="Evidence Journal",
                fields_of_study=["Computer Science"],
                citation_counts=[
                    CitationCountClaim(
                        provider=self.name,
                        count=citations,
                        source_record_id=identifier,
                    )
                ],
            )

        return ProviderBatch(
            papers=[
                paper("low", citations=12, year=2022, title="Graph evidence"),
                paper("high", citations=40, year=2024, title="Graph retrieval"),
                paper("noise", citations=100, year=2024, title="Unrelated methods"),
            ]
        )


def test_reference_and_citation_directions_are_explicit() -> None:
    async def scenario() -> None:
        service = ScholarService([FakeProvider()])
        references = await service.references("seed")
        citations = await service.citations("seed")

        ref_edge = references.assertions[0]
        cite_edge = citations.assertions[0]
        assert (ref_edge.subject_record_id, ref_edge.object_record_id, ref_edge.relation) == (
            "fake:SEED",
            "fake:R",
            RelationKind.REFERENCES,
        )
        assert (cite_edge.subject_record_id, cite_edge.object_record_id, cite_edge.relation) == (
            "fake:C",
            "fake:SEED",
            RelationKind.CITES,
        )
        assert ref_edge.evidence_type == "provider_graph"
        assert ref_edge.verification_status == "provider_asserted"
        assert ref_edge.assertion_id.startswith("ca:")
        assert references.edges[0].edge_id.startswith("ce:")
        assert references.edges[0].verification_status == "provider_asserted"

    asyncio.run(scenario())


def test_visible_graph_drops_provider_self_relation() -> None:
    async def scenario() -> None:
        service = ScholarService([SelfRelationProvider()])
        result = await service.citations("seed")

        # Preserve the raw provider assertion for audit, but never expose a
        # self-loop as a usable citation edge.
        assert len(result.assertions) == 1
        assert result.assertions[0].subject_record_id == result.assertions[0].object_record_id
        assert result.edges == []

    asyncio.run(scenario())


def test_crossref_reference_assertion_is_typed_as_depositor_metadata() -> None:
    async def scenario() -> None:
        service = ScholarService([DepositorReferenceProvider()])
        result = await service.references("seed")

        assert result.assertions[0].evidence_type == "depositor_metadata"
        assert result.assertions[0].verification_status == "provider_asserted"

    asyncio.run(scenario())


def test_related_rrf_fuses_canonical_records_and_applies_negative_exclusion() -> None:
    async def scenario() -> None:
        service = ScholarService(
            [
                RelatedFixtureProvider("related_a", ("X", "Y")),
                RelatedFixtureProvider("related_b", ("Y", "X")),
            ]
        )
        result = await service.related(RelatedQuery(text="citation discovery", limit=10, rrf_k=60))
        excluded = await service.related(
            RelatedQuery(
                text="citation discovery",
                negative_identifiers=["NEG"],
                limit=10,
            )
        )

        assert result.status == RunStatus.COMPLETE
        assert result.raw_record_count == 4
        assert result.canonical_record_count == 2
        assert len(result.rankings) == 2
        assert all(len(ranking.evidence) == 2 for ranking in result.rankings)
        assert result.rankings[0].score == result.rankings[1].score
        assert [paper.title for paper in excluded.papers] == ["Related X"]
        assert excluded.fingerprint is not None

    asyncio.run(scenario())


def test_advanced_search_filters_canonical_records_and_sorts_locally() -> None:
    async def scenario() -> None:
        service = ScholarService([AdvancedSearchProvider()])
        result = await service.search(
            SearchQuery(
                text="graph",
                title="graph",
                abstract="retrieval",
                venue="evidence",
                field="computer",
                work_types=[" Article "],
                min_citations=10,
                sort="citations",
                limit=2,
            )
        )

        assert [paper.record_id for paper in result.papers] == [
            "advanced:high",
            "advanced:low",
        ]
        execution = result.provider_reports[0].filter_execution
        assert execution["title"] == "local"
        assert execution["min_citations"] == "local"
        assert execution["sort"] == "local"

    asyncio.run(scenario())


def test_search_plan_discloses_provider_local_and_unsupported_execution() -> None:
    service = ScholarService()
    plan = service.plan_search(
        SearchQuery(
            text="citation graph",
            author="Ada",
            open_access=True,
            title="retrieval",
            work_types=["article"],
            min_citations=5,
            limit=4,
        ),
        sources=["openalex", "crossref"],
    )

    by_provider = {item["provider"]: item for item in plan["plans"]}
    assert plan["fanout"] == 2
    assert by_provider["openalex"]["execution"]["open_access"] == "provider"
    assert by_provider["openalex"]["execution"]["title"] == "provider"
    assert by_provider["openalex"]["execution"]["work_types"] == "provider"
    assert by_provider["crossref"]["execution"]["work_types"] == "local"
    assert by_provider["crossref"]["execution"]["open_access"] == "unsupported"
    assert by_provider["crossref"]["execution"]["title"] == "local"
    assert by_provider["openalex"]["execution"]["min_citations"] == "provider"
    assert by_provider["openalex"]["provider_limit"] == 12


def test_one_throttled_provider_produces_partial_result() -> None:
    async def scenario() -> None:
        service = ScholarService([FakeProvider(), ThrottledProvider()])
        result = await service.search(SearchQuery(text="query"))

        assert result.status == "partial"
        assert [paper.record_id for paper in result.papers] == ["fake:S"]
        assert {report.provider: report.status for report in result.provider_reports} == {
            "fake": "complete",
            "throttled": "throttled",
        }

    asyncio.run(scenario())


def test_relation_seed_throttle_remains_partial_instead_of_becoming_skipped() -> None:
    async def scenario() -> None:
        service = ScholarService([FakeProvider(), ThrottledResolveProvider()])
        result = await service.citations("seed")

        report = next(
            item
            for item in result.provider_reports
            if item.provider == "throttled_resolve" and item.operation == "citations"
        )
        assert result.status == RunStatus.PARTIAL
        assert report.status == RunStatus.THROTTLED
        assert report.error_code == "http_429"
        assert report.context["failed_stage"] == "seed_resolve"
        assert result.papers

    asyncio.run(scenario())


def test_capability_router_skips_count_only_citation_provider() -> None:
    async def scenario() -> None:
        provider = CountOnlyProvider()
        service = ScholarService([provider])
        result = await service.citations("seed")

        assert provider.citations_called is False
        assert result.status == RunStatus.DEGRADED
        relation_report = next(
            report for report in result.provider_reports if report.operation == "citations"
        )
        assert relation_report.status == RunStatus.SKIPPED
        assert relation_report.error_code == "capability_count"

    asyncio.run(scenario())


def test_capability_router_skips_provider_without_keyword_index() -> None:
    async def scenario() -> None:
        provider = ResolveOnlyProvider()
        service = ScholarService([provider])
        result = await service.search(SearchQuery(text="citation graph"))

        assert provider.search_called is False
        assert result.status == RunStatus.DEGRADED
        assert result.provider_reports[0].status == RunStatus.SKIPPED
        assert result.provider_reports[0].error_code == "capability_none"

    asyncio.run(scenario())


def test_expected_provider_limit_is_isolated_as_a_failed_branch() -> None:
    async def scenario() -> None:
        service = ScholarService([LimitedRelationProvider()])
        result = await service.citations("seed")

        assert result.status == RunStatus.FAILED
        report = next(item for item in result.provider_reports if item.operation == "citations")
        assert report.error_code == "response_too_large"
        assert "too large" in (report.error_message or "")

    asyncio.run(scenario())


def test_graph_result_retains_unresolved_reference_records() -> None:
    async def scenario() -> None:
        service = ScholarService([MixedReferenceProvider()])
        result = await service.references("seed")

        assert result.status == RunStatus.COMPLETE
        assert len(result.papers) == 1
        assert len(result.unresolved_references) == 1
        relation_report = next(
            report for report in result.provider_reports if report.operation == "references"
        )
        assert relation_report.retrieved_count == 1
        assert relation_report.unresolved_count == 1

    asyncio.run(scenario())


def test_sqlite_run_audit_records_success_and_failure() -> None:
    async def scenario() -> None:
        service = ScholarService([FakeProvider()], database_path=":memory:")
        successful = await service.search(SearchQuery(text="audited query"))
        try:
            await service.search(SearchQuery(text="bad source"), sources=["unknown"])
        except ValueError:
            pass
        else:
            raise AssertionError("unknown provider must fail")

        assert service.store is not None
        rows = service.store._connection.execute(
            "SELECT status, result_json, result_fingerprint FROM query_runs ORDER BY started_at"
        ).fetchall()
        assert [row["status"] for row in rows] == ["complete", "failed"]
        assert "fake:S" in rows[0]["result_json"]
        assert rows[0]["result_fingerprint"] == successful.fingerprint
        assert "ValueError" in rows[1]["result_json"]
        await service.close()

    asyncio.run(scenario())


def test_audited_relation_run_persists_replayable_citation_evidence() -> None:
    async def scenario() -> None:
        service = ScholarService([FakeProvider()], database_path=":memory:")
        result = await service.references("seed", limit=1)

        assert service.store is not None
        assert service.store.stats()["citation_assertions"] == 1
        assert service.store.stats()["visible_citation_edges"] == 1
        persisted = service.store.get_citation_edge(result.edges[0].edge_id or "")
        assert persisted is not None
        assert persisted.model_dump(mode="json") == result.edges[0].model_dump(mode="json")
        replay = service.citation_evidence(result.edges[0].edge_id or "")
        assert replay["edge"]["edge_id"] == result.edges[0].edge_id
        assert replay["status_events"] == []
        await service.close()

    asyncio.run(scenario())


def test_result_fingerprint_ignores_retrieval_time_but_detects_semantic_change() -> None:
    async def scenario() -> None:
        service = ScholarService([DoiResolvingProvider("source")])
        first = await service.resolve("10.1234/resolved")
        second = await service.resolve("10.1234/resolved")
        changed = await service.resolve("10.1234/a-different-request")

        assert first.fingerprint is not None
        assert first.fingerprint == second.fingerprint
        # The requested identifier is part of the result contract even when a
        # fixture provider happens to return the same paper for every identifier.
        assert changed.fingerprint != first.fingerprint

    asyncio.run(scenario())


def test_provider_continuation_is_exposed_by_aggregate_report() -> None:
    async def scenario() -> None:
        service = ScholarService([PagedSearchProvider()])
        result = await service.search(SearchQuery(text="paged query", limit=1))

        assert result.truncated is True
        assert result.provider_reports[0].next_cursor == "opaque-continuation"
        assert result.fingerprint is not None

    asyncio.run(scenario())


def test_resolve_merges_same_identifier_and_preserves_provider_evidence() -> None:
    async def scenario() -> None:
        service = ScholarService(
            [DoiResolvingProvider("source_a"), DoiResolvingProvider("source_b")]
        )
        result = await service.resolve("https://doi.org/10.1234/resolved")

        assert result.status == RunStatus.COMPLETE
        assert len(result.papers) == 1
        assert {record.provider for record in result.papers[0].source_records} == {
            "source_a",
            "source_b",
        }
        assert [decision.decision for decision in result.identity_decisions] == ["must_link"]

    asyncio.run(scenario())


def test_payload_mapping_error_is_diagnosable_without_losing_good_results() -> None:
    async def scenario() -> None:
        service = ScholarService([FakeProvider(), MalformedProvider()])
        result = await service.search(SearchQuery(text="query"))

        assert result.status == RunStatus.PARTIAL
        assert [paper.record_id for paper in result.papers] == ["fake:S"]
        report = next(item for item in result.provider_reports if item.provider == "malformed")
        assert report.status == RunStatus.FAILED
        assert report.error_code == "ValueError"
        assert report.error_message == "fixture payload missing required title"

    asyncio.run(scenario())


def test_attribute_mapping_drift_is_isolated_to_one_provider() -> None:
    async def scenario() -> None:
        service = ScholarService([FakeProvider(), DriftedPayloadProvider()])
        result = await service.search(SearchQuery(text="query"))

        report = next(item for item in result.provider_reports if item.provider == "drifted")
        assert result.status == RunStatus.PARTIAL
        assert [paper.record_id for paper in result.papers] == ["fake:S"]
        assert report.status == RunStatus.FAILED
        assert report.error_code == "AttributeError"
        assert "object to string" in (report.error_message or "")

    asyncio.run(scenario())


def test_relation_lookup_stops_before_graph_call_when_seed_is_missing() -> None:
    async def scenario() -> None:
        provider = MissingSeedProvider()
        service = ScholarService([provider])
        try:
            await service.references("10.1234/not-found")
        except LookupError as exc:
            assert "paper not found" in str(exc)
        else:
            raise AssertionError("missing seed must raise LookupError")
        assert provider.relations_called is False

    asyncio.run(scenario())


def test_library_rejects_blank_identifiers_and_unbounded_relation_work() -> None:
    async def scenario() -> None:
        service = ScholarService([FakeProvider()])
        for action in (
            lambda: service.resolve("  "),
            lambda: service.references("seed", limit=0),
            lambda: service.citations("seed", limit=1001),
        ):
            try:
                await action()
            except ValueError:
                pass
            else:
                raise AssertionError("invalid library input must fail before provider I/O")

    asyncio.run(scenario())


def test_search_tokens_use_light_stemming_for_plurals_and_inflections() -> None:
    tokens = ScholarService._search_tokens
    assert tokens("Natural-Language Agent Harnesses") == tokens("natural language agent harness")
    assert ScholarService._stem("harnesses") == "harness"
    assert ScholarService._stem("retrieving") == "retriev"
    assert ScholarService._stem("retrieved") == "retriev"
    assert ScholarService._stem("queries") == "query"
    assert ScholarService._stem("analysis") == "analysis"
    assert ScholarService._stem("class") == "class"
    assert ScholarService._stem("bert") == "bert"


def test_keyword_search_drops_notices_and_demotes_retracted_papers() -> None:
    async def scenario() -> None:
        class TypedProvider(FakeProvider):
            name = "typed"

            async def search(self, query: SearchQuery) -> ProviderBatch:
                rows = [
                    ("Deep learning medical image segmentation", "Retraction of Publication"),
                    (
                        "Retracted: Deep learning medical image segmentation",
                        "Retracted Publication",
                    ),
                    ("Deep learning medical image segmentation review", "journal-article"),
                    ("Erratum: deep learning medical image segmentation", "erratum"),
                    ("Deep learning for medical image segmentation", "research-article"),
                ]
                return ProviderBatch(
                    papers=[
                        Paper(
                            record_id=f"typed:{index}",
                            title=title,
                            work_type=work_type,
                            source_records=[
                                SourceRecord(
                                    provider="typed",
                                    source_record_id=str(index),
                                    provider_rank=index,
                                )
                            ],
                        )
                        for index, (title, work_type) in enumerate(rows, start=1)
                    ]
                )

        service = ScholarService([TypedProvider()])
        result = await service.search(
            SearchQuery(text="deep learning medical image segmentation", limit=10)
        )
        titles = [paper.title for paper in result.papers]
        assert "Deep learning medical image segmentation" not in titles  # retraction notice
        assert not any(title.startswith("Erratum") for title in titles)
        assert titles[-1] == "Retracted: Deep learning medical image segmentation"
        assert len(titles) == 3

        explicit = await service.search(
            SearchQuery(
                text="deep learning medical image segmentation",
                work_types=["retraction of publication"],
                limit=10,
            )
        )
        assert [paper.title for paper in explicit.papers] == [
            "Deep learning medical image segmentation"
        ]

    asyncio.run(scenario())


def test_keyword_search_ranks_inflected_title_match_above_partial_matches() -> None:
    async def scenario() -> None:
        provider = RankedKeywordProvider(
            "only",
            [
                "Agent-based Natural Language Interface to Robots",
                "Natural-Language Agent Harnesses",
                "Intelligent Natural Language Dialogue Agent",
            ],
        )
        service = ScholarService([provider])
        result = await service.search(SearchQuery(text="natural language agent harness", limit=3))
        assert result.papers[0].title == "Natural-Language Agent Harnesses"

    asyncio.run(scenario())


def test_throttled_report_includes_provider_hint() -> None:
    class HintedProvider(FakeProvider):
        name = "hinted"
        throttle_hint = "configure HINTED_API_KEY"

        async def search(self, query: SearchQuery) -> ProviderBatch:
            request = httpx.Request("GET", "https://hinted.example/search")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("throttled", request=request, response=response)

    async def scenario() -> None:
        service = ScholarService([HintedProvider()])
        result = await service.search(SearchQuery(text="anything", limit=3))
        report = result.provider_reports[0]
        assert report.status == RunStatus.THROTTLED
        assert report.error_code == "http_429"
        assert "configure HINTED_API_KEY" in (report.error_message or "")
        assert "hinted.example" not in (report.error_message or "")

    asyncio.run(scenario())
