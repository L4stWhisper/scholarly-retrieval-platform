from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs

import httpx

from scholarly_retrieval.models import IdentifierScheme, RelatedQuery, SearchQuery
from scholarly_retrieval.providers.semantic_scholar import SemanticScholarProvider
from scholarly_retrieval.reliability import RetryPolicy
from scholarly_retrieval.storage import SQLiteStore


def s2_paper(paper_id: str, *, title: str = "S2 Paper", doi: str = "10.1234/example"):
    return {
        "paperId": paper_id,
        "corpusId": 123,
        "externalIds": {"DOI": doi, "ArXiv": "2401.12345"},
        "url": f"https://www.semanticscholar.org/paper/{paper_id}",
        "title": title,
        "abstract": "Abstract",
        "venue": "TestConf",
        "publicationVenue": {"name": "Test Conference"},
        "year": 2024,
        "publicationDate": "2024-01-02",
        "publicationTypes": ["Conference"],
        "authors": [{"authorId": "A1", "name": "Ada Researcher"}],
        "citationCount": 7,
        "referenceCount": 4,
        "isOpenAccess": True,
        "openAccessPdf": {"url": "https://example.org/paper.pdf"},
        "fieldsOfStudy": ["Computer Science"],
    }


def test_s2_search_maps_ids_and_discloses_local_author_filter() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/graph/v1/paper/search"
            query = parse_qs(request.url.query.decode())
            assert query["query"] == ["graph retrieval"]
            assert query["year"] == ["2024-2025"]
            return httpx.Response(200, json={"total": 1, "data": [s2_paper("S1")]})

        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(handler),
        )
        provider = SemanticScholarProvider(client=client, api_key="secret")
        batch = await provider.search(
            SearchQuery(
                text="graph retrieval",
                year_from=2024,
                year_to=2025,
                author="Ada",
            )
        )
        await client.aclose()

        assert batch.filter_execution["author"] == "local"
        assert batch.papers[0].record_id == "semantic_scholar:S1"
        assert {(claim.scheme, claim.value) for claim in batch.papers[0].identifiers} >= {
            (IdentifierScheme.DOI, "10.1234/example"),
            (IdentifierScheme.ARXIV, "2401.12345"),
        }

    asyncio.run(scenario())


def test_s2_related_uses_native_multi_positive_and_negative_feedback() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/recommendations/v1/papers"
            assert json.loads(request.content) == {
                "positivePaperIds": ["S1", "S2"],
                "negativePaperIds": ["S3"],
            }
            return httpx.Response(
                200,
                json={"recommendedPapers": [s2_paper("R1", doi="10.1234/r1")]},
            )

        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(handler),
        )
        provider = SemanticScholarProvider(client=client)
        batch = await provider.related(
            RelatedQuery(
                positive_identifiers=["S1", "S2"],
                negative_identifiers=["S3"],
                limit=5,
            )
        )
        await client.aclose()

        assert len(batch.papers) == 1
        assert batch.filter_execution["negative_identifiers"] == "provider"
        assert batch.papers[0].source_records[0].retrieval_context == {
            "positive_identifiers": ["S1", "S2"],
            "negative_identifiers": ["S3"],
            "recommendation_mode": "multi_example",
        }
        assert all(
            paper.source_records[0].retrieval_method == "semantic_scholar_recommendation"
            for paper in batch.papers
        )

    asyncio.run(scenario())


def test_s2_related_accepts_string_publication_venue_seen_in_live_response() -> None:
    async def scenario() -> None:
        recommendation = s2_paper("R1", doi="10.1234/r1")
        recommendation["publicationVenue"] = "Recommendation Venue"

        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"recommendedPapers": [recommendation]},
                )
            ),
        )
        provider = SemanticScholarProvider(client=client)
        batch = await provider.related(RelatedQuery(positive_identifiers=["S1"], limit=1))
        await client.aclose()

        assert batch.papers[0].venue == "Recommendation Venue"

    asyncio.run(scenario())


def test_s2_reference_and_citation_payload_keys() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query["offset"] == ["0"]
            if request.url.path.endswith("/references"):
                data = [{"citedPaper": s2_paper("R1", title="Reference")}]
            else:
                data = [{"citingPaper": s2_paper("C1", title="Citing")}]
            return httpx.Response(200, json={"offset": 0, "next": None, "data": data})

        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(handler),
        )
        provider = SemanticScholarProvider(client=client)
        references = await provider.references("DOI:10.1234/example", limit=1)
        citations = await provider.citations("10.1234/example", limit=1)
        await client.aclose()

        assert references.papers[0].record_id == "semantic_scholar:R1"
        assert citations.papers[0].record_id == "semantic_scholar:C1"

    asyncio.run(scenario())


