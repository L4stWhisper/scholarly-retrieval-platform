from __future__ import annotations

import asyncio

from mcp.shared.memory import create_connected_server_and_client_session

from scholarly_retrieval.mcp_server import StaticTokenVerifier, create_mcp_server
from scholarly_retrieval.models import (
    Paper,
    ProviderBatch,
    RelatedQuery,
    RetrievalMethod,
    SearchQuery,
    SourceRecord,
)
from scholarly_retrieval.providers.base import ProviderCapabilities, ScholarlyProvider
from scholarly_retrieval.service import ScholarService


class McpFixtureProvider(ScholarlyProvider):
    name = "mcp_fixture"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        resolve_id=True,
        references="list",
        citations="list",
        related=True,
    )

    async def search(self, query: SearchQuery) -> ProviderBatch:
        return ProviderBatch(papers=[Paper(record_id="mcp:1", title=query.text)])

    async def resolve(self, identifier: str) -> Paper | None:
        return Paper(record_id=f"mcp:{identifier}", title=identifier)

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(
            papers=[
                Paper(
                    record_id="mcp:ref",
                    title="Reference",
                    publication_year=2020,
                    work_type="article",
                )
            ]
        )

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(papers=[Paper(record_id="mcp:cite", title="Citation")])

    async def related(self, query: RelatedQuery) -> ProviderBatch:
        return ProviderBatch(
            papers=[
                Paper(
                    record_id="mcp:related",
                    title="Related",
                    source_records=[
                        SourceRecord(
                            provider=self.name,
                            source_record_id="related",
                            provider_rank=1,
                            retrieval_method=RetrievalMethod.OPENALEX_SEMANTIC,
                        )
                    ],
                )
            ]
        )


def service_factory() -> ScholarService:
    return ScholarService([McpFixtureProvider()])


def test_mcp_static_bearer_verifier_rejects_unknown_tokens() -> None:
    async def scenario() -> None:
        verifier = StaticTokenVerifier({"mcp-secret"})
        accepted = await verifier.verify_token("mcp-secret")
        rejected = await verifier.verify_token("wrong")
        assert accepted is not None
        assert accepted.scopes == ["scholar:read"]
        assert rejected is None

    asyncio.run(scenario())


def test_mcp_tools_are_discoverable_and_call_the_shared_library() -> None:
    async def scenario() -> None:
        server = create_mcp_server(service_factory)
        async with create_connected_server_and_client_session(server) as session:
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "providers",
                "search",
                "resolve",
                "related",
                "extract_references",
                "link_references",
                "references",
                "citations",
                "expand",
            }
            search_tool = next(tool for tool in tools.tools if tool.name == "search")
            expand_tool = next(tool for tool in tools.tools if tool.name == "expand")
            assert "context" not in search_tool.inputSchema["properties"]
            assert search_tool.inputSchema["properties"]["limit"]["maximum"] == 100
            assert "min_citations" in search_tool.inputSchema["properties"]
            assert "sort" in search_tool.inputSchema["properties"]
            assert "identifiers" in expand_tool.inputSchema["properties"]
            assert "include_work_types" in expand_tool.inputSchema["properties"]

            response = await session.call_tool(
                "search",
                {"query": "citation graph", "limit": 1, "sources": ["mcp_fixture"]},
            )
            resolved = await session.call_tool(
                "resolve",
                {"identifier": "seed", "sources": ["mcp_fixture"]},
            )
            related = await session.call_tool(
                "related",
                {"text": "citation discovery", "sources": ["mcp_fixture"]},
            )
            extracted = await session.call_tool(
                "extract_references",
                {
                    "source_format": "bibtex",
                    "content": "@article{x, title={Paper X}, year={2024}}",
                },
            )
            linked = await session.call_tool(
                "link_references",
                {
                    "seed_identifier": "seed",
                    "sources": ["mcp_fixture"],
                    "extraction": {
                        "source_format": "jats",
                        "bibliography_entry_count": 1,
                        "cited_entry_count": 1,
                        "references": [
                            {
                                "reference_id": "R1",
                                "ordinal": 1,
                                "cited_in_text": True,
                                "evidence_level": "verified_anchor",
                                "raw_text": "Scored paper",
                                "title": "Scored paper",
                                "callout_count": 1,
                            }
                        ],
                    },
                },
            )
            references = await session.call_tool(
                "references",
                {"identifier": "seed", "sources": ["mcp_fixture"]},
            )
            citations = await session.call_tool(
                "citations",
                {"identifier": "seed", "sources": ["mcp_fixture"]},
            )
            invalid = await session.call_tool("search", {"query": "query", "limit": 0})
            expanded = await session.call_tool(
                "expand",
                {
                    "identifier": "seed",
                    "direction": "references",
                    "depth": 1,
                    "frontier_cap": 5,
                    "top_k": 5,
                    "per_node_limit": 5,
                    "sources": ["mcp_fixture"],
                },
            )
            multi_seed = await session.call_tool(
                "expand",
                {
                    "identifiers": ["first", "second"],
                    "direction": "references",
                    "depth": 1,
                    "year_from": 2019,
                    "year_to": 2021,
                    "include_work_types": ["article"],
                    "sources": ["mcp_fixture"],
                },
            )

        assert response.isError is False
        assert response.structuredContent is not None
        payload = response.structuredContent
        assert payload["papers"][0]["record_id"] == "mcp:1"
        assert payload["provider_reports"][0]["provider"] == "mcp_fixture"
        assert payload["fingerprint"].startswith("v1:")
        assert resolved.structuredContent["papers"][0]["record_id"] == "mcp:seed"
        assert related.structuredContent["rankings"][0]["score_method"] == "rrf"
        assert extracted.structuredContent["source_format"] == "bibtex"
        assert extracted.structuredContent["references"][0]["evidence_level"] == (
            "bibliography_only"
        )
        assert linked.structuredContent["links"][0]["candidate_scores"][0][
            "total_score"
        ] == 1.0
        assert linked.structuredContent["links"][0]["decision_reason"] == (
            "score_and_margin_satisfied"
        )
        assert references.structuredContent["assertions"][0]["relation"] == "references"
        assert citations.structuredContent["assertions"][0]["relation"] == "cites"
        assert invalid.isError is True
        assert expanded.structuredContent["operation"] == "expand"
        assert expanded.structuredContent["edges"][0]["citing_record_id"] == "mcp:seed"
        assert {paper["record_id"] for paper in multi_seed.structuredContent["seeds"]} == {
            "mcp:first",
            "mcp:second",
        }
        assert multi_seed.structuredContent["bibliographic_couplings"][0]["shared_record_ids"] == [
            "mcp:ref"
        ]
        assert multi_seed.structuredContent["filtered_candidate_count"] == 0

    asyncio.run(scenario())
