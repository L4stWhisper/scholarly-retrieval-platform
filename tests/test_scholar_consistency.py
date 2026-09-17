"""Contract tests for fresh Scholar rounds, identity discovery and evidence."""

import asyncio

import httpx
import pytest

from scholarly_retrieval.providers.serpapi_google_scholar import SerpApiGoogleScholarProvider
from scholarly_retrieval.storage import SQLiteStore


def item(result="one", *, cluster=None, cites=None):
    return {
        "result_id": result,
        "title": "A Verified Seed",
        "link": "https://arxiv.org/abs/2501.12345",
        "publication_info": {"authors": [{"name": "Ada Researcher"}]},
        "inline_links": {
            "versions": {"cluster_id": cluster} if cluster else {},
            "cited_by": {"cites_id": cites} if cites else {},
        },
    }


def test_clusters_are_not_cites_and_discovery_follows_versions():
    async def scenario():
        seen = []

        def handler(request):
            p = request.url.params
            seen.append(dict(p))
            if p.get("cluster") == "200":
                return httpx.Response(
                    200,
                    json={
                        "organic_results": [
                            item("second", cluster="300", cites="902"),
                            {
                                **item("unrelated", cluster="400", cites="999"),
                                "link": "https://arxiv.org/abs/2501.99999",
                            },
                        ]
                    },
                )
            if p.get("cluster") == "300":
                return httpx.Response(200, json={"organic_results": [item("third", cites="903")]})
            return httpx.Response(200, json={"organic_results": [item(cluster="200", cites="901")]})

        async with httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        ) as client:
            provider = SerpApiGoogleScholarProvider(api_key="test", client=client)
            seed = await provider.resolve("arxiv:2501.12345")
            assert provider.traversal_identifier(seed, "") == "google_scholar:901,902,903"
            assert {p["cluster"] for p in seen if "cluster" in p} == {"200", "300"}
            assert all(p["no_cache"] == "true" for p in seen)

    asyncio.run(scenario())


def test_different_cites_keep_duplicate_records_until_service():
    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://serpapi.com",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"organic_results": [item()]})
            ),
        ) as client:
            batch = await SerpApiGoogleScholarProvider(api_key="test", client=client).citations(
                "google_scholar:111,222", limit=10
            )
            assert len(batch.papers) == 2
            assert batch.papers[0].record_id == batch.papers[1].record_id
            assert [
                p.source_records[0].retrieval_context["seed_cites_id"] for p in batch.papers
            ] == ["111", "222"]
            assert all(
                len(p.source_records[0].retrieval_context["observations"]) == 2
                for p in batch.papers
            )

    asyncio.run(scenario())


def test_explicit_unverified_cites_hint_is_retained_without_identity_claim():
    async def scenario():
        def handler(request):
            p = request.url.params
            if p.get("cluster") == "222":
                return httpx.Response(200, json={"organic_results": []})
            return httpx.Response(200, json={"organic_results": [item(cluster="111", cites="901")]})

        async with httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        ) as client:
            provider = SerpApiGoogleScholarProvider(api_key="test", client=client)
            seed = await provider.resolve("google_scholar:111,222")
            assert provider.traversal_identifier(seed, "") == "google_scholar:901,222"
            assert "222" not in {c.value for c in seed.identifiers}
            hint = next(
                r for r in seed.source_records if r.retrieval_context.get("cites_id") == "222"
            )
            assert hint.retrieval_context["seed_alias_evidence"] == "caller_supplied_not_verified"

    asyncio.run(scenario())


def test_versions_pagination_finds_additional_cites_id():
    async def scenario():
        def handler(request):
            p = request.url.params
            payload = {"organic_results": [item(cluster="111", cites="901")]}
            if p.get("cluster") == "111":
                if p.get("start") == "20":
                    payload = {"organic_results": [item("second", cites="902")]}
                else:
                    payload["serpapi_pagination"] = {
                        "next": "https://serpapi.com/search.json?cluster=111&start=20"
                    }
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        ) as client:
            provider = SerpApiGoogleScholarProvider(api_key="test", client=client)
            seed = await provider.resolve("arxiv:2501.12345")
            assert provider.traversal_identifier(seed, "") == "google_scholar:901,902"

    asyncio.run(scenario())


def test_no_cache_bypasses_sqlite_and_serpapi(tmp_path):
    async def scenario():
        calls = []

        def handler(request):
            calls.append(request.url.params.get("no_cache"))
            return httpx.Response(200, json={"organic_results": [item()]})

        store = SQLiteStore(tmp_path / "cache.sqlite")
        async with httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        ) as client:
            provider = SerpApiGoogleScholarProvider(api_key="test", client=client, store=store)
            for _ in range(2):
                await provider.citations("google_scholar:111")
            assert calls == ["true"] * 4
        store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "next_link",
    [
        "https://evil.example/search.json?start=10",
        "https://serpapi.com/search.json?start=0",
        "https://serpapi.com/search.json?start=10&cites=222",
        "https://serpapi.com/search.json?start=10&engine=google",
    ],
)
def test_invalid_next_preserves_data_and_marks_partial(next_link):
    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://serpapi.com",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "organic_results": [item()],
                        "serpapi_pagination": {"next": next_link},
                    },
                )
            ),
        ) as client:
            batch = await SerpApiGoogleScholarProvider(api_key="secret", client=client).citations(
                "google_scholar:111"
            )
            assert len(batch.papers) == 1 and batch.truncated
            assert batch.context["cluster_outcomes"][0]["stop_reason"] == "invalid_pagination"

    asyncio.run(scenario())


def test_same_count_different_ids_is_not_stability():
    async def scenario():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"organic_results": [item(str(calls))]})

        async with httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        ) as client:
            batch = await SerpApiGoogleScholarProvider(api_key="test", client=client).citations(
                "google_scholar:111"
            )
            assert calls == 4
            assert len(batch.papers) == 4 and batch.truncated
            assert batch.context["cluster_outcomes"][0]["stop_reason"] == "round_budget"

    asyncio.run(scenario())


def test_stable_subset_of_advertised_total_is_partial():
    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://serpapi.com",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "organic_results": [item()],
                        "search_information": {"total_results": 46},
                        "search_metadata": {"id": "fixture-archive-id"},
                    },
                )
            ),
        ) as client:
            batch = await SerpApiGoogleScholarProvider(api_key="test", client=client).citations(
                "google_scholar:111"
            )
            assert len(batch.papers) == 1 and batch.truncated
            assert len(batch.context["cluster_outcomes"][0]["rounds"]) == 4
            assert (
                batch.context["cluster_outcomes"][0]["rounds"][0]["pages"][0]["search_id"]
                == "fixture-archive-id"
            )

    asyncio.run(scenario())


def test_next_offset_is_followed_even_when_first_total_is_one():
    async def scenario():
        offsets = []

        def handler(request):
            p = request.url.params
            offsets.append(p["start"])
            assert p["api_key"] == "test"
            assert p["no_cache"] == "true"
            payload = {
                "organic_results": [item(p["start"])],
                "search_information": {"total_results": 1},
            }
            if p["start"] == "0":
                payload["serpapi_pagination"] = {
                    "next": "https://serpapi.com/search.json?start=17&cites=111&api_key=bad&no_cache=false"
                }
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        ) as client:
            batch = await SerpApiGoogleScholarProvider(api_key="test", client=client).citations(
                "google_scholar:111"
            )
            assert offsets == ["0", "17", "0", "17"]
            assert len(batch.papers) == 2 and not batch.truncated

    asyncio.run(scenario())
