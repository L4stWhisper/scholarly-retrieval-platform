"""Crossref REST adapter for publication metadata and deposited references."""

from __future__ import annotations

import html
import os
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
    SourceRecord,
    UnresolvedReference,
    utc_now,
)
from ..normalization import normalize_doi, normalize_text
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider


class CrossrefProvider(ScholarlyProvider):
    """Crossref exposes deposited outgoing references, but only incoming counts."""

    name = "crossref"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="list",
        citations="count",
        related=False,
        fulltext=False,
        search_filter_execution={
            "text": "provider",
            "year_from": "provider",
            "year_to": "provider",
            "author": "provider",
            "open_access": "unsupported",
        },
        pagination={"search": "offset", "references": "embedded_bounded_list"},
        access_tier="public_polite_pool",
        credential_variables=["CROSSREF_MAILTO"],
        terms_url="https://www.crossref.org/documentation/retrieve-metadata/rest-api/",
        redistribution_policy="metadata_license_varies_by_field",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        mailto: str | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        self._mailto = mailto or os.getenv("CROSSREF_MAILTO")
        user_agent = "scholarly-retrieval-platform/0.1"
        if self._mailto:
            user_agent += f" (mailto:{self._mailto})"
        self._client = client or httpx.AsyncClient(
            base_url="https://api.crossref.org",
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": user_agent},
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(cache_ttl_seconds=86400, max_concurrency=2),
        )

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        operation: str = "get",
    ) -> dict[str, Any]:
        merged = dict(params or {})
        if self._mailto:
            merged["mailto"] = self._mailto
        response = await self._http.get(path, params=merged, operation=operation)
        response.raise_for_status()
        return response.json()

    async def search(self, query: SearchQuery) -> ProviderBatch:
        params = {
            "query.bibliographic": query.text,
            "rows": str(query.limit),
        }
        filters: list[str] = []
        execution = {"text": "provider"}
        if query.year_from is not None:
            filters.append(f"from-pub-date:{query.year_from}-01-01")
            execution["year_from"] = "provider"
        if query.year_to is not None:
            filters.append(f"until-pub-date:{query.year_to}-12-31")
            execution["year_to"] = "provider"
        if filters:
            params["filter"] = ",".join(filters)
        if query.author:
            params["query.author"] = query.author
            execution["author"] = "provider"
        if query.open_access is not None:
            # Crossref licenses are not a reliable open-access boolean.
            execution["open_access"] = "unsupported"
        payload = await self._get("/v1/works", params, operation="search")
        message = payload.get("message") or {}
        items = message.get("items") or []
        papers = [
            self._paper_from_item(item, rank=index + 1)
            for index, item in enumerate(items)
            if item.get("DOI")
        ]
        total = message.get("total-results")
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(total is not None and total > len(items)),
            filter_execution=execution,
        )

    async def resolve(self, identifier: str) -> Paper | None:
        doi = normalize_doi(identifier)
        if not doi:
            return None
        try:
            payload = await self._get(f"/v1/works/{quote(doi, safe='')}", operation="resolve")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        item = payload.get("message") or {}
        return self._paper_from_item(item) if item.get("DOI") else None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        doi = normalize_doi(identifier)
        if not doi:
            return ProviderBatch()
        try:
            payload = await self._get(f"/v1/works/{quote(doi, safe='')}", operation="references")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return ProviderBatch()
            raise
        item = payload.get("message") or {}
        deposited = item.get("reference") or []
        papers: list[Paper] = []
        unresolved: list[UnresolvedReference] = []
        for ordinal, raw in enumerate(deposited[:limit], start=1):
            reference_doi = normalize_doi(str(raw.get("DOI") or ""))
            if reference_doi:
                papers.append(self._paper_from_reference(raw, doi, ordinal, reference_doi))
            else:
                # Deposited references are optional and often unstructured. Keep
                # the original item as evidence instead of inventing a Paper ID.
                provenance = Provenance(
                    provider=self.name,
                    source_record_id=f"{doi}#reference-{ordinal}",
                    source_url=f"https://doi.org/{doi}",
                )
                unresolved.append(
                    UnresolvedReference(
                        provider=self.name,
                        seed_record_id=doi,
                        ordinal=ordinal,
                        raw=raw,
                        title=self._clean_text(raw.get("article-title")),
                        author=self._clean_text(raw.get("author")),
                        publication_year=self._parse_year(raw.get("year")),
                        reason="missing_strong_identifier",
                        provenance=provenance,
                    )
                )
        return ProviderBatch(
            papers=papers,
            unresolved_references=unresolved,
            total_available=len(deposited),
            truncated=len(deposited) > limit,
        )

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        # Capability routing prevents this method from being used for list retrieval.
        return ProviderBatch()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _paper_from_item(self, item: dict[str, Any], rank: int | None = None) -> Paper:
        retrieved_at = utc_now()
        doi = normalize_doi(str(item["DOI"]))
        if not doi:
            raise ValueError(f"invalid Crossref DOI: {item['DOI']}")
        source_url = item.get("URL") or f"https://doi.org/{doi}"
        provenance = Provenance(
            provider=self.name,
            source_record_id=doi,
            retrieved_at=retrieved_at,
            source_url=source_url,
        )
        authors = []
        for author in item.get("author") or []:
            name = normalize_text(
                " ".join(filter(None, [author.get("given"), author.get("family")]))
            )
            if name:
                authors.append(Author(name=name, orcid=author.get("ORCID")))
        publication_date = self._date_from_item(item)
        title = self._first_text(item.get("title")) or f"Untitled {doi}"
        links = item.get("link") or []
        pdf_url = next(
            (
                link.get("URL")
                for link in links
                if "pdf" in str(link.get("content-type") or "").lower()
            ),
            None,
        )
        return Paper(
            record_id=f"crossref:{doi}",
            title=title,
            abstract=self._strip_markup(item.get("abstract")),
            authors=authors,
            publication_date=publication_date,
            publication_year=publication_date.year if publication_date else None,
            work_type=item.get("type"),
            venue=self._first_text(item.get("container-title")),
            fields_of_study=[self._text(value) for value in item.get("subject") or []],
            language=item.get("language"),
            landing_page_url=source_url,
            pdf_url=pdf_url,
            identifiers=[
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value=doi,
                    provenance=provenance,
                )
            ],
            citation_counts=[
                CitationCountClaim(
                    provider=self.name,
                    count=max(0, int(item.get("is-referenced-by-count") or 0)),
                    source_record_id=doi,
                    retrieved_at=retrieved_at,
                )
            ],
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
                for field in [
                    "title",
                    "abstract",
                    "authors",
                    "publication_date",
                    "work_type",
                    "venue",
                ]
            },
        )

    def _paper_from_reference(
        self, raw: dict[str, Any], seed_doi: str, ordinal: int, doi: str
    ) -> Paper:
        record_id = f"{seed_doi}#reference-{ordinal}"
        provenance = Provenance(
            provider=self.name,
            source_record_id=record_id,
            source_url=f"https://doi.org/{doi}",
        )
        title = (
            self._clean_text(raw.get("article-title"))
            or self._clean_text(raw.get("unstructured"))
            or f"DOI {doi}"
        )
        author = self._clean_text(raw.get("author"))
        year = self._parse_year(raw.get("year"))
        return Paper(
            record_id=f"crossref-reference:{record_id}",
            title=title,
            authors=[Author(name=author)] if author else [],
            publication_year=year,
            landing_page_url=f"https://doi.org/{doi}",
            identifiers=[
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value=doi,
                    provenance=provenance,
                )
            ],
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=record_id,
                    source_url=f"https://doi.org/{doi}",
                )
            ],
            field_provenance={"title": [provenance]},
        )

    @classmethod
    def _strip_markup(cls, value: Any) -> str | None:
        if not value:
            return None
        return cls._clean_text(html.unescape(re.sub(r"<[^>]+>", " ", str(value))))

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        if value is None:
            return None
        cleaned = normalize_text(html.unescape(str(value)))
        return cleaned or None

    @classmethod
    def _first_text(cls, value: Any) -> str | None:
        if isinstance(value, list):
            value = value[0] if value else None
        return cls._clean_text(value)

    @staticmethod
    def _parse_year(value: Any) -> int | None:
        try:
            year = int(value)
        except (TypeError, ValueError):
            return None
        return year if 1000 <= year <= 3000 else None

    @classmethod
    def _date_from_item(cls, item: dict[str, Any]) -> date | None:
        for key in ("published-print", "published-online", "published", "issued", "created"):
            value = item.get(key) or {}
            parts_list = value.get("date-parts") or []
            if not parts_list:
                continue
            parts = parts_list[0]
            try:
                return date(
                    int(parts[0]),
                    int(parts[1]) if len(parts) > 1 else 1,
                    int(parts[2]) if len(parts) > 2 else 1,
                )
            except (TypeError, ValueError, IndexError):
                continue
        return None
