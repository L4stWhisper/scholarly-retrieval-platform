"""Optional, user-authorized Google Scholar access through SerpApi."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

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
    SourceRecord,
)
from ..normalization import normalize_doi
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider

DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[^\s<>\]}]+", re.IGNORECASE)
YEAR_PATTERN = re.compile(r"\b(1\d{3}|2\d{3})\b")


class SerpApiGoogleScholarProvider(ScholarlyProvider):
    """Paid/authorized adapter; it never scrapes scholar.google.com directly."""

    name = "google_scholar_serpapi"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="none",
        citations="list",
        search_filter_execution={
            "text": "provider",
            "year_from": "provider",
            "year_to": "provider",
            "author": "provider",
            "venue": "provider",
            "open_access": "local",
        },
        pagination={"search": "start_offset", "citations": "start_offset"},
        access_tier="paid_api_key",
        credential_variables=["SERPAPI_API_KEY"],
        terms_url="https://serpapi.com/legal",
        redistribution_policy="serpapi_terms_and_upstream_rights_apply",
    )

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        key = api_key or os.getenv("SERPAPI_API_KEY")
        if not key:
            raise ValueError("SERPAPI_API_KEY is required for Google Scholar")
        self._api_key = key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://serpapi.com", timeout=httpx.Timeout(30.0)
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(cache_ttl_seconds=3600, max_concurrency=1),
        )

    async def _query(self, params: dict[str, str], operation: str) -> dict[str, Any]:
        response = await self._http.get(
            "/search.json",
            params={"engine": "google_scholar", "api_key": self._api_key, **params},
            operation=operation,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise ValueError("SerpApi Google Scholar returned an API error")
        return payload

    async def search(self, query: SearchQuery) -> ProviderBatch:
        terms = [query.text]
        if query.author:
            terms.append(f'author:"{query.author}"')
        if query.venue:
            terms.append(f'source:"{query.venue}"')
        params = {"q": " ".join(terms)}
        if query.year_from is not None:
            params["as_ylo"] = str(query.year_from)
        if query.year_to is not None:
            params["as_yhi"] = str(query.year_to)
        papers, total, next_start = await self._pages(params, limit=query.limit, operation="search")
        if query.open_access is not None:
            papers = [paper for paper in papers if paper.open_access == query.open_access]
        execution = {"text": "provider"}
        if query.author:
            execution["author"] = "provider"
        if query.venue:
            execution["venue"] = "provider"
        if query.year_from is not None:
            execution["year_from"] = "provider"
        if query.year_to is not None:
            execution["year_to"] = "provider"
        if query.open_access is not None:
            execution["open_access"] = "local"
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=next_start is not None or (total is not None and total > len(papers)),
            next_cursor=str(next_start) if next_start is not None else None,
            filter_execution=execution,
        )

    async def resolve(self, identifier: str) -> Paper | None:
        normalized = identifier.strip()
        if normalized.casefold().startswith("google_scholar:"):
            normalized = normalized.split(":", 1)[1]
        if normalized.isdigit():
            payload = await self._query({"cluster": normalized, "num": "1"}, "resolve")
            items = payload.get("organic_results", [])
            return self._paper(items[0]) if items else None
        # DOI and other portable IDs are looked up as an exact quoted query; the
        # returned identifier evidence remains provider asserted, not guaranteed.
        payload = await self._query({"q": f'"{normalized}"', "num": "1"}, "resolve")
        items = payload.get("organic_results", [])
        return self._paper(items[0]) if items else None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        cites_id = identifier.strip()
        if cites_id.casefold().startswith("google_scholar:"):
            cites_id = cites_id.split(":", 1)[1]
        if not cites_id.isdigit():
            seed = await self.resolve(identifier)
            if seed is None:
                return ProviderBatch()
            claim = next(
                (
                    item
                    for item in seed.identifiers
                    if item.scheme == IdentifierScheme.GOOGLE_SCHOLAR
                ),
                None,
            )
            if claim is None:
                return ProviderBatch()
            cites_id = claim.value
        papers, total, next_start = await self._pages(
            {"cites": cites_id}, limit=limit, operation="citations"
        )
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=next_start is not None or (total is not None and total > len(papers)),
            next_cursor=str(next_start) if next_start is not None else None,
        )

    async def _pages(
        self, params: dict[str, str], *, limit: int, operation: str
    ) -> tuple[list[Paper], int | None, int | None]:
        papers: list[Paper] = []
        start = 0
        total: int | None = None
        next_start: int | None = None
        while len(papers) < limit:
            page_size = min(20, limit - len(papers))
            payload = await self._query(
                {**params, "start": str(start), "num": str(page_size)}, operation
            )
            items = payload.get("organic_results", [])
            papers.extend(self._paper(item) for item in items)
            total = payload.get("search_information", {}).get("total_results", total)
            next_link = payload.get("serpapi_pagination", {}).get("next")
            next_start = start + len(items) if next_link and items else None
            if next_start is None or not items:
                break
            start = next_start
        return papers[:limit], total, next_start

    def _paper(self, item: dict[str, Any]) -> Paper:
        inline = item.get("inline_links") or {}
        cited_by = inline.get("cited_by") or {}
        versions = inline.get("versions") or {}
        cites_id = str(cited_by.get("cites_id") or versions.get("cluster_id") or "")
        result_id = str(item.get("result_id") or cites_id)
        if not result_id:
            material = f"{item.get('title', '')}\0{item.get('link', '')}".encode()
            result_id = hashlib.sha256(material).hexdigest()[:24]
        provenance = Provenance(provider=self.name, source_record_id=result_id)
        summary = str((item.get("publication_info") or {}).get("summary") or "")
        year_matches = YEAR_PATTERN.findall(summary)
        authors_data = (item.get("publication_info") or {}).get("authors") or []
        authors = [
            Author(name=str(author.get("name"))) for author in authors_data if author.get("name")
        ]
        if not authors and summary:
            author_text = summary.split(" - ", 1)[0]
            authors = [
                Author(name=value.strip()) for value in author_text.split(",") if value.strip()
            ]
        searchable = " ".join(str(item.get(field) or "") for field in ("title", "link", "snippet"))
        doi_match = DOI_PATTERN.search(searchable)
        identifiers = []
        if cites_id:
            identifiers.append(
                IdentifierClaim(
                    scheme=IdentifierScheme.GOOGLE_SCHOLAR,
                    value=cites_id,
                    authority="Google Scholar cluster",
                    provenance=provenance,
                )
            )
        if doi_match:
            identifiers.append(
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value=normalize_doi(doi_match.group(0)),
                    authority="DOI",
                    provenance=provenance,
                )
            )
        resources = item.get("resources") or []
        pdf_url = next(
            (
                resource.get("link")
                for resource in resources
                if str(resource.get("file_format", "")).casefold() == "pdf"
            ),
            None,
        )
        count = cited_by.get("total")
        return Paper(
            record_id=f"google_scholar:{result_id}",
            work_family_id=f"google_scholar_cluster:{cites_id}" if cites_id else None,
            title=str(item.get("title") or "Untitled Google Scholar result"),
            abstract=item.get("snippet"),
            authors=authors,
            publication_year=int(year_matches[-1]) if year_matches else None,
            landing_page_url=item.get("link"),
            pdf_url=pdf_url,
            open_access=pdf_url is not None,
            identifiers=identifiers,
            citation_counts=(
                [
                    CitationCountClaim(
                        provider=self.name,
                        count=int(count),
                        source_record_id=result_id,
                    )
                ]
                if isinstance(count, int) and count >= 0
                else []
            ),
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=result_id,
                    source_url=item.get("link"),
                    provider_rank=item.get("position"),
                )
            ],
            field_provenance={
                field: [provenance]
                for field in [
                    "title",
                    "abstract",
                    "authors",
                    "publication_year",
                    "open_access",
                ]
            },
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
