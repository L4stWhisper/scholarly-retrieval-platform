"""Low-load real-API contracts for the optional open metadata adapters."""

from __future__ import annotations

import asyncio
import os

import pytest

from scholarly_retrieval.config import load_environment
from scholarly_retrieval.models import IdentifierScheme, RunStatus, SearchQuery
from scholarly_retrieval.service import ScholarService

load_environment()
RUN_LIVE = os.getenv("SCHOLAR_RUN_LIVE_TESTS") == "1"
GRAPH_DOI = "10.1186/1756-8722-6-59"
TRANSIENT_ERRORS = {
    "ConnectError",
    "ConnectTimeout",
    "ReadError",
    "ReadTimeout",
    "http_500",
    "http_502",
    "http_503",
    "http_504",
}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not RUN_LIVE,
        reason="set SCHOLAR_RUN_LIVE_TESTS=1 to call real provider APIs",
    ),
]


@pytest.mark.parametrize(
    ("provider", "query_text", "expected_title_token"),
    [
        ("acl_anthology", "BERT pre-training", "bert"),
        ("datacite", "AlphaFold protein structure database", "alphafold"),
        ("dblp", "Attention is All you Need", "attention"),
        ("openaire", "climate change adaptation", "climate"),
        ("openreview", "vision transformer", "vision"),
        ("inspire", "gauge gravity duality", "gravity"),
    ],
)
def test_live_public_search_contract(
    provider: str,
    query_text: str,
    expected_title_token: str,
) -> None:
    async def scenario() -> None:
        service = ScholarService()
        try:
            result = await service.search(
                SearchQuery(text=query_text, limit=3),
                sources=[provider],
            )
            error_codes = {
                report.error_code for report in result.provider_reports if report.error_code
            }
            if error_codes & TRANSIENT_ERRORS:
                pytest.xfail(
                    f"{provider} public endpoint unavailable: {', '.join(sorted(error_codes))}"
                )
            if provider == "openreview" and "http_403" in error_codes:
                pytest.xfail("OpenReview challenge verification blocked this network")
            assert result.status == RunStatus.COMPLETE
            assert 1 <= len(result.papers) <= 3
            assert any(expected_title_token in paper.title.casefold() for paper in result.papers)
            assert result.provider_reports[0].provider == provider
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_acl_anthology_id_resolve_contract() -> None:
    async def scenario() -> None:
        service = ScholarService()
        try:
            try:
                result = await service.resolve("acl_anthology:N19-1423", sources=["acl_anthology"])
            except LookupError as exc:
                if any(error in str(exc) for error in TRANSIENT_ERRORS):
                    pytest.xfail(f"ACL Anthology endpoint unavailable: {exc}")
                raise
            assert result.status == RunStatus.COMPLETE
            paper = result.papers[0]
            assert "bert" in paper.title.casefold()
            assert any(
                claim.scheme == IdentifierScheme.ACL_ANTHOLOGY and claim.value == "N19-1423"
                for claim in paper.identifiers
            )
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_acl_anthology_advanced_search_contract() -> None:
    async def scenario() -> None:
        service = ScholarService()
        try:
            result = await service.search(
                SearchQuery(
                    text="BERT Pre-training of Deep Bidirectional Transformers",
                    year_from=2019,
                    year_to=2019,
                    author="Devlin",
                    open_access=True,
                    limit=3,
                ),
                sources=["acl_anthology"],
            )
            error_codes = {
                report.error_code for report in result.provider_reports if report.error_code
            }
            if error_codes & TRANSIENT_ERRORS:
                pytest.xfail(f"ACL Anthology endpoint unavailable: {sorted(error_codes)}")
            assert result.status == RunStatus.COMPLETE
            assert any(
                "bert" in paper.title.casefold()
                and paper.publication_year == 2019
                and any("devlin" in author.name.casefold() for author in paper.authors)
                for paper in result.papers
            )
            assert result.provider_reports[0].filter_execution == {
                "text": "provider",
                "year_from": "local",
                "year_to": "local",
                "author": "local",
                "open_access": "local",
            }
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_dblp_key_resolve_contract() -> None:
    async def scenario() -> None:
        service = ScholarService()
        try:
            try:
                result = await service.resolve(
                    "dblp:conf/nips/VaswaniSPUJGKP17",
                    sources=["dblp"],
                )
            except LookupError as exc:
                if any(error in str(exc) for error in TRANSIENT_ERRORS):
                    pytest.xfail(f"DBLP public endpoint unavailable: {exc}")
                raise
            assert result.status == RunStatus.COMPLETE
            paper = result.papers[0]
            assert "attention is all you need" in paper.title.casefold()
            assert any(
                claim.scheme == IdentifierScheme.DBLP
                and claim.value == "conf/nips/VaswaniSPUJGKP17"
                for claim in paper.identifiers
            )
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_opencitations_resolve_and_bidirectional_graph_contract() -> None:
    async def scenario() -> None:
        service = ScholarService()
        try:
            try:
                resolved = await service.resolve(GRAPH_DOI, sources=["opencitations"])
            except LookupError as exc:
                if any(error in str(exc) for error in TRANSIENT_ERRORS):
                    pytest.xfail(f"OpenCitations public endpoint unavailable: {exc}")
                raise
            assert resolved.status == RunStatus.COMPLETE
            assert any(
                claim.scheme == IdentifierScheme.DOI and claim.value == GRAPH_DOI
                for claim in resolved.papers[0].identifiers
            )

            references = await service.references(
                GRAPH_DOI,
                limit=2,
                sources=["opencitations"],
            )
            citations = await service.citations(
                GRAPH_DOI,
                limit=2,
                sources=["opencitations"],
            )
            for result in (references, citations):
                error_codes = {
                    report.error_code for report in result.provider_reports if report.error_code
                }
                if error_codes & TRANSIENT_ERRORS:
                    pytest.xfail(
                        "OpenCitations public endpoint unavailable: "
                        f"{', '.join(sorted(error_codes))}"
                    )
                assert result.status == RunStatus.COMPLETE
                relation_report = next(
                    report
                    for report in result.provider_reports
                    if report.operation == result.operation
                )
                # Index v2 may return the same pair from more than one index.
                # Raw assertions remain auditable while visible papers/edges
                # are canonicalized, so raw count need not equal entity count.
                assert relation_report.retrieved_count == 2
                assert 1 <= len(result.papers) <= 2
                assert len(result.edges) == len(result.papers)
                assert len(result.assertions) >= len(result.edges)
                assert relation_report.total_available >= 2
            assert all(
                edge.citing_record_id == references.seed.record_id for edge in references.edges
            )
            assert all(edge.cited_record_id == citations.seed.record_id for edge in citations.edges)
        finally:
            await service.close()

    asyncio.run(scenario())


def test_live_inspire_bidirectional_graph_contract() -> None:
    async def scenario() -> None:
        service = ScholarService()
        try:
            try:
                references = await service.references(
                    "inspire:451647",
                    limit=2,
                    sources=["inspire"],
                )
                citations = await service.citations(
                    "inspire:451647",
                    limit=2,
                    sources=["inspire"],
                )
            except LookupError as exc:
                if any(error in str(exc) for error in TRANSIENT_ERRORS):
                    pytest.xfail(f"INSPIRE public endpoint unavailable: {exc}")
                raise
            for result in (references, citations):
                error_codes = {
                    report.error_code for report in result.provider_reports if report.error_code
                }
                if error_codes & TRANSIENT_ERRORS:
                    pytest.xfail(
                        f"INSPIRE public endpoint unavailable: {', '.join(sorted(error_codes))}"
                    )
                assert result.status == RunStatus.COMPLETE
                assert 1 <= len(result.papers) <= 2
                assert len(result.edges) == len(result.papers)
            assert all(
                edge.citing_record_id == references.seed.record_id for edge in references.edges
            )
            assert all(edge.cited_record_id == citations.seed.record_id for edge in citations.edges)
        finally:
            await service.close()

    asyncio.run(scenario())
