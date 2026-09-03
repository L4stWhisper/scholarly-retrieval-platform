from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx

from scholarly_retrieval.models import IdentifierScheme, SearchQuery
from scholarly_retrieval.providers.crossref import CrossrefProvider


def make_crossref_item() -> dict:
    return {
        "DOI": "10.5555/example",
        "URL": "https://doi.org/10.5555/example",
        "title": ["A Crossref Paper"],
        "abstract": "<jats:p>An <b>abstract</b>.</jats:p>",
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "published-online": {"date-parts": [[2024, 2, 3]]},
        "type": "journal-article",
        "container-title": ["Information Processing &amp; Management"],
        "is-referenced-by-count": 7,
        "reference": [
            {
                "DOI": "10.1000/linked",
                "article-title": "Linked reference",
                "author": "Turing",
                "year": "1950",
            },
            {"unstructured": "A reference without a persistent identifier"},
        ],
    }


def test_search_maps_crossref_envelope_and_filters() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert request.url.path == "/v1/works"
            assert query["query.bibliographic"] == ["citation graph"]
            assert query["query.author"] == ["Lovelace"]
            assert query["filter"] == ["from-pub-date:2020-01-01,until-pub-date:2024-12-31"]
            return httpx.Response(
                200,
                json={"message": {"total-results": 9, "items": [make_crossref_item()]}},
            )

        client = httpx.AsyncClient(
            base_url="https://api.crossref.org", transport=httpx.MockTransport(handler)
        )
        provider = CrossrefProvider(client=client)
        batch = await provider.search(
            SearchQuery(
                text="citation graph",
                author="Lovelace",
                year_from=2020,
                year_to=2024,
                open_access=True,
                limit=1,
            )
        )
        await client.aclose()

        paper = batch.papers[0]
        assert paper.abstract == "An abstract ."
        assert paper.venue == "Information Processing & Management"
        assert paper.publication_date.isoformat() == "2024-02-03"
        assert paper.citation_counts[0].count == 7
        assert batch.filter_execution["open_access"] == "unsupported"
        assert batch.truncated is True

    asyncio.run(scenario())


def test_references_preserves_resolvable_and_unresolved_deposits() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            base_url="https://api.crossref.org",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"message": make_crossref_item()})
            ),
        )
        provider = CrossrefProvider(client=client)
        batch = await provider.references("https://doi.org/10.5555/example")
        await client.aclose()

        assert batch.total_available == 2
        assert len(batch.papers) == 1
        assert any(
            claim.scheme == IdentifierScheme.DOI and claim.value == "10.1000/linked"
            for claim in batch.papers[0].identifiers
        )
        assert len(batch.unresolved_references) == 1
        assert batch.unresolved_references[0].reason == "missing_strong_identifier"

    asyncio.run(scenario())


def test_resolve_rejects_non_doi_without_network_request() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        client = httpx.AsyncClient(
            base_url="https://api.crossref.org", transport=httpx.MockTransport(handler)
        )
        provider = CrossrefProvider(client=client)
        paper = await provider.resolve("not-a-doi")
        await client.aclose()

        assert paper is None
        assert calls == 0

    asyncio.run(scenario())
