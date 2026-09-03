"""arXiv Atom API adapter for preprint search and identifier resolution."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import date, datetime

import httpx

from ..models import (
    Author,
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
    ProviderBatch,
    SearchQuery,
    SourceRecord,
    utc_now,
)
from ..normalization import normalize_arxiv_id, normalize_doi, normalize_text
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider

ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"
OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"
NS = {"atom": ATOM, "arxiv": ARXIV, "opensearch": OPENSEARCH}


class ArxivProvider(ScholarlyProvider):
    name = "arxiv"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="none",
        citations="none",
        related=False,
        fulltext=False,
        search_filter_execution={
            "text": "provider",
            "year_from": "provider",
            "year_to": "provider",
            "author": "provider",
            "open_access": "unsupported",
        },
        pagination={"search": "start_offset"},
        access_tier="public_rate_limited",
        credential_variables=["ARXIV_MAILTO"],
        terms_url="https://info.arxiv.org/help/api/tou.html",
        redistribution_policy="arxiv_api_terms_apply",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        mailto: str | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        contact = mailto or os.getenv("ARXIV_MAILTO")
        user_agent = "scholarly-retrieval-platform/0.1"
        if contact:
            user_agent += f" ({contact})"
        self._client = client or httpx.AsyncClient(
            base_url="https://export.arxiv.org",
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": user_agent},
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(
                cache_ttl_seconds=86400,
                min_interval_seconds=3.0,
                max_concurrency=1,
            ),
        )

    async def _query(self, params: dict[str, str], *, operation: str) -> ET.Element:
        response = await self._http.get("/api/query", params=params, operation=operation)
        response.raise_for_status()
        try:
            return ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise ValueError(f"invalid arXiv Atom response: {exc}") from exc

    async def search(self, query: SearchQuery) -> ProviderBatch:
        clauses = [f'all:"{self._escape_phrase(query.text)}"']
        execution = {"text": "provider"}
        if query.author:
            clauses.append(f'au:"{self._escape_phrase(query.author)}"')
            execution["author"] = "provider"
        if query.year_from is not None or query.year_to is not None:
            start = query.year_from or 1000
            end = query.year_to or 3000
            clauses.append(f"submittedDate:[{start}01010000 TO {end}12312359]")
            if query.year_from is not None:
                execution["year_from"] = "provider"
            if query.year_to is not None:
                execution["year_to"] = "provider"
        if query.open_access is not None:
            execution["open_access"] = "unsupported"
        root = await self._query(
            {
                "search_query": " AND ".join(clauses),
                "start": "0",
                "max_results": str(query.limit),
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
            operation="search",
        )
        papers = [
            self._paper_from_entry(entry, rank=index + 1)
            for index, entry in enumerate(root.findall("atom:entry", NS))
        ]
        total = self._int_text(root.find("opensearch:totalResults", NS))
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(total is not None and total > len(papers)),
            filter_execution=execution,
        )

    async def resolve(self, identifier: str) -> Paper | None:
        # Keeping a requested vN asks arXiv for that exact manifestation; omitting
        # it deliberately resolves the latest available version.
        arxiv_id = normalize_arxiv_id(identifier, keep_version=True)
        if not arxiv_id:
            return None
        root = await self._query({"id_list": arxiv_id, "max_results": "1"}, operation="resolve")
        entry = root.find("atom:entry", NS)
        if entry is None or self._is_error_entry(entry):
            return None
        return self._paper_from_entry(entry)

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _paper_from_entry(self, entry: ET.Element, rank: int | None = None) -> Paper:
        source_url = self._text(entry.find("atom:id", NS))
        arxiv_id = normalize_arxiv_id(source_url or "", keep_version=True)
        if not arxiv_id:
            raise ValueError(f"invalid arXiv entry id: {source_url}")
        family_id = normalize_arxiv_id(arxiv_id, keep_version=False)
        retrieved_at = utc_now()
        provenance = Provenance(
            provider=self.name,
            source_record_id=arxiv_id,
            source_url=source_url,
            retrieved_at=retrieved_at,
        )
        identifiers = [
            IdentifierClaim(
                scheme=IdentifierScheme.ARXIV,
                value=family_id or arxiv_id,
                provenance=provenance,
            )
        ]
        doi = normalize_doi(self._text(entry.find("arxiv:doi", NS)) or "")
        if doi:
            identifiers.append(
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value=doi,
                    provenance=provenance,
                )
            )
        authors = []
        for author in entry.findall("atom:author", NS):
            name = self._text(author.find("atom:name", NS))
            if name:
                authors.append(Author(name=normalize_text(name)))
        links = entry.findall("atom:link", NS)
        pdf_url = next(
            (
                link.attrib.get("href")
                for link in links
                if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf"
            ),
            None,
        )
        primary = entry.find("arxiv:primary_category", NS)
        category = primary.attrib.get("term") if primary is not None else None
        publication_date = self._parse_date(self._text(entry.find("atom:published", NS)))
        return Paper(
            record_id=f"arxiv:{arxiv_id}",
            work_family_id=f"arxiv:{family_id}" if family_id else None,
            title=self._text(entry.find("atom:title", NS)) or f"Untitled {arxiv_id}",
            abstract=self._text(entry.find("atom:summary", NS)),
            authors=authors,
            publication_date=publication_date,
            publication_year=publication_date.year if publication_date else None,
            work_type="preprint",
            venue=category,
            fields_of_study=[category] if category else [],
            open_access=True,
            landing_page_url=source_url,
            pdf_url=pdf_url,
            identifiers=identifiers,
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=arxiv_id,
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
                    "open_access",
                ]
            },
        )

    @staticmethod
    def _text(element: ET.Element | None) -> str | None:
        if element is None or element.text is None:
            return None
        value = normalize_text(element.text)
        return value or None

    @staticmethod
    def _int_text(element: ET.Element | None) -> int | None:
        try:
            return int(element.text) if element is not None and element.text else None
        except ValueError:
            return None

    @classmethod
    def _is_error_entry(cls, entry: ET.Element) -> bool:
        entry_id = cls._text(entry.find("atom:id", NS)) or ""
        return entry_id.startswith("http://arxiv.org/api/errors")

    @staticmethod
    def _parse_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None

    @staticmethod
    def _escape_phrase(value: str) -> str:
        return normalize_text(value).replace("\\", "\\\\").replace('"', '\\"')
