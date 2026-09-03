from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx

from scholarly_retrieval.models import IdentifierScheme, SearchQuery, SearchSort
from scholarly_retrieval.providers.datacite import DataCiteProvider


def _resource() -> dict:
    return {
        "id": "10.5281/zenodo.123",
        "type": "dois",
        "attributes": {
            "doi": "10.5281/zenodo.123",
            "titles": [{"title": "Citation Graph Dataset"}],
            "creators": [{"name": "Ada Researcher"}],
            "published": "2024-03-02",
            "publicationYear": 2024,
            "publisher": "Zenodo",
            "types": {"resourceTypeGeneral": "Dataset"},
            "descriptions": [
                {"descriptionType": "Abstract", "description": "An open graph dataset."}
            ],
            "url": "https://zenodo.org/records/123",
            "citationCount": 7,
        },
    }


def test_datacite_search_compiles_fields_and_maps_claims() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query["page[size]"] == ["2"]
            assert query["sort"] == ["-citation-count"]
            assert 'titles.title:"Citation Graph"' in query["query"][0]
            assert 'creators.name:"Ada"' in query["query"][0]
            assert "publicationYear:[2020 TO 2025]" in query["query"][0]
            return httpx.Response(
                200,
                json={"data": [_resource()], "meta": {"total": 9}},
            )

        client = httpx.AsyncClient(
            base_url="https://api.datacite.org", transport=httpx.MockTransport(handler)
        )
        provider = DataCiteProvider(client=client)
        batch = await provider.search(
            SearchQuery(
                text="citation graph",
                title="Citation Graph",
                author="Ada",
                year_from=2020,
                year_to=2025,
                sort=SearchSort.CITATIONS,
                limit=2,
            )
        )
        await client.aclose()

        paper = batch.papers[0]
        assert paper.record_id == "datacite:10.5281/zenodo.123"
        assert paper.work_type == "dataset"
        assert paper.citation_counts[0].count == 7
        assert paper.abstract == "An open graph dataset."
        assert batch.total_available == 9
        assert batch.truncated is True
        assert batch.filter_execution["title"] == "provider"

    asyncio.run(scenario())


def test_datacite_resolve_uses_exact_doi() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # ``URL.path`` is decoded; ``raw_path`` verifies the actual wire URL.
            assert request.url.raw_path == b"/dois/10.5281%2Fzenodo.123"
            return httpx.Response(200, json={"data": _resource()})

        client = httpx.AsyncClient(
            base_url="https://api.datacite.org", transport=httpx.MockTransport(handler)
        )
        provider = DataCiteProvider(client=client)
        paper = await provider.resolve("https://doi.org/10.5281/zenodo.123")
        await client.aclose()

        assert paper is not None
        assert any(
            claim.scheme == IdentifierScheme.DOI and claim.value == "10.5281/zenodo.123"
            for claim in paper.identifiers
        )

    asyncio.run(scenario())
