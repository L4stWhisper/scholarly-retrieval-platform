"""Low-load contract tests against real scholarly APIs.

These tests deliberately do not use MockTransport. They are skipped unless
SCHOLAR_RUN_LIVE_TESTS=1, keeping the normal unit suite deterministic and free
of public API quota consumption. Run one provider at a time while diagnosing a
failure; the printed summary contains no credentials or raw response bodies.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from scholarly_retrieval.config import load_environment
from scholarly_retrieval.models import (
    ExtractedReference,
    IdentifierScheme,
    ReferenceEvidenceLevel,
    ReferenceExtractionResult,
    RelatedQuery,
    RelationKind,
    RunStatus,
    SearchQuery,
    SearchSort,
)
from scholarly_retrieval.reference_benchmark import (
    ReferenceGoldDocument,
    evaluate_reference_document,
)
from scholarly_retrieval.reference_smoke import (
    fetch_europe_pmc_jats,
    validate_reference_smoke,
)
from scholarly_retrieval.service import ScholarService

load_environment()
RUN_LIVE = os.getenv("SCHOLAR_RUN_LIVE_TESTS") == "1"
KNOWN_DOI = "10.1038/s41586-021-03819-2"
KNOWN_GOOGLE_SCHOLAR_CLUSTER = "9943926152122871332"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not RUN_LIVE,
        reason="set SCHOLAR_RUN_LIVE_TESTS=1 to call real provider APIs",
    ),
]


def _diagnostic(result: object) -> str:
    """Return a small, credential-free result summary for failed live tests."""

    reports = getattr(result, "provider_reports", [])
    papers = getattr(result, "papers", [])
    payload = {
        "status": str(getattr(result, "status", "unknown")),
        "papers": [
            {"record_id": paper.record_id, "title": paper.title[:160]}
            for paper in papers[:5]
        ],
        "reports": [
            {
                "provider": report.provider,
                "operation": report.operation,
                "status": str(report.status),
                "retrieved_count": report.retrieved_count,
                "truncated": report.truncated,
                "has_next_cursor": report.next_cursor is not None,
                "error_code": report.error_code,
                "error_message": report.error_message,
            }
            for report in reports
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _require_complete(result: object, provider: str, summary: str) -> None:
    """Treat anonymous S2 quota exhaustion as an external smoke-test block."""

    status = getattr(result, "status", None)
    if (
        provider == "semantic_scholar"
        and status == RunStatus.THROTTLED
        and not os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    ):
        pytest.xfail("Semantic Scholar anonymous API returned HTTP 429; configure an API key")
    assert status == RunStatus.COMPLETE, summary


@pytest.mark.parametrize(
    ("provider", "query"),
    [
        ("openalex", "citation graph retrieval"),
        ("semantic_scholar", "citation graph retrieval"),
        ("crossref", "citation graph retrieval"),
        ("arxiv", "retrieval augmented generation"),
    ],
)
def test_live_keyword_search_contract(provider: str, query: str) -> None:
    """Every public connector must map real search results with provenance."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            result = await service.search(SearchQuery(text=query, limit=2), sources=[provider])
            summary = _diagnostic(result)
            print(summary)

            _require_complete(result, provider, summary)
            assert 1 <= len(result.papers) <= 2, summary
            assert result.provider_reports[0].retrieved_count >= len(result.papers), summary
            assert all(paper.title.strip() for paper in result.papers), summary
            assert all(
                any(record.provider == provider for record in paper.source_records)
                for paper in result.papers
            ), summary
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_multi_source_keyword_aggregation_contract() -> None:
    """Three public indexes must contribute to one canonical search result."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            sources = ["openalex", "crossref", "arxiv"]
            result = await service.search(
                SearchQuery(text="citation graph retrieval", limit=6),
                sources=sources,
            )
            summary = _diagnostic(result)
            print(summary)

            assert result.status == RunStatus.COMPLETE, summary
            assert 1 <= len(result.papers) <= 6, summary
            assert {report.provider for report in result.provider_reports} == set(sources)
            contributing = {
                record.provider for paper in result.papers for record in paper.source_records
            }
            assert len(contributing) >= 2, summary
            assert result.raw_record_count >= result.canonical_record_count
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_advanced_search_contract() -> None:
    """Provider filters and canonical local filters must work together on real data."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            query = SearchQuery(
                text="citation graph",
                title="citation",
                year_from=2010,
                year_to=2026,
                work_types=["article"],
                min_citations=5,
                sort=SearchSort.CITATIONS,
                limit=3,
            )
            result = await service.search(query, sources=["openalex"])
            summary = _diagnostic(result)
            print(summary)

            assert result.status == RunStatus.COMPLETE, summary
            assert 1 <= len(result.papers) <= 3, summary
            assert all("citation" in paper.title.casefold() for paper in result.papers)
            assert all(
                paper.publication_year is not None
                and 2010 <= paper.publication_year <= 2026
                for paper in result.papers
            )
            counts = [
                max(claim.count for claim in paper.citation_counts)
                for paper in result.papers
            ]
            assert all(count >= 5 for count in counts)
            assert counts == sorted(counts, reverse=True)
            execution = result.provider_reports[0].filter_execution
            assert execution["year_from"] == "provider"
            assert execution["title"] == "provider"
            assert execution["min_citations"] == "provider"
            assert execution["work_types"] == "provider"
            assert execution["sort"] == "provider"
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_related_multi_retriever_contract() -> None:
    """OpenAlex semantic and S2 seed recommendation share one ranked contract."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            result = await service.related(
                RelatedQuery(
                    # Keep text and seed intent coherent so this contract tests
                    # retrieval quality rather than deliberate query conflict.
                    text="protein structure prediction with deep learning",
                    positive_identifiers=[KNOWN_DOI],
                    limit=3,
                ),
                sources=["openalex", "semantic_scholar"],
            )
            summary = _diagnostic(result)
            print(summary)

            related_reports = [
                report for report in result.provider_reports if report.operation == "related"
            ]
            openalex = next(report for report in related_reports if report.provider == "openalex")
            semantic_scholar = next(
                report for report in related_reports if report.provider == "semantic_scholar"
            )
            assert openalex.status == RunStatus.COMPLETE, summary
            if semantic_scholar.status == RunStatus.THROTTLED:
                assert not os.getenv("SEMANTIC_SCHOLAR_API_KEY")
                assert result.status == RunStatus.PARTIAL, summary
            else:
                assert semantic_scholar.status == RunStatus.COMPLETE, summary
                assert result.status == RunStatus.COMPLETE, summary
            assert 1 <= len(result.papers) <= 3, summary
            assert len(result.rankings) == len(result.papers)
            assert all(ranking.evidence for ranking in result.rankings)
            assert any(
                evidence.provider == "openalex"
                for ranking in result.rankings
                for evidence in ranking.evidence
            )
            assert any(
                token in paper.title.casefold()
                for paper in result.papers
                for token in ("protein", "structure", "fold")
            ), summary
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_multi_source_citation_aggregation_degrades_explicitly() -> None:
    """OpenAlex results survive an independently throttled S2 citation branch."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            result = await service.citations(
                KNOWN_DOI,
                limit=2,
                sources=["openalex", "semantic_scholar"],
            )
            summary = _diagnostic(result)
            print(summary)

            relation_reports = [
                report for report in result.provider_reports if report.operation == "citations"
            ]
            openalex = next(report for report in relation_reports if report.provider == "openalex")
            semantic_scholar = next(
                report for report in relation_reports if report.provider == "semantic_scholar"
            )
            assert openalex.status == RunStatus.COMPLETE, summary
            if semantic_scholar.status == RunStatus.THROTTLED:
                assert not os.getenv("SEMANTIC_SCHOLAR_API_KEY")
                assert result.status == RunStatus.PARTIAL, summary
            else:
                assert semantic_scholar.status == RunStatus.COMPLETE, summary
                assert result.status == RunStatus.COMPLETE, summary
            assert result.papers, summary
            assert all(edge.cited_record_id == result.seed.record_id for edge in result.edges)
            assert all(edge.citing_record_id != edge.cited_record_id for edge in result.edges)
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_europe_pmc_biomedical_search_and_graph_contract() -> None:
    """Europe PMC must execute biomedical filters and both graph directions."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            search = await service.search(
                SearchQuery(
                    text="protein structure prediction",
                    year_from=2020,
                    open_access=True,
                    limit=2,
                ),
                sources=["europe_pmc"],
            )
            search_summary = _diagnostic(search)
            print(search_summary)
            assert search.status == RunStatus.COMPLETE, search_summary
            assert 1 <= len(search.papers) <= 2, search_summary
            assert search.provider_reports[0].filter_execution == {
                "text": "provider",
                "year_from": "provider",
                "open_access": "provider",
            }
            assert all(paper.open_access is True for paper in search.papers)

            resolved = await service.resolve("PMID:34265844", sources=["europe_pmc"])
            resolve_summary = _diagnostic(resolved)
            print(resolve_summary)
            assert resolved.status == RunStatus.COMPLETE, resolve_summary
            assert any(
                claim.scheme == IdentifierScheme.PMID and claim.value == "34265844"
                for claim in resolved.papers[0].identifiers
            )

            references = await service.references(
                "PMID:34265844", limit=2, sources=["europe_pmc"]
            )
            citations = await service.citations(
                "PMID:34265844", limit=2, sources=["europe_pmc"]
            )
            for result in (references, citations):
                summary = _diagnostic(result)
                print(summary)
                assert result.status == RunStatus.COMPLETE, summary
                assert 1 <= len(result.papers) <= 2, summary
                assert len(result.edges) == len(result.papers), summary
                assert all(
                    edge.citing_record_id != edge.cited_record_id for edge in result.edges
                )
            assert all(
                edge.citing_record_id == references.seed.record_id
                for edge in references.edges
            )
            assert all(
                edge.cited_record_id == citations.seed.record_id for edge in citations.edges
            )
        finally:
            await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["openalex", "semantic_scholar", "crossref"])
def test_live_resolve_known_doi_contract(provider: str) -> None:
    """A stable DOI must resolve and retain its normalized DOI claim."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            result = await service.resolve(KNOWN_DOI, sources=[provider])
            summary = _diagnostic(result)
            print(summary)

            _require_complete(result, provider, summary)
            assert len(result.papers) == 1, summary
            assert any(
                claim.scheme == IdentifierScheme.DOI and claim.value == KNOWN_DOI
                for claim in result.papers[0].identifiers
            ), summary
            assert any(
                record.provider == provider for record in result.papers[0].source_records
            ), summary
        finally:
            await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["openalex", "semantic_scholar"])
