"""OpenAIRE Graph v3 adapter for broad repository and publication discovery."""

from __future__ import annotations

import re
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
from ..normalization import normalize_arxiv_id, normalize_doi
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider


class OpenAireProvider(ScholarlyProvider):
    """Search the multidisciplinary OpenAIRE Graph without an API key."""

    name = "openaire"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="none",
        citations="none",
        search_filter_execution={
            "text": "provider",
            "title": "provider",
            "author": "provider",
            "year_from": "provider",
            "year_to": "provider",
            "sort": "provider",
        },
        pagination={"search": "cursor_or_page"},
        access_tier="public_optional_auth",
        terms_url="https://graph.openaire.eu/docs/apis/graph-api/overview/",
        redistribution_policy="openaire_graph_terms_apply",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://api.openaire.eu",
            timeout=httpx.Timeout(25.0),
            headers={"User-Agent": "scholarly-retrieval-platform/0.1"},
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(cache_ttl_seconds=3600, max_concurrency=2),
        )

    def plan_search_filter_execution(self, query: SearchQuery) -> dict[str, str]:
        execution = dict(self.capabilities.search_filter_execution)
        if query.open_access is True:
            execution["open_access"] = "provider"
        elif query.open_access is False:
            # "Not open" includes more states than the API's Closed Access label.
            execution["open_access"] = "local"
        return execution

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = await self._http.get(path, params=params, operation="openaire_get")
        response.raise_for_status()
        return response.json()

    async def search(self, query: SearchQuery) -> ProviderBatch:
        params = {
            "search": query.text,
            "type": "publication",
            "pageSize": str(query.limit),
        }
        execution = {"text": "provider"}
        for field, api_name, value in (
            ("title", "mainTitle", query.title),
            ("author", "authorFullName", query.author),
            ("year_from", "fromPublicationYear", query.year_from),
            ("year_to", "toPublicationYear", query.year_to),
        ):
            if value is not None:
                params[api_name] = str(value)
                execution[field] = "provider"
        if query.open_access is True:
            params["accessRightLabel"] = "Open Access"
            execution["open_access"] = "provider"
        sort = {
            SearchSort.RELEVANCE: "relevance DESC",
            SearchSort.NEWEST: "publicationDate DESC",
            SearchSort.OLDEST: "publicationDate ASC",
            SearchSort.CITATIONS: "citationCount DESC",
        }[query.sort]
        params["sortBy"] = sort
        if query.sort != SearchSort.RELEVANCE:
            execution["sort"] = "provider"

        payload = await self._get("/graph/v3/research-products", params)
        header = payload.get("header") or {}
        resources = payload.get("results") or []
        papers = [
            self._paper_from_resource(resource, rank=index + 1)
            for index, resource in enumerate(resources)
            if isinstance(resource, dict) and resource.get("mainTitle")
        ]
        total = self._int(header.get("numFound"))
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(total is not None and total > len(papers)),
            next_cursor=header.get("nextCursor"),
            filter_execution=execution,
        )

    async def resolve(self, identifier: str) -> Paper | None:
        normalized = identifier.strip()
        if normalized.casefold().startswith("openaire:"):
            record_id = normalized[len("openaire:") :]
            if not record_id:
                return None
            try:
                payload = await self._get(
                    f"/graph/v3/research-products/{quote(record_id, safe=':_')}"
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return None
                raise
            return self._paper_from_resource(payload)

        pid = self._persistent_id(identifier)
        if pid is None:
            return None
        payload = await self._get(
            "/graph/v3/research-products",
            {"pid": pid, "type": "publication", "pageSize": "10"},
        )
        expected_doi = normalize_doi(identifier)
        expected_arxiv = normalize_arxiv_id(identifier, keep_version=False)
        for resource in payload.get("results") or []:
            paper = self._paper_from_resource(resource)
            if expected_doi and self._has_claim(paper, IdentifierScheme.DOI, expected_doi):
                return paper
            if expected_arxiv and self._has_claim(paper, IdentifierScheme.ARXIV, expected_arxiv):
                return paper
            prefix, _, value = identifier.strip().partition(":")
            scheme = {"pmid": IdentifierScheme.PMID, "pmcid": IdentifierScheme.PMCID}.get(
                prefix.casefold()
            )
            if scheme and self._has_claim(paper, scheme, value):
                return paper
        return None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _paper_from_resource(self, resource: dict[str, Any], rank: int | None = None) -> Paper:
        record_id = str(resource.get("id") or "unknown")
        encoded_record_id = quote(record_id, safe=":_")
        source_url = (
            f"https://api.openaire.eu/graph/v3/research-products/{encoded_record_id}"
        )
        provenance = Provenance(
            provider=self.name,
            source_record_id=record_id,
            source_url=source_url,
        )
        title = str(resource.get("mainTitle") or record_id).strip()
        authors = [
            Author(name=str(item.get("fullName")).strip())
            for item in resource.get("authors") or []
            if isinstance(item, dict) and item.get("fullName")
        ]
        publication_date = self._date(resource.get("publicationDate"))
        descriptions = resource.get("descriptions") or []
        if isinstance(descriptions, str):
            descriptions = [descriptions]
        abstract = next((str(value).strip() for value in descriptions if str(value).strip()), None)
        instances = [item for item in resource.get("instances") or [] if isinstance(item, dict)]
        container = resource.get("container") or {}
        venue = container.get("name") or resource.get("publisher")
        access_label = str((resource.get("bestAccessRight") or {}).get("label") or "")
        citation_count = self._int(
            ((resource.get("indicators") or {}).get("citationImpact") or {}).get(
                "citationCount"
            )
        )
        identifiers = [
            IdentifierClaim(
                scheme=IdentifierScheme.OPENAIRE,
                value=record_id,
                provenance=provenance,
            )
        ]
        for item in resource.get("pids") or []:
            if not isinstance(item, dict):
                continue
            scheme = {
                "doi": IdentifierScheme.DOI,
                "arxiv": IdentifierScheme.ARXIV,
                "pmid": IdentifierScheme.PMID,
                "pmc": IdentifierScheme.PMCID,
                "pmcid": IdentifierScheme.PMCID,
            }.get(str(item.get("scheme") or "").casefold())
            value = str(item.get("value") or "").strip()
            if scheme == IdentifierScheme.DOI:
                value = normalize_doi(value) or ""
            elif scheme == IdentifierScheme.ARXIV:
                value = normalize_arxiv_id(value, keep_version=False) or ""
            if scheme is not None and value:
                identifiers.append(
                    IdentifierClaim(scheme=scheme, value=value, provenance=provenance)
                )
        landing_page = next(
            (
                str(url)
                for instance in instances
                for url in instance.get("urls") or []
                if str(url).startswith(("http://", "https://"))
            ),
            source_url,
        )
        retrieved_at = utc_now()
        work_type = next(
            (str(item.get("type")).casefold() for item in instances if item.get("type")),
            str(resource.get("type") or "publication").casefold(),
        )
        return Paper(
            record_id=f"openaire:{record_id}",
            title=title,
            abstract=abstract,
            authors=authors,
            publication_date=publication_date,
            publication_year=(
                publication_date.year
                if publication_date
                else self._year(resource.get("publicationDate"))
            ),
            work_type=work_type,
            venue=venue,
            open_access=("OPEN" in access_label.upper()) if access_label else None,
            landing_page_url=landing_page,
            identifiers=identifiers,
            citation_counts=(
                [
                    CitationCountClaim(
                        provider=self.name,
                        count=citation_count,
                        source_record_id=record_id,
                        retrieved_at=retrieved_at,
                    )
                ]
                if citation_count is not None
                else []
            ),
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=record_id,
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
                    "publication_date": publication_date,
                    "work_type": work_type,
                    "venue": venue,
                    "open_access": access_label,
                }.items()
                if value not in (None, [], "")
            },
        )

    @staticmethod
    def _persistent_id(identifier: str) -> str | None:
        if doi := normalize_doi(identifier):
            return doi
        if arxiv := normalize_arxiv_id(identifier, keep_version=False):
            return arxiv
        prefix, separator, value = identifier.strip().partition(":")
        if separator and prefix.casefold() in {"pmid", "pmcid"} and value:
            return value
        return None

    @staticmethod
    def _has_claim(paper: Paper, scheme: IdentifierScheme, value: str) -> bool:
        return any(claim.scheme == scheme and claim.value == value for claim in paper.identifiers)

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _date(value: Any) -> date | None:
        text = str(value or "")
        try:
            return date.fromisoformat(text[:10]) if len(text) >= 10 else None
        except ValueError:
            return None

    @staticmethod
    def _year(value: Any) -> int | None:
        match = re.match(r"(1\d{3}|2\d{3})", str(value or ""))
        return int(match.group(1)) if match else None
