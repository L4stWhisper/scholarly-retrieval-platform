from __future__ import annotations

import asyncio

import httpx
import pytest

from scholarly_retrieval.models import IdentifierScheme
from scholarly_retrieval.providers.base import ProviderOperationError
from scholarly_retrieval.providers.opencitations import OpenCitationsProvider

SEED_METADATA = {
    "id": "doi:10.1234/seed openalex:W1 pmid:1 omid:br/1",
    "title": "Seed Paper",
    "author": "Researcher, Ada [orcid:0000-0000-0000-0001 omid:ra/1]",
    "pub_date": "2024-01-02",
    "venue": "Journal [issn:1234-5678]",
    "type": "journal article",
}


def test_opencitations_resolve_maps_multi_identifier_metadata() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/meta/v1/metadata/doi:10.1234/seed"
            return httpx.Response(200, json=[SEED_METADATA])

        client = httpx.AsyncClient(
            base_url="https://api.opencitations.net", transport=httpx.MockTransport(handler)
        )
        provider = OpenCitationsProvider(client=client)
        paper = await provider.resolve("10.1234/seed")
        await client.aclose()

        assert paper is not None
        assert paper.title == "Seed Paper"
        assert paper.venue == "Journal"
        assert {claim.scheme for claim in paper.identifiers} == {
            IdentifierScheme.DOI,
            IdentifierScheme.OPENALEX,
            IdentifierScheme.PMID,
            IdentifierScheme.OMID,
        }

    asyncio.run(scenario())


def test_opencitations_references_counts_fetches_and_enriches_edges() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "/reference-count/" in path:
                return httpx.Response(200, json=[{"count": "2"}])
            if "/index/v2/references/" in path:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "oci": "1-2",
                            "citing": "doi:10.1234/seed omid:br/1",
                            "cited": "doi:10.1234/ref-a openalex:W2 omid:br/2",
                            "creation": "2024-01-02",
                            "timespan": "P1Y",
                        },
                        {
                            "oci": "1-3",
                            "citing": "doi:10.1234/seed omid:br/1",
                            "cited": "doi:10.1234/ref-b omid:br/3",
                            "creation": "2024-01-02",
                            "timespan": "P2Y",
                        },
                    ],
                )
            assert "/meta/v1/metadata/doi:10.1234/ref-a__doi:10.1234/ref-b" in path
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "doi:10.1234/ref-a openalex:W2 omid:br/2",
                        "title": "Reference A",
                        "author": "Author, Alice [omid:ra/2]",
                        "pub_date": "2023",
                        "venue": "Venue A [issn:1]",
                        "type": "journal article",
                    },
                    {
                        "id": "doi:10.1234/ref-b omid:br/3",
                        "title": "Reference B",
                        "author": "Author, Bob [omid:ra/3]",
                        "pub_date": "2022",
                        "venue": "Venue B [issn:2]",
                        "type": "journal article",
                    },
                ],
            )

        client = httpx.AsyncClient(
            base_url="https://api.opencitations.net", transport=httpx.MockTransport(handler)
        )
        provider = OpenCitationsProvider(client=client)
        batch = await provider.references("10.1234/seed", limit=2)
        await client.aclose()

        assert [paper.title for paper in batch.papers] == ["Reference A", "Reference B"]
        assert batch.total_available == 2
        assert batch.truncated is False
        assert batch.papers[0].source_records[0].retrieval_context["oci"] == "1-2"

    asyncio.run(scenario())


def test_opencitations_refuses_unpageable_oversized_relation() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            base_url="https://api.opencitations.net",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=[{"count": "100"}])
            ),
        )
        provider = OpenCitationsProvider(client=client, max_relations=10)
        with pytest.raises(ProviderOperationError, match="exceeds safe fetch cap"):
            await provider.citations("10.1234/seed", limit=2)
        await client.aclose()

    asyncio.run(scenario())
