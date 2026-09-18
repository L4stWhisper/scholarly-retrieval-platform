"""Contract tests for stale Google Scholar snapshot pages inside one cites list.

Google can answer one cites list from index snapshots that disagree: a page
advertises 21 results, the next advertises 47, or a page promised by a next
link comes back empty. These fixtures replay those observed shapes.
"""

import asyncio

import httpx
import pytest

from scholarly_retrieval.providers.serpapi_google_scholar import SerpApiGoogleScholarProvider


def page(ids, *, total, start, next_start=None, search_id=None):
    payload = {
        "search_information": {"total_results": total},
        "search_metadata": {"id": search_id or f"search-{start}-{total}"},
        "organic_results": [{"result_id": f"R{i}", "title": f"Citing {i}"} for i in ids],
    }
    if next_start is not None:
        payload["serpapi_pagination"] = {
            "next": f"https://serpapi.com/search.json?engine=google_scholar&cites=111&start={next_start}"
        }
    return payload


def make_provider(handler, **kwargs):
    client = httpx.AsyncClient(
        base_url="https://serpapi.com", transport=httpx.MockTransport(handler)
    )
    return client, SerpApiGoogleScholarProvider(api_key="fixture", client=client, **kwargs)


def test_stale_first_page_is_re_requested_until_it_matches_consensus():
    """Round 1 page 0 comes from a 21-result snapshot; later pages advertise 47.

    Round 2 must re-request page 0 when it again advertises 21 and accept the
    fresh 47-result page, giving two identical complete rounds of 47 IDs.
    """

    async def scenario():
        requests = []
        full = {0: range(0, 20), 20: range(20, 40), 40: range(40, 47)}
        # The stale snapshot only holds 21 works and overlaps the full one.
        stale_first = list(range(0, 10)) + list(range(30, 40))
        stale_left = {"count": 2}  # round 1 page 0, then first attempt of round 2 page 0

        def handler(request):
            p = request.url.params
            assert p["num"] == "20" and p["no_cache"] == "true"
            start = int(p["start"])
            requests.append(start)
            if start == 0 and stale_left["count"] > 0:
                stale_left["count"] -= 1
                return httpx.Response(200, json=page(stale_first, total=21, start=0, next_start=20))
            next_start = start + 20 if start + 20 < 47 else None
            return httpx.Response(
                200, json=page(full[start], total=47, start=start, next_start=next_start)
            )

        client, provider = make_provider(handler)
        async with client:
            batch = await provider.citations("google_scholar:111", limit=100)
        assert len(batch.papers) == 47
        assert batch.total_available == 47
        assert not batch.truncated
        outcome = batch.context["cluster_outcomes"][0]
        assert outcome["stop_reason"] == "stable"
        rounds = outcome["rounds"]
        # Round 1 could not know page 0 was stale (37 IDs); round 2 retried it
        # once and saw 47; round 3 repeated the same 47 IDs, which is stability.
        assert requests == [0, 20, 40, 0, 0, 20, 40, 0, 20, 40]
        assert [r["observed_count"] for r in rounds] == [37, 47, 47]
        assert [r["stale_retries"] for r in rounds] == [0, 1, 0]
        assert [r["consistent"] for r in rounds] == [False, True, True]
        assert rounds[1]["pages"][0]["stale_retries"] == 1 and not rounds[1]["pages"][0]["stale"]
        # Stale attempts are still real observations and are kept as evidence.
        r30 = next(p for p in batch.papers if p.record_id == "google_scholar:R30")
        observations = r30.source_records[0].retrieval_context["observations"]
        assert {(o["round"], o["start"]) for o in observations} == {
            (1, "0"),
            (1, "20"),
            (2, "0"),
            (2, "20"),
            (3, "20"),
        }

    asyncio.run(scenario())


def test_empty_page_promised_by_next_link_is_retried():
    async def scenario():
        requests = []
        empty_once = {"pending": True}

        def handler(request):
            start = int(request.url.params["start"])
            requests.append(start)
            if start == 0:
                return httpx.Response(
                    200, json=page(range(0, 20), total=27, start=0, next_start=20)
                )
            if empty_once["pending"]:
                empty_once["pending"] = False
                # SerpApi reports this as an error string; _query maps it to [].
                return httpx.Response(
                    200, json={"error": "Google hasn't returned any results for this query."}
                )
            return httpx.Response(200, json=page(range(20, 27), total=27, start=20))

        client, provider = make_provider(handler)
        async with client:
            batch = await provider.citations("google_scholar:111", limit=100)
        assert len(batch.papers) == 27 and not batch.truncated
        assert requests == [0, 20, 20, 0, 20]
        first = batch.context["cluster_outcomes"][0]["rounds"][0]
        assert first["pages"][1]["stale_retries"] == 1 and first["consistent"] is True

    asyncio.run(scenario())


