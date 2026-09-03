"""DataCite public REST API adapter for DOI-bearing research outputs."""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote

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
    SearchSort,
    SourceRecord,
    utc_now,
)
from ..normalization import normalize_doi
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider


class DataCiteProvider(ScholarlyProvider):
    """Search DataCite's public, unauthenticated Findable DOI metadata."""

    name = "datacite"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="none",
        citations="count",
        search_filter_execution={
            "text": "provider",
            "year_from": "provider",
            "year_to": "provider",
            "author": "provider",
            "title": "provider",
            "sort": "provider",
        },
        pagination={"search": "page_number"},
        access_tier="public_no_key",
        terms_url="https://support.datacite.org/docs/rest-api",
        redistribution_policy="CC0_metadata_subject_to_datacite_terms",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://api.datacite.org",
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "scholarly-retrieval-platform/0.1"},
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(cache_ttl_seconds=3600, max_concurrency=3),
        )

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = await self._http.get(path, params=params, operation="datacite_get")
        response.raise_for_status()
        return response.json()

    async def search(self, query: SearchQuery) -> ProviderBatch:
        terms = [self._escape_query(query.text)]
        if query.title:
            terms.append(f'titles.title:"{self._escape_query(query.title)}"')
        if query.author:
            terms.append(f'creators.name:"{self._escape_query(query.author)}"')
        if query.year_from is not None or query.year_to is not None:
            lower = query.year_from if query.year_from is not None else "*"
            upper = query.year_to if query.year_to is not None else "*"
            terms.append(f"publicationYear:[{lower} TO {upper}]")
        params = {
            "query": " AND ".join(terms),
            "page[size]": str(query.limit),
        }
        sort = {
            SearchSort.NEWEST: "-published",
            SearchSort.OLDEST: "published",
            SearchSort.CITATIONS: "-citation-count",
            SearchSort.RELEVANCE: "relevance",
        }[query.sort]
        params["sort"] = sort
        payload = await self._get("/dois", params)
        papers = [
            self._paper_from_resource(resource, rank=index + 1)
            for index, resource in enumerate(payload.get("data", []))
        ]
        total = self._int(payload.get("meta", {}).get("total"))
        execution = {"text": "provider"}
        for field, enabled in {
            "title": query.title is not None,
            "author": query.author is not None,
            "year_from": query.year_from is not None,
            "year_to": query.year_to is not None,
            "sort": query.sort != SearchSort.RELEVANCE,
        }.items():
            if enabled:
                execution[field] = "provider"
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(total is not None and total > len(papers)),
            filter_execution=execution,
        )

    async def resolve(self, identifier: str) -> Paper | None:
        doi = normalize_doi(identifier)
        if not doi:
            return None
        try:
            payload = await self._get(f"/dois/{quote(doi, safe='')}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        resource = payload.get("data")
        return self._paper_from_resource(resource) if isinstance(resource, dict) else None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        # DataCite exposes counts in DOI metadata, not a guaranteed citing-work list.
        return ProviderBatch()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _paper_from_resource(self, resource: dict[str, Any], rank: int | None = None) -> Paper:
        attributes = resource.get("attributes") or {}
        doi = normalize_doi(str(attributes.get("doi") or resource.get("id") or ""))
        if not doi:
            raise ValueError("DataCite resource has no valid DOI")
        source_url = attributes.get("url") or f"https://doi.org/{doi}"
        provenance = Provenance(
            provider=self.name,
            source_record_id=doi,
            source_url=source_url,
        )
        titles = attributes.get("titles") or []
        title = next(
            (str(item.get("title")).strip() for item in titles if item.get("title")),
            doi,
        )
        creators = attributes.get("creators") or []
        authors = [Author(name=str(item["name"]).strip()) for item in creators if item.get("name")]
        published = self._date(attributes.get("published"))
        publication_year = self._int(attributes.get("publicationYear"))
        descriptions = attributes.get("descriptions") or []
        abstract = next(
            (
                str(item.get("description")).strip()
                for item in descriptions
                if str(item.get("descriptionType") or "").casefold() == "abstract"
                and item.get("description")
            ),
            None,
        )
        types = attributes.get("types") or {}
        work_type = types.get("resourceTypeGeneral") or types.get("resourceType")
        citation_count = self._int(attributes.get("citationCount"))
        retrieved_at = utc_now()
        return Paper(
            record_id=f"datacite:{doi}",
            title=title,
            abstract=abstract,
            authors=authors,
            publication_date=published,
            publication_year=publication_year or (published.year if published else None),
            work_type=str(work_type).casefold() if work_type else None,
            venue=attributes.get("publisher"),
            landing_page_url=source_url,
            identifiers=[
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value=doi,
                    provenance=provenance,
                )
            ],
            citation_counts=(
                [
                    CitationCountClaim(
                        provider=self.name,
                        count=citation_count,
                        source_record_id=doi,
                        retrieved_at=retrieved_at,
                    )
                ]
                if citation_count is not None
                else []
            ),
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=doi,
                    source_url=source_url,
                    provider_rank=rank,
                    retrieved_at=retrieved_at,
                )
            ],
            field_provenance={
                field: [provenance]
                for field, value in {
                    "title": title,
                    "abstract": abstract,
                    "authors": authors,
                    "publication_date": published,
                    "publication_year": publication_year,
                    "work_type": work_type,
                    "venue": attributes.get("publisher"),
                }.items()
                if value not in (None, [], "")
            },
        )

    @staticmethod
    def _escape_query(value: str) -> str:
        return " ".join(value.replace("\\", " ").replace('"', " ").split())

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
