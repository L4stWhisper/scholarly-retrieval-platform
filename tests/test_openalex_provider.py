from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
from conftest import make_openalex_work

from scholarly_retrieval.fingerprint import stable_fingerprint
from scholarly_retrieval.models import (
    IdentifierScheme,
    ProviderBatch,
    RelatedQuery,
    SearchQuery,
    SearchSort,
)
from scholarly_retrieval.providers.openalex import OpenAlexProvider
from scholarly_retrieval.reliability import RetryPolicy
from scholarly_retrieval.storage import SQLiteStore


def test_search_maps_work_and_reports_truncation() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query["search"] == ["graph retrieval"]
            assert query["per_page"] == ["1"]
            return httpx.Response(
                200,
                json={"meta": {"count": 12}, "results": [make_openalex_work("W1")]},
            )

        client = httpx.AsyncClient(
            base_url="https://api.openalex.org", transport=httpx.MockTransport(handler)
        )
        provider = OpenAlexProvider(client=client)
        batch = await provider.search(SearchQuery(text="graph retrieval", limit=1))
        await client.aclose()

        assert batch.truncated is True
        assert batch.total_available == 12
        assert batch.papers[0].abstract == "A test abstract"
        assert batch.papers[0].source_records[0].provider == "openalex"
        assert any(
            claim.scheme == IdentifierScheme.DOI and claim.value == "10.1234/example"
            for claim in batch.papers[0].identifiers
        )

    asyncio.run(scenario())


def test_search_pushes_title_filter_to_openalex_and_sanitizes_delimiters() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query["search"] == ["contextual representations"]
            assert query["filter"] == [
                "from_publication_date:2017-01-01,"
                "to_publication_date:2019-12-31,"
                "title.search:Deep Contextualized Word Representations Transformers,"
                "cited_by_count:>4,type:article"
            ]
            assert query["sort"] == ["cited_by_count:desc"]
            return httpx.Response(
                200,
                json={"meta": {"count": 1}, "results": [make_openalex_work("W1")]},
            )

        client = httpx.AsyncClient(
            base_url="https://api.openalex.org", transport=httpx.MockTransport(handler)
        )
        provider = OpenAlexProvider(client=client)
        batch = await provider.search(
            SearchQuery(
                text="contextual representations",
                title="Deep Contextualized Word Representations, Transformers|",
                year_from=2017,
                year_to=2019,
                min_citations=5,
                work_types=["article"],
                sort=SearchSort.CITATIONS,
                limit=1,
            )
        )
        await client.aclose()

        assert batch.filter_execution["title"] == "provider"
        assert batch.filter_execution["min_citations"] == "provider"
        assert batch.filter_execution["work_types"] == "provider"
        assert batch.filter_execution["sort"] == "provider"

    asyncio.run(scenario())


def test_semantic_related_uses_official_parameter_and_preserves_score() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query["search.semantic"] == ["graph based literature discovery"]
            assert query["filter"] == [
                "from_publication_date:2020-01-01,open_access.is_oa:true,type:article"
            ]
            work = make_openalex_work("W9", title="Semantic result")
            work["relevance_score"] = 0.87
            return httpx.Response(
                200,
                json={"meta": {"count": 1}, "results": [work]},
            )

        client = httpx.AsyncClient(
            base_url="https://api.openalex.org", transport=httpx.MockTransport(handler)
        )
        provider = OpenAlexProvider(client=client)
        batch = await provider.related(
            RelatedQuery(
                text="graph based literature discovery",
                year_from=2020,
                open_access=True,
                include_work_types=["article"],
                limit=5,
            )
        )
        await client.aclose()

        source = batch.papers[0].source_records[0]
        assert source.retrieval_method == "openalex_semantic"
        assert source.provider_score == 0.87
        assert source.provider_rank == 1

    asyncio.run(scenario())