def test_exhausted_retry_budget_keeps_data_and_stays_partial():
    async def scenario():
        requests = []

        def handler(request):
            start = int(request.url.params["start"])
            requests.append(start)
            if start == 0:
                return httpx.Response(
                    200, json=page(range(0, 20), total=47, start=0, next_start=20)
                )
            # Every later page is served from a smaller snapshot and ends early.
            return httpx.Response(200, json=page(range(20, 33), total=33, start=20))

        client, provider = make_provider(handler, citation_rounds=2, page_retries=2)
        async with client:
            batch = await provider.citations("google_scholar:111", limit=100)
        assert len(batch.papers) == 33
        assert batch.total_available == 47
        # Same 33 IDs in two complete rounds, but the advertised 47 gap remains.
        assert batch.truncated
        outcome = batch.context["cluster_outcomes"][0]
        assert outcome["stop_reason"] == "round_budget"
        assert requests == [0, 20, 20, 20, 0, 20, 20, 20]
        assert all(r["stale_retries"] == 2 and not r["consistent"] for r in outcome["rounds"])
        assert outcome["rounds"][0]["pages"][1]["stale"] is True

    asyncio.run(scenario())


def test_page_retries_zero_disables_re_requests():
    async def scenario():
        requests = []

        def handler(request):
            start = int(request.url.params["start"])
            requests.append(start)
            total = 5 if start == 0 else 9
            return httpx.Response(
                200,
                json=page(
                    range(start, start + 5),
                    total=total,
                    start=start,
                    next_start=None if start else 20,
                ),
            )

        client, provider = make_provider(handler, citation_rounds=2, page_retries=0)
        async with client:
            batch = await provider.citations("google_scholar:111", limit=100)
        assert requests == [0, 20, 0, 20]
        assert batch.context["cluster_outcomes"][0]["page_retries_per_round"] == 0
        assert batch.context["cluster_outcomes"][0]["rounds"][1]["pages"][0]["stale"] is True

    asyncio.run(scenario())


def test_page_retries_env_and_validation(monkeypatch):
    monkeypatch.setenv("SCHOLAR_GOOGLE_PAGE_RETRIES", "5")
    provider = SerpApiGoogleScholarProvider(api_key="fixture")
    assert provider._page_retries == 5
    with pytest.raises(ValueError):
        SerpApiGoogleScholarProvider(api_key="fixture", page_retries=-1)


def seed_item(cites, total):
    return {
        "result_id": f"SEED-{cites}",
        "title": "A Verified Seed",
        "link": "https://arxiv.org/abs/2501.12345",
        "publication_info": {"authors": [{"name": "Ada Researcher"}]},
        "inline_links": {"cited_by": {"cites_id": cites, "total": total}},
    }


def advertised_scenario(list_totals):
    """Seed advertises Cited by 6; the cites list answers with list_totals per request."""

    async def scenario():
        requests = []

        def handler(request):
            p = request.url.params
            if "cites" not in p:
                return httpx.Response(200, json={"organic_results": [seed_item("777", 6)]})
            requests.append(int(p["start"]))
            total = list_totals[min(len(requests) - 1, len(list_totals) - 1)]
            return httpx.Response(200, json=page(range(total), total=total, start=0))

        client, provider = make_provider(handler, citation_rounds=3, page_retries=2)
        async with client:
            seed = await provider.resolve("arxiv:2501.12345")
            identifier = provider.traversal_identifier(seed, "")
            assert identifier == "google_scholar:777"
            assert provider._advertised_totals == {"777": 6}
            batch = await provider.citations(identifier, limit=100)
        return requests, batch

    return asyncio.run(scenario())


def test_seed_advertised_count_forces_retry_of_smaller_snapshots():
    # Round 1: 2 (stale, retry) -> 4 (stale, retry) -> 6. Round 2: 6. Stable at 6.
    requests, batch = advertised_scenario([2, 4, 6, 6])
    assert requests == [0, 0, 0, 0]
    assert len(batch.papers) == 6 and batch.total_available == 6 and not batch.truncated
    outcome = batch.context["cluster_outcomes"][0]
    assert outcome["advertised_total"] == 6 and outcome["stop_reason"] == "stable"
    assert [r["stale_retries"] for r in outcome["rounds"]] == [2, 0]


def test_seed_advertised_count_never_reached_stays_partial():
    # Every request answers from a 2-result snapshot; two identical rounds are
    # not "complete" because Google itself advertised 6.
    requests, batch = advertised_scenario([2])
    assert len(requests) == 9  # 3 rounds x (1 page + 2 retries)
    assert len(batch.papers) == 2 and batch.total_available == 6 and batch.truncated
    outcome = batch.context["cluster_outcomes"][0]
    assert outcome["stop_reason"] == "round_budget"
    assert all(r["ended"] and not r["consistent"] for r in outcome["rounds"])
