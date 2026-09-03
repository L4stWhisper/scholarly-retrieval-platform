"""MCP adapter exposing the core library to agent clients."""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from .config import load_environment
from .models import (
    ExpansionDirection,
    GraphExpansionQuery,
    ReferenceExtractionResult,
    RelatedQuery,
    SearchQuery,
    SearchSort,
    StopRule,
)
from .reference_extraction import extract_references_from_text
from .service import ScholarService

ServiceFactory = Callable[[], ScholarService]


class StaticTokenVerifier:
    """Constant-time verifier for operator-managed MCP bearer tokens."""

    def __init__(self, tokens: set[str]) -> None:
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        if not any(hmac.compare_digest(token, expected) for expected in self._tokens):
            return None
        return AccessToken(
            token=token,
            client_id="scholarly-retrieval-client",
            scopes=["scholar:read"],
            subject="static-token",
        )


@dataclass
class _McpRateWindow:
    started_at: float
    count: int


def create_mcp_server(
    service_factory: ServiceFactory = ScholarService,
    *,
    api_keys: set[str] | None = None,
) -> FastMCP:
    """Build an MCP server without duplicating service or schema logic."""

    load_environment()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[ScholarService]:
        service = service_factory()
        try:
            yield service
        finally:
            await service.close()

    configured_keys = api_keys
    if configured_keys is None:
        configured_keys = {
            value.strip()
            for value in os.getenv("SCHOLAR_MCP_API_KEYS", "").split(",")
            if value.strip()
        }
    host = os.getenv("SCHOLAR_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("SCHOLAR_MCP_PORT", "8001"))
    issuer_url = os.getenv("SCHOLAR_MCP_ISSUER_URL", f"http://{host}:{port}")
    resource_url = os.getenv("SCHOLAR_MCP_RESOURCE_URL", f"http://{host}:{port}/mcp")
    request_limit = int(os.getenv("SCHOLAR_MCP_RATE_LIMIT_PER_MINUTE", "60"))
    rate_windows: dict[str, _McpRateWindow] = {}
    server = FastMCP(
        "Scholarly Retrieval Platform",
        instructions=(
            "Search scholarly sources, resolve paper identifiers, and traverse "
            "reference/citation relations. Inspect provider_reports for partial "
            "coverage, truncation, errors, and per-provider continuations."
        ),
        lifespan=lifespan,
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        max_request_body_size=int(
            os.getenv("SCHOLAR_MCP_MAX_REQUEST_BODY_BYTES", str(4 * 1024 * 1024))
        ),
        token_verifier=(StaticTokenVerifier(configured_keys) if configured_keys else None),
        auth=(
            AuthSettings(
                issuer_url=issuer_url,
                resource_server_url=resource_url,
                required_scopes=["scholar:read"],
            )
            if configured_keys
            else None
        ),
    )

    def service_from(context: Context) -> ScholarService:
        # FastMCP keeps the yielded lifespan value in the request context. This
        # gives all tools one cache/store/client lifecycle for the server process.
        access_token = get_access_token()
        subject = (
            (access_token.subject or access_token.client_id)
            if access_token is not None
            else (context.client_id or "stdio-client")
        )
        if request_limit > 0:
            now = monotonic()
            window = rate_windows.get(subject)
            if window is None or now - window.started_at >= 60:
                rate_windows[subject] = _McpRateWindow(now, 1)
            elif window.count >= request_limit:
                raise ValueError("MCP request quota exceeded")
            else:
                window.count += 1
        return context.request_context.lifespan_context

    @server.tool()
    async def providers(context: Context) -> dict[str, dict]:
        """List configured sources and their real search/citation capabilities."""

        return service_from(context).provider_capabilities()

    @server.tool()
    async def search(
        query: str,
        context: Context,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        sources: list[str] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        author: str | None = None,
        open_access: bool | None = None,
        title: str | None = None,
        abstract: str | None = None,
        venue: str | None = None,
        field: str | None = None,
        work_types: list[str] | None = None,
        min_citations: Annotated[int | None, Field(ge=0)] = None,
        sort: SearchSort = SearchSort.RELEVANCE,
        expression: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Search papers by text and optional portable field filters."""

        request = SearchQuery(
            text=query,
            expression=expression,
            limit=limit,
            year_from=year_from,
            year_to=year_to,
            author=author,
            open_access=open_access,
            title=title,
            abstract=abstract,
            venue=venue,
            field=field,
            work_types=work_types or [],
            min_citations=min_citations,
            sort=sort,
        )
        result = await service_from(context).search(request, sources=sources)
        return result.model_dump(mode="json")

    @server.tool()
    async def resolve(
        identifier: str,
        context: Context,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Resolve a DOI, arXiv ID, or provider ID to canonical paper evidence."""

        result = await service_from(context).resolve(identifier, sources=sources)
        return result.model_dump(mode="json")

    @server.tool()
    async def related(
        context: Context,
        text: str | None = None,
        positive_identifiers: list[str] | None = None,
        negative_identifiers: list[str] | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        year_from: Annotated[int | None, Field(ge=1000, le=3000)] = None,
        year_to: Annotated[int | None, Field(ge=1000, le=3000)] = None,
        open_access: bool | None = None,
        include_work_types: list[str] | None = None,
        rrf_k: Annotated[int, Field(ge=1, le=1000)] = 60,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fuse semantic-text and seed-paper recommendation results with RRF."""

        query = RelatedQuery(
            text=text,
            positive_identifiers=positive_identifiers or [],
            negative_identifiers=negative_identifiers or [],
            limit=limit,
            year_from=year_from,
            year_to=year_to,
            open_access=open_access,
            include_work_types=include_work_types or [],
            rrf_k=rrf_k,
        )
        result = await service_from(context).related(query, sources=sources)
        return result.model_dump(mode="json")

    @server.tool()
    async def extract_references(
        content: str,
        source_format: str,
        context: Context,
        include_uncited: bool = False,
    ) -> dict[str, Any]:
        """Extract JATS, TEI, LaTeX, or BibTeX references supplied by the user."""

        # Even this CPU-only tool passes through the shared MCP subject quota.
        service_from(context)
        result = extract_references_from_text(
            content,
            source_format=source_format,
            include_uncited=include_uncited,
        )
        return result.model_dump(mode="json")

    @server.tool()
    async def link_references(
        seed_identifier: str,
        extraction: dict[str, Any],
        context: Context,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Link extracted references to canonical papers and verified edges."""

        parsed = ReferenceExtractionResult.model_validate(extraction)
        result = await service_from(context).link_references(
            seed_identifier, parsed, sources=sources
        )
        return result.model_dump(mode="json")

    @server.tool()
    async def references(
        identifier: str,
        context: Context,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return works referenced by a seed paper (seed -> cited work)."""

        result = await service_from(context).references(
            identifier,
            limit=limit,
            sources=sources,
        )
        return result.model_dump(mode="json")

    @server.tool()
    async def citations(
        identifier: str,
        context: Context,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return later works citing a seed paper (citing work -> seed)."""

        result = await service_from(context).citations(
            identifier,
            limit=limit,
            sources=sources,
        )
        return result.model_dump(mode="json")

    @server.tool()
    async def expand(
        context: Context,
        identifier: str | None = None,
        identifiers: list[str] | None = None,
        direction: ExpansionDirection = ExpansionDirection.BOTH,
        depth: Annotated[int, Field(ge=1, le=3)] = 2,
        frontier_cap: Annotated[int, Field(ge=1, le=1000)] = 200,
        top_k: Annotated[int, Field(ge=1, le=5000)] = 100,
        per_node_limit: Annotated[int, Field(ge=1, le=1000)] = 100,
        stop_rule: StopRule = StopRule.BUDGET,
        year_from: Annotated[int | None, Field(ge=1000, le=3000)] = None,
        year_to: Annotated[int | None, Field(ge=1000, le=3000)] = None,
        include_work_types: list[str] | None = None,
        max_runtime_seconds: Annotated[float | None, Field(ge=0.1, le=3600)] = None,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build a bounded graph from one identifier or up to 20 seed identifiers."""

        query = GraphExpansionQuery(
            identifier=identifier,
            identifiers=identifiers or [],
            direction=direction,
            depth=depth,
            frontier_cap=frontier_cap,
            top_k=top_k,
            per_node_limit=per_node_limit,
            stop_rule=stop_rule,
            year_from=year_from,
            year_to=year_to,
            include_work_types=include_work_types or [],
            max_runtime_seconds=max_runtime_seconds,
        )
        result = await service_from(context).expand(query, sources=sources)
        return result.model_dump(mode="json")

    return server


mcp = create_mcp_server()


def main() -> None:
    """Run over stdio by default, or Streamable HTTP when configured."""

    transport = os.getenv("SCHOLAR_MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("SCHOLAR_MCP_TRANSPORT must be 'stdio' or 'streamable-http'")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