@pytest.mark.parametrize(
    ("operation", "expected_relation"),
    [("references", RelationKind.REFERENCES), ("citations", RelationKind.CITES)],
)
def test_live_citation_graph_contract(
    provider: str,
    operation: str,
    expected_relation: RelationKind,
) -> None:
    """Real graph calls must preserve bounded results and citing-to-cited direction."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            method = getattr(service, operation)
            try:
                result = await method(KNOWN_DOI, limit=3, sources=[provider])
            except LookupError as exc:
                if (
                    provider == "semantic_scholar"
                    and "http_429" in str(exc)
                    and not os.getenv("SEMANTIC_SCHOLAR_API_KEY")
                ):
                    pytest.xfail(
                        "Semantic Scholar anonymous resolve returned HTTP 429; configure an API key"
                    )
                raise
            summary = _diagnostic(result)
            print(summary)

            _require_complete(result, provider, summary)
            assert 1 <= len(result.papers) <= 3, summary
            assert len(result.edges) == len(result.papers), summary
            assert all(assertion.relation == expected_relation for assertion in result.assertions)
            assert all(
                assertion.evidence_type == "provider_graph"
                and assertion.verification_status == "provider_asserted"
                and assertion.assertion_id.startswith("ca:")
                for assertion in result.assertions
            )
            assert all(
                edge.edge_id.startswith("ce:")
                and edge.verification_status == "provider_asserted"
                for edge in result.edges
            )
            paper_ids = {paper.record_id for paper in result.papers}
            if operation == "references":
                assert all(edge.citing_record_id == result.seed.record_id for edge in result.edges)
                assert {edge.cited_record_id for edge in result.edges} == paper_ids
            else:
                assert all(edge.cited_record_id == result.seed.record_id for edge in result.edges)
                assert {edge.citing_record_id for edge in result.edges} == paper_ids
            assert all(edge.citing_record_id != edge.cited_record_id for edge in result.edges)
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_openalex_reference_accounting_contract() -> None:
    """Every selected OpenAlex reference ID is returned or explicitly unresolved."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            result = await service.references(KNOWN_DOI, limit=100, sources=["openalex"])
            summary = _diagnostic(result)
            print(summary)

            report = next(
                item
                for item in result.provider_reports
                if item.provider == "openalex" and item.operation == "references"
            )
            assert report.status == RunStatus.COMPLETE, summary
            assert report.total_available is not None
            selected_count = min(report.total_available, 100)
            assert report.retrieved_count + report.unresolved_count == selected_count, summary
            assert result.truncated is (report.total_available > 100)
            assert all(
                item.reason == "referenced_openalex_work_not_returned"
                and item.raw.get("openalex_id")
                for item in result.unresolved_references
            )
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_reference_title_scoring_contract() -> None:
    """A real BERT bibliography item must conservatively resolve the ELMo paper."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            extraction = ReferenceExtractionResult(
                source_format="jats",
                bibliography_entry_count=1,
                cited_entry_count=1,
                references=[
                    ExtractedReference(
                        reference_id="elmo",
                        ordinal=1,
                        cited_in_text=True,
                        evidence_level=ReferenceEvidenceLevel.VERIFIED_ANCHOR,
                        raw_text=(
                            "Peters et al. Deep Contextualized Word Representations. 2018."
                        ),
                        title="Deep Contextualized Word Representations",
                        authors=["Peters, Matthew E."],
                        publication_year=2018,
                        callout_count=1,
                    )
                ],
            )
            result = await service.link_references(
                "W2963341956",
                extraction,
                sources=["openalex"],
            )
            summary = _diagnostic(result)
            print(summary)

            assert result.links[0].status == "resolved", summary
            assert result.links[0].candidate_scores[0].title_score == 1.0
            assert result.links[0].candidate_scores[0].author_score == 1.0
            assert result.links[0].candidate_scores[0].year_score == 1.0
            assert result.links[0].decision_reason == "score_and_margin_satisfied"
            assert len(result.edges) == 1
            assert result.edges[0].verification_status == "verified"
            assert result.links[0].candidates[0].record_id == "openalex:W2787560479"

            # This one-item contract validates mechanics, not cross-domain quality.
            # Provenance remains explicit so it cannot be mistaken for the future
            # independently adjudicated reference-edge gold dataset.
            gold = ReferenceGoldDocument(
                document_id="bert-elmo-live-contract",
                domain="computer science",
                source_format="curated_bibliography_item",
                seed_record_ids=[result.seed.record_id],
                provenance=(
                    "Manually checked BERT bibliography item and OpenAlex ELMo Work; "
                    "low-load live contract only"
                ),
                references=[
                    {
                        "reference_id": "elmo",
                        "cited_in_text": True,
                        "title": "Deep Contextualized Word Representations",
                        "authors": ["Peters, Matthew E."],
                        "publication_year": 2018,
                        "target_annotation": "linked",
                        "accepted_target_record_ids": ["openalex:W2787560479"],
                    }
                ],
            )
            metrics = evaluate_reference_document(gold, result)
            assert metrics["candidate_recall"]["recall"] == 1.0
            assert metrics["entity_linking"]["f1"] == 1.0
            assert metrics["verified_edges"]["f1"] == 1.0
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_alphafold_public_jats_reference_smoke() -> None:
    """Public JATS and three open providers must agree on a DOI-backed sample."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            jats_xml = await fetch_europe_pmc_jats("PMC8371605")
            result = await validate_reference_smoke(
                service,
                seed_identifier="10.1038/s41586-021-03819-2",
                pmcid="PMC8371605",
                jats_xml=jats_xml,
                sources=["openalex", "crossref", "europe_pmc"],
                sample_size=5,
                relation_limit=100,
            )
            print(result.model_dump_json(indent=2))

            assert result.bibliography_entry_count >= 80
            assert result.cited_entry_count >= 70
            assert result.bibliography_doi_count >= 60
            assert result.provider_relation_doi_recall is not None
            assert result.provider_relation_doi_recall >= 0.95
            assert result.graph_truncated is False
            assert result.link_success_count == 5
            assert result.path_agreement_count == 5
        finally:
            await service.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.getenv("SERPAPI_API_KEY"),
    reason="SERPAPI_API_KEY is required for the real Google Scholar connector",
)
def test_live_google_scholar_search_contract() -> None:
    """The licensed Google Scholar path is tested only with an explicit key."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            result = await service.search(
                SearchQuery(text="citation graph retrieval", limit=2),
                sources=["google_scholar_serpapi"],
            )
            summary = _diagnostic(result)
            print(summary)
            assert result.status == RunStatus.COMPLETE, summary
            assert 1 <= len(result.papers) <= 2, summary
        finally:
            await service.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.getenv("SERPAPI_API_KEY"),
    reason="SERPAPI_API_KEY is required for the real Google Scholar connector",
)
def test_live_google_scholar_citation_contract() -> None:
    """A real Scholar cluster must expose bounded citing-paper edges."""

    async def scenario() -> None:
        service = ScholarService()
        try:
            result = await service.citations(
                f"google_scholar:{KNOWN_GOOGLE_SCHOLAR_CLUSTER}",
                limit=2,
                sources=["google_scholar_serpapi"],
            )
            summary = _diagnostic(result)
            print(summary)

            assert result.status == RunStatus.COMPLETE, summary
            assert 1 <= len(result.papers) <= 2, summary
            assert len(result.edges) == len(result.papers), summary
            assert all(edge.cited_record_id == result.seed.record_id for edge in result.edges)
            assert all(edge.citing_record_id != edge.cited_record_id for edge in result.edges)
            citation_report = next(
                report for report in result.provider_reports if report.operation == "citations"
            )
            assert citation_report.provider == "google_scholar_serpapi"
            assert citation_report.status == RunStatus.COMPLETE
        finally:
            await service.close()

    asyncio.run(scenario())
