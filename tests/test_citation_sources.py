"""Provider contracts and generic cross-source seed recovery (no live keys)."""

import asyncio

import httpx
import pytest

from scholarly_retrieval.models import (
    Author,
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
    ProviderBatch,
    SearchQuery,
    SourceRecord,
)
from scholarly_retrieval.providers.ads import ADSProvider
from scholarly_retrieval.providers.base import ProviderCapabilities, ScholarlyProvider
from scholarly_retrieval.providers.serpapi_google_scholar import SerpApiGoogleScholarProvider
from scholarly_retrieval.service import ScholarService


def ads_doc(index):
    return {
        "bibcode": f"2025Fixture{index}",
        "title": [f"Citing paper {index}"],
        "author": ["Researcher, Ada"],
        "year": "2025",
        "doi": [f"10.1234/citing{index}"],
        "identifier": ["arXiv:2501.12345"],
    }


def test_ads_pagination_identifiers_and_direction():
    async def scenario():
        requests = []

        def handler(request):
            assert request.headers["Authorization"] == "Bearer fixture"
            params = request.url.params
            requests.append(dict(params))
            start = int(params["start"])
            docs = [ads_doc(i) for i in range(start, min(start + 2, 3))]
            return httpx.Response(200, json={"response": {"numFound": 3, "docs": docs}})

        async with httpx.AsyncClient(
            base_url="https://api.adsabs.harvard.edu", transport=httpx.MockTransport(handler)
        ) as client:
            provider = ADSProvider(api_token="fixture", client=client)
            batch = await provider.citations("arxiv:2501.12345", limit=3)
            assert len(batch.papers) == 3
            assert not batch.truncated
            assert [q["start"] for q in requests] == ["0", "2"]
            assert requests[0]["q"] == 'citations(identifier:"arXiv:2501.12345")'
            assert batch.papers[0].source_records[0].source_url.endswith("/abstract")
            assert any(c.scheme == IdentifierScheme.DOI for c in batch.papers[0].identifiers)
            refs = await provider.references("ads:2025Fixture0", limit=1)
            assert refs.truncated and refs.next_cursor == "2"
            assert requests[-1]["q"] == 'references(bibcode:"2025Fixture0")'

    asyncio.run(scenario())


def test_ads_token_is_required(monkeypatch):
    monkeypatch.delenv("ADS_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="ADS_API_TOKEN"):
        ADSProvider()


@pytest.mark.parametrize(
    "identifier",
    [
        "arxiv:2501.12345v2",
        "https://arxiv.org/abs/2501.12345",
        "10.48550/arxiv.2501.12345",
    ],
)
def test_ads_arxiv_identifier_forms(identifier):
    assert ADSProvider._identifier_query(identifier) == 'identifier:"arXiv:2501.12345"'


def test_ads_auth_failure_is_not_empty():
    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://api.adsabs.harvard.edu",
            transport=httpx.MockTransport(lambda req: httpx.Response(401)),
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await ADSProvider(api_token="bad", client=client).resolve("10.1234/seed")

    asyncio.run(scenario())


class SeedProvider(ScholarlyProvider):
    name = "fixture_seed"
    capabilities = ProviderCapabilities(resolve_id=True, citations="list")

    async def resolve(self, identifier):
        return Paper(
            record_id="fixture_seed:1",
            title="A Generic Seed Paper",
            publication_year=2025,
            authors=[Author(name="Researcher, Ada")],
            identifiers=[
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value="10.1234/seed",
                    provenance=Provenance(provider=self.name, source_record_id="1"),
                )
            ],
        )

    async def citations(self, identifier, *, limit=100):
        return ProviderBatch(
            papers=[
                Paper(
                    record_id="fixture_seed:citing",
                    title="A Shared Citing Paper",
                    identifiers=[
                        IdentifierClaim(
                            scheme=IdentifierScheme.DOI,
                            value="10.1234/citing",
                            provenance=Provenance(provider=self.name, source_record_id="citing"),
                        )
                    ],
                    source_records=[SourceRecord(provider=self.name, source_record_id="citing")],
                )
            ]
        )

    async def references(self, identifier, *, limit=100):
        return ProviderBatch()

    async def search(self, query: SearchQuery):
        return ProviderBatch()