def test_references_fetches_neighbor_records() -> None:
    async def scenario() -> None:
        seed = make_openalex_work(
            "W1", references=["https://openalex.org/W2", "https://openalex.org/W3"]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/works/W1"):
                return httpx.Response(200, json=seed)
            query = parse_qs(request.url.query.decode())
            assert query["filter"] == ["openalex:W2|W3"]
            return httpx.Response(
                200,
                json={
                    "meta": {"count": 2},
                    "results": [
                        make_openalex_work("W2", title="Reference 1"),
                        make_openalex_work("W3", title="Reference 2"),
                    ],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://api.openalex.org", transport=httpx.MockTransport(handler)
        )
        provider = OpenAlexProvider(client=client)
        batch = await provider.references("W1", limit=10)
        await client.aclose()

        assert [paper.record_id for paper in batch.papers] == ["openalex:W2", "openalex:W3"]
        assert batch.truncated is False

    asyncio.run(scenario())


def test_references_preserves_ids_missing_from_batched_works_response() -> None:
    async def scenario() -> None:
        seed = make_openalex_work(
            "W1", references=["https://openalex.org/W2", "https://openalex.org/W404"]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/works/W1"):
                return httpx.Response(200, json=seed)
            return httpx.Response(
                200,
                json={"meta": {"count": 1}, "results": [make_openalex_work("W2")]},
            )

        client = httpx.AsyncClient(
            base_url="https://api.openalex.org", transport=httpx.MockTransport(handler)
        )
        provider = OpenAlexProvider(client=client)
        batch = await provider.references("W1", limit=10)
        await client.aclose()

        assert [paper.record_id for paper in batch.papers] == ["openalex:W2"]
        assert batch.total_available == 2
        assert batch.truncated is False
        assert len(batch.unresolved_references) == 1
        missing = batch.unresolved_references[0]
        assert missing.ordinal == 2
        assert missing.raw == {"openalex_id": "W404"}
        assert missing.reason == "referenced_openalex_work_not_returned"

    asyncio.run(scenario())


def test_search_keeps_unknown_work_type_for_safe_local_filtering() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert "filter" not in query
            return httpx.Response(
                200,
                json={"meta": {"count": 1}, "results": [make_openalex_work("W1")]},
            )

        client = httpx.AsyncClient(
            base_url="https://api.openalex.org", transport=httpx.MockTransport(handler)
        )
        provider = OpenAlexProvider(client=client)
        query = SearchQuery(text="graph retrieval", work_types=["custom-synthesis"])
        batch = await provider.search(query)
        await client.aclose()

        assert "work_types" not in batch.filter_execution
        assert "work_types" not in provider.plan_search_filter_execution(query)

    asyncio.run(scenario())


def test_citations_uses_incoming_edge_filter() -> None:
    async def scenario() -> None:
        seed = make_openalex_work("W1")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/works/W1"):
                return httpx.Response(200, json=seed)
            query = parse_qs(request.url.query.decode())
            assert query["filter"] == ["cites:W1"]
            assert query["cursor"] == ["*"]
            return httpx.Response(
                200,
                json={
                    "meta": {"count": 2, "next_cursor": None},
                    "results": [
                        make_openalex_work("W9", title="Citing 1"),
                        make_openalex_work("W10", title="Citing 2"),
                    ],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://api.openalex.org", transport=httpx.MockTransport(handler)
        )
        provider = OpenAlexProvider(client=client)
        batch = await provider.citations("W1", limit=10)
        await client.aclose()

        assert {paper.record_id for paper in batch.papers} == {"openalex:W9", "openalex:W10"}
        assert batch.total_available == 2

    asyncio.run(scenario())


def test_citations_follows_opaque_cursor_until_requested_limit() -> None:
    async def scenario() -> None:
        seed = make_openalex_work("W1")
        seen_cursors: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/works/W1"):
                return httpx.Response(200, json=seed)
            query = parse_qs(request.url.query.decode())
            cursor = query["cursor"][0]
            seen_cursors.append(cursor)
            if cursor == "*":
                return httpx.Response(
                    200,
                    json={
                        "meta": {"count": 2, "next_cursor": "opaque-page-2"},
                        "results": [make_openalex_work("W9", title="First page")],
                    },
                )
            assert cursor == "opaque-page-2"
            assert query["per_page"] == ["1"]
            return httpx.Response(
                200,
                json={
                    "meta": {"count": 2, "next_cursor": None},
                    "results": [make_openalex_work("W10", title="Second page")],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://api.openalex.org", transport=httpx.MockTransport(handler)
        )
        provider = OpenAlexProvider(client=client)
        batch = await provider.citations("W1", limit=2)
        await client.aclose()

        assert seen_cursors == ["*", "opaque-page-2"]
        assert [paper.record_id for paper in batch.papers] == [
            "openalex:W9",
            "openalex:W10",
        ]
        assert batch.next_cursor is None
        assert batch.truncated is False

    asyncio.run(scenario())


def test_citations_filters_upstream_self_relation_and_fills_limit() -> None:
    async def scenario() -> None:
        seed = make_openalex_work("W1")
        seen_cursors: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/works/W1"):
                return httpx.Response(200, json=seed)
            query = parse_qs(request.url.query.decode())
            cursor = query["cursor"][0]
            seen_cursors.append(cursor)
            if cursor == "*":
                return httpx.Response(
                    200,
                    json={
                        "meta": {"count": 3, "next_cursor": "after-self"},
                        "results": [
                            make_openalex_work("W1", title="Upstream self relation"),
                            make_openalex_work("W9", title="Real citing paper"),
                        ],
                    },
                )
            assert query["per_page"] == ["1"]
            return httpx.Response(
                200,
                json={
                    "meta": {"count": 3, "next_cursor": None},
                    "results": [make_openalex_work("W10", title="Replacement paper")],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://api.openalex.org", transport=httpx.MockTransport(handler)
        )
        provider = OpenAlexProvider(client=client)
        batch = await provider.citations("W1", limit=2)
        await client.aclose()

        assert seen_cursors == ["*", "after-self"]
        assert [paper.record_id for paper in batch.papers] == ["openalex:W9", "openalex:W10"]

    asyncio.run(scenario())


def test_citations_exposes_resume_cursor_when_limit_truncates_page_walk() -> None:
    async def scenario() -> None:
        seed = make_openalex_work("W1")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/works/W1"):
                return httpx.Response(200, json=seed)
            return httpx.Response(
                200,
                json={
                    "meta": {"count": 3, "next_cursor": "resume-here"},
                    "results": [make_openalex_work("W9")],
                },
            )

        client = httpx.AsyncClient(
            base_url="https://api.openalex.org", transport=httpx.MockTransport(handler)
        )
        provider = OpenAlexProvider(client=client)
        batch = await provider.citations("W1", limit=1)
        await client.aclose()

        assert batch.truncated is True
        assert batch.next_cursor == "resume-here"

    asyncio.run(scenario())


def test_citations_resume_from_last_complete_page_after_transport_failure() -> None:
    async def scenario() -> None:
        seed = make_openalex_work("W1")
        seen_cursors: list[str] = []
        store = SQLiteStore(":memory:")

        def failing_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/works/W1"):
                return httpx.Response(200, json=seed)
            cursor = parse_qs(request.url.query.decode())["cursor"][0]
            seen_cursors.append(cursor)
            if cursor == "*":
                return httpx.Response(
                    200,
                    json={
                        "meta": {"count": 2, "next_cursor": "page-2"},
                        "results": [make_openalex_work("W9", title="First page")],
                    },
                )
            raise httpx.ConnectError("injected page-two failure", request=request)

        first_client = httpx.AsyncClient(
            base_url="https://api.openalex.org",
            transport=httpx.MockTransport(failing_handler),
        )
        first_provider = OpenAlexProvider(client=first_client, store=store)
        first_provider._http.policy = RetryPolicy(max_attempts=1)
        try:
            await first_provider.citations("W1", limit=2)
        except httpx.ConnectError:
            pass
        else:
            raise AssertionError("the injected second-page failure must escape the provider")
        assert store.stats()["cursor_checkpoints"] == 1
        await first_client.aclose()

        def recovery_handler(request: httpx.Request) -> httpx.Response:
            cursor = parse_qs(request.url.query.decode())["cursor"][0]
            seen_cursors.append(cursor)
            assert cursor == "page-2"
            return httpx.Response(
                200,
                json={
                    "meta": {"count": 2, "next_cursor": None},
                    "results": [make_openalex_work("W10", title="Second page")],
                },
            )

        recovery_client = httpx.AsyncClient(
            base_url="https://api.openalex.org",
            transport=httpx.MockTransport(recovery_handler),
        )
        recovery_provider = OpenAlexProvider(client=recovery_client, store=store)
        recovery_provider._http.policy = RetryPolicy(max_attempts=1)
        recovered = await recovery_provider.citations("W1", limit=2)

        assert seen_cursors == ["*", "page-2", "page-2"]
        assert [paper.record_id for paper in recovered.papers] == [
            "openalex:W9",
            "openalex:W10",
        ]
        expected = ProviderBatch(
            papers=[
                recovery_provider._paper_from_work(make_openalex_work("W9", title="First page")),
                recovery_provider._paper_from_work(make_openalex_work("W10", title="Second page")),
            ],
            total_available=2,
        )
        assert stable_fingerprint(recovered) == stable_fingerprint(expected)
        assert store.stats()["cursor_checkpoints"] == 0
        await recovery_client.aclose()
        store.close()

    asyncio.run(scenario())
