"""NASA ADS official search API and citation/reference operators."""

from __future__ import annotations

import json
import os
from urllib.parse import quote, unquote

import httpx

from ..models import (
    Author,
    CitationCountClaim,
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
    ProviderBatch,
    SearchQuery,
)
from ..normalization import normalize_arxiv_id, normalize_doi
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider


class ADSProvider(ScholarlyProvider):
    name = "ads"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        resolve_id=True,
        references="list",
        citations="list",
        search_filter_execution={"text": "provider"},
        pagination={
            "search": "start_offset",
            "citations": "start_offset",
            "references": "start_offset",
        },
        access_tier="api_token_required",
        credential_variables=["ADS_API_TOKEN"],
        terms_url="https://adsabs.github.io/help/terms/",
    )
    fields = "bibcode,title,author,year,doi,identifier,abstract,pub,citation_count"

    def __init__(
        self,
        *,
        api_token: str | None = None,
        client: httpx.AsyncClient | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        token = api_token or os.getenv("ADS_API_TOKEN")
        if not token:
            raise ValueError("ADS_API_TOKEN is required for NASA ADS")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://api.adsabs.harvard.edu",
            timeout=30.0,
        )
        self._client.headers["Authorization"] = f"Bearer {token}"
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(cache_ttl_seconds=3600, max_concurrency=1),
        )

    @staticmethod
    def _identifier_query(identifier: str) -> str:
        value = unquote(identifier.strip())
        if value.startswith("https://ui.adsabs.harvard.edu/abs/"):
            value = value.split("/abs/", 1)[1].split("/")[0]
        if value.startswith("ads:"):
            return "bibcode:" + json.dumps(value[4:])
        doi = normalize_doi(value)
        arxiv = normalize_arxiv_id(value, keep_version=False)
        if doi and doi.startswith("10.48550/arxiv."):
            arxiv = normalize_arxiv_id(doi.split("arxiv.", 1)[1], keep_version=False)
        if arxiv:
            return "identifier:" + json.dumps("arXiv:" + arxiv)
        return ("doi:" if doi else "identifier:") + json.dumps(doi or value)

    async def _pages(self, query: str, *, limit: int, operation: str) -> ProviderBatch:
        papers: list[Paper] = []
        start = 0
        total = 0
        while len(papers) < limit:
            response = await self._http.get(
                "/v1/search/query",
                params={
                    "q": query,
                    "fl": self.fields,
                    "start": str(start),
                    "rows": str(min(200, limit - len(papers))),
                    "sort": "bibcode asc",
                },
                operation=operation,
            )
            response.raise_for_status()
            payload = response.json()
            if "response" not in payload:
                raise ValueError("ADS search response is missing response data")
            data = payload["response"]
            total = int(data["numFound"])
            docs = data.get("docs", [])
            if not docs:
                break
            papers.extend(self._paper(doc) for doc in docs)
            start += len(docs)
            if start >= total:
                break
        return ProviderBatch(
            papers=papers[:limit],
            total_available=total,
            truncated=start < total,
            next_cursor=str(start) if start < total else None,
        )

    async def search(self, query: SearchQuery) -> ProviderBatch:
        return await self._pages(query.text, limit=query.limit, operation="search")

    async def resolve(self, identifier: str) -> Paper | None:
        batch = await self._pages(
            self._identifier_query(identifier),
            limit=1,
            operation="resolve",
        )
        return batch.papers[0] if batch.papers else None

    def traversal_identifier(self, paper: Paper, fallback: str) -> str:
        return paper.record_id

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        # ADS operators define direction: citations(seed) returns citing works.
        return await self._pages(
            f"citations({self._identifier_query(identifier)})",
            limit=limit,
            operation="citations",
        )

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return await self._pages(
            f"references({self._identifier_query(identifier)})",
            limit=limit,
            operation="references",
        )

    def _paper(self, doc: dict) -> Paper:
        bibcode = doc["bibcode"]
        url = "https://ui.adsabs.harvard.edu/abs/" + quote(bibcode, safe="") + "/abstract"
        provenance = Provenance(provider=self.name, source_record_id=bibcode, source_url=url)
        claims = []
        for value in [*doc.get("doi", []), *doc.get("identifier", [])]:
            doi = normalize_doi(value)
            arxiv = normalize_arxiv_id(value, keep_version=False)
            if doi and doi.startswith("10.48550/arxiv."):
                arxiv = normalize_arxiv_id(doi.split("arxiv.", 1)[1], keep_version=False)
            for scheme, normalized in [
                (IdentifierScheme.DOI, doi),
                (IdentifierScheme.ARXIV, arxiv),
            ]:
                if normalized and not any(
                    c.scheme == scheme and c.value == normalized for c in claims
                ):
                    claims.append(
                        IdentifierClaim(
                            scheme=scheme,
                            value=normalized,
                            provenance=provenance,
                        )
                    )
        return Paper(
            record_id="ads:" + bibcode,
            title=doc["title"][0],
            authors=[Author(name=name) for name in doc.get("author", [])],
            publication_year=int(doc["year"]) if doc.get("year") else None,
            venue=doc.get("pub"),
            abstract=doc.get("abstract"),
            landing_page_url=url,
            identifiers=claims,
            citation_counts=[
                CitationCountClaim(
                    provider=self.name,
                    source_record_id=bibcode,
                    count=doc["citation_count"],
                )
            ]
            if doc.get("citation_count") is not None
            else [],
            source_records=[
                {
                    "provider": self.name,
                    "source_record_id": bibcode,
                    "source_url": url,
                }
            ],
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
