"""Semantic Scholar Academic Graph adapter."""

from __future__ import annotations

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
    RelatedQuery,
    RetrievalMethod,
    SearchQuery,
    SourceRecord,
    utc_now,
)
from ..normalization import normalize_arxiv_id, normalize_doi, normalize_text
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore, make_checkpoint_key
from .base import ProviderCapabilities, ScholarlyProvider

PAPER_FIELDS = ",".join(
    [
        "paperId",
        "corpusId",
        "externalIds",
        "url",
        "title",
        "abstract",
        "venue",
        "publicationVenue",
        "year",
        "publicationDate",
        "publicationTypes",
        "authors",
        "citationCount",
        "referenceCount",
        "isOpenAccess",
        "openAccessPdf",
        "fieldsOfStudy",
    ]
)


class SemanticScholarProvider(ScholarlyProvider):
    name = "semantic_scholar"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="list",
        citations="list",
        related=True,
        fulltext=False,
        search_filter_execution={
            "text": "provider",
            "year_from": "provider",
            "year_to": "provider",
            "author": "local",
            "open_access": "provider",
        },
        pagination={"references": "next_offset", "citations": "next_offset"},
        access_tier="public_or_api_key",
        credential_variables=["SEMANTIC_SCHOLAR_API_KEY"],
        terms_url="https://www.semanticscholar.org/product/api/license",
        redistribution_policy="semantic_scholar_api_license_applies",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        key = api_key or os.getenv("SEMANTIC_SCHOLAR_API_KEY")
        headers = {"User-Agent": "scholarly-retrieval-platform/0.1"}
        if key:
            headers["x-api-key"] = key
        self._keyed = bool(key)
        # Surfaced in provider reports on HTTP 429 so users learn the cause
        # instead of assuming the retry policy is broken.
        self.throttle_hint = (
            None
            if key
            else (
                "SEMANTIC_SCHOLAR_API_KEY is not configured; anonymous traffic shares one "
                "global pool per endpoint, so even the bulk-search/batch fallbacks can be "
                "throttled; request a free key at https://www.semanticscholar.org/product/api"
            )
        )
        self._store = store
        self._client = client or httpx.AsyncClient(
            base_url="https://api.semanticscholar.org/graph/v1",
            timeout=httpx.Timeout(20.0),
            headers=headers,
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(
                cache_ttl_seconds=3600,
                # The documented introductory keyed limit is one request per
                # second. Anonymous traffic shares a pool and may be throttled
                # even below that rate, so use conservative pacing plus a real
                # bounded exponential retry window. Anonymous 429s rarely clear
                # within a request, so the anonymous window is shorter (about
                # 6 s instead of 30 s) to keep multi-source searches responsive.
                min_interval_seconds=1.1,
                max_concurrency=1,
                max_attempts=5 if key else 3,
                base_delay_seconds=2.0,
                max_delay_seconds=30.0,
                max_retry_after_seconds=120.0,
                jitter_ratio=0.25,
            ),
        )

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        operation: str = "get",
    ) -> dict[str, Any]:
        response = await self._http.get(path, params=params, operation=operation)
        response.raise_for_status()
        return response.json()

    async def search(self, query: SearchQuery) -> ProviderBatch:
        """Relevance search when keyed; bulk search anonymously or after a 429.

        Semantic Scholar throttles anonymous traffic per endpoint. The shared
        pool for ``/paper/search`` and ``/paper/{id}`` is normally exhausted,
        while ``/paper/search/bulk`` and ``/paper/batch`` usually answer. Bulk
        search has no relevance ranking, so results are requested sorted by
        citation count and the service's lexical fusion supplies relevance.
        """

        params = {"query": query.text, "fields": PAPER_FIELDS}
        if query.year_from or query.year_to:
            start = query.year_from or ""
            end = query.year_to or ""
            params["year"] = f"{start}-{end}"
        context: dict[str, Any] = {"endpoint": "paper/search", "ranking": "relevance"}
        payload: dict[str, Any] | None = None
        if self._keyed:
            keyed_params = {**params, "limit": str(query.limit)}
            if query.open_access is not None:
                keyed_params["openAccessPdf"] = str(query.open_access).lower()
            try:
                payload = await self._get("/paper/search", keyed_params, operation="search")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429:
                    raise
                context["fallback_reason"] = "http_429"
        bulk = payload is None
        if bulk:
            bulk_params = {**params, "sort": "citationCount:desc"}
            # Bulk search only offers a presence flag for open access.
            if query.open_access:
                bulk_params["openAccessPdf"] = ""
            payload = await self._get("/paper/search/bulk", bulk_params, operation="search")
            context.update({"endpoint": "paper/search/bulk", "ranking": "citation_count_desc"})
        items = list(payload.get("data", []))
        if bulk:
            # The bulk endpoint ignores ``limit`` and returns up to 1000 rows.
            items = items[: query.limit]
        papers = [
            self._paper_from_data(item, rank=index + 1, retrieval_context=dict(context))
            for index, item in enumerate(items)
        ]
        execution = {"text": "provider"}
        if query.year_from is not None:
            execution["year_from"] = "provider"
        if query.year_to is not None:
            execution["year_to"] = "provider"
        if query.open_access is not None:
            if bulk and query.open_access is False:
                papers = [paper for paper in papers if paper.open_access is False]
                execution["open_access"] = "local"
            else:
                execution["open_access"] = "provider"
        if query.author:
            wanted = normalize_text(query.author).casefold()
            papers = [
                paper
                for paper in papers
                if any(wanted in normalize_text(author.name).casefold() for author in paper.authors)
            ]
            execution["author"] = "local"
        total = payload.get("total")
        continuation = payload.get("token") if bulk else payload.get("next")
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(total is not None and total > query.limit),
            next_cursor=str(continuation) if continuation is not None else None,
            filter_execution=execution,
            context=context,
        )

    async def resolve(self, identifier: str) -> Paper | None:
        """Single-paper lookup when keyed; batch lookup anonymously or after a 429."""

        paper_id = self._paper_identifier(identifier)
        context: dict[str, Any] = {"endpoint": "paper/batch"}
        if self._keyed:
            try:
                payload = await self._get(
                    f"/paper/{quote(paper_id, safe='')}",
                    {"fields": PAPER_FIELDS},
                    operation="resolve",
                )
                return self._paper_from_data(payload, retrieval_context={"endpoint": "paper"})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return None
                if exc.response.status_code != 429:
                    raise
                context["fallback_reason"] = "http_429"
        response = await self._http.post_json(
            "/paper/batch",
            params={"fields": PAPER_FIELDS},
            json_body={"ids": [paper_id]},
            operation="resolve",
        )
        response.raise_for_status()
        items = response.json()
        # Unknown identifiers come back as null entries, not as HTTP 404.
        item = items[0] if isinstance(items, list) and items else None
        if not isinstance(item, dict) or not item.get("paperId"):
            return None
        return self._paper_from_data(item, retrieval_context=context)

    async def related(self, query: RelatedQuery) -> ProviderBatch:
        if not query.positive_identifiers:
            return ProviderBatch(filter_execution={"text": "unsupported"})

        positive_ids = [
            self._paper_identifier(identifier) for identifier in query.positive_identifiers
        ]
        negative_ids = [
            self._paper_identifier(identifier) for identifier in query.negative_identifiers
        ]
        response = await self._http.post_json(
            "https://api.semanticscholar.org/recommendations/v1/papers",
            params={"fields": PAPER_FIELDS, "limit": str(min(query.limit, 500))},
            json_body={
                "positivePaperIds": positive_ids,
                "negativePaperIds": negative_ids,
            },
            operation="related",
        )
        response.raise_for_status()
        payload = response.json()
        context = {
            "positive_identifiers": query.positive_identifiers,
            "negative_identifiers": query.negative_identifiers,
            "recommendation_mode": "multi_example",
        }
        papers = []
        for rank, item in enumerate(payload.get("recommendedPapers", []), start=1):
            paper = self._paper_from_data(
                item,
                rank=rank,
                retrieval_method=RetrievalMethod.SEMANTIC_SCHOLAR_RECOMMENDATION,
                retrieval_context=context,
            )
            if self._matches_related_filters(paper, query):
                papers.append(paper)

        execution = {"positive_identifiers": "provider"}
        if query.text is not None:
            execution["text"] = "unsupported"
        if query.negative_identifiers:
            execution["negative_identifiers"] = "provider"
        for field, enabled in {
            "year_from": query.year_from is not None,
            "year_to": query.year_to is not None,
            "open_access": query.open_access is not None,
            "include_work_types": bool(query.include_work_types),
        }.items():
            if enabled:
                execution[field] = "local"
        return ProviderBatch(
            papers=papers,
            truncated=len(papers) > query.limit,
            filter_execution=execution,
        )

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return await self._relations(identifier, relation="references", limit=limit)

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return await self._relations(identifier, relation="citations", limit=limit)

    async def _relations(self, identifier: str, *, relation: str, limit: int) -> ProviderBatch:
        paper_id = self._paper_identifier(identifier)
        offset = 0
        papers: list[Paper] = []
        next_offset: int | None = None
        total: int | None = None
        resumed = False
        checkpoint_key = make_checkpoint_key(self.name, relation, paper_id, limit)
        if self._store:
            checkpoint = self._store.load_checkpoint(checkpoint_key)
            if checkpoint:
                try:
                    if (
                        checkpoint.provider != self.name
                        or checkpoint.operation != relation
                        or checkpoint.state.get("version") != 1
                    ):
                        raise ValueError("stale Semantic Scholar checkpoint")
                    papers = [Paper.model_validate(item) for item in checkpoint.state["papers"]]
                    total = checkpoint.state.get("total")
                    next_offset = int(checkpoint.cursor) if checkpoint.cursor is not None else None
                    offset = next_offset if next_offset is not None else 0
                    resumed = True
                except (KeyError, TypeError, ValueError):
                    self._store.delete_checkpoint(checkpoint_key)
                    offset = 0
                    papers = []
                    total = None
                    next_offset = None
        while len(papers) < limit:
            # A checkpoint with no continuation was written immediately before a
            # crash at normal completion; do not repeat the terminal page.
            if resumed and next_offset is None:
                break
            resumed = False
            page_size = min(100, limit - len(papers))
            payload = await self._get(
                f"/paper/{quote(paper_id, safe='')}/{relation}",
                {"fields": PAPER_FIELDS, "offset": str(offset), "limit": str(page_size)},
                operation=relation,
            )
            key = "citedPaper" if relation == "references" else "citingPaper"
            items = [item.get(key) for item in payload.get("data", [])]
            papers.extend(
                self._paper_from_data(item) for item in items if item and item.get("paperId")
            )
            total = payload.get("total", total)
            next_offset = payload.get("next")
            if self._store:
                self._store.save_checkpoint(
                    checkpoint_key,
                    provider=self.name,
                    operation=relation,
                    cursor=str(next_offset) if next_offset is not None else None,
                    state={
                        "version": 1,
                        "total": total,
                        "papers": [paper.model_dump(mode="json") for paper in papers],
                    },
                )
            if next_offset is None or not items:
                break
            # S2 returns an opaque next offset for this result set. It may differ
            # from len(data), so resume from the supplied value exactly.
            offset = int(next_offset)
        if self._store:
            self._store.delete_checkpoint(checkpoint_key)
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(next_offset is not None or (total is not None and total > len(papers))),
            next_cursor=str(next_offset) if next_offset is not None else None,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _paper_identifier(value: str) -> str:
        raw = value.strip()
        doi = normalize_doi(raw)
        if doi:
            return f"DOI:{doi}"
        arxiv = normalize_arxiv_id(raw, keep_version=False)
        if arxiv:
            return f"ARXIV:{arxiv}"
        if re.fullmatch(r"(?:PMID:)?\d+", raw, re.I):
            return raw if raw.upper().startswith("PMID:") else f"PMID:{raw}"
        return raw

    def _paper_from_data(
        self,
        data: dict[str, Any],
        rank: int | None = None,
        *,
        retrieval_method: RetrievalMethod | None = None,
        retrieval_context: dict[str, Any] | None = None,
    ) -> Paper:
        retrieved_at = utc_now()
        paper_id = data["paperId"]
        source_url = data.get("url") or f"https://www.semanticscholar.org/paper/{paper_id}"
        provenance = Provenance(
            provider=self.name,
            source_record_id=paper_id,
            source_url=source_url,
            retrieved_at=retrieved_at,
        )
        identifiers = [
            IdentifierClaim(
                scheme=IdentifierScheme.SEMANTIC_SCHOLAR,
                value=paper_id,
                provenance=provenance,
            )
        ]
        external_value = data.get("externalIds") or {}
        external = external_value if isinstance(external_value, dict) else {}
        mappings = {
            "DOI": (IdentifierScheme.DOI, normalize_doi),
            "ArXiv": (
                IdentifierScheme.ARXIV,
                lambda value: normalize_arxiv_id(value, keep_version=False),
            ),
            "PubMed": (IdentifierScheme.PMID, lambda value: str(value)),
            "PubMedCentral": (IdentifierScheme.PMCID, lambda value: str(value)),
        }
        for key, (scheme, normalizer) in mappings.items():
            if external.get(key):
                normalized = normalizer(external[key])
                if normalized:
                    identifiers.append(
                        IdentifierClaim(scheme=scheme, value=normalized, provenance=provenance)
                    )
        authors = [
            Author(
                name=author["name"],
                provider_ids={"semantic_scholar": author["authorId"]}
                if author.get("authorId")
                else {},
            )
            for author in data.get("authors") or []
            if author.get("name")
        ]
        publication_venue_value = data.get("publicationVenue")
        if isinstance(publication_venue_value, dict):
            publication_venue = publication_venue_value.get("name")
        elif isinstance(publication_venue_value, str):
            # Recommendations has returned a plain string here in production,
            # while Academic Graph commonly returns a structured object.
            publication_venue = publication_venue_value
        else:
            publication_venue = None
        publication_types = data.get("publicationTypes") or []
        open_pdf_value = data.get("openAccessPdf") or {}
        open_pdf = open_pdf_value if isinstance(open_pdf_value, dict) else {}
        return Paper(
            record_id=f"semantic_scholar:{paper_id}",
            title=data.get("title") or f"Untitled {paper_id}",
            abstract=data.get("abstract"),
            authors=authors,
            publication_date=self._parse_date(data.get("publicationDate")),
            publication_year=data.get("year"),
            work_type=publication_types[0] if publication_types else None,
            venue=publication_venue or data.get("venue"),
            fields_of_study=list(data.get("fieldsOfStudy") or []),
            open_access=data.get("isOpenAccess"),
            landing_page_url=source_url,
            pdf_url=open_pdf.get("url"),
            identifiers=identifiers,
            citation_counts=[
                CitationCountClaim(
                    provider=self.name,
                    count=int(data.get("citationCount") or 0),
                    source_record_id=paper_id,
                    retrieved_at=retrieved_at,
                )
            ],
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=paper_id,
                    source_url=source_url,
                    provider_rank=rank,
                    retrieval_method=retrieval_method,
                    retrieval_context=retrieval_context or {},
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
    def _parse_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _matches_related_filters(paper: Paper, query: RelatedQuery) -> bool:
        if query.year_from is not None and (
            paper.publication_year is None or paper.publication_year < query.year_from
        ):
            return False
        if query.year_to is not None and (
            paper.publication_year is None or paper.publication_year > query.year_to
        ):
            return False
        if query.open_access is not None and paper.open_access != query.open_access:
            return False
        if query.include_work_types and (
            paper.work_type is None
            or " ".join(paper.work_type.split()).casefold() not in query.include_work_types
        ):
            return False
        return True