def test_s2_relations_follow_next_offset_without_duplicates() -> None:
    async def scenario() -> None:
        seen_offsets: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            offset = int(query["offset"][0])
            seen_offsets.append(offset)
            if offset == 0:
                return httpx.Response(
                    200,
                    json={
                        "total": 2,
                        "next": 1,
                        "data": [{"citedPaper": s2_paper("R1", title="Page one")}],
                    },
                )
            assert query["limit"] == ["1"]
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "next": None,
                    "data": [{"citedPaper": s2_paper("R2", title="Page two")}],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(handler),
        )
        provider = SemanticScholarProvider(client=client)
        # Disable production pacing in this fixture; pacing has its own unit tests.
        provider._http.policy = provider._http.policy.__class__(
            max_attempts=1, min_interval_seconds=0
        )
        batch = await provider.references("10.1234/example", limit=2)
        await client.aclose()

        assert seen_offsets == [0, 1]
        assert [paper.record_id for paper in batch.papers] == [
            "semantic_scholar:R1",
            "semantic_scholar:R2",
        ]
        assert batch.next_cursor is None
        assert batch.truncated is False

    asyncio.run(scenario())


def test_s2_resolve_maps_not_found_to_empty_not_failure() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        )
        provider = SemanticScholarProvider(client=client, api_key="secret")
        paper = await provider.resolve("10.1234/missing")
        await client.aclose()

        assert paper is None

    asyncio.run(scenario())


def _no_sleep(provider: SemanticScholarProvider) -> None:
    async def instant(_seconds: float) -> None:
        return None

    provider._http._sleep = instant


def test_anonymous_search_uses_bulk_endpoint_sorted_by_citations() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/graph/v1/paper/search/bulk"
            query = parse_qs(request.url.query.decode(), keep_blank_values=True)
            assert query["sort"] == ["citationCount:desc"]
            assert query["openAccessPdf"] == [""]
            assert "limit" not in query
            return httpx.Response(
                200,
                json={
                    "total": 3,
                    "token": "next-token",
                    "data": [s2_paper(f"S{i}") for i in range(3)],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(handler),
        )
        provider = SemanticScholarProvider(client=client)
        batch = await provider.search(
            SearchQuery(text="graph retrieval", open_access=True, limit=2)
        )
        await client.aclose()

        assert [paper.record_id for paper in batch.papers] == [
            "semantic_scholar:S0",
            "semantic_scholar:S1",
        ]
        assert batch.context == {"endpoint": "paper/search/bulk", "ranking": "citation_count_desc"}
        assert (
            batch.papers[0].source_records[0].retrieval_context["endpoint"] == "paper/search/bulk"
        )
        assert batch.truncated and batch.next_cursor == "next-token"
        assert batch.filter_execution["open_access"] == "provider"

    asyncio.run(scenario())


def test_anonymous_search_filters_closed_access_locally_on_bulk() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "openAccessPdf" not in parse_qs(request.url.query.decode())
            closed = {**s2_paper("C1"), "isOpenAccess": False, "openAccessPdf": None}
            return httpx.Response(200, json={"total": 2, "data": [s2_paper("O1"), closed]})

        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(handler),
        )
        provider = SemanticScholarProvider(client=client)
        batch = await provider.search(SearchQuery(text="graph retrieval", open_access=False))
        await client.aclose()

        assert [paper.record_id for paper in batch.papers] == ["semantic_scholar:C1"]
        assert batch.filter_execution["open_access"] == "local"

    asyncio.run(scenario())


def test_keyed_search_falls_back_to_bulk_after_429() -> None:
    async def scenario() -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/paper/search"):
                return httpx.Response(429, json={"message": "Too Many Requests"})
            return httpx.Response(200, json={"total": 1, "data": [s2_paper("B1")]})

        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(handler),
        )
        provider = SemanticScholarProvider(client=client, api_key="secret")
        _no_sleep(provider)
        batch = await provider.search(SearchQuery(text="graph retrieval"))
        await client.aclose()

        assert paths.count("/graph/v1/paper/search") == 5
        assert paths[-1] == "/graph/v1/paper/search/bulk"
        assert batch.papers[0].record_id == "semantic_scholar:B1"
        assert batch.context["fallback_reason"] == "http_429"

    asyncio.run(scenario())