def test_title_fallback_verifies_seed_unions_clusters_and_deduplicates():
    async def scenario():
        traversed = []

        def candidate(cluster, *, author="Ada Researcher", link="https://example.org/seed"):
            return {
                "title": "A Generic Seed Paper",
                "result_id": cluster,
                "link": link,
                "publication_info": {
                    "summary": f"{author} - Venue, 2025",
                    "authors": [{"name": author}],
                },
                "inline_links": {"cited_by": {"cites_id": cluster}},
            }

        def handler(request):
            p = request.url.params
            if "cites" in p:
                traversed.append(p["cites"])
                return httpx.Response(
                    200,
                    json={
                        "organic_results": [
                            {
                                "title": "A Shared Citing Paper",
                                "result_id": "shared",
                                "link": "https://doi.org/10.1234/citing",
                            }
                        ]
                    },
                )
            if p.get("q") == '"A Generic Seed Paper"':
                return httpx.Response(
                    200,
                    json={
                        "organic_results": [
                            candidate("111"),
                            candidate("222"),
                            candidate("333", author="Different Person"),
                            candidate("444", link="https://doi.org/10.1234/conflict"),
                        ]
                    },
                )
            return httpx.Response(
                200, json={"error": "Google hasn't returned any results for this query."}
            )

        async with httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        ) as client:
            google = SerpApiGoogleScholarProvider(api_key="fixture", client=client)
            service = ScholarService(providers=[SeedProvider(), google])
            try:
                result = await service.citations("W123456789", limit=100)
                assert list(dict.fromkeys(traversed)) == ["111", "222"]
                assert len(result.papers) == 1
                assert {s.provider for s in result.papers[0].source_records} == {
                    "fixture_seed",
                    "google_scholar_serpapi",
                }
                assert result.raw_record_count == 3
                assert {
                    s.retrieval_context.get("seed_cites_id")
                    for s in result.papers[0].source_records
                    if s.provider == "google_scholar_serpapi"
                } == {"111", "222"}
            finally:
                await service.close()

    asyncio.run(scenario())


def test_serpapi_quota_error_remains_failure():
    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://serpapi.com",
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"error": "Quota exhausted"})
            ),
        ) as client:
            provider = SerpApiGoogleScholarProvider(api_key="fixture", client=client)
            with pytest.raises(ValueError, match="API error"):
                await provider.resolve("10.1234/seed")

    asyncio.run(scenario())


def test_scholar_fresh_rounds_follow_next_and_recover_false_last_page():
    async def scenario():
        offsets = []
        round_number = 0

        def handler(request):
            nonlocal round_number
            p = request.url.params
            assert p["filter"] == "0"
            assert p["no_cache"] == "true"
            start = int(p["start"])
            if start == 0:
                round_number += 1
            offsets.append(start)
            ids = {0: [1, 2], 10: [2, 3], 20: [4]}.get(start, [])
            next_page = start + 10 if round_number > 1 and start < 20 else None
            return httpx.Response(
                200,
                json={
                    "search_information": {"total_results": 2 if start == 0 else 4},
                    "organic_results": [{"result_id": str(i), "title": f"Citing {i}"} for i in ids],
                    "serpapi_pagination": {
                        "next": f"https://serpapi.com/search.json?cites=111&start={next_page}"
                    }
                    if next_page is not None
                    else {},
                },
            )

        async with httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        ) as client:
            provider = SerpApiGoogleScholarProvider(api_key="fixture", client=client)
            batch = await provider.citations("google_scholar:111", limit=100)
            # Round 3 sees page 0 advertise 2 after page 10 advertised 4, so the
            # stale page is re-requested up to the per-round budget before
            # moving on; the round still completes and stability is reached.
            assert offsets == [0, 0, 10, 20, 0, 0, 0, 0, 10, 20]
            assert len(batch.papers) == 4
            assert batch.total_available == 4
            assert not batch.truncated
            rounds = batch.context["cluster_outcomes"][0]["rounds"]
            assert [r["stale_retries"] for r in rounds] == [0, 0, 3]
            assert rounds[2]["pages"][0]["stale"] and rounds[2]["pages"][0]["stale_retries"] == 3
            assert not rounds[2]["consistent"]

    asyncio.run(scenario())


def test_scholar_later_page_failure_preserves_collected_papers():
    async def scenario():
        def handler(request):
            if request.url.params["start"] != "0":
                return httpx.Response(200, json={"error": "Quota exhausted"})
            return httpx.Response(
                200,
                json={
                    "organic_results": [{"result_id": "one", "title": "Citing work"}],
                    "serpapi_pagination": {
                        "next": "https://serpapi.com/search.json?cites=111&start=10"
                    },
                },
            )

        async with httpx.AsyncClient(
            base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
        ) as client:
            provider = SerpApiGoogleScholarProvider(api_key="fixture", client=client)
            batch = await provider.citations("google_scholar:111", limit=100)
            assert len(batch.papers) == 1
            assert batch.truncated and batch.next_cursor == "10"

    asyncio.run(scenario())
