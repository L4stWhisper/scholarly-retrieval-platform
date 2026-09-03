from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx

from scholarly_retrieval.models import IdentifierScheme, SearchQuery
from scholarly_retrieval.providers.openreview import OpenReviewProvider


def _note() -> dict:
    return {
        "id": "abc_DEF-123",
        "forum": "abc_DEF-123",
        "cdate": 1704067200000,
        "invitation": "ICLR.cc/2024/Conference/-/Submission",
        "license": "CC BY 4.0",
        "content": {
            "title": {"value": "Reliable Scholarly Retrieval"},
            "abstract": {"value": "A provenance-aware retrieval system."},
            "authors": {"value": ["Ada Researcher", "Bob Author"]},
            "authorids": {"value": ["~Ada_Researcher1", "~Bob_Author1"]},
            "venue": {"value": "ICLR 2024"},
            "venueid": {"value": "ICLR.cc/2024/Conference"},
            "keywords": {"value": ["information retrieval", "provenance"]},
            "pdf": {"value": "/pdf/abc_DEF-123.pdf"},
            "doi": {"value": "https://doi.org/10.1234/Example"},
            "arxiv_id": {"value": "arXiv:2401.01234v2"},
        },
    }


def test_openreview_search_maps_v2_wrapped_values_and_cursor() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/notes/search"
            assert parse_qs(request.url.query.decode()) == {
                "count": ["true"],
                "limit": ["2"],
                "offset": ["0"],
                "query": ["scholarly retrieval"],
                "source": ["forum"],
            }
            return httpx.Response(200, json={"notes": [_note()], "count": 12})

        client = httpx.AsyncClient(
            base_url="https://api2.openreview.net",
            transport=httpx.MockTransport(handler),
        )
        provider = OpenReviewProvider(client=client)
        batch = await provider.search(SearchQuery(text="scholarly retrieval", limit=2))
        await client.aclose()

        paper = batch.papers[0]
        assert paper.record_id == "openreview:abc_DEF-123"
        assert paper.title == "Reliable Scholarly Retrieval"
        assert paper.publication_year == 2024
        assert paper.venue == "ICLR 2024"
        assert paper.pdf_url == "https://openreview.net/pdf/abc_DEF-123.pdf"
        assert paper.fields_of_study == ["information retrieval", "provenance"]
        assert paper.source_records[0].retrieval_context["license"] == "CC BY 4.0"
        assert {(claim.scheme, claim.value) for claim in paper.identifiers} >= {
            (IdentifierScheme.OPENREVIEW, "abc_DEF-123"),
            (IdentifierScheme.DOI, "10.1234/example"),
            (IdentifierScheme.ARXIV, "2401.01234v2"),
        }
        assert batch.total_available == 12
        assert batch.truncated is True
        assert batch.next_cursor == "1"

    asyncio.run(scenario())


def test_openreview_resolve_accepts_public_forum_url() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/notes"
            assert parse_qs(request.url.query.decode()) == {"id": ["abc_DEF-123"]}
            return httpx.Response(200, json={"notes": [_note()]})

        client = httpx.AsyncClient(
            base_url="https://api2.openreview.net",
            transport=httpx.MockTransport(handler),
        )
        provider = OpenReviewProvider(client=client)
        paper = await provider.resolve("https://openreview.net/forum?id=abc_DEF-123")
        await client.aclose()

        assert paper is not None
        assert paper.record_id == "openreview:abc_DEF-123"

    asyncio.run(scenario())


def test_openreview_missing_id_and_relation_capabilities_are_explicit() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            base_url="https://api2.openreview.net",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"notes": []})),
        )
        provider = OpenReviewProvider(client=client)
        assert await provider.resolve("openreview:not_found") is None
        assert (await provider.references("openreview:not_found")).papers == []
        assert (await provider.citations("openreview:not_found")).papers == []
        assert provider.capabilities.references == "none"
        assert provider.capabilities.citations == "none"
        await client.aclose()

    asyncio.run(scenario())


def test_openreview_resolve_404_returns_none() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            base_url="https://api2.openreview.net",
            transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
        )
        provider = OpenReviewProvider(client=client)
        assert await provider.resolve("openreview:missing_id") is None
        await client.aclose()

    asyncio.run(scenario())
