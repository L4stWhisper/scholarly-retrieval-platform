"""INSPIRE HEP adapter for physics literature and citation relations."""

from __future__ import annotations

import hashlib
import json
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
    UnresolvedReference,
    utc_now,
)
from ..normalization import normalize_arxiv_id, normalize_doi
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider


class InspireProvider(ScholarlyProvider):
    """Use INSPIRE's public read-only API for high-energy physics records."""

    name = "inspire"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=False,
        resolve_id=True,
        references="list",
        citations="list",
        search_filter_execution={"text": "provider", "sort": "provider"},
        pagination={
            "search": "page_number",
            "references": "embedded",
            "citations": "page_number",
        },
        access_tier="public_no_key",
        terms_url="https://github.com/inspirehep/rest-api-doc",
        redistribution_policy="CC0_most_metadata_restricted_fields_apply",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://inspirehep.net",
            timeout=httpx.Timeout(25.0),
            headers={"User-Agent": "scholarly-retrieval-platform/0.1"},
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            # INSPIRE documents 15 requests per five-second IP window. A small
            # connector-local concurrency bound complements the shared retry layer.
            policy=RetryPolicy(cache_ttl_seconds=3600, max_concurrency=2),
        )

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = await self._http.get(path, params=params, operation="inspire_get")
        response.raise_for_status()
        return response.json()

    async def search(self, query: SearchQuery) -> ProviderBatch:
        params = {"q": query.text, "size": str(query.limit)}
        if query.sort == SearchSort.NEWEST:
            params["sort"] = "mostrecent"
        elif query.sort == SearchSort.CITATIONS:
            params["sort"] = "mostcited"
        payload = await self._get("/api/literature", params)
        return self._batch_from_search(payload, filter_execution={"text": "provider"})

    async def resolve(self, identifier: str) -> Paper | None:
        payload = await self._resolve_payload(identifier)
        if payload is None:
            return None
        return self._paper_from_hit(payload)

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        payload = await self._resolve_payload(identifier)
        if payload is None:
            return ProviderBatch()
        references = (payload.get("metadata") or {}).get("references") or []
        selected = references[:limit]
        papers: list[Paper] = []
        unresolved: list[UnresolvedReference] = []
        seed_record_id = str(payload.get("id") or "unknown")
        for ordinal, item in enumerate(selected, start=1):
            if not isinstance(item, dict):
                continue
            if self._reference_has_strong_id(item):
                papers.append(self._paper_from_reference(item, ordinal=ordinal))
            else:
                metadata = item.get("reference") or item
                titles = metadata.get("titles") or []
                authors = metadata.get("authors") or []
                publication_info = metadata.get("publication_info") or []
                year = next(
                    (
                        self._int(info.get("year"))
                        for info in publication_info
                        if isinstance(info, dict) and self._int(info.get("year"))
                    ),
                    None,
                )
                provenance = Provenance(
                    provider=self.name,
                    source_record_id=f"{seed_record_id}#reference-{ordinal}",
                    source_url=f"https://inspirehep.net/literature/{seed_record_id}",
                )
                unresolved.append(
                    UnresolvedReference(
                        provider=self.name,
                        seed_record_id=seed_record_id,
                        ordinal=ordinal,
                        raw=item,
                        title=next(
                            (
                                str(title.get("title")).strip()
                                for title in titles
                                if isinstance(title, dict) and title.get("title")
                            ),
                            None,
                        ),
                        author=next(
                            (
                                str(author.get("full_name")).strip()
                                for author in authors
                                if isinstance(author, dict) and author.get("full_name")
                            ),
                            None,
                        ),
                        publication_year=year,
                        reason="missing_strong_identifier",
                        provenance=provenance,
                    )
                )
        return ProviderBatch(
            papers=papers,
            unresolved_references=unresolved,
            total_available=len(references),
            truncated=len(references) > len(selected),
        )

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        payload = await self._resolve_payload(identifier)
        if payload is None:
            return ProviderBatch()
        record_id = str(payload.get("id") or "")
        if not record_id:
            return ProviderBatch()
        citing = await self._get(
            "/api/literature",
            {"q": f"refersto:recid:{record_id}", "size": str(limit)},
        )
        return self._batch_from_search(citing)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _resolve_payload(self, identifier: str) -> dict[str, Any] | None:
        normalized = identifier.strip()
        if doi := normalize_doi(normalized):
            path = f"/api/doi/{quote(doi, safe='/')}"
        elif arxiv := normalize_arxiv_id(normalized, keep_version=False):
            path = f"/api/arxiv/{quote(arxiv, safe='/')}"
        elif normalized.casefold().startswith("inspire:"):
            record_id = normalized[len("inspire:") :]
            if not record_id.isdigit():
                return None
            path = f"/api/literature/{record_id}"
        else:
            return None
        try:
            return await self._get(path)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def _batch_from_search(
        self,
        payload: dict[str, Any],
        *,
        filter_execution: dict[str, str] | None = None,
    ) -> ProviderBatch:
        hits_block = payload.get("hits") or {}
        hits = hits_block.get("hits") or []
        total_value = hits_block.get("total")
        if isinstance(total_value, dict):
            total_value = total_value.get("value")
        total = self._int(total_value)
        papers = [
            self._paper_from_hit(hit, rank=index + 1)
            for index, hit in enumerate(hits)
            if isinstance(hit, dict)
        ]
        next_url = (payload.get("links") or {}).get("next")
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(total is not None and total > len(papers)),
            next_cursor=str(next_url) if next_url else None,
            filter_execution=filter_execution or {},
        )

    def _paper_from_hit(self, hit: dict[str, Any], rank: int | None = None) -> Paper:
        metadata = hit.get("metadata") or {}
        record_id = str(hit.get("id") or metadata.get("control_number") or "unknown")
        return self._paper_from_metadata(metadata, record_id=record_id, rank=rank)

    def _paper_from_reference(self, item: dict[str, Any], *, ordinal: int) -> Paper:
        metadata = item.get("reference") or item
        record_url = str((item.get("record") or {}).get("$ref") or "")
        record_id = record_url.rstrip("/").rsplit("/", 1)[-1] if record_url else ""
        if not record_id:
            # Some references have bibliographic metadata but no linked INSPIRE
            # record. A content hash keeps those candidates stable and auditable.
            serialized = json.dumps(item, sort_keys=True, ensure_ascii=False)
            record_id = "unlinked-" + hashlib.sha256(serialized.encode()).hexdigest()[:16]
        return self._paper_from_metadata(
            metadata,
            record_id=record_id,
            rank=ordinal,
            retrieval_context={"relation": "reference", "linked": bool(record_url)},
        )

    @staticmethod
    def _reference_has_strong_id(item: dict[str, Any]) -> bool:
        if (item.get("record") or {}).get("$ref"):
            return True
        metadata = item.get("reference") or item
        if any(
            normalize_doi(str(value.get("value") or ""))
            for value in metadata.get("dois") or []
            if isinstance(value, dict)
        ):
            return True
        return any(
            normalize_arxiv_id(str(value.get("value") or ""), keep_version=False)
            for value in metadata.get("arxiv_eprints") or []
            if isinstance(value, dict)
        )

    def _paper_from_metadata(
        self,
        metadata: dict[str, Any],
        *,
        record_id: str,
        rank: int | None = None,
        retrieval_context: dict[str, Any] | None = None,
    ) -> Paper:
        source_url = f"https://inspirehep.net/literature/{record_id}"
        provenance = Provenance(
            provider=self.name,
            source_record_id=record_id,
            source_url=source_url,
        )
        titles = metadata.get("titles") or []
        title = next(
            (str(item.get("title")).strip() for item in titles if item.get("title")),
            f"INSPIRE reference {record_id}",
        )
        authors = [
            Author(name=str(item.get("full_name")).strip())
            for item in metadata.get("authors") or []
            if isinstance(item, dict) and item.get("full_name")
        ]
        abstracts = metadata.get("abstracts") or []
        abstract = next(
            (str(item.get("value")).strip() for item in abstracts if item.get("value")),
            None,
        )
        publication_info = metadata.get("publication_info") or []
        first_publication = next(
            (item for item in publication_info if isinstance(item, dict)),
            {},
        )
        year = self._int(first_publication.get("year")) or self._year(
            metadata.get("earliest_date")
        )
        publication_date = self._date(metadata.get("earliest_date"))
        venue = first_publication.get("journal_title") or first_publication.get(
            "conference_title"
        )
        identifiers = [
            IdentifierClaim(
                scheme=IdentifierScheme.INSPIRE,
                value=record_id,
                provenance=provenance,
            )
        ]
        for item in metadata.get("dois") or []:
            doi = normalize_doi(str(item.get("value") or "")) if isinstance(item, dict) else None
            if doi:
                identifiers.append(
                    IdentifierClaim(
                        scheme=IdentifierScheme.DOI,
                        value=doi,
                        provenance=provenance,
                    )
                )
        for item in metadata.get("arxiv_eprints") or []:
            arxiv = (
                normalize_arxiv_id(str(item.get("value") or ""), keep_version=False)
                if isinstance(item, dict)
                else None
            )
            if arxiv:
                identifiers.append(
                    IdentifierClaim(
                        scheme=IdentifierScheme.ARXIV,
                        value=arxiv,
                        provenance=provenance,
                    )
                )
        document_types = metadata.get("document_type") or []
        work_type = str(document_types[0]).casefold() if document_types else None
        citation_count = self._int(metadata.get("citation_count"))
        retrieved_at = utc_now()
        return Paper(
            record_id=f"inspire:{record_id}",
            title=title,
            abstract=abstract,
            authors=authors,
            publication_date=publication_date,
            publication_year=year or (publication_date.year if publication_date else None),
            work_type=work_type,
            venue=venue,
            landing_page_url=source_url,
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
                    retrieval_context=retrieval_context or {},
                )
            ],
            field_provenance={
                field: [provenance]
                for field, value in {
                    "title": title,
                    "abstract": abstract,
                    "authors": authors,
                    "publication_date": publication_date,
                    "publication_year": year,
                    "work_type": work_type,
                    "venue": venue,
                }.items()
                if value not in (None, [], "")
            },
        )

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _year(value: Any) -> int | None:
        match = re.match(r"(1\d{3}|2\d{3})", str(value or ""))
        return int(match.group(1)) if match else None

    @staticmethod
    def _date(value: Any) -> date | None:
        text = str(value or "")
        try:
            return date.fromisoformat(text[:10]) if len(text) >= 10 else None
        except ValueError:
            return None
