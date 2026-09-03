"""OpenReview submission metadata adapter using the official API v2."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin

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
)
from ..normalization import (
    normalize_arxiv_id,
    normalize_doi,
    normalize_openreview_id,
)
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider


class OpenReviewProvider(ScholarlyProvider):
    """Retrieve public OpenReview forum notes without requiring an API key."""

    name = "openreview"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="none",
        citations="none",
        search_filter_execution={"text": "provider"},
        pagination={"search": "offset"},
        access_tier="public_rate_limited",
        terms_url="https://openreview.net/legal/terms",
        redistribution_policy="note_license_and_openreview_terms_apply",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://api2.openreview.net",
            timeout=httpx.Timeout(25.0),
            headers={"User-Agent": "scholarly-retrieval-platform/0.1"},
        )
        # The public endpoint currently advertises a small per-minute budget.
        # Serial pacing keeps Agent fan-out from creating an avoidable burst.
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(
                cache_ttl_seconds=3600,
                max_concurrency=1,
                min_interval_seconds=3.1,
            ),
        )

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        response = await self._http.get(path, params=params, operation="openreview_get")
        response.raise_for_status()
        return response.json()

    async def search(self, query: SearchQuery) -> ProviderBatch:
        payload = await self._get(
            "/notes/search",
            {
                "query": query.text,
                # Forum notes are submissions; omitting this also returns reviews
                # and comments, which are not scholarly works.
                "source": "forum",
                "limit": str(query.limit),
                "offset": "0",
                "count": "true",
            },
        )
        raw_notes = payload.get("notes") or []
        papers = [
            self._paper_from_note(note, rank=index + 1)
            for index, note in enumerate(raw_notes)
            if isinstance(note, dict) and self._value(note.get("content"), "title")
        ]
        total = self._int(payload.get("count"))
        truncated = bool(total is not None and total > len(papers))
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=truncated,
            next_cursor=str(len(raw_notes)) if truncated else None,
            filter_execution={"text": "provider"},
        )

    async def resolve(self, identifier: str) -> Paper | None:
        openreview_id = normalize_openreview_id(identifier)
        if openreview_id:
            try:
                payload = await self._get("/notes", {"id": openreview_id})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return None
                raise
            notes = payload.get("notes") or []
            return self._paper_from_note(notes[0]) if notes else None

        # Some archival notes contain a DOI. OpenReview has no DOI lookup route,
        # so use its documented full-text search and require an exact DOI claim.
        doi = normalize_doi(identifier)
        if doi:
            batch = await self.search(SearchQuery(text=doi, limit=20))
            return next(
                (
                    paper
                    for paper in batch.papers
                    if any(
                        claim.scheme == IdentifierScheme.DOI and claim.value == doi
                        for claim in paper.identifiers
                    )
                ),
                None,
            )
        return None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _paper_from_note(self, note: dict[str, Any], rank: int | None = None) -> Paper:
        note_id = str(note.get("id") or note.get("forum") or "unknown")
        forum_id = str(note.get("forum") or note_id)
        source_url = f"https://openreview.net/forum?id={forum_id}"
        provenance = Provenance(
            provider=self.name,
            source_record_id=note_id,
            source_url=source_url,
        )
        content = note.get("content") if isinstance(note.get("content"), dict) else {}
        title = str(self._value(content, "title") or note_id).strip()
        abstract_value = self._value(content, "abstract")
        abstract = str(abstract_value).strip() if abstract_value else None

        author_values = self._as_list(self._value(content, "authors"))
        author_ids = self._as_list(self._value(content, "authorids"))
        authors = []
        for index, name in enumerate(author_values):
            normalized_name = str(name).strip()
            if not normalized_name:
                continue
            provider_ids = {}
            if index < len(author_ids) and str(author_ids[index]).strip():
                provider_ids["openreview"] = str(author_ids[index]).strip()
            authors.append(Author(name=normalized_name, provider_ids=provider_ids))

        published = self._date_from_millis(
            note.get("pdate") or note.get("cdate") or note.get("tcdate")
        )
        venue_value = self._value(content, "venue") or self._value(content, "venueid")
        venue = str(venue_value).strip() if venue_value else None
        keywords = [
            str(item).strip()
            for item in self._as_list(self._value(content, "keywords"))
            if str(item).strip()
        ]
        pdf_value = self._value(content, "pdf")
        pdf_url = urljoin("https://openreview.net", str(pdf_value)) if pdf_value else None

        identifiers = [
            IdentifierClaim(
                scheme=IdentifierScheme.OPENREVIEW,
                value=note_id,
                provenance=provenance,
            )
        ]
        seen = {(IdentifierScheme.OPENREVIEW, note_id)}
        for key, scheme, normalizer in (
            ("doi", IdentifierScheme.DOI, normalize_doi),
            ("arxiv", IdentifierScheme.ARXIV, normalize_arxiv_id),
            ("arxiv_id", IdentifierScheme.ARXIV, normalize_arxiv_id),
        ):
            for raw_value in self._as_list(self._value(content, key)):
                value = normalizer(str(raw_value))
                if value and (scheme, value) not in seen:
                    seen.add((scheme, value))
                    identifiers.append(
                        IdentifierClaim(scheme=scheme, value=value, provenance=provenance)
                    )

        retrieval_context = {
            key: value
            for key, value in {
                "forum_id": forum_id,
                "venue_id": self._value(content, "venueid"),
                "invitation": note.get("invitation"),
                "license": note.get("license") or self._value(content, "license"),
            }.items()
            if value is not None
        }
        field_values = {
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "publication_date": published,
            "publication_year": published.year if published else None,
            "venue": venue,
            "fields_of_study": keywords,
            "open_access": bool(pdf_url),
            "landing_page_url": source_url,
            "pdf_url": pdf_url,
        }
        return Paper(
            record_id=f"openreview:{note_id}",
            title=title,
            abstract=abstract,
            authors=authors,
            publication_date=published,
            publication_year=published.year if published else None,
            work_type="conference-paper" if venue else "preprint",
            venue=venue,
            fields_of_study=keywords,
            open_access=bool(pdf_url),
            landing_page_url=source_url,
            pdf_url=pdf_url,
            identifiers=identifiers,
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=note_id,
                    source_url=source_url,
                    provider_rank=rank,
                    retrieval_context=retrieval_context,
                )
            ],
            field_provenance={
                field: [provenance] for field, value in field_values.items() if value is not None
            },
        )

    @staticmethod
    def _value(content: Any, key: str) -> Any:
        if not isinstance(content, dict):
            return None
        value = content.get(key)
        # API v2 wraps content values so edit metadata can live beside the value.
        if isinstance(value, dict) and "value" in value:
            return value["value"]
        return value

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _date_from_millis(value: Any) -> date | None:
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=UTC).date()
        except (TypeError, ValueError, OSError, OverflowError):
            return None
