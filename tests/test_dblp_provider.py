from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx

from scholarly_retrieval.models import IdentifierScheme, SearchQuery
from scholarly_retrieval.providers.dblp import DblpProvider


def _payload() -> dict:
    return {
        "result": {
            "hits": {
                "@total": "180",
                "hit": [
                    {
                        "info": {
                            "authors": {
                                "author": [
                                    {"@pid": "1", "text": "Ada Researcher"},
                                    {"@pid": "2", "text": "Bob Author"},
                                ]
                            },
                            "title": "Citation Graph Retrieval.",
                            "venue": "CoRR",
                            "year": "2024",
                            "type": "Informal Publications",
                            "access": "open",
                            "key": "journals/corr/example",
                            "doi": "10.48550/ARXIV.2401.00001",
                            "url": "https://dblp.org/rec/journals/corr/example",
                        }
                    }
                ],
            }
        }
    }


def test_dblp_search_maps_json_api_and_total() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            assert query == {"format": ["json"], "h": ["2"], "q": ["citation graph"]}
            return httpx.Response(200, json=_payload())

        client = httpx.AsyncClient(
            base_url="https://dblp.org", transport=httpx.MockTransport(handler)
        )
        provider = DblpProvider(client=client)
        batch = await provider.search(SearchQuery(text="citation graph", limit=2))
        await client.aclose()

        paper = batch.papers[0]
        assert paper.record_id == "dblp:journals/corr/example"
        assert paper.title == "Citation Graph Retrieval"
        assert [author.name for author in paper.authors] == ["Ada Researcher", "Bob Author"]
        assert paper.publication_year == 2024
        assert any(
            claim.scheme == IdentifierScheme.DBLP
            and claim.value == "journals/corr/example"
            for claim in paper.identifiers
        )
        assert batch.total_available == 180
        assert batch.truncated is True

    asyncio.run(scenario())


def test_dblp_resolve_doi_requires_exact_claim_match() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            base_url="https://dblp.org",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=_payload())),
        )
        provider = DblpProvider(client=client)
        found = await provider.resolve("10.48550/arxiv.2401.00001")
        missing = await provider.resolve("10.9999/not-present")
        await client.aclose()

        assert found is not None
        assert missing is None

    asyncio.run(scenario())


def test_dblp_resolve_key_maps_single_record_xml() -> None:
    async def scenario() -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <dblp>
          <inproceedings key="conf/nips/VaswaniSPUJGKP17">
            <author pid="v/AshishVaswani">Ashish Vaswani</author>
            <author>Noam Shazeer</author>
            <title>Attention is All you Need.</title>
            <year>2017</year>
            <booktitle>NIPS</booktitle>
            <ee type="oa">https://papers.nips.cc/paper/7181-attention-is-all-you-need</ee>
            <url>db/conf/nips/nips2017.html#VaswaniSPUJGKP17</url>
          </inproceedings>
        </dblp>"""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/rec/conf/nips/VaswaniSPUJGKP17.xml"
            return httpx.Response(200, text=xml)

        client = httpx.AsyncClient(
            base_url="https://dblp.org", transport=httpx.MockTransport(handler)
        )
        provider = DblpProvider(client=client)
        paper = await provider.resolve("dblp:conf/nips/VaswaniSPUJGKP17")
        await client.aclose()

        assert paper is not None
        assert paper.title == "Attention is All you Need"
        assert paper.venue == "NIPS"
        assert paper.open_access is True
        assert [author.name for author in paper.authors] == ["Ashish Vaswani", "Noam Shazeer"]
        assert any(
            claim.scheme == IdentifierScheme.DBLP
            and claim.value == "conf/nips/VaswaniSPUJGKP17"
            for claim in paper.identifiers
        )

    asyncio.run(scenario())


def test_dblp_resolve_key_rejects_entity_declarations() -> None:
    async def scenario() -> None:
        xml = """<!DOCTYPE dblp [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
        <dblp><article key="journals/test/unsafe"><title>&xxe;</title></article></dblp>"""
        client = httpx.AsyncClient(
            base_url="https://dblp.org",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=xml)),
        )
        provider = DblpProvider(client=client)
        try:
            await provider.resolve("dblp:journals/test/unsafe")
        except ValueError as exc:
            assert "entity declaration" in str(exc)
        else:
            raise AssertionError("unsafe DBLP XML was accepted")
        finally:
            await client.aclose()

    asyncio.run(scenario())
