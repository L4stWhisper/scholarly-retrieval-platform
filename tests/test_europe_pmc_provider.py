from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx

from scholarly_retrieval.models import IdentifierScheme, SearchQuery
from scholarly_retrieval.providers.europe_pmc import EuropePmcProvider


def epmc_result(record_id: str = "34265844") -> dict:
    return {
        "id": record_id,
        "source": "MED",
        "pmid": record_id,
        "pmcid": "PMC8371605",
        "doi": "10.1038/s41586-021-03819-2",
        "title": "Highly accurate protein structure prediction with AlphaFold.",
        "abstractText": "Protein structure prediction.",
        "authorList": {
            "author": [
                {"firstName": "John", "lastName": "Jumper", "fullName": "Jumper J"}
            ]
        },
        "journalInfo": {"journal": {"title": "Nature"}},
        "pubYear": "2021",
        "firstPublicationDate": "2021-07-15",
        "pubTypeList": {"pubType": ["research article"]},
        "language": "eng",
        "isOpenAccess": "Y",
        "citedByCount": 34984,
        "fullTextUrlList": {
            "fullTextUrl": [
                {
                    "documentStyle": "pdf",
                    "url": "https://europepmc.org/articles/PMC8371605?pdf=render",
                }
            ]
        },
    }


def test_europe_pmc_search_compiles_filters_and_maps_core_metadata() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            compiled = query["query"][0]
            assert "protein folding" in compiled
            assert 'AUTH:"Jumper"' in compiled
            assert "FIRST_PDATE:[2020-01-01 TO 2022-12-31]" in compiled
            assert "OPEN_ACCESS:Y" in compiled
            assert query["resultType"] == ["core"]
            assert query["cursorMark"] == ["*"]
            return httpx.Response(
                200,
                json={
                    "hitCount": 5,
                    "nextCursorMark": "opaque-next",
                    "resultList": {"result": [epmc_result()]},
                },
            )

        client = httpx.AsyncClient(
            base_url="https://www.ebi.ac.uk/europepmc/webservices/rest",
            transport=httpx.MockTransport(handler),
        )
        provider = EuropePmcProvider(client=client)
        batch = await provider.search(
            SearchQuery(
                text="protein folding",
                author="Jumper",
                year_from=2020,
                year_to=2022,
                open_access=True,
                limit=1,
            )
        )
        await client.aclose()

        paper = batch.papers[0]
        assert paper.record_id == "europe_pmc:MED:34265844"
        assert paper.venue == "Nature"
        assert paper.open_access is True
        assert paper.pdf_url is not None
        assert {(claim.scheme, claim.value) for claim in paper.identifiers} >= {
            (IdentifierScheme.DOI, "10.1038/s41586-021-03819-2"),
            (IdentifierScheme.PMID, "34265844"),
            (IdentifierScheme.PMCID, "PMC8371605"),
        }
        assert batch.next_cursor == "opaque-next"
        assert batch.truncated is True

    asyncio.run(scenario())


def test_europe_pmc_resolve_supports_direct_pmid_and_doi_search() -> None:
    async def scenario() -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/article/MED/34265844"):
                return httpx.Response(200, json={"hitCount": 1, "result": epmc_result()})
            query = parse_qs(request.url.query.decode())
            assert query["query"] == ['DOI:"10.1038/s41586-021-03819-2"']
            return httpx.Response(
                200,
                json={"hitCount": 1, "resultList": {"result": [epmc_result()]}},
            )

        client = httpx.AsyncClient(
            base_url="https://www.ebi.ac.uk/europepmc/webservices/rest",
            transport=httpx.MockTransport(handler),
        )
        provider = EuropePmcProvider(client=client)
        by_pmid = await provider.resolve("PMID:34265844")
        by_doi = await provider.resolve("10.1038/s41586-021-03819-2")
        await client.aclose()

        assert by_pmid is not None and by_pmid.record_id == "europe_pmc:MED:34265844"
        assert by_doi is not None and by_doi.record_id == by_pmid.record_id
        assert paths == [
            "/europepmc/webservices/rest/article/MED/34265844",
            "/europepmc/webservices/rest/search",
        ]

    asyncio.run(scenario())


def test_europe_pmc_relations_map_matched_records_and_keep_unresolved_references() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query["page"] == ["1"]
            assert query["pageSize"] == ["2"]
            if request.url.path.endswith("/references"):
                return httpx.Response(
                    200,
                    json={
                        "hitCount": 65,
                        "referenceList": {
                            "reference": [
                                {
                                    "id": "34368623",
                                    "source": "MED",
                                    "title": "Matched reference",
                                    "authorString": "Xu J.",
                                    "pubYear": 2021,
                                    "citationType": "JOURNAL ARTICLE",
                                },
                                {
                                    "title": "Unmatched reference",
                                    "authorString": "Unknown A.",
                                    "pubYear": 1999,
                                    "match": "N",
                                },
                            ]
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "hitCount": 10,
                    "citationList": {
                        "citation": [
                            {
                                "id": "42583835",
                                "source": "MED",
                                "title": "Citing paper",
                                "pubYear": 2026,
                                "citedByCount": 0,
                            }
                        ]
                    },
                },
            )

        client = httpx.AsyncClient(
            base_url="https://www.ebi.ac.uk/europepmc/webservices/rest",
            transport=httpx.MockTransport(handler),
        )
        provider = EuropePmcProvider(client=client)
        references = await provider.references("PMID:34265844", limit=2)
        citations = await provider.citations("PMID:34265844", limit=2)
        await client.aclose()

        assert [paper.record_id for paper in references.papers] == [
            "europe_pmc:MED:34368623"
        ]
        assert references.unresolved_references[0].title == "Unmatched reference"
        assert references.total_available == 65
        assert references.next_cursor == "2"
        assert [paper.record_id for paper in citations.papers] == [
            "europe_pmc:MED:42583835"
        ]
        assert citations.total_available == 10

    asyncio.run(scenario())