def test_anonymous_resolve_uses_batch_lookup_and_maps_null_to_none() -> None:
    async def scenario() -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/graph/v1/paper/batch"
            body = json.loads(request.content)
            bodies.append(body)
            if body["ids"] == ["DOI:10.1234/missing"]:
                return httpx.Response(200, json=[None])
            return httpx.Response(200, json=[s2_paper("R1")])

        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(handler),
        )
        provider = SemanticScholarProvider(client=client)
        found = await provider.resolve("arXiv:2401.12345")
        missing = await provider.resolve("10.1234/missing")
        await client.aclose()

        assert bodies[0] == {"ids": ["ARXIV:2401.12345"]}
        assert found is not None and found.record_id == "semantic_scholar:R1"
        assert found.source_records[0].retrieval_context["endpoint"] == "paper/batch"
        assert missing is None

    asyncio.run(scenario())


def test_keyed_resolve_falls_back_to_batch_after_429() -> None:
    async def scenario() -> None:
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            if request.method == "GET":
                return httpx.Response(429, json={"message": "Too Many Requests"})
            return httpx.Response(200, json=[s2_paper("F1")])

        client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(handler),
        )
        provider = SemanticScholarProvider(client=client, api_key="secret")
        _no_sleep(provider)
        paper = await provider.resolve("arXiv:2401.12345")
        await client.aclose()

        assert methods == ["GET"] * 5 + ["POST"]
        assert paper is not None
        assert paper.source_records[0].retrieval_context == {
            "endpoint": "paper/batch",
            "fallback_reason": "http_429",
        }

    asyncio.run(scenario())


def test_s2_relations_resume_from_server_supplied_next_offset() -> None:
    async def scenario() -> None:
        store = SQLiteStore(":memory:")
        seen_offsets: list[int] = []

        def failing_handler(request: httpx.Request) -> httpx.Response:
            offset = int(parse_qs(request.url.query.decode())["offset"][0])
            seen_offsets.append(offset)
            if offset == 0:
                return httpx.Response(
                    200,
                    json={
                        "total": 2,
                        "next": 7,
                        "data": [{"citedPaper": s2_paper("R1", title="First page")}],
                    },
                )
            raise httpx.ConnectError("injected page-two failure", request=request)

        first_client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(failing_handler),
        )
        first_provider = SemanticScholarProvider(client=first_client, store=store)
        first_provider._http.policy = RetryPolicy(max_attempts=1, min_interval_seconds=0)
        try:
            await first_provider.references("10.1234/example", limit=2)
        except httpx.ConnectError:
            pass
        else:
            raise AssertionError("the injected second-page failure must escape the provider")
        assert store.stats()["cursor_checkpoints"] == 1
        await first_client.aclose()

        def recovery_handler(request: httpx.Request) -> httpx.Response:
            offset = int(parse_qs(request.url.query.decode())["offset"][0])
            seen_offsets.append(offset)
            assert offset == 7
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "next": None,
                    "data": [{"citedPaper": s2_paper("R2", title="Second page")}],
                },
            )

        recovery_client = httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            transport=httpx.MockTransport(recovery_handler),
        )
        recovery_provider = SemanticScholarProvider(client=recovery_client, store=store)
        recovery_provider._http.policy = RetryPolicy(max_attempts=1, min_interval_seconds=0)
        recovered = await recovery_provider.references("10.1234/example", limit=2)

        assert seen_offsets == [0, 7, 7]
        assert [paper.record_id for paper in recovered.papers] == [
            "semantic_scholar:R1",
            "semantic_scholar:R2",
        ]
        assert store.stats()["cursor_checkpoints"] == 0
        await recovery_client.aclose()
        store.close()

    asyncio.run(scenario())


def test_anonymous_semantic_scholar_explains_throttling_and_retries_less(monkeypatch) -> None:
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    anonymous = SemanticScholarProvider()
    assert anonymous.throttle_hint and "SEMANTIC_SCHOLAR_API_KEY" in anonymous.throttle_hint
    assert anonymous._http.policy.max_attempts == 3

    keyed = SemanticScholarProvider(api_key="secret")
    assert keyed.throttle_hint is None
    assert keyed._http.policy.max_attempts == 5
