from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from scholarly_retrieval.models import IdentifierScheme, SearchQuery
from scholarly_retrieval.providers.arxiv import ArxivProvider

ATOM_RESPONSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>42</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2401.12345v2</id>
    <updated>2024-02-02T00:00:00Z</updated>
    <published>2024-01-15T00:00:00Z</published>
    <title>A Citation Graph Paper</title>
    <summary>An arXiv abstract.</summary>
    <author><name>Ada Researcher</name></author>
    <link title="pdf" href="https://arxiv.org/pdf/2401.12345v2" type="application/pdf" />
    <arxiv:primary_category term="cs.DL" />
    <arxiv:doi>https://doi.org/10.5555/arxiv-example</arxiv:doi>
  </entry>
</feed>
"""


def test_search_constructs_official_query_and_parses_atom() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert 'all:"citation" AND all:"graph"' in query["search_query"][0]
            assert 'au:"Ada"' in query["search_query"][0]
            assert "submittedDate:[202001010000 TO 202412312359]" in query["search_query"][0]
            assert query["max_results"] == ["1"]
            return httpx.Response(200, content=ATOM_RESPONSE)

        client = httpx.AsyncClient(
            base_url="https://export.arxiv.org", transport=httpx.MockTransport(handler)
        )
        provider = ArxivProvider(client=client)
        batch = await provider.search(
            SearchQuery(
                text="citation graph",
                author="Ada",
                year_from=2020,
                year_to=2024,
                limit=1,
            )
        )
        await client.aclose()

        paper = batch.papers[0]
        assert paper.record_id == "arxiv:2401.12345v2"
        assert paper.work_family_id == "arxiv:2401.12345"
        assert paper.venue == "cs.DL"
        assert paper.pdf_url.endswith("2401.12345v2")
        assert any(
            claim.scheme == IdentifierScheme.DOI and claim.value == "10.5555/arxiv-example"
            for claim in paper.identifiers
        )
        assert batch.total_available == 42
        assert batch.truncated is True

    asyncio.run(scenario())


def test_resolve_uses_id_list_and_preserves_requested_version() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query["id_list"] == ["2401.12345v2"]
            return httpx.Response(200, content=ATOM_RESPONSE)

        client = httpx.AsyncClient(
            base_url="https://export.arxiv.org", transport=httpx.MockTransport(handler)
        )
        provider = ArxivProvider(client=client)
        paper = await provider.resolve("arXiv:2401.12345v2")
        await client.aclose()

        assert paper is not None
        assert paper.record_id == "arxiv:2401.12345v2"

    asyncio.run(scenario())


def test_invalid_atom_is_reported_as_mapping_error() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            base_url="https://export.arxiv.org",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"<not-closed>")
            ),
        )
        provider = ArxivProvider(client=client)
        with pytest.raises(ValueError, match="invalid arXiv Atom response"):
            await provider.search(SearchQuery(text="graph"))
        await client.aclose()

    asyncio.run(scenario())
