from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx

from scholarly_retrieval.models import IdentifierScheme, SearchQuery
from scholarly_retrieval.providers.acl_anthology import AclAnthologyProvider

MODS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<modsCollection xmlns="http://www.loc.gov/mods/v3">
  <mods ID="devlin-etal-2019-bert">
    <titleInfo><title>BERT: Pre-training of Deep Bidirectional Transformers</title></titleInfo>
    <name type="personal">
      <namePart type="given">Jacob</namePart><namePart type="family">Devlin</namePart>
      <nameIdentifier type="orcid">0000-0001-2345-6789</nameIdentifier>
      <role><roleTerm type="text">author</roleTerm></role>
    </name>
    <name type="personal">
      <namePart type="given">Ming-Wei</namePart><namePart type="family">Chang</namePart>
      <role><roleTerm type="text">author</roleTerm></role>
    </name>
    <originInfo><dateIssued>2019-06</dateIssued></originInfo>
    <relatedItem type="host"><titleInfo><title>NAACL-HLT 2019</title></titleInfo></relatedItem>
    <identifier type="citekey">devlin-etal-2019-bert</identifier>
    <identifier type="doi">10.18653/v1/N19-1423</identifier>
    <language><languageTerm>eng</languageTerm></language>
    <location><url>https://aclanthology.org/N19-1423/</url></location>
  </mods>
</modsCollection>
"""


def test_acl_search_uses_dblp_only_for_discovery_then_maps_official_xml() -> None:
    async def scenario() -> None:
        def discovery_handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/search/publ/api"
            assert parse_qs(request.url.query.decode()) == {
                "format": ["json"],
                "h": ["20"],
                "q": ["BERT pre-training"],
            }
            return httpx.Response(
                200,
                json={
                    "result": {
                        "hits": {
                            "@total": "50",
                            "hit": [
                                {
                                    "info": {
                                        "key": "conf/naacl/DevlinCLT19",
                                        "ee": "https://aclanthology.org/N19-1423",
                                        "url": "https://dblp.org/rec/conf/naacl/DevlinCLT19",
                                    }
                                },
                                {"info": {"ee": "https://example.org/not-acl"}},
                            ],
                        }
                    }
                },
            )

        def acl_handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/N19-1423.xml"
            return httpx.Response(200, text=MODS_XML)

        acl_client = httpx.AsyncClient(
            base_url="https://aclanthology.org",
            transport=httpx.MockTransport(acl_handler),
        )
        discovery_client = httpx.AsyncClient(
            base_url="https://dblp.org",
            transport=httpx.MockTransport(discovery_handler),
        )
        provider = AclAnthologyProvider(
            client=acl_client,
            discovery_client=discovery_client,
        )
        batch = await provider.search(SearchQuery(text="BERT pre-training", limit=2))
        await provider.close()
        await acl_client.aclose()
        await discovery_client.aclose()

        paper = batch.papers[0]
        assert paper.record_id == "acl_anthology:N19-1423"
        assert paper.title.startswith("BERT:")
        assert [author.name for author in paper.authors] == ["Jacob Devlin", "Ming-Wei Chang"]
        assert paper.authors[0].orcid == "0000-0001-2345-6789"
        assert paper.publication_year == 2019
        assert paper.venue == "NAACL-HLT 2019"
        assert paper.open_access is True
        assert paper.source_records[0].retrieval_context == {
            "discovery_index": "dblp",
            "discovery_record_id": "conf/naacl/DevlinCLT19",
            "discovery_url": "https://dblp.org/rec/conf/naacl/DevlinCLT19",
        }
        assert {(claim.scheme, claim.value) for claim in paper.identifiers} == {
            (IdentifierScheme.ACL_ANTHOLOGY, "N19-1423"),
            (IdentifierScheme.DOI, "10.18653/v1/n19-1423"),
        }
        assert batch.total_available is None
        assert batch.truncated is True

    asyncio.run(scenario())


def test_acl_resolve_accepts_id_url_and_canonical_doi() -> None:
    async def scenario() -> None:
        requested: list[str] = []

        def acl_handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            return httpx.Response(200, text=MODS_XML)

        acl_client = httpx.AsyncClient(
            base_url="https://aclanthology.org",
            transport=httpx.MockTransport(acl_handler),
        )
        discovery_client = httpx.AsyncClient(base_url="https://dblp.org")
        provider = AclAnthologyProvider(
            client=acl_client,
            discovery_client=discovery_client,
        )
        for identifier in (
            "acl_anthology:N19-1423",
            "https://aclanthology.org/N19-1423.pdf",
            "https://doi.org/10.18653/v1/N19-1423",
        ):
            paper = await provider.resolve(identifier)
            assert paper is not None
            assert paper.record_id == "acl_anthology:N19-1423"
        await acl_client.aclose()
        await discovery_client.aclose()

        assert requested == ["/N19-1423.xml"] * 3

    asyncio.run(scenario())


def test_acl_rejects_entity_declarations() -> None:
    async def scenario() -> None:
        unsafe = """<!DOCTYPE mods [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
        <mods xmlns="http://www.loc.gov/mods/v3"><titleInfo><title>&xxe;</title></titleInfo></mods>"""
        acl_client = httpx.AsyncClient(
            base_url="https://aclanthology.org",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=unsafe)),
        )
        discovery_client = httpx.AsyncClient(base_url="https://dblp.org")
        provider = AclAnthologyProvider(
            client=acl_client,
            discovery_client=discovery_client,
        )
        try:
            await provider.resolve("N19-1423")
        except ValueError as exc:
            assert "entity declaration" in str(exc)
        else:
            raise AssertionError("unsafe ACL Anthology XML was accepted")
        finally:
            await acl_client.aclose()
            await discovery_client.aclose()

    asyncio.run(scenario())


def test_acl_resolve_404_returns_none() -> None:
    async def scenario() -> None:
        acl_client = httpx.AsyncClient(
            base_url="https://aclanthology.org",
            transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
        )
        discovery_client = httpx.AsyncClient(base_url="https://dblp.org")
        provider = AclAnthologyProvider(
            client=acl_client,
            discovery_client=discovery_client,
        )
        assert await provider.resolve("acl_anthology:N19-9999") is None
        await acl_client.aclose()
        await discovery_client.aclose()

    asyncio.run(scenario())
