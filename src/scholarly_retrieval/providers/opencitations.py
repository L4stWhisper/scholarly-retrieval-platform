"""OpenCitations Meta and Index v2 adapter for open citation edges."""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Any
from urllib.parse import quote

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
from ..normalization import normalize_doi
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ProviderOperationError, ScholarlyProvider


class OpenCitationsProvider(ScholarlyProvider):
    """Resolve metadata and retrieve explicit DOI/PMID citation relations."""

    name = "opencitations"
    capabilities = ProviderCapabilities(
        keyword_search=False,
        advanced_search=False,
        resolve_id=True,
        references="list",
        citations="list",
        pagination={"references": "none", "citations": "none"},
        access_tier="public_token_recommended",
        credential_variables=["OPENCITATIONS_ACCESS_TOKEN"],
        terms_url="https://api.opencitations.net/index/v2",
        redistribution_policy="CC0_citation_data_subject_to_opencitations_terms",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        access_token: str | None = None,
        store: SQLiteStore | None = None,
        max_relations: int | None = None,
    ) -> None:
        headers = {"User-Agent": "scholarly-retrieval-platform/0.1"}
        token = access_token or os.getenv("OPENCITATIONS_ACCESS_TOKEN")
        if token:
            headers["authorization"] = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://api.opencitations.net",
            timeout=httpx.Timeout(20.0),
            headers=headers,
        )
        self._max_relations = max_relations or int(
            os.getenv("OPENCITATIONS_MAX_RELATIONS", "5000")
        )
        if self._max_relations < 1:
            raise ValueError("OPENCITATIONS_MAX_RELATIONS must be positive")
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            # The public endpoint can occasionally become unreachable. Two
            # bounded attempts keep one optional graph branch from occupying a
            # worker for several minutes while still tolerating a transient loss.
            policy=RetryPolicy(
                cache_ttl_seconds=3600,
                max_concurrency=2,
                max_attempts=2,
            ),
        )

    async def _get(self, path: str, *, operation: str) -> Any:
        response = await self._http.get(path, operation=operation)
        response.raise_for_status()
        return response.json()

    async def search(self, query: SearchQuery) -> ProviderBatch:
        return ProviderBatch(filter_execution={"text": "unsupported"})

    async def resolve(self, identifier: str) -> Paper | None:
        endpoint_id = self._endpoint_id(identifier)
        if endpoint_id is None:
            return None
        try:
            payload = await self._get(
                f"/meta/v1/metadata/{quote(endpoint_id, safe=':/')}",
                operation="resolve",
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return self._paper_from_metadata(payload[0]) if payload else None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return await self._relations(identifier, operation="references", limit=limit)

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return await self._relations(identifier, operation="citations", limit=limit)

    async def _relations(
        self,
        identifier: str,
        *,
        operation: str,
        limit: int,
    ) -> ProviderBatch:
        endpoint_id = self._endpoint_id(identifier)
        if endpoint_id is None:
            return ProviderBatch()
        count_name = "reference-count" if operation == "references" else "citation-count"
        count_payload = await self._get(
            f"/index/v2/{count_name}/{quote(endpoint_id, safe=':/')}",
            operation=operation,
        )
        total = int(count_payload[0].get("count", 0)) if count_payload else 0
        if total > self._max_relations:
            # Index v2 has no server-side pagination. Refuse an unexpectedly
            # large body instead of exhausting a worker just to return Top-N.
            raise ProviderOperationError(
                "response_too_large",
                f"OpenCitations {operation} count {total} exceeds safe fetch cap "
                f"{self._max_relations}",
            )
        if total == 0:
            return ProviderBatch(total_available=0)
        payload = await self._get(
            f"/index/v2/{operation}/{quote(endpoint_id, safe=':/')}",
            operation=operation,
        )
        selected_edges = list(payload)[:limit]
        target_field = "cited" if operation == "references" else "citing"
        target_ids = [
            self._parse_identifiers(str(edge.get(target_field) or ""))
            for edge in selected_edges
        ]
        metadata = await self._metadata_for(target_ids, operation=operation)
        papers = []
        for edge, ids in zip(selected_edges, target_ids, strict=True):
            item = next(
                (
                    candidate
                    for candidate in metadata
                    if set(self._raw_id_tokens(candidate.get("id", ""))) & set(ids)
                ),
                {},
            )
            papers.append(
                self._paper_from_metadata(
                    item,
                    fallback_ids=ids,
                    relation_context={
                        "oci": edge.get("oci"),
                        "creation": edge.get("creation"),
                        "timespan": edge.get("timespan"),
                    },
                )
            )
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=total > len(papers),
        )

    async def _metadata_for(
        self,
        identifiers: list[list[str]],
        *,
        operation: str,
    ) -> list[dict[str, Any]]:
        endpoint_ids = [self._preferred_metadata_id(values) for values in identifiers]
        endpoint_ids = [value for value in endpoint_ids if value is not None]
        metadata: list[dict[str, Any]] = []
        for start in range(0, len(endpoint_ids), 20):
            joined = "__".join(endpoint_ids[start : start + 20])
            payload = await self._get(
                f"/meta/v1/metadata/{quote(joined, safe=':/_')}",
                operation=f"{operation}_metadata",
            )
            metadata.extend(payload)
        return metadata

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _paper_from_metadata(
        self,
        item: dict[str, Any],
        *,
        fallback_ids: list[str] | None = None,
        relation_context: dict[str, Any] | None = None,
    ) -> Paper:
        ids = self._raw_id_tokens(str(item.get("id") or "")) or list(fallback_ids or [])
        primary = self._preferred_metadata_id(ids) or (ids[0] if ids else "unknown")
        source_url = self._landing_url(ids)
        provenance = Provenance(
            provider=self.name,
            source_record_id=primary,
            source_url=source_url,
        )
        title = str(item.get("title") or primary).strip()
        author_text = str(item.get("author") or "")
        authors = [
            Author(name=re.sub(r"\s+\[.*\]$", "", value).strip())
            for value in author_text.split(";")
            if value.strip()
        ]
        publication_date = self._date(item.get("pub_date"))
        venue = re.sub(r"\s+\[.*\]$", "", str(item.get("venue") or "")).strip() or None
        identifier_claims = []
        for value in ids:
            prefix, _, raw = value.partition(":")
            scheme = {
                "doi": IdentifierScheme.DOI,
                "openalex": IdentifierScheme.OPENALEX,
                "pmid": IdentifierScheme.PMID,
                "pmcid": IdentifierScheme.PMCID,
                "omid": IdentifierScheme.OMID,
            }.get(prefix)
            if scheme is None or not raw:
                continue
            if scheme == IdentifierScheme.DOI:
                normalized = normalize_doi(raw)
            elif scheme == IdentifierScheme.OPENALEX:
                normalized = raw.upper()
            else:
                normalized = raw
            if normalized:
                identifier_claims.append(
                    IdentifierClaim(scheme=scheme, value=normalized, provenance=provenance)
                )
        return Paper(
            record_id=f"opencitations:{primary}",
            title=title,
            authors=authors,
            publication_date=publication_date,
            publication_year=(
                publication_date.year
                if publication_date
                else self._year(item.get("pub_date"))
            ),
            work_type=str(item.get("type") or "").casefold() or None,
            venue=venue,
            landing_page_url=source_url,
            identifiers=identifier_claims,
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=primary,
                    source_url=source_url,
                    retrieval_context=relation_context or {},
                )
            ],
            field_provenance={
                field: [provenance]
                for field, value in {
                    "title": item.get("title"),
                    "authors": authors,
                    "publication_date": publication_date,
                    "venue": venue,
                    "work_type": item.get("type"),
                }.items()
                if value not in (None, [], "")
            },
        )

    @staticmethod
    def _endpoint_id(identifier: str) -> str | None:
        if doi := normalize_doi(identifier):
            return f"doi:{doi}"
        normalized = identifier.strip().casefold()
        for prefix in ("pmid:", "omid:"):
            if normalized.startswith(prefix) and normalized[len(prefix) :]:
                return normalized
        return None

    @staticmethod
    def _raw_id_tokens(value: str) -> list[str]:
        return [token.casefold() for token in value.split() if ":" in token]

    def _parse_identifiers(self, value: str) -> list[str]:
        return self._raw_id_tokens(value)

    @staticmethod
    def _preferred_metadata_id(values: list[str]) -> str | None:
        for prefix in ("doi:", "pmid:", "omid:"):
            if value := next((item for item in values if item.startswith(prefix)), None):
                return value
        return None

    @staticmethod
    def _landing_url(values: list[str]) -> str | None:
        if doi := next((value[4:] for value in values if value.startswith("doi:")), None):
            return f"https://doi.org/{doi}"
        if omid := next((value[5:] for value in values if value.startswith("omid:")), None):
            return f"https://w3id.org/oc/meta/{omid}"
        return None

    @staticmethod
    def _date(value: Any) -> date | None:
        text = str(value or "")
        if len(text) < 10:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None

    @staticmethod
    def _year(value: Any) -> int | None:
        match = re.match(r"(1\d{3}|2\d{3})", str(value or ""))
        return int(match.group(1)) if match else None
