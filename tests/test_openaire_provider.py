from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx

from scholarly_retrieval.models import IdentifierScheme, SearchQuery, SearchSort
from scholarly_retrieval.providers.openaire import OpenAireProvider


def _resource() -> dict:
    return {
        "id": "doi_dedup___::abc123",
        "type": "publication",
        "mainTitle": "Climate policy and open science",
        "authors": [{"fullName": "Ada Researcher"}, {"fullName": "Bo Author"}],
        "descriptions": ["A multidisciplinary research abstract."],
        "publicationDate": "2024-03-12",
        "publisher": "Open Publisher",
        "container": {"name": "Research Policy"},
        "bestAccessRight": {"label": "OPEN"},
        "pids": [
            {"scheme": "doi", "value": "10.1234/OPEN.1"},
            {"scheme": "pmid", "value": "12345678"},
        ],
        "instances": [
            {
                "type": "Article",
                "urls": ["https://repository.example/paper"],
            }
        ],
        "indicators": {"citationImpact": {"citationCount": 17}},
    }


def test_openaire_search_compiles_v3_filters_and_maps_provenance() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/graph/v3/research-products"
            query = parse_qs(request.url.query.decode())
            assert query == {
                "accessRightLabel": ["Open Access"],
                "authorFullName": ["Ada"],
                "fromPublicationYear": ["2020"],
                "mainTitle": ["climate"],
                "pageSize": ["3"],
                "search": ["open science"],
                "sortBy": ["citationCount DESC"],
                "toPublicationYear": ["2025"],
                "type": ["publication"],
            }
            return httpx.Response(
                200,
                json={
                    "header": {"numFound": 42, "nextCursor": "opaque-next"},
                    "results": [_resource()],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://api.openaire.eu", transport=httpx.MockTransport(handler)
        )
        provider = OpenAireProvider(client=client)
        batch = await provider.search(
            SearchQuery(
                text="open science",
                title="climate",
                author="Ada",
                year_from=2020,
                year_to=2025,
                open_access=True,
                sort=SearchSort.CITATIONS,
                limit=3,
            )
        )
        await client.aclose()

        assert batch.total_available == 42
        assert batch.truncated is True
        assert batch.next_cursor == "opaque-next"
        paper = batch.papers[0]
        assert paper.title == "Climate policy and open science"
        assert paper.publication_year == 2024
        assert paper.venue == "Research Policy"
        assert paper.open_access is True
        assert paper.citation_counts[0].count == 17
        assert {claim.scheme for claim in paper.identifiers} == {
            IdentifierScheme.OPENAIRE,
            IdentifierScheme.DOI,
            IdentifierScheme.PMID,
        }
        assert paper.source_records[0].provider == "openaire"

    asyncio.run(scenario())


def test_openaire_resolve_doi_requires_exact_pid_match() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query["pid"] in (["10.1234/open.1"], ["10.9999/missing"])
            resources = [_resource()] if query["pid"] == ["10.1234/open.1"] else []
            return httpx.Response(
                200,
                json={"header": {"numFound": len(resources)}, "results": resources},
            )

        client = httpx.AsyncClient(
            base_url="https://api.openaire.eu", transport=httpx.MockTransport(handler)
        )
        provider = OpenAireProvider(client=client)
        found = await provider.resolve("https://doi.org/10.1234/OPEN.1")
        missing = await provider.resolve("10.9999/missing")
        await client.aclose()

        assert found is not None
        assert any(
            claim.scheme == IdentifierScheme.DOI and claim.value == "10.1234/open.1"
            for claim in found.identifiers
        )
        assert missing is None

    asyncio.run(scenario())


def test_openaire_resolve_native_id_and_404() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("missing"):
                return httpx.Response(404)
            assert request.url.path.endswith("doi_dedup___::abc123")
            return httpx.Response(200, json=_resource())

        client = httpx.AsyncClient(
            base_url="https://api.openaire.eu", transport=httpx.MockTransport(handler)
        )
        provider = OpenAireProvider(client=client)
        found = await provider.resolve("openaire:doi_dedup___::abc123")
        missing = await provider.resolve("openaire:missing")
        await client.aclose()

        assert found is not None
        assert found.record_id == "openaire:doi_dedup___::abc123"
        assert missing is None

    asyncio.run(scenario())
