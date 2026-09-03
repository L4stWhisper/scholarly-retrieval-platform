from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx

from scholarly_retrieval.models import IdentifierScheme, SearchQuery, SearchSort
from scholarly_retrieval.providers.inspire import InspireProvider


def _hit(record_id: int = 451647, *, title: str = "The Large N Limit") -> dict:
    return {
        "id": record_id,
        "metadata": {
            "control_number": record_id,
            "titles": [{"title": title}],
            "authors": [{"full_name": "Juan Maldacena"}],
            "abstracts": [{"value": "A high-energy physics abstract."}],
            "earliest_date": "1997-11-27",
            "publication_info": [{"year": 1998, "journal_title": "Adv. Theor. Math. Phys."}],
            "document_type": ["article"],
            "dois": [{"value": "10.1023/A:1026654312961"}],
            "arxiv_eprints": [{"value": "hep-th/9711200"}],
            "citation_count": 20000,
        },
    }


def test_inspire_search_maps_hep_metadata_and_pagination() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/literature"
            assert parse_qs(request.url.query.decode()) == {
                "q": ["gauge gravity duality"],
                "size": ["2"],
                "sort": ["mostcited"],
            }
            return httpx.Response(
                200,
                json={
                    "hits": {"total": 500, "hits": [_hit()]},
                    "links": {"next": "https://inspirehep.net/api/literature?page=2"},
                },
            )

        client = httpx.AsyncClient(
            base_url="https://inspirehep.net", transport=httpx.MockTransport(handler)
        )
        provider = InspireProvider(client=client)
        batch = await provider.search(
            SearchQuery(
                text="gauge gravity duality",
                sort=SearchSort.CITATIONS,
                limit=2,
            )
        )
        await client.aclose()

        assert batch.total_available == 500
        assert batch.truncated is True
        assert batch.next_cursor == "https://inspirehep.net/api/literature?page=2"
        paper = batch.papers[0]
        assert paper.record_id == "inspire:451647"
        assert paper.publication_year == 1998
        assert paper.citation_counts[0].count == 20000
        assert {claim.scheme for claim in paper.identifiers} == {
            IdentifierScheme.INSPIRE,
            IdentifierScheme.DOI,
            IdentifierScheme.ARXIV,
        }

    asyncio.run(scenario())


def test_inspire_resolves_doi_and_handles_404() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "missing" in str(request.url):
                return httpx.Response(404)
            assert request.url.path == "/api/doi/10.1023/a:1026654312961"
            return httpx.Response(200, json=_hit())

        client = httpx.AsyncClient(
            base_url="https://inspirehep.net", transport=httpx.MockTransport(handler)
        )
        provider = InspireProvider(client=client)
        found = await provider.resolve("10.1023/A:1026654312961")
        missing = await provider.resolve("10.9999/missing")
        await client.aclose()

        assert found is not None
        assert found.title == "The Large N Limit"
        assert missing is None

    asyncio.run(scenario())


def test_inspire_references_preserve_unlinked_items_as_unresolved() -> None:
    async def scenario() -> None:
        payload = _hit()
        payload["metadata"]["references"] = [
            {
                "record": {"$ref": "https://inspirehep.net/api/literature/4328"},
                "reference": {
                    "titles": [{"title": "Weak Interactions"}],
                    "authors": [{"full_name": "S. Glashow"}],
                    "publication_info": [{"year": 1961, "journal_title": "Nucl. Phys."}],
                    "dois": [{"value": "10.1016/0029-5582(61)90469-2"}],
                },
            },
            {
                "reference": {
                    "titles": [{"title": "An unlinked historical note"}],
                    "publication_info": [{"year": 1950}],
                }
            },
        ]
        client = httpx.AsyncClient(
            base_url="https://inspirehep.net",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
        )
        provider = InspireProvider(client=client)
        batch = await provider.references("inspire:451647", limit=2)
        await client.aclose()

        assert batch.total_available == 2
        assert [paper.title for paper in batch.papers] == ["Weak Interactions"]
        assert batch.papers[0].record_id == "inspire:4328"
        assert batch.papers[0].source_records[0].retrieval_context["linked"] is True
        assert len(batch.unresolved_references) == 1
        assert batch.unresolved_references[0].title == "An unlinked historical note"
        assert batch.unresolved_references[0].reason == "missing_strong_identifier"

    asyncio.run(scenario())


def test_inspire_citations_use_refersto_query() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                assert request.url.path == "/api/literature/451647"
                return httpx.Response(200, json=_hit())
            assert parse_qs(request.url.query.decode()) == {
                "q": ["refersto:recid:451647"],
                "size": ["1"],
            }
            return httpx.Response(
                200,
                json={"hits": {"total": {"value": 12}, "hits": [_hit(999, title="Citing work")]}},
            )

        client = httpx.AsyncClient(
            base_url="https://inspirehep.net", transport=httpx.MockTransport(handler)
        )
        provider = InspireProvider(client=client)
        batch = await provider.citations("inspire:451647", limit=1)
        await client.aclose()

        assert batch.total_available == 12
        assert batch.truncated is True
        assert batch.papers[0].record_id == "inspire:999"
        assert batch.papers[0].title == "Citing work"

    asyncio.run(scenario())
