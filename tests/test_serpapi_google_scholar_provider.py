import asyncio
from urllib.parse import parse_qs

import httpx

from scholarly_retrieval.models import IdentifierScheme, RunStatus, SearchQuery
from scholarly_retrieval.providers.serpapi_google_scholar import (
    SerpApiGoogleScholarProvider,
)
from scholarly_retrieval.service import ScholarService


def scholar_item(index: int, *, cites_id: str = "12345") -> dict:
    return {
        "position": index,
        "title": f"Scholar Paper {index}",
        "result_id": f"RESULT-{index}",
        "link": f"https://doi.org/10.1234/PAPER{index}",
        "snippet": "An abstract snippet.",
        "publication_info": {
            "summary": "Ada Researcher, Bob Scholar - Journal, 2024",
            "authors": [{"name": "Ada Researcher"}, {"name": "Bob Scholar"}],
        },
        "resources": [
            {
                "file_format": "PDF",
                "link": f"https://example.org/{index}.pdf",
            }
        ],
        "inline_links": {
            "cited_by": {"total": 10 + index, "cites_id": cites_id},
            "versions": {"cluster_id": cites_id},
        },
    }


def test_serpapi_search_maps_claims_and_uses_official_20_item_pagination() -> None:
    async def scenario() -> None:
        starts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query["engine"] == ["google_scholar"]
            assert query["api_key"] == ["secret"]
            starts.append(int(query["start"][0]))
            start = starts[-1]
            count = 20 if start == 0 else 5
            return httpx.Response(
                200,
                json={
                    "search_information": {"total_results": 30},
                    "organic_results": [scholar_item(start + i) for i in range(count)],
                    "serpapi_pagination": ({"next": "present"} if start == 0 else {}),
                },
            )

        client = httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        )
        provider = SerpApiGoogleScholarProvider(api_key="secret", client=client)
        batch = await provider.search(
            SearchQuery(
                text="citation graph",
                author="Ada",
                venue="Journal",
                year_from=2020,
                year_to=2025,
                limit=25,
            )
        )
        await client.aclose()

        assert starts == [0, 20]
        assert len(batch.papers) == 25
        paper = batch.papers[0]
        assert paper.record_id == "google_scholar:RESULT-0"
        assert paper.publication_year == 2024
        assert paper.open_access is True
        assert paper.citation_counts[0].count == 10
        assert {(claim.scheme, claim.value) for claim in paper.identifiers} == {
            (IdentifierScheme.GOOGLE_SCHOLAR, "12345"),
            (IdentifierScheme.DOI, "10.1234/paper0"),
        }
        assert batch.filter_execution["author"] == "provider"
        assert batch.next_cursor is None

    asyncio.run(scenario())


def test_serpapi_citations_uses_cluster_cites_parameter() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query["cites"] == ["12345"]
            return httpx.Response(
                200,
                json={
                    "search_information": {"total_results": 1},
                    "organic_results": [scholar_item(1, cites_id="999")],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        )
        provider = SerpApiGoogleScholarProvider(api_key="secret", client=client)
        batch = await provider.citations("google_scholar:12345", limit=10)
        await client.aclose()

        assert [paper.record_id for paper in batch.papers] == ["google_scholar:RESULT-1"]
        assert batch.truncated is False

    asyncio.run(scenario())


def test_serpapi_citations_unions_multiple_google_scholar_clusters() -> None:
    async def scenario() -> None:
        seen_clusters: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            seen_clusters.append(query["cites"][0])
            result_index = 1 if query["cites"] == ["111"] else 2
            return httpx.Response(
                200,
                json={
                    "search_information": {"total_results": 1},
                    "organic_results": [scholar_item(result_index, cites_id="999")],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        )
        provider = SerpApiGoogleScholarProvider(api_key="secret", client=client)
        batch = await provider.citations("google_scholar:111,222", limit=10)
        await client.aclose()

        assert list(dict.fromkeys(seen_clusters)) == ["111", "222"]
        assert len(batch.papers) == 2
        assert batch.total_available is None  # Overlapping lists have no additive total.

    asyncio.run(scenario())


def test_serpapi_multi_cluster_keeps_successful_cluster_when_an_alias_fails() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            if query["cites"] == ["222"]:
                return httpx.Response(200, json={"error": "cluster unavailable"})
            return httpx.Response(
                200,
                json={
                    "search_information": {"total_results": 1},
                    "organic_results": [scholar_item(1, cites_id="999")],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        )
        provider = SerpApiGoogleScholarProvider(api_key="secret", client=client)
        batch = await provider.citations("google_scholar:111,222", limit=10)
        await client.aclose()

        assert len(batch.papers) == 1
        assert batch.status == RunStatus.PARTIAL
        assert batch.truncated is True
        assert [item["status"] for item in batch.context["cluster_outcomes"]] == [
            "complete",
            "failed",
        ]

    asyncio.run(scenario())


def test_serpapi_multi_cluster_resolve_retains_aliases_and_arxiv_id() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            cluster = query.get("cluster", ["111"])[0]
            item = scholar_item(0, cites_id=cluster)
            item["link"] = "https://arxiv.org/abs/2603.25723"
            return httpx.Response(200, json={"organic_results": [item]})

        client = httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        )
        provider = SerpApiGoogleScholarProvider(api_key="secret", client=client)
        paper = await provider.resolve("google_scholar:111,222")
        await client.aclose()

        assert paper is not None
        identifiers = {(claim.scheme, claim.value) for claim in paper.identifiers}
        assert (IdentifierScheme.GOOGLE_SCHOLAR, "111") in identifiers
        assert (IdentifierScheme.GOOGLE_SCHOLAR, "222") in identifiers
        assert (IdentifierScheme.ARXIV, "2603.25723") in identifiers

    asyncio.run(scenario())


def test_serpapi_provider_is_registered_only_with_user_key(monkeypatch) -> None:
    async def scenario() -> None:
        # An explicit empty process value wins over the developer's .env and
        # simulates a deployment where the licensed connector is disabled.
        monkeypatch.setenv("SERPAPI_API_KEY", "")
        without_key = ScholarService()
        assert "google_scholar_serpapi" not in without_key.providers
        await without_key.close()

        monkeypatch.setenv("SERPAPI_API_KEY", "authorized-fixture-key")
        with_key = ScholarService()
        assert "google_scholar_serpapi" in with_key.providers
        await with_key.close()

    asyncio.run(scenario())
